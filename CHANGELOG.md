# Changelog

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
