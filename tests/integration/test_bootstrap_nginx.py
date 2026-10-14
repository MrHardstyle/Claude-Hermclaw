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
    assert "listen 80;" in without and "[::]" not in without
    (tmp_path / "v6").mkdir()
    facts = {"ansible_facts": {"all_ipv6_addresses": ["fd00::225"]}}
    with_v6 = _render_site(tmp_path / "v6", facts).read_text(encoding="utf-8")
    assert "listen 80;" in with_v6 and "listen [::]:80;" in with_v6
    (tmp_path / "forced").mkdir()
    forced = _render_site(tmp_path / "forced", {"nginx_listen_ipv6": False, **facts}).read_text(encoding="utf-8")
    assert "[::]" not in forced
    (tmp_path / "default-server").mkdir()
    default = _render_site(tmp_path / "default-server", {"nginx_default_server": True, **facts}).read_text(encoding="utf-8")
    assert "listen 80 default_server;" in default and "listen [::]:80 default_server;" in default


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
    resp = httpx.get(running_nginx + "/api/health?x=1", headers={"Host": "hermclaw.lan"}, timeout=5.0)
    assert resp.status_code == 200
    seen = resp.json()
    assert seen["path"] == "/api/health?x=1"
    headers = seen["headers"]
    assert headers["host"] == "hermclaw.lan"
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
