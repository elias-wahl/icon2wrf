import xarray as xr
import numpy as np
import sys
from pathlib import Path

def validate_forcing_file(nc_file):
    print(f"\n=== Validating openAMUNDSEN Forcing File ===")
    print(f"File: {nc_file}")
    
    if not Path(nc_file).exists():
        print(f"[ERROR] File {nc_file} does not exist.")
        return False
        
    ds = xr.open_dataset(nc_file)
    
    # Define physical limits (min, max) for variables
    limits = {
        'temp': {'min': 223.15, 'max': 313.15, 'unit': 'K'}, # -50C to +40C
        'rel_hum': {'min': 0.0, 'max': 100.1, 'unit': '%'}, # 0% to 100% (allow tiny over-saturation rounding)
        'wind_speed': {'min': 0.0, 'max': 50.0, 'unit': 'm/s'}, # 0 to 50 m/s
        'precip': {'min': 0.0, 'max': 100.0, 'unit': 'kg/m2'}, # 0 to 100 mm/h
        'sw_in': {'min': -1.0, 'max': 1400.0, 'unit': 'W/m2'}, # Allow -1 for nighttime rounding errors
        'swe': {'min': 0.0, 'max': 5000.0, 'unit': 'kg/m2'},
        'snow_depth': {'min': 0.0, 'max': 20.0, 'unit': 'm'},
        'alt': {'min': -10.0, 'max': 5000.0, 'unit': 'm'} # Allow CDO padding zeros and peaks up to 5000m
    }
    
    passed = True
    
    for var, bounds in limits.items():
        if var not in ds:
            print(f"⚠️ [WARNING] Missing expected variable: {var}")
            continue
            
        data = ds[var].values
        
        # Check for NaNs
        nan_count = np.isnan(data).sum()
        if nan_count > 0:
            print(f"❌ [ERROR] {var}: Contains {nan_count} NaN values!")
            passed = False
            
        # Check limits (ignoring NaNs for the min/max calculation)
        v_min = np.nanmin(data)
        v_max = np.nanmax(data)
        
        errors = []
        
        # Hard boundary violations
        if v_min < bounds['min']:
            errors.append(f"Minimum {v_min:.2f} is below realistic limit ({bounds['min']} {bounds['unit']})")
        if v_max > bounds['max']:
            errors.append(f"Maximum {v_max:.2f} is above realistic limit ({bounds['max']} {bounds['unit']})")
            
        # Specific strict logic checks (e.g. physics failures)
        if var == 'precip' and v_min < -0.01:
            errors.append(f"Negative precipitation detected ({v_min:.4f})! De-accumulation math likely failed.")
        if var == 'sw_in' and v_min < -5.0:
            errors.append(f"Significantly negative shortwave radiation detected ({v_min:.2f})! De-averaging math likely failed.")
        if var == 'rel_hum' and v_max > 105.0:
            errors.append(f"Relative humidity massively exceeds 100% ({v_max:.1f}%)")
            
        if errors:
            print(f"❌ [FAIL] {var:<12} (Min: {v_min:>7.2f}, Max: {v_max:>7.2f})")
            for e in errors:
                print(f"         -> {e}")
            passed = False
        else:
            print(f"✅ [OK]   {var:<12} (Min: {v_min:>7.2f}, Max: {v_max:>7.2f})")
            
    print("\n--- Validation Summary ---")
    if passed:
        print("🎉 Validation PASSED. All variables are within realistic physical bounds.")
    else:
        print("⚠️ Validation FAILED. Please review the errors above to determine if the forcing data is physically compromised.")
        
    ds.close()
    return passed

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m src.icon2wrf.amundsen_validator <path_to_nc_file>")
        sys.exit(1)
    
    validate_forcing_file(sys.argv[1])
