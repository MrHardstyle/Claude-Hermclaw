"""P32 – deployment automation without the hosts (BLOCKER-001): playbook structure, lint, role input validation, the
configuration strategy, credential and enablement checks, the operator CLI (with a fake ``systemd-run``), the unit
tweaks of the deployment, the self-signed TLS certificate and the final nginx site under a real nginx.

The roles' task files are run for real by ``ansible-playbook`` on localhost (``include_role ... tasks_from``) against
temporary directories; the converge of the complete playbook lives in ``test_deploy_converge.py``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tarfile
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

from tests.integration.test_bootstrap_infra import (
    ANSIBLE_PLAYBOOK,
    EXAMPLE_INVENTORY,
    GROUP_VARS,
    PLAYBOOKS_DIR,
    REPO,
    ROLES_DIR,
    VENV_BIN,
    _ansible_env,
    _render,
    _render_vars,
    _run,
)

pytestmark = pytest.mark.integration

DEPLOY_ROLES = ["hermclaw_release", "hermclaw_config", "hermclaw_orchestrator", "hermclaw_worker", "hermclaw_ui", "hermclaw_verify"]
# Settings operators set in the inventory (all.vars, group vars) that keep a shared name: role defaults of every role
# that reads them, never playbook group_vars (those beat all.vars and the group vars of hosts.yml, findings 15/27).
SHARED_ROLE_SETTINGS: dict[str, list[str]] = {
    "hermclaw_keep_releases": ["hermclaw_release", "hermclaw_ui"],
    "hermclaw_sandbox_image_version": ["hermclaw_config", "hermclaw_release", "hermclaw_worker"],
    "hermclaw_timezone": ["common"],
}
NGINX_IMAGE = "docker.io/library/nginx:1.28"
BUILD = REPO / "scripts" / "deploy" / "build-release.sh"


# --------------------------------------------------------------------------------------------- helpers


def _env(tmp_path: Path) -> dict[str, str]:
    env = _ansible_env(tmp_path)
    env["ANSIBLE_ROLES_PATH"] = str(ROLES_DIR)
    return env


def _playbook(
    tmp_path: Path,
    plays: list[dict[str, Any]],
    extra: dict[str, Any],
    *,
    inventory: Path | None = None,
    args: tuple[str, ...] = (),
    path_prefix: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    name = uuid.uuid4().hex[:8]
    pb = tmp_path / f"play-{name}.yml"
    pb.write_text(yaml.safe_dump(plays, width=1000), encoding="utf-8")
    ev = tmp_path / f"extra-{name}.json"
    ev.write_text(json.dumps(extra), encoding="utf-8")
    inv = str(inventory) if inventory else "localhost,"
    env = _env(tmp_path)
    if path_prefix is not None:  # test doubles of system tools (systemctl, systemd-run, ...)
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    return _run([str(ANSIBLE_PLAYBOOK), "-i", inv, str(pb), "-e", f"@{ev}", *args], env=env, cwd=tmp_path, timeout=900)


def _local_play(tasks: list[dict[str, Any]], hosts: str = "localhost") -> list[dict[str, Any]]:
    return [
        {
            "name": "deploy test",
            "hosts": hosts,
            "connection": "local",
            "gather_facts": False,
            "become": False,
            "vars_files": [str(GROUP_VARS)],
            "tasks": tasks,
        }
    ]


def _include(role: str, tasks_from: str, **kw: Any) -> dict[str, Any]:
    return {"name": f"{role} {tasks_from}", "ansible.builtin.include_role": {"name": role, "tasks_from": tasks_from, "public": True}, **kw}


RECAP = re.compile(r"^(\S+)\s+: ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", re.M)


def _changed(res: subprocess.CompletedProcess[str]) -> int:
    return sum(int(m.group(3)) for m in RECAP.finditer(res.stdout))


# --------------------------------------------------------------------------------------------- playbooks


@pytest.mark.parametrize("playbook", ["deploy.yml", "site.yml"])
def test_deploy_playbooks_pass_the_syntax_check(playbook: str, tmp_path: Path) -> None:
    res = _run([str(ANSIBLE_PLAYBOOK), "-i", str(EXAMPLE_INVENTORY), "--syntax-check", f"playbooks/{playbook}"], env=_env(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr


def test_deploy_playbook_structure() -> None:
    plays = yaml.safe_load((PLAYBOOKS_DIR / "deploy.yml").read_text(encoding="utf-8"))
    pre = plays[0]
    assert pre["hosts"] == "orchestrator:webui:execution_workers:model_workers" and pre["any_errors_fatal"] is True
    assert [t["ansible.builtin.include_role"]["tasks_from"] for t in pre["tasks"]] == ["preflight.yml", "data.yml", "mark_deployed.yml"], (
        "read-only checks first; the only write of the preflight play (the deployed fact) after every check passed"
    )
    from tests.integration.test_bootstrap_infra import COMMAND_MODULES, _iter_tasks, _module_of

    read_only = {"ansible.builtin.stat", "ansible.builtin.assert", "ansible.builtin.set_fact", "ansible.builtin.include_tasks",
                 "ansible.builtin.import_tasks", "ansible.builtin.uri", "ansible.builtin.slurp", "ansible.builtin.fail",
                 *COMMAND_MODULES}  # fmt: skip
    for name in ("preflight.yml", "orchestrator_first.yml", "credentials.yml", "validate.yml"):
        for task in _iter_tasks(yaml.safe_load((ROLES_DIR / "hermclaw_release" / "tasks" / name).read_text(encoding="utf-8"))):
            module = _module_of(task)
            assert module is None or module in read_only, f"{name}: {task.get('name')!r} ({module}) is not read-only"
            assert module not in COMMAND_MODULES or task.get("changed_when") is False, f"{name}: {task.get('name')!r}"
    preflight = (ROLES_DIR / "hermclaw_release" / "tasks" / "preflight.yml").read_text(encoding="utf-8")
    assert "orchestrator_first.yml" in preflight, "workers and UI never get ahead of the orchestrator"

    by_hosts = {p["hosts"]: [r["role"] for r in p.get("roles", [])] for p in plays[1:]}
    order = [p["hosts"] for p in plays[1:]]
    assert order == ["orchestrator", "execution_workers", "model_workers", "webui", "orchestrator"], "orchestrator first, doctor last"
    assert [r["role"] for r in plays[1]["roles"]] == ["systemd_skeleton", "hermclaw_release", "hermclaw_config", "hermclaw_orchestrator"]
    assert plays[1]["any_errors_fatal"] is True, "a failed orchestrator (schema change) stops workers and UI too"
    assert (
        by_hosts["execution_workers"]
        == by_hosts["model_workers"]
        == ["systemd_skeleton", "hermclaw_release", "hermclaw_config", "hermclaw_worker"]
    )
    assert by_hosts["webui"] == ["nginx", "hermclaw_ui"]
    assert [r["role"] for r in plays[-1]["roles"]] == ["hermclaw_verify"]
    assert all(p.get("become") is True for p in plays)
    site = yaml.safe_load((PLAYBOOKS_DIR / "site.yml").read_text(encoding="utf-8"))
    assert [p["ansible.builtin.import_playbook"] for p in site] == ["bootstrap.yml", "deploy.yml"]
    for role in DEPLOY_ROLES:
        meta = yaml.safe_load((ROLES_DIR / role / "meta" / "main.yml").read_text(encoding="utf-8"))
        assert meta["galaxy_info"]["platforms"] == [{"name": "Debian", "versions": ["trixie"]}]
        defaults = yaml.safe_load((ROLES_DIR / role / "defaults" / "main.yml").read_text(encoding="utf-8"))
        assert all(k.startswith(role + "_") or k in SHARED_ROLE_SETTINGS for k in defaults), (
            f"{role}: role variables carry the role prefix (shared inventory settings: SHARED_ROLE_SETTINGS)"
        )


def test_deploy_tasks_are_idempotent_by_construction() -> None:
    """Every command/shell task has changed_when or creates; secrets are no_log; no ignore_errors anywhere."""
    from tests.integration.test_bootstrap_infra import COMMAND_MODULES, _iter_tasks, _module_of

    count = 0
    for role in DEPLOY_ROLES:
        for f in sorted((ROLES_DIR / role / "tasks").glob("*.yml")):
            for task in _iter_tasks(yaml.safe_load(f.read_text(encoding="utf-8"))):
                module = _module_of(task)
                if module is None:
                    continue
                count += 1
                where = f"{f.relative_to(REPO)}: {task.get('name')!r}"
                assert module.startswith("ansible.builtin.") and "ignore_errors" not in task, where
                if module in COMMAND_MODULES:
                    params = task[module] if isinstance(task[module], dict) else {}
                    assert "changed_when" in task or "creates" in params or "removes" in params, where
                blob = json.dumps(task[module])
                if "token-create" in blob or "_key_src" in blob:
                    assert task.get("no_log") is True, f"{where} handles a secret"
    assert count > 120


def test_ansible_lint_production_profile_for_the_deployment(tmp_path: Path) -> None:
    lint = VENV_BIN / "ansible-lint"
    if not lint.exists():
        pytest.skip("ansible-lint not installed in the venv")
    env = _env(tmp_path)
    env["PATH"] = f"{VENV_BIN}{os.pathsep}{env['PATH']}"
    roles = [f"roles/{r}" for r in [*DEPLOY_ROLES, "nginx", "systemd_skeleton", "common"]]
    res = _run(
        [str(lint), "--offline", "--profile=production", "--format=pep8", "playbooks/deploy.yml", "playbooks/site.yml", *roles],
        env=env,
        timeout=900,
    )
    assert res.returncode == 0, res.stdout[-4000:] + res.stderr[-4000:]


def test_build_script_is_shellcheck_clean() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed")
    res = _run([shellcheck, str(BUILD)], cwd=REPO)
    assert res.returncode == 0, res.stdout + res.stderr
    assert os.access(BUILD, os.X_OK)


# --------------------------------------------------------------------------------------------- inventory settings


def _role_defaults(role: str) -> dict[str, Any]:
    return yaml.safe_load((ROLES_DIR / role / "defaults" / "main.yml").read_text(encoding="utf-8")) or {}


def _group_level_inventory_names(text: str) -> set[str]:
    """Variables an inventory sets or mentions as a commented example (``# name: value``) at group level.

    Group level = the ``vars`` of ``all`` or of a group (also nested), never below ``hosts`` (host vars beat the
    playbook group_vars anyway).
    """
    key_re = re.compile(r"^( *)([A-Za-z_][A-Za-z0-9_-]*):(?:\s|$)")
    example_re = re.compile(r"^( *)# ([a-z_][a-z0-9_]*):(?:\s|$)")  # exactly one space: no nested keys
    names: set[str] = set()
    stack: list[tuple[int, str]] = []

    def group_level(path: list[str]) -> bool:
        return len(path) >= 2 and path[-1] == "vars" and "hosts" not in path

    for line in text.splitlines():
        if m := key_re.match(line):
            indent = len(m.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if group_level([k for _, k in stack]):
                names.add(m.group(2))
            stack.append((indent, m.group(2)))
        elif c := example_re.match(line):
            if group_level([k for i, k in stack if i < len(c.group(1))]):
                names.add(c.group(2))
    return names


def test_shared_inventory_settings_are_role_defaults_that_agree() -> None:
    """Every role that reads a shared inventory setting defines it with the same default; group_vars do not."""
    group_vars = yaml.safe_load(GROUP_VARS.read_text(encoding="utf-8"))
    roles = sorted(p.name for p in ROLES_DIR.iterdir() if (p / "defaults" / "main.yml").exists())
    for name, owners in SHARED_ROLE_SETTINGS.items():
        assert name not in group_vars, f"{name} in playbooks/group_vars/all.yml would beat all.vars and group vars"
        word = re.compile(rf"\b{name}\b")
        readers = sorted(
            role
            for role in roles
            if any(
                word.search(f.read_text(encoding="utf-8"))
                for sub in ("tasks", "templates", "handlers", "vars", "meta")
                for f in (ROLES_DIR / role / sub).rglob("*")
                if f.is_file()
            )
        )
        assert readers == owners, f"{name}: read by {readers}, SHARED_ROLE_SETTINGS says {owners}"
        defined = {role: _role_defaults(role)[name] for role in roles if name in _role_defaults(role)}
        assert sorted(defined) == owners, f"{name}: every role that reads it defines a default ({sorted(defined)})"
        assert len({json.dumps(v) for v in defined.values()}) == 1, f"{name}: role defaults differ: {defined}"
        for playbook in PLAYBOOKS_DIR.glob("*.yml"):
            assert not word.search(playbook.read_text(encoding="utf-8")), f"{playbook.name} reads {name} outside its roles"
    assert _role_defaults("hermclaw_release")["hermclaw_keep_releases"] == 3
    assert _role_defaults("hermclaw_worker")["hermclaw_sandbox_image_version"] == "1"
    assert _role_defaults("common")["hermclaw_timezone"] == "Europe/Berlin"
    # a role variable without the role prefix is a shared setting from the list above (ansible-lint noqa)
    for role in roles:
        stray = [k for k in _role_defaults(role) if not k.startswith(role + "_") and k not in SHARED_ROLE_SETTINGS]
        assert not stray, f"{role}: {stray}"


def test_inventory_example_settings_are_not_playbook_group_vars() -> None:
    """A setting the inventory example offers at group level must take effect there (findings 15/27)."""
    text = EXAMPLE_INVENTORY.read_text(encoding="utf-8")
    names = _group_level_inventory_names(text)
    inventory = yaml.safe_load(text)
    assert set(inventory["all"]["vars"]) <= names, "the parser sees every real all.vars key"
    assert {"hermclaw_keep_releases", "hermclaw_sandbox_image_version", "hermclaw_timezone"} <= names, "commented examples"
    assert {"hermclaw_config_overrides", "hermclaw_config_host_overrides"} <= names
    assert not names & {"policies", "exec-222", "wol_mac", "hermclaw_host_id", "nginx_tls_enabled"}, "nested keys, host vars"
    group_vars = yaml.safe_load(GROUP_VARS.read_text(encoding="utf-8"))
    shadowed = sorted(names & set(group_vars))
    assert not shadowed, f"set in all.vars/group vars of hosts.yml but beaten by playbooks/group_vars/all.yml: {shadowed}"
    # the parser itself: group vars of a (nested) group count, host vars and nested mappings do not
    sample = (
        "all:\n  vars:\n    a_one: 1\n    # a_two: 2\n    #   a_nested: 3\n    a_map:\n      a_inner: 4\n"
        "  children:\n    g:\n      vars:\n        # g_one: 1\n      hosts:\n        h:\n          h_one: 1\n"
        "          # h_two: 2\n      children:\n        sub:\n          vars: {s_flow: 1}\n          hosts:\n"
        "            h2:\n              vars_like: 1\n"
    )
    assert _group_level_inventory_names(sample) == {"a_one", "a_two", "a_map", "g_one"}


def test_inventory_group_vars_take_effect_in_the_roles(tmp_path: Path) -> None:
    """ansible-inventory and a real play with the playbooks' group_vars: all.vars and group vars reach the roles."""
    inventory = {
        "all": {
            "vars": {
                "ansible_connection": "local",
                "hermclaw_keep_releases": 7,
                "hermclaw_timezone": "UTC",
                "hermclaw_release_root": "/inventory/dist",  # a playbook group_var: -e or host vars only
            },
            "children": {
                "orchestrator": {"hosts": {"o": {}}},
                "execution_workers": {"vars": {"hermclaw_sandbox_image_version": "2"}, "hosts": {"e": {}}},
            },
        }
    }
    inv = tmp_path / "hosts.yml"
    inv.write_text(yaml.safe_dump(inventory), encoding="utf-8")
    env = _env(tmp_path)

    def inventory_vars(host: str) -> dict[str, Any]:
        res = _run(
            [str(VENV_BIN / "ansible-inventory"), "-i", str(inv), "--playbook-dir", str(PLAYBOOKS_DIR), "--host", host],
            env=env,
            cwd=tmp_path,
        )
        assert res.returncode == 0, res.stdout[-2000:] + res.stderr[-2000:]
        host_vars: dict[str, Any] = json.loads(res.stdout)
        return host_vars

    e, o = inventory_vars("e"), inventory_vars("o")
    assert e["hermclaw_api_port"] == 8000, "--playbook-dir loads playbooks/group_vars/all.yml"
    assert e["hermclaw_keep_releases"] == o["hermclaw_keep_releases"] == 7
    assert e["hermclaw_sandbox_image_version"] == "2" and "hermclaw_sandbox_image_version" not in o
    assert e["hermclaw_timezone"] == "UTC"
    # negative control: a playbook group_var still beats all.vars
    assert e["hermclaw_release_root"] == "{{ playbook_dir }}/../../../dist"

    # the real precedence chain: playbook-adjacent group_vars (a copy of the real file) + the role defaults
    pbdir = tmp_path / "playbooks"
    (pbdir / "group_vars").mkdir(parents=True)
    shutil.copy(GROUP_VARS, pbdir / "group_vars" / "all.yml")
    out = tmp_path / "out"
    out.mkdir()
    content = (
        "{{ {'keep': hermclaw_keep_releases, 'image': hermclaw_sandbox_image_version, 'tz': hermclaw_timezone,"
        " 'root': hermclaw_release_root} | to_json }}"
    )
    play = [
        {
            "name": "dump the settings the roles see",
            "hosts": "all",
            "gather_facts": False,
            "become": False,
            "roles": ["common", "hermclaw_release", "hermclaw_config", "hermclaw_worker", "hermclaw_ui"],
            "post_tasks": [
                {
                    "name": "dump",
                    "ansible.builtin.copy": {"dest": f"{out}/{{{{ inventory_hostname }}}}.json", "content": content, "mode": "0644"},
                    "tags": ["dump"],
                }
            ],
        }
    ]
    pb = pbdir / "dump.yml"
    pb.write_text(yaml.safe_dump(play, width=1000), encoding="utf-8")

    def dump(inventory_file: Path, *args: str) -> dict[str, dict[str, Any]]:
        for f in out.glob("*.json"):
            f.unlink()
        res = _run([str(ANSIBLE_PLAYBOOK), "-i", str(inventory_file), str(pb), "--tags", "dump", *args], env=env, cwd=tmp_path)
        assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-2000:]
        return {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in out.glob("*.json")}

    seen = dump(inv)
    assert seen["e"]["keep"] == seen["o"]["keep"] == 7 and seen["e"]["tz"] == seen["o"]["tz"] == "UTC"
    assert seen["e"]["image"] == "2" and seen["o"]["image"] == "1", "group vars beat the role default"
    assert seen["o"]["root"] != "/inventory/dist" and seen["o"]["root"].endswith("/dist"), "playbook group_var wins"
    plain = tmp_path / "plain.yml"
    plain.write_text(
        yaml.safe_dump({"all": {"vars": {"ansible_connection": "local"}, "children": {"orchestrator": {"hosts": {"o": {}}}}}}),
        encoding="utf-8",
    )
    seen = dump(plain, "-e", "hermclaw_release_root=/extra/dist")
    assert seen["o"] == {"keep": 3, "image": "1", "tz": "Europe/Berlin", "root": "/extra/dist"}, "role defaults; -e wins"


