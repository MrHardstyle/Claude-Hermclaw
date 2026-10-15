"""P26 26.1 host registry, 26.2 key handling, 26.8 separation checks."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from hermclaw.core.config import HostConfig, SshConfig, load_config
from hermclaw.core.errors import PolicyViolation
from hermclaw.security.secrets import SecretStore
from hermclaw.sshtools.errors import SshConfigError, SshPolicyError
from hermclaw.sshtools.keys import (
    KeyManager,
    authorized_keys_entry,
    check_key_file,
    check_known_hosts_file,
    generate_keypair,
    purge_stale_keys,
)
from hermclaw.sshtools.registry import HostRegistry, valid_address
from hermclaw.sshtools.separation import (
    assert_ssh_allowed,
    sandbox_isolation_issues,
    scrub_ssh_environment,
    ssh_allowed,
)

needs_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen missing")


def _host(host_id: str = "web", **ssh: object) -> HostConfig:
    return HostConfig(id=host_id, address="192.168.178.223", role="webui", ssh=SshConfig(**ssh))  # type: ignore[arg-type]


# ==================================================================================================== 26.1 registry
def test_registry_from_example_config() -> None:
    config = load_config()
    registry = HostRegistry.from_config(config)
    assert registry.ids() == config.policies.ssh.allowed_hosts
    host = registry.get("exec-222")
    assert host.destination == "hermclaw-admin@192.168.178.222"
    assert host.known_hosts == Path("/etc/hermclaw/ssh/known_hosts") and host.known_hosts_name == "192.168.178.222"
    assert "/usr/bin/systemctl suspend" in host.allow_sudo_commands
    # the GitLab host has an ssh block (Git over SSH) but is no SSH tool target
    gitlab = next(h for h in config.hosts.hosts if h.id == "gitlab-226")
    assert gitlab.ssh is not None and "gitlab-226" not in registry.ids() and not registry.is_allowed("gitlab-226")
    with pytest.raises(SshPolicyError) as unlisted:
        registry.get("gitlab-226")
    assert unlisted.value.code == "SSH_HOST_NOT_ALLOWED"


def test_unknown_and_unlisted_hosts_are_policy_violations() -> None:
    registry = HostRegistry([_host("web"), _host("db")], ["web"])
    with pytest.raises(PolicyViolation) as unknown:
        registry.get("nope")
    assert unknown.value.code == "SSH_HOST_UNKNOWN"
    with pytest.raises(SshPolicyError) as unlisted:
        registry.get("db")
    assert unlisted.value.code == "SSH_HOST_NOT_ALLOWED"
    assert registry.is_allowed("web") and not registry.is_allowed("db")
    with pytest.raises(SshPolicyError) as weird:
        registry.get("x\n" * 50)
    assert "\n" not in str(weird.value.details["host"]).replace("\\n", "")


def test_registry_validates_entries_at_startup() -> None:
    with pytest.raises(SshConfigError, match="unknown host"):
        HostRegistry([_host("web")], ["typo"])
    with pytest.raises(SshConfigError, match="no ssh section"):
        HostRegistry([HostConfig(id="web", address="10.0.0.1", role="webui")], ["web"])
    bad = HostConfig(id="web", address="-oProxyCommand=evil", role="webui", ssh=SshConfig())
    with pytest.raises(SshConfigError, match="invalid address"):
        HostRegistry([bad], ["web"])
    for kwargs, message in (
        ({"user": "-oX"}, "invalid ssh user"),
        ({"port": 0}, "invalid ssh port"),
        ({"key_ref": "relative/key"}, "key_ref"),
        ({"key_ref": "file:relative/key"}, "absolute"),
        ({"key_ref": "file:/etc/../root/key"}, "absolute"),
        ({"key_ref": "cred:../../etc/shadow"}, "plain name"),
        ({"known_hosts": "known_hosts"}, "known_hosts"),
        ({"known_hosts": ""}, "known_hosts"),
        # ssh splits UserKnownHostsFile on whitespace and expands %-tokens / '~': only plain paths are accepted
        ({"known_hosts": "/etc/hermclaw/known hosts"}, "known_hosts"),
        ({"known_hosts": "/etc/hermclaw/%u_known_hosts"}, "known_hosts"),
        ({"key_ref": "file:/etc/hermclaw/my key"}, "absolute"),
        ({"key_ref": "file:/etc/hermclaw/%d/key"}, "absolute"),
        ({"allow_sudo_commands": ["  "]}, "allow_sudo_commands"),
    ):
        with pytest.raises(SshConfigError, match=message):
            HostRegistry([_host("web", **kwargs)], ["web"])


def test_non_default_port_uses_bracketed_known_hosts_name() -> None:
    registry = HostRegistry([_host("web", port=2222)], ["web"])
    assert registry.get("web").known_hosts_name == "[192.168.178.223]:2222"


@pytest.mark.parametrize(
    ("address", "ok"), [("10.0.0.1", True), ("::1", True), ("host.lan", True), ("-o", False), ("a b", False), ("", False)]
)
def test_valid_address(address: str, ok: bool) -> None:
    assert valid_address(address) is ok


# ==================================================================================================== 26.2 keys
def test_key_file_permissions(tmp_path: Path) -> None:
    key = tmp_path / "id"
    key.write_text("x")
    for mode in (0o600, 0o400):
        os.chmod(key, mode)
        check_key_file(key)
    for mode in (0o640, 0o644, 0o700):
        os.chmod(key, mode)
        with pytest.raises(SshConfigError) as exc:
            check_key_file(key)
        assert exc.value.code == "SSH_KEY_PERMISSIONS"
    with pytest.raises(SshConfigError) as missing:
        check_key_file(tmp_path / "missing")
    assert missing.value.code == "SSH_KEY_MISSING"
    with pytest.raises(SshConfigError):
        check_key_file(tmp_path)


def test_known_hosts_checks(tmp_path: Path) -> None:
    kh = tmp_path / "known_hosts"
    with pytest.raises(SshConfigError) as missing:
        check_known_hosts_file(kh)
    assert missing.value.code == "SSH_KNOWN_HOSTS_MISSING"
    kh.write_text("")
    with pytest.raises(SshConfigError):
        check_known_hosts_file(kh)
    kh.write_text("host ssh-ed25519 AAAA\n")
    os.chmod(kh, 0o666)
    with pytest.raises(SshConfigError) as writable:
        check_known_hosts_file(kh)
    assert writable.value.code == "SSH_KNOWN_HOSTS_INSECURE"
    os.chmod(kh, 0o644)
    check_known_hosts_file(kh)


@needs_keygen
async def test_known_hosts_entry_lookup_incl_hashed(tmp_path: Path) -> None:
    pub = (await generate_keypair(tmp_path / "hk")).split()
    kh = tmp_path / "known_hosts"
    kh.write_text(f"[10.0.0.5]:2222 {pub[0]} {pub[1]}\n10.0.0.6 {pub[0]} {pub[1]}\n")
    subprocess.run(["ssh-keygen", "-q", "-H", "-f", str(kh)], check=True, capture_output=True)  # hash all entries
    os.chmod(kh, 0o644)
    assert "10.0.0.6" not in kh.read_text()
    registry = HostRegistry(
        [
            HostConfig(id="a", address="10.0.0.5", role="webui", ssh=SshConfig(port=2222, known_hosts=str(kh))),
            HostConfig(id="b", address="10.0.0.6", role="webui", ssh=SshConfig(known_hosts=str(kh))),
            HostConfig(id="c", address="10.0.0.7", role="webui", ssh=SshConfig(known_hosts=str(kh))),
        ],
        ["a", "b", "c"],
    )
    keys = KeyManager()
    await keys.check_known_hosts(registry.get("a"))
    await keys.check_known_hosts(registry.get("b"))
    with pytest.raises(SshConfigError) as exc:
        await keys.check_known_hosts(registry.get("c"))
    assert exc.value.code == "SSH_KNOWN_HOSTS_NO_ENTRY"


@needs_keygen
async def test_generate_keypair_and_authorized_keys_line(tmp_path: Path) -> None:
    key = tmp_path / "ssh" / "id_ed25519"
    pub = await generate_keypair(key, comment="hermclaw-admin@orchestrator")
    assert pub.startswith("ssh-ed25519 ") and pub.endswith("hermclaw-admin@orchestrator")
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    check_key_file(key)
    with pytest.raises(SshConfigError, match="overwrite"):
        await generate_keypair(key)
    with pytest.raises(SshConfigError):
        await generate_keypair(tmp_path / "other", comment="bad;comment")
    line = authorized_keys_entry(pub, from_addresses=["192.168.178.225"], forced_command="/usr/local/bin/hermclaw-ssh-gate")
    assert line.startswith('restrict,from="192.168.178.225",command="/usr/local/bin/hermclaw-ssh-gate" ssh-ed25519 ')
    assert authorized_keys_entry(pub, restrict=False) == pub
    with pytest.raises(SshConfigError):
        authorized_keys_entry(pub, forced_command='x" evil')
    with pytest.raises(SshConfigError):
        authorized_keys_entry(pub, from_addresses=['1.2.3.4" ,command="x'])
    with pytest.raises(SshConfigError):
        authorized_keys_entry("not a key")


@needs_keygen
async def test_cred_key_materialised_privately_and_removed(tmp_path: Path) -> None:
    await generate_keypair(tmp_path / "k")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    shutil.copy(tmp_path / "k", secrets / "admin_key")
    runtime = tmp_path / "run" / "hermclaw-ssh"
    keys = KeyManager(secret_store=SecretStore(env="development", secrets_dir=secrets), runtime_dir=runtime)
    keys.check_identity("cred:admin_key")
    with keys.identity("cred:admin_key") as path:
        assert path.parent == runtime and stat.S_IMODE(runtime.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.read_text().endswith("\n")
    assert not path.exists()
    with pytest.raises(RuntimeError), keys.identity("cred:admin_key") as path2:
        raise RuntimeError("boom")
    assert not path2.exists()  # removed on errors too
    (secrets / "not_a_key").write_text("not-a-private-key-value-93a1")  # unique: SecretStore registers it for redaction
    with pytest.raises(SshConfigError) as invalid:
        keys.check_identity("cred:not_a_key")
    assert invalid.value.code == "SSH_KEY_INVALID"
    with pytest.raises(SshConfigError) as missing:
        keys.check_identity("cred:absent")
    assert missing.value.code == "SSH_KEY_MISSING"


def test_runtime_dir_must_be_private(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(SshConfigError) as symlink:
        KeyManager(runtime_dir=link).private_runtime_dir()
    assert symlink.value.code == "SSH_RUNTIME_DIR_INSECURE"
    loose = tmp_path / "loose"
    loose.mkdir()
    os.chmod(loose, 0o755)
    with pytest.raises(SshConfigError):
        KeyManager(runtime_dir=loose).private_runtime_dir()


# ==================================================================================================== 26.8 separation
def test_capability_gate() -> None:
    for cap in ("ssh", "deploy"):
        assert ssh_allowed(cap)
        assert_ssh_allowed(cap)
    denied: tuple[str | None, ...] = ("implement", "database", "docker", "", None)
    for denied_cap in denied:
        with pytest.raises(SshPolicyError) as exc:
            assert_ssh_allowed(denied_cap)
        assert exc.value.code == "SSH_CAPABILITY_DENIED"


def test_sandbox_isolation_of_example_config(tmp_path: Path) -> None:
    config = load_config()
    assert sandbox_isolation_issues(config, workspaces_dir=tmp_path / "workspaces") == []


def test_sandbox_isolation_violations(tmp_path: Path) -> None:
    config = load_config()
    workspaces = tmp_path / "ws"
    leaky = config.model_copy(deep=True)
    leaky.policies.sandbox.env_allowlist.append("SSH_AUTH_SOCK")
    host = leaky.hosts.hosts[1]
    assert host.ssh is not None
    host.ssh.key_ref = f"file:{workspaces}/job/id_ed25519"
    leaky.capabilities.get("ssh").worker_kind = "execution"
    issues = sandbox_isolation_issues(leaky, workspaces_dir=workspaces, extra_paths=[workspaces / "runtime"])
    joined = "\n".join(issues)
    assert "SSH_AUTH_SOCK" in joined and "key of host" in joined and "runtime path" in joined and "execution worker" in joined


def test_scrub_ssh_environment() -> None:
    env = {"PATH": "/bin", "SSH_AUTH_SOCK": "/tmp/agent", "GIT_SSH_COMMAND": "ssh -i k", "SSH_CONNECTION": "x", "HOME": "/root"}
    assert scrub_ssh_environment(env) == {"PATH": "/bin", "HOME": "/root"}


def test_stale_materialised_keys_of_dead_processes_are_purged(tmp_path: Path) -> None:
    runtime = tmp_path / "rt"
    runtime.mkdir(mode=0o700)
    dead = subprocess.Popen(["true"])
    dead.wait()
    stale = runtime / f"key-{dead.pid}-abcdef"
    stale.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END OPENSSH PRIVATE KEY-----\n")
    own = runtime / f"key-{os.getpid()}-abcdef"  # in-flight key of this process: kept
    own.write_text("x")
    unrelated = runtime / "notes.txt"
    unrelated.write_text("x")
    assert KeyManager(runtime_dir=runtime).private_runtime_dir() == runtime
    assert not stale.exists() and own.exists() and unrelated.exists()
    assert purge_stale_keys(runtime) == 0


# ---------------------------------------------------------------------------------------------- upload helpers (26.5)
@pytest.mark.parametrize(
    ("remote_path", "critical"),
    [
        ("/home/admin/.ssh/authorized_keys", True),
        ("/root/.ssh/authorized_keys2", True),
        ("/srv/app/.ssh/config", True),
        ("/etc/ssh/sshd_config", True),
        ("/etc/ssh/sshd_config.d/10-hermclaw.conf", True),
        ("/etc/sudoers", True),
        ("/etc/sudoers.d/hermclaw", True),
        ("/etc/passwd", True),
        ("/etc/shadow", True),
        ("/etc/pam.d/sshd", True),
        ("/etc/cron.d/backup", True),
        ("/etc/ld.so.preload", True),
        ("/etc/hermclaw/ssh-allowlist", True),
        ("/usr/local/bin/hermclaw-ssh-gate", True),
        ("/boot/grub/grub.cfg", True),
        ("/srv/app/config.yaml", False),
        ("/etc/nginx/sites-available/app.conf", False),
        ("/etc/systemd/system/app.service", False),
        ("/etc/sudoers-notes.txt", False),
        ("/home/admin/ssh/notes", False),
    ],
)
def test_critical_remote_paths(remote_path: str, critical: bool) -> None:
    from hermclaw.sshtools.tools import critical_remote_path

    assert (critical_remote_path(remote_path) is not None) is critical


def test_upload_source_must_stay_inside_the_workspace_after_open(tmp_path: Path) -> None:
    from hermclaw.sshtools.errors import SshPolicyError
    from hermclaw.sshtools.tools import read_upload_source

    workspace = tmp_path / "ws"
    (workspace / "conf").mkdir(parents=True)
    (workspace / "conf" / "app.conf").write_bytes(b"inside\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.conf").write_bytes(b"secret outside\n")
    assert read_upload_source(workspace / "conf" / "app.conf", 1000, within=workspace) == b"inside\n"
    # a directory component swapped for a symlink after resolve(): O_NOFOLLOW only guards the last component
    (workspace / "conf" / "app.conf").unlink()
    (workspace / "conf").rmdir()
    (workspace / "conf").symlink_to(outside)
    with pytest.raises(SshPolicyError, match="outside the workspace"):
        read_upload_source(workspace / "conf" / "app.conf", 1000, within=workspace)
    assert read_upload_source(workspace / "conf" / "app.conf", 1000) == b"secret outside\n"  # no 'within': plain read
