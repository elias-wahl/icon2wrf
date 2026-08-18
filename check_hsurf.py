import xarray as xr
import sys

def check_hsurf(filename):
    print(f"Checking {filename} for HSURF (altitude)...")
    try:
        import cfgrib
        dss = cfgrib.open_datasets(filename)
        found = False
        for i, ds in enumerate(dss):
            if 'HSURF' in ds.data_vars or 'orog' in ds.data_vars:
                print(f"  -> FOUND in hypercube {i}! Variables: {list(ds.data_vars)}")
                found = True
                
        # Also check directly via backend_kwargs for paramId 500000
        try:
            ds_param = xr.open_dataset(filename, engine='cfgrib', backend_kwargs={'filter_by_keys': {'paramId': 500000}})
            if len(ds_param.data_vars) > 0:
                print(f"  -> FOUND via paramId=500000! Variables: {list(ds_param.data_vars)}")
                found = True
        except Exception:
            pass
            
        if not found:
            print("  -> NOT FOUND.")
    except Exception as e:
        print(f"Error reading {filename}: {e}")

if __name__ == "__main__":
    check_hsurf("input/2025071800_ilf3f00000000")
    check_hsurf("input/2025071812_ilf3f00120000")