# --------------------------------------------------------------------------------------------- input validation

VALIDATION_CASES: list[tuple[str, str, str, dict[str, Any], str]] = [
    ("release-defaults", "hermclaw_release", "validate.yml", {}, "PASS"),
    ("release-keep-0", "hermclaw_release", "validate.yml", {"hermclaw_keep_releases": 0}, "FAIL"),
    ("release-keep-1", "hermclaw_release", "validate.yml", {"hermclaw_keep_releases": 1}, "PASS"),
    ("release-dash-rc", "hermclaw_release", "validate.yml", {"hermclaw_version": "0.1.0-rc1"}, "FAIL"),
    ("release-float", "hermclaw_release", "validate.yml", {"hermclaw_version": 1.0}, "FAIL"),
    ("release-injection", "hermclaw_release", "validate.yml", {"hermclaw_version": "1.0;reboot"}, "FAIL"),
    ("release-post", "hermclaw_release", "validate.yml", {"hermclaw_version": "1.2.3.post1"}, "PASS"),
    ("release-keep-text", "hermclaw_release", "validate.yml", {"hermclaw_keep_releases": "all"}, "FAIL"),
    ("release-keep-huge", "hermclaw_release", "validate.yml", {"hermclaw_keep_releases": 100}, "FAIL"),
    ("release-traversal", "hermclaw_release", "validate.yml", {"hermclaw_releases_dir": "/opt/hermclaw/../etc"}, "FAIL"),
    ("release-relative-webroot", "hermclaw_release", "validate.yml", {"hermclaw_webroot": "var/www/x"}, "FAIL"),
    ("release-venv-elsewhere", "hermclaw_release", "validate.yml", {"hermclaw_venv": "/srv/venv"}, "FAIL"),
    ("release-ui-releases-is-webroot", "hermclaw_release", "validate.yml", {"hermclaw_ui_releases_dir": "/var/www/hermclaw-next"}, "FAIL"),
    ("release-toolchain-bad", "hermclaw_release", "validate.yml", {"hermclaw_sandbox_toolchains": ["python", "Bad Name"]}, "FAIL"),
    ("release-default-toolchain", "hermclaw_release", "validate.yml", {"hermclaw_sandbox_default_toolchain": "rust"}, "FAIL"),
    ("release-image-version", "hermclaw_release", "validate.yml", {"hermclaw_sandbox_image_version": "1;rm"}, "FAIL"),
    ("release-gate-newline", "hermclaw_release", "validate.yml", {"hermclaw_release_ssh_gate_allowlist": ["^ls$\n^.*$"]}, "FAIL"),
    ("release-gate-string", "hermclaw_release", "validate.yml", {"hermclaw_release_ssh_gate_allowlist": "^ls$"}, "FAIL"),
    ("release-gate-ok", "hermclaw_release", "validate.yml", {"hermclaw_release_ssh_gate_allowlist": ["^uptime$", "^df -h$"]}, "PASS"),
    ("orch-defaults", "hermclaw_orchestrator", "validate.yml", {}, "PASS"),
    ("orch-token-name", "hermclaw_orchestrator", "validate.yml", {"hermclaw_orchestrator_admin_token_credential": "Bad Name"}, "FAIL"),
    ("orch-litellm-link", "hermclaw_orchestrator", "validate.yml", {"hermclaw_orchestrator_litellm_link": "opt/litellm/venv"}, "FAIL"),
    ("orch-api-url", "hermclaw_orchestrator", "validate.yml", {"hermclaw_orchestrator_api_url": "https://x/ ; rm"}, "FAIL"),
    ("orch-units", "hermclaw_orchestrator", "validate.yml", {"hermclaw_orchestrator_units": ["sshd.service"]}, "FAIL"),
    ("orch-litellm-keep-0", "hermclaw_orchestrator", "validate.yml", {"hermclaw_orchestrator_litellm_keep": 0}, "FAIL"),
    ("nginx-tls-self-signed", "nginx", "validate.yml", {"hermclaw_web_server_name": "hermclaw.lan", "nginx_tls_enabled": True}, "PASS"),
    ("nginx-tls-no-name", "nginx", "validate.yml", {"hermclaw_web_server_name": "_", "nginx_tls_enabled": True}, "FAIL"),
    (
        "nginx-tls-ip-only",
        "nginx",
        "validate.yml",
        {"hermclaw_web_server_name": "_", "nginx_tls_enabled": True, "ansible_host": "192.168.178.223"},
        "PASS",
    ),
    (
        "nginx-tls-provided-missing",
        "nginx",
        "validate.yml",
        {"hermclaw_web_server_name": "hermclaw.lan", "nginx_tls_enabled": True, "nginx_tls_source": "provided"},
        "FAIL",
    ),
    (
        "nginx-tls-provided",
        "nginx",
        "validate.yml",
        {
            "hermclaw_web_server_name": "hermclaw.lan",
            "nginx_tls_enabled": True,
            "nginx_tls_source": "provided",
            "nginx_tls_cert_src": "files/web.crt",
            "nginx_tls_key_src": "files/web.key",
        },
        "PASS",
    ),
    ("nginx-tls-same-port", "nginx", "validate.yml", {"hermclaw_web_server_name": "hermclaw.lan", "nginx_tls_port": 8080}, "FAIL"),
    ("nginx-tls-source", "nginx", "validate.yml", {"hermclaw_web_server_name": "hermclaw.lan", "nginx_tls_source": "acme"}, "FAIL"),
    ("nginx-tls-days", "nginx", "validate.yml", {"hermclaw_web_server_name": "hermclaw.lan", "nginx_tls_self_signed_days": 825}, "FAIL"),
    (
        "nginx-csp-quote",
        "nginx",
        "validate.yml",
        {"hermclaw_web_server_name": "hermclaw.lan", "nginx_csp": "default-src 'self'\" always; x"},
        "FAIL",
    ),
    (
        "nginx-csp-newline",
        "nginx",
        "validate.yml",
        {"hermclaw_web_server_name": "hermclaw.lan", "nginx_csp": "default-src 'self';\nreturn 200"},
        "FAIL",
    ),
    ("nginx-hsts", "nginx", "validate.yml", {"hermclaw_web_server_name": "hermclaw.lan", "nginx_hsts_max_age": "1y"}, "FAIL"),
]


