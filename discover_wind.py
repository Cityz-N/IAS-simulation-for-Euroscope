"""
Discovery for the wind/temperature relay, round 3.
Known so far: base /previnum/DPPaquetARPEGE/v1 ; grids 0.1 and 0.25 ; time format 000H024H (3 digits H 3 digits H) ;
0.25 IP3/IP4 download fine (hourly steps). We need package IP1 (T, HU, U, V, Z on isobaric levels).
This round: size of ONE step of IP1 (0.25), which time ranges are accepted, and does the 0.1 grid work.
Prints NO secrets.
"""
import datetime as dt
import os
import sys
import time

import requests

KEY = (os.environ.get("MF_WIND_KEY", "") or os.environ.get("MF_API_KEY", "")).strip()
if not KEY:
    print("ERROR: neither the secret MF_WIND_KEY nor MF_API_KEY is set.")
    sys.exit(1)
BASE = "https://public-api.meteofrance.fr/previnum/DPPaquetARPEGE/v1/models/ARPEGE/grids"
CAP = 450 * 1024 * 1024
_last = [0.0]


def title(t):
    print("\n" + "=" * 70 + "\n" + t + "\n" + "=" * 70, flush=True)


def call(url, params, stream=True, timeout=240):
    for attempt in range(3):
        wait = 1.6 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            r = requests.get(url, headers={"apikey": KEY, "accept": "*/*"}, params=params, timeout=timeout, stream=stream)
        except requests.RequestException as e:
            print("   request failed:", type(e).__name__, str(e)[:160])
            return None
        if r.status_code == 429:
            print("   429 quota hit -> waiting 70 s")
            r.close()
            time.sleep(70)
            continue
        return r
    return None


def runs(n=4):
    now = dt.datetime.now(dt.timezone.utc)
    t = now.replace(hour=(now.hour // 6) * 6, minute=0, second=0, microsecond=0)
    return [t - dt.timedelta(hours=6 * i) for i in range(1, n + 1)]


def status_only(grid, pkg, run, tm):
    ref = run.strftime("%Y-%m-%dT%H:%M:%SZ")
    r = call(f"{BASE}/{grid}/packages/{pkg}/productARP", {"referencetime": ref, "time": tm, "format": "grib2"})
    if r is None:
        print(f"grid {grid} {pkg} {ref} {tm}: no answer")
        return False
    ok = r.status_code == 200
    print(f"grid {grid} {pkg} {ref} {tm}: HTTP {r.status_code}",
          f"content-length={r.headers.get('content-length')} type={r.headers.get('content-type')}" if ok
          else r.text[:160].replace("\n", " "))
    r.close()
    return ok


def download(grid, pkg, run, tm, fn, cap=CAP):
    ref = run.strftime("%Y-%m-%dT%H:%M:%SZ")
    r = call(f"{BASE}/{grid}/packages/{pkg}/productARP", {"referencetime": ref, "time": tm, "format": "grib2"})
    if r is None or r.status_code != 200:
        print(f"grid {grid} {pkg} {ref} {tm}: HTTP {r.status_code if r is not None else '?'}",
              r.text[:160].replace("\n", " ") if r is not None else "")
        return False
    n = 0
    t0 = time.time()
    with open(fn, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
            n += len(chunk)
            if n >= cap:
                break
    done = n < cap
    r.close()
    print(f"grid {grid} {pkg} {ref} {tm}: saved {n / 1e6:.1f} MB in {time.time() - t0:.0f}s ({'COMPLETE' if done else 'CUT at cap'})")
    return True


def inventory(fn):
    try:
        import eccodes
    except Exception as e:
        print("   eccodes not available:", e)
        return
    seen = {}
    first = None
    count = 0
    with open(fn, "rb") as f:
        while True:
            try:
                h = eccodes.codes_grib_new_from_file(f)
            except Exception as e:
                print("   (stopped at a truncated message:", str(e)[:80], ")")
                break
            if h is None:
                break
            count += 1
            try:
                g = lambda k: eccodes.codes_get(h, k)
                key = (g("shortName"), g("typeOfLevel"), g("level"), g("stepRange"))
                seen[key] = seen.get(key, 0) + 1
                if first is None:
                    first = {}
                    for k in ("Ni", "Nj", "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees",
                              "latitudeOfLastGridPointInDegrees", "longitudeOfLastGridPointInDegrees",
                              "iDirectionIncrementInDegrees", "jDirectionIncrementInDegrees", "jScansPositively",
                              "dataDate", "dataTime", "gridType", "units", "packingType", "bitsPerValue"):
                        try:
                            first[k] = g(k)
                        except Exception:
                            first[k] = "?"
            finally:
                eccodes.codes_release(h)
    print("   messages:", count, " file MB:", round(os.path.getsize(fn) / 1e6, 1))
    print("   grid:", first)
    for sn in sorted({k[0] for k in seen}):
        lv = sorted({(k[1], k[2]) for k in seen if k[0] == sn})
        print(f"   {sn}: {len(lv)} levels: {[l[1] for l in lv]}  (type {lv[0][0]})")
    print("   steps seen:", sorted({k[3] for k in seen}))


def main():
    rs = runs(4)

    title("A. 0.25 IP1 : which time ranges are accepted (status only)")
    ok_run = None
    for run in rs[:3]:
        if status_only("0.25", "IP1", run, "000H000H"):
            ok_run = run
            break
    if ok_run is None:
        print("no run available for 0.25 IP1 ?")
        return
    for tm in ("003H003H", "000H003H", "006H006H", "012H012H"):
        status_only("0.25", "IP1", ok_run, tm)

    title("B. 0.25 IP1 : ONE step, full download + inventory")
    if download("0.25", "IP1", ok_run, "000H000H", "ip1_025_step0.grib2"):
        inventory("ip1_025_step0.grib2")

    title("C. 0.1 grid : does it work with other runs / ranges? (status only, then small sample)")
    ok01 = None
    for run in rs:
        for tm in ("000H000H", "000H003H"):
            if status_only("0.1", "IP1", run, tm):
                ok01 = (run, tm)
                break
        if ok01:
            break
    if ok01:
        if download("0.1", "IP1", ok01[0], ok01[1], "ip1_01.grib2", cap=60 * 1024 * 1024):
            inventory("ip1_01.grib2")
    else:
        print("0.1 IP1 not available for any of the last 4 runs with these ranges")


if __name__ == "__main__":
    main()
