#!/bin/bash
# shellcheck shell=bash
# ==============================================================================
# File: pve-r2-backup.sh
# Description: Copy Proxmox vzdump backups to Cloudflare R2 using rclone.
# Project: https://github.com/pacmac/pve-r2-backup   License: MIT
#
# Usage:
#   pve-r2-backup.sh install            install rclone + jq, hook, timer, env template
#   pve-r2-backup.sh config             set R2 credentials, write rclone config, test
#   pve-r2-backup.sh sync [MODE] [-n]   upload backups (MODE: --latest | --all | --since DAYS, default --since 2)
#   pve-r2-backup.sh status             show config, last result, hook, timer and what is in R2
#   pve-r2-backup.sh weekly             catch-up sync + summary email (run by the timer)
#   pve-r2-backup.sh notify-test        send a test email through the Proxmox notification system
#   pve-r2-backup.sh uninstall          remove hook and timer (keeps config + R2 data)
#
# Layout in R2:  <bucket>/<path>/<class>/<vzdump files>
#   e.g. my-bucket/backups/pve/site1/weekly/   (bucket = R2_BUCKET, folder = R2_PATH, unique per host)
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
# ==============================================================================

set -o pipefail

readonly SELF_INSTALLED="/usr/local/sbin/pve-r2-backup"
# credentials (r2.env) and generated rclone.conf live in root's home, mode 700/600
readonly CONF_DIR="/root/.pve-r2"
readonly ENV_FILE="${CONF_DIR}/r2.env"
readonly RCLONE_CONF="${CONF_DIR}/rclone.conf"
readonly LOG_FILE="/var/log/pve-r2-backup.log"
readonly LOCK_FILE="/run/pve-r2-backup.lock"
readonly VZDUMP_CONF="/etc/vzdump.conf"
readonly UNIT_NAME="pve-r2-backup"
readonly STATE_FILE="/var/lib/pve-r2-backup/state"

# rclone tuning for a fast (>1 Gbit) uplink; R2 allows max 10000 parts per object
readonly RCLONE_OPTS=(--s3-chunk-size 64M --s3-upload-concurrency 4 --transfers 2 --retries 5 --low-level-retries 10 --log-level INFO --stats 1m --stats-one-line --stats-log-level NOTICE)

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG_FILE" >&2; }
# inside a real sync, a fatal error is also recorded and mailed
die() {
    echo "[ERROR] $*" >&2
    if [[ -n "${IN_SYNC:-}" ]]; then
        log "ERROR: $*"
        save_state "FAILED" "$*"
        printf 'Upload aborted before completion:\n\n  %s\n\nLast log lines:\n%s\n' "$*" "$(tail -n 20 "$LOG_FILE")" | notify "FAILED - $*"
    fi
    exit 1
}

site_name() { echo "${R2_PATH##*/}"; }
human() { numfmt --to=iec-i --suffix=B "${1:-0}" 2>/dev/null || echo "${1:-0} B"; }

### Function: notify
# Mail stdin to root with a "[pve-r2] <site>: <subject>" subject line. Root mail
# goes through /root/.forward -> proxmox-mail-forward -> Proxmox notification targets.
notify() {
    local subject="$1"
    { printf 'Subject: [pve-r2] %s: %s\n' "$(site_name)" "$subject"
      printf 'X-PVE-R2-Host: %s\n\n' "$(hostname -f)"
      cat
      printf '\n-- \n%s on %s, %s\n' "$SELF_INSTALLED" "$(hostname -f)" "$(date '+%F %T %Z')"
    } | sendmail root || log "WARNING: could not hand report to sendmail"
}

