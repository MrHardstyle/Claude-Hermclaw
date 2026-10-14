"""P01 1.5 / 1.9 / 1.11 – version gates of the build tooling (Python, Git, Node.js).

The real task files run (``common/tasks/python.yml``, ``common/tasks/git.yml``,
``nodejs/tasks/main.yml`` with package installation off) against fake binaries that print
real-world version strings, and against the real binaries of this build host. The
gates must accept every supported version and refuse everything older (or, for Node.js,
another major line: Vite needs Node >= 20.19 / >= 22.12, research 20261008-018).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.integration.test_bootstrap_roles import Case, _fake_tool, _run_cases

pytestmark = pytest.mark.integration


def test_node_version_gate(tmp_path: Path) -> None:
    versions = {
        "lts-22": ("v22.20.0", "PASS"),
        "minimum": ("v22.12.0", "PASS"),
        "below-vite-minimum": ("v22.11.0", "FAIL"),
        "old-major": ("v20.19.5", "FAIL"),
        "other-major": ("v24.10.0", "FAIL"),  # nodejs_major pins the NodeSource line
        "garbage": ("node: bad option", "FAIL"),
    }
    cases: list[Case] = []
    for name, (output, want) in versions.items():
        bindir = tmp_path / name
        _fake_tool(bindir, "node", f"echo '{output}'\n")
        _fake_tool(bindir, "npm", "echo 10.9.3\n")
        cases.append(
            (f"node-{name}", "nodejs", "main.yml", {"hermclaw_install_packages": False}, {"PATH": f"{bindir}:/usr/bin:/bin"}, want)
        )
    cases.append(("node-missing", "nodejs", "main.yml", {"hermclaw_install_packages": False}, {"PATH": f"{tmp_path / 'empty'}"}, "FAIL"))
    (tmp_path / "empty").mkdir()
    _run_cases(tmp_path, cases)


def test_node_version_gate_with_the_real_node(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node on this build host")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=False).stdout.strip()
    major = int(version.lstrip("v").split(".")[0])
    path = f"{Path(node).parent}:/usr/bin:/bin"
    want = "PASS" if major == 22 and tuple(int(x) for x in version.lstrip("v").split(".")[:2]) >= (22, 12) else "FAIL"
    _run_cases(tmp_path, [("node-real", "nodejs", "main.yml", {"hermclaw_install_packages": False}, {"PATH": path}, want)])


def test_git_version_gate(tmp_path: Path) -> None:
    versions = {
        "trixie": ("git version 2.47.3", "PASS"),
        "minimum": ("git version 2.39.5", "PASS"),
        "vendor-suffix": ("git version 2.39.3 (Apple Git-145)", "PASS"),
        "too-old": ("git version 2.38.1", "FAIL"),
        "ancient": ("git version 1.9.5", "FAIL"),
    }
    cases: list[Case] = []
    for name, (output, want) in versions.items():
        bindir = tmp_path / name
        _fake_tool(bindir, "git", f"echo '{output}'\n")
        cases.append((f"git-{name}", "common", "git.yml", {}, {"PATH": f"{bindir}:/usr/bin:/bin"}, want))
    git = shutil.which("git")
    if git is not None:
        cases.append(("git-real", "common", "git.yml", {}, {"PATH": f"{Path(git).parent}:/usr/bin:/bin"}, "PASS"))
    _run_cases(tmp_path, cases)


def test_python_version_gate_and_venv(tmp_path: Path) -> None:
    cases: list[Case] = []
    for name, version, want in (("trixie", "3.13.5", "PASS"), ("minimum", "3.12.0", "PASS"), ("bookworm", "3.11.2", "FAIL")):
        fake = tmp_path / f"python-{name}"
        _fake_tool(fake.parent / f"bin-{name}", "python3", f"echo {version}\n")
        cases.append(
            (
                f"python-{name}",
                "common",
                "python.yml",
                {
                    "hermclaw_install_packages": False,
                    "common_create_venv": False,
                    "common_python_bin": str(fake.parent / f"bin-{name}" / "python3"),
                },
                {},
                want,
            )
        )
    # the real interpreter creates a working application venv (idempotent: creates=)
    venv = tmp_path / "app-venv"
    real = {"hermclaw_install_packages": False, "common_python_bin": "/usr/bin/python3", "hermclaw_venv": str(venv)}
    if Path("/usr/bin/python3").is_file():
        cases.append(("python-real-venv", "common", "python.yml", real, {}, "PASS"))
        cases.append(("python-real-venv-again", "common", "python.yml", real, {}, "PASS"))
    _run_cases(tmp_path, cases)
    if Path("/usr/bin/python3").is_file():
        res = subprocess.run([str(venv / "bin" / "python"), "-c", "import sys; print(sys.prefix != sys.base_prefix)"],
                             capture_output=True, text=True, check=False)  # fmt: skip
        assert res.stdout.strip() == "True"
