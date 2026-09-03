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
        
        # cfgrib often splits HSURF into its own dataset. We must forcefully merge it back in!
        for ds in dss:
            for var in ['HSURF', 'orog', 'z']:
                if var in ds.data_vars and var not in ds_sfc.data_vars:
                    ds_sfc[var] = ds[var]
        
        # Force CDO to recognize HSURF as GRIB2 Parameter 6, Category 3, Discipline 0 (SOILHGT)
        if 'HSURF' in ds_sfc.data_vars:
            ds_sfc['HSURF'].attrs['param'] = '6.3.0'
            ds_sfc['HSURF'].attrs.pop('GRIB_paramId', None)
                    
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
            
        # CDO reads the 'param' attribute to determine the GRIB2 parameter (num.cat.dis)
        for var in ds_soil.data_vars:
            ds_soil[var].attrs['param'] = '20.3.2'
            ds_soil[var].attrs.pop('GRIB_paramId', None)
            
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
            
        # CDO reads the 'param' attribute to determine the GRIB2 parameter (num.cat.dis)
        for var in ds_soil.data_vars:
            ds_soil[var].attrs['param'] = '18.3.2'
            ds_soil[var].attrs.pop('GRIB_paramId', None)
            
        ds_soil.to_netcdf(output_nc)
        print(f"Successfully saved Soil Temperature fields to {output_nc}")
        return True
    except Exception as e:
        print(f"Could not extract Soil Temperature fields: {e}")
        return False

def extract_openamundsen_fields(input_grib: str, output_nc: str) -> bool:
    print(f"Extracting openAMUNDSEN fields from {input_grib}...")
    try:
        vars_to_extract = {
            167: '2t', 
            165: '10u', 
            166: '10v', 
            260242: '2r', 
            228228: 'tp', 
            3066: 'sde', 
            228141: 'sd', 
            500480: 'ASWDIR_S', 
            500481: 'ASWDIFD_S',
            500000: 'alt'
        }
        vars_to_merge = []
        
        try:
            import cfgrib
            dss = cfgrib.open_datasets(input_grib)
            for ds in dss:
                for var_name, da in ds.data_vars.items():
                    paramId = da.attrs.get('GRIB_paramId')
                    shortName = da.attrs.get('GRIB_shortName', var_name)
                    
                    # Extract standard variables
                    if paramId in vars_to_extract:
                        expected_name = vars_to_extract[paramId]
                        # Don't extract the same variable twice if it's duplicated across hypercubes
                        if not any(v.name == expected_name for v in vars_to_merge):
                            da = da.rename(expected_name)
                            coords_to_drop = [c for c in da.coords if c not in ['time', 'step', 'valid_time', 'latitude', 'longitude', 'values']]
                            da = da.drop_vars(coords_to_drop, errors='ignore')
                            vars_to_merge.append(da)
                            
                    # Extract HSURF altitude as a special robust fallback
                    elif paramId == 500000 or shortName == 'HSURF':
                        if not any(v.name == 'alt' for v in vars_to_merge):
                            da = da.rename('alt')
                            coords_to_drop = [c for c in da.coords if c not in ['time', 'step', 'valid_time', 'latitude', 'longitude', 'values']]
                            da = da.drop_vars(coords_to_drop, errors='ignore')
                            vars_to_merge.append(da)
        except Exception as e:
            print(f"[ERROR] Failed to process {input_grib}: {e}")
            pass
        
        if not vars_to_merge:
            return False
            
        ds_out = xr.merge(vars_to_merge, compat='override')
        
        ds_out.to_netcdf(output_nc)
        print(f"Successfully saved openAMUNDSEN fields to {output_nc}")
        return True
    except Exception as e:
        print(f"Error extracting openAMUNDSEN fields: {e}")
        return False


# ---------------------------------------------------------------------------
# Model-level route (2026-09-02, OPEN_ISSUES A19/A21): the raw ICON files carry
# u, v, t, q, pres on all 65 native model levels (typeOfLevel generalVerticalLayer)
# plus HHL (half-level heights, 66) in the lead-0 file. The default extract_3d
# keeps only the 11 diagnostic pressure levels, which have nothing between
# ~584 and ~1522 m ASL and erase the valley wind and the cold pool. This route
# interpolates the model levels in ln(p) onto a dense pressure ladder and
# writes a dataset with the SAME variables/attributes as the pressure-level
# product (z, t, r, u, v; w omitted), so metgrid/real stay unchanged.
# ---------------------------------------------------------------------------
ML_PLEVS_HPA = [1000, 990, 980, 970, 960, 950, 940, 930, 920, 910, 900, 890, 880,
                870, 860, 850, 840, 830, 820, 810, 800, 780, 760, 740, 720, 700,
                650, 600, 550, 500, 450, 400, 350, 300, 250, 200]


