"""
Entrypoint that wires the whole KiCad Chat Assistant together — the
equivalent of ``impart_action.py`` in the sibling Import-LIB-KiCad-Plugin
project.

``run_chat(parent=None)`` is the single public entrypoint: it builds the
provider factory, the tool registry, the system prompt, and opens the chat
dialog. It never lets an exception escape to KiCad — every failure mode
(missing pip package, unconfigured provider, unexpected error) is caught and
shown via ``wx.MessageBox`` instead of crashing the host application.

This module imports ``wx`` at the top level (it only ever runs inside KiCad's
wx-based environment, same as ``chat_gui.py``) but never imports ``pcbnew`` at
module scope — the best-effort project-context lookup in
``_build_system_prompt`` imports it lazily inside a try/except so the module
still imports cleanly in a plain wx environment without pcbnew.

i18n: ``run_chat()`` is the earliest point this plugin can initialise the
active language (before any dialog/widget exists), so it is also where
``setup_i18n()`` and the native ``wx.Locale`` are set up — see
``_init_wx_locale`` below. ``_build_system_prompt()`` is the one deliberate
exception to "everything user-facing goes through _()": its output is sent
TO THE LLM as instructions, never shown to the end user directly, and is
kept in English by convention (LLMs follow English system prompts more
reliably) — see its own docstring.
"""

from __future__ import annotations

import wx

# --- llm_providers -----------------------------------------------------
# Relative import works when KiCad imports this file as part of the
# ``plugins`` package; the absolute fallback covers ad-hoc/test imports
# that run this file outside of that package context.
try:  # pragma: no cover - import shim
    from .llm_providers import (
        PROVIDER_IDS,
        PROVIDER_LABELS,
        ProviderError,
        create_provider,
        get_config_path,
        load_config,
    )
except ImportError:  # pragma: no cover - import shim
    from llm_providers import (  # type: ignore[no-redef]
        PROVIDER_IDS,
        PROVIDER_LABELS,
        ProviderError,
        create_provider,
        get_config_path,
        load_config,
    )

# --- actions -------------------------------------------------------------
try:  # pragma: no cover - import shim
    from .actions.framework import ActionRegistry, run_tool_loop
except ImportError:  # pragma: no cover - import shim
    from actions.framework import ActionRegistry, run_tool_loop  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.kicad_tools import register_kicad_tools
except ImportError:  # pragma: no cover - import shim
    from actions.kicad_tools import register_kicad_tools  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.kicad_write_tools import register_kicad_write_tools
except ImportError:  # pragma: no cover - import shim
    from actions.kicad_write_tools import register_kicad_write_tools  # type: ignore[no-redef]

# --- chat GUI --------------------------------------------------------------
try:  # pragma: no cover - import shim
    from .chat_gui import ChatDialog
except ImportError:  # pragma: no cover - import shim
    from chat_gui import ChatDialog  # type: ignore[no-redef]

# --- i18n --------------------------------------------------------------
try:  # pragma: no cover - import shim
    from . import i18n as _i18n
    from .i18n import setup_i18n
except ImportError:  # pragma: no cover - import shim
    import i18n as _i18n  # type: ignore[no-redef]
    from i18n import setup_i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    """Trampoline into i18n._, looked up fresh on every call — see the
    identical helper (and its full rationale) in chat_gui.py. Needed here
    too because run_chat()/provider_factory() build user-facing error
    strings independently of the dialog."""
    return _i18n._(message)


# wx.Locale native-language ids for every SUPPORTED_LANGUAGES code. Missing
# entries (should not happen given the list above) fall back to
# wx.LANGUAGE_DEFAULT in _init_wx_locale.
_WX_LANGUAGE_IDS = {
    "en": wx.LANGUAGE_ENGLISH,
    "pt": wx.LANGUAGE_PORTUGUESE,
    "es": wx.LANGUAGE_SPANISH,
    "fr": wx.LANGUAGE_FRENCH,
    "de": wx.LANGUAGE_GERMAN,
    "it": wx.LANGUAGE_ITALIAN,
    "nl": wx.LANGUAGE_DUTCH,
    "pl": wx.LANGUAGE_POLISH,
    "gl": getattr(wx, "LANGUAGE_GALICIAN", wx.LANGUAGE_DEFAULT),
    "ca": wx.LANGUAGE_CATALAN,
    "zh": wx.LANGUAGE_CHINESE_SIMPLIFIED,
}

