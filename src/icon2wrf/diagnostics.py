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
            
            # Print pressure range hint
            min_p = min(levels)
            max_p = max(levels)
            print(f"[INFO] Pressure Range: {min_p} to {max_p} (Min pressure is highest altitude)")
            
            # If the value is in hPa (e.g. 200), we suggest Pa (20000). If it's already in Pa, just print it.
            suggested_ptop = min_p * 100 if min_p < 2000 else min_p
            print(f"       -> Ensure namelist 'p_top_requested' is >= {int(suggested_ptop)} Pa")
                    
        elif level_type == "model":
            vtable_suggestion = "Vtable.ICONm"
            if len(levels) > 1:
                print(f"[WARNING] Native model levels detected ({levels[0]} to {levels[-1]}).")
                print("          ICON natively orders levels Top-to-Bottom (1=Top, N=Surface).")
                print("          WRF's metgrid expects Bottom-to-Top.")
                print("          Check if invertlev is needed.")
                
    # 1.5 Extract Domain Size
    print("\n--- Domain Extent ---")
    try:
        lat_key = "lat" if "lat" in ds else "latitude" if "latitude" in ds else None
        lon_key = "lon" if "lon" in ds else "longitude" if "longitude" in ds else None
        
        if lat_key and lon_key:
            lat_min, lat_max = float(ds[lat_key].min()), float(ds[lat_key].max())
            lon_min, lon_max = float(ds[lon_key].min()), float(ds[lon_key].max())
            print(f"Latitude boundaries  : {lat_min:.2f} to {lat_max:.2f} degrees")
            print(f"Longitude boundaries : {lon_min:.2f} to {lon_max:.2f} degrees")
            print("-> Ensure your WRF domain (e_we, e_sn) fits entirely WITHIN these boundaries.")
        else:
            print("[INFO] Could not automatically determine lat/lon boundaries from file.")
    except Exception as e:
        print(f"[INFO] Could not automatically determine lat/lon boundaries: {e}")

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
                print(f"[WARNING] Missing {desc} (Looked for: {', '.join(names)}) - This may cause issues in metgrid/real.")
            
    print("\nDone with intermediate diagnostics.\n")
    return True, vtable_suggestion

def check_final_gribs(grib_3d=None, grib_sfc=None, grib_soil_moist=None, grib_soil_temp=None):
    """
    Validates the generated GRIB2 files to ensure they contain the required 
    GRIB variables and parameters exactly as WRF/WPS expects them based on Vtable.ICONp.
    """
    try:
        import eccodes
    except ImportError:
        print("[WARNING] python eccodes module not found. Skipping final GRIB validation.")
        return

    print(f"\n{'='*50}")
    print("--- Final GRIB2 Output Validation ---")
    print(f"{'='*50}")

    def scan_grib(filepath):
        if not filepath or not os.path.exists(filepath):
            return []
        params = set()
        with open(filepath, 'rb') as f:
            while True:
                gid = eccodes.codes_grib_new_from_file(f)
                if gid is None:
                    break
                try:
                    dis = eccodes.codes_get_long(gid, 'discipline')
                    cat = eccodes.codes_get_long(gid, 'parameterCategory')
                    num = eccodes.codes_get_long(gid, 'parameterNumber')
                    # Format as num.cat.dis for easy reading, or just tuple
                    params.add((dis, cat, num))
                except eccodes.KeyValueNotFoundError:
                    pass
                eccodes.codes_release(gid)
        return params

    if grib_sfc:
        print(f"\nChecking Surface GRIB2: {os.path.basename(grib_sfc)}")
        sfc_params = scan_grib(grib_sfc)
        # Check for SOILHGT (0, 3, 6)
        if (0, 3, 6) in sfc_params:
            print("[OK] SOILHGT (Terrain Height) is correctly mapped (Discipline 0, Category 3, Parameter 6)!")
        else:
            print("[ERROR] SOILHGT (0, 3, 6) is MISSING in the final surface file!")
            print("        -> WPS will fail to compute surface pressure in real.exe!")
            
        # Check for LANDSEA (2, 0, 0)
        if (2, 0, 0) in sfc_params:
            print("[OK] LANDSEA (Land/Sea Mask) is correctly mapped (Discipline 2, Category 0, Parameter 0)!")
        else:
            print("[INFO] LANDSEA (2, 0, 0) is missing. WRF will fallback to static geogrid mask.")

    if grib_soil_moist:
        print(f"\nChecking Soil Moisture GRIB2: {os.path.basename(grib_soil_moist)}")
        sm_params = scan_grib(grib_soil_moist)
        # Check for Soil Moisture (2, 3, 20)
        if (2, 3, 20) in sm_params:
            print("[OK] Soil Moisture is correctly mapped (Discipline 2, Category 3, Parameter 20)!")
        else:
            print("[ERROR] Soil Moisture (2, 3, 20) is MISSING in the final soil moisture file!")

    if grib_soil_temp:
        print(f"\nChecking Soil Temp GRIB2: {os.path.basename(grib_soil_temp)}")
        st_params = scan_grib(grib_soil_temp)
        # Check for Soil Temp (2, 3, 18)
        if (2, 3, 18) in st_params:
            print("[OK] Soil Temperature is correctly mapped (Discipline 2, Category 3, Parameter 18)!")
        else:
            print("[ERROR] Soil Temperature (2, 3, 18) is MISSING in the final soil temp file!")
            
    print("\nDone with final GRIB validation.\n")

