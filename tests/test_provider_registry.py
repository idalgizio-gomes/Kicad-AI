"""Tests for llm_providers/__init__.py (registry, config, factory).

These must run without pcbnew, wx, or any provider SDK installed — the
registry module only imports provider modules lazily inside create_provider().
"""

import json
import sys

import pytest

import llm_providers
from llm_providers import (
    PROVIDER_IDS,
    PROVIDER_LABELS,
    ProviderError,
    create_provider,
    load_config,
    resolve_api_key,
    save_config,
)


def test_import_llm_providers_without_any_sdk_installed():
    """The package itself must import clean regardless of SDK availability."""
    assert "llm_providers" in sys.modules
    assert set(PROVIDER_IDS) == {"claude", "claude_cli", "chatgpt", "gemini"}
    assert PROVIDER_LABELS["claude"] == "Claude (Anthropic - API paga)"
    assert PROVIDER_LABELS["claude_cli"] == "Claude Code (subscrição local)"
    assert PROVIDER_LABELS["chatgpt"] == "ChatGPT (OpenAI)"
    assert PROVIDER_LABELS["gemini"] == "Gemini (Google)"


def test_load_config_missing_file_returns_empty_dict(tmp_path, monkeypatch):
    missing_path = tmp_path / "does_not_exist" / "config.json"
    monkeypatch.setattr(llm_providers, "get_config_path", lambda: missing_path)
    assert load_config() == {}


def test_load_config_corrupted_file_returns_empty_dict(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(llm_providers, "get_config_path", lambda: cfg_path)
    assert load_config() == {}


def test_save_and_load_config_roundtrip(tmp_path, monkeypatch):
    cfg_path = tmp_path / "nested" / "config.json"
    monkeypatch.setattr(llm_providers, "get_config_path", lambda: cfg_path)

    cfg = {
        "default_provider": "claude",
        "providers": {"claude": {"api_key": "sk-test", "model": None}},
    }
    save_config(cfg)

    assert cfg_path.exists()
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk == cfg

    loaded = load_config()
    assert loaded == cfg


def test_resolve_api_key_prefers_config_over_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    cfg = {"providers": {"claude": {"api_key": "config-key"}}}
    assert resolve_api_key("claude", cfg) == "config-key"


def test_resolve_api_key_falls_back_to_env_var(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    assert resolve_api_key("claude", {}) == "env-key"


def test_resolve_api_key_openai_env_var(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-env-key")
    assert resolve_api_key("chatgpt", {}) == "openai-env-key"


def test_resolve_api_key_gemini_env_var_google(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    assert resolve_api_key("gemini", {}) == "google-key"


def test_resolve_api_key_gemini_env_var_gemini_fallback(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    assert resolve_api_key("gemini", {}) == "gemini-key"


def test_resolve_api_key_none_when_nothing_set(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert resolve_api_key("claude", {}) is None


def test_create_provider_unknown_id_raises_provider_error():
    with pytest.raises(ProviderError):
        create_provider("not-a-real-provider", {})


def test_create_provider_claude_returns_claude_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    provider = create_provider("claude", {})
    assert provider.id == "claude"
    assert provider.display_name == "Claude (Anthropic)"


def test_create_provider_claude_cli_returns_claude_code_cli_provider():
    provider = create_provider("claude_cli", {})
    assert provider.id == "claude_cli"
    assert provider.display_name == "Claude Code (subscrição local)"


def test_create_provider_model_override_wins_over_config():
    cfg = {"providers": {"claude_cli": {"model": "from-config"}}}
    provider = create_provider("claude_cli", cfg, model_override="from-override")
    assert provider.model == "from-override"


def test_create_provider_model_override_applies_to_any_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    provider = create_provider("chatgpt", {}, model_override="gpt-9000")
    assert provider.model == "gpt-9000"


def test_create_provider_no_override_falls_back_to_config():
    cfg = {"providers": {"claude_cli": {"model": "from-config"}}}
    provider = create_provider("claude_cli", cfg)
    assert provider.model == "from-config"


def test_create_provider_chatgpt_returns_openai_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    provider = create_provider("chatgpt", {})
    assert provider.id == "chatgpt"


def test_create_provider_gemini_returns_gemini_provider(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    provider = create_provider("gemini", {})
    assert provider.id == "gemini"


def test_create_provider_missing_sdk_raises_provider_error_with_pip_hint(monkeypatch):
    """Simulate the claude_provider submodule itself being unimportable (e.g. a
    packaging problem, or standing in for a hard ImportError deep in the SDK
    import chain) — create_provider must wrap it in ProviderError with the
    pip install hint, never let ImportError escape."""
    monkeypatch.setitem(sys.modules, "llm_providers.claude_provider", None)

    with pytest.raises(ProviderError) as excinfo:
        create_provider("claude", {})
    assert "pip install anthropic" in str(excinfo.value)
