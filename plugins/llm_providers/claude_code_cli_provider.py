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

TOOL CALLING (text-convention based, not native): headless `claude -p` runs
Claude Code's own agent loop internally, not a raw single completion call —
it has no equivalent of the Messages API's `tools` parameter that lets the
CALLER define custom tools and get raw `tool_use` blocks back structurally.
Two paths were considered:
  1. The CLI's own `--mcp-config` mechanism, letting Claude Code invoke
     tools *without* going through this plugin's mandatory approval gate
     (actions/framework.py, run_tool_loop) — rejected, it would silently
     defeat that gate's non-negotiable design.
  2. A TEXT CONVENTION: when `tools` is passed, the prompt instructs the
     model to emit a fenced ```action code block containing a single JSON
     object ({"name": ..., "arguments": {...}}) if it wants to propose a
     tool call, instead of just describing it in prose. `send()` parses any
     such block out of the CLI's response text and turns it into a real
     ToolCall with stop_reason="tool_use" — which then flows through the
     EXACT SAME execute_tool_call()/approval-gate path as every other
     provider's native tool_use blocks. This is less reliable than a real
     structured API (a model could ignore the convention, or the parse
     could fail), so every failure mode below degrades to plain text
     instead of silently dropping the proposed action — see
     _extract_action_block().

CONFIRMED, REAL CAVEATS (found via actual `claude` CLI runs, not
speculation): (a) the parameter schema, not just each tool's name and
description, has to be spelled out verbatim in the prompt
(_build_tools_instructions) — an early version without it had the model
invent plausible-but-wrong argument names (e.g. "x"/"y" instead of a
schema's required "x_mm"/"y_mm"), which would have failed at execution
time; fixed by including the full JSON schema per tool. (b) `claude -p` is
a FULL Claude Code agent, not a bare completion endpoint — it reads
whatever real CWD the KiCad process happens to have (CLAUDE.md, git
status, project files) and can occasionally let that ambient context leak
into the reply instead of staying strictly inside the simulated
conversation this module builds. Not something this module can fully
suppress (there is no "ignore your surroundings" flag for headless mode);
noted here so a stray reply mentioning unrelated local files is recognised
as this, not a new bug.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from typing import Any

try:
    from .base import (
        ChatMessage,
        ChatResponse,
        LLMProvider,
        ProviderError,
        ToolCall,
        ToolSpec,
    )
except ImportError:  # pragma: no cover - fallback for test import via conftest
    from llm_providers.base import (  # type: ignore
        ChatMessage,
        ChatResponse,
        LLMProvider,
        ProviderError,
        ToolCall,
        ToolSpec,
    )

# i18n: every string literal below is ALREADY Portuguese — wrapping in _()
# must not change any wording, only make it translatable. See chat_gui.py's
# `_()` docstring for why this is a fresh-lookup trampoline rather than
# `from ..i18n import _`.
try:  # pragma: no cover - import shim
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - import shim
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


_TIMEOUT_S = 180.0

# Matches a fenced ```action ... ``` block anywhere in the response text.
# DOTALL so the JSON body (which may itself contain newlines) is captured
# whole; re.IGNORECASE so "```Action"/"```ACTION" still match — models are
# not perfectly consistent about casing even when told the exact fence tag.
_ACTION_BLOCK_RE = re.compile(r"```action\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


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


def _extract_action_block(text: str) -> tuple[str, ToolCall | None]:
    """Look for a ```action fenced JSON block in `text`.

    Returns (remaining_text_with_block_removed, ToolCall_or_None). Any
    failure to find/parse a valid block — no fence at all, malformed JSON,
    missing "name" key — returns (text, None) unchanged rather than raising:
    a text-convention parse miss must degrade to "the model just replied in
    plain text", never crash the turn.
    """
    match = _ACTION_BLOCK_RE.search(text)
    if not match:
        return text, None

    raw_json = match.group(1).strip()
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return text, None

    if not isinstance(payload, dict):
        return text, None
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        return text, None
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    remaining = (text[: match.start()] + text[match.end() :]).strip()
    tool_call = ToolCall(id=f"cli_{uuid.uuid4().hex[:12]}", name=name, arguments=arguments)
    return remaining, tool_call