# Kept alive at module scope: a wx.Locale reverts its effect on the native
# widgets as soon as it is garbage-collected, and this plugin has no
# persistent wx.App/top-level frame of its own to anchor it to (KiCad owns
# the wx.App; ChatDialog is a plain modal Dialog built fresh per run_chat()
# call). A module global that simply gets replaced on the next run_chat()
# call is the simplest thing that reliably outlives the dialog it was set
# up for.
_wx_locale = None


def _init_wx_locale(lang: str) -> None:
    """Best-effort wx.Locale so NATIVE wx strings (file-picker captions,
    standard message-box button labels, etc. — never this plugin's own
    _()-wrapped text) follow the active language too.

    Must run BEFORE any dialog/widget is constructed: wx reads the locale
    at widget-construction time, not lazily, so calling this after
    ChatDialog(...) already exists would be a no-op for that dialog. Per
    the i18n skill guide, native strings picked up this way typically only
    fully refresh on a restart, unlike this plugin's own _() strings which
    re-render live — so this is a one-shot, startup-only step, not part of
    the live language-switch path in chat_gui.py.

    Never raises: a language pack missing from the OS (very common — most
    Windows installs only ship a handful) must not block chat startup.
    """
    global _wx_locale
    lang_id = _WX_LANGUAGE_IDS.get(lang, wx.LANGUAGE_DEFAULT)
    try:
        # wx.LogNull suppresses the "language pack not installed" warning
        # wx.Locale would otherwise pop up as an alert box — an unavailable
        # OS language pack is expected/common, not something to alarm the
        # user with on ordinary chat startup.
        with wx.LogNull():
            _wx_locale = wx.Locale(lang_id)
    except Exception:
        _wx_locale = None


def _show_error(message: str, title: str = "KiCad Chat Assistant") -> None:
    """Show an error dialog without ever raising — safe to call from any
    context, including one where a wx.App may not yet exist."""
    try:
        app = wx.App() if not wx.GetApp() else None
        wx.MessageBox(message, title, wx.OK | wx.ICON_ERROR)
        if app:
            app.Destroy()
    except Exception:
        # Absolute last resort — never let error reporting itself crash KiCad.
        print(f"[KiCad Chat Assistant] {title}: {message}")


def provider_factory(provider_id: str, model: str | None = None):
    """Build a configured LLMProvider for ``provider_id``.

    ``model``, when given, overrides the config file's model for this one
    instance — this is how the GUI's model field (chat_gui.py) works, and it
    applies to every provider generically (each one already reads its own
    ``self.model`` when calling its API/CLI).

    Never raises: a missing pip package or an invalid provider id (both
    reported as ``ProviderError`` by ``create_provider``) is shown via
    ``wx.MessageBox`` — including the exact ``pip install ...`` command when
    relevant — and ``None`` is returned instead. A provider that IS created
    but lacks an API key is still returned (so the user can switch to it and
    later configure a key); a warning is shown once at selection time so the
    gap doesn't surface only as a confusing failure mid-conversation.
    """
    cfg = load_config()
    try:
        provider = create_provider(provider_id, cfg, model_override=model)
    except ProviderError as exc:
        _show_error(str(exc))
        return None
    except Exception as exc:  # never let a raw SDK/import error reach the GUI
        _show_error(
            _("Erro inesperado ao criar o provider '{provider_id}': {exc}").format(
                provider_id=provider_id, exc=exc
            )
        )
        return None

    if not provider.is_configured():
        label = _(PROVIDER_LABELS.get(provider_id, provider_id))
        _show_error(
            _(
                "{label} não tem uma API key configurada.\n\n"
                "Defina-a em {config_path} (chave 'providers.{provider_id}.api_key') "
                "ou na variável de ambiente correspondente, e volte a selecionar o provider."
            ).format(
                label=label,
                config_path=get_config_path(),
                provider_id=provider_id,
            ),
            title=_("Provedor não configurado"),
        )

    return provider


