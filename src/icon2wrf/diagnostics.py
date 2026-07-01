import xarray as xr
import sys
import os

def check_wrf_ready(filename, sfc_file=None, soil_files=None):
    print(f"\n{'='*50}")
    print(f"--- WRF WPS Diagnostics for: {filename} ---")
    print(f"{'='*50}")
    
    try:
        ds = xr.open_dataset(filename)
        vars_in_ds = [str(v).lower() for v in ds.data_vars]
        
        if sfc_file and os.path.exists(sfc_file):
            print(f"[INFO] Also checking {sfc_file} for surface fields...")
            ds_sfc = xr.open_dataset(sfc_file)
            vars_in_ds.extend([str(v).lower() for v in ds_sfc.data_vars])
            
        if soil_files:
            for s_file in soil_files:
                if s_file and os.path.exists(s_file):
                    print(f"[INFO] Also checking {s_file} for soil fields...")
                    ds_soil = xr.open_dataset(s_file)
                    vars_in_ds.extend([str(v).lower() for v in ds_soil.data_vars])
            
    except Exception as e:
        print(f"[ERROR] Could not open dataset: {e}")
        return False, None
        
    # 1. Identify Vertical Levels & Recommend Vtable
    z_dim = None
    level_type = "unknown"
    invert_needed = False
    for dim in ds.dims:
        if "isobaric" in dim.lower() or "plev" in dim.lower():
            z_dim = dim
            level_type = "pressure"
            break
        elif "layer" in dim.lower() or "model" in dim.lower() or "lev" in dim.lower() or "generalvertical" in dim.lower():
            if dim not in ["lat", "lon", "time", "step", "valid_time"]:
                z_dim = dim
                level_type = "model"
                break
                
    vtable_suggestion = None
    if not z_dim:
        print("[WARNING] Could not identify a vertical dimension (isobaric or model levels).")
        print("          Are you sure this file contains 3D atmospheric data?")
    else:
        levels = ds[z_dim].values
        print(f"[OK] Found vertical dimension: '{z_dim}' ({level_type} levels)")
        
        if level_type == "pressure":
            vtable_suggestion = "Vtable.ICONp"
            if len(levels) > 1:
                if levels[0] < levels[-1]:
                    print(f"[ERROR] Pressure levels are ascending ({levels[0]} to {levels[-1]}).")
                    print("        WRF expects pressure to decrease (e.g. 1000 hPa -> 10 hPa).")
                    invert_needed = True
                else:
                    print(f"[OK] Pressure levels are in the correct descending order ({levels[0]} down to {levels[-1]}).")
                    
        elif level_type == "model":
            vtable_suggestion = "Vtable.ICONm"
            if len(levels) > 1:
                print(f"[WARNING] Native model levels detected ({levels[0]} to {levels[-1]}).")
                print("          ICON natively orders levels Top-to-Bottom (1=Top, N=Surface).")
                print("          WRF's metgrid expects Bottom-to-Top.")
                print("          Check if invertlev is needed.")

    # 2. Check for Essential Variables
    expected_atmos = {
        "Temperature": ["t", "tt", "temp"],
        "U-wind": ["u", "uu"],
        "V-wind": ["v", "vv"],
        "Humidity (RH or Spec. Hum)": ["r", "rh", "q", "hus"]
    }
    
    expected_sfc = {
        "Surface Pressure": ["sp", "ps", "psfc"],
        "Sea-level Pressure": ["msl", "pmsl"],
        "Skin Temperature": ["skt", "skintemp", "ts", "t_g"],
        "Land/Sea Mask": ["lsm", "landsea", "fr_land"],
        "Soil Temperature": ["t_so", "st000010"],
        "Soil Moisture": ["w_so", "smi", "sm000010"]
    }
    
    print("\n--- Checking Atmospheric Variables ---")
    for desc, names in expected_atmos.items():
        found = [name for name in names if name in vars_in_ds]
        if found:
            print(f"[OK] Found {desc} (variable: {found[0]})")
        else:
            print(f"[ERROR] Missing {desc} (Looked for: {', '.join(names)})")
            
    print("\n--- Checking Surface Variables ---")
    for desc, names in expected_sfc.items():
        found = [name for name in names if name in vars_in_ds]
        if found:
            print(f"[OK] Found {desc} (variable: {found[0]})")
        else:
            if "Sea-level" in desc:
                print(f"[INFO] Missing {desc} (Looked for: {', '.join(names)})")
                print("       -> NOT A PROBLEM: WRF metgrid will automatically calculate this from Surface Pressure and topography.")
            elif "Land/Sea" in desc:
                print(f"[INFO] Missing {desc} (Looked for: {', '.join(names)})")
                print("       -> NOT A PROBLEM: WRF metgrid will automatically fall back to using your static geogrid Land/Sea mask.")
            else:
                print(f"[WARNING] Missing {desc} (Looked for: {', '.join(names)}) - This may cause issues in metgrid/real.")
            
    print("\nDone with diagnostics.\n")
    return True, vtable_suggestion
