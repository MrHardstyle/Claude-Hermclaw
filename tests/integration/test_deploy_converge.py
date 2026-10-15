"""P32 32.1-32.10 – the real deployment playbooks converge on a Debian 13 / CPython 3.13 userland.

``playbooks/site.yml`` (bootstrap + deployment) and ``playbooks/deploy.yml`` run unmodified inside throw-away
``python:3.13-slim-trixie`` containers (network ``none``, local connection, ansible-core installed offline into the
container) with the **real release set** built by ``scripts/deploy/build-release.sh`` (``test_deploy_release.py``):

* container A holds the orchestrator ``.225``, the web UI ``.223`` (TLS on, self-signed) and the execution worker
  ``.222`` (its own install directory, so the forks never share a venv); container B the model worker ``.224``;
* a private PostgreSQL cluster on the host (unix socket only, mounted to ``/run/postgresql``) makes the schema tasks
  real: ``hermclaw migrate`` runs once before the switch, as ``hermclaw`` with the production environment, through the
  operator CLI ``/usr/local/sbin/hermclaw`` and a test double of ``systemd-run`` (no systemd PID 1 here); the admin API
  token is created once and stored as an encrypted credential (test double of ``systemd-creds``), never printed;
* ``hermclaw_manage_services=false``: nothing is started, units are enabled offline (the links ``systemctl enable``
  creates) and verified with ``systemd-analyze verify --root=<container rootfs>``; the nginx site is tested with
  ``nginx -t`` in the official nginx image.

Sequence (container A, container B likewise for the model worker): ``bootstrap.yml`` -> ``deploy.yml --check --diff``
on the freshly bootstrapped hosts (the documented first dry run: succeeds, nothing on disk changes) -> ``deploy.yml``
0.1.0rc1 (the bootstrap's plain venv directory is migrated to the symlink layout) -> ``site.yml`` and ``deploy.yml
--check --diff`` report ``changed=0`` -> dry run of the upgrade to V2 (nothing on disk changes) -> V2
(switch) -> V3 (migration to 0003, pruning with ``hermclaw_keep_releases=1``) -> rollback to V2 refused
without the schema-downgrade opt-in (no live file changes, although the run also carries a configuration change), then
done with it (downgrade with the newer release's migrations, no reinstall) -> ``changed=0`` again.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.integration.test_deploy_release import V1, V2, V3, Releases, releases_for

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
IMAGE = "docker.io/library/python:3.13-slim-trixie"
NGINX_IMAGE = "docker.io/library/nginx:1.28"
PG_BIN = Path("/usr/lib/postgresql/16/bin")
LAUNCH = "set -e; export {env}; cd /src/infra/ansible; exec /opt/ansible/bin/ansible-playbook -i /work/hosts.yml playbooks/{pb} -e @/work/overrides.json {args}"
RECAP = re.compile(r"^(\S+)\s+: ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", re.M)
HOSTS_A = ["hermclaw-orchestrator", "hermclaw-webui", "hermclaw-exec-222"]

FAKE_SYSTEMD_CREDS = r'''#!/usr/local/bin/python3
"""Test double of systemd-creds (no systemd in the container): a name-bound base64 envelope, never a real secret."""
import base64, sys
MAGIC = b"HERMCLAW-TEST-CREDENTIAL"
cmd, rest = sys.argv[1], sys.argv[2:]
name = next((a.split("=", 1)[1] for a in rest if a.startswith("--name=")), "")
pos = [a for a in rest if not a.startswith("-") or a == "-"]
read = lambda p: sys.stdin.buffer.read() if p == "-" else open(p, "rb").read()
def write(path, data):
    if path == "-":
        sys.stdout.buffer.write(data)
    else:
        with open(path, "wb") as fh:
            fh.write(data)
if cmd == "encrypt":
    write(pos[1], MAGIC + b"\n" + name.encode() + b"\n" + base64.b64encode(read(pos[0])) + b"\n")
elif cmd == "decrypt":
    magic, embedded, payload = read(pos[0]).split(b"\n")[:3]
    if magic != MAGIC or (name and embedded.decode() != name):
        sys.exit("Embedded credential name does not match, refusing.")
    write(pos[1] if len(pos) > 1 else "-", base64.b64decode(payload))
else:
    sys.exit(f"unsupported: {cmd}")
'''

FAKE_SYSTEMD_RUN = r'''#!/usr/local/bin/python3
"""Test double of `systemd-run --pipe --wait` (no systemd PID 1): runs the command as --uid/--gid with EnvironmentFile=,
-E, WorkingDirectory=, UMask= and LoadCredentialEncrypted= (decrypted into a private $CREDENTIALS_DIRECTORY) and
propagates its exit status. Logs argv only (never environment values) to /var/log/hermclaw-fake-systemd-run.jsonl."""
import grp, json, os, pwd, shutil, subprocess, sys, tempfile
argv = sys.argv[1:]
with open("/var/log/hermclaw-fake-systemd-run.jsonl", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\n")
props, setenv, uid, gid, cmd, i = [], {}, None, None, None, 0
while i < len(argv):
    a = argv[i]
    if a == "--":
        cmd = argv[i + 1:]
        break
    if a in ("-p", "--property", "-E", "--setenv"):
        (props.append(argv[i + 1]) if a in ("-p", "--property") else setenv.__setitem__(*argv[i + 1].split("=", 1)))
        i += 2
        continue
    if a.startswith("--uid="):
        uid = a.split("=", 1)[1]
    elif a.startswith("--gid="):
        gid = a.split("=", 1)[1]
    elif not a.startswith("-"):
        cmd = argv[i:]
        break
    i += 1
pw = pwd.getpwnam(uid) if uid else pwd.getpwuid(0)
env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": pw.pw_dir, "USER": pw.pw_name, "LANG": "C.UTF-8"}
cwd, umask, creds = "/", 0o022, []
for p in props:
    key, _, value = p.partition("=")
    if key == "EnvironmentFile":
        for line in open(value, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"" else v
    elif key == "WorkingDirectory":
        cwd = value
    elif key == "UMask":
        umask = int(value, 8)
    elif key == "LoadCredentialEncrypted":
        creds.append(value.split(":", 1))
env.update(setenv)
credir = tempfile.mkdtemp(prefix="creds-")
try:
    for name, path in creds:
        out = subprocess.run(["systemd-creds", "decrypt", f"--name={name}", path, "-"], capture_output=True, check=False)
        if out.returncode != 0:
            print(f"Failed to set up credentials: {name}", file=sys.stderr)
            sys.exit(243)
        with open(os.path.join(credir, name), "wb") as fh:
            fh.write(out.stdout)
        os.chown(os.path.join(credir, name), pw.pw_uid, -1)
        os.chmod(os.path.join(credir, name), 0o400)
    os.chown(credir, pw.pw_uid, -1)
    os.chmod(credir, 0o500)
    if creds:
        env["CREDENTIALS_DIRECTORY"] = credir
    gid_num = grp.getgrnam(gid).gr_gid if gid else pw.pw_gid
    def demote():
        os.setgid(gid_num)
        os.initgroups(pw.pw_name, gid_num)
        os.setuid(pw.pw_uid)
        os.umask(umask)
    rc = subprocess.run(cmd, env=env, cwd=cwd, preexec_fn=demote, check=False).returncode
finally:
    shutil.rmtree(credir, ignore_errors=True)
sys.exit(rc)
'''


def _run(argv: list[str], *, timeout: int = 1800, input_text: str | None = None, cwd: Path = Path("/")) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        input=input_text,
        stdin=subprocess.DEVNULL if input_text is None else None,
    )


def _podman(*args: str, timeout: int = 1800, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return _run(["podman", *args], timeout=timeout, input_text=input_text)


# --------------------------------------------------------------------------------------------- PostgreSQL


@dataclass
class Postgres:
    root: Path
    sock: Path

    def sql(self, query: str, db: str = "hermclaw") -> str:
        res = _run(["psql", "-h", str(self.sock), "-U", "postgres", "-d", db, "-XtAc", query])
        assert res.returncode == 0, res.stderr
        return res.stdout.strip()


@pytest.fixture(scope="module")
def postgres() -> Iterator[Postgres | None]:
    """A private PostgreSQL cluster (trust, unix socket only) with the hermclaw role, database and pgvector."""
    if os.geteuid() != 0 or not (PG_BIN / "initdb").exists() or shutil.which("psql") is None:
        yield None
        return
    root = Path(tempfile.mkdtemp(prefix="hcl-pg-", dir="/tmp"))
    root.chmod(0o755)
    data, sock, logs = root / "data", root / "sock", root / "log"
    for d in (data, sock, logs):
        d.mkdir()
        shutil.chown(d, "postgres", "postgres")
    sock.chmod(0o755)
    as_pg = ["runuser", "-u", "postgres", "--"]
    init = _run([*as_pg, str(PG_BIN / "initdb"), "-D", str(data), "-A", "trust", "-U", "postgres", "--no-sync"], timeout=300)
    if init.returncode != 0:
        shutil.rmtree(root, ignore_errors=True)
        yield None
        return
    opts = f"-k {sock} -c listen_addresses='' -p 5432 -c fsync=off -c full_page_writes=off"
    start = _run([*as_pg, str(PG_BIN / "pg_ctl"), "-D", str(data), "-o", opts, "-l", str(logs / "pg.log"), "-w", "start"], timeout=120)
    if start.returncode != 0:
        log = (logs / "pg.log").read_text(encoding="utf-8", errors="replace") if (logs / "pg.log").exists() else ""
        shutil.rmtree(root, ignore_errors=True)
        pytest.fail(f"private PostgreSQL did not start: {start.stdout}{start.stderr}{log[-2000:]}")
    pg = Postgres(root, sock)
    try:
        pg.sql("CREATE ROLE hermclaw LOGIN", db="postgres")
        pg.sql("CREATE DATABASE hermclaw OWNER hermclaw", db="postgres")
        pg.sql("CREATE EXTENSION IF NOT EXISTS vector")
        yield pg
    finally:
        _run([*as_pg, str(PG_BIN / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"], timeout=120)
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------------------------- containers


@dataclass
class Container:
    name: str
    workdir: Path
    hosts: list[str]
    limit: str
    runs: dict[str, subprocess.CompletedProcess[str]] = field(default_factory=dict)
    snapshots: dict[str, str] = field(default_factory=dict)

    def sh(self, script: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        return _podman("exec", self.name, "bash", "-c", script, timeout=timeout)

    def out(self, script: str) -> str:
        res = self.sh(script)
        assert res.returncode == 0, f"{script}: {res.stderr}"
        return res.stdout

    def playbook(
        self, label: str, pb: str, *args: str, forks: int = 3
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, tuple[int, ...]]]:
        env = (
            f"PATH=/opt/fakes:/opt/hosttools:$PATH ANSIBLE_CONFIG=/src/infra/ansible/ansible.cfg ANSIBLE_HOME=/tmp/ah "
            f"ANSIBLE_LOCAL_TEMP=/tmp/alt ANSIBLE_REMOTE_TEMP=/tmp/art ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0 "
            f"ANSIBLE_RETRY_FILES_ENABLED=0 ANSIBLE_PIPELINING=1 ANSIBLE_FORKS={forks}"
        )
        script = LAUNCH.format(env=env, pb=pb, args=" ".join([f"--limit {self.limit}", *args]))
        res = _podman("exec", self.name, "bash", "-c", script, timeout=3600)
        (self.workdir / f"{label}.log").write_text(res.stdout + "\n" + res.stderr, encoding="utf-8")
        self.runs[label] = res
        return res, {m.group(1): tuple(int(x) for x in m.groups()[1:]) for m in RECAP.finditer(res.stdout)}


def _changed_tasks(res: subprocess.CompletedProcess[str]) -> str:
    lines = res.stdout.splitlines()
    out = []
    for i, ln in enumerate(lines):
        if ln.startswith("changed:"):
            task = next((lines[j] for j in range(i, -1, -1) if lines[j].startswith("TASK [")), "?")
            out.append(f"{task} -> {ln}")
    return "\n".join(out[-60:])


def _assert_ok(res: subprocess.CompletedProcess[str], recap: dict[str, tuple[int, ...]], hosts: list[str], *, idempotent: bool) -> None:
    assert res.returncode == 0, res.stdout[-8000:] + res.stderr[-3000:]
    assert set(recap) == set(hosts), recap
    for host, (_ok, changed, unreachable, failed) in recap.items():
        assert failed == 0 and unreachable == 0, (host, recap[host])
        if idempotent:
            assert changed == 0, f"{host} changed {changed} task(s):\n{_changed_tasks(res)}"


def _inventory(model_only: bool) -> dict[str, Any]:
    exec_vars = {
        "ansible_host": "192.168.178.222",
        "hermclaw_worker_id": "exec-222",
        "wol_mac": "52:54:00:12:02:22",
        "wol_interface": "lo",
        # its own install directory: the parallel forks of this test never share a venv with the orchestrator
        "hermclaw_install_dir": "/opt/hermclaw-exec",
        "hermclaw_venv": "/opt/hermclaw-exec/venv",
    }
    return {
        "all": {
            "vars": {
                "ansible_connection": "local",
                "ansible_python_interpreter": "/usr/local/bin/python3",
                "hermclaw_version": V1,
                "hermclaw_orchestrator_ip": "192.168.178.225",
                "hermclaw_lan_broadcast": "192.168.178.255",
            },
            "children": {
                "orchestrator": {
                    "hosts": {"hermclaw-orchestrator": {"ansible_host": "192.168.178.225", "hermclaw_host_id": "orchestrator-225"}}
                },
                "webui": {
                    "hosts": {
                        "hermclaw-webui": {
                            "ansible_host": "192.168.178.223",
                            "hermclaw_host_id": "webui-223",
                            "hermclaw_web_server_name": "hermclaw.lan 192.168.178.223",
                            "nginx_tls_enabled": True,
                        }
                    }
                },
                "execution_workers": {"hosts": {"hermclaw-exec-222": exec_vars}},
                "model_workers": {
                    "hosts": {
                        "hermclaw-model-224": {
                            "ansible_host": "192.168.178.224",
                            "hermclaw_worker_id": "model-224",
                            "wol_mac": "52:54:00:12:02:24",
                            "wol_interface": "lo",
                        }
                    }
                },
                "gitlab": {"hosts": {"gitlab-226": {"ansible_host": "192.168.178.226", "hermclaw_host_id": "gitlab-226"}}},
                "backup": {"hosts": {"terra-60": {"ansible_host": "192.168.178.60", "hermclaw_host_id": "terra-60"}}},
            },
        }
    }


def _overrides(database: bool) -> dict[str, Any]:
    return {
        "hermclaw_install_packages": False,
        "hermclaw_manage_services": False,
        "ansible_become": False,  # already root; the image has no sudo
        "common_python_bin": "/usr/local/bin/python3",
        "common_preflight_min_cpus": "1",
        "common_preflight_min_ram_mb": "256",
        "common_preflight_min_disk_gb": "1",
        "common_ssh_runtime_sudo_commands": [],
        "wol_target_require_mac_match": False,  # only lo exists (network none)
        "systemd_skeleton_generate_credentials": True,  # with the systemd-creds test double
        "hermclaw_release_check_credentials_decrypt": True,
        "hermclaw_release_root": "/dist",
        "hermclaw_keep_releases": 1,
        "hermclaw_orchestrator_run_database_tasks": database,
    }


@pytest.fixture(scope="module")
def ansible_wheels(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """ansible-core (same version as the repository venv) for the container's CPython 3.13, downloaded once."""
    import ansible.release

    dest = tmp_path_factory.getbasetemp() / "hermclaw-ansible-cp313"
    if not (dest / ".done").exists():
        dest.mkdir(exist_ok=True)
        res = _run(
            [
                "python3.13",
                "-m",
                "pip",
                "download",
                "--quiet",
                "--only-binary=:all:",
                "--python-version",
                "3.13",
                "--implementation",
                "cp",
                "--abi",
                "cp313",
                "--platform",
                "manylinux_2_34_x86_64",
                "--platform",
                "manylinux_2_28_x86_64",
                "--platform",
                "manylinux_2_17_x86_64",
                "--platform",
                "manylinux2014_x86_64",
                "-d",
                str(dest),
                f"ansible-core=={ansible.release.__version__}",
            ],
            timeout=900,
        )
        if res.returncode != 0:
            pytest.skip(f"cannot download ansible-core for CPython 3.13: {res.stderr[-300:]}")
        (dest / ".done").write_text("ok", encoding="utf-8")
    return dest


