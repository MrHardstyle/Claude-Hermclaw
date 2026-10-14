# 20261014-029 – Deployment practices: ansible-core on Debian 13, versioned venv releases, systemd hardening and credentials

## Question
1. **ansible-core**: which version does Debian 13 "trixie" ship, which Python do target hosts need, which
   `ansible.builtin` modules does a versioned Python deployment need (`pip` with `virtualenv_command`, `file`
   `state=link` for an atomic `/opt/hermclaw/venv -> venv-<version>` switch, `template`, `systemd_service`, `uri`
   for health checks, `get_url` with `checksum`), and how is idempotency tested (`--check --diff`, second run
   `changed=0`)?
2. **Python wheel into a fresh venv on Debian 13**: `python3-venv`, offline install from a local wheelhouse
   (`--no-index --find-links`), keeping N previous venvs for rollback.
3. **systemd hardening for Python network services**: `NoNewPrivileges`, `ProtectSystem=strict` + `ReadWritePaths`,
   `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `RestrictAddressFamilies`, `CapabilityBoundingSet=`,
   `SystemCallFilter=@system-service`, `MemoryDenyWriteExecute` caveats for Python/JIT. What breaks for a service
   that must run rootless Podman (execution worker `.222`) or use the GPU (`/dev/nvidia*`, Ollama on `.224`)?
4. **systemd credentials**: `LoadCredentialEncrypted=`, `systemd-creds encrypt` (host key vs. TPM2, `--with-key`),
   `SetCredentialEncrypted=`, `$CREDENTIALS_DIRECTORY`, `ImportCredential=`, the systemd version in trixie and what
   it supports.
5. **uvicorn as a systemd service behind nginx**: workers, `proxy_headers`, `forwarded_allow_ips`.

## Why needed
P32 (deployment playbook, 32.1–32.10), P36 (security), INSTALLATION.md and `docs/runbooks/upgrade.md`. The bootstrap
roles (P01) install unit skeletons (`infra/systemd/*.j2`, research R-020 from 2026-10-08) and an empty
`/opt/hermclaw/venv`. The deployment phase has to install real releases, switch them atomically, enable hardened
units, and load secrets as encrypted credentials. This re-check found **one blocking defect** (the execution worker
unit implicitly enables `NoNewPrivileges`, which breaks rootless Podman) and several smaller gaps (bootstrap venv vs.
symlink switch, `forwarded_allow_ips="*"`, TPM/PCR binding of credentials, check-mode noise of the `pip` module).
See Decision.

## Sources (accessed 2026-10-14)
**Access notes.** These hosts were **not reachable** from this build sandbox (egress proxy answered 403 to CONNECT):
`*.debian.org` (packages, tracker, sources, release, deb mirrors, piuparts), `docs.ansible.com`, `freedesktop.org`
(systemd man pages), `www.uvicorn.org`, `docs.python.org`, `pip.pypa.io`, `repology.org`, third-party Debian mirrors.
`ollama.com` was not used (registry blocked; not needed for this topic). **Reachable:** `pypi.org` (JSON API),
`raw.githubusercontent.com` (official source/doc repositories at pinned tags), `launchpad.net` (its Debian mirror only
has sid/experimental data, no trixie). For blocked documentation sites I read **the same document in the vendor's own
source repository at the matching release tag**. Debian trixie package versions come from **search-engine snippets of
the primary Debian pages** (`[P/snippet]`), not from the pages themselves. Run `apt-cache policy <pkg>` on the hosts
to confirm them.

Legend: `[P]` primary source read directly · `[P/snippet]` primary page seen only as a search snippet ·
`[S]` secondary source · `[E]` experiment run in this sandbox (Ubuntu 24.04, kernel 6.18, Python 3.12.3 and
3.13.16, podman 4.9.3: **not Debian**, so trixie builds must be re-checked on the hosts) · `[R]` repository state.

Primary, read directly (`[P]`):
- ansible-core on PyPI (versions, upload times, `requires_python`, sha256): https://pypi.org/pypi/ansible-core/json ,
  https://pypi.org/pypi/ansible-core/2.21.5/json , …/2.20.10/json , …/2.19.14/json , …/2.22.0rc1/json
- Ansible release and maintenance (support matrix, control-node and target Python):
  https://github.com/ansible/ansible-documentation/blob/devel/docs/docsite/rst/reference_appendices/release_and_maintenance.rst
- Ansible check mode and diff mode:
  https://github.com/ansible/ansible-documentation/blob/devel/docs/docsite/rst/playbook_guide/playbooks_checkmode.rst
- ansible-core v2.21.5 module sources (documentation block plus implementation):
  https://github.com/ansible/ansible/blob/v2.21.5/lib/ansible/modules/pip.py , `file.py`, `systemd_service.py`,
  `uri.py`, `get_url.py`, `template.py`
- Launchpad mirror of the Debian archive, ansible-core publishing history (sid only):
  https://launchpad.net/debian/+source/ansible-core/+publishinghistory
- systemd **v257.13** (trixie's version): `NEWS`, `man/systemd.exec.xml`, `man/systemd-creds.xml`,
  `man/systemd.resource-control.xml`, `man/systemd.service.xml`, `man/systemd-system.conf.xml`, `docs/CREDENTIALS.md`,
  `src/core/exec-invoke.c` (`context_has_seccomp`, `context_has_no_new_privileges`,
  `apply_memory_deny_write_execute`), `src/core/exec-credential.c` (`load_credential_glob`),
  `src/shared/creds-util.c` / `creds-util.h`, `src/shared/seccomp-util.c` (syscall groups):
  https://github.com/systemd/systemd/tree/v257.13
- uvicorn 0.54.0 (`docs/settings.md`, `docs/deployment/index.md`, `uvicorn/config.py`, `uvicorn/main.py`,
  `uvicorn/middleware/proxy_headers.py`): https://github.com/Kludex/uvicorn/tree/0.54.0 ; PyPI metadata
  https://pypi.org/pypi/uvicorn/0.54.0/json
- pip 26.2.1 docs (`docs/html/user_guide.rst` "Installing from local packages", `docs/html/topics/secure-installs.md`,
  `docs/html/cli/pip_download.rst`): https://github.com/pypa/pip/tree/26.2.1/docs/html
- CPython 3.13 `Doc/library/venv.rst` and `Doc/whatsnew/3.13.rst` (JIT build option):
  https://github.com/python/cpython/tree/v3.13.7/Doc
- nginx `ngx_http_upstream_module.xml` (`keepalive_timeout`): https://github.com/nginx/nginx.org/blob/main/xml/en/docs/http/ngx_http_upstream_module.xml
- Podman v5.4.2 `troubleshooting.md`: https://github.com/containers/podman/blob/v5.4.2/troubleshooting.md
- Ollama v0.40.4 `docs/linux.mdx` (manual unit), `docs/gpu.mdx` (nvidia_uvm reload):
  https://github.com/ollama/ollama/tree/v0.40.4/docs
- *Added by the verifier (2026-10-14):* ansible-core changelogs (release dates, CVE-2026-11332):
  https://github.com/ansible/ansible/blob/stable-2.19/changelogs/CHANGELOG-v2.19.rst ,
  https://github.com/ansible/ansible/blob/stable-2.21/changelogs/CHANGELOG-v2.21.rst
- *Added by the verifier:* pip 26.2.1 platform-tag expansion:
  https://github.com/pypa/pip/blob/26.2.1/src/pip/_internal/utils/compatibility_tags.py
- *Added by the verifier:* nvidia-modprobe option parsing (main branch):
  https://github.com/NVIDIA/nvidia-modprobe/blob/main/option-table.h ,
  https://github.com/NVIDIA/nvidia-modprobe/blob/main/nvidia-modprobe.c ,
  https://github.com/NVIDIA/nvidia-modprobe/blob/main/common-utils/nvgetopt.c
- *Added by the verifier:* PyPI JSON for `tree-sitter-language-pack` 1.22.1, `pyyaml`, `uvicorn`, `litellm` 1.104.2:
  https://pypi.org/pypi/tree-sitter-language-pack/1.22.1/json , https://pypi.org/pypi/litellm/1.104.2/json

Primary pages seen only through search snippets (`[P/snippet]`):
- Debian Package Tracker ansible-core (stable `2.19.4-0+deb13u1`, oldstable `2.14.18-0+deb12u2`):
  https://tracker.debian.org/pkg/src:ansible-core ; proposed-updates debdiff 2.19.1-0+deb13u1 → 2.19.4-0+deb13u1:
  https://release.debian.org/proposed-updates/trixie_diffs/ansible-core_2.19.4-0+deb13u1.debdiff ;
  sources.debian.org (`ansible` 12.0.0+dfsg-0+deb13u1 in trixie): https://sources.debian.org/patches/ansible-core
- *Added by the verifier:* trixie-proposed-updates ansible-core `2.19.11-0+deb13u1` (`.changes` dated 2026-06-19,
  debdiff): https://release.debian.org/proposed-updates/trixie_diffs/ansible-core_2.19.11-0+deb13u1.debdiff ,
  https://ftp4.de.freebsd.org/debian/dists/proposed-updates/ansible-core_2.19.11-0+deb13u1_source.changes (Debian
  mirror); piuparts pipx dependencies: https://piuparts.debian.org/stable2sid/pass/pipx_1.7.1-2.log
- Debian Package Tracker systemd (stable `257.13-1~deb13u1`, accepted into proposed-updates 2026-05-03):
  https://tracker.debian.org/pkg/src:systemd ; piuparts trixie-pu: https://piuparts.debian.org/trixie/source/s/systemd.html
- Debian python-pip control file / packages (trixie `25.1.1+dfsg-1`, `python3-pip-whl`):
  https://tracker.debian.org/media/packages/p/python-pip/control-25.1.1dfsg-1 , https://packages.debian.org/src:python-pip

Secondary (`[S]`, marked wherever used):
- aptpeek.dev index of trixie / trixie-updates (python3.13 `3.13.5-2+deb13u2` / `3.13.5-2+deb13u4`):
  https://aptpeek.dev/debian/trixie-updates/main/amd64/python3.13/3.13.5-2+deb13u4
- nixpkgs commits using `DeviceAllow=char-nvidiactl char-nvidia-caps char-nvidia-frontend char-nvidia-uvm` for CUDA
  services: https://code.ornl.gov/nix/nixpkgs/-/commit/79cec564b677a5881ffb4ea1bcbdd3a7046422be ,
  https://code.ornl.gov/nix/nixpkgs/-/commit/ee595b709e18882162ef5e435ef5c9fe275bdf80
- NixOS discourse / nixos commit on rootless Podman from a systemd service needing `NoNewPrivileges=false`:
  https://discourse.nixos.org/t/cannot-start-rootless-podman-compose-from-dynamic-user-systemd-service/34081 ,
  https://gitea.hillion.co.uk/JakeHillion/nixos/pulls/1087

Repository state read for this re-check (`[R]`): `pyproject.toml`, `infra/ansible/ansible.cfg`,
`infra/ansible/playbooks/group_vars/all.yml`, `infra/ansible/roles/*/defaults/main.yml`,
`infra/ansible/roles/common/tasks/python.yml`, `infra/ansible/roles/systemd_skeleton/tasks/{main,credentials}.yml`,
`infra/systemd/*.j2`, `infra/systemd/hermclaw-cred-env`, `scripts/bootstrap/{hermclaw-credential.sh,bootstrap-control-node.sh}`,
`scripts/ops/hermclaw-model-pull`, `docs/runbooks/{upgrade,cutover}.md`, `hermclaw/api/app.py`, `hermclaw/api/events.py`,
`hermclaw/events/store.py`, `hermclaw/workers/{api,registry}.py`, `worker/common/server.py`, `worker/execution/sandbox.py`,
`hermclaw/cli.py`, `config/hosts.example.yaml`; earlier research R-001, R-002, R-005, R-020, R-026, R-028.

## Relevant facts

### A. ansible-core (control node and targets)

| # | Fact | Source |
|---|---|---|
| A1 | Latest patch releases (all 2026-10-05): **2.19.14** (`requires_python >=3.11`), **2.20.10** (`>=3.12`), **2.21.5** (`>=3.12`). **2.22.0rc1** was published 2026-10-13 and needs **Python >=3.13**. `ansible_core-2.21.5-py3-none-any.whl` sha256 `08886f9f3e23d129aa98a01271c4a47833516e1cb7c737a246788cb474f41222`. 2.21.5 depends on `jinja2>=3.1.0, PyYAML>=5.1, cryptography, packaging, resolvelib>=0.8.0,<2.0.0`. | [P] PyPI JSON |
| A2 | Support matrix. The table states: "Dates listed indicate the **start** date of the maintenance cycle" (GA → general bugfixes, "Critical" → security + critical bugfixes only, "Security" → security fixes only). **2.19**: GA 21 Jul 2025, critical-only from 03 Nov 2025, security-only since 18 May 2026, **EOL Nov 2026**, controller Python 3.11–3.13, targets 3.8–3.13. **2.20**: GA 03 Nov 2025, critical-only since 18 May 2026, security-only from 02 Nov 2026, **EOL May 2027**, controller 3.12–3.14, targets 3.9–3.14. **2.21**: GA May 2026 (2.21.0 = 2026-05-18), general bugfixes until 2.22 GA, critical-only from Nov 2026, security-only from May 2027, **EOL Nov 2027**, controller 3.12–3.14, targets 3.9–3.14. **2.22**: GA Nov 2026, critical from May 2027, security from Nov 2027, EOL May 2028, controller **3.13–3.15**, targets 3.9–3.15. *(Verifier correction 2026-10-14: an earlier version of this row read the 2.20/2.21 dates as end dates, e.g. "2.21 critical fixes until Nov 2026".)* | [P] release_and_maintenance.rst |
| A3 | Debian trixie: the tracker snippets show **ansible-core 2.19.4-0+deb13u1** as "stable" (stable update on top of 2.19.1-0+deb13u1, accepted into proposed-updates 2025-11-08) and `ansible` 12.0.0+dfsg-0+deb13u1. **Verifier addition:** a newer stable update **2.19.11-0+deb13u1** exists in trixie-proposed-updates (`.changes` dated 2026-06-19, Distribution: trixie, closes #1139175; debdiff on release.debian.org). It carries the upstream fix for **CVE-2026-11332** (ansible-galaxy role install could inject arbitrary git configuration; fixed upstream in 2.19.11 on 2026-06-18 and in 2.21.1). Whether 2.19.11-0+deb13u1 has already reached trixie stable through a point release **could not be confirmed** (Debian hosts blocked). Even that update lacks the upstream 2.19.12–2.19.14 fixes (2.19.13 masks URL credentials in `uri`/`get_url`/`apt_key` output and adds `tempfile` path-traversal checks). sid has 2.22.0~beta2-1 (published 2026-10-02), and testing had 2.21.x. No trixie-backports build was found. So the Debian package is on a branch that goes upstream-EOL in Nov 2026. | [P/snippet] tracker, release.debian.org debdiffs, proposed-updates `.changes` (mirror); [P] Launchpad sid history; [P] upstream CHANGELOG-v2.19.rst / CHANGELOG-v2.21.rst |
| A4 | trixie's `python3.13` is **3.13.5** (Debian revision `3.13.5-2+deb13uN`: aptpeek shows `+deb13u2`/`+deb13u3` depending on architecture snapshot in trixie main and `+deb13u4` in trixie-updates; the exact current revision is not confirmed). That is inside every supported target range (A2). | [S] aptpeek index; R-001 |
| A5 | `ansible.builtin.pip` requires "pip, virtualenv, **setuptools or packaging**" importable by the **Ansible interpreter** (otherwise `missing_required_lib("packaging")`). It creates the venv only when `<virtualenv>/bin/activate` is missing. `virtualenv_command` may be `python3 -m venv` (detected by `-m venv`), and then `virtualenv_python` must not be set. `umask` is a module option. | [P] pip.py v2.21.5 |
| A6 | `pip` module change detection: with `name:` the task is `changed` iff pip printed `Successfully installed`. With `requirements:` or VCS it compares `pip freeze` before and after. **In check mode it always reports `changed=True` when `extra_args` or `requirements` is set**, and venv creation exits with `changed=True`. So a pip task with `--no-index --find-links` breaks a "`--check` shows no changes" gate unless it is guarded. | [P] pip.py v2.21.5 |
| A7 | `ansible.builtin.file state=link`: when the destination already is a symlink, the module creates `.<pid>.<time>.tmp` next to it, `os.symlink()`s, then `os.rename()`s over the old link, which is **atomic**. When the destination is a **directory**, it needs `force: true`, refuses a non-empty directory ("the directory … is not empty, refusing to convert it"), and does `rmdir` + symlink + rename, which is **not atomic**. Check and diff mode are fully supported. | [P] file.py v2.21.5 |
| A8 | `ansible.builtin.systemd_service`: `started`/`stopped` are idempotent, `restarted` "will always bounce the unit", `daemon_reload` runs before other operations, `scope: system\|user\|global`, and check mode is supported. | [P] systemd_service.py |
| A9 | `ansible.builtin.uri`: `check_mode: support: none`, so it is **skipped under `--check`** unless the task sets `check_mode: false`. `status_code` defaults to `[200]`, and `unix_socket` is available. | [P] uri.py |
| A10 | `ansible.builtin.get_url` `checksum: "<algorithm>:<checksum\|url>"`. If `dest` exists and its digest matches, the download is skipped (idempotent). Check mode is partial. | [P] get_url.py |
| A11 | `template` has full check and diff support. Check mode: `check_mode: false` forces a task to run under `--check`, and `ansible_check_mode` is available. `--diff` "can reveal sensitive information", so use `diff: false` for such tasks. | [P] template.py; playbooks_checkmode.rst |
| A12 | Repo: the control node is pinned to `ansible-core>=2.19,<2.22` (`pyproject.toml` dev extra, `bootstrap-control-node.sh`). The build venv has **2.21.5**. `ansible.cfg` sets `interpreter_python = /usr/bin/python3` and needs no Galaxy collections. `roles/common` installs `python3-apt`, `python3-debian`, `acl`, `sudo`, `python3-venv`, `python3-pip` and pipx, but **not `python3-packaging`** explicitly. A piuparts log for `pipx 1.7.1-2` (the trixie-era build) lists `Depends: python3-venv, python3-argcomplete, python3-packaging, …`, so it is very likely pulled in transitively via `pipx`; listing it explicitly is still the robust choice (the dependency must not vanish if `pipx` is dropped). | [R]; [P/snippet] piuparts pipx_1.7.1-2 log |

### B. Versioned venv releases and offline wheel installs

| # | Fact | Source |
|---|---|---|
| B1 | trixie `python-pip` **25.1.1+dfsg-1**. `python3-pip-whl` ships `/usr/share/python-wheels/pip-25.1.1-py3-none-any.whl`, which `python3 -m venv` (via `python3-venv` → `python3.13-venv`) bootstraps into each new venv. | [P/snippet] tracker/packages.debian.org |
| B2 | CPython: virtual environments are "**Not considered as movable or copyable** – you just recreate the same environment in the target location". So build each release's venv at its final path `/opt/hermclaw/venv-<version>` and never copy or rename venvs. | [P] cpython Doc/library/venv.rst |
| B3 | pip offline install: `python -m pip install --no-index --find-links=DIR -r requirements.txt`. Secure installs: `--require-hashes` plus `--only-binary :all:`. Hash checking is all-or-nothing (every requirement and dependency needs `--hash`). | [P] pip 26.2.1 user_guide.rst, secure-installs.md |
| B4 | `pip download --platform/--python-version/--implementation/--abi` needs `--only-binary=:all:` (or `--no-deps`). "It is recommended to specify all of these options if you are specifying one". | [P] pip_download.rst |
| B5 | Experiment: `pip download --only-binary=:all: --python-version 3.13 --implementation cp --abi cp313 --platform manylinux_2_41_x86_64` **failed for pyyaml** (only manylinux_2_17/2_28 wheels exist). **pip did not widen a single `manylinux_2_41` tag to older manylinux tags.** Listing `manylinux_2_41/2_34/2_28/2_17/2014` worked. `tree-sitter-language-pack 1.22.1` ships **only a `manylinux_2_34` wheel** (glibc ≥ 2.34; trixie has 2.41). Verifier check: pip 26.2.1 `_get_custom_platforms()` expands only `manylinux2014` (→ 2010, 1) and `manylinux2010` (→ 1); a `manylinux_X_Y` tag is passed through unexpanded, which confirms the experiment. Consequence: a wheel tagged with any *other* `manylinux_2_Y` (e.g. `_2_27`, `_2_31`, `_2_35`) is not matched by the five-tag list. PyPI confirms `tree_sitter_language_pack-1.22.1-cp310-abi3-manylinux_2_34_x86_64.whl` and `pyyaml-6.0.3-cp313-…manylinux2014/_2_17/_2_28_x86_64.whl`. | [E] pip 26.2.1; [P] pip `src/pip/_internal/utils/compatibility_tags.py` @26.2.1; [P] PyPI JSON |
| B6 | Experiment: built `hermclaw-0.1.0rc1-py3-none-any.whl` with `python -m build` in a scratch copy (43 wheels in the wheelhouse). A fresh `python3.13 -m venv` + `pip install --no-index --find-links <wheelhouse> --only-binary=:all: hermclaw==0.1.0rc1` installed. A second identical run printed only "Requirement already satisfied", and `pip check` reported "No broken requirements". Data files land in `<venv>/share/hermclaw/{alembic.ini,migrations}`. The resolved versions **differ from the tested dev venv** (e.g. pydantic-core 2.50.1 vs. 2.46.5, uvloop 0.23.0) because `pyproject.toml` has only lower bounds. | [E]; [R] pyproject.toml |
| B7 | Experiment: entry-point scripts have an **absolute shebang** `#!/opt/hermclaw/venv-<ver>/bin/python3.13`. A process started as `/opt/hermclaw/venv/bin/hermclaw` therefore gets `sys.prefix = …/venv-<ver>` and stays on its release after the symlink is switched. `/opt/hermclaw/venv/bin/python -m …` gets `sys.prefix = /opt/hermclaw/venv` (the symlink path), so its **lazy imports would load the new release** after a switch. | [E] |
| B8 | Repo: `roles/common` creates `/opt/hermclaw/venv` as a **real, non-empty directory** (`python3 -m venv`, `creates:`). The upgrade runbook expects `venv` to be a symlink to `venv-<version>` that `ln -sfn` can switch, and `playbooks/deploy.yml` (referenced there) does not exist yet. Per A7, the first `file state=link` onto that directory fails. LiteLLM venv path: *verifier correction* — `docs/runbooks/upgrade.md` (as edited 2026-10-14 18:14 UTC) now names `/opt/litellm/venv` (`systemd_skeleton_litellm_venv`), which matches `roles/systemd_skeleton/defaults/main.yml`; no `/opt/hermclaw/litellm-venv` reference remains in `docs/runbooks/` or `infra/`. The inconsistency reported earlier is resolved. | [R] |

### C. systemd hardening (trixie: systemd 257)

| # | Fact | Source |
|---|---|---|
| C1 | trixie stable ships **systemd 257.13-1~deb13u1** (accepted into proposed-updates 2026-05-03; changelog dated 2026-04-13 lists CVE fixes incl. CVE-2026-40225/-40226/-29111/-4105 — ids seen only in a search snippet, not verified; earlier 257.8-1~deb13u2 / 257.9-1~deb13u1). Upstream has no `v257.14` tag (checked on GitHub raw: v257.13 exists, v257.14 404), so 257.13 is the newest v257 stable. sid had 261~rc3-1 in June 2026 (snippet); upstream already has `v262`, so the current sid/testing version is not confirmed. | [P/snippet] tracker, piuparts; [P] github.com/systemd/systemd tags |
| C2 | **Implicit `NoNewPrivileges`.** In v257, `context_has_no_new_privileges()` returns true when `NoNewPrivileges=yes`, **or** when the process lacks `CAP_SYS_ADMIN` (true for every `User=` service after the user switch, which happens before this check) **and** any of these is set: `LockPersonality`, `MemoryDenyWriteExecute`, `PrivateDevices`, `ProtectClock`, `ProtectHostname`, `ProtectKernelTunables`, `ProtectKernelModules`, `ProtectKernelLogs`, `RestrictAddressFamilies`, `RestrictNamespaces`, `RestrictRealtime`, `RestrictSUIDSGID`, `SystemCallArchitectures`, `SystemCallFilter`, `SystemCallLog`. | [P] src/core/exec-invoke.c v257.13 |
| C3 | `NoNewPrivileges=`: "the service process and all its children can never gain new privileges through execve() (e.g. via setuid or setgid bits, or filesystem capabilities)". With a mount namespace and no SELinux, explicit NNP also mounts everything `MS_NOSUID`. | [P] systemd.exec(5) v257 |
| C4 | Experiment: `/usr/bin/newuidmap` is **setuid root** (Ubuntu `uidmap`; Debian is expected to match, not verified). As uid 1000 with subuid range 100000:65536, `unshare --user --map-auto` worked without NNP (`0 1000 1 / 1 100000 65536`). Under `setpriv --no-new-privs` it failed with "**newuidmap: write to uid_map failed: Operation not permitted**". `podman unshare` (4.9.3) under NNP failed with "**cannot set up namespace using "/usr/bin/newuidmap": exit status 1**". A secondary source (NixOS) reports the same and fixes it by forcing `NoNewPrivileges=false`. | [E]; [S] NixOS |
| C5 | `@system-service` is the "recommended starting point for allow-listing" and **excludes `@clock`, `@mount`, `@swap`, `@reboot`**. `@mount` = `chroot fsconfig fsmount fsopen fspick mount mount_setattr move_mount open_tree pivot_root umount umount2`. An OCI runtime (crun) needs `mount`/`pivot_root`, and filters are inherited by children, so **`SystemCallFilter=@system-service` cannot be used for the unit that launches rootless Podman**. `@system-service` includes `@aio` (with `io_uring_*`), `@process` (`clone3`, `pidfd_open`), `@default` (`getrandom`, `rseq`, `sched_getaffinity`, `prlimit64`), `@ipc` (`memfd_create`). | [P] seccomp-util.c, systemd.exec(5) |
| C6 | `MemoryDenyWriteExecute=`: rejects W+X `mmap`, `mprotect`/`pkey_mprotect` adding `PROT_EXEC`, `shmat(SHM_EXEC)`. It is "incompatible with … JIT execution engines, executable stacks, and code 'trampoline' feature", can be circumvented through writable non-`noexec` mounts or `memfd_create()`, and should be combined with `SystemCallArchitectures=native`. **Since v254 it uses `prctl(PR_SET_MDWE, PR_MDWE_REFUSE_EXEC_GAIN)`** when the kernel has it (≥ 6.3; trixie runs 6.12) and falls back to seccomp otherwise. | [P] systemd.exec(5); exec-invoke.c; NEWS v254 |
| C7 | Experiment, same `prctl` as systemd uses (C6). (a) The Hermclaw runtime venv (Py 3.12 dev venv and Py 3.13.16 offline venv from B6) worked: ctypes callbacks (libffi 3.4.6), pydantic-core, lxml, tree-sitter + language pack, psycopg-binary (libpq), ssl. uvicorn with **uvloop + httptools** served `GET /api/version` → 200. (b) **cffi old-style `ffi.callback()` fails** ("Cannot allocate write+execute memory"). No package in the venvs calls `ffi.callback` (grep). (c) **LiteLLM 1.104.2 proxy** (Py 3.13) started and `/health/liveliness` returned 200. (d) MDWE is **inherited by child processes** (a child Python had MDWE=1 and was refused W+X). `git grep -P`, `rg -P`, `ssh`, `rsync` still ran. These were Ubuntu/deadsnakes builds, not trixie's. | [E] |
| C8 | The CPython 3.13 JIT exists only in builds configured with `--enable-experimental-jit` (values `no/yes/yes-off/interpreter`). Whether Debian's python3.13 is built that way was not verified. Check it on the host (Implementation consequences). | [P] whatsnew/3.13.rst |
| C9 | `PrivateDevices=yes`: new `/dev` with only API pseudo devices (read-only, `noexec`), `DevicePolicy=closed`, drops `CAP_MKNOD`/`CAP_SYS_RAWIO`, `@raw-io` filter, so `/dev/nvidia*` are gone. `DeviceAllow=` takes a `/dev/…` path or `char-<group>` from `/proc/devices` **with glob support** (`char-nvidia*`). "Device groups not resolvable [at unit start] are not added to the allow list", so pair it with `Wants=/After=modprobe@<module>.service`. `DevicePolicy=closed` additionally allows `/dev/null zero full random urandom`. The `+` ExecStart prefix bypasses `User=`/namespacing **but not cgroup-wide `DevicePolicy=`**. | [P] systemd.exec(5), systemd.resource-control(5), systemd.service(5) |
| C10 | `ProtectHome=yes` makes **`/home`, `/root` and `/run/user`** inaccessible. A unit that relies on `XDG_RUNTIME_DIR=/run/user/<uid>` (rootless Podman) must not set it (or must use `tmpfs` + `BindPaths=`). | [P] systemd.exec(5) |
| C11 | `ProtectSystem=strict` makes the whole hierarchy read-only except `/dev`, `/proc`, `/sys`. `ReadWritePaths=` and `StateDirectory=` etc. exempt paths, and with `PrivateTmp=` `/tmp` and `/var/tmp` stay writable. `full` only covers `/usr`, `/boot`, `/efi`, `/etc`. | [P] systemd.exec(5) |
| C12 | New in v257: `ProtectControlGroups=private\|strict` (cgroup namespace), `PrivateTmp=disconnected`, `PrivateUsers=identity`, `PrivatePIDs=`. "Except for container managers no services should require write access to the control groups hierarchies". | [P] NEWS v257; systemd.exec(5) |
| C13 | `DefaultLimitNOFILE=` defaults to `1024:` + the high hard limit, so services start with a soft fd limit of 1024. | [P] systemd-system.conf(5) |
| C14 | NVIDIA `/proc/devices` group names used for CUDA services in practice: `char-nvidiactl`, `char-nvidia-frontend`, `char-nvidia-uvm`, `char-nvidia-caps`. The list differs between driver versions. | [S] nixpkgs commits |
| C15 | Ollama's documented manual unit (`User=ollama`, `Restart=always`) has **no sandboxing**. The docs fix a suspend/resume bug with `rmmod nvidia_uvm && modprobe nvidia_uvm`. | [P] ollama v0.40.4 docs |
| C16 | Repo: `hermclaw-exec-worker.service.j2` says "NoNewPrivileges/RestrictNamespaces/PrivateDevices stay off" but sets **`ProtectKernelTunables`, `ProtectKernelModules`, `ProtectKernelLogs`, `ProtectClock`, `ProtectHostname`, `RestrictRealtime`, `LockPersonality`, `SystemCallArchitectures=native`, `RestrictAddressFamilies`**. With `User=hermclaw`, all nine **imply NNP** (C2), so rootless Podman cannot create its user namespace (C4). It probably works only while a pause process from an earlier podman call (e.g. the `podman` role's `podman info` check) is still alive, and then fails after the next reboot. That last part was not verified (plausible from podman's pause-process design). `ProtectHome` and `ProtectControlGroups` are correctly absent. | [R]; C2, C4 |
| C17 | Repo: API, scheduler, LiteLLM and backup units already use `NoNewPrivileges`, `ProtectSystem=strict` + `ReadWritePaths`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `RestrictAddressFamilies`, `CapabilityBoundingSet=`, `SystemCallArchitectures=native`, but **no `SystemCallFilter`** and **no `MemoryDenyWriteExecute`**. The model worker correctly omits `PrivateDevices` (GPU). The installed `ollama.service` has no hardening. | [R] |

### D. systemd credentials (systemd 257)

| # | Fact | Source |
|---|---|---|
| D1 | `LoadCredential[Encrypted]=ID:PATH`: the data is put "at a read-only location that (if possible and permitted) is backed by non-swappable memory", is accessible only to the unit's `User=` (and root), and is exposed as `$CREDENTIALS_DIRECTORY`, `%d` (in `Environment=`) or `/run/credentials/<unit>` for system services. **Absolute PATH → a missing file is fatal**. A relative/omitted path searches `/etc/credstore[.encrypted]`, `/run/…`, `/usr/lib/…`, and a miss there "is not considered fatal". When `DevicePolicy` is closed/strict, `/dev/tpmrm0` is added to `DeviceAllow` automatically. | [P] systemd.exec(5) v257 |
| D2 | Encryption: AES256-GCM keyed by SHA256 of the **host key `/var/lib/systemd/credential.secret`** (root-only, created by `systemd-creds setup` or on first host-key encrypt) and/or a **TPM2**-derived key. `--with-key=host\|tpm2\|host+tpm2\|null\|auto\|auto-initrd` (`-H`/`-T`). **`auto` (default) = TPM2 if present and not in a container, plus the host key if `/var/lib/systemd` is persistent**, so on a TPM machine it is host+tpm2. The TPM2 key is bound to **PCR 7 by default** (`--tpm2-pcrs=` empty = no PCRs). `systemd.exec(5)` v257 misspells the file as `credentials.secret`. `systemd-creds(1)` and the source use `credential.secret`. | [P] systemd-creds(1); creds-util.c; CREDENTIALS.md |
| D3 | The file name (or `--name=`) is embedded in the ciphertext, and decryption refuses a mismatch ("Embedded credential name … does not match filename …, refusing", `EREMOTE`) unless `SYSTEMD_CREDENTIAL_VALIDATE_NAME=0`. | [P] creds-util.c |
| D4 | `SetCredential=` only for non-secrets (unit files are world-readable, also via D-Bus). `SetCredentialEncrypted=` takes literal ciphertext (generate with `systemd-creds encrypt -p --name=… - -`). Priority: `LoadCredential*` > `ImportCredential` > `SetCredential*` (fallback default). | [P] systemd.exec(5), CREDENTIALS.md |
| D5 | `ImportCredential=GLOB` (v254) takes only a single trailing `*` and searches system credentials and then the credstore directories. **v257** added renaming (`ImportCredential=a.*:b.`), and the renamed name is the one validated against the embedded name (D3). A credential not found by `ImportCredential` is silently skipped. | [P] systemd.exec(5); exec-credential.c; NEWS v257 |
| D6 | Encrypted credentials for **per-user** service managers must be made with `systemd-creds encrypt --user [--uid=]` (v256+), and the user's UID, user name and machine-id go into the key. | [P] systemd-creds(1); NEWS v256 |
| D7 | Feature timeline (all present in 257): v247 credentials; **v250** `LoadCredentialEncrypted=`/`SetCredentialEncrypted=` + `systemd-creds`; v251 credstore search paths, `%d`, empty-key creds; v253 tmpfiles creates `/etc/credstore`; **v254** `ImportCredential=`, MDWE via prctl; v256 `--user/--uid`, `null` key (renamed from `tpm2-absent`), `--allow-null`, Varlink API; **v257** `ImportCredential` renaming, `systemd-creds cat` expects base64, `has-tpm2` moved to **`systemd-analyze has-tpm2`**. | [P] NEWS |
| D8 | Limits: 1 MiB per credential and 1 MiB total per service. An encrypted file may be 1 MiB + 128 KiB. | [P] creds-util.h |
| D9 | Enable mount namespacing so the credentials directory is invisible to other services. `ProtectSystem=` and similar imply `PrivateMounts=`. | [P] CREDENTIALS.md |
| D10 | Repo: units use `LoadCredentialEncrypted=<id>:/etc/credstore.encrypted/hermclaw-<id>` (absolute → fatal if missing, good). `hermclaw-credential.sh` encrypts with `--name=<id>` and **`--with-key=${HERMCLAW_CREDS_WITH_KEY:-auto}`**. Worker tokens are decrypted on the orchestrator and re-encrypted on the worker (`no_log`). The application reads `cred:<name>` from `$CREDENTIALS_DIRECTORY`. | [R] |

### E. uvicorn behind nginx

| # | Fact | Source |
|---|---|---|
| E1 | uvicorn **0.54.0** (2026-09-25) is the latest release and needs Python ≥ 3.10. `[standard]` adds `httptools`, `uvloop`, `websockets`, `watchfiles`, `python-dotenv`, `pyyaml`. | [P] PyPI |
| E2 | `--proxy-headers` defaults to on but only trusts `--forwarded-allow-ips`. **That default is `$FORWARDED_ALLOW_IPS` or `127.0.0.1,::1`, and it applies only when `forwarded_allow_ips` is `None`.** IPs, networks (`10.0.0.0/16`) and literals are accepted, and `'*'` trusts everything. | [P] settings.md, config.py |
| E3 | `ProxyHeadersMiddleware`: with a trust list, the client is the **right-most untrusted** `X-Forwarded-For` entry. With `'*'` it is the **left-most** entry, which the client controls (nginx's `$proxy_add_x_forwarded_for` appends). `X-Forwarded-Proto`: if the header occurs several times the **last header line** wins, and the whole (stripped) value must be exactly `http`, `https`, `ws` or `wss`, so a comma-separated value such as `https, http` is ignored (scheme unchanged). | [P] proxy_headers.py |
| E4 | `--workers N` is a spawn-based manager that restarts dead children (`SIGHUP` = rolling restart). `workers`/`reload` need the app as an **import string** (otherwise the warning "You must pass the application as an import string to enable 'reload' or 'workers'"). `WEB_CONCURRENCY` sets the default. The docs recommend nginx in front, preferably over a UDS. | [P] deployment/index.md, main.py |
| E5 | `--timeout-keep-alive` default **5 s**. nginx upstream `keepalive_timeout` default **60 s** (since 1.15.3). R-028 enables upstream `keepalive 16;` on `.223`. | [P] settings.md; nginx upstream doc; R-028 |
| E6 | `timeout_graceful_shutdown` defaults to `None` (wait for connections). `server_header` defaults to `True`. | [P] config.py |
| E7 | Repo: the API runs `uvicorn.run(create_app(), host, port, proxy_headers=True, forwarded_allow_ips="*")` with uvicorn's default log config, server header and no graceful timeout. The worker runner uses `log_config=None, access_log=False, proxy_headers=False, server_header=False, timeout_graceful_shutdown=30`. SSE fan-out goes through **PostgreSQL LISTEN/NOTIFY** (`hermclaw/events/store.py`), so it is not process-local. Worker heartbeats store `request.client.host` as `last_remote_addr` (and `address` for heartbeat-registered workers). Workers reach `:8000` directly (`ORCHESTRATOR_URL`). | [R] |

## Decision / recommendation for Hermclaw

### 1. ansible-core
- **Control node**: dedicated venv with **`ansible-core==2.21.5`** (range `>=2.21.5,<2.22`; A1, A2). It is the only
  branch still in its general-bugfix phase until Nov 2026 and supported (security) until Nov 2027 (A2). Do **not**
  use trixie's `ansible-core` package (2.19.4-0+deb13u1, or 2.19.11-0+deb13u1 once that stable update is released;
  upstream EOL Nov 2026, missing the 2.19.12–2.19.14 fixes, A3), except as an emergency fallback (it satisfies the
  role code's `>=2.19`; the 2.19.4-0+deb13u1 build predates CVE-2026-11332, whose Debian fix is the 2.19.11 stable
  update; the CVE matters only if `ansible-galaxy role install` is used, and Hermclaw needs no Galaxy content). Move to 2.22 only after its GA (Nov 2026) **and** only on a control node with Python ≥ 3.13 (Debian 13
  qualifies; the Ubuntu 24.04 build host with 3.12 does not).
- **Targets (Debian 13)**: `python3` 3.13.5 (supported by 2.21 targets 3.9–3.14). Required Debian packages for the
  modules in use: `python3-apt` (apt), `python3-debian` (deb822_repository), **`python3-packaging`** (pip module, A5),
  `acl` (become_user to `hermclaw`), `sudo`, `python3-venv`. Keep `interpreter_python = /usr/bin/python3`.
- **Modules for the deploy playbook** (all `ansible.builtin`, no collections): `copy`/`get_url` (with `checksum:
  sha256:…`), `unarchive` (`remote_src: true`, `creates:`), `stat`, `file` (`state: link`, `follow: false`), `pip`
  (`virtualenv_command: /usr/bin/python3 -m venv`, `umask: "0022"`), `command` (argv form, `changed_when: false` for
  checks), `template`, `systemd_service`, `uri` (`check_mode: false`, `retries`/`until`), `find`, `assert`,
  `block`/`rescue`, `meta: flush_handlers`.
- **Idempotency gate (32.10)**: (1) converge, (2) second run must report `changed=0 failed=0` for every host,
  (3) `--check --diff` must report `changed=0`. The pip task must be guarded by a release marker (A6), and `uri`
  needs `check_mode: false` (A9).

### 2. Release layout and offline install (orchestrator and both workers)
```
/opt/hermclaw/
  wheelhouse/<version>/           *.whl + SHA256SUMS (from hermclaw-wheelhouse-<version>.tar.gz)
  venv-<version>/                 root:root 0755, created in place, never moved (B2)
  venv-<version>/.hermclaw-release   marker: version + tarball sha256, written last
  venv -> venv-<version>          relative symlink, switched atomically (A7)
```
- **Keep 3 releases** (`hermclaw_keep_releases: 3` = current + 2 previous, never deleting the one `venv` points to)
  plus their wheelhouses, so `ln`-style rollback never needs the network.
- **Build the wheelhouse on Debian 13 / CPython 3.13** (on `.225` or in `docker.io/library/debian:trixie` with
  podman). That way no platform tags have to be guessed. Cross-building from the Ubuntu 24.04 / Py 3.12 build host
  is possible but must list every manylinux tag that any dependency uses (B5). pip does not expand `manylinux_2_Y`
  tags, so the safe form generates the whole range, e.g.
  `$(for y in $(seq 41 -1 17); do printf -- '--platform manylinux_2_%s_x86_64 ' "$y"; done) --platform manylinux2014_x86_64`
  instead of the five hand-picked tags below (which were sufficient for the dependency set tested in B5):
  ```bash
  python3 -m build --wheel --outdir dist/                    # hermclaw-<ver>-py3-none-any.whl
  python3 -m pip download --only-binary=:all: --python-version 3.13 --implementation cp --abi cp313 \
    --platform manylinux_2_41_x86_64 --platform manylinux_2_34_x86_64 --platform manylinux_2_28_x86_64 \
    --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 \
    -c constraints-<ver>.txt -d wheelhouse/<ver> dist/hermclaw-<ver>-py3-none-any.whl
  cp dist/hermclaw-<ver>-py3-none-any.whl wheelhouse/<ver>/
  (cd wheelhouse/<ver> && sha256sum *.whl > SHA256SUMS)
  tar -C wheelhouse -czf hermclaw-wheelhouse-<ver>.tar.gz <ver> && sha256sum hermclaw-wheelhouse-<ver>.tar.gz
  ```
- **Lock what was tested (B6)**: create `constraints-<ver>.txt` from a *clean runtime venv* that passed the release
  tests (`pip freeze --exclude hermclaw`, not the dev venv, which also contains dev tools and LiteLLM). The wheelhouse
  then holds exactly one version per package, so `--no-index` installs are deterministic. `--require-hashes` with a
  generated `name==ver --hash=sha256:…` file is optional defence in depth. The mandatory integrity check is the
  tarball sha256 verified by Ansible.
- **Install on the target** (what the playbook does):
  ```bash
  /usr/bin/python3 -m venv /opt/hermclaw/venv-<ver>
  /opt/hermclaw/venv-<ver>/bin/pip install --no-index --find-links=/opt/hermclaw/wheelhouse/<ver> \
      --only-binary=:all: --no-cache-dir "hermclaw==<ver>"
  /opt/hermclaw/venv-<ver>/bin/pip check
  /opt/hermclaw/venv-<ver>/bin/hermclaw --version
  ```
  An exact pre-release pin (`==0.1.0rc1`) needs no `--pre` (B6). Keep pip's default byte-compilation, because the
  venv is read-only for the services (`ProtectSystem=strict`).
- **Never start long-running services as `<venv>/bin/python -m …`**. Always use the entry points
  (`/opt/hermclaw/venv/bin/hermclaw …`, `hermclaw-exec-worker`, `hermclaw-model-worker`), so a running process stays
  on its own release across a switch (B7). The units already do this.
- **LiteLLM** (`.225`, separate venv per R-006): same pattern, `/opt/litellm/venv-1.104.2` +
  `/opt/litellm/venv -> venv-1.104.2` (the runbook already uses `/opt/litellm/venv`, B8; `litellm[proxy]==1.104.2`
  is on PyPI, uploaded 2026-10-08, `requires_python >=3.10,<3.15`; PyPI latest is 1.105.1, not adopted here).

### 3. systemd hardening per unit
**Orchestrator Python services** (`hermclaw-api`, `hermclaw-scheduler`, `hermclaw-litellm`, `hermclaw-backup`):
keep the existing set (C17) and add, behind a per-unit switch that is enabled after the staging check below:
```ini
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
MemoryDenyWriteExecute=yes          # api, litellm, scheduler (C7); not for backup until pg_dump is tested
RemoveIPC=yes
LimitNOFILE=8192                    # api only: SSE connections + DB pool (C13)
```
Do **not** add `ProtectProc=invisible` or `ProcSubset=pid` to services that inspect other processes (port-owner checks
of `hermclaw doctor` / infratools read `/proc/<pid>/comm`).

**Execution worker** (`hermclaw-exec-worker`, rootless Podman): **remove every directive that implies NNP** (C2, C16)
and keep only mount-namespace protections, which do not set NNP:
```ini
# keep
ProtectSystem=full
UMask=0027
KillMode=mixed
# REMOVE (each implies NoNewPrivileges for a User= unit and breaks setuid newuidmap/newgidmap):
#   ProtectKernelTunables ProtectKernelModules ProtectKernelLogs ProtectClock ProtectHostname
#   RestrictRealtime LockPersonality SystemCallArchitectures RestrictAddressFamilies
# NEVER add: NoNewPrivileges, PrivateDevices, RestrictNamespaces, RestrictSUIDSGID, SystemCallFilter,
#   MemoryDenyWriteExecute (inherited into containers, C7d), ProtectHome (hides /run/user, C10),
#   ProtectControlGroups (crun writes cgroupfs, C12), CapabilityBoundingSet= (the setuid-root helper needs
#   CAP_SETUID/CAP_SETGID from the bounding set)
```
The isolation of executed code comes from the container flags already in `worker/execution/sandbox.py`
(`--network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges --userns=keep-id`, limits, default
seccomp). The container-level NNP is applied by crun inside the container and does not affect `newuidmap`.
Alternative (larger change, not required): run the worker as a **user unit** of `hermclaw` (lingering user manager).
That needs `systemd-creds encrypt --user --uid=hermclaw` credentials (D6), and the same "no namespacing options" rule
applies there because user units imply `PrivateUsers=` for them.

**Model worker** (`hermclaw-model-worker`, nvidia-smi, ffmpeg NVENC): keep (no `PrivateDevices`). Add
`DevicePolicy=closed` + `DeviceAllow=char-nvidia* rw` (C9, C14) once `grep nvidia /proc/devices` on `.224` has
confirmed the group names. **No `MemoryDenyWriteExecute`** (inherited by ffmpeg/CUDA children, untested; C7d).

**Ollama** (`.224`): add a separate drop-in `ollama.service.d/50-hermclaw-hardening.conf`, gated by
`ollama_hardening: false` until the GPU smoke test (`ollama_expected_gpu_library: CUDA`) passes with it:
```ini
[Unit]
Wants=modprobe@nvidia_uvm.service
After=modprobe@nvidia_uvm.service nvidia-persistenced.service
[Service]
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=<ollama home> <OLLAMA_MODELS dir>
ProtectHome=yes                       # only if home/models are not below /home
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
CapabilityBoundingSet=
DevicePolicy=closed
DeviceAllow=char-nvidia* rw
# NOT: PrivateDevices (hides /dev/nvidia*), MemoryDenyWriteExecute and SystemCallFilter (CUDA/ggml untested)
```
With NNP, CUDA can no longer use the setuid `nvidia-modprobe` to create missing `/dev/nvidia-uvm*` nodes. Create them
before Ollama starts with a root oneshot `hermclaw-nvidia-devnodes.service` (`Type=oneshot`, `RemainAfterExit=yes`,
two lines `ExecStart=/usr/bin/nvidia-modprobe -c=0` and `ExecStart=/usr/bin/nvidia-modprobe -u -c=0`,
`Before=ollama.service hermclaw-model-worker.service`). Verifier check against the NVIDIA/nvidia-modprobe source
(`option-table.h`, `nvidia-modprobe.c`, `common-utils/nvgetopt.c`, main branch, **not** the exact version Debian ships):
`-c`/`--create-nvidia-device-file=MINOR` takes an integer, `-c=0` is parsed correctly (`-c0` is **not**), and with
`-u`/`--unified-memory` the tool loads `nvidia-uvm` and creates the **UVM** device file(s) *instead of* the
`nvidia` ones. So `-u -c=0` alone does not create `/dev/nvidia0`/`/dev/nvidiactl`, hence the two lines. Still run
`nvidia-modprobe --help` on `.224` to confirm the installed build. After the documented `rmmod/modprobe nvidia_uvm`
workaround (C15), restart Ollama, because `DeviceAllow` resolves majors at unit start.

### 4. Credentials (systemd 257.13)
- Keep **`LoadCredentialEncrypted=<id>:/etc/credstore.encrypted/hermclaw-<id>`** with an absolute path, so a missing
  credential fails the start (D1). Do **not** switch to `ImportCredential=`: a miss there is silent (D5). It would
  work with v257 renaming (`ImportCredential=hermclaw-worker-token-*:worker-token-`), but loses the fail-fast
  property. Do **not** use `SetCredentialEncrypted=` (ciphertext in git-managed unit files gains nothing over the
  credstore).
- **Encrypt with the host key explicitly**: set the default of `HERMCLAW_CREDS_WITH_KEY` to **`host`** (D2). With
  `auto`, a host with a TPM2 gets host+tpm2 bound to **PCR 7**. Toggling Secure Boot (the `nvidia` role's MOK/Secure
  Boot path on `.224`) or changing the Secure Boot databases makes every credential undecryptable (status
  243/CREDENTIALS). Whether MOK enrollment alone changes PCR 7 was not verified. Accepted trade-off: root can always
  read the host key. Hermclaw's secrets are regenerable (api-admin-token, litellm-master-key, worker tokens), and the
  GitLab token is re-issued from `.226`.
- **Do not back up `/var/lib/systemd/credential.secret` together with `/etc/credstore.encrypted/`** (together they
  are plaintext-equivalent). Disaster recovery means re-running `hermclaw-credential generate/set`.
- Commands (root):
  ```bash
  systemctl --version | head -1                          # expect "systemd 257 (257.13-1~deb13u1)" or newer
  systemd-analyze has-tpm2 || true                       # informational (v257 location)
  systemd-creds setup                                    # creates /var/lib/systemd/credential.secret if missing
  HERMCLAW_CREDS_WITH_KEY=host /opt/hermclaw/bin/hermclaw-credential generate api-admin-token
  # equivalent raw call (secret only on stdin):
  head -c 32 /dev/urandom | base64 -w0 | \
    systemd-creds encrypt --with-key=host --name=api-admin-token - /etc/credstore.encrypted/hermclaw-api-admin-token
  /opt/hermclaw/bin/hermclaw-credential check api-admin-token        # OK/FAIL, never prints the value
  systemd-run -P --wait -p LoadCredentialEncrypted=api-admin-token:/etc/credstore.encrypted/hermclaw-api-admin-token \
    systemd-creds cat api-admin-token | wc -c                         # > 0 bytes, value not shown
  ```

### 5. uvicorn (API on `.225`)
| Setting | Value | Why |
|---|---|---|
| workers | **1** | single-user LAN load; `uvicorn.run(app_object)` cannot spawn workers anyway (E4). SSE is LISTEN/NOTIFY-based, so `hermclaw.api.app:create_app --factory --workers N` stays possible later (E7) |
| host / port | `HERMCLAW_API_HOST=192.168.178.225`, `8000` | workers call the API directly (E7). nftables on `.225` allows `:8000` only from `.222/.223/.224` (R-028) |
| proxy_headers | `True` | nginx on `.223` sets `X-Forwarded-For`/`-Proto` |
| forwarded_allow_ips | **`192.168.178.223,127.0.0.1`** (new setting `HERMCLAW_API_FORWARDED_ALLOW_IPS`, default `127.0.0.1`) | `"*"` takes the **left-most, client-supplied** XFF entry even through nginx (E3) and lets direct callers (workers) set `request.client.host`/scheme (E7). With the trust list the right-most untrusted entry is used, so nginx's `$proxy_add_x_forwarded_for` is safe. Matches R-028 |
| timeout_graceful_shutdown | **10** | SSE streams otherwise block shutdown until systemd's `TimeoutStopSec=30s` SIGKILL (E6) |
| timeout_keep_alive | **75** | larger than nginx's upstream `keepalive_timeout 60s` (E5), so nginx does not reuse a connection uvicorn just closed. Alternatively set `keepalive_timeout 4s;` in the nginx upstream block. The 502-race reasoning is operational knowledge, not re-verified here |
| server_header | `False` | as in the worker runner |
| log_config / access_log | `None` / `False` | keep the JSON logging (as in `worker/common/server.py`) |
| systemd | `Type=exec`, `Restart=on-failure`, `TimeoutStopSec=30s`, `ExecStart=/opt/hermclaw/venv/bin/hermclaw api` | unchanged. `TimeoutStopSec` > graceful timeout |

## Implementation consequences

**INSTALLATION.md must:**
1. Control node: `sudo apt install python3 python3-venv openssh-client` and then
   `HERMCLAW_ANSIBLE_CORE_SPEC='ansible-core>=2.21.5,<2.22' scripts/bootstrap/bootstrap-control-node.sh`, or by hand
   `python3 -m venv ~/.local/share/hermclaw/ansible-venv && ~/.local/share/hermclaw/ansible-venv/bin/pip install
   'ansible-core==2.21.5'`. State that trixie's `apt install ansible-core` (2.19.4-0+deb13u1, or 2.19.11-0+deb13u1
   after the pending stable update; check with `apt-cache policy ansible-core`) is only a fallback.
2. Targets: Debian 13 with `python3` present (`ansible -m raw -a 'apt-get install -y python3' <host>` on minimal
   installs). The `common` role then installs `python3-apt python3-debian python3-packaging python3-venv acl sudo`.
3. Release build: the wheelhouse commands from Decision §2, the sha256 of the tarball, and
   `ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml -e hermclaw_version=<ver> -e
   hermclaw_wheelhouse_sha256=<sha>`.
4. Host verification after deploy:
   ```bash
   systemd-analyze verify /etc/systemd/system/hermclaw-*.service
   systemd-analyze security --no-pager hermclaw-api.service hermclaw-scheduler.service hermclaw-litellm.service
   grep -E 'NoNewPrivs|Seccomp' /proc/$(systemctl show -p MainPID --value hermclaw-api)/status   # NoNewPrivs: 1
   grep NoNewPrivs /proc/$(systemctl show -p MainPID --value hermclaw-exec-worker)/status       # .222: must be 0
   sudo -u hermclaw XDG_RUNTIME_DIR=/run/user/$(id -u hermclaw) podman unshare cat /proc/self/uid_map  # 2 lines
   python3 -c "import sysconfig; print('jit' in (sysconfig.get_config_var('CONFIG_ARGS') or ''))"     # expect False
   stat -c '%A %n' /usr/bin/newuidmap /usr/bin/newgidmap; getcap /usr/bin/newuidmap /usr/bin/newgidmap
   grep -i nvidia /proc/devices                                                                     # .224
   journalctl -k -g 'audit.*SECCOMP|write\+execute' --since -1h                                     # after MDWE/filter
   ```
5. Credentials: §4 commands, the warning about Secure Boot/TPM, and the backup rule for `credential.secret`.

**Deployment playbook (`playbooks/deploy.yml`, P32) must:**
1. **Migrate the bootstrap venv once**: if `/opt/hermclaw/venv` is a directory and not a link, remove it (it only
   holds the empty bootstrap venv) before the first `file state=link` (A7, B8). Better: change `roles/common` so it
   no longer creates `hermclaw_venv` (see repo changes).
2. Transfer and verify: `copy` the wheelhouse tarball to `/var/cache/hermclaw/` (or `get_url` from the GitLab package
   registry with `checksum: "sha256:{{ hermclaw_wheelhouse_sha256 }}"`), then `unarchive` to
   `/opt/hermclaw/wheelhouse/<ver>` with `creates:`.
3. Install guarded by the marker: `stat` `<venv-ver>/.hermclaw-release`. If it is missing, remove any half-installed
   `venv-<ver>`, then `ansible.builtin.pip` with `name: "hermclaw=={{ hermclaw_version }}"`, `virtualenv:
   /opt/hermclaw/venv-{{ hermclaw_version }}`, `virtualenv_command: /usr/bin/python3 -m venv`, `extra_args:
   "--no-index --find-links=/opt/hermclaw/wheelhouse/{{ hermclaw_version }} --only-binary=:all: --no-cache-dir"`,
   `umask: "0022"` (files must be readable by `hermclaw`), `when: not marker.stat.exists`. Then `pip check` and
   `hermclaw --version` (`changed_when: false`), and write the marker last.
4. Orchestrator order: render config and units (`template`, handlers `daemon_reload`). Stop `hermclaw-scheduler`
   (drain). Run `/opt/hermclaw/venv-<ver>/bin/hermclaw config-check`, then `… migrate` as `hermclaw` (with a
   `changed_when` derived from its output). Switch the symlink with `file: src=venv-<ver> dest=/opt/hermclaw/venv
   state=link follow=false`, restart the api/scheduler/litellm units with `systemd_service`, and poll
   `uri: url=http://{{ hermclaw_orchestrator_ip }}:8000/api/health status_code=200` with `retries: 30, delay: 2,
   until: result.status == 200, check_mode: false`. In a `block/rescue`, switch the link back to the previous
   release, restart, and fail.
5. Workers: the same venv steps. Health via `uri` on `:8787/health` and Ollama `:11434/api/version`.
6. Prune: `find paths=/opt/hermclaw patterns='venv-*' file_type=directory`, sort by marker mtime, and delete all but
   `hermclaw_keep_releases` (3), never the link target. Do the same for `wheelhouse/<ver>`.
7. Units: apply the exec-worker changes (Decision §3) **before** enabling `hermclaw-exec-worker`. Add the
   staged `SystemCallFilter`/`MemoryDenyWriteExecute` switches for api/scheduler/litellm, and the Ollama hardening and
   devnode units gated by `ollama_hardening`.
8. Idempotency test in CI/converge: run 1, run 2 (assert every `PLAY RECAP` line has `changed=0` and `failed=0`),
   then `--check --diff` (assert `changed=0`). Example gate:
   `ansible-playbook … | tee run2.log; awk '/PLAY RECAP/{f=1;next} f&&/changed=/{if($0!~/changed=0 /||$0!~/failed=0 /)bad=1} END{exit bad}' run2.log`.

## Open risks
- **Debian versions are snippet-based** (A3, A4, B1, C1). In particular, ansible-core in trixie may be 2.19.4 or
  2.19.11 depending on whether the proposed update has been released. Confirm with `apt-cache policy ansible-core
  systemd python3.13 python3-pip-whl` on the hosts.
- **MDWE/seccomp results come from Ubuntu builds** (C7). trixie's python3.13 (3.13.5), libffi and the LiteLLM venv
  must pass the staging check on `.225` before `MemoryDenyWriteExecute`/`SystemCallFilter` become default. A future
  dependency that uses cffi `ffi.callback()` would fail under MDWE. `pg_dump` under MDWE was not tested.
- **Exec worker without the NNP-implying directives** has a weaker systemd sandbox (protection relies on rootless
  Podman plus the container flags). The pause-process "works until reboot" behaviour (C16) is plausible but was not
  reproduced with podman 5.4.2. Verify on `.222` after a reboot.
- **NVIDIA device groups** (C14) are secondary-sourced; the `nvidia-modprobe` flag syntax was checked against the
  upstream source (main branch), not the Debian build on `.224`.
  `DeviceAllow` resolves majors at start, so a module reload needs a service restart.
- **TPM/PCR 7**: if someone sets `HERMCLAW_CREDS_WITH_KEY=auto/tpm2` on a TPM host, Secure Boot changes lock the
  services out. Losing `/var/lib/systemd/credential.secret` (reinstall) makes every credential on that host
  unrecoverable (regenerate).
- **Venv and Python minor upgrades**: venvs symlink `/usr/bin/python3.13`. A Debian dist-upgrade to a new Python
  minor (forky) breaks all `venv-*`, so redeploy after OS upgrades.
- **No lock file yet**: until `constraints-<ver>.txt` exists, two builds of the same version can contain different
  dependency versions (B6).
- **ansible-core lifecycle**: 2.21 general-bugfix support ends with 2.22 GA (Nov 2026); it then gets critical
  fixes until May 2027 and security fixes until EOL Nov 2027 (A2). 2.22 needs Python ≥ 3.13 on the control node.
- **`timeout_keep_alive` vs. nginx keepalive**: the 502 race on reused upstream connections is operational knowledge
  and was not reproduced.
