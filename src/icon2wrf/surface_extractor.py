import xarray as xr
import sys
import logging

import cfgrib
import xarray as xr
import warnings

warnings.filterwarnings("ignore", category=FutureWarning)

def extract_surface(input_grib: str, output_nc: str) -> bool:
    print(f"Extracting surface fields from {input_grib}...")
    try:
        dss = cfgrib.open_datasets(
            input_grib, 
            filter_by_keys={'typeOfLevel': 'surface', 'stepType': 'instant'}
        )
        if not dss:
            return False
        ds_sfc = max(dss, key=lambda d: len(d.data_vars))
        ds_sfc.to_netcdf(output_nc)
        print(f"Successfully saved surface fields to {output_nc}")
        return True
    except Exception as e:
        print(f"Error extracting surface fields: {e}")
        return False

def extract_3d(input_grib: str, output_nc: str) -> bool:
    print(f"Extracting 3D atmospheric fields from {input_grib}...")
    try:
        ds_3d = xr.open_dataset(
            input_grib, 
            engine='cfgrib', 
            backend_kwargs={'filter_by_keys': {'typeOfLevel': 'isobaricInhPa'}}
        )
        if len(ds_3d.data_vars) == 0:
            print(f"  -> No 3D atmospheric variables found in {input_grib}")
            return False
        ds_3d.to_netcdf(output_nc)
        print(f"Successfully saved 3D fields to {output_nc}")
        return True
    except Exception as e:
        print(f"Error extracting 3D fields: {e}")
        return False

def extract_soil_moist(input_grib: str, output_nc: str) -> bool:
    print(f"Extracting Soil Moisture fields from {input_grib}...")
    try:
        ds_soil = xr.open_dataset(
            input_grib, 
            engine='cfgrib', 
            backend_kwargs={'filter_by_keys': {'typeOfLevel': 'depthBelowLandLayer', 'stepType': 'instant'}}
        )
        if len(ds_soil.data_vars) == 0:
            print(f"  -> No Soil Moisture variables found in {input_grib}")
            return False
        ds_soil.to_netcdf(output_nc)
        print(f"Successfully saved Soil Moisture fields to {output_nc}")
        return True
    except Exception as e:
        print(f"Could not extract Soil Moisture fields: {e}")
        return False

def extract_soil_temp(input_grib: str, output_nc: str) -> bool:
    print(f"Extracting Soil Temperature fields from {input_grib}...")
    try:
        ds_soil = xr.open_dataset(
            input_grib, 
            engine='cfgrib', 
            backend_kwargs={'filter_by_keys': {'typeOfLevel': 'depthBelowLand', 'stepType': 'instant'}}
        )
        if len(ds_soil.data_vars) == 0:
            print(f"  -> No Soil Temperature variables found in {input_grib}")
            return False
        ds_soil.to_netcdf(output_nc)
        print(f"Successfully saved Soil Temperature fields to {output_nc}")
        return True
    except Exception as e:
        print(f"Could not extract Soil Temperature fields: {e}")
        return False
