#!/bin/bash
# Re-enable strict error handling
set -e

# Load CDO module if not already loaded
module load cdo || true

# Initialize conda if it's not already activated
if ! command -v conda &> /dev/null; then
    if [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
        source "$HOME/miniconda3/etc/profile.d/conda.sh"
    elif [ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]; then
        source "$HOME/anaconda3/etc/profile.d/conda.sh"
    else
        source ~/.bashrc || true
        eval "$(conda shell.bash hook)"
    fi
fi

# Ensure the environment is activated
conda activate icon || true

# Load FTP password
if [ -f ".ftp_pass" ]; then
    export FTP_PASSWORD=$(cat .ftp_pass)
else
    echo "Warning: .ftp_pass file not found!"
fi

echo "======================================================"
echo "Starting 1-Week Interpolation Test (Jan 10 - Jan 17)"
echo "This week spans the 14-hour gap on Jan 13."
echo "Running with 8 cores for interactive mode."
echo "======================================================"

python -m src.icon2wrf.orchestrator \
    --profile openamundsen \
    --start 2025011000 \
    --end 2025011700 \
    --run-strategy freshest \
    --spinup 9 \
    --jobs 8 \
    --output test_interpolation_week.nc

echo "Test complete! The output file is: test_interpolation_week.nc"
