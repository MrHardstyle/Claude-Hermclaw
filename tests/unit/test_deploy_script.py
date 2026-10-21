"""The operator's deploy wrapper (scripts/deploy/hermclaw-deploy.sh): argument checks that run without root."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/deploy/hermclaw-deploy.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=30, check=False)


def test_the_script_is_valid_bash() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0
    if shutil.which("shellcheck"):
        assert subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, check=False).returncode == 0


@pytest.mark.parametrize("args", [(), ("--check",), ("0.1.0rc64", "0.1.0rc65"), ("--unknown", "0.1.0rc64")])
def test_usage_errors_exit_2(args: tuple[str, ...]) -> None:
    assert _run(*args).returncode == 2


@pytest.mark.parametrize("version", ["latest", "0.1", "0.1.0rc64; rm -rf /", "../0.1.0", "0.1.0-rc64"])
def test_only_release_versions_are_accepted(version: str) -> None:
    res = _run(version)
    assert res.returncode == 2 and "not a release version" in res.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="the root check needs a non-root user")
def test_a_valid_version_still_needs_root() -> None:
    for version in ("0.1.0rc65", "0.1.0", "1.2.3.post1"):
        res = _run(version, "--check")
        assert res.returncode == 1 and "must run as root" in res.stderr, res.stderr
