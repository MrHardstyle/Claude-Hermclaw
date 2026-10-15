"""P26 26.8 – scripts/ops/hermclaw-ssh-gate (authorized_keys forced command), run directly with SSH_ORIGINAL_COMMAND."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "hermclaw-ssh-gate"
SHELLS = [s for s in ("dash", "bash", "busybox") if shutil.which(s)]


@dataclass
class Gate:
    root: Path
    allowlist: Path
    syslog: Path
    shell: list[str]

    def run(self, command: str | None, *, stdin: bytes = b"", allowlist: Path | None = None) -> subprocess.CompletedProcess[bytes]:
        env = {"PATH": f"{self.root / 'bin'}:/usr/bin:/bin", "SSH_CLIENT": "192.168.178.225 50000 22"}
        if command is not None:
            env["SSH_ORIGINAL_COMMAND"] = command
        return subprocess.run(
            [*self.shell, str(GATE), str(allowlist or self.allowlist)], input=stdin, env=env, capture_output=True, timeout=20, check=False
        )

    def log(self) -> str:
        return self.syslog.read_text() if self.syslog.exists() else ""


@pytest.fixture(params=SHELLS or ["sh"])
def gate(request: pytest.FixtureRequest, tmp_path: Path) -> Gate:
    shell = ["busybox", "sh"] if request.param == "busybox" else [request.param]
    (tmp_path / "bin").mkdir()
    syslog = tmp_path / "syslog"
    logger = tmp_path / "bin" / "logger"
    logger.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{syslog}"\n')
    os.chmod(logger, 0o755)
    allowlist = tmp_path / "ssh-allowlist"
    allowlist.write_text(
        "# hermclaw ssh allow-list\n"
        "\n"
        "uptime\n"
        "systemctl (status|is-active) [A-Za-z0-9@._-]+\n"
        "cat /etc/(hostname|os-release)\n"
        "echo [a-z]+\n"
        "shell:sh -c '[^']*' hermclaw-upload .*\n"
        "invalid(regex\n"
        "exit [0-9]+"  # last line without newline
    )
    os.chmod(allowlist, 0o644)
    return Gate(tmp_path, allowlist, syslog, shell)


def test_allowed_command_runs_with_its_own_exit_code(gate: Gate) -> None:
    res = gate.run("echo hello")
    assert res.returncode == 0 and res.stdout == b"hello\n"
    assert gate.run("exit 3").returncode == 3  # last allow-list line (no trailing newline) is honoured
    assert "allowed" in gate.log() and "from=192.168.178.225" in gate.log() and "cmd=echo hello" in gate.log()


def test_rules_match_the_whole_command(gate: Gate) -> None:
    for command in ("echo hello world", "uptime -p", "cat /etc/shadow", "systemctl stop nginx", "xuptime"):
        res = gate.run(command)
        assert res.returncode == 126 and b"not allowed" in res.stderr, command
    assert "reason=no-matching-rule" in gate.log()


def test_metacharacters_need_a_shell_rule(gate: Gate, tmp_path: Path) -> None:
    flag = tmp_path / "pwned"
    for command in (f"echo hi; touch {flag}", f"echo hi && touch {flag}", f"echo $(touch {flag})", f"echo `touch {flag}`", "echo a | sh"):
        assert gate.run(command).returncode == 126, command
    assert not flag.exists()
    # explicit shell: rule – stdin reaches the executed command (uploads stream through the gate)
    target = tmp_path / "up.txt"
    res = gate.run(f"sh -c 'cat > \"$1\"' hermclaw-upload {target}", stdin=b"payload\n")
    assert res.returncode == 0, res.stderr
    assert target.read_bytes() == b"payload\n"


def test_no_command_multiline_and_control_characters_are_rejected(gate: Gate) -> None:
    assert gate.run(None).returncode == 126  # interactive login attempt
    assert gate.run("").returncode == 126
    assert gate.run("uptime\nid").returncode == 126
    assert gate.run("echo a\x1b[2J").returncode == 126
    log = gate.log()
    assert "reason=no-command" in log and "reason=multi-line" in log and "reason=control-characters" in log


def test_untrusted_allowlist_is_refused(gate: Gate, tmp_path: Path) -> None:
    os.chmod(gate.allowlist, 0o666)
    assert gate.run("uptime").returncode == 126
    assert "reason=allowlist-writable" in gate.log()
    os.chmod(gate.allowlist, 0o644)
    assert gate.run("uptime", allowlist=tmp_path / "missing").returncode == 126
    link = tmp_path / "link-allowlist"
    link.symlink_to(gate.allowlist)
    assert gate.run("uptime", allowlist=link).returncode == 126
    assert "reason=allowlist-missing" in gate.log()


def test_invalid_regex_is_logged_not_fatal(gate: Gate) -> None:
    assert gate.run("uptime").returncode == 0 or shutil.which("uptime") is None
    assert gate.run("nothing-matches").returncode == 126
    assert "invalid regular expression" in gate.log()


def test_gate_is_posix_sh_and_executable() -> None:
    assert os.access(GATE, os.X_OK)
    assert GATE.read_text().startswith("#!/bin/sh\n")
    assert subprocess.run(["sh", "-n", str(GATE)], check=False).returncode == 0


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_gate_passes_shellcheck_as_posix_sh() -> None:
    res = subprocess.run(["shellcheck", "-s", "sh", str(GATE)], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout


def test_meta_characters_in_quotes_still_need_a_shell_rule(gate: Gate, tmp_path: Path) -> None:
    # quoting is irrelevant for the gate: any metacharacter forces a 'shell:' rule (no parser on the host side)
    flag = tmp_path / "quoted"
    assert gate.run(f"echo 'a;b' && touch {flag}").returncode == 126
    assert gate.run("echo 'a;b'").returncode == 126  # 'echo [a-z]+' is a plain rule, the ';' needs a shell: rule
    assert not flag.exists()


def test_expansion_characters_need_a_shell_rule(gate: Gate, tmp_path: Path) -> None:
    # 'echo [a-z]+' means one word; brace expansion (bash as /bin/sh), globs and '~' would turn it into other words
    (tmp_path / "abc").write_text("x")
    for command in ("echo {a,b}", "echo a*", "echo a?c", "echo [ab]", "echo a]", "echo ~root"):
        res = gate.run(command)
        assert res.returncode == 126 and b"not allowed" in res.stderr, command
    assert gate.run("echo plain").stdout == b"plain\n"  # the plain rule itself still works
    allow = tmp_path / "shell-allowlist"
    allow.write_text("shell:echo \\{[a-z,]+\\}\n")
    os.chmod(allow, 0o644)
    res = gate.run("echo {a,b}", allowlist=allow)
    assert res.returncode == 0 and res.stdout in (b"{a,b}\n", b"a b\n")  # dash keeps the braces, bash expands them


# --------------------------------------------------------------------------------------------- deployed layout (BUG-142)

REPO = Path(__file__).resolve().parents[2]


def test_default_allowlist_is_readable_outside_the_config_dir() -> None:
    """The gate runs as hermclaw-admin; /etc/hermclaw is 0750 root:hermclaw, so the default allow-list lives outside."""
    import re

    import yaml

    default = re.search(r"^ALLOWLIST=\$\{1:-([^}]+)\}$", GATE.read_text(encoding="utf-8"), re.M)
    assert default is not None
    release = yaml.safe_load((REPO / "infra/ansible/roles/hermclaw_release/defaults/main.yml").read_text(encoding="utf-8"))
    common = yaml.safe_load((REPO / "infra/ansible/roles/common/defaults/main.yml").read_text(encoding="utf-8"))
    assert release["hermclaw_release_ssh_gate_allowlist_file"] == default.group(1) == "/etc/hermclaw-ssh-allowlist"
    group_vars = yaml.safe_load((REPO / "infra/ansible/playbooks/group_vars/all.yml").read_text(encoding="utf-8"))

    def resolve(path: str) -> str:
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(group_vars[m.group(1)]), path)

    config_dirs = [resolve(d["path"]) for d in common["common_directories"] if str(d.get("mode", "")).endswith(("750", "700"))]
    assert "/etc/hermclaw" in config_dirs, config_dirs
    assert not any(default.group(1).startswith(str(d).rstrip("/") + "/") for d in config_dirs), config_dirs
    task = (REPO / "infra/ansible/roles/hermclaw_release/tasks/scripts.yml").read_text(encoding="utf-8")
    block = task[task.index("Write the SSH gate allow-list") :]
    assert "owner: root" in block and "group: root" in block and 'mode: "0644"' in block


@pytest.mark.skipif(os.geteuid() != 0 or shutil.which("runuser") is None, reason="needs root and runuser")
def test_gate_reads_a_root_owned_allowlist_as_an_unprivileged_user() -> None:
    """Regression BUG-142: as a non-root user the gate reaches the allow-list in a traversable directory, and a list
    inside a 0750 root directory (the old /etc/hermclaw layout) is reported as missing, not silently used."""
    import tempfile

    try:
        subprocess.run(["id", "nobody"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        pytest.skip("no user nobody")
    root = Path(tempfile.mkdtemp(prefix="hermclaw-gate-", dir="/tmp"))
    try:
        os.chmod(root, 0o755)
        (root / "bin").mkdir(mode=0o755)
        gate = root / "bin" / "hermclaw-ssh-gate"
        shutil.copy(GATE, gate)
        os.chmod(gate, 0o755)
        syslog = root / "syslog"
        syslog.touch()
        os.chmod(syslog, 0o666)
        logger = root / "bin" / "logger"
        logger.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{syslog}"\n')
        os.chmod(logger, 0o755)
        readable = root / "hermclaw-ssh-allowlist"
        readable.write_text("uptime\n")
        os.chmod(readable, 0o644)
        closed = root / "hermclaw"
        closed.mkdir(mode=0o750)
        (closed / "ssh-allowlist").write_text("uptime\n")
        os.chmod(closed / "ssh-allowlist", 0o644)

        def run(allowlist: Path) -> subprocess.CompletedProcess[bytes]:
            env = {"PATH": f"{root / 'bin'}:/usr/bin:/bin", "SSH_CLIENT": "192.168.178.225 50000 22", "SSH_ORIGINAL_COMMAND": "uptime"}
            return subprocess.run(
                ["runuser", "-u", "nobody", "--", "/usr/bin/env", "-i", *[f"{k}={v}" for k, v in env.items()], "sh", str(gate), str(allowlist)],
                capture_output=True, timeout=20, check=False, cwd="/",
            )  # fmt: skip

        ok = run(readable)
        assert ok.returncode == 0 and b"load average" in ok.stdout, ok.stderr
        denied = run(closed / "ssh-allowlist")
        assert denied.returncode == 126 and "allowlist-missing" in syslog.read_text(), syslog.read_text()
    finally:
        shutil.rmtree(root, ignore_errors=True)
