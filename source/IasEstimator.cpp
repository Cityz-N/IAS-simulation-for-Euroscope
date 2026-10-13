// IasEstimator - estimated IAS / Mach / wind for VATSIM aircraft, as EuroScope tag items for lists and tags.
// Method: ground speed + track (from the last positions) - forecast wind (ARPEGE, via the GitHub Pages relay)
//         = true airspeed; Mach from the forecast temperature; IAS (calibrated) from pressure altitude + Mach.
// Build: MFC DLL, x86, Release, "Use MFC in a Static Library", Runtime Library /MT, no precompiled headers.
// Files: IasEstimator.cpp + IasCore.h (same folder). Output: IasEstimator.dll next to IasEstimator.ini.
#include <afxwin.h>          // MFC (EuroScope's SDK header relies on it)
#include <winhttp.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cmath>
#include <cstdio>
#include <ctime>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "EuroScopePlugIn.h"
#include "IasCore.h"

#pragma comment(lib, "winhttp.lib")

using namespace EuroScopePlugIn;

#define IAS_VERSION "0.1.0"

class CIasApp : public CWinApp {
public:
    virtual BOOL InitInstance() { CWinApp::InitInstance(); return TRUE; }
};
CIasApp theApp;

enum {
    TAG_IAS = 1,        // estimated IAS, kt
    TAG_MACH = 2,       // estimated Mach
    TAG_SPEED = 3,      // IAS below the Mach threshold, Mach above it
    TAG_WIND = 4,       // forecast wind at the aircraft: 270/045
    TAG_HEADWIND = 5,   // H25 = 25 kt headwind, T12 = 12 kt tailwind
    TAG_TAS = 6         // estimated TAS, kt
};

// ----------------------------------------------------------------------------
static std::string ModuleDir()
{
    HMODULE h = NULL;
    GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       (LPCSTR)&ModuleDir, &h);
    char p[MAX_PATH] = { 0 };
    GetModuleFileNameA(h, p, MAX_PATH);
    std::string s(p);
    size_t k = s.find_last_of("\\/");
    return (k == std::string::npos) ? std::string(".") : s.substr(0, k);
}

static std::wstring Widen(const std::string& s) { return std::wstring(s.begin(), s.end()); }

// HTTP download with WinHTTP (built into Windows)
static bool HttpGet(const std::string& url8, std::vector<unsigned char>& out, std::string& err)
{
    out.clear();
    std::wstring url = Widen(url8);
    wchar_t host[256] = {};
    wchar_t path[2048] = {};
    URL_COMPONENTS uc = {};
    uc.dwStructSize = sizeof(uc);
    uc.lpszHostName = host;     uc.dwHostNameLength = 256;
    uc.lpszUrlPath = path;      uc.dwUrlPathLength = 2048;
    if (!WinHttpCrackUrl(url.c_str(), 0, 0, &uc)) { err = "bad URL"; return false; }

    HINTERNET ses = WinHttpOpen(L"EuroScope-IasEstimator/0.1", WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                                WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!ses) { err = "WinHttpOpen failed"; return false; }
    WinHttpSetTimeouts(ses, 8000, 8000, 15000, 30000);

    bool ok = false;
    err = "connection failed";
    HINTERNET con = WinHttpConnect(ses, host, uc.nPort, 0);
    if (con) {
        DWORD flags = (uc.nScheme == INTERNET_SCHEME_HTTPS) ? WINHTTP_FLAG_SECURE : 0;
        HINTERNET req = WinHttpOpenRequest(con, L"GET", path, NULL, WINHTTP_NO_REFERER,
                                           WINHTTP_DEFAULT_ACCEPT_TYPES, flags);
        if (req) {
            if (WinHttpSendRequest(req, WINHTTP_NO_ADDITIONAL_HEADERS, 0, WINHTTP_NO_REQUEST_DATA, 0, 0, 0) &&
                WinHttpReceiveResponse(req, NULL)) {
                DWORD status = 0, sz = sizeof(status);
                WinHttpQueryHeaders(req, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                                    WINHTTP_HEADER_NAME_BY_INDEX, &status, &sz, WINHTTP_NO_HEADER_INDEX);
                if (status == 200) {
                    ok = true;
                    for (;;) {
                        DWORD avail = 0;
                        if (!WinHttpQueryDataAvailable(req, &avail)) { ok = false; err = "read error"; break; }
                        if (avail == 0) break;
                        size_t old = out.size();
                        out.resize(old + avail);
                        DWORD got = 0;
                        if (!WinHttpReadData(req, &out[old], avail, &got)) { ok = false; err = "read error"; break; }
                        out.resize(old + got);
                        if (out.size() > 16 * 1024 * 1024) { ok = false; err = "file too big"; break; }
                    }
                } else {
                    err = "HTTP " + std::to_string((long long)status);
                }
            }
            WinHttpCloseHandle(req);
        }
        WinHttpCloseHandle(con);
    }
    WinHttpCloseHandle(ses);
    return ok;
}