@pytest.fixture(scope="module")
def releases(tmp_path_factory: pytest.TempPathFactory) -> Releases:
    return releases_for(tmp_path_factory)


def _start(name: str, workdir: Path, releases: Releases, wheels: Path, model_only: bool, pg: Postgres | None) -> Container:
    tools = {t: shutil.which(t) for t in ("ssh-keygen", "git")}
    if not all(tools.values()):
        pytest.skip("host ssh-keygen and git are needed inside the container")
    fakes = workdir / "fakes"
    fakes.mkdir()
    for fname, text in (("systemd-creds", FAKE_SYSTEMD_CREDS), ("systemd-run", FAKE_SYSTEMD_RUN)):
        (fakes / fname).write_text(text, encoding="utf-8")
        (fakes / fname).chmod(0o755)
    inputs = workdir / "inputs"
    inputs.mkdir()
    (inputs / "hosts.yml").write_text(yaml.safe_dump(_inventory(model_only)), encoding="utf-8")
    (inputs / "overrides.json").write_text(json.dumps(_overrides(pg is not None and not model_only)), encoding="utf-8")
    argv = [
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--network=none",
        "--tmpfs",
        "/tmp:exec",
        "-v",
        f"{REPO}:/src:ro",
        "-v",
        f"{releases.dist}:/dist:ro",
        "-v",
        f"{wheels}:/ansible-wheels:ro",
        "-v",
        f"{fakes}:/opt/fakes:ro",
        "-v",
        f"{inputs}:/work:ro",
    ]
    for tool, path in tools.items():
        argv += ["-v", f"{path}:/opt/hosttools/{tool}:ro"]
    if pg is not None and not model_only:
        argv += ["-v", f"{pg.sock}:/run/postgresql"]
    if model_only:  # check-host.sh wants the GTX 1080 on the PCI bus: one fake NVIDIA display controller
        gpu = workdir / "pci" / "0000:01:00.0"
        gpu.mkdir(parents=True)
        (gpu / "vendor").write_text("0x10de\n", encoding="utf-8")
        (gpu / "class").write_text("0x030000\n", encoding="utf-8")
        argv += ["-v", f"{gpu.parent}:/sys/bus/pci/devices:ro"]
    started = _podman(*argv, IMAGE, "sleep", "14400")
    if started.returncode != 0:
        pytest.skip(f"cannot start a container here: {started.stderr.strip()[:300]}")
    c = Container(
        name,
        workdir,
        ["hermclaw-model-224"] if model_only else HOSTS_A,
        "model_workers" if model_only else "orchestrator:webui:execution_workers",
    )
    setup = c.sh(
        "set -e; grep -q VERSION_CODENAME=trixie /etc/os-release; mkdir -p /etc/ssh; "
        "/opt/hosttools/ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key; "
        "python3 -m venv /opt/ansible; /opt/ansible/bin/pip install -q --no-index --find-links /ansible-wheels ansible-core; "
        # the operator-supplied GitLab token, stored like INSTALLATION.md 6.1 says (before the first deployment)
        "export PATH=/opt/fakes:$PATH HERMCLAW_CREDSTORE_DIR=/etc/credstore.encrypted; "
        + (
            "printf 'worker-token-from-orchestrator\\n' | /src/scripts/bootstrap/hermclaw-credential.sh set worker-token >/dev/null"
            if model_only
            else "printf 'glpat-TESTONLY-gitlab-token\\n' | /src/scripts/bootstrap/hermclaw-credential.sh set gitlab-token >/dev/null"
        ),
        timeout=600,
    )
    assert setup.returncode == 0, setup.stdout + setup.stderr
    return c


