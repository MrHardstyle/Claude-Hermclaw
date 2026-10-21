"""P01 1.15 – reproducible bootstrap test (static + rendering + script behaviour).

Everything here runs locally without the target hosts (BLOCKER-001):

* every YAML file parses, every playbook passes ``ansible-playbook --syntax-check``
* every Jinja2 template (roles/*/templates, infra/systemd) is rendered by Ansible itself
  with the example inventory values, and the rendered security properties are asserted
* rendered unit files pass ``systemd-analyze verify``; the Quadlet passes ``quadlet -dryrun``
* idempotency lint: command/shell/raw/script tasks need creates/removes or changed_when
* the shell scripts (check-host, bootstrap-control-node, hermclaw-credential,
  hermclaw-cred-env) are executed for real
* ansible-lint (production profile; dev extra – HERMCLAW_ANSIBLE_LINT, the venv or PATH)
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
ANSIBLE_DIR = REPO / "infra" / "ansible"
ROLES_DIR = ANSIBLE_DIR / "roles"
PLAYBOOKS_DIR = ANSIBLE_DIR / "playbooks"
SYSTEMD_DIR = REPO / "infra" / "systemd"
SCRIPTS_DIR = REPO / "scripts" / "bootstrap"
EXAMPLE_INVENTORY = ANSIBLE_DIR / "inventory" / "hosts.example.yml"
GROUP_VARS = PLAYBOOKS_DIR / "group_vars" / "all.yml"
VENV_BIN = Path(sys.executable).parent
ANSIBLE_PLAYBOOK = VENV_BIN / "ansible-playbook"

ROLES = ["common", "postgresql", "podman", "nodejs", "nginx", "nvidia", "ollama", "wol_target", "systemd_skeleton"]
WORKER_IDS = ["exec-222", "model-224"]
# syntactically valid base64 blob for known_hosts test lines (test data, not a real key)
KEY_B64 = "AAAAC3NzaC1lZDI1NTE5AAAAIHRlc3Qta2V5LWZvci1oZXJtY2xhdy1ib290c3RyYXA"

TASK_KEYWORDS = {
    "name",
    "when",
    "tags",
    "register",
    "loop",
    "loop_control",
    "with_items",
    "vars",
    "become",
    "become_user",
    "delegate_to",
    "run_once",
    "notify",
    "changed_when",
    "failed_when",
    "check_mode",
    "environment",
    "args",
    "no_log",
    "until",
    "retries",
    "delay",
    "block",
    "rescue",
    "always",
    "ignore_errors",
    "listen",
    "timeout",
    "diff",
    "any_errors_fatal",
    "throttle",
    "module_defaults",
    "collections",
    "debugger",
    "become_method",
}
COMMAND_MODULES = {"ansible.builtin.command", "ansible.builtin.shell", "ansible.builtin.raw", "ansible.builtin.script"}


# --------------------------------------------------------------------------------------------- helpers


def _ansible_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "ANSIBLE_CONFIG": str(ANSIBLE_DIR / "ansible.cfg"),
            "ANSIBLE_HOME": str(tmp_path / "ansible-home"),
            "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-tmp"),
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_FORCE_COLOR": "0",
            "PATH": f"{VENV_BIN}{os.pathsep}{env.get('PATH', '')}",
        }
    )
    return env


def _run(
    argv: list[str], *, env: Mapping[str, str] | None = None, cwd: Path = ANSIBLE_DIR, timeout: int = 300, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        input=input_text,
        stdin=subprocess.DEVNULL if input_text is None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _yaml_files() -> list[Path]:
    files = sorted(ANSIBLE_DIR.rglob("*.yml")) + sorted(ANSIBLE_DIR.rglob("*.yaml"))
    return [f for f in files if "inventory-results" not in f.parts]


def _task_files() -> list[Path]:
    files = sorted(ROLES_DIR.glob("*/tasks/*.yml")) + sorted(ROLES_DIR.glob("*/handlers/*.yml"))
    return files


def _iter_tasks(tasks: Any) -> Iterator[dict[str, Any]]:
    if not isinstance(tasks, list):
        return
    for task in tasks:
        if not isinstance(task, dict):
            continue
        yield task
        for section in ("block", "rescue", "always"):
            yield from _iter_tasks(task.get(section))


def _module_of(task: dict[str, Any]) -> str | None:
    if any(k in task for k in ("block", "rescue", "always")):
        return None
    modules = [k for k in task if k not in TASK_KEYWORDS]
    assert len(modules) == 1, f"task {task.get('name')!r} has no or several module keys: {modules}"
    return modules[0]


def _playbook_tasks() -> Iterator[tuple[Path, dict[str, Any]]]:
    for f in _task_files():
        for task in _iter_tasks(yaml.safe_load(f.read_text(encoding="utf-8"))):
            yield f, task
    for f in sorted(PLAYBOOKS_DIR.glob("*.yml")):
        for play in yaml.safe_load(f.read_text(encoding="utf-8")) or []:
            for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                for task in _iter_tasks(play.get(section)):
                    yield f, task


def _check_host_tables() -> tuple[set[tuple[str, int, str]], set[tuple[str, str]]]:
    text = (SCRIPTS_DIR / "check-host.sh").read_text(encoding="utf-8")
    port_block = re.search(r"PORT_PLAN=\$\(cat <<'EOF'\n(.*?)\nEOF", text, re.S)
    unit_block = re.search(r"UNIT_PLAN=\$\(cat <<'EOF'\n(.*?)\nEOF", text, re.S)
    assert port_block and unit_block
    ports = set()
    for line in port_block.group(1).splitlines():
        group, port, service, expect = line.split()
        ports.add((group, int(port), service))
        assert expect
    units = {tuple(line.split()) for line in unit_block.group(1).splitlines()}
    return ports, units  # type: ignore[return-value]


def _all_templates() -> list[Path]:
    return sorted(ROLES_DIR.glob("*/templates/*.j2")) + sorted(SYSTEMD_DIR.glob("*.j2"))


def _render(tmp_path: Path, out: Path, overrides: Mapping[str, Any]) -> dict[str, Path]:
    """Render every template with Ansible's own template engine (localhost, no become)."""
    out.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    rendered: dict[str, Path] = {}
    for tpl in _all_templates():
        role = tpl.parts[-3] if tpl.parent.name == "templates" else "systemd"
        stem = tpl.name[: -len(".j2")]
        if stem == "worker.env":
            for kind in ("execution", "model"):
                dest = out / role / f"worker-{kind}.env"
                items.append({"src": str(tpl), "dest": str(dest), "kind": kind})
                rendered[f"{role}/worker-{kind}.env"] = dest
            continue
        dest = out / role / stem
        items.append({"src": str(tpl), "dest": str(dest)})
        rendered[f"{role}/{stem}"] = dest
    for d in {Path(i["dest"]).parent for i in items}:
        d.mkdir(parents=True, exist_ok=True)
    playbook = [
        {
            "name": "render bootstrap templates",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "become": False,
            # + the deployment role whose template (the portal button, D-013) reads its own defaults
            "vars_files": [str(GROUP_VARS)] + [str(ROLES_DIR / r / "defaults" / "main.yml") for r in [*ROLES, "hermclaw_ui"]],
            "tasks": [
                {
                    "name": "render",
                    "ansible.builtin.template": {"src": "{{ item.src }}", "dest": "{{ item.dest }}", "mode": "0644"},
                    "vars": {"systemd_skeleton_worker_kind": "{{ item.kind | default('execution') }}"},
                    "loop": "{{ render_items }}",
                }
            ],
        }
    ]
    pb = tmp_path / f"render-{out.name}.yml"
    pb.write_text(yaml.safe_dump(playbook), encoding="utf-8")
    extra = tmp_path / f"render-{out.name}-vars.json"
    extra.write_text(json.dumps({**overrides, "render_items": items}), encoding="utf-8")
    res = _run(
        [str(ANSIBLE_PLAYBOOK), "-i", "localhost,", str(pb), "-e", f"@{extra}"],
        env=_ansible_env(tmp_path),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stdout[-4000:] + res.stderr[-2000:]
    for key, path in rendered.items():
        assert path.is_file(), f"{key} was not rendered"
    return rendered


def _render_vars(paths: Mapping[str, Any] | None = None) -> dict[str, Any]:
    inv = yaml.safe_load(EXAMPLE_INVENTORY.read_text(encoding="utf-8"))
    values: dict[str, Any] = dict(inv["all"]["vars"])
    values.pop("ansible_user", None)
    values.pop("ansible_become", None)
    values.update(
        {
            "hermclaw_web_server_name": "hermclaw.lan",
            "systemd_skeleton_worker_ids": WORKER_IDS,
            "systemd_skeleton_worker_token_refs": {w: f"cred:worker-token-{w}" for w in WORKER_IDS},
            "systemd_skeleton_hermclaw_uid": "998",
            "systemd_skeleton_worker_id": "exec-222",
            "systemd_skeleton_worker_bind": "192.168.178.222",
            "ollama_effective_home": "/var/lib/ollama",
            "ollama_effective_models_dir": "/var/lib/ollama/models",
            "ollama_llamacpp_model_path": "/var/lib/ollama/models/blobs/sha256-" + "0" * 64,
            "ollama_llamacpp_model_paths": {"qwen3.6:35b": "/var/lib/ollama/models/blobs/sha256-" + "0" * 64},
            "common_ssh_runtime_sudo_commands": ["/usr/bin/systemctl suspend"],
        }
    )
    if paths:
        values.update(paths)
    return values


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    tmp = tmp_path_factory.mktemp("render")
    return _render(tmp, tmp / "out", _render_vars())


def _systemd_env_value(line: str) -> str:
    """Value of KEY=VALUE as systemd's EnvironmentFile= parser sees it (quotes)."""
    _, _, raw = line.partition("=")
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    assert "'" not in raw and '"' not in raw, f"unquoted value with quotes would be mangled: {line}"
    return raw


# --------------------------------------------------------------------------------------------- YAML / syntax


def test_every_yaml_file_parses() -> None:
    files = _yaml_files()
    assert len(files) >= 30
    for f in files:
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        assert data is not None, f"{f} is empty"


@pytest.mark.parametrize("playbook", sorted(p.name for p in PLAYBOOKS_DIR.glob("*.yml")))
def test_playbook_syntax_check(playbook: str, tmp_path: Path) -> None:
    res = _run(
        [str(ANSIBLE_PLAYBOOK), "-i", str(EXAMPLE_INVENTORY), "--syntax-check", f"playbooks/{playbook}"],
        env=_ansible_env(tmp_path),
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_bootstrap_playbook_structure() -> None:
    plays = yaml.safe_load((PLAYBOOKS_DIR / "bootstrap.yml").read_text(encoding="utf-8"))
    by_hosts = {p["hosts"]: [r["role"] for r in p.get("roles", [])] for p in plays}
    assert by_hosts["orchestrator"] == ["common", "postgresql", "podman", "systemd_skeleton"]
    assert by_hosts["webui"] == ["common", "nodejs", "nginx"]
    assert by_hosts["execution_workers"] == ["common", "podman", "wol_target", "systemd_skeleton"]
    assert by_hosts["model_workers"] == ["common", "nvidia", "ollama", "wol_target", "systemd_skeleton"]
    # BUG-149: the compose sandbox of the container tools needs podman-compose on the execution worker only
    podman_vars = {
        p["hosts"]: next(r.get("vars", {}) for r in p["roles"] if r["role"] == "podman")
        for p in plays
        if p.get("roles") and any(r["role"] == "podman" for r in p["roles"])
    }
    assert podman_vars["execution_workers"].get("podman_extra_packages") == ["podman-compose"]
    assert "podman_extra_packages" not in podman_vars["orchestrator"]
    podman_main = (ROLES_DIR / "podman" / "tasks" / "main.yml").read_text(encoding="utf-8")
    assert '"{{ podman_packages + podman_extra_packages }}"' in podman_main
    # orchestrator first: it generates SSH keys and worker tokens the other hosts consume
    role_plays = [p["hosts"] for p in plays if p.get("roles")]
    assert role_plays[0] == "orchestrator"
    # GitLab and Terra are never bootstrapped
    assert not any("gitlab" in p["hosts"] or "backup" in p["hosts"] for p in plays)
    site = yaml.safe_load((PLAYBOOKS_DIR / "site.yml").read_text(encoding="utf-8"))
    assert site[0]["ansible.builtin.import_playbook"] == "bootstrap.yml"
    for role in ROLES:
        assert (ROLES_DIR / role / "tasks" / "main.yml").is_file()
        meta = yaml.safe_load((ROLES_DIR / role / "meta" / "main.yml").read_text(encoding="utf-8"))
        assert meta["galaxy_info"]["platforms"] == [{"name": "Debian", "versions": ["trixie"]}]


def test_example_inventory_keeps_fixed_host_roles(tmp_path: Path) -> None:
    res = _run(
        [str(VENV_BIN / "ansible-inventory"), "-i", str(EXAMPLE_INVENTORY), "--list"],
        env=_ansible_env(tmp_path),
    )
    assert res.returncode == 0, res.stderr
    inv = json.loads(res.stdout)
    hostvars = inv["_meta"]["hostvars"]

    def addresses(group: str) -> list[str]:
        return [hostvars[h]["ansible_host"] for h in inv[group]["hosts"]]

    assert addresses("orchestrator") == ["192.168.178.225"]
    assert addresses("webui") == ["192.168.178.223"]
    assert addresses("execution_workers") == ["192.168.178.222"]
    assert addresses("model_workers") == ["192.168.178.224"]
    assert addresses("gitlab") == ["192.168.178.226"]
    assert addresses("backup") == ["192.168.178.60"]
    for h in inv["execution_workers"]["hosts"] + inv["model_workers"]["hosts"]:
        assert hostvars[h]["hermclaw_worker_id"] in WORKER_IDS
        assert "wol_mac" in hostvars[h]


# --------------------------------------------------------------------------------------------- consistency


# webui ports of the plan follow nginx's configuration: a host var nginx_listen_port/nginx_tls_port, else the shared
# group_vars hermclaw_webui_http_port/hermclaw_webui_https_port (D-013)
WEBUI_PORT_TEMPLATES = {
    "{{ nginx_listen_port | default(hermclaw_webui_http_port) }}": "hermclaw_webui_http_port",
    "{{ nginx_tls_port | default(hermclaw_webui_https_port) }}": "hermclaw_webui_https_port",
}


def test_port_plan_is_in_sync_and_matches_bauplan() -> None:
    ports, _ = _check_host_tables()
    gv = yaml.safe_load(GROUP_VARS.read_text(encoding="utf-8"))

    def port_of(entry: dict[str, Any]) -> int:
        value = entry["port"]
        if isinstance(value, str):
            assert value in WEBUI_PORT_TEMPLATES, value
            return int(gv[WEBUI_PORT_TEMPLATES[value]])
        return int(value)

    plan = {(g, port_of(e), e["service"]) for g, entries in gv["hermclaw_port_plan"].items() for e in entries}
    assert plan == ports, "check-host.sh PORT_PLAN and group_vars hermclaw_port_plan differ"
    templated = [e["port"] for e in gv["hermclaw_port_plan"]["webui"]]
    assert templated == list(WEBUI_PORT_TEMPLATES), "the webui entries follow the nginx ports"
    expect_names = {(g, port_of(e)): ",".join(e["expect"]) for g, entries in gv["hermclaw_port_plan"].items() for e in entries}
    text = (SCRIPTS_DIR / "check-host.sh").read_text(encoding="utf-8")
    for (group, port), names in expect_names.items():
        assert re.search(rf"^{group} {port} \S+ {re.escape(names)}$", text, re.M), (group, port)
    by_group = {(g, p) for g, p, _ in ports}
    # Bauplan §6; the web host's nginx on 8080/8443 next to the legacy portal (D-013, DECISIONS.md)
    for required in [
        ("orchestrator", 8000),
        ("orchestrator", 4000),
        ("orchestrator", 5432),
        ("orchestrator", 9100),
        ("webui", 8080),
        ("webui", 8443),
        ("execution_workers", 8787),
        ("model_workers", 8787),
        ("model_workers", 11434),
    ]:
        assert required in by_group
    assert not {("webui", 80), ("webui", 443)} & by_group, "80/443 stay with the legacy portal on .223"
    assert gv["hermclaw_webui_shared_host"] is True
    assert (gv["hermclaw_webui_http_port"], gv["hermclaw_webui_https_port"]) == (8080, 8443)
    assert "D-013" in (REPO / "DECISIONS.md").read_text(encoding="utf-8")
    assert gv["hermclaw_api_port"] == 8000 and gv["hermclaw_litellm_port"] == 4000
    assert gv["hermclaw_postgres_port"] == 5432 and gv["hermclaw_ollama_port"] == 11434
    assert gv["hermclaw_worker_port"] == 8787


def test_unit_plan_is_in_sync() -> None:
    _, units = _check_host_tables()
    defaults = yaml.safe_load((ROLES_DIR / "systemd_skeleton" / "defaults" / "main.yml").read_text(encoding="utf-8"))
    expected = {(g, u) for g, lst in defaults["systemd_skeleton_units"].items() for u in lst}
    # installed by the wol_target and ollama roles instead of systemd_skeleton
    expected |= {
        ("execution_workers", "hermclaw-wol@.service"),
        ("model_workers", "hermclaw-wol@.service"),
        ("model_workers", "hermclaw-ollama-firewall.service"),
    }
    assert units == expected
    for _, unit in units:
        assert (SYSTEMD_DIR / unit).exists() or (SYSTEMD_DIR / f"{unit}.j2").exists(), unit
    for enabled in defaults["systemd_skeleton_enable_list"].values():
        for unit in enabled:
            assert any(unit == u for _, u in units)
    assert defaults["systemd_skeleton_enable_units"] is False, "bootstrap must not start services"


def test_every_template_is_used_by_a_role() -> None:
    sources = "\n".join(p.read_text(encoding="utf-8") for p in _task_files())
    sources += (ROLES_DIR / "wol_target" / "defaults" / "main.yml").read_text(encoding="utf-8")
    defaults = yaml.safe_load((ROLES_DIR / "systemd_skeleton" / "defaults" / "main.yml").read_text(encoding="utf-8"))
    units = {u for lst in defaults["systemd_skeleton_units"].values() for u in lst}
    for tpl in _all_templates():
        stem = tpl.name[: -len(".j2")]
        assert tpl.name in sources or stem in units, f"{tpl} is not referenced by any task"
    assert "hermclaw-cred-env" in sources and "hermclaw-wol@.service" in sources


# --------------------------------------------------------------------------------------------- idempotency lint


def test_tasks_use_fqcn_and_command_tasks_are_idempotent() -> None:
    count = 0
    for f, task in _playbook_tasks():
        module = _module_of(task)
        if module is None:
            continue
        count += 1
        where = f"{f.relative_to(REPO)}: {task.get('name')!r}"
        assert module.startswith("ansible.builtin."), f"{where} must use the FQCN (ansible.builtin.*)"
        assert "ignore_errors" not in task, f"{where}: ignore_errors hides failures, use failed_when"
        assert task.get("name"), f"{where} has no name"
        if module in COMMAND_MODULES:
            params = task[module] if isinstance(task[module], dict) else {}
            args = task.get("args") or {}
            has_guard = any(k in params or k in args for k in ("creates", "removes"))
            assert has_guard or "changed_when" in task, f"{where}: needs creates/removes or changed_when"
        if module == "ansible.builtin.apt":
            params = task[module]
            assert params.get("state", "present") != "latest", f"{where}: state=latest is not reproducible"
    assert count > 100


def test_secret_handling_tasks_are_no_log() -> None:
    for f, task in _playbook_tasks():
        module = _module_of(task)
        if module is None:
            continue
        blob = json.dumps(task[module]) + json.dumps(task.get("args") or {})
        where = f"{f.relative_to(REPO)}: {task.get('name')!r}"
        if "decrypt" in blob or "worker_token_plain" in blob:
            assert task.get("no_log") is True, f"{where} handles plaintext secrets and must be no_log"
        # secrets must never travel on a command line
        argv = task[module].get("argv", []) if isinstance(task[module], dict) else []
        assert not any("token_plain" in str(a) for a in argv), where


# --------------------------------------------------------------------------------------------- rendering


def test_every_template_renders(rendered: dict[str, Path]) -> None:
    assert len(rendered) == len(_all_templates()) + 1  # worker.env twice
    for key, path in rendered.items():
        text = path.read_text(encoding="utf-8")
        assert "{{" not in text and "{%" not in text, f"{key}: unrendered Jinja"
        if key.endswith((".service", ".timer", ".container", ".conf", ".pref", ".env")):
            assert "Managed by Hermclaw" in text, f"{key} lacks the managed marker"


def test_rendered_postgresql_is_local_only(rendered: dict[str, Path]) -> None:
    conf = rendered["postgresql/hermclaw.conf"].read_text(encoding="utf-8")
    assert "listen_addresses = 'localhost'" in conf
    assert "port = 5432" in conf
    assert "password_encryption = 'scram-sha-256'" in conf
    assert "log_line_prefix = '%m [%p] %q%u@%d '" in conf
    assert "shared_buffers = 2GB" in conf
    hba = [
        ln.split()
        for ln in rendered["postgresql/pg_hba.conf"].read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    assert ["local", "hermclaw", "hermclaw", "peer"] in hba
    for rule in hba:
        assert rule[-1] in {"peer", "scram-sha-256"}, rule
        if rule[0] == "host":
            assert rule[3] in {"127.0.0.1/32", "::1/128"}, f"non-local host rule {rule}"
    pin = rendered["postgresql/pgdg.pref"].read_text(encoding="utf-8")
    assert "Package: postgresql-17-pgvector" in pin and "Pin: origin apt.postgresql.org" in pin


def test_postgresql_conf_quotes_hostile_values(tmp_path: Path) -> None:
    out = _render(
        tmp_path,
        tmp_path / "hostile",
        _render_vars({"postgresql_settings": {"application_name": "x'; drop\nshared_buffers = 1", "Bad-Key": 1}}),
    )
    conf = out["postgresql/hermclaw.conf"].read_text(encoding="utf-8")
    assert "application_name = 'x''; drop shared_buffers = 1'" in conf
    assert "Bad-Key" not in conf
    assert "\nshared_buffers = 1" not in conf


def test_pg_hba_extra_rules_precede_the_generic_rules(tmp_path: Path) -> None:
    """pg_hba is first-match: operator rules must be evaluated before the catch-all lines."""
    extra = [
        {"type": "local", "database": "hermclaw", "user": "reporting", "method": "reject"},
        {"type": "host", "database": "hermclaw", "user": "backup", "address": "127.0.0.1/32", "method": "scram-sha-256"},
    ]
    out = _render(tmp_path, tmp_path / "hba", _render_vars({"postgresql_hba_extra": extra}))
    hba = [
        ln.split() for ln in out["postgresql/pg_hba.conf"].read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")
    ]
    reject = hba.index(["local", "hermclaw", "reporting", "reject"])
    backup = hba.index(["host", "hermclaw", "backup", "127.0.0.1/32", "scram-sha-256"])
    generic_local = hba.index(["local", "all", "all", "peer"])
    generic_host = hba.index(["host", "all", "all", "127.0.0.1/32", "scram-sha-256"])
    assert hba[0] == ["local", "all", "postgres", "peer"], "the postgres superuser must never be locked out"
    assert reject < generic_local and backup < generic_host
    assert hba.index(["local", "hermclaw", "hermclaw", "peer"]) < reject


def test_rendered_nginx_site_supports_sse(rendered: dict[str, Path]) -> None:
    site = rendered["nginx/hermclaw-next.conf"].read_text(encoding="utf-8")
    assert "server 192.168.178.225:8000;" in site
    assert "root /var/www/hermclaw-next;" in site
    assert "server_name hermclaw.lan;" in site
    assert "default_server" not in site
    sse = site[site.index("events/stream$") : site.index("location /api/ {")]
    for directive in (
        "proxy_buffering off;",
        "proxy_cache off;",
        "gzip off;",
        "proxy_http_version 1.1;",
        'proxy_set_header Connection "";',
        "proxy_read_timeout 3600s;",
    ):
        assert directive in sse, directive
    assert re.search(r"location ~ \^/api/\(jobs/\[\^/\]\+/\)\?events/stream\$", site)
    assert re.fullmatch(r"\^/api/\(jobs/\[\^/\]\+/\)\?events/stream\$", r"^/api/(jobs/[^/]+/)?events/stream$")
    pattern = re.compile(r"^/api/(jobs/[^/]+/)?events/stream$")
    assert pattern.match("/api/jobs/123e4567/events/stream") and pattern.match("/api/events/stream")
    assert not pattern.match("/api/jobs/a/b/events/stream")
    assert "try_files $uri $uri/ /index.html;" in site


def test_rendered_units_load_credentials_not_secrets(rendered: dict[str, Path]) -> None:
    api = rendered["systemd/hermclaw-api.service"].read_text(encoding="utf-8")
    assert "LoadCredentialEncrypted=api-admin-token:/etc/credstore.encrypted/hermclaw-api-admin-token" in api
    for w in WORKER_IDS:
        assert f"LoadCredentialEncrypted=worker-token-{w}:/etc/credstore.encrypted/hermclaw-worker-token-{w}" in api
    assert "litellm-master-key" not in api, "least privilege: the API does not need the LiteLLM key"
    sched = rendered["systemd/hermclaw-scheduler.service"].read_text(encoding="utf-8")
    assert "LoadCredentialEncrypted=gitlab-token:" in sched and "LoadCredentialEncrypted=litellm-master-key:" in sched
    assert "ExecStart=/opt/hermclaw/venv/bin/hermclaw scheduler" in sched
    litellm = rendered["systemd/hermclaw-litellm.service"].read_text(encoding="utf-8")
    assert "hermclaw-cred-env LITELLM_MASTER_KEY=litellm-master-key -- /opt/litellm/venv/bin/litellm" in litellm
    assert "--host 127.0.0.1 --port 4000" in litellm
    for name in ("hermclaw-exec-worker.service", "hermclaw-model-worker.service"):
        unit = rendered[f"systemd/{name}"].read_text(encoding="utf-8")
        assert "LoadCredentialEncrypted=worker-token:/etc/credstore.encrypted/hermclaw-worker-token" in unit
        assert "EnvironmentFile=/etc/hermclaw/worker.env" in unit
    exec_unit = rendered["systemd/hermclaw-exec-worker.service"].read_text(encoding="utf-8")
    assert "Environment=XDG_RUNTIME_DIR=/run/user/998" in exec_unit and "%U" not in exec_unit.split("#")[0]
    assert "NoNewPrivileges=yes" not in exec_unit, "rootless podman needs setuid newuidmap"
    timer = rendered["systemd/hermclaw-backup.timer"].read_text(encoding="utf-8")
    assert "Persistent=true" in timer and "OnCalendar=*-*-* 02:30:00" in timer
    for key, path in rendered.items():
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(?i)(password|secret|token)\s*=\s*[A-Za-z0-9+/]{16,}", text), key
        for line in text.splitlines():
            if line.startswith(("ExecStart=", "Environment=")) and key.startswith("systemd/"):
                assert not re.search(r"(?i)(passw|apikey)=", line), f"{key}: secret-looking value in {line}"


def test_rendered_environment_files(rendered: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    env = {}
    for line in rendered["systemd/hermclaw.env"].read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            env[line.split("=", 1)[0]] = _systemd_env_value(line)
    assert env["HERMCLAW_ENV"] == "production"
    assert env["HERMCLAW_DATABASE_URL"] == "postgresql+psycopg://hermclaw@/hermclaw?host=/run/postgresql"
    assert env["HERMCLAW_API_HOST"] == "192.168.178.225"
    assert json.loads(env["HERMCLAW_WORKER_TOKEN_REFS"]) == {w: f"cred:worker-token-{w}" for w in WORKER_IDS}
    assert all(v.startswith("cred:") for k, v in env.items() if k.endswith("_REF"))
    # the settings model accepts the rendered values
    from hermclaw.core.settings import Settings

    # the settings model reads the rendered file from the environment, exactly as systemd passes it
    for key in [k for k in os.environ if k.startswith("HERMCLAW_")]:
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = Settings()
    assert settings.env == "production" and str(settings.config_dir) == "/etc/hermclaw"
    assert settings.database_url == env["HERMCLAW_DATABASE_URL"] and settings.api_port == 8000
    assert settings.worker_token_refs == {w: f"cred:worker-token-{w}" for w in WORKER_IDS}
    assert settings.api_token_ref == "cred:api-admin-token" and settings.log_json is True
    execution = rendered["systemd/worker-execution.env"].read_text(encoding="utf-8")
    model = rendered["systemd/worker-model.env"].read_text(encoding="utf-8")
    assert "WORKER_KIND=execution" in execution and "WORKER_POLICIES_FILE=/etc/hermclaw/policies.yaml" in execution
    assert "OLLAMA_URL" not in execution
    assert "WORKER_KIND=model" in model and "OLLAMA_URL=http://127.0.0.1:11434" in model
    assert "WORKER_BIND=192.168.178.222:8787" in execution
    assert "ORCHESTRATOR_URL=http://192.168.178.225:8000" in model


def test_rendered_ollama_and_nvidia_policy(rendered: dict[str, Path]) -> None:
    dropin = rendered["systemd/ollama-hermclaw.conf"].read_text(encoding="utf-8")
    assert "Environment=OLLAMA_HOST=0.0.0.0:11434" in dropin
    assert "Environment=OLLAMA_MAX_LOADED_MODELS=2" in dropin
    assert "Environment=OLLAMA_KEEP_ALIVE=5m" in dropin
    assert "UnsetEnvironment=OLLAMA_CONTEXT_LENGTH" in dropin
    assert "OLLAMA_CONTEXT_LENGTH=" not in dropin
    # unset would mean "auto", which enables flash attention on compute capability 6.1 (research 026 E5)
    assert "Environment=OLLAMA_FLASH_ATTENTION=0" in dropin
    assert "OLLAMA_KV_CACHE_TYPE" not in dropin
    assert "Environment=OLLAMA_NO_CLOUD=1" in dropin and "Environment=OLLAMA_VULKAN=0" in dropin
    assert "Environment=OLLAMA_LOAD_TIMEOUT=10m" in dropin
    keys = [line.split("=")[1] for line in dropin.splitlines() if line.startswith("Environment=")]
    assert len(keys) == len(set(keys)), f"duplicate variables: {keys}"
    assert "CUDA_VISIBLE_DEVICES" not in dropin

    assert "ExecStart=\nExecStart=/opt/ollama/current/bin/ollama serve" in dropin
    # runfile driver (default): every apt-provided NVIDIA driver package is blocked
    pin = rendered["nvidia/nvidia-pin.pref"].read_text(encoding="utf-8")
    records = [r for r in pin.split("\n\n") if "Pin:" in r]
    assert len(records) == 1 and "Pin: version *\nPin-Priority: -1" in records[0], pin
    # runfile driver (default source on Debian 13): nouveau off, persistence daemon unit
    blacklist = rendered["nvidia/blacklist-nouveau.conf"].read_text(encoding="utf-8")
    assert "blacklist nouveau" in blacklist and "options nouveau modeset=0" in blacklist
    persistenced = rendered["nvidia/nvidia-persistenced.service"].read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/nvidia-persistenced --user nvidia-persistenced" in persistenced
    defaults = yaml.safe_load((ROLES_DIR / "nvidia" / "defaults" / "main.yml").read_text(encoding="utf-8"))
    assert defaults["nvidia_source"] == "runfile" and defaults["nvidia_runfile_version"].startswith("580.")
    assert "--kernel-module-type=proprietary" in defaults["nvidia_runfile_options"], "Pascal has no open modules"
    # Ollama: GPU groups detected on the host, firewall unit and table
    unit = rendered["systemd/ollama.service"].read_text(encoding="utf-8")
    assert "SupplementaryGroups=video render" in unit
    fw_unit = rendered["systemd/hermclaw-ollama-firewall.service"].read_text(encoding="utf-8")
    assert "ExecStart=/usr/sbin/nft -f /etc/hermclaw/nftables/hermclaw-ollama.nft" in fw_unit
    assert "ExecStop=/usr/sbin/nft delete table inet hermclaw_ollama" in fw_unit
    assert "PartOf=nftables.service" in fw_unit and "ReloadPropagatedFrom=nftables.service" in fw_unit
    assert "Before=network-pre.target ollama.service shutdown.target" in fw_unit
    table = rendered["ollama/hermclaw-ollama.nft"].read_text(encoding="utf-8")
    assert "ip saddr { 192.168.178.225 } tcp dport 11434 accept" in table
    assert table.rstrip().endswith("tcp dport 11434 counter drop\n\t}\n}")
    assert "ip6 saddr" not in table and "flush ruleset" not in table, "only the own table may be replaced"
    nft = shutil.which("nft")
    if nft is not None:
        check = _run([nft, "-c", "-f", str(rendered["ollama/hermclaw-ollama.nft"])], cwd=REPO)
        if "Operation not permitted" not in check.stderr:
            assert check.returncode == 0, check.stderr


def test_ollama_stays_off_the_gpu_while_llamacpp_holds_it(tmp_path: Path) -> None:
    """BUG-313: with llama.cpp the GPU belongs to its resident llama-server and Ollama serves only the embedding model.
    Ollama on the GPU took the free VRAM while the router swapped the planner for the coder (embeddinggemma 25/25
    layers, 587 MiB), and qwen3.6 then failed to load with CUDA OOM on every retry."""
    out = _render(tmp_path, tmp_path / "out", _render_vars({"ollama_llamacpp_enabled": True}))
    dropin = out["systemd/ollama-hermclaw.conf"].read_text(encoding="utf-8")
    assert "Environment=CUDA_VISIBLE_DEVICES=-1" in dropin.splitlines()
    # the role's check that Ollama found the GPU would fail the rollout: it runs only without llama.cpp
    tasks = yaml.safe_load((ROLES_DIR / "ollama" / "tasks" / "main.yml").read_text(encoding="utf-8"))
    check = next(t for t in tasks if t["name"] == "Verify that Ollama uses the GPU")
    assert "not ollama_llamacpp_enabled | bool" in check["when"]


def test_llamacpp_unit_and_firewall_cover_the_llama_server_port(tmp_path: Path) -> None:
    """D-015: the resident llama-server serves the Ollama blob read-only, as the ollama account, and its port is in
    the same firewall table as Ollama's (loopback and the orchestrator only)."""
    out = _render(tmp_path, tmp_path / "out", _render_vars({"ollama_llamacpp_enabled": True}))
    unit = out["systemd/hermclaw-llamacpp.service"].read_text(encoding="utf-8")
    assert "User=ollama" in unit and "ReadOnlyPaths=/var/lib/ollama/models" in unit
    assert "--alias qwen3.6:35b --host 0.0.0.0 --port 18080 -c 32768 -ngl 99 --n-cpu-moe 30" in unit
    assert "-m /var/lib/ollama/models/blobs/sha256-" in unit and "Requires=hermclaw-ollama-firewall.service" in unit
    table = out["ollama/hermclaw-ollama.nft"].read_text(encoding="utf-8")
    assert "ip saddr { 192.168.178.225 } tcp dport { 11434, 18080 } accept" in table
    assert "tcp dport { 11434, 18080 } counter drop" in table
    nft = shutil.which("nft")
    if nft is not None:
        check = _run([nft, "-c", "-f", str(out["ollama/hermclaw-ollama.nft"])], cwd=REPO)
        if "Operation not permitted" not in check.stderr:
            assert check.returncode == 0, check.stderr


def test_llamacpp_archive_must_carry_the_build_it_is_installed_as(tmp_path: Path) -> None:
    """A new archive under the old ``ollama_llamacpp_build`` was skipped by the extract step (``creates:``) and the old
    llama-server kept running (rc60 deploy); the role refuses an archive whose name names another build."""
    ansible = shutil.which("ansible")
    if ansible is None:
        pytest.skip("ansible not installed")
    tasks = yaml.safe_load((ANSIBLE_DIR / "roles/ollama/tasks/llamacpp.yml").read_text(encoding="utf-8"))
    that = next(t for t in tasks if t.get("name") == "Validate the llama.cpp settings")["ansible.builtin.assert"]["that"]
    rule = next(c for c in that if "basename" in c)

    def holds(build: str, archive: str) -> bool:
        res = _run(
            [
                ansible,
                "localhost",
                "-c",
                "local",
                "-m",
                "ansible.builtin.assert",
                "-a",
                json.dumps({"that": [rule]}),
                "-e",
                json.dumps({"ollama_llamacpp_build": build, "ollama_llamacpp_archive_src": archive}),
            ],
            cwd=tmp_path,
        )
        return res.returncode == 0

    assert holds("b11696", "/srv/dist/llamacpp-b11696-sm61.tgz")
    assert not holds("b11361", "/srv/dist/llamacpp-b11696-sm61.tgz")
    assert not holds("b1169", "/srv/dist/llamacpp-b11696-sm61.tgz"), "a prefix of the build is another build"


def test_llamacpp_router_mode_serves_further_models_under_their_ollama_tags(tmp_path: Path) -> None:
    """R-033: with ollama_llamacpp_models the llama-server runs in router mode – one preset per model, the served name
    is the Ollama tag (alias; a section name with ':' would be upper-cased by llama-server), the main model keeps its
    MoE split and loads at start, at most one model is loaded at a time."""
    blob = "/var/lib/ollama/models/blobs/sha256-"
    out = _render(
        tmp_path,
        tmp_path / "out",
        _render_vars(
            {
                "ollama_llamacpp_enabled": True,
                "ollama_llamacpp_models": [{"model": "qwen3.8:27b", "settings": {"fit": "on"}}],
                "ollama_llamacpp_model_paths": {"qwen3.6:35b": blob + "0" * 64, "qwen3.8:27b": blob + "1" * 64},
            }
        ),
    )
    unit = out["systemd/hermclaw-llamacpp.service"].read_text(encoding="utf-8")
    exec_lines = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
    assert len(exec_lines) == 1
    assert "--models-preset /opt/llamacpp/models.ini --models-max 1 --host 0.0.0.0 --port 18080" in exec_lines[0]
    assert " -m " not in exec_lines[0]
    ini = configparser.ConfigParser()
    ini.read_string("[top]\n" + out["ollama/llamacpp-models.ini"].read_text(encoding="utf-8"))
    main, planner = ini["qwen3.6-35b"], ini["qwen3.8-27b"]
    assert main["alias"] == "qwen3.6:35b" and main["model"] == blob + "0" * 64
    assert main["n-cpu-moe"] == "30" and main["n-gpu-layers"] == "99" and main["load-on-startup"] == "true"
    assert planner["alias"] == "qwen3.8:27b" and planner["model"] == blob + "1" * 64 and planner["fit"] == "on"
    assert "load-on-startup" not in planner
    assert ini["*"]["ctx-size"] == "32768" and ini["*"]["parallel"] == "1"


def test_llamacpp_main_model_takes_preset_settings_and_without_them_stays_as_before(tmp_path: Path) -> None:
    """R-034: ollama_llamacpp_model_settings carries tuned preset values of the resident model – a line each in its
    models.ini section (router mode), `--<option> <value>` in single-model mode (`true`: the bare flag). Without
    values both stay exactly as before."""
    blob = "/var/lib/ollama/models/blobs/sha256-"
    router = {"ollama_llamacpp_enabled": True, "ollama_llamacpp_models": [{"model": "qwen3.8:27b"}]}
    router["ollama_llamacpp_model_paths"] = {"qwen3.6:35b": blob + "0" * 64, "qwen3.8:27b": blob + "1" * 64}
    settings = {"ollama_llamacpp_model_settings": {"ubatch-size": 1024, "no-mmap": "true"}}
    single_exec = (
        f"ExecStart=/opt/llamacpp/current/bin/llama-server -m {blob}{'0' * 64} --alias qwen3.6:35b --host 0.0.0.0 "
        "--port 18080 -c 32768 -ngl 99 --n-cpu-moe 30 -fa on -t 6 -np 1 --jinja --no-webui"
    )
    for name, overrides, extra in [("plain", {}, ""), ("tuned", settings, " --ubatch-size 1024 --no-mmap")]:
        single = _render(tmp_path, tmp_path / f"single-{name}", _render_vars({"ollama_llamacpp_enabled": True, **overrides}))
        unit = single["systemd/hermclaw-llamacpp.service"].read_text(encoding="utf-8")
        assert [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")] == [single_exec + extra]
        out = _render(tmp_path, tmp_path / f"router-{name}", _render_vars({**router, **overrides}))
        ini = configparser.ConfigParser()
        ini.read_string("[top]\n" + out["ollama/llamacpp-models.ini"].read_text(encoding="utf-8"))
        expected = {"alias": "qwen3.6:35b", "model": blob + "0" * 64, "n-gpu-layers": "99", "n-cpu-moe": "30"}
        expected["load-on-startup"] = "true"
        if overrides:
            expected |= {"ubatch-size": "1024", "no-mmap": "true"}
        assert dict(ini["qwen3.6-35b"]) == expected
        assert dict(ini["qwen3.8-27b"]) == {"alias": "qwen3.8:27b", "model": blob + "1" * 64}


def test_the_ollama_restarts_reload_systemd_themselves() -> None:
    """BUG-314: `notify: Reload systemd` resolves to systemd_skeleton's handler, which runs after the ollama role's
    restarts – the BUG-313 drop-in was written, Ollama restarted with the old environment, systemd reloaded after."""
    handlers = yaml.safe_load((ROLES_DIR / "ollama" / "handlers" / "main.yml").read_text(encoding="utf-8"))
    restarts = [h for h in handlers if h.get("ansible.builtin.systemd_service", {}).get("state") == "restarted"]
    assert {h["name"] for h in restarts} == {"Restart ollama", "Restart llama-server"}
    assert all(h["ansible.builtin.systemd_service"].get("daemon_reload") is True for h in restarts)


def test_rendered_podman_and_searxng(rendered: dict[str, Path]) -> None:
    delegate = rendered["podman/delegate.conf"].read_text(encoding="utf-8")
    assert "[Service]\nDelegate=cpu cpuset io memory pids" in delegate
    containers = rendered["podman/containers-hermclaw.conf"].read_text(encoding="utf-8")
    assert 'default_rootless_network_cmd = "pasta"' in containers
    quadlet = rendered["systemd/hermclaw-searxng.container"].read_text(encoding="utf-8")
    assert "PublishPort=127.0.0.1:8888:8080" in quadlet
    assert re.search(r"^Image=docker.io/searxng/searxng:\d{4}\.\d+\.\d+-[0-9a-f]+@sha256:[0-9a-f]{64}$", quadlet, re.M), (
        "image must be pinned by tag and digest"
    )
    assert "Environment=GRANIAN_HOST=0.0.0.0" in quadlet, "no dependency on IPv6 in the container (research 027 S4)"
    assert "Secret=hermclaw-searxng-secret,type=env,target=SEARXNG_SECRET" in quadlet
    settings = yaml.safe_load(rendered["systemd_skeleton/searxng-settings.yml"].read_text(encoding="utf-8"))
    assert "json" in settings["search"]["formats"]
    assert settings["server"]["limiter"] is False
    # bootstrap never starts services: without [Install] the Quadlet unit is not pulled in at boot
    directives = [ln for ln in quadlet.splitlines() if ln and not ln.startswith("#")]
    assert "[Install]" not in directives and not any(ln.startswith("WantedBy=") for ln in directives)


def test_searxng_quadlet_is_installable_once_units_are_enabled(tmp_path: Path) -> None:
    out = _render(tmp_path, tmp_path / "enabled", _render_vars({"systemd_skeleton_enable_units": True}))
    quadlet = out["systemd/hermclaw-searxng.container"].read_text(encoding="utf-8")
    assert quadlet.rstrip().endswith("[Install]\nWantedBy=default.target")


def test_rendered_sudoers_allowlist(rendered: dict[str, Path], tmp_path: Path) -> None:
    text = rendered["common/sudoers-runtime"].read_text(encoding="utf-8")
    rules = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert rules == ["Defaults:hermclaw-admin !lecture", "hermclaw-admin ALL=(root) NOPASSWD: /usr/bin/systemctl suspend"]
    visudo = shutil.which("visudo") or ("/usr/sbin/visudo" if Path("/usr/sbin/visudo").exists() else None)
    if visudo is None:
        pytest.skip("visudo not installed")
    res = _run([visudo, "-cf", str(rendered["common/sudoers-runtime"])], cwd=tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    broken = tmp_path / "broken"
    broken.write_text("hermclaw-admin ALL=(root NOPASSWD: /usr/bin/systemctl suspend\n", encoding="utf-8")
    assert _run([visudo, "-cf", str(broken)], cwd=tmp_path).returncode != 0, "visudo check is vacuous"


# --------------------------------------------------------------------------------------------- systemd verify


def _stub_paths(root: Path) -> dict[str, str]:
    paths = {
        "hermclaw_venv": str(root / "opt/hermclaw/venv"),
        "hermclaw_bin_dir": str(root / "opt/hermclaw/bin"),
        "systemd_skeleton_litellm_venv": str(root / "opt/litellm/venv"),
        "systemd_skeleton_backup_command": str(root / "opt/hermclaw/bin/hermclaw-backup"),
        "systemd_skeleton_restore_test_command": str(root / "opt/hermclaw/bin/hermclaw-restore-test"),
        "ollama_install_root": str(root / "opt/ollama"),
        "ollama_llamacpp_install_root": str(root / "opt/llamacpp"),
        "nvidia_persistenced_bin": str(root / "usr/bin/nvidia-persistenced"),
        "ollama_firewall_nft_bin": str(root / "usr/sbin/nft"),
    }
    for exe in (
        "opt/hermclaw/venv/bin/hermclaw",
        "opt/hermclaw/venv/bin/hermclaw-exec-worker",
        "opt/hermclaw/venv/bin/hermclaw-model-worker",
        "opt/hermclaw/bin/hermclaw-cred-env",
        "opt/hermclaw/bin/hermclaw-backup",
        "opt/hermclaw/bin/hermclaw-restore-test",
        "opt/litellm/venv/bin/litellm",
        "opt/ollama/current/bin/ollama",
        "opt/llamacpp/current/bin/llama-server",
        "usr/sbin/ethtool",
        "usr/bin/nvidia-persistenced",
        "usr/sbin/nft",
    ):
        p = root / exe
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)
    return paths


SYSTEMD_ERROR = re.compile(r"Unknown (key|section|lvalue)|Failed to parse|Invalid|is not executable|not found|bad unit")


def test_rendered_units_pass_systemd_analyze_verify(tmp_path: Path) -> None:
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze not installed")
    stub_root = tmp_path / "root"
    out = _render(tmp_path, tmp_path / "units", _render_vars(_stub_paths(stub_root)))
    unit_dir = tmp_path / "verify"
    unit_dir.mkdir()
    units: list[Path] = []
    for key, path in out.items():
        name = key.split("/", 1)[1]
        if name.endswith((".service", ".timer")):
            target = unit_dir / name
            shutil.copy(path, target)
            units.append(target)
    wol = (SYSTEMD_DIR / "hermclaw-wol@.service").read_text(encoding="utf-8")
    wol_instance = unit_dir / "hermclaw-wol@lo.service"
    wol_instance.write_text(wol.replace("/usr/sbin/ethtool", str(stub_root / "usr/sbin/ethtool")), encoding="utf-8")
    units.append(wol_instance)
    # the Ollama drop-in is verified on top of the base unit
    dropin_dir = unit_dir / "ollama.service.d"
    dropin_dir.mkdir()
    shutil.move(str(out["systemd/ollama-hermclaw.conf"]), dropin_dir / "hermclaw.conf")
    assert {u.name for u in units} >= {
        "hermclaw-api.service",
        "hermclaw-scheduler.service",
        "hermclaw-litellm.service",
        "hermclaw-backup.service",
        "hermclaw-backup.timer",
        "hermclaw-restore-test.service",
        "hermclaw-restore-test.timer",
        "hermclaw-exec-worker.service",
        "hermclaw-model-worker.service",
        "ollama.service",
        "hermclaw-ollama-firewall.service",
        "hermclaw-llamacpp.service",
        "nvidia-persistenced.service",
    }
    res = _run([analyze, "verify", *[str(u) for u in units]], cwd=unit_dir, timeout=120)
    problems = [ln for ln in res.stderr.splitlines() if SYSTEMD_ERROR.search(ln) and "sysinit.target" not in ln]
    assert res.returncode == 0 and not problems, res.stderr


def test_systemd_analyze_detects_broken_units(tmp_path: Path) -> None:
    """Guard against a vacuous verify: the same check must fail for a broken unit."""
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze not installed")
    bad = tmp_path / "hermclaw-broken.service"
    bad.write_text("[Service]\nExecStart=/nonexistent/hermclaw\nLoadCredentialEncryptd=x:/y\n", encoding="utf-8")
    res = _run([analyze, "verify", str(bad)], cwd=tmp_path, timeout=60)
    assert res.returncode != 0 or any(SYSTEMD_ERROR.search(ln) for ln in res.stderr.splitlines())
    assert "LoadCredentialEncryptd" in res.stderr


def test_searxng_quadlet_dryrun(rendered: dict[str, Path], tmp_path: Path) -> None:
    quadlet = next((p for p in ("/usr/libexec/podman/quadlet", "/usr/lib/podman/quadlet") if Path(p).exists()), None)
    if quadlet is None:
        pytest.skip("podman quadlet generator not installed")
    qdir = tmp_path / "quadlet"
    qdir.mkdir()
    shutil.copy(rendered["systemd/hermclaw-searxng.container"], qdir / "hermclaw-searxng.container")
    env = {**os.environ, "QUADLET_UNIT_DIRS": str(qdir)}
    res = _run([quadlet, "-dryrun", "-user"], env=env, cwd=tmp_path, timeout=60)
    assert res.returncode == 0, res.stderr
    assert "---hermclaw-searxng.service---" in res.stdout
    exec_start = next(ln for ln in res.stdout.splitlines() if ln.startswith("ExecStart="))
    for flag in (
        "--publish 127.0.0.1:8888:8080",
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        "--secret hermclaw-searxng-secret,type=env,target=SEARXNG_SECRET",
        "/etc/searxng:ro",
    ):
        assert flag in exec_start, flag


# --------------------------------------------------------------------------------------------- ansible-lint


def test_ansible_lint_production_profile(tmp_path: Path) -> None:
    venv_lint = VENV_BIN / "ansible-lint"
    lint = os.environ.get("HERMCLAW_ANSIBLE_LINT") or (str(venv_lint) if venv_lint.exists() else shutil.which("ansible-lint"))
    if not lint or not Path(lint).exists():
        pytest.skip("ansible-lint not available (set HERMCLAW_ANSIBLE_LINT=/path/to/ansible-lint)")
    env = _ansible_env(tmp_path)
    env["PATH"] = f"{Path(lint).parent}{os.pathsep}{env['PATH']}"
    res = _run(
        [
            lint,
            "--offline",
            "--profile=production",
            "--format=pep8",
            "playbooks/bootstrap.yml",
            "playbooks/site.yml",
            "playbooks/inventory.yml",
            "roles",
        ],
        env=env,
        timeout=600,
    )
    assert res.returncode == 0, res.stdout[-4000:] + res.stderr[-4000:]


# --------------------------------------------------------------------------------------------- scripts


SCRIPTS = [*sorted(SCRIPTS_DIR.glob("*.sh")), SYSTEMD_DIR / "hermclaw-cred-env"]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_are_valid(script: Path) -> None:
    assert script.stat().st_mode & stat.S_IXUSR, f"{script} is not executable"
    assert script.read_text(encoding="utf-8").startswith("#!")
    res = _run(["bash", "-n", str(script)], cwd=REPO)
    assert res.returncode == 0, res.stderr
    shellcheck = shutil.which("shellcheck")
    if shellcheck:
        res = _run([shellcheck, "-S", "warning", str(script)], cwd=REPO)
        assert res.returncode == 0, res.stdout


def _cred_env(
    tmp_path: Path, args: list[str], creds: Mapping[str, str] | None = None, set_dir: bool = True
) -> subprocess.CompletedProcess[str]:
    cdir = tmp_path / "creds"
    cdir.mkdir(exist_ok=True)
    for name, value in (creds or {}).items():
        (cdir / name).write_text(value, encoding="utf-8")
    env = {"PATH": os.environ["PATH"]}
    if set_dir:
        env["CREDENTIALS_DIRECTORY"] = str(cdir)
    return _run([str(SYSTEMD_DIR / "hermclaw-cred-env"), *args], env=env, cwd=tmp_path)


def test_cred_env_exports_credentials_and_execs(tmp_path: Path) -> None:
    res = _cred_env(
        tmp_path,
        [
            "LITELLM_MASTER_KEY=litellm-master-key",
            "OTHER=other",
            "--",
            sys.executable,
            "-c",
            "import os; print(os.environ['LITELLM_MASTER_KEY'] + '|' + os.environ['OTHER'])",
        ],
        {"litellm-master-key": "sk-first-line\nsecond-line\n", "other": "o"},
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "sk-first-line|o"
    assert res.stderr == ""


@pytest.mark.parametrize(
    ("args", "creds", "set_dir", "code"),
    [
        (["K=c", "--", "true"], {"c": "v"}, False, 78),
        (["K=missing", "--", "true"], {}, True, 78),
        (["K=empty", "--", "true"], {"empty": ""}, True, 78),
        (["K=../etc/passwd", "--", "true"], {}, True, 64),
        (["K=a..b", "--", "true"], {}, True, 64),
        (["lower=c", "--", "true"], {"c": "v"}, True, 64),
        (["K=c", "--"], {"c": "v"}, True, 64),
        (["--", "true"], {}, True, 64),
        (["K=c", "true"], {"c": "v"}, True, 64),
    ],
)
def test_cred_env_rejects_bad_input(tmp_path: Path, args: list[str], creds: dict[str, str], set_dir: bool, code: int) -> None:
    res = _cred_env(tmp_path, args, creds, set_dir)
    assert res.returncode == code, res.stderr
    assert "v" not in res.stdout


FAKE_SYSTEMD_CREDS = """#!/bin/bash
# test double: records argv, "encrypts" by prefixing the embedded name
printf '%s\\n' "$*" >> "$FAKE_CREDS_LOG"
mode="$1"; shift
name=""; with_key=""
while [[ "$1" == --* ]]; do
  case "$1" in --name=*) name="${1#--name=}";; --with-key=*) with_key="${1#--with-key=}";; esac
  shift
done
in="$1"; out="$2"
if [[ "$mode" == encrypt ]]; then
  { printf 'ENC:%s:%s:' "$name" "$with_key"; cat; } > "$out"
elif [[ "$mode" == decrypt ]]; then
  content=$(cat "$in")
  [[ "$content" == "ENC:${name}:"* ]] || { echo "name mismatch" >&2; exit 1; }
  printf '%s' "${content#ENC:${name}:*:}"
fi
"""


@pytest.fixture
def fake_creds(tmp_path: Path) -> dict[str, str]:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    fake = bindir / "systemd-creds"
    fake.write_text(FAKE_SYSTEMD_CREDS, encoding="utf-8")
    fake.chmod(0o755)
    store = tmp_path / "credstore"
    return {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HERMCLAW_CREDSTORE_DIR": str(store),
        "FAKE_CREDS_LOG": str(tmp_path / "creds.log"),
    }


def _credential(env: Mapping[str, str], *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return _run([str(SCRIPTS_DIR / "hermclaw-credential.sh"), *args], env=env, cwd=REPO, input_text=stdin)


def test_credential_set_generate_check_list(fake_creds: dict[str, str]) -> None:
    store = Path(fake_creds["HERMCLAW_CREDSTORE_DIR"])
    res = _credential(fake_creds, "set", "gitlab-token", stdin="glpat-SUPERSECRET-123\n")
    assert res.returncode == 0, res.stderr
    target = store / "hermclaw-gitlab-token"
    assert target.read_text(encoding="utf-8") == "ENC:gitlab-token:auto:glpat-SUPERSECRET-123"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert "LoadCredentialEncrypted=gitlab-token:" in res.stdout
    assert "SUPERSECRET" not in res.stdout + res.stderr
    assert "SUPERSECRET" not in Path(fake_creds["FAKE_CREDS_LOG"]).read_text(encoding="utf-8"), "secret in argv"
    # never overwritten silently
    res = _credential(fake_creds, "set", "gitlab-token", stdin="other\n")
    assert res.returncode == 73
    assert "SUPERSECRET" in target.read_text(encoding="utf-8")
    res = _credential(fake_creds, "set", "gitlab-token", "--force", stdin="rotated\n")
    assert res.returncode == 0 and target.read_text(encoding="utf-8").endswith(":rotated")
    # generated secrets are random, url-safe and long enough
    for name in ("api-admin-token", "litellm-master-key"):
        res = _credential(fake_creds, "generate", name)
        assert res.returncode == 0, res.stderr
    a = (store / "hermclaw-api-admin-token").read_text(encoding="utf-8").split(":", 3)[3]
    b = (store / "hermclaw-litellm-master-key").read_text(encoding="utf-8").split(":", 3)[3]
    assert a != b and len(a) >= 40 and re.fullmatch(r"[A-Za-z0-9_-]+", a)
    res = _credential(fake_creds, "check", "api-admin-token")
    assert res.returncode == 0 and res.stdout.strip() == "OK api-admin-token"
    assert a not in res.stdout
    res = _credential(fake_creds, "list")
    assert res.stdout.split() == ["api-admin-token", "gitlab-token", "litellm-master-key"]
    assert not list(store.glob(".hermclaw-*")), "temporary files left behind"


def test_credential_rejects_bad_names_and_empty_secrets(fake_creds: dict[str, str]) -> None:
    for name in ("../escape", "UPPER", "a..b", "", "-x"):
        res = _credential(fake_creds, "set", name, stdin="v\n")
        assert res.returncode == 64, name
    assert _credential(fake_creds, "set", "empty", stdin="\n").returncode == 65
    assert _credential(fake_creds, "generate", "short", "--bytes", "4").returncode == 64
    assert _credential(fake_creds, "check", "missing").returncode == 66
    assert _credential(fake_creds, "bogus").returncode == 64
    store = Path(fake_creds["HERMCLAW_CREDSTORE_DIR"])
    assert not store.exists(), "rejected calls must not create the credstore"
    store.mkdir(mode=0o700)
    (store / "hermclaw-tampered").write_text("ENC:other-name:auto:x", encoding="utf-8")
    assert _credential(fake_creds, "check", "tampered").returncode == 65


def _check_host(*args: str) -> subprocess.CompletedProcess[str]:
    return _run(["bash", str(SCRIPTS_DIR / "check-host.sh"), *args], cwd=REPO, timeout=120)


def test_check_host_reports_json_and_refuses_non_debian() -> None:
    res = _check_host("--role", "orchestrator,webui", "--json")
    report = json.loads(res.stdout)
    checks = {c["check"]: c for c in report["checks"]}
    for port in (8000, 4000, 5432, 8888, 9100, 8080, 8443):
        assert f"port:{port}" in checks
    assert "port:80" not in checks and "port:443" not in checks, "the legacy portal's ports are not Hermclaw's (D-013)"
    assert "unit:hermclaw-api.service" in checks
    os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    is_trixie = "ID=debian" in os_release and 'VERSION_ID="13"' in os_release
    assert (checks["os"]["status"] == "OK") is is_trixie
    if not is_trixie:
        assert res.returncode == 1 and report["failures"] >= 1
    assert report["failures"] == sum(1 for c in report["checks"] if c["status"] == "FAIL")


@pytest.mark.parametrize("args", [[], ["--role"], ["--role", "Bad;rm"], ["--role", "webui", "--min-ram-mb", "x"], ["--bogus"]])
def test_check_host_usage_errors(args: list[str]) -> None:
    assert _check_host(*args).returncode == 64


def test_bootstrap_control_node_creates_but_never_overwrites_inventory(tmp_path: Path) -> None:
    inventory = tmp_path / "inv" / "hosts.yml"
    argv = [str(SCRIPTS_DIR / "bootstrap-control-node.sh"), "--ansible-bin-dir", str(VENV_BIN), "--inventory", str(inventory)]
    env = _ansible_env(tmp_path)
    res = _run(argv, env=env, cwd=tmp_path, timeout=600)
    assert res.returncode == 0, res.stdout + res.stderr
    assert inventory.read_text(encoding="utf-8") == EXAMPLE_INVENTORY.read_text(encoding="utf-8")
    assert stat.S_IMODE(inventory.stat().st_mode) == 0o640
    assert "placeholder MAC" in res.stdout
    for pb in ("bootstrap.yml", "site.yml", "inventory.yml"):
        assert f"syntax OK: {pb}" in res.stdout
    custom = inventory.read_text(encoding="utf-8").replace("hermclaw.lan", "hermclaw.example")
    inventory.write_text(custom, encoding="utf-8")
    res = _run([*argv, "--skip-syntax-check"], env=env, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "left unchanged" in res.stdout
    assert inventory.read_text(encoding="utf-8") == custom


def test_bootstrap_control_node_rejects_incomplete_inventory(tmp_path: Path) -> None:
    inventory = tmp_path / "hosts.yml"
    inventory.write_text("all:\n  children:\n    orchestrator:\n      hosts:\n        o: {}\n", encoding="utf-8")
    res = _run(
        [
            str(SCRIPTS_DIR / "bootstrap-control-node.sh"),
            "--ansible-bin-dir",
            str(VENV_BIN),
            "--inventory",
            str(inventory),
            "--skip-syntax-check",
        ],
        env=_ansible_env(tmp_path),
        cwd=tmp_path,
    )
    assert res.returncode == 1
    assert "group 'webui' has no hosts" in res.stderr


# --------------------------------------------------------------------------------------------- role input validation

# (case id, role, task vars, expected outcome). Roles validate every value that ends up in a
# config file, unit, sudoers rule or command line before touching the host.
VALIDATION_CASES: list[tuple[str, str, dict[str, Any], str]] = [
    ("common-defaults", "common", {}, "PASS"),
    ("common-path-traversal", "common", {"hermclaw_data_dir": "/var/lib/../etc"}, "FAIL"),
    ("common-relative-path", "common", {"hermclaw_log_dir": "var/log/hermclaw"}, "FAIL"),
    ("common-bad-user", "common", {"hermclaw_user": "root;rm"}, "FAIL"),
    ("common-bad-orchestrator-ip", "common", {"hermclaw_orchestrator_ip": "192.168.178.225,0.0.0.0/0"}, "FAIL"),
    ("common-sudo-list-injection", "common", {"common_ssh_runtime_sudo_commands": ["/usr/bin/systemctl suspend, ALL"]}, "FAIL"),
    ("common-sudo-wildcard", "common", {"common_ssh_runtime_sudo_commands": ["/usr/bin/systemctl *"]}, "FAIL"),
    ("common-sudo-relative", "common", {"common_ssh_runtime_sudo_commands": ["systemctl suspend"]}, "FAIL"),
    ("common-sudo-ok", "common", {"common_ssh_runtime_sudo_commands": ["/usr/bin/systemctl suspend", "/usr/sbin/reboot"]}, "PASS"),
    ("common-forced-command-quote", "common", {"common_ssh_forced_command": '/usr/local/bin/gate" ,x'}, "FAIL"),
    ("common-forced-command-ok", "common", {"common_ssh_forced_command": "/usr/local/bin/hermclaw-ssh-gate"}, "PASS"),
    ("common-shared-admin-login", "common", {"ansible_user": "hermclaw-admin"}, "FAIL"),
    ("common-account-comment-colon", "common", {"common_service_account_comment": "Hermclaw: x"}, "FAIL"),
    ("common-account-comments-equal", "common", {"common_runtime_account_comment": "Hermclaw Next service account"}, "FAIL"),
    ("common-shared-admin-allowed", "common", {"ansible_user": "hermclaw-admin", "common_allow_shared_admin_login": True}, "PASS"),
    (
        "common-known-hosts-ok",
        "common",
        {
            "common_known_hosts_extra": [
                f"gitlab.lan,192.168.178.226 ssh-ed25519 {KEY_B64}",
                f"[192.168.178.60]:2222 ecdsa-sha2-nistp256 {KEY_B64}=",
            ]
        },
        "PASS",
    ),
    (
        "common-known-hosts-newline",
        "common",
        {"common_known_hosts_extra": [f"gitlab.lan ssh-ed25519 {KEY_B64}\n* ssh-ed25519 {KEY_B64}"]},
        "FAIL",
    ),
    ("common-known-hosts-marker", "common", {"common_known_hosts_extra": [f"@cert-authority * ssh-ed25519 {KEY_B64}"]}, "FAIL"),
    ("common-known-hosts-wildcard", "common", {"common_known_hosts_extra": [f"* ssh-ed25519 {KEY_B64}"]}, "FAIL"),
    ("common-known-hosts-bad-type", "common", {"common_known_hosts_extra": [f"gitlab.lan ssh-dss {KEY_B64}"]}, "FAIL"),
    ("common-known-hosts-no-key", "common", {"common_known_hosts_extra": ["gitlab.lan ssh-ed25519"]}, "FAIL"),
    ("pg-defaults", "postgresql", {}, "PASS"),
    ("pg-listen-lan", "postgresql", {"postgresql_listen_addresses": "0.0.0.0"}, "FAIL"),
    ("pg-db-injection", "postgresql", {"postgresql_db_name": "hermclaw; drop"}, "FAIL"),
    ("pg-vulnerable-pgvector", "postgresql", {"postgresql_pgvector_min_version": "0.8.1"}, "FAIL"),
    ("pg-recommended-below-cve-2026-103484", "postgresql", {"postgresql_pgvector_recommended_version": "0.8.6"}, "FAIL"),
    (
        "pg-hba-trust",
        "postgresql",
        {"postgresql_hba_extra": [{"type": "host", "database": "all", "user": "all", "address": "0.0.0.0/0", "method": "trust"}]},
        "FAIL",
    ),
    (
        "pg-hba-host-without-address",
        "postgresql",
        {"postgresql_hba_extra": [{"type": "host", "database": "hermclaw", "user": "hermclaw", "method": "scram-sha-256"}]},
        "FAIL",
    ),
    (
        "pg-hba-newline",
        "postgresql",
        {"postgresql_hba_extra": [{"type": "local", "database": "all\nhost all all 0.0.0.0/0 trust", "user": "x", "method": "peer"}]},
        "FAIL",
    ),
    (
        "pg-hba-ok",
        "postgresql",
        {
            "postgresql_hba_extra": [
                {"type": "host", "database": "hermclaw", "user": "backup", "address": "127.0.0.1/32", "method": "scram-sha-256"}
            ]
        },
        "PASS",
    ),
    ("nginx-defaults", "nginx", {"hermclaw_web_server_name": "hermclaw.lan"}, "PASS"),
    # D-013: on the shared web host 80/443 belong to the legacy portal
    ("nginx-shared-port-80", "nginx", {"hermclaw_webui_http_port": 80}, "FAIL"),
    ("nginx-shared-listen-443", "nginx", {"nginx_listen_port": 443}, "FAIL"),
    (
        "nginx-shared-tls-443",
        "nginx",
        {"nginx_tls_enabled": True, "hermclaw_web_server_name": "hermclaw.lan", "nginx_tls_port": 443},
        "FAIL",
    ),
    ("nginx-shared-tls-8443", "nginx", {"nginx_tls_enabled": True, "hermclaw_web_server_name": "hermclaw.lan"}, "PASS"),
    (
        "nginx-shared-hsts",
        "nginx",
        {"nginx_tls_enabled": True, "hermclaw_web_server_name": "hermclaw.lan", "nginx_hsts_max_age": 300},
        "FAIL",
    ),
    ("nginx-dedicated-port-80", "nginx", {"hermclaw_webui_shared_host": False, "hermclaw_webui_http_port": 80}, "PASS"),
    (
        "nginx-dedicated-hsts",
        "nginx",
        {
            "hermclaw_webui_shared_host": False,
            "nginx_tls_enabled": True,
            "hermclaw_web_server_name": "hermclaw.lan",
            "nginx_hsts_max_age": 300,
        },
        "PASS",
    ),
    ("nginx-listen-port-range", "nginx", {"nginx_listen_port": 70000}, "FAIL"),
    ("nginx-listen-port-text", "nginx", {"nginx_listen_port": "8080; listen 80"}, "FAIL"),
    ("nginx-reserved-string", "nginx", {"nginx_reserved_ports": "80"}, "FAIL"),
    ("nginx-firewall-ok", "nginx", {"nginx_firewall_allow_from": ["192.168.178.0/24", "192.168.178.2", "fd00::/8"]}, "PASS"),
    ("nginx-firewall-injection", "nginx", {"nginx_firewall_allow_from": ["192.168.178.0/24 port 22"]}, "FAIL"),
    ("nginx-firewall-string", "nginx", {"nginx_firewall_allow_from": "192.168.178.0/24"}, "FAIL"),
    ("nginx-server-name-injection", "nginx", {"hermclaw_web_server_name": "x; return 301 http://evil"}, "FAIL"),
    ("nginx-upstream-injection", "nginx", {"nginx_api_upstream": "http://192.168.178.225:8000/; deny all"}, "FAIL"),
    ("ollama-defaults", "ollama", {}, "PASS"),
    ("ollama-context-length", "ollama", {"ollama_extra_env": {"OLLAMA_CONTEXT_LENGTH": "8192"}}, "FAIL"),
    ("ollama-env-injection", "ollama", {"ollama_extra_env": {"OLLAMA_DEBUG": "1\nExecStartPre=/bin/sh"}}, "FAIL"),
    ("ollama-foreign-env", "ollama", {"ollama_extra_env": {"LD_PRELOAD": "/tmp/x.so"}}, "FAIL"),
    ("ollama-http-download", "ollama", {"ollama_release_base": "http://github.com/ollama/ollama/releases/download"}, "FAIL"),
    ("ollama-flash-attention-env-ok", "ollama", {"ollama_extra_env": {"OLLAMA_DEBUG": "1"}}, "PASS"),
    # dedicated settings must not be duplicated through ollama_extra_env (two Environment= lines)
    ("ollama-extra-env-managed-key", "ollama", {"ollama_extra_env": {"OLLAMA_FLASH_ATTENTION": "1"}}, "FAIL"),
    ("ollama-extra-env-host", "ollama", {"ollama_extra_env": {"OLLAMA_HOST": "0.0.0.0:1"}}, "FAIL"),
    ("ollama-flash-auto", "ollama", {"ollama_flash_attention": "auto"}, "PASS"),
    ("ollama-flash-bool", "ollama", {"ollama_flash_attention": True}, "PASS"),
    ("ollama-flash-bogus", "ollama", {"ollama_flash_attention": "maybe"}, "FAIL"),
    ("ollama-kv-q8-without-fa", "ollama", {"ollama_kv_cache_type": "q8_0"}, "FAIL"),
    ("ollama-kv-q8-with-fa", "ollama", {"ollama_kv_cache_type": "q8_0", "ollama_flash_attention": "true"}, "PASS"),
    ("ollama-kv-unknown", "ollama", {"ollama_kv_cache_type": "q2_k", "ollama_flash_attention": "true"}, "FAIL"),
    ("ollama-load-timeout-injection", "ollama", {"ollama_load_timeout": "10m\nExecStartPre=/bin/sh"}, "FAIL"),
    ("ollama-gpu-library-unknown", "ollama", {"ollama_expected_gpu_library": "ROCm"}, "FAIL"),
    ("ollama-gpu-check-off", "ollama", {"ollama_expected_gpu_library": ""}, "PASS"),
    ("ollama-below-minimum", "ollama", {"ollama_version": "0.30.1"}, "FAIL"),
    (
        "ollama-firewall-sources-ok",
        "ollama",
        {"ollama_firewall_allowed_sources": ["192.168.178.225", "192.168.178.0/24", "fd00::/8", "::1"]},
        "PASS",
    ),
    (
        "ollama-firewall-source-injection",
        "ollama",
        {"ollama_firewall_allowed_sources": ["192.168.178.225 } accept; tcp dport 22 drop; ip saddr {"]},
        "FAIL",
    ),
    ("ollama-firewall-source-newline", "ollama", {"ollama_firewall_allowed_sources": ["192.168.178.225\n"]}, "FAIL"),
    ("ollama-firewall-bad-cidr", "ollama", {"ollama_firewall_allowed_sources": ["192.168.178.0/33"]}, "FAIL"),
    ("ollama-firewall-bad-ip", "ollama", {"ollama_firewall_allowed_sources": ["192.168.178.256"]}, "FAIL"),
    ("podman-defaults", "podman", {}, "PASS"),
    ("podman-bad-controller", "podman", {"podman_delegate_controllers": ["cpu", "devices"]}, "FAIL"),
    ("podman-low-subids", "podman", {"podman_subid_start": 1000}, "FAIL"),
    ("podman-extra-packages", "podman", {"podman_extra_packages": ["podman-compose"]}, "PASS"),
    ("podman-extra-packages-string", "podman", {"podman_extra_packages": "podman-compose"}, "FAIL"),
    ("podman-extra-packages-option", "podman", {"podman_extra_packages": ["-o=evil"]}, "FAIL"),
    ("nvidia-defaults", "nvidia", {}, "PASS"),
    ("nvidia-590-drops-pascal", "nvidia", {"nvidia_driver_branch": "590"}, "FAIL"),
    # Debian's 550 drives Pascal, but Ollama >= 0.30 ignores CC 6.1 GPUs below driver 570 (research 026 O7)
    ("nvidia-debian-550", "nvidia", {"nvidia_source": "debian", "nvidia_driver_branch": "550"}, "FAIL"),
    (
        "nvidia-debian-550-without-ollama",
        "nvidia",
        {"nvidia_source": "debian", "nvidia_driver_branch": "550", "nvidia_allow_branch_below_ollama_minimum": True},
        "PASS",
    ),
    ("nvidia-cuda-570", "nvidia", {"nvidia_source": "cuda", "nvidia_driver_branch": "570"}, "PASS"),
    ("nvidia-bad-min-branch", "nvidia", {"nvidia_min_driver_branch": "57x"}, "FAIL"),
    ("nvidia-unknown-source", "nvidia", {"nvidia_source": "ppa"}, "FAIL"),
    ("nvidia-runfile-other-branch", "nvidia", {"nvidia_runfile_version": "590.48.01"}, "FAIL"),
    ("nvidia-runfile-sha-ok", "nvidia", {"nvidia_runfile_sha256": "a" * 64}, "PASS"),
    ("nvidia-runfile-sha-bad", "nvidia", {"nvidia_runfile_sha256": "abc"}, "FAIL"),
    ("nvidia-runfile-http", "nvidia", {"nvidia_runfile_base": "http://download.nvidia.com/XFree86/Linux-x86_64"}, "FAIL"),
    ("nvidia-runfile-open-modules", "nvidia", {"nvidia_runfile_options": ["--silent", "--kernel-module-type=open"]}, "FAIL"),
    ("nvidia-runfile-option-injection", "nvidia", {"nvidia_runfile_options": ["--silent; reboot"]}, "FAIL"),
    ("nvidia-download-traversal", "nvidia", {"nvidia_runfile_download_dir": "/var/cache/../../etc"}, "FAIL"),
    ("nodejs-defaults", "nodejs", {}, "PASS"),
    ("nodejs-too-old", "nodejs", {"nodejs_major": 20, "nodejs_min_version": "20.19.0"}, "FAIL"),
    ("nodejs-plain-http", "nodejs", {"nodejs_repo_uri": "http://deb.nodesource.com/node_22.x"}, "FAIL"),
    ("wol-plain", "wol_target", {"wol_interface": "enp3s0", "wol_mac": "AA:bb:cc:00:11:22"}, "PASS"),
    ("wol-dash-escaped", "wol_target", {"wol_interface": "br-lan"}, "PASS"),
    ("wol-injection", "wol_target", {"wol_interface": "eth0;reboot"}, "FAIL"),
    ("wol-leading-dash", "wol_target", {"wol_interface": "-s"}, "FAIL"),
    ("wol-bad-mac", "wol_target", {"wol_interface": "eth0", "wol_mac": "aa:bb:cc:dd:ee"}, "FAIL"),
]


def test_role_input_validation(tmp_path: Path) -> None:
    plays = []
    for case_id, role, case_vars, _ in VALIDATION_CASES:
        plays.append(
            {
                "name": case_id,
                "hosts": "localhost",
                "connection": "local",
                "gather_facts": False,
                "become": False,
                "vars_files": [str(GROUP_VARS)],
                "vars": {"hermclaw_orchestrator_ip": "192.168.178.225"},
                "tasks": [
                    {
                        "name": "validate",
                        "block": [
                            {
                                "name": "run the validation",
                                "ansible.builtin.include_role": {"name": role, "tasks_from": "validate.yml"},
                                "vars": case_vars,
                            },
                            {
                                "name": "report",
                                "ansible.builtin.debug": {
                                    "msg": f"HERMCLAW_CASE {case_id} PASS {{{{ wol_target_instance | default('-') }}}}"
                                },
                            },
                        ],
                        "rescue": [{"name": "report", "ansible.builtin.debug": {"msg": f"HERMCLAW_CASE {case_id} FAIL -"}}],
                    }
                ],
            }
        )
    pb = tmp_path / "validate.yml"
    pb.write_text(yaml.safe_dump(plays, width=1000), encoding="utf-8")
    env = _ansible_env(tmp_path)
    env["ANSIBLE_ROLES_PATH"] = str(ROLES_DIR)
    res = _run([str(ANSIBLE_PLAYBOOK), "-i", "localhost,", str(pb)], env=env, cwd=tmp_path, timeout=600)
    assert res.returncode == 0, res.stdout[-4000:] + res.stderr[-2000:]
    outcomes = {m.group(1): (m.group(2), m.group(3)) for m in re.finditer(r"HERMCLAW_CASE (\S+) (PASS|FAIL) (\S+)", res.stdout)}
    wrong = {case_id: outcomes.get(case_id) for case_id, _, _, want in VALIDATION_CASES if outcomes.get(case_id, ("?",))[0] != want}
    assert not wrong, f"unexpected validation outcomes: {wrong}"
    # systemd-escape semantics for the Wake-on-LAN instance: '-' becomes \x2d
    assert outcomes["wol-plain"][1] == "enp3s0"
    assert outcomes["wol-dash-escaped"][1] == "br\\x2dlan"
    if shutil.which("systemd-escape"):
        assert _run(["systemd-escape", "br-lan"], cwd=tmp_path).stdout.strip() == outcomes["wol-dash-escaped"][1]


def test_role_defaults_per_group(tmp_path: Path) -> None:
    """Group-dependent defaults: sudo allowlist on workers only, port plan rendered for check-host.sh."""
    inventory: dict[str, Any] = {
        "all": {
            "vars": {"ansible_connection": "local", "hermclaw_orchestrator_ip": "192.168.178.225"},
            "children": {
                "orchestrator": {"hosts": {"o": {}}},
                "webui": {"hosts": {"w": {}}},
                "execution_workers": {"hosts": {"e": {}}},
                "model_workers": {"hosts": {"m": {}}},
            },
        }
    }
    inv = tmp_path / "hosts.yml"
    inv.write_text(yaml.safe_dump(inventory), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    play = [
        {
            "name": "dump defaults",
            "hosts": "all",
            "gather_facts": False,
            "become": False,
            "vars_files": [str(GROUP_VARS), str(ROLES_DIR / "common" / "defaults" / "main.yml")],
            "tasks": [
                {
                    "name": "dump",
                    "ansible.builtin.copy": {
                        "dest": f"{out}/{{{{ inventory_hostname }}}}.json",
                        "content": (
                            "{{ {'sudo': common_ssh_runtime_sudo_commands, 'plan': common_preflight_port_plan,"
                            " 'shared': common_shared_host | bool} | to_json }}"
                        ),
                        "mode": "0644",
                    },
                }
            ],
        }
    ]
    pb = tmp_path / "dump.yml"
    pb.write_text(yaml.safe_dump(play, width=1000), encoding="utf-8")
    res = _run([str(ANSIBLE_PLAYBOOK), "-i", str(inv), str(pb)], env=_ansible_env(tmp_path), cwd=tmp_path)
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-2000:]
    dumps = {h: json.loads((out / f"{h}.json").read_text(encoding="utf-8")) for h in "owem"}
    assert dumps["o"]["sudo"] == [] and dumps["w"]["sudo"] == []
    assert dumps["e"]["sudo"] == dumps["m"]["sudo"] == ["/usr/bin/systemctl suspend"]
    # the sudo allowlist matches what the runtime is configured to use (config/hosts.example.yaml)
    hosts_cfg = yaml.safe_load((REPO / "config" / "hosts.example.yaml").read_text(encoding="utf-8"))
    for host in hosts_cfg["hosts"]:
        if host.get("role") in {"execution_worker", "model_worker"}:
            assert host["ssh"]["allow_sudo_commands"] == dumps["e"]["sudo"]
            assert host["idle_sleep_command"] == "sudo " + dumps["e"]["sudo"][0]
    # the plan handed to check-host.sh equals its built-in table
    text = (SCRIPTS_DIR / "check-host.sh").read_text(encoding="utf-8")
    builtin = re.search(r"PORT_PLAN=\$\(cat <<'EOF'\n(.*?)\nEOF", text, re.S).group(1)  # type: ignore[union-attr]
    assert sorted(dumps["o"]["plan"].splitlines()) == sorted(builtin.splitlines())
    # D-013: only the web host is a shared host (time setup, account/directory guard)
    assert {h: d["shared"] for h, d in dumps.items()} == {"o": False, "w": True, "e": False, "m": False}

    # the plan follows the nginx ports of the web host (host vars; vars_files here are play vars, so the group_vars
    # ports themselves are overridden through nginx_*_port - test_deploy_infra.py covers the real precedence chain),
    # the shared flag can be switched off per host
    inventory["all"]["children"]["webui"]["hosts"]["w"] = {"nginx_listen_port": 9080, "nginx_tls_port": 9443}
    inventory["all"]["children"]["webui"]["hosts"]["w2"] = {}
    inv.write_text(yaml.safe_dump(inventory), encoding="utf-8")
    res = _run([str(ANSIBLE_PLAYBOOK), "-i", str(inv), str(pb)], env=_ansible_env(tmp_path), cwd=tmp_path)
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-2000:]
    w = json.loads((out / "w.json").read_text(encoding="utf-8"))
    assert {"webui 9080 nginx-http nginx", "webui 9443 nginx-https nginx"} <= set(w["plan"].splitlines())
    assert not any(line.startswith("webui 8080") for line in w["plan"].splitlines())
    assert {"webui 8080 nginx-http nginx", "webui 8443 nginx-https nginx"} <= set(
        json.loads((out / "w2.json").read_text(encoding="utf-8"))["plan"].splitlines()
    )


# --------------------------------------------------------------------------------------------- check-host behaviour


FAKE_TRIXIE = 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\n'


@pytest.fixture
def listener() -> Iterator[tuple[int, str]]:
    """A real listening TCP socket owned by a separate process with a recognisable command line."""
    code = (
        "import socket, sys, time\n"
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
        "print(s.getsockname()[1], flush=True); time.sleep(120)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, "hermclaw-preflight-marker"], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        port = int(proc.stdout.readline())
        comm = Path(f"/proc/{proc.pid}/comm").read_text(encoding="utf-8").strip()
        yield port, comm
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _check_host_with(
    tmp_path: Path, plan: str, *args: str, os_release: str = FAKE_TRIXIE, env: Mapping[str, str] | None = None
) -> tuple[int, dict[str, Any]]:
    rel = tmp_path / "os-release"
    rel.write_text(os_release, encoding="utf-8")
    argv = [
        "bash",
        str(SCRIPTS_DIR / "check-host.sh"),
        "--role",
        "orchestrator",
        "--json",
        "--os-release",
        str(rel),
        "--min-cpus",
        "0",
        "--min-ram-mb",
        "0",
        "--min-disk-gb",
        "0",
        *args,
    ]
    if plan:
        plan_file = tmp_path / "plan"
        plan_file.write_text(plan, encoding="utf-8")
        argv += ["--port-plan", str(plan_file)]
    res = _run(argv, env=env, cwd=REPO, timeout=120)
    return res.returncode, json.loads(res.stdout)


def test_check_host_accepts_debian_13_and_rejects_others(tmp_path: Path) -> None:
    code, report = _check_host_with(tmp_path, "")
    checks = {c["check"]: c for c in report["checks"]}
    assert checks["os"]["status"] == "OK" and "Debian 13 (trixie)" in checks["os"]["message"]
    code, report = _check_host_with(tmp_path, "", os_release='ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n')
    checks = {c["check"]: c for c in report["checks"]}
    assert code == 1 and checks["os"]["status"] == "FAIL" and "VERSION_ID=12" in checks["os"]["message"]
    code, report = _check_host_with(tmp_path, "", os_release='ID=ubuntu\nVERSION_ID="13"\n')
    assert code == 1 and {c["check"]: c for c in report["checks"]}["os"]["status"] == "FAIL"


def test_check_host_ram_minimum_tolerates_kernel_reserved_memory(tmp_path: Path) -> None:
    """MemTotal is below the installed RAM: the RAM check passes from 90 % of the minimum on (finding 5)."""
    match = re.search(r"^MemTotal:\s+(\d+) kB$", Path("/proc/meminfo").read_text(encoding="utf-8"), re.M)
    assert match is not None
    ram_mb = int(match.group(1)) // 1024  # what check-host.sh measures

    def ram_check(minimum: int) -> tuple[int, dict[str, str]]:
        code, report = _check_host_with(tmp_path, "", "--min-ram-mb", str(minimum))
        return code, {c["check"]: c for c in report["checks"]}["ram"]

    # MemTotal at 93 % of the minimum (e.g. 7618 MiB on a host with 8192 MiB installed): passes
    minimum = ram_mb * 100 // 93
    assert 92.9 < ram_mb * 100 / minimum < 93.1
    _, check = ram_check(minimum)
    assert check["status"] == "OK", check
    assert check["message"] == f"{ram_mb} MiB RAM (minimum {minimum}, 90 % tolerance)"
    # MemTotal at 85 % of the minimum: fails, the message names the bound
    minimum = -(-ram_mb * 100 // 85)
    assert 84.9 < ram_mb * 100 / minimum <= 85.0
    code, check = ram_check(minimum)
    assert code == 1 and check["status"] == "FAIL", check
    floor = -(-minimum * 90 // 100)
    assert check["message"] == f"{ram_mb} MiB RAM, minimum {minimum} (90 % tolerance: at least {floor} MiB MemTotal)"
    # exactly at the bound and one MiB above it
    boundary = ram_mb * 10 // 9
    assert ram_check(boundary)[1]["status"] == "OK"
    assert ram_check(boundary + 1)[1]["status"] == "FAIL"
    # without an override the role minimum (installed RAM, Table 2.1) applies with the same tolerance
    res = _check_host("--role", "webui", "--json")
    check = {c["check"]: c for c in json.loads(res.stdout)["checks"]}["ram"]
    assert check["message"].endswith("MiB RAM (minimum 1024, 90 % tolerance)") or "minimum 1024 (90 % tolerance" in check["message"]
    script = (SCRIPTS_DIR / "check-host.sh").read_text(encoding="utf-8")
    assert "readonly RAM_TOLERANCE_PCT=90" in script and 'orchestrator) echo "4 8192 20"' in script


def test_check_host_detects_port_conflicts(tmp_path: Path, listener: tuple[int, str]) -> None:
    port, comm = listener

    def port_check(expect: str, *args: str) -> tuple[int, dict[str, str]]:
        code, report = _check_host_with(tmp_path, f"# test plan\norchestrator {port} demo {expect}\n", *args)
        assert report["failures"] == sum(1 for c in report["checks"] if c["status"] == "FAIL")
        return code, {c["check"]: c for c in report["checks"]}[f"port:{port}"]

    # a port held by a foreign process is a conflict (never overwrite existing services)
    code, check = port_check("nginx")
    assert check["status"] == "FAIL" and "CONFLICT" in check["message"] and comm[:15] in check["message"]
    assert code == 1
    # a generic interpreter only counts when its command line names the service
    code, check = port_check(f"{comm}~hermclaw-api")
    assert check["status"] == "FAIL", check
    code, check = port_check(f"nginx,{comm}~hermclaw-preflight-marker")
    assert check["status"] == "OK" and "expected service" in check["message"], check
    # an explicit override downgrades the conflict to a warning
    code, check = port_check("nginx", "--allow-conflicts")
    assert check["status"] == "WARN" and "conflict allowed" in check["message"]
    # the Ansible role hands the plan over through the environment
    rel = tmp_path / "os-release-env"
    rel.write_text(FAKE_TRIXIE, encoding="utf-8")
    res = _run(
        ["bash", str(SCRIPTS_DIR / "check-host.sh"), "--role", "webui", "--json", "--os-release", str(rel)],
        env={**os.environ, "HERMCLAW_PORT_PLAN": f"webui {port} demo nginx\n"},
        cwd=REPO,
    )
    checks = {c["check"]: c for c in json.loads(res.stdout)["checks"]}
    assert checks[f"port:{port}"]["status"] == "FAIL" and "port:80" not in checks


@pytest.mark.parametrize(
    "plan",
    [
        "orchestrator 8000 api",
        "orchestrator 70000 api x",
        "Orchestrator 80 api x",
        "orchestrator 80 api x;rm",
        "orchestrator 80 api x extra",
    ],
)
def test_check_host_rejects_invalid_port_plans(tmp_path: Path, plan: str) -> None:
    plan_file = tmp_path / "plan"
    plan_file.write_text(plan + "\n", encoding="utf-8")
    res = _run(["bash", str(SCRIPTS_DIR / "check-host.sh"), "--role", "orchestrator", "--port-plan", str(plan_file)], cwd=REPO)
    assert res.returncode == 64, res.stdout + res.stderr
    missing = _run(["bash", str(SCRIPTS_DIR / "check-host.sh"), "--role", "webui", "--port-plan", str(tmp_path / "nope")], cwd=REPO)
    assert missing.returncode == 64


# --------------------------------------------------------------------------------------------- runtime compatibility


def test_rendered_worker_env_is_accepted_by_the_worker_daemons(tmp_path: Path) -> None:
    etc = tmp_path / "etc-hermclaw"
    etc.mkdir()
    shutil.copy(REPO / "config" / "capabilities.example.yaml", etc / "capabilities.yaml")
    shutil.copy(REPO / "config" / "policies.example.yaml", etc / "policies.yaml")
    out = _render(tmp_path, tmp_path / "workers", _render_vars({"hermclaw_config_dir": str(etc)}))
    from hermclaw.contracts.common import WorkerKind
    from worker.common.settings import WorkerDaemonSettings

    for kind, worker_id in ((WorkerKind.execution, "exec-222"), (WorkerKind.model, "model-224")):
        env: dict[str, str] = {}
        for line in out[f"systemd/worker-{kind.value}.env"].read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                env[key] = _systemd_env_value(line)
        env["WORKER_ID"] = worker_id
        env["CREDENTIALS_DIRECTORY"] = str(tmp_path / "creds")
        settings = WorkerDaemonSettings.from_env(env, default_kind=kind)
        assert settings.kind == kind and settings.worker_id == worker_id
        assert settings.bind_port == 8787 and settings.orchestrator_url == "http://192.168.178.225:8000"
        assert settings.token_file == tmp_path / "creds" / "worker-token"
        assert str(settings.data_dir) == "/var/lib/hermclaw/worker"
        if kind == WorkerKind.model:
            assert settings.ollama_url == "http://127.0.0.1:11434"


def test_credential_helper_with_real_systemd_creds(tmp_path: Path) -> None:
    """hermclaw-credential.sh against the real systemd-creds (null key without TPM: no host state)."""
    if shutil.which("systemd-creds") is None:
        pytest.skip("systemd-creds not installed")
    store = tmp_path / "credstore"
    env = {**os.environ, "HERMCLAW_CREDSTORE_DIR": str(store), "HERMCLAW_CREDS_WITH_KEY": "auto-initrd"}
    probe = _run(
        ["systemd-creds", "encrypt", "--with-key=auto-initrd", "--name=probe", "-", str(tmp_path / "p")], input_text="x", cwd=tmp_path
    )
    if probe.returncode != 0:
        pytest.skip(f"systemd-creds cannot encrypt here: {probe.stderr.strip()[:200]}")
    res = _credential(env, "set", "gitlab-token", stdin="glpat-REALSECRET-42\n")
    assert res.returncode == 0, res.stderr
    target = store / "hermclaw-gitlab-token"
    assert b"REALSECRET" not in target.read_bytes()
    assert "REALSECRET" not in res.stdout + res.stderr
    dec = _run(["systemd-creds", "decrypt", "--name=gitlab-token", str(target), "-"], cwd=tmp_path)
    assert dec.returncode == 0 and dec.stdout == "glpat-REALSECRET-42"
    assert _credential(env, "check", "gitlab-token").stdout.strip() == "OK gitlab-token"
    # the embedded name binds the file to its credential id: a renamed copy is refused
    shutil.copy(target, store / "hermclaw-other-token")
    assert _credential(env, "check", "other-token").returncode == 65
    res = _credential(env, "generate", "worker-token-exec-222")
    assert res.returncode == 0, res.stderr
    dec = _run(
        ["systemd-creds", "decrypt", "--name=worker-token-exec-222", str(store / "hermclaw-worker-token-exec-222"), "-"], cwd=tmp_path
    )
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", dec.stdout), "32 random bytes, base64url without padding"
