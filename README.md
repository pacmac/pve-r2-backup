# pve-r2-backup

Off-site copies of Proxmox VE `vzdump` backups in **Cloudflare R2**, an
interactive **restore** that pulls them back, and email reports through
Proxmox's own notification system.

No Proxmox Backup Server required. vzdump keeps writing to your local backup
storage as usual; after every backup job this script uploads the new archives
to R2 with [rclone](https://rclone.org).

## Features

- **Automatic upload** after every backup job (vzdump hook), plus a weekly
  catch-up timer for anything missed.
- **Never deletes from R2.** Uploads use `rclone copy`, not `sync`, so local
  pruning, a mistake or a compromised host cannot remove what's already off-site.
  Retention in R2 is done with R2 lifecycle rules.
- **Retention classes.** Each guest goes to `weekly/`, `monthly/` or `other/`,
  based on the schedule of the backup job it belongs to, so each class can
  have its own R2 lifecycle rule.
- **Several hosts, one bucket.** Each host has its own folder. A small `.owner`
  marker stops two hosts from writing to the same folder.
- **Reports by email.** A failed upload is mailed straight away. A weekly
  summary is also sent, flagged **STALE** if nothing has uploaded successfully
  for 8 days. Mail goes through Proxmox notification targets (SMTP, Gotify,
  webhook, …).
- **Restore from a menu:** pick a guest, then a dated version, then download
  only, restore as a new guest, or restore over the existing one. Downloads are
  checksum-verified and archive-tested before anything is restored.
- **Host configuration backup:** a daily, always-encrypted copy of the host's own
  configuration (`/etc` incl. `/etc/pve`, the Proxmox config database, root's SSH keys,
  `/usr/local`, cron, any extra folders) plus an inventory of disks, pools and network,
  so a lost host can be rebuilt. `hostconfig get` pulls one back.
- **Every upload is checked in R2:** after rclone finishes, each file is looked up in R2
  and compared by size. A missing file fails the run and is mailed; a backup deleted
  locally before it could be uploaded is reported.
- **Optional client-side encryption** (rclone crypt). Folder names stay
  readable so the lifecycle rules keep working.
- Safe to re-run: `install` and `config` can be run again at any time;
  `config` shows the saved values as defaults.

## Requirements

- Proxmox VE 8 or 9 (tested on 9.2), run as root. Uses the Python 3 that comes
  with Proxmox (standard library only, nothing extra to install).
- A Cloudflare R2 bucket and an **R2 API token** with *Object Read & Write*,
  ideally limited to that bucket.
- `install` adds the current `rclone` from rclone.org. The rclone in Debian's
  repositories is too old for R2.

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/pacmac/pve-r2-backup/main/pve-r2-backup.py -o /root/pve-r2-backup.py
python3 /root/pve-r2-backup.py install  # rclone, vzdump hook, weekly timer
pve-r2-backup config                    # R2 details + this host's folder, tests access
pve-r2-backup notify-test               # check that emails reach you
pve-r2-backup sync --latest -n          # dry run: what would be uploaded
pve-r2-backup sync --latest             # first real upload
```

### Getting the R2 values

In the Cloudflare dashboard go to **Storage & databases → R2 → Overview**,
then **API Tokens → Manage** (on the right of the page) → **Create API token**:

- Permission: **Object Read & Write**, *Apply to specific buckets only*: your bucket
- Copy the **Access Key ID** (32 hex chars) and **Secret Access Key** (64 hex chars).
  The secret is shown only once.
- The bucket's S3 URL `https://<account-id>.r2.cloudflarestorage.com/<bucket>`
  can be pasted straight into `config`.

`config` then asks for the **full R2 path for this host**, for example
`my-bucket/backups/pve/site1`. Each host needs its own path.

## Layout in R2

```
<bucket>/<path>/weekly/    guests in weekly (or more frequent) backup jobs
<bucket>/<path>/monthly/   guests in monthly / yearly jobs
<bucket>/<path>/other/     guests in no enabled job (e.g. deleted guests)
<bucket>/<path>/host/      host configuration backups (<hostname>-config-<date>.tar.zst.gpg)
<bucket>/<path>/.owner     which host owns this path
```

A guest that is in both a weekly and a monthly job counts as `weekly`.

## Retention

**Locally:** set `keep-last` (or keep-daily/weekly/…) on your backup jobs in
*Datacenter → Backup*. Proxmox prunes old backups when the job runs.

**In R2:** add lifecycle rules in the bucket's *Settings → Object lifecycle
rules*, one per prefix. For example:

| Prefix | Delete after |
|---|---|
| `backups/pve/site1/weekly/` | 90 days |
| `backups/pve/site1/monthly/` | 180 days |
| `backups/pve/site1/other/` | long, or never (may be the only copy of deleted guests) |
| `backups/pve/site1/host/` | 365 days (about 1 MB a day) |

R2 counts the age from the **upload** time, not from when the backup was made.

## Commands

| Command | What it does |
|---|---|
| `install` | Installs rclone, this script as `/usr/local/sbin/pve-r2-backup`, the vzdump hook (`script:` in `/etc/vzdump.conf`), a weekly timer (Mon 06:00), a daily host config timer (03:30) and logrotate |
| `config` | Asks for (or confirms) the R2 details, path and storage, then writes `/root/.pve-r2/{r2.env,rclone.conf}` (mode 600) and tests read/write |
| `sync [--since N \| --latest \| --all] [-n]` | Upload backups changed in the last N days (default 2), the newest good backup per guest, or everything. `-n` = dry run |
| `restore [VMID [VERSION]]` | Choose a guest and a version from R2, then download / restore (see below) |
| `hostconfig [-n]` | Encrypted backup of this host's configuration to `<path>/host/` (a daily timer runs it) |
| `hostconfig list` / `hostconfig get [VERSION] [--to DIR]` | List them, or download, decrypt and unpack one into a new folder (never over `/`) |
| `weekly` | Catch-up upload of the last 14 days plus the summary email (this is what the timer runs) |
| `status` | Config, last run/success, hook, timer, and what's in R2 |
| `notify-test` | Sends a test email |
| `uninstall` | Removes the hook, timer and logrotate entry. Keeps the config and everything in R2 |

Settings in `/root/.pve-r2/r2.env` that `config` doesn't ask for:

| Setting | Default | Meaning |
|---|---|---|
| `R2_NOTIFY` | `failures` | `always` also emails successful uploads |
| `R2_STALE_DAYS` | `8` | Weekly summary says STALE after this many days without a successful upload |
| `R2_HOSTCONFIG_EXTRA` | empty | Extra folders for the host config backup, comma separated (also asked by `config`) |

After editing `r2.env` by hand, run `config` again so `rclone.conf` is rebuilt.

## Email reports

Reports are mailed to `root`. On Proxmox, `/root/.forward` passes root's mail to
`proxmox-mail-forward`, which sends it on through your **notification
targets** (Datacenter → Notifications).

If you use the default `mail-to-root` target with a local Postfix that sends
directly to the internet, many mail servers will **reject** the mail: the
sender is `root@<host>.<local-domain>`, a domain that doesn't exist publicly
(`450 4.1.8 Sender address rejected: Domain not found`). Check with:

```bash
journalctl -t postfix/smtp --since -7d | grep -E 'status=(deferred|bounced)'
```

The reliable fix is an **SMTP target** that logs in to a real mailbox, then
pointing the default matcher at it:

```bash
pvesh create /cluster/notifications/endpoints/smtp --name mymail \
  --server mail.example.com --port 465 --mode tls \
  --username alerts@example.com --password 'xxx' \
  --from-address alerts@example.com --mailto you@example.com
pvesh set /cluster/notifications/matchers/default-matcher --target mymail
pvesh create /cluster/notifications/targets/mymail/test
```

This also fixes the normal Proxmox backup-job emails.

## Restoring from R2

Run `pve-r2-backup restore` and follow the menus:

```
Guests with backups in my-bucket/backups/pve/site1:

    #    VMID  type  name                    here     ver  newest                  size
    1     100  lxc   postgres                exists     2  2026-10-18 01:15   194.5 MiB
    2     117  lxc   zt-oslan                deleted    1  2026-02-22 01:18   212.9 MiB
   ...
Choose number (or q to quit): 2

Backups of lxc 117:
    #  date              folder          size  local
    1  2026-02-22 01:18  other      212.9 MiB

What now?
    1  Download only (shows under the storage's Backups in the GUI)
    2  Restore as a NEW guest (original is not touched)
    3  Restore OVER existing guest ...   (only if it exists and is stopped)
```

- You can type the list number or the VMID itself. Names of deleted guests
  come from the backup's `.notes` file.
- The backup is downloaded into the dump directory of `R2_STORAGE`, so it also
  appears under that storage's *Backups* in the GUI. Download speed is about
  100 MiB/s for large files (16 parallel streams). rclone checks the checksum,
  then the archive is tested (`zstd -t`). A copy already on local disk is not
  downloaded again.
- **New guest:** next free VMID suggested, target storage chosen from a list,
  `--unique` (new MAC addresses) when the original still exists. Not started
  unless you say so.
- **Over the existing guest:** only when it is stopped, and you have to type
  the VMID again to confirm.

Without menus:

```bash
pve-r2-backup restore --list                         # guests in R2
pve-r2-backup restore 101                            # versions of 101 (menu)
pve-r2-backup restore 101 latest --download-only
pve-r2-backup restore 101 2026_10_11 --new-id 9101 --storage local-lvm
pve-r2-backup restore 101 latest --overwrite         # 101 must be stopped
pve-r2-backup restore --path my-bucket/backups/pve/site2   # another host's backups
```

**Disaster recovery on a new host:** install Proxmox, run `install` and
`config` with the *same* R2 credentials (and encryption passwords, if used),
then `restore --path <bucket>/<old host's path>`. Keep the R2 token and the
encryption passwords somewhere other than the hosts being backed up.

**By hand**, with plain rclone (`r2crypt:` instead of `r2:<bucket>` if
encrypted):

```bash
rclone --config /root/.pve-r2/rclone.conf lsf r2:my-bucket/backups/pve/site1/weekly/
rclone --config /root/.pve-r2/rclone.conf copy r2:my-bucket/backups/pve/site1/weekly/ /var/lib/vz/dump/ \
  --include 'vzdump-lxc-101-2026_10_18-*' -P
pct restore 9101 /var/lib/vz/dump/vzdump-lxc-101-2026_10_18-01_15_04.tar.zst --storage local-lvm
```

## Host configuration backup

vzdump only backs up guests. `hostconfig` saves the host itself, once a day at 03:30
(and whenever you run it, e.g. before an upgrade). One archive holds:

| What | Why |
|---|---|
| `/etc`, including `/etc/pve` | network, storage, backup jobs, users, firewall, notification targets, guest configs, certificates, `fstab`, `modprobe.d`, SSH host keys |
| `_hostconfig/config.db` | consistent copy of `/var/lib/pve-cluster/config.db`, the database behind `/etc/pve` |
| `_hostconfig/inventory.txt` | `pveversion -v`, disks, LVM, ZFS, IP addresses, routes, storages, guests, installed packages |
| `/root/.ssh`, `/root/.pve-r2`, `/usr/local`, cron, `/var/lib/vz/snippets` | keys, this tool's settings, local scripts, hook/cloud-init snippets |
| your extra folders | `R2_HOSTCONFIG_EXTRA`, e.g. `/usr/share/scripts` (`.git`, `venv`, `node_modules` and disk images are skipped) |

It is **always encrypted** (gpg, AES-256) with its own passphrase, because it contains
the Proxmox keys and all passwords. `config` asks for it (Enter generates one). **Keep
the passphrase outside the host**, or the backup can't be opened when it's needed.
Typical size: under 1 MB.

```bash
pve-r2-backup hostconfig            # make one now
pve-r2-backup hostconfig list       # what's in R2
pve-r2-backup hostconfig get        # newest -> /root/hostconfig-<host>-<date>/
pve-r2-backup hostconfig get 2026_10_29 --to /root/cfg --path my-bucket/backups/pve/site2
```

Single files (e.g. `etc/pve/qemu-server/100.conf`) can be copied out of the unpacked
folder. To rebuild a lost host, follow `_hostconfig/RESTORE.txt` in the archive: install
the same Proxmox version with the same hostname, recreate pools from the inventory, then
put `config.db` back (`systemctl stop pve-cluster`, copy, `systemctl start pve-cluster`)
and reboot.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `SignatureDoesNotMatch` | Wrong Secret Access Key, or it doesn't belong to that Access Key ID. Run `config` again. |
| `Failed to create file system … is a file not a directory` | Usually also bad credentials (rclone reports the failed login confusingly) |
| `already belongs to another host` | Two hosts configured with the same path. Pick another, or delete `<path>/.owner` if the old host is gone |
| Backups of one guest are missing | Check that the backup itself works: `--latest` skips runs that produced only a `.log`. A container with an unreachable NFS mount fails in `suspend` mode; `snapshot` mode avoids that |
| No emails | `pve-r2-backup notify-test`, then see [Email reports](#email-reports) |

Log: `/var/log/pve-r2-backup.log` · State: `/var/lib/pve-r2-backup/state`

## Development

One file, Python 3.11+ standard library only. Tests for the selection and
parsing logic (no Proxmox or R2 needed):

```bash
python3 -m unittest discover -s tests -v
```

## How the hook works

vzdump calls the script at every stage of a backup. At `job-end`, it starts
`sync --since 2 --wait` in the background and returns straight away. It
always exits 0, so it can never make a backup fail. If two jobs finish close
together, the second upload waits for the first (lock) instead of being skipped.

## Roadmap

Planned: scheduled restore tests (`verify`), support for other S3 providers, a Debian package. See [ROADMAP.md](ROADMAP.md).

## License

MIT, see [LICENSE](LICENSE).