@pytest.fixture(scope="module")
def deployment(
    tmp_path_factory: pytest.TempPathFactory, releases: Releases, ansible_wheels: Path, postgres: Postgres | None
) -> Iterator[dict[str, Any]]:
    if os.geteuid() != 0 or shutil.which("podman") is None:
        pytest.skip("needs root and podman")
    if _podman("image", "exists", IMAGE).returncode != 0:
        pytest.skip(f"{IMAGE} not available locally")
    work = tmp_path_factory.mktemp("deploy-converge")
    suffix = uuid.uuid4().hex[:8]
    (work / "a").mkdir()
    (work / "b").mkdir()
    a = _start(f"hcl-deploy-a-{suffix}", work / "a", releases, ansible_wheels, False, postgres)
    try:
        b = _start(f"hcl-deploy-b-{suffix}", work / "b", releases, ansible_wheels, True, None)
    except BaseException:
        _podman("rm", "-f", a.name, timeout=120)
        raise

    def model_worker() -> None:  # container B runs in parallel with A's sequence
        skip = "--skip-tags postgresql,nodejs,nvidia,ollama"
        b.playbook("bootstrap", "bootstrap.yml", skip)
        if b.runs["bootstrap"].returncode != 0:
            return
        b.snapshots["before dry run"] = _snapshot(b)
        b.playbook("dry-run", "deploy.yml", "--check", "--diff")
        b.snapshots["after dry run"] = _snapshot(b)
        b.playbook("deploy-1", "deploy.yml")
        if b.runs["deploy-1"].returncode == 0:
            b.playbook("site-2", "site.yml", skip)
            b.playbook("check", "deploy.yml", "--check", "--diff")

    thread = threading.Thread(target=model_worker, daemon=True)
    thread.start()
    try:
        yield {"a": a, "b": b, "b_thread": thread, "pg": postgres, "releases": releases, "work": work}
    finally:
        thread.join(timeout=3600)
        for c in (a, b):
            _podman("rm", "-f", c.name, timeout=120)


