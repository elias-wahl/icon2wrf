import numpy as np
import xarray as xr
import pyproj
import os

def create_grid():
    # Oetztal grid specs from the plan
    x = np.linspace(620250, 669750, 100)
    y = np.linspace(5170250, 5244750, 150)
    
    # pyproj needs meshgrid for 2D transformation
    X, Y = np.meshgrid(x, y)
    
    # EPSG:32632 is UTM 32N WGS84
    proj = pyproj.Proj("EPSG:32632")
    lon, lat = proj(X, Y, inverse=True)
    
    ds = xr.Dataset(
        coords={
            'y': (['y'], y),
            'x': (['x'], x),
            'lon': (['y', 'x'], lon),
            'lat': (['y', 'x'], lat),
        }
    )
    
    ds['lon'].attrs = {'standard_name': 'longitude', 'units': 'degrees_east'}
    ds['lat'].attrs = {'standard_name': 'latitude', 'units': 'degrees_north'}
    ds['x'].attrs = {'standard_name': 'projection_x_coordinate', 'units': 'm'}
    ds['y'].attrs = {'standard_name': 'projection_y_coordinate', 'units': 'm'}
    
    os.makedirs('config', exist_ok=True)
    ds.to_netcdf('config/oetztal_grid.nc')
    print("Created config/oetztal_grid.nc")

if __name__ == '__main__':
    create_grid()
