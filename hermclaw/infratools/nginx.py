"""Nginx deploy gate: ``nginx_test`` and ``proxy_config_validate`` (Bauplan §31, research 20261008-014, P28.5).

``nginx_test`` runs the real ``nginx -t`` against the configuration inside a private temporary prefix:

* a *site* snippet (server/upstream/map blocks – what lands in ``sites-available``) is included into a minimal
  ``nginx.conf`` wrapper (``events {}`` + ``http { include site.conf; }``) whose pid, error log and temp paths live in
  the prefix;
* a *main* config (has top-level ``events``/``http``) is tested as the main file; ``mime.types`` and extra snippet
  files can be placed next to it.

``nginx -t`` really binds the ``listen`` sockets (only ``EADDRINUSE`` is tolerated in test mode – a foreign address
fails with ``EADDRNOTAVAIL``, a privileged port without root with ``EACCES``) and opens/creates log, pid, temp and
cache paths. The tester therefore rewrites them for the test (:func:`rewrite_for_test`): listen sockets move to free
loopback ports (sockets shared by several servers stay shared), paths move into the prefix. Messages are mapped back
to the original file name and ``listen`` value.

``proxy_config_validate`` is a structural check over the parsed config (:mod:`hermclaw.infratools.nginxconf`):

* duplicate ``server_name``/``listen`` pairs and duplicate ``default_server`` per socket;
* every location proxying an event-stream endpoint (``/events/stream``, ``event-stream``, ``/sse``) must have
  ``proxy_buffering off``, ``proxy_http_version 1.1`` and ``proxy_read_timeout`` ≥ 60 s (inherited values from
  ``server``/``http`` count); ``Connection ""``, caching and gzip are reported as warnings;
* ``proxy_pass`` targets must be allowed: loopback, the internal addresses of ``hosts.yaml`` or an explicit
  allow-list; host names must resolve to allowed addresses, upstream blocks are checked server by server;
  request-controlled variables in the target host (open proxy/SSRF) are errors;
* no ``autoindex on``;
* TLS sanity for ``listen 443``/``ssl``: ``ssl`` flag, certificate and key present, no SSLv2/SSLv3, TLSv1/1.1 warned,
  weak cipher tokens warned.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import shutil
import socket
import tempfile
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from hermclaw.core.config import HostsConfig
from hermclaw.core.redaction import DEFAULT_REDACTOR, Redactor
from hermclaw.infratools.errors import (
    INFRA_INVALID_ARGUMENT,
    INFRA_NGINX_INVALID,
    INFRA_NGINX_UNAVAILABLE,
    INFRA_PATH_INVALID,
    Finding,
    InfraToolError,
    has_errors,
)
from hermclaw.infratools.nginxconf import (
    Directive,
    NginxConfig,
    NginxParseError,
    parse,
    parse_listen,
    parse_time,
    server_listens,
    server_names,
)
from hermclaw.infratools.process import HostExecutor, InfraContext

DEFAULT_SSE_PATTERNS = (r"events?/stream", r"event-?stream", r"(^|/)sse(/|$|\b)")
MIN_SSE_READ_TIMEOUT = 60.0
MAX_LOCATION_REGEX = 500  # location regexes come from the (model-written) config – bound the matching work
MAX_PROBE_PATH = 512
DEFAULT_NGINX_TIMEOUT = 30.0
MAX_EXTRA_FILES = 50
_MSG_RE = re.compile(r"\[(emerg|alert|crit|error|warn|notice|info)\]\s*(?:\d+#\d+:\s*)?(.*)$")
_LOC_RE = re.compile(r"\s+in\s+(\S+?):(\d+)\s*$")
_REQUEST_VARS = re.compile(r"\$(arg_|http_|cookie_|host\b|request_uri\b|uri\b|args\b|query_string\b|request\b|document_uri\b)")
Resolver = Callable[[str], Awaitable[list[str]]]


# ============================================================================================ nginx -t
@dataclass(frozen=True)
class NginxMessage:
    level: str
    message: str
    file: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class NginxTestReport:
    status: Literal["passed", "failed", "skipped"]
    mode: Literal["site", "main"]
    source: str
    nginx: str | None = None
    messages: list[NginxMessage] = field(default_factory=list)
    output: str = ""
    reason: str | None = None
    code: str | None = None
    host: str = "local"
    notes: list[str] = field(default_factory=list)  # test-only rewrites (listen sockets, log paths)

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "mode": self.mode,
            "source": self.source,
            "nginx": self.nginx,
            "host": self.host,
            "messages": [m.to_dict() for m in self.messages],
            "reason": self.reason,
            "code": self.code,
            "notes": self.notes,
        }

    def render(self, limit: int = 12000) -> str:
        lines = [f"nginx_test {self.source} ({self.mode}{', ' + self.nginx if self.nginx else ''}): {self.status.upper()}"]
        if self.reason:
            lines.append(f"reason: {self.reason}")
        for m in self.messages:
            where = f" ({m.file}:{m.line})" if m.file else ""
            lines.append(f"[{m.level}] {m.message}{where}")
        if self.status == "failed" and not self.messages and self.output:
            lines.append(self.output[-3000:])
        return "\n".join(lines)[:limit]


def parse_nginx_output(output: str, *, path_map: Mapping[str, str] | None = None) -> list[NginxMessage]:
    """``nginx: [emerg] unknown directive "x" in /p/conf/site.conf:3`` (or ``2026/10/14 … [emerg] 12#12: …``)."""
    messages: list[NginxMessage] = []
    mapping = dict(path_map or {})
    for raw in output.splitlines():
        m = _MSG_RE.search(raw)
        if not m:
            continue
        level, text = m.group(1), m.group(2).strip()
        file = line = None
        loc = _LOC_RE.search(text)
        if loc:
            file, line = loc.group(1), int(loc.group(2))
            text = text[: loc.start()].strip()
        for real, shown in mapping.items():
            text = text.replace(real, shown)
            if file is not None:
                file = file.replace(real, shown)
        messages.append(NginxMessage(level, text, file, line))
    return messages


def _wrapper(prefix: Path, site: Path, mime_types: Path | None) -> str:
    tmp = prefix / "tmp"
    mime = f"    include {mime_types};\n" if mime_types else "    types { text/html html; }\n"
    return (
        "worker_processes 1;\n"
        f"pid {prefix / 'logs' / 'nginx.pid'};\n"
        f"error_log {prefix / 'logs' / 'error.log'} warn;\n"
        "events { worker_connections 64; }\n"
        "http {\n"
        f"{mime}"
        "    access_log off;\n"
        f"    client_body_temp_path {tmp / 'client_body'};\n"
        f"    proxy_temp_path {tmp / 'proxy'};\n"
        f"    fastcgi_temp_path {tmp / 'fastcgi'};\n"
        f"    uwsgi_temp_path {tmp / 'uwsgi'};\n"
        f"    scgi_temp_path {tmp / 'scgi'};\n"
        f"    include {site};\n"
        "}\n"
    )


def _safe_rel(name: str) -> PurePosixPath:
    rel = PurePosixPath(name.replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or any(p in ("..", "") for p in rel.parts):
        raise InfraToolError(f"extra file name '{name}' must be a relative path inside the prefix", code=INFRA_INVALID_ARGUMENT)
    return rel


@dataclass(frozen=True)
class NginxRewrite:
    kind: Literal["listen", "path"]
    directive: str
    line: int
    original: str
    new: str

    def describe(self) -> str:
        return f"line {self.line}: {self.directive} {self.original} → {self.new} (test only)"


_LOG_DIRECTIVES = ("access_log", "error_log")
_PATH_DIRECTIVES = (
    "pid",
    "lock_file",
    "client_body_temp_path",
    "proxy_temp_path",
    "fastcgi_temp_path",
    "uwsgi_temp_path",
    "scgi_temp_path",
    "proxy_cache_path",
    "fastcgi_cache_path",
    "uwsgi_cache_path",
    "scgi_cache_path",
)


def _free_loopback_port(transport: str, taken: set[tuple[str, int]]) -> int:
    kind = socket.SOCK_DGRAM if transport == "udp" else socket.SOCK_STREAM
    for _ in range(50):
        with socket.socket(socket.AF_INET, kind) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if (transport, port) not in taken:
            taken.add((transport, port))
            return port
    raise InfraToolError("no free loopback port for the nginx test", code=INFRA_NGINX_UNAVAILABLE)


def rewrite_for_test(config: str, parsed: NginxConfig, prefix: Path) -> tuple[str, list[NginxRewrite]]:
    """The config with listen sockets and writable paths moved into the test sandbox (see :meth:`NginxTester.test`)."""
    edits: list[tuple[int, int, str]] = []
    rewrites: list[NginxRewrite] = []
    sockets: dict[tuple[str, int | None, str], str] = {}
    taken: set[tuple[str, int]] = set()
    counter = 0
    for d in parsed.walk():
        if len(d.tokens) < 2:
            continue
        arg = d.tokens[1]
        if d.name == "listen":
            try:
                lst = parse_listen(d)
            except NginxParseError:
                continue
            key = (lst.address, lst.port, lst.transport)
            if key not in sockets:
                if lst.is_unix:
                    counter += 1
                    sockets[key] = f"unix:{prefix / 'run' / f'sock-{counter}'}"
                else:
                    sockets[key] = f"127.0.0.1:{_free_loopback_port(lst.transport, taken)}"
            new = sockets[key]
            edits.append((arg.start, arg.end, new))
            for tok in d.tokens[2:]:
                if tok.value.startswith("ipv6only="):
                    edits.append((tok.start, tok.end, ""))
            rewrites.append(NginxRewrite("listen", "listen", d.line, arg.value, new))
            continue
        value = arg.value
        if d.name in _LOG_DIRECTIVES:
            if value in ("off", "stderr") or value.startswith(("syslog:", "memory:", "/dev/")) or "$" in value:
                continue
            counter += 1
            new = str(prefix / "logs" / f"{d.name}-{counter}.log")
        elif d.name in _PATH_DIRECTIVES:
            if "$" in value:
                continue
            counter += 1
            sub = "logs" if d.name in ("pid", "lock_file") else ("cache" if d.name.endswith("_cache_path") else "tmp")
            new = str(prefix / sub / f"{d.name}-{counter}")
        else:
            continue
        edits.append((arg.start, arg.end, new))
        rewrites.append(NginxRewrite("path", d.name, d.line, value, new))
    text = config
    for start, end, new in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:start] + new + text[end:]
    return text, rewrites


class NginxTester:
    def __init__(
        self,
        executor: HostExecutor,
        *,
        binary: str = "nginx",
        mime_types: Path | None = Path("/etc/nginx/mime.types"),
        timeout: float = DEFAULT_NGINX_TIMEOUT,
        redactor: Redactor = DEFAULT_REDACTOR,
        tmp_root: Path | None = None,
    ) -> None:
        self.executor = executor
        self.binary = binary
        self.mime_types = mime_types
        self.timeout = timeout
        self.redactor = redactor
        self.tmp_root = tmp_root

    def available(self) -> str | None:
        return shutil.which(self.binary)

    async def test(
        self,
        config: str | None = None,
        *,
        path: Path | None = None,
        mode: Literal["auto", "site", "main"] = "auto",
        extra_files: Mapping[str, str] | None = None,
        rewrite: bool = True,
        source: str | None = None,
        ctx: InfraContext | None = None,
    ) -> NginxTestReport:
        """Real ``nginx -t`` in a private prefix.

        ``nginx -t`` opens the configured listen sockets and log/temp/cache paths. With ``rewrite`` (default) every
        ``listen`` is moved to a free loopback port (``ipv6only`` dropped, unix sockets into the prefix) and file
        paths of ``access_log``/``error_log``/``pid``/``*_temp_path``/``*_cache_path``/``lock_file`` into the prefix –
        the test needs no root, does not collide with running services and leaves nothing behind. Messages that name
        a rewritten socket are mapped back to the original ``listen``.
        """
        if (config is None) == (path is None):
            raise InfraToolError("give exactly one of config text or path", code=INFRA_INVALID_ARGUMENT)
        source = source or (str(path) if path is not None else "<config>")
        if path is not None:
            try:
                config = await asyncio.to_thread(path.read_text, encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise InfraToolError(f"cannot read '{path}': {exc}", code=INFRA_PATH_INVALID) from None
        assert config is not None
        if extra_files and len(extra_files) > MAX_EXTRA_FILES:
            raise InfraToolError(f"at most {MAX_EXTRA_FILES} extra files", code=INFRA_INVALID_ARGUMENT)
        names = [_safe_rel(name) for name in (extra_files or {})]
        parsed: NginxConfig | None
        try:
            parsed = parse(config)
        except NginxParseError:
            parsed = None  # nginx -t reports the syntax error itself
        real_mode: Literal["site", "main"]
        if mode == "auto":
            if parsed is not None:
                real_mode = "main" if parsed.is_main else "site"
            else:
                real_mode = "main" if re.search(r"^\s*(events|http)\s*\{", config, re.M) else "site"
        else:
            real_mode = mode
        binary = self.available()
        if binary is None:
            return NginxTestReport(
                "skipped", real_mode, source, reason=f"'{self.binary}' is not installed on this machine", code=INFRA_NGINX_UNAVAILABLE
            )
        version = await self._version(binary, ctx)
        prefix = Path(
            await asyncio.to_thread(tempfile.mkdtemp, prefix="hermclaw-nginx-", dir=str(self.tmp_root) if self.tmp_root else None)
        )
        try:
            text, rewrites = (config, []) if parsed is None or not rewrite else rewrite_for_test(config, parsed, prefix)
            main, path_map = await asyncio.to_thread(
                self._prepare, prefix, text, real_mode, source, dict(zip(names, (extra_files or {}).values(), strict=True))
            )
            for rw in rewrites:
                if rw.kind == "listen":
                    path_map[rw.new] = rw.original
            path_map[str(prefix)] = "<prefix>"
            ordered = dict(sorted(path_map.items(), key=lambda kv: -len(kv[0])))
            res = await self.executor.run(
                [binary, "-t", "-q", "-p", f"{prefix}/", "-c", str(main), "-e", "stderr"], timeout=self.timeout, ctx=ctx
            )
            raw = self.redactor.text(res.stderr + res.stdout)
            output = raw
            for real, shown in ordered.items():
                output = output.replace(real, shown)
            messages = parse_nginx_output(raw, path_map=ordered)
            notes = [rw.describe() for rw in rewrites]
            if res.timed_out:
                return NginxTestReport(
                    "failed",
                    real_mode,
                    source,
                    version,
                    messages,
                    output,
                    reason="nginx -t timed out",
                    code=INFRA_NGINX_INVALID,
                    notes=notes,
                )
            if res.error:
                return NginxTestReport(
                    "skipped", real_mode, source, version, messages, output, reason=res.error, code=INFRA_NGINX_UNAVAILABLE, notes=notes
                )
            if res.exit_code == 0:
                return NginxTestReport("passed", real_mode, source, version, messages, output, notes=notes)
            first = next((m for m in messages if m.level in ("emerg", "alert", "crit", "error")), None)
            reason = first.message if first else "nginx -t failed"
            return NginxTestReport(
                "failed", real_mode, source, version, messages, output, reason=reason, code=INFRA_NGINX_INVALID, notes=notes
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, prefix, True)

    def _prepare(
        self, prefix: Path, config: str, mode: Literal["site", "main"], source: str, extra: Mapping[PurePosixPath, str]
    ) -> tuple[Path, dict[str, str]]:
        for sub in ("conf", "logs", "tmp", "cache", "run"):
            (prefix / sub).mkdir(mode=0o700)
        conf_dir = prefix / "conf"
        for rel, content in extra.items():
            target = conf_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        mime: Path | None = None
        if self.mime_types is not None and self.mime_types.is_file():
            mime = conf_dir / "mime.types"
            if not mime.exists():
                shutil.copyfile(self.mime_types, mime)
        main = conf_dir / "nginx.conf"
        if mode == "site":
            site = conf_dir / "site.conf"
            site.write_text(config, encoding="utf-8")
            main.write_text(_wrapper(prefix, site, mime), encoding="utf-8")
            return main, {str(site): source, str(main): "<wrapper nginx.conf>"}
        main.write_text(config, encoding="utf-8")
        return main, {str(main): source}

    async def _version(self, binary: str, ctx: InfraContext | None) -> str | None:
        res = await self.executor.run([binary, "-v"], timeout=10, ctx=ctx, audit=False)
        m = re.search(r"nginx/(\S+)", res.stderr + res.stdout)
        return f"nginx {m.group(1)}" if m else None

    async def test_host(
        self, host: str, *, binary: str = "/usr/sbin/nginx", sudo: bool = False, ctx: InfraContext | None = None
    ) -> NginxTestReport:
        """``nginx -t`` of the installed configuration on a registered host (deploy gate after an upload)."""
        argv = (["sudo", "-n"] if sudo else []) + [binary, "-t"]
        res = await self.executor.run(argv, host=host, timeout=self.timeout, ctx=ctx)
        output = self.redactor.text(res.stderr + res.stdout)
        messages = parse_nginx_output(output)
        if res.error or res.missing_binary:
            return NginxTestReport(
                "skipped",
                "main",
                f"{host}:nginx.conf",
                messages=messages,
                output=output,
                reason=res.error or f"{binary} not found on {host}",
                code=INFRA_NGINX_UNAVAILABLE,
                host=host,
            )
        status: Literal["passed", "failed"] = "passed" if res.ok else "failed"
        first = next((m for m in messages if m.level in ("emerg", "alert", "crit", "error")), None)
        return NginxTestReport(
            status,
            "main",
            f"{host}:nginx.conf",
            messages=messages,
            output=output,
            host=host,
            reason=None if res.ok else (first.message if first else res.describe(500)),
            code=None if res.ok else INFRA_NGINX_INVALID,
        )


# ============================================================================================ proxy validation
def _effective(node: Directive, name: str) -> Directive | None:
    """The directive as nginx inherits it: the nearest definition in this block or an enclosing one."""
    current: Directive | None = node
    while current is not None:
        found = current.first(name)
        if found is not None:
            return found
        current = current.parent
    return None


def _effective_headers(node: Directive) -> list[Directive]:
    """``proxy_set_header`` is inherited only when the current level defines none."""
    current: Directive | None = node
    while current is not None:
        headers = current.children("proxy_set_header")
        if headers:
            return headers
        current = current.parent
    return []


def _context(node: Directive) -> str:
    parts: list[str] = []
    for n in [node, *node.ancestors()]:
        if n.name == "location":
            parts.append(f"location {' '.join(n.args)}")
        elif n.name == "server":
            names = [a for d in n.children("server_name") for a in d.args]
            parts.append(f"server {names[0] if names else '_'}")
    return " / ".join(reversed(parts)) or "main"


@dataclass(frozen=True)
class UpstreamTarget:
    scheme: str
    host: str
    port: int | None
    unix: str | None = None
    variable: bool = False


def parse_proxy_pass(url: str) -> UpstreamTarget | None:
    m = re.match(r"^(https?|grpcs?)://(.*)$", url)
    if not m:
        return None
    scheme, rest = m.group(1), m.group(2)
    if rest.startswith("unix:"):
        return UpstreamTarget(scheme, "unix", None, unix=rest[5:].split(":", 1)[0])
    hostport = re.split(r"[/?]", rest, maxsplit=1)[0]
    variable = "$" in hostport
    if hostport.startswith("["):
        end = hostport.find("]")
        host = hostport[1:end] if end > 0 else hostport
        port_s = hostport[end + 2 :] if end > 0 and hostport[end + 1 : end + 2] == ":" else ""
    elif hostport.count(":") == 1:
        host, port_s = hostport.split(":", 1)
    else:
        host, port_s = hostport, ""
    port = int(port_s) if port_s.isdigit() else None
    return UpstreamTarget(scheme, host, port, variable=variable)


async def default_resolver(name: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(name, None, type=socket.SOCK_STREAM), 3)
    except (OSError, TimeoutError):
        return []
    return sorted({str(info[4][0]) for info in infos})


class UpstreamPolicy:
    """Where a proxy may forward: loopback + internal host addresses + explicit entries (IPs, CIDRs, names)."""

    def __init__(self, allowed: Iterable[str] = (), *, allow_loopback: bool = True) -> None:
        self.networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self.names: set[str] = set()
        self.allow_loopback = allow_loopback
        for item in allowed:
            text = item.strip()
            if not text:
                continue
            try:
                self.networks.append(ipaddress.ip_network(text, strict=False))
            except ValueError:
                self.names.add(text.lower())

    @classmethod
    def from_hosts(cls, hosts: HostsConfig | None, extra: Iterable[str] = (), *, allow_loopback: bool = True) -> UpstreamPolicy:
        addresses = [h.address for h in hosts.hosts] if hosts else []
        return cls([*addresses, *extra], allow_loopback=allow_loopback)

    def ip_allowed(self, value: str) -> bool:
        try:
            ip = ipaddress.ip_address(value.strip("[]").split("%", 1)[0])
        except ValueError:
            return False
        if ip.is_loopback:
            return self.allow_loopback
        return any(ip in net for net in self.networks)

    def describe(self) -> str:
        nets = ", ".join(str(n) for n in self.networks) or "-"
        return f"loopback{'' if self.allow_loopback else ' (denied)'}, {nets}" + (
            f", names: {', '.join(sorted(self.names))}" if self.names else ""
        )


@dataclass
class ProxyValidationReport:
    source: str
    findings: list[Finding]
    servers: int = 0
    locations: int = 0
    sse_locations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not has_errors(self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "servers": self.servers,
            "locations": self.locations,
            "sse_locations": self.sse_locations,
            "findings": [f.to_dict() for f in self.findings],
        }

    def render(self, limit: int = 12000) -> str:
        lines = [
            f"proxy_config_validate {self.source}: {'OK' if self.ok else 'FAILED'} – {self.servers} server(s), "
            f"{self.locations} location(s), SSE locations: {', '.join(self.sse_locations) or '-'}"
        ]
        lines.extend(f.render() for f in self.findings)
        return "\n".join(lines)[:limit]


_WEAK_CIPHERS = ("NULL", "EXPORT", "RC4", "DES", "3DES", "MD5", "ADH", "AECDH", "LOW", "EXP", "PSK", "SRP", "IDEA", "SEED")


class ProxyConfigValidator:
    def __init__(
        self,
        *,
        upstreams: UpstreamPolicy | None = None,
        resolver: Resolver | None = None,
        sse_patterns: Sequence[str] = DEFAULT_SSE_PATTERNS,
        min_sse_read_timeout: float = MIN_SSE_READ_TIMEOUT,
    ) -> None:
        self.upstreams = upstreams or UpstreamPolicy()
        self.resolver = resolver or default_resolver
        self.sse_patterns = [re.compile(p, re.I) for p in sse_patterns]
        self.min_sse_read_timeout = min_sse_read_timeout

    async def validate(self, config: str, *, source: str = "<config>", sse_paths: Sequence[str] = ()) -> ProxyValidationReport:
        try:
            cfg = parse(config)
        except NginxParseError as exc:
            return ProxyValidationReport(source, [Finding("error", "NGINX_SYNTAX", exc.message, line=exc.line)])
        findings: list[Finding] = []
        servers = cfg.servers()
        locations = [d for d in cfg.walk() if d.name == "location" and d.is_block]
        report = ProxyValidationReport(source, findings, len(servers), len(locations))
        self._check_listen_pairs(servers, findings)
        for loc in locations:
            if self._is_sse(loc) and loc.first("proxy_pass") is not None:
                report.sse_locations.append(" ".join(loc.args))
                self._check_sse(loc, findings)
        for probe in sse_paths:
            self._check_probe(servers, probe, findings)
        await self._check_proxy_pass(cfg, findings)
        for d in cfg.find("autoindex"):
            if d.args and d.args[0].lower() == "on":
                findings.append(
                    Finding("error", "NGINX_AUTOINDEX", "autoindex on exposes directory listings", location=_context(d), line=d.line)
                )
        for server in servers:
            self._check_tls(server, findings)
        for d in cfg.find("include"):
            findings.append(
                Finding(
                    "info",
                    "NGINX_INCLUDE_UNCHECKED",
                    f"included file '{' '.join(d.args)}' is not checked structurally",
                    location=_context(d),
                    line=d.line,
                )
            )
        return report

    # ------------------------------------------------------------------------------------------ rules
    @staticmethod
    def _check_listen_pairs(servers: list[Directive], findings: list[Finding]) -> None:
        pairs: dict[tuple[str, str], int] = {}
        defaults: dict[str, int] = {}
        for server in servers:
            try:
                listens = server_listens(server)
            except NginxParseError as exc:
                findings.append(Finding("error", "NGINX_LISTEN_INVALID", exc.message, line=exc.line))
                continue
            names = server_names(server)
            seen_here: set[str] = set()
            for lst in listens:
                key = lst.key()
                if key in seen_here:
                    findings.append(
                        Finding("warning", "NGINX_DUPLICATE_LISTEN", f"{key} is listed twice in the same server block", line=lst.line)
                    )
                    continue
                seen_here.add(key)
                if lst.default_server:
                    if key in defaults:
                        findings.append(
                            Finding(
                                "error",
                                "NGINX_DUPLICATE_DEFAULT",
                                f"a duplicate default server for {key} (first at line {defaults[key]})",
                                line=lst.line,
                            )
                        )
                    defaults.setdefault(key, lst.line)
                for name in dict.fromkeys(n.lower() for n in names):
                    pair = (key, name)
                    if pair in pairs:
                        shown = name or '""'
                        findings.append(
                            Finding(
                                "error",
                                "NGINX_DUPLICATE_SERVER",
                                f"server_name {shown} on {key} is already defined at line {pairs[pair]} (nginx ignores the second block)",
                                line=server.line,
                            )
                        )
                    else:
                        pairs[pair] = server.line

    def _is_sse(self, loc: Directive) -> bool:
        target = " ".join(loc.args)
        return any(p.search(target) for p in self.sse_patterns)

    def _check_sse(self, loc: Directive, findings: list[Finding]) -> None:
        where = _context(loc)
        buffering = _effective(loc, "proxy_buffering")
        if buffering is None or not buffering.args or buffering.args[0].lower() != "off":
            findings.append(
                Finding(
                    "error",
                    "SSE_BUFFERING",
                    "event-stream location must set 'proxy_buffering off' (events would be held back)",
                    location=where,
                    line=loc.line,
                )
            )
        version = _effective(loc, "proxy_http_version")
        if version is None or not version.args or version.args[0] != "1.1":
            findings.append(
                Finding(
                    "error", "SSE_HTTP_VERSION", "event-stream location must set 'proxy_http_version 1.1'", location=where, line=loc.line
                )
            )
        timeout = _effective(loc, "proxy_read_timeout")
        if timeout is None:
            findings.append(
                Finding(
                    "error",
                    "SSE_READ_TIMEOUT",
                    f"event-stream location must set proxy_read_timeout ≥ {self.min_sse_read_timeout:g}s "
                    "explicitly (e.g. 3600s; the 60s default drops quiet streams)",
                    location=where,
                    line=loc.line,
                )
            )
        else:
            seconds = parse_time(timeout.args[0]) if timeout.args else None
            if seconds is None or seconds < self.min_sse_read_timeout:
                findings.append(
                    Finding(
                        "error",
                        "SSE_READ_TIMEOUT",
                        f"proxy_read_timeout {' '.join(timeout.args)} is below {self.min_sse_read_timeout:g}s",
                        location=where,
                        line=timeout.line,
                    )
                )
        headers = _effective_headers(loc)
        conn = [h for h in headers if h.args and h.args[0].lower() == "connection"]
        if not conn or (len(conn[0].args) > 1 and conn[0].args[1] not in ("", '""')):
            findings.append(
                Finding(
                    "warning",
                    "SSE_CONNECTION_HEADER",
                    "set 'proxy_set_header Connection \"\"' (keep-alive to the upstream)",
                    location=where,
                    line=loc.line,
                )
            )
        cache = _effective(loc, "proxy_cache")
        if cache is not None and cache.args and cache.args[0].lower() != "off":
            findings.append(
                Finding(
                    "warning",
                    "SSE_CACHE",
                    "proxy_cache is active for an event stream – set 'proxy_cache off'",
                    location=where,
                    line=cache.line,
                )
            )
        gzip = _effective(loc, "gzip")
        if gzip is not None and gzip.args and gzip.args[0].lower() == "on":
            findings.append(
                Finding(
                    "warning", "SSE_GZIP", "gzip buffers the event stream – set 'gzip off' in this location", location=where, line=gzip.line
                )
            )
        chunked = _effective(loc, "chunked_transfer_encoding")
        if chunked is not None and chunked.args and chunked.args[0].lower() == "off":
            findings.append(
                Finding(
                    "warning", "SSE_CHUNKED", "chunked_transfer_encoding off breaks streaming responses", location=where, line=chunked.line
                )
            )

    def _check_probe(self, servers: list[Directive], path: str, findings: list[Finding]) -> None:
        for server in servers:
            if _redirects(server):
                continue  # e.g. the HTTP server of a TLS site: the client follows to the server that proxies
            loc = match_location(server, path)
            where = f"server {server_names(server)[0] or '_'}"
            if loc is not None and _redirects(loc):
                continue
            if loc is None:
                findings.append(Finding("warning", "SSE_PROBE_UNMATCHED", f"no location handles {path}", location=where, line=server.line))
                continue
            if loc.first("proxy_pass") is None:
                findings.append(
                    Finding(
                        "warning",
                        "SSE_PROBE_NOT_PROXIED",
                        f"{path} is served by '{loc.describe()}' without proxy_pass",
                        location=where,
                        line=loc.line,
                    )
                )
                continue
            before = len(findings)
            self._check_sse(loc, findings)
            for i in range(before, len(findings)):
                f = findings[i]
                findings[i] = Finding(f.severity, f.code, f"{path} → {loc.describe()}: {f.message}", f.location, f.line)

    async def _check_proxy_pass(self, cfg: NginxConfig, findings: list[Finding]) -> None:
        upstreams = cfg.upstreams()
        cache: dict[str, list[str]] = {}
        for d in cfg.find("proxy_pass"):
            if not d.args:
                continue
            where = _context(d)
            target = parse_proxy_pass(d.args[0])
            if target is None:
                findings.append(
                    Finding(
                        "error", "PROXY_PASS_INVALID", f"proxy_pass '{d.args[0]}' is not an http(s)/grpc URL", location=where, line=d.line
                    )
                )
                continue
            if target.unix is not None:
                continue
            if target.variable:
                if _REQUEST_VARS.search(target.host):
                    findings.append(
                        Finding(
                            "error",
                            "PROXY_PASS_OPEN_PROXY",
                            f"proxy_pass host '{target.host}' comes from the request (open proxy / SSRF)",
                            location=where,
                            line=d.line,
                        )
                    )
                else:
                    findings.append(
                        Finding(
                            "warning",
                            "PROXY_PASS_VARIABLE",
                            f"proxy_pass host '{target.host}' uses variables and cannot be checked",
                            location=where,
                            line=d.line,
                        )
                    )
                continue
            if target.host in upstreams:
                for srv in upstreams[target.host].children("server"):
                    if srv.args:
                        host = srv.args[0]
                        if host.startswith("unix:"):
                            continue
                        name = host.strip("[]") if host.startswith("[") else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
                        await self._check_host(name, f"upstream {target.host}", srv, findings, cache)
                continue
            await self._check_host(target.host, where, d, findings, cache)

    async def _check_host(self, host: str, where: str, d: Directive, findings: list[Finding], cache: dict[str, list[str]]) -> None:
        name = host.strip("[]")
        try:
            ipaddress.ip_address(name)
            addresses = [name]
        except ValueError:
            if name.lower() in self.upstreams.names:
                return
            if name not in cache:
                cache[name] = await self.resolver(name)
            addresses = cache[name]
            if not addresses:
                findings.append(
                    Finding("error", "PROXY_PASS_UNRESOLVABLE", f"upstream host '{name}' does not resolve", location=where, line=d.line)
                )
                return
        denied = [a for a in addresses if not self.upstreams.ip_allowed(a)]
        if denied:
            findings.append(
                Finding(
                    "error",
                    "PROXY_PASS_NOT_ALLOWED",
                    f"upstream '{name}' ({', '.join(denied)}) is not an allowed internal address (allowed: {self.upstreams.describe()})",
                    location=where,
                    line=d.line,
                )
            )

    @staticmethod
    def _check_tls(server: Directive, findings: list[Finding]) -> None:
        try:
            listens = server_listens(server)
        except NginxParseError:
            return
        where = _context(server)
        legacy_ssl = server.first("ssl")
        if legacy_ssl is not None:
            findings.append(
                Finding(
                    "warning",
                    "TLS_SSL_DIRECTIVE",
                    "the 'ssl on' directive is obsolete – use 'listen … ssl'",
                    location=where,
                    line=legacy_ssl.line,
                )
            )
        legacy_on = legacy_ssl is not None and bool(legacy_ssl.args) and legacy_ssl.args[0].lower() == "on"
        tls = [lst for lst in listens if lst.ssl or lst.transport == "udp" or legacy_on]
        for lst in listens:
            if lst.port == 443 and not lst.ssl and lst.transport == "tcp" and not legacy_on:
                findings.append(
                    Finding(
                        "error",
                        "TLS_LISTEN_WITHOUT_SSL",
                        f"'{lst.raw}' serves plain HTTP on 443 – add the ssl parameter",
                        location=where,
                        line=lst.line,
                    )
                )
        if not tls:
            return
        for name, code in (("ssl_certificate", "TLS_NO_CERTIFICATE"), ("ssl_certificate_key", "TLS_NO_KEY")):
            if _effective(server, name) is None:
                findings.append(Finding("error", code, f"TLS server without {name}", location=where, line=server.line))
        protocols = _effective(server, "ssl_protocols")
        if protocols is None:
            findings.append(
                Finding(
                    "warning",
                    "TLS_PROTOCOLS_DEFAULT",
                    "ssl_protocols not set – set 'ssl_protocols TLSv1.2 TLSv1.3' explicitly",
                    location=where,
                    line=server.line,
                )
            )
        else:
            for proto in protocols.args:
                if proto in ("SSLv2", "SSLv3"):
                    findings.append(
                        Finding(
                            "error",
                            "TLS_PROTOCOL_INSECURE",
                            f"{proto} is broken and must not be enabled",
                            location=where,
                            line=protocols.line,
                        )
                    )
                elif proto in ("TLSv1", "TLSv1.1"):
                    findings.append(
                        Finding(
                            "warning", "TLS_PROTOCOL_DEPRECATED", f"{proto} is deprecated (RFC 8996)", location=where, line=protocols.line
                        )
                    )
        ciphers = _effective(server, "ssl_ciphers")
        if ciphers is not None and ciphers.args:
            for token in re.split(r"[:,\s]+", ciphers.args[0]):
                if not token or token[0] in "!-":
                    continue
                upper = token.upper()
                if any(upper == w or upper.startswith(w + "-") or f"-{w}-" in f"-{upper}-" for w in _WEAK_CIPHERS):
                    findings.append(
                        Finding(
                            "warning",
                            "TLS_WEAK_CIPHER",
                            f"ssl_ciphers enables the weak suite/class '{token}'",
                            location=where,
                            line=ciphers.line,
                        )
                    )


# ============================================================================================ location matching
def _location_parts(loc: Directive) -> tuple[str, str]:
    args = loc.args
    if len(args) >= 2 and args[0] in ("=", "~", "~*", "^~"):
        return args[0], args[1]
    if args and args[0].startswith("@"):
        return "@", args[0]
    return "", args[0] if args else ""


_REDIRECT_CODES = {"301", "302", "303", "307", "308"}


def _redirects(block: Directive) -> bool:
    """True when ``block`` (a server or location) answers every request with a redirect (``return 301 …``)."""
    ret = block.first("return")
    return ret is not None and bool(ret.args) and ret.args[0] in _REDIRECT_CODES


def match_location(server: Directive, path: str) -> Directive | None:
    """nginx location selection: exact ``=``, longest prefix (``^~`` stops), then regexes in order, else the prefix."""
    return _match(server.children("location"), path)


def _match(locations: list[Directive], path: str) -> Directive | None:
    best_prefix: Directive | None = None
    best_len = -1
    stop = False
    for loc in locations:
        kind, value = _location_parts(loc)
        if kind == "=" and value == path:
            return loc
        if kind in ("", "^~") and path.startswith(value) and len(value) > best_len:
            best_prefix, best_len, stop = loc, len(value), kind == "^~"
    if best_prefix is not None:
        nested = _match(best_prefix.children("location"), path)
        if nested is not None:
            return nested
    if not stop:
        for loc in locations:
            kind, value = _location_parts(loc)
            if kind in ("~", "~*"):
                try:
                    if (
                        len(value) <= MAX_LOCATION_REGEX
                        and len(path) <= MAX_PROBE_PATH
                        and re.search(value, path, re.I if kind == "~*" else 0)
                    ):
                        return loc
                except re.error:
                    continue
    return best_prefix


__all__ = [
    "DEFAULT_SSE_PATTERNS",
    "NginxMessage",
    "NginxRewrite",
    "NginxTestReport",
    "NginxTester",
    "ProxyConfigValidator",
    "ProxyValidationReport",
    "UpstreamPolicy",
    "UpstreamTarget",
    "default_resolver",
    "match_location",
    "parse_nginx_output",
    "parse_proxy_pass",
    "rewrite_for_test",
]
