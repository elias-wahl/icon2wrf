import argparse
import ftplib
import getpass
import os
import sys
import gzip
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

# Fallback for Python < 3.11
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        print("[ERROR] Please install tomli for Python < 3.11: pip install tomli")
        sys.exit(1)

from .download_ftp import (
    load_credentials,
    save_credentials,
    get_filename_for_offset,
    CREDENTIALS_FILE,
)


def extract_gz(gz_path, out_path):
    print(f"  Unzipping to {out_path}...")
    tmp_out_path = Path(str(out_path) + ".tmp")
    try:
        with gzip.open(gz_path, "rb") as f_in:
            with open(tmp_out_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        tmp_out_path.rename(out_path)
    finally:
        if tmp_out_path.exists():
            tmp_out_path.unlink()
        if Path(gz_path).exists():
            Path(gz_path).unlink()


def run_cdo_regrid(input_nc, output_file, source_grid, target_grid):
    """Runs CDO to regrid NetCDF."""
    cmd = [
        "cdo",
        "-f",
        "nc",
        f"-remapdis,{target_grid}",
        f"-setgrid,{source_grid}",
        str(input_nc),
        str(output_file),
    ]
    print(f"  Running: {' '.join(cmd)}")
    process = subprocess.Popen(
        cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    stdout, stderr = process.communicate()

    if process.returncode != 0:
        print(f"  [ERROR] CDO failed with exit code {process.returncode}")
        print(stderr)
        return False

    # Filter harmless warnings for cleaner output
    harmless = [
        "ECCODES ERROR",
        "grib_set_string",
        "gribapiDefParam",
        "cdfInqContents",
        "Parameter Database",
        "Changed zaxis type",
        "set_coordinates_varids",
    ]
    filtered_err = [
        line for line in stderr.splitlines() if not any(h in line for h in harmless)
    ]

    for line in filtered_err:
        if line.strip():
            print(f"  {line}")

    return True


# --- Placeholders ---
def filter_fields(input_nc, output_nc):
    """
    Placeholder: Use new script (to be written later) to filter the fields to only the ones necessary.
    """
    print(f"  [Placeholder] Filtering fields from {input_nc} to {output_nc}...")
    shutil.copy(input_nc, output_nc)
    return True


def filter_grid_points(input_nc, output_nc):
    """
    Placeholder: Filter grid points due to a set domain box.
    """
    print(f"  [Placeholder] Filtering grid points from {input_nc} to {output_nc}...")
    shutil.copy(input_nc, output_nc)
    return True


def transform_variables(input_nc, output_nc):
    """
    Placeholder: Transform/calculate the extracted variables to be openamundsen compatible.
    """
    print(f"  [Placeholder] Transforming variables from {input_nc} to {output_nc}...")
    shutil.copy(input_nc, output_nc)
    return True


# --------------------


def main():
    print("=== OpenAMUNDSEN Orchestrator ===")

    parser = argparse.ArgumentParser(
        description="ICON to OpenAMUNDSEN Forcing Orchestrator"
    )
    parser.add_argument("--source-grid", help="Path to custom source grid file")
    parser.add_argument("--target-grid", help="Path to custom target grid file")
    args = parser.parse_args()

    config_path = "config/config.toml"
    if not os.path.exists(config_path):
        print(f"[ERROR] Configuration file {config_path} not found.")
        sys.exit(1)

    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    paths = config.get("paths", {})
    source_grid = (
        args.source_grid
        if args.source_grid
        else paths.get("source_grid", "source_grid.txt")
    )
    target_grid = (
        args.target_grid
        if args.target_grid
        else paths.get("target_grid", "target_grid.txt")
    )

    # Ensure grid files exist
    if not os.path.exists(source_grid) or not os.path.exists(target_grid):
        print(f"[ERROR] Grid definitions not found: {source_grid} or {target_grid}")
        sys.exit(1)

    # 1. Setup Phase
    creds = load_credentials()
    default_url = creds.get("url", "")
    default_user = creds.get("username", "")

    url = input(f"FTP URL [{default_url}]: ").strip() or default_url
    username = input(f"Username [{default_user}]: ").strip() or default_user
    password = getpass.getpass("Password: ")

    if url != default_url or username != default_user:
        save_choice = (
            input(
                f"Would you like to save this URL and username to {CREDENTIALS_FILE}? (y/n): "
            )
            .strip()
            .lower()
        )
        if save_choice == "y":
            save_credentials(url, username)

    start_str = input("\nEnter start date/time (YYYYMMDDHH) e.g. 2024121812: ").strip()
    end_str = input("Enter end date/time (YYYYMMDDHH) e.g. 2024122000: ").strip()

    try:
        start_dt = datetime.strptime(start_str, "%Y%m%d%H")
        end_dt = datetime.strptime(end_str, "%Y%m%d%H")
    except ValueError:
        print("[ERROR] Invalid date format. Please use exactly YYYYMMDDHH.")
        sys.exit(1)

    print("\nThe FTP server runs simulations every 12h, and each covers 48h.")
    print("How would you like to build your time span?")
    print(
        "  1) Freshest Run: Always use the run that started closest to the target time step (stitches multiple runs, changes every 12h)."
    )
    print(
        "  2) Longest Run: Use the most recent run that covers the time step, and stick with it for up to 48h before switching."
    )
    strategy = input("Select strategy (1 or 2): ").strip()
    if strategy not in ["1", "2"]:
        print("[ERROR] Invalid strategy.")
        sys.exit(1)

    input_dir = Path("input_amundsen")
    output_dir = Path("output_amundsen")
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nConnecting to {url}...")
    try:
        ftp = ftplib.FTP(url)
        ftp.login(username, password)
    except Exception as e:
        print(f"[ERROR] Failed to connect or login: {e}")
        sys.exit(1)

    print("Connected! Fetching available runs...")
    items = []
    ftp.dir(items.append)

    available_runs = []
    for item in items:
        parts = item.split()
        if len(parts) > 0 and parts[0].startswith("d"):
            dir_name = parts[-1]
            try:
                run_dt = datetime.strptime(dir_name, "%Y%m%d_%H")
                available_runs.append((run_dt, dir_name))
            except ValueError:
                pass

    available_runs.sort()  # Oldest to newest

    if not available_runs:
        print("No valid run directories found on the FTP.")
        ftp.quit()
        sys.exit(1)

    interval_str = input("Enter interval in hours (default: 1): ").strip()
    try:
        interval_hours = int(interval_str) if interval_str else 1
        if interval_hours < 1:
            interval_hours = 1
    except ValueError:
        interval_hours = 1

    target_times = []
    curr = start_dt
    while curr <= end_dt:
        target_times.append(curr)
        curr += timedelta(hours=interval_hours)

    download_queue = []  # list of (dir_name, target_dt, filename)
    current_longest_run = None
    first_run_chosen = False

    for target in target_times:
        valid_runs = [
            r
            for r in available_runs
            if r[0] <= target and (target - r[0]).total_seconds() <= 48 * 3600
        ]

        if not valid_runs:
            print(f"[WARNING] No valid previous run covers {target}.")
            continue

        if strategy == "1":
            chosen_run_dt, dir_name = valid_runs[-1]
        else:
            if current_longest_run and current_longest_run in valid_runs:
                chosen_run_dt, dir_name = current_longest_run
            else:
                chosen_run_dt, dir_name = valid_runs[-1]
                current_longest_run = valid_runs[-1]

        if not first_run_chosen:
            init_file = get_filename_for_offset(0)
            download_queue.append((dir_name, chosen_run_dt, init_file))
            first_run_chosen = True

        offset_hours = int((target - chosen_run_dt).total_seconds() // 3600)
        gz_file = get_filename_for_offset(offset_hours)

        # Avoid duplicating the init file
        if not (
            offset_hours == 0
            and len(download_queue) > 0
            and download_queue[-1][2] == gz_file
        ):
            download_queue.append((dir_name, target, gz_file))

    if not download_queue:
        print("No valid files to process.")
        ftp.quit()
        return

    print(f"\n--- Planning to process {len(download_queue)} timesteps ---\n")

    # 2. Per-Time-Step Execution Loop
    for dir_name, target, gz_file in download_queue:
        target_str = target.strftime("%Y%m%d%H")
        local_gz_path = input_dir / f"{target_str}_{gz_file}"
        raw_nc_path = input_dir / f"{target_str}_{gz_file[:-3]}"

        filtered_nc_path = input_dir / f"{target_str}_filtered.nc"
        cropped_nc_path = input_dir / f"{target_str}_cropped.nc"
        regridded_nc_path = output_dir / f"{target_str}_regridded.nc"
        final_nc_path = output_dir / f"{target_str}_amundsen.nc"

        print(
            f"\n[{target_str}] Processing timestep (FTP directory: {dir_name}, file: {gz_file})"
        )

        # a) Download
        if not raw_nc_path.exists():
            print(f"  Downloading {gz_file} for {target_str}...")
            try:
                ftp.cwd("/" + dir_name)
            except Exception:
                try:
                    ftp.cwd(dir_name)
                except Exception as e:
                    print(f"  [ERROR] Cannot enter {dir_name}: {e}")
                    continue

            tmp_gz_path = local_gz_path.with_suffix(".gz.tmp")
            try:
                with open(tmp_gz_path, "wb") as f:
                    ftp.retrbinary(f"RETR {gz_file}", f.write)
                tmp_gz_path.rename(local_gz_path)
                extract_gz(local_gz_path, raw_nc_path)
            except Exception as e:
                print(f"  [ERROR] Failed downloading {gz_file}: {e}")
                if tmp_gz_path.exists():
                    tmp_gz_path.unlink()
                if local_gz_path.exists():
                    local_gz_path.unlink()
                continue
            finally:
                ftp.cwd("/")
        else:
            print(f"  Skipping download, {raw_nc_path.name} already exists.")

        # b) Filter fields
        if not filtered_nc_path.exists():
            filter_fields(raw_nc_path, filtered_nc_path)
        else:
            print(f"  Skipping field filter, {filtered_nc_path.name} already exists.")

        # c1) Filter grid points
        if not cropped_nc_path.exists():
            filter_grid_points(filtered_nc_path, cropped_nc_path)
        else:
            print(f"  Skipping grid filter, {cropped_nc_path.name} already exists.")

        # c2) Regrid
        if not regridded_nc_path.exists():
            print(f"  Regridding to target grid...")
            run_cdo_regrid(cropped_nc_path, regridded_nc_path, source_grid, target_grid)
        else:
            print(f"  Skipping regridding, {regridded_nc_path.name} already exists.")

        # d) Transform Variables
        if not final_nc_path.exists():
            transform_variables(regridded_nc_path, final_nc_path)
        else:
            print(f"  Skipping transform, {final_nc_path.name} already exists.")

    print("\nProcessing pipeline completed successfully!")
    ftp.quit()


if __name__ == "__main__":
    main()