def test_deploy_input_validation(tmp_path: Path) -> None:
    plays = []
    for case_id, role, tasks_from, case_vars, _ in VALIDATION_CASES:
        plays.append(
            {
                "name": case_id,
                "hosts": "localhost",
                "connection": "local",
                "gather_facts": False,
                "become": False,
                "vars_files": [str(GROUP_VARS)],
                "vars": {"hermclaw_orchestrator_ip": "192.168.178.225", "hermclaw_version": "0.1.0rc1"},
                "tasks": [
                    {
                        "name": "validate",
                        "block": [
                            {"name": "run", "ansible.builtin.include_role": {"name": role, "tasks_from": tasks_from}, "vars": case_vars},
                            {"name": "report", "ansible.builtin.debug": {"msg": f"HERMCLAW_CASE {case_id} PASS"}},
                        ],
                        "rescue": [{"name": "report", "ansible.builtin.debug": {"msg": f"HERMCLAW_CASE {case_id} FAIL"}}],
                    }
                ],
            }
        )
    pb = tmp_path / "validate.yml"
    pb.write_text(yaml.safe_dump(plays, width=1000), encoding="utf-8")
    res = _run([str(ANSIBLE_PLAYBOOK), "-i", "localhost,", str(pb)], env=_env(tmp_path), cwd=tmp_path, timeout=600)
    assert res.returncode == 0, res.stdout[-4000:] + res.stderr[-2000:]
    outcomes = dict(re.findall(r"HERMCLAW_CASE (\S+) (PASS|FAIL)", res.stdout))
    wrong = {c: outcomes.get(c) for c, _, _, _, want in VALIDATION_CASES if outcomes.get(c) != want}
    assert not wrong, f"unexpected validation outcomes: {wrong}"


# --------------------------------------------------------------------------------------------- configuration


def _config_inventory(tmp_path: Path, **host_vars: dict[str, Any]) -> Path:
    exec_vars = {"ansible_host": "10.9.0.22", "hermclaw_worker_id": "exec-222", "wol_mac": "52:54:00:aa:02:22", **host_vars.get("exec", {})}
    model_vars = {
        "ansible_host": "10.9.0.24",
        "hermclaw_worker_id": "model-224",
        "wol_mac": "52:54:00:aa:02:24",
        **host_vars.get("model", {}),
    }
    inventory = {
        "all": {
            "vars": {
                "ansible_connection": "local",
                "hermclaw_orchestrator_ip": "10.9.0.25",
                "hermclaw_lan_broadcast": "10.9.0.255",
                "hermclaw_version": "0.1.0rc1",
            },
            "children": {
                "orchestrator": {"hosts": {"o": {"ansible_host": "10.9.0.25", "hermclaw_host_id": "orchestrator-225"}}},
                "webui": {"hosts": {"w": {"ansible_host": "10.9.0.23", "hermclaw_host_id": "webui-223", **host_vars.get("web", {})}}},
                "execution_workers": {"hosts": {"e": exec_vars}},
                "model_workers": {"hosts": {"m": model_vars}},
                "gitlab": {"hosts": {"g": {"ansible_host": "10.9.0.26", "hermclaw_host_id": "gitlab-226"}}},
                "backup": {"hosts": {"b": {"ansible_host": "10.9.0.60", "hermclaw_host_id": "terra-60"}}},
            },
        }
    }
    inv = tmp_path / "hosts.yml"
    inv.write_text(yaml.safe_dump(inventory), encoding="utf-8")
    return inv


def _release_with_config(tmp_path: Path) -> Path:
    rel = tmp_path / "dist" / "release-0.1.0rc1" / "config"
    rel.mkdir(parents=True)
    for f in (REPO / "config").glob("*.example.yaml"):
        shutil.copy(f, rel / f.name)
    return tmp_path / "dist"


def _render_config(tmp_path: Path, extra: dict[str, Any], **host_vars: dict[str, Any]) -> tuple[subprocess.CompletedProcess[str], Path]:
    out = tmp_path / "etc"
    out.mkdir(exist_ok=True)
    tasks = [
        _include("hermclaw_config", "data.yml"),
        {
            "name": "write",
            "ansible.builtin.copy": {
                "content": "{{ hermclaw_config_rendered[item] }}",
                "dest": f"{out}/{{{{ item }}}}.yaml",
                "mode": "0644",
            },
            "loop": "{{ hermclaw_config_names }}",
        },
    ]
    res = _playbook(
        tmp_path,
        _local_play(tasks, hosts="o"),
        {"hermclaw_release_root": str(_release_with_config(tmp_path)), **extra},
        inventory=_config_inventory(tmp_path, **host_vars),
    )
    return res, out


def test_configuration_from_examples_inventory_and_overrides(tmp_path: Path) -> None:
    overrides = {
        "hermclaw_config_overrides": {
            "policies": {"coder": {"max_turns": 24}, "git": {"create_merge_request": True}},
            "models": {"litellm": {"request_timeout_seconds": 900}},
        },
        "hermclaw_config_host_overrides": {
            "exec-222": {"labels": {"lease_resources": "code-executor-222,gpu-x"}},
            "nas-27": {"address": "10.9.0.27", "role": "backup"},
        },
        "hermclaw_sandbox_image_version": "7",
    }
    res, out = _render_config(tmp_path, overrides)
    assert res.returncode == 0, res.stdout[-4000:]
    for name in ("hosts", "models", "policies", "capabilities", "logging", "databases"):
        text = (out / f"{name}.yaml").read_text(encoding="utf-8")
        assert text.startswith("# Managed by Hermclaw - generated by playbooks/deploy.yml"), name
        example = yaml.safe_load((REPO / "config" / f"{name}.example.yaml").read_text(encoding="utf-8"))
        data = yaml.safe_load(text)
        assert data.keys() == example.keys() or name == "hosts", name
    hosts = {h["id"]: h for h in yaml.safe_load((out / "hosts.yaml").read_text(encoding="utf-8"))["hosts"]}
    assert list(hosts)[-1] == "nas-27" and hosts["nas-27"]["address"] == "10.9.0.27", "new hosts are appended"
    assert hosts["orchestrator-225"]["address"] == "10.9.0.25" and hosts["gitlab-226"]["address"] == "10.9.0.26"
    assert hosts["terra-60"]["address"] == "10.9.0.60" and hosts["terra-60"]["ssh"]["user"] == "root", "example values are kept"
    for hid, ip, mac in (("exec-222", "10.9.0.22", "52:54:00:aa:02:22"), ("model-224", "10.9.0.24", "52:54:00:aa:02:24")):
        assert hosts[hid]["address"] == ip and hosts[hid]["worker_api"] == f"http://{ip}:8787"
        assert hosts[hid]["wake_on_lan"] == {**hosts[hid]["wake_on_lan"], "enabled": True, "mac": mac, "broadcast": "10.9.0.255"}
    assert hosts["exec-222"]["labels"]["lease_resources"] == "code-executor-222,gpu-x"
    # D-013: nginx's HTTP port on the shared .223 (hermclaw_webui_http_port), /healthz also answers with TLS on
    assert hosts["webui-223"]["services"] == [{"name": "nginx", "port": 8080, "http_path": "/healthz"}]
    policies = yaml.safe_load((out / "policies.yaml").read_text(encoding="utf-8"))
    assert policies["coder"]["max_turns"] == 24 and policies["git"]["create_merge_request"] is True
    assert policies["coder"]["tool_output_chars"] == 12000, "deep merge keeps the other keys"
    assert policies["sandbox"]["image"] == "localhost/hermclaw-sandbox-generic:7"
    assert policies["sandbox"]["images"] == {t: f"localhost/hermclaw-sandbox-{t}:7" for t in ("python", "node", "php", "generic")}
    assert (
        policies["commands"]["forbidden_patterns"]
        == yaml.safe_load((REPO / "config" / "policies.example.yaml").read_text())["commands"]["forbidden_patterns"]
    ), "regular expressions survive the YAML round trip"
    models = yaml.safe_load((out / "models.yaml").read_text(encoding="utf-8"))
    assert models["litellm"]["request_timeout_seconds"] == 900
    assert {p["role"]: p["model"] for p in models["profiles"]}["coder"] == "qwen3-coder:30b", "fixed model roles stay"
    # the rendered set is a valid Hermclaw configuration (the deployment runs the same check with the new release)
    env = {**os.environ, "HERMCLAW_CONFIG_DIR": str(out), "HERMCLAW_ENV": "production"}
    check = _run([str(VENV_BIN / "hermclaw"), "config-check"], env=env, cwd=tmp_path)
    assert check.returncode == 0, check.stderr
    summary = json.loads(check.stdout)
    assert {h["id"]: h["address"] for h in summary["hosts"]}["exec-222"] == "10.9.0.22"
    assert summary["coder_max_turns"] == 24


@pytest.mark.parametrize(
    ("web_vars", "port"),
    [({"nginx_listen_port": 9080}, 9080), ({"hermclaw_webui_http_port": 9180}, 9180), ({"hermclaw_webui_http_port": 80}, 80)],
)
def test_hosts_yaml_probes_the_configured_nginx_port(tmp_path: Path, web_vars: dict[str, Any], port: int) -> None:
    """The readiness probe of webui-223 in hosts.yaml follows the web host's nginx port (host vars of the inventory)."""
    res, out = _render_config(tmp_path, {}, web=web_vars)
    assert res.returncode == 0, res.stdout[-4000:]
    hosts = {h["id"]: h for h in yaml.safe_load((out / "hosts.yaml").read_text(encoding="utf-8"))["hosts"]}
    assert hosts["webui-223"]["services"] == [{"name": "nginx", "port": port, "http_path": "/healthz"}]


@pytest.mark.parametrize(
    ("extra", "host_vars", "expect"),
    [
        ({}, {"exec": {"wol_mac": "00:00:00:00:00:00"}}, "placeholder MAC address"),
        ({}, {"model": {"wol_mac": "zz:00:00:00:02:24"}}, "model-224"),
        ({"hermclaw_config_allow_placeholder_macs": True}, {"exec": {"wol_mac": "00:00:00:00:00:00"}}, None),
        (
            {"hermclaw_config_host_overrides": {"exec-222": {"wake_on_lan": {"enabled": False}}}},
            {"exec": {"wol_mac": "00:00:00:00:00:00"}},
            None,
        ),
        ({"hermclaw_config_overrides": {"secrets": {"x": 1}}}, {}, "hermclaw_config_overrides must map file names"),
        ({"hermclaw_config_overrides": {"models": ["not", "a", "mapping"]}}, {}, "hermclaw_config_overrides must map file names"),
        ({"hermclaw_config_unmanaged": ["hermclaw"]}, {}, "hermclaw_config_unmanaged"),
    ],
)
def test_configuration_refuses_placeholder_macs_and_bad_overrides(
    tmp_path: Path, extra: dict[str, Any], host_vars: dict[str, dict[str, Any]], expect: str | None
) -> None:
    res, _ = _render_config(tmp_path, extra, **host_vars)
    if expect is None:
        assert res.returncode == 0, res.stdout[-3000:]
    else:
        assert res.returncode != 0 and expect in res.stdout, res.stdout[-3000:]


# --------------------------------------------------------------------------------------------- credentials / enable


def _units(tmp_path: Path, credstore: Path) -> Path:
    units = tmp_path / "units"
    units.mkdir()
    (units / "hermclaw-scheduler.service").write_text(
        "[Service]\nExecStart=/bin/true\n"
        f"LoadCredentialEncrypted=litellm-master-key:{credstore}/hermclaw-litellm-master-key\n"
        f"LoadCredentialEncrypted=gitlab-token:{credstore}/hermclaw-gitlab-token\n"
        f"LoadCredentialEncrypted=worker-token-exec-222:{credstore}/hermclaw-worker-token-exec-222\n"
        "[Install]\nWantedBy=multi-user.target\n",
        encoding="utf-8",
    )
    (units / "hermclaw-backup.timer").write_text("[Timer]\nOnCalendar=daily\n[Install]\nWantedBy=timers.target\n", encoding="utf-8")
    (units / "hermclaw-noinstall.service").write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
    return units


def _fake_credential_helper(bindir: Path, failing: str = "") -> Path:
    """hermclaw-credential double: `check NAME` prints OK, or FAIL (exit 65) for the name given as failing."""
    bindir.mkdir(parents=True, exist_ok=True)
    helper = bindir / "hermclaw-credential"
    helper.write_text(
        f'#!/bin/sh\n[ "$1" = check ] || exit 64\n[ "$2" = "{failing}" ] && {{ echo "FAIL $2" >&2; exit 65; }}\necho "OK $2"\n',
        encoding="utf-8",
    )
    helper.chmod(0o755)
    return bindir


def test_credential_check_names_every_missing_credential_before_anything_changes(tmp_path: Path) -> None:
    store = tmp_path / "credstore"
    store.mkdir()
    units = _units(tmp_path, store)
    (store / "hermclaw-litellm-master-key").write_text("x", encoding="utf-8")
    tasks = [_include("hermclaw_release", "credentials.yml")]
    extra = {
        "hermclaw_release_credential_units": ["hermclaw-scheduler.service"],
        "hermclaw_release_systemd_dir": str(units),
        "hermclaw_bin_dir": str(_fake_credential_helper(tmp_path / "bin")),
        "hermclaw_release_check_credentials_decrypt": True,
    }
    res = _playbook(tmp_path, _local_play(tasks), extra)
    assert res.returncode != 0
    out = res.stdout
    assert "2 credential(s) missing - nothing has been deployed" in out
    assert f"hermclaw-scheduler.service loads 'gitlab-token' from {store}/hermclaw-gitlab-token" in out
    assert "hermclaw-credential set gitlab-token" in out and "never echoed" in out
    assert "worker-token-exec-222" in out and "hermclaw-credential generate worker-token-exec-222" in out
    # all present: passes, and every credential is decrypted once (values never shown)
    for name in ("gitlab-token", "worker-token-exec-222"):
        (store / f"hermclaw-{name}").write_text("x", encoding="utf-8")
    res = _playbook(tmp_path, _local_play(tasks), extra)
    assert res.returncode == 0, res.stdout[-3000:]
    decrypted = re.findall(r"^ok: \[localhost\] => \(item=([a-z0-9-]+)\)$", res.stdout, re.M)
    assert sorted(set(decrypted)) == ["gitlab-token", "litellm-master-key", "worker-token-exec-222"], decrypted
    # a blob that cannot be decrypted on this host (other host key, TPM2/PCR change) fails the run
    extra["hermclaw_bin_dir"] = str(_fake_credential_helper(tmp_path / "failing-bin", failing="gitlab-token"))
    res = _playbook(tmp_path, _local_play(tasks), extra)
    assert res.returncode != 0 and "FAIL gitlab-token" in res.stdout, res.stdout[-3000:]


