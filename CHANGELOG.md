# Changelog

## 2.1.0 - 2026-10-29

- New `hostconfig` command: daily encrypted (gpg AES-256) backup of the host's own
  configuration to `<path>/host/`: `/etc` incl. `/etc/pve`, a consistent copy of
  `config.db`, an inventory of disks/LVM/ZFS/network/guests/packages, root's SSH keys,
  `/usr/local`, cron and optional extra folders (`R2_HOSTCONFIG_EXTRA`, comma separated).
  `hostconfig list` / `hostconfig get` download, decrypt and unpack one; the archive
  contains `RESTORE.txt` with rebuild steps. `config` asks for the passphrase and folders;
  `install` adds a daily timer (03:30). Failures are mailed; the weekly summary warns
  when the last one is older than 3 days.
- Every upload is now checked in R2: each selected file must exist there with the same
  size. rclone could report success after dropping a file that disappeared during the
  upload. Missing files fail the run; backups deleted locally before upload are reported
  (and mailed).

## 2.0.3 - 2026-10-29

- `config` no longer suggests a storage called `backups` that may not exist (and then
  stopped with `storage 'backups' does not exist`). It now suggests the storage your
  backup jobs write to, explains the question, lists only enabled storages, and asks
  again on a wrong name.
- `config` saves the credentials and path as soon as they are entered, so an error
  in a later step doesn't lose them.
- The vzdump hook does nothing until `config` has been completed.

## 2.0.2 - 2026-10-23

- Fixed `install` failing with `HTTP Error 403` when downloading rclone: downloads.rclone.org
  rejects Python's default User-Agent. Downloads now identify as `pve-r2-backup/<version>`,
  and network errors give a clear message instead of a traceback.

## 2.0.1 - 2026-10-23

- Messages stay in order with rclone/pct output when the output is piped or logged.
- Verified end to end on Proxmox VE 9.2: a container that only existed in R2 was
  downloaded, checked and restored with `restore 117 latest --new-id 9100`.

## 2.0.0 - 2026-10-23

Rewritten in Python (3.11+, standard library only); same commands, config files and R2 layout,
so existing installs upgrade by running `install` with the new file.

- New `restore` command: menu of guests in R2 -> dated versions -> download only, restore as a
  new guest, or restore over the existing (stopped) guest. Also usable without menus.
- Restore downloads use 16 parallel streams, are checksum-verified and archive-tested (`zstd -t`).
- `restore --path` browses another host's backups (disaster recovery on a new host).
- `jq` is no longer needed.
- Unit tests for guest classification, `--latest` selection, upload counting and `r2.env` parsing.
- Fixed: log lines written twice during background uploads.

## 1.0.0 - 2026-10-23

First release.

- Upload vzdump backups to Cloudflare R2 after each backup job (vzdump hook) and weekly (systemd timer)
- `rclone copy` only: nothing in R2 is ever deleted by the script
- Retention classes `weekly` / `monthly` / `other` from the backup job schedules
- One bucket for several hosts, with a per-path `.owner` marker
- `config` with visible, validated prompts and saved values as defaults; accepts the R2 bucket URL
- Email reports through Proxmox notifications: failures straight away, weekly summary, STALE warning
- `--latest` picks the newest backup that has an archive (skips failed runs)
- Optional rclone crypt encryption with readable folder names
