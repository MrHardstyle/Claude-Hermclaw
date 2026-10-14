# Runbook – Clean System Bootstrap (P01)

Exact steps to bring the Hermclaw Next hosts from a fresh Debian 13 installation to the
bootstrapped state. Architecture and design rationale: [`docs/architecture/bootstrap.md`](../architecture/bootstrap.md).

| Host | IP | Inventory group | What the bootstrap installs |
|---|---|---|---|
| Orchestrator | 192.168.178.225 | `orchestrator` | base, PostgreSQL 17 + pgvector ≥ 0.8.2, rootless Podman (SearXNG), SSH keys, unit skeleton, credentials |
| WebUI | 192.168.178.223 | `webui` | base, Node.js 22 (UI build), nginx baseline site |
| Execution worker | 192.168.178.222 | `execution_workers` | base, rootless Podman sandbox, Wake-on-LAN, worker unit |
| Model / media worker | 192.168.178.224 | `model_workers` | base, NVIDIA 580 (GTX 1080), Ollama, Wake-on-LAN, worker unit |
| GitLab | 192.168.178.226 | `gitlab` | nothing (inventory / known_hosts only) |
| Terra / Unraid | 192.168.178.60 | `backup` | nothing (backup key is installed manually, see step 7) |

Order matters: **orchestrator first** (it generates the SSH keys and the worker tokens), then
WebUI and workers. Every step can be repeated; a rerun reports `changed=0`.

> Never run the bootstrap against a host whose role differs from the table – host roles are
> fixed (Bauplan §2). The preflight refuses to touch ports or units owned by other services.

## 0. Prerequisites on every host (manual, once)

1. Debian 13 "trixie" amd64, minimal installation with "SSH server" and "standard system utilities".
2. A personal admin account with sudo (example below: `admin`), reachable by SSH key from
   the admin machine. **Do not** name it `hermclaw-admin` – that login is created by the
   bootstrap as the restricted runtime account.
3. Verify each host key out of band (console): `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`,
   then on the admin machine `ssh admin@192.168.178.22X` and compare the fingerprint
   (`StrictHostKeyChecking=yes` is set in `ansible.cfg`).
4. `.222` / `.224`: enable Wake-on-LAN in the firmware (often "Power On By PCI-E").
   Note the MAC address of the wired interface: `ip -br link`.
5. `.224`: Secure Boot **disabled** in the firmware (the DKMS-built NVIDIA modules are unsigned),
   or enrol the DKMS key after step 6 (`mokutil --import /var/lib/dkms/mok.pub`, reboot,
   confirm in MokManager) and set `nvidia_secureboot_mok_enrolled: true`. The role refuses an
   enabled Secure Boot otherwise.
6. GitLab `.226` / Terra `.60`: collect their SSH host keys (verified out of band) as
   known_hosts lines, e.g. `ssh-keyscan -t ed25519 192.168.178.226` **and** compare
   `ssh-keygen -lf` with the console output before trusting it.

## 1. Admin machine (Ansible control node)

```sh
git clone <hermclaw-next repository> && cd hermclaw-next
scripts/bootstrap/bootstrap-control-node.sh
#   * ansible-core (>=2.19,<2.22) in ~/.local/share/hermclaw/ansible-venv
#   * creates infra/ansible/inventory/hosts.yml from hosts.example.yml (never overwrites)
#   * validates the inventory and syntax-checks every playbook
```

Edit `infra/ansible/inventory/hosts.yml`:

* `ansible_user: admin` (your sudo account)
* `wol_mac` of `hermclaw-exec-222` and `hermclaw-model-224` (step 0.4), `wol_interface` if the
  interface is not the one of the default route
* `hermclaw_web_server_name` (nginx `server_name`, e.g. `hermclaw.lan`)
* optional: `common_known_hosts_extra` (step 0.6) on the orchestrator host or in `all.vars`:

```yaml
    orchestrator:
      hosts:
        hermclaw-orchestrator:
          ansible_host: 192.168.178.225
          common_known_hosts_extra:
            - "gitlab.lan,192.168.178.226 ssh-ed25519 AAAAC3Nz..."
            - "192.168.178.60 ssh-ed25519 AAAAC3Nz..."
```

Rerun the script to validate the edited inventory (it warns about placeholder MACs), then:

