# ICON-to-WRF Regridder

This package was originally tailored specifically to regrid the special high-resolution (500m) **ICON runs for the TEAMx campaign** into WRF forcing format suitable for `ungrib.exe` and `metgrid.exe`. 

**However, it is now capable of transforming any standard ICON unstructured GRIB files!** When running the script, you will be prompted to either use the pre-configured TEAMx grids or dynamically calculate a new source and target grid on-the-fly from your own specific ICON domain file.

## Features
- **Batch Processing**: Automatically scans the `input/` folder and processes any files that haven't been completed in the `output/` folder.
- **Dynamic Grid Generation**: Interactively generate temporary source and target grids specifically for your custom ICON domain, allowing you to process non-TEAMx datasets without overwriting default grid templates.
- **Surface & 3D Separation**: Automatically extracts surface fields into a separate stream to bypass `cfgrib` hypercube collision issues.
- **WRF Diagnostics**: Verifies vertical levels and expected standard meteorological variables, making safe adjustments like `invertlev` for WRF compatibility.
- **Vtable Recommendations**: Analyzes the structure of your data and informs you whether to use `Vtable.ICONp` or `Vtable.ICONm`.

## Installation

This package requires Python 3.9+ and the **Climate Data Operators (CDO)** system binary. CDO must be installed separately as it is a system-level dependency and cannot be installed via `pip` in `pyproject.toml`.

### 1. Install CDO

**Using Conda (Recommended):**
```bash
conda install -c conda-forge cdo
```

**Using Ubuntu/Debian:**
```bash
sudo apt-get install cdo
```

**Using macOS (Homebrew):**
```bash
brew install cdo
```

### 2. Install Python Package
Once CDO is installed on your system, you can install this package and its Python dependencies:
```bash
pip install -e .
```

## Usage
1. (Optional) Run the automated FTP downloader to fetch your data:
```bash
./download_data.sh
```
   - It will prompt for your FTP credentials, securely hiding your password.
   - You can choose to save the URL and Username to `config.toml` for future use.
   - Enter your desired start and end times (`YYYYMMDDHH`) and it will automatically crawl the FTP, download the matching `ilf*` files, unzip them, and place them cleanly in your `input/` folder!

2. Place any additional raw `ilf*` GRIB files into the `input/` directory manually.
3. Edit `config.toml` if you need to customize directory paths.
4. Run the main executable script:
```bash
./run_regrid.sh
```
5. **Follow the interactive prompt**:
   - Choose `1` to use the pre-calculated standard grid configurations for the TEAMx 500m campaign.
   - Choose `2` and provide the path to your custom ICON domain file (e.g. an invariant grid file or the first timestep of your dataset) if you are processing a different ICON domain. The script will automatically calculate the required grids!

6. Link the generated `.grib2` files in your `output/` folder to your WPS working directory using `link_grib.csh` and run `ungrib.exe`.

> **IMPORTANT - WPS Namelist**: Since ICON provides its own pressure fields natively, you do not need to generate `PRES` files (e.g. via `calc_ecmwf_p.exe`). Make sure that your `namelist.wps` does **NOT** include `'PRES'` in the `fg_name` parameter of the `&metgrid` section (e.g., use `fg_name = 'FILE'` or `fg_name = 'FILE', 'SFC'`, but not `'PRES'`). Otherwise, `metgrid.exe` will crash complaining about missing `PRES` files.

> **Note on WRF Vtables**: If you are using an older version of WRF/WPS that does not natively include the Vtables for ICON, we have included `Vtable.ICONp` and `Vtable.ICONm` in the `vtables/` directory of this repository! You can simply copy or symlink them to your WPS folder.


## Vertical modes of the 3-D product (2026-09-03)

`python -m src.icon2wrf.orchestrator [--vertical native|plevs|isobaric] [--out-dir DIR]`

| mode | source | levels written | WPS Vtable (3-D step) | notes |
|---|---|---|---|---|
| `native` (**default**) | 65 ICON model levels (`generalVerticalLayer`, needs the lead-0 file for HHL) | 65, untouched: t, u, v, q, pres, h (geometric height) on GRIB2 level type 150 | `Vtable.ICONm` | no vertical interpolation; ~880 MB/h; metgrid/real use PRESSURE and HGT per level (validated 2026-09-03: met_em 66 levels, real.exe OK) |
| `plevs` (alias `--ml-plevs`) | same 65 model levels | 36 pressure levels: 10 hPa steps 1000–800, 20 hPa to 700, 50 hPa to 200; linear in ln p per column, hypsometric extrapolation below the lowest layer | `Vtable.ICONp` | ~410 MB/h; the A21 fix run X12 |
| `isobaric` | ICON's own 11 diagnostic pressure levels (1000 … 200 hPa) | 11: z, t, r, u, v, w | `Vtable.ICONp` | the pre-2026-09 product; nothing between ~584 and ~1522 m ASL (branko/OPEN_ISSUES A19/A21) |