// ----------------------------------------------------------------------------
class CIasPlugIn : public CPlugIn {
public:
    CIasPlugIn() : CPlugIn(COMPATIBILITY_CODE, "IasEstimator", IAS_VERSION, "IAS estimator", "free to use")
    {
        stop = false; reloadNow = false; lastFailed = false;
        LoadIni();
        RegisterTagItemType("Est. IAS (kt)", TAG_IAS);
        RegisterTagItemType("Est. Mach", TAG_MACH);
        RegisterTagItemType("Est. IAS or Mach", TAG_SPEED);
        RegisterTagItemType("Forecast wind", TAG_WIND);
        RegisterTagItemType("Head/tailwind (kt)", TAG_HEADWIND);
        RegisterTagItemType("Est. TAS (kt)", TAG_TAS);

        // cached copy from the last session, so that it works at once
        std::vector<unsigned char> b;
        if (ReadWhole(CachePath(), b)) {
            std::shared_ptr<ias::Wind> w(new ias::Wind());
            std::string e;
            if (w->Load(b.data(), b.size(), &e)) SetWind(w);
        }
        Msg(std::string("IasEstimator ") + IAS_VERSION + " loaded. " + (GetWind() ? "Cached wind data found." : "Waiting for wind data..."));
        if (url.find("YOURNAME") != std::string::npos)
            Msg("Edit IasEstimator.ini: Url= must point to YOUR wind.bin (see README).");
        worker = std::thread(&CIasPlugIn::WorkerMain, this);
    }

    ~CIasPlugIn()
    {
        { std::lock_guard<std::mutex> lk(cvm); stop = true; }
        cv.notify_all();
        if (worker.joinable()) worker.join();
    }

    // ---- messages (worker thread queues, main thread shows) --------------
    void Msg(const std::string& m)
    {
        DisplayUserMessage("IAS", "IAS", m.c_str(), true, true, true, false, false);
    }
    void Queue(const std::string& m)
    {
        std::lock_guard<std::mutex> lk(qm);
        q.push_back(m);
    }
    virtual void OnTimer(int Counter)
    {
        (void)Counter;
        std::deque<std::string> local;
        { std::lock_guard<std::mutex> lk(qm); local.swap(q); }
        for (size_t i = 0; i < local.size(); i++) Msg(local[i]);
    }