def test_units_are_enabled_offline_with_the_links_systemctl_would_create(tmp_path: Path) -> None:
    store = tmp_path / "credstore"
    store.mkdir()
    units = _units(tmp_path, store)
    tasks = [_include("hermclaw_release", "enable.yml")]
    extra = {
        "hermclaw_release_enable": ["hermclaw-scheduler.service", "hermclaw-backup.timer"],
        "hermclaw_release_systemd_dir": str(units),
        "hermclaw_manage_services": False,
    }
    res = _playbook(tmp_path, _local_play(tasks), extra)
    assert res.returncode == 0, res.stdout[-3000:]
    link = units / "multi-user.target.wants" / "hermclaw-scheduler.service"
    assert link.is_symlink() and os.readlink(link) == str(units / "hermclaw-scheduler.service")
    assert os.readlink(units / "timers.target.wants" / "hermclaw-backup.timer") == str(units / "hermclaw-backup.timer")
    again = _playbook(tmp_path, _local_play(tasks), extra)
    assert again.returncode == 0 and _changed(again) == 0
    extra["hermclaw_release_enable"] = ["hermclaw-noinstall.service"]
    res = _playbook(tmp_path, _local_play(tasks), extra)
    assert res.returncode != 0 and "units without WantedBy=" in res.stdout


# --------------------------------------------------------------------------------------------- switch / prune / dry run


def _fake_release_venv(install: Path, version: str, installed_at: float) -> None:
    """What hermclaw_release main.yml leaves behind: venv-<version> (with its marker) and releases/<version>."""
    venv = install / f"venv-{version}"
    (venv / "bin").mkdir(parents=True)
    (venv / ".hermclaw-release").write_text(version, encoding="utf-8")
    payload = install / "releases" / version
    payload.mkdir(parents=True)
    for path in (venv, payload):
        os.utime(path, (installed_at, installed_at))


def test_switch_and_prune_keep_the_previously_active_release(tmp_path: Path) -> None:
    """Rollback followed by an upgrade: the release active before the last switch survives pruning (keep=1)."""
    if os.geteuid() != 0:
        pytest.skip("the switch makes the venvs root-owned")
    install = tmp_path / "opt" / "hermclaw"
    (install / "venv").mkdir(parents=True)  # the bootstrap's plain venv directory
    (install / "venv" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (install / "venv-manual").mkdir()  # not a release: never pruned
    (install / "releases" / "notes").mkdir(parents=True)
    base = {
        "hermclaw_install_dir": str(install),
        "hermclaw_venv": str(install / "venv"),
        "hermclaw_releases_dir": str(install / "releases"),
        "hermclaw_keep_releases": 1,
    }
    tasks = [_include("hermclaw_release", "switch.yml")]

    def switch(version: str, *args: str) -> subprocess.CompletedProcess[str]:
        res = _playbook(tmp_path, _local_play(tasks), {**base, "hermclaw_version": version}, args=args)
        assert res.returncode == 0, res.stdout[-3000:]
        return res

    def state() -> tuple[str, list[str], list[str]]:
        return (
            os.readlink(install / "venv"),
            sorted(p.name for p in install.glob("venv-*")),
            sorted(p.name for p in (install / "releases").iterdir()),
        )

    now = time.time()
    _fake_release_venv(install, "1.0.0", now - 5000)
    switch("1.0.0")
    assert state()[0] == "venv-1.0.0" and os.readlink(install / "current") == "releases/1.0.0"
    (kept,) = install.glob("venv.pre-release-*")
    assert (kept / "pyvenv.cfg").is_file(), "the bootstrap's directory is kept, not deleted"
    assert _changed(switch("1.0.0")) == 0
    _fake_release_venv(install, "1.1.0", now - 4000)
    switch("1.1.0")
    _fake_release_venv(install, "1.2.0", now - 3000)
    switch("1.2.0")
    assert state() == ("venv-1.2.0", ["venv-1.1.0", "venv-1.2.0", "venv-manual"], ["1.1.0", "1.2.0", "notes"])
    switch("1.1.0")  # rollback: reuses the kept venv
    assert state() == ("venv-1.1.0", ["venv-1.1.0", "venv-1.2.0", "venv-manual"], ["1.1.0", "1.2.0", "notes"])
    _fake_release_venv(install, "1.3.0", now - 2000)
    switch("1.3.0")
    assert state() == ("venv-1.3.0", ["venv-1.1.0", "venv-1.3.0", "venv-manual"], ["1.1.0", "1.3.0", "notes"]), (
        "the release active before the switch (1.1.0) is kept, not the most recently installed other one (1.2.0)"
    )
    assert _changed(switch("1.3.0")) == 0
    # dry run of an upgrade to a release that is not installed: reports the switch, changes nothing on disk
    res = switch("1.4.0", "--check", "--diff")
    assert _changed(res) > 0
    assert state() == ("venv-1.3.0", ["venv-1.1.0", "venv-1.3.0", "venv-manual"], ["1.1.0", "1.3.0", "notes"])
    assert os.readlink(install / "current") == "releases/1.3.0"


def _ui_release(dist: Path, version: str) -> None:
    rel = dist / f"release-{version}"
    rel.mkdir(parents=True)
    src = dist / f"ui-src-{version}"
    (src / "assets").mkdir(parents=True)
    (src / "index.html").write_text(f'<!doctype html><div id="root"></div><!-- {version} -->\n', encoding="utf-8")
    (src / "assets" / "app.js").write_text("console.log(1)\n", encoding="utf-8")
    with tarfile.open(rel / f"hermclaw-ui-{version}.tar.gz", "w:gz") as tf:
        tf.add(src, arcname=".")
    meta = {"name": "hermclaw", "version": version, "ui": {"payload": f"hermclaw-ui-{version}.tar.gz"}}
    (rel / "release.json").write_text(json.dumps(meta), encoding="utf-8")


def test_ui_build_switch_rollback_pruning_and_dry_run(tmp_path: Path) -> None:
    """roles/hermclaw_ui on a real directory tree: placeholder webroot -> versioned builds + atomic symlink."""
    if os.geteuid() != 0:
        pytest.skip("the UI files are made root-owned")
    dist, www = tmp_path / "dist", tmp_path / "www"
    for version in ("1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"):
        _ui_release(dist, version)
    webroot = www / "hermclaw-next"
    webroot.mkdir(parents=True)
    placeholder = (ROLES_DIR / "nginx" / "templates" / "index.html.j2").read_text(encoding="utf-8")
    (webroot / "index.html").write_text(placeholder, encoding="utf-8")
    releases = www / "hermclaw-next-releases"
    (releases / "keep-me").mkdir(parents=True)  # not a release: never pruned
    base = {
        "hermclaw_webroot": str(webroot),
        "hermclaw_release_root": str(dist),
        "hermclaw_manage_services": False,
        "hermclaw_ui_owner": "root",
        "hermclaw_ui_group": "root",
        "hermclaw_keep_releases": 1,
    }
    tasks = [_include("hermclaw_ui", "main.yml")]

    def deploy(version: str, *args: str) -> subprocess.CompletedProcess[str]:
        res = _playbook(tmp_path, _local_play(tasks), {**base, "hermclaw_version": version}, args=args)
        assert res.returncode == 0, res.stdout[-3000:]
        return res

    def state() -> tuple[str, list[str]]:
        return os.readlink(webroot), sorted(p.name for p in releases.iterdir())

    deploy("1.0.0")
    assert state() == ("hermclaw-next-releases/1.0.0", ["1.0.0", "1.0.0.release", "keep-me"])
    assert "<!-- 1.0.0 -->" in (webroot / "index.html").read_text(encoding="utf-8"), "the placeholder webroot is replaced"
    assert not list(www.glob("hermclaw-next.pre-release-*")), "a webroot with only the placeholder is not kept"
    assert _changed(deploy("1.0.0")) == 0
    deploy("1.1.0")
    deploy("1.2.0")
    assert state()[1] == ["1.1.0", "1.1.0.release", "1.2.0", "1.2.0.release", "keep-me"]
    deploy("1.1.0")  # rollback
    assert state() == ("hermclaw-next-releases/1.1.0", ["1.1.0", "1.1.0.release", "1.2.0", "1.2.0.release", "keep-me"])
    deploy("1.3.0")
    assert state() == ("hermclaw-next-releases/1.3.0", ["1.1.0", "1.1.0.release", "1.3.0", "1.3.0.release", "keep-me"]), (
        "the build active before the switch is kept"
    )
    # dry run of an upgrade: nothing is unpacked, the webroot stays
    deploy("1.4.0", "--check", "--diff")
    assert state() == ("hermclaw-next-releases/1.3.0", ["1.1.0", "1.1.0.release", "1.3.0", "1.3.0.release", "keep-me"])


def _fake_payload_release(tmp_path: Path, version: str) -> Path:
    """release.json + runtime/LiteLLM payload archives (content irrelevant: a dry run never unpacks them)."""
    dist = tmp_path / "dist"
    rel = dist / f"release-{version}"
    rel.mkdir(parents=True)
    for name in (f"hermclaw-runtime-{version}.tar", f"hermclaw-litellm-{version}.tar"):
        with tarfile.open(rel / name, "w") as tf:
            info = tarfile.TarInfo("SHA256SUMS")
            tf.addfile(info)
    meta = {
        "name": "hermclaw",
        "version": version,
        "python": {"version": "3.13"},
        "alembic_head": "0002",
        "runtime": {"payload": f"hermclaw-runtime-{version}.tar", "lock_sha256": "0" * 64},
        "litellm": {
            "payload": f"hermclaw-litellm-{version}.tar",
            "version": "1.104.2",
            "requirements": ["litellm[proxy]==1.104.2"],
            "lock_sha256": "1" * 64,
            "venv_name": "venv-1.104.2-111111111111",
        },
    }
    (rel / "release.json").write_text(json.dumps(meta), encoding="utf-8")
    return dist


def test_dry_run_of_a_new_release_unpacks_and_installs_nothing(tmp_path: Path) -> None:
    """`deploy.yml -e hermclaw_version=<new> --check --diff` (docs/runbooks/upgrade.md step 2) must not fail on the
    payload directories it does not create, and must leave no trace."""
    dist = _fake_payload_release(tmp_path, "2.0.0")
    root = tmp_path / "root"
    tasks = [
        _include("hermclaw_release", "main.yml"),
        {
            "name": "LiteLLM venv",
            "ansible.builtin.include_role": {"name": "hermclaw_orchestrator", "tasks_from": "litellm.yml"},
        },
        _include("hermclaw_release", "switch.yml"),
    ]
    extra = {
        "hermclaw_version": "2.0.0",
        "hermclaw_release_root": str(dist),
        "hermclaw_install_dir": str(root / "opt" / "hermclaw"),
        "hermclaw_venv": str(root / "opt" / "hermclaw" / "venv"),
        "hermclaw_releases_dir": str(root / "opt" / "hermclaw" / "releases"),
        "hermclaw_litellm_root": str(root / "opt" / "litellm"),
        "hermclaw_orchestrator_ip": "192.168.178.225",
        "common_python_bin": shutil.which("python3") or "/usr/bin/python3",
    }
    res = _playbook(tmp_path, _local_play(tasks), extra, args=("--check", "--diff"))
    assert res.returncode == 0, res.stdout[-4000:]
    assert _changed(res) > 0, "the dry run reports what the real run would do"
    assert not root.exists(), sorted(str(p) for p in root.rglob("*"))


FAKE_OLLAMA = r"""#!/usr/bin/env python3
# test double of the ollama CLI: installed tags in $FAKE_OLLAMA_STATE, tags in $FAKE_OLLAMA_UNKNOWN cannot be pulled
import json, os, sys
state = os.environ["FAKE_OLLAMA_STATE"]
tags = json.load(open(state)) if os.path.exists(state) else []
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(["ollama", *sys.argv[1:], os.environ.get("OLLAMA_HOST", "")]) + "\n")
cmd = sys.argv[1]
if cmd == "list":
    print("NAME    ID    SIZE    MODIFIED")
    for tag in tags:
        print(f"{tag}    0123456789ab    1 GB    1 minute ago")
elif cmd == "pull":
    if sys.argv[2] in os.environ.get("FAKE_OLLAMA_UNKNOWN", "").split():
        sys.exit(f"Error: pull model manifest: file does not exist ({sys.argv[2]})")
    if sys.argv[2] not in tags:
        json.dump(tags + [sys.argv[2]], open(state, "w"))
elif cmd == "show":
    sys.exit(0 if sys.argv[2] in tags else 1)
"""

FAKE_SYSTEMD_RUN_SYNC = r"""#!/usr/bin/env python3
# test double of `systemd-run --unit=NAME ...`: runs the command synchronously, keeps the unit result for systemctl
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(["systemd-run", *args]) + "\n")
unit, env, i = None, dict(os.environ), 0
while args[i].startswith("--"):
    if args[i].startswith("--unit="):
        unit = args[i].split("=", 1)[1]
    elif args[i].startswith("--setenv="):
        k, v = args[i].split("=", 1)[1].split("=", 1)
        env[k] = v
    i += 1
units = os.environ["FAKE_UNITS"]
if os.path.exists(os.path.join(units, unit + ".service")):
    sys.exit(f"Failed to start transient service unit: Unit {unit}.service was already loaded or has a fragment file.")
rc = subprocess.run(args[i:], env=env, check=False).returncode
if rc != 0:
    open(os.path.join(units, unit + ".service"), "w").write("failed")
"""

FAKE_SYSTEMCTL = r"""#!/bin/sh
# test double of systemctl is-active / reset-failed for the transient units of the fake systemd-run
echo "systemctl $*" >> "$FAKE_LOG.systemctl"
f="$FAKE_UNITS/$2"
case "$1" in
    is-active) if [ -f "$f" ]; then cat "$f"; echo; exit 3; else echo inactive; exit 3; fi ;;
    reset-failed) rm -f "$f" ;;
esac
"""


@pytest.fixture
def model_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """roles/hermclaw_worker models.yml against test doubles of ollama, systemd-run, systemctl and journalctl, with
    the release's real scripts/ops/hermclaw-model-pull."""
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    for name, text in (("ollama", FAKE_OLLAMA), ("systemd-run", FAKE_SYSTEMD_RUN_SYNC), ("systemctl", FAKE_SYSTEMCTL)):
        (fakes / name).write_text(text, encoding="utf-8")
        (fakes / name).chmod(0o755)
    (fakes / "journalctl").write_text("#!/bin/sh\necho 'pull log: manifest not found'\n", encoding="utf-8")
    (fakes / "journalctl").chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shutil.copy(REPO / "scripts" / "ops" / "hermclaw-model-pull", bindir / "hermclaw-model-pull")
    units = tmp_path / "units"
    units.mkdir()
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    dropin = tmp_path / "hermclaw.conf"
    dropin.write_text(f"[Service]\nEnvironment=OLLAMA_MODELS={models_dir}\n", encoding="utf-8")
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_OLLAMA_STATE", str(tmp_path / "ollama.json"))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_UNITS", str(units))
    monkeypatch.delenv("FAKE_OLLAMA_UNKNOWN", raising=False)
    models = yaml.safe_load((REPO / "config" / "models.example.yaml").read_text(encoding="utf-8"))
    extra = {
        "hermclaw_config_data": {"models": models},
        "systemd_skeleton_worker_id": "model-224",
        "hermclaw_worker_ollama_bin": str(fakes / "ollama"),
        "hermclaw_worker_ollama_dropin": str(dropin),
        "hermclaw_bin_dir": str(bindir),
        "hermclaw_worker_model_sizes_gb": {},
        "hermclaw_worker_model_default_size_gb": 0,
        "hermclaw_worker_model_disk_headroom_gb": 0,
        "hermclaw_worker_model_pull_poll": 1,
        "hermclaw_worker_model_pull_timeout": 5,
    }
    return {"tmp": tmp_path, "fakes": fakes, "extra": extra, "log": log, "models": models}


