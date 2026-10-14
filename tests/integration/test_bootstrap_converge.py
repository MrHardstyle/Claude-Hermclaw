"""P01 1.15 – converge the real bootstrap playbook on Debian 13 (trixie) containers.

``playbooks/bootstrap.yml`` runs unmodified against four rootful Podman containers built from
the official Debian 13 Python image (one per Hermclaw host group). A small connection plugin
(test code only) executes the tasks with ``podman exec``. Package installation and service
management are switched off (``hermclaw_install_packages=false``, ``hermclaw_manage_services=false``)
because the build environment has neither apt mirror access nor systemd as PID 1; tasks that
need the missing binaries (git, PostgreSQL, Node.js, NVIDIA) are skipped by tag and covered by
the static/rendering tests and the live runbook (BLOCKER-001).

Asserted:
* the first run converges without errors on real Debian 13 userland
* the second run reports ``changed=0`` on every host (idempotency)
* users, directory modes, SSH key distribution (``restrict,from=``), known_hosts, sub-ID
  ranges, cgroup delegation, lingering flags, unit files and environment files on disk
* foreign unit files are never overwritten (preflight and role guard)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.integration, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[2]
ANSIBLE_DIR = REPO / "infra" / "ansible"
VENV_BIN = Path(sys.executable).parent
IMAGE = os.environ.get("HERMCLAW_BOOTSTRAP_TEST_IMAGE", "docker.io/library/python:3.13-slim-trixie")
PYTHON = "/usr/local/bin/python3"
ORCHESTRATOR_IP = "192.0.2.25"
SKIP_TAGS = "git,postgresql,nodejs,nvidia"

CONNECTION_PLUGIN = '''
from __future__ import annotations

DOCUMENTATION = """
name: hermclaw_podman
short_description: run tasks inside a local Podman container (Hermclaw bootstrap tests only)
description:
  - Executes commands with podman exec and copies files with podman cp.
author: Hermclaw tests
options:
  container:
    description: name of the running container
    required: true
    vars:
      - name: hermclaw_container
extends_documentation_fragment:
  - connection_pipelining
