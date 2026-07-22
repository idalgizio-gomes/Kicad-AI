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

# Cross-plugin tools (EMC-EMI Analyzer, LibForge, KiKit): each register_*
# function is safe to call even when the sibling plugin/tool isn't installed
# on this machine — the handler itself reports that honestly at CALL time
# (see _sibling_plugin.py's SiblingPluginNotFoundError), never at import or
# registration time, so chat startup never fails just because e.g. LibForge
# isn't installed alongside this plugin.
try:  # pragma: no cover - import shim
    from .actions.emc_emi_tools import register_emc_emi_tools
except ImportError:  # pragma: no cover - import shim
    from actions.emc_emi_tools import register_emc_emi_tools  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.libforge_tools import register_libforge_tools
except ImportError:  # pragma: no cover - import shim
    from actions.libforge_tools import register_libforge_tools  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.kikit_tools import register_kikit_tools
except ImportError:  # pragma: no cover - import shim
    from actions.kikit_tools import register_kikit_tools  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.kicad_schematic_tools import register_schematic_tools
except ImportError:  # pragma: no cover - import shim
    from actions.kicad_schematic_tools import register_schematic_tools  # type: ignore[no-redef]

# Third-party (PCM-installed) sibling plugins — same "safe to call even when
# not installed" contract as the forks above, resolved via
# _sibling_plugin.py's find_pcm_plugin_dir() instead of a nested plugins/
# subfolder junction (see each module's own docstring).
try:  # pragma: no cover - import shim
    from .actions.kicad_parasitics_tools import register_kicad_parasitics_tools
except ImportError:  # pragma: no cover - import shim
    from actions.kicad_parasitics_tools import register_kicad_parasitics_tools  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.board2pdf_tools import register_board2pdf_tools
except ImportError:  # pragma: no cover - import shim
    from actions.board2pdf_tools import register_board2pdf_tools  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.jlcpcb_tools import register_jlcpcb_tools
except ImportError:  # pragma: no cover - import shim
    from actions.jlcpcb_tools import register_jlcpcb_tools  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.pinout_generator_tools import register_pinout_generator_tools
except ImportError:  # pragma: no cover - import shim
    from actions.pinout_generator_tools import register_pinout_generator_tools  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.round_tracks_tools import register_round_tracks_tools
except ImportError:  # pragma: no cover - import shim
    from actions.round_tracks_tools import register_round_tracks_tools  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.coil_creator_tools import register_coil_creator_tools
except ImportError:  # pragma: no cover - import shim
    from actions.coil_creator_tools import register_coil_creator_tools  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.coil_generators_tools import register_coil_generators_tools
except ImportError:  # pragma: no cover - import shim
    from actions.coil_generators_tools import register_coil_generators_tools  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.via_pad_tools import register_via_pad_tools
except ImportError:  # pragma: no cover - import shim
    from actions.via_pad_tools import register_via_pad_tools  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.testpoints_tools import register_testpoints_tools
except ImportError:  # pragma: no cover - import shim
    from actions.testpoints_tools import register_testpoints_tools  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.cammer_tools import register_cammer_tools
except ImportError:  # pragma: no cover - import shim
    from actions.cammer_tools import register_cammer_tools  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.projectinstances_tools import register_projectinstances_tools
except ImportError:  # pragma: no cover - import shim
    from actions.projectinstances_tools import register_projectinstances_tools  # type: ignore[no-redef]

try:  # pragma: no cover - import shim
    from .actions.freerouting_tools import register_freerouting_tools
except ImportError:  # pragma: no cover - import shim
    from actions.freerouting_tools import register_freerouting_tools  # type: ignore[no-redef]

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


