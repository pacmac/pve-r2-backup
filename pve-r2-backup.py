#!/usr/bin/env python3
# ==============================================================================
# File: pve-r2-backup.py
# Description: Copy Proxmox vzdump backups to Cloudflare R2 using rclone,
#              and pull them back again.
# Project: https://github.com/pacmac/pve-r2-backup   License: MIT
#
# Usage:
#   pve-r2-backup install            install rclone, the vzdump hook, timer, env template
#   pve-r2-backup config             set R2 credentials and this host's path, test access
#   pve-r2-backup sync [MODE] [-n]   upload backups (--latest | --all | --since DAYS, default --since 2)
#   pve-r2-backup restore [VMID [VERSION]] [options]
#                                    pick a guest and a version from R2, download and/or restore
#   pve-r2-backup hostconfig [list | get [VERSION]]
#                                    encrypted backup of this host's own configuration
#                                    (/etc incl. /etc/pve, config.db, inventory); list or pull one back
#   pve-r2-backup status             show config, last result, hook, timer and what is in R2
#   pve-r2-backup weekly             catch-up sync + summary email (run by the timer)
#   pve-r2-backup notify-test        send a test email through the Proxmox notification system
#   pve-r2-backup uninstall          remove hook and timer (keeps config + R2 data)
#
# Layout in R2:  <bucket>/<path>/<class>/<vzdump files>
#   e.g. my-bucket/backups/pve/site1/weekly/  (bucket = R2_BUCKET, folder = R2_PATH, unique per host)
#   <bucket>/<path>/host/<hostname>-config-<date>.tar.zst.gpg   host configuration (always encrypted)
#   class = weekly | monthly | other, derived from the backup job schedule that
#   includes the guest. Use one R2 lifecycle rule per <path>/<class>/ prefix.
#   NOTE: R2 lifecycle age counts from UPLOAD time, not from the backup date.
#
# Uploads use 'rclone copy' (never 'sync'), so local pruning or a compromised
# host cannot delete what is already in R2.
#
# Reports are mailed to root, which Proxmox forwards through its notification
# targets (Datacenter > Notifications). Failed runs always mail; R2_NOTIFY=always
# also mails successes. The weekly timer mails a summary and flags STALE when
# there was no successful upload for R2_STALE_DAYS (default 8).
#
# Python 3.11+ standard library only (ships with Proxmox VE 8 and 9).
# ==============================================================================

import argparse
import concurrent.futures
import fcntl
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

VERSION = "2.1.0"

SELF_INSTALLED = Path("/usr/local/sbin/pve-r2-backup")
# credentials (r2.env) and generated rclone.conf live in root's home, mode 700/600
CONF_DIR = Path("/root/.pve-r2")
ENV_FILE = CONF_DIR / "r2.env"
RCLONE_CONF = CONF_DIR / "rclone.conf"
LOG_FILE = Path("/var/log/pve-r2-backup.log")
LOCK_FILE = Path("/run/pve-r2-backup.lock")
STATE_FILE = Path("/var/lib/pve-r2-backup/state")
VZDUMP_CONF = Path("/etc/vzdump.conf")
UNIT_NAME = "pve-r2-backup"
CLASSES = ("weekly", "monthly", "other")

# rclone tuning for a fast (>1 Gbit) uplink; R2 allows max 10000 parts per object
UPLOAD_OPTS = ["--s3-chunk-size", "64M", "--s3-upload-concurrency", "4", "--transfers", "2",
               "--retries", "5", "--low-level-retries", "10", "--log-level", "INFO",
               "--stats", "1m", "--stats-one-line", "--stats-log-level", "NOTICE"]
# restores: many parallel streams per file, measured ~100 MiB/s vs ~12 MiB/s single stream
DOWNLOAD_OPTS = ["--multi-thread-streams", "16", "--multi-thread-chunk-size", "64M",
                 "--multi-thread-cutoff", "64M", "--retries", "5"]

# vzdump-<type>-<vmid>-<YYYY_MM_DD-HH_MM_SS>.<ext>
VZDUMP_RE = re.compile(r"^vzdump-(qemu|lxc)-(\d+)-(\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2})\.(.+)$")
SIDECAR_EXTS = (".log", ".notes", ".protected")
# job schedules that run monthly or less often
RARE_SCHEDULE_RE = re.compile(r"(monthly|yearly|annually|quarterly|semiannually|[0-9*]+-[0-9*]+-[0-9])")
# host configuration backup: paths relative to /, plus R2_HOSTCONFIG_EXTRA
HOST_DIR = "host"
HOSTCONFIG_PATHS = ["etc", "root/.pve-r2", "root/.ssh", "root/.bashrc", "root/.profile",
                    "usr/local", "var/spool/cron/crontabs", "var/lib/vz/snippets"]
HOSTCONFIG_RE = re.compile(r"^(.+)-config-(\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2})\.tar\.zst\.gpg$")
# rebuildable clutter skipped inside every included folder
HOSTCONFIG_EXCLUDES = [".git", "venv", ".venv", "node_modules", "__pycache__", ".cache"]
PVE_DB = Path("/var/lib/pve-cluster/config.db")
# written to inventory.txt: what is needed to rebuild disks, pools and network by hand
INVENTORY_CMDS = ["pveversion -v", "hostnamectl", "ip -br link", "ip -br addr", "ip route",
                  "lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINTS", "blkid", "pvs", "vgs", "lvs -a",
                  "zpool status", "zpool list -v", "zfs list -o name,used,avail,refer,mountpoint",
                  "pvesm status", "qm list", "pct list", "df -h", "dpkg --get-selections"]
# days without a host config backup before the weekly summary warns
HOSTCONFIG_STALE_DAYS = 3

# rclone INFO line for a finished file; large files are "Multi-thread Copied (new)"
COPIED_RE = re.compile(r"INFO  : (.+?): (?:Multi-thread )?Copied \((?:new|replaced existing)\)")

ENV_KEYS = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PATH",
            "R2_STORAGE", "R2_NOTIFY", "R2_STALE_DAYS", "R2_CRYPT_PASSWORD", "R2_CRYPT_SALT",
            "R2_HOSTCONFIG_PASS", "R2_HOSTCONFIG_EXTRA"]
ENV_COMMENTS = {
    "R2_ACCOUNT_ID": "Cloudflare R2 settings for pve-r2-backup (keep chmod 600)",
    "R2_BUCKET": "Full R2 path for THIS host = <R2_BUCKET>/<R2_PATH>, unique per host",
    "R2_STORAGE": "Proxmox storage whose dump/ directory is uploaded",
    "R2_NOTIFY": "Mail successful runs too (always) or only failures (failures); weekly summary is always sent",
    "R2_STALE_DAYS": "Weekly summary flags STALE when the last successful upload is older than this",
    "R2_CRYPT_PASSWORD": "Optional client-side encryption. Keep these OUTSIDE this host too,\n"
                         "# without them the R2 copies cannot be restored.",
    "R2_HOSTCONFIG_PASS": "Passphrase for the encrypted host config backups (hostconfig). Keep a copy\n"
                          "# OUTSIDE this host (password manager): it is needed to rebuild a lost host.",
    "R2_HOSTCONFIG_EXTRA": "Extra folders for the host config backup, comma separated (e.g. /usr/share/scripts,/opt/tools)",
}


class Fatal(Exception):
    """An error that ends the command with a message (and a report during sync)."""


# ------------------------------------------------------------------------------
# small helpers
# ------------------------------------------------------------------------------

def _stderr_is_log():
    """True when stderr already goes into LOG_FILE (background run started by the hook)."""
    try:
        a, b = os.fstat(2), LOG_FILE.stat()
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    except OSError:
        return False


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    if not _stderr_is_log():
        print(line, file=sys.stderr)
    try:
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run(cmd, check=True, capture=True, input=None):
    """Run a command; return stdout (str). Raises Fatal on failure when check=True."""
    try:
        p = subprocess.run(cmd, input=input, text=True,
                           stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.PIPE if capture else None)
    except FileNotFoundError:
        raise Fatal(f"command not found: {cmd[0]}")
    if check and p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        raise Fatal(f"{' '.join(cmd[:3])} failed: {err[-1] if err else 'exit ' + str(p.returncode)}")
    return p.stdout or ""


def pvesh(path, *args):
    return json.loads(run(["pvesh", "get", path, "--output-format", "json", *args]) or "null")


def human(n):
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def age_text(epoch):
    if not epoch:
        return "never"
    s = int(time.time()) - int(epoch)
    return f"{datetime.fromtimestamp(int(epoch)):%Y-%m-%d %H:%M:%S} ({s // 86400}d {s % 86400 // 3600}h ago)"


