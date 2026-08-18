import argparse
import os
import sys
import subprocess
import shutil
import glob
import copy
import concurrent.futures
import threading
import multiprocessing
from datetime import datetime, timedelta
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

from .surface_extractor import extract_surface, extract_3d, extract_soil_moist, extract_soil_temp, extract_openamundsen_fields
from .diagnostics import check_wrf_ready, check_final_gribs

def process_chunk_wrapper(chunk_start, chunk_end, args, config, input_dir, output_dir, source_grid, target_grid, queue):
    from .amundsen_runner import run_openamundsen_profile
    import copy
    import multiprocessing
    import uuid
    import os
    import shutil
    from pathlib import Path
    
    # Assign a worker ID dynamically from the process name
    worker_id = multiprocessing.current_process().name.split('-')[-1]
    
    chunk_args = copy.copy(args)
    chunk_args.start = chunk_start.strftime("%Y%m%d%H")
    chunk_args.end = chunk_end.strftime("%Y%m%d%H")
    if hasattr(chunk_args, 'output'):
        chunk_args.output = None
        
    use_ramdisk = getattr(args, 'ramdisk', False)
    real_output_dir = output_dir
    ram_dir = None
    
    if use_ramdisk and os.path.exists("/dev/shm"):
        ram_dir = Path("/dev/shm") / f"icon2wrf_ramdisk_{os.environ.get('USER', 'user')}_{worker_id}_{uuid.uuid4().hex[:8]}"
        ram_dir.mkdir(parents=True, exist_ok=True)
        input_dir = ram_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir = ram_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        
    try:
        run_openamundsen_profile(chunk_args, config, input_dir, output_dir, source_grid, target_grid, queue, worker_id)
        
        if use_ramdisk and ram_dir is not None:
            expected_nc = f"openamundsen_forcing_{chunk_args.start}_{chunk_args.end}.nc"
            ram_nc = output_dir / expected_nc
            if ram_nc.exists():
                shutil.copy(str(ram_nc), str(real_output_dir / expected_nc))
                
    finally:
        if use_ramdisk and ram_dir is not None and ram_dir.exists():
            shutil.rmtree(str(ram_dir), ignore_errors=True)

def run_cdo_regrid(input_nc, output_file, source_grid, target_grid, invertlev=False, extra_args=None, as_netcdf=False):
    """Runs CDO to regrid NetCDF, suppressing harmless ECCODES warnings."""
    if as_netcdf:
        cmd = ["cdo", "-f", "nc", "-settunits,hours", "-setmisstonn"]
    else:
        cmd = ["cdo", "-f", "grb2", "-b", "16", "-settunits,hours", "-setmisstonn"]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(f"-remapdis,{target_grid}")
    if invertlev:
        cmd.append("-invertlev")
    cmd.extend([f"-setgrid,{source_grid}", str(input_nc), str(output_file)])
    
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
        try:
            import eccodes
            try:
                with open(grib_file, 'rb') as fin, open(temp_file, 'wb') as fout:
                    while True:
                        gid = eccodes.codes_grib_new_from_file(fin)
                        if gid is None:
                            break
                        eccodes.codes_set(gid, 'indicatorOfUnitOfTimeRange', 1)
                        eccodes.codes_set(gid, 'forecastTime', 0)
                        eccodes.codes_set(gid, 'dataDate', int(f"{yyyy}{mm}{dd}"))
                        eccodes.codes_set(gid, 'dataTime', int(f"{hh}00"))
                        eccodes.codes_write(gid, fout)
                        eccodes.codes_release(gid)
                temp_file.replace(grib_file)
                return True
            except Exception as e:
                print(f"[ERROR] Python eccodes failed to fix time on {grib_file.name}: {e}")
                if temp_file.exists(): temp_file.unlink()
                return False
        except ImportError:
            print("[ERROR] Neither wgrib2, grib_set, nor Python eccodes module found. Cannot fix corrupted time units from CDO.")
            return False

