"""
Discovery for the wind/temperature relay: what does the Meteo-France ARPEGE package API give us?
Prints NO secrets (the API key comes from the GitHub secret MF_API_KEY).
Goal: find (1) a working URL pattern, (2) which package holds U/V wind + temperature on
pressure levels, (3) the grid, (4) the file size.
"""
import datetime as dt
import os
import sys
import time

import requests

KEY = (os.environ.get("MF_WIND_KEY", "") or os.environ.get("MF_API_KEY", "")).strip()   # separate key if you made one, else the radar key
if not KEY:
    print("ERROR: neither the secret MF_WIND_KEY nor MF_API_KEY is set.")
    sys.exit(1)
HEAD = {"apikey": KEY, "accept": "*/*"}
BASES = [
    "https://public-api.meteofrance.fr/public/DPPaquetARPEGE/v1",
    "https://public-api.meteofrance.fr/previnum/DPPaquetARPEGE/v1",
]
PARTIAL_BYTES = 40 * 1024 * 1024        # we only need the first messages of a file to see what is inside


def title(t):
    print("\n" + "=" * 70 + "\n" + t + "\n" + "=" * 70, flush=True)


def get(url, params=None, accept="application/json", stream=False, timeout=60):
    try:
        return requests.get(url, headers={"apikey": KEY, "accept": accept}, params=params,
                            timeout=timeout, stream=stream)
    except requests.RequestException as e:
        print("   request failed:", type(e).__name__, str(e)[:200])
        return None


def show(r, n=1800):
    if r is None:
        return
    print("   status:", r.status_code, " content-type:", r.headers.get("content-type"))
    if r.status_code != 200 or "json" in (r.headers.get("content-type") or ""):
        print("   body:", r.text[:n])


def find_base():
    title("1. Which URL pattern works?")
    for b in BASES:
        for path in ("/models/ARPEGE", "/models"):
            url = b + path
            print("GET", url.replace("https://public-api.meteofrance.fr", ""))
            r = get(url)
            show(r)
            if r is not None and r.status_code == 200:
                return b
    return None


def main():
    base = find_base()
    if not base:
        print("\nNo URL worked. Most likely your application is not subscribed to the ARPEGE package API:")
        print("portail-api.meteofrance.fr -> API -> 'Paquets ARPEGE' (DPPaquetARPEGE) -> Subscribe, then generate a new key.")
        return
    print("\nWORKING BASE:", base)

    title("2. Grids and packages")
    grids = []
    r = get(base + "/models/ARPEGE/grids")
    show(r, 3000)
    try:
        j = r.json()
        grids = [str(g.get("name", g)) if isinstance(g, dict) else str(g) for g in (j.get("grids", j) if isinstance(j, dict) else j)]
    except Exception:
        pass
    print("grids found:", grids)
    for g in grids[:6]:
        print("\n-- grid", g)
        rr = get(base + f"/models/ARPEGE/grids/{g}/packages")
        show(rr, 2500)

    title("3. Description of the isobaric packages")
    for g in grids[:6]:
        for p in ("IP1", "IP2", "IP3", "IP4", "SP1"):
            rr = get(base + f"/models/ARPEGE/grids/{g}/packages/{p}")
            if rr is not None and rr.status_code == 200:
                print(f"grid {g} package {p}:", rr.text[:1500])

    title("4. Try a download (first 40 MB only)")
    now = dt.datetime.utcnow()
    runs = []
    for back in range(5, 40, 6):
        t = now - dt.timedelta(hours=back)
        t = t.replace(hour=(t.hour // 6) * 6, minute=0, second=0, microsecond=0)
        if t not in runs:
            runs.append(t)
    got = None
    for g in grids[:6]:
        for p in ("IP1", "IP2", "IP3", "IP4"):
            for run in runs[:3]:
                for tm in ("000H012H", "00H06H", "000H024H", "00H24H", "0H6H"):
                    ref = run.strftime("%Y-%m-%dT%H:%M:%SZ")
                    url = base + f"/models/ARPEGE/grids/{g}/packages/{p}/productARP"
                    r = get(url, {"referencetime": ref, "time": tm, "format": "grib2"}, accept="*/*", stream=True, timeout=120)
                    if r is None:
                        continue
                    code = r.status_code
                    print(f"grid {g} {p} run {ref} time {tm}: HTTP {code}", "" if code == 200 else r.text[:150].replace("\n", " "))
                    if code == 200:
                        fn = f"sample_{g}_{p}.grib2"
                        n = 0
                        t0 = time.time()
                        with open(fn, "wb") as f:
                            for chunk in r.iter_content(1 << 20):
                                f.write(chunk)
                                n += len(chunk)
                                if n >= PARTIAL_BYTES:
                                    break
                        r.close()
                        print(f"   saved {fn}: {n / 1e6:.1f} MB in {time.time() - t0:.0f}s (total size header: {r.headers.get('content-length')})")
                        got = fn
                        inventory(fn)
                        break
                if got and got.startswith(f"sample_{g}_{p}"):
                    break
            if got and got.startswith(f"sample_{g}_{p}"):
                got = None          # try the next package too, so we see all of them
                continue


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
                    first = {k: g(k) for k in ("Ni", "Nj", "latitudeOfFirstPointInDegrees", "longitudeOfFirstPointInDegrees",
                                               "latitudeOfLastPointInDegrees", "longitudeOfLastPointInDegrees",
                                               "iDirectionIncrementInDegrees", "jDirectionIncrementInDegrees",
                                               "dataDate", "dataTime", "gridType")}
            finally:
                eccodes.codes_release(h)
    print("   grid:", first)
    names = sorted({(k[0], k[1]) for k in seen})
    print("   parameters (shortName, typeOfLevel):", names)
    for sn in sorted({k[0] for k in seen}):
        lv = sorted({(k[1], k[2]) for k in seen if k[0] == sn})
        print(f"   {sn}: levels {lv[:40]}")
    print("   steps seen:", sorted({k[3] for k in seen})[:20])


if __name__ == "__main__":
    main()
