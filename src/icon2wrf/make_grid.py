import sys
import xarray as xr
import numpy as np

if len(sys.argv) < 2:
    print("Usage: python make_grid.py <source_file>")
    sys.exit(1)

source_file = sys.argv[1]
print(f"Loading {source_file}...")
ds = xr.open_dataset(source_file)

if 'clat' in ds:
    lats = ds['clat'].values
    lons = ds['clon'].values
else:
    lats = ds['lat'].values
    lons = ds['lon'].values

# Convert radians to degrees if necessary
if np.nanmax(np.abs(lats)) < 4.0:
    lats = np.rad2deg(lats)
    lons = np.rad2deg(lons)

gridsize = len(lats)

print(f"Generating temp_source_grid.txt for {gridsize} points...")
with open('temp_source_grid.txt', 'w') as f:
    f.write("gridtype = unstructured\n")
    f.write(f"gridsize = {gridsize}\n")
    
    # Write longitudes (xvals)
    f.write("xvals = ")
    # write in chunks to be fast
    np.savetxt(f, lons.reshape(1, -1), fmt='%.6f', delimiter=' ')
    f.write("\n")
    
    # Write latitudes (yvals)
    f.write("yvals = ")
    np.savetxt(f, lats.reshape(1, -1), fmt='%.6f', delimiter=' ')
    f.write("\n")

print("Generating temp_target_grid.txt (0.0065 x 0.0045 resolution)...")
xinc = 0.0065
yinc = 0.0045
xfirst = float(np.nanmin(lons))
yfirst = float(np.nanmin(lats))
xmax = float(np.nanmax(lons))
ymax = float(np.nanmax(lats))
xsize = int(np.ceil((xmax - xfirst) / xinc))
ysize = int(np.ceil((ymax - yfirst) / yinc))

with open('temp_target_grid.txt', 'w') as f:
    f.write("gridtype  = lonlat\n")
    f.write(f"xsize     = {xsize}\n")
    f.write(f"ysize     = {ysize}\n")
    f.write(f"xfirst    = {xfirst:.4f}\n")
    f.write(f"xinc      = {xinc:.4f}\n")
    f.write(f"yfirst    = {yfirst:.4f}\n")
    f.write(f"yinc      = {yinc:.4f}\n")

print("Done! Temporary grids generated successfully.")
