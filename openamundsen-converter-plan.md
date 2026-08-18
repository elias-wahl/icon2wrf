# ICON-to-openAMUNDSEN converter plan

## Purpose

Extend `elias-wahl/icon2wrf` with an openAMUNDSEN export path for a conventional,
non-data-assimilation winter simulation. The converter will transform the current
500 m ICON products into one compact, scientifically explicit NetCDF file for the
Oetztal study area. A separate adapter in Franz's study project will later reshape
that grid into openAMUNDSEN's supported in-memory point-forcing dataset.

This first implementation targets ICON, not final WRF output. The scientific
comparison is therefore station-forced versus ICON-forced openAMUNDSEN, with ICON
snow depth and snow water equivalent retained as independent reference fields.

## Current pipeline problem

The current `--netcdf` path in `icon2wrf` is still a WRF-oriented broad export:

- It retains many variables that openAMUNDSEN does not need.
- The current sample contains 53 surface variables plus pressure-level and soil
  data and occupies about 416 MiB for one timestep.
- The surface extraction does not provide the required true 2 m air temperature
  and 10 m wind fields in the resulting NetCDF.
- It applies the same distance-weighted remapping to all variables.
- It uses `setmisstonn`, which silently fills missing values with nearest
  neighbours.
- Accumulation and averaging periods are not explicit enough for a continuous
  winter forcing series.

The openAMUNDSEN converter must therefore branch from the ICON processing chain
before the broad WRF-oriented NetCDF loses required level and time metadata. It
must not be implemented as a filter that merely renames variables in the current
sample file.

## openAMUNDSEN source of truth

The compatibility target is openAMUNDSEN v1.1.6, local commit
`5613405c65049c247d6d591b1fc8104ef1950489`.

Relevant upstream contracts:

- `openamundsen/constants.py`: `METEO_VAR_METADATA` and
  `MINIMUM_REQUIRED_METEO_VARS`
- `openamundsen/forcing.py`: point-dataset structure and validation
- `openamundsen/fileio/meteo.py`: NetCDF reading, unit checking and precipitation
  conversion
- `openamundsen/README.md`: documented meteorological input schema

openAMUNDSEN requires these five meteorological forcings:

| Variable | Exact unit | Standard name | Reference height/meaning |
| --- | --- | --- | --- |
| `temp` | `K` | `air_temperature` | Air temperature at 2 m |
| `precip` | `kg m-2` | `precipitation_amount` | Amount accumulated over the timestep |
| `rel_hum` | `%` | `relative_humidity` | Relative humidity at 2 m |
| `sw_in` | `W m-2` | `surface_downwelling_shortwave_flux_in_air` | Mean incoming global shortwave flux over the timestep |
| `wind_speed` | `m s-1` | `wind_speed` | Wind speed at 10 m |

The unit strings above must be written exactly because openAMUNDSEN validates
them. Surface pressure and incoming longwave radiation may be useful internally
for derivations, but they are not part of the exported forcing contract.

openAMUNDSEN's documented file input uses one NetCDF time series per station.
That is unsuitable for approximately 15,000 ICON cells. The converter will
therefore create a canonical gridded handoff file. Franz's adapter will use the
official openAMUNDSEN `memory` input path, which accepts a combined
`(time, station)` dataset. This remains a standard openAMUNDSEN model run and does
not involve data assimilation.

## Locked output contract

### File grouping and time coverage

- Produce one NetCDF file for the complete requested winter.
- Start and end times are required configuration/CLI inputs; no winter is
  hard-coded.
- Preserve the native ICON output times. The current source is hourly.
- Never silently upsample hourly ICON forcing to 30-minute forcing.
- Permit explicit aggregation to a coarser interval only if all aggregation rules
  are documented and tested.
- Require a strictly increasing, duplicate-free and complete forcing timeline.

Suggested filename:

```text
icon_openamundsen_oetztal_<start>_<end>.nc
```

### Spatial grid

Use a regular 500 m grid in UTM zone 32N (`EPSG:32632`) over the approved
Oetztal rectangle plus a 10 km buffer:

| Boundary | Value |
| --- | ---: |
| Minimum easting | 620000 m |
| Maximum easting | 670000 m |
| Minimum northing | 5170000 m |
| Maximum northing | 5245000 m |
| Resolution | 500 m |
| Columns | 100 |
| Rows | 150 |

Use cell centres from 620250 to 669750 m easting and from 5170250 to
5244750 m northing. The rectangle corners in WGS84 are approximately:

