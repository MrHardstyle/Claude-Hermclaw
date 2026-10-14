"""SSH keys and host keys for the admin tools (P26 26.2, research 20261008-021 / -020).

* ``key_ref`` ``file:<path>`` – the key file is used in place after a permission check: regular file,
  mode ``0600`` or ``0400``, owned by the effective user (OpenSSH refuses looser keys too, but with a
  less helpful message).
* ``key_ref`` ``cred:<name>`` (systemd credential, see :mod:`hermclaw.security.secrets`) – and in
  development ``env:``/``literal:`` – the key is materialised into a fresh ``0600`` file inside a private
  ``0700`` runtime directory (``$RUNTIME_DIRECTORY`` → ``$XDG_RUNTIME_DIR`` → system temp dir) and deleted
  as soon as the ssh process ended. The key material is registered with the default redactor.
* ``known_hosts`` is mandatory (``StrictHostKeyChecking=yes``): it must exist, be a regular file that only
  its owner (root or the service user) can write, and list the target host.

The runtime never hands these files to the execution sandbox (26.8, see :mod:`hermclaw.sshtools.separation`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import stat
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

from hermclaw.core.errors import ConfigError
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.security.secrets import SecretStore
from hermclaw.sshtools.errors import (
    SSH_KEY_INVALID,
    SSH_KEY_MISSING,
    SSH_KEY_PERMISSIONS,
    SSH_KNOWN_HOSTS_INSECURE,
    SSH_KNOWN_HOSTS_MISSING,
    SSH_KNOWN_HOSTS_NO_ENTRY,
    SSH_RUNTIME_DIR_INSECURE,
    SshConfigError,
)
from hermclaw.sshtools.registry import SshHost

_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.+-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S)
_PUBLIC_KEY_RE = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]+( [^\n\"]*)?$"
)
_FROM_RE = re.compile(r"^!?[A-Za-z0-9.:*?/_-]+$")
_ALLOWED_KEY_MODES = (0o600, 0o400)
_MATERIALISED_RE = re.compile(r"^key-(\d+)-")  # key-<pid>-XXXXXXXX: the owner process of a materialised key


def default_runtime_base() -> Path:
    """Base directory for materialised keys: a tmpfs-backed runtime dir when available."""
    for var in ("RUNTIME_DIRECTORY", "XDG_RUNTIME_DIR"):
        value = os.environ.get(var, "").split(":", 1)[0]
        if value and Path(value).is_dir():
            return Path(value)
    return Path(tempfile.gettempdir())


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def purge_stale_keys(directory: Path) -> int:
    """Delete materialised keys left behind by processes that died (SIGKILL/OOM between write and unlink)."""
    removed = 0
    for entry in directory.iterdir():
        match = _MATERIALISED_RE.match(entry.name)
        if match is None:
            continue
        pid = int(match.group(1))
        if pid == os.getpid() or _pid_alive(pid):
            continue
        with contextlib.suppress(FileNotFoundError, PermissionError):
            entry.unlink()
            removed += 1
    return removed


def check_key_file(path: Path) -> None:
    """A usable private key file: regular, mode 0600/0400, owned by the effective user."""
    try:
        st = path.stat()
    except FileNotFoundError:
        raise SshConfigError(f"SSH key file {path} does not exist", code=SSH_KEY_MISSING) from None
    except OSError as exc:
        raise SshConfigError(f"SSH key file {path} is not accessible: {exc.strerror}", code=SSH_KEY_MISSING) from None
    if not stat.S_ISREG(st.st_mode):
        raise SshConfigError(f"SSH key {path} is not a regular file", code=SSH_KEY_PERMISSIONS)
    mode = stat.S_IMODE(st.st_mode)
    if mode not in _ALLOWED_KEY_MODES:
        raise SshConfigError(f"SSH key {path} has mode {mode:04o}; required 0600 or 0400", code=SSH_KEY_PERMISSIONS)
    if st.st_uid != os.geteuid():
        raise SshConfigError(f"SSH key {path} must be owned by uid {os.geteuid()} (is {st.st_uid})", code=SSH_KEY_PERMISSIONS)


def check_known_hosts_file(path: Path) -> None:
    """known_hosts must exist, be non-empty and writable only by its owner (root or the effective user)."""
    try:
        st = path.stat()
    except OSError:
        raise SshConfigError(
            f"known_hosts file {path} is missing; host keys must be pinned (StrictHostKeyChecking=yes)",
            code=SSH_KNOWN_HOSTS_MISSING,
        ) from None
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
        raise SshConfigError(f"known_hosts {path} must be a non-empty regular file", code=SSH_KNOWN_HOSTS_MISSING)
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SshConfigError(f"known_hosts {path} is group/world writable", code=SSH_KNOWN_HOSTS_INSECURE)
    if st.st_uid not in (0, os.geteuid()):
        raise SshConfigError(f"known_hosts {path} must be owned by root or uid {os.geteuid()}", code=SSH_KNOWN_HOSTS_INSECURE)


class KeyManager:
    """Resolves ``key_ref`` to a key file for exactly one ssh invocation and checks ``known_hosts``."""

    def __init__(
        self,
        *,
        secret_store: SecretStore | None = None,
        runtime_dir: Path | None = None,
        ssh_keygen: str = "ssh-keygen",
    ) -> None:
        self._secrets = secret_store
        self._runtime_dir = runtime_dir
        self._ssh_keygen = ssh_keygen
        self._known_cache: dict[tuple[str, str], tuple[int, int]] = {}
        self._lock = asyncio.Lock()
        self._purged = False

    # --------------------------------------------------------------------------------- runtime dir
    def private_runtime_dir(self) -> Path:
        """``<base>/hermclaw-ssh-<uid>`` with mode 0700, owned by us and not a symlink (pre-creation attack)."""
        target = self._runtime_dir or default_runtime_base() / f"hermclaw-ssh-{os.geteuid()}"
        if self._runtime_dir is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            target.mkdir(mode=0o700, parents=False)
        st = target.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise SshConfigError(f"SSH runtime dir {target} is not a real directory", code=SSH_RUNTIME_DIR_INSECURE)
        if st.st_uid != os.geteuid():
            raise SshConfigError(f"SSH runtime dir {target} is owned by uid {st.st_uid}", code=SSH_RUNTIME_DIR_INSECURE)
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise SshConfigError(f"SSH runtime dir {target} must have mode 0700", code=SSH_RUNTIME_DIR_INSECURE)
        if not self._purged:
            self._purged = True
            purge_stale_keys(target)
        return target

    # --------------------------------------------------------------------------------- private keys
    def _material(self, key_ref: str) -> str:
        store = self._secrets or SecretStore()
        try:
            material = store.resolve(key_ref)
        except ConfigError as exc:
            raise SshConfigError(f"SSH key reference could not be resolved: {exc.message}", code=SSH_KEY_MISSING) from None
        if material is None or not _PRIVATE_KEY_RE.search(material):
            raise SshConfigError("SSH key reference does not contain a PEM/OpenSSH private key", code=SSH_KEY_INVALID)
        DEFAULT_REDACTOR.add_literal(material)
        return material

    def check_identity(self, key_ref: str) -> None:
        """Validate ``key_ref`` without materialising it (pre-flight before an approval is consumed)."""
        kind, _, rest = key_ref.partition(":")
        if kind == "file":
            check_key_file(Path(rest))
        else:
            self._material(key_ref)

    @contextlib.contextmanager
    def identity(self, key_ref: str) -> Iterator[Path]:
        """Path of the private key for one ssh run; materialised secrets are deleted on exit."""
        kind, _, rest = key_ref.partition(":")
        if kind == "file":
            path = Path(rest)
            check_key_file(path)
            yield path
            return
        material = self._material(key_ref)
        directory = self.private_runtime_dir()
        fd, name = tempfile.mkstemp(prefix=f"key-{os.getpid()}-", dir=directory)  # O_EXCL, mode 0600
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(material.rstrip("\n").encode("utf-8") + b"\n")  # OpenSSH needs the final newline
            os.chmod(path, 0o600)
            yield path
        finally:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    # --------------------------------------------------------------------------------- host keys
    async def check_known_hosts(self, host: SshHost) -> None:
        """Fail early (with a stable code) if host keys are not pinned for ``host``."""
        path = host.known_hosts
        check_known_hosts_file(path)
        st = path.stat()
        key = (str(path), host.known_hosts_name)
        stamp = (st.st_mtime_ns, st.st_size)
        if self._known_cache.get(key) == stamp:
            return
        async with self._lock:
            if self._known_cache.get(key) == stamp:
                return
            if not await self._known_hosts_lists(path, host.known_hosts_name):
                raise SshConfigError(
                    f"known_hosts {path} has no entry for {host.known_hosts_name} (host '{host.id}'); "
                    "add the verified host key (ssh-keyscan + fingerprint check) first",
                    code=SSH_KNOWN_HOSTS_NO_ENTRY,
                )
            self._known_cache[key] = stamp

    async def _known_hosts_lists(self, path: Path, name: str) -> bool:
        """``ssh-keygen -F`` understands hashed entries, wildcards and markers exactly like ssh."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._ssh_keygen,
                "-F",
                name,
                "-f",
                str(path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return _plain_known_hosts_lists(path, name)
        return await proc.wait() == 0


def _plain_known_hosts_lists(path: Path, name: str) -> bool:
    """Fallback without ssh-keygen: exact (non-hashed) host patterns only."""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if fields[0].startswith("@"):
            fields = fields[1:]
        if fields and name in fields[0].split(","):
            return True
    return False


def _prepare_keypair_target(path: Path, comment: str) -> None:
    if os.path.lexists(path) or os.path.lexists(f"{path}.pub"):
        raise SshConfigError(f"refusing to overwrite existing key {path}", code=SSH_KEY_INVALID)
    if not re.fullmatch(r"[A-Za-z0-9@._ -]{1,100}", comment):
        raise SshConfigError("key comment may only contain letters, digits and @._- ", code=SSH_KEY_INVALID)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def _finish_keypair(path: Path) -> str:
    os.chmod(path, 0o600)
    return Path(f"{path}.pub").read_text(encoding="utf-8").strip()


async def generate_keypair(path: Path, *, comment: str = "hermclaw-admin", ssh_keygen: str = "ssh-keygen") -> str:
    """Create an ed25519 key pair (private ``path`` 0600, ``path.pub``); returns the public key line.

    Refuses to overwrite an existing key. Used by the installation docs/CLI to provision the admin key
    (``authorized_keys_entry`` turns the public key into the restricted line for the target host).
    """
    await asyncio.to_thread(_prepare_keypair_target, path, comment)
    proc = await asyncio.create_subprocess_exec(
        ssh_keygen,
        "-q",
        "-t",
        "ed25519",
        "-N",
        "",
        "-C",
        comment,
        "-f",
        str(path),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        raise SshConfigError(f"ssh-keygen failed: {err.decode(errors='replace').strip()[:300]}", code=SSH_KEY_INVALID)
    return await asyncio.to_thread(_finish_keypair, path)


def authorized_keys_entry(
    public_key: str,
    *,
    from_addresses: Iterable[str] = (),
    forced_command: str | None = None,
    restrict: bool = True,
) -> str:
    """``authorized_keys`` line for the target host (``restrict,from="…",command="…" <key>``)."""
    key = public_key.strip()
    if not _PUBLIC_KEY_RE.match(key):
        raise SshConfigError("not an OpenSSH public key line", code=SSH_KEY_INVALID)
    options: list[str] = ["restrict"] if restrict else []
    sources = list(from_addresses)
    for src in sources:
        if not _FROM_RE.match(src):
            raise SshConfigError(f"invalid from= pattern {src!r}", code=SSH_KEY_INVALID)
    if sources:
        options.append(f'from="{",".join(sources)}"')
    if forced_command is not None:
        if not forced_command.strip() or any(ch in forced_command for ch in ('"', "\\", "\n", "\r", "\x00")):
            raise SshConfigError("forced command must be a single line without quotes or backslashes", code=SSH_KEY_INVALID)
        options.append(f'command="{forced_command}"')
    return f"{','.join(options)} {key}" if options else key


__all__ = [
    "KeyManager",
    "authorized_keys_entry",
    "check_key_file",
    "check_known_hosts_file",
    "default_runtime_base",
    "generate_keypair",
    "purge_stale_keys",
]