# --------------------------------------------------------------------------------------------- helpers on A


def _link(c: Container, path: str) -> str:
    return c.out(f"readlink {path}").strip()


def _fake_runs(c: Container) -> list[list[str]]:
    res = c.sh("cat /var/log/hermclaw-fake-systemd-run.jsonl 2>/dev/null || true")
    return [json.loads(ln) for ln in res.stdout.splitlines() if ln.strip()]


def _cli_commands(c: Container) -> list[str]:
    """`<venv>/bin/<tool> <args>` of every operator-CLI call (after systemd-run's --; a leading `/usr/bin/env
    HERMCLAW_CONFIG_DIR=<dir>` of --config-dir is shown as `[<dir>] `)."""
    out = []
    for argv in _fake_runs(c):
        cmd = argv[argv.index("--") + 1 :] if "--" in argv else argv
        prefix = ""
        if cmd[0] == "/usr/bin/env":
            prefix, cmd = f"[{cmd[1].split('=', 1)[1]}] ", cmd[2:]
        out.append(prefix + " ".join([cmd[0].rsplit("/", 3)[-3] + "/" + cmd[0].rsplit("/", 1)[-1], *cmd[1:]]))
    return out


SNAPSHOT_ROOTS = "/opt /etc/hermclaw /etc/credstore.encrypted /etc/systemd/system /etc/ansible /etc/containers /var/www /etc/nginx"


def _snapshot(c: Container) -> str:
    """Every path below the deployment's directories with type, size, mode, owner, mtime and link target."""
    return c.out(
        f"find {SNAPSHOT_ROOTS} -path /opt/ansible -prune -o -name __pycache__ -prune -o "
        "-printf '%p %y %s %m %u %T@ %l\\n' 2>/dev/null | LC_ALL=C sort"
    )


def _no_secrets(c: Container) -> None:
    """Neither the operator's GitLab token nor the generated admin token appears in any Ansible output."""
    token = (
        _decrypt(c, "api-operator-token") if c.sh("test -f /etc/credstore.encrypted/hermclaw-api-operator-token").returncode == 0 else None
    )
    for label, res in c.runs.items():
        text = res.stdout + res.stderr
        assert "glpat-TESTONLY" not in text, label
        assert token is None or token not in text, label


def _decrypt(c: Container, name: str) -> str:
    blob = c.out(f"cat /etc/credstore.encrypted/hermclaw-{name}").encode()
    magic, embedded, payload = blob.split(b"\n")[:3]
    assert magic == b"HERMCLAW-TEST-CREDENTIAL" and embedded.decode() == name
    return base64.b64decode(payload).decode()


# --------------------------------------------------------------------------------------------- tests


