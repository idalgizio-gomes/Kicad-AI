"""
Registry / factory for LLM providers, plus config-file handling for API keys.

CRITICAL: this module must import cleanly in ANY Python environment — inside
KiCad, inside a plain venv with no SDKs installed, and inside the test suite
(which runs outside KiCad and never has pcbnew/wx available). It must NOT:
  - import pcbnew or wx (directly or transitively)
  - import any provider module or its SDK at module scope

Provider modules (claude_provider.py, openai_provider.py, gemini_provider.py)
are imported lazily, inside create_provider(), so a missing pip package
(anthropic / openai / google-generativeai) only breaks the one provider that
needs it — never the whole plugin.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .base import (
    ChatMessage,
    ChatResponse,
    LLMProvider,
    ProviderError,
    ToolCall,
    ToolSpec,
)

# i18n: every string literal below is ALREADY Portuguese (this module
# predates the i18n infrastructure) — wrapping in _() must not change any
# wording, only make it translatable (existing tests assert on the exact pt
# substrings). See chat_gui.py's `_()` docstring for why this is a
# fresh-lookup trampoline rather than `from ..i18n import _`.
try:  # pragma: no cover - import shim
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - import shim
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)

__all__ = [
    "ChatMessage",
    "ToolCall",
    "ToolSpec",
    "ChatResponse",
    "ProviderError",
    "LLMProvider",
    "PROVIDER_IDS",
    "PROVIDER_LABELS",
    "get_config_path",
    "load_config",
    "save_config",
    "resolve_api_key",
    "create_provider",
]

PROVIDER_IDS = ["claude", "claude_cli", "chatgpt", "gemini"]

# Built once at import time, so — like every other module-level constant
# wrapped in _() before setup_i18n() has necessarily run yet — each value
# here ends up equal to its own (Portuguese) msgid text. Callers that want a
# LIVE translation (e.g. chat_gui.py re-rendering the provider picker after
# a language switch) re-feed these raw strings through their own _() at
# render time; see chat_gui.py's ChatDialog._provider_labels docstring.
PROVIDER_LABELS = {
    "claude": _("Claude (Anthropic - API paga)"),
    "claude_cli": _("Claude Code (subscrição local)"),
    "chatgpt": _("ChatGPT (OpenAI)"),
    "gemini": _("Gemini (Google)"),
}

# Maps provider id -> (pip package name, env var(s) to check for an API key).
# Order matters for env vars: first one found wins.
# claude_cli has no entry: it shells out to the `claude` CLI binary, not a
# pip package, so it never goes through the ImportError -> "pip install"
# path in create_provider() below.
_PIP_PACKAGES = {
    "claude": "anthropic",
    "chatgpt": "openai",
    "gemini": "google-generativeai",
}

_ENV_VARS = {
    "claude": ["ANTHROPIC_API_KEY"],
    "chatgpt": ["OPENAI_API_KEY"],
    "gemini": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
}


def get_config_path() -> Path:
    """Location of the plugin's config file: ~/.kicad_chat_assistant/config.json"""
    return Path.home() / ".kicad_chat_assistant" / "config.json"


def load_config() -> dict:
    """Load the config JSON. Tolerates a missing or corrupted file by
    returning {} — never raises."""
    path = get_config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    """Write the config JSON, creating the parent directory if needed."""
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def resolve_api_key(provider_id: str, cfg: dict) -> str | None:
    """Resolve the API key for a provider: config file first, then the
    provider's environment variable(s) (first one set wins)."""
    providers_cfg = cfg.get("providers", {}) if isinstance(cfg, dict) else {}
    provider_cfg = providers_cfg.get(provider_id, {}) if isinstance(providers_cfg, dict) else {}
    key = provider_cfg.get("api_key") if isinstance(provider_cfg, dict) else None
    if key:
        return key

    for env_var in _ENV_VARS.get(provider_id, []):
        value = os.environ.get(env_var)
        if value:
            return value

    return None


def create_provider(
    provider_id: str, cfg: dict | None = None, model_override: str | None = None
) -> LLMProvider:
    """Factory: build a configured LLMProvider instance for provider_id.

    Imports the concrete provider module (and its SDK) lazily, so that a
    missing pip package only affects providers that need it. Raises
    ProviderError (never lets ImportError / raw exceptions escape) for:
      - an unknown provider_id
      - a missing pip dependency (with the exact `pip install ...` command)

    ``model_override``, when given, wins over the config file's
    ``providers.<id>.model`` — this is how the GUI's live model switcher
    (chat_gui.py) rebuilds a provider with a different model without
    touching the persisted config.
    """
    if cfg is None:
        cfg = load_config()

    if provider_id not in PROVIDER_IDS:
        raise ProviderError(_("Provedor desconhecido: '{id}'").format(id=provider_id))

    api_key = resolve_api_key(provider_id, cfg)
    providers_cfg = cfg.get("providers", {}) if isinstance(cfg, dict) else {}
    provider_cfg = providers_cfg.get(provider_id, {}) if isinstance(providers_cfg, dict) else {}
    model = model_override or (
        provider_cfg.get("model") if isinstance(provider_cfg, dict) else None
    )

    try:
        if provider_id == "claude":
            from .claude_provider import ClaudeProvider

            return ClaudeProvider(api_key=api_key, model=model)
        if provider_id == "claude_cli":
            from .claude_code_cli_provider import ClaudeCodeCLIProvider

            return ClaudeCodeCLIProvider(api_key=api_key, model=model)
        if provider_id == "chatgpt":
            from .openai_provider import OpenAIProvider

            return OpenAIProvider(api_key=api_key, model=model)
        if provider_id == "gemini":
            from .gemini_provider import GeminiProvider

            return GeminiProvider(api_key=api_key, model=model)
    except ImportError as exc:
        pip_name = _PIP_PACKAGES.get(provider_id, provider_id)
        raise ProviderError(
            _("Pacote '{pip_name}' não instalado. Instale com: pip install {pip_name}").format(
                pip_name=pip_name
            )
        ) from exc

    # Unreachable given the provider_id check above, but keeps mypy/pyflakes happy.
    raise ProviderError(f"Provedor desconhecido: '{provider_id}'")
