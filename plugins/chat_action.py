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

# --- chat GUI --------------------------------------------------------------
try:  # pragma: no cover - import shim
    from .chat_gui import ChatDialog
except ImportError:  # pragma: no cover - import shim
    from chat_gui import ChatDialog  # type: ignore[no-redef]


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


def provider_factory(provider_id: str):
    """Build a configured LLMProvider for ``provider_id``.

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
        provider = create_provider(provider_id, cfg)
    except ProviderError as exc:
        _show_error(str(exc))
        return None
    except Exception as exc:  # never let a raw SDK/import error reach the GUI
        _show_error(f"Erro inesperado ao criar o provider '{provider_id}': {exc}")
        return None

    if not provider.is_configured():
        label = PROVIDER_LABELS.get(provider_id, provider_id)
        _show_error(
            f"{label} não tem uma API key configurada.\n\n"
            f"Defina-a em {get_config_path()} (chave 'providers.{provider_id}.api_key') "
            "ou na variável de ambiente correspondente, e volte a selecionar o provider.",
            title="Provider não configurado",
        )

    return provider


def _build_system_prompt() -> str:
    """English system prompt for the LLM: identity, approval-gate policy,
    and best-effort context about the currently open KiCad project."""
    lines = [
        "You are the KiCad Chat Assistant, an AI assistant embedded inside "
        "the KiCad PCB design tool.",
        "You can propose actions via tools; every tool call requires "
        "explicit user approval before execution.",
        "All available tools are read-only in this version — you cannot "
        "modify the board, schematic, or any files.",
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


def run_chat(parent=None) -> None:
    """Build the registry, provider factory, and system prompt, then open
    the chat dialog. Wrapped in a top-level try/except so any unexpected
    failure surfaces as a message box instead of crashing KiCad."""
    try:
        registry = ActionRegistry()
        register_kicad_tools(registry)

        system_prompt = _build_system_prompt()

        dialog = ChatDialog(
            parent,
            provider_factory,
            PROVIDER_IDS,
            PROVIDER_LABELS,
            registry,
            run_tool_loop,
            system_prompt,
        )
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
    except Exception as exc:
        _show_error(str(exc))
