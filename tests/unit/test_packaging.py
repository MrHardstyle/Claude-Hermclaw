"""Release packaging: every console script of pyproject.toml resolves to a callable (the deployment starts the
services through these entry points – a wrong target only fails on the real host)."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_console_scripts_resolve_to_callables() -> None:
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert {"hermclaw", "hermclaw-exec-worker", "hermclaw-model-worker"} <= set(scripts)
    for name, target in scripts.items():
        module_name, _, attr = target.partition(":")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attr, None)), f"{name} = {target}: no callable {attr} in {module_name}"
