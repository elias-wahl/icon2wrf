#!/bin/bash
#SBATCH --job-name=icon2wrf_season
#SBATCH --partition=zen3_0512
#SBATCH --qos=zen3_0512                # Production QoS (devel QoS caps at 10 min wall time)
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --hint=nomultithread
#SBATCH --mem=0
#SBATCH --time=48:00:00
#SBATCH --mail-user=elias.wahl@uibk.ac.at
#SBATCH --mail-type=BEGIN,END,FAIL
#SBATCH --output=season_%j.out
#SBATCH --error=season_%j.err

# (Removed set -e because module commands can sometimes return non-zero exit codes)

# Load CDO module
module load cdo

# Initialize the user's personal Conda installation (bypassing the system module)
if [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
    source "$HOME/miniconda3/etc/profile.d/conda.sh"
elif [ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]; then
    source "$HOME/anaconda3/etc/profile.d/conda.sh"
else
    # Fallback to evaluating conda hook if it's already in the PATH via .bashrc
    source ~/.bashrc || true
    eval "$(conda shell.bash hook)"
fi

conda activate icon

# Re-enable strict error handling now that modules are loaded
set -e

# Define the season start and end dates (YYYYMMDDHH)
# Adjust these dates for your specific season!
START_DATE="2024121812" # Start in December
END_DATE="2025063000"   # End in June

echo "======================================================"
echo "Starting ICON to openAMUNDSEN season processing"
echo "Timeframe: $START_DATE -> $END_DATE"
echo "Cores: 8 (matches FTP server's 8-connection-per-IP cap)"
echo "======================================================"

mkdir -p logs

# Load FTP password from a secret file that is ignored by git
if [ -f ".ftp_pass" ]; then
    export FTP_PASSWORD=$(cat .ftp_pass)
else
    echo "Warning: .ftp_pass file not found! The script may fail to authenticate with the FTP server."
fi

# Run the Python orchestrator
# The orchestrator will internally split the timeframe and run it concurrently across the 16 jobs
python -m src.icon2wrf.orchestrator \
    --profile openamundsen \
    --start $START_DATE \
    --end $END_DATE \
    --run-strategy freshest \
    --jobs 8 \
    --ramdisk

echo "Season processing completed successfully!"