# provider_id -> (CLI binary name, install command) for every CLI-based
# provider (never an API-key one) — used by provider_factory() below to
# show an ACCURATE "not configured" message instead of the API-key one,
# which was a real, reported bug when the CLI providers were added: a user
# selecting Gemini CLI was told to set "providers.gemini_cli.api_key" in
# config.json, which does nothing for a provider that never reads one.
# Every install command here is a real, verified npm package name (never
# guessed) — see each provider module's own docstring for the same command.
_CLI_INSTALL_HINTS: dict[str, tuple[str, str]] = {
    "claude_cli": ("claude", "npm install -g @anthropic-ai/claude-code"),
    "codex_cli": ("codex", "npm install -g @openai/codex"),
    "gemini_cli": ("gemini", "npm install -g @google/gemini-cli"),
}


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
        cli_hint = _CLI_INSTALL_HINTS.get(provider_id)
        if cli_hint is not None:
            # CLI-based providers (claude_cli/codex_cli/gemini_cli) are
            # "not configured" when the CLI BINARY isn't found on PATH —
            # nothing to do with an API key at all. Showing the API-key
            # message here (as an earlier version did, unconditionally) was
            # a real, reported bug: a user pointed at "config.json ->
            # providers.gemini_cli.api_key" has no way to act on that
            # advice, since this provider never reads an API key.
            _show_error(
                _(
                    "{label} não encontrou o comando '{cli}' no PATH.\n\n"
                    "Instale com: {install}\n"
                    "Depois, autentique-se com a sua conta correndo '{cli}' "
                    "num terminal (ou '{cli} login', se existir esse comando) "
                    "e volte a selecionar o provider."
                ).format(
                    label=label,
                    cli=cli_hint[0],
                    install=cli_hint[1],
                ),
                title=_("Provedor não configurado"),
            )
            return provider
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
        "Most tools are read-only (project info, component list, DRC/ERC, "
        "EMC/EMI coupling analysis, library duplicate scanning, track/via "
        "listing, schematic wire/label/symbol listing). Many tools modify "
        "the board, the schematic file, or write new files: move/rotate a "
        "footprint, change a footprint's value, add/delete a footprint, "
        "add/delete a track or via, reassign a pad's net, create a brand "
        "new empty board file, add/delete a schematic wire, label, or "
        "placed symbol, generate a symbol/footprint from a pinout/package "
        "spec, panelize a board — use those ONLY when the user clearly "
        "asked for that specific change, never speculatively, and always "
        "state exactly what you are about to do before calling the tool.",
        "Some tools depend on sibling plugins or external tools (EMC-EMI "
        "Analyzer, LibForge, KiKit, FastHenry2/FastCap2, and third-party "
        "PCM-installed plugins: KiCad-Parasitics for resistance/impedance "
        "path analysis, Board2Pdf for PDF export, JLC-Plugin-for-KiCad for "
        "JLCPCB fabrication files, Pinout Generator for pinout exports, "
        "Round Tracks for rounding track corners, Coil Creator and Coil "
        "Generators for spiral/loop PCB coils, Thermal Relief Via and Set "
        "Hole Diameter for via/pad geometry, kicad_testpoints for "
        "bed-of-nails test-point CSV export, SparkFun CAMmer for zipped "
        "Gerber/drill generation, ProjectInstances for hierarchical-sheet "
        "PCB replication STATUS only — reapplying the replication itself "
        "is not yet supported, only reporting what's already configured, "
        "Freerouting for real Java-based autorouting) that may not be "
        "installed on this user's machine — if "
        "a tool call reports one is missing, relay that "
        "honestly instead of pretending it worked. These third-party tools "
        "are NOT this project's own code — treat their results as coming "
        "from an external plugin, and never claim broader KiCad-wide "
        "capability than the specific tools actually registered.",
        "IMPORTANT limitations to always state honestly when relevant: PCB "
        "tools mutate the LIVE board in the open PCB editor (undoable via "
        "Ctrl+Z, never auto-saved — the user must save with Ctrl+S). "
        "Schematic tools are DIFFERENT: there is no live schematic-editor "
        "API, so they edit the saved .kicad_sch FILE directly on disk — the "
        "user must close and reopen the schematic (or accept KiCad's "
        "reload prompt) to see the change, and every schematic write tool's "
        "result message says so. add_schematic_symbol only supports flat, "
        "non-hierarchical schematics. Net/pad reassignment does not "
        "auto-rebuild the ratsnest — tell the user to re-run DRC/ERC "
        "afterward. create_board_from_scratch writes a brand new separate "
        "file and never touches the currently open board.",
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
        register_emc_emi_tools(registry)
        register_libforge_tools(registry)
        register_kikit_tools(registry)
        register_schematic_tools(registry)
        register_kicad_parasitics_tools(registry)
        register_board2pdf_tools(registry)
        register_jlcpcb_tools(registry)
        register_pinout_generator_tools(registry)
        register_round_tracks_tools(registry)
        register_coil_creator_tools(registry)
        register_coil_generators_tools(registry)
        register_via_pad_tools(registry)
        register_testpoints_tools(registry)
        register_cammer_tools(registry)
        register_projectinstances_tools(registry)
        register_freerouting_tools(registry)

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