### Function: save_state
# Record the outcome of a run. $1=OK|FAILED, $2=summary line
save_state() {
    mkdir -p "$(dirname "$STATE_FILE")"
    local last_ok=""
    [[ -f "$STATE_FILE" ]] && last_ok=$(awk -F= '$1=="last_success"{print $2}' "$STATE_FILE")
    [[ "$1" == "OK" ]] && last_ok=$(date +%s)
    {
        echo "last_run=$(date +%s)"
        echo "last_result=$1"
        echo "last_summary=${2//$'\n'/ }"
        echo "last_success=${last_ok}"
    } >"${STATE_FILE}.new" && mv "${STATE_FILE}.new" "$STATE_FILE"
}

state_get() { awk -F= -v k="$1" '$1==k{sub(/^[^=]*=/,""); print}' "$STATE_FILE" 2>/dev/null; }

### Function: age_text
# "3d 4h ago" for an epoch value, or "never"
age_text() {
    [[ -z "$1" ]] && { echo "never"; return; }
    local s=$(( $(date +%s) - $1 ))
    printf '%s (%dd %dh ago)' "$(date -d "@$1" '+%F %T')" $((s/86400)) $((s%86400/3600))
}

need_root() { [[ $EUID -eq 0 ]] || die "Run as root"; }

### Function: load_env
# Source the per-host settings file, applying defaults.
load_env() {
    [[ -f "$ENV_FILE" ]] || die "Missing $ENV_FILE - run: $0 install && $0 config"
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    R2_STORAGE="${R2_STORAGE:-backups}"
    # older configs used R2_BASE_PATH + R2_HOST_PREFIX
    if [[ -z "${R2_PATH:-}" && -n "${R2_HOST_PREFIX:-}" ]]; then
        R2_PATH="${R2_BASE_PATH:+${R2_BASE_PATH}/}${R2_HOST_PREFIX}"
    fi
    R2_PATH="${R2_PATH#/}"; R2_PATH="${R2_PATH%/}"
}

crypt_enabled() { grep -q '^\[r2crypt\]' "$RCLONE_CONF" 2>/dev/null; }

### Function: remote_path
# rclone path for a folder in the bucket ($1), via the crypt wrapper if configured.
remote_path() {
    if crypt_enabled; then
        echo "r2crypt:$1"
    else
        echo "r2:${R2_BUCKET}/$1"
    fi
}

rc() { rclone --config "$RCLONE_CONF" "$@"; }

### Function: dump_dir
# Resolve the dump directory of the configured Proxmox storage.
dump_dir() {
    local path
    path=$(pvesh get "/storage/${R2_STORAGE}" --output-format json 2>/dev/null | jq -r '.path // empty')
    [[ -n "$path" && -d "${path}/dump" ]] || die "Storage '${R2_STORAGE}' has no dump directory (path: ${path:-unknown})"
    echo "${path}/dump"
}

