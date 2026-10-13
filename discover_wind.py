"""
Discovery for the wind/temperature relay, round 2.
Round 1 found the working address (/previnum/DPPaquetARPEGE/v1) and two grids (0.1 and 0.25),
but hit the API rate limit because it sent far too many requests. This version is polite:
about one request every 1.5 s, and it waits when the API says "quota exceeded".
Prints NO secrets (the API key comes from a GitHub secret).
"""
import datetime as dt
import json
import os
import re
import sys
import time

import requests

KEY = (os.environ.get("MF_WIND_KEY", "") or os.environ.get("MF_API_KEY", "")).strip()
if not KEY:
    print("ERROR: neither the secret MF_WIND_KEY nor MF_API_KEY is set.")
    sys.exit(1)
BASE = "https://public-api.meteofrance.fr/previnum/DPPaquetARPEGE/v1"
PARTIAL_BYTES = 60 * 1024 * 1024        # we only need the first messages of a file to see what is inside
_last = [0.0]


def title(t):
    print("\n" + "=" * 70 + "\n" + t + "\n" + "=" * 70, flush=True)


def call(url, params=None, accept="application/json", stream=False, timeout=90):
    """One polite request: spacing between calls and waiting on 429."""
    for attempt in range(3):
        wait = 1.6 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            r = requests.get(url, headers={"apikey": KEY, "accept": accept}, params=params,
                             timeout=timeout, stream=stream)
        except requests.RequestException as e:
            print("   request failed:", type(e).__name__, str(e)[:160])
            return None
        if r.status_code == 429:
            print("   429 quota hit:", r.text[:200].replace("\n", " "), "-> waiting 70 s")
            r.close()
            time.sleep(70)
            continue
        return r
    return None


def short(path):
    return path.replace(BASE, "")


def main():
    title("1. Grids")
    r = call(BASE + "/models/ARPEGE/grids")
    grids = []
    if r is not None and r.status_code == 200:
        for l in r.json().get("links", []):
            m = re.search(r"/grids/([0-9.]+)$", l.get("href", ""))
            if m:
                grids.append(m.group(1))
    print("grids:", grids)
    if not grids:
        print("   (could not read the grid list)", r.text[:500] if r is not None else "")
        return

    title("2. Packages of each grid")
    packages = {}
    for g in grids:
        r = call(BASE + f"/models/ARPEGE/grids/{g}/packages")
        print(f"\n-- grid {g}: HTTP {r.status_code if r is not None else '?'}")
        if r is None or r.status_code != 200:
            print(r.text[:600] if r is not None else "")
            continue
        print(json.dumps(r.json(), ensure_ascii=False)[:3500])
        names = []
        for l in r.json().get("links", []):
            m = re.search(r"/packages/([A-Za-z0-9_]+)$", l.get("href", ""))
            if m:
                names.append(m.group(1))
        packages[g] = names
        print("   package names:", names)

    title("3. Description of each package (one request each)")
    desc = {}
    for g in grids:
        for p in packages.get(g, []):
            r = call(BASE + f"/models/ARPEGE/grids/{g}/packages/{p}")
            if r is None or r.status_code != 200:
                print(f"grid {g} {p}: HTTP {r.status_code if r is not None else '?'}")
                continue
            txt = json.dumps(r.json(), ensure_ascii=False)
            desc[(g, p)] = txt
            print(f"\ngrid {g} package {p}:\n  {txt[:1800]}")

    title("4. Download test (first 60 MB only), few attempts")
    now = dt.datetime.now(dt.timezone.utc)
    runs = []
    for back in (6, 12, 18):
        t = now - dt.timedelta(hours=back)
        runs.append(t.replace(hour=(t.hour // 6) * 6, minute=0, second=0, microsecond=0))
    tried = 0
    done = set()
    for g in grids:
        # the isobaric packages are called IPx; try them first, one per grid, then HPx
        for p in [x for x in packages.get(g, []) if x.upper().startswith("IP")][:2]:
            ok = False
            for run in runs[:2]:
                for tm in ("000H024H", "00H24H"):
                    ref = run.strftime("%Y-%m-%dT%H:%M:%SZ")
                    url = BASE + f"/models/ARPEGE/grids/{g}/packages/{p}/productARP"
                    r = call(url, {"referencetime": ref, "time": tm, "format": "grib2"}, accept="*/*", stream=True, timeout=180)
                    tried += 1
                    if r is None:
                        continue
                    print(f"grid {g} {p} run {ref} time {tm}: HTTP {r.status_code}",
                          "" if r.status_code == 200 else r.text[:200].replace("\n", " "))
                    if r.status_code == 200:
                        fn = f"sample_{g}_{p}.grib2"
                        n = 0
                        t0 = time.time()
                        with open(fn, "wb") as f:
                            for chunk in r.iter_content(1 << 20):
                                f.write(chunk)
                                n += len(chunk)
                                if n >= PARTIAL_BYTES:
                                    break
                        total = r.headers.get("content-length")
                        r.close()
                        print(f"   saved {fn}: {n / 1e6:.1f} MB in {time.time() - t0:.0f}s (full size: {total})")
                        inventory(fn)
                        ok = True
                        break
                if ok:
                    break
            if tried > 14:
                print("(stopping: enough attempts for one run)")
                return


def inventory(fn):
    try:
        import eccodes
    except Exception as e:
        print("   eccodes not available:", e)
        return
    seen = {}
    first = None
    with open(fn, "rb") as f:
        while True:
            try:
                h = eccodes.codes_grib_new_from_file(f)
            except Exception as e:
                print("   (stopped at a truncated message:", str(e)[:80], ")")
                break
            if h is None:
                break
            try:
                g = lambda k: eccodes.codes_get(h, k)
                key = (g("shortName"), g("typeOfLevel"), g("level"), g("stepRange"))
                seen[key] = seen.get(key, 0) + 1
                if first is None:
                    first = {}
                    for k in ("Ni", "Nj", "latitudeOfFirstPointInDegrees", "longitudeOfFirstPointInDegrees",
                              "latitudeOfLastPointInDegrees", "longitudeOfLastPointInDegrees",
                              "iDirectionIncrementInDegrees", "jDirectionIncrementInDegrees",
                              "dataDate", "dataTime", "gridType"):
                        try:
                            first[k] = g(k)
                        except Exception:
                            first[k] = "?"
            finally:
                eccodes.codes_release(h)
    print("   grid:", first)
    for sn in sorted({k[0] for k in seen}):
        lv = sorted({(k[1], k[2]) for k in seen if k[0] == sn})
        print(f"   {sn}: {len(lv)} levels e.g. {lv[:30]}")
    print("   steps seen:", sorted({k[3] for k in seen})[:20])


if __name__ == "__main__":
    main()
