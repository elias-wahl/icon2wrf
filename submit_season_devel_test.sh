#!/bin/bash
#SBATCH --job-name=icon2wrf_devel_test
#SBATCH --partition=zen3_0512
#SBATCH --qos=zen3_0512_devel          # Devel QoS: hard-capped at 10 min wall time
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --hint=nomultithread
#SBATCH --mem=0
#SBATCH --time=00:10:00
#SBATCH --mail-user=elias.wahl@uibk.ac.at
#SBATCH --mail-type=BEGIN,END,FAIL
#SBATCH --output=devel_test_%j.out
#SBATCH --error=devel_test_%j.err

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

# Same full season range as the production run -- this is a smoke test of the
# real config (16 workers, ramdisk) capped by devel QoS at 10 min wall time,
# not a functional end-to-end test (it will be killed by the time limit).
START_DATE="2024121812" # Start in December
END_DATE="2025063000"   # End in June

echo "======================================================"
echo "DEVEL SMOKE TEST: ICON to openAMUNDSEN season processing"
echo "Timeframe: $START_DATE -> $END_DATE"
echo "Cores: 16 (devel QoS, 10 min cap)"
echo "======================================================"

mkdir -p logs

# Load FTP password from a secret file that is ignored by git
if [ -f ".ftp_pass" ]; then
    export FTP_PASSWORD=$(cat .ftp_pass)
else
    echo "Warning: .ftp_pass file not found! The script may fail to authenticate with the FTP server."
fi

# Run the Python orchestrator
python -m src.icon2wrf.orchestrator \
    --profile openamundsen \
    --start $START_DATE \
    --end $END_DATE \
    --run-strategy freshest \
    --jobs 16 \
    --ramdisk

echo "Devel smoke test finished without crashing (likely killed by time limit, which is expected)."
