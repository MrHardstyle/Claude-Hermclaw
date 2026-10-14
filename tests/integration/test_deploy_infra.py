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
NGINX_IMAGE = "docker.io/library/nginx:1.28"
BUILD = REPO / "scripts" / "deploy" / "build-release.sh"


# --------------------------------------------------------------------------------------------- helpers


def _env(tmp_path: Path) -> dict[str, str]:
    env = _ansible_env(tmp_path)
    env["ANSIBLE_ROLES_PATH"] = str(ROLES_DIR)
    return env


def _playbook(
    tmp_path: Path, plays: list[dict[str, Any]], extra: dict[str, Any], *, inventory: Path | None = None, args: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    name = uuid.uuid4().hex[:8]
    pb = tmp_path / f"play-{name}.yml"
    pb.write_text(yaml.safe_dump(plays, width=1000), encoding="utf-8")
    ev = tmp_path / f"extra-{name}.json"
    ev.write_text(json.dumps(extra), encoding="utf-8")
    inv = str(inventory) if inventory else "localhost,"
    return _run([str(ANSIBLE_PLAYBOOK), "-i", inv, str(pb), "-e", f"@{ev}", *args], env=_env(tmp_path), cwd=tmp_path, timeout=900)


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
    assert [t["ansible.builtin.include_role"]["tasks_from"] for t in pre["tasks"]] == ["preflight.yml", "data.yml"]
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
        assert all(k.startswith(role + "_") for k in defaults), f"{role}: role variables carry the role prefix"


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


# --------------------------------------------------------------------------------------------- input validation

VALIDATION_CASES: list[tuple[str, str, str, dict[str, Any], str]] = [
    ("release-defaults", "hermclaw_release", "validate.yml", {}, "PASS"),
    ("release-keep-0", "hermclaw_release", "validate.yml", {"hermclaw_keep_releases": 0}, "PASS"),
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
    ("nginx-tls-same-port", "nginx", "validate.yml", {"hermclaw_web_server_name": "hermclaw.lan", "nginx_tls_port": 80}, "FAIL"),
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
                "webui": {"hosts": {"w": {"ansible_host": "10.9.0.23", "hermclaw_host_id": "webui-223"}}},
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
    assert hosts["webui-223"]["services"] == [{"name": "nginx", "port": 80, "http_path": "/healthz"}]
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
                f'#!/bin/sh\necho "{v.name}/{tool} $* env=$HERMCLAW_ENV home=$HOME"\nread -r line && echo "stdin=$line"\nexit 3\n',
                encoding="utf-8",
            )
            exe.chmod(0o755)
    conf = root / "etc" / "hermclaw"
    conf.mkdir(parents=True)
    (conf / "hermclaw.env").write_text("HERMCLAW_ENV=production\n", encoding="utf-8")
    store = root / "credstore"
    store.mkdir()
    for name in ("litellm-master-key", "worker-token-exec-222"):
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
        f"LoadCredentialEncrypted=litellm-master-key:{cli['store']}/hermclaw-litellm-master-key",
        f"LoadCredentialEncrypted=worker-token-exec-222:{cli['store']}/hermclaw-worker-token-exec-222",
    ]
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


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (["--venv", "/nonexistent/venv", "doctor"], 69, "is not installed"),
        (["--venv", "/opt/../tmp", "doctor"], 69, "is not installed"),
        (["--venv"], 64, "--venv needs a directory"),
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
    assert health.status_code == 200 and health.text == "ok\n", "readiness probe of hosts.yaml stays 2xx on port 80"
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
    assert seen["x-forwarded-proto"] == "https" and seen["host"] == "hermclaw.lan"
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
    assert "listen 80;" in text and "ssl" not in text.split("server {", 1)[1].split("location", 1)[0]
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