"""

import subprocess

from ansible.errors import AnsibleError
from ansible.plugins.connection import ConnectionBase


class Connection(ConnectionBase):
    transport = "hermclaw_podman"
    has_pipelining = True

    def _connect(self):
        self._connected = True
        return self

    def exec_command(self, cmd, in_data=None, sudoable=True):
        super().exec_command(cmd, in_data=in_data, sudoable=sudoable)
        argv = ["podman", "exec", "-i", self.get_option("container"), "/bin/sh", "-c", cmd]
        if in_data is None:
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, check=False)
        else:
            proc = subprocess.run(argv, input=in_data, capture_output=True, check=False)
        return proc.returncode, proc.stdout, proc.stderr

    def _cp(self, src, dst):
        proc = subprocess.run(["podman", "cp", src, dst], capture_output=True, check=False)
        if proc.returncode != 0:
            raise AnsibleError("podman cp %s %s failed: %s" % (src, dst, proc.stderr))

    def put_file(self, in_path, out_path):
        super().put_file(in_path, out_path)
        self._cp(in_path, "%s:%s" % (self.get_option("container"), out_path))

    def fetch_file(self, in_path, out_path):
        super().fetch_file(in_path, out_path)
        self._cp("%s:%s" % (self.get_option("container"), in_path), out_path)

    def close(self):
        self._connected = False
'''

HOSTS = {
    # inventory host -> (group, ansible_host, extra host vars)
    "orch": ("orchestrator", ORCHESTRATOR_IP, {}),
    "web": ("webui", "192.0.2.23", {"hermclaw_web_server_name": "hermclaw.test"}),
    "exec": ("execution_workers", "192.0.2.22",
             {"hermclaw_worker_id": "exec-test", "wol_mac": "00:00:00:00:00:00", "wol_interface": "lo"}),
    "model": ("model_workers", "192.0.2.24",
              {"hermclaw_worker_id": "model-test", "wol_mac": "00:00:00:00:00:00", "wol_interface": "lo",
               # no GPU in the build environment: the preflight would (rightly) fail on the PCI check
               "common_run_preflight": False}),
}  # fmt: skip


def _podman(*args: str, check: bool = True, input_text: str | None = None,
            timeout: int = 120) -> subprocess.CompletedProcess[str]:  # fmt: skip
    res = subprocess.run(  # noqa: S603 - fixed argv built by the test
        ["podman", *args], input=input_text, capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and res.returncode != 0:
        raise AssertionError(f"podman {' '.join(args)} failed: {res.stderr}")
    return res


def _sh(container: str, script: str, check: bool = True) -> str:
    return _podman("exec", "-i", container, "/bin/sh", "-c", script, check=check).stdout


def _image_available() -> bool:
    if shutil.which("podman") is None:
        return False
    if _podman("image", "exists", IMAGE, check=False).returncode == 0:
        return True
    return _podman("pull", "-q", IMAGE, check=False, timeout=600).returncode == 0


def _keygen(path: Path, comment: str) -> None:
    subprocess.run(  # noqa: S603 - fixed argv
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
        check=True, capture_output=True, stdin=subprocess.DEVNULL,
    )  # fmt: skip


class Fleet:
    """Four Debian 13 containers plus inventory and plugin wiring for ansible-playbook."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.prefix = f"hc-boot-{uuid.uuid4().hex[:8]}"
        self.containers = {h: f"{self.prefix}-{h}" for h in HOSTS}
        self.keys: dict[str, str] = {}
        self.host_keys: dict[str, str] = {}

    # -- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        for host, name in self.containers.items():
            _podman("run", "-d", "--rm", "--name", name, "--hostname", f"hermclaw-{host}", "--network", "none",
                    IMAGE, "sleep", "infinity")  # fmt: skip
        keydir = self.workdir / "keys"
        keydir.mkdir()
        # the orchestrator's keys exist already (ssh-keygen is not in the image): the role's
        # `creates:` guard keeps them, the distribution logic is exercised for real
        _sh(self.containers["orch"], "mkdir -p /etc/hermclaw/ssh")
        for name, comment in (("id_ed25519", "hermclaw-runtime@orchestrator"), ("backup_ed25519", "hermclaw-backup@orchestrator")):
            _keygen(keydir / name, comment)
            for suffix in ("", ".pub"):
                _podman("cp", str(keydir / f"{name}{suffix}"), f"{self.containers['orch']}:/etc/hermclaw/ssh/{name}{suffix}")
        self.keys["runtime"] = (keydir / "id_ed25519.pub").read_text(encoding="utf-8").split()[1]
        # SSH targets need a host key (no sshd in the image)
        for host in ("web", "exec", "model"):
            _keygen(keydir / f"host_{host}", f"root@{host}")
            _sh(self.containers[host], "mkdir -p /etc/ssh")
            _podman("cp", str(keydir / f"host_{host}.pub"), f"{self.containers[host]}:/etc/ssh/ssh_host_ed25519_key.pub")
            self.host_keys[host] = (keydir / f"host_{host}.pub").read_text(encoding="utf-8").split()[1]
        # pre-existing sub-ID ranges of other users must never be overlapped
        _sh(self.containers["exec"], "printf 'alice:100000:65536\\nbob:300000:65536\\n' > /etc/subuid; : > /etc/subgid")

    def stop(self) -> None:
        for name in self.containers.values():
            _podman("rm", "-f", "-t", "0", name, check=False)

    # -- ansible ---------------------------------------------------------------------------
    def inventory(self) -> Path:
        children: dict[str, dict[str, dict[str, dict[str, object]]]] = {}
        for host, (group, address, extra) in HOSTS.items():
            hostvars = {"ansible_host": address, "hermclaw_container": self.containers[host], **extra}
            children.setdefault(group, {"hosts": {}})["hosts"][host] = hostvars
        inv = {
            "all": {
                "vars": {
                    "ansible_connection": "hermclaw_podman",
                    "ansible_python_interpreter": PYTHON,
                    "ansible_become": False,
                    "hermclaw_orchestrator_ip": ORCHESTRATOR_IP,
                    "hermclaw_lan_broadcast": "192.0.2.255",
                },
                "children": children,
            }
        }
        path = self.workdir / "hosts.yml"
        path.write_text(yaml.safe_dump(inv), encoding="utf-8")
        return path

    def playbook(self, *extra: str) -> subprocess.CompletedProcess[str]:
        plugins = self.workdir / "connection_plugins"
        plugins.mkdir(exist_ok=True)
        (plugins / "hermclaw_podman.py").write_text(CONNECTION_PLUGIN, encoding="utf-8")
        env = dict(os.environ)
        env.update(
            {
                "ANSIBLE_CONFIG": str(ANSIBLE_DIR / "ansible.cfg"),
                "ANSIBLE_CONNECTION_PLUGINS": str(plugins),
                "ANSIBLE_HOME": str(self.workdir / "ansible-home"),
                "ANSIBLE_LOCAL_TEMP": str(self.workdir / "ansible-tmp"),
                "ANSIBLE_NOCOLOR": "1",
                "ANSIBLE_FORCE_COLOR": "0",
                "PATH": f"{VENV_BIN}{os.pathsep}{env.get('PATH', '')}",
            }
        )
        argv = [
            str(VENV_BIN / "ansible-playbook"), "-i", str(self.inventory()), "playbooks/bootstrap.yml",
            "--skip-tags", SKIP_TAGS,
            "-e", "hermclaw_install_packages=false", "-e", "hermclaw_manage_services=false",
            "-e", f"common_python_bin={PYTHON}",
            "-e", "common_preflight_min_cpus=1", "-e", "common_preflight_min_ram_mb=128",
            "-e", "common_preflight_min_disk_gb=1",
            # systemd-creds/visudo are not part of the image; both are covered by other tests
            "-e", "systemd_skeleton_generate_credentials=false",
            "-e", '{"common_ssh_runtime_sudo_commands": []}',
            *extra,
        ]  # fmt: skip
        return subprocess.run(  # noqa: S603 - fixed argv
            argv, cwd=ANSIBLE_DIR, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=1500, check=False,
        )  # fmt: skip


