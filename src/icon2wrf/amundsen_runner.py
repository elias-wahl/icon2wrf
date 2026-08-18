import os
import sys
import xarray as xr
import numpy as np
from pathlib import Path
import subprocess
import ftplib
from datetime import datetime, timedelta

from .surface_extractor import extract_openamundsen_fields
from .download_ftp import load_credentials, get_filename_for_offset, extract_gz

os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"
os.environ["HDF5_DISABLE_VERSION_CHECK"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"  # 16 Python workers * 1 CDO thread = 16 Cores saturated (matches --jobs 16 / --cpus-per-task=16)

def run_cdo(cmd, logger=print):
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        logger(f"[CDO ERROR] {' '.join(cmd)}")
        logger(f"STDERR:\n{e.stderr}")
        raise

def build_download_queue(ftp, start_dt, end_dt, strategy, spinup_hours=9, interval_hours=1, log=print):
    items = []
    ftp.dir(items.append)
    
    available_runs = []
    for item in items:
        parts = item.split()
        if len(parts) > 0 and parts[0].startswith('d'):
            dir_name = parts[-1]
            try:
                run_dt = datetime.strptime(dir_name, "%Y%m%d_%H")
                available_runs.append((run_dt, dir_name))
            except ValueError:
                pass
                
    available_runs.sort()
    
    target_times = []
    curr = start_dt
    while curr <= end_dt:
        target_times.append(curr)
        curr += timedelta(hours=interval_hours)
        
    download_queue = {}
    
    dir_cache = {}
    def check_file_exists(d_name, f_name):
        if d_name not in dir_cache:
            try:
                dir_cache[d_name] = ftp.nlst(d_name)
            except Exception:
                dir_cache[d_name] = []
        return any(f_name in f or f.endswith("/" + f_name) for f in dir_cache[d_name])

    first_run_chosen = False
    
    for target in target_times:
        valid_runs = [r for r in available_runs if r[0] <= target]
        chosen_run_dt = None
        dir_name = None
        gz_file = None
        is_copy_last = False
        
        def find_best(runs_subset):
            if not runs_subset: return None, None, None
            for r_dt, d_name in reversed(runs_subset):
                offset = int((target - r_dt).total_seconds() // 3600)
                gf = get_filename_for_offset(offset)
                if check_file_exists(d_name, gf):
                    return r_dt, d_name, gf
            return None, None, None
            
        # Normal: 9h to 48h spinup
        normal_subset = [r for r in valid_runs if 9 <= (target - r[0]).total_seconds() // 3600 <= 48]
        chosen_run_dt, dir_name, gz_file = find_best(normal_subset)
        
        if not chosen_run_dt:
            # Measure 2: Next run at +3h to +8h spinup
            m2_subset = [r for r in valid_runs if 3 <= (target - r[0]).total_seconds() // 3600 < 9]
            chosen_run_dt, dir_name, gz_file = find_best(m2_subset)
            if chosen_run_dt:
                log(f"[WARNING] Normal coverage missing for {target}. Escalating to Measure 2: Using short spinup (+3h).")
            else:
                # Measure 3: Copy the last time step
                log(f"[WARNING] Normal coverage and Measure 2 failed for {target}. Escalating to Measure 3: COPY_LAST.")
                is_copy_last = True
                
        if is_copy_last:
            gap_key = f"COPY_LAST_DIR_{target.strftime('%Y%m%d%H')}"
            download_queue[gap_key] = [(target, "COPY_LAST", False)]
            continue
            
        offset_hours = int((target - chosen_run_dt).total_seconds() // 3600)
        
        if dir_name not in download_queue: 
            download_queue[dir_name] = []
            if not first_run_chosen:
                init_gz = get_filename_for_offset(0)
                if check_file_exists(dir_name, init_gz):
                    download_queue[dir_name].append((chosen_run_dt, init_gz, True))
                first_run_chosen = True
                
            if offset_hours > 0:
                prefetch_offset = offset_hours - 1
                prefetch_gz = get_filename_for_offset(prefetch_offset)
                prefetch_target = target - timedelta(hours=1)
                if check_file_exists(dir_name, prefetch_gz):
                    download_queue[dir_name].append((prefetch_target, prefetch_gz, True))
                
        if not (offset_hours == 0 and any(f[1] == gz_file for f in download_queue[dir_name])):
            download_queue[dir_name].append((target, gz_file, False))
            
    return download_queue

def ensure_oetztal_grid():
    grid_path = "config/oetztal_grid.nc"
    if os.path.exists(grid_path):
        return grid_path
        
    import pyproj
    x = np.linspace(620250, 669750, 100)
    y = np.linspace(5170250, 5244750, 150)
    X, Y = np.meshgrid(x, y)
    
    proj = pyproj.Proj("EPSG:32632")
    lon, lat = proj(X, Y, inverse=True)
    
    ds = xr.Dataset(
        data_vars={'dummy': (['y', 'x'], np.zeros((150, 100)))},
        coords={
            'y': (['y'], y),
            'x': (['x'], x),
            'lon': (['y', 'x'], lon),
            'lat': (['y', 'x'], lat),
        }
    )
    
    ds['dummy'].attrs['coordinates'] = 'lon lat'
    ds['lon'].attrs = {'standard_name': 'longitude', 'units': 'degrees_east'}
    ds['lat'].attrs = {'standard_name': 'latitude', 'units': 'degrees_north'}
    ds['x'].attrs = {'standard_name': 'projection_x_coordinate', 'units': 'm'}
    ds['y'].attrs = {'standard_name': 'projection_y_coordinate', 'units': 'm'}
    
    os.makedirs('config', exist_ok=True)
    ds.to_netcdf(grid_path)
    return grid_path

def run_openamundsen_profile(args, config, input_dir, output_dir, source_grid, target_grid, queue=None, worker_id="?"):
    import time
    import uuid
    
    def log(msg):
        if queue:
            queue.put({"type": "LOG", "worker_id": worker_id, "msg": msg})
        else:
            print(msg)

    log("Running openAMUNDSEN profile...")
    # Generate and use the Oetztal UTM grid instead of the WRF default
    oetztal_grid = ensure_oetztal_grid()
    if args.output:
        output_nc = Path(args.output)
    else:
        start_dt = datetime.strptime(args.start, "%Y%m%d%H")
        end_dt = datetime.strptime(args.end, "%Y%m%d%H")
        start_str = start_dt.strftime("%Y%m%d%H")
        end_str = end_dt.strftime("%Y%m%d%H")
        output_nc = output_dir / f"openamundsen_forcing_{start_str}_{end_str}.nc"
        
    partial_nc = output_nc.with_suffix(".partial.nc")
    
    # Create an isolated sandbox to prevent collision between parallel SLURM jobs
    sandbox_dir = input_dir / f"sandbox_{datetime.now().strftime('%Y%m%d%H%M%S')}_{worker_id}_{uuid.uuid4().hex[:8]}"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    
    invariant_raw_nc = sandbox_dir / "invariant_raw.nc"
    invariant_remapped_nc = sandbox_dir / "invariant_remapped.nc"
    
    if partial_nc.exists():
        partial_nc.unlink()

    # Determine processing mode (Streaming vs Local)
    files_to_process = [] # list of (file_path, is_temp)
    ftp = None
    download_queue = {}
    
    if args.start and args.end:
        log(f"Streaming mode enabled: downloading from FTP ({args.start} to {args.end})")
        start_dt = datetime.strptime(args.start, "%Y%m%d%H")
        end_dt = datetime.strptime(args.end, "%Y%m%d%H")
        
        creds = load_credentials()
        url = creds.get("url")
        username = creds.get("username")
        
        if not url or not username:
            log("[ERROR] FTP credentials not found. Run python -m src.icon2wrf.download_ftp first to save them.")
            return
            
        log(f"Connecting to FTP {url}...")
        import getpass
        password = os.environ.get("FTP_PASSWORD")
        if not password:
            password = getpass.getpass(f"Password for {username}@{url}: ")
            
        import random
        import time
        # Stagger initial connections to prevent all cores hitting the FTP server simultaneously
        time.sleep(random.uniform(0.1, 15.0))
        
        attempt = 0
        ftp = None
        while True:
            try:
                ftp = ftplib.FTP(url)
                ftp.login(username, password)
                break
            except Exception as e:
                attempt += 1
                log(f"[WARNING] Initial FTP connection failed (attempt {attempt}): {e}")
                time.sleep(min(60, 2 ** attempt + random.uniform(0, 5)))
        
        download_queue = build_download_queue(ftp, start_dt, end_dt, args.run_strategy, spinup_hours=args.spinup, log=log)
        if not download_queue:
            log("No valid files found on FTP for this time range.")
            return
            
        total_files = sum(len(items) for items in download_queue.values())
        if queue:
            queue.put({"type": "INIT_TOTAL", "total": total_files})
            
        # Download and prepare invariant topography
        # We must skip over any COPY_LAST_DIR keys when looking for the first valid run directory
        first_dir = next((k for k in download_queue.keys() if not k.startswith("COPY_LAST_DIR")), list(download_queue.keys())[0])
        invariant_gz = "ilf3f00000000.gz"
        invariant_raw = sandbox_dir / "invariant_000"
        log("FETCHING INVARIANT TOPOGRAPHY")
        attempt = 0
        while True:
            try:
                try:
                    ftp.cwd("/" + first_dir)
                except Exception:
                    ftp.cwd(first_dir)
                    
                log(f"Downloading {invariant_gz}...")
                with open(sandbox_dir / invariant_gz, "wb") as f:
                    ftp.retrbinary(f"RETR {invariant_gz}", f.write)
                break
            except Exception as e:
                attempt += 1
                log(f"[WARNING] Invariant download failed (attempt {attempt}): {e}")
                time.sleep(min(60, 2 ** attempt + random.uniform(0, 5)))
                try:
                    ftp = ftplib.FTP(url)
                    ftp.login(username, password)
                except:
                    pass
        extract_gz(str(sandbox_dir / invariant_gz), str(invariant_raw))
        (sandbox_dir / invariant_gz).unlink(missing_ok=True)
        
        extract_openamundsen_fields(str(invariant_raw), str(invariant_raw_nc))
        run_cdo(["cdo", "-s", "-O", "-f", "nc", f"-remapdis,{oetztal_grid}", f"-setgrid,{source_grid}", str(invariant_raw_nc), str(invariant_remapped_nc)], log)
        invariant_raw.unlink(missing_ok=True)
        
    else:
        log("Local mode enabled: processing existing files in input directory.")
        for f in input_dir.iterdir():
            if f.is_file() and not f.name.endswith(".idx") and not f.name.endswith(".nc"):
                files_to_process.append((f, False, False))
        files_to_process.sort(key=lambda x: x[0].name)
        if not files_to_process:
            log("No files to process.")
            return
            
        # Find local +000 file for invariant topography
        invariant_raw = next((f[0] for f in files_to_process if f[0].name.endswith("00000000")), None)
        if invariant_raw and not invariant_remapped_nc.exists():
            log(f"\n--- PREPARING LOCAL INVARIANT TOPOGRAPHY ---")
            extract_openamundsen_fields(str(invariant_raw), str(invariant_raw_nc))
            run_cdo(["cdo", "-s", "-O", "-f", "nc", f"-remapdis,{oetztal_grid}", f"-setgrid,{source_grid}", str(invariant_raw_nc), str(invariant_remapped_nc)], log)

    previous_state = {}
    
    # Load invariant topography into memory
    ds_invariant = None
    if invariant_remapped_nc.exists():
        ds_invariant = xr.open_dataset(str(invariant_remapped_nc))
        
    # We will use a generator to yield files one by one, either downloading them or yielding local ones.
    def file_generator():
        nonlocal ftp
        if ftp:
            for dir_name, items in download_queue.items():
                if dir_name.startswith("COPY_LAST_DIR"):
                    for target, gz_file, is_prefetch in items:
                        yield ("COPY_LAST", False, False, 0, 0, target)
                    continue
                    
                log(f"Entering FTP directory {dir_name}...")
                try:
                    ftp.cwd("/" + dir_name)
                except Exception:
                    ftp.cwd(dir_name)
                    
                for target, gz_file, is_prefetch in items:
                    target_str = target.strftime("%Y%m%d%H")
                    local_gz_path = sandbox_dir / f"{target_str}_{gz_file}"
                    local_nc_path = sandbox_dir / f"{target_str}_{gz_file.replace('.gz', '')}"
                    
                    if local_nc_path.exists():
                        yield (local_nc_path, False, is_prefetch, 0, local_nc_path.stat().st_size, target)
                        continue
                    
                    if queue: queue.put({"type": "DOWNLOAD_START", "worker_id": worker_id})
                    dl_start = time.time()

                    tmp_gz_path = local_gz_path.with_suffix(".gz.tmp")
                    extraction_ok = False
                    dl_duration = 0
                    file_size_bytes = 0
                    max_extract_attempts = 3

                    for extract_attempt in range(1, max_extract_attempts + 1):
                        attempt = 0
                        while True:
                            try:
                                # Re-establish connection if it dropped
                                try:
                                    ftp.voidcmd("NOOP")
                                except:
                                    ftp = ftplib.FTP(url)
                                    ftp.login(username, password)
                                    try:
                                        ftp.cwd("/" + dir_name)
                                    except Exception:
                                        ftp.cwd(dir_name)

                                with open(tmp_gz_path, 'wb') as f:
                                    ftp.retrbinary(f"RETR {gz_file}", f.write)
                                tmp_gz_path.rename(local_gz_path)
                                break
                            except Exception as e:
                                attempt += 1
                                log(f"[WARNING] FTP download failed for {gz_file} (attempt {attempt}): {e}")
                                time.sleep(min(60, 2 ** attempt + random.uniform(0, 5)))

                        try:
                            dl_duration = time.time() - dl_start
                            file_size_bytes = local_gz_path.stat().st_size

                            extract_gz(local_gz_path, local_nc_path)
                            extraction_ok = True
                            break
                        except Exception as e:
                            log(f"[WARNING] Failed extracting {gz_file} (extract attempt {extract_attempt}/{max_extract_attempts}): {e}")
                            if tmp_gz_path.exists(): tmp_gz_path.unlink()
                            if local_gz_path.exists(): local_gz_path.unlink()
                            if local_nc_path.exists(): local_nc_path.unlink()
                            if extract_attempt < max_extract_attempts:
                                time.sleep(min(30, 2 ** extract_attempt))

                    if extraction_ok:
                        yield (local_nc_path, True, is_prefetch, dl_duration, file_size_bytes, target)
                        if local_gz_path.exists(): local_gz_path.unlink()
                    else:
                        log(f"[ERROR] Giving up on {gz_file} for {target_str} after {max_extract_attempts} download+extract attempts. Treating as missing (will interpolate/copy-last instead of silently dropping).")
                        yield ("COPY_LAST", False, False, 0, 0, target)
            ftp.quit()
        else:
            for item in files_to_process:
                try:
                    target_dt = datetime.strptime(item[0].name.split('_')[0], "%Y%m%d%H")
                except:
                    target_dt = None
                yield (item[0], item[1], item[2], 0, item[0].stat().st_size, target_dt)

    missing_buffer = []

    # Process files iteratively
    for f, is_temp, is_prefetch, dl_duration, file_size_bytes, target in file_generator():
        prefix = "[PRE-FETCH] " if is_prefetch else ""
        log(f"{prefix}Processing {f if f == 'COPY_LAST' else f.name}...")
        
        if f == "COPY_LAST":
            missing_buffer.append(target)
            continue

        pr_start = time.time()
        if queue and dl_duration is not None:
            queue.put({
                "type": "PROCESS_START", 
                "worker_id": worker_id, 
                "duration": dl_duration,
                "file_size": file_size_bytes
            })
            
        temp_raw = sandbox_dir / "temp_am_raw.nc"
        temp_remapped = sandbox_dir / "temp_am_remapped.nc"
        temp_final = sandbox_dir / "temp_am_final.nc"
        temp_derived = sandbox_dir / "temp_am_derived.nc"
        
        if not extract_openamundsen_fields(str(f), str(temp_raw)):
            log(f"[WARNING] Field extraction failed for {f.name}. Treating {target} as missing (will interpolate/copy-last instead of silently dropping).")
            if is_temp and f.exists(): f.unlink()
            if target is not None:
                missing_buffer.append(target)
            continue
            
        ds = xr.open_dataset(temp_raw)
        
        # De-accumulation logic for tp and sw_in
        cycle = ds.time.values
        step = ds.step.values
        valid_time = ds.valid_time.values
        
        step_hours = float(step) / 3.6e12 # nanoseconds to hours
        
        ds_derived = ds.copy()
        
        # Precip
        if 'tp' in ds_derived:
            if cycle in previous_state and 'tp' in previous_state[cycle]:
                prev_tp = previous_state[cycle]['tp']
                diff = ds['tp'] - prev_tp
                ds_derived['tp'] = xr.where(diff < 0, 0, diff)
            else:
                ds_derived['tp'] = ds['tp']
        
        # SW In
        if 'ASWDIR_S' in ds and 'ASWDIFD_S' in ds:
            total_avg = ds['ASWDIR_S'] + ds['ASWDIFD_S']
            total_energy = total_avg * (step_hours * 3600)
            
            if cycle in previous_state and 'sw_energy' in previous_state[cycle]:
                prev_energy = previous_state[cycle]['sw_energy']
                prev_step = previous_state[cycle]['step']
                interval_seconds = (step_hours - prev_step) * 3600
                diff = (total_energy - prev_energy) / interval_seconds if interval_seconds > 0 else total_avg
                ds_derived['sw_in'] = xr.where(diff < 0, 0, diff)
            else:
                ds_derived['sw_in'] = total_avg
                
        ds_derived = ds_derived.drop_vars(['ASWDIR_S', 'ASWDIFD_S'], errors='ignore')
        
        # Expand time dimension to make it a time series for CDO mergetime
        import pandas as pd
        if target is not None:
            # FORCE the exact time from the orchestrator, ignoring corrupt GRIB metadata
            if 'valid_time' in ds_derived.coords:
                ds_derived = ds_derived.drop_vars('valid_time')
            if 'time' in ds_derived.coords:
                ds_derived = ds_derived.rename({'time': 'forecast_reference_time'})
            ds_derived = ds_derived.expand_dims(time=[pd.Timestamp(target)])
        else:
            if 'valid_time' in ds_derived.coords:
                ds_derived = ds_derived.expand_dims('valid_time')
                if 'time' in ds_derived.coords:
                    ds_derived = ds_derived.rename({'valid_time': 'time', 'time': 'forecast_reference_time'})
                else:
                    ds_derived = ds_derived.rename({'valid_time': 'time'})
                
        ds_derived.to_netcdf(str(temp_derived))
        ds.close()
        ds_derived.close()
        
        # Remap using CDO (using the precise Oetztal Grid!)
        cmd = ["cdo", "-s", "-O", "-f", "nc", f"-remapdis,{oetztal_grid}", f"-setgrid,{source_grid}", str(temp_derived), str(temp_remapped)]
        log(f"  -> Running CDO: {' '.join(cmd)}")
        run_cdo(cmd, log)
        
        # Post-remapping derivations
        ds_remap = xr.open_dataset(str(temp_remapped))
        
        if '10u' in ds_remap and '10v' in ds_remap:
            ds_remap['wind_speed'] = np.sqrt(ds_remap['10u']**2 + ds_remap['10v']**2)
            ds_remap = ds_remap.drop_vars(['10u', '10v'])
            
        rename_dict = {}
        if '2t' in ds_remap: rename_dict['2t'] = 'temp'
        if '2r' in ds_remap: rename_dict['2r'] = 'rel_hum'
        if 'tp' in ds_remap: rename_dict['tp'] = 'precip'
        if 'sde' in ds_remap: rename_dict['sde'] = 'snow_depth'
        if 'sd' in ds_remap: rename_dict['sd'] = 'swe'
        
        ds_remap = ds_remap.rename(rename_dict)
        
        # Cap relative humidity at 100%
        if 'rel_hum' in ds_remap:
            ds_remap['rel_hum'] = xr.where(ds_remap['rel_hum'] > 100.0, 100.0, ds_remap['rel_hum'])
        
        # Remove coordinates attribute to prevent CDO cdf_read_xcoord warnings
        for var in ds_remap.data_vars:
            if 'coordinates' in ds_remap[var].attrs:
                del ds_remap[var].attrs['coordinates']
                
        ds_remap.to_netcdf(str(temp_final))
        ds_remap.close()
        
        # If there are buffered missing targets, interpolate them before adding this temp_final
        if missing_buffer and not is_prefetch:
            last_good = sandbox_dir / "last_good_step.nc"
            if last_good.exists():
                log(f"  -> Interpolating {len(missing_buffer)} missing steps between last good and current step...")
                try:
                    import pandas as pd
                    ds_last = xr.open_dataset(last_good)
                    ds_next = xr.open_dataset(temp_final)
                    
                    t_name = 'time' if 'time' in ds_last.coords else 'forecast_reference_time'
                    
                    t_last = pd.Timestamp(missing_buffer[0] - timedelta(hours=1))
                    t_next = pd.Timestamp(target)
                    
                    ds_last = ds_last.assign_coords({t_name: [t_last]})
                    ds_next = ds_next.assign_coords({t_name: [t_next]})
                    
                    ds_concat = xr.concat([ds_last, ds_next], dim=t_name)
                    
                    interp_times = [pd.Timestamp(t) for t in missing_buffer]
                    ds_interp = ds_concat.interp({t_name: interp_times}, method='linear')
                    
                    interp_nc = sandbox_dir / "temp_interp.nc"
                    ds_interp.to_netcdf(str(interp_nc))
                    
                    ds_last.close()
                    ds_next.close()
                    ds_interp.close()
                    
                    temp_merged_interp = sandbox_dir / "temp_merged_interp.nc"
                    if partial_nc.exists():
                        run_cdo(["cdo", "-s", "-O", "mergetime", str(partial_nc), str(interp_nc), str(temp_merged_interp)], log)
                        os.replace(str(temp_merged_interp), str(partial_nc))
                    else:
                        run_cdo(["cdo", "-s", "-O", "copy", str(interp_nc), str(partial_nc)], log)
                        
                    if interp_nc.exists(): interp_nc.unlink()
                except Exception as e:
                    log(f"[ERROR] Interpolation failed: {e}. Targets will be skipped.")
            else:
                log(f"[WARNING] No last_good_step available to interpolate for {len(missing_buffer)} steps. They will be skipped.")
            missing_buffer = []

        # Append temp_final to partial_nc
        temp_merged = sandbox_dir / "temp_merged.nc"
        if not is_prefetch:
            if not partial_nc.exists():
                run_cdo(["cdo", "-s", "-O", "copy", str(temp_final), str(partial_nc)], log)
            else:
                run_cdo(["cdo", "-s", "-O", "mergetime", str(partial_nc), str(temp_final), str(temp_merged)], log)
                os.replace(str(temp_merged), str(partial_nc))
        else:
            log(f"  -> Skipping CDO mergetime for {f.name} (pre-fetch only)")
            
        # Update previous state
        ds_raw = xr.open_dataset(str(temp_raw))
        previous_state[cycle] = {
            'step': step_hours,
            'tp': ds_raw['tp'].values if 'tp' in ds_raw else 0,
            'sw_energy': (ds_raw['ASWDIR_S'].values + ds_raw['ASWDIFD_S'].values) * (step_hours * 3600) if 'ASWDIR_S' in ds_raw else 0
        }
        ds_raw.close()
        
        # Delete downloaded source file to save space if in streaming mode
        if is_temp and f.exists():
            f.unlink()
            
        last_good = sandbox_dir / "last_good_step.nc"
        if temp_final.exists() and not is_prefetch:
            import shutil
            shutil.copy(str(temp_final), str(last_good))
            
        if queue:
            queue.put({"type": "STEP_COMPLETE", "worker_id": worker_id, "duration": time.time() - pr_start})
            
    # If the chunk ended perfectly inside a gap, flush the buffer with standard COPY_LAST
    if missing_buffer:
        last_good = sandbox_dir / "last_good_step.nc"
        if last_good.exists():
            log(f"  -> Chunk ended on a gap! Applying COPY_LAST fallback for {len(missing_buffer)} remaining missing steps...")
            try:
                import pandas as pd
                ds_last = xr.open_dataset(last_good)
                t_name = 'time' if 'time' in ds_last.coords else 'forecast_reference_time'
                
                for t in missing_buffer:
                    ds_copy = ds_last.assign_coords({t_name: [pd.Timestamp(t)]})
                    temp_fallback = sandbox_dir / "temp_fallback.nc"
                    ds_copy.to_netcdf(str(temp_fallback))
                    
                    temp_merged = sandbox_dir / "temp_merged_fallback.nc"
                    if partial_nc.exists():
                        run_cdo(["cdo", "-s", "-O", "mergetime", str(partial_nc), str(temp_fallback), str(temp_merged)], log)
                        os.replace(str(temp_merged), str(partial_nc))
                    else:
                        run_cdo(["cdo", "-s", "-O", "copy", str(temp_fallback), str(partial_nc)], log)
                        
                    if temp_fallback.exists(): temp_fallback.unlink()
                ds_last.close()
            except Exception as e:
                log(f"[ERROR] Fallback COPY_LAST failed: {e}")
                
    log("FINALIZING FORCING FILE")
    if partial_nc.exists():
        ds_final = xr.open_dataset(partial_nc)
        
        # Inject the true invariant topography (alt)
        if ds_invariant is not None and 'alt' in ds_invariant:
            alt_da = ds_invariant['alt']
            if 'time' in alt_da.dims:
                alt_da = alt_da.isel(time=0, drop=True)
            ds_final['alt'] = alt_da
        else:
            y_dim = ds_final.dims['y'] if 'y' in ds_final.dims else 150
            x_dim = ds_final.dims['x'] if 'x' in ds_final.dims else 100
            ds_final['alt'] = (['y', 'x'], np.full((y_dim, x_dim), np.nan))
            
        ds_final['alt'].attrs = {'standard_name': 'surface_altitude', 'units': 'm'}
        
        # Add scalar grid-mapping variable
        import pyproj
        crs = pyproj.CRS.from_epsg(32632)
        crs_cf = crs.to_cf()
        crs_cf['spatial_ref'] = crs.to_wkt()
        crs_cf['crs_wkt'] = crs.to_wkt()
        
        ds_final['crs'] = 0
        ds_final['crs'].attrs.update(crs_cf)
        
        for var in ds_final.data_vars:
            if var != 'crs':
                ds_final[var].attrs['grid_mapping'] = 'crs'
                
        # Ensure x and y coordinates have proper standard names
        if 'x' in ds_final.coords:
            ds_final['x'].attrs['standard_name'] = 'projection_x_coordinate'
            ds_final['x'].attrs['units'] = 'm'
        if 'y' in ds_final.coords:
            ds_final['y'].attrs['standard_name'] = 'projection_y_coordinate'
            ds_final['y'].attrs['units'] = 'm'
            
        # Add units
        for var in ds_final.data_vars:
            if var == 'temp': ds_final[var].attrs['units'] = 'K'
            if var == 'precip': ds_final[var].attrs['units'] = 'kg m-2'
            if var == 'rel_hum': ds_final[var].attrs['units'] = '%'
            if var == 'sw_in': ds_final[var].attrs['units'] = 'W m-2'
            if var == 'wind_speed': ds_final[var].attrs['units'] = 'm s-1'
            if var == 'snow_depth': ds_final[var].attrs['units'] = 'm'
            if var == 'swe': ds_final[var].attrs['units'] = 'kg m-2'
            
        ds_final.to_netcdf(output_nc)
        ds_final.close()
        if ds_invariant is not None:
            ds_invariant.close()
        
        log(f"Successfully generated chunk: {output_nc}")
        
        # Run the physics validator on the final output
        from .amundsen_validator import validate_forcing_file
        validate_forcing_file(str(output_nc))
        
        for f in [temp_raw, temp_derived, temp_remapped, temp_final, invariant_raw_nc, invariant_remapped_nc]:
            if f.exists(): f.unlink()
        try:
            sandbox_dir.rmdir()
        except Exception:
            pass
            
        if partial_nc.exists():
            partial_nc.unlink()
    else:
        log("[ERROR] Output generation failed.")
