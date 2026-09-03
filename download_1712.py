#!/usr/bin/env python
# Non-interactive downloader for ONE ICON run: 20250717_12, leads 0..35
# (valid 2025-07-17 12:00 -> 2025-07-19 00:00). Reads [ftp] url/username/password
# from config/credentials.toml (add `password = "..."` there); falls back to
# getpass if run in a terminal. Saves input/<valid>_ilf3f<DD><HH>0000 (gunzipped),
# same naming as src/icon2wrf/download_ftp.py, skipping files already present.
import ftplib, gzip, shutil, sys, os
from pathlib import Path
from datetime import datetime, timedelta
try: import tomllib
except ImportError: import tomli as tomllib
os.chdir(os.path.dirname(os.path.abspath(__file__)))
creds={}
for f in ("config/config.toml","config/credentials.toml"):
    if os.path.exists(f):
        with open(f,"rb") as fh: creds.update(tomllib.load(fh).get("ftp",{}))
url=creds.get("url"); user=creds.get("username"); pw=creds.get("password")
if not pw and os.environ.get("FTP_PASSWORD"): pw=os.environ["FTP_PASSWORD"]
if not pw and os.path.exists(".ftp_pass"): pw=open(".ftp_pass").read().strip()
if not pw:
    if sys.stdin.isatty():
        import getpass; pw=getpass.getpass("FTP password for %s@%s: "%(user,url))
    else:
        print("[ERROR] no password in config/credentials.toml ([ftp] password = \"...\") and no tty"); sys.exit(2)
RUN="20250717_12"; run_dt=datetime(2025,7,17,12); MAXLEAD=36
ftp=ftplib.FTP(url,timeout=60); ftp.login(user,pw)
print("logged in to",url)
items=[]; ftp.dir(items.append)
dirs=[l.split()[-1] for l in items if l.startswith('d')]
if RUN not in dirs:
    print("[ERROR] run dir %s not on FTP. Available July dirs:"%RUN,[d for d in dirs if d.startswith("202507")]); sys.exit(3)
ftp.cwd("/"+RUN)
inp=Path("input"); inp.mkdir(exist_ok=True)
ok=fail=0
for lead in range(0,MAXLEAD):
    valid=run_dt+timedelta(hours=lead)
    gz="ilf3f%02d%02d0000.gz"%(lead//24,lead%24)
    out=inp/("%s_%s"%(valid.strftime("%Y%m%d%H"),gz[:-3]))
    if out.exists(): print("skip",out.name); ok+=1; continue
    tmp=Path(str(out)+".gz.tmp")
    try:
        with open(tmp,"wb") as fh: ftp.retrbinary("RETR "+gz, fh.write)
        with gzip.open(tmp,"rb") as fi, open(str(out)+".tmp","wb") as fo: shutil.copyfileobj(fi,fo)
        Path(str(out)+".tmp").rename(out); tmp.unlink()
        print("got ",out.name); ok+=1
    except Exception as e:
        print("[FAIL]",gz,e); fail+=1
        for p in (tmp,Path(str(out)+".tmp")):
            if p.exists(): p.unlink()
ftp.quit()
print("done: %d ok, %d failed"%(ok,fail)); sys.exit(0 if fail==0 else 4)