def _recap(stdout: str) -> dict[str, dict[str, int]]:
    recap: dict[str, dict[str, int]] = {}
    for m in re.finditer(r"^(\S+)\s+:\s+(ok=.*)$", stdout.split("PLAY RECAP", 1)[-1], re.M):
        recap[m.group(1)] = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", m.group(2))}
    return recap


def _changed_tasks(stdout: str) -> list[str]:
    tasks, current = [], ""
    for line in stdout.splitlines():
        if line.startswith("TASK ["):
            current = line
        elif line.startswith("changed:"):
            tasks.append(f"{current} {line}")
    return tasks


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Fleet]:
    if os.geteuid() != 0:
        pytest.skip("needs rootful podman (run as root)")
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen not installed on the test host")
    if not _image_available():
        pytest.skip(f"podman or the image {IMAGE} is not available")
    f = Fleet(tmp_path_factory.mktemp("converge"))
    try:
        f.start()
        yield f
    finally:
        f.stop()


@pytest.fixture(scope="module")
def converged(fleet: Fleet) -> Mapping[str, str]:
    first = fleet.playbook()
    assert first.returncode == 0, first.stdout[-6000:] + first.stderr[-3000:]
    recap = _recap(first.stdout)
    assert set(recap) == set(HOSTS), recap
    assert all(r["failed"] == 0 and r["unreachable"] == 0 for r in recap.values()), recap
    assert all(r["changed"] > 0 for r in recap.values()), "first run must change every host"
    second = fleet.playbook()
    return {"first": first.stdout, "second": second.stdout, "second_rc": str(second.returncode),
            "second_err": second.stderr}  # fmt: skip


