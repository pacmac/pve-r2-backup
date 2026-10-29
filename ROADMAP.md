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

## Ideas

- **Central dashboard ("mini backup server")**: one page for all hosts: last upload
  and result per host, host config backups, STALE warnings, size and cost per bucket,
  a browser of what is in R2 (per guest and date, with the date the lifecycle rule will
  delete it) and a read-only view of each host's settings and backup jobs. Runs on a
  separate small machine/container with a read-only R2 token; write actions (restore,
  config) only later, if at all.
- **Pull model**: a separate machine fetches new backups from the hosts and uploads
  them, so the Proxmox hosts hold no R2 credentials and a compromised host cannot
  touch the off-site copies.
- **Compare with PBS 4 + S3 datastore** (technology preview): check Cloudflare R2
  compatibility, local cache needs and cost; it would add incremental/deduplicated
  backups and GUI restore, which this tool cannot do.

- **MQTT status** for Home Assistant: publish a retained JSON status
  (result, time, files, bytes) to `pve-r2/<site>/status`, so an outside system
  can alert when uploads stop even if the host itself is down.
- **Upload speed setting** in `r2.env` (parallel files / parts), for hosts with
  more RAM or a faster uplink than the defaults assume.
- **Proxmox backup-provider plugin** (long term, separate project): a Perl
  storage plugin implementing Proxmox's backup provider API
  (`new_backup_provider()`), so R2 appears as a backup storage in the GUI and
  backup/restore use the normal Proxmox buttons. Waits for that API to settle.
