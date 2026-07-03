import argparse
import os
import sys
import subprocess
import shutil
from pathlib import Path

# Fallback for Python < 3.11
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        print("[ERROR] Please install tomli for Python < 3.11: pip install tomli")
        sys.exit(1)

from .surface_extractor import extract_surface, extract_3d, extract_soil_moist, extract_soil_temp
from .diagnostics import check_wrf_ready

def run_cdo_regrid(input_nc, output_grib, source_grid, target_grid, invertlev=False):
    """Runs CDO to regrid NetCDF to GRIB2, suppressing harmless ECCODES warnings."""
    cmd = ["cdo", "-f", "grb2", "-b", "16", "-settunits,hours", f"remapdis,{target_grid}"]
    if invertlev:
        cmd.append("-invertlev")
    cmd.extend([f"-setgrid,{source_grid}", str(input_nc), str(output_grib)])
    
    print(f"Running: {' '.join(cmd)}")
    process = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    stdout, stderr = process.communicate()
    
    if process.returncode != 0:
        print(f"[ERROR] CDO failed with exit code {process.returncode}")
        print(stderr)
        return False
        
    # Filter harmless warnings for cleaner output
    harmless = ["ECCODES ERROR", "grib_set_string", "gribapiDefParam", "cdfInqContents", "Parameter Database", "Changed zaxis type", "set_coordinates_varids"]
    filtered_err = [line for line in stderr.splitlines() if not any(h in line for h in harmless)]
    
    for line in filtered_err:
        if line.strip():
            print(line)
            
    return True

def fix_time_metadata(grib_file, datestr):
    """Fixes the time metadata in GRIB2 files that CDO corrupts by resetting it using wgrib2 or eccodes."""
    # Extract date from first 10 chars (e.g. 2019100100)
    if len(datestr) < 10 or not datestr[:10].isdigit():
        print(f"[WARNING] Could not parse valid date from filename: {datestr}. Skipping time metadata fix.")
        return False

    yyyy = datestr[:4]
    mm = datestr[4:6]
    dd = datestr[6:8]
    hh = datestr[8:10]
    
    temp_file = grib_file.with_name(f"temp_fix_{grib_file.name}")
    
    if shutil.which("wgrib2"):
        cmd = ["wgrib2", str(grib_file), "-set_date", f"{yyyy}{mm}{dd}{hh}", "-set_ftime", "0 hours", "-grib", str(temp_file)]
        process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        _, stderr = process.communicate()
        if process.returncode == 0:
            temp_file.replace(grib_file)
            return True
        else:
            print(f"[ERROR] wgrib2 failed to fix time on {grib_file.name}: {stderr}")
            if temp_file.exists(): temp_file.unlink()
            return False
            
    elif shutil.which("grib_set"):
        cmd = ["grib_set", "-s", f"indicatorOfUnitOfTimeRange=1,forecastTime=0,dataDate={yyyy}{mm}{dd},dataTime={hh}00", str(grib_file), str(temp_file)]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _, stderr = process.communicate()
        if process.returncode == 0:
            temp_file.replace(grib_file)
            return True
        else:
            print(f"[ERROR] grib_set failed to fix time on {grib_file.name}: {stderr}")
            if temp_file.exists(): temp_file.unlink()
            return False
            
    else:
        print("[ERROR] Neither wgrib2 nor grib_set (eccodes) found. Cannot fix corrupted time units from CDO.")
        return False

