#!/bin/bash

cd output || { echo "Run this script from the icon2wrf root directory."; exit 1; }

FILES=(
    "openamundsen_forcing_2025012401_2025012500.nc"
    "openamundsen_forcing_2025012501_2025012600.nc"
    "openamundsen_forcing_2025012601_2025012700.nc"
    "openamundsen_forcing_2025012701_2025012800.nc"
    "openamundsen_forcing_2025012801_2025012900.nc"
    "openamundsen_forcing_2025012901_2025013000.nc"
    "openamundsen_forcing_2025013001_2025013100.nc"
)

FINAL_OUTPUT="openamundsen_forcing_2025012401_2025013100.nc"

# Check if all chunks exist
echo "Checking for chunk files..."
for f in "${FILES[@]}"; do
    if [ ! -f "$f" ]; then
        echo "❌ Error: File $f is missing!"
        echo "Please wait until the final chunk is generated before running this script."
        exit 1
    else
        echo "✅ Found $f"
    fi
done

echo ""
echo "All chunk files found! Merging into $FINAL_OUTPUT..."
cdo -s -O mergetime "${FILES[@]}" "$FINAL_OUTPUT"

if [ $? -eq 0 ]; then
    echo "🎉 Successfully created $FINAL_OUTPUT!"
    # Optional: uncomment the loop below to clean up chunk files after a successful merge
    # for f in "${FILES[@]}"; do
    #     rm "$f"
    # done
else
    echo "❌ Error during cdo mergetime!"
    exit 1
fi
