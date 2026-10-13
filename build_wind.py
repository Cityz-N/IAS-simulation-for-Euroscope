"""
Builds the wind/temperature file used by the EuroScope IAS estimator.

  1. downloads Meteo-France ARPEGE 0.25 deg, package IP1 (T, U, V ... on isobaric levels), steps 0-24 h
     (the newest run that is available, falling back to older runs)
  2. cuts a Europe window at 0.5 deg, keeps levels 100-1000 hPa and every 3rd hour
  3. writes out/wind.bin (compact, little-endian) and out/meta.json

wind.bin layout (all little-endian):
  char[4] "IASW" ; int32 version(=1) ; int64 runTime (unix s) ; int32 nFrames ; int32 nLev ; int32 nLat ; int32 nLon
  float32 lat0 (south) ; float32 lon0 (west) ; float32 dlat ; float32 dlon ; int32 stepHours
  float32 levels[nLev]               hPa, ascending (100 ... 1000)
  int32   frameHour[nFrames]         forecast hour of each frame
  int16   data[nFrames][nLev][3][nLat][nLon]   var 0=u, 1=v (m/s * 100), 2=T (K * 100); lat south->north, lon west->east
Prints NO secrets.
"""
import datetime as dt
import json
import os
import struct
import sys
import time

import numpy as np
import requests

BASE = "https://public-api.meteofrance.fr/previnum/DPPaquetARPEGE/v1/models/ARPEGE/grids/0.25/packages/IP1/productARP"
LAT_N, LAT_S = 54.0, 36.0
LON_W, LON_E = -8.0, 12.0
D = 0.5
STEP_H = 3
HOURS = list(range(0, 25, STEP_H))
LEV_MIN, LEV_MAX = 100, 1000
OUT = "out"
GRIB = "ip1.grib2"


def get_key():
    k = (os.environ.get("MF_WIND_KEY", "") or os.environ.get("MF_API_KEY", "")).strip()
    if not k:
        print("ERROR: neither MF_WIND_KEY nor MF_API_KEY is set")
        sys.exit(1)
    return k


def download(key, run):
    ref = run.strftime("%Y-%m-%dT%H:%M:%SZ")
    for attempt in range(1, 4):
        try:
            r = requests.get(BASE, headers={"apikey": key, "accept": "*/*"},
                             params={"referencetime": ref, "time": "000H024H", "format": "grib2"},
                             stream=True, timeout=(30, 120))
        except requests.RequestException as e:
            print(f"  run {ref}: request failed ({type(e).__name__}), attempt {attempt}")
            time.sleep(20)
            continue
        if r.status_code == 404:
            print(f"  run {ref}: not available (404)")
            r.close()
            return False
        if r.status_code == 429:
            print(f"  run {ref}: 429 quota, waiting 70 s")
            r.close()
            time.sleep(70)
            continue
        if r.status_code != 200:
            print(f"  run {ref}: HTTP {r.status_code} {r.text[:200]}")
            r.close()
            return False
        n = 0
        t0 = time.time()
        try:
            with open(GRIB, "wb") as f:
                for chunk in r.iter_content(4 << 20):
                    f.write(chunk)
                    n += len(chunk)
        except requests.RequestException as e:
            print(f"  run {ref}: download interrupted after {n / 1e6:.0f} MB ({type(e).__name__}), attempt {attempt}")
            time.sleep(20)
            continue
        finally:
            r.close()
        print(f"  run {ref}: downloaded {n / 1e6:.0f} MB in {time.time() - t0:.0f} s")
        return True
    return False