# Real, verified model ids the `claude` CLI's own --model flag accepts
# ("Provide an alias for the latest model (e.g. 'fable', 'opus', or
# 'sonnet') or a model's full name" — confirmed via `claude --help`). No
# live "list models" CLI subcommand exists to query this dynamically, so
# this is a maintained static list rather than a guess — kept in sync with
# the model table the claude-api skill tracks.
KNOWN_CLAUDE_MODELS = [
    "claude-fable-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
]


# Real values accepted by the CLI's own --permission-mode flag (confirmed
# via `claude --help`), scoped down to the three that make sense for a
# non-interactive (-p) call — "auto"/"bypassPermissions"/"dontAsk" also
# exist but were never tested here and are broader than what was asked for.
#
# "manual" is the ORIGINAL/default behavior (no flag was passed before this
# feature existed): headless mode has no terminal to answer an interactive
# approval prompt, so anything requiring one (Write/Edit) is denied outright
# — confirmed via a real call, not merely documented. It is the SAFE
# default: nothing is ever auto-approved.
#
# "plan" makes the model describe what it would do without executing — no
# file is ever modified. CONFIRMED REAL QUIRK (via an actual call, not
# documented behavior): it does NOT just print a description — it writes a
# genuine plan file to the user's own `~/.claude/plans/` folder and then
# reports it "can't formally exit plan mode in this context" (the tool for
# that, ExitPlanMode, is an interactive-session concept with no headless
# equivalent). This is a real, slightly confusing side effect (a small
# unrequested file appears on disk) and an odd-sounding message the chat
# user has no button to act on — kept anyway because it is genuinely
# side-effect-free for the PROJECT files themselves (nothing about the
# user's actual work is ever touched in this mode) and was explicitly
# requested; the GUI should hint at this quirk rather than hide it.
#
# "acceptEdits" auto-approves file edits without asking (headless mode has
# no other way to approve them) — confirmed via a real call: with it, Write
# actually creates a file on disk; without it, the same request comes back
# with a permission_denials entry and nothing is written. This is the mode
# that matches "trabalhar como cowork" / "todas as funcionalidades que tu
# tens" — deliberately opt-in, not the default, because it removes the
# per-action confirmation this plugin's OWN tools always keep (see
# actions/framework.py's mandatory approval gate) — Claude Code's built-in
# Write/Edit tools bypass that gate entirely once auto-approved, which is
# why this is a user-facing GUI choice (chat_gui.py's "Modo:" selector)
# rather than something this provider ever defaults to on its own.
PERMISSION_MODE_MANUAL = "manual"
PERMISSION_MODE_PLAN = "plan"
PERMISSION_MODE_AUTO = "acceptEdits"
PERMISSION_MODES = [PERMISSION_MODE_MANUAL, PERMISSION_MODE_PLAN, PERMISSION_MODE_AUTO]

# Built-in tools enabled regardless of permission mode — file read/write/
# navigate ("abrir, modificar ficheiros e navegar por pastas"), i.e. Read,
# Write, Edit, Glob, Grep. Bash is deliberately NEVER included: confirmed
# via a real call that scoping --tools this way makes Bash genuinely
# UNAVAILABLE to the model (it reports having no shell-execution tool at
# all, not merely a denied one) — shell/arbitrary-command execution is a
# materially larger risk than file I/O and was not part of what was asked
# for ("ficheiros e pastas"), so it stays excluded until asked for
# separately and deliberately.
_ENABLED_BUILTIN_TOOLS = "Read,Write,Edit,Glob,Grep"