    // ---- tag items -------------------------------------------------------
    virtual void OnGetTagItem(CFlightPlan FlightPlan, CRadarTarget RadarTarget, int ItemCode, int TagData,
                              char sItemString[16], int* pColorCode, COLORREF* pRGB, double* pFontSize)
    {
        (void)FlightPlan; (void)TagData; (void)pFontSize;
        strcpy_s(sItemString, 16, "---");
        std::shared_ptr<const ias::Wind> w = GetWind();
        if (!w) { strcpy_s(sItemString, 16, "n/a"); return; }
        ias::Result r;
        if (!Compute(RadarTarget, *w, r)) return;

        char buf[32];
        switch (ItemCode) {
        case TAG_IAS:  snprintf(buf, sizeof(buf), "%d", Rnd(r.cas)); break;
        case TAG_TAS:  snprintf(buf, sizeof(buf), "%d", Rnd(r.tas)); break;
        case TAG_MACH: snprintf(buf, sizeof(buf), "M.%02d", (int)floor(r.mach * 100.0 + 0.5)); break;
        case TAG_SPEED:
            if (r.mach >= machAbove) snprintf(buf, sizeof(buf), "M.%02d", (int)floor(r.mach * 100.0 + 0.5));
            else snprintf(buf, sizeof(buf), "%d", Rnd(r.cas));
            break;
        case TAG_WIND: {
            int d = (int)floor(r.windDir + 0.5); if (d == 0) d = 360; if (d > 360) d -= 360;
            snprintf(buf, sizeof(buf), "%03d/%03d", d, (int)floor(r.windSpd + 0.5));
            break; }
        case TAG_HEADWIND: {
            int hw = (int)floor(r.headwind + 0.5);
            snprintf(buf, sizeof(buf), "%c%d", hw >= 0 ? 'H' : 'T', hw >= 0 ? hw : -hw);
            break; }
        default: return;
        }
        buf[15] = 0;
        strcpy_s(sItemString, 16, buf);
        if (r.flags & 1) { *pColorCode = TAG_COLOR_RGB_DEFINED; *pRGB = RGB(255, 165, 0); }   // forecast out of date
    }

    // ---- commands --------------------------------------------------------
    virtual bool OnCompileCommand(const char* sCommandLine)
    {
        std::string c(sCommandLine);
        if (c.size() < 4 || (c.compare(0, 4, ".ias") != 0 && c.compare(0, 4, ".IAS") != 0)) return false;
        size_t p = 4;
        while (p < c.size() && c[p] == ' ') p++;
        std::string rest = c.substr(p);
        std::string cmd = rest, arg;
        size_t sp = rest.find(' ');
        if (sp != std::string::npos) { cmd = rest.substr(0, sp); arg = rest.substr(sp + 1); while (!arg.empty() && arg[0] == ' ') arg.erase(0, 1); }
        for (size_t i = 0; i < cmd.size(); i++) cmd[i] = (char)tolower((unsigned char)cmd[i]);

        if (cmd == "status" || cmd.empty()) {
            std::shared_ptr<const ias::Wind> w = GetWind();
            if (!w) { Msg("No wind data loaded yet. URL: " + url); return true; }
            char t[200];
            double now = (double)time(NULL);
            snprintf(t, sizeof(t), "Wind data: ARPEGE run %s UTC (+%.1f h), %d frames of %d h, %d levels, window %.0f..%.0fN %.0f..%.0fE. Round %d kt, Mach above %.2f.",
                     FmtTime((time_t)w->runTime).c_str(), (now - (double)w->runTime) / 3600.0, w->nFrames, w->stepH, w->nLev,
                     w->lat0, w->lat0 + (w->nLat - 1) * w->dlat, w->lon0, w->lon0 + (w->nLon - 1) * w->dlon, roundKt, machAbove);
            Msg(t);
            return true;
        }
        if (cmd == "reload") { { std::lock_guard<std::mutex> lk(cvm); reloadNow = true; } cv.notify_all(); Msg("Downloading wind data..."); return true; }
        if (cmd == "url" && !arg.empty()) { { std::lock_guard<std::mutex> lk(cvm); url = arg; reloadNow = true; } cv.notify_all(); Msg("URL set for this session (edit the ini to keep it)."); return true; }
        if (cmd == "round") { int v = atoi(arg.c_str()); if (v >= 1 && v <= 20) { roundKt = v; Msg("Rounding set to " + std::to_string((long long)v) + " kt."); } return true; }
        if (cmd == "mach") { double v = atof(arg.c_str()); if (v >= 0.3 && v <= 1.0) { machAbove = v; Msg("Mach shown above M" + arg); } return true; }
        if (cmd == "test" && !arg.empty()) { TestTarget(arg); return true; }
        Msg("Commands: .ias status | reload | url <URL> | round <kt> | mach <0.60> | test <CALLSIGN>");
        return true;
    }

private:
    // ---- data ------------------------------------------------------------
    std::mutex wm;
    std::shared_ptr<const ias::Wind> wind;
    std::shared_ptr<const ias::Wind> GetWind() { std::lock_guard<std::mutex> lk(wm); return wind; }
    void SetWind(std::shared_ptr<const ias::Wind> w) { std::lock_guard<std::mutex> lk(wm); wind = w; }

