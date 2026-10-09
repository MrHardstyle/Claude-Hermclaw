from pathlib import Path

import pytest

from hermclaw.core.config import load_config
from hermclaw.core.errors import ConfigError
from hermclaw.core.redaction import REDACTED, Redactor


def test_example_config_loads_with_fixed_model_roles():
    cfg = load_config()
    roles = {p.role: p.model for p in cfg.models.profiles}
    assert roles["planner"].startswith("gemma4:26b")
    assert roles["planner_fallback"].startswith("gemma4:12b")
    assert roles["coder"].startswith("qwen3-coder:30b")
    assert roles["heavy"].startswith("qwen3.8:27b")
    assert roles["fast"].startswith("qwen3:8b")
    assert cfg.models.by_alias("embedding").embedding_dimensions == 768
    assert cfg.models.fallback_for("planner-gemma").alias == "planner-gemma-fallback"
    assert cfg.policies.coder.max_turns == 20
    assert {h.role for h in cfg.hosts.hosts} >= {"orchestrator", "webui", "execution_worker", "model_worker", "gitlab", "backup"}


def test_config_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_invalid_mac_rejected(tmp_path):
    for name in ("models", "policies", "capabilities", "logging"):
        (tmp_path / f"{name}.yaml").write_text(Path(f"config/{name}.example.yaml").read_text())
    (tmp_path / "hosts.yaml").write_text(
        "hosts:\n  - {id: a, address: 1.2.3.4, role: model_worker, wake_on_lan: {enabled: true, mac: 'zz:zz'}}\n"
    )
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_redactor_masks_known_secret_shapes():
    r = Redactor(["supersecretvalue"])
    text = (
        "password=hunter22 token: abcdef123456 Authorization: Bearer abc.def.ghi123 "
        "glpat-ABCDEFGHIJKLMNOPQRSTUV postgresql+psycopg://user:pw123@db/x supersecretvalue"
    )
    out = r.text(text)
    for leaked in ["hunter22", "abcdef123456", "abc.def.ghi123", "glpat-ABCDEFGHIJKLMNOPQRSTUV", "pw123", "supersecretvalue"]:
        assert leaked not in out
    assert REDACTED in out
    assert r.obj({"api_key": "x1234", "nested": {"password": "p"}, "ok": "fine"}) == {
        "api_key": REDACTED,
        "nested": {"password": REDACTED},
        "ok": "fine",
    }


def test_numeric_token_counters_are_not_redacted() -> None:
    from hermclaw.core.redaction import REDACTED, redact

    out = redact({"prompt_tokens": 120, "completion_tokens": 30, "max_tokens": 512, "token_count": 3, "token": 123456, "api_key": "abcd1234", "ok": True})
    assert out["prompt_tokens"] == 120 and out["completion_tokens"] == 30 and out["max_tokens"] == 512 and out["token_count"] == 3
    assert out["token"] == REDACTED and out["api_key"] == REDACTED and out["ok"] is True


def test_prefixed_secret_identifiers_are_redacted() -> None:
    from hermclaw.core.redaction import DEFAULT_REDACTOR, REDACTED

    for text in ("DB_PASSWORD = 'hunter2hunter2'", 'smtp-password: s3cr3tvalue', '"apiToken": "abcd1234efgh"', "X_API_KEY=zzzzyyyyxxxx", "client_secret=abcdefgh"):
        out = DEFAULT_REDACTOR.text(text)
        assert REDACTED in out and not any(v in out for v in ("hunter2hunter2", "s3cr3tvalue", "abcd1234efgh", "zzzzyyyyxxxx", "abcdefgh")), (text, out)
    for text in ("max_tokens = 1000", "tokens: 1234", "passwords_count = 12345", "token_budget: 5000"):
        assert DEFAULT_REDACTOR.text(text) == text, text
