IasEstimator - estimated IAS / Mach / wind in EuroScope lists and tags (version 0.1.0)

WHAT IT DOES
  For every aircraft EuroScope shows, it takes the ground speed and the track (from the last positions),
  the pressure altitude, and the Meteo-France ARPEGE forecast wind and temperature at that point and
  altitude, and works out
      TAS = ground speed minus wind (vector)       Mach = TAS / speed of sound (forecast temperature)
      IAS (calibrated) = from pressure altitude and Mach (compressible flow formula)
  It is an ESTIMATE of what the pilot reads: forecast wind is not the real wind (typical error a few
  knots, more near jet streams, fronts and low levels), and the pilot's IAS instrument is ignored.
  The estimate is only as good as the wind: expect +-5 to 10 kt, usually better.

TAG ITEMS (EuroScope > Settings > Lists / Tag editor: pick them in the "Item" list)
  Est. IAS (kt)        e.g. 285
  Est. Mach            e.g. M.78
  Est. IAS or Mach     IAS below M0.60 (MachAbove in the ini), Mach above it
  Forecast wind        e.g. 270/045   (direction it blows FROM, speed kt)
  Head/tailwind (kt)   H25 = 25 kt headwind on the aircraft's track, T12 = tailwind
  Est. TAS (kt)        e.g. 462
  "---" = no estimate (slow / on the ground, no track yet, outside the wind window); "n/a" = no data loaded.
  Orange text = the forecast is out of date (the relay has not produced a new file for more than a day).

COMMANDS (type them in the EuroScope command line)
  .ias status              which forecast run is loaded
  .ias reload              download again now
  .ias test CALLSIGN       shows altitude, wind, TAS, Mach and IAS of one aircraft (handy to check against a pilot)
  .ias round 5             rounding in kt      .ias mach 0.60   switch point of the combined item
  .ias url https://...     other address for this session

INSTALL
  1. Relay: see STEPS_RELAY.txt (GitHub Actions publishes wind.bin on GitHub Pages, 4 times a day).
  2. Build: MFC DLL project named IasEstimator, platform x86, Release, "Use MFC in a Static Library",
     C/C++ > Code Generation > Runtime Library /MT, no precompiled headers, include + lib folders pointing to
     the EuroScope SDK. Replace the generated cpp with IasEstimator.cpp, put IasCore.h next to it,
     remove the other generated files.
  3. Copy IasEstimator.dll and IasEstimator.ini to the same folder, edit Url= in the ini, load the DLL in
     EuroScope (Other SET > Plug-ins). The last downloaded file is kept as IasEstimator_wind.cache.

COVERAGE AND DATA
  Window: latitude 36 to 54 N, longitude 8 W to 12 E (France and surroundings), 0.5 degree grid,
  24 pressure levels from 1000 to 100 hPa (about 53000 ft), forecast every 3 h, interpolated in space,
  pressure (log) and time. Outside the window: "---".
  Source: Meteo-France ARPEGE 0.25 degree via the public API, Etalab open licence. Mention "Source: Meteo-France".

KNOWN LIMITS (first version, not yet tested inside EuroScope)
  - The track is computed from the last positions (about 5 s apart on VATSIM), so it lags a little in turns.
  - Aircraft on a reduced position rate or far from the previous position may show "---" for a moment.
  - Pressure altitude is taken as reported by the network (standard 1013.25 setting).
  - Supersonic speeds are not handled.
