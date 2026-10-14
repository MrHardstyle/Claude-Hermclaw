"""scripts/ops/hermclaw-model-pull against a fake ``ollama`` binary: role models are hard requirements; the embedding
model is ``embeddinggemma:300m`` (EmbeddingGemma 2 is MLX-only on Linux, research 20261014-026); ``--embedding TAG``
tries another one first and falls back."""

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


def _run(tmp_path: Path, available: set[str], *extra: str) -> subprocess.CompletedProcess[str]:
    fake = _fake_ollama(tmp_path, available)
    return subprocess.run(["bash", str(SCRIPT), "--ollama", str(fake), *extra], capture_output=True, text=True, check=False)


ROLES = {"gemma4:26b", "gemma4:12b", "qwen3-coder:30b", "qwen3.8:27b", "qwen3:8b"}


def test_all_models_present_with_the_default_embedding(tmp_path: Path) -> None:
    res = _run(tmp_path, ROLES | {"embeddinggemma:300m"})
    assert res.returncode == 0, res.stderr
    assert "all models present (embedding: embeddinggemma:300m)" in res.stdout and "ACTION" not in res.stdout
    assert "embeddinggemma-2" not in (tmp_path / "calls.log").read_text(), "the MLX-only model is not pulled by default"


def test_other_embedding_falls_back_to_the_default(tmp_path: Path) -> None:
    res = _run(tmp_path, ROLES | {"embeddinggemma:300m"}, "--embedding", "embeddinggemma-2:740m")
    assert res.returncode == 0, res.stderr
    assert "falling back to embeddinggemma:300m" in res.stderr and "embedding: embeddinggemma:300m" in res.stdout
    assert "ACTION" not in res.stdout, "the fallback is the shipped default – nothing to change in models.yaml"


def test_other_embedding_that_pulls_needs_a_config_change(tmp_path: Path) -> None:
    res = _run(tmp_path, ROLES | {"embeddinggemma-2:740m"}, "--embedding", "embeddinggemma-2:740m")
    assert res.returncode == 0, res.stderr
    assert "ACTION on .225" in res.stdout and "model: embeddinggemma-2:740m" in res.stdout


def test_missing_role_or_embedding_model_is_a_hard_error(tmp_path: Path) -> None:
    res = _run(tmp_path, (ROLES - {"qwen3.8:27b"}) | {"embeddinggemma:300m"})
    assert res.returncode == 1 and "qwen3.8:27b" in res.stderr
    res = _run(tmp_path, ROLES)
    assert res.returncode == 1 and "embedding:embeddinggemma:300m" in res.stderr


def test_dry_run_only_prints(tmp_path: Path) -> None:
    res = subprocess.run(["bash", str(SCRIPT), "--dry-run", "--ollama", "/nonexistent/ollama"], capture_output=True, text=True, check=False)
    assert res.returncode == 0 and "+ /nonexistent/ollama pull qwen3-coder:30b" in res.stdout
    assert "+ /nonexistent/ollama pull embeddinggemma:300m" in res.stdout
