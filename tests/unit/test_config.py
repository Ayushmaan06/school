"""M0-1: settings load from env, and the LLM tier stays off by default."""

import pytest
from pydantic import ValidationError

from school_intel.config import Settings

ENV = {"DATABASE_URL": "postgresql+psycopg://u:p@localhost/db"}


def test_loads_from_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)
    assert s.database_url == ENV["DATABASE_URL"]


def test_missing_required_var_raises(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_llm_off_and_no_api_key_required(monkeypatch):
    """ADR-013. A missing ANTHROPIC_API_KEY must not raise."""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EXTRACTION_LLM_ENABLED", raising=False)
    s = Settings(_env_file=None)
    assert s.extraction_llm_enabled is False
    assert s.anthropic_api_key is None


def test_target_states_default(monkeypatch):
    """M1-1: v1 scope is three states, not national."""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TARGET_STATES", raising=False)
    assert Settings(_env_file=None).target_states == ["KA", "TN", "MH"]


def test_postgres_url_fix(monkeypatch):
    """Render / Aiven URLs starting with postgres:// or postgresql:// are normalized to postgresql+psycopg://."""
    monkeypatch.setenv(
        "DATABASE_URL", "postgres://user:pass@host:5432/db?sslmode=require"
    )
    s = Settings(_env_file=None)
    assert (
        s.database_url == "postgresql+psycopg://user:pass@host:5432/db?sslmode=require"
    )

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host:5432/db")
    s = Settings(_env_file=None)
    assert s.database_url == "postgresql+psycopg://user:pass@host:5432/db"