def _pull(host: dict[str, Any], *args: str, **extra: Any) -> subprocess.CompletedProcess[str]:
    tasks = [_include("hermclaw_worker", "models.yml")]
    return _playbook(host["tmp"], _local_play(tasks), {**host["extra"], **extra}, args=args, path_prefix=host["fakes"])


def _log(host: dict[str, Any]) -> list[list[str]]:
    if not host["log"].exists():
        return []
    return [json.loads(line) for line in host["log"].read_text(encoding="utf-8").splitlines()]


def test_model_pull_follows_models_yaml_and_survives_a_dry_run(model_host: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """32.6: the fixed roles' tags of models.yaml are pulled once (hermclaw-model-pull in a transient unit), a tag the
    inventory changed is pulled too, a dry run pulls nothing, and a tag that cannot be pulled fails the play."""
    profiles = model_host["models"]["profiles"]
    wanted = sorted({p["model"] for p in profiles if p.get("enabled", True)})
    assert {"gemma4:26b", "qwen3-coder:30b", "qwen3.8:27b", "qwen3:8b", "embeddinggemma:300m"} <= set(wanted)
    stops = model_host["log"].with_name(model_host["log"].name + ".systemctl")

    def stopped() -> list[str]:
        lines = stops.read_text(encoding="utf-8").splitlines() if stops.exists() else []
        return [ln for ln in lines if ln.startswith("systemctl stop ")]

    # dry run on a host without models: reports, pulls nothing, leaves the daemon alone
    res = _pull(model_host, "--check")
    assert res.returncode == 0, res.stdout[-3000:]
    assert not [c for c in _log(model_host) if c[0] == "systemd-run" or c[1] == "pull"], _log(model_host)
    assert stopped() == []
    # the real run: the worker daemon is stopped for the download (no heartbeat - the scheduler cannot suspend the
    # host mid-download; main.yml starts it again after the pulls)
    res = _pull(model_host)
    assert res.returncode == 0, res.stdout[-3000:]
    assert stopped() == ["systemctl stop hermclaw-model-worker.service"]
    runs = [c for c in _log(model_host) if c[0] == "systemd-run"]
    assert len(runs) == 1 and "--unit=hermclaw-model-pull" in runs[0] and f"{model_host['tmp']}/bin/hermclaw-model-pull" in runs[0]
    assert "--setenv=OLLAMA_HOST=http://127.0.0.1:11434" in runs[0]
    installed = json.loads(Path(os.environ["FAKE_OLLAMA_STATE"]).read_text(encoding="utf-8"))
    assert set(wanted) <= set(installed)
    # nothing missing: no pull at all
    before = len(_log(model_host))
    again = _pull(model_host)
    assert again.returncode == 0 and _changed(again) == 0
    assert [c for c in _log(model_host)[before:] if c[0] == "systemd-run" or c[1] == "pull"] == []
    assert len(stopped()) == 1, "nothing to download: the daemon keeps running"
    # a changed tag (hermclaw_config_overrides.models): pulled by its own transient unit
    changed = json.loads(json.dumps(model_host["models"]))
    for p in changed["profiles"]:
        if p["role"] == "coder":
            p["model"] = "qwen3-coder:30b-a3b-q4_K_M"
    res = _pull(model_host, hermclaw_config_data={"models": changed})
    assert res.returncode == 0, res.stdout[-3000:]
    tag_runs = [c for c in _log(model_host) if c[0] == "systemd-run" and "--unit=hermclaw-model-pull-tag" in c]
    assert len(tag_runs) == 1 and tag_runs[0][-2:] == ["pull", "qwen3-coder:30b-a3b-q4_K_M"]
    # a tag that cannot be pulled stops the play with the log of the pull
    for p in changed["profiles"]:
        if p["role"] == "fast":
            p["model"] = "qwen3:9b-does-not-exist"
    monkeypatch.setenv("FAKE_OLLAMA_UNKNOWN", "qwen3:9b-does-not-exist")
    res = _pull(model_host, hermclaw_config_data={"models": changed})
    assert res.returncode != 0
    assert "still missing on localhost: qwen3:9b-does-not-exist" in res.stdout and "manifest not found" in res.stdout
    # tags become arguments of ollama pull: an option-like tag is refused before anything runs
    for p in changed["profiles"]:
        if p["role"] == "fast":
            p["model"] = "--insecure"
    res = _pull(model_host, hermclaw_config_data={"models": changed})
    assert res.returncode != 0 and "models.yaml names invalid Ollama tags" in res.stdout


def test_workers_and_ui_never_get_ahead_of_the_orchestrator(tmp_path: Path) -> None:
    """A run that does not deploy the orchestrator (--limit, --tags) needs the orchestrator on that release already."""
    upstream = _Upstream()  # /api/health -> {"status": "ok", "version": "0.1.0rc1"}
    try:
        inventory = {
            "all": {
                "vars": {"ansible_connection": "local", "hermclaw_orchestrator_ip": "127.0.0.1"},
                "children": {
                    "orchestrator": {"hosts": {"o": {}}},
                    "execution_workers": {"hosts": {"e": {}}},
                    "webui": {"hosts": {"w": {}}},
                },
            }
        }
        inv = tmp_path / "hosts.yml"
        inv.write_text(yaml.safe_dump(inventory), encoding="utf-8")
        api = f"http://127.0.0.1:{upstream.port}"

        def run(hosts: str, version: str, *args: str, **extra: Any) -> subprocess.CompletedProcess[str]:
            play = _local_play([_include("hermclaw_release", "orchestrator_first.yml")], hosts=hosts)
            play[0]["tags"] = ["always"]  # like the preflight play of deploy.yml
            vars_ = {
                "hermclaw_version": version,
                "hermclaw_release_require_orchestrator_first": True,
                "hermclaw_release_orchestrator_api_url": api,
                **extra,
            }
            return _playbook(tmp_path, play, vars_, inventory=inv, args=args)

        res = run("orchestrator:execution_workers:webui", "0.2.0")
        assert res.returncode == 0 and upstream.requests == [], "the orchestrator goes first in this run: nothing to ask"
        res = run("execution_workers:webui", "0.1.0rc1")
        assert res.returncode == 0, res.stdout[-3000:]
        assert [r["path"] for r in upstream.requests] == ["/api/health"], "asked once (run_once, from the control node)"
        res = run("execution_workers", "0.2.0")
        assert res.returncode != 0
        assert "must never run a newer release than the orchestrator" in res.stdout and "reports version 0.1.0rc1" in res.stdout
        res = run("orchestrator:webui", "0.2.0", "--tags", "hermclaw_ui")
        assert res.returncode != 0, "--tags hermclaw_ui deploys the UI without the orchestrator role"
        res = run("orchestrator:webui", "0.2.0", "--skip-tags", "hermclaw_orchestrator")
        assert res.returncode != 0
        res = run("execution_workers", "0.2.0", hermclaw_release_orchestrator_api_url=f"http://127.0.0.1:{_free_port()}")
        assert res.returncode != 0 and "/api/health reports" in res.stdout, "an unreachable orchestrator is refused too"
        res = run("execution_workers", "0.2.0", hermclaw_release_require_orchestrator_first=False)
        assert res.returncode == 0, "explicit opt-out (disaster recovery with the orchestrator down)"
    finally:
        upstream.close()


def test_nothing_live_changes_before_the_schema_decision() -> None:
    """Live configuration, LiteLLM and the venv symlink switch only after the drain and the migration; restarts also
    see unit/env changes the bootstrap part of site.yml made; LiteLLM is healthy before the scheduler starts."""
    plays = yaml.safe_load((PLAYBOOKS_DIR / "deploy.yml").read_text(encoding="utf-8"))
    for play in plays[1:4]:
        (config,) = [r for r in play["roles"] if r["role"] == "hermclaw_config"]
        assert config["vars"]["hermclaw_config_install_at_switch"] is True, play["name"]

    def names(role: str, file: str) -> list[str]:
        return [t["name"] for t in yaml.safe_load((ROLES_DIR / role / "tasks" / file).read_text(encoding="utf-8"))]

    order = names("hermclaw_orchestrator", "main.yml")

    def at(prefix: str) -> int:
        (index,) = [i for i, n in enumerate(order) if n.startswith(prefix)]
        return index

    assert (
        at("LiteLLM proxy venv")
        < at("Database schema of the release")
        < at("Enable the orchestrator units")
        < at("Drain")
        < at("Migrate the database schema")
        < at("Install the validated configuration")
        < at("Switch LiteLLM")
        < at("Switch to release")
        < at("Start LiteLLM")
        < at("LiteLLM is healthy")
        < at("Start SearXNG and the services")
        < at("Health checks of the orchestrator")
    ), order
    litellm = (ROLES_DIR / "hermclaw_orchestrator" / "tasks" / "litellm.yml").read_text(encoding="utf-8")
    assert "state: link" not in litellm and "hermclaw_orchestrator_litellm_config" not in litellm, "installs only"
    worker = names("hermclaw_worker", "main.yml")
    assert worker.index("Install the validated configuration of release {{ hermclaw_version }}") + 1 == worker.index(
        "Switch to release {{ hermclaw_version }}"
    )
    config_main = yaml.safe_load((ROLES_DIR / "hermclaw_config" / "tasks" / "main.yml").read_text(encoding="utf-8"))
    (install,) = [t for t in config_main if t.get("ansible.builtin.import_tasks") == "install.yml"]
    assert install["when"] == "not hermclaw_config_install_at_switch | bool"
    # every operator-CLI call before the configuration is live uses the new release's staged configuration
    tasks = yaml.safe_load((ROLES_DIR / "hermclaw_orchestrator" / "tasks" / "main.yml").read_text(encoding="utf-8"))
    database = yaml.safe_load((ROLES_DIR / "hermclaw_orchestrator" / "tasks" / "database.yml").read_text(encoding="utf-8"))
    calls = [t for t in database if "--venv" in json.dumps(t)]
    calls += [t for t in tasks[at("Migrate the database schema")]["block"] if "--alembic" not in json.dumps(t)]
    assert len(calls) == 3 and all("--config-dir" in json.dumps(t) for t in calls)
    # restart decisions use the accumulated flags, never a registered result another play could overwrite
    for f in sorted(ROLES_DIR.glob("hermclaw_*/tasks/*.yml")):
        text = f.read_text(encoding="utf-8")
        assert "systemd_skeleton_unit_files" not in text and "systemd_skeleton_env_file" not in text, f.name
    skeleton = (ROLES_DIR / "systemd_skeleton" / "tasks" / "main.yml").read_text(encoding="utf-8")
    assert "systemd_skeleton_units_changed | default(false) | bool or systemd_skeleton_unit_files is changed" in skeleton


# --------------------------------------------------------------------------------------------- operator CLI

FAKE_SYSTEMD_RUN = r"""#!/usr/bin/env python3
# test double: records argv, then runs the command after "--" with the EnvironmentFile and -E variables
import json, os, subprocess, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_RUN_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\n")
cmd = argv[argv.index("--") + 1:]
env = {"PATH": os.environ["PATH"]}
for i, a in enumerate(argv[: argv.index("--")]):
    if a == "-p" and argv[i + 1].startswith("EnvironmentFile="):
        for line in open(argv[i + 1].split("=", 1)[1], encoding="utf-8"):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v
    if a == "-E":
        k, v = argv[i + 1].split("=", 1)
        env[k] = v
sys.exit(subprocess.run(cmd, env=env, check=False).returncode)
"""


@pytest.fixture
def cli(tmp_path: Path) -> dict[str, Any]:
    """The operator CLI rendered from the role template with temporary paths, plus a fake systemd-run and venv."""
    root = tmp_path / "root"
    venv = root / "opt" / "hermclaw" / "venv"
    other = root / "opt" / "hermclaw" / "venv-0.2.0"
    for v in (venv, other):
        (v / "bin").mkdir(parents=True)
        (v / "share" / "hermclaw").mkdir(parents=True)
        for tool in ("hermclaw", "alembic"):
            exe = v / "bin" / tool
            exe.write_text(
                f'#!/bin/sh\necho "{v.name}/{tool} $* env=$HERMCLAW_ENV home=$HOME${{HERMCLAW_CONFIG_DIR:+ cfg=$HERMCLAW_CONFIG_DIR}}"\n'
                'read -r line && echo "stdin=$line"\nexit 3\n',
                encoding="utf-8",
            )
            exe.chmod(0o755)
    conf = root / "etc" / "hermclaw"
    conf.mkdir(parents=True)
    (conf / "hermclaw.env").write_text("HERMCLAW_ENV=production\n", encoding="utf-8")
    store = root / "credstore"
    store.mkdir()
    for name in ("litellm-master-key", "worker-token-exec-222", "backup-manifest-key"):
        (store / f"hermclaw-{name}").write_text("blob", encoding="utf-8")
    wrapper = tmp_path / "hermclaw"
    tasks = [
        {
            "name": "render",
            "ansible.builtin.template": {
                "src": str(ROLES_DIR / "hermclaw_orchestrator" / "templates" / "hermclaw-cli.sh.j2"),
                "dest": str(wrapper),
                "mode": "0755",
            },
        }
    ]
    extra = {
        "hermclaw_venv": str(venv),
        "hermclaw_config_dir": str(conf),
        "hermclaw_credstore_dir": str(store),
        "hermclaw_data_dir": str(tmp_path),
        "hermclaw_home": "/var/lib/hermclaw",
        "hermclaw_orchestrator_ip": "192.168.178.225",
        "systemd_skeleton_credentials": {"scheduler": ["litellm-master-key", "gitlab-token"]},
        "systemd_skeleton_worker_ids": ["exec-222", "model-224"],
    }
    play = _local_play(tasks)
    play[0]["vars_files"].append(str(ROLES_DIR / "systemd_skeleton" / "defaults" / "main.yml"))
    play[0]["vars_files"].append(str(ROLES_DIR / "hermclaw_orchestrator" / "defaults" / "main.yml"))
    res = _playbook(tmp_path, play, extra)
    assert res.returncode == 0, res.stdout[-3000:]
    fake = tmp_path / "fake-systemd-run"
    fake.write_text(FAKE_SYSTEMD_RUN, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "run.jsonl"
    env = {"PATH": os.environ["PATH"], "HERMCLAW_SYSTEMD_RUN": str(fake), "FAKE_RUN_LOG": str(log)}
    return {"wrapper": wrapper, "env": env, "log": log, "venv": venv, "other": other, "store": store, "conf": conf}


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_operator_cli_runs_the_cli_in_the_services_context(cli: dict[str, Any], tmp_path: Path) -> None:
    if os.geteuid() != 0:
        pytest.skip("the operator CLI requires root")
    shellcheck = shutil.which("shellcheck")
    if shellcheck:
        lint = _run([shellcheck, str(cli["wrapper"])], cwd=tmp_path)
        assert lint.returncode == 0, lint.stdout
    res = _run([str(cli["wrapper"]), "token-create", "alice", "--scopes", "read"], env=cli["env"], cwd=tmp_path, input_text="hello\n")
    assert res.returncode == 3, "the command's exit status is propagated"
    assert res.stdout.splitlines() == [
        "venv/hermclaw token-create alice --scopes read env=production home=/var/lib/hermclaw",
        "stdin=hello",
    ]
    assert "credential(s) not available: gitlab-token worker-token-model-224" in res.stderr
    (argv,) = _calls(cli["log"])
    sep = argv.index("--")
    opts, cmd = argv[:sep], argv[sep + 1 :]
    assert cmd == [f"{cli['venv']}/bin/hermclaw", "token-create", "alice", "--scopes", "read"]
    for flag in ("--quiet", "--pipe", "--wait", "--collect", "--service-type=exec", "--uid=hermclaw", "--gid=hermclaw"):
        assert flag in opts, flag
    props = [opts[i + 1] for i, a in enumerate(opts) if a == "-p"]
    assert f"EnvironmentFile={cli['conf']}/hermclaw.env" in props and "UMask=0027" in props
    assert sorted(p for p in props if p.startswith("LoadCredentialEncrypted=")) == [
        f"LoadCredentialEncrypted=backup-manifest-key:{cli['store']}/hermclaw-backup-manifest-key",
        f"LoadCredentialEncrypted=litellm-master-key:{cli['store']}/hermclaw-litellm-master-key",
        f"LoadCredentialEncrypted=worker-token-exec-222:{cli['store']}/hermclaw-worker-token-exec-222",
    ], "the scheduler's credentials plus the backup's optional ones that exist (sudo hermclaw backup ...)"
    assert any(a.startswith("--unit=hermclaw-cli-") for a in opts)
    assert "blob" not in json.dumps(argv), "credential contents never travel on the command line"
    # another installed release (the deployment migrates with the new venv before the switch) and alembic
    res = _run([str(cli["wrapper"]), "--venv", str(cli["other"]), "--alembic", "downgrade", "0002"], env=cli["env"], cwd=tmp_path)
    assert res.returncode == 3
    assert _calls(cli["log"])[-1][-5:] == [
        f"{cli['other']}/bin/alembic",
        "-c",
        f"{cli['other']}/share/hermclaw/alembic.ini",
        "downgrade",
        "0002",
    ]
    # the deployment's staged configuration (EnvironmentFile= would win over --setenv: env(1) sets it in the unit)
    staging = cli["conf"] / "deploy-staging"
    staging.mkdir()
    res = _run([str(cli["wrapper"]), "--venv", str(cli["other"]), "--config-dir", str(staging), "doctor"], env=cli["env"], cwd=tmp_path)
    assert res.returncode == 3 and res.stdout.splitlines()[0].endswith(f" cfg={staging}"), res.stdout + res.stderr
    assert _calls(cli["log"])[-1][-4:] == ["/usr/bin/env", f"HERMCLAW_CONFIG_DIR={staging}", f"{cli['other']}/bin/hermclaw", "doctor"]
    # an optional credential that does not exist is left out silently
    (cli["store"] / "hermclaw-backup-manifest-key").unlink()
    res = _run([str(cli["wrapper"]), "backup", "list"], env=cli["env"], cwd=tmp_path)
    assert "backup-manifest-key" not in res.stderr + json.dumps(_calls(cli["log"])[-1]), res.stderr


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (["--venv", "/nonexistent/venv", "doctor"], 69, "is not installed"),
        (["--venv", "/opt/../tmp", "doctor"], 69, "is not installed"),
        (["--venv"], 64, "--venv needs a directory"),
        (["--config-dir"], 64, "--config-dir needs a directory"),
        (["--config-dir", "/nonexistent/staging", "doctor"], 64, "is not a directory"),
        (["--config-dir", "etc/hermclaw", "doctor"], 64, "is not a directory"),
    ],
)
def test_operator_cli_refuses_bad_input(cli: dict[str, Any], tmp_path: Path, args: list[str], code: int, message: str) -> None:
    if os.geteuid() != 0:
        pytest.skip("the operator CLI requires root")
    res = _run([str(cli["wrapper"]), *args], env=cli["env"], cwd=tmp_path)
    assert res.returncode == code and message in res.stderr, res.stderr
    assert not cli["log"].exists(), "nothing was started"