| Corner | Longitude | Latitude |
| --- | ---: | ---: |
| Southwest | 10.568876 E | 46.672801 N |
| Southeast | 11.222146 E | 46.661975 N |
| Northeast | 11.250297 E | 47.336368 N |
| Northwest | 10.588761 E | 47.347451 N |

### Dimensions and support metadata

The main dimensions are:

```text
(time, y, x)
```

The file may contain only the seven scientific fields defined below, plus the
support data required to interpret them:

- `time(time)`: valid time, UTC
- `time_bounds(time, bounds)`: start and end of each forcing interval
- `x(x)`, `y(y)`: UTM cell-centre coordinates in metres
- `lon(y, x)`, `lat(y, x)`: WGS84 cell-centre coordinates
- `alt(y, x)`: ICON terrain altitude in metres, with
  `standard_name = "surface_altitude"`
- Forecast reference time and/or forecast lead for every output timestep so run
  stitching remains traceable
- Global provenance attributes: converter version/commit, source model, source
  files or source-cycle summary, run-selection strategy, creation history, CRS,
  bounds, resolution and measurement heights

No pressure, longwave radiation, soil state, cloud, radiation-component, wind
component or other ICON scientific variable may remain in the final file.

### Seven scientific variables

| Output | Exact unit | Standard name | ICON source or derivation |
| --- | --- | --- | --- |
| `temp` | `K` | `air_temperature` | True 2 m air temperature; never `T_G` |
| `precip` | `kg m-2` | `precipitation_amount` | Interval amount derived from total precipitation `tp` |
| `rel_hum` | `%` | `relative_humidity` | Prefer true 2 m RH; see decision gate below |
| `sw_in` | `W m-2` | `surface_downwelling_shortwave_flux_in_air` | `ASWDIR_S + ASWDIFD_S`, converted to an interval mean when required |
| `wind_speed` | `m s-1` | `wind_speed` | Remap 10 m U and V separately, then calculate magnitude |
| `snow_depth` | `m` | `surface_snow_thickness` | ICON `sde` |
| `swe` | `kg m-2` | `surface_snow_amount` | ICON `sd` |

Snow depth and SWE are comparison/reference variables only. They must not be
used to initialize, nudge, force or assimilate openAMUNDSEN snow states. Sparse
snow reference times are allowed, but whenever a snow timestamp is populated,
both fields should be present and internally consistent. Missing snow reference
times must not weaken the strict completeness requirements for the five
meteorological forcings.

## Extraction and conversion rules

### Near-surface fields

- Select fields by GRIB parameter and level metadata, not fragile short-name-only
  matching.
- Require 2 m air temperature and 10 m wind components.
- Remap 10 m U and V components before calculating
  `wind_speed = sqrt(u10**2 + v10**2)`.
- Do not use `T_G` as air temperature.
- Do not use `AUMFL_S` or `AVMFL_S` as wind components; they are momentum fluxes.
- Do not use net shortwave radiation in place of incoming global shortwave.

### Humidity decision gate for Elias

Before implementation, inspect the raw ICON GRIB inventory and record the exact
available humidity parameters and levels in this plan or the implementation PR.

Recommended priority:

1. Use direct 2 m relative humidity if available.
2. If direct RH is unavailable, derive it only from humidity, temperature and
   pressure fields whose levels and time semantics are physically compatible.
3. Document the formula, phase convention and pressure treatment and cover them
   with numerical tests.
4. If a defensible matching-level derivation is impossible, stop with a precise
   missing-field error.

`QV_S + sp` must not be accepted automatically without establishing that the
humidity and temperature levels match the intended 2 m forcing.

### Precipitation

`precip` must represent the amount during each output interval, not a rate and
not accumulation since forecast initialization.

- Read forecast reference time, valid time, `stepType`, start step and end step
  from GRIB metadata.
- If `tp` already describes the individual output interval, use it directly.
- If `tp` accumulates from forecast initialization, difference successive values
  from the same forecast cycle.
- Handle forecast-cycle resets explicitly; never difference fields belonging to
  different cycles.
- Require any predecessor timestep needed for deaccumulation. Fail rather than
  guessing when it is missing.
- Clip only tiny floating-point negatives within a documented tolerance to zero.
  Larger negative interval amounts are an error.
- Label each amount at the interval end and write its exact `time_bounds`.

Do not use the current `crr` or `lsrr` metadata without validation: their reported
rate units conflict with their accumulation behaviour in the received sample.

### Shortwave radiation

- Combine downward direct and downward diffuse shortwave radiation.
- Determine from GRIB time-range metadata whether each source value is an
  interval mean or a mean since forecast initialization.
- If it is a forecast-period mean, convert it back to accumulated energy and
  difference successive fields before calculating the interval mean.