def fix_soil_levels(grib_file, is_temp=True):
    """Forces the Level Type to 106 and injects exact physical depths in meters."""
    temp_file = grib_file.with_name(f"temp_soil_fix_{grib_file.name}")
    depths_temp = [0.005, 0.02, 0.06, 0.18, 0.54, 1.62, 4.86, 14.58]
    depths_moist_top = [0.0, 0.01, 0.03, 0.09, 0.27, 0.81, 2.43, 7.29]
    depths_moist_bot = [0.01, 0.03, 0.09, 0.27, 0.81, 2.43, 7.29, 21.87]
    
    try:
        import eccodes
        with open(grib_file, 'rb') as fin, open(temp_file, 'wb') as fout:
            idx = 0
            while True:
                gid = eccodes.codes_grib_new_from_file(fin)
                if gid is None:
                    break
                    
                # Force the surface type to 106 (Depth Below Land)
                # Setting both the string and the integer ensures eccodes doesn't revert it
                try:
                    eccodes.codes_set(gid, 'typeOfLevel', 'depthBelowLand')
                except Exception:
                    pass
                eccodes.codes_set(gid, 'typeOfFirstFixedSurface', 106)
                
                if is_temp:
                    if idx < len(depths_temp):
                        eccodes.codes_set_double(gid, 'level', depths_temp[idx])
                else:
                    if idx < len(depths_moist_top):
                        # For moisture, Vtable expects two levels (Layer)
                        eccodes.codes_set(gid, 'typeOfSecondFixedSurface', 106)
                        
                        # In ICON moisture depths, the values in Vtable are in CM (0-1, 1-3, etc)
                        # We set the top and bottom of the layer in meters.
                        if idx == 0:
                            eccodes.codes_set_double(gid, 'topLevel', 0.0)
                            eccodes.codes_set_double(gid, 'bottomLevel', 0.01)
                        elif idx == 1:
                            eccodes.codes_set_double(gid, 'topLevel', 0.01)
                            eccodes.codes_set_double(gid, 'bottomLevel', 0.03)
                        elif idx == 2:
                            eccodes.codes_set_double(gid, 'topLevel', 0.03)
                            eccodes.codes_set_double(gid, 'bottomLevel', 0.09)
                        elif idx == 3:
                            eccodes.codes_set_double(gid, 'topLevel', 0.09)
                            eccodes.codes_set_double(gid, 'bottomLevel', 0.27)
                        elif idx == 4:
                            eccodes.codes_set_double(gid, 'topLevel', 0.27)
                            eccodes.codes_set_double(gid, 'bottomLevel', 0.81)
                        elif idx == 5:
                            eccodes.codes_set_double(gid, 'topLevel', 0.81)
                            eccodes.codes_set_double(gid, 'bottomLevel', 2.43)
                        elif idx == 6:
                            eccodes.codes_set_double(gid, 'topLevel', 2.43)
                            eccodes.codes_set_double(gid, 'bottomLevel', 7.29)
                        elif idx == 7:
                            eccodes.codes_set_double(gid, 'topLevel', 7.29)
                            eccodes.codes_set_double(gid, 'bottomLevel', 21.87)
                        else:
                            eccodes.codes_set_double(gid, 'level', depths_moist_top[idx])
                        try:
                            eccodes.codes_set(gid, 'typeOfSecondFixedSurface', 106)
                        except:
                            pass

                        # ICON's W_SO is a layer-integrated mass (kg m-2), but
                        # WRF's SMOIS is a volumetric fraction (m3 m-3).
                        # Nothing downstream (metgrid, real.exe) converts this
                        # generically -- WRF only has a hardcoded conversion
                        # for UM-sourced soil moisture (flag_um_soil), which
                        # ICON data does not set. Convert here, at the one
                        # point where the exact layer thickness is known.
                        thickness_m = depths_moist_bot[idx] - depths_moist_top[idx]
                        vals = eccodes.codes_get_values(gid)
                        vals = vals / (thickness_m * 1000.0)
                        eccodes.codes_set_values(gid, vals)

                eccodes.codes_write(gid, fout)
                eccodes.codes_release(gid)
                idx += 1
                
        temp_file.replace(grib_file)
        return True
    except Exception as e:
        print(f"[ERROR] Failed to fix soil levels on {grib_file.name}: {e}")
        if temp_file.exists(): temp_file.unlink()
        return False