def test_operator_cli_requires_root(cli: dict[str, Any], tmp_path: Path) -> None:
    setpriv = shutil.which("setpriv")
    if os.geteuid() != 0 or setpriv is None:
        pytest.skip("needs root and setpriv")
    script = cli["wrapper"].read_text(encoding="utf-8")
    res = _run(
        [setpriv, "--reuid=65534", "--regid=65534", "--clear-groups", "bash", "-s", "doctor"],
        env=cli["env"],
        cwd=Path("/"),
        input_text=script,
    )
    assert res.returncode == 77 and "run as root" in res.stderr, res.stderr
    assert not cli["log"].exists()


# --------------------------------------------------------------------------------------------- units


def test_units_carry_the_deployment_tweaks(tmp_path: Path) -> None:
    out = _render(tmp_path, tmp_path / "units", _render_vars())
    exec_unit = out["systemd/hermclaw-exec-worker.service"].read_text(encoding="utf-8")
    directives = {ln.split("=", 1)[0] for ln in exec_unit.splitlines() if "=" in ln and not ln.startswith("#")}
    implies_nnp = {
        "NoNewPrivileges",
        "ProtectKernelTunables",
        "ProtectKernelModules",
        "ProtectKernelLogs",
        "ProtectClock",
        "ProtectHostname",
        "RestrictRealtime",
        "LockPersonality",
        "SystemCallArchitectures",
        "RestrictAddressFamilies",
        "RestrictSUIDSGID",
        "RestrictNamespaces",
        "PrivateDevices",
        "SystemCallFilter",
        "MemoryDenyWriteExecute",
    }
    assert not directives & implies_nnp, f"rootless podman needs setuid newuidmap: {directives & implies_nnp}"
    assert "ProtectSystem" in directives and "ProtectHome" not in directives
    litellm = out["systemd/hermclaw-litellm.service"].read_text(encoding="utf-8")
    assert "Environment=LITELLM_LOCAL_MODEL_COST_MAP=True" in litellm and "Environment=DISABLE_ADMIN_UI=True" in litellm
    assert "LITELLM_TELEMETRY" not in litellm
    backup = out["systemd/hermclaw-backup.service"].read_text(encoding="utf-8")
    assert "ConditionPathExists=/etc/hermclaw/backup.yaml" in backup
    # the deployed form of infra/backup/hermclaw-backup.service (P33): config path, lock exit code, credentials only
    # when they exist on the host (a missing LoadCredentialEncrypted= file fails the unit with 243/CREDENTIALS)
    assert "Environment=HERMCLAW_BACKUP_CONFIG=/etc/hermclaw/backup.yaml" in backup and "SuccessExitStatus=75" in backup
    assert "\nLoadCredentialEncrypted=" not in backup
    with_key = _render(tmp_path, tmp_path / "with-key", _render_vars({"systemd_skeleton_backup_credentials": ["backup-manifest-key"]}))
    backup = with_key["systemd/hermclaw-backup.service"].read_text(encoding="utf-8")
    assert "\nLoadCredentialEncrypted=backup-manifest-key:/etc/credstore.encrypted/hermclaw-backup-manifest-key\n" in backup
    restore = with_key["systemd/hermclaw-restore-test.service"].read_text(encoding="utf-8")
    assert "\nLoadCredentialEncrypted=backup-manifest-key:/etc/credstore.encrypted/hermclaw-backup-manifest-key\n" in restore
    assert "ExecStart=/opt/hermclaw/bin/hermclaw-restore-test --latest" in restore and "SuccessExitStatus=75" in restore
    assert "\nReadWritePaths=/var/lib/hermclaw /var/log/hermclaw\n" in restore
    # a mounted target (unraid-share-setup.md variant B) is writable for both units, optional while unmounted
    mounted = _render(tmp_path, tmp_path / "mounted", _render_vars({"systemd_skeleton_backup_write_paths": ["/mnt/terra-backup/hermclaw"]}))
    for unit in ("hermclaw-backup.service", "hermclaw-restore-test.service"):
        text = mounted[f"systemd/{unit}"].read_text(encoding="utf-8")
        assert "\nReadWritePaths=/var/lib/hermclaw /var/log/hermclaw -/mnt/terra-backup/hermclaw\n" in text, unit
    reference = (REPO / "infra" / "backup" / "hermclaw-backup.service").read_text(encoding="utf-8")
    assert "LoadCredentialEncrypted=backup-manifest-key:/etc/credstore.encrypted/hermclaw-backup-manifest-key" in reference
    quadlet = out["systemd/hermclaw-searxng.container"].read_text(encoding="utf-8")
    directives = [ln for ln in quadlet.splitlines() if ln and not ln.startswith("#")]
    assert "WantedBy=default.target" not in directives, "bootstrap of a host that was never deployed"
    deployed = _render(
        tmp_path, tmp_path / "deployed", _render_vars({"ansible_facts": {"ansible_local": {"hermclaw": {"deployed": True}}}})
    )
    quadlet = deployed["systemd/hermclaw-searxng.container"].read_text(encoding="utf-8")
    assert quadlet.rstrip().endswith("[Install]\nWantedBy=default.target"), "deployed host: bootstrap and deployment agree"


