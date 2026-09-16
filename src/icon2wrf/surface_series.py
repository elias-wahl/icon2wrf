"""Hourly ICON surface-forcing time series for an offline land-surface model (HRLDAS / Noah-MP spin-up).

Streams the TEAMx 500 m ICON forecasts from the ACINN FTP one hour at a time: download the ~900 MB
hourly file, pull the handful of surface fields below out of it with ecCodes, regrid them to the
lon-lat product grid (config/target_grid.txt, the same 0.0065 x 0.0045 deg grid the WRF product uses),
de-accumulate the run-cumulative fields against the previous lead of the same run, and delete the raw
file before the next download. One NetCDF with a time axis comes out (`cdo mergetime` of the hourly
pieces). Ported 2026-09-15 from the download-filter-delete pipeline of branch icon2amundsen
(amundsen_runner.py) and reduced to what a land model needs; nothing in the WRF profile is touched.

Forecast stitching ("freshest" strategy, as on the branch): for every target hour take the newest run
whose lead is >= --spinup hours (default 9; runs at 00/12 UTC, 48 h each); when a run is entered,
its lead-1 file is fetched too so the accumulated fields can be differenced. Hours no run on the server
covers are filled afterwards by `fill_gaps` (--fill auto|linear|diurnal: linear for short gaps, for
longer ones the diurnal cycle of the day before/after blended to the bracket hours; listed in the global
attribute `filled_hours`, method in `fill_method`). The wrapper runs that pass once over the merged
series (chunks use --no-fill), so gaps at chunk edges are bracketed too. Disk: pieces and the result are
zlib-compressed, ~26 MB per hour each, plus a ~12 GB `cdo mergetime` spike per job.

Output grid: --wrf-grid <wrfinput_d01> remaps straight onto the WRF mass grid (500 x 600 for the production
domain, 3.8x fewer cells than the product grid and no second interpolation for HRLDAS); without it the
lon-lat product grid of config/target_grid.txt is used.
Output variables (HRLDAS LDASIN names where they exist), hourly:
  T2D   2 m temperature [K]              Q2D   2 m specific humidity [kg/kg] (from 2 m dew point + PSFC)
  U2D   10 m u wind [m/s]                V2D   10 m v wind [m/s]
  PSFC  surface pressure [Pa]            RAINRATE  precipitation rate [kg m-2 s-1] (tp differenced)
  SWDOWN downwelling shortwave, hourly mean [W/m2] (ASWDIR_S + ASWDIFD_S de-averaged)
  LWDOWN downwelling longwave, hourly mean [W/m2] (= net surface LW de-averaged + sigma*T_G^4, eps = 1)
  TG    ground temperature T_G [K]       SHFLX / LHFLX  ICON's own hourly-mean sensible / latent heat flux, upward positive [W/m2]
  TSOIL(soil_depth)  ICON T_SO [K]       WSOIL(soil_layer)  ICON W_SO column water [kg m-2]
  SNOWH snow depth [m]   SNOWC snow cover [%]   ALBEDO forecast albedo [%]   HSURF terrain of the run [m] (lead 0)

Usage (from the icon2wrf root, `icon` conda env, `module load cdo`, FTP_PASSWORD in the environment):
  python -m src.icon2wrf.surface_series --start 2025061500 --end 2025071800 --out output/icon_surface_2025061500_2025071800.nc
  python -m src.icon2wrf.surface_series --start 2025071712 --end 2025071715 --out /tmp/test.nc --keep-raw   # smoke test
The bash wrapper run_surface_series.sh splits a long range into parallel chunks and merges them.
"""
import argparse
import ftplib
import os
import random
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .download_ftp import load_credentials, extract_gz, get_filename_for_offset

SB = 5.670374e-8