```sh
export ANSIBLE_CONFIG=$PWD/infra/ansible/ansible.cfg PATH=$HOME/.local/share/hermclaw/ansible-venv/bin:$PATH
cd infra/ansible
ansible -i inventory/hosts.yml 'all:!gitlab:!backup' -m ansible.builtin.ping -K   # -K if sudo needs a password
```

## 2. Preflight on all hosts (read-only)

```sh
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --tags preflight -K
```

Alternatively directly on a host (copy the script first):

```sh
sudo bash check-host.sh --role orchestrator            # human readable
sudo bash check-host.sh --role model_workers --json    # JSON report
```

Exit code 0 = OK (warnings allowed), 1 = at least one FAIL. Typical FAILs and what to do:

| FAIL | Meaning | Action |
|---|---|---|
| `os` / `arch` | not Debian 13 trixie / not x86_64 | reinstall the host |
| `port:<n>` | a foreign process listens on a planned port (§6) | move or stop that service yourself; never override with `--allow-conflicts` unless the process is known to be the Hermclaw service |
| `unit:<name>` | a unit file with a Hermclaw name that Hermclaw does not manage | rename/remove it manually |
| `ram` / `cpu` / `disk` | below the per-role minimum | upgrade, or override `common_preflight_min_*` deliberately |
| `gpu` (`.224`) | no NVIDIA GPU visible on the PCI bus | check the card / slot; the model worker needs the GTX 1080 |

## 3. Orchestrator `.225`

```sh
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit orchestrator --check --diff -K   # optional dry run
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit orchestrator -K
```

Verify on `.225`:

```sh
cat /etc/debian_version                                   # 13.x
id hermclaw; ls -ld /opt/hermclaw /etc/hermclaw /var/lib/hermclaw/{workspaces,artifacts,repos} /var/log/hermclaw /etc/credstore.encrypted
sudo ls -l /etc/hermclaw/ssh                               # id_ed25519 (0600 hermclaw), *.pub, known_hosts
dpkg-query -W postgresql-17 postgresql-17-pgvector         # pgvector >= 0.8.2 (from PGDG)
sudo -u postgres psql -tAc "SHOW listen_addresses"         # localhost
sudo -u postgres psql -d hermclaw -tAc "SELECT extversion FROM pg_extension WHERE extname='vector'"
ss -ltnp | grep -E ':(5432|8000|4000|8888|9100)\b'         # only 127.0.0.1:5432 / [::1]:5432 so far
grep -E '^hermclaw:' /etc/subuid /etc/subgid
cat /etc/systemd/system/user@.service.d/delegate.conf      # Delegate=cpu cpuset io memory pids
sudo -u hermclaw XDG_RUNTIME_DIR=/run/user/$(id -u hermclaw) podman info --format '{{.Host.Security.Rootless}} {{.Host.CgroupsVersion}} {{.Host.CgroupControllers}}'
systemctl list-unit-files 'hermclaw-*'                     # installed, disabled (enabled in P32)
sudo /opt/hermclaw/bin/hermclaw-credential list            # api-admin-token litellm-master-key worker-token-exec-222 worker-token-model-224
```

Set the GitLab token (issued in GitLab: project/group access token, scopes `api`, `read_repository`,
`write_repository`); the value is read from stdin and never echoed:

```sh
sudo /opt/hermclaw/bin/hermclaw-credential set gitlab-token
sudo /opt/hermclaw/bin/hermclaw-credential check gitlab-token      # OK gitlab-token
```

## 4. WebUI `.223`

```sh
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit webui -K
```

Verify on `.223`:

```sh
node --version; npm --version                              # v22.x (>= 22.12)
sudo nginx -t
curl -sI http://127.0.0.1/ -H 'Host: hermclaw.lan' | head -5   # 200, placeholder page
cat /etc/nginx/sites-available/hermclaw-next.conf | grep -n 'proxy_buffering off'
grep hermclaw-runtime ~hermclaw-admin/.ssh/authorized_keys     # restrict,from="192.168.178.225" ...
```

## 5. Execution worker `.222`

```sh
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit execution_workers -K
```

Verify on `.222`:

