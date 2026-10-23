# Changelog

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