# CF metadata for the fields copied straight from the GRIB (units as in ICON's GRIB2 headers)
META = {"T2D": ("2 m temperature", "K"), "U2D": ("10 m u wind component", "m s-1"), "V2D": ("10 m v wind component", "m s-1"),
        "PSFC": ("surface pressure", "Pa"), "TG": ("ground (skin) temperature T_G", "K"), "SNOWH": ("snow depth", "m"),
        "SNOWC": ("snow cover fraction", "%"), "ALBEDO": ("forecast surface albedo", "%"),
        "TSOIL": ("soil temperature T_SO (ICON multilayer soil model)", "K"),
        "WSOIL": ("column-integrated soil water W_SO per layer (ICON multilayer soil model)", "kg m-2"),
        "HSURF": ("ICON terrain height (lead-0 file)", "m")}
# physical bounds applied to filled (interpolated) values
BOUNDS = {"SWDOWN": (0, None), "RAINRATE": (0, None), "SNOWH": (0, None), "SNOWC": (0, 100), "ALBEDO": (0, 100),
          "WSOIL": (0, None), "Q2D": (0, None)}
INTERMITTENT = ("SWDOWN", "RAINRATE", "SHFLX", "LHFLX")   # filled without the offset blend (see fill_gaps)

# shortName -> (output name, stepType expected)   "avg"/"accum" fields are run-cumulative and get de-accumulated
INSTANT = {"2t": "T2D", "2d": "TD2", "10u": "U2D", "10v": "V2D", "sp": "PSFC", "T_G": "TG",
           "sde": "SNOWH", "snowc": "SNOWC", "al": "ALBEDO"}
CUMUL = {"ASWDIR_S": "swdir", "ASWDIFD_S": "swdif", "avg_snlwrf": "lwnet", "avg_ishf": "SHFLX",
         "avg_slhtf": "LHFLX", "tp": "tp"}
SOIL = {"T_SO": "TSOIL", "W_SO": "WSOIL"}
INVARIANT = {"HSURF": "HSURF"}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- GRIB -> NetCDF
def extract_fields(grib_path, out_nc, wanted_invariant=False):
    """One ecCodes pass over the hourly file; writes the wanted fields on the native 'values' axis."""
    import eccodes
    import xarray as xr

    fields, soil, soil_depth = {}, {}, {}
    with open(grib_path, "rb") as fh:
        while True:
            gid = eccodes.codes_grib_new_from_file(fh)
            if gid is None:
                break
            try:
                sn = eccodes.codes_get(gid, "shortName", str)
                st = eccodes.codes_get(gid, "stepType", str) if eccodes.codes_is_defined(gid, "stepType") else "instant"
                if sn in INSTANT and st == "instant":
                    fields[INSTANT[sn]] = eccodes.codes_get_values(gid)
                elif sn in CUMUL and st in ("avg", "accum"):
                    fields[CUMUL[sn]] = eccodes.codes_get_values(gid)
                elif sn in SOIL:
                    sv = eccodes.codes_get(gid, "scaledValueOfFirstFixedSurface", float)
                    sf = eccodes.codes_get(gid, "scaleFactorOfFirstFixedSurface", float)
                    depth = sv * 10.0 ** (-sf)
                    if sn == "W_SO":  # layer: use the bottom bound of the layer
                        sv2 = eccodes.codes_get(gid, "scaledValueOfSecondFixedSurface", float)
                        sf2 = eccodes.codes_get(gid, "scaleFactorOfSecondFixedSurface", float)
                        depth = sv2 * 10.0 ** (-sf2)
                    soil.setdefault(SOIL[sn], {})[depth] = eccodes.codes_get_values(gid)
                elif wanted_invariant and sn in INVARIANT:
                    fields[INVARIANT[sn]] = eccodes.codes_get_values(gid)
            finally:
                eccodes.codes_release(gid)
    if not fields and not soil:
        return False
    data = {k: (("values",), v.astype("f4")) for k, v in fields.items()}
    for name, levs in soil.items():
        depths = sorted(levs)
        dim = "soil_depth" if name == "TSOIL" else "soil_layer_bottom"
        data[name] = ((dim, "values"), np.stack([levs[d] for d in depths]).astype("f4"))
        data.setdefault(dim, ((dim,), np.array(depths, "f4")))
    ds = xr.Dataset(data)
    for dim, ln in (("soil_depth", "depth below land surface"), ("soil_layer_bottom", "bottom of soil layer below land surface")):
        if dim in ds.coords:   # a real z-axis for cdo (it refuses 'generic' coordinates on remap)
            ds[dim].attrs = {"units": "m", "positive": "down", "axis": "Z", "long_name": ln, "standard_name": "depth"}
    ds.to_netcdf(out_nc)
    return True