def test_second_run_is_idempotent(converged: Mapping[str, str]) -> None:
    assert converged["second_rc"] == "0", converged["second"][-6000:] + converged["second_err"][-2000:]
    recap = _recap(converged["second"])
    assert set(recap) == set(HOSTS)
    assert all(r["changed"] == 0 and r["failed"] == 0 for r in recap.values()), _changed_tasks(converged["second"])


def _stat(container: str, *paths: str) -> dict[str, str]:
    out = _sh(container, "stat -c '%n|%a|%U|%G|%F' " + " ".join(paths))
    return {line.split("|", 1)[0]: line.split("|", 1)[1] for line in out.splitlines()}


def test_accounts_and_directories(fleet: Fleet, converged: Mapping[str, str]) -> None:
    orch = fleet.containers["orch"]
    passwd = _sh(orch, "getent passwd hermclaw").strip().split(":")
    assert passwd[5] == "/var/lib/hermclaw" and passwd[6] == "/usr/sbin/nologin"
    assert int(passwd[2]) < 1000, "hermclaw must be a system account"
    assert _sh(orch, "getent shadow hermclaw").split(":")[1] in {"!", "!*", "*"}
    modes = _stat(orch, "/opt/hermclaw", "/etc/hermclaw", "/etc/hermclaw/ssh", "/etc/hermclaw/secrets",
                  "/var/lib/hermclaw", "/var/lib/hermclaw/workspaces", "/var/lib/hermclaw/artifacts",
                  "/var/lib/hermclaw/repos", "/var/log/hermclaw", "/etc/credstore.encrypted")  # fmt: skip
    assert modes["/etc/hermclaw"] == "750|root|hermclaw|directory"
    assert modes["/etc/hermclaw/ssh"] == "750|root|hermclaw|directory"
    assert modes["/etc/credstore.encrypted"] == "700|root|root|directory"
    for sub in ("workspaces", "artifacts", "repos"):
        assert modes[f"/var/lib/hermclaw/{sub}"] == "750|hermclaw|hermclaw|directory"
    assert modes["/var/log/hermclaw"] == "750|hermclaw|hermclaw|directory"
    assert modes["/opt/hermclaw"] == "755|root|hermclaw|directory"
    # 1.5 / 1.6: the application venv exists and is a real venv of the Debian 13 Python
    out = _sh(orch, "/opt/hermclaw/venv/bin/python -c 'import sys; print(sys.prefix, sys.version_info[:2])'")
    assert out.startswith("/opt/hermclaw/venv (3, 13)")
    assert _sh(orch, "readlink /etc/localtime").strip().endswith("Europe/Berlin")
    # worker state directory only on workers
    assert _sh(fleet.containers["exec"], "stat -c %a /var/lib/hermclaw/worker").strip() == "750"
    assert _sh(orch, "test -e /var/lib/hermclaw/worker && echo yes || echo no").strip() == "no"