    std::string url;
    int refreshMin, roundKt, minGs;
    double machAbove;

    std::thread worker;
    std::mutex cvm;
    std::condition_variable cv;
    bool stop, reloadNow, lastFailed;
    std::mutex qm;
    std::deque<std::string> q;

    int Rnd(double v) const { int r = roundKt < 1 ? 1 : roundKt; return (int)floor(v / r + 0.5) * r; }

    static std::string FmtTime(time_t t)
    {
        struct tm g;
        gmtime_s(&g, &t);
        char b[40];
        snprintf(b, sizeof(b), "%04d-%02d-%02d %02dZ", g.tm_year + 1900, g.tm_mon + 1, g.tm_mday, g.tm_hour);
        return b;
    }

    std::string CachePath() { return ModuleDir() + "\\IasEstimator_wind.cache"; }

    static bool ReadWhole(const std::string& path, std::vector<unsigned char>& out)
    {
        FILE* f = fopen(path.c_str(), "rb");
        if (!f) return false;
        out.clear();
        unsigned char tmp[65536];
        size_t n;
        while ((n = fread(tmp, 1, sizeof(tmp), f)) > 0) out.insert(out.end(), tmp, tmp + n);
        fclose(f);
        return !out.empty();
    }

    void LoadIni()
    {
        std::string ini = ModuleDir() + "\\IasEstimator.ini";
        char b[1024];
        GetPrivateProfileStringA("IAS", "Url", "https://YOURNAME.github.io/IAS-simulation-for-Euroscope/wind.bin", b, sizeof(b), ini.c_str());
        url = b;
        refreshMin = (int)GetPrivateProfileIntA("IAS", "RefreshMinutes", 30, ini.c_str());
        if (refreshMin < 5) refreshMin = 5;
        roundKt = (int)GetPrivateProfileIntA("IAS", "RoundKt", 5, ini.c_str());
        if (roundKt < 1) roundKt = 1;
        minGs = (int)GetPrivateProfileIntA("IAS", "MinGroundSpeed", 40, ini.c_str());
        GetPrivateProfileStringA("IAS", "MachAbove", "0.60", b, sizeof(b), ini.c_str());
        machAbove = atof(b);
        if (machAbove < 0.3 || machAbove > 1.0) machAbove = 0.60;
    }

    // ---- download thread -------------------------------------------------
    void WorkerMain()
    {
        std::unique_lock<std::mutex> lk(cvm);
        while (!stop) {
            std::string u = url;
            lk.unlock();
            FetchOnce(u);
            lk.lock();
            reloadNow = false;
            int waitMin = lastFailed ? 3 : refreshMin;
            cv.wait_for(lk, std::chrono::minutes(waitMin), [this] { return stop || reloadNow; });
        }
    }

    void FetchOnce(const std::string& u)
    {
        std::vector<unsigned char> b;
        std::string err;
        if (!HttpGet(u, b, err)) {
            if (!lastFailed) Queue("Wind download failed (" + err + "). Will retry; the previous data stay in use.");
            lastFailed = true;
            return;
        }
        std::shared_ptr<ias::Wind> w(new ias::Wind());
        if (!w->Load(b.data(), b.size(), &err)) {
            if (!lastFailed) Queue("Wind file rejected (" + err + ").");
            lastFailed = true;
            return;
        }
        lastFailed = false;
        std::shared_ptr<const ias::Wind> old = GetWind();
        bool isNew = !old || old->runTime != w->runTime;
        SetWind(w);
        FILE* f = fopen(CachePath().c_str(), "wb");
        if (f) { fwrite(b.data(), 1, b.size(), f); fclose(f); }
        if (isNew) Queue("Wind data updated: ARPEGE run " + FmtTime((time_t)w->runTime) + " UTC.");
    }

