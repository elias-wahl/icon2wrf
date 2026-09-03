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

## Deploying on a New Cluster (openAMUNDSEN Season Runs)

The `--profile openamundsen` pipeline (`orchestrator.py --profile openamundsen`, driven by `submit_season_32cores.sh` / `submit_season_devel_test.sh`) streams ICON GRIB files straight from the FTP server and produces a stitched openAMUNDSEN forcing NetCDF for a `--start`/`--end` window. `git clone` alone is **not** enough to run it — the following are gitignored or cluster-specific and must be set up on the new machine first:

1. **FTP credentials**: create `config/credentials.toml` with your FTP URL and username (or add an `[ftp]` section to `config/config.toml`):
   ```toml
   [ftp]
   url = "your.ftp.server"
   username = "your_username"
   ```
   Without this, the season scripts fail immediately with `FATAL ERROR: FTP Connection Failed` — they only prompt interactively for the *password*, not the URL/username.
2. **FTP password**: create a `.ftp_pass` file (gitignored) in the repo root containing just the plaintext password, or export `FTP_PASSWORD` yourself before submitting the job.
3. **Python environment**: create/activate a conda env (the scripts assume it's named `icon`) and install this package and its dependencies:
   ```bash
   conda create -n icon python=3.11
   conda activate icon
   conda install -c conda-forge cdo
   pip install -e .
   ```
4. **CDO module**: the submit scripts run `module load cdo` before activating conda — if the new cluster doesn't have a `cdo` environment module, remove that line and rely on the conda-installed `cdo` binary instead.
5. **SLURM partition/QoS names**: `submit_season_32cores.sh` and `submit_season_devel_test.sh` hardcode `--partition=zen3_0512` and `--qos=zen3_0512*`, which are specific to this cluster's queue names. Update those (and `--mail-user`) to match the new cluster before submitting.

`config/config.toml`, `source_grid.txt` and `target_grid.txt` are tracked in git and come with the clone as-is; `config/oetztal_grid.nc` is gitignored but self-generates on first run via `ensure_oetztal_grid()`, so no action is needed there.

Once the above is in place: `sbatch submit_season_devel_test.sh` first as a smoke test (10 min wall time, same config as production), then `sbatch submit_season_32cores.sh` for the full season run.

## Vertical modes of the 3-D product (2026-09-03)

`python -m src.icon2wrf.orchestrator [--vertical native|plevs|isobaric] [--out-dir DIR]`

| mode | source | levels written | WPS Vtable (3-D step) | notes |
|---|---|---|---|---|
| `native` (**default**) | 65 ICON model levels (`generalVerticalLayer`, needs the lead-0 file for HHL) | 65, untouched: t, u, v, q, pres, h (geometric height) on GRIB2 level type 150 | `Vtable.ICONm` | no vertical interpolation; ~880 MB/h; metgrid/real use PRESSURE and HGT per level (validated 2026-09-03: met_em 66 levels, real.exe OK) |
| `plevs` (alias `--ml-plevs`) | same 65 model levels | 36 pressure levels: 10 hPa steps 1000–800, 20 hPa to 700, 50 hPa to 200; linear in ln p per column, hypsometric extrapolation below the lowest layer | `Vtable.ICONp` | ~410 MB/h; the A21 fix run X12 |
| `isobaric` | ICON's own 11 diagnostic pressure levels (1000 … 200 hPa) | 11: z, t, r, u, v, w | `Vtable.ICONp` | the pre-2026-09 product; nothing between ~584 and ~1522 m ASL (branko/OPEN_ISSUES A19/A21) |

The model-level fields are CCSDS/AEC-packed; cdo cannot decode them (KNOWN_ISSUES E38), python-eccodes/cfgrib can — hence the Python extraction step.
The SFC and ICON_INIT ungrib steps must use a Vtable **without** 3-D rows (`Vtable.ICONsfc`) for surface files made before commit `9069aee`, which carried isobaric geopotential (KNOWN_ISSUES E39).

## Under the Hood (Technical Details)
This package includes several advanced fail-safes and workarounds specific to processing complex ICON data for WRF:
- **AEC Compression Bypass**: The script uses `xarray` and `cfgrib` in Python to extract the GRIB fields first, bypassing the notorious `eccodes` AEC compression errors that often cause standard CDO binaries or Docker containers to crash on ICON data.
- **Advanced Soil Splitting**: In ICON, Soil Moisture (`W_SO`) and Soil Temperature (`T_SO`) are frequently stored on conflicting vertical coordinates (`depthBelowLandLayer` vs `depthBelowLand`). The orchestrator automatically splits, extracts, and regrids these streams separately to prevent hypercube crashes.
- **Smart Level Inversion**: WRF expects pressure levels to be strictly descending. The script checks your vertical layer structure and automatically applies CDO's `-invertlev` argument if necessary.
- **Clean Input Filtering**: The script automatically filters out `.nc` files and background `.idx` files from the input directory so they don't break the batch loop.
