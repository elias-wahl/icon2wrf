import sys
import warnings

# Suppress the spammy xarray FutureWarnings
warnings.filterwarnings("ignore", category=FutureWarning)

import cfgrib

if len(sys.argv) < 2:
    print("Usage: python inspect_cluster_grib.py <path_to_grib2_file>")
    sys.exit(1)

file_path = sys.argv[1]
print(f"Opening {file_path}")

try:
    # Get all datasets since cfgrib splits them by hypercube (level type)
    dss = cfgrib.open_datasets(file_path)
    for ds in dss:
        print("\n--- Dataset ---")
        print("Coordinates/Levels:", list(ds.coords))
        print("Variables:")
        for var in ds.data_vars:
            da = ds[var]
            attrs = da.attrs
            param = attrs.get('GRIB_paramId', attrs.get('param', 'unknown'))
            name = attrs.get('GRIB_name', attrs.get('long_name', 'unknown'))
            shortName = attrs.get('GRIB_shortName', 'unknown')
            stepType = attrs.get('GRIB_stepType', 'unknown')
            print(f"  {shortName} (paramId={param}): {name} [stepType={stepType}]")
except Exception as e:
    print(f"Error reading file: {e}")
    print("\nIf you are on the cluster, ensure you have 'cfgrib' and 'xarray' installed in your active Python environment.")