# --------------------------------------------------------------------------------------------- TLS certificate


def _x509(cert: Path) -> str:
    return _run(["openssl", "x509", "-in", str(cert), "-noout", "-text"], cwd=cert.parent).stdout


def test_self_signed_certificate_is_generated_once_and_renewed_before_expiry(tmp_path: Path) -> None:
    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    tls = tmp_path / "tls"
    tasks = [_include("nginx", "tls.yml")]
    extra = {
        "nginx_tls_enabled": True,
        "nginx_tls_dir": str(tls),
        "hermclaw_web_server_name": "hermclaw.lan 192.168.178.223",
        "ansible_host": "192.168.178.223",
        "hermclaw_manage_services": False,
        "hermclaw_orchestrator_ip": "192.168.178.225",
    }
    play = _local_play(tasks)
    play[0]["vars_files"].append(str(ROLES_DIR / "nginx" / "defaults" / "main.yml"))
    res = _playbook(tmp_path, play, extra)
    assert res.returncode == 0, res.stdout[-3000:]
    cert, key = tls / "hermclaw-web.crt", tls / "hermclaw-web.key"
    text = _x509(cert)
    assert "DNS:hermclaw.lan" in text and "IP Address:192.168.178.223" in text
    assert "TLS Web Server Authentication" in text and "CA:FALSE" in text and "prime256v1" in text
    assert "Subject: CN=hermclaw.lan" in text or "Subject: CN = hermclaw.lan" in text
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    first = cert.read_bytes()
    again = _playbook(tmp_path, play, extra)
    assert again.returncode == 0 and _changed(again) == 0 and cert.read_bytes() == first
    # a certificate that expires within 30 days is replaced
    gen = _run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-days",
            "5",
            "-noenc",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=old",
        ],
        cwd=tmp_path,
    )
    assert gen.returncode == 0, gen.stderr
    renew = _playbook(tmp_path, play, extra)
    assert renew.returncode == 0 and _changed(renew) >= 1
    assert "CN=hermclaw.lan" in _x509(cert).replace(" = ", "=")
    assert _run(["openssl", "x509", "-in", str(cert), "-noout", "-checkend", str(390 * 86400)], cwd=tmp_path).returncode == 0


# --------------------------------------------------------------------------------------------- final nginx site

NGINX_CONF = """user nginx;
worker_processes 1;
error_log /dev/stderr warn;
pid /tmp/nginx.pid;
events { worker_connections 128; }
http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    sendfile on;
    gzip on;
    include /etc/nginx/sites-enabled/*;
}
"""


