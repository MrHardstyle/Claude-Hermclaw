"""P28.5 – real ``nginx -t`` in a private prefix (nginx installed in the build container, see docs/architecture/infratools.md)."""

from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path

import pytest

from hermclaw.infratools import InfraTools
from hermclaw.infratools.errors import INFRA_INVALID_ARGUMENT, INFRA_NGINX_INVALID, INFRA_NGINX_UNAVAILABLE, InfraToolError
from hermclaw.infratools.nginx import NginxTester
from hermclaw.infratools.process import HostExecutor
from tests.unit.test_infratools_nginx import render_repo_template

pytestmark = [pytest.mark.integration, pytest.mark.skipif(shutil.which("nginx") is None, reason="nginx not installed")]

SITE = """
upstream app { server 127.0.0.1:8000; }
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name example.lan;
    access_log /var/log/nginx/hermclaw-test-should-not-exist.log;
    location / { proxy_pass http://app; }
}
"""


@pytest.fixture
def tester(tmp_path: Path) -> NginxTester:
    return NginxTester(HostExecutor(), tmp_root=tmp_path)


async def test_valid_site_passes_and_leaves_nothing_behind(tester: NginxTester, tmp_path: Path) -> None:
    report = await tester.test(SITE)
    assert report.status == "passed", report.render()
    assert report.mode == "site" and report.nginx and report.nginx.startswith("nginx ")
    assert any("listen 80" in n or "80 → 127.0.0.1:" in n for n in report.notes)
    assert not os.path.exists("/var/log/nginx/hermclaw-test-should-not-exist.log")
    assert list(tmp_path.iterdir()) == []  # prefix removed


async def test_listen_on_foreign_address_and_busy_port_is_rewritten(tester: NginxTester) -> None:
    """nginx -t binds the listen sockets: an address of another host (EADDRNOTAVAIL) fails without the rewrite."""
    text = "server { listen 192.0.2.10:8084; listen [::]:8085; return 204; }"
    raw = await tester.test(text, rewrite=False)
    assert raw.status == "failed" and raw.reason and "192.0.2.10:8084" in raw.reason
    report = await tester.test(text)
    assert report.ok, report.render()
    assert sum("→ 127.0.0.1:" in n for n in report.notes) == 2
    with socket.socket() as busy:  # a busy port does not matter either
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        assert (await tester.test(f"server {{ listen 127.0.0.1:{busy.getsockname()[1]}; return 204; }}")).ok


async def test_invalid_directive_reports_line_of_the_original(tester: NginxTester) -> None:
    report = await tester.test("server {\n    listen 8081;\n    locatoin / { return 200; }\n}\n")
    assert report.status == "failed" and report.code == INFRA_NGINX_INVALID
    assert report.reason == 'unknown directive "locatoin"'
    emerg = next(m for m in report.messages if m.level == "emerg")
    assert emerg.file == "<config>" and emerg.line == 3
    assert "<prefix>" not in (report.reason or "") and "hermclaw-nginx-" not in report.render()


async def test_semantic_error_found_by_nginx(tester: NginxTester) -> None:
    text = "server { listen 8082 default_server; }\nserver { listen 8082 default_server; server_name b; }\n"
    report = await tester.test(text)
    assert report.status == "failed" and "duplicate default server" in (report.reason or "")
    assert "127.0.0.1:" not in (report.reason or "") or "8082" in (report.reason or "")  # rewritten socket mapped back


async def test_main_config_and_extra_files(tester: NginxTester, tmp_path: Path) -> None:
    main = (
        "worker_processes 1;\npid /run/nginx-hermclaw-test.pid;\nerror_log /var/log/nginx/hermclaw-main-test.err;\n"
        "events { worker_connections 16; }\n"
        "http { include mime.types; include snippets/site.conf; }\n"
    )
    report = await tester.test(main, extra_files={"snippets/site.conf": "server { listen 8083; return 204; }"})
    assert report.mode == "main" and report.ok, report.render()
    assert not os.path.exists("/run/nginx-hermclaw-test.pid") and not os.path.exists("/var/log/nginx/hermclaw-main-test.err")
    with pytest.raises(InfraToolError) as err:
        await tester.test(main, extra_files={"../escape.conf": "x"})
    assert err.value.code == INFRA_INVALID_ARGUMENT


async def test_path_input_and_repo_template(tester: NginxTester, tmp_path: Path) -> None:
    site = tmp_path / "site.conf"
    site.write_text(render_repo_template())
    report = await tester.test(path=site)
    assert report.ok, report.render()
    assert report.source == str(site)


async def test_missing_binary_is_skipped_with_reason() -> None:
    report = await NginxTester(HostExecutor(), binary="nginx-not-installed-xyz").test("server { listen 1; }")
    assert report.status == "skipped" and report.code == INFRA_NGINX_UNAVAILABLE and "not installed" in (report.reason or "")


async def test_facade_predeploy_gate_for_nginx(tmp_path: Path) -> None:
    tools = InfraTools(HostExecutor())
    tools.nginx.tmp_root = tmp_path
    report = await tools.predeploy_check(
        "nginx", render_repo_template().replace("192.168.178.225:8000", "127.0.0.1:8000"), owner="nginx", inventory=False
    )
    checks = {s["check"]: s for s in report.syntax}
    assert checks["nginx_test"]["status"] == "passed" and checks["proxy_config_validate"]["ok"]
    assert report.ports is not None and report.ports.live_checked
    bad = await tools.predeploy_check("nginx", "server { listen 80; location / { autoindex on; } }", live_ports=False, inventory=False)
    assert not bad.ok and "BLOCKED" in bad.render()
