#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e

echo "Starting ICON-to-WRF Batch Regridder..."

echo ""
echo "Select Grid Specification:"
echo "1) Use standard grid specification for the TEAMx ICON 500m runs"
echo "2) Calculate a new source grid from a source file"
read -p "Enter your choice (1 or 2): " grid_choice

if [ "$grid_choice" == "2" ]; then
    read -p "Enter the path to the source file (e.g., input/domain2_DOM02.nc): " source_file
    if [ ! -f "$source_file" ]; then
        echo "Error: Source file not found!"
        exit 1
    fi
    echo "Running make_grid.py to generate temporary grids..."
    python -m src.icon2wrf.make_grid "$source_file"
    echo ""
    python -m src.icon2wrf.orchestrator --source-grid temp_source_grid.txt --target-grid temp_target_grid.txt --skip-file "$source_file" "$@"
    
    # Clean up temporary grid files
    rm -f temp_source_grid.txt temp_target_grid.txt
else
    echo ""
    python -m src.icon2wrf.orchestrator "$@"
fi