    // ---- per aircraft ----------------------------------------------------
    static double BearingDeg(double lat1, double lon1, double lat2, double lon2, double& distNm)
    {
        double k = cos(lat1 * 3.14159265358979 / 180.0);
        double dy = (lat2 - lat1) * 60.0;
        double dx = (lon2 - lon1) * 60.0 * k;
        distNm = sqrt(dx * dx + dy * dy);
        double b = atan2(dx, dy) * 180.0 / 3.14159265358979;
        return b < 0 ? b + 360.0 : b;
    }

    bool Compute(CRadarTarget& rt, const ias::Wind& w, ias::Result& r, double* trkOut = NULL, double* gsOut = NULL, int* altOut = NULL)
    {
        if (!rt.IsValid()) return false;
        CRadarTargetPositionData pd = rt.GetPosition();
        if (!pd.IsValid()) return false;
        int gs = pd.GetReportedGS();
        int alt = pd.GetPressureAltitude();
        if (gs < minGs || alt < -1000 || alt > 60000) return false;

        // true track from the last positions (the earliest one that is at least 0.25 NM away)
        double lat = pd.GetPosition().m_Latitude, lon = pd.GetPosition().m_Longitude;
        double trk = -1;
        CRadarTargetPositionData prev = rt.GetPreviousPosition(pd);
        for (int k = 0; k < 5 && prev.IsValid(); k++) {
            double d;
            double b = BearingDeg(prev.GetPosition().m_Latitude, prev.GetPosition().m_Longitude, lat, lon, d);
            if (d >= 0.25) { trk = b; break; }
            prev = rt.GetPreviousPosition(prev);
        }
        if (trk < 0) return false;

        r = ias::Estimate(w, (double)time(NULL), lat, lon, (double)alt, (double)gs, trk);
        if (trkOut) *trkOut = trk;
        if (gsOut) *gsOut = gs;
        if (altOut) *altOut = alt;
        return r.ok;
    }

    void TestTarget(const std::string& csIn)
    {
        std::string want = csIn;
        for (size_t i = 0; i < want.size(); i++) want[i] = (char)toupper((unsigned char)want[i]);
        std::shared_ptr<const ias::Wind> w = GetWind();
        if (!w) { Msg("No wind data."); return; }
        for (CRadarTarget rt = RadarTargetSelectFirst(); rt.IsValid(); rt = RadarTargetSelectNext(rt)) {
            if (want != rt.GetCallsign()) continue;
            ias::Result r; double trk = 0, gs = 0; int alt = 0;
            if (!Compute(rt, *w, r, &trk, &gs, &alt)) { Msg(want + ": no estimate (too slow, no track yet, or outside the wind window)."); return; }
            char t[300];
            snprintf(t, sizeof(t), "%s  pressure alt %d ft, GS %.0f, track %.0f | wind %03.0f/%.0f, T %.1f C | TAS %.0f, Mach %.3f, IAS(est.) %.0f | headwind %.0f%s",
                     want.c_str(), alt, gs, trk, r.windDir, r.windSpd, r.tempK - 273.15, r.tas, r.mach, r.cas, r.headwind,
                     (r.flags & 1) ? " | FORECAST OUT OF DATE" : "");
            Msg(t);
            return;
        }
        Msg(want + ": not found.");
    }
};

// ----------------------------------------------------------------------------
static CIasPlugIn* g_plugin = NULL;

void __declspec(dllexport) EuroScopePlugInInit(CPlugIn** ppPlugInInstance)
{
    g_plugin = new CIasPlugIn();
    *ppPlugInInstance = g_plugin;
}

void __declspec(dllexport) EuroScopePlugInExit(void)
{
    delete g_plugin;
    g_plugin = NULL;
}