def ts_text(ts):
    """2026_10_18-01_15_00 -> 2026-10-18 01:15"""
    d, t = ts.split("-")
    return f"{d.replace('_', '-')} {t.replace('_', ':')[:5]}"


def need_root():
    if os.geteuid() != 0:
        raise Fatal("Run as root")


def ask(text, default=None, regex=None, keep_spaces=False):
    """Visible prompt; Enter keeps the default. Optional regex is checked, the prompt
    repeats on mismatch (catches double pastes). Input is never hidden on purpose.
    Whitespace is removed (pasted keys often carry some) unless keep_spaces."""
    while True:
        shown = f" [{default}]" if default else ""
        try:
            value = input(f"{text}{shown}: ")
        except EOFError:
            value = ""
        value = value.strip() if keep_spaces else "".join(value.split())
        if not value:
            value = default or ""
        if regex and value and not re.fullmatch(regex, value):
            print(f"  -> invalid ({len(value)} chars) - paste it once, then press Enter", file=sys.stderr)
            if value == default:
                default = None      # a bad saved value must be re-entered
            continue
        return value


def ask_yes(text, default=False):
    try:
        ans = input(f"{text} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    except EOFError:
        ans = ""
    return default if not ans else ans.startswith("y")


def choose(title, rows, header):
    """Numbered menu. rows = list of (key, columns...). Returns the chosen key or None.
    The user may type the row number or the key itself (e.g. a VMID)."""
    print(f"\n{title}\n")
    print(f"  {'#':>3}  {header}")
    for i, (_, text) in enumerate(rows, 1):
        print(f"  {i:>3}  {text}")
    keys = [str(k) for k, _ in rows]
    while True:
        try:
            ans = input("\nChoose number (or q to quit): ").strip()
        except EOFError:
            return None
        if ans.lower() in ("q", "quit", ""):
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(rows):
            return rows[int(ans) - 1][0]
        if ans in keys:
            return rows[keys.index(ans)][0]
        print("  -> not in the list")


# ------------------------------------------------------------------------------
# configuration
# ------------------------------------------------------------------------------

def parse_env(text):
    """Parse KEY=VALUE lines with shell quoting (as written by this tool or by bash %q)."""
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        try:
            parts = shlex.split(value)
            env[key.strip()] = parts[0] if parts else ""
        except ValueError:
            env[key.strip()] = value
    return env


def format_env(env):
    out = []
    for key in ENV_KEYS:
        if key in ENV_COMMENTS:
            out.append(f"# {ENV_COMMENTS[key]}")
        out.append(f"{key}={shlex.quote(str(env.get(key, '')))}")
    return "\n".join(out) + "\n"


def load_env():
    if not ENV_FILE.exists():
        raise Fatal(f"Missing {ENV_FILE} - run: pve-r2-backup install && pve-r2-backup config")
    env = parse_env(ENV_FILE.read_text())
    # older configs used R2_BASE_PATH + R2_HOST_PREFIX
    if not env.get("R2_PATH") and env.get("R2_HOST_PREFIX"):
        base = env.get("R2_BASE_PATH", "")
        env["R2_PATH"] = f"{base}/{env['R2_HOST_PREFIX']}" if base else env["R2_HOST_PREFIX"]
    env["R2_PATH"] = env.get("R2_PATH", "").strip("/")
    env["R2_STORAGE"] = env.get("R2_STORAGE", "")
    env["R2_NOTIFY"] = env.get("R2_NOTIFY") or "failures"
    env["R2_STALE_DAYS"] = env.get("R2_STALE_DAYS") or "8"
    return env


def save_env(env):
    CONF_DIR.mkdir(mode=0o700, exist_ok=True)
    tmp = ENV_FILE.with_suffix(".new")
    tmp.write_text(format_env(env))
    tmp.chmod(0o600)
    tmp.replace(ENV_FILE)


def crypt_enabled():
    try:
        return "[r2crypt]" in RCLONE_CONF.read_text()
    except OSError:
        return False


def remote_path(env, sub, bucket=None):
    """rclone path for a folder in the bucket, via the crypt wrapper if configured."""
    bucket = bucket or env["R2_BUCKET"]
    if crypt_enabled():
        if bucket != env["R2_BUCKET"]:
            raise Fatal("encryption is bound to the configured bucket; cannot browse another bucket")
        return f"r2crypt:{sub}"
    return f"r2:{bucket}/{sub}"


def rclone(*args, check=True, input=None):
    return run(["rclone", "--config", str(RCLONE_CONF), *args], check=check, input=input)


def site_name(env):
    return env.get("R2_PATH", "").rsplit("/", 1)[-1] or socket.gethostname().split(".")[0]


def dump_dir(env):
    if not env.get("R2_STORAGE"):
        raise Fatal("No Proxmox storage set - run: pve-r2-backup config")
    path = (pvesh(f"/storage/{env['R2_STORAGE']}") or {}).get("path")
    if not path or not Path(path, "dump").is_dir():
        raise Fatal(f"Storage '{env['R2_STORAGE']}' has no dump directory (path: {path or 'unknown'})")
    return Path(path, "dump")


def local_node():
    nodes = [n["node"] for n in pvesh("/nodes")]
    short = socket.gethostname().split(".")[0]
    return short if short in nodes else nodes[0]


def guests():
    """{vmid: {type, name, status}} for all guests in the cluster."""
    return {str(r["vmid"]): {"type": r["type"], "name": r.get("name", ""), "status": r.get("status", "")}
            for r in pvesh("/cluster/resources", "--type", "vm")}


# ------------------------------------------------------------------------------
# reporting
# ------------------------------------------------------------------------------

def notify(env, subject, body):
    """Mail to root: /root/.forward -> proxmox-mail-forward -> Proxmox notification targets."""
    host = socket.getfqdn()
    msg = (f"Subject: [pve-r2] {site_name(env)}: {subject}\nX-PVE-R2-Host: {host}\n\n{body}\n"
           f"\n-- \n{SELF_INSTALLED} on {host}, {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}\n")
    try:
        subprocess.run(["sendmail", "root"], input=msg, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        log("WARNING: could not hand report to sendmail")


def read_state():
    try:
        return dict(line.split("=", 1) for line in STATE_FILE.read_text().splitlines() if "=" in line)
    except OSError:
        return {}


def write_state(**updates):
    """Merge updates into the state file; other keys are kept."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = read_state()
    state.update({k: str(v).replace("\n", " ") for k, v in updates.items()})
    state.setdefault("last_success", "")
    tmp = STATE_FILE.with_suffix(".new")
    tmp.write_text("".join(f"{k}={v}\n" for k, v in state.items()))
    tmp.replace(STATE_FILE)


def save_state(result, summary):
    now = int(time.time())
    write_state(last_run=now, last_result=result, last_summary=summary,
                **({"last_success": now} if result == "OK" else {}))


# ------------------------------------------------------------------------------
# selecting and classifying backups
# ------------------------------------------------------------------------------

def vmid_classes(jobs, all_ids):
    """{vmid: class} for every guest in an enabled backup job. Jobs with a monthly-or-rarer
    schedule give 'monthly', everything else 'weekly'; a guest in both is 'weekly'."""
    classes = {}
    for job in jobs:
        if str(job.get("enabled", 1)) in ("0", "False", "false"):
            continue
        cls = "monthly" if RARE_SCHEDULE_RE.search(job.get("schedule", "")) else "weekly"
        if str(job.get("all", 0)) in ("1", "True", "true"):
            exclude = set(filter(None, str(job.get("exclude", "")).split(",")))
            ids = [i for i in all_ids if i not in exclude]
        else:
            ids = [i for i in str(job.get("vmid", "")).split(",") if i.isdigit()]
        for vmid in ids:
            if classes.get(vmid) != "weekly":
                classes[vmid] = cls
    return classes


def default_storage(saved, stores, jobs):
    """Suggested storage for config: the saved one if it still exists, else the one
    most enabled backup jobs write to, else the only/first storage with backups."""
    if saved in stores:
        return saved
    used = [j.get("storage") for j in jobs if str(j.get("enabled", 1)) != "0"]
    used = [s for s in used if s in stores]
    if used:
        return max(dict.fromkeys(used), key=used.count)
    return stores[0] if stores else ""


def current_classes():
    return vmid_classes(pvesh("/cluster/backup"), list(guests()))


def is_archive(name):
    return not name.endswith(SIDECAR_EXTS)


def select_files(names, mode, days=None, mtimes=None, now=None):
    """Pick vzdump file names for upload.
      all    every backup file
      since  files modified in the last `days` days (mtimes: {name: epoch})
      latest newest backup of each guest that produced an archive (+ its log/notes);
             failed runs leave only a .log and are skipped"""
    names = [n for n in names if VZDUMP_RE.match(n) and not n.endswith((".dat", ".tmp"))]
    if mode == "all":
        return sorted(names)
    if mode == "since":
        now = now or time.time()
        return sorted(n for n in names if now - mtimes[n] < days * 86400)
    newest = {}
    for n in names:
        if not is_archive(n):
            continue
        _, vmid, ts, _ = VZDUMP_RE.match(n).groups()
        key = n[:n.index(ts) + len(ts)]
        if vmid not in newest or ts > newest[vmid][0]:
            newest[vmid] = (ts, key)
    keys = {key + "." for _, key in newest.values()}
    return sorted(n for n in names if any(n.startswith(k) for k in keys))


def parse_copied(lines):
    """File names rclone reported as uploaded."""
    return [m.group(1) for line in lines for m in [COPIED_RE.search(line)] if m]


# ------------------------------------------------------------------------------
# sync
# ------------------------------------------------------------------------------

class Lock:
    def __init__(self, wait):
        self.wait = wait

    def __enter__(self):
        self.fh = LOCK_FILE.open("w")
        deadline = time.time() + self.wait
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.time() >= deadline:
                    return False
                time.sleep(5)

    def __exit__(self, *exc):
        self.fh.close()


def rclone_copy(src_dir, dest, files, dry):
    """Upload `files` from src_dir to dest; returns (ok, output lines). Output goes to the
    log, and to the terminal when run interactively."""
    with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False) as f:
        f.write("\n".join(files) + "\n")
        list_file = f.name
    cmd = ["rclone", "--config", str(RCLONE_CONF), "copy", str(src_dir), dest,
           "--files-from", list_file, "--no-traverse", *UPLOAD_OPTS] + (["--dry-run"] if dry else [])
    lines = []
    try:
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as p, \
                LOG_FILE.open("a") as logf:
            for line in p.stdout:
                lines.append(line.rstrip("\n"))
                logf.write(line)
                logf.flush()
                if sys.stdout.isatty() and "INFO  :" not in line:
                    sys.stdout.write(line)
        return p.returncode == 0, lines
    finally:
        os.unlink(list_file)


def check_upload(selected, local_sizes, remote_sizes):
    """Compare what should be in R2 with what is there.
    selected: names chosen for upload; local_sizes / remote_sizes: {name: bytes} now.
    Returns (missing, vanished): missing = still local but absent or a different size in R2
    (a real failure); vanished = deleted locally before it could be uploaded (reported only)."""
    missing, vanished = [], []
    for name in selected:
        if name not in local_sizes:
            if name not in remote_sizes:
                vanished.append(name)
        elif remote_sizes.get(name) != local_sizes[name]:
            missing.append(name)
    return sorted(missing), sorted(vanished)


def remote_sizes(dest, files):
    """{name: size} of `files` in the R2 folder `dest` (only those that exist)."""
    with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False) as f:
        f.write("\n".join(files) + "\n")
        list_file = f.name
    try:
        items = json.loads(rclone("lsjson", dest, "--files-from", list_file, "--files-only") or "[]")
    finally:
        os.unlink(list_file)
    return {it["Name"]: it["Size"] for it in items}


def do_sync(mode="since", days=2, dry=False, wait=0):
    env = load_env()
    with Lock(wait) as locked:
        if not locked:
            log("Another sync is running - skipped")
            return 0
        try:
            return _sync(env, mode, days, dry)
        except Fatal as e:
            if not dry:
                log(f"ERROR: {e}")
                save_state("FAILED", str(e))
                tail = LOG_FILE.read_text().splitlines()[-20:] if LOG_FILE.exists() else []
                notify(env, f"FAILED - {e}", f"Upload aborted before completion:\n\n  {e}\n\nLast log lines:\n" + "\n".join(tail))
            raise


def _sync(env, mode, days, dry):
    src = dump_dir(env)
    classes = current_classes()
    entries = {e.name: e.stat().st_mtime for e in os.scandir(src) if e.is_file()}
    files = select_files(list(entries), mode, days, entries)

    by_class = {}
    for f in files:
        vmid = VZDUMP_RE.match(f).group(2)
        by_class.setdefault(classes.get(vmid, "other"), []).append(f)

    label = f"--{mode}" + (f" ({days}d)" if mode == "since" else "")
    ok_all, total_files, total_bytes, report = True, 0, 0, []
    for cls, cls_files in sorted(by_class.items()):
        dest = remote_path(env, f"{env['R2_PATH']}/{cls}")
        log(f"Uploading {len(cls_files)} file(s) {label} to {dest}{' --dry-run' if dry else ''}")
        ok, lines = rclone_copy(src, dest, cls_files, dry)
        if not ok:
            log(f"Upload to {dest} FAILED")
            ok_all = False
        copied = parse_copied(lines)
        size = sum((src / n).stat().st_size for n in copied if (src / n).exists())
        total_files += len(copied)
        total_bytes += size
        extra = []
        if not dry:
            # rclone can report success after silently dropping files, so check R2 itself
            local_now = {n: (src / n).stat().st_size for n in cls_files if (src / n).exists()}
            try:
                missing, vanished = check_upload(cls_files, local_now, remote_sizes(dest, cls_files))
            except (Fatal, ValueError) as e:
                missing, vanished = [f"(could not list R2: {e})"], []
            if missing:
                ok = False
                log(f"Verify {dest}: {len(missing)} file(s) missing or wrong size in R2: {', '.join(missing)}")
                extra.append(f"           missing in R2: {', '.join(missing)}")
            if vanished:
                log(f"Verify {dest}: deleted locally before upload: {', '.join(vanished)}")
                extra.append(f"           deleted locally before upload (not in R2): {', '.join(vanished)}")
        if not ok:
            ok_all = False
        report.append(f"{cls:<8} {'OK' if ok else 'FAILED':<6} {len(cls_files):>3} selected, "
                      f"{len(copied):>3} uploaded ({human(size)})" + ("" if dry else ", verified in R2"))
        report += extra
        if not ok:
            report += ["           " + l for l in [l for l in lines if "ERROR" in l or "Failed" in l][-5:]]
    if not report:
        report = [f"nothing selected ({label})"]
    if dry:
        return 0 if ok_all else 1

    summary = f"{total_files} file(s), {human(total_bytes)} uploaded"
    body = "\n".join(report)
    vanished_any = any("deleted locally before upload" in l for l in report)
    if ok_all:
        log(f"Sync finished OK - {summary}")
        save_state("OK", summary)
        if env["R2_NOTIFY"] == "always" or vanished_any:
            notify(env, f"OK - {summary}", f"Upload {label} to {env['R2_BUCKET']}/{env['R2_PATH']} finished OK.\n\n{body}")
        return 0
    save_state("FAILED", summary)
    notify(env, f"FAILED - upload errors ({summary})",
           f"Upload {label} to {env['R2_BUCKET']}/{env['R2_PATH']} had errors.\n\n{body}\n\nLog: {LOG_FILE}")
    return 1


def r2_sizes(env):
    out = {}
    for cls in CLASSES:
        try:
            j = json.loads(rclone("size", "--json", remote_path(env, f"{env['R2_PATH']}/{cls}")))
            out[cls] = f"{j['count']:>4} objects  {human(j['bytes'])}"
        except (Fatal, ValueError, KeyError) as e:
            out[cls] = f"(error: {e})"
    return out


def do_weekly():
    """Timer job: catch-up upload of the last 14 days, then a summary mail that warns
    when the last successful upload is older than R2_STALE_DAYS."""
    env = load_env()
    try:
        rcode = do_sync("since", 14)
    except Fatal:
        rcode = 1
    state = read_state()
    stale_days = int(env["R2_STALE_DAYS"])
    last_ok = state.get("last_success")
    if not last_ok or time.time() - int(last_ok) > stale_days * 86400:
        subject = f"STALE - no successful upload for more than {stale_days} days"
    elif rcode:
        subject = "weekly: catch-up upload FAILED"
    else:
        subject = "weekly OK"
    hc_line, hc_ok = hostconfig_summary(env, state)
    if not hc_ok and subject == "weekly OK":
        subject = "weekly: host config backup missing"
    try:
        local = [b for b in pvesh(f"/nodes/{local_node()}/storage/{env['R2_STORAGE']}/content", "--content", "backup")]
        local_txt = f"{len(local)}, {human(sum(b.get('size', 0) for b in local))}"
    except Fatal as e:
        local_txt = f"(error: {e})"
    body = [f"Last successful upload : {age_text(last_ok)}",
            f"Last run               : {age_text(state.get('last_run'))} - {state.get('last_result')}: {state.get('last_summary')}",
            f"Local backups          : {local_txt}",
            f"Host config backup     : {hc_line}", "",
            f"In R2 ({env['R2_BUCKET']}/{env['R2_PATH']}):"]
    body += [f"  {cls:<8} {txt}" for cls, txt in r2_sizes(env).items()]
    notify(env, subject, "\n".join(body))
    return rcode


def do_notify_test():
    env = load_env()
    notify(env, "notification test", f"Test from pve-r2-backup on {socket.getfqdn()}.\n"
                                     "If you can read this, upload reports from this host will reach you.")
    print("Test mail handed to sendmail -> root -> Proxmox notification targets. Check your inbox.")
    print("Nothing arriving? Check Datacenter > Notifications (target + default-matcher), "
          "and 'mailq' / journalctl -t postfix/smtp")


# ------------------------------------------------------------------------------
# host configuration backup
# ------------------------------------------------------------------------------

RESTORE_TXT = """Host configuration backup made by pve-r2-backup on {host}, {date}.

Contents
  etc/                 /etc of the host, including etc/pve (guest configs, storage.cfg,
                       jobs.cfg, users, firewall, notifications, certificates)
  _hostconfig/config.db
                       copy of /var/lib/pve-cluster/config.db, the database behind /etc/pve
  _hostconfig/inventory.txt
                       disks, LVM, ZFS, network, storages, guests, packages at backup time
  root/, usr/local/, var/spool/cron/ and any extra folders

Single files: copy them from here, e.g. etc/pve/qemu-server/100.conf or
etc/pve/lxc/101.conf (into /etc/pve/... on the host).

Rebuild a lost host:
  1. Install the same Proxmox VE major version with the SAME hostname and IP.
  2. Recreate disks / pools / volume groups as listed in _hostconfig/inventory.txt.
  3. Compare and copy etc/network/interfaces (NIC names can differ on new hardware).
  4. Restore the Proxmox configuration:
       systemctl stop pve-cluster
       cp _hostconfig/config.db /var/lib/pve-cluster/config.db
       rm -f /var/lib/pve-cluster/config.db-wal /var/lib/pve-cluster/config.db-shm
       systemctl start pve-cluster
     then reboot.
  5. Copy back what else you need (root/.ssh, usr/local, cron, ...).
  6. pve-r2-backup install + config, then restore guests: pve-r2-backup restore
"""


def hostconfig_paths(env):
    """Paths (relative to /) that go into the archive: the defaults plus R2_HOSTCONFIG_EXTRA
    (comma and/or space separated), only those that exist."""
    extra = [x.strip().strip("/") for x in re.split(r"[,\s]+", env.get("R2_HOSTCONFIG_EXTRA", "")) if x.strip("/ ")]
    out = []
    for rel in HOSTCONFIG_PATHS + extra:
        if rel not in out and Path("/", rel).exists():
            out.append(rel)
    return out


def hostconfig_versions(items):
    """[{name, host, ts, size}] newest first, from an rclone lsjson listing of <path>/host/."""
    out = []
    for it in items:
        m = HOSTCONFIG_RE.match(it.get("Name") or it.get("Path", ""))
        if m:
            out.append({"name": m.group(0), "host": m.group(1), "ts": m.group(2), "size": it.get("Size", 0)})
    return sorted(out, key=lambda v: v["ts"], reverse=True)


def inventory_text():
    parts = []
    for cmd in INVENTORY_CMDS:
        try:
            p = subprocess.run(shlex.split(cmd), text=True, capture_output=True, timeout=120)
            body = (p.stdout + p.stderr).rstrip()
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            body = "(timed out)"
        parts.append(f"===== {cmd}\n{body}\n")
    return "\n".join(parts)


def gpg(workdir, passphrase, *args):
    """gpg with its own temporary home, passphrase from a file (never on the command line)."""
    home = Path(workdir, "gnupg")
    home.mkdir(mode=0o700, exist_ok=True)
    pf = Path(workdir, "pass")
    pf.write_text(passphrase)
    pf.chmod(0o600)
    try:
        run(["gpg", "--homedir", str(home), "--batch", "--yes", "--quiet", "--pinentry-mode", "loopback",
             "--passphrase-file", str(pf), *args])
    finally:
        pf.unlink(missing_ok=True)


def build_hostconfig(env, workdir, host, ts):
    """Create the encrypted archive in workdir; returns (path, list of included paths)."""
    stage = Path(workdir, "stage", "_hostconfig")
    stage.mkdir(parents=True)
    if PVE_DB.exists():
        run(["sqlite3", str(PVE_DB), f".backup {stage / 'config.db'}"])
    (stage / "inventory.txt").write_text(inventory_text())
    (stage / "RESTORE.txt").write_text(RESTORE_TXT.format(host=host, date=ts_text(ts)))
    paths = hostconfig_paths(env)
    tar = Path(workdir, "hostconfig.tar.zst")
    p = subprocess.run(["tar", "--zstd", "-cf", str(tar), "--ignore-failed-read", "--warning=no-file-changed",
                        "--warning=no-file-ignored", *[f"--exclude={x}" for x in HOSTCONFIG_EXCLUDES],
                        "-C", "/", *paths, "-C", str(stage.parent), "_hostconfig"],
                       text=True, capture_output=True)
    if p.returncode not in (0, 1):          # 1 = some file changed while reading: fine
        err = p.stderr.strip().splitlines()
        raise Fatal(f"tar failed: {err[-1] if err else p.returncode}")
    out = Path(workdir, f"{host}-config-{ts}.tar.zst.gpg")
    gpg(workdir, env["R2_HOSTCONFIG_PASS"], "--symmetric", "--cipher-algo", "AES256",
        "--compress-algo", "none", "-o", str(out), str(tar))
    tar.unlink()
    return out, paths


def do_hostconfig(dry=False, if_configured=False):
    env = load_env()
    if not env.get("R2_HOSTCONFIG_PASS"):
        if if_configured:               # timer on a host where it was never set up
            return 0
        raise Fatal("No host config passphrase set - run: pve-r2-backup config")
    host = socket.gethostname().split(".")[0]
    ts = datetime.now().strftime("%Y_%m_%d-%H_%M_%S")
    try:
        with tempfile.TemporaryDirectory(prefix="pve-r2-hostconfig-", dir="/var/tmp") as wd:
            out, paths = build_hostconfig(env, wd, host, ts)
            size = out.stat().st_size
            dest = remote_path(env, f"{env['R2_PATH']}/{HOST_DIR}/{out.name}")
            if dry:
                print(f"Would upload {out.name} ({human(size)}) to {dest}\nIncludes: /" + ", /".join(paths))
                return 0
            rclone("copyto", str(out), dest, "--retries", "5")
            got = remote_sizes(remote_path(env, f"{env['R2_PATH']}/{HOST_DIR}"), [out.name]).get(out.name)
            if got != size:
                raise Fatal(f"uploaded {out.name} but R2 shows size {got}, expected {size}")
    except Fatal as e:
        log(f"Host config backup FAILED: {e}")
        write_state(last_hostconfig_result=f"FAILED: {e}")
        notify(env, f"host config backup FAILED - {e}",
               f"The encrypted backup of this host's configuration could not be made or uploaded:\n\n  {e}")
        raise
    log(f"Host config backup OK - {out.name} ({human(size)}) to {dest}")
    write_state(last_hostconfig=int(time.time()), last_hostconfig_result=f"OK {out.name} ({human(size)})")
    return 0


def hostconfig_summary(env, state):
    """(text, ok) for the weekly summary and status."""
    if not env.get("R2_HOSTCONFIG_PASS"):
        return "not set up (run: pve-r2-backup config)", False
    last = state.get("last_hostconfig")
    ok = bool(last) and time.time() - int(last) < HOSTCONFIG_STALE_DAYS * 86400
    text = f"{age_text(last)} - {state.get('last_hostconfig_result', '')}"
    return (text if ok else f"{text}  <-- no backup for more than {HOSTCONFIG_STALE_DAYS} days"), ok


def do_hostconfig_list(env, bucket, path):
    try:
        items = json.loads(rclone("lsjson", "--files-only", remote_path(env, f"{path}/{HOST_DIR}", bucket)))
    except (ValueError, Fatal):
        items = []
    versions = hostconfig_versions(items)
    if not versions:
        print(f"No host config backups in {bucket}/{path}/{HOST_DIR}/")
    return versions


def do_hostconfig_cmd(a):
    need_root()
    if a.action == "backup":
        return do_hostconfig(a.dry_run, a.if_configured)
    env = load_env()
    bucket, path = env["R2_BUCKET"], env["R2_PATH"]
    if a.path:
        bucket, _, path = a.path.strip("/").partition("/")
    versions = do_hostconfig_list(env, bucket, path)
    if not versions:
        return 1
    if a.action == "list" or not versions:
        print(f"Host config backups in {bucket}/{path}/{HOST_DIR}/:\n")
        for i, v in enumerate(versions, 1):
            print(f"  {i:>3}  {ts_text(v['ts'])}  {v['host']:<16} {human(v['size']):>10}")
        return 0
    v = pick_version(versions, a.version or "latest")
    target = Path(a.to or f"/root/hostconfig-{v['host']}-{v['ts']}")
    if target.resolve() == Path("/") or (target.exists() and any(target.iterdir())):
        raise Fatal(f"{target} must be a new or empty folder (never /) - choose another with --to")
    passphrase = env.get("R2_HOSTCONFIG_PASS", "")
    if a.path or not passphrase:
        passphrase = ask("Passphrase of that host's config backups", passphrase or None, keep_spaces=True)
    with tempfile.TemporaryDirectory(prefix="pve-r2-hostconfig-", dir="/var/tmp") as wd:
        enc = Path(wd, v["name"])
        print(f"Downloading {v['name']} ({human(v['size'])})...")
        rclone("copyto", remote_path(env, f"{path}/{HOST_DIR}/{v['name']}", bucket), str(enc))
        tar = Path(wd, "hostconfig.tar.zst")
        try:
            gpg(wd, passphrase, "--decrypt", "-o", str(tar), str(enc))
        except Fatal:
            raise Fatal("decryption failed - wrong passphrase?")
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o700)
        run(["tar", "--zstd", "-xf", str(tar), "-C", str(target)])
    print(f"\nUnpacked into {target}  (mode 700: it contains keys and passwords)")
    print(f"Read {target}/_hostconfig/RESTORE.txt for what is in it and how to rebuild a host.")
    return 0


# ------------------------------------------------------------------------------
# vzdump hook
# ------------------------------------------------------------------------------

HOOK_PHASES = {"job-init", "job-start", "job-end", "job-abort", "backup-start", "backup-end",
               "backup-abort", "log-end", "pre-stop", "pre-restart", "post-restart"}


def do_hook(phase):
    """vzdump calls this for every phase. Never fails the backup: always exit 0."""
    try:
        if phase == "job-end" and ENV_FILE.exists():
            env = load_env()
            store = os.environ.get("STOREID", "")
            # only react to jobs that wrote to our storage, and only once configured
            if env["R2_STORAGE"] and env.get("R2_BUCKET") and store in ("", env["R2_STORAGE"]):
                with LOG_FILE.open("a") as logf:
                    subprocess.Popen([str(SELF_INSTALLED), "sync", "--since", "2", "--wait"],
                                     stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                     start_new_session=True)
    except Exception as e:      # noqa: BLE001 - the hook must never break a backup
        log(f"hook error (ignored): {e}")
    return 0


# ------------------------------------------------------------------------------
# install / config / uninstall
# ------------------------------------------------------------------------------

SERVICE = f"""[Unit]
Description=Catch-up upload of vzdump backups to Cloudflare R2 + weekly report
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={SELF_INSTALLED} weekly
Nice=10
IOSchedulingClass=idle
"""

TIMER = """[Unit]
Description=Weekly catch-up upload of vzdump backups to Cloudflare R2

[Timer]
OnCalendar=Mon *-*-* 06:00
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
"""

HC_UNIT = f"{UNIT_NAME}-hostconfig"
HC_SERVICE = f"""[Unit]
Description=Encrypted backup of this Proxmox host's configuration to Cloudflare R2
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={SELF_INSTALLED} hostconfig --if-configured
Nice=10
IOSchedulingClass=idle
"""

HC_TIMER = """[Unit]
Description=Daily host configuration backup to Cloudflare R2

[Timer]
OnCalendar=*-*-* 03:30
Persistent=true
RandomizedDelaySec=30m

[Install]
WantedBy=timers.target
"""

LOGROTATE = f"""{LOG_FILE} {{
    monthly
    rotate 6
    compress
    missingok
    notifempty
}}
"""


def fetch(url, dest):
    """Download url to dest. downloads.rclone.org rejects Python's default
    User-Agent with 403, so identify as this tool."""
    req = urllib.request.Request(url, headers={"User-Agent": f"pve-r2-backup/{VERSION} "
                                               "(+https://github.com/pacmac/pve-r2-backup)"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
            shutil.copyfileobj(resp, out)
    except (urllib.error.URLError, OSError) as e:
        raise Fatal(f"download of {url} failed: {e}")


def rclone_version():
    try:
        m = re.search(r"rclone v(\d+)\.(\d+)", run(["rclone", "version"]))
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    except Fatal:
        return (0, 0)


def do_install():
    need_root()
    if not shutil.which("pveversion"):
        raise Fatal("Not a Proxmox VE host")
    if rclone_version() < (1, 65):
        print("Installing current rclone from rclone.org...")
        arch = run(["dpkg", "--print-architecture"]).strip()
        with tempfile.NamedTemporaryFile(suffix=".deb", delete=False) as f:
            deb = f.name
        try:
            fetch(f"https://downloads.rclone.org/rclone-current-linux-{arch}.deb", deb)
            run(["dpkg", "-i", deb], capture=False)
        finally:
            os.unlink(deb)
    print(f"rclone: {run(['rclone', 'version']).splitlines()[0]}")

    me = Path(__file__).resolve()
    if me != SELF_INSTALLED.resolve():
        shutil.copyfile(me, SELF_INSTALLED)
    SELF_INSTALLED.chmod(0o755)

    CONF_DIR.mkdir(mode=0o700, exist_ok=True)
    if not ENV_FILE.exists():
        save_env({"R2_NOTIFY": "failures", "R2_STALE_DAYS": "8"})
        print(f"Created {ENV_FILE}")

    # vzdump hook - refuse to replace a hook script that isn't ours
    VZDUMP_CONF.touch()
    lines = VZDUMP_CONF.read_text().splitlines()
    current = [l.split(None, 1)[1].strip() for l in lines if l.startswith("script:") and len(l.split()) > 1]
    if not current:
        with VZDUMP_CONF.open("a") as f:
            f.write(f"script: {SELF_INSTALLED}\n")
        print(f"Added vzdump hook to {VZDUMP_CONF}")
    elif current[0] != str(SELF_INSTALLED):
        print(f"[WARN] {VZDUMP_CONF} already uses hook '{current[0]}' - not changed. "
              "Uploads will only run from the timer.", file=sys.stderr)

    Path(f"/etc/systemd/system/{UNIT_NAME}.service").write_text(SERVICE)
    Path(f"/etc/systemd/system/{UNIT_NAME}.timer").write_text(TIMER)
    Path(f"/etc/systemd/system/{HC_UNIT}.service").write_text(HC_SERVICE)
    Path(f"/etc/systemd/system/{HC_UNIT}.timer").write_text(HC_TIMER)
    Path(f"/etc/logrotate.d/{UNIT_NAME}").write_text(LOGROTATE)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", f"{UNIT_NAME}.timer", f"{HC_UNIT}.timer"])
    print("Weekly catch-up timer and daily host config timer enabled.\n")
    print(f"Next: {SELF_INSTALLED.name} config")


def write_rclone_conf(env):
    conf = (f"[r2]\ntype = s3\nprovider = Cloudflare\naccess_key_id = {env['R2_ACCESS_KEY_ID']}\n"
            f"secret_access_key = {env['R2_SECRET_ACCESS_KEY']}\n"
            f"endpoint = https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com\n"
            "acl = private\nno_check_bucket = true\n")
    if env.get("R2_CRYPT_PASSWORD"):
        obscure = lambda pw: run(["rclone", "obscure", "-"], input=pw).strip()  # noqa: E731
        conf += (f"\n[r2crypt]\ntype = crypt\nremote = r2:{env['R2_BUCKET']}\n"
                 "filename_encryption = standard\ndirectory_name_encryption = false\n"
                 f"password = {obscure(env['R2_CRYPT_PASSWORD'])}\n")
        if env.get("R2_CRYPT_SALT"):
            conf += f"password2 = {obscure(env['R2_CRYPT_SALT'])}\n"
    old = os.umask(0o077)
    try:
        RCLONE_CONF.write_text(conf)
    finally:
        os.umask(old)
    RCLONE_CONF.chmod(0o600)


def claim_site(env):
    """Make sure no other host already uploads to <bucket>/<path>/, then mark it ours.
    Uses /etc/machine-id so a renamed host keeps its path."""
    marker = f"r2:{env['R2_BUCKET']}/{env['R2_PATH']}/.owner"
    me = f"{Path('/etc/machine-id').read_text().strip()} {socket.getfqdn()}"
    owner = rclone("cat", marker, check=False).strip().splitlines()
    owner = owner[0] if owner else ""
    if owner and owner.split()[0] != me.split()[0]:
        raise Fatal(f"R2 path '{env['R2_BUCKET']}/{env['R2_PATH']}/' already belongs to another host: "
                    f"{owner.split(' ', 1)[-1]}\n       Pick a different path, or delete "
                    f"{env['R2_BUCKET']}/{env['R2_PATH']}/.owner in R2 if that host is gone.")
    if not owner:
        rclone("rcat", marker, input=me + "\n")
    print(f"R2 path {env['R2_BUCKET']}/{env['R2_PATH']}/ belongs to this host")


def do_config():
    """Ask for (or confirm) all settings, write rclone.conf, test access and claim the path.
    Safe to re-run: every prompt defaults to the saved value."""
    need_root()
    if not shutil.which("rclone"):
        raise Fatal("rclone missing - run: pve-r2-backup install")
    env = load_env()

    print("== Cloudflare R2 (same for all hosts) ==")
    print("Paste the bucket URL from Cloudflare, e.g. https://<account-id>.r2.cloudflarestorage.com/my-bucket")
    print("(or just the account ID).")
    acct = ask("Bucket URL or Account ID", env.get("R2_ACCOUNT_ID"))
    url_path = ""
    m = re.fullmatch(r"(?:https?://)?([0-9a-fA-F]+)\.r2\.cloudflarestorage\.com/?(.*)", acct)
    if m:
        acct, url_path = m.group(1), m.group(2).strip("/")
    env["R2_ACCOUNT_ID"] = acct
    print("From the R2 API token page (R2 Overview > API Tokens > Manage):")
    env["R2_ACCESS_KEY_ID"] = ask("Access Key ID (32 hex chars)", env.get("R2_ACCESS_KEY_ID"), r"[0-9a-f]{32}")
    env["R2_SECRET_ACCESS_KEY"] = ask("Secret Access Key (64 hex chars)", env.get("R2_SECRET_ACCESS_KEY"), r"[0-9a-f]{64}")
    if not (env["R2_ACCOUNT_ID"] and env["R2_ACCESS_KEY_ID"] and env["R2_SECRET_ACCESS_KEY"]):
        raise Fatal("Account ID and both keys are required")
    if not re.fullmatch(r"[0-9a-f]{32}", env["R2_ACCOUNT_ID"]):
        print(f"[WARN] Account ID is usually 32 hex chars, got {len(env['R2_ACCOUNT_ID'])}")

    print("\n== This host ==")
    saved = f"{env['R2_BUCKET']}/{env['R2_PATH']}" if env.get("R2_BUCKET") else ""
    if "/" in url_path:
        default = url_path                     # URL included a folder
    elif saved and (not url_path or saved.split("/")[0] == url_path):
        default = saved                        # same bucket as before: keep saved folder
    else:
        default = url_path
    full = ask("Full R2 path for THIS host's backups, unique per host (e.g. my-bucket/backups/pve/site1)",
               default, r"[A-Za-z0-9._/-]+").strip("/")
    if "/" not in full:
        raise Fatal("Give bucket AND folder, e.g. my-bucket/backups/pve/site1 - not just the bucket")
    env["R2_BUCKET"], env["R2_PATH"] = full.split("/", 1)
    print(f"  Bucket : {env['R2_BUCKET']}\n  Folder : {env['R2_PATH']}/{{weekly,monthly,other}}/")
    save_env(env)           # keep what was typed so far, even if a later step fails
    stores = [s["storage"] for s in pvesh("/storage")
              if "backup" in s.get("content", "") and not s.get("disable")]
    if not stores:
        raise Fatal("No enabled Proxmox storage holds backups - add one or enable it first")
    print("\nWhich Proxmox storage do your backup jobs write to? Its dump/ folder is what gets")
    print(f"uploaded (Datacenter > Backup > job > Storage). Storages with backups: {', '.join(stores)}")
    default = default_storage(env["R2_STORAGE"], stores, pvesh("/cluster/backup"))
    while True:
        env["R2_STORAGE"] = ask("Storage to upload from", default)
        if env["R2_STORAGE"] in stores:
            break
        print(f"  -> '{env['R2_STORAGE']}' is not one of: {', '.join(stores)}", file=sys.stderr)
    print(f"  Uploading from {dump_dir(env)}")

    if not env.get("R2_CRYPT_PASSWORD"):
        if ask_yes("Encrypt backups before upload?"):
            env["R2_CRYPT_PASSWORD"] = ask("Encryption password", keep_spaces=True)
            env["R2_CRYPT_SALT"] = ask("Second password / salt", keep_spaces=True)
    else:
        print(f"Encryption: ON (passwords in {ENV_FILE})")

    print("\n== Host configuration backup (daily, always encrypted) ==")
    print("Saves /etc (incl. /etc/pve), the Proxmox config database, root's keys, /usr/local and cron,")
    print("so a lost host can be rebuilt. Needs its own passphrase - keep a copy OFF this host.")
    pw = ask("Passphrase (Enter = generate one)", env.get("R2_HOSTCONFIG_PASS") or None, keep_spaces=True)
    if not pw:
        pw = secrets.token_urlsafe(18)
        print(f"\n  Generated passphrase:  {pw}\n  SAVE IT NOW in your password manager.\n")
    env["R2_HOSTCONFIG_PASS"] = pw
    print("Always included: /" + ", /".join(HOSTCONFIG_PATHS))
    print("Add your own scripts/data folders and credential files outside /etc and /root/.ssh,")
    print("e.g. /usr/share/scripts,/root/.config  (.git, venv and node_modules are skipped)")
    env["R2_HOSTCONFIG_EXTRA"] = ask("Extra folders to include, comma separated (Enter = none)",
                                     env.get("R2_HOSTCONFIG_EXTRA") or None, keep_spaces=True)
    missing = [x for x in re.split(r"[,\s]+", env["R2_HOSTCONFIG_EXTRA"]) if x and not Path(x).exists()]
    if missing:
        print(f"  [WARN] not found (skipped until they exist): {', '.join(missing)}")

    save_env(env)
    write_rclone_conf(env)
    print(f"\nWrote {ENV_FILE} and {RCLONE_CONF}")

    print("Testing bucket access...")
    try:
        rclone("lsf", f"r2:{env['R2_BUCKET']}", "--max-depth", "1")
    except Fatal:
        raise Fatal(f"Cannot list bucket '{env['R2_BUCKET']}' - check credentials/token permissions")
    # unique name so a bucket lock rule can't block re-running config
    test_obj = f"r2:{env['R2_BUCKET']}/{env['R2_PATH']}/_test-{int(time.time())}.txt"
    try:
        rclone("rcat", test_obj, input=f"pve-r2-backup test {socket.gethostname()} {datetime.now().isoformat()}\n")
    except Fatal:
        raise Fatal("Write test failed - token needs Object Read & Write")
    try:
        rclone("deletefile", test_obj)
    except Fatal:
        print(f"(test file {test_obj} could not be deleted - fine if bucket lock is on)")
    print("OK: bucket readable and writable")
    claim_site(env)
    if env.get("R2_CRYPT_PASSWORD"):
        print(f"Encryption ON - store the passwords from {ENV_FILE} somewhere off this host!")
    print(f"\nUploads go to: {env['R2_BUCKET']}/{env['R2_PATH']}/{{weekly,monthly,other,host}}/")
    print(f"Next: {SELF_INSTALLED.name} sync --latest -n   (dry run), then without -n")


def do_uninstall():
    need_root()
    if VZDUMP_CONF.exists():
        keep = [l for l in VZDUMP_CONF.read_text().splitlines() if l.strip() != f"script: {SELF_INSTALLED}"]
        VZDUMP_CONF.write_text("\n".join(keep) + ("\n" if keep else ""))
    run(["systemctl", "disable", "--now", f"{UNIT_NAME}.timer", f"{HC_UNIT}.timer"], check=False)
    for p in (f"/etc/systemd/system/{UNIT_NAME}.service", f"/etc/systemd/system/{UNIT_NAME}.timer",
              f"/etc/systemd/system/{HC_UNIT}.service", f"/etc/systemd/system/{HC_UNIT}.timer",
              f"/etc/logrotate.d/{UNIT_NAME}"):
        Path(p).unlink(missing_ok=True)
    run(["systemctl", "daemon-reload"], check=False)
    print(f"Removed hook and timers. Kept {CONF_DIR}, {SELF_INSTALLED} and everything in R2.")


def do_status():
    env = load_env()
    state = read_state()
    hook = [l for l in VZDUMP_CONF.read_text().splitlines() if l.startswith("script:")] if VZDUMP_CONF.exists() else []
    timer_on = run(["systemctl", "is-enabled", f"{UNIT_NAME}.timer"], check=False).strip()
    nxt = run(["systemctl", "show", f"{UNIT_NAME}.timer", "-p", "NextElapseUSecRealtime", "--value"], check=False).strip()
    print(f"Version     : {VERSION}")
    print(f"R2 path     : {env['R2_BUCKET']}/{env['R2_PATH']}/{{weekly,monthly,other}}/")
    print(f"Bucket      : {env['R2_BUCKET'] or '<not set>'}  (encryption {'on' if crypt_enabled() else 'off'})")
    print(f"Storage     : {env['R2_STORAGE']} -> {dump_dir(env)}")
    print(f"vzdump hook : {hook[0] if hook else 'none'}")
    print(f"Timer       : {timer_on or 'not installed'} / next {nxt}")
    print(f"Reports     : mail to root ({env['R2_NOTIFY']} + weekly summary), stale after {env['R2_STALE_DAYS']} days")
    print(f"Last run    : {age_text(state.get('last_run'))} - {state.get('last_result', '')} {state.get('last_summary', '')}")
    print(f"Last success: {age_text(state.get('last_success'))}")
    print(f"Host config : {hostconfig_summary(env, state)[0]}")
    print(f"  includes  : /" + ", /".join(hostconfig_paths(env)) + "\n")
    print("Guest classes:")
    for vmid, cls in sorted(current_classes().items(), key=lambda kv: int(kv[0])):
        print(f"  {vmid:<6} {cls}")
    print()
    for cls, txt in r2_sizes(env).items():
        print(f"{cls:<8} {txt}")
    print("\nLast log lines:")
    if LOG_FILE.exists():
        print("\n".join(LOG_FILE.read_text().splitlines()[-5:]))


# ------------------------------------------------------------------------------
# restore
# ------------------------------------------------------------------------------

def r2_catalog(env, bucket, path):
    """Backups in R2 grouped by guest: {vmid: {type, versions: [{ts, cls, archive, size, files}]}}
    newest version first. `files` = archive + its log/notes."""
    try:
        items = json.loads(rclone("lsjson", "-R", "--files-only", remote_path(env, path, bucket)))
    except ValueError:
        raise Fatal("could not read the R2 listing")
    groups = {}                                   # (vmid, ts) -> version
    for it in items:
        parts = it["Path"].split("/")
        if len(parts) != 2 or parts[0] not in CLASSES:
            continue
        cls, name = parts
        m = VZDUMP_RE.match(name)
        if not m:
            continue
        gtype, vmid, ts, _ = m.groups()
        v = groups.setdefault((vmid, ts), {"ts": ts, "cls": cls, "type": gtype, "archive": None,
                                           "size": 0, "files": []})
        if name not in [f for _, f in v["files"]]:
            v["files"].append((cls, name))
        if is_archive(name) and v["archive"] is None:
            v.update(archive=name, size=it["Size"], cls=cls)
    catalog = {}
    for (vmid, _), v in groups.items():
        if v["archive"] is None:                  # failed run: log only
            continue
        g = catalog.setdefault(vmid, {"type": v["type"], "versions": []})
        g["versions"].append(v)
    for g in catalog.values():
        g["versions"].sort(key=lambda v: v["ts"], reverse=True)
    return catalog


def guest_name(env, bucket, path, vmid, version, live):
    if vmid in live:
        return live[vmid]["name"]
    notes = [f for c, f in version["files"] if f.endswith(".notes")]
    if notes:
        cls = [c for c, f in version["files"] if f == notes[0]][0]
        txt = rclone("cat", remote_path(env, f"{path}/{cls}/{notes[0]}", bucket), check=False).strip()
        return txt.splitlines()[0][:30] if txt else ""
    return ""


def pick_version(versions, spec):
    if spec in (None, "", "latest"):
        return versions[0]
    if spec.isdigit() and 1 <= int(spec) <= len(versions) and len(spec) < 4:
        return versions[int(spec) - 1]
    norm = spec.replace("-", "_").replace(":", "_").replace(" ", "-")
    hits = [v for v in versions if v["ts"].startswith(norm) or v["ts"].replace("-", "_").startswith(norm)]
    if not hits:
        raise Fatal(f"no version matching '{spec}'")
    return hits[0]


def integrity_test(path):
    name = path.name
    tool = ("zstd" if name.endswith(".zst") else "gzip" if name.endswith(".gz")
            else "lzop" if name.endswith(".lzo") else None)
    if not tool or not shutil.which(tool):
        print(f"  (no integrity test for {name})")
        return True
    print(f"  testing archive with {tool} -t ...")
    ok = subprocess.run([tool, "-tq", str(path)]).returncode == 0
    print("  archive OK" if ok else "  ARCHIVE IS DAMAGED")
    return ok


def download(env, bucket, path, version, dest_dir):
    """Download a version (archive + log/notes) into dest_dir. Returns the archive path."""
    archive = dest_dir / version["archive"]
    if archive.exists() and archive.stat().st_size == version["size"]:
        print(f"Already on local disk: {archive}")
        return archive
    free = shutil.disk_usage(dest_dir).free
    if free < version["size"] * 1.05:
        raise Fatal(f"not enough space in {dest_dir}: need {human(version['size'])}, free {human(free)}")
    print(f"Downloading {human(version['size'])} to {dest_dir} ...")
    for cls, name in sorted(version["files"], key=lambda cf: is_archive(cf[1])):
        cmd = ["rclone", "--config", str(RCLONE_CONF), "copyto",
               remote_path(env, f"{path}/{cls}/{name}", bucket), str(dest_dir / name), *DOWNLOAD_OPTS]
        if is_archive(name) and sys.stdout.isatty():
            cmd.append("-P")
        if subprocess.run(cmd).returncode != 0:
            if is_archive(name):
                raise Fatal(f"download of {name} failed")
            print(f"  (could not download {name}, continuing)")
    print(f"Downloaded and checksum-verified by rclone: {archive}")
    if not integrity_test(archive):
        raise Fatal(f"{archive} is damaged - try another version")
    return archive


def restore_storages(node, gtype):
    content = "rootdir" if gtype == "lxc" else "images"
    return [(s["storage"], s.get("avail", 0)) for s in pvesh(f"/nodes/{node}/storage", "--content", content)
            if s.get("active", 1)]


def do_restore(args):
    need_root()
    env = load_env()
    bucket, path = env["R2_BUCKET"], env["R2_PATH"]
    if args.path:
        bucket, path = args.path.strip("/").split("/", 1)
    live = guests()

    print(f"Reading backups in R2: {bucket}/{path} ...")
    catalog = r2_catalog(env, bucket, path)
    if not catalog:
        raise Fatal(f"no backups found in {bucket}/{path}")

    # 1. guest
    vmid = args.vmid
    if not vmid or args.list:
        ids = sorted(catalog, key=int)
        # names of deleted guests come from their .notes in R2; fetch those in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            names = dict(zip(ids, pool.map(
                lambda vid: guest_name(env, bucket, path, vid, catalog[vid]["versions"][0], live), ids)))
        rows = []
        for vid in ids:
            g = catalog[vid]
            newest = g["versions"][0]
            name = names[vid]
            here = "exists" if vid in live else "deleted"
            rows.append((vid, f"{vid:>6}  {g['type']:<4}  {name:<22.22}  {here:<7}  {len(g['versions']):>3}  "
                              f"{ts_text(newest['ts'])}  {human(newest['size']):>10}"))
        header = f"{'VMID':>6}  {'type':<4}  {'name':<22}  {'here':<7}  {'ver':>3}  {'newest':<16}  {'size':>10}"
        if args.list:
            print(f"\n  {'':>3}  {header}")
            for _, text in rows:
                print(f"  {'':>3}  {text}")
            return 0
        vmid = choose(f"Guests with backups in {bucket}/{path}:", rows, header)
        if not vmid:
            return 0
    if vmid not in catalog:
        raise Fatal(f"no backups of guest {vmid} in {bucket}/{path}")
    g = catalog[vmid]

    # 2. version
    src_dir = dump_dir(env)
    if args.version:
        version = pick_version(g["versions"], args.version)
    else:
        rows = []
        for v in g["versions"]:
            local = src_dir / v["archive"]
            here = "yes" if local.exists() and local.stat().st_size == v["size"] else ""
            rows.append((v["ts"], f"{ts_text(v['ts'])}  {v['cls']:<8}  {human(v['size']):>10}  {here:<5}"))
        ts = choose(f"Backups of {g['type']} {vmid}:", rows, f"{'date':<16}  {'folder':<8}  {'size':>10}  local")
        if not ts:
            return 0
        version = next(v for v in g["versions"] if v["ts"] == ts)
    print(f"\nSelected: {version['archive']} ({human(version['size'])}, {version['cls']}/)")

    # 3. action
    exists = vmid in live
    if args.download_only:
        action = "download"
    elif args.overwrite:
        action = "overwrite"
    elif args.new_id or args.storage:
        action = "new"
    else:
        rows = [("download", "Download only (shows under the storage's Backups in the GUI)"),
                ("new", "Restore as a NEW guest (original is not touched)")]
        if exists:
            rows.append(("overwrite", f"Restore OVER existing guest {vmid} ({live[vmid]['name']}, "
                                      f"{live[vmid]['status']}) - must be stopped"))
        action = choose("What now?", rows, "action")
        if not action:
            return 0

    archive = download(env, bucket, path, version, src_dir)
    if action == "download":
        print(f"\nDone. Restore later from the GUI, or:\n  {restore_cmd_text(g['type'], '<new-id>', archive, '<storage>')}")
        return 0

    node = local_node()
    if action == "overwrite":
        if not exists:
            raise Fatal(f"guest {vmid} does not exist here - use a new guest instead")
        status = live[vmid]["status"]
        if status != "stopped":
            raise Fatal(f"guest {vmid} is {status}; stop it first ({'pct' if g['type'] == 'lxc' else 'qm'} stop {vmid})")
        target, unique = vmid, False
        print(f"\nThis REPLACES guest {vmid} ({live[vmid]['name']}) and its disks with the backup from "
              f"{ts_text(version['ts'])}. This cannot be undone.")
        if args.yes:
            pass
        elif ask("Type the VMID again to confirm") != vmid:
            print("Not confirmed - nothing restored.")
            return 1
    else:
        nxt = str(pvesh("/cluster/nextid"))
        target = args.new_id or (nxt if args.yes else ask("New VMID", nxt, r"\d+"))
        if target in live:
            raise Fatal(f"VMID {target} is already in use")
        unique = args.unique if args.unique is not None else exists
    stores = restore_storages(node, g["type"])
    if not stores:
        raise Fatal("no active storage for guest disks on this node")
    storage = args.storage
    if not storage:
        if args.yes:
            storage = stores[0][0]
        else:
            storage = choose("Restore disks to which storage?",
                             [(s, f"{s:<16} {human(a):>10} free") for s, a in stores], f"{'storage':<16} {'free':>10}")
            if not storage:
                return 0
    cmd = restore_cmd(g["type"], target, archive, storage, unique, action == "overwrite")
    print(f"\nWill run:\n  {' '.join(shlex.quote(c) for c in cmd)}")
    if unique:
        print("  (--unique: new MAC addresses, so it can run next to the original)")
    if not args.yes and not ask_yes("Run it now?"):
        print("Nothing restored. The downloaded backup stays in", src_dir)
        return 0
    if subprocess.run(cmd).returncode != 0:
        raise Fatal("restore failed - see the output above")
    print(f"\nRestored as {g['type']} {target}.")
    start = args.start or (not args.yes and ask_yes(f"Start {target} now?"))
    if start:
        run(["pct" if g["type"] == "lxc" else "qm", "start", target], capture=False)
    print(f"The downloaded backup stays in {src_dir} (normal backup-job retention applies).")
    return 0


def restore_cmd(gtype, target, archive, storage, unique, force):
    if gtype == "lxc":
        cmd = ["pct", "restore", str(target), str(archive), "--storage", storage]
    else:
        cmd = ["qmrestore", str(archive), str(target), "--storage", storage]
    if unique:
        cmd += ["--unique", "1"]
    if force:
        cmd += ["--force", "1"]
    return cmd


def restore_cmd_text(gtype, target, archive, storage):
    return " ".join(restore_cmd(gtype, target, archive, storage, False, False))


# ------------------------------------------------------------------------------
# command line
# ------------------------------------------------------------------------------

def main(argv=None):
    # line-buffered so our messages stay in order with rclone/pct output when piped or logged
    sys.stdout.reconfigure(line_buffering=True)
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in HOOK_PHASES:
        return do_hook(argv[0])

    ap = argparse.ArgumentParser(prog="pve-r2-backup", description="Proxmox vzdump backups to Cloudflare R2")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("install", help="install rclone, the vzdump hook and weekly timer")
    sub.add_parser("config", help="set R2 credentials and this host's path, test access")
    s = sub.add_parser("sync", help="upload backups")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--since", type=int, metavar="DAYS", help="files changed in the last DAYS days (default 2)")
    g.add_argument("--latest", action="store_true", help="newest good backup of every guest")
    g.add_argument("--all", action="store_true", help="every backup")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.add_argument("--wait", action="store_true", help="wait for a running upload instead of skipping")
    r = sub.add_parser("restore", help="pick a backup in R2, download and/or restore it")
    r.add_argument("vmid", nargs="?", help="guest ID (skip the guest menu)")
    r.add_argument("version", nargs="?", help="latest | list number | date prefix e.g. 2026_10_18")
    r.add_argument("--list", action="store_true", help="only list guests with backups in R2")
    r.add_argument("--path", help="browse another host's R2 path, e.g. my-bucket/backups/pve/site2")
    r.add_argument("--download-only", action="store_true")
    r.add_argument("--new-id", help="restore as this new VMID")
    r.add_argument("--overwrite", action="store_true", help="restore over the existing (stopped) guest")
    r.add_argument("--storage", help="target storage for the guest's disks")
    r.add_argument("--unique", dest="unique", action="store_true", default=None, help="new MAC addresses")
    r.add_argument("--no-unique", dest="unique", action="store_false")
    r.add_argument("--start", action="store_true", help="start the guest after restore")
    r.add_argument("-y", "--yes", action="store_true", help="no questions (uses defaults)")
    h = sub.add_parser("hostconfig", help="encrypted backup of this host's configuration; list / get")
    h.add_argument("action", nargs="?", choices=["backup", "list", "get"], default="backup")
    h.add_argument("version", nargs="?", help="for get: latest | list number | date prefix")
    h.add_argument("--to", help="for get: folder to unpack into (default /root/hostconfig-<host>-<date>)")
    h.add_argument("--path", help="another host's R2 path, e.g. my-bucket/backups/pve/site2")
    h.add_argument("-n", "--dry-run", action="store_true", help="build the archive but don't upload")
    h.add_argument("--if-configured", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("status", help="config, last result, hook, timer and what is in R2")
    sub.add_parser("weekly", help="catch-up sync + summary email (run by the timer)")
    sub.add_parser("notify-test", help="send a test email")
    sub.add_parser("uninstall", help="remove hook and timer (keeps config and R2 data)")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "install":
            do_install()
        elif a.cmd == "config":
            do_config()
        elif a.cmd == "sync":
            need_root()
            mode = "latest" if a.latest else "all" if a.all else "since"
            return do_sync(mode, a.since or 2, a.dry_run, 7200 if a.wait else 0)
        elif a.cmd == "restore":
            return do_restore(a)
        elif a.cmd == "hostconfig":
            return do_hostconfig_cmd(a)
        elif a.cmd == "status":
            do_status()
        elif a.cmd == "weekly":
            need_root()
            return do_weekly()
        elif a.cmd == "notify-test":
            do_notify_test()
        elif a.cmd == "uninstall":
            do_uninstall()
        else:
            ap.print_help()
            return 1
    except Fatal as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