def test_ssh_keys_are_distributed_restricted(fleet: Fleet, converged: Mapping[str, str]) -> None:
    orch = fleet.containers["orch"]
    keys = _stat(orch, "/etc/hermclaw/ssh/id_ed25519", "/etc/hermclaw/ssh/id_ed25519.pub",
                 "/etc/hermclaw/ssh/backup_ed25519", "/etc/hermclaw/ssh/known_hosts")  # fmt: skip
    assert keys["/etc/hermclaw/ssh/id_ed25519"] == "600|hermclaw|hermclaw|regular file"
    assert keys["/etc/hermclaw/ssh/backup_ed25519"] == "600|hermclaw|hermclaw|regular file"
    assert keys["/etc/hermclaw/ssh/id_ed25519.pub"] == "644|root|hermclaw|regular file"
    known = _sh(orch, "cat /etc/hermclaw/ssh/known_hosts").splitlines()
    for host in ("web", "exec", "model"):
        address = HOSTS[host][1]
        assert f"{address},{host} ssh-ed25519 {fleet.host_keys[host]}" in known
    assert len(known) == 3, known
    for host in ("web", "exec", "model"):
        container = fleet.containers[host]
        home = _sh(container, "getent passwd hermclaw-admin").strip().split(":")[5]
        auth = _stat(container, f"{home}/.ssh", f"{home}/.ssh/authorized_keys")
        assert auth[f"{home}/.ssh"].startswith("700|hermclaw-admin|")
        assert auth[f"{home}/.ssh/authorized_keys"].startswith("600|hermclaw-admin|")
        lines = _sh(container, f"cat {home}/.ssh/authorized_keys").splitlines()
        assert lines == [f'restrict,from="{ORCHESTRATOR_IP}" ssh-ed25519 {fleet.keys["runtime"]} hermclaw-runtime@orchestrator']
        assert "sudo" not in _sh(container, "id -nG hermclaw-admin").split()
        assert _sh(container, "test -e /etc/sudoers.d/hermclaw-runtime && echo yes || echo no").strip() == "no"
    # the orchestrator does not authorise its own key
    assert _sh(orch, "getent passwd hermclaw-admin || true").strip() == ""


def test_rootless_podman_prerequisites(fleet: Fleet, converged: Mapping[str, str]) -> None:
    for host in ("orch", "exec"):
        container = fleet.containers[host]
        delegate = _sh(container, "cat /etc/systemd/system/user@.service.d/delegate.conf")
        assert "Delegate=cpu cpuset io memory pids" in delegate
        assert 'default_rootless_network_cmd = "pasta"' in _sh(container, "cat /etc/containers/containers.conf.d/50-hermclaw.conf")
        assert _sh(container, "test -f /var/lib/systemd/linger/hermclaw && echo yes").strip() == "yes"
    for name in ("subuid", "subgid"):
        ranges = [tuple(ln.split(":")) for ln in _sh(fleet.containers["exec"], f"cat /etc/{name}").splitlines() if ln]
        mine = [r for r in ranges if r[0] == "hermclaw"]
        assert len(mine) == 1, ranges
        start, count = int(mine[0][1]), int(mine[0][2])
        assert count == 65536 and start % 65536 == 0 and start >= 100000
        for other in ranges:
            if other[0] == "hermclaw":
                continue
            o_start, o_end = int(other[1]), int(other[1]) + int(other[2])
            assert start >= o_end or start + count <= o_start, f"{mine} overlaps {other}"
    assert ("alice", "100000", "65536") in [tuple(ln.split(":")) for ln in _sh(fleet.containers["exec"], "cat /etc/subuid").splitlines()]


