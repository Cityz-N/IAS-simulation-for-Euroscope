// IasCore.h - wind file reader, interpolation and airspeed physics for the IAS estimator.
// No Windows / EuroScope dependency: this file is also compiled and tested on its own.
#pragma once
#include <cmath>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

namespace ias {

static const double KT_PER_MS = 1.943844492;
static const double A0_KT = 661.4788;        // ISA sea level speed of sound, kt
static const double P0_HPA = 1013.25;

// ISA static pressure for a PRESSURE altitude in feet (what Mode C / VATSIM altitude is).
inline double IsaPressureHpa(double ftPressAlt)
{
    double h = ftPressAlt * 0.3048;
    if (h <= 11000.0) return P0_HPA * std::pow(1.0 - 2.25577e-5 * h, 5.25588);
    return 226.3206 * std::exp(-1.576883e-4 * (h - 11000.0));
}

struct Result {
    bool ok = false;
    double tas = 0, cas = 0, mach = 0;          // kt, kt, -
    double windDir = 0, windSpd = 0;            // wind FROM (deg true), kt
    double headwind = 0;                        // kt, positive = headwind
    double tempK = 0;
    int flags = 0;                              // 1 = forecast older than its last frame
};

class Wind {
public:
    bool valid = false;
    int64_t runTime = 0;
    int nFrames = 0, nLev = 0, nLat = 0, nLon = 0, stepH = 0;
    float lat0 = 0, lon0 = 0, dlat = 0, dlon = 0;
    std::vector<float> lev;                     // hPa ascending
    std::vector<int32_t> hours;
    std::vector<int16_t> data;                  // [frame][lev][var 0=u 1=v 2=T][lat][lon]

    bool Load(const unsigned char* b, size_t n, std::string* err = NULL)
    {
        valid = false;
        const size_t HDR = 52;
        if (n < HDR || std::memcmp(b, "IASW", 4) != 0) { if (err) *err = "not a wind file"; return false; }
        int32_t ver; std::memcpy(&ver, b + 4, 4);
        if (ver != 1) { if (err) *err = "unsupported version"; return false; }
        std::memcpy(&runTime, b + 8, 8);
        std::memcpy(&nFrames, b + 16, 4); std::memcpy(&nLev, b + 20, 4);
        std::memcpy(&nLat, b + 24, 4);    std::memcpy(&nLon, b + 28, 4);
        std::memcpy(&lat0, b + 32, 4);    std::memcpy(&lon0, b + 36, 4);
        std::memcpy(&dlat, b + 40, 4);    std::memcpy(&dlon, b + 44, 4);
        std::memcpy(&stepH, b + 48, 4);
        if (nFrames < 1 || nFrames > 64 || nLev < 2 || nLev > 64 || nLat < 2 || nLat > 2000 || nLon < 2 || nLon > 2000 ||
            dlat <= 0 || dlon <= 0) { if (err) *err = "bad header"; return false; }
        size_t cnt = (size_t)nFrames * nLev * 3 * nLat * nLon;
        size_t need = HDR + 4 * (size_t)nLev + 4 * (size_t)nFrames + 2 * cnt;
        if (n != need) { if (err) *err = "wrong size"; return false; }
        size_t o = HDR;
        lev.resize(nLev);   std::memcpy(lev.data(), b + o, 4 * (size_t)nLev);    o += 4 * (size_t)nLev;
        hours.resize(nFrames); std::memcpy(hours.data(), b + o, 4 * (size_t)nFrames); o += 4 * (size_t)nFrames;
        data.resize(cnt);   std::memcpy(data.data(), b + o, 2 * cnt);
        for (int i = 1; i < nLev; i++) if (!(lev[i] > lev[i - 1])) { if (err) *err = "levels not ascending"; return false; }
        for (int i = 1; i < nFrames; i++) if (!(hours[i] > hours[i - 1])) { if (err) *err = "frames not ascending"; return false; }
        valid = true;
        return true;
    }

