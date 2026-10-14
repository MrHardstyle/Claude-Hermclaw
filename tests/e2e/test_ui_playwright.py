"""P31 Web UI: run the UI's own test suites from pytest (vitest unit tests + Playwright E2E against the real backend).

The Playwright global setup (ui/tests/e2e/global-setup.ts) creates a throw-away PostgreSQL database, starts uvicorn
with ``hermclaw.api.app:create_app``, seeds data through the API and the ORM and serves the built UI. This wrapper
only makes the suites part of the Python test campaigns; it skips with a reason when Node/Chromium are unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
NODE_DIRS = ("/opt/node22/bin",)
BROWSERS = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([*NODE_DIRS, env.get("PATH", "")])
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS)
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    env["CI"] = "1"
    return env


def _npx(env: dict[str, str]) -> str:
    npx = shutil.which("npx", path=env["PATH"])
    if npx is None:
        pytest.skip("npx (Node 22) not available")
    if not (UI / "node_modules").is_dir():
        pytest.skip("ui/node_modules missing – run 'npm ci' in ui/")
    return npx


def _run(cmd: list[str], env: dict[str, str], timeout: int) -> None:
    proc = subprocess.run(cmd, cwd=UI, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-80:])
        pytest.fail(f"{' '.join(cmd[1:])} failed (exit {proc.returncode}):\n{tail}")


def test_ui_unit_tests() -> None:
    env = _env()
    _run([_npx(env), "vitest", "run"], env, timeout=300)


def test_ui_typecheck_and_lint() -> None:
    env = _env()
    npx = _npx(env)
    _run([npx, "tsc", "-b", "--pretty", "false"], env, timeout=300)
    _run([npx, "eslint", ".", "--max-warnings=0"], env, timeout=300)


def test_ui_playwright_e2e_against_real_backend() -> None:
    env = _env()
    npx = _npx(env)
    if not BROWSERS.is_dir() or not any(BROWSERS.glob("chromium*")):
        pytest.skip(f"no pre-installed Chromium under {BROWSERS}")
    if shutil.which("pg_isready") is not None:
        ready = subprocess.run(["pg_isready", "-h", "127.0.0.1", "-p", "5432"], capture_output=True, check=False)
        if ready.returncode != 0:
            pytest.skip("local PostgreSQL 16 on 127.0.0.1:5432 not reachable")
    _run([npx, "playwright", "test", "--reporter=line"], env, timeout=1500)
