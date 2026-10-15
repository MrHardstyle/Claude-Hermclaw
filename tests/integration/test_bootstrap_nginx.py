"""P01 1.12 / 1.15 – the baseline nginx site of the ``nginx`` role under a real nginx.

The site template is rendered with Ansible's template engine (role defaults + group_vars, as
the role does) and loaded by a real nginx (official ``nginx`` image, Debian based, in a
throw-away Podman container) inside a Debian style ``nginx.conf`` that includes
``sites-enabled/*`` and enables gzip for event streams, like an operator might:

* ``nginx -t`` accepts the rendered site, and rejects a deliberately broken one (the check
  is not vacuous);
* the SSE locations (``/api/events/stream``, ``/api/jobs/<id>/events/stream``) deliver the
  first event while the upstream still holds the stream open (``proxy_buffering off``),
  uncompressed, whereas the generic ``/api/`` location buffers (negative control);
* API requests reach the upstream with ``Host`` / ``X-Forwarded-*`` and without
  ``Connection: close`` (upstream keepalive); unknown paths fall back to ``index.html``
  (SPA) with the security headers.

The upstream is a small HTTP server started by the test on 127.0.0.1; nginx runs with the
host network so it can reach it. Skipped only when Podman or the image is unavailable.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.integration.test_bootstrap_infra import ANSIBLE_PLAYBOOK, GROUP_VARS, ROLES_DIR, _ansible_env, _run

pytestmark = pytest.mark.integration

IMAGE = "docker.io/library/nginx:1.28"
TEMPLATE = ROLES_DIR / "nginx" / "templates" / "hermclaw-next.conf.j2"

# Debian's nginx.conf layout (sites-enabled include) plus gzip for event streams: the SSE
# locations must switch it off themselves.
NGINX_CONF = """user nginx;
worker_processes 1;
error_log /dev/stderr warn;
pid /tmp/nginx.pid;
events { worker_connections 128; }
http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    sendfile on;
    keepalive_timeout 65;
    gzip on;
    gzip_min_length 1;
    gzip_types text/event-stream application/json text/plain;
    include /etc/nginx/conf.d/*.conf;
    include /etc/nginx/sites-enabled/*;
}
"""

INDEX_HTML = "<!doctype html><title>Hermclaw Next</title><div id=root>hermclaw-spa</div>\n"


def _podman(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)


@pytest.fixture(scope="module")
def nginx_image() -> str:
    if shutil.which("podman") is None:
        pytest.skip("podman not available")
    if _podman("image", "exists", IMAGE).returncode != 0:
        for _ in range(3):
            if _podman("pull", "-q", IMAGE, timeout=300).returncode == 0:
                break
            time.sleep(5)
        else:
            pytest.skip(f"{IMAGE} cannot be pulled (registry unreachable or rate limited)")
    return IMAGE


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _render_site(tmp_path: Path, overrides: dict[str, Any]) -> Path:
    """Render the site exactly as the role does (group_vars + nginx role defaults)."""
    dest = tmp_path / "hermclaw-next.conf"
    play = [
        {
            "name": "render nginx site",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "become": False,
            "vars_files": [str(GROUP_VARS), str(ROLES_DIR / "nginx" / "defaults" / "main.yml")],
            "tasks": [{"name": "render", "ansible.builtin.template": {"src": str(TEMPLATE), "dest": str(dest), "mode": "0644"}}],
        }
    ]
    pb = tmp_path / "render-nginx.yml"
    pb.write_text(yaml.safe_dump(play), encoding="utf-8")
    extra = tmp_path / "render-nginx.json"
    extra.write_text(json.dumps({"hermclaw_orchestrator_ip": "192.168.178.225", **overrides}), encoding="utf-8")
    res = _run([str(ANSIBLE_PLAYBOOK), "-i", "localhost,", str(pb), "-e", f"@{extra}"], env=_ansible_env(tmp_path), cwd=tmp_path)
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-2000:]
    return dest


def _mounts(tmp_path: Path, site: Path) -> list[str]:
    conf = tmp_path / "nginx.conf"
    conf.write_text(NGINX_CONF, encoding="utf-8")
    conf.chmod(0o644)
    site.chmod(0o644)
    webroot = tmp_path / "webroot"
    (webroot / "assets").mkdir(parents=True, exist_ok=True)
    (webroot / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (webroot / "assets" / "app.js").write_text("console.log('hermclaw');\n", encoding="utf-8")
    for path in (webroot, webroot / "assets"):
        path.chmod(0o755)
    for path in (webroot / "index.html", webroot / "assets" / "app.js"):
        path.chmod(0o644)
    return [
        "-v", f"{conf}:/etc/nginx/nginx.conf:ro",
        "-v", f"{site}:/etc/nginx/sites-enabled/hermclaw-next.conf:ro",
        "-v", f"{webroot}:/var/www/hermclaw-next:ro",
    ]  # fmt: skip


def _nginx_test(tmp_path: Path, image: str, site: Path) -> subprocess.CompletedProcess[str]:
    return _podman("run", "--rm", "--network", "none", *_mounts(tmp_path, site), image, "nginx", "-t")


# --------------------------------------------------------------------------------------------- upstream


class _Upstream:
    """API stand-in: event streams that stay open until released, and a header echo."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.requests: list[dict[str, str]] = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _chunk(self, data: bytes) -> None:
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

            def do_GET(self) -> None:
                upstream.requests.append({"path": self.path, **{k.lower(): v for k, v in self.headers.items()}})
                if self.path.endswith("/stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    try:
                        self._chunk(b"event: job.created\ndata: first\n\n")
                        upstream.release.wait(timeout=15)
                        self._chunk(b"event: job.completed\ndata: second\n\n")
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        self.close_connection = True  # client (nginx) went away: expected in the negative control
                    return
                body = json.dumps({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", "upstream_session=1; Path=/")  # must never reach the browser
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _Upstream:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream() -> Iterator[_Upstream]:
    with _Upstream() as up:
        yield up


@pytest.fixture
def running_nginx(tmp_path: Path, nginx_image: str, upstream: _Upstream) -> Iterator[str]:
    port = _free_port()
    site = _render_site(
        tmp_path,
        {"hermclaw_web_server_name": "hermclaw.lan", "nginx_listen_port": port, "nginx_api_upstream": f"http://127.0.0.1:{upstream.port}"},
    )
    name = f"hcl-nginx-{uuid.uuid4().hex[:8]}"
    res = _podman(
        "run", "-d", "--rm", "--name", name, "--network", "host", *_mounts(tmp_path, site), nginx_image, "nginx", "-g", "daemon off;"
    )
    if res.returncode != 0 and "network" in res.stderr.lower():
        pytest.skip(f"podman host networking unavailable: {res.stderr.strip()[:200]}")
    assert res.returncode == 0, res.stderr
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                httpx.get(f"{base}/", headers={"Host": "hermclaw.lan"}, timeout=1.0)
                break
            except httpx.TransportError:
                if time.monotonic() > deadline:
                    logs = _podman("logs", name).stderr
                    pytest.fail(f"nginx did not start: {logs[-2000:]}")
                time.sleep(0.2)
        yield base
    finally:
        upstream.release.set()
        _podman("rm", "-f", "-t", "0", name)


# --------------------------------------------------------------------------------------------- tests


def test_nginx_accepts_the_rendered_site_and_rejects_a_broken_one(tmp_path: Path, nginx_image: str) -> None:
    site = _render_site(tmp_path, {"hermclaw_web_server_name": "hermclaw.lan"})
    text = site.read_text(encoding="utf-8")
    assert "server 192.168.178.225:8000;" in text and "server_name hermclaw.lan;" in text
    ok = _nginx_test(tmp_path, nginx_image, site)
    assert ok.returncode == 0, ok.stderr
    assert "syntax is ok" in ok.stderr and "test is successful" in ok.stderr

    broken = tmp_path / "broken" / "hermclaw-next.conf"
    broken.parent.mkdir()
    broken.write_text(text.replace("proxy_buffering off;", "proxy_buffering sometimes;"), encoding="utf-8")
    bad = _nginx_test(tmp_path / "broken", nginx_image, broken)
    assert bad.returncode != 0 and "proxy_buffering" in bad.stderr, bad.stderr


def test_ipv6_listener_follows_the_host(tmp_path: Path) -> None:
    """``listen [::]`` breaks nginx on kernels without IPv6 (found by the real nginx test above)."""
    without = _render_site(tmp_path, {}).read_text(encoding="utf-8")  # no facts = no IPv6 address
    assert "listen 8080;" in without and "[::]" not in without
    (tmp_path / "v6").mkdir()
    facts = {"ansible_facts": {"all_ipv6_addresses": ["fd00::225"]}}
    with_v6 = _render_site(tmp_path / "v6", facts).read_text(encoding="utf-8")
    assert "listen 8080;" in with_v6 and "listen [::]:8080;" in with_v6
    (tmp_path / "forced").mkdir()
    forced = _render_site(tmp_path / "forced", {"nginx_listen_ipv6": False, **facts}).read_text(encoding="utf-8")
    assert "[::]" not in forced
    (tmp_path / "default-server").mkdir()
    default = _render_site(tmp_path / "default-server", {"nginx_default_server": True, **facts}).read_text(encoding="utf-8")
    assert "listen 8080 default_server;" in default and "listen [::]:8080 default_server;" in default


def test_listen_ports_follow_the_web_host_ports(tmp_path: Path) -> None:
    """D-013: 8080/8443 on the shared .223 (group_vars), any port per host, 80/443 for a host of Hermclaw alone."""
    tls = {"nginx_tls_enabled": True, "hermclaw_web_server_name": "hermclaw.lan"}
    shared = _render_site(tmp_path, tls).read_text(encoding="utf-8")
    assert "listen 8443 ssl;" in shared and "listen 8080;" in shared
    assert "return 301 https://$host:8443$request_uri;" in shared
    assert not re.search(r"listen (\[::\]:)?(80|443)[ ;]", shared), "never the legacy portal's ports"
    assert "Shared host (D-013)" in shared
    (tmp_path / "custom").mkdir()
    custom = _render_site(tmp_path / "custom", {**tls, "hermclaw_webui_http_port": 9080, "hermclaw_webui_https_port": 9443})
    text = custom.read_text(encoding="utf-8")
    assert "listen 9443 ssl;" in text and "listen 9080;" in text and "https://$host:9443$request_uri" in text
    (tmp_path / "dedicated").mkdir()
    dedicated = _render_site(
        tmp_path / "dedicated",
        {**tls, "hermclaw_webui_shared_host": False, "hermclaw_webui_http_port": 80, "hermclaw_webui_https_port": 443},
    ).read_text(encoding="utf-8")
    assert "listen 443 ssl;" in dedicated and "listen 80;" in dedicated and "return 301 https://$host$request_uri;" in dedicated
    assert "Shared host" not in dedicated


@pytest.mark.parametrize("path", ["/api/events/stream", "/api/jobs/7f3c2a/events/stream"])
def test_sse_locations_stream_unbuffered_and_uncompressed(running_nginx: str, upstream: _Upstream, path: str) -> None:
    timeout = httpx.Timeout(10.0, read=5.0)
    with (
        httpx.Client(timeout=timeout) as client,
        client.stream("GET", running_nginx + path, headers={"Host": "hermclaw.lan", "Accept-Encoding": "gzip"}) as resp,
    ):
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert "content-encoding" not in resp.headers, "gzip off for event streams"
        chunks = resp.iter_raw()
        first = next(chunks)
        # the upstream is still blocked: the event can only be here because nginx does not buffer
        assert not upstream.release.is_set()
        assert b"data: first" in first
        upstream.release.set()
        rest = b"".join(chunks)
    assert b"data: second" in first + rest
    assert upstream.requests[-1]["path"] == path


def test_generic_api_location_buffers_streams(running_nginx: str, upstream: _Upstream) -> None:
    """Negative control: without the SSE location nginx holds the first event (and even the headers) back."""
    received: list[bytes] = []
    with (
        httpx.Client(timeout=httpx.Timeout(10.0, read=1.5)) as client,
        pytest.raises(httpx.ReadTimeout),
        client.stream("GET", running_nginx + "/api/other/stream", headers={"Host": "hermclaw.lan"}) as resp,
    ):
        received.extend(resp.iter_raw())
    assert not any(b"data: first" in chunk for chunk in received)
    assert upstream.requests and upstream.requests[-1]["path"] == "/api/other/stream", "the upstream did send the event"
    upstream.release.set()


def test_api_proxy_headers_and_keepalive(running_nginx: str, upstream: _Upstream) -> None:
    # the legacy portal's cookies (same host name, other port: browsers send them along) never reach the API
    portal_cookies = {"Cookie": "hermclaw_session=portal-secret; hermclaw_2fa_geraet=sel:val"}
    resp = httpx.get(running_nginx + "/api/health?x=1", headers={"Host": "hermclaw.lan", **portal_cookies}, timeout=5.0)
    assert resp.status_code == 200
    seen = resp.json()
    assert seen["path"] == "/api/health?x=1"
    headers = seen["headers"]
    # with the port: absolute redirects of the API stay on this server (8080 here, the portal owns 80)
    assert headers["host"] == f"hermclaw.lan:{running_nginx.rsplit(':', 1)[1]}"
    assert "cookie" not in headers and "set-cookie" not in resp.headers
    with (
        httpx.Client(timeout=httpx.Timeout(10.0, read=5.0)) as client,
        client.stream("GET", running_nginx + "/api/events/stream", headers={"Host": "hermclaw.lan", **portal_cookies}) as stream,
    ):
        assert stream.status_code == 200 and "set-cookie" not in stream.headers
        upstream.release.set()
    assert "cookie" not in upstream.requests[-1], "the SSE location drops the cookies too"
    assert headers["x-forwarded-proto"] == "http"
    assert headers["x-real-ip"] == "127.0.0.1" and headers["x-forwarded-for"] == "127.0.0.1"
    assert headers.get("connection", "").lower() != "close", "upstream keepalive (proxy_set_header Connection '')"
    assert "server" not in resp.headers or "nginx/" not in resp.headers["server"], "server_tokens off"


def test_spa_fallback_and_security_headers(running_nginx: str) -> None:
    host = {"Host": "hermclaw.lan"}
    deep = httpx.get(running_nginx + "/jobs/123/timeline", headers=host, timeout=5.0)
    assert deep.status_code == 200 and "hermclaw-spa" in deep.text
    assert deep.headers["x-frame-options"] == "DENY"
    assert deep.headers["x-content-type-options"] == "nosniff"
    assert deep.headers["referrer-policy"] == "same-origin"
    assert deep.headers["cache-control"] == "no-cache", "index.html must not be cached"
    asset = httpx.get(running_nginx + "/assets/app.js", headers=host, timeout=5.0)
    assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
    assert asset.headers["x-content-type-options"] == "nosniff"
    missing = httpx.get(running_nginx + "/assets/missing.js", headers=host, timeout=5.0)
    assert missing.status_code == 404, "assets never fall back to index.html"


# --------------------------------------------------------------------------------------------- shared web host (D-013)

# the legacy portal stand-in: another web server that owns port 80 of the host (Apache on .223)
FOREIGN_CONF = """pid /tmp/foreign.pid;
error_log /dev/stderr warn;
events { worker_connections 16; }
http { access_log off; server { listen 80; return 200 "legacy-portal\\n"; } }
"""
# what Debian's nginx package enables on a fresh install (sites-enabled/default -> sites-available/default), without
# its `listen [::]:80 default_server;` (the test container has no IPv6: nginx -t would fail on that line already)
DEBIAN_DEFAULT = """server {
\tlisten 80 default_server;
\troot /var/www/html;
\tserver_name _;
}
"""
SHARED_SCRIPT = r"""
get() { exec 3<>"/dev/tcp/127.0.0.1/$1"; printf 'GET %s HTTP/1.0\r\nHost: hermclaw.lan\r\n\r\n' "$2" >&3; cat <&3; exec 3<&-; }
nginx -c /etc/nginx/foreign.conf && echo FOREIGN-STARTED
get 80 / | tail -n 1
nginx -t 2>&1 && echo HERMCLAW-T-OK
nginx -T -q > /out/hermclaw.dump
nginx && echo HERMCLAW-STARTED
get 8080 /healthz | tail -n 1
get 80 / | tail -n 1
nginx -s quit; sleep 1
ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
nginx -t 2>&1 && echo DEFAULT-T-OK
nginx -T -q > /out/with-default.dump
nginx 2>&1 || echo DEFAULT-START-FAILED
"""


def test_shared_host_nginx_runs_next_to_a_foreign_server_on_port_80(tmp_path: Path, nginx_image: str) -> None:
    """A real nginx with the rendered site (8080) next to another web server on port 80, as on .223.

    It starts and serves while the other server keeps port 80. With Debian's default site enabled nginx cannot
    start at all - `nginx -t` still reports success (it tolerates the busy port), which is why the role disables
    that site and checks the listen directives of `nginx -T` itself (listeners.yml, run here on the real dumps).
    """
    from tests.integration.test_bootstrap_roles import Case, _fake_tool, _run_cases

    site = _render_site(tmp_path, {"hermclaw_web_server_name": "hermclaw.lan 192.168.178.223", "nginx_api_upstream": "http://127.0.0.1:9"})
    mounts = _mounts(tmp_path, site)
    (tmp_path / "foreign.conf").write_text(FOREIGN_CONF, encoding="utf-8")
    (tmp_path / "default").write_text(DEBIAN_DEFAULT, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    for path in (tmp_path / "foreign.conf", tmp_path / "default"):
        path.chmod(0o644)
    # sites-enabled must be writable inside the container (the default site gets linked there)
    mounts = [m.replace(":/etc/nginx/sites-enabled/hermclaw-next.conf:ro", ":/etc/nginx/hermclaw-next.conf:ro") for m in mounts]
    # the image's own conf.d/default.conf listens on 80 as well: gone, like the Debian default site on .223
    script = "set -u; rm -f /etc/nginx/conf.d/default.conf; mkdir -p /etc/nginx/sites-enabled /var/www/html; " + (
        "ln -s /etc/nginx/hermclaw-next.conf /etc/nginx/sites-enabled/hermclaw-next.conf; " + SHARED_SCRIPT
    )
    res = _podman(
        "run", "--rm", "--network", "none", *mounts,
        "-v", f"{tmp_path / 'foreign.conf'}:/etc/nginx/foreign.conf:ro",
        "-v", f"{tmp_path / 'default'}:/etc/nginx/sites-available/default:ro",
        "-v", f"{out}:/out",
        nginx_image, "bash", "-c", script,
    )  # fmt: skip
    log = res.stdout + res.stderr
    assert "FOREIGN-STARTED" in log and log.count("legacy-portal") == 2, log
    assert "HERMCLAW-T-OK" in log and "HERMCLAW-STARTED" in log, log
    assert "\nok\n" in log.replace("\r", ""), f"/healthz on 8080: {log}"
    # the busy port is invisible to nginx -t, but the start fails on it
    assert "DEFAULT-T-OK" in log and "DEFAULT-START-FAILED" in log, log
    assert "bind() to 0.0.0.0:80 failed (98" in log, log

    bindir = tmp_path / "bin"
    _fake_tool(bindir, "nginx", '[ "$1" = "-T" ] || exit 64\ncat "$FAKE_NGINX_DUMP"\n')
    cases: list[Case] = [
        ("real-dump-hermclaw", "nginx", "listeners.yml", {}, {"FAKE_NGINX_DUMP": str(out / "hermclaw.dump")}, "PASS"),
        ("real-dump-with-default", "nginx", "listeners.yml", {}, {"FAKE_NGINX_DUMP": str(out / "with-default.dump")}, "FAIL"),
    ]
    _run_cases(tmp_path, cases, bindir)
    assert "listen 8080;" in (out / "hermclaw.dump").read_text(encoding="utf-8")


def test_tokens_of_event_streams_never_reach_the_logs(tmp_path: Path, nginx_image: str) -> None:
    """The legacy portal's PHP runs as www-data on the same host: no log file may hold an ?access_token= (D-013)."""
    port, dead = _free_port(), _free_port()  # nothing listens on `dead`: every proxied request fails with 502
    site = _render_site(
        tmp_path, {"hermclaw_web_server_name": "hermclaw.lan", "nginx_listen_port": port, "nginx_api_upstream": f"http://127.0.0.1:{dead}"}
    )
    logs = tmp_path / "logs"
    logs.mkdir()
    logs.chmod(0o777)
    name = f"hcl-nginx-logs-{uuid.uuid4().hex[:8]}"
    res = _podman(
        "run", "-d", "--rm", "--name", name, "--network", "host", *_mounts(tmp_path, site), "-v", f"{logs}:/var/log/nginx",
        nginx_image, "nginx", "-g", "daemon off;",
    )  # fmt: skip
    if res.returncode != 0 and "network" in res.stderr.lower():
        pytest.skip(f"podman host networking unavailable: {res.stderr.strip()[:200]}")
    assert res.returncode == 0, res.stderr
    base, host = f"http://127.0.0.1:{port}", {"Host": "hermclaw.lan"}
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                httpx.get(f"{base}/healthz", headers=host, timeout=1.0)
                break
            except httpx.TransportError:
                if time.monotonic() > deadline:
                    pytest.fail(_podman("logs", name).stderr[-2000:])
                time.sleep(0.2)
        stream = httpx.get(f"{base}/api/events/stream?access_token=hct_STREAMSECRET", headers=host, timeout=10.0)
        job = httpx.get(f"{base}/api/jobs/7/events/stream?access_token=hct_JOBSECRET", headers=host, timeout=10.0)
        plain = httpx.get(f"{base}/api/health", headers=host, timeout=10.0)
        assert stream.status_code == job.status_code == plain.status_code == 502
        time.sleep(0.5)
    finally:
        _podman("rm", "-f", "-t", "0", name)
    text = "".join(f.read_text(encoding="utf-8", errors="replace") for f in logs.iterdir() if f.is_file())
    # negative control: the error log works and writes the request line (it would carry a query string)
    assert "connect() failed" in text and 'request: "GET /api/health HTTP/1.1"' in text, text[-2000:]
    assert "STREAMSECRET" not in text and "JOBSECRET" not in text and "access_token" not in text
