"""P26 SSH/admin tools against a REAL OpenSSH server on 127.0.0.1 (temp host key, temp authorized_keys).

The target hosts of the LAN (BLOCKER-001) are replaced by a local ``sshd`` that runs the same OpenSSH code path:
key authentication, pinned host key (``StrictHostKeyChecking=yes``), forced-command gate, stdin streaming.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import select

from hermclaw.contracts.events import EventType
from hermclaw.core.config import CommandPolicy, HostConfig, SshConfig, SshPolicy, load_config
from hermclaw.core.redaction import REDACTED, Redactor
from hermclaw.persistence.models import CommandRun, Event, Job
from hermclaw.security.secrets import SecretStore
from hermclaw.sshtools.approvals import APPROVALS_KEY, CONSUMED_KEY, approval_state, grant_approval
from hermclaw.sshtools.client import SshRunner
from hermclaw.sshtools.errors import SshConfigError, SshPolicyError, SshTimeoutError
from hermclaw.sshtools.keys import KeyManager
from hermclaw.sshtools.policy import SshCommandPolicy, approval_token
from hermclaw.sshtools.registry import HostRegistry
from hermclaw.sshtools.tools import SshCallContext, SshOutcome, SshTools
from hermclaw.wol.idle import RemoteCommandRunner

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "scripts" / "ops" / "hermclaw-ssh-gate"
SSHD = shutil.which("sshd") or "/usr/sbin/sshd"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (Path(SSHD).exists() and shutil.which("ssh") and shutil.which("ssh-keygen")), reason="OpenSSH not installed"),
]

FAKE_SYSTEMCTL = """#!/bin/sh
# test double: is-active / status for a few unit names; 'nosystemd' behaves like a container without systemd
verb=$1; shift
for last; do :; done
unit=$last
case $unit in
  realprobe) exec /usr/bin/systemctl "$verb" "$@" ;;
  nosystemd) echo "System has not been booted with systemd as init system (PID 1). Can't operate." >&2
             echo "Failed to connect to bus: Host is down" >&2; exit 1 ;;
esac
case $verb in
  is-active)
    case $unit in nginx) echo active; exit 0 ;; stopped) echo inactive; exit 3 ;; *) echo inactive; exit 4 ;; esac ;;
  status)
    case $unit in
      nginx) printf '* nginx.service - fake nginx\\n     Active: active (running)\\n'; exit 0 ;;
      stopped) printf '* stopped.service - fake\\n     Active: inactive (dead)\\n'; exit 3 ;;
      *) echo "Unit $unit.service could not be found." >&2; exit 4 ;;
    esac ;;
