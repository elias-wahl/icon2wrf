"""Per-hour plausibility check of a surface-forcing series (output of surface_series.py / run_surface_series.sh).
For each time step and variable: NaN count, min, max, and flags for out-of-range values, all-NaN
steps, suspiciously constant fields, and hour-to-hour jumps of the domain mean. Streams in time
blocks (never loads a whole variable). Usage: python -m src.icon2wrf.validate_surface_series FILE [FILE ...]"""
import sys
import numpy as np, xarray as xr

# physical plausibility ranges (units as in the file)
RANGES = {"T2D": (220, 330), "Q2D": (0, 0.04), "U2D": (-60, 60), "V2D": (-60, 60), "PSFC": (50000, 106000),
          "SWDOWN": (0, 1400), "LWDOWN": (100, 550), "RAINRATE": (0, 0.05), "SHFLX": (-600, 1200),
          "LHFLX": (-600, 1200), "TG": (220, 345), "TSOIL": (220, 345), "WSOIL": (0, 40000),
          "SNOWH": (0, 15), "SNOWC": (0, 100), "ALBEDO": (0, 100), "HSURF": (0, 4200)}
# fields that may legitimately be constant over the domain in some hours (dry hour, night, no snow)
MAY_BE_CONSTANT = {"RAINRATE", "SWDOWN", "SNOWH", "SNOWC", "SHFLX", "LHFLX"}
# max plausible hour-to-hour change of the domain mean
JUMP = {"T2D": 6, "PSFC": 800, "LWDOWN": 60, "TG": 8, "TSOIL": 3, "Q2D": 0.003}
BLK = 24

for f in sys.argv[1:]:
    ds = xr.open_dataset(f)
    times = ds.time.values; nt = len(times)
    print(f"\n=== {f}\n    {nt} steps {str(times[0])[:13]} .. {str(times[-1])[:13]}   filled_hours: {ds.attrs.get('filled_hours', '?')}")
    dt = np.diff(times)
    if not np.all(dt == np.timedelta64(1, "h")):
        print(f"    !! time axis not strictly hourly: {np.unique(dt)}")
    problems = {}
    summary = {}
    for v in ds.data_vars:
        a = ds[v]
        if "time" not in a.dims:
            x = a.values; lo, hi = RANGES.get(v, (-np.inf, np.inf))
            bad = int(np.isnan(x).sum()) + int(((x < lo) | (x > hi)).sum())
            summary[v] = (float(np.nanmin(x)), float(np.nanmax(x)), 0, bad)
            continue
        lo, hi = RANGES.get(v, (-np.inf, np.inf))
        vmin, vmax, nan_tot, flagged, prev_mean = np.inf, -np.inf, 0, [], None
        for i in range(0, nt, BLK):
            x = a.isel(time=slice(i, i + BLK)).values
            n = x.shape[0]; xf = x.reshape(n, -1)
            nan = np.isnan(xf); nan_per = nan.sum(axis=1); nan_tot += int(nan.sum())
            with np.errstate(all="ignore"):
                mn, mx = np.nanmin(xf, axis=1), np.nanmax(xf, axis=1)
                mean = np.nanmean(xf, axis=1); std = np.nanstd(xf, axis=1)
            vmin, vmax = min(vmin, float(np.nanmin(mn))), max(vmax, float(np.nanmax(mx)))
            for k in range(n):
                t = str(times[i + k])[:13]; why = []
                if nan_per[k] == xf.shape[1]: why.append("all NaN")
                elif nan_per[k] > 0: why.append(f"{int(nan_per[k])} NaN")
                if not np.isnan(mn[k]) and (mn[k] < lo or mx[k] > hi): why.append(f"out of range [{mn[k]:.4g}, {mx[k]:.4g}] vs [{lo}, {hi}]")
                if std[k] == 0 and v not in MAY_BE_CONSTANT: why.append(f"constant {mean[k]:.4g}")
                if prev_mean is not None and v in JUMP and abs(mean[k] - prev_mean) > JUMP[v]:
                    why.append(f"domain-mean jump {prev_mean:.4g} -> {mean[k]:.4g}")
                prev_mean = mean[k]
                if why: flagged.append((t, "; ".join(why)))
        summary[v] = (vmin, vmax, nan_tot, len(flagged))
        if flagged: problems[v] = flagged
    print(f"    {'var':9s} {'min':>10s} {'max':>10s} {'NaNs':>8s} {'flagged steps':>14s}")
    for v, (mn, mx, nan, nfl) in summary.items():
        print(f"    {v:9s} {mn:10.4g} {mx:10.4g} {nan:8d} {nfl:14d}")
    if problems:
        print("    flagged details (first 6 per variable):")
        for v, fl in problems.items():
            for t, why in fl[:6]: print(f"      {v:9s} {t}  {why}")
            if len(fl) > 6: print(f"      {v:9s} ... {len(fl) - 6} more")
    else:
        print("    all time steps of all fields within plausible ranges, no NaNs, no jumps")
    nan_pairs = [(t, v, why) for v, fl in problems.items() for t, why in fl if "NaN" in why]
    print(f"    NaN scan over all variables and hours: {len(nan_pairs)} (hour, variable) pairs with NaNs" + (":" if nan_pairs else ""))
    for t, v, why in nan_pairs:
        print(f"      NAN {t} {v} {why}")
    ds.close()