def test_units_and_environment_files(fleet: Fleet, converged: Mapping[str, str]) -> None:
    orch = fleet.containers["orch"]
    for unit in ("hermclaw-api.service", "hermclaw-scheduler.service", "hermclaw-litellm.service",
                 "hermclaw-backup.service", "hermclaw-backup.timer"):  # fmt: skip
        text = _sh(orch, f"cat /etc/systemd/system/{unit}")
        assert "Managed by Hermclaw" in text and "{{" not in text
    api = _sh(orch, "cat /etc/systemd/system/hermclaw-api.service")
    for worker in ("exec-test", "model-test"):
        assert f"LoadCredentialEncrypted=worker-token-{worker}:/etc/credstore.encrypted/hermclaw-worker-token-{worker}" in api
    env = _stat(orch, "/etc/hermclaw/hermclaw.env")["/etc/hermclaw/hermclaw.env"]
    assert env == "640|root|hermclaw|regular file"
    assert 'HERMCLAW_WORKER_TOKEN_REFS=\'{"exec-test":"cred:worker-token-exec-test","model-test":"cred:worker-token-model-test"}\'' in _sh(
        orch, "cat /etc/hermclaw/hermclaw.env"
    )
    uid = _sh(orch, "id -u hermclaw").strip()
    quadlet = _sh(orch, f"cat /etc/containers/systemd/users/{uid}/hermclaw-searxng.container")
    assert "PublishPort=127.0.0.1:8888:8080" in quadlet
    assert _stat(orch, "/opt/hermclaw/bin/hermclaw-cred-env")["/opt/hermclaw/bin/hermclaw-cred-env"].startswith("755|root|root")
    execu = fleet.containers["exec"]
    unit = _sh(execu, "cat /etc/systemd/system/hermclaw-exec-worker.service")
    assert f"Environment=XDG_RUNTIME_DIR=/run/user/{_sh(execu, 'id -u hermclaw').strip()}" in unit
    assert "WORKER_KIND=execution" in _sh(execu, "cat /etc/hermclaw/worker.env")
    assert "WORKER_BIND=192.0.2.22:8787" in _sh(execu, "cat /etc/hermclaw/worker.env")
    assert "ExecStart=/usr/sbin/ethtool -s %I wol g" in _sh(execu, "cat /etc/systemd/system/hermclaw-wol@.service")
    model = fleet.containers["model"]
    assert "WORKER_KIND=model" in _sh(model, "cat /etc/hermclaw/worker.env")
    dropin = _sh(model, "cat /etc/systemd/system/ollama.service.d/hermclaw.conf")
    assert "Environment=OLLAMA_HOST=0.0.0.0:11434" in dropin and "UnsetEnvironment=OLLAMA_CONTEXT_LENGTH" in dropin
    assert "User=ollama" in _sh(model, "cat /etc/systemd/system/ollama.service")
    web = fleet.containers["web"]
    assert "server_name hermclaw.test;" in _sh(web, "cat /etc/nginx/sites-available/hermclaw-next.conf")
    assert _sh(web, "readlink /etc/nginx/sites-enabled/hermclaw-next.conf").strip() == "/etc/nginx/sites-available/hermclaw-next.conf"
    assert "Hermclaw Next" in _sh(web, "cat /var/www/hermclaw-next/index.html")
    # services that do not belong to a group are not installed there
    assert _sh(web, "ls /etc/systemd/system/ | grep -c hermclaw || true").strip() == "0"


def test_preflight_report_on_debian_13(fleet: Fleet, converged: Mapping[str, str]) -> None:
    container = fleet.containers["web"]
    _podman("cp", str(REPO / "scripts" / "bootstrap" / "check-host.sh"), f"{container}:/tmp/check-host.sh")
    out = _sh(container, "bash /tmp/check-host.sh --role webui --json --min-cpus 1 --min-ram-mb 128 --min-disk-gb 1")
    report = yaml.safe_load(out)
    checks = {c["check"]: c for c in report["checks"]}
    assert checks["os"]["status"] == "OK" and "Debian 13" in checks["os"]["message"]
    assert checks["port:80"]["status"] == "OK" and checks["port:443"]["status"] == "OK"
    assert report["failures"] == 0


def test_foreign_unit_files_are_never_overwritten(fleet: Fleet, converged: Mapping[str, str]) -> None:
    container = fleet.containers["exec"]
    foreign = "[Unit]\nDescription=someone else's service\n[Service]\nExecStart=/bin/true\n"
    _podman("exec", "-i", container, "/bin/sh", "-c", "cat > /etc/systemd/system/hermclaw-exec-worker.service",
            input_text=foreign)  # fmt: skip
    try:
        res = fleet.playbook("--limit", "exec")
        assert res.returncode != 0
        assert "Preflight failed" in res.stdout and "unit:hermclaw-exec-worker.service" in res.stdout
        assert _sh(container, "cat /etc/systemd/system/hermclaw-exec-worker.service") == foreign
        # without the preflight the role's own guard refuses as well
        res = fleet.playbook("--limit", "exec", "-e", "common_run_preflight=false")
        assert res.returncode != 0
        assert "refusing to overwrite an existing service" in res.stdout
        assert _sh(container, "cat /etc/systemd/system/hermclaw-exec-worker.service") == foreign
    finally:
        _sh(container, "rm -f /etc/systemd/system/hermclaw-exec-worker.service")
