"""Unit tests: model profiles / architecture checks (8.2–8.7), token estimation + context validation (8.9),
LiteLLM proxy config generation (8.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from hermclaw.core.config import load_config
from hermclaw.core.errors import ConfigError, ValidationFailed
from hermclaw.models.litellm_config import build_litellm_config, main, render_litellm_config
from hermclaw.models.profiles import (
    ProfileRegistry,
    assert_architecture,
    check_architecture,
    normalize_model_tag,
    ollama_base_url,
    resolve_api_key,
    same_model,
    validate_http_url,
)
from hermclaw.models.protocols import ChatMessage
from hermclaw.models.tokens import (
    CHARS_PER_TOKEN,
    MESSAGE_OVERHEAD_TOKENS,
    REPLY_PRIMING_TOKENS,
    context_budget,
    estimate_json_tokens,
    estimate_messages_tokens,
    estimate_tokens,
    max_prompt_chars,
    validate_context,
)
from tests.integration.test_models_support import models_config


def replace_profile(cfg: Any, target: str, **update: Any) -> Any:
    return cfg.model_copy(update={"profiles": [p.model_copy(update=update) if p.alias == target else p for p in cfg.profiles]})


# ----------------------------------------------------------------------------------------------- tokens (8.9)
def test_estimate_tokens_is_conservative() -> None:
    assert CHARS_PER_TOKEN == 3.2
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("x" * 32) == 10
    assert estimate_tokens("x" * 33) == 11
    assert estimate_json_tokens({"a": 1}) == estimate_tokens('{"a":1}')


def test_message_estimate_includes_overhead() -> None:
    msgs = [ChatMessage("system", "x" * 32), ChatMessage("user", "")]
    expected = REPLY_PRIMING_TOKENS + 2 * MESSAGE_OVERHEAD_TOKENS + 10 + estimate_tokens("system") + estimate_tokens("user")
    assert estimate_messages_tokens(msgs) == expected
    assert estimate_messages_tokens(msgs, extra_texts=["y" * 64]) == expected + 20


def test_validate_context_boundaries() -> None:
    p = models_config().by_alias("fast-router")  # 16384 ctx, 2048 out
    budget = validate_context(p, [ChatMessage("user", "hello")])
    assert budget.fits and budget.max_output_tokens == 2048 and budget.remaining_tokens > 0
    free_chars = max_prompt_chars(p, max_tokens=2048)
    # the largest single message that still fits
    overhead = REPLY_PRIMING_TOKENS + MESSAGE_OVERHEAD_TOKENS + estimate_tokens("user")
    content_tokens = 16384 - 2048 - overhead
    ok = "x" * int(content_tokens * CHARS_PER_TOKEN)
    assert validate_context(p, [ChatMessage("user", ok)], max_tokens=2048).remaining_tokens >= 0
    with pytest.raises(ValidationFailed) as exc:
        validate_context(p, [ChatMessage("user", ok + "x" * 4)], max_tokens=2048)
    assert exc.value.code == "CONTEXT_OVERFLOW"
    assert exc.value.details["fits"] is False and exc.value.details["context_tokens"] == 16384
    assert free_chars > 0


def test_validate_context_counts_max_tokens_and_reserve() -> None:
    p = models_config().by_alias("fast-router")
    msgs = [ChatMessage("user", "hi")]
    with pytest.raises(ValidationFailed):
        validate_context(p, msgs, max_tokens=16384)
    with pytest.raises(ValidationFailed):
        validate_context(p, msgs, max_tokens=8000, reserve_tokens=8400)
    with pytest.raises(ValidationFailed) as exc:
        context_budget(p, msgs, max_tokens=-1)
    assert exc.value.code == "INVALID_TOKEN_BUDGET"


# ----------------------------------------------------------------------------------------------- profiles (8.2–8.7)
def test_example_config_satisfies_architecture() -> None:
    cfg = load_config()
    issues = check_architecture(cfg.models)
    assert [i for i in issues if i.severity == "error"] == []
    reg = ProfileRegistry(cfg.models)
    assert reg.by_role("fast").model == "qwen3:8b"  # 8.3
    assert reg.by_role("planner").model.startswith("gemma4:26b")  # 8.4
    assert reg.fallback_for("planner-gemma") is not None and reg.fallback_for("planner-gemma").model.startswith("gemma4:12b")  # type: ignore[union-attr]
    assert reg.by_role("coder").model.startswith("qwen3-coder:30b")  # 8.5
    assert reg.by_role("heavy").model.startswith("qwen3.8:27b")  # 8.6
    emb = reg.embedding_profile()  # 8.7
    assert emb is not None and emb.kind == "embedding" and emb.embedding_dimensions == 768


@pytest.mark.parametrize(
    ("alias", "update", "fragment"),
    [
        ("coder-main", {"model": "llama3:70b"}, "violates the fixed architecture"),
        ("planner-gemma", {"model": "qwen3:32b"}, "violates the fixed architecture"),
        ("heavy-review", {"kind": "embedding"}, "kind must be"),
        ("embedding", {"embedding_dimensions": None}, "embedding_dimensions"),
        ("planner-gemma-fallback", {"fallback_for": "coder-main"}, "planner_fallback must be the fallback"),
        ("planner-gemma-fallback", {"fallback_for": "ghost"}, "unknown alias"),
        ("fast-router", {"max_output_tokens": 20000}, "smaller than context_tokens"),
        ("fast-router", {"alias": "bad alias!"}, "alias must match"),
    ],
)
def test_architecture_errors(alias: str, update: dict[str, Any], fragment: str) -> None:
    cfg = replace_profile(models_config(), alias, **update)
    errors = [i for i in check_architecture(cfg) if i.severity == "error"]
    assert any(fragment in i.message for i in errors), errors
    with pytest.raises(ConfigError) as exc:
        assert_architecture(cfg)
    assert exc.value.code == "MODEL_ARCHITECTURE_VIOLATION"


def test_architecture_missing_role_and_warnings() -> None:
    cfg = replace_profile(models_config(), "heavy-review", enabled=False)
    assert any(i.role == "heavy" and "no enabled profile" in i.message for i in check_architecture(cfg))
    warn = replace_profile(models_config(), "coder-main", context_tokens=16384, max_output_tokens=2048)
    issues = check_architecture(warn)
    assert {i.severity for i in issues if i.alias == "coder-main"} == {"warning"}
    assert len([i for i in issues if i.alias == "coder-main"]) == 2
    assert assert_architecture(warn)  # warnings only -> returned, not raised


def test_registry_lookups() -> None:
    cfg = replace_profile(models_config(), "heavy-review", enabled=False)
    reg = ProfileRegistry(cfg)
    assert "fast-router" in reg and "nope" not in reg
    with pytest.raises(ConfigError) as exc:
        reg.get("heavy-review")
    assert exc.value.code == "MODEL_ALIAS_DISABLED"
    with pytest.raises(ConfigError):
        reg.get("nope")
    with pytest.raises(ConfigError):
        reg.by_role("heavy")
    assert [p.alias for p in reg.group_members("small-model-224")] == ["fast-router", "embedding"]
    assert [p.alias for p in reg.by_model("qwen3:8b")] == ["fast-router"]
    assert reg.hosts() == ["model-224"]
    assert len(reg.enabled(kind="chat")) == 4


def test_model_tag_normalisation() -> None:
    assert normalize_model_tag("qwen3") == "qwen3:latest"
    assert normalize_model_tag("registry.local:5000/qwen3") == "registry.local:5000/qwen3:latest"
    assert same_model("qwen3:8b", " qwen3:8b ")
    assert not same_model("qwen3:8b", "qwen3:14b")


# ----------------------------------------------------------------------------------------------- secrets / urls
def test_resolve_api_key_refs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HC_TEST_KEY", "sk-env-secret-value-123456")
    assert resolve_api_key("env:HC_TEST_KEY", env="test") == "sk-env-secret-value-123456"
    f = tmp_path / "key"
    f.write_text("file-secret-value\n", encoding="utf-8")
    f.chmod(0o600)
    assert resolve_api_key(f"file:{f}", env="test") == "file-secret-value"
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert resolve_api_key("cred:key", env="test") == "file-secret-value"
    assert resolve_api_key("env:HC_MISSING_KEY", required=False, env="test") is None
    with pytest.raises(ConfigError) as exc:
        resolve_api_key("env:HC_MISSING_KEY", env="test")
    assert exc.value.code == "SECRET_MISSING"
    with pytest.raises(ConfigError) as exc:
        resolve_api_key("env:HC_TEST_KEY", env="production")
    assert exc.value.code == "SECRET_POLICY"
    with pytest.raises(ConfigError) as exc:
        resolve_api_key("plain-secret", env="test")
    assert exc.value.code == "SECRET_REF_INVALID"
    with pytest.raises(ConfigError):
        resolve_api_key("", env="test")
    assert resolve_api_key(None, required=False) is None


@pytest.mark.parametrize("url", ["ftp://x", "http://", "file:///etc/passwd", "http://u:p@h:1", "http://h:1/?x=1", "gopher://h"])
def test_validate_http_url_rejects(url: str) -> None:
    with pytest.raises(ConfigError):
        validate_http_url(url)


def test_ollama_base_url_from_hosts() -> None:
    cfg = load_config()
    assert ollama_base_url(cfg.hosts, "model-224") == "http://192.168.178.224:11434"
    with pytest.raises(ConfigError):
        ollama_base_url(cfg.hosts, "ghost")


# ----------------------------------------------------------------------------------------------- litellm config (8.1)
def test_build_litellm_config() -> None:
    cfg = models_config()
    conf = build_litellm_config(cfg, ollama_urls_by_host={"model-224": "http://10.0.0.5:11434/"}, keep_alive="15m")
    by_name = {m["model_name"]: m for m in conf["model_list"]}
    planner = by_name["planner-gemma"]["litellm_params"]
    assert planner == {
        "model": "ollama_chat/gemma4:26b",
        "api_base": "http://10.0.0.5:11434",
        "num_ctx": 32768,
        "timeout": 60,
        "keep_alive": "15m",
        "think": False,
    }
    emb = by_name["embedding"]
    assert emb["litellm_params"]["model"] == "ollama/embeddinggemma-2:740m" and "think" not in emb["litellm_params"]
    assert emb["model_info"] == {"mode": "embedding", "max_input_tokens": 2048, "output_vector_size": 8}
    assert conf["general_settings"] == {"master_key": "os.environ/LITELLM_MASTER_KEY", "background_health_checks": False, "health_check_details": False}
    assert conf["router_settings"]["disable_cooldowns"] is True
    rendered = render_litellm_config(conf)
    assert rendered.startswith("# Generated") and yaml.safe_load(rendered) == conf


def test_build_litellm_config_errors() -> None:
    cfg = models_config()
    with pytest.raises(ConfigError):
        build_litellm_config(cfg, ollama_urls_by_host={})
    with pytest.raises(ConfigError):
        build_litellm_config(cfg, ollama_urls_by_host={"model-224": "http://h:1"}, master_key_env="BAD-NAME;rm")
    disabled = replace_profile(cfg, "heavy-review", enabled=False)
    conf = build_litellm_config(disabled, ollama_urls_by_host={"model-224": "http://h:1"}, check_architecture=False)
    assert "heavy-review" not in [m["model_name"] for m in conf["model_list"]]


def test_cli_writes_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "sub" / "litellm.yaml"
    assert main([str(out), "--ollama-url", "model-224=http://127.0.0.1:11434", "--keep-alive", "5m"]) == 0
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["model_list"][0]["litellm_params"]["api_base"] == "http://127.0.0.1:11434"
    assert data["model_list"][0]["litellm_params"]["keep_alive"] == "5m"
    assert oct(out.stat().st_mode & 0o777) == "0o644"
    assert main(["-"]) == 0
    assert "192.168.178.224:11434" in capsys.readouterr().out
    assert main([str(out), "--ollama-url", "nonsense"]) == 2