class ClaudeCodeCLIProvider(LLMProvider):
    """Talk to the local `claude` CLI in headless mode and translate its
    JSON output to the plugin's provider-agnostic dataclasses."""

    id = "claude_cli"
    display_name = "Claude Code (subscrição local)"

    def __init__(self, api_key: str | None, model: str | None = None) -> None:
        super().__init__(api_key, model)
        # Safe by default — see PERMISSION_MODE_MANUAL's docstring above.
        # Set directly by the GUI's "Modo:" selector, same pattern as
        # self.model (chat_gui.py owns the widget, this is just a plain
        # attribute the next send() call reads).
        self.permission_mode: str = PERMISSION_MODE_MANUAL

    def default_model(self) -> str:
        # Empty on purpose: `claude -p` uses whatever model the user's own
        # Claude Code install is configured for. Passing an explicit
        # ANTHROPIC-style model id here would be guessing at a CLI flag
        # this provider doesn't use.
        return ""

    def is_configured(self) -> bool:
        return find_claude_cli() is not None

    def list_models(self) -> list[str]:
        return list(KNOWN_CLAUDE_MODELS)

    # ------------------------------------------------------------------ #
    # Request mapping (plugin conversation -> a single CLI prompt)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_tools_instructions(tools: list[ToolSpec]) -> str:
        lines = [
            _(
                "Tens acesso às seguintes ações (ferramentas). Cada uma exige "
                "aprovação explícita do utilizador antes de ser executada — "
                "propor uma ação é seguro, só é executada se o utilizador "
                "aceitar."
            ),
            _(
                "Para propor UMA ação, responde com um bloco de código com a "
                "etiqueta exata 'action' contendo um único objeto JSON com as "
                "chaves \"name\" e \"arguments\", por exemplo:"
            ),
            '```action\n{"name": "nome_da_ferramenta", "arguments": {"chave": "valor"}}\n```',
            _(
                "Usa no máximo UM bloco 'action' por resposta. Se não precisares "
                "de nenhuma ferramenta, responde normalmente em texto, sem "
                "nenhum bloco 'action'."
            ),
            _("Ferramentas disponíveis:"),
        ]
        for spec in tools:
            # The full parameters JSON schema is included, not just
            # name+description — confirmed necessary by a real end-to-end
            # test: without it, the model invented plausible-but-wrong
            # argument names (e.g. "x"/"y" instead of the schema's required
            # "x_mm"/"y_mm"), which would have failed at execution time.
            # Text-convention tool calling has no schema-enforcement of its
            # own (unlike a native API's structured tool_use), so the
            # instructions have to spell the exact keys out.
            schema_json = json.dumps(spec.parameters, ensure_ascii=False)
            lines.append(f"- {spec.name}: {spec.description}\n  parameters: {schema_json}")
        return "\n".join(lines)

    @staticmethod
    def _build_prompt(
        messages: list[ChatMessage], tools: list[ToolSpec] | None = None
    ) -> str:
        """Flatten the conversation into one prompt string.

        Headless `claude -p` is a one-shot call, not a multi-turn API
        conversation replayed as structured messages — each invocation
        starts a fresh Claude Code session. Prior turns (including tool
        proposals and their results, for a multi-round tool loop) are
        rendered as plain transcript text ahead of the final turn so the
        model still has conversational context, at the cost of it being
        text the model has to re-read (not free, but there is no other
        channel for it in this mode).
        """
        system_parts = [m.content for m in messages if m.role == "system" and m.content]

        # tool_call_id -> tool name, so a "tool" role message (a result) can
        # be rendered with a readable label instead of a bare id.
        tool_call_names: dict[str, str] = {}
        for m in messages:
            if m.role == "assistant":
                for tc in m.tool_calls:
                    tool_call_names[tc.id] = tc.name

        transcript: list[tuple[str, str]] = []
        for m in messages:
            if m.role == "user" and (m.content or m.attachments):
                text = m.content or ""
                if m.attachments:
                    # Unlike the API-key providers (which read + base64 the
                    # file and embed it in the request), the CLI provider
                    # is a full Claude Code agent with REAL filesystem
                    # tool access — telling it the file's real path and
                    # letting it Read the file itself (any type: text,
                    # image, PDF, ...) is simpler and more capable than
                    # re-implementing that same classification/encoding
                    # here, and it's exactly what Claude Code's own Read
                    # tool is for.
                    notes = "\n".join(
                        _(
                            "[Ficheiro anexado: {name} — caminho completo: {path}]"
                        ).format(name=a.name, path=a.path)
                        for a in m.attachments
                    )
                    text = f"{text}\n{notes}" if text else notes
                transcript.append(("Utilizador", text))
            elif m.role == "assistant" and (m.content or m.tool_calls):
                text = m.content or ""
                for tc in m.tool_calls:
                    text += (
                        f"\n[ação anteriormente proposta: {tc.name}"
                        f"({json.dumps(tc.arguments, ensure_ascii=False)})]"
                    )
                transcript.append(("Assistente", text))
            elif m.role == "tool":
                label = tool_call_names.get(m.tool_call_id or "", "ferramenta")
                transcript.append(("Resultado", f"[{label}] {m.content}"))

        parts: list[str] = []
        if system_parts:
            parts.append("\n\n".join(system_parts))
        if tools:
            parts.append(ClaudeCodeCLIProvider._build_tools_instructions(tools))

        if transcript:
            # The final USER turn stands out unprefixed (the model's actual
            # instruction for this call). Everything else — including a
            # trailing tool RESULT when this call is a tool-loop
            # continuation, not a fresh user question — keeps its label, so
            # the model can tell "here is what happened" from "answer this
            # now" apart.
            last_speaker, last_text = transcript[-1]
            head = transcript[:-1]
            for speaker, text in head:
                parts.append(f"{speaker}: {text}")
            if last_speaker == "Utilizador":
                parts.append(last_text)
            else:
                parts.append(f"{last_speaker}: {last_text}")

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
                _(
                    "CLI 'claude' não encontrado. Instale com: "
                    "npm install -g @anthropic-ai/claude-code"
                )
            )

        prompt = self._build_prompt(messages, tools)
        if not prompt.strip():
            raise ProviderError(_("Sem conteúdo para enviar ao Claude Code."))

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
            args = [cli_path, "-p", "--output-format", "json"]
            if self.model:
                # Accepts an alias ("opus", "sonnet", "fable", "haiku") or a
                # full model id — passed through as-is, the CLI validates it.
                args += ["--model", self.model]
            # File read/write/navigate access, scoped explicitly — see the
            # PERMISSION_MODE_*/_ENABLED_BUILTIN_TOOLS constants' docstrings
            # above for what each mode actually does and why Bash is never
            # included. self.permission_mode defaults to "manual" (nothing
            # auto-approved) unless the GUI's "Modo:" selector set it to
            # something else for this provider instance.
            args += [
                "--tools",
                _ENABLED_BUILTIN_TOOLS,
                "--permission-mode",
                self.permission_mode,
            ]
            result = subprocess.run(
                args,
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
            raise ProviderError(_("CLI 'claude' não encontrado: {err}").format(err=exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                _("O Claude Code CLI não respondeu em {timeout:.0f}s.").format(
                    timeout=_TIMEOUT_S
                )
            ) from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise ProviderError(
                _("Claude Code CLI terminou com erro (código {code})").format(
                    code=result.returncode
                )
                + (f": {detail}" if detail else ".")
            )

        payload: Any
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                _(
                    "Resposta inesperada do Claude Code CLI (saída não é JSON "
                    "válido): {err}"
                ).format(err=exc)
            ) from exc

        if not isinstance(payload, dict):
            raise ProviderError(
                _("Resposta inesperada do Claude Code CLI (JSON não é um objeto).")
            )

        if payload.get("is_error"):
            raise ProviderError(
                _("Claude Code CLI reportou erro: {result}").format(
                    result=payload.get("result") or _("desconhecido")
                )
            )

        text = payload.get("result")
        if not isinstance(text, str):
            raise ProviderError(_("Claude Code CLI não devolveu texto de resposta."))

        cost_usd = payload.get("total_cost_usd")
        cost_usd = cost_usd if isinstance(cost_usd, (int, float)) else None

        remaining_text, tool_call = _extract_action_block(text)
        if tool_call is not None:
            return ChatResponse(
                content=remaining_text,
                tool_calls=[tool_call],
                raw=payload,
                stop_reason="tool_use",
                cost_usd=cost_usd,
            )

        return ChatResponse(
            content=text,
            tool_calls=[],
            raw=payload,
            stop_reason="end",
            cost_usd=cost_usd,
        )
