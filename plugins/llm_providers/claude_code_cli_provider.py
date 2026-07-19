"""
Claude Code CLI provider for the KiCad Chat Assistant.

Shells out to the user's own already-authenticated `claude` CLI
(`claude -p "<prompt>" --output-format json`) instead of a paid Anthropic
API key from console.anthropic.com. This lets a user with a Claude Pro/Max
subscription chat through their existing login, at the cost of consuming
that subscription's own usage quota per call (confirmed empirically: even
a trivial one-line reply costs real `total_cost_usd`, due to Claude Code's
built-in tool schemas/system prompt always being loaded in this mode).
See project memory `project_kicad_chat_assistant_claude_billing.md` for
the full investigation this module is based on.

This module MUST import even when the `claude` CLI is not installed — the
plugin should never crash at import time. `subprocess` is stdlib, so there
is no optional pip dependency to guard against here (unlike the SDK-based
providers); the only thing that can be "missing" is the external binary,
checked lazily in `is_configured()`/`send()`.

HONEST LIMITATION (documented, not hidden): headless `claude -p` runs
Claude Code's own agent loop internally, not a raw single completion call —
it has no equivalent of the Messages API's `tools` parameter that lets the
CALLER define custom tools and get raw `tool_use` blocks back to execute
under this plugin's mandatory approval gate (see `actions/framework.py`,
`run_tool_loop`). Routing this provider's tool calls through the CLI's own
`--mcp-config` mechanism instead would let Claude Code invoke tools
*without* going through that approval gate, which would silently defeat
its non-negotiable design. So this provider ALWAYS ignores `tools` and
answers in plain chat only — it cannot query the PCB board via the
plugin's own actions. Use the API-key `ClaudeProvider` (claude_provider.py)
when PCB tool access is needed; use this provider for plain chat/Q&A that
doesn't need board data.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

try:
    from .base import ChatMessage, ChatResponse, LLMProvider, ProviderError, ToolSpec
except ImportError:  # pragma: no cover - fallback for test import via conftest
    from llm_providers.base import (  # type: ignore
        ChatMessage,
        ChatResponse,
        LLMProvider,
        ProviderError,
        ToolSpec,
    )

_TIMEOUT_S = 180.0


def find_claude_cli() -> str | None:
    """Resolve the `claude` executable.

    `npm install -g @anthropic-ai/claude-code` on Windows places the binary
    in `%APPDATA%\\npm`, which is not automatically on PATH for existing (or
    even new) shells on this kind of setup — confirmed directly on the
    user's own machine (`claude` unrecognized in a fresh PowerShell right
    after install). `shutil.which` is tried first because it respects
    whatever PATH the KiCad process actually has; the `%APPDATA%\\npm`
    fallback below only matters when that lookup fails.
    """
    found = shutil.which("claude")
    if found:
        return found

    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            for name in ("claude.cmd", "claude.exe", "claude"):
                candidate = os.path.join(appdata, "npm", name)
                if os.path.isfile(candidate):
                    return candidate

    return None


class ClaudeCodeCLIProvider(LLMProvider):
    """Talk to the local `claude` CLI in headless mode and translate its
    JSON output to the plugin's provider-agnostic dataclasses."""

    id = "claude_cli"
    display_name = "Claude Code (subscrição local)"

    def default_model(self) -> str:
        # Empty on purpose: `claude -p` uses whatever model the user's own
        # Claude Code install is configured for. Passing an explicit
        # ANTHROPIC-style model id here would be guessing at a CLI flag
        # this provider doesn't use.
        return ""

    def is_configured(self) -> bool:
        return find_claude_cli() is not None

    # ------------------------------------------------------------------ #
    # Request mapping (plugin conversation -> a single CLI prompt)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_prompt(messages: list[ChatMessage]) -> str:
        """Flatten the conversation into one prompt string.

        Headless `claude -p` is a one-shot call, not a multi-turn API
        conversation replayed as structured messages — each invocation
        starts a fresh Claude Code session. Prior turns are rendered as
        plain transcript text ahead of the final user turn so the model
        still has conversational context, at the cost of it being text the
        model has to re-read (not free, but there is no other channel for
        it in this mode)."""
        system_parts = [m.content for m in messages if m.role == "system" and m.content]
        transcript = [m for m in messages if m.role in ("user", "assistant") and m.content]

        parts: list[str] = []
        if system_parts:
            parts.append("\n\n".join(system_parts))
        for m in transcript[:-1]:
            speaker = "Utilizador" if m.role == "user" else "Assistente"
            parts.append(f"{speaker}: {m.content}")
        if transcript:
            parts.append(transcript[-1].content)

        return "\n\n".join(parts)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def send(
        self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None
    ) -> ChatResponse:
        cli_path = find_claude_cli()
        if cli_path is None:
            raise ProviderError(
                "CLI 'claude' não encontrado. Instale com: "
                "npm install -g @anthropic-ai/claude-code"
            )

        prompt = self._build_prompt(messages)
        if not prompt.strip():
            raise ProviderError("Sem conteúdo para enviar ao Claude Code.")

        try:
            # The prompt is piped via stdin (`-p` with no argument), NOT
            # passed as a CLI argument. Confirmed by real reproduction: on
            # Windows, `claude` is a `claude.CMD` batch-file shim (npm
            # global install), and cmd.exe's own tokenizer treats embedded
            # newlines as line/command separators even inside a quoted
            # argument — a multi-line prompt passed as `argv` silently
            # corrupted the rest of the command line and dropped
            # `--output-format json` (the CLI fell back to its default
            # plain-text output, which then failed JSON parsing below).
            # stdin has no such parsing step and is the documented way to
            # feed longer/structured input to headless mode.
            result = subprocess.run(
                [cli_path, "-p", "--output-format", "json"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
                encoding="utf-8",
                # `claude` resolves to a `claude.CMD` batch-file shim on
                # Windows (npm global install). KiCad itself is a windowed
                # GUI process with no console attached; spawning a
                # console-subsystem child (cmd.exe running the batch file)
                # from it makes Windows auto-allocate a new VISIBLE console
                # window for the child, which flashes open and closed -
                # exactly the "abriu uma linha de comando a dizer 'claude'"
                # symptom the user reported. CREATE_NO_WINDOW suppresses
                # that allocation; stdin/stdout/stderr stay fully piped
                # either way, so capture is unaffected. No-op (0) on
                # non-Windows, where there is no console to allocate.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            raise ProviderError(f"CLI 'claude' não encontrado: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                f"O Claude Code CLI não respondeu em {_TIMEOUT_S:.0f}s."
            ) from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise ProviderError(
                f"Claude Code CLI terminou com erro (código {result.returncode})"
                + (f": {detail}" if detail else ".")
            )

        payload: Any
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "Resposta inesperada do Claude Code CLI (saída não é JSON válido): "
                f"{exc}"
            ) from exc

        if not isinstance(payload, dict):
            raise ProviderError("Resposta inesperada do Claude Code CLI (JSON não é um objeto).")

        if payload.get("is_error"):
            raise ProviderError(
                f"Claude Code CLI reportou erro: {payload.get('result') or 'desconhecido'}"
            )

        text = payload.get("result")
        if not isinstance(text, str):
            raise ProviderError("Claude Code CLI não devolveu texto de resposta.")

        return ChatResponse(content=text, tool_calls=[], raw=payload, stop_reason="end")
