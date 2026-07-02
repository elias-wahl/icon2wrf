import ftplib
import getpass
import os
import sys
import gzip
import shutil
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

CREDENTIALS_FILE = "config/credentials.toml"

def load_credentials():
    creds = {}
    if os.path.exists("config/config.toml"):
        with open("config/config.toml", "rb") as f:
            creds.update(tomllib.load(f).get("ftp", {}))
            
    if os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE, "rb") as f:
            creds.update(tomllib.load(f).get("ftp", {}))
            
    return creds

def save_credentials(url, username):
    lines = []
    if os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE, "r") as f:
            lines = f.readlines()
            
    ftp_idx = -1
    for i, line in enumerate(lines):
        if line.strip() == "[ftp]":
            ftp_idx = i
            break
            
    if ftp_idx == -1:
        lines.append("[ftp]\n")
        lines.append(f'url = "{url}"\n')
        lines.append(f'username = "{username}"\n')
    else:
        out_lines = []
        in_ftp = False
        url_written = False
        user_written = False
        for line in lines:
            if line.strip().startswith("["):
                if in_ftp:
                    if not url_written: out_lines.append(f'url = "{url}"\n')
                    if not user_written: out_lines.append(f'username = "{username}"\n')
                    in_ftp = False
                if line.strip() == "[ftp]":
                    in_ftp = True
                out_lines.append(line)
            elif in_ftp and line.strip().startswith("url"):
                out_lines.append(f'url = "{url}"\n')
                url_written = True
            elif in_ftp and line.strip().startswith("username"):
                out_lines.append(f'username = "{username}"\n')
                user_written = True
            else:
                out_lines.append(line)
                
        if in_ftp:
            if not url_written: out_lines.append(f'url = "{url}"\n')
            if not user_written: out_lines.append(f'username = "{username}"\n')
            
        lines = out_lines

    with open(CREDENTIALS_FILE, "w") as f:
        f.writelines(lines)
    print(f"Credentials saved to {CREDENTIALS_FILE} (password omitted).")

def extract_gz(gz_path, out_path):
    print(f"  Unzipping to {out_path}...")
    tmp_out_path = Path(str(out_path) + ".tmp")
    try:
        with gzip.open(gz_path, 'rb') as f_in:
            with open(tmp_out_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        tmp_out_path.rename(out_path)
    finally:
        if tmp_out_path.exists():
            tmp_out_path.unlink()
        if Path(gz_path).exists():
            Path(gz_path).unlink()

def get_filename_for_offset(offset_hours):
    days = offset_hours // 24
    hours = offset_hours % 24
    return f"ilf3f{days:02d}{hours:02d}0000.gz"

def main():
    print("=== ICON FTP Downloader ===")
    creds = load_credentials()
    
    default_url = creds.get("url", "")
    default_user = creds.get("username", "")
    
    url = input(f"FTP URL [{default_url}]: ").strip() or default_url
    username = input(f"Username [{default_user}]: ").strip() or default_user
    password = getpass.getpass("Password: ")
    
    if url != default_url or username != default_user:
        save_choice = input(f"Would you like to save this URL and username to {CREDENTIALS_FILE}? (y/n): ").strip().lower()
        if save_choice == 'y':
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
    print("  1) Freshest Run: Always use the run that started closest to the target time step (stitches multiple runs, changes every 12h).")
    print("  2) Longest Run: Use the most recent run that covers the time step, and stick with it for up to 48h before switching.")
    strategy = input("Select strategy (1 or 2): ").strip()
    if strategy not in ["1", "2"]:
        print("[ERROR] Invalid strategy.")
        sys.exit(1)

    input_dir = Path("input")
    input_dir.mkdir(parents=True, exist_ok=True)
    
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
        if len(parts) > 0 and parts[0].startswith('d'):
            dir_name = parts[-1]
            try:
                run_dt = datetime.strptime(dir_name, "%Y%m%d_%H")
                available_runs.append((run_dt, dir_name))
            except ValueError:
                pass
                
    available_runs.sort() # Oldest to newest
    
    if not available_runs:
        print("No valid run directories found on the FTP.")
        ftp.quit()
        sys.exit(1)

    interval_str = input("Enter download interval in hours (default: 1): ").strip()
    try:
        interval_hours = int(interval_str) if interval_str else 1
        if interval_hours < 1: interval_hours = 1
    except ValueError:
        interval_hours = 1

    # Build the required list of target datetimes
    target_times = []
    curr = start_dt
    while curr <= end_dt:
        target_times.append(curr)
        curr += timedelta(hours=interval_hours)
        
    # Map target times to (run_directory, filename)
    download_queue = {} # dir_name -> list of (target_dt, filename)
    
    current_longest_run = None
    
    for target in target_times:
        # Strictly enforce offset > 0 to avoid +00h initialization files
        valid_runs = [r for r in available_runs if r[0] < target and (target - r[0]).total_seconds() <= 48 * 3600]
        
        if not valid_runs:
            print(f"[WARNING] No valid previous run covers {target} (skipping to avoid +00h initialization files).")
            continue
            
        if strategy == "1":
            # Freshest: latest valid run
            chosen_run_dt, dir_name = valid_runs[-1]
        else:
            # Longest: if current_longest_run is valid, use it. Else pick the most recent valid run.
            if current_longest_run and current_longest_run in valid_runs:
                chosen_run_dt, dir_name = current_longest_run
            else:
                chosen_run_dt, dir_name = valid_runs[-1]
                current_longest_run = valid_runs[-1]
                
        offset_hours = int((target - chosen_run_dt).total_seconds() // 3600)
        gz_file = get_filename_for_offset(offset_hours)
        
        if dir_name not in download_queue:
            download_queue[dir_name] = []
        download_queue[dir_name].append((target, gz_file))

    if not download_queue:
        print("No files to download.")
        ftp.quit()
        return

    print("\n--- Download Plan ---")
    for d, files in download_queue.items():
        print(f"From {d}: {len(files)} files (e.g. {files[0][1]} to {files[-1][1]})")
    print("---------------------\n")
    
    for dir_name, files in download_queue.items():
        print(f"Entering {dir_name}...")
        try:
            ftp.cwd("/" + dir_name)
        except Exception:
            try:
                ftp.cwd(dir_name)
            except Exception as e:
                print(f"[ERROR] Cannot enter {dir_name}: {e}")
                continue
                
        for target, gz_file in files:
            target_str = target.strftime("%Y%m%d%H")
            local_gz_path = input_dir / f"{target_str}_{gz_file}"
            local_nc_path = input_dir / f"{target_str}_{gz_file[:-3]}"
            
            if local_nc_path.exists():
                print(f"Skipping {target_str} (already downloaded).")
                continue
                
            print(f"Downloading {gz_file} for {target_str}...")
            tmp_gz_path = local_gz_path.with_suffix(".gz.tmp")
            try:
                with open(tmp_gz_path, 'wb') as f:
                    ftp.retrbinary(f"RETR {gz_file}", f.write)
                tmp_gz_path.rename(local_gz_path)
                extract_gz(local_gz_path, local_nc_path)
            except Exception as e:
                print(f"[ERROR] Failed downloading {gz_file}: {e}")
                if tmp_gz_path.exists():
                    tmp_gz_path.unlink()
                if local_gz_path.exists():
                    local_gz_path.unlink()
                if local_nc_path.exists():
                    local_nc_path.unlink()
        
        ftp.cwd("/")
        
    print("\nAll downloads complete!")
    ftp.quit()

if __name__ == "__main__":
    main()