def main():
    parser = argparse.ArgumentParser(description="ICON to WRF Batch Regridder")
    parser.add_argument("--source-grid", help="Path to custom source grid file")
    parser.add_argument("--target-grid", help="Path to custom target grid file")
    parser.add_argument("--skip-file", help="Filename to skip (e.g. the domain file)")
    args = parser.parse_args()

    config_path = "config/config.toml"
    if not os.path.exists(config_path):
        print(f"[ERROR] Configuration file {config_path} not found.")
        sys.exit(1)
        
    with open(config_path, "rb") as f:
        config = tomllib.load(f)
        
    paths = config.get("paths", {})
    input_dir = Path(paths.get("input_dir", "input"))
    output_dir = Path(paths.get("output_dir", "output"))
    
    source_grid = args.source_grid if args.source_grid else paths.get("source_grid", "source_grid.txt")
    target_grid = args.target_grid if args.target_grid else paths.get("target_grid", "target_grid.txt")
    
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Ensure grid files exist
    if not os.path.exists(source_grid) or not os.path.exists(target_grid):
        print(f"[ERROR] Grid definitions not found: {source_grid} or {target_grid}")
        sys.exit(1)
        
    input_files = [
        f for f in input_dir.iterdir() 
        if f.is_file() 
        and not f.name.startswith('.') 
        and not f.name.endswith('.idx') 
        and not f.name.endswith('.nc')
        and not f.name.endswith('.gz')
    ]
    
    if args.skip_file:
        skip_path = Path(args.skip_file).resolve()
        input_files = [f for f in input_files if f.resolve() != skip_path]
    
    if not input_files:
        print(f"No files found in {input_dir}. Please place your raw GRIB files there.")
        return
        
    print(f"Found {len(input_files)} file(s) in {input_dir}")
    
    results = {}
    vtables_found = set()
    
    for input_file in input_files:
        basename = input_file.name
        out_3d = output_dir / f"{basename}_3d.grib2"
        out_sfc = output_dir / f"{basename}_sfc.grib2"
        
        if out_3d.exists() and out_sfc.exists():
            print(f"[{basename}] Skipping: Processed files already exist in output directory.")
            results[basename] = "SKIPPED: already in output"
            continue
            
        print(f"\nProcessing {basename}...")
        
        temp_3d = Path("temp_3d_processing.nc")
        temp_sfc = Path("temp_sfc_processing.nc")
        temp_soil_moist = Path("temp_soil_moist_processing.nc")
        temp_soil_temp = Path("temp_soil_temp_processing.nc")
        
        out_soil_moist = output_dir / f"{basename}_soil_moist.grib2"
        out_soil_temp = output_dir / f"{basename}_soil_temp.grib2"
        
        # 1. Extract NetCDFs using Python (Bypasses AEC compression errors)
        if not extract_3d(str(input_file), str(temp_3d)):
            if basename.endswith("00000000"):
                print(f"[INFO] Skipping {basename}: It is an initialization file (+00h) and lacks 3D fields. Please use the previous run instead.")
                results[basename] = "SKIPPED: +00h Initialization file (use previous run)"
            else:
                results[basename] = "ERROR: 3D Extraction failed"
            continue
        if not extract_surface(str(input_file), str(temp_sfc)):
            results[basename] = "ERROR: Surface Extraction failed"
            continue
            
        # Try extracting soil (it's okay if this fails if the file has no soil data)
        has_soil_moist = extract_soil_moist(str(input_file), str(temp_soil_moist))
        has_soil_temp = extract_soil_temp(str(input_file), str(temp_soil_temp))
            
        soil_files_for_diag = []
        if has_soil_moist: soil_files_for_diag.append(str(temp_soil_moist))
        if has_soil_temp: soil_files_for_diag.append(str(temp_soil_temp))
            
        # 2. Run Diagnostics
        print("\nRunning Diagnostics...")
        diag_ok, vtable_sugg = check_wrf_ready(str(temp_3d), str(temp_sfc), soil_files=soil_files_for_diag)
        if not diag_ok:
            print("[ERROR] Diagnostics failed.")
            results[basename] = "ERROR: Diagnostics failed"
            continue
        if vtable_sugg:
            vtables_found.add(vtable_sugg)
            
        # 3. Regrid 3D fields
        print("\nRegridding 3D fields...")
        if not run_cdo_regrid(temp_3d, out_3d, source_grid, target_grid, invertlev=True):
            results[basename] = "ERROR: 3D Regridding failed"
            continue
        fix_time_metadata(out_3d, basename)
            
        # 4. Regrid Surface fields
        print("\nRegridding Surface fields...")
        if not run_cdo_regrid(temp_sfc, out_sfc, source_grid, target_grid, invertlev=False):
            results[basename] = "ERROR: Surface Regridding failed"
            continue
        fix_time_metadata(out_sfc, basename)
            
        # 5. Regrid Soil fields if present
        if has_soil_moist:
            print("\nRegridding Soil Moisture fields...")
            if not run_cdo_regrid(temp_soil_moist, out_soil_moist, source_grid, target_grid, invertlev=False):
                results[basename] = "ERROR: Soil Moisture Regridding failed"
                continue
            fix_time_metadata(out_soil_moist, basename)
        if has_soil_temp:
            print("\nRegridding Soil Temperature fields...")
            if not run_cdo_regrid(temp_soil_temp, out_soil_temp, source_grid, target_grid, invertlev=False):
                results[basename] = "ERROR: Soil Temperature Regridding failed"
                continue
            fix_time_metadata(out_soil_temp, basename)
            
        # Cleanup
        if temp_3d.exists(): temp_3d.unlink()
        if temp_sfc.exists(): temp_sfc.unlink()
        if temp_soil_moist.exists(): temp_soil_moist.unlink()
        if temp_soil_temp.exists(): temp_soil_temp.unlink()
        
        results[basename] = "SUCCESS"
        print(f"[{basename}] Processing Complete! Outputs saved to {output_dir}")

    print("\n" + "="*50)
    print("--- BATCH PROCESSING SUMMARY ---")
    print("="*50)
    for file_name, status in results.items():
        if status == "SUCCESS":
            print(f"✅ {file_name}: {status}")
        elif status.startswith("SKIPPED"):
            print(f"⏭️  {file_name}: {status}")
        else:
            print(f"❌ {file_name}: {status}")
            
    print("\n--- VTABLE SUGGESTION ---")
    if len(vtables_found) == 1:
        print(f"✅ All processed files use the same vertical grid. Use: {list(vtables_found)[0]}")
    elif len(vtables_found) > 1:
        print(f"⚠️ DISCREPANCY DETECTED: Files have mixed vertical grids! Found: {', '.join(vtables_found)}")
        print("   This will cause WRF metgrid to crash. Please process pressure-level and model-level datasets separately.")
    else:
        print("Unknown (No valid 3D fields processed)")
        
    print("="*50 + "\n")

if __name__ == "__main__":
    main()