### Function: vmid_classes
# Print "vmid class" for every guest in an enabled backup job.
# Jobs with a monthly-or-rarer schedule give 'monthly', everything else 'weekly'.
# A guest in both kinds of job is classed 'weekly'.
vmid_classes() {
    local all_ids
    local rare='(monthly|yearly|annually|quarterly|semiannually|[0-9*]+-[0-9*]+-[0-9])'
    all_ids=$(pvesh get /cluster/resources --type vm --output-format json | jq -r '.[].vmid')
    pvesh get /cluster/backup --output-format json | jq -r '.[] | select((.enabled // 1) != 0)
        | [(.schedule // ""), (.vmid // ""), ((.all // 0) | tostring), (.exclude // "")] | @tsv' |
    while IFS=$'\t' read -r schedule vmids all exclude; do
        local class=weekly
        [[ "$schedule" =~ $rare ]] && class=monthly
        if [[ "$all" == "1" || "$all" == "true" ]]; then
            vmids=$(comm -23 <(tr ' ' '\n' <<<"$all_ids" | sort -u) <(tr ',' '\n' <<<"$exclude" | sort -u) | paste -sd,)
        fi
        tr ',' '\n' <<<"$vmids" | grep -E '^[0-9]+$' | sed "s/\$/ ${class}/"
    done | sort -k1,1n -k2,2r | awk '!seen[$1]++'   # -r puts 'weekly' before 'monthly'
}

### Function: select_files
# List backup file names in the dump dir for the given mode.
#   --all        every backup
#   --latest     newest backup of each guest (archive + log + notes)
#   --since N    files modified in the last N days
select_files() {
    local dir="$1" mode="$2" days="$3"
    local pattern='^vzdump-(qemu|lxc)-[0-9]+-[0-9_]+-[0-9_]+\.'
    names() { find "$dir" -maxdepth 1 -type f -printf '%f\n' | grep -E "$pattern"; }
    case "$mode" in
        --all)   names ;;
        --since) find "$dir" -maxdepth 1 -type f -mtime "-${days}" -printf '%f\n' | grep -E "$pattern" ;;
        --latest)
            # group key = vzdump-<type>-<vmid>-<date>-<time>; keep the newest key per vmid,
            # comparing by date/time (a vmid can change between qemu and lxc over time)
            local keys
            # only backups that produced an archive count (failed runs leave just a .log)
            keys=$(names | grep -vE '\.(log|notes|protected)$' | grep -oE '^vzdump-(qemu|lxc)-[0-9]+-[0-9_]+-[0-9_]+' |
                   sort -u | awk -F- '{print $3" "$4"-"$5" "$0}' | sort -k1,1n -k2,2 |
                   awk '{last[$1]=$3} END{for(v in last) print last[v]}')
            names | grep -F -f <(sed 's/$/./' <<<"$keys")
            ;;
    esac | grep -vE '\.(dat|tmp)$' | sort
}