def wrf_grid_description(wrfinput, out_txt):
    """CDO curvilinear grid description of the WRF mass grid (XLAT/XLONG of a wrfinput), cached in out_txt."""
    import netCDF4 as nc
    out_txt = Path(out_txt)
    if out_txt.exists():
        return out_txt
    ds = nc.Dataset(wrfinput)
    lat, lon = np.asarray(ds["XLAT"][0]), np.asarray(ds["XLONG"][0]); ds.close()
    ny, nx = lat.shape
    with open(out_txt, "w") as f:
        f.write(f"gridtype = curvilinear\ngridsize = {lat.size}\nxsize = {nx}\nysize = {ny}\n")
        f.write("xvals = " + " ".join(f"{v:.6f}" for v in lon.ravel()) + "\n")
        f.write("yvals = " + " ".join(f"{v:.6f}" for v in lat.ravel()) + "\n")
    log(f"wrote WRF grid description {out_txt} ({ny} x {nx})")
    return out_txt


def cdo_remap(in_nc, out_nc, source_grid, target_grid):
    cmd = ["cdo", "-s", "-O", "-f", "nc4", f"-remapdis,{target_grid}", f"-setgrid,{source_grid}", str(in_nc), str(out_nc)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cdo failed: {' '.join(cmd)}\n{r.stderr[-800:]}")


# --------------------------------------------------------------------------- FTP queue
def ftp_connect(url, user, password):
    for attempt in range(1, 8):
        try:
            ftp = ftplib.FTP(url, timeout=120)
            ftp.login(user, password)
            return ftp
        except Exception as e:
            log(f"[WARNING] FTP connect failed (attempt {attempt}): {e}")
            time.sleep(min(60, 2 ** attempt + random.uniform(0, 5)))
    raise RuntimeError("FTP connection failed")


def build_queue(ftp, start, end, spinup, log=log):
    """{run_dir: [(target_dt, gz, is_prefetch), ...]} in processing order; 'freshest' strategy."""
    items = []
    ftp.dir(items.append)
    runs = []
    for it in items:
        p = it.split()
        if p and p[0].startswith("d"):
            try:
                runs.append((datetime.strptime(p[-1], "%Y%m%d_%H"), p[-1]))
            except ValueError:
                pass
    runs.sort()
    cache = {}

    def has(d, f):
        if d not in cache:
            try:
                cache[d] = ftp.nlst(d)
            except Exception:
                cache[d] = []
        return any(x == f or x.endswith("/" + f) for x in cache[d])

    def best(target, lo, hi):
        for r_dt, d in reversed([r for r in runs if lo <= (target - r[0]).total_seconds() // 3600 <= hi]):
            off = int((target - r_dt).total_seconds() // 3600)
            gz = get_filename_for_offset(off)
            if has(d, gz):
                return r_dt, d, gz, off
        return None

    queue, missing = {}, []
    t = start
    while t <= end:
        pick = best(t, spinup, 48) or best(t, 1, spinup - 1)
        if pick is None:
            missing.append(t)
        else:
            r_dt, d, gz, off = pick
            if d not in queue:
                queue[d] = []
                if off > 0 and has(d, get_filename_for_offset(off - 1)):
                    queue[d].append((t - timedelta(hours=1), get_filename_for_offset(off - 1), True))
            queue[d].append((t, gz, False))
        t += timedelta(hours=1)
    if missing:
        log(f"[WARNING] no file for {len(missing)} hours: {[m.strftime('%Y%m%d%H') for m in missing][:10]} ...")
    return queue, missing


# --------------------------------------------------------------------------- derived fields
def derive(ds, run_dir, lead_h, state):
    """De-accumulate the run-cumulative fields against the previous lead of the same run and build the
    HRLDAS variables. `state[run_dir]` holds the previous lead's cumulative fields."""
    import xarray as xr

    prev = state.get(run_dir)
    out = ds[[v for v in ("T2D", "U2D", "V2D", "PSFC", "TG", "SNOWH", "SNOWC", "ALBEDO", "TSOIL", "WSOIL") if v in ds]]
    td = ds["TD2"]
    e = 6.112 * np.exp(17.67 * (td - 273.15) / (td - 29.65)) * 100.0     # Pa, over water
    out["Q2D"] = (0.622 * e / (ds["PSFC"] - 0.378 * e)).astype("f4")
    out["Q2D"].attrs = {"long_name": "2 m specific humidity from 2 m dew point", "units": "kg kg-1"}
    if prev is not None and prev["lead"] < lead_h:
        dt = (lead_h - prev["lead"]) * 3600.0
        def hourly_mean(name):   # run-mean since start -> mean over the last interval
            return (ds[name] * lead_h * 3600.0 - prev[name] * prev["lead"] * 3600.0) / dt
        sw = hourly_mean("swdir") + hourly_mean("swdif")
        lwnet = hourly_mean("lwnet")
        out["SHFLX"] = (-hourly_mean("SHFLX")).astype("f4"); out["LHFLX"] = (-hourly_mean("LHFLX")).astype("f4")   # ICON: positive downward -> upward positive
        out["RAINRATE"] = (xr.where(ds["tp"] - prev["tp"] < 0, 0.0, ds["tp"] - prev["tp"]) / dt).astype("f4")
        tg_mean = 0.5 * (ds["TG"] + prev["TG"])
        interval = f"mean over lead {prev['lead']}-{lead_h} h of run {run_dir}"
    else:   # first lead of a run without its predecessor: the run-mean since start is the best available
        sw = ds["swdir"] + ds["swdif"]; lwnet = ds["lwnet"]
        out["SHFLX"] = (-ds["SHFLX"]).astype("f4"); out["LHFLX"] = (-ds["LHFLX"]).astype("f4")
        out["RAINRATE"] = (ds["tp"] / max(lead_h, 1) / 3600.0).astype("f4")
        tg_mean = ds["TG"]
        interval = f"run-mean since start of {run_dir} (no previous lead)"
    out["SWDOWN"] = xr.where(sw < 0, 0.0, sw).astype("f4")
    out["LWDOWN"] = (lwnet + SB * tg_mean ** 4).astype("f4")
    # `interval` describes the rule, not this piece: cdo mergetime keeps only the first piece's variable
    # attributes, so a per-piece text would end up frozen in the merged file
    generic = ("hourly mean over the hour ending at the time stamp, de-accumulated against the previous lead of "
               "the same run (run-mean since start where a run has no previous lead)")
    for v, ln, u in (("SWDOWN", "downwelling shortwave at the surface, direct + diffuse", "W m-2"),
                     ("LWDOWN", "downwelling longwave at the surface = net LW + sigma*TG^4 (emissivity 1)", "W m-2"),
                     ("RAINRATE", "precipitation rate", "kg m-2 s-1"), ("SHFLX", "ICON sensible heat flux, upward positive (sign flipped from ICON)", "W m-2"),
                     ("LHFLX", "ICON latent heat flux, upward positive (sign flipped from ICON)", "W m-2")):
        out[v].attrs = {"long_name": ln, "units": u, "interval": generic}
    for v, (ln, u) in META.items():
        if v in out:
            out[v].attrs = {"long_name": ln, "units": u}
    out.attrs["interval"] = interval
    state[run_dir] = {"lead": lead_h, "TG": ds["TG"], **{k: ds[k] for k in ("swdir", "swdif", "lwnet", "SHFLX", "LHFLX", "tp")}}
    return out


# --------------------------------------------------------------------------- provenance
FLAG_MEANINGS = ("normal_freshest_run_within_the_12h_window", "lead_below_spinup_fallback", "older_run_newer_run_missing_on_server",
                 "filled_no_data_on_server", "missing_not_filled")


def provenance_variables(full, prov, spinup):
    """source_run / lead_hours / data_flag along the time axis (data_flag: 0 normal = lead spinup..spinup+11 h,
    1 lead below spinup, 2 older run (lead above spinup+11 h), 3 filled, 4 missing). Flag 4 -> 3 is set by fill_gaps."""
    import xarray as xr
    n = len(full); run = np.zeros(n, "i4"); lead = np.full(n, -1, "i2"); flag = np.full(n, 4, "i1")   # i4: cdo has no int64
    for i, t in enumerate(full):
        p = prov.get(datetime.utcfromtimestamp(int(t.astype("datetime64[s]").astype(int))))
        if p is not None:
            run[i] = int(p[0].replace("_", "")); lead[i] = p[1]
            flag[i] = 0 if spinup <= p[1] <= spinup + 11 else (1 if p[1] < spinup else 2)
    return {"source_run": xr.DataArray(run, dims="time", attrs={"long_name": "ICON run the hour was taken from, as YYYYMMDDHH (0 = no data)"}),
            "lead_hours": xr.DataArray(lead, dims="time", attrs={"long_name": "forecast lead of that run in hours (-1 = no data)", "units": "h"}),
            "data_flag": xr.DataArray(flag, dims="time", attrs={"long_name": "provenance of the hour", "flag_values": np.arange(5, dtype="i1"),
                                                                 "flag_meanings": " ".join(FLAG_MEANINGS)})}


def provenance_summary(nc, stamp):
    """Text for the global attribute provenance_summary from the data_flag variable (blocks per class)."""
    flag = nc["data_flag"][:]; run = nc["source_run"][:]; lead = nc["lead_hours"][:]
    def blocks(k):
        idx = np.where(flag == k)[0]; out = []
        for i in idx:
            if out and i == out[-1][1] + 1: out[-1][1] = i
            else: out.append([i, i])
        return "; ".join(f"{stamp(a)}..{stamp(b)}" + (f" (run {run[a]}, leads {lead[a]}-{lead[b]})" if k in (1, 2) else "") for a, b in out) or "-"
    return f"{len(flag)} hours: " + "; ".join(f"{int((flag == k).sum())} {FLAG_MEANINGS[k]}" + (f": {blocks(k)}" if k and (flag == k).any() else "")
                                                for k in range(5)) + ". Per hour: variables source_run, lead_hours, data_flag."


# --------------------------------------------------------------------------- gap filling
def fill_gaps(path, method="auto", short=3):
    """Fill the hours of an hourly series that hold no data (all-NaN) in place, one gap block at a time.

    linear   between the last valid hour before and the first valid hour after the block (what
             xarray's interpolate_na('time') does; fine for a few hours, but a gap spanning a night
             bracketed by daytime hours gets daytime radiation all night).
    diurnal  same clock hour of the day before and the day after, weighted linearly across the block,
             plus a linearly blended offset so state variables join the bracket hours without a step
             (not applied to the intermittent fluxes SWDOWN/RAINRATE/SHFLX/LHFLX); falls back to what is
             available where a day-before/after reference is itself missing, and to linear where none is.
    auto     linear for blocks of <= `short` hours, diurnal for longer ones (default).
    Blocks without a valid hour on both sides (series edges) stay NaN and are listed in `unfilled_hours`;
    this is why the wrapper runs the pass once over the merged series instead of per chunk.
    Values are clipped to physical bounds (BOUNDS). Sets the global attributes filled_hours,
    unfilled_hours and fill_method. Streams: a handful of single-hour fields in memory at a time."""
    import netCDF4
    from datetime import timedelta as _td
    with netCDF4.Dataset(path, "r+") as nc:
        nc.set_auto_mask(False)
        tv = nc["time"]
        times = netCDF4.num2date(tv[:], tv.units, getattr(tv, "calendar", "standard"), only_use_cftime_datetimes=False)
        stamp = lambda i: (times[i] + _td(minutes=30)).strftime("%Y-%m-%dT%H")     # nearest hour
        tvars = [v for v in nc.variables if v != "time" and "time" in nc[v].dimensions and nc[v].ndim >= 3]
        ind = "T2D" if "T2D" in tvars else tvars[0]
        nt = len(times)
        empty = np.array([bool(np.isnan(nc[ind][i, ...]).all()) for i in range(nt)])
        blocks, i = [], 0
        while i < nt:
            if empty[i]:
                j = i
                while j + 1 < nt and empty[j + 1]:
                    j += 1
                blocks.append((i, j)); i = j + 1
            else:
                i += 1
        row = lambda v, i: nc[v][i, ...].astype("f8")
        valid = lambda i: 0 <= i < nt and not empty[i]
        filled, unfilled, used = [], [], set()
        for i0, i1 in blocks:
            a, b = i0 - 1, i1 + 1
            if a < 0 or b >= nt:
                unfilled += list(range(i0, i1 + 1)); continue
            n = i1 - i0 + 1
            m = method if method != "auto" else ("linear" if n <= short else "diurnal")
            for v in tvars:
                A, B = row(v, a), row(v, b)
                cA = A - row(v, a - 24) if valid(a - 24) else None
                cB = B - row(v, b + 24) if valid(b + 24) else None
                for i in range(i0, i1 + 1):
                    w = (b - i) / (b - a)                      # 1 next to a, 0 next to b
                    est = w * A + (1 - w) * B                  # linear
                    if m == "diurnal":
                        p = row(v, i - 24) if valid(i - 24) else None
                        q = row(v, i + 24) if valid(i + 24) else None
                        if p is not None and q is not None:
                            est = w * p + (1 - w) * q
                        elif p is not None or q is not None:
                            est = p if p is not None else q
                        if (p is not None or q is not None) and v not in INTERMITTENT:
                            if cA is not None and cB is not None:
                                est = est + w * cA + (1 - w) * cB
                            elif cA is not None or cB is not None:
                                est = est + (cA if cA is not None else cB)
                    lo, hi = BOUNDS.get(v, (None, None))
                    if lo is not None or hi is not None:
                        est = np.clip(est, lo, hi)
                    nc[v][i, ...] = est.astype(nc[v].dtype)
            filled += list(range(i0, i1 + 1)); used.add(m)
            log(f"filled {n} h {stamp(i0)}..{stamp(i1)} ({m})")
        if "data_flag" in nc.variables:
            flag = nc["data_flag"][:]; flag[filled] = 3; flag[unfilled] = 4; nc["data_flag"][:] = flag
            nc.setncattr("provenance_summary", provenance_summary(nc, stamp))
        nc.setncattr("filled_hours", " ".join(stamp(i) for i in filled) if filled else "none")
        nc.setncattr("unfilled_hours", " ".join(stamp(i) for i in unfilled) if unfilled else "none")
        nc.setncattr("fill_method", f"--fill {method}" + (f" (used: {', '.join(sorted(used))})" if used else "") +
                     "; linear = between the bracket hours; diurnal = same hour of the day before/after, weighted across the "
                     "gap, offset-blended to the bracket hours except SWDOWN/RAINRATE/SHFLX/LHFLX; clipped to physical bounds")
    log(f"gap filling: {len(filled)} hours filled, {len(unfilled)} left NaN (series edge) in {path}")
    return filled, unfilled


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", help="first target hour YYYYMMDDHH (UTC)")
    ap.add_argument("--end", help="last target hour YYYYMMDDHH (UTC)")
    ap.add_argument("--out", help="output NetCDF (one file, hourly)")
    ap.add_argument("--spinup", type=int, default=9, help="minimum forecast lead in hours (default 9)")
    ap.add_argument("--work", help="scratch dir for the raw files (default: <input_dir>/sandbox_surface_<id>)")
    ap.add_argument("--keep-raw", action="store_true", help="do not delete the raw hourly files (debug)")
    ap.add_argument("--source-grid", default="config/source_grid.txt")
    ap.add_argument("--target-grid", default="config/target_grid.txt", help="CDO grid description of the output grid (lon-lat product grid by default)")
    ap.add_argument("--wrf-grid", help="a wrfinput_d01: remap straight onto its mass grid (south_north x west_east) instead of --target-grid")
    ap.add_argument("--fill", default="auto", choices=("auto", "linear", "diurnal"), help="how missing hours are filled (see fill_gaps; default auto)")
    ap.add_argument("--no-fill", action="store_true", help="leave missing hours as NaN (the wrapper fills once over the merged series)")
    ap.add_argument("--fill-only", metavar="FILE", help="only run the gap-filling pass in place on an existing hourly series and exit")
    args = ap.parse_args()
    import xarray as xr

    if args.fill_only:
        fill_gaps(args.fill_only, args.fill)
        return
    if not (args.start and args.end and args.out):
        ap.error("--start, --end and --out are required (or use --fill-only FILE)")
    if args.wrf_grid:
        args.target_grid = str(wrf_grid_description(args.wrf_grid, Path("config") / f"wrf_grid_{Path(args.wrf_grid).stem}_{Path(args.wrf_grid).parent.name}.txt"))
    start, end = datetime.strptime(args.start, "%Y%m%d%H"), datetime.strptime(args.end, "%Y%m%d%H")
    out_nc = Path(args.out); out_nc.parent.mkdir(parents=True, exist_ok=True)
    work = Path(args.work) if args.work else Path("input") / f"sandbox_surface_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    creds = load_credentials(); url, user = creds.get("url"), creds.get("username")
    password = os.environ.get("FTP_PASSWORD")
    if not (url and user and password):
        sys.exit("FTP url/username (config/credentials.toml) and FTP_PASSWORD (environment) are required")
    ftp = ftp_connect(url, user, password)
    queue, missing = build_queue(ftp, start, end, args.spinup)
    n_total = sum(len(v) for v in queue.values())
    log(f"{n_total} files to stream for {int((end - start).total_seconds() // 3600) + 1} target hours from {len(queue)} runs")

    state, pieces, hsurf_nc, prov = {}, [], None, {}      # prov: target hour -> (run_dir, lead)
    for run_dir, items in queue.items():
        for k in range(3):
            try:
                ftp.cwd("/" + run_dir); break
            except Exception:
                ftp = ftp_connect(url, user, password)
        fetch = list(items)
        if hsurf_nc is None:
            fetch.insert(0, (None, get_filename_for_offset(0), True))     # lead 0: HSURF (first run that has one)
        for target, gz, is_prefetch in fetch:
            raw = work / (gz[:-3] if target is None else f"{target:%Y%m%d%H}_{gz[:-3]}")
            if not raw.exists():
                tmp = raw.with_suffix(".gz")
                for attempt in range(1, 6):
                    try:
                        try:
                            ftp.voidcmd("NOOP")
                        except Exception:
                            ftp = ftp_connect(url, user, password); ftp.cwd("/" + run_dir)
                        with open(tmp, "wb") as fh:
                            ftp.retrbinary(f"RETR {gz}", fh.write)
                        extract_gz(tmp, raw); break
                    except Exception as e:
                        log(f"[WARNING] {run_dir}/{gz} attempt {attempt}: {e}")
                        tmp.unlink(missing_ok=True); time.sleep(min(60, 2 ** attempt))
                else:
                    log(f"[ERROR] giving up on {run_dir}/{gz}; {target} left as a gap")
                    if target is not None: missing.append(target)
                    continue
            lead_h = int(gz[5:7]) * 24 + int(gz[7:9])
            raw_nc, rem_nc = work / (raw.name + "_fields.nc"), work / (raw.name + "_grid.nc")
            if target is None:                                     # invariant terrain
                if extract_fields(raw, raw_nc, wanted_invariant=True):
                    cdo_remap(raw_nc, rem_nc, args.source_grid, args.target_grid); hsurf_nc = rem_nc
                if not args.keep_raw: raw.unlink(missing_ok=True)
                raw_nc.unlink(missing_ok=True); continue
            ok = extract_fields(raw, raw_nc)
            if not args.keep_raw: raw.unlink(missing_ok=True)
            if not ok:
                log(f"[ERROR] no fields in {raw.name}; {target} left as a gap"); missing.append(target); continue
            cdo_remap(raw_nc, rem_nc, args.source_grid, args.target_grid); raw_nc.unlink(missing_ok=True)
            ds = xr.open_dataset(rem_nc).load(); rem_nc.unlink(missing_ok=True)
            der = derive(ds, run_dir, lead_h, state)
            if is_prefetch:
                continue
            der = der.expand_dims(time=[np.datetime64(target, "ns")]); prov[target] = (run_dir, lead_h)
            der.attrs["source_run"] = run_dir; der.attrs["lead_hours"] = lead_h
            piece = work / f"piece_{target:%Y%m%d%H}.nc"
            der.to_netcdf(piece, encoding={v: {"zlib": True, "complevel": 4} for v in der.data_vars}); pieces.append(piece)
            log(f"{target:%Y-%m-%d %H} UT <- {run_dir} lead {lead_h:2d} h   ({len(pieces)} hours done)")
    try:
        ftp.quit()
    except Exception:
        pass
    if not pieces:
        sys.exit("nothing produced")
    merged = work / "merged.nc"
    r = subprocess.run(["cdo", "-s", "-O", "-f", "nc4", "-z", "zip_4", "mergetime"] + [str(p) for p in sorted(pieces)] + [str(merged)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        merged.unlink(missing_ok=True)       # the pieces stay for a rerun / manual merge
        sys.exit(f"cdo mergetime failed: {r.stderr[-800:]}")
    ds = xr.open_dataset(merged).load()
    full = np.arange(np.datetime64(start, "ns"), np.datetime64(end, "ns") + np.timedelta64(1, "h"), np.timedelta64(1, "h"))
    n_missing = len(full) - len(ds.time)
    ds = ds.reindex(time=full)               # missing hours become all-NaN steps; fill_gaps handles them
    ds = ds.assign(provenance_variables(full, prov, args.spinup))
    if hsurf_nc is not None and hsurf_nc.exists():
        ds["HSURF"] = xr.open_dataset(hsurf_nc)["HSURF"].load(); ds["HSURF"].attrs = dict(zip(("long_name", "units"), META["HSURF"]))
    ds.attrs.update({"title": "ICON 500 m (TEAMx sEOP) hourly surface forcing for an offline land-surface model",
                     "source": "ACINN FTP, freshest run with lead >= %d h; icon2wrf surface_series.py" % args.spinup,
                     "grid": grid_description_text(args.target_grid, args.wrf_grid),
                     "filled_hours": "none", "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")})
    ds.attrs.pop("interval", None)
    enc = {v: {"zlib": True, "complevel": 4} for v in ds.data_vars}
    ds.to_netcdf(out_nc, encoding=enc); ds.close()
    for p in pieces: p.unlink(missing_ok=True)
    merged.unlink(missing_ok=True)
    if hsurf_nc is not None: hsurf_nc.unlink(missing_ok=True)
    try:
        work.rmdir()
    except OSError:
        pass
    log(f"wrote {out_nc}  ({len(full)} hours, {n_missing} missing" + (", left NaN for the final fill pass)" if args.no_fill else ")"))
    if n_missing and not args.no_fill:
        fill_gaps(out_nc, args.fill)


def grid_description_text(target_grid, wrf_grid=None):
    """Human-readable description of the output grid for the global attribute `grid`."""
    if wrf_grid:
        return f"WRF mass grid of {wrf_grid} (south_north x west_east), HRLDAS-ready"
    try:
        gridtype = next(l.split("=")[1].strip() for l in open(target_grid) if l.strip().startswith("gridtype"))
    except (OSError, StopIteration):
        gridtype = "unknown"
    if gridtype == "curvilinear":
        return f"curvilinear target grid from CDO grid description {target_grid} (a WRF mass grid, south_north x west_east: HRLDAS-ready)"
    return f"{target_grid} ({gridtype} product grid); interpolate to the land-model grid downstream"


if __name__ == "__main__":
    main()
