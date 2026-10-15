# Terra/Unraid `.60` as backup target – share setup

Backup target of Hermclaw Next (Bauplan §36, research 20261008-022). The orchestrator `.225` pushes one backup
set per night (`hermclaw-backup.timer`, 02:30) into a dedicated share on Terra `192.168.178.60`; the weekly
restore test (`hermclaw-restore-test.timer`, Sunday 04:30) reads it back. Two ways to reach the share – pick one
and put it into `/etc/hermclaw/backup.yaml` (template: `infra/backup/backup.example.yaml`).

> Status: the LAN hosts are not reachable from the build environment (BLOCKER-001). Everything below is verified
> locally against a loopback `sshd` with an `rrsync`-restricted key and a local directory
> (`tests/integration/test_backup_rsync.py`); the steps on the Unraid box itself are to be executed and
> checked once at cutover (`docs/operations/backup-restore.md`, "First run on .60").

## 1. Create the share (Unraid web UI)

1. *Shares → Add Share*: name `backups`, *Use cache pool*: `No` (or `Yes` + mover – backups should land on the
   array), *Export*: `No` for SMB/NFS when you use rsync over SSH (option A), else see option B.
2. Directory for Hermclaw: `/mnt/user/backups/hermclaw` (created once, `chmod 700`).
3. Plan capacity: a backup set is roughly `pg_dump -Fc` size + compressed artifacts; with the default retention
   (`keep_last 2`, `keep_daily 7`, `keep_weekly 4`, `keep_monthly 3`) up to ~16 sets are on the share.

## 2. Option A (preferred): rsync over SSH with a dedicated, restricted key

On `.225` the deployment playbook generates `/etc/hermclaw/ssh/backup_ed25519` (role `common`,
`common_ssh_keys`); the private key never leaves the orchestrator.

On Terra (as root – Unraid only offers root for SSH):

```bash
# Unraid ≥ 6.9 keeps /root/.ssh on the flash drive (persistent); verify after a reboot.
mkdir -p /root/.ssh && chmod 700 /root/.ssh
# restrict the key to rsync into the Hermclaw directory, only from the orchestrator:
echo 'restrict,from="192.168.178.225",command="/usr/local/bin/rrsync /mnt/user/backups/hermclaw" ssh-ed25519 AAAA… hermclaw-backup@orchestrator' >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
```

* `rrsync` ships with rsync (`support/rrsync`, Python 3). If the box has no Python 3, install it via the
  Unraid plugin "NerdTools"/"un-get", or use the key without `command=` but **with** `restrict,from=…` (weaker:
  the key could run any command as root – then prefer option B).
* With `rrsync` the client path is relative to the restricted directory: `path: "."` in `backup.yaml`.
  Without it use the absolute path `/mnt/user/backups/hermclaw`.
* Host key: put Terra's host key into Hermclaw's own known_hosts (never `StrictHostKeyChecking=no`):

  ```bash
  ssh-keyscan -t ed25519 192.168.178.60 | sudo tee -a /etc/hermclaw/ssh/known_hosts
  # compare the fingerprint with the one shown on the Unraid console: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
  ```

* Check from `.225`: `sudo -u hermclaw /opt/hermclaw/bin/hermclaw-backup list` → `{"sets": []}`.

The backup uses `BatchMode=yes`, `IdentitiesOnly=yes`, `StrictHostKeyChecking=yes`, `-F /dev/null` (no user ssh
config), `--partial` and verifies every transfer with an `rsync --checksum --dry-run --itemize-changes` pass
before `MANIFEST.json` (the completeness marker) is sent; listing and deletion are plain rsync operations, so
the `rrsync` restriction is sufficient (no shell needed).

## 3. Option B: share mounted on `.225`

NFS export on Unraid (*Shares → backups → NFS Security Settings*: Export `Yes`, Security `Private`,
rule `192.168.178.225(sec=sys,rw,no_root_squash)`), on `.225`:

```bash
sudo mkdir -p /mnt/terra-backup
echo '192.168.178.60:/mnt/user/backups /mnt/terra-backup nfs4 _netdev,noauto,x-systemd.automount,x-systemd.idle-timeout=10min,nofail 0 0' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload && sudo mkdir -p /mnt/terra-backup/hermclaw && sudo chown hermclaw:hermclaw /mnt/terra-backup/hermclaw
```

`backup.yaml`: `target: {kind: local, path: /mnt/terra-backup/hermclaw, require_mount: true}` – with
`require_mount` an unmounted share fails the run instead of filling the local disk; the target directory is
never created implicitly. The units must be allowed to write there (`ProtectSystem=strict`): on a deployed host set
`systemd_skeleton_backup_write_paths: [/mnt/terra-backup/hermclaw]` for the orchestrator in the inventory and re-run
`playbooks/deploy.yml --limit orchestrator`; on a hand-built host add `ReadWritePaths=-/mnt/terra-backup/hermclaw` to
both units of `infra/backup/`.

## 4. What lands on the share

```
/mnt/user/backups/hermclaw/
  hermclaw-20261015T023000Z-3fa1c2/
    db.dump            pg_dump -Fc of the Hermclaw database (snapshot-consistent with the row counts)
    globals.sql        pg_dumpall --globals-only --no-role-passwords (absent when not permitted)
    config.tar.gz      /etc/hermclaw without secrets/, private keys, *.key/*.pem, file:-referenced secrets
    artifacts.tar.gz   artifact store + records/*.jsonl (research records, deployment manifests)
    MANIFEST.json      sha256 + size per file, row counts, alembic revision, versions (written last)
    MANIFEST.json.sig  HMAC-SHA256 (when manifest_signing_key_ref is configured)
    RESTORE_REPORT.json  result of the last restore test of this set
```

Files are `0600`, directories `0700`. Nothing on the share is ever executed by Hermclaw; a restore verifies
checksums (and the signature) before it uses a set.