### Function: do_sync
# Upload selected backups, one rclone copy per class.
do_sync() {
    local mode="--since" days=2 dry=() wait=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --all|--latest) mode="$1"; days="" ;;
            --wait) wait=7200 ;;
            --since) mode="--since"; days="${2:?--since needs DAYS}"; shift ;;
            -n|--dry-run) dry=(--dry-run) ;;
            *) die "Unknown sync option: $1" ;;
        esac
        shift
    done
    load_env
    [[ ${#dry[@]} -eq 0 ]] && IN_SYNC=1

    # hook-triggered runs (--wait) queue behind a running upload instead of skipping
    exec 9>"$LOCK_FILE"
    flock -w "$wait" 9 || { log "Another sync is running - skipped"; return 0; }

    local dir classes tmp
    dir=$(dump_dir) || exit 1   # dump_dir's die already logged and mailed
    classes=$(vmid_classes)
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' RETURN

    select_files "$dir" "$mode" "$days" | while read -r f; do
        local vmid class
        vmid=$(cut -d- -f3 <<<"$f")
        class=$(awk -v v="$vmid" '$1==v{print $2}' <<<"$classes")
        echo "$f" >>"${tmp}/${class:-other}"
    done

    local rcode=0 list count class out copied bytes total_files=0 total_bytes=0 report=""
    for list in "$tmp"/*; do
        [[ -f "$list" ]] || continue
        class=$(basename "$list")
        count=$(wc -l <"$list")
        local dest
        dest=$(remote_path "${R2_PATH}/${class}")
        out="${tmp}/out.${class}"
        log "Uploading ${count} file(s) ${mode} ${days:+(${days}d) }to ${dest} ${dry[*]}"
        local status=OK
        if ! rc copy "$dir" "$dest" --files-from "$list" --no-traverse "${RCLONE_OPTS[@]}" "${dry[@]}" 2>&1 | tee -a "$LOG_FILE" >"$out"; then
            log "Upload to ${dest} FAILED"
            status=FAILED
            rcode=1
        fi
        # files actually sent this run (rclone INFO lines), and their size
        # large files are logged as "Multi-thread Copied (new)"
        copied=$(grep -oE 'INFO  : [^:]+: (Multi-thread )?Copied \((new|replaced existing)\)' "$out" | sed -E 's/^INFO  : //; s/: (Multi-thread )?Copied.*//')
        bytes=0
        if [[ -n "$copied" ]]; then
            bytes=$(cd "$dir" && tr '\n' '\0' <<<"$copied" | xargs -0 stat -c%s 2>/dev/null | awk '{t+=$1} END{print t+0}')
        fi
        local n=0; [[ -n "$copied" ]] && n=$(wc -l <<<"$copied")
        total_files=$((total_files + n)); total_bytes=$((total_bytes + bytes))
        report+=$(printf '%-8s %-6s %3d selected, %3d uploaded (%s)' "$class" "$status" "$count" "$n" "$(human "$bytes")")$'\n'
        [[ "$status" == FAILED ]] && report+="$(grep -E 'ERROR|Failed' "$out" | tail -n 5 | sed 's/^/           /')"$'\n'
    done
    [[ -z "$report" ]] && report="nothing selected (${mode} ${days:+${days}d})"$'\n'

    [[ ${#dry[@]} -gt 0 ]] && return $rcode
    local summary
    summary="${total_files} file(s), $(human "$total_bytes") uploaded"
    if [[ $rcode -eq 0 ]]; then
        log "Sync finished OK - ${summary}"
        save_state OK "$summary"
        [[ "${R2_NOTIFY:-failures}" == "always" ]] &&
            printf 'Upload %s to %s/%s finished OK.\n\n%s' "$mode" "$R2_BUCKET" "$R2_PATH" "$report" | notify "OK - ${summary}"
    else
        save_state FAILED "$summary"
        printf 'Upload %s to %s/%s had errors.\n\n%s\nLog: %s\n' "$mode" "$R2_BUCKET" "$R2_PATH" "$report" "$LOG_FILE" | notify "FAILED - upload errors (${summary})"
    fi
    IN_SYNC=""
    return $rcode
}

### Function: do_weekly
# Timer job: catch-up upload of the last 14 days, then a summary mail that
# warns when the last successful upload is older than R2_STALE_DAYS.
do_weekly() {
    load_env
    local rcode=0
    do_sync --since 14 || rcode=$?
    local last_ok stale_days="${R2_STALE_DAYS:-8}" subject body class
    last_ok=$(state_get last_success)
    if [[ -z "$last_ok" || $(( $(date +%s) - last_ok )) -gt $(( stale_days * 86400 )) ]]; then
        subject="STALE - no successful upload for more than ${stale_days} days"
    elif [[ $rcode -ne 0 ]]; then
        subject="weekly: catch-up upload FAILED"
    else
        subject="weekly OK"
    fi
    body="Last successful upload : $(age_text "$last_ok")"$'\n'
    body+="Last run               : $(age_text "$(state_get last_run)") - $(state_get last_result): $(state_get last_summary)"$'\n'
    body+="Local backups          : $(pvesm list "$R2_STORAGE" --content backup 2>/dev/null | awk 'NR>1{n++; t+=$4} END{printf "%d, %.1f GiB", n, t/2^30}')"$'\n\n'
    body+="In R2 (${R2_BUCKET}/${R2_PATH}):"$'\n'
    for class in weekly monthly other; do
        body+="  $(printf '%-8s' "$class") $(rc size "$(remote_path "${R2_PATH}/${class}")" 2>&1 | tr '\n' ' ')"$'\n'
    done
    printf '%s' "$body" | notify "$subject"
    return $rcode
}

### Function: do_notify_test
do_notify_test() {
    load_env
    printf 'Test from pve-r2-backup on %s.\nIf you can read this, upload reports from this host will reach you.\n' "$(hostname -f)" | notify "notification test"
    echo "Test mail handed to sendmail -> root -> Proxmox notification targets. Check your inbox."
    echo "Nothing arriving? Check: Datacenter > Notifications (target + default-matcher), and 'mailq' / journalctl -t postfix/smtp"
}

### Function: do_hook
# vzdump hook entry point (args: phase mode vmid). Never fails the backup.
do_hook() {
    local phase="$1"
    if [[ "$phase" == "job-end" && -f "$ENV_FILE" ]]; then
        # shellcheck source=/dev/null
        source "$ENV_FILE"
        # only react to jobs that wrote to our storage
        if [[ -z "${STOREID:-}" || "${STOREID}" == "${R2_STORAGE:-backups}" ]]; then
            setsid nohup "$SELF_INSTALLED" sync --since 2 --wait </dev/null >>"$LOG_FILE" 2>&1 &
        fi
    fi
    exit 0
}

### Function: do_install
# Install packages, this script, the env template, vzdump hook and weekly timer.
do_install() {
    need_root
    command -v pveversion >/dev/null || die "Not a Proxmox VE host"

    if ! command -v jq >/dev/null; then
        echo "Installing jq..."
        apt-get update -qq && apt-get install -y -qq jq || die "jq install failed"
    fi
    if ! command -v rclone >/dev/null || ! rclone version 2>/dev/null | grep -q 'rclone v1\.\(6[5-9]\|[7-9][0-9]\)'; then
        echo "Installing current rclone from rclone.org..."
        local deb arch
        arch=$(dpkg --print-architecture)
        deb=$(mktemp --suffix=.deb)
        curl -fsSL "https://downloads.rclone.org/rclone-current-linux-${arch}.deb" -o "$deb" || die "rclone download failed"
        dpkg -i "$deb" || die "rclone install failed"
        rm -f "$deb"
    fi
    echo "rclone: $(rclone version | head -1)"

    [[ "$(readlink -f "$0")" != "$SELF_INSTALLED" ]] && install -m 755 "$(readlink -f "$0")" "$SELF_INSTALLED"
    install -d -m 700 "$CONF_DIR"
    if [[ ! -f "$ENV_FILE" ]]; then
        cat >"$ENV_FILE" <<'EOF'
# Cloudflare R2 settings for pve-r2-backup (keep chmod 600)
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
# Full R2 path for THIS host: <bucket>/<folder...>, unique per host, e.g. my-bucket/backups/pve/site1
# (stored split into bucket + folder)
R2_BUCKET=
R2_PATH=
# Proxmox storage whose dump/ directory is uploaded
R2_STORAGE=backups
# Mail successful runs too (always) or only failures (failures); weekly summary is always sent
R2_NOTIFY=failures
# Weekly summary flags STALE when the last successful upload is older than this
R2_STALE_DAYS=8
# Optional client-side encryption. Store these OUTSIDE this host too,
# without them the R2 copies cannot be restored.
R2_CRYPT_PASSWORD=
R2_CRYPT_SALT=
EOF
        chmod 600 "$ENV_FILE"
        echo "Created $ENV_FILE"
    fi

    # vzdump hook - refuse to replace a hook script that isn't ours
    touch "$VZDUMP_CONF"
    local current
    current=$(grep -E '^script:' "$VZDUMP_CONF" | awk '{print $2}')
    if [[ -z "$current" ]]; then
        echo "script: ${SELF_INSTALLED}" >>"$VZDUMP_CONF"
        echo "Added vzdump hook to $VZDUMP_CONF"
    elif [[ "$current" != "$SELF_INSTALLED" ]]; then
        echo "[WARN] $VZDUMP_CONF already uses hook '$current' - not changed. Uploads will only run from the timer." >&2
    fi

    cat >"/etc/systemd/system/${UNIT_NAME}.service" <<EOF
[Unit]
Description=Catch-up upload of vzdump backups to Cloudflare R2
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=${SELF_INSTALLED} weekly
Nice=10
IOSchedulingClass=idle
EOF
    cat >"/etc/systemd/system/${UNIT_NAME}.timer" <<EOF
[Unit]
Description=Weekly catch-up upload of vzdump backups to Cloudflare R2

[Timer]
OnCalendar=Mon *-*-* 06:00
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
EOF
    cat >"/etc/logrotate.d/${UNIT_NAME}" <<EOF
${LOG_FILE} {
    monthly
    rotate 6
    compress
    missingok
    notifempty
}
EOF
    systemctl daemon-reload
    systemctl enable --now "${UNIT_NAME}.timer" >/dev/null
    echo "Weekly catch-up timer enabled."
    echo
    echo "Next: ${SELF_INSTALLED} config"
}

### Function: prompt_var
# Ask for a value, showing the saved one as default (Enter keeps it).
# Input is visible so pasted values can be checked. Optional $3 = regex the
# value must match; on mismatch the prompt repeats (catches double pastes).
# $1=var name, $2=prompt, $3=regex
prompt_var() {
    local name="$1" text="$2" regex="${3:-}" value shown
    while true; do
        shown=""
        [[ -n "${!name}" ]] && shown=" [${!name}]"
        read -r -p "${text}${shown}: " value
        value="${value//[[:space:]]/}"
        [[ -z "$value" ]] && value="${!name}"
        if [[ -n "$regex" && -n "$value" && ! "$value" =~ $regex ]]; then
            echo "  -> invalid (${#value} chars) - expected ${text##*(}" >&2
            echo "     paste it once, then press Enter" >&2
            [[ "$value" == "${!name}" ]] && printf -v "$name" '%s' ""   # bad saved value: force re-entry
            continue
        fi
        printf -v "$name" '%s' "$value"
        return
    done
}

### Function: claim_site
# Make sure no other host already uploads to <base>/<site>/, then mark it ours.
# Uses /etc/machine-id so a renamed host keeps its site.
claim_site() {
    local marker="r2:${R2_BUCKET}/${R2_PATH}/.owner"
    local me owner
    me="$(cat /etc/machine-id) $(hostname -f)"
    owner=$(rc cat "$marker" 2>/dev/null | head -1)
    if [[ -n "$owner" && "${owner%% *}" != "${me%% *}" ]]; then
        die "R2 path '${R2_BUCKET}/${R2_PATH}/' already belongs to another host: ${owner#* }
       Pick a different path, or delete ${R2_BUCKET}/${R2_PATH}/.owner in R2 if that host is gone."
    fi
    [[ -z "$owner" ]] && { printf '%s\n' "$me" | rc rcat "$marker" || die "Could not write site marker"; }
    echo "R2 path ${R2_BUCKET}/${R2_PATH}/ belongs to this host"
}

### Function: do_config
# Ask for (or confirm) all settings, write rclone.conf, test access and claim the site folder.
# Safe to re-run: every prompt defaults to the saved value.
do_config() {
    need_root
    command -v rclone >/dev/null || die "rclone missing - run: $0 install"
    load_env

    echo "== Cloudflare R2 (same for all hosts) =="
    echo "Paste the bucket URL from Cloudflare, e.g. https://<account-id>.r2.cloudflarestorage.com/my-bucket"
    echo "(or just the account ID)."
    prompt_var R2_ACCOUNT_ID "Bucket URL or Account ID"
    # accept the S3 URL: account ID from the host name, optional bucket/path after it
    local url_path=""
    if [[ "$R2_ACCOUNT_ID" =~ ^(https?://)?([0-9a-fA-F]+)\.r2\.cloudflarestorage\.com/?(.*)$ ]]; then
        R2_ACCOUNT_ID="${BASH_REMATCH[2]}"
        url_path="${BASH_REMATCH[3]}"
    fi
    echo "From the R2 API token page (R2 Overview > API Tokens > Manage):"
    prompt_var R2_ACCESS_KEY_ID "Access Key ID (32 hex chars)" '^[0-9a-f]{32}$'
    prompt_var R2_SECRET_ACCESS_KEY "Secret Access Key (64 hex chars)" '^[0-9a-f]{64}$'
    [[ -n "$R2_ACCOUNT_ID" && -n "$R2_ACCESS_KEY_ID" && -n "$R2_SECRET_ACCESS_KEY" ]] || die "Account ID and both keys are required"
    [[ "$R2_ACCOUNT_ID" =~ ^[0-9a-f]{32}$ ]] || echo "[WARN] Account ID is usually 32 hex chars, got ${#R2_ACCOUNT_ID}"

    echo
    echo "== This host =="
    # one prompt for bucket + folder; default = saved value, or what came with the URL
    local saved_path="${R2_BUCKET:+${R2_BUCKET}/${R2_PATH}}" full_path
    url_path="${url_path%/}"
    if [[ "$url_path" == */* ]]; then
        full_path="$url_path"                 # URL included a folder
    elif [[ -n "$saved_path" && ( -z "$url_path" || "${saved_path%%/*}" == "$url_path" ) ]]; then
        full_path="$saved_path"               # same bucket as before: keep saved folder
    else
        full_path="$url_path"
    fi
    prompt_var full_path "Full R2 path for THIS host's backups, unique per host (e.g. my-bucket/backups/pve/site1)"
    full_path="${full_path#/}"; full_path="${full_path%/}"
    [[ "$full_path" == */* ]] || die "Give bucket AND folder, e.g. my-bucket/backups/pve/site1 - not just the bucket"
    [[ "$full_path" =~ ^[A-Za-z0-9._/-]+$ ]] || die "Path may only contain letters, digits, . _ - /"
    R2_BUCKET="${full_path%%/*}"
    R2_PATH="${full_path#*/}"
    echo "  Bucket : ${R2_BUCKET}"
    echo "  Folder : ${R2_PATH}/{weekly,monthly,other}/"
    echo "Proxmox storages with backups: $(pvesh get /storage --output-format json | jq -r '[.[] | select((.content // "") | test("backup")) | .storage] | join(", ")')"
    prompt_var R2_STORAGE "Storage to upload from"
    dump_dir >/dev/null

    if [[ -z "$R2_CRYPT_PASSWORD" ]]; then
        local ans
        read -r -p "Encrypt backups before upload? [y/N]: " ans
        if [[ "$ans" =~ ^[Yy] ]]; then
            prompt_var R2_CRYPT_PASSWORD "Encryption password"
            prompt_var R2_CRYPT_SALT "Second password / salt"
        fi
    else
        echo "Encryption: ON (passwords in $ENV_FILE)"
    fi

    # persist answers (values may contain any characters, so quote them)
    {
        echo "# Cloudflare R2 settings for pve-r2-backup (keep chmod 600)"
        local v
        for v in R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET R2_PATH R2_STORAGE R2_NOTIFY R2_STALE_DAYS R2_CRYPT_PASSWORD R2_CRYPT_SALT; do
            printf '%s=%q\n' "$v" "${!v}"
        done
    } >"${ENV_FILE}.new" && chmod 600 "${ENV_FILE}.new" && mv "${ENV_FILE}.new" "$ENV_FILE"

    umask 077
    {
        cat <<EOF
[r2]
type = s3
provider = Cloudflare
access_key_id = ${R2_ACCESS_KEY_ID}
secret_access_key = ${R2_SECRET_ACCESS_KEY}
endpoint = https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com
acl = private
no_check_bucket = true
EOF
        if [[ -n "$R2_CRYPT_PASSWORD" ]]; then
            cat <<EOF

[r2crypt]
type = crypt
remote = r2:${R2_BUCKET}
filename_encryption = standard
directory_name_encryption = false
password = $(printf '%s' "$R2_CRYPT_PASSWORD" | rclone obscure -)
EOF
            [[ -n "$R2_CRYPT_SALT" ]] && echo "password2 = $(printf '%s' "$R2_CRYPT_SALT" | rclone obscure -)"
        fi
    } >"$RCLONE_CONF"
    chmod 600 "$RCLONE_CONF"
    echo
    echo "Wrote $ENV_FILE and $RCLONE_CONF"

    echo "Testing bucket access..."
    rc lsf "r2:${R2_BUCKET}" --max-depth 1 >/dev/null || die "Cannot list bucket '${R2_BUCKET}' - check credentials/token permissions"
    # unique name so a bucket lock rule can't block re-running config
    local test_obj
    test_obj="${R2_PATH}/_test-$(date +%s).txt"
    printf 'pve-r2-backup test %s %s\n' "$(hostname -s)" "$(date -Is)" |
        rc rcat "r2:${R2_BUCKET}/${test_obj}" || die "Write test failed - token needs Object Read & Write"
    rc deletefile "r2:${R2_BUCKET}/${test_obj}" 2>/dev/null || echo "(test file ${test_obj} could not be deleted - fine if bucket lock is on)"
    echo "OK: bucket readable and writable"
    claim_site
    [[ -n "$R2_CRYPT_PASSWORD" ]] && echo "Encryption ON - store the passwords from $ENV_FILE somewhere off this host!"
    echo
    echo "Uploads go to: ${R2_BUCKET}/${R2_PATH}/{weekly,monthly,other}/"
    echo "Next: ${SELF_INSTALLED} sync --latest -n   (dry run), then without -n"
}

### Function: do_status
do_status() {
    load_env
    echo "R2 path     : ${R2_BUCKET}/${R2_PATH}/{weekly,monthly,other}/"
    echo "Bucket      : ${R2_BUCKET:-<not set>}  (encryption $(crypt_enabled && echo on || echo off))"
    echo "Storage     : ${R2_STORAGE} -> $(dump_dir)"
    echo "vzdump hook : $(grep -E '^script:' "$VZDUMP_CONF" 2>/dev/null || echo none)"
    echo "Timer       : $(systemctl is-enabled "${UNIT_NAME}.timer" 2>/dev/null) / next $(systemctl show "${UNIT_NAME}.timer" -p NextElapseUSecRealtime --value 2>/dev/null)"
    echo "Reports     : mail to root (${R2_NOTIFY:-failures} + weekly summary), stale after ${R2_STALE_DAYS:-8} days"
    echo "Last run    : $(age_text "$(state_get last_run)") - $(state_get last_result) $(state_get last_summary)"
    echo "Last success: $(age_text "$(state_get last_success)")"
    echo
    echo "Guest classes:"; vmid_classes | awk '{printf "  %-6s %s\n", $1, $2}'
    echo
    local class
    for class in weekly monthly other; do
        printf '%-8s ' "$class"; rc size "$(remote_path "${R2_PATH}/${class}")" 2>/dev/null | tr '\n' ' '; echo
    done
    echo
    echo "Last log lines:"; tail -n 5 "$LOG_FILE" 2>/dev/null
}

### Function: do_uninstall
do_uninstall() {
    need_root
    sed -i "\#^script: ${SELF_INSTALLED}\$#d" "$VZDUMP_CONF"
    systemctl disable --now "${UNIT_NAME}.timer" 2>/dev/null
    rm -f "/etc/systemd/system/${UNIT_NAME}.service" "/etc/systemd/system/${UNIT_NAME}.timer" "/etc/logrotate.d/${UNIT_NAME}"
    systemctl daemon-reload
    echo "Removed hook and timer. Kept $CONF_DIR, $SELF_INSTALLED and everything in R2."
}

case "${1:-}" in
    install)   do_install ;;
    config)    do_config ;;
    sync)      shift; need_root; do_sync "$@" ;;
    status)    do_status ;;
    weekly)    need_root; do_weekly ;;
    notify-test) do_notify_test ;;
    uninstall) do_uninstall ;;
    job-init|job-start|job-end|job-abort|backup-start|backup-end|backup-abort|log-end|pre-stop|pre-restart|post-restart)
               do_hook "$@" ;;
    *) sed -n '5,16p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
