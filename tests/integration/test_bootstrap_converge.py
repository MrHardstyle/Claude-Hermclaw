"""P01 1.15 – the real bootstrap playbook converges idempotently on a Debian 13 userland.

``playbooks/bootstrap.yml`` runs unmodified inside a throw-away Debian 13 "trixie" container
(``python:3.12-slim``, network ``none``) for three inventory hosts that all use the local
connection: orchestrator .225, WebUI .223 and execution worker .222. Package installation and
service management are switched off (``hermclaw_install_packages=false``,
``hermclaw_manage_services=false``: no apt mirror and no systemd in this build environment,
BLOCKER-001); the roles that need real packages or hardware (postgresql, nodejs, nvidia,
ollama) are excluded by tag/host. Everything else runs for real: the Debian 13 assertion,
check-host.sh, users, directories, venv, SSH key generation and distribution with
``restrict,from=``, known_hosts, sub-IDs, cgroup delegation drop-in, linger flags, nginx site
files, Wake-on-LAN and Hermclaw unit files. The second run and a ``--check`` run must report
``changed=0``.

Ansible itself comes from this repository's virtualenv (mounted read-only, same CPython 3.12
ABI); ``ssh-keygen`` and ``git`` are the host binaries (the slim image has none).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tests.integration.test_bootstrap_infra import REPO, _run

pytestmark = pytest.mark.integration

IMAGE = "docker.io/library/python:3.12-slim"
SITE_PACKAGES = Path(sys.executable).parent.parent / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "site-packages"
HOSTS = ["hermclaw-orchestrator", "hermclaw-webui", "hermclaw-exec-222"]
LAUNCHER = "import sys; from ansible.cli.playbook import main; sys.argv[0] = 'ansible-playbook'; sys.exit(main())"
RECAP = re.compile(r"^(\S+)\s+: ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", re.M)


def _podman(*args: str, timeout: int = 600, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return _run(["podman", *args], cwd=Path("/"), timeout=timeout, input_text=input_text)


@pytest.fixture(scope="module")
def trixie() -> Iterator[str]:
    """A running Debian 13 container with the repository and Ansible mounted read-only."""
    if os.geteuid() != 0 or shutil.which("podman") is None:
        pytest.skip("needs root and podman")
    if sys.version_info[:2] != (3, 12) or not (SITE_PACKAGES / "ansible").is_dir():
        pytest.skip("the mounted Ansible must match the image's CPython 3.12")
    if _podman("image", "exists", IMAGE).returncode != 0:
        pytest.skip(f"{IMAGE} not available locally")
    tools = {name: shutil.which(name) for name in ("ssh-keygen", "git")}
    if not all(tools.values()):
        pytest.skip("host ssh-keygen and git are needed inside the container")
    name = f"hcl-converge-{uuid.uuid4().hex[:8]}"
    argv = ["run", "-d", "--rm", "--name", name, "--network=none", "--tmpfs", "/tmp:exec",
            "-v", f"{REPO}:/src:ro", "-v", f"{SITE_PACKAGES}:/ansible-site:ro"]  # fmt: skip
    for tool, path in tools.items():
        argv += ["-v", f"{path}:/opt/hosttools/{tool}:ro"]
    started = _podman(*argv, IMAGE, "sleep", "3600")
    if started.returncode != 0:
        pytest.skip(f"cannot start a container here: {started.stderr.strip()[:300]}")
    try:
        release = _podman("exec", name, "cat", "/etc/os-release").stdout
        assert 'VERSION_CODENAME=trixie' in release and 'VERSION_ID="13"' in release, release
        # sshd is not installed in the slim image: give the worker a host key like openssh-server would
        setup = _podman("exec", name, "sh", "-c",
                        "mkdir -p /etc/ssh && /opt/hosttools/ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key")  # fmt: skip
        assert setup.returncode == 0, setup.stderr
        yield name
    finally:
        _podman("rm", "-f", name, timeout=120)


def _write_inputs(tmp_dir: Path) -> None:
    inventory = {
        "all": {
            "vars": {
                "ansible_connection": "local",
                "ansible_python_interpreter": "/usr/local/bin/python3",
                "hermclaw_orchestrator_ip": "192.168.178.225",
                "hermclaw_lan_broadcast": "192.168.178.255",
            },
            "children": {
                "orchestrator": {"hosts": {"hermclaw-orchestrator": {"ansible_host": "192.168.178.225"}}},
                "webui": {"hosts": {"hermclaw-webui": {"ansible_host": "192.168.178.223", "hermclaw_web_server_name": "hermclaw.lan"}}},
                "execution_workers": {
                    "hosts": {
                        "hermclaw-exec-222": {
                            "ansible_host": "192.168.178.222",
                            "hermclaw_worker_id": "exec-222",
                            "wol_mac": "00:00:00:00:00:00",
                            "wol_interface": "lo",
                        }
                    }
                },
                "model_workers": {"hosts": {}},
            },
        }
    }
    (tmp_dir / "hosts.yml").write_text(yaml.safe_dump(inventory), encoding="utf-8")
    overrides = {
        "hermclaw_install_packages": False,
        "hermclaw_manage_services": False,
        "ansible_become": False,  # already root; the image has no sudo
        "common_python_bin": "/usr/local/bin/python3",
        "common_preflight_min_cpus": "1",
        "common_preflight_min_ram_mb": "256",
        "common_preflight_min_disk_gb": "1",
        # no systemd-creds / visudo in the image: the helpers are tested on the host instead
        "systemd_skeleton_generate_credentials": False,
        "common_ssh_runtime_sudo_commands": [],
    }
    (tmp_dir / "overrides.json").write_text(json.dumps(overrides), encoding="utf-8")


def _playbook(container: str, workdir: str, *extra: str) -> tuple[subprocess.CompletedProcess[str], dict[str, tuple[int, ...]]]:
    script = (
        "set -e; export PATH=/opt/hosttools:$PATH PYTHONPATH=/ansible-site ANSIBLE_CONFIG=/src/infra/ansible/ansible.cfg "
        f"ANSIBLE_HOME={workdir}/home ANSIBLE_LOCAL_TEMP={workdir}/tmp ANSIBLE_REMOTE_TEMP={workdir}/rtmp "
        "ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0 ANSIBLE_RETRY_FILES_ENABLED=0; cd /src/infra/ansible; "
        f'exec python3 -c "{LAUNCHER}" -i {workdir}/hosts.yml playbooks/bootstrap.yml '
        f"--skip-tags postgresql,nodejs -e @{workdir}/overrides.json " + " ".join(extra)
    )
    res = _podman("exec", container, "bash", "-c", script, timeout=900)
    recap = {m.group(1): tuple(int(x) for x in m.groups()[1:]) for m in RECAP.finditer(res.stdout)}
    return res, recap


def _stat(container: str, *paths: str) -> dict[str, str]:
    res = _podman("exec", container, "stat", "-c", "%n %a %U %G", *paths)
    assert res.returncode == 0, res.stderr
    return {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in res.stdout.splitlines()}


def _cat(container: str, path: str) -> str:
    res = _podman("exec", container, "cat", path)
    assert res.returncode == 0, f"{path}: {res.stderr}"
    return res.stdout


def test_bootstrap_converges_and_is_idempotent_on_debian_13(trixie: str) -> None:
    workdir = "/tmp/hermclaw-converge"
    _podman("exec", trixie, "mkdir", "-p", workdir)
    local = Path(f"/tmp/{trixie}-inputs")
    local.mkdir(exist_ok=True)
    try:
        _write_inputs(local)
        for f in ("hosts.yml", "overrides.json"):
            assert _podman("cp", str(local / f), f"{trixie}:{workdir}/{f}").returncode == 0
    finally:
        shutil.rmtree(local, ignore_errors=True)

    first, recap = _playbook(trixie, workdir)
    assert first.returncode == 0, first.stdout[-6000:] + first.stderr[-2000:]
    assert set(recap) == set(HOSTS), recap
    for host, (ok, changed, unreachable, failed) in recap.items():
        assert failed == 0 and unreachable == 0 and ok > 10, (host, recap[host])
        assert changed > 0, f"{host}: first run changed nothing"
    assert "Assert Debian 13 trixie on x86_64" in first.stdout
    assert "Run host preflight (scripts/bootstrap/check-host.sh, read-only)" in first.stdout

    # ---------------------------------------------------------------- resulting state (1.2-1.6, 1.9, 1.10, 1.13)
    passwd = _podman("exec", trixie, "getent", "passwd", "hermclaw", "hermclaw-admin").stdout.splitlines()
    hermclaw = next(p.split(":") for p in passwd if p.startswith("hermclaw:"))
    admin = next(p.split(":") for p in passwd if p.startswith("hermclaw-admin:"))
    assert hermclaw[5] == "/var/lib/hermclaw" and hermclaw[6] == "/usr/sbin/nologin" and int(hermclaw[2]) < 1000
    assert admin[6] == "/bin/bash"
    shadow = _podman("exec", trixie, "getent", "shadow", "hermclaw").stdout
    assert shadow.split(":")[1] == "!", "locked password"
    groups = _podman("exec", trixie, "id", "-nG", "hermclaw-admin").stdout.split()
    assert not set(groups) & {"sudo", "wheel", "admin", "root"}

    modes = _stat(trixie, "/opt/hermclaw", "/etc/hermclaw", "/etc/hermclaw/ssh", "/etc/hermclaw/secrets",
                  "/var/lib/hermclaw", "/var/lib/hermclaw/workspaces", "/var/lib/hermclaw/artifacts",
                  "/var/lib/hermclaw/repos", "/var/lib/hermclaw/worker", "/var/log/hermclaw", "/etc/credstore.encrypted",
                  "/etc/hermclaw/ssh/id_ed25519", "/etc/hermclaw/ssh/id_ed25519.pub", "/etc/hermclaw/ssh/backup_ed25519",
                  "/var/www/hermclaw-next", "/etc/hermclaw/hermclaw.env", "/etc/hermclaw/worker.env")  # fmt: skip
    assert modes == {
        "/opt/hermclaw": "755 root hermclaw",
        "/etc/hermclaw": "750 root hermclaw",
        "/etc/hermclaw/ssh": "750 root hermclaw",
        "/etc/hermclaw/secrets": "750 root hermclaw",
        "/var/lib/hermclaw": "750 hermclaw hermclaw",
        "/var/lib/hermclaw/workspaces": "750 hermclaw hermclaw",
        "/var/lib/hermclaw/artifacts": "750 hermclaw hermclaw",
        "/var/lib/hermclaw/repos": "750 hermclaw hermclaw",
        "/var/lib/hermclaw/worker": "750 hermclaw hermclaw",
        "/var/log/hermclaw": "750 hermclaw hermclaw",
        "/etc/credstore.encrypted": "700 root root",
        "/etc/hermclaw/ssh/id_ed25519": "600 hermclaw hermclaw",
        "/etc/hermclaw/ssh/id_ed25519.pub": "644 root hermclaw",
        "/etc/hermclaw/ssh/backup_ed25519": "600 hermclaw hermclaw",
        "/var/www/hermclaw-next": "755 root www-data",
        "/etc/hermclaw/hermclaw.env": "640 root hermclaw",
        "/etc/hermclaw/worker.env": "640 root hermclaw",
    }

    # 1.4: the runtime key is authorised on the targets for the orchestrator address only
    pub = _cat(trixie, "/etc/hermclaw/ssh/id_ed25519.pub").split()
    assert pub[0] == "ssh-ed25519"
    authorized = _cat(trixie, f"{admin[5]}/.ssh/authorized_keys").splitlines()
    assert authorized == [f'restrict,from="192.168.178.225" ssh-ed25519 {pub[1]} hermclaw-runtime@orchestrator']
    assert _stat(trixie, f"{admin[5]}/.ssh/authorized_keys")[f"{admin[5]}/.ssh/authorized_keys"].startswith("600 hermclaw-admin")
    hostkey = _cat(trixie, "/etc/ssh/ssh_host_ed25519_key.pub").split()
    known = _cat(trixie, "/etc/hermclaw/ssh/known_hosts").splitlines()
    assert sorted(known) == sorted(f"{ip},{name} ssh-ed25519 {hostkey[1]}"
                                   for ip, name in (("192.168.178.223", "hermclaw-webui"),
                                                    ("192.168.178.222", "hermclaw-exec-222")))  # fmt: skip
    keygen = _podman("exec", trixie, "/opt/hosttools/ssh-keygen", "-y", "-f", "/etc/hermclaw/ssh/id_ed25519")
    assert keygen.stdout.split()[1] == pub[1], "public key belongs to the private key"

    # 1.5/1.6: application venv on the Debian Python
    venv = _podman("exec", trixie, "/opt/hermclaw/venv/bin/python", "-c", "import sys; print(sys.prefix, sys.base_prefix)")
    assert venv.returncode == 0 and venv.stdout.split() == ["/opt/hermclaw/venv", "/usr/local"]

    # 1.10: sub-IDs, cgroup delegation, lingering
    subuid = _cat(trixie, "/etc/subuid").splitlines()
    assert [ln for ln in subuid if ln.startswith("hermclaw:")] == ["hermclaw:131072:65536"]
    assert _cat(trixie, "/etc/subgid").count("hermclaw:") == 1
    assert "Delegate=cpu cpuset io memory pids" in _cat(trixie, "/etc/systemd/system/user@.service.d/delegate.conf")
    assert _podman("exec", trixie, "test", "-f", "/var/lib/systemd/linger/hermclaw").returncode == 0

    # 1.12/1.13: nginx site and unit files are in place, but nothing is enabled by the bootstrap
    site = _cat(trixie, "/etc/nginx/sites-available/hermclaw-next.conf")
    assert "server_name hermclaw.lan;" in site and "proxy_buffering off;" in site
    units = _podman("exec", trixie, "ls", "/etc/systemd/system").stdout.split()
    for unit in ("hermclaw-api.service", "hermclaw-scheduler.service", "hermclaw-litellm.service",
                 "hermclaw-backup.service", "hermclaw-backup.timer", "hermclaw-exec-worker.service",
                 "hermclaw-wol@.service"):  # fmt: skip
        assert unit in units, unit
    assert "multi-user.target.wants" not in units or not any(
        u.startswith("hermclaw") for u in _podman("exec", trixie, "ls", "/etc/systemd/system/multi-user.target.wants").stdout.split()
    )
    api = _cat(trixie, "/etc/systemd/system/hermclaw-api.service")
    assert "LoadCredentialEncrypted=api-admin-token:/etc/credstore.encrypted/hermclaw-api-admin-token" in api
    assert re.search(r"^Environment=XDG_RUNTIME_DIR=/run/user/\d+$", _cat(trixie, "/etc/systemd/system/hermclaw-exec-worker.service"), re.M)
    assert _podman("exec", trixie, "test", "-x", "/opt/hermclaw/bin/hermclaw-credential").returncode == 0

    # ---------------------------------------------------------------- idempotency (second run and check mode)
    second, recap2 = _playbook(trixie, workdir)
    assert second.returncode == 0, second.stdout[-6000:]
    changed = {h: r[1] for h, r in recap2.items() if r[1] != 0}
    assert set(recap2) == set(HOSTS) and not changed, f"second run changed tasks: {changed}\n" + "\n".join(
        ln for ln in second.stdout.splitlines() if ln.startswith("changed:") or ln.startswith("TASK [")
    )[-4000:]
    check, recap3 = _playbook(trixie, workdir, "--check", "--diff")
    assert check.returncode == 0, check.stdout[-6000:]
    assert set(recap3) == set(HOSTS) and all(r[1] == 0 for r in recap3.values()), recap3
