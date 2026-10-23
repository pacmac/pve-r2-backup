# Roadmap

## Planned

### Scheduled restore test (`verify`)

Monthly timer: download one random backup from R2, compare it with the local
copy (or its stored checksum when the local one was pruned), test the archive,
and report the result in the email. Proves every month that backups can be
pulled back, not just that they were uploaded.

### Debian package

Ship as `pve-r2-backup_<version>_all.deb` (GitHub releases, later an APT
repository): installs the script, vzdump hook, timers, logrotate, man page and
shell completion; `apt remove` cleans up. Credentials in `/root/.pve-r2/` are
left alone by upgrades.

### Other S3-compatible storage providers

rclone already speaks to almost every S3 service, so most of the tool is
provider-neutral. Support for e.g. **AWS S3, Backblaze B2, Wasabi, Akamai/Linode
Object Storage, Hetzner, OVHcloud, Scaleway, IDrive e2** and self-hosted
**MinIO / Garage** needs:

- `config`: choose the provider first, then ask only what it needs (endpoint,
  region, access key / secret). rclone's provider presets (`provider = AWS`,
  `Wasabi`, `Minio`, …) handle most quirks.
- Settings renamed from `R2_*` to provider-neutral names (`S3_*`), still
  reading old `r2.env` files so existing installs keep working.
- Provider notes in the README: **egress fees** (R2 has none; most others charge
  for downloads, which matters for restores and `verify`), how to set lifecycle
  rules, object lock / immutability, and storage classes to avoid for backups
  (e.g. archive tiers with retrieval delays).
- Per-provider upload limits: part size and maximum parts per object, so large
  `vma.zst` files always fit.
- Probably a new name (e.g. `pve-s3-backup`), with `pve-r2-backup` kept as an alias.

### Host configuration backup (`hostconfig`)

vzdump only backs up guests. The host's own configuration (storage, backup
jobs, users, firewall, networking, notification targets, …) is not saved
anywhere. On a single node it is only a few MB, so a daily copy is cheap.

**Contents** of one archive:

| What | Why |
|---|---|
| `/var/lib/pve-cluster/config.db`, copied with `sqlite3 .backup` | Real source of `/etc/pve`; restoring it on a reinstalled host brings back most of the Proxmox configuration |
| `/etc/pve` as plain files | Readable, so single files (e.g. one guest's `.conf`) can be taken out without a full restore |
| `/etc` | Network, `fstab`, GRUB, `modprobe.d` (e.g. `vfio.conf`), SSH host keys, cron, Samba, UPS, custom systemd units |
| `/root/.pve-r2`, `/root/.ssh`, `/usr/local` | This tool's R2 setup, root's SSH keys, local scripts |
| Inventory text file | `pveversion -v`, `zpool status` / `zfs list`, LVM layout, `lsblk`, IP addresses, `pvesm status`, guest list, installed packages: how to rebuild disks and pools |

**Design:**

- New command `pve-r2-backup hostconfig`: build `tar.zst`, encrypt, upload to
  `<path>/host/<hostname>-config-<date>.tar.zst.gpg`.
- **Always encrypted** (`gpg --symmetric`, AES-256), even when guest backups
  are not: the archive holds the Proxmox CA key, SSH host keys, SMTP and R2
  credentials. The passphrase is asked for once in `config`, stored in
  `r2.env`, and must also be kept off the host (password manager).
- Daily systemd timer; failures mailed like uploads; the weekly summary shows
  the date of the last config backup and warns if it is older than a few days.
- README section on restore: decrypt, extract single files, restore
  `config.db` on a reinstalled host (`systemctl stop pve-cluster`, replace,
  start).
- Suggested R2 lifecycle rule: `<path>/host/` after 365 days.

## Ideas

- **MQTT status** for Home Assistant: publish a retained JSON status
  (result, time, files, bytes) to `pve-r2/<site>/status`, so an outside system
  can alert when uploads stop even if the host itself is down.
- **Upload speed setting** in `r2.env` (parallel files / parts), for hosts with
  more RAM or a faster uplink than the defaults assume.
- **Proxmox backup-provider plugin** (long term, separate project): a Perl
  storage plugin implementing Proxmox's backup provider API
  (`new_backup_provider()`), so R2 appears as a backup storage in the GUI and
  backup/restore use the normal Proxmox buttons. Waits for that API to settle.
