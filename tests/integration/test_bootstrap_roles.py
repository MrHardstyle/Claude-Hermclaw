"""P01 1.15 – behaviour of individual bootstrap task files against fake host tools and real kernels.

The decision logic of the roles (NIC Wake-on-LAN checks, nginx include check, apt candidate and
package conflict checks of the NVIDIA driver) runs with Ansible itself on localhost; the host
tools it reads (ethtool, apt-cache, dpkg-query, nginx.conf) are replaced by fakes in a private
PATH (test code only). The Ollama firewall table is loaded with the real nft into the network
namespace of a real Podman container and probed with real TCP connections.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.integration.test_bootstrap_infra import (
    ANSIBLE_DIR,
    ANSIBLE_PLAYBOOK,
    GROUP_VARS,
    PLAYBOOKS_DIR,
    ROLES_DIR,
    SYSTEMD_DIR,
    _ansible_env,
    _iter_tasks,
    _run,
)

pytestmark = pytest.mark.integration

Case = tuple[str, str, str, dict[str, Any], dict[str, str], str]


def _fake_tool(bindir: Path, name: str, body: str) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    path = bindir / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _run_cases(tmp_path: Path, cases: list[Case], bindir: Path | None = None) -> dict[str, str]:
    """Run role task files (one play per case) and return case id -> PASS/FAIL."""
    plays = []
    for case_id, role, tasks_from, case_vars, case_env, _ in cases:
        env = dict(case_env)
        if bindir is not None:
            env["PATH"] = f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"
        plays.append(
            {
                "name": case_id,
                "hosts": "localhost",
                "connection": "local",
                "gather_facts": False,
                "become": False,
                "vars_files": [str(GROUP_VARS)],
                "vars": {"hermclaw_orchestrator_ip": "192.168.178.225"},
                "environment": env,
                "tasks": [
                    {
                        "name": "case",
                        "block": [
                            {
                                "name": "run the task file",
                                "ansible.builtin.include_role": {"name": role, "tasks_from": tasks_from},
                                "vars": case_vars,
                            },
                            {"name": "report", "ansible.builtin.debug": {"msg": f"HERMCLAW_CASE {case_id} PASS"}},
                        ],
                        "rescue": [{"name": "report", "ansible.builtin.debug": {"msg": f"HERMCLAW_CASE {case_id} FAIL"}}],
                    }
                ],
            }
        )
    pb = tmp_path / "cases.yml"
    pb.write_text(yaml.safe_dump(plays, width=1000), encoding="utf-8")
    env = _ansible_env(tmp_path)
    env["ANSIBLE_ROLES_PATH"] = str(ROLES_DIR)
    res = _run([str(ANSIBLE_PLAYBOOK), "-i", "localhost,", str(pb)], env=env, cwd=tmp_path, timeout=600)
    assert res.returncode == 0, res.stdout[-4000:] + res.stderr[-2000:]
    outcomes = dict(re.findall(r"HERMCLAW_CASE (\S+) (PASS|FAIL)", res.stdout))
    wrong = {c[0]: outcomes.get(c[0]) for c in cases if outcomes.get(c[0]) != c[5]}
    assert not wrong, f"unexpected outcomes {wrong}\n{res.stdout[-3000:]}"
    return outcomes


# --------------------------------------------------------------------------------------------- Wake-on-LAN

ETHTOOL_OK = """Settings for enp3s0:
\tSupported ports: [ TP ]
\tSupports Wake-on: pumbg
\tWake-on: g
\tLink detected: yes
"""
ETHTOOL_OFF = ETHTOOL_OK.replace("\tWake-on: g", "\tWake-on: d")
ETHTOOL_NO_MAGIC = ETHTOOL_OFF.replace("Supports Wake-on: pumbg", "Supports Wake-on: pumb")
ETHTOOL_NONE = "Settings for wlan0:\n\tLink detected: yes\n"


def test_wol_ethtool_checks(tmp_path: Path) -> None:
    """support.yml needs magic-packet support, verify.yml needs the active mode to be exactly g."""
    bindir = tmp_path / "bin"
    _fake_tool(bindir, "ethtool", '[ "$1" = "$FAKE_IF" ] || exit 75\ncat "$FAKE_ETHTOOL"\n')
    outputs = {"ok": ETHTOOL_OK, "off": ETHTOOL_OFF, "nomagic": ETHTOOL_NO_MAGIC, "none": ETHTOOL_NONE}
    for name, text in outputs.items():
        (tmp_path / f"{name}.txt").write_text(text, encoding="utf-8")

    def env(name: str) -> dict[str, str]:
        return {"FAKE_ETHTOOL": str(tmp_path / f"{name}.txt"), "FAKE_IF": "enp3s0"}

    wol_vars = {"wol_target_if": "enp3s0"}
    cases: list[Case] = [
        ("support-ok", "wol_target", "support.yml", wol_vars, env("off"), "PASS"),
        ("support-no-magic-packet", "wol_target", "support.yml", wol_vars, env("nomagic"), "FAIL"),
        ("support-no-wol-info", "wol_target", "support.yml", wol_vars, env("none"), "FAIL"),
        ("verify-active-g", "wol_target", "verify.yml", wol_vars, env("ok"), "PASS"),
        # "Supports Wake-on: pumbg" contains g but the active mode is d
        ("verify-disabled", "wol_target", "verify.yml", wol_vars, env("off"), "FAIL"),
        ("verify-no-wol-info", "wol_target", "verify.yml", wol_vars, env("none"), "FAIL"),
        # the interface name reaches ethtool unchanged (fake exits 75 for any other name)
        ("support-wrong-interface", "wol_target", "support.yml", {"wol_target_if": "eth9"}, env("ok"), "FAIL"),
    ]
    _run_cases(tmp_path, cases, bindir)


# --------------------------------------------------------------------------------------------- nginx

NGINX_DEBIAN = """user www-data;
http {
\tinclude /etc/nginx/mime.types;
\tinclude /etc/nginx/conf.d/*.conf;
\tinclude /etc/nginx/sites-enabled/*;
}
"""


def test_nginx_sites_include_check(tmp_path: Path) -> None:
    variants = {
        "debian": (NGINX_DEBIAN, "PASS"),
        "spaces": (NGINX_DEBIAN.replace("\tinclude /etc/nginx/sites-enabled/*;", "    include    /etc/nginx/sites-enabled/*.conf;"), "PASS"),
        "commented": (NGINX_DEBIAN.replace("\tinclude /etc/nginx/sites-enabled/*;", "\t# include /etc/nginx/sites-enabled/*;"), "FAIL"),
        "nginx-org": (NGINX_DEBIAN.replace("\tinclude /etc/nginx/sites-enabled/*;\n", ""), "FAIL"),
        "other-dir": (NGINX_DEBIAN.replace("/etc/nginx/sites-enabled/*", "/srv/nginx/sites-enabled/*"), "FAIL"),
    }  # fmt: skip
    cases: list[Case] = []
    for name, (text, want) in variants.items():
        conf = tmp_path / f"nginx-{name}.conf"
        conf.write_text(text, encoding="utf-8")
        cases.append((f"nginx-{name}", "nginx", "sites_include.yml", {"nginx_main_conf_path": str(conf)}, {}, want))
    _run_cases(tmp_path, cases)
    real_debian = Path("/etc/nginx/nginx.conf")
    if real_debian.is_file() and shutil.which("nginx"):
        _run_cases(tmp_path, [("nginx-host", "nginx", "sites_include.yml",
                                {"nginx_main_conf_path": str(real_debian)}, {}, "PASS")])  # fmt: skip


# --------------------------------------------------------------------------------------------- NVIDIA


def _policy(pkg: str, candidate: str, installed: str = "(none)") -> str:
    table = "" if candidate == "(none)" else f"     {candidate} 500\n        500 https://example.invalid/repo  Packages\n"
    return f"{pkg}:\n  Installed: {installed}\n  Candidate: {candidate}\n  Version table:\n{table}"


def test_nvidia_candidate_check(tmp_path: Path) -> None:
    """Driver installs from apt only start when the pinned branch is actually installable."""
    bindir = tmp_path / "bin"
    # apt-cache policy <pkg>: prints $FAKE_APT/<pkg> when it exists, nothing otherwise (unknown package)
    _fake_tool(bindir, "apt-cache", '[ "$1" = policy ] || exit 64\n[ -f "$FAKE_APT/$2" ] && cat "$FAKE_APT/$2"\nexit 0\n')

    def repo(name: str, packages: Mapping[str, str]) -> dict[str, str]:
        d = tmp_path / f"apt-{name}"
        d.mkdir()
        for pkg, candidate in packages.items():
            (d / pkg).write_text(_policy(pkg, candidate), encoding="utf-8")
        return {"FAKE_APT": str(d)}

    cuda_vars = {"nvidia_source": "cuda", "nvidia_candidate_present": ["nvidia-driver-pinning-580"],
                 "nvidia_candidate_branch": ["cuda-drivers"]}  # fmt: skip
    debian_vars = {"nvidia_source": "debian", "nvidia_candidate_present": [], "nvidia_candidate_branch": ["nvidia-driver"]}
    cases: list[Case] = [
        ("cuda-580-available", "nvidia", "candidate.yml", cuda_vars,
         repo("cuda-ok", {"nvidia-driver-pinning-580": "580.95.05-1", "cuda-drivers": "580.95.05-1"}), "PASS"),
        # NVIDIA's debian13 repo on 2026-10-14: only 590+, which the Hermclaw pin turns into (none)
        ("cuda-debian13-590-only", "nvidia", "candidate.yml", cuda_vars, repo("cuda-590", {"cuda-drivers": "(none)"}), "FAIL"),
        ("cuda-pinning-package-missing", "nvidia", "candidate.yml", cuda_vars,
         repo("cuda-nopin", {"cuda-drivers": "580.95.05-1"}), "FAIL"),
        ("cuda-wrong-branch-candidate", "nvidia", "candidate.yml", cuda_vars,
         repo("cuda-590b", {"nvidia-driver-pinning-580": "1.0-1", "cuda-drivers": "590.48.01-1"}), "FAIL"),
        ("cuda-epoch-version", "nvidia", "candidate.yml", cuda_vars,
         repo("cuda-epoch", {"nvidia-driver-pinning-580": "580.95.05-1", "cuda-drivers": "1:580.95.05-1"}), "PASS"),
        ("debian-550-for-branch-580", "nvidia", "candidate.yml", debian_vars,
         repo("deb-550", {"nvidia-driver": "550.163.01-4"}), "FAIL"),
        ("debian-550-for-branch-550", "nvidia", "candidate.yml", {**debian_vars, "nvidia_driver_branch": "550"},
         repo("deb-550b", {"nvidia-driver": "550.163.01-4"}), "PASS"),
        # 5800.x must not pass as branch 580
        ("debian-prefix-is-not-branch", "nvidia", "candidate.yml", debian_vars,
         repo("deb-5800", {"nvidia-driver": "5800.1-1"}), "FAIL"),
    ]  # fmt: skip
    _run_cases(tmp_path, cases, bindir)


def test_nvidia_runfile_conflict_check(tmp_path: Path) -> None:
    """A runfile driver is never installed next to packaged Debian/CUDA drivers."""
    bindir = tmp_path / "bin"
    # dpkg-query -W -f=... patterns...: prints $FAKE_DPKG and exits with $FAKE_RC; records its argv
    _fake_tool(bindir, "dpkg-query", 'printf "%s\\n" "$@" > "$FAKE_ARGS"\n[ -n "$FAKE_DPKG" ] && printf "$FAKE_DPKG"\nexit "$FAKE_RC"\n')

    def env(name: str, listing: str, rc: int) -> dict[str, str]:
        return {"FAKE_DPKG": listing, "FAKE_RC": str(rc), "FAKE_ARGS": str(tmp_path / f"args-{name}")}

    cases: list[Case] = [
        ("no-driver-packages", "nvidia", "conflicts.yml", {}, env("none", "", 1), "PASS"),
        ("debian-driver-installed", "nvidia", "conflicts.yml", {}, env("deb", "nvidia-driver ii \\n", 0), "FAIL"),
        ("cuda-drivers-unpacked", "nvidia", "conflicts.yml", {}, env("cuda", "cuda-drivers iU \\n", 0), "FAIL"),
        ("only-config-files-left", "nvidia", "conflicts.yml", {}, env("rc", "nvidia-kernel-dkms rc \\n", 1), "PASS"),
        ("dpkg-error", "nvidia", "conflicts.yml", {}, env("err", "", 2), "FAIL"),
    ]
    _run_cases(tmp_path, cases, bindir)
    argv = (tmp_path / "args-none").read_text(encoding="utf-8").splitlines()
    assert argv[0] == "-W" and argv[1].startswith("-f=${Package} ${db:Status-Abbrev}")
    defaults = yaml.safe_load((ROLES_DIR / "nvidia" / "defaults" / "main.yml").read_text(encoding="utf-8"))
    assert [a for a in argv[2:] if a] == defaults["nvidia_package_conflicts"]


def test_nvidia_secure_boot_guard(tmp_path: Path) -> None:
    """Unsigned DKMS modules do not load under Secure Boot: refuse unless the MOK key is enrolled."""

    def efivar(name: str, state: int, attributes: int = 0x06) -> str:
        path = tmp_path / f"SecureBoot-{name}"
        path.write_bytes(bytes([attributes, 0, 0, 0, state]))
        return str(path)

    on, off = efivar("on", 1), efivar("off", 0)
    cases: list[Case] = [
        ("sb-enabled", "nvidia", "secureboot.yml", {"nvidia_secureboot_efivar": on}, {}, "FAIL"),
        ("sb-enabled-other-attributes", "nvidia", "secureboot.yml", {"nvidia_secureboot_efivar": efivar("on27", 1, 0x27)}, {}, "FAIL"),
        ("sb-enabled-mok-enrolled", "nvidia", "secureboot.yml", {"nvidia_secureboot_efivar": on, "nvidia_secureboot_mok_enrolled": True}, {}, "PASS"),
        ("sb-disabled", "nvidia", "secureboot.yml", {"nvidia_secureboot_efivar": off}, {}, "PASS"),
        ("legacy-boot", "nvidia", "secureboot.yml", {"nvidia_secureboot_efivar": str(tmp_path / "missing")}, {}, "PASS"),
    ]  # fmt: skip
    _run_cases(tmp_path, cases)


# --------------------------------------------------------------------------------------------- Ollama firewall

PY_IMAGE = "docker.io/library/python:3.12-slim"


def _podman(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return _run(["podman", *args], cwd=Path("/"), timeout=timeout)


def _render_one(tmp_path: Path, template: Path, overrides: Mapping[str, Any]) -> Path:
    dest = tmp_path / template.name.removesuffix(".j2")
    play = [
        {
            "name": "render",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "become": False,
            "vars_files": [str(GROUP_VARS), str(ROLES_DIR / "ollama" / "defaults" / "main.yml")],
            "tasks": [{"name": "render", "ansible.builtin.template": {"src": str(template), "dest": str(dest), "mode": "0644"}}],
        }
    ]
    pb = tmp_path / "render-one.yml"
    pb.write_text(yaml.safe_dump(play), encoding="utf-8")
    extra = tmp_path / "render-one.json"
    extra.write_text(json.dumps(dict(overrides)), encoding="utf-8")
    res = _run([str(ANSIBLE_PLAYBOOK), "-i", "localhost,", str(pb), "-e", f"@{extra}"], env=_ansible_env(tmp_path), cwd=tmp_path)
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-2000:]
    return dest


@pytest.fixture
def podman_net() -> Iterator[tuple[str, str]]:
    """A throw-away rootful Podman bridge network: (name, subnet prefix like '10.89.3.')."""
    if os.geteuid() != 0 or not all(shutil.which(t) for t in ("podman", "nsenter", "nft")):
        pytest.skip("needs root, podman, nsenter and nft")
    if _podman("image", "exists", PY_IMAGE).returncode != 0:
        pytest.skip(f"{PY_IMAGE} not available locally")
    name = f"hclfw-{uuid.uuid4().hex[:8]}"
    if _podman("network", "create", name).returncode != 0:
        pytest.skip("cannot create a podman network here")
    try:
        info = json.loads(_podman("network", "inspect", name).stdout)[0]
        subnet = info["subnets"][0]["subnet"]
        assert subnet.endswith(".0/24"), subnet
        yield name, subnet[: -len("0/24")]
    finally:
        for cid in _podman("ps", "-aq", "--filter", f"network={name}").stdout.split():
            _podman("rm", "-f", cid)
        _podman("network", "rm", "-f", name)


CONNECT = (
    "import socket,sys\n"
    "s=socket.socket(); s.settimeout(3)\n"
    "try:\n s.connect((sys.argv[1], int(sys.argv[2]))); print('OPEN')\n"
    "except OSError as e:\n print('BLOCKED', type(e).__name__); sys.exit(3)\n"
)


def test_ollama_firewall_blocks_foreign_sources(tmp_path: Path, podman_net: tuple[str, str]) -> None:
    net, prefix = podman_net
    server_ip, allowed_ip, foreign_ip = prefix + "10", prefix + "20", prefix + "30"
    table = _render_one(
        tmp_path,
        ROLES_DIR / "ollama" / "templates" / "hermclaw-ollama.nft.j2",
        {"ollama_firewall_allowed_sources": [allowed_ip, "fd00:1::/64"]},
    )
    text = table.read_text(encoding="utf-8")
    assert f"ip saddr {{ {allowed_ip} }} tcp dport 11434 accept" in text and "ip6 saddr { fd00:1::/64 }" in text
    srv = _podman("run", "-d", "--rm", "--name", f"{net}-srv", "--network", net, "--ip", server_ip, PY_IMAGE, "sh", "-c",
                  "python -m http.server 11434 & exec python -m http.server 8080")  # fmt: skip
    assert srv.returncode == 0, srv.stderr
    pid = _podman("inspect", "-f", "{{.State.Pid}}", f"{net}-srv").stdout.strip()

    def probe(source: str, port: int) -> str:
        res = _podman("run", "--rm", "--network", net, "--ip", source, PY_IMAGE, "python", "-c", CONNECT, server_ip, str(port))
        return res.stdout.strip().split(" ")[0] or res.stderr.strip()[-200:]

    def in_server_ns(*argv: str) -> subprocess.CompletedProcess[str]:
        return _run(["nsenter", "-t", pid, "-n", *argv], cwd=tmp_path, timeout=60)

    # wait until both listeners are up (seen from the allowed client, before any rule exists)
    for _ in range(20):
        if probe(allowed_ip, 11434) == "OPEN" and probe(allowed_ip, 8080) == "OPEN":
            break
    assert probe(foreign_ip, 11434) == "OPEN", "precondition: reachable without the firewall"

    nft = shutil.which("nft") or "nft"
    for _ in range(2):  # loading twice = reload: the table is replaced atomically, never duplicated
        load = in_server_ns(nft, "-f", str(table))
        assert load.returncode == 0, load.stderr
    tables = in_server_ns(nft, "list", "tables").stdout
    assert tables.count("table inet hermclaw_ollama") == 1, tables

    assert probe(allowed_ip, 11434) == "OPEN"
    assert probe(foreign_ip, 11434) == "BLOCKED"
    assert probe(foreign_ip, 8080) == "OPEN", "only the Ollama port is restricted"
    loopback = in_server_ns("python3", "-c", CONNECT, "127.0.0.1", "11434")
    assert loopback.stdout.startswith("OPEN"), loopback.stdout + loopback.stderr
    counters = in_server_ns(nft, "list", "table", "inet", "hermclaw_ollama").stdout
    assert re.search(r"tcp dport 11434 counter packets [1-9]", counters), counters

    # ExecStop of hermclaw-ollama-firewall.service removes the restriction again
    unit = (SYSTEMD_DIR / "hermclaw-ollama-firewall.service.j2").read_text(encoding="utf-8")
    stop = re.search(r"^ExecStop=\{\{ ollama_firewall_nft_bin \}\} (.+)$", unit, re.M)
    assert stop is not None
    assert in_server_ns(nft, *stop.group(1).split()).returncode == 0
    assert probe(foreign_ip, 11434) == "OPEN"


# --------------------------------------------------------------------------------------------- tag inheritance


def test_dynamic_includes_apply_their_tags() -> None:
    """include_tasks does not pass its tags on: without apply.tags, `--tags <role>` silently skips the file."""
    found = 0
    for f in sorted(ROLES_DIR.glob("*/tasks/*.yml")) + sorted(PLAYBOOKS_DIR.glob("*.yml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or []
        tasks = [t for play in data for t in play.get("tasks", [])] if f.parent == PLAYBOOKS_DIR else data
        for task in _iter_tasks(tasks):
            spec = task.get("ansible.builtin.include_tasks")
            if spec is None:
                continue
            found += 1
            assert isinstance(spec, dict) and spec.get("apply", {}).get("tags"), f"{f.name}: {task.get('name')!r}"
    assert found >= 5


def test_ansible_dir_is_selfcontained() -> None:
    """Roles only reference files inside the repository (no absolute host paths for sources)."""
    for f in ROLES_DIR.rglob("*.yml"):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"src: \"\{\{ role_path \}\}/([^\"]+)\"", text):
            target = (f.parents[1] / m.group(1)).resolve()
            assert target.exists() and ANSIBLE_DIR.parent.parent in target.parents, (f, m.group(1))
