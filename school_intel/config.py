"""Settings. This is the ONLY module in the package that reads os.environ.

AGENTS.md code conventions: `os.environ` is read **only** in config.py. Everything
else takes a `Settings` instance or a value off it.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = Field(alias="DATABASE_URL")
    raw_store_path: Path = Field(default=Path("./raw"), alias="RAW_STORE_PATH")
    user_agent: str = Field(
        default="TensorSchoolIntel/0.1 (+internal BD research; contact: bd@tensor.school)",
        alias="USER_AGENT",
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ADR-013. Committed default is false and must stay false. The extract stage
    # runs to completion with no ANTHROPIC_API_KEY set; test_extractor_versioning
    # asserts it.
    extraction_llm_enabled: bool = Field(default=False, alias="EXTRACTION_LLM_ENABLED")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    serper_api_key: str | None = Field(default=None, alias="SERPER_API_KEY")

    # M1-1: v1 scope is Karnataka, Tamil Nadu, Maharashtra. Widening this is a
    # config change, not a code change.
    target_states: list[str] = Field(default=["KA", "TN", "MH"], alias="TARGET_STATES")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so the env is read once per process."""
    return Settings()  # type: ignore[call-arg]  # pydantic-settings fills from env
