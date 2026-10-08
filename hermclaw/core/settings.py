"""Process settings from environment (prefix ``HERMCLAW_``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HERMCLAW_", env_file=None, extra="ignore")

    env: Literal["production", "development", "test"] = "development"
    instance_id: str = "orchestrator-225"
    database_url: str = "postgresql+psycopg://hermclaw@127.0.0.1:5432/hermclaw"
    config_dir: Path = Field(default=REPO_ROOT / "config")
    data_dir: Path = Path("/var/lib/hermclaw")
    log_level: str = "INFO"
    log_json: bool = True
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    cors_origins: list[str] = Field(default_factory=list)
    # Secret references (see hermclaw.security.secrets); never literal secrets in env for production.
    api_token_ref: str = "cred:api-admin-token"
    litellm_key_ref: str = "cred:litellm-master-key"
    gitlab_token_ref: str = "cred:gitlab-token"
    worker_token_refs: dict[str, str] = Field(default_factory=dict)
    scheduler_poll_seconds: float = 1.0
    scheduler_concurrency: int = 4

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def repos_cache_dir(self) -> Path:
        return self.data_dir / "repos"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