- Handle cycle resets using the same rules as precipitation.
- Write `cell_methods = "time: mean"` and exact `time_bounds`.

### Instantaneous fields

Temperature, relative humidity, wind and snow states are instantaneous at the
valid time unless their source metadata explicitly states otherwise. Preserve
that meaning and record an appropriate `cell_methods` attribute.

## Forecast-run stitching decision gate for Elias

The existing downloader supports two scientifically different strategies:

- `freshest`: switch to the newest run approximately every 12 hours
- `longest`: remain with a forecast for up to approximately 48 hours

The converter must support the selected downloader output without hiding cycle
changes. Recommended implementation behaviour:

- Require `freshest` or `longest` as an explicit runtime choice; do not use a
  silent default.
- Preserve forecast reference time and lead for every valid time.
- Validate that every selected timestamp is covered exactly once.
- Perform accumulation/average conversion within each forecast cycle before
  stitching the interval fields into the winter series.
- Record the strategy in global metadata.

Elias should select the production strategy based on the atmospheric experiment,
but both strategies must produce deterministic, traceable output.

## Remapping policy

Use variable-aware remapping onto the approved 500 m UTM grid:

- Conservative remapping for interval precipitation amount and SWE.
- Continuous interpolation for temperature, relative humidity, shortwave
  radiation, snow depth and terrain altitude.
- Remap U and V wind components independently, then calculate wind speed.
- Preserve source missingness during remapping.
- Do not run `setmisstonn` or any equivalent blanket nearest-neighbour fill.

The implementation must document the exact CDO or alternative operators used and
their treatment of missing source cells.

## Missing-data and validity policy

Fail conversion if any of the following occurs:

- A required meteorological source field is unavailable.
- An expected forcing timestamp is missing or duplicated.
- Time is not strictly increasing.
- Any of the five forcing variables is missing on a valid target cell.
- Units cannot be converted unambiguously to the exact output unit.
- Precipitation intervals or radiation averaging intervals cannot be resolved.
- Values violate physical ranges beyond a documented numerical tolerance.

Cells outside valid ICON coverage may remain NaN and are excluded by the
downstream adapter. The converter must distinguish such cells from accidental
gaps inside the valid study area. A separate scientific mask variable is not
part of the seven-variable output contract; validity can be represented through
the support coordinates/altitude and consistent NaNs.

Minimum range checks should include:

- `precip >= 0`
- `0 <= rel_hum <= 100`
- `sw_in >= 0`
- `wind_speed >= 0`
- `snow_depth >= 0` where present
- `swe >= 0` where present
- Plausible configured bounds for 2 m temperature and terrain altitude

## File format and operational behaviour

- Use NetCDF4/HDF5, not uncompressed NetCDF3.
- Store scientific fields as `float32` with shuffle and moderate compression
  (for example DEFLATE level 4).
- Select chunks that support streaming conversion and time-slice validation.
- Never construct the complete winter dataset eagerly in memory.
- Write incrementally to a temporary `.partial.nc` file.
- Validate the completed partial file before atomically renaming it to the final
  output path.
- On restart, either validate and resume a partial file or remove/rebuild it
  explicitly; never silently append duplicate times.
- Refuse to overwrite a completed output unless an explicit overwrite option is
  supplied.

## Architecture decision gate for Elias

Recommended design: add a separate `icon2openamundsen` module and CLI entry point
inside the `icon2wrf` package. Reuse low-level download, GRIB and grid utilities,
but give the openAMUNDSEN path its own extraction, derivation, validation and
writer components.

Alternative: add an `openamundsen` profile to the existing orchestrator. If Elias
chooses this, the implementation must keep the WRF and openAMUNDSEN paths clearly
separated and preserve existing WRF behaviour and CLI defaults.

Do not implement the converter as a postprocessor for the current broad
`--netcdf` output: the required near-surface level fields and time semantics may
already have been lost, and the intermediate files are unnecessarily large.

Suggested non-interactive interface, adaptable to the chosen architecture:

```text
icon2openamundsen \
  --config config/openamundsen_oetztal.toml \
  --start <UTC timestamp> \
  --end <UTC timestamp> \
  --run-strategy <freshest|longest> \
  --output <path.nc>
```

The Oetztal grid configuration should be version controlled. Credentials, raw
GRIB files and generated NetCDF outputs must remain ignored.

## Downstream study-project adapter

The adapter is Franz's responsibility and does not belong in the first
`icon2wrf` implementation.

It will:

1. Open the canonical winter NetCDF.
2. Validate the seven-field contract, units, time bounds and CRS.
3. Select valid 500 m cells needed for the openAMUNDSEN domain.
4. Flatten those cells into a `station` dimension with stable IDs.
5. Create an xarray dataset with dimensions `(time, station)` containing scalar
   station metadata `lon`, `lat`, `alt`, `station_name` and the five forcing
   variables.
6. Exclude `snow_depth` and `swe` from meteorological forcing while retaining the
   canonical file for independent reference comparisons.
7. Validate the point dataset with
   `openamundsen.forcing.is_valid_point_dataset`.
8. Set `input_data.meteo.format: memory` and pass the dataset to
   `OpenAmundsen.initialize(meteo=...)`.

openAMUNDSEN then performs its standard interpolation from the 500 m pseudo-
stations to the model grid. This workflow does not change openAMUNDSEN source
code and does not introduce data assimilation.

## Testing requirements

Add a proper `pytest` test suite and CI coverage for the converter. Tests should
use small synthetic fixtures plus at least one controlled real ICON sample when
licensing and repository size allow it.

### Unit tests

- GRIB parameter and level selection
- Exact unit normalization
- U/V-to-wind-speed conversion
- Direct RH and, if implemented, derived RH
- Direct-plus-diffuse shortwave calculation
- Precipitation deaccumulation within a forecast cycle
- Radiation de-averaging within a forecast cycle
- Forecast-cycle reset handling
- Physical-range validation and numerical tolerances
- Output-variable whitelist

### Spatial tests

- EPSG:32632 grid has exactly 100 columns and 150 rows
- Cell centres and bounds match the approved rectangle
- `lon` and `lat` correctly transform from `x` and `y`
- Variable-specific remapping methods are applied
- Missing source coverage is not silently filled

### Time-series tests

- One monotonic output time per expected native timestep
- No duplicates at forecast-cycle boundaries
- Correct `time_bounds`
- Correct interval precipitation and mean shortwave radiation
- Configurable start and end selection
- Sparse snow-reference timestamps accepted
- Forcing gaps rejected

### openAMUNDSEN compatibility test

Add openAMUNDSEN v1.1.6 as a test-only dependency. Convert a small fixture,
reshape one or more valid grid cells into the planned `(time, station)` memory
dataset and require:

```python
openamundsen.forcing.is_valid_point_dataset(dataset, dates=expected_dates)
```

to return `True`. Also verify the exact five forcing variables, station metadata,
units and measurement-height assumptions.

### Integration and regression tests

- Convert the received ICON sample and compare all seven outputs with independently
  calculated expected values within remapping tolerances.
- Confirm `snow_depth` and `swe` preserve the source state semantics.
- Confirm no forbidden scientific variables exist in the final file.
- Confirm a missing required source field stops conversion with a precise error.
- Confirm the existing WRF conversion workflow and default behaviour remain
  unchanged.

## Acceptance criteria

The converter is complete when:

1. A configured native-resolution winter range produces one valid compressed
   NetCDF over the approved Oetztal grid.
2. The file contains exactly the seven scientific fields and required support
   metadata.
3. All five forcing fields use the exact openAMUNDSEN names and units.
4. Forecast cycles, precipitation intervals and radiation averaging intervals
   are explicit and reproducible.
5. Missing forcing data cause a hard failure rather than silent filling.
6. Snow depth and SWE are clearly marked as optional reference fields.
7. A converted subset passes the openAMUNDSEN v1.1.6 point-dataset validator.
8. Existing WRF functionality remains unchanged.
9. The README documents the new command/profile, required source fields, output
   schema, scientific limitations and an example invocation.

## Ownership and next steps

### Elias and his Claude session

- Inventory the raw ICON near-surface fields and resolve the humidity decision
  gate.
- Choose the internal architecture while preserving the output contract.
- Confirm the production forecast-stitching strategy while implementing explicit
  support and provenance.
- Implement extraction, time conversion, remapping, validation, streaming output,
  tests and documentation in `icon2wrf`.

### Franz

- Review the field mapping and sample output against openAMUNDSEN v1.1.6.
- Implement the study-project memory-input adapter after the canonical converter
  output is stable.
- Configure and run the conventional station-forced and ICON-forced winter
  simulations.
- Compare forcing fields and simulated snow states; use ICON snow depth and SWE
  only as independent references.

## Out of scope for the first converter

- WRF or WRF-LES output conversion
- Data assimilation, nudging or snow-state initialization
- Changes to openAMUNDSEN source code
- Soil-temperature or soil-moisture initialization
- Longwave-radiation export
- Pressure export in the final handoff
- Arbitrary temporal upsampling
- Full TEAMx-domain output
- Automatic filling of missing forcing data