The model-level fields are CCSDS/AEC-packed; cdo cannot decode them (KNOWN_ISSUES E38), python-eccodes/cfgrib can — hence the Python extraction step.
The SFC and ICON_INIT ungrib steps must use a Vtable **without** 3-D rows (`Vtable.ICONsfc`) for surface files made before commit `9069aee`, which carried isobaric geopotential (KNOWN_ISSUES E39).

WPS Vtables for all three modes are kept in `vtables/`: `Vtable.ICONm` (native model levels, GRIB2 level type 150), `Vtable.ICONp` (pressure levels: ladder and isobaric), `Vtable.ICONsfc` (surface/soil steps, 2-D rows only). Copy them to `WPS/ungrib/Variable_Tables/`.

## Under the Hood (Technical Details)
This package includes several advanced fail-safes and workarounds specific to processing complex ICON data for WRF:
- **AEC Compression Bypass**: The script uses `xarray` and `cfgrib` in Python to extract the GRIB fields first, bypassing the notorious `eccodes` AEC compression errors that often cause standard CDO binaries or Docker containers to crash on ICON data.
- **Advanced Soil Splitting**: In ICON, Soil Moisture (`W_SO`) and Soil Temperature (`T_SO`) are frequently stored on conflicting vertical coordinates (`depthBelowLandLayer` vs `depthBelowLand`). The orchestrator automatically splits, extracts, and regrids these streams separately to prevent hypercube crashes.
- **Smart Level Inversion**: WRF expects pressure levels to be strictly descending. The script checks your vertical layer structure and automatically applies CDO's `-invertlev` argument if necessary.
- **Clean Input Filtering**: The script automatically filters out `.nc` files and background `.idx` files from the input directory so they don't break the batch loop.

## Surface forcing series for an offline land-surface model (2026-09-15)

`./run_surface_series.sh START END [OUT.nc] [JOBS]` streams the hourly ICON files from the FTP
(download -> extract -> regrid -> delete, one raw file on disk per job) and writes ONE hourly NetCDF
on the lon-lat product grid with what HRLDAS / Noah-MP needs — `T2D Q2D U2D V2D PSFC SWDOWN LWDOWN
RAINRATE` — plus ICON's own `TG SHFLX LHFLX TSOIL(9 depths) WSOIL(8 layers) SNOWH SNOWC ALBEDO HSURF`.
Run-cumulative fields are de-averaged/differenced against the previous lead of the same run; the
stitching rule is "freshest run with lead >= 9 h" (`--spinup`); `LWDOWN = net LW + sigma*TG^4`.
Ported from branch `icon2amundsen` (download-filter-delete) and reduced: `src/icon2wrf/surface_series.py`.
By default the output is confined to the production WRF mass grid (500 x 600; 5th argument = a wrfinput_d01
or the committed `config/wrf_grid_wrfinput_d01_innval_pbl3d_X16b.txt`; `none` = full product grid, 3.8x larger).
Smoke test: `./run_surface_series.sh 2025071712 2025071715 /tmp/test.nc 1` (~90 s per hour, download-bound).
Three-month spin-up forcing: `./run_surface_series.sh 2025040100 2025071800 output/icon_surface_2025040100_2025071800.nc 8`.

Disk and time: the compressed hourly pieces take ~26 MB per hour during the run (a 3-month series ~65 GB, all of it
on disk until each job's final merge), each job's `cdo mergetime` adds a ~12 GB spike, and the result is again
~26 MB per hour (3 months ~65 GB). Make sure the quota covers that: at the hard limit downloads fail and every
hour so lost becomes a filled hour. With 8 jobs the FTP delivers roughly one hour of data per minute per job.

Missing hours: hours that no run on the server covers with a lead of 1-48 h stay NaN in the chunks (`--no-fill`)
and are filled once over the merged series by `fill_gaps` (`FILL=auto|linear|diurnal ./run_surface_series.sh ...`,
or `python -m src.icon2wrf.surface_series --fill-only FILE --fill diurnal`). `linear` interpolates between the
bracket hours; `diurnal` uses the same clock hour of the day before and after, weighted across the gap and
offset-blended to the bracket hours, so a gap spanning a night keeps its night; `auto` (default) takes linear
for gaps of up to 3 h and diurnal beyond. The filled hours are listed in the global attribute `filled_hours`,
the method in `fill_method`; gaps at the very start or end of the series stay NaN (`unfilled_hours`).

Check a finished series with `python -m src.icon2wrf.validate_surface_series FILE`: per hour and field it reports
NaNs, min/max against physical ranges, suspiciously constant fields and jumps of the domain mean, streaming the
file in 24-hour blocks (a 65 GB file takes ~15 min).

On a cluster whose `module load cdo` provides an old cdo, activate the conda env first: the wrapper only loads the
module when no `cdo` is on the PATH.
Motivation: OPEN_ISSUES A28 (the ICON soil state) in the WRF project.