def _build_system_prompt() -> str:
    """English system prompt for the LLM: identity, approval-gate policy,
    and best-effort context about the currently open KiCad project."""
    lines = [
        "You are the KiCad Chat Assistant, an AI assistant embedded inside "
        "the KiCad PCB design tool.",
        "You can propose actions via tools; every tool call requires "
        "explicit user approval before execution — nothing runs silently.",
        "Most tools are read-only (project info, component list, DRC/ERC). "
        "A few tools modify the board directly (move/rotate a footprint, "
        "change a footprint's value) — use those ONLY when the user clearly "
        "asked for that specific change, never speculatively, and always "
        "state exactly what you are about to change before calling the tool.",
        "There is no way to add, delete, or rewire components/tracks/nets "
        "yet, and no schematic-editing tool exists — say so plainly if asked "
        "for something outside this scope instead of attempting a workaround.",
        "Be concise and technical. When you don't have enough information, "
        "call the appropriate tool instead of guessing.",
    ]

    try:
        import pcbnew  # imported lazily — unavailable outside KiCad

        board = pcbnew.GetBoard()
        if board is not None:
            try:
                file_name = board.GetFileName() or "(not saved yet)"
            except Exception:
                file_name = "(unknown)"
            try:
                footprint_count = len(list(board.GetFootprints()))
            except Exception:
                footprint_count = -1
            lines.append(
                f"Currently open board: {file_name} "
                f"({footprint_count} footprints)."
            )
    except Exception:
        pass  # best-effort only — never let context gathering break startup

    return "\n".join(lines)


# Default session cost-alert limit (USD-equivalent), used when the config
# file doesn't set "cost_alert_limit_usd" explicitly. Only providers that
# report a real per-call cost (currently ClaudeCodeCLIProvider) count toward
# it — API-key providers bill separately and aren't metered by this plugin.
_DEFAULT_COST_ALERT_LIMIT_USD = 1.0


def _resolve_cost_alert_limit(cfg: dict) -> float | None:
    """Reads ``cost_alert_limit_usd`` from the config file. Missing key ->
    the default above; explicit ``null``/``0``/negative -> disabled
    (``None``); anything non-numeric -> falls back to the default rather
    than crashing chat startup over a typo in a hand-edited config file."""
    if "cost_alert_limit_usd" not in cfg:
        return _DEFAULT_COST_ALERT_LIMIT_USD
    value = cfg.get("cost_alert_limit_usd")
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return _DEFAULT_COST_ALERT_LIMIT_USD
    return value if value > 0 else None


def run_chat(parent=None) -> None:
    """Build the registry, provider factory, and system prompt, then open
    the chat dialog. Wrapped in a top-level try/except so any unexpected
    failure surfaces as a message box instead of crashing KiCad."""
    try:
        # Must run before ANY dialog/widget is constructed below: wx.Locale
        # (native strings) and setup_i18n (this plugin's own _() strings)
        # are both read at construction time, not lazily. No explicit
        # language is persisted in the config file (out of scope for this
        # version) — each invocation re-detects the OS language, same as a
        # fresh chat window always starting a fresh session cost count.
        active_lang = setup_i18n()
        _init_wx_locale(active_lang)

        registry = ActionRegistry()
        register_kicad_tools(registry)
        register_kicad_write_tools(registry)

        system_prompt = _build_system_prompt()
        cost_alert_limit_usd = _resolve_cost_alert_limit(load_config())

        dialog = ChatDialog(
            parent,
            provider_factory,
            PROVIDER_IDS,
            PROVIDER_LABELS,
            registry,
            run_tool_loop,
            system_prompt,
            cost_alert_limit_usd,
        )
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
    except Exception as exc:
        _show_error(str(exc))
