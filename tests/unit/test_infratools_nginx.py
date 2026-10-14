"""P28.5 – nginx tokenizer/parser, proxy_config_validate rules (SSE, upstream allow-list, TLS, autoindex) and test rewrites."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hermclaw.core.config import HostConfig, HostsConfig
from hermclaw.infratools.errors import Finding
from hermclaw.infratools.nginx import (
    ProxyConfigValidator,
    UpstreamPolicy,
    match_location,
    parse_nginx_output,
    parse_proxy_pass,
    rewrite_for_test,
)
from hermclaw.infratools.nginxconf import NginxParseError, parse, parse_listen, parse_time, tokenize

ROOT = Path(__file__).resolve().parents[2]
HOSTS = HostsConfig(
    hosts=[
        HostConfig(id="orchestrator-225", address="192.168.178.225", role="orchestrator"),
        HostConfig(id="web-223", address="192.168.178.223", role="webui"),
    ]
)


async def fake_resolver(name: str) -> list[str]:
    return {"api.internal": ["192.168.178.225"], "evil.example": ["203.0.113.7"], "mixed.example": ["192.168.178.225", "8.8.8.8"]}.get(
        name, []
    )


def validator() -> ProxyConfigValidator:
    return ProxyConfigValidator(upstreams=UpstreamPolicy.from_hosts(HOSTS), resolver=fake_resolver)


def codes(findings: list[Finding]) -> dict[str, str]:
    return {f.code: f.severity for f in findings}


def render_repo_template() -> str:
    """infra/ansible/roles/nginx/templates/hermclaw-next.conf.j2 with the role defaults filled in."""
    tpl = (ROOT / "infra/ansible/roles/nginx/templates/hermclaw-next.conf.j2").read_text()
    tpl = tpl.replace("{{ nginx_api_upstream | regex_replace('^http://', '') }}", "192.168.178.225:8000")
    tpl = re.sub(r"\{\{ ' default_server' if nginx_default_server \| bool else '' \}\}", " default_server", tpl)
    tpl = re.sub(r"\{% if nginx_listen_ipv6 \| bool %\}\n(.*?)\{% endif %\}\n", r"\1", tpl, flags=re.S)
    values = {
        "nginx_listen_port": "80",
        "nginx_server_name": "hermclaw.lan",
        "hermclaw_webroot": "/var/www/hermclaw-next",
        "nginx_client_max_body_size": "10m",
        "nginx_site_name": "hermclaw-next",
        "nginx_sse_read_timeout": "3600s",
    }
    for key, value in values.items():
        tpl = tpl.replace("{{ " + key + " }}", value)
    return "\n".join(line for line in tpl.splitlines() if not line.lstrip().startswith("#"))


# ---------------------------------------------------------------------------------------------- tokenizer
def test_tokenizer_quotes_variables_comments_and_offsets() -> None:
    text = "add_header X-A \"a;b {c}\" always; # comment ; {\nset $x ${host}suffix;\nreturn 200 'it\\'s';"
    toks = tokenize(text)
    values = [t.value for t in toks]
    assert values == ["add_header", "X-A", "a;b {c}", "always", ";", "set", "$x", "${host}suffix", ";", "return", "200", "it's", ";"]
    quoted = toks[2]
    assert quoted.quoted and text[quoted.start : quoted.end] == '"a;b {c}"' and quoted.line == 1
    assert toks[7].line == 2


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("server {\n listen 80;\n", 1),
        ("server {\n listen 80\n}", 2),
        ("}\n", 1),
        ("a 'unterminated;\n", 1),
        ('server {\n return 200 "x"y;\n}', 2),
        (";", 1),
    ],
)
def test_parse_errors_carry_lines(text: str, line: int) -> None:
    with pytest.raises(NginxParseError) as err:
        parse(text)
    assert err.value.line == line


def test_parse_tree_and_listen() -> None:
    cfg = parse("http { server { listen [::]:443 ssl http2 default_server; listen 127.0.0.1:8080; listen 81; listen unix:/x.sock; } }")
    (server,) = cfg.servers()
    listens = [parse_listen(d) for d in server.children("listen")]
    assert (listens[0].address, listens[0].port, listens[0].ssl, listens[0].http2, listens[0].default_server) == (
        "::",
        443,
        True,
        True,
        True,
    )
    assert listens[0].key() == "[::]:443/tcp" and listens[1].key() == "127.0.0.1:8080/tcp"
    assert listens[2].address == "*" and listens[3].is_unix
    assert cfg.is_main and server.parent is not None and server.parent.name == "http"


@pytest.mark.parametrize(
    ("value", "seconds"), [("60", 60), ("60s", 60), ("1m30s", 90), ("500ms", 0.5), ("1h", 3600), ("x", None), ("", None)]
)
def test_parse_time(value: str, seconds: float | None) -> None:
    assert parse_time(value) == seconds


def test_parse_proxy_pass() -> None:
    t = parse_proxy_pass("http://127.0.0.1:8000/api/")
    assert t is not None and (t.host, t.port, t.variable) == ("127.0.0.1", 8000, False)
    t = parse_proxy_pass("https://[::1]:8443")
    assert t is not None and (t.host, t.port) == ("::1", 8443)
    t = parse_proxy_pass("http://unix:/run/app.sock:/x")
    assert t is not None and t.unix == "/run/app.sock"
    assert parse_proxy_pass("ftp://x") is None
    t = parse_proxy_pass("http://$backend")
    assert t is not None and t.variable


# ---------------------------------------------------------------------------------------------- SSE rules
GOOD_SSE = """
upstream hermclaw_api { server 192.168.178.225:8000; keepalive 16; }
server {
    listen 80;
    server_name hermclaw.lan;
    location ~ ^/api/(jobs/[^/]+/)?events/stream$ {
        proxy_pass http://hermclaw_api;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        gzip off;
        proxy_read_timeout 3600s;
    }
    location /api/ { proxy_pass http://hermclaw_api; }
}
"""


async def test_good_sse_config_passes() -> None:
    report = await validator().validate(GOOD_SSE, sse_paths=("/api/jobs/123/events/stream", "/api/events/stream"))
    assert report.ok, report.render()
    assert report.sse_locations == ["~ ^/api/(jobs/[^/]+/)?events/stream$"]
    assert not [f for f in report.findings if f.severity != "info"], report.render()


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("proxy_buffering off;", "SSE_BUFFERING"),
        ("proxy_http_version 1.1;", "SSE_HTTP_VERSION"),
        ("proxy_read_timeout 3600s;", "SSE_READ_TIMEOUT"),
    ],
)
async def test_missing_sse_settings_are_errors(change: str, code: str) -> None:
    report = await validator().validate(GOOD_SSE.replace(change, ""))
    assert codes(report.findings).get(code) == "error", report.render()
    assert not report.ok


async def test_short_read_timeout_and_warnings() -> None:
    text = GOOD_SSE.replace("proxy_read_timeout 3600s;", "proxy_read_timeout 30s;").replace("gzip off;", "gzip on;")
    text = text.replace('proxy_set_header Connection "";', "").replace("proxy_cache off;", "proxy_cache zone1;")
    found = codes((await validator().validate(text)).findings)
    assert found["SSE_READ_TIMEOUT"] == "error"
    assert found["SSE_GZIP"] == found["SSE_CONNECTION_HEADER"] == found["SSE_CACHE"] == "warning"


async def test_sse_settings_are_inherited_from_server_level() -> None:
    text = """
    server {
        listen 80;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 1h;
        proxy_set_header Connection "";
        location /events/stream { proxy_pass http://127.0.0.1:8000; }
    }
    """
    report = await validator().validate(text)
    assert report.ok and report.sse_locations == ["/events/stream"], report.render()


async def test_sse_probe_paths_find_the_matching_location() -> None:
    text = """
    server {
        listen 80;
        location /api/ { proxy_pass http://127.0.0.1:8000; }
        location = /health { return 200; }
    }
    """
    report = await validator().validate(text, sse_paths=("/api/jobs/1/events/stream", "/nothing"))
    msgs = [f for f in report.findings if f.code.startswith("SSE_")]
    assert any(f.code == "SSE_BUFFERING" and "/api/jobs/1/events/stream" in f.message for f in msgs)
    unmatched = [f for f in msgs if f.code == "SSE_PROBE_UNMATCHED"]
    assert len(unmatched) == 1 and "/nothing" in unmatched[0].message  # no "/" location – only the second probe is unmatched
    text2 = "server { listen 80; location /static/ { root /srv; } }"
    report2 = await validator().validate(text2, sse_paths=("/api/events/stream",))
    assert "SSE_PROBE_UNMATCHED" in codes(report2.findings)


def test_match_location_semantics() -> None:
    server = parse(
        """server {
        location / { }
        location /api/ { location /api/admin/ { } }
        location ^~ /static/ { }
        location ~* \\.(png|jpg)$ { }
        location = /exact { }
        location ~ ^/api/(jobs/[^/]+/)?events/stream$ { }
    }"""
    ).servers()[0]

    def target(path: str) -> str:
        loc = match_location(server, path)
        return " ".join(loc.args) if loc else "-"

    assert target("/exact") == "= /exact"
    assert target("/static/a.png") == "^~ /static/"  # ^~ stops the regex search
    assert target("/img/a.PNG") == "~* \\.(png|jpg)$"
    assert target("/api/jobs/7/events/stream") == "~ ^/api/(jobs/[^/]+/)?events/stream$"
    assert target("/api/admin/users") == "/api/admin/"
    assert target("/other") == "/"


# ---------------------------------------------------------------------------------------------- other rules
@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("server { listen 80; server_name a.lan; }\nserver { listen 80; server_name a.lan; }", "NGINX_DUPLICATE_SERVER"),
        ("server { listen 80 default_server; }\nserver { listen 80 default_server; server_name b; }", "NGINX_DUPLICATE_DEFAULT"),
        ("server { listen 80; location / { root /var/www; autoindex on; } }", "NGINX_AUTOINDEX"),
        ("server { listen 80; location / { proxy_pass http://203.0.113.9:8000; } }", "PROXY_PASS_NOT_ALLOWED"),
        ("server { listen 80; location / { proxy_pass http://evil.example; } }", "PROXY_PASS_NOT_ALLOWED"),
        ("server { listen 80; location / { proxy_pass http://mixed.example; } }", "PROXY_PASS_NOT_ALLOWED"),
        ("server { listen 80; location / { proxy_pass http://nowhere.invalid; } }", "PROXY_PASS_UNRESOLVABLE"),
        ("server { listen 80; location / { proxy_pass http://$http_host$request_uri; } }", "PROXY_PASS_OPEN_PROXY"),
        ("server { listen 80; location / { proxy_pass http://$arg_target; } }", "PROXY_PASS_OPEN_PROXY"),
        ("upstream u { server 198.51.100.1:80; }\nserver { listen 80; location / { proxy_pass http://u; } }", "PROXY_PASS_NOT_ALLOWED"),
        ("server { listen 80; location / { proxy_pass localhost:8000; } }", "PROXY_PASS_INVALID"),
        ("server { listen 443; server_name x; }", "TLS_LISTEN_WITHOUT_SSL"),
        ("server { listen 443 ssl; ssl_certificate_key /k; }", "TLS_NO_CERTIFICATE"),
        ("server { listen 443 ssl; ssl_certificate /c; }", "TLS_NO_KEY"),
        ("server { listen 443 ssl; ssl_certificate /c; ssl_certificate_key /k; ssl_protocols SSLv3 TLSv1.2; }", "TLS_PROTOCOL_INSECURE"),
        ("server { listen 80; listen 80 x; }", "NGINX_DUPLICATE_LISTEN"),
        ("server { listen 80; server_name a A; }", "NO_DUPLICATE_FOR_SAME_SERVER"),
        ("server { listen 80; location / {", "NGINX_SYNTAX"),
    ],
)
async def test_proxy_rules(text: str, code: str) -> None:
    report = await validator().validate(text)
    found = codes(report.findings)
    if code == "NGINX_DUPLICATE_LISTEN":  # a warning; the unknown flag 'x' is left to nginx -t
        assert found[code] == "warning" and report.ok
        return
    if code == "NO_DUPLICATE_FOR_SAME_SERVER":
        assert report.ok and "NGINX_DUPLICATE_SERVER" not in found, report.render()
        return
    assert found.get(code) == "error", report.render()
    assert not report.ok


async def test_tls_warnings_and_allowed_upstreams() -> None:
    text = """
    upstream api { server api.internal:8000; server 127.0.0.1:8001; server unix:/run/x.sock; }
    server {
        listen 443 ssl;
        ssl_certificate /etc/ssl/c.pem;
        ssl_certificate_key /etc/ssl/k.pem;
        ssl_protocols TLSv1.1 TLSv1.2;
        ssl_ciphers HIGH:!aNULL:RC4-SHA;
        location / { proxy_pass http://api; }
        location /local/ { proxy_pass http://[::1]:9000; }
        location /var/ { proxy_pass http://$upstream_name; }
    }
    """
    report = await validator().validate(text)
    found = codes(report.findings)
    assert report.ok, report.render()
    assert found["TLS_PROTOCOL_DEPRECATED"] == found["TLS_WEAK_CIPHER"] == found["PROXY_PASS_VARIABLE"] == "warning"
    denied = ProxyConfigValidator(upstreams=UpstreamPolicy([], allow_loopback=False), resolver=fake_resolver)
    assert "PROXY_PASS_NOT_ALLOWED" in codes((await denied.validate("server { location / { proxy_pass http://127.0.0.1:1; } }")).findings)
    named = ProxyConfigValidator(upstreams=UpstreamPolicy(["backend.lan"]), resolver=fake_resolver)
    assert (await named.validate("server { location / { proxy_pass http://backend.lan:80; } }")).ok


async def test_repo_nginx_template_passes_the_proxy_rules() -> None:
    report = await validator().validate(render_repo_template(), sse_paths=("/api/jobs/abc/events/stream", "/api/events/stream"))
    assert report.ok, report.render()
    assert report.sse_locations and not [f for f in report.findings if f.severity == "warning"], report.render()


# ---------------------------------------------------------------------------------------------- output / rewrite
def test_parse_nginx_output_maps_paths() -> None:
    out = (
        'nginx: [emerg] unknown directive "locatoin" in /tmp/p/conf/site.conf:3\n'
        '2026/10/14 10:00:00 [warn] 12#12: conflicting server name "a" on 0.0.0.0:80, ignored\n'
        "nginx: configuration file /tmp/p/conf/nginx.conf test failed\n"
    )
    msgs = parse_nginx_output(out, path_map={"/tmp/p/conf/site.conf": "<config>"})
    assert (
        msgs[0].level == "emerg" and msgs[0].file == "<config>" and msgs[0].line == 3 and msgs[0].message == 'unknown directive "locatoin"'
    )
    assert msgs[1].level == "warn" and "conflicting server name" in msgs[1].message


def test_rewrite_for_test_moves_sockets_and_paths(tmp_path: Path) -> None:
    text = (
        "server {\n  listen 80 default_server;\n  listen [::]:80 ipv6only=on default_server;\n  listen unix:/run/a.sock;\n"
        "  access_log /var/log/nginx/x.log combined;\n  error_log /var/log/nginx/x.err warn;\n  access_log off;\n"
        "  access_log syslog:server=127.0.0.1;\n  access_log /var/log/$host.log;\n}\n"
        "server {\n  listen 80;\n  server_name b;\n}\n"
        "proxy_cache_path /var/cache/nginx/c keys_zone=c:1m;\n"
    )
    new, rewrites = rewrite_for_test(text, parse(text), tmp_path)
    assert new.count("\n") == text.count("\n")  # line numbers stay valid for nginx messages
    listens = [r for r in rewrites if r.kind == "listen"]
    assert len(listens) == 4
    first, v6, unix, again = (r.new for r in listens)
    assert first == again  # both servers keep sharing one socket
    assert first != v6 and first.startswith("127.0.0.1:") and v6.startswith("127.0.0.1:")
    assert unix.startswith(f"unix:{tmp_path}/run/")
    assert "ipv6only" not in new and "/var/log/nginx" not in new and "/var/cache/nginx" not in new
    assert "access_log off;" in new and "syslog:server=127.0.0.1" in new and "/var/log/$host.log" in new
    assert {r.directive for r in rewrites if r.kind == "path"} == {"access_log", "error_log", "proxy_cache_path"}
    assert "default_server" in new and parse(new)  # still valid structure


def test_location_matching_bounds_regex_work() -> None:
    evil = "(a+)+" * 120 + "$"  # longer than the limit – skipped instead of backtracking for minutes
    server = parse(f"server {{ location ~ {evil} {{ }} location / {{ }} }}").servers()[0]
    loc = match_location(server, "/" + "a" * 40 + "b")
    assert loc is not None and loc.args == ["/"]