def main():
    parser = argparse.ArgumentParser(description="ICON to WRF Batch Regridder")
    parser.add_argument("--source-grid", help="Path to custom source grid file")
    parser.add_argument("--target-grid", help="Path to custom target grid file")
    parser.add_argument("--skip-file", help="Filename to skip (e.g. the domain file)")
    parser.add_argument("--netcdf", action="store_true", help="Save output as NetCDF instead of GRIB2")
    parser.add_argument("--profile", choices=["wrf", "openamundsen"], default="wrf", help="Which processing profile to run")
    parser.add_argument("--output", help="Output filename for openamundsen profile")
    parser.add_argument("--start", help="Start date/time (YYYYMMDDHH)")
    parser.add_argument("--end", help="End date/time (YYYYMMDDHH)")
    parser.add_argument("--run-strategy", choices=["freshest", "longest"], default="freshest", help="Forecast stitching strategy")
    parser.add_argument("--spinup", type=int, default=9, help="Minimum lead time (hours) to use, avoiding forecast spin-up shock (default: 9)")
    parser.add_argument("--jobs", type=int, default=8, help="Number of concurrent workers for parallel chunking in interactive sessions (default: 8)")
    parser.add_argument("--ramdisk", action="store_true", help="Use /dev/shm to aggressively isolate chunking in RAM (streaming mode only)")
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
    
    # Clean up old idx files to improve cfgrib run time
    for d in [input_dir, output_dir]:
        for idx_file in d.glob("*.idx"):
            try:
                idx_file.unlink()
            except Exception:
                pass
    
    # Ensure grid files exist
    if not os.path.exists(source_grid) or not os.path.exists(target_grid):
        print(f"[ERROR] Grid definitions not found: {source_grid} or {target_grid}")
        sys.exit(1)
        
    if args.profile == "openamundsen":
        from .amundsen_runner import run_openamundsen_profile, ensure_oetztal_grid
        from .download_ftp import load_credentials
        
        # Sequentially ensure the target grid exists so 8 workers don't collide trying to create it
        ensure_oetztal_grid()
        
        # Sequentially prompt for FTP password if needed, preventing 8 workers from blocking on stdin
        if args.start and args.end:
            creds = load_credentials()
            url = creds.get("url")
            username = creds.get("username")
            if url and username and not os.environ.get("FTP_PASSWORD"):
                import getpass
                pwd = getpass.getpass(f"Password for {username}@{url}: ")
                os.environ["FTP_PASSWORD"] = pwd
                
            print("\nPerforming pre-flight FTP validation and extracting missing targets...")
            import ftplib
            from .amundsen_runner import build_download_queue
            missing_targets = set()
            try:
                with ftplib.FTP(url) as ftp:
                    ftp.login(username, os.environ.get("FTP_PASSWORD"))
                    master_queue = build_download_queue(ftp, datetime.strptime(args.start, "%Y%m%d%H"), datetime.strptime(args.end, "%Y%m%d%H"), args.run_strategy, spinup_hours=args.spinup, log=lambda x: None)
                    for dir_name, items in master_queue.items():
                        if dir_name.startswith("COPY_LAST_DIR"):
                            for target, _, _ in items:
                                missing_targets.add(target)
            except ValueError as e:
                print(f"\n[FATAL ERROR] {e}")
                sys.exit(1)
            except Exception as e:
                print(f"\n[FATAL ERROR] FTP Connection Failed: {e}")
                sys.exit(1)
                
        if args.start and args.end:
            start_dt = datetime.strptime(args.start, "%Y%m%d%H")
            end_dt = datetime.strptime(args.end, "%Y%m%d%H")
            
            all_targets = []
            curr = start_dt
            while curr <= end_dt:
                all_targets.append(curr)
                curr += timedelta(hours=1)
                
            chunk_size_ideal = len(all_targets) / args.jobs
            chunks = []
            current_idx = 0
            
            for i in range(args.jobs):
                if current_idx >= len(all_targets): break
                
                # If last job, take the remainder
                if i == args.jobs - 1:
                    chunks.append((all_targets[current_idx], all_targets[-1]))
                    break
                    
                end_idx = int((i + 1) * chunk_size_ideal) - 1
                if end_idx >= len(all_targets):
                    end_idx = len(all_targets) - 1
                    
                # Ensure chunk doesn't end on a missing target (pushes the boundary to the next valid file for interpolation)
                while end_idx < len(all_targets) - 1 and all_targets[end_idx] in missing_targets:
                    end_idx += 1
                    
                chunks.append((all_targets[current_idx], all_targets[end_idx]))
                current_idx = end_idx + 1
                
            print(f"\n==========================================================")
            print(f"Parallelizing openAMUNDSEN season into {len(chunks)} chunk(s)")
            print(f"Dispatching to {args.jobs} concurrent workers...")
            print(f"==========================================================\n")
            
            from .progress import ProgressTracker
            m = multiprocessing.Manager()
            q = m.Queue()
            tracker = ProgressTracker(q)
            ui_thread = threading.Thread(target=tracker.monitor, daemon=True)
            ui_thread.start()
            
            with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as executor:
                futures = [executor.submit(process_chunk_wrapper, c[0], c[1], args, config, input_dir, output_dir, source_grid, target_grid, q) for c in chunks]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        q.put({"type": "LOG", "worker_id": "?", "msg": f"Chunk failed: {e}"})
                        
            q.put({"type": "STOP"})
            ui_thread.join(timeout=2.0)
                        
            # Final mergetime stitch
            print(f"\nStitching chunks together...")
            output_name = args.output if args.output else f"openamundsen_forcing_{args.start}_{args.end}.nc"
            final_nc = output_dir / output_name
            
            chunk_files = []
            for c in chunks:
                start_str = c[0].strftime("%Y%m%d%H")
                end_str = c[1].strftime("%Y%m%d%H")
                expected_nc = str(output_dir / f"openamundsen_forcing_{start_str}_{end_str}.nc")
                if os.path.exists(expected_nc):
                    chunk_files.append(expected_nc)
            
            if len(chunk_files) > 0:
                if len(chunk_files) == 1 and chunk_files[0] == str(final_nc):
                    print(f"\nSingle chunk generated. No merge needed: {final_nc}")
                else:
                    try:
                        subprocess.run(["cdo", "-s", "-O", "mergetime"] + chunk_files + [str(final_nc)], check=True, capture_output=True, text=True)
                        print(f"\nSuccessfully created final season file: {final_nc}")
                    except subprocess.CalledProcessError as e:
                        print(f"\n[CDO MERGETIME ERROR]")
                        print(f"STDERR:\n{e.stderr}")
                        raise
                        
                    # Clean up chunk files
                    for f in chunk_files:
                        if f != str(final_nc):
                            try:
                                os.remove(f)
                            except:
                                pass
            else:
                print("[FATAL ERROR] No output chunks found to merge. All worker chunks failed.")
                sys.exit(1)

        else:
            run_openamundsen_profile(args, config, input_dir, output_dir, source_grid, target_grid)
        return

    # Gather all unique timestamps/basenames
    input_files = []
    for f in input_dir.iterdir():
        if f.is_file() and not f.name.endswith(".idx"):
            # The base name is everything before '_3d', '_sfc', etc.
            # If it's a raw file, it won't have those suffixes.
            base = f.name.split('_3d')[0].split('_sfc')[0].split('_soil')[0]
            if base not in [x.name for x in input_files]:
                # We append a Path object that represents the base prefix
                input_files.append(input_dir / base)
                
    input_files.sort()
                
    print(f"\nFound {len(input_files)} file(s) in {input_dir.name}")
    
    results = {}
    vtables_found = set()
    
    for input_file in input_files:
        basename = input_file.name
        ext = ".nc" if args.netcdf else ".grib2"
        out_3d = output_dir / f"{basename}_3d{ext}"
        out_sfc = output_dir / f"{basename}_sfc{ext}"
        
        out_soil_moist = output_dir / f"{basename}_soil_moist{ext}"
        out_soil_temp = output_dir / f"{basename}_soil_temp{ext}"
        
        # Determine what's already processed to avoid skipping if only some files were deleted
        if out_3d.exists() and out_sfc.exists() and out_soil_moist.exists() and out_soil_temp.exists():
            print(f"[{basename}] Skipping: All processed files already exist in output directory.")
            results[basename] = "SKIPPED: already in output"
            continue
            
        print(f"\nProcessing {basename}...")
        
        temp_3d = Path("temp_3d_processing.nc")
        temp_sfc = Path("temp_sfc_processing.nc")
        temp_soil_moist = Path("temp_soil_moist_processing.nc")
        temp_soil_temp = Path("temp_soil_temp_processing.nc")
        
        # These are now defined above, so just keep the temp files here
        
        # 1. Extract NetCDFs using Python (Bypasses AEC compression errors)
        has_3d = extract_3d(str(input_file), str(temp_3d))
        if not has_3d:
            if basename.endswith("00000000"):
                print(f"[INFO] Initial file {basename} lacks 3D fields. Proceeding with surface data only.")
            else:
                print(f"[WARNING] 3D Extraction failed for {basename}. Proceeding with surface data only if available.")
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
        if has_3d:
            diag_ok, vtable_sugg = check_wrf_ready(str(temp_3d), str(temp_sfc), soil_files=soil_files_for_diag)
            if not diag_ok:
                print("[ERROR] Diagnostics failed.")
                results[basename] = "ERROR: Diagnostics failed"
                continue
            if vtable_sugg:
                vtables_found.add(vtable_sugg)
        else:
            print("[INFO] Skipping 3D diagnostics because no 3D fields are present.")
            
        # Track files for final validation
        final_3d = None
        final_sfc = None
        final_sm = None
        final_st = None
            
        # 3. Regrid 3D fields
        if has_3d:
            print("\nRegridding 3D fields...")
            if not run_cdo_regrid(temp_3d, out_3d, source_grid, target_grid, invertlev=True, as_netcdf=args.netcdf):
                results[basename] = "ERROR: 3D Regridding failed"
                continue
            if not args.netcdf:
                fix_time_metadata(out_3d, basename)
            final_3d = str(out_3d)
            
        # 4. Regrid Surface fields
        print("\nRegridding Surface fields...")
        if not run_cdo_regrid(temp_sfc, out_sfc, source_grid, target_grid, invertlev=False, as_netcdf=args.netcdf):
            results[basename] = "ERROR: Surface Regridding failed"
            continue
        if not args.netcdf:
            fix_time_metadata(out_sfc, basename)
        final_sfc = str(out_sfc)
            
        # 5. Regrid Soil fields if present
        if has_soil_moist:
            print("\nRegridding Soil Moisture fields...")
            if not run_cdo_regrid(temp_soil_moist, out_soil_moist, source_grid, target_grid, invertlev=False, as_netcdf=args.netcdf):
                results[basename] = "ERROR: Soil Moisture Regridding failed"
                continue
            if not args.netcdf:
                fix_time_metadata(out_soil_moist, basename)
                fix_soil_levels(out_soil_moist, is_temp=False)
            final_sm = str(out_soil_moist)
        if has_soil_temp:
            print("\nRegridding Soil Temperature fields...")
            if not run_cdo_regrid(temp_soil_temp, out_soil_temp, source_grid, target_grid, invertlev=False, as_netcdf=args.netcdf):
                results[basename] = "ERROR: Soil Temperature Regridding failed"
                continue
            if not args.netcdf:
                fix_time_metadata(out_soil_temp, basename)
                fix_soil_levels(out_soil_temp, is_temp=True)
            final_st = str(out_soil_temp)

        # 7. Final Validation on Generated GRIB2 Files
        if input_file == input_files[0] and not args.netcdf:
            check_final_gribs(
                grib_3d=final_3d, 
                grib_sfc=final_sfc, 
                grib_soil_moist=final_sm, 
                grib_soil_temp=final_st
            )
            
        # Cleanup
        if has_3d and temp_3d.exists(): temp_3d.unlink()
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