esac
exit 2
"""

FAKE_LOGGER = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _keygen(path: Path) -> None:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(path)], check=True)


@dataclass
class SshdServer:
    root: Path
    port: int
    user: str
    client_key: Path
    gate_key: Path
    known_hosts: Path
    allowlist: Path
    syslog: Path
    proc: subprocess.Popen[bytes]

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def host(
        self,
        host_id: str = "lab",
        *,
        key: Path | None = None,
        key_ref: str | None = None,
        known_hosts: Path | None = None,
        sudo: tuple[str, ...] = (),
        port: int | None = None,
    ) -> HostConfig:
        return HostConfig(
            id=host_id,
            address="127.0.0.1",
            role="webui",
            ssh=SshConfig(
                user=self.user,
                port=port or self.port,
                key_ref=key_ref or f"file:{key or self.client_key}",
                known_hosts=str(known_hosts or self.known_hosts),
                allow_sudo_commands=list(sudo),
            ),
        )


def start_sshd(root: Path) -> SshdServer:
    """Start ``sshd -D`` on a free loopback port; the current user logs in with a fresh ed25519 key."""
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    user = pwd.getpwuid(os.geteuid()).pw_name
    _keygen(root / "host_key")
    _keygen(root / "client_key")
    _keygen(root / "gate_key")
    fakebin = root / "fakebin"
    fakebin.mkdir()
    syslog = root / "syslog.txt"
    for name, body in (("systemctl", FAKE_SYSTEMCTL), ("logger", FAKE_LOGGER.format(log=syslog))):
        (fakebin / name).write_text(body, encoding="utf-8")
        os.chmod(fakebin / name, 0o755)
    allowlist = root / "ssh-allowlist"
    allowlist.write_text(
        "# test allow-list\necho gated-ok\ncat /etc/hostname\nshell:sh -c .* hermclaw-upload .*\n",
        encoding="utf-8",
    )
    os.chmod(allowlist, 0o644)
    (root / "authorized_keys").write_text(
        (root / "client_key.pub").read_text() + f'restrict,command="{GATE} {allowlist}" ' + (root / "gate_key.pub").read_text(),
        encoding="utf-8",
    )
    os.chmod(root / "authorized_keys", 0o600)
    port = _free_port()
    config = root / "sshd_config"
    config.write_text(
        "\n".join(
            [
                f"Port {port}",
                "ListenAddress 127.0.0.1",
                f"HostKey {root / 'host_key'}",
                f"AuthorizedKeysFile {root / 'authorized_keys'}",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "PubkeyAuthentication yes",
                "UsePAM no",
                "StrictModes no",
                "PermitRootLogin prohibit-password",
                f"PidFile {root / 'sshd.pid'}",
                f"SetEnv PATH={fakebin}:/usr/local/bin:/usr/bin:/bin",
                "MaxStartups 100",
                "MaxSessions 50",
                "LogLevel VERBOSE",
                "",
            ]
        ),
        encoding="utf-8",
    )
    if os.geteuid() == 0:
        Path("/run/sshd").mkdir(mode=0o755, exist_ok=True)
    log = (root / "sshd.log").open("wb")
    proc = subprocess.Popen([SSHD, "-D", "-e", "-f", str(config)], stdout=log, stderr=log)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"sshd exited: {(root / 'sshd.log').read_text()[-500:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.05)
    host_pub = (root / "host_key.pub").read_text().split()
    known_hosts = root / "known_hosts"
    known_hosts.write_text(f"[127.0.0.1]:{port} {host_pub[0]} {host_pub[1]}\n", encoding="utf-8")
    os.chmod(known_hosts, 0o644)
    return SshdServer(root, port, user, root / "client_key", root / "gate_key", known_hosts, allowlist, syslog, proc)


@pytest.fixture(scope="module")
def sshd(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SshdServer]:
    server = start_sshd(tmp_path_factory.mktemp("sshd"))
    yield server
    server.stop()


def _commands() -> CommandPolicy:
    return load_config().policies.commands


def build_tools(
    server: SshdServer,
    sessionmaker: object,
    tmp_path: Path,
    *,
    require_approval: bool = True,
    compound: tuple[str, ...] = (),
    max_output: int = 200_000,
    ssh_binary: str = "ssh",
    redactor: Redactor | None = None,
    extra_hosts: tuple[HostConfig, ...] = (),
    allowed: tuple[str, ...] = ("lab", "gated"),
) -> SshTools:
    hosts = [server.host("lab", sudo=("/bin/true hermclaw-sudo-check",)), server.host("gated", key=server.gate_key), *extra_hosts]
    hosts.append(HostConfig(id="not-listed", address="127.0.0.1", role="webui", ssh=SshConfig(port=server.port)))
    registry = HostRegistry(hosts, allowed)
    policy = SshCommandPolicy(
        _commands(), SshPolicy(allowed_hosts=list(allowed), require_approval_for_mutations=require_approval), compound_allowlist=compound
    )
    kwargs = {"redactor": redactor} if redactor is not None else {}
    runner = SshRunner(
        registry,
        KeyManager(runtime_dir=tmp_path / "runtime"),
        policy=policy,
        default_timeout=30,
        max_output_bytes=max_output,
        ssh_binary=ssh_binary,
        kill_grace_seconds=1.0,
        **kwargs,  # type: ignore[arg-type]
    )
    return SshTools(runner, policy, sessionmaker, **kwargs)  # type: ignore[arg-type]


async def _job(sessionmaker: object) -> uuid.UUID:
    async with sessionmaker() as s, s.begin():  # type: ignore[operator]
        job = Job(title=f"ssh-{uuid.uuid4().hex[:6]}", prompt="p", status="running")
        s.add(job)
        await s.flush()
        return job.id


def _ctx(job_id: uuid.UUID | None = None, *, capability: str = "ssh", step_id: uuid.UUID | None = None) -> SshCallContext:
    return SshCallContext(job_id=job_id, step_id=step_id or uuid.uuid4(), attempt_id=uuid.uuid4(), capability=capability)


async def _runs(sessionmaker: object, step_id: uuid.UUID | None) -> list[CommandRun]:
    async with sessionmaker() as s:  # type: ignore[operator]
        return list((await s.scalars(select(CommandRun).where(CommandRun.step_id == step_id).order_by(CommandRun.created_at))).all())


async def _events(sessionmaker: object, step_id: uuid.UUID | None) -> list[Event]:
    async with sessionmaker() as s:  # type: ignore[operator]
        rows = await s.scalars(
            select(Event).where(Event.step_id == step_id, Event.event_type == EventType.SSH_COMMAND).order_by(Event.sequence)
        )
        return list(rows.all())


# ==================================================================================================== runner (transport)
async def test_runner_satisfies_remote_command_runner_protocol(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    runner: RemoteCommandRunner = tools.runner  # static: SshRunner implements the WOL idle-hook protocol
    code, out, err = await runner.run("lab", "echo hello-$((40+2)) && echo to-stderr >&2", 20)
    assert (code, out, err) == (0, "hello-42\n", "to-stderr\n")
    code, _out, _err = await runner.run("lab", "exit 7", 20)
    assert code == 7


async def test_runner_argv_is_hardened(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    host = tools.runner.registry.get("lab")
    argv = tools.runner.build_argv(host, sshd.client_key, "uptime")
    joined = " ".join(argv)
    for opt in (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        f"UserKnownHostsFile={sshd.known_hosts}",
        "IdentitiesOnly=yes",
        "ConnectTimeout=10",
        "ForwardAgent=no",
        "IdentityAgent=none",
    ):
        assert opt in joined
    assert argv[:3] == ["ssh", "-F", "/dev/null"]
    assert argv[-3:] == [f"{sshd.user}@127.0.0.1", "--", "uptime"]
    assert argv[argv.index("-p") + 1] == str(sshd.port) and argv[argv.index("-i") + 1] == str(sshd.client_key)


async def test_runner_timeout_kills_the_ssh_client(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    marker = f"sleep 3{uuid.uuid4().int % 1000}.25"
    started = time.monotonic()
    res = await tools.runner.execute("lab", f"echo before; {marker}", timeout=1.0)
    elapsed = time.monotonic() - started
    assert res.timed_out and res.exit_code is None
    assert res.stdout == "before\n"  # output gathered before the kill is kept
    assert elapsed < 8
    local = subprocess.run(["pgrep", "-f", f"ssh .*{marker}"], capture_output=True, text=True, check=False)
    assert local.stdout.strip() == "", "the local ssh client must be gone after the timeout"
    with pytest.raises(SshTimeoutError):
        await tools.runner.run("lab", "sleep 20", 0.5)


async def test_runner_cancellation_stops_the_client(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    marker = f"sleep 4{uuid.uuid4().int % 1000}.5"
    task = asyncio.create_task(tools.runner.execute("lab", marker, timeout=60))
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    local = subprocess.run(["pgrep", "-f", f"ssh .*{marker}"], capture_output=True, text=True, check=False)
    assert local.stdout.strip() == ""


async def test_runner_output_cap_and_redaction(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    red = Redactor(["sup3r-literal-secret"])
    tools = build_tools(sshd, sessionmaker, tmp_path, max_output=1000, redactor=red)
    res = await tools.runner.execute("lab", "head -c 300000 /dev/zero | tr '\\000' a", timeout=20)
    assert res.exit_code == 0 and res.stdout_truncated and len(res.stdout) == 1000 and res.stdout_bytes == 300000
    res = await tools.runner.execute("lab", "echo password=hunter2hunter2; echo sup3r-literal-secret >&2", timeout=20)
    assert "hunter2hunter2" not in res.stdout and REDACTED in res.stdout
    assert "sup3r-literal-secret" not in res.stderr and REDACTED in res.stderr


async def test_runner_stdin_is_streamed(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    payload = os.urandom(200_000)
    res = await tools.runner.execute("lab", "sha256sum", timeout=20, stdin=payload)
    assert res.exit_code == 0 and res.stdout.split()[0] == hashlib.sha256(payload).hexdigest()


async def test_runner_refuses_unknown_and_unlisted_hosts(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    with pytest.raises(SshPolicyError) as unknown:
        await tools.runner.run("nope", "true", 5)
    assert unknown.value.code == "SSH_HOST_UNKNOWN"
    with pytest.raises(SshPolicyError) as unlisted:
        await tools.runner.run("not-listed", "true", 5)
    assert unlisted.value.code == "SSH_HOST_NOT_ALLOWED"
    with pytest.raises(SshPolicyError) as forbidden:
        await tools.runner.run("lab", "sudo rm -rf /", 5)
    assert forbidden.value.code in ("SSH_SUDO_NOT_ALLOWED", "SSH_COMMAND_FORBIDDEN")


async def test_runner_requires_pinned_host_key(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    other = tmp_path / "known_hosts_other"
    _keygen(tmp_path / "unrelated")
    pub = (tmp_path / "unrelated.pub").read_text().split()
    other.write_text(f"[127.0.0.1]:{sshd.port} {pub[0]} {pub[1]}\n", encoding="utf-8")
    empty = tmp_path / "known_hosts_empty_entry"
    empty.write_text(f"[127.0.0.9]:{sshd.port} {pub[0]} {pub[1]}\n", encoding="utf-8")
    wrong_key = sshd.host("wrongkey", known_hosts=other)
    no_entry = sshd.host("noentry", known_hosts=empty)
    tools = build_tools(sshd, sessionmaker, tmp_path, extra_hosts=(wrong_key, no_entry), allowed=("lab", "gated", "wrongkey", "noentry"))
    code, _out, err = await tools.runner.run("wrongkey", "true", 20)
    assert code == 255 and "host key" in err.lower()  # MITM protection: changed host key is refused
    with pytest.raises(SshConfigError) as exc:
        await tools.runner.run("noentry", "true", 20)
    assert exc.value.code == "SSH_KNOWN_HOSTS_NO_ENTRY"


async def test_runner_cred_key_is_materialised_and_removed(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    shutil.copy(sshd.client_key, secrets_dir / "ssh_admin_key")
    cred_host = sshd.host("credhost", key_ref="cred:ssh_admin_key")
    registry = HostRegistry([cred_host], ["credhost"])
    runtime = tmp_path / "rt"
    runner = SshRunner(registry, KeyManager(secret_store=SecretStore(env="development", secrets_dir=secrets_dir), runtime_dir=runtime))
    code, out, _ = await runner.run("credhost", "echo via-cred", 20)
    assert (code, out) == (0, "via-cred\n")
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
    assert list(runtime.iterdir()) == []  # the materialised key is deleted after the run


# ==================================================================================================== tools: read
async def test_ssh_read_runs_and_is_audited(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    job_id = await _job(sessionmaker)
    ctx = _ctx(job_id)
    res = await tools.ssh_read("lab", "ls -d / | head -n 1", ctx=ctx)
    assert res.outcome == SshOutcome.ok and res.classification == "read" and res.stdout == "/\n"
    runs = await _runs(sessionmaker, ctx.step_id)
    assert len(runs) == 1
    run = runs[0]
    assert (run.target, run.classification, run.exit_code, run.worker_id, run.network) == ("ssh", "read", 0, "lab", True)
    assert run.job_id == job_id and run.attempt_id == ctx.attempt_id and run.stdout_excerpt == "/\n" and run.duration_ms is not None
    events = await _events(sessionmaker, ctx.step_id)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["outcome"] == "ok" and payload["host"] == "lab" and payload["classification"] == "read"
    assert payload["command_run_id"] == str(run.id) and payload["exit_code"] == 0 and events[0].source_type == "ssh"
    assert "stdout" not in payload  # outputs live in command_runs excerpts, not in events


async def test_ssh_read_refuses_non_read_commands(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    ctx = _ctx(await _job(sessionmaker))
    target = tmp_path / "must-not-exist"
    for command in (f"touch {target}", f"ls > {target}", f"find {tmp_path} -exec touch {target} ;", "echo $(id)"):
        res = await tools.ssh_read("lab", command, ctx=ctx)
        assert res.outcome == SshOutcome.denied and res.code == "SSH_NOT_READ_ONLY", command
    assert not target.exists()
    assert await _runs(sessionmaker, ctx.step_id) == []  # nothing ran
    events = await _events(sessionmaker, ctx.step_id)
    assert [e.payload["outcome"] for e in events] == ["denied"] * 4


async def test_ssh_read_failed_command_records_exit_code(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    ctx = _ctx(await _job(sessionmaker))
    res = await tools.ssh_read("lab", "ls /definitely/not/here", ctx=ctx)
    assert res.outcome == SshOutcome.failed and res.exit_code == 2 and res.code == "SSH_COMMAND_FAILED"
    (run,) = await _runs(sessionmaker, ctx.step_id)
    assert run.exit_code == 2 and "No such file" in (run.stderr_excerpt or "")


# ==================================================================================================== tools: command + approvals
async def test_mutation_needs_approval_then_runs_once(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    job_id = await _job(sessionmaker)
    ctx = _ctx(job_id)
    target = tmp_path / "approved-file"
    command = f"touch {target}"
    first = await tools.ssh_command("lab", command, ctx=ctx)
    assert first.outcome == SshOutcome.approval_required and first.code == "SSH_APPROVAL_REQUIRED"
    assert first.approval_token == approval_token("lab", command, ctx.step_id) and not target.exists()
    rendered = first.render()
    assert f"pass {first.approval_token} as approval_token" in rendered and REDACTED not in Redactor().text(rendered)

    # a token for another command / host is not accepted
    bogus = approval_token("lab", f"touch {target}-other", ctx.step_id)
    wrong = await tools.ssh_command("lab", command, approval_token=bogus, ctx=ctx)
    assert wrong.outcome == SshOutcome.approval_required and wrong.code == "SSH_APPROVAL_INVALID"

    # right token, but no operator approval yet
    pending = await tools.ssh_command("lab", command, approval_token=first.approval_token, ctx=ctx)
    assert pending.code == "SSH_APPROVAL_PENDING" and not target.exists()

    async with sessionmaker() as s, s.begin():  # type: ignore[operator]
        assert await grant_approval(s, job_id, first.approval_token or "", approved_by="operator-anna")
    ok = await tools.ssh_command("lab", command, approval_token=first.approval_token, ctx=ctx)
    assert ok.outcome == SshOutcome.ok and target.exists() and ok.classification == "mutate"

    again = await tools.ssh_command("lab", command, approval_token=first.approval_token, ctx=ctx)
    assert again.outcome == SshOutcome.approval_required and again.code == "SSH_APPROVAL_PENDING"  # single use
    async with sessionmaker() as s:  # type: ignore[operator]
        assert await approval_state(s, job_id, first.approval_token or "") == "consumed"
        meta = (await s.get(Job, job_id)).metadata_  # type: ignore[union-attr]
    assert meta[APPROVALS_KEY] == [] and meta[CONSUMED_KEY][0]["approved_by"] == "operator-anna"

    runs = await _runs(sessionmaker, ctx.step_id)
    assert [(r.classification, r.exit_code) for r in runs] == [("mutate", 0)]
    events = await _events(sessionmaker, ctx.step_id)
    outcomes = [(e.payload["outcome"], e.payload.get("code")) for e in events]
    assert outcomes == [
        ("approval_required", "SSH_APPROVAL_REQUIRED"),
        ("approval_required", "SSH_APPROVAL_INVALID"),
        ("approval_required", "SSH_APPROVAL_PENDING"),
        ("ok", None),
        ("approval_required", "SSH_APPROVAL_PENDING"),
    ]
    assert events[0].payload["approval"] == first.approval_token and events[0].severity == "warning"
    assert events[3].payload["approved_by"] == "operator-anna"


async def test_reapproval_after_consumption_allows_another_run(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    job_id = await _job(sessionmaker)
    ctx = _ctx(job_id)
    log = tmp_path / "counter"
    command = f"tee -a {log} </dev/null"
    token = approval_token("lab", command, ctx.step_id)
    for _ in range(2):
        async with sessionmaker() as s, s.begin():  # type: ignore[operator]
            assert await grant_approval(s, job_id, token, approved_by="op")
        res = await tools.ssh_command("lab", command, approval_token=token, ctx=ctx)
        assert res.outcome == SshOutcome.ok
    assert len(await _runs(sessionmaker, ctx.step_id)) == 2


async def test_concurrent_use_of_one_approval_runs_once(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    job_id = await _job(sessionmaker)
    ctx = _ctx(job_id)
    log = tmp_path / "once.log"
    command = f"tee -a {log}"
    token = approval_token("lab", command, ctx.step_id)
    async with sessionmaker() as s, s.begin():  # type: ignore[operator]
        await grant_approval(s, job_id, token, approved_by="op")
    results = await asyncio.gather(*(tools.ssh_command("lab", command, approval_token=token, ctx=ctx) for _ in range(5)))
    assert sorted(r.outcome.value for r in results) == ["approval_required"] * 4 + ["ok"]
    assert len(await _runs(sessionmaker, ctx.step_id)) == 1


async def test_mutation_without_approval_policy_runs_directly(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path, require_approval=False)
    ctx = _ctx(await _job(sessionmaker))
    target = tmp_path / "direct"
    res = await tools.ssh_command("lab", f"touch {target}", ctx=ctx)
    assert res.outcome == SshOutcome.ok and target.exists()
    # destructive stays approval-bound even then
    res = await tools.ssh_command("lab", f"rm -rf {target}", ctx=ctx)
    assert res.outcome == SshOutcome.approval_required and target.exists()


async def test_forbidden_sudo_compound_and_hops_are_denied(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path, require_approval=False)
    ctx = _ctx(await _job(sessionmaker))
    marker = tmp_path / "never"
    cases = {
        "shutdown -h now": "SSH_COMMAND_FORBIDDEN",
        "sudo systemctl restart nginx": "SSH_SUDO_NOT_ALLOWED",
        f"touch {marker} && touch {marker}2": "SSH_COMPOUND_NOT_ALLOWED",
        f"touch {marker}; id": "SSH_COMPOUND_NOT_ALLOWED",
        f"sh -c 'touch {marker}'": "SSH_COMPOUND_NOT_ALLOWED",
        f"ssh other-host touch {marker}": "SSH_COMMAND_FORBIDDEN",
        "": "SSH_COMMAND_INVALID",
    }
    for command, code in cases.items():
        res = await tools.ssh_command("lab", command, ctx=ctx)
        assert res.outcome == SshOutcome.denied and res.code == code, (command, res.code)
    assert not marker.exists()
    assert await _runs(sessionmaker, ctx.step_id) == []


async def test_allowlisted_sudo_and_compound_commands(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    marker = tmp_path / "compound"
    compound = f"touch {marker} && echo done"
    tools = build_tools(sshd, sessionmaker, tmp_path, require_approval=False, compound=(rf"touch {re.escape(str(marker))} && echo done",))
    ctx = _ctx(await _job(sessionmaker))
    res = await tools.ssh_command("lab", compound, ctx=ctx)
    assert res.outcome == SshOutcome.ok and marker.exists() and res.data["compound"] is True
    if shutil.which("sudo") and os.geteuid() == 0:
        res = await tools.ssh_command("lab", "sudo -n /bin/true hermclaw-sudo-check", ctx=ctx)
        assert res.outcome == SshOutcome.ok and res.data["sudo"] is True
    res = await tools.ssh_command("lab", "sudo -n /bin/true other-args", ctx=ctx)
    assert res.outcome == SshOutcome.denied and res.code == "SSH_SUDO_NOT_ALLOWED"


async def test_capability_gate(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    for cap in ("implement", "database", None):
        ctx = SshCallContext(job_id=None, step_id=uuid.uuid4(), capability=cap)
        res = await tools.ssh_read("lab", "uptime", ctx=ctx)
        assert res.outcome == SshOutcome.denied and res.code == "SSH_CAPABILITY_DENIED"
        assert await _runs(sessionmaker, ctx.step_id) == []
    ok = await tools.ssh_read("lab", "uptime", ctx=_ctx(capability="deploy"))
    assert ok.outcome == SshOutcome.ok


async def test_unknown_host_is_denied_and_audited(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    ctx = _ctx(await _job(sessionmaker))
    res = await tools.ssh_read("not-listed", "uptime", ctx=ctx)
    assert res.outcome == SshOutcome.denied and res.code == "SSH_HOST_NOT_ALLOWED"
    res = await tools.ssh_read("evil\nhost", "uptime", ctx=ctx)
    assert res.code == "SSH_HOST_UNKNOWN" and "\n" not in res.host
    events = await _events(sessionmaker, ctx.step_id)
    assert [e.payload["code"] for e in events] == ["SSH_HOST_NOT_ALLOWED", "SSH_HOST_UNKNOWN"]


async def test_command_secrets_are_redacted_in_audit(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path, require_approval=False)
    ctx = _ctx(await _job(sessionmaker))
    target = tmp_path / "cfg"
    res = await tools.ssh_command("lab", f"echo password=s3cr3tvalue123 | tee {target}", ctx=ctx, timeout=20)
    # pipes into a mutating tool are chaining: refused, but the refusal audit must not leak the secret either
    assert res.outcome == SshOutcome.denied
    res = await tools.ssh_read("lab", "echo api_key=abcdef123456", ctx=ctx)
    assert res.outcome == SshOutcome.denied  # echo is not a read command on admin hosts
    res = await tools.ssh_command("lab", "echo api_key=abcdef123456", ctx=ctx)
    assert res.outcome == SshOutcome.ok and "abcdef123456" not in res.stdout and "abcdef123456" not in res.command
    (run,) = await _runs(sessionmaker, ctx.step_id)
    assert "abcdef123456" not in run.command and "abcdef123456" not in (run.stdout_excerpt or "")
    for event in await _events(sessionmaker, ctx.step_id):
        assert "s3cr3tvalue123" not in str(event.payload) and "abcdef123456" not in str(event.payload)


async def test_tool_timeout_is_recorded(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    ctx = _ctx(await _job(sessionmaker))
    res = await tools.ssh_read("lab", "tail -f /dev/null", ctx=ctx, timeout=1)
    assert res.outcome == SshOutcome.timed_out and res.code == "SSH_TIMEOUT" and res.timed_out
    (run,) = await _runs(sessionmaker, ctx.step_id)
    assert run.timed_out and run.exit_code is None
    bad = await tools.ssh_read("lab", "uptime", ctx=ctx, timeout=0)
    assert bad.outcome == SshOutcome.denied and bad.code == "SSH_COMMAND_INVALID"


async def test_cancelled_tool_call_completes_audit(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    ctx = _ctx(await _job(sessionmaker))
    task = asyncio.create_task(tools.ssh_read("lab", "tail -f /dev/null", ctx=ctx, timeout=60))
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)
    (run,) = await _runs(sessionmaker, ctx.step_id)
    assert "cancelled" in (run.stderr_excerpt or "")
    assert [e.payload["outcome"] for e in await _events(sessionmaker, ctx.step_id)] == ["failed"]


# ==================================================================================================== tools: upload
async def test_upload_atomic_with_mode_and_approval(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    job_id = await _job(sessionmaker)
    ctx = _ctx(job_id)
    remote = tmp_path / "remote" / "app.conf"
    remote.parent.mkdir()
    content = b"listen 8080;\npassword=top-secret-value\n" + os.urandom(64)
    first = await tools.ssh_upload("lab", content, str(remote), mode="0640", ctx=ctx)
    assert first.outcome == SshOutcome.approval_required and not remote.exists()
    async with sessionmaker() as s, s.begin():  # type: ignore[operator]
        await grant_approval(s, job_id, first.approval_token or "", approved_by="op")
    # the approval covers exactly this content: different bytes need a new approval
    other = await tools.ssh_upload("lab", content + b"x", str(remote), mode="0640", approval_token=first.approval_token, ctx=ctx)
    assert other.code == "SSH_APPROVAL_INVALID" and not remote.exists()
    ok = await tools.ssh_upload("lab", content, str(remote), mode="0640", approval_token=first.approval_token, ctx=ctx)
    assert ok.outcome == SshOutcome.ok, ok.render()
    assert remote.read_bytes() == content and stat.S_IMODE(remote.stat().st_mode) == 0o640
    assert [p.name for p in remote.parent.iterdir()] == ["app.conf"]  # no temp file left behind
    (run,) = await _runs(sessionmaker, ctx.step_id)
    assert run.classification == "mutate" and "app.conf" in run.command
    for event in await _events(sessionmaker, ctx.step_id):
        assert "top-secret-value" not in str(event.payload)
    assert (await _events(sessionmaker, ctx.step_id))[-1].payload["sha256"] == ok.data["sha256"]


async def test_upload_from_local_file_and_failures(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path, require_approval=False)
    ctx = _ctx(await _job(sessionmaker))
    src = tmp_path / "local.txt"
    src.write_bytes(b"hello upload\n")
    target_dir = tmp_path / "updir"
    target_dir.mkdir()
    ok = await tools.ssh_upload("lab", src, str(target_dir / "x.txt"), ctx=ctx)
    assert ok.outcome == SshOutcome.ok and (target_dir / "x.txt").read_bytes() == b"hello upload\n"
    assert stat.S_IMODE((target_dir / "x.txt").stat().st_mode) == 0o644
    # replacing an existing file is atomic and keeps the new content
    ok = await tools.ssh_upload("lab", b"v2\n", str(target_dir / "x.txt"), ctx=ctx)
    assert ok.outcome == SshOutcome.ok and (target_dir / "x.txt").read_bytes() == b"v2\n"
    is_dir = await tools.ssh_upload("lab", b"x", str(target_dir), ctx=ctx)
    assert is_dir.outcome == SshOutcome.failed and is_dir.code == "SSH_UPLOAD_INVALID" and target_dir.is_dir()
    missing = await tools.ssh_upload("lab", b"x", str(tmp_path / "no-such-dir" / "f"), ctx=ctx)
    assert missing.outcome == SshOutcome.failed
    for bad_path in ("relative/path", f"{target_dir}/../escape", f"{target_dir}/a b", f"{target_dir}/$(id)", "/", f"{target_dir}/"):
        res = await tools.ssh_upload("lab", b"x", bad_path, ctx=ctx)
        assert res.outcome == SshOutcome.denied and res.code == "SSH_UPLOAD_INVALID", bad_path
    for bad_mode in ("4755", "0999", "rwx", "06440"):
        res = await tools.ssh_upload("lab", b"x", str(target_dir / "m"), mode=bad_mode, ctx=ctx)
        assert res.code == "SSH_UPLOAD_INVALID", bad_mode
    link = tmp_path / "link.txt"
    link.symlink_to(src)
    res = await tools.ssh_upload("lab", link, str(target_dir / "l"), ctx=ctx)
    assert res.code == "SSH_UPLOAD_INVALID"  # final symlink is not followed
    small = build_tools(sshd, sessionmaker, tmp_path, require_approval=False)
    small.max_upload_bytes = 10
    res = await small.ssh_upload("lab", b"x" * 11, str(target_dir / "big"), ctx=ctx)
    assert res.code == "SSH_UPLOAD_INVALID" and not (target_dir / "big").exists()


# ==================================================================================================== tools: service status
async def test_service_status_states(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    ctx = _ctx(await _job(sessionmaker))
    active = await tools.ssh_service_status("lab", "nginx", ctx=ctx)
    assert active.outcome == SshOutcome.ok and active.data["active_state"] == "active" and active.data["active"] is True
    assert "Active: active (running)" in active.stdout and len(active.command_run_ids) == 2
    stopped = await tools.ssh_service_status("lab", "stopped", ctx=ctx)
    assert stopped.outcome == SshOutcome.ok and stopped.data["active_state"] == "inactive" and stopped.data["active"] is False
    unknown = await tools.ssh_service_status("lab", "nounit", ctx=ctx)
    assert unknown.outcome == SshOutcome.ok and unknown.data["active_state"] == "not-found"
    nosd = await tools.ssh_service_status("lab", "nosystemd", ctx=ctx)
    assert nosd.outcome == SshOutcome.failed and nosd.code == "SSH_SERVICE_UNAVAILABLE" and nosd.data["systemd_available"] is False
    bad = await tools.ssh_service_status("lab", "nginx; id", ctx=ctx)
    assert bad.outcome == SshOutcome.denied and bad.code == "SSH_UNIT_INVALID"
    runs = await _runs(sessionmaker, ctx.step_id)
    assert all(r.classification == "read" for r in runs) and len(runs) == 7  # 3 x 2 + 1 (no status call without systemd)


async def test_service_status_with_the_hosts_real_systemctl(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    """Unit 'realprobe' is passed to the real systemctl: in a container without systemd as PID 1 it is reported as
    unavailable (no crash); on a systemd host the state of an unknown unit is reported."""
    if not Path("/usr/bin/systemctl").exists():
        pytest.skip("no systemctl binary on this machine")
    tools = build_tools(sshd, sessionmaker, tmp_path)
    res = await tools.ssh_service_status("lab", "realprobe", ctx=_ctx(await _job(sessionmaker)))
    if Path("/run/systemd/system").is_dir():
        assert res.outcome == SshOutcome.ok and res.data["active_state"] in ("inactive", "not-found", "unknown")
    else:
        assert res.outcome == SshOutcome.failed and res.code == "SSH_SERVICE_UNAVAILABLE"
        assert res.data["active_state"] == "unavailable"


# ==================================================================================================== forced-command gate over ssh
async def test_gate_forced_command_over_real_ssh(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path, require_approval=False)
    code, out, _ = await tools.runner.run("gated", "echo gated-ok", 20)
    assert (code, out) == (0, "gated-ok\n")
    code, _out, err = await tools.runner.run("gated", "id", 20)
    assert code == 126 and "not allowed" in err
    code, _out, err = await tools.runner.run("gated", "echo gated-ok; id", 20)
    assert code == 126  # metacharacters need a 'shell:' rule
    # uploads stream through the gate's stdin (allow-listed with an explicit shell: rule)
    ctx = _ctx(await _job(sessionmaker))
    target = tmp_path / "gated-upload.txt"
    res = await tools.ssh_upload("gated", b"through the gate\n", str(target), ctx=ctx)
    assert res.outcome == SshOutcome.ok, res.render()
    assert target.read_bytes() == b"through the gate\n"
    log = sshd.syslog.read_text()
    assert "allowed" in log and "rejected" in log and "reason=no-matching-rule" in log


async def test_upload_to_security_critical_path_always_needs_approval(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    # even with require_approval_for_mutations off, replacing login keys is destructive (approval every time)
    tools = build_tools(sshd, sessionmaker, tmp_path, require_approval=False)
    job_id = await _job(sessionmaker)
    ctx = _ctx(job_id)
    keys = tmp_path / "victim" / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True)
    keys.write_text("ssh-ed25519 AAAAoriginal operator\n")
    attacker = b"ssh-ed25519 AAAAattacker evil\n"
    first = await tools.ssh_upload("lab", attacker, str(keys), mode="0600", ctx=ctx)
    assert first.outcome == SshOutcome.approval_required and first.classification == "destructive", first.render()
    assert first.data["critical_path"] is True and "login keys" in first.reason
    assert keys.read_text() == "ssh-ed25519 AAAAoriginal operator\n"
    assert await _runs(sessionmaker, ctx.step_id) == []  # nothing ran
    (event,) = await _events(sessionmaker, ctx.step_id)
    assert event.payload["outcome"] == "approval_required" and event.payload["critical_path"] is True
    async with sessionmaker() as s, s.begin():  # type: ignore[operator]
        await grant_approval(s, job_id, first.approval_token or "", approved_by="op")
    ok = await tools.ssh_upload("lab", attacker, str(keys), mode="0600", approval_token=first.approval_token, ctx=ctx)
    assert ok.outcome == SshOutcome.ok and keys.read_bytes() == attacker
    (run,) = await _runs(sessionmaker, ctx.step_id)
    assert run.classification == "destructive"
    # an ordinary target in the same session still runs without approval (policy flag honoured)
    plain = await tools.ssh_upload("lab", b"x\n", str(tmp_path / "victim" / "plain.txt"), ctx=ctx)
    assert plain.outcome == SshOutcome.ok and plain.classification == "mutate"


async def test_upload_refuses_world_writable_modes(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path, require_approval=False)
    ctx = _ctx(await _job(sessionmaker))
    for mode in ("0666", "0777", "0602", "4755", "abc"):
        target = tmp_path / f"ww-{mode}.txt"
        res = await tools.ssh_upload("lab", b"x", str(target), mode=mode, ctx=ctx)
        assert res.outcome == SshOutcome.denied and res.code == "SSH_UPLOAD_INVALID", mode
        assert not target.exists()
    ok = await tools.ssh_upload("lab", b"x", str(tmp_path / "ww-ok.txt"), mode="0775", ctx=ctx)
    assert ok.outcome == SshOutcome.ok and stat.S_IMODE((tmp_path / "ww-ok.txt").stat().st_mode) == 0o775