```sh
sudo ethtool "$(ip route show default | awk '{print $5; exit}')" | grep Wake-on   # Wake-on: g
systemctl is-enabled 'hermclaw-wol@*'                      # enabled
sudo -u hermclaw XDG_RUNTIME_DIR=/run/user/$(id -u hermclaw) podman info --format '{{.Host.Security.Rootless}}'   # true
sudo /opt/hermclaw/bin/hermclaw-credential check worker-token                       # OK worker-token
sudo -l -U hermclaw-admin                                  # only: /usr/bin/systemctl suspend
```

Wake-on-LAN test (from the orchestrator, after `sudo systemctl suspend` on `.222`):
`wakeonlan -i 192.168.178.255 <mac>` (Debian package `wakeonlan`, not installed by the bootstrap) or the Hermclaw doctor (`hermclaw doctor`, after P32).

## 6. Model worker `.224`

```sh
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit model_workers -K
```

The first run builds the NVIDIA 580 driver (runfile, DKMS). If nouveau was loaded or the module
cannot be loaded yet, the play stops with a reboot hint (or reboots itself with
`-e nvidia_allow_reboot=true`). After the reboot run the same command again. Verify:

```sh
nvidia-smi --query-gpu=driver_version,name,memory.total,compute_cap --format=csv,noheader
#   580.x, NVIDIA GeForce GTX 1080, 8192 MiB, 6.1
cat /etc/apt/preferences.d/hermclaw-nvidia.pref           # every apt NVIDIA driver package blocked (runfile)
curl -s http://127.0.0.1:11434/api/version                 # {"version":"0.40.4"}
systemctl cat ollama | grep -E 'OLLAMA_(HOST|KEEP_ALIVE|MAX_LOADED_MODELS)|UnsetEnvironment'
journalctl -u ollama -b --no-pager | grep 'inference compute'   # library=CUDA compute=6.1
sudo nft list table inet hermclaw_ollama                   # drop 11434 except lo + 192.168.178.225
curl -s --max-time 3 http://192.168.178.224:11434/api/version   # from .223: must time out (firewall)
```

Pinned versions: `nvidia_runfile_version` and `ollama_version` (roles' defaults). Upgrade only after
the GPU smoke test (research 20261014-026, step 6) – never to a 590+ driver (no Pascal support).

## 7. After all hosts

```sh
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml -K               # full run: changed=0
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --check --diff -K  # changed=0
```

Runtime SSH from the orchestrator (key + known_hosts written by the bootstrap):

```sh
sudo -u hermclaw ssh -i /etc/hermclaw/ssh/id_ed25519 -o UserKnownHostsFile=/etc/hermclaw/ssh/known_hosts \
  -o StrictHostKeyChecking=yes hermclaw-admin@192.168.178.222 true && echo OK
```

Terra `.60` backup key (manual, Unraid): append `/etc/hermclaw/ssh/backup_ed25519.pub` of the
orchestrator to the backup user's `authorized_keys` on Terra, prefixed with
`restrict,from="192.168.178.225"` (research 20261008-021/-022).

## Re-running, partial failures, rollback

* Every task is idempotent; fix the cause and rerun the same command.
* nginx / PostgreSQL configuration errors are rolled back automatically and nothing is reloaded.
* Worker play fails with "bootstrap the orchestrator first": run step 3, then rerun.
* Rotating a worker token: `sudo /opt/hermclaw/bin/hermclaw-credential generate worker-token-<id> --force`
  on the orchestrator, `sudo rm /etc/credstore.encrypted/hermclaw-worker-token` on the worker, rerun the worker play,
  then restart `hermclaw-api`/`hermclaw-scheduler` and the worker service (credentials are read at start).
* Units stay disabled until the deployment phase (`systemd_skeleton_enable_units: true`).

## Testing the automation without the hosts

```sh
.venv/bin/pytest -q tests/integration/test_bootstrap_*.py      # ~15 min (Podman, PostgreSQL, systemd-analyze, shellcheck)
HERMCLAW_ANSIBLE_LINT=/path/to/ansible-lint .venv/bin/pytest -q tests/integration/test_bootstrap_infra.py -k ansible_lint
```

Live status: the target hosts are not reachable from the build environment (BLOCKER-001); all
steps above are automated and tested locally (converge on a Debian 13 userland, real
PostgreSQL/nginx/nftables/systemd-analyze), the host-specific verifications are pending.
