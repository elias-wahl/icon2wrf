#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e

python -m src.icon2wrf.download_ftp
