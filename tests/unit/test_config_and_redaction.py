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

    out = redact(
        {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "max_tokens": 512,
            "token_count": 3,
            "token": 123456,
            "api_key": "abcd1234",
            "ok": True,
        }
    )
    assert out["prompt_tokens"] == 120 and out["completion_tokens"] == 30 and out["max_tokens"] == 512 and out["token_count"] == 3
    assert out["token"] == REDACTED and out["api_key"] == REDACTED and out["ok"] is True


def test_prefixed_secret_identifiers_are_redacted() -> None:
    from hermclaw.core.redaction import DEFAULT_REDACTOR, REDACTED

    for text in (
        "DB_PASSWORD = 'hunter2hunter2'",
        "smtp-password: s3cr3tvalue",
        '"apiToken": "abcd1234efgh"',
        "X_API_KEY=zzzzyyyyxxxx",
        "client_secret=abcdefgh",
    ):
        out = DEFAULT_REDACTOR.text(text)
        assert REDACTED in out and not any(
            v in out for v in ("hunter2hunter2", "s3cr3tvalue", "abcd1234efgh", "zzzzyyyyxxxx", "abcdefgh")
        ), (text, out)
    for text in ("max_tokens = 1000", "tokens: 1234", "passwords_count = 12345", "token_budget: 5000"):
        assert DEFAULT_REDACTOR.text(text) == text, text


def test_default_config_dir_is_the_checkout_or_etc_hermclaw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Installed release (no config/ next to the package): /etc/hermclaw, not <site-packages>/config."""
    from hermclaw.core import settings as settings_mod

    monkeypatch.delenv("HERMCLAW_CONFIG_DIR", raising=False)
    assert settings_mod.default_config_dir() == settings_mod.REPO_ROOT / "config"
    assert settings_mod.Settings().config_dir == settings_mod.REPO_ROOT / "config"
    monkeypatch.setattr(settings_mod, "REPO_ROOT", tmp_path / "site-packages")
    assert settings_mod.default_config_dir() == Path("/etc/hermclaw")
    assert settings_mod.Settings().config_dir == Path("/etc/hermclaw")
    monkeypatch.setenv("HERMCLAW_CONFIG_DIR", str(tmp_path))
    assert settings_mod.Settings().config_dir == tmp_path


def test_api_trusts_forwarded_headers_only_from_configured_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never forwarded_allow_ips="*": only the configured proxy (nginx on .223) may set the client address."""
    import uvicorn

    from hermclaw.api import app as app_mod
    from hermclaw.core.settings import reset_settings_cache

    seen: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.setattr(app_mod, "create_app", object)
    monkeypatch.setattr(app_mod, "configure_logging", lambda *a, **k: None)
    monkeypatch.delenv("HERMCLAW_API_FORWARDED_ALLOW_IPS", raising=False)
    reset_settings_cache()
    app_mod.main()
    assert seen["proxy_headers"] is True and seen["forwarded_allow_ips"] == "127.0.0.1"
    monkeypatch.setenv("HERMCLAW_API_FORWARDED_ALLOW_IPS", "192.168.178.223")
    reset_settings_cache()
    app_mod.main()
    assert seen["forwarded_allow_ips"] == "192.168.178.223"
    reset_settings_cache()


def test_code_reading_secrets_and_placeholders_stay_readable() -> None:
    """BUG-225: masking ``api_key = config["x"]`` and ``"your-secret-here"`` showed the coder broken code and markers,
    which it copied into new files; every such write was refused (grand job G1)."""
    from hermclaw.core.redaction import DEFAULT_REDACTOR, REDACTED

    for text in (
        'api_token_secret = "your-secret-here"',
        'api_key = "your-unraid-api-key-here"',
        'password = "changeme"',
        'token = "<your token>"',
        "TOKEN=${GITLAB_TOKEN}",
        'secret = os.environ["X"]',
        'api_key = config["unraid"]["api_key"]',
        "token = self.token",
        "password = getpass.getpass()",
        'headers = {"x-api-key": api_key}',
        'self.api_token_secret = cfg.get("api_token_secret", "")',
        "api_key = 'xxxx-xxxx'",
    ):
        assert DEFAULT_REDACTOR.text(text) == text, text
    for text in ("PASSWORD=hunter2realpw", 'api_key = "abc.def.ghi"', "password: hunter2", 'secret = "s3cr3t.value"'):
        assert REDACTED in DEFAULT_REDACTOR.text(text), text


def test_role_values_a_test_compares_stay_visible() -> None:
    """Cycle 2 M3 on rc60: the review saw ``token="***REDACTED***"`` for both the valid and the wrong token of the
    tests and could not tell them apart; the verifier already accepted both as placeholders (BUG-293)."""
    assert Redactor().text('def _get(self, path, token="test-token-123"):') == 'def _get(self, path, token="test-token-123"):'
    assert Redactor().text('self._get("/notes", token="wrong-token")') == 'self._get("/notes", token="wrong-token")'
    assert Redactor().text("token: 'valid_token'") == "token: 'valid_token'"
    # a real value, or one with a part that is no role word, stays masked
    assert Redactor().text('token="hX9q2LmP4tR8"') == f'token="{REDACTED}"'
    assert Redactor().text('password = "S3cr3t!Pa55"') == f'password = "{REDACTED}"'
    assert Redactor().text('token="wrong-hX9q2LmP4tR8"') == f'token="{REDACTED}"'