    // u, v in m/s (blowing TOWARDS east / north), T in K, interpolated in space, ln(pressure) and time.
    bool Sample(double nowUnix, double lat, double lon, double pHpa, double& u, double& v, double& T, int& flags) const
    {
        if (!valid) return false;
        double fx = (lon - lon0) / dlon, fy = (lat - lat0) / dlat;
        if (!(fx >= 0 && fy >= 0 && fx <= nLon - 1 && fy <= nLat - 1)) return false;
        int i0 = (int)fx; if (i0 > nLon - 2) i0 = nLon - 2;
        int j0 = (int)fy; if (j0 > nLat - 2) j0 = nLat - 2;
        double ax = fx - i0, ay = fy - j0;

        int l0, l1; double wl = 0;
        if (pHpa <= lev[0]) { l0 = l1 = 0; }
        else if (pHpa >= lev[nLev - 1]) { l0 = l1 = nLev - 1; }
        else {
            l1 = 1;
            while (l1 < nLev - 1 && lev[l1] < pHpa) l1++;
            l0 = l1 - 1;
            wl = (std::log(pHpa) - std::log((double)lev[l0])) / (std::log((double)lev[l1]) - std::log((double)lev[l0]));
        }

        double fh = (nowUnix - (double)runTime) / 3600.0;
        int f0, f1; double wf = 0;
        flags = 0;
        if (fh <= hours[0]) { f0 = f1 = 0; }
        else if (fh >= hours[nFrames - 1]) { f0 = f1 = nFrames - 1; if (fh > hours[nFrames - 1] + 1.0) flags |= 1; }
        else {
            f1 = 1;
            while (f1 < nFrames - 1 && hours[f1] < fh) f1++;
            f0 = f1 - 1;
            wf = (fh - hours[f0]) / (double)(hours[f1] - hours[f0]);
        }

        double out[3];
        for (int var = 0; var < 3; var++) {
            double acc = 0;
            for (int a = 0; a < 2; a++) {
                int f = a ? f1 : f0; double wa = a ? wf : 1.0 - wf;
                if (wa == 0) continue;
                for (int b = 0; b < 2; b++) {
                    int l = b ? l1 : l0; double wb = b ? wl : 1.0 - wl;
                    if (wb == 0) continue;
                    acc += wa * wb * Bilinear(f, l, var, j0, i0, ax, ay);
                }
            }
            out[var] = acc;
        }
        u = out[0]; v = out[1]; T = out[2];
        return true;
    }

private:
    double At(int f, int l, int var, int j, int i) const
    {
        size_t idx = ((((size_t)f * nLev + l) * 3 + var) * nLat + j) * nLon + i;
        return data[idx] * 0.01;
    }
    double Bilinear(int f, int l, int var, int j0, int i0, double ax, double ay) const
    {
        double a = At(f, l, var, j0, i0), b = At(f, l, var, j0, i0 + 1);
        double c = At(f, l, var, j0 + 1, i0), d = At(f, l, var, j0 + 1, i0 + 1);
        return (a * (1 - ax) + b * ax) * (1 - ay) + (c * (1 - ax) + d * ax) * ay;
    }
};

// Estimate true airspeed, Mach and calibrated airspeed (~ indicated) from what the network shows.
//   gsKt, trackDeg (TRUE track), pressAltFt (pressure altitude, standard 1013.25 setting).
inline Result Estimate(const Wind& w, double nowUnix, double lat, double lon, double pressAltFt, double gsKt, double trackDeg)
{
    Result r;
    double p = IsaPressureHpa(pressAltFt);
    double u, v, T; int fl = 0;
    if (!w.Sample(nowUnix, lat, lon, p, u, v, T, fl)) return r;
    double uk = u * KT_PER_MS, vk = v * KT_PER_MS;
    double th = trackDeg * 3.14159265358979323846 / 180.0;
    double gx = gsKt * std::sin(th), gy = gsKt * std::cos(th);
    double tas = std::sqrt((gx - uk) * (gx - uk) + (gy - vk) * (gy - vk));
    double a = 38.967854 * std::sqrt(T);                      // speed of sound, kt
    double M = tas / a;
    double qcOverP = std::pow(1.0 + 0.2 * M * M, 3.5) - 1.0;
    double qc = p * qcOverP;
    double cas = A0_KT * std::sqrt(5.0 * (std::pow(qc / P0_HPA + 1.0, 2.0 / 7.0) - 1.0));
    double dir = std::atan2(-uk, -vk) * 180.0 / 3.14159265358979323846;
    if (dir < 0) dir += 360.0;
    r.ok = true; r.tas = tas; r.cas = cas; r.mach = M;
    r.windDir = dir; r.windSpd = std::sqrt(uk * uk + vk * vk);
    r.headwind = -(uk * std::sin(th) + vk * std::cos(th));
    r.tempK = T; r.flags = fl;
    return r;
}

} // namespace ias
