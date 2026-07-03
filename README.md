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

## Under the Hood (Technical Details)
This package includes several advanced fail-safes and workarounds specific to processing complex ICON data for WRF:
- **AEC Compression Bypass**: The script uses `xarray` and `cfgrib` in Python to extract the GRIB fields first, bypassing the notorious `eccodes` AEC compression errors that often cause standard CDO binaries or Docker containers to crash on ICON data.
- **Advanced Soil Splitting**: In ICON, Soil Moisture (`W_SO`) and Soil Temperature (`T_SO`) are frequently stored on conflicting vertical coordinates (`depthBelowLandLayer` vs `depthBelowLand`). The orchestrator automatically splits, extracts, and regrids these streams separately to prevent hypercube crashes.
- **Smart Level Inversion**: WRF expects pressure levels to be strictly descending. The script checks your vertical layer structure and automatically applies CDO's `-invertlev` argument if necessary.
- **Clean Input Filtering**: The script automatically filters out `.nc` files and background `.idx` files from the input directory so they don't break the batch loop.