class _Upstream:
    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                upstream.requests.append({"path": self.path, **{k.lower(): v for k, v in self.headers.items()}})
                body = json.dumps({"status": "ok", "version": "0.1.0rc1"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _podman(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return _run(["podman", *args], cwd=Path("/"), timeout=timeout)


def _render_site(tmp_path: Path, overrides: dict[str, Any]) -> Path:
    dest = tmp_path / "hermclaw-next.conf"
    tasks = [
        {
            "name": "render",
            "ansible.builtin.template": {
                "src": str(ROLES_DIR / "nginx" / "templates" / "hermclaw-next.conf.j2"),
                "dest": str(dest),
                "mode": "0644",
            },
        }
    ]
    play = _local_play(tasks)
    play[0]["vars_files"].append(str(ROLES_DIR / "nginx" / "defaults" / "main.yml"))
    res = _playbook(tmp_path, play, {"hermclaw_orchestrator_ip": "192.168.178.225", **overrides})
    assert res.returncode == 0, res.stdout[-3000:]
    return dest


@pytest.fixture(scope="module")
def nginx_image() -> str:
    if shutil.which("podman") is None or os.geteuid() != 0:
        pytest.skip("needs root and podman")
    if _podman("image", "exists", NGINX_IMAGE).returncode != 0:
        pytest.skip(f"{NGINX_IMAGE} not available locally")
    return NGINX_IMAGE


@pytest.fixture
def tls_nginx(tmp_path: Path, nginx_image: str) -> Iterator[dict[str, Any]]:
    """nginx with the final site (TLS on, HSTS on) in front of a stub API, plus a fake UI build as webroot."""
    upstream = _Upstream()
    http_port, tls_port = _free_port(), _free_port()
    tls = tmp_path / "tls"
    tls.mkdir()
    gen = _run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-days",
            "30",
            "-noenc",
            "-keyout",
            str(tls / "hermclaw-web.key"),
            "-out",
            str(tls / "hermclaw-web.crt"),
            "-subj",
            "/CN=hermclaw.lan",
            "-addext",
            "subjectAltName=DNS:hermclaw.lan,IP:127.0.0.1",
        ],
        cwd=tmp_path,
    )
    assert gen.returncode == 0, gen.stderr
    for f in tls.iterdir():
        f.chmod(0o644)
    site = _render_site(
        tmp_path,
        {
            "hermclaw_web_server_name": "hermclaw.lan",
            "nginx_tls_enabled": True,
            "nginx_listen_port": http_port,
            "nginx_tls_port": tls_port,
            "nginx_hsts_max_age": 31536000,
            "nginx_api_upstream": f"http://127.0.0.1:{upstream.port}",
        },
    )
    conf = tmp_path / "nginx.conf"
    conf.write_text(NGINX_CONF, encoding="utf-8")
    webroot = tmp_path / "webroot"
    (webroot / "assets").mkdir(parents=True)
    (webroot / "index.html").write_text('<!doctype html><div id="root"></div>\n', encoding="utf-8")
    (webroot / "assets" / "app.js").write_text("console.log(1)\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    for p in (conf, site, webroot, webroot / "assets", logs, *webroot.rglob("*")):
        p.chmod(0o777 if p == logs else (0o755 if p.is_dir() else 0o644))
    mounts = [
        "-v",
        f"{conf}:/etc/nginx/nginx.conf:ro",
        "-v",
        f"{site}:/etc/nginx/sites-enabled/hermclaw-next.conf:ro",
        "-v",
        f"{webroot}:/var/www/hermclaw-next:ro",
        "-v",
        f"{tls}:/etc/nginx/tls:ro",
        "-v",
        f"{logs}:/var/log/nginx",
    ]
    test = _podman("run", "--rm", "--network", "none", *mounts, nginx_image, "nginx", "-t")
    assert test.returncode == 0 and "test is successful" in test.stderr, test.stderr
    name = f"hcl-tls-{uuid.uuid4().hex[:8]}"
    started = _podman("run", "-d", "--rm", "--name", name, "--network", "host", *mounts, nginx_image, "nginx", "-g", "daemon off;")
    if started.returncode != 0 and "network" in started.stderr.lower():
        upstream.close()
        pytest.skip(f"podman host networking unavailable: {started.stderr.strip()[:200]}")
    assert started.returncode == 0, started.stderr
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                httpx.get(f"http://127.0.0.1:{http_port}/healthz", timeout=1.0)
                break
            except httpx.TransportError:
                if time.monotonic() > deadline:
                    pytest.fail(_podman("logs", name).stderr[-2000:])
                time.sleep(0.2)
        ctx = ssl.create_default_context(cafile=str(tls / "hermclaw-web.crt"))
        yield {
            "http": f"http://127.0.0.1:{http_port}",
            "https": f"https://127.0.0.1:{tls_port}",
            "tls_port": tls_port,
            "ssl": ctx,
            "upstream": upstream,
            "name": name,
            "logs": logs,
            "site": site.read_text(encoding="utf-8"),
        }
    finally:
        _podman("rm", "-f", "-t", "0", name)
        upstream.close()


def test_final_site_redirects_to_https_and_serves_the_ui_with_csp(tls_nginx: dict[str, Any]) -> None:
    host = {"Host": "hermclaw.lan"}
    health = httpx.get(tls_nginx["http"] + "/healthz", headers=host, timeout=5.0)
    assert health.status_code == 200 and health.text == "ok\n", "readiness probe of hosts.yaml stays 2xx on the HTTP port"
    redirect = httpx.get(tls_nginx["http"] + "/jobs/1?tab=plan", headers=host, timeout=5.0)
    assert redirect.status_code == 301
    assert redirect.headers["location"] == f"https://hermclaw.lan:{tls_nginx['tls_port']}/jobs/1?tab=plan"
    with httpx.Client(verify=tls_nginx["ssl"], timeout=5.0) as client:
        page = client.get(tls_nginx["https"] + "/jobs/1", headers=host)
        assert page.status_code == 200 and '<div id="root">' in page.text
        csp = page.headers["content-security-policy"]
        for directive in (
            "default-src 'none'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' blob:",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "object-src 'none'",
            "upgrade-insecure-requests",
        ):
            assert directive in csp, directive
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert page.headers["cache-control"] == "no-cache"
        assert page.headers["strict-transport-security"] == "max-age=31536000"
        assert page.headers["cross-origin-opener-policy"] == "same-origin" and page.headers["x-frame-options"] == "DENY"
        assert "camera=()" in page.headers["permissions-policy"]
        asset = client.get(tls_nginx["https"] + "/assets/app.js", headers=host)
        assert asset.status_code == 200 and asset.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert "content-security-policy" in asset.headers, "locations with their own add_header repeat the security headers"
        assert client.get(tls_nginx["https"] + "/assets/missing.js", headers=host).status_code == 404
        api = client.get(tls_nginx["https"] + "/api/health?access_token=hct_SECRETVALUE", headers=host)
        assert api.status_code == 200 and api.json()["status"] == "ok"
    seen = tls_nginx["upstream"].requests[-1]
    # the Host header carries nginx's port: the API's absolute redirects stay on this server, not the portal's 80/443
    assert seen["x-forwarded-proto"] == "https" and seen["host"] == f"hermclaw.lan:{tls_nginx['tls_port']}"
    curl = shutil.which("curl")
    if curl:
        h2 = _run(
            [curl, "-sk", "--http2", "-o", "/dev/null", "-w", "%{http_version}", "-H", "Host: hermclaw.lan", tls_nginx["https"] + "/"],
            cwd=Path("/"),
        )
        if h2.returncode == 0 and h2.stdout:
            assert h2.stdout.strip() == "2", "HTTP/2 lifts the six-connections limit for the SSE streams"
    time.sleep(0.5)
    access = (tls_nginx["logs"] / "hermclaw-next.access.log").read_text(encoding="utf-8")
    assert "GET /api/health HTTP" in access and "SECRETVALUE" not in access, "query strings (access_token) are never logged"


def test_final_site_without_tls_keeps_http_with_csp(tmp_path: Path, nginx_image: str) -> None:
    site = _render_site(tmp_path, {"hermclaw_web_server_name": "hermclaw.lan"})
    text = site.read_text(encoding="utf-8")
    assert "listen 8080;" in text and "ssl" not in text.split("server {", 1)[1].split("location", 1)[0]
    assert "upgrade-insecure-requests" not in text and "Strict-Transport-Security" not in text
    assert "location = /healthz" in text and "hermclaw_noquery" in text
    assert text.count('add_header Content-Security-Policy "') == 3, "server level + index.html + assets"
    conf = tmp_path / "nginx.conf"
    conf.write_text(NGINX_CONF, encoding="utf-8")
    for p in (conf, site):
        p.chmod(0o644)
    res = _podman(
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        f"{conf}:/etc/nginx/nginx.conf:ro",
        "-v",
        f"{site}:/etc/nginx/sites-enabled/hermclaw-next.conf:ro",
        nginx_image,
        "nginx",
        "-t",
    )
    assert res.returncode == 0 and "test is successful" in res.stderr, res.stderr


# --------------------------------------------------------------------------------------------- Kontrollraum button (D-013)

PORTAL_TEMPLATE = ROLES_DIR / "hermclaw_ui" / "templates" / "portal-kontrollraum.php.j2"
PORTAL_STATIC = REPO / "integrations" / "kowacore-portal" / "nav" / "work_ki_kontrollraum.php"
PORTAL_FIELDS = ("ICON", "TITLE", "PRIO", "BEREICH", "RECHT")


def _portal_meta(head_bytes: bytes) -> dict[str, str]:
    """navigation_meta_laden() of the legacy portal (inc/funktionen_navigation.php), reimplemented: the first 700 bytes,
    preg_match('/<FIELD>:\\s*(.+)/') per field - unanchored, the first match wins, PCRE '.' stops at a line break,
    trim() -, all five fields or no menu entry at all, URL: optional (it replaces index.php?seite=<file>)."""
    head = head_bytes[:700].decode("latin-1")
    meta: dict[str, str] = {}
    for field in PORTAL_FIELDS:
        if m := re.search(field + r":\s*(.+)", head):
            meta[field] = m.group(1).strip(" \t\n\r\0\x0b")
    if len(meta) != len(PORTAL_FIELDS):
        return {}
    if m := re.search(r"URL:\s*(.+)", head):
        meta["URL"] = m.group(1).strip(" \t\n\r\0\x0b")
    return meta


def _portal_vars(**extra: Any) -> dict[str, Any]:
    return {"ansible_host": "192.168.178.223", "hermclaw_version": "0.1.0rc1", **extra}


def _render_portal_button(tmp_path: Path, name: str, **extra: Any) -> bytes:
    dest = tmp_path / f"{name}.php"
    play = _local_play([{"name": "render", "ansible.builtin.template": {"src": str(PORTAL_TEMPLATE), "dest": str(dest), "mode": "0644"}}])
    play[0]["vars_files"] += [str(ROLES_DIR / r / "defaults" / "main.yml") for r in ("nginx", "hermclaw_ui")]
    res = _playbook(tmp_path, play, {"hermclaw_orchestrator_ip": "192.168.178.225", **_portal_vars(**extra)})
    assert res.returncode == 0, res.stdout[-3000:]
    return dest.read_bytes()


def test_portal_button_parses_like_a_menu_entry_of_the_portal(tmp_path: Path) -> None:
    default = _render_portal_button(tmp_path, "default")
    meta = _portal_meta(default)
    assert meta == {"ICON": "cpu", "TITLE": "KI-Kontrollraum", "PRIO": "19", "BEREICH": "work", "RECHT": "dashboard_anzeigen"}
    assert "URL" not in meta, "the menu entry opens the portal page with the button (index.php?seite=work_ki_kontrollraum)"
    assert default == PORTAL_STATIC.read_bytes(), "integrations/kowacore-portal is the deployment's file with the defaults"
    text = default.decode("ascii")  # ASCII only: umlauts as entities, like the portal's own pages
    header_end = text.index("?>") + 2
    assert header_end < 700 and text.startswith("<?php\n/*\n") and text.count("<?") == 1 and text.count("?>") == 1
    assert "Managed by Hermclaw" in text[:header_end], "the deployment's marker (never replaces a foreign page)"
    assert '<a class="button button--primaer" href="http://192.168.178.223:8080/" target="_blank" rel="noopener noreferrer">' in text
    assert "KI-Kontrollraum &ouml;ffnen</a>" in text and "<code>http://192.168.178.223:8080/</code>" in text
    assert "<iframe" not in text and 'seitenkopf__eyebrow">Arbeit<' in text and "selbstsignierte" not in text
    for word in ("ICON", "TITLE", "PRIO", "BEREICH", "RECHT", "URL"):
        assert len(re.findall(word + ":", text[:700])) == (1 if word != "URL" else 0), f"{word}: appears once in the header"

    tls = _render_portal_button(tmp_path, "tls", nginx_tls_enabled=True, hermclaw_web_server_name="hermclaw.lan")
    assert 'href="https://192.168.178.223:8443/"' in tls.decode("ascii") and b"selbstsignierte Zertifikat" in tls
    direct = _render_portal_button(tmp_path, "direct", hermclaw_ui_portal_link_direct=True, hermclaw_ui_portal_link_title="Kontrollraum")
    meta = _portal_meta(direct)
    assert meta["URL"] == "http://192.168.178.223:8080/" and meta["TITLE"] == "Kontrollraum", "the menu entry jumps there directly"
    dedicated = _render_portal_button(tmp_path, "dedicated", hermclaw_webui_shared_host=False, hermclaw_webui_http_port=80)
    assert b'href="http://192.168.178.223/"' in dedicated
    # a header the portal would not take as a menu entry (negative control of the parser)
    assert _portal_meta(b"<?php\n/*\nICON: cpu\nTITLE: x\nPRIO: 1\nBEREICH: work\n*/ ?>") == {}
    assert _portal_meta(b"<?php /* " + b" " * 700 + b"ICON: cpu */ ?>") == {}


def _portal_tree(root: Path, *, owner: int = 65534, group: int = 65534) -> Path:
    (root / "nav").mkdir(parents=True)
    (root / "index.php").write_text("<?php // front controller of the stand-in portal\n", encoding="utf-8")
    (root / "nav" / "main_dashboard.php").write_text("<?php\n/*\nICON: layout-dashboard\nTITLE: Dashboard\n*/ ?>\n", encoding="utf-8")
    os.chown(root / "nav", owner, group)
    return root / "nav" / "work_ki_kontrollraum.php"


def _portal_run(tmp_path: Path, root: Path, *args: str, **extra: Any) -> subprocess.CompletedProcess[str]:
    tasks = [_include("hermclaw_ui", "portal_link.yml")]
    return _playbook(tmp_path, _local_play(tasks), {**_portal_vars(hermclaw_ui_legacy_portal_root=str(root)), **extra}, args=args)


def test_portal_button_is_installed_idempotently_with_the_portal_owner(tmp_path: Path) -> None:
    if os.geteuid() != 0:
        pytest.skip("needs root (owner of the portal's nav/)")
    root = tmp_path / "noobclaw"
    button = _portal_tree(root)
    before = sorted(p.name for p in (root / "nav").iterdir())
    dry = _portal_run(tmp_path, root, "--check")
    assert dry.returncode == 0 and _changed(dry) == 1 and not button.exists(), "--check reports the button, writes nothing"
    first = _portal_run(tmp_path, root)
    assert first.returncode == 0 and _changed(first) == 1, first.stdout[-3000:]
    st = button.stat()
    assert (st.st_uid, st.st_gid, stat_mode(st.st_mode)) == (65534, 65534, 0o644), "owner/group of nav/, mode 0644"
    assert button.read_bytes() == PORTAL_STATIC.read_bytes()
    assert sorted(p.name for p in (root / "nav").iterdir()) == sorted([*before, button.name]), "nothing else changes"
    second = _portal_run(tmp_path, root)
    assert second.returncode == 0 and _changed(second) == 0, "idempotent"
    # a changed address is rewritten; switched off, Hermclaw's button is removed again
    moved = _portal_run(tmp_path, root, hermclaw_webui_http_port=9080)
    assert moved.returncode == 0 and b"http://192.168.178.223:9080/" in button.read_bytes()
    off = _portal_run(tmp_path, root, hermclaw_ui_portal_link=False)
    assert off.returncode == 0 and not button.exists() and (root / "nav" / "main_dashboard.php").exists()
    assert _portal_run(tmp_path, root, hermclaw_ui_portal_link=False).returncode == 0, "absent stays absent"


def stat_mode(mode: int) -> int:
    return mode & 0o7777


@pytest.mark.parametrize(
    ("layout", "extra", "outcome"),
    [
        ("no-portal", {}, "skip"),
        ("index-only", {}, "skip"),
        ("nav-only", {}, "skip"),
        ("foreign-page", {}, "refused"),
        ("foreign-page", {"hermclaw_ui_portal_link": False}, "refused"),
        ("symlink", {}, "refused"),
        ("portal", {"hermclaw_ui_portal_link_url": "javascript:alert(1)//"}, "invalid"),
        ("portal", {"hermclaw_ui_portal_link_url": 'http://192.168.178.223:8080/" onmouseover="x'}, "invalid"),
        ("portal", {"hermclaw_ui_portal_link_name": "../index"}, "invalid"),
        ("portal", {"hermclaw_ui_portal_link_title": "KI\nURL: http://evil/"}, "invalid"),
        ("portal", {"hermclaw_ui_portal_link_title": "RECHT: x"}, "invalid"),
        ("portal", {"hermclaw_ui_portal_link_right": "__ausgeblendet__"}, "invalid"),
        ("portal", {"hermclaw_ui_portal_link_prio": "19; x"}, "invalid"),
        ("portal-in-webroot", {}, "invalid"),
    ],
)
def test_portal_button_never_touches_an_unknown_or_foreign_page(tmp_path: Path, layout: str, extra: dict[str, Any], outcome: str) -> None:
    root = tmp_path / "noobclaw"
    button = root / "nav" / "work_ki_kontrollraum.php"
    if layout in ("portal", "foreign-page", "symlink"):
        _portal_tree(root, owner=os.getuid(), group=os.getgid())
    elif layout == "index-only":
        root.mkdir()
        (root / "index.php").write_text("<?php\n", encoding="utf-8")
    elif layout == "nav-only":
        (root / "nav").mkdir(parents=True)
    elif layout == "portal-in-webroot":
        extra = {**extra, "hermclaw_webroot": str(tmp_path), "hermclaw_ui_releases_dir": str(tmp_path) + "-releases"}
        _portal_tree(root, owner=os.getuid(), group=os.getgid())
    if layout == "foreign-page":
        button.write_text("<?php\n/*\nICON: cpu\nTITLE: Eigene Seite\nPRIO: 19\nBEREICH: work\nRECHT: x\n*/ ?>\n", encoding="utf-8")
    if layout == "symlink":
        (tmp_path / "elsewhere.php").write_text("<?php // Managed by Hermclaw (planted)\n", encoding="utf-8")
        button.symlink_to(tmp_path / "elsewhere.php")
    snapshot = {p: (p.read_bytes() if p.is_file() else None) for p in tmp_path.rglob("*") if not p.name.endswith((".yml", ".json"))}
    res = _portal_run(tmp_path, root, **extra)
    if outcome == "skip":
        assert res.returncode == 0 and _changed(res) == 0 and not button.exists(), res.stdout[-3000:]
    elif outcome == "refused":
        assert res.returncode != 0 and "not Hermclaw's button" in res.stdout, res.stdout[-3000:]
    else:
        assert res.returncode != 0 and "invalid portal button settings" in res.stdout, res.stdout[-3000:]
    after = {p: (p.read_bytes() if p.is_file() else None) for p in tmp_path.rglob("*") if not p.name.endswith((".yml", ".json"))}
    assert {p: v for p, v in after.items() if "ansible" not in str(p)} == {p: v for p, v in snapshot.items() if "ansible" not in str(p)}


@pytest.mark.parametrize(
    ("extra", "base", "with_nginx_role"),
    [
        ({}, "http://192.168.178.223:8080", True),
        ({}, "http://192.168.178.223:8080", False),
        ({"nginx_tls_enabled": True}, "https://192.168.178.223:8443", True),
        ({"nginx_tls_enabled": True}, "https://192.168.178.223:8443", False),
        ({"nginx_listen_port": 9080}, "http://192.168.178.223:9080", False),
        ({"hermclaw_webui_http_port": 80}, "http://192.168.178.223", True),
        ({"hermclaw_webui_https_port": 443, "nginx_tls_enabled": True}, "https://192.168.178.223", True),
    ],
)
def test_ui_health_and_portal_urls_follow_the_nginx_ports(tmp_path: Path, extra: dict[str, Any], base: str, with_nginx_role: bool) -> None:
    out = tmp_path / "urls.json"
    play = _local_play(
        [
            {
                "name": "dump",
                "ansible.builtin.copy": {
                    "content": "{{ {'base': hermclaw_ui_base_url, 'link': hermclaw_ui_portal_link_url} | to_json }}",
                    "dest": str(out),
                    "mode": "0644",
                },
            }
        ]
    )
    play[0]["vars_files"] += [
        str(ROLES_DIR / r / "defaults" / "main.yml") for r in (["nginx"] if with_nginx_role else []) + ["hermclaw_ui"]
    ]
    res = _playbook(tmp_path, play, {"hermclaw_orchestrator_ip": "192.168.178.225", **_portal_vars(**extra)})
    assert res.returncode == 0, res.stdout[-3000:]
    urls = json.loads(out.read_text(encoding="utf-8"))
    assert urls == {"base": base, "link": base + "/"}