def convert(path, run):
    import eccodes

    lats = np.arange(LAT_S, LAT_N + 1e-6, D)                       # south -> north
    lons = np.arange(LON_W, LON_E + 1e-6, D)
    rows = np.round((90.0 - lats) / 0.25).astype(int)              # native grid goes 90 -> -90
    cols = np.round((lons % 360.0) / 0.25).astype(int)
    nlat, nlon = len(lats), len(lons)
    want = {"u", "v", "t"}
    var_idx = {"u": 0, "v": 1, "t": 2}
    store = {}                                                      # (hour, level) -> array[3, nlat, nlon]
    levels = set()
    count = 0
    with open(path, "rb") as f:
        while True:
            try:
                h = eccodes.codes_grib_new_from_file(f)
            except Exception as e:
                print("  (stopped at a damaged message:", str(e)[:80], ")")
                break
            if h is None:
                break
            try:
                sn = eccodes.codes_get(h, "shortName")
                if sn not in want or eccodes.codes_get(h, "typeOfLevel") != "isobaricInhPa":
                    continue
                lev = int(eccodes.codes_get(h, "level"))
                step = int(eccodes.codes_get(h, "step"))
                if lev < LEV_MIN or lev > LEV_MAX or step not in HOURS:
                    continue
                ni, nj = eccodes.codes_get(h, "Ni"), eccodes.codes_get(h, "Nj")
                if (ni, nj) != (1440, 721):
                    raise RuntimeError(f"unexpected grid {ni}x{nj}")
                eccodes.codes_set(h, "missingValue", 1e30)
                a = eccodes.codes_get_values(h).reshape(nj, ni).astype(np.float32)
                a[a >= 1e29] = np.nan
                sub = a[np.ix_(rows, cols)]
                store.setdefault((step, lev), np.full((3, nlat, nlon), np.nan, np.float32))[var_idx[sn]] = sub
                levels.add(lev)
                count += 1
            finally:
                eccodes.codes_release(h)
    levels = sorted(levels)
    print(f"  kept {count} messages, {len(levels)} levels, levels {levels}")
    need = len(HOURS) * len(levels) * 3
    if not levels or count < need:
        raise RuntimeError(f"incomplete data: {count} of {need} messages")

    data = np.zeros((len(HOURS), len(levels), 3, nlat, nlon), np.float32)
    for fi, hr in enumerate(HOURS):
        for li, lev in enumerate(levels):
            data[fi, li] = store[(hr, lev)]
    # below-ground points: copy from the nearest valid level above (lower pressure first, then the other way)
    for fi in range(len(HOURS)):
        for v in range(3):
            plane = data[fi, :, v]
            for li in range(1, len(levels)):
                bad = np.isnan(plane[li])
                plane[li][bad] = plane[li - 1][bad]
            for li in range(len(levels) - 2, -1, -1):
                bad = np.isnan(plane[li])
                plane[li][bad] = plane[li + 1][bad]
    data = np.nan_to_num(data, nan=0.0)

    q = np.empty(data.shape, np.int16)
    q[:, :, 0:2] = np.clip(np.round(data[:, :, 0:2] * 100.0), -32000, 32000)
    q[:, :, 2] = np.clip(np.round(data[:, :, 2] * 100.0), 0, 32000)

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "wind.bin"), "wb") as f:
        f.write(b"IASW")
        f.write(struct.pack("<iqiiii", 1, int(run.timestamp()), len(HOURS), len(levels), nlat, nlon))
        f.write(struct.pack("<ffffi", float(lats[0]), float(lons[0]), D, D, STEP_H))
        f.write(np.array(levels, "<f4").tobytes())
        f.write(np.array(HOURS, "<i4").tobytes())
        f.write(q.astype("<i2").tobytes())
    size = os.path.getsize(os.path.join(OUT, "wind.bin"))
    meta = {
        "run": run.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "frames_hours": HOURS, "levels_hPa": levels,
        "lat": [float(lats[0]), float(lats[-1])], "lon": [float(lons[0]), float(lons[-1])], "step_deg": D,
        "bytes": size,
    }
    with open(os.path.join(OUT, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    li500 = levels.index(500) if 500 in levels else len(levels) // 2
    print(f"  wrote wind.bin {size / 1e6:.2f} MB ; sanity @{levels[li500]} hPa frame0: "
          f"u mean {data[0, li500, 0].mean():.1f} m/s, v mean {data[0, li500, 1].mean():.1f} m/s, "
          f"T mean {data[0, li500, 2].mean():.1f} K")


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--convert-only":      # test mode: python build_wind.py --convert-only file 2026-10-13T06
        run = dt.datetime.strptime(sys.argv[3], "%Y-%m-%dT%H").replace(tzinfo=dt.timezone.utc) if len(sys.argv) > 3 else dt.datetime.now(dt.timezone.utc)
        convert(sys.argv[2], run)
        return
    key = get_key()
    now = dt.datetime.now(dt.timezone.utc)
    latest = now.replace(hour=(now.hour // 6) * 6, minute=0, second=0, microsecond=0)
    for back in range(0, 4):
        run = latest - dt.timedelta(hours=6 * back)
        print("trying run", run.strftime("%Y-%m-%dT%H:%MZ"))
        if not download(key, run):
            continue
        try:
            convert(GRIB, run)
        except Exception as e:
            print("  conversion failed:", e)
            continue
        finally:
            if os.path.exists(GRIB):
                os.remove(GRIB)
        print("OK")
        return
    print("ERROR: no usable run found")
    sys.exit(1)


if __name__ == "__main__":
    main()
