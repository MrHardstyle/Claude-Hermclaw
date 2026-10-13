"""scripts/ops/hermclaw-model-pull against a fake ``ollama`` binary (role models are hard requirements, only the
embedding model falls back to embeddinggemma:300m)."""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "hermclaw-model-pull"


def _fake_ollama(tmp_path: Path, available: set[str]) -> Path:
    log = tmp_path / "calls.log"
    fake = tmp_path / "ollama"
    tags = " ".join(sorted(available))
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> {log}\n'
        f'avail=" {tags} "\n'
        'case "$1" in\n'
        '  pull|show) [[ "$avail" == *" $2 "* ]] && exit 0; echo "pull model manifest: file does not exist" >&2; exit 1;;\n'
        '  list) echo "NAME"; exit 0;;\n'
        "esac\n"
    )
    fake.chmod(0o755)
    return fake


def _run(tmp_path: Path, available: set[str]) -> subprocess.CompletedProcess[str]:
    fake = _fake_ollama(tmp_path, available)
    return subprocess.run(["bash", str(SCRIPT), "--ollama", str(fake)], capture_output=True, text=True, check=False)


ROLES = {"gemma4:26b", "gemma4:12b", "qwen3-coder:30b", "qwen3.8:27b", "qwen3:8b"}


def test_all_models_present(tmp_path: Path) -> None:
    res = _run(tmp_path, ROLES | {"embeddinggemma-2:740m"})
    assert res.returncode == 0, res.stderr
    assert "all models present (embedding: embeddinggemma-2:740m)" in res.stdout


def test_embedding_falls_back_with_instructions(tmp_path: Path) -> None:
    res = _run(tmp_path, ROLES | {"embeddinggemma:300m"})
    assert res.returncode == 0, res.stderr
    assert "model: embeddinggemma:300m" in res.stdout and "embedding: embeddinggemma:300m" in res.stdout


def test_missing_role_model_is_a_hard_error(tmp_path: Path) -> None:
    res = _run(tmp_path, (ROLES - {"qwen3.8:27b"}) | {"embeddinggemma-2:740m"})
    assert res.returncode == 1 and "qwen3.8:27b" in res.stderr


def test_dry_run_only_prints(tmp_path: Path) -> None:
    res = subprocess.run(["bash", str(SCRIPT), "--dry-run", "--ollama", "/nonexistent/ollama"], capture_output=True, text=True, check=False)
    assert res.returncode == 0 and "+ /nonexistent/ollama pull qwen3-coder:30b" in res.stdout