def _rh_from_q(q, T, p):
    import numpy as np
    e = q * p / (0.622 + 0.378 * q)
    es = 611.2 * np.exp(17.67 * (T - 273.15) / (T - 29.65))
    return np.clip(100.0 * e / es, 0.0, 100.0)


def extract_3d_ml(input_grib: str, lead0_grib: str, output_nc: str, plevs_hpa=None) -> bool:
    """Build z,t,r,u,v on dense pressure levels from the ICON model levels."""
    import numpy as np
    plevs_hpa = plevs_hpa or ML_PLEVS_HPA
    print(f"Extracting 3D fields from MODEL LEVELS of {input_grib} -> {len(plevs_hpa)} pressure levels...")
    try:
        kw = dict(engine="cfgrib", backend_kwargs={"filter_by_keys": {"typeOfLevel": "generalVerticalLayer"}})
        ml = xr.open_dataset(input_grib, **kw)
        need = ["u", "v", "t", "q", "pres"]
        if any(n not in ml.data_vars for n in need):
            print(f"  -> model-level fields missing ({[n for n in need if n not in ml.data_vars]})")
            return False
        hhl = xr.open_dataset(lead0_grib, engine="cfgrib",
                              backend_kwargs={"filter_by_keys": {"shortName": "HHL"}})["HHL"].values  # (66, N), 1 = top
        tmpl = xr.open_dataset(input_grib, engine="cfgrib",
                               backend_kwargs={"filter_by_keys": {"typeOfLevel": "isobaricInhPa"}})

        # bottom-first ordering (index 0 = lowest layer = ICON layer 65)
        order = np.argsort(ml["generalVerticalLayer"].values)[::-1]
        p = ml["pres"].values[order].astype(np.float64)          # Pa, decreasing along axis 0
        T = ml["t"].values[order].astype(np.float64)
        q = ml["q"].values[order].astype(np.float64)
        u = ml["u"].values[order].astype(np.float64)
        v = ml["v"].values[order].astype(np.float64)
        zf = 0.5 * (hhl[:-1] + hhl[1:])                           # full-level heights, 1 = top
        zf = zf[::-1]                                             # bottom-first, matches `order`
        lnp = np.log(p)
        N = p.shape[1]
        RD, G, GAMMA = 287.05, 9.80665, 0.0065

        out = {k: np.empty((len(plevs_hpa), N), dtype=np.float32) for k in ("z", "t", "r", "u", "v")}
        for li, ph in enumerate(plevs_hpa):
            pt = ph * 100.0
            lnpt = np.log(pt)
            k = (p > pt).sum(axis=0)                              # first level with p <= pt
            below = k == 0                                        # target under the lowest layer
            top = k >= p.shape[0]
            lo = np.clip(k - 1, 0, p.shape[0] - 2)
            hi = lo + 1
            def gat(a, idx):
                return np.take_along_axis(a, idx[None, :], axis=0)[0]
            w = (lnpt - gat(lnp, lo)) / (gat(lnp, hi) - gat(lnp, lo))
            w = np.where(top, 1.0, w)
            def lin(a):
                return gat(a, lo) + w * (gat(a, hi) - gat(a, lo))
            Tt, ut, vt, qt, zt = lin(T), lin(u), lin(v), lin(q), lin(zf)
            if below.any():                                       # hypsometric extrapolation downward
                T0, z0, p0 = T[0][below], zf[0][below], p[0][below]
                zb = z0 - RD * T0 / G * np.log(pt / p0)
                zt[below] = zb
                Tt[below] = T0 + GAMMA * (z0 - zb)
                ut[below], vt[below], qt[below] = u[0][below], v[0][below], q[0][below]
            out["z"][li] = (G * zt).astype(np.float32)
            out["t"][li] = Tt
            out["r"][li] = _rh_from_q(qt, Tt, pt)
            out["u"][li] = ut
            out["v"][li] = vt

        coords = {"isobaricInhPa": ("isobaricInhPa", np.array(plevs_hpa, dtype=np.float64), dict(tmpl["isobaricInhPa"].attrs)),
                  "values": ("values", np.arange(N))}
        for c in ("time", "step", "valid_time"):
            if c in ml.coords:
                coords[c] = ml[c]
        ds = xr.Dataset({k: (("isobaricInhPa", "values"), out[k]) for k in out}, coords=coords, attrs=dict(tmpl.attrs))
        for k in out:
            if k in tmpl.data_vars:
                ds[k].attrs = dict(tmpl[k].attrs)
        ds.attrs["history"] = ds.attrs.get("history", "") + " | icon2wrf extract_3d_ml: 65 model levels -> dense plevs (2026-09-02)"
        ds.to_netcdf(output_nc)
        print(f"Successfully saved model-level-derived 3D fields ({len(plevs_hpa)} levels) to {output_nc}")
        return True
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"Error in extract_3d_ml: {e}")
        return False
