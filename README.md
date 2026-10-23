# pve-r2-backup

Off-site copies of Proxmox VE `vzdump` backups in **Cloudflare R2**, with
email reports through Proxmox's own notification system.

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
- **Optional client-side encryption** (rclone crypt). Folder names stay
  readable so the lifecycle rules keep working.
- Safe to re-run: `install` and `config` can be run again at any time;
  `config` shows the saved values as defaults.

## Requirements

- Proxmox VE 8 or 9 (tested on 9.2), run as root.
- A Cloudflare R2 bucket and an **R2 API token** with *Object Read & Write*,
  ideally limited to that bucket.
- `install` adds `jq` (from Debian) and the current `rclone` (from rclone.org).
  The rclone in Debian's repositories is too old for R2.

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/pacmac/pve-r2-backup/main/pve-r2-backup.sh -o /root/pve-r2-backup.sh
bash /root/pve-r2-backup.sh install     # rclone, jq, vzdump hook, weekly timer
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

R2 counts the age from the **upload** time, not from when the backup was made.

## Commands

| Command | What it does |
|---|---|
| `install` | Installs rclone and jq, this script as `/usr/local/sbin/pve-r2-backup`, the vzdump hook (`script:` in `/etc/vzdump.conf`), and a weekly timer (Mon 06:00) with logrotate |
| `config` | Asks for (or confirms) the R2 details, path and storage, then writes `/root/.pve-r2/{r2.env,rclone.conf}` (mode 600) and tests read/write |
| `sync [--since N \| --latest \| --all] [-n]` | Upload backups changed in the last N days (default 2), the newest good backup per guest, or everything. `-n` = dry run |
| `weekly` | Catch-up upload of the last 14 days plus the summary email (this is what the timer runs) |
| `status` | Config, last run/success, hook, timer, and what's in R2 |
| `notify-test` | Sends a test email |
| `uninstall` | Removes the hook, timer and logrotate entry. Keeps the config and everything in R2 |

Settings in `/root/.pve-r2/r2.env` that `config` doesn't ask for:

| Setting | Default | Meaning |
|---|---|---|
| `R2_NOTIFY` | `failures` | `always` also emails successful uploads |
| `R2_STALE_DAYS` | `8` | Weekly summary says STALE after this many days without a successful upload |

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

You need the R2 credentials (and the encryption passwords, if used). On any
Proxmox host with rclone set up through `config`:

```bash
# see what's there
rclone --config /root/.pve-r2/rclone.conf lsf r2:my-bucket/backups/pve/site1/weekly/

# download one backup into a dump directory
rclone --config /root/.pve-r2/rclone.conf copy \
  r2:my-bucket/backups/pve/site1/weekly/ /var/lib/vz/dump/ \
  --include 'vzdump-lxc-101-2026_10_18-*' -P

# then restore as usual (GUI: storage → Backups → Restore), or:
pct restore 101 /var/lib/vz/dump/vzdump-lxc-101-2026_10_18-01_15_04.tar.zst --storage local-lvm
qmrestore /var/lib/vz/dump/vzdump-qemu-1001-....vma.zst 1001 --storage local-lvm
```

With encryption enabled, use the `r2crypt:` remote instead of `r2:<bucket>`,
e.g. `r2crypt:backups/pve/site1/weekly/`.

**Test a restore** now and then, and keep the R2 token (and the encryption
passwords) somewhere other than the host being backed up.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `SignatureDoesNotMatch` | Wrong Secret Access Key, or it doesn't belong to that Access Key ID. Run `config` again. |
| `Failed to create file system … is a file not a directory` | Usually also bad credentials (rclone reports the failed login confusingly) |
| `already belongs to another host` | Two hosts configured with the same path. Pick another, or delete `<path>/.owner` if the old host is gone |
| Backups of one guest are missing | Check that the backup itself works: `--latest` skips runs that produced only a `.log`. A container with an unreachable NFS mount fails in `suspend` mode; `snapshot` mode avoids that |
| No emails | `pve-r2-backup notify-test`, then see [Email reports](#email-reports) |

Log: `/var/log/pve-r2-backup.log` · State: `/var/lib/pve-r2-backup/state`

## How the hook works

vzdump calls the script at every stage of a backup. At `job-end`, it starts
`sync --since 2 --wait` in the background and returns straight away. It
always exits 0, so it can never make a backup fail. If two jobs finish close
together, the second upload waits for the first (lock) instead of being skipped.

## License

MIT, see [LICENSE](LICENSE).