def test_site_converges_bootstrap_and_deployment(deployment: dict[str, Any]) -> None:
    a: Container = deployment["a"]
    pg: Postgres | None = deployment["pg"]
    res, recap = a.playbook("bootstrap", "bootstrap.yml", "--skip-tags postgresql,nodejs")
    _assert_ok(res, recap, HOSTS_A, idempotent=False)
    # the first deployment's dry run (docs/operations/deployment.md section 3) on freshly bootstrapped hosts
    before = _snapshot(a)
    res, recap = a.playbook("dry-run", "deploy.yml", "--check", "--diff")
    _assert_ok(res, recap, HOSTS_A, idempotent=False)
    assert all(r[1] > 0 for r in recap.values()), "the dry run reports what the deployment would change"
    assert _snapshot(a) == before, "the dry run changes nothing on disk"
    res, recap = a.playbook("deploy-1", "deploy.yml")
    _assert_ok(res, recap, HOSTS_A, idempotent=False)
    assert all(r[1] > 0 for r in recap.values())

    # 32.1 release venv + atomic symlink; the bootstrap's plain venv directory is kept under a new name
    assert _link(a, "/opt/hermclaw/venv") == f"venv-{V1}" and _link(a, "/opt/hermclaw/current") == f"releases/{V1}"
    kept = a.out("ls -d /opt/hermclaw/venv.pre-release-* | wc -l").strip()
    assert kept == "1", "plain venv directory migrated without data loss"
    assert a.out("/opt/hermclaw/venv/bin/hermclaw --version").strip() == f"hermclaw {V1}"
    assert a.out(f"cat /opt/hermclaw/venv-{V1}/.hermclaw-release").count(V1) == 1
    assert a.out(f"stat -c '%U %a' /opt/hermclaw/venv-{V1} /opt/hermclaw/venv-{V1}/bin/hermclaw").split() == ["root", "755", "root", "755"]
    assert _link(a, "/opt/hermclaw-exec/venv") == f"venv-{V1}", "execution worker"
    a.out(f"test -f /opt/hermclaw/releases/{V1}/.hermclaw-unpacked && test -d /opt/hermclaw/releases/{V1}/sandbox-images/python")
    # tree-sitter grammars of the release: the services' environment points at them, and they load as the service
    # user without network (the container has none) and without writing (research 027 Y6)
    grammars = a.out(
        "set -e; ts=$(sed -n 's/^TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=//p' /etc/hermclaw/hermclaw.env); echo \"dir $ts\"; "
        "runuser -u hermclaw -- env HOME=/nonexistent TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=$ts /opt/hermclaw/venv/bin/python -I -c '"
        "from hermclaw.repo_intelligence.symbols import TREE_SITTER_GRAMMARS, _parser\n"
        "for lang in sorted(set(TREE_SITTER_GRAMMARS.values())):\n"
        '    root = _parser(lang).parse(b"<?php function f() {}" if lang == "php" else b"function f() {}").root_node\n'
        "    print(lang, root.type, root.has_error)'"
    ).splitlines()
    assert grammars[0] == "dir /opt/hermclaw/current/tree-sitter", grammars
    # the API trusts X-Forwarded-For/-Proto of the nginx host only (research 029 E2/E3), never "*"
    env = a.out("cat /etc/hermclaw/hermclaw.env").splitlines()
    assert "HERMCLAW_API_FORWARDED_ALLOW_IPS=192.168.178.223" in env, env
    assert grammars[1:] == [f"{lang} program False" for lang in deployment["releases"].meta(V1)["tree_sitter"]["languages"]]

    # configuration: rendered, validated and owned by the deployment (real MAC addresses from the inventory)
    modes = a.out("stat -c '%n %U %G %a' /etc/hermclaw/{hosts,models,policies,capabilities,logging,databases,litellm}.yaml").splitlines()
    assert all(m.endswith(" root hermclaw 640") for m in modes), modes
    check = json.loads(a.out("HERMCLAW_CONFIG_DIR=/etc/hermclaw HERMCLAW_ENV=production /opt/hermclaw/venv/bin/hermclaw config-check"))
    assert check["version"] == V1 and {h["id"] for h in check["hosts"]} >= {"orchestrator-225", "webui-223", "exec-222", "model-224"}
    hosts = {h["id"]: h for h in yaml.safe_load(a.out("cat /etc/hermclaw/hosts.yaml"))["hosts"]}
    assert (
        hosts["exec-222"]["wake_on_lan"]["mac"] == "52:54:00:12:02:22" and hosts["model-224"]["wake_on_lan"]["mac"] == "52:54:00:12:02:24"
    )
    policies = yaml.safe_load(a.out("cat /etc/hermclaw/policies.yaml"))
    assert policies["sandbox"]["images"]["python"] == "localhost/hermclaw-sandbox-python:1"

    # 32.5 LiteLLM: pinned venv from the release lock, config generated from models.yaml, unit enabled
    meta = deployment["releases"].meta(V1)
    llm_venv = str(meta["litellm"]["venv_name"])
    assert _link(a, "/opt/litellm/venv") == llm_venv
    assert (
        a.out("/opt/litellm/venv/bin/python -I -c \"import importlib.metadata as m; print(m.version('litellm'))\"").strip()
        == meta["litellm"]["version"]
    )
    assert a.out("find /opt/litellm -name litellm_init.pth | wc -l").strip() == "0"
    litellm = yaml.safe_load(a.out("cat /etc/hermclaw/litellm.yaml"))
    assert litellm["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"
    aliases = {d["model_name"]: d["litellm_params"] for d in litellm["model_list"]}
    assert aliases["coder-main"]["model"] == "ollama_chat/qwen3-coder:30b" and aliases["planner-gemma"]["model"] == "ollama_chat/gemma4:26b"
    assert aliases["heavy-review"]["model"] == "ollama_chat/qwen3.8:27b" and aliases["fast-router"]["model"] == "ollama_chat/qwen3:8b"
    assert aliases["embedding"]["model"] == "ollama/embeddinggemma:300m"
    assert {p["api_base"] for p in aliases.values()} == {"http://192.168.178.224:11434"}

    # units: enabled for boot (the links systemctl enable creates), SearXNG Quadlet with [Install]
    wants = a.out("ls /etc/systemd/system/multi-user.target.wants /etc/systemd/system/timers.target.wants").split()
    for unit in (
        "hermclaw-api.service",
        "hermclaw-scheduler.service",
        "hermclaw-litellm.service",
        "hermclaw-exec-worker.service",
        "hermclaw-backup.timer",
        "hermclaw-restore-test.timer",
    ):
        assert unit in wants, unit
    assert _link(a, "/etc/systemd/system/multi-user.target.wants/hermclaw-api.service") == "/etc/systemd/system/hermclaw-api.service"
    quadlet = a.out("cat /etc/containers/systemd/users/*/hermclaw-searxng.container")
    assert quadlet.rstrip().endswith("[Install]\nWantedBy=default.target")
    assert json.loads(a.out("cat /etc/ansible/facts.d/hermclaw.fact"))["deployed"] is True

    # operator tools
    for path, mode in (
        ("/usr/local/sbin/hermclaw", "755"),
        ("/opt/hermclaw/bin/hermclaw-backup", "755"),
        ("/opt/hermclaw/bin/hermclaw-restore-test", "755"),
        ("/opt/hermclaw/bin/hermclaw-build-sandbox-images", "755"),
        ("/usr/local/bin/hermclaw-ssh-gate", "755"),
    ):
        assert a.out(f"stat -c '%U %a' {path}").split() == ["root", mode], path
    # reference configuration of the backup (copied to /etc/hermclaw/backup.yaml by the operator)
    assert "restore_test:" in a.out("cat /opt/hermclaw/current/backup/backup.example.yaml")

    # 32.2 UI: versioned build + atomic webroot symlink (the bootstrap's placeholder page is gone); 32.7 TLS
    assert _link(a, "/var/www/hermclaw-next") == f"hermclaw-next-releases/{V1}"
    assert '<div id="root">' in a.out("cat /var/www/hermclaw-next/index.html")
    a.out(f"test -f /var/www/hermclaw-next-releases/{V1}.release")
    site = a.out("cat /etc/nginx/sites-enabled/hermclaw-next.conf")
    # D-013: the shared .223 keeps 80/443 for the legacy portal, Hermclaw's nginx uses 8080/8443
    assert "listen 8443 ssl;" in site and "http2 on;" in site and "return 301 https://$host:8443$request_uri;" in site
    assert "listen 8080;" in site and not re.search(r"listen (\[::\]:)?(80|443)[ ;]", site)
    assert "upgrade-insecure-requests" in site and 'proxy_set_header Cookie "";' in site
    assert hosts["webui-223"]["services"] == [{"name": "nginx", "port": 8080, "http_path": "/healthz"}]
    # no legacy portal in the container: the portal button is skipped, nothing else is written below /var/www
    assert a.out("ls -A /var/www").split() == ["hermclaw-next", "hermclaw-next-releases"]
    san = a.out("openssl x509 -in /etc/nginx/tls/hermclaw-web.crt -noout -ext subjectAltName")
    assert "DNS:hermclaw.lan" in san and "IP Address:192.168.178.223" in san
    assert a.out("stat -c '%a' /etc/nginx/tls/hermclaw-web.key").strip() == "600"

    if pg is None:
        pytest.skip("no private PostgreSQL cluster: schema tasks not exercised (the rest of the converge passed)")
    # 32.1 migration before the switch, through the operator CLI (as hermclaw, production env, credentials)
    assert pg.sql("SELECT version_num FROM alembic_version") == meta["alembic_head"]
    cmds = _cli_commands(a)
    staging = "[/etc/hermclaw/deploy-staging] "  # the new release's validated configuration, before it is live
    assert cmds[:3] == [
        f"{staging}venv-{V1}/hermclaw doctor --offline --json",
        f"{staging}venv-{V1}/hermclaw migrate",
        f"{staging}venv-{V1}/hermclaw doctor --offline --json",
    ]
    first = _fake_runs(a)[1]
    assert "--uid=hermclaw" in first and "--pipe" in first and "--wait" in first and "--collect" in first
    props = [first[i + 1] for i, x in enumerate(first) if x == "-p"]
    assert "EnvironmentFile=/etc/hermclaw/hermclaw.env" in props
    assert {p.split(":", 1)[0] for p in props if p.startswith("LoadCredentialEncrypted=")} == {
        "LoadCredentialEncrypted=litellm-master-key",
        "LoadCredentialEncrypted=gitlab-token",
        "LoadCredentialEncrypted=worker-token-exec-222",
        "LoadCredentialEncrypted=worker-token-model-224",
    }
    # 32.8 admin API token: created once, stored as an encrypted credential, never printed
    token = _decrypt(a, "api-operator-token")
    assert token.startswith("hct_") and len(token) > 40
    assert any(c.startswith("venv/hermclaw token-create operator-admin-") and c.endswith("--scopes admin") for c in cmds), cmds
    import hashlib

    row = pg.sql(f"SELECT name || ' ' || scopes::text FROM api_tokens WHERE token_hash = '{hashlib.sha256(token.encode()).hexdigest()}'")
    assert row.startswith("operator-admin-") and '"admin"' in row
    assert token not in res.stdout + res.stderr, "the token never reaches Ansible's output"
    assert "glpat-TESTONLY" not in res.stdout + res.stderr


def test_second_site_run_and_check_mode_change_nothing(deployment: dict[str, Any]) -> None:
    a: Container = deployment["a"]
    if a.runs.get("deploy-1") is None or a.runs["deploy-1"].returncode != 0:
        pytest.skip("first converge failed")
    res, recap = a.playbook("site-2", "site.yml", "--skip-tags postgresql,nodejs")
    _assert_ok(res, recap, HOSTS_A, idempotent=True)
    res, recap = a.playbook("check", "deploy.yml", "--check", "--diff")
    _assert_ok(res, recap, HOSTS_A, idempotent=True)
    if deployment["pg"] is not None:
        assert sum(1 for c in _cli_commands(a) if c.endswith(" migrate")) == 1, "the migration ran exactly once"
        assert sum(1 for c in _cli_commands(a) if "token-create" in c) == 1, "the admin token was created exactly once"


def test_deployed_units_pass_systemd_analyze_verify_in_the_rootfs(deployment: dict[str, Any]) -> None:
    a: Container = deployment["a"]
    if a.runs.get("deploy-1") is None or a.runs["deploy-1"].returncode != 0:
        pytest.skip("first converge failed")
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze not installed")
    mount = _podman("mount", a.name)
    if mount.returncode != 0:
        pytest.skip(f"podman mount unavailable: {mount.stderr[:200]}")
    root = mount.stdout.strip()
    try:
        units = [
            "hermclaw-api.service",
            "hermclaw-scheduler.service",
            "hermclaw-litellm.service",
            "hermclaw-backup.service",
            "hermclaw-backup.timer",
            "hermclaw-restore-test.service",
            "hermclaw-restore-test.timer",
            "hermclaw-exec-worker.service",
        ]
        res = _run([analyze, "verify", f"--root={root}", *units], timeout=300)
        problems = [ln for ln in res.stderr.splitlines() if "sysinit.target" not in ln and "Unit " not in ln and ln.strip()]
        problems = [
            ln
            for ln in problems
            if re.search(r"Unknown (key|section|lvalue)|Failed to parse|Invalid|is not executable|not found|bad unit", ln)
        ]
        assert not problems, res.stderr
        # not vacuous: an ExecStart that does not exist in this rootfs is reported
        broken = Path(root) / "etc/systemd/system/hermclaw-deploy-broken.service"
        broken.write_text("[Service]\nExecStart=/opt/hermclaw/venv/bin/does-not-exist\n", encoding="utf-8")
        try:
            bad = _run([analyze, "verify", f"--root={root}", "hermclaw-deploy-broken.service"], timeout=60)
            assert "is not executable" in bad.stderr, bad.stderr
        finally:
            broken.unlink()
    finally:
        _podman("unmount", a.name)


def test_operator_cli_and_doctor_in_the_services_context(deployment: dict[str, Any]) -> None:
    a: Container = deployment["a"]
    if a.runs.get("deploy-1") is None or a.runs["deploy-1"].returncode != 0:
        pytest.skip("first converge failed")
    if deployment["pg"] is None:
        pytest.skip("no PostgreSQL for the doctor's database checks")
    res = a.sh("PATH=/opt/fakes:$PATH /usr/local/sbin/hermclaw doctor --offline --json")
    assert res.returncode in (0, 1), res.stderr
    checks = {c["name"]: c for c in json.loads(res.stdout)["checks"]}
    assert checks["config:files"]["status"] == "ok", "real /etc/hermclaw/*.yaml, not the .example fallbacks"
    assert "config:wake_on_lan" not in checks, "no placeholder MAC addresses"
    assert checks["config:roles"]["status"] == "ok" and checks["config:hosts"]["status"] == "ok"
    assert checks["database"]["status"] == "ok" and checks["api_tokens"]["status"] == "ok"
    assert all(c["status"] in ("ok", "warn", "skip") for n, c in checks.items() if n != "database:pgvector"), checks
    denied = a.sh("setpriv --reuid=hermclaw --regid=hermclaw --init-groups /usr/local/sbin/hermclaw doctor")
    assert denied.returncode == 77


def test_deployed_nginx_site_passes_nginx_t(deployment: dict[str, Any]) -> None:
    a: Container = deployment["a"]
    if a.runs.get("deploy-1") is None or a.runs["deploy-1"].returncode != 0:
        pytest.skip("first converge failed")
    if _podman("image", "exists", NGINX_IMAGE).returncode != 0:
        pytest.skip(f"{NGINX_IMAGE} not available locally")
    out = deployment["work"] / "nginx"
    out.mkdir(exist_ok=True)
    for src, dest in (
        ("/etc/nginx/sites-available/hermclaw-next.conf", "site.conf"),
        ("/etc/nginx/tls", "tls"),
        (f"/var/www/hermclaw-next-releases/{V1}", "webroot"),
    ):
        assert _podman("cp", f"{a.name}:{src}", str(out / dest)).returncode == 0, src
    conf = out / "nginx.conf"
    conf.write_text(
        "user nginx;\nevents {}\nhttp {\n include /etc/nginx/mime.types;\n include /etc/nginx/sites-enabled/*;\n}\n", encoding="utf-8"
    )
    for p in [conf, *out.rglob("*")]:
        p.chmod(0o755 if p.is_dir() else 0o644)
    res = _podman(
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        f"{conf}:/etc/nginx/nginx.conf:ro",
        "-v",
        f"{out / 'site.conf'}:/etc/nginx/sites-enabled/hermclaw-next.conf:ro",
        "-v",
        f"{out / 'tls'}:/etc/nginx/tls:ro",
        "-v",
        f"{out / 'webroot'}:/var/www/hermclaw-next:ro",
        NGINX_IMAGE,
        "nginx",
        "-t",
    )
    assert res.returncode == 0 and "test is successful" in res.stderr, res.stderr


def test_upgrade_switches_and_prunes_old_releases(deployment: dict[str, Any]) -> None:
    a: Container = deployment["a"]
    pg: Postgres | None = deployment["pg"]
    if a.runs.get("deploy-1") is None or a.runs["deploy-1"].returncode != 0:
        pytest.skip("first converge failed")
    llm_before = a.out("stat -c %Y /opt/litellm/venv/.hermclaw-litellm").strip()
    # dry run of the upgrade (docs/runbooks/upgrade.md step 2): the new release is neither unpacked nor installed
    before = _snapshot(a)
    res, recap = a.playbook("v2-dry-run", "deploy.yml", f"-e hermclaw_version={V2}", "--check", "--diff")
    _assert_ok(res, recap, HOSTS_A, idempotent=False)
    assert all(r[1] > 0 for r in recap.values()), "the dry run reports the switch on every host"
    assert _snapshot(a) == before, "the dry run of an upgrade changes nothing on disk"
    calls_before = len(_fake_runs(a))
    res, recap = a.playbook("v2", "deploy.yml", f"-e hermclaw_version={V2}")
    _assert_ok(res, recap, HOSTS_A, idempotent=False)
    assert _link(a, "/opt/hermclaw/venv") == f"venv-{V2}" and _link(a, "/opt/hermclaw-exec/venv") == f"venv-{V2}"
    assert _link(a, "/var/www/hermclaw-next") == f"hermclaw-next-releases/{V2}"
    assert a.out("/opt/hermclaw/venv/bin/hermclaw --version").strip() == f"hermclaw {V2}"
    assert a.out("stat -c %Y /opt/litellm/venv/.hermclaw-litellm").strip() == llm_before, "same LiteLLM lock: not reinstalled"
    if pg is not None:
        assert [c for c in _cli_commands(a)[calls_before:] if c.endswith(" migrate")] == [], "same schema head: no migration"
    res, recap = a.playbook("v3", "deploy.yml", f"-e hermclaw_version={V3}")
    _assert_ok(res, recap, HOSTS_A, idempotent=False)
    assert _link(a, "/opt/hermclaw/venv") == f"venv-{V3}"
    # hermclaw_keep_releases=1: the active release and one previous one stay, older venvs/payloads/UI builds go
    assert sorted(a.out("ls -d /opt/hermclaw/venv-* /opt/hermclaw/releases/*").split()) == sorted(
        [f"/opt/hermclaw/venv-{V2}", f"/opt/hermclaw/venv-{V3}", f"/opt/hermclaw/releases/{V2}", f"/opt/hermclaw/releases/{V3}"]
    )
    assert a.out("ls /var/www/hermclaw-next-releases").split() == [V2, f"{V2}.release", V3, f"{V3}.release"]
    assert sorted(a.out("ls -d /opt/hermclaw-exec/venv-*").split()) == [f"/opt/hermclaw-exec/venv-{V2}", f"/opt/hermclaw-exec/venv-{V3}"]
    a.out("ls -d /opt/hermclaw/venv.pre-release-*")  # the bootstrap's directory is never pruned
    if pg is not None:
        assert pg.sql("SELECT version_num FROM alembic_version") == "0003"
        assert pg.sql("SELECT count(*) FROM information_schema.tables WHERE table_name = 'deploy_test_marker'") == "1"


def test_rollback_needs_the_schema_downgrade_opt_in_and_reuses_the_installed_venv(deployment: dict[str, Any]) -> None:
    a: Container = deployment["a"]
    pg: Postgres | None = deployment["pg"]
    if a.runs.get("v3") is None or a.runs["v3"].returncode != 0:
        pytest.skip("upgrade to the third release failed")
    venv_mtime = a.out(f"stat -c %Y /opt/hermclaw/venv-{V2}/.hermclaw-release").strip()
    if pg is not None:
        live = "sha256sum /etc/hermclaw/*.yaml && readlink /opt/hermclaw/venv /opt/hermclaw/current /opt/litellm/venv"
        before = a.out(live)
        # the refused run also carries a configuration change (policies.yaml, litellm.yaml): it must not become live
        change = {
            "hermclaw_config_overrides": {"policies": {"coder": {"max_turns": 7}}, "models": {"litellm": {"request_timeout_seconds": 901}}}
        }
        res, recap = a.playbook("rollback-refused", "deploy.yml", f"-e hermclaw_version={V2}", f"-e {shlex.quote(json.dumps(change))}")
        assert res.returncode != 0
        assert f"The database is at schema revision 0003, which release {V2}" in res.stdout and "allow_schema_downgrade=true" in res.stdout
        assert a.out(live) == before, "nothing was switched: venvs, LiteLLM and /etc/hermclaw are unchanged"
        assert _link(a, "/var/www/hermclaw-next") == f"hermclaw-next-releases/{V3}"
    args = [f"-e hermclaw_version={V2}", "-e hermclaw_orchestrator_allow_schema_downgrade=true"]
    res, recap = a.playbook("rollback", "deploy.yml", *args)
    _assert_ok(res, recap, HOSTS_A, idempotent=False)
    assert _link(a, "/opt/hermclaw/venv") == f"venv-{V2}" and _link(a, "/opt/hermclaw-exec/venv") == f"venv-{V2}"
    assert _link(a, "/var/www/hermclaw-next") == f"hermclaw-next-releases/{V2}"
    assert a.out(f"stat -c %Y /opt/hermclaw/venv-{V2}/.hermclaw-release").strip() == venv_mtime, "rollback reuses the kept venv"
    assert "Install the release offline" not in _changed_tasks(res)
    if pg is not None:
        assert pg.sql("SELECT version_num FROM alembic_version") == "0002"
        assert any(
            c == f"venv-{V3}/alembic -c /opt/hermclaw/venv-{V3}/share/hermclaw/alembic.ini downgrade 0002" for c in _cli_commands(a)
        ), _cli_commands(a)
    res, recap = a.playbook("rollback-2", "deploy.yml", *args)
    _assert_ok(res, recap, HOSTS_A, idempotent=True)
    _no_secrets(a)


def test_model_worker_converges_and_is_idempotent(deployment: dict[str, Any]) -> None:
    b: Container = deployment["b"]
    deployment["b_thread"].join(timeout=3600)
    hosts = ["hermclaw-model-224"]
    for label in ("bootstrap", "dry-run", "deploy-1"):
        res = b.runs.get(label)
        assert res is not None, f"the model worker run {label} did not start"
        recap = {m.group(1): tuple(int(x) for x in m.groups()[1:]) for m in RECAP.finditer(res.stdout)}
        _assert_ok(res, recap, hosts, idempotent=False)
    assert b.snapshots["before dry run"] == b.snapshots["after dry run"], "the dry run changes nothing on disk"
    assert _link(b, "/opt/hermclaw/venv") == f"venv-{V1}"
    assert "WORKER_KIND=model" in b.out("cat /etc/hermclaw/worker.env")
    files = b.out("ls /etc/hermclaw").split()
    assert "capabilities.yaml" in files and "hosts.yaml" not in files and "models.yaml" not in files, "workers get only what they read"
    b.out("test -L /etc/systemd/system/multi-user.target.wants/hermclaw-model-worker.service")
    assert b.out("stat -c '%U %a' /opt/hermclaw/bin/hermclaw-model-pull").split() == ["root", "755"]
    for label in ("site-2", "check"):
        res = b.runs.get(label)
        assert res is not None, label
        recap = {m.group(1): tuple(int(x) for x in m.groups()[1:]) for m in RECAP.finditer(res.stdout)}
        _assert_ok(res, recap, hosts, idempotent=True)
    for label, res in b.runs.items():
        assert "worker-token-from-orchestrator" not in res.stdout + res.stderr, label
