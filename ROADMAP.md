# Roadmap

## Planned

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
- **`restore` helper**: list backups of a guest in R2 and download one into a
  dump directory in a single step.
