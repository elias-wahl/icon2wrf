#!/bin/bash
# Hourly ICON surface forcing for an offline land-surface model (HRLDAS / Noah-MP spin-up) in ONE file.
#
#   ./run_surface_series.sh START END [OUT.nc] [JOBS] [WRFINPUT]
#   ./run_surface_series.sh 2025061500 2025071800                       # -> output/icon_surface_2025061500_2025071800.nc
#   ./run_surface_series.sh 2025071712 2025071715 /tmp/test.nc 1        # 4-hour smoke test
#
# Streams each hour from the ACINN FTP (download -> extract the surface fields -> regrid -> delete the
# 900 MB raw file), so the disk footprint is one raw file per job. JOBS (default 4, max 8 = the FTP's
# per-IP connection cap) splits the range into equal chunks that run in parallel; the chunks are
# merged with `cdo mergetime`. WRFINPUT (5th arg, or $WRFINPUT, default: the production domain's
# wrfinput below) confines the output to that WRF mass grid (south_north x west_east, HRLDAS-ready);
# a CDO grid description .txt works too (config/wrf_grid_wrfinput_d01_innval_pbl3d_X16b.txt is the
# production grid, committed, so no wrfinput is needed on another machine); "none" = full product grid.
# Missing hours are filled ONCE over the merged series ($FILL = auto|linear|diurnal, default auto; the chunks run
# with --no-fill so gaps at chunk edges are bracketed too). Disk: ~26 MB per hour for the compressed pieces
# during the run and about the same for the result (a 3-month series is ~65 GB) plus a ~12 GB merge spike per job.
# Fields, stitching rule, units, fill methods: src/icon2wrf/surface_series.py.
# Needs: `module load cdo`, the `icon` conda env, config/credentials.toml, and the FTP password in
# .ftp_pass (git-ignored). Run from the icon2wrf root, on the login node or inside a SLURM job.
set -u
cd "$(dirname "$0")"
START=${1:?START YYYYMMDDHH}; END=${2:?END YYYYMMDDHH}
OUT=${3:-output/icon_surface_${START}_${END}.nc}; JOBS=${4:-4}
WRFINPUT=${5:-${WRFINPUT:-/gpfs/data/fs72996/ewahl/branko_runs/innval_pbl3d_X16b/wrfinput_d01}}
GRIDARG=()
if [ "$WRFINPUT" != "none" ]; then
    [ -f "$WRFINPUT" ] || { echo "grid source not found: $WRFINPUT  (pass a wrfinput_d01, a CDO grid description .txt such as config/wrf_grid_wrfinput_d01_innval_pbl3d_X16b.txt, or 'none')"; exit 1; }
    case "$WRFINPUT" in *.txt) GRIDARG=(--target-grid "$WRFINPUT");; *) GRIDARG=(--wrf-grid "$WRFINPUT");; esac
fi
[ "$JOBS" -gt 8 ] && JOBS=8

command -v cdo >/dev/null 2>&1 || module load cdo >/dev/null 2>&1 || true   # a cdo already on PATH (e.g. the conda env's) wins over the module
if [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then source "$HOME/miniconda3/etc/profile.d/conda.sh"; conda activate icon; fi
[ -f .ftp_pass ] && export FTP_PASSWORD="$(cat .ftp_pass)"
: "${FTP_PASSWORD:?put the FTP password in .ftp_pass or export FTP_PASSWORD}"
command -v cdo >/dev/null || { echo "cdo not found (module load cdo)"; exit 1; }
mkdir -p "$(dirname "$OUT")" logs

# split [START, END] into JOBS chunks of whole hours (python for the date arithmetic)
mapfile -t CHUNKS < <(python - "$START" "$END" "$JOBS" <<'EOF'
import sys
from datetime import datetime, timedelta
s, e, n = datetime.strptime(sys.argv[1], "%Y%m%d%H"), datetime.strptime(sys.argv[2], "%Y%m%d%H"), int(sys.argv[3])
hours = int((e - s).total_seconds() // 3600) + 1
n = max(1, min(n, hours))
per = -(-hours // n)
t = s
while t <= e:
    u = min(t + timedelta(hours=per - 1), e)
    print(f"{t:%Y%m%d%H} {u:%Y%m%d%H}")
    t = u + timedelta(hours=1)
EOF
)
FILL=${FILL:-auto}
echo "=== $START -> $END in ${#CHUNKS[@]} chunk(s) -> $OUT   grid: ${WRFINPUT}   fill: ${FILL}   ($(date))"
PIDS=(); PARTS=()
for c in "${CHUNKS[@]}"; do
    set -- $c
    part="${OUT%.nc}_part_$1_$2.nc"; PARTS+=("$part")
    python -m src.icon2wrf.surface_series --start "$1" --end "$2" --out "$part" --no-fill "${GRIDARG[@]}" > "logs/surface_series_$1_$2.log" 2>&1 &
    PIDS+=($!)
    sleep 2
done
FAIL=0
for i in "${!PIDS[@]}"; do wait "${PIDS[$i]}" || { echo "chunk ${CHUNKS[$i]} FAILED (see logs/)"; FAIL=1; }; done
[ "$FAIL" -eq 1 ] && { echo "keeping the finished parts in $(dirname "$OUT") for a rerun of the failed chunk(s)"; exit 1; }
if [ "${#PARTS[@]}" -eq 1 ]; then
    mv "${PARTS[0]}" "$OUT"
else
    cdo -s -O -f nc4 -z zip_4 mergetime "${PARTS[@]}" "$OUT" && rm -f "${PARTS[@]}"
fi
python -m src.icon2wrf.surface_series --fill-only "$OUT" --fill "$FILL"    # one pass over the whole series: chunk-edge gaps are bracketed here
python - "$OUT" <<'EOF'
import sys, xarray as xr
ds = xr.open_dataset(sys.argv[1])
print(f"{sys.argv[1]}: {len(ds.time)} hours {str(ds.time.values[0])[:13]} .. {str(ds.time.values[-1])[:13]}, filled: {ds.attrs.get('filled_hours')}, unfilled: {ds.attrs.get('unfilled_hours')}")
for v in ("T2D", "Q2D", "U2D", "PSFC", "SWDOWN", "LWDOWN", "RAINRATE", "TSOIL", "HSURF"):
    if v in ds: print(f"  {v:9s} min {float(ds[v].min()):10.4g} max {float(ds[v].max()):10.4g}")
EOF
echo "=== done $(date)"
