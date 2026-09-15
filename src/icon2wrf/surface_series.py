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
its lead-1 file is fetched too so the accumulated fields can be differenced. Missing hours are left
as gaps and time-interpolated at the end (listed in the global attribute `filled_hours`).

Output variables (HRLDAS LDASIN names where they exist), hourly, on the product grid:
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
    for v, ln, u in (("SWDOWN", "downwelling shortwave at the surface, direct + diffuse", "W m-2"),
                     ("LWDOWN", "downwelling longwave at the surface = net LW + sigma*TG^4 (emissivity 1)", "W m-2"),
                     ("RAINRATE", "precipitation rate", "kg m-2 s-1"), ("SHFLX", "ICON sensible heat flux, upward positive (sign flipped from ICON)", "W m-2"),
                     ("LHFLX", "ICON latent heat flux, upward positive (sign flipped from ICON)", "W m-2")):
        out[v].attrs = {"long_name": ln, "units": u, "interval": interval}
    state[run_dir] = {"lead": lead_h, "TG": ds["TG"], **{k: ds[k] for k in ("swdir", "swdif", "lwnet", "SHFLX", "LHFLX", "tp")}}
    return out


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", required=True, help="first target hour YYYYMMDDHH (UTC)")
    ap.add_argument("--end", required=True, help="last target hour YYYYMMDDHH (UTC)")
    ap.add_argument("--out", required=True, help="output NetCDF (one file, hourly)")
    ap.add_argument("--spinup", type=int, default=9, help="minimum forecast lead in hours (default 9)")
    ap.add_argument("--work", help="scratch dir for the raw files (default: <input_dir>/sandbox_surface_<id>)")
    ap.add_argument("--keep-raw", action="store_true", help="do not delete the raw hourly files (debug)")
    ap.add_argument("--source-grid", default="config/source_grid.txt")
    ap.add_argument("--target-grid", default="config/target_grid.txt")
    args = ap.parse_args()
    import xarray as xr

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

    state, pieces, hsurf_nc = {}, [], None
    first_dir = next(iter(queue))
    for run_dir, items in queue.items():
        for k in range(3):
            try:
                ftp.cwd("/" + run_dir); break
            except Exception:
                ftp = ftp_connect(url, user, password)
        fetch = list(items)
        if run_dir == first_dir:
            fetch.insert(0, (None, get_filename_for_offset(0), True))     # lead 0: HSURF
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
            der = der.expand_dims(time=[np.datetime64(target, "ns")])
            der.attrs["source_run"] = run_dir; der.attrs["lead_hours"] = lead_h
            piece = work / f"piece_{target:%Y%m%d%H}.nc"; der.to_netcdf(piece); pieces.append(piece)
            log(f"{target:%Y-%m-%d %H} UT <- {run_dir} lead {lead_h:2d} h   ({len(pieces)} hours done)")
    try:
        ftp.quit()
    except Exception:
        pass
    if not pieces:
        sys.exit("nothing produced")
    merged = work / "merged.nc"
    r = subprocess.run(["cdo", "-s", "-O", "mergetime"] + [str(p) for p in sorted(pieces)] + [str(merged)], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"cdo mergetime failed: {r.stderr[-800:]}")
    ds = xr.open_dataset(merged).load()
    filled = []
    if missing:
        full = np.arange(np.datetime64(start, "ns"), np.datetime64(end, "ns") + np.timedelta64(1, "h"), np.timedelta64(1, "h"))
        ds = ds.reindex(time=full)
        filled = [str(t)[:13] for t in full if t not in xr.open_dataset(merged).time.values]
        ds = ds.interpolate_na("time")
    if hsurf_nc is not None and hsurf_nc.exists():
        ds["HSURF"] = xr.open_dataset(hsurf_nc)["HSURF"].load(); ds["HSURF"].attrs = {"long_name": "ICON terrain height (lead-0 file)", "units": "m"}
    ds.attrs.update({"title": "ICON 500 m (TEAMx sEOP) hourly surface forcing for an offline land-surface model",
                     "source": "ACINN FTP, freshest run with lead >= %d h; icon2wrf surface_series.py" % args.spinup,
                     "grid": f"{args.target_grid} (lon-lat product grid); interpolate to the land-model grid downstream",
                     "filled_hours": " ".join(filled) if filled else "none", "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")})
    enc = {v: {"zlib": True, "complevel": 4} for v in ds.data_vars}
    ds.to_netcdf(out_nc, encoding=enc)
    for p in pieces: p.unlink(missing_ok=True)
    merged.unlink(missing_ok=True)
    if hsurf_nc is not None: hsurf_nc.unlink(missing_ok=True)
    try:
        work.rmdir()
    except OSError:
        pass
    log(f"wrote {out_nc}  ({len(ds.time)} hours, {len(filled)} interpolated)")


if __name__ == "__main__":
    main()
