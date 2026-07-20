"""
Codex CLI provider for the KiCad Chat Assistant.

Shells out to the user's own already-authenticated `codex` CLI
(github.com/openai/codex, `codex exec - --json`) instead of a paid OpenAI
API key. This lets a user with a ChatGPT Plus/Pro subscription chat through
their existing login (`codex login` / `codex login --device-auth`, run by
the user themselves in a terminal — never invoked BY this provider), at
whatever cost/quota model that subscription's own Codex CLI usage carries.

Deliberately independent of claude_code_cli_provider.py: same overall shape
(shell-out-to-a-logged-in-CLI, text-convention tool calling, permission-mode
concept) because both providers solve the same problem for two different
CLIs, but this module must import and work on its own — nothing here
imports from that module.

This module MUST import even when the `codex` CLI is not installed — the
plugin should never crash at import time. `subprocess` is stdlib, so there
is no optional pip dependency to guard against here (unlike the SDK-based
providers); the only thing that can be "missing" is the external binary,
checked lazily in `is_configured()`/`send()`.

TOOL CALLING (text-convention based, not native): headless `codex exec` runs
Codex's own agent loop internally, not a raw single completion call — like
Claude Code's `-p` mode, it has no equivalent of a raw API's `tools`
parameter that lets the CALLER define custom tools and get structured
tool_use blocks back. The same TEXT CONVENTION as the Claude Code CLI
provider is used here: when `tools` is passed, the prompt instructs the
model to emit a fenced ```action code block containing a single JSON object
({"name": ..., "arguments": {...}}) if it wants to propose a tool call.
`send()` parses any such block out of the CLI's response text and turns it
into a real ToolCall with stop_reason="tool_use" — which then flows through
the EXACT SAME execute_tool_call()/approval-gate path as every other
provider's native tool_use blocks (actions/framework.py). Every parse
failure mode degrades to plain text instead of silently dropping the
proposed action — see _extract_action_block().

JSON OUTPUT PARSING — NEEDS REAL-WORLD VERIFICATION: `codex exec --json`
emits newline-delimited JSON EVENTS (one JSON object per stdout line,
representing state changes as the agent runs), unlike Claude Code CLI's
`--output-format json` which prints a single JSON object at the end. The
exact event schema was not available to verify against a real run of the
binary (not installed on the machine this module was written on), so
`_extract_event_text()` below is deliberately defensive: it tries a small
set of plausible key names ("text"/"message"/"content"/"response"/"result",
including one level of nesting under a "msg" sub-object, a shape seen in
comparable JSONL agent-event protocols) against EVERY parsed line and keeps
the text from the LAST line where one of those keys held a non-empty
string. If nothing usable is found across the whole stream, `send()` raises
a clear ProviderError rather than silently returning empty text. Once
someone can run `codex exec --json` for real and see the actual event
schema, only `_EVENT_TEXT_KEYS` (and possibly the "msg" nesting check)
should need correcting — the rest of the parsing loop is schema-agnostic.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid

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


def find_codex_cli() -> str | None:
    """Resolve the `codex` executable.

    Mirrors claude_code_cli_provider.find_claude_cli()'s reasoning: a
    global npm install (`npm install -g @openai/codex`, one of the
    documented install paths for this CLI) on Windows places the binary in
    `%APPDATA%\\npm`, which is not automatically on PATH for existing (or
    even new) shells on this kind of setup. `shutil.which` is tried first
    because it respects whatever PATH the KiCad process actually has; the
    `%APPDATA%\\npm` fallback below only matters when that lookup fails.
    """
    found = shutil.which("codex")
    if found:
        return found

    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            for name in ("codex.cmd", "codex.exe", "codex"):
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


# Keys tried, in order, against each parsed --json event line (and, one
# level deep, against a "msg" sub-object) to find the assistant-visible
# text. See the module docstring's "JSON OUTPUT PARSING" section: this is a
# deliberately defensive guess, not a confirmed schema.
_EVENT_TEXT_KEYS = ("text", "message", "content", "response", "result")


def _extract_event_text(event: dict) -> str | None:
    """Best-effort extraction of assistant-visible text from ONE parsed
    `--json` event line. Returns None if none of the plausible key names
    held a non-empty string — the caller keeps scanning subsequent lines."""
    for key in _EVENT_TEXT_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value

    nested = event.get("msg")
    if isinstance(nested, dict):
        for key in _EVENT_TEXT_KEYS:
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value

    return None


def _extract_event_error(event: dict) -> str | None:
    """Best-effort extraction of an error message from ONE parsed `--json`
    event line — same "needs real-world verification" caveat as
    _extract_event_text(). Checked separately (not just via _EVENT_TEXT_KEYS)
    so a stray "error" key doesn't get misread as ordinary reply text."""
    value = event.get("error")
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, dict):
        message = value.get("message")
        if isinstance(message, str) and message.strip():
            return message
    return None


# No live "list models" endpoint is documented for the Codex CLI, and this
# module's author could not verify current Codex-CLI-accepted model ids
# against a real install. Rather than ship a maintained list that might be
# subtly wrong (a real risk this codebase has been burned by before — see
# CLAUDE.md's "never guess an external tool's ... shapes" rule), this
# follows the SAME deliberate choice as OpenAIProvider/GeminiProvider's own
# list_models() (see openai_provider.py): return empty rather than guess,
# and let the GUI fall back to free-text model entry.
KNOWN_CODEX_MODELS: list[str] = []


# Real values accepted by the CLI's own --sandbox flag (per Codex CLI docs).
# This is the closest Codex analogue to the Claude Code provider's own
# --permission-mode concept, so the naming/defaulting mirrors it directly.
#
# "read-only" is the SAFE default — mirroring the Claude Code CLI
# provider's "manual" safe-by-default rule: nothing is ever auto-approved
# or auto-written to disk unless the user (via the GUI's mode selector)
# explicitly opts into a more permissive sandbox.
PERMISSION_MODE_READ_ONLY = "read-only"
PERMISSION_MODE_WORKSPACE_WRITE = "workspace-write"
PERMISSION_MODE_DANGER_FULL_ACCESS = "danger-full-access"
PERMISSION_MODES = [
    PERMISSION_MODE_READ_ONLY,
    PERMISSION_MODE_WORKSPACE_WRITE,
    PERMISSION_MODE_DANGER_FULL_ACCESS,
]


class CodexCLIProvider(LLMProvider):
    """Talk to the local `codex` CLI in headless (`exec`) mode and translate
    its newline-delimited JSON event stream to the plugin's
    provider-agnostic dataclasses."""

    id = "codex_cli"
    display_name = "Codex CLI (subscrição ChatGPT)"

    def __init__(self, api_key: str | None, model: str | None = None) -> None:
        super().__init__(api_key, model)
        # Safe by default — see PERMISSION_MODE_READ_ONLY's docstring above.
        # Set directly by the GUI's "Modo:" selector, same pattern as
        # self.model (chat_gui.py owns the widget, this is just a plain
        # attribute the next send() call reads).
        self.permission_mode: str = PERMISSION_MODE_READ_ONLY

    def default_model(self) -> str:
        # Empty on purpose: `codex exec` uses whatever model the user's own
        # Codex CLI install is configured for. Passing an explicit model id
        # here would be guessing at a default this provider doesn't own.
        return ""

    def is_configured(self) -> bool:
        return find_codex_cli() is not None

    def list_models(self) -> list[str]:
        return list(KNOWN_CODEX_MODELS)

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
            # name+description — same reasoning as the Claude Code CLI
            # provider (confirmed there via a real end-to-end test):
            # text-convention tool calling has no schema-enforcement of its
            # own, so the instructions have to spell the exact keys out or
            # the model can invent plausible-but-wrong argument names.
            schema_json = json.dumps(spec.parameters, ensure_ascii=False)
            lines.append(f"- {spec.name}: {spec.description}\n  parameters: {schema_json}")
        return "\n".join(lines)

    @staticmethod
    def _build_prompt(
        messages: list[ChatMessage], tools: list[ToolSpec] | None = None
    ) -> str:
        """Flatten the conversation into one prompt string.

        Headless `codex exec` is a one-shot call, not a multi-turn API
        conversation replayed as structured messages — each invocation
        starts a fresh Codex session. Prior turns (including tool proposals
        and their results, for a multi-round tool loop) are rendered as
        plain transcript text ahead of the final turn so the model still
        has conversational context, at the cost of it being text the model
        has to re-read (not free, but there is no other channel for it in
        this mode). Mirrors claude_code_cli_provider._build_prompt() shape
        exactly — same tradeoffs apply to a headless CLI agent either way.
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
                    # Codex CLI, like Claude Code CLI, is a full coding
                    # agent with REAL filesystem tool access — telling it
                    # the file's real path and letting it read the file
                    # itself is simpler and more capable than re-reading +
                    # re-encoding it here.
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
            parts.append(CodexCLIProvider._build_tools_instructions(tools))

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
        cli_path = find_codex_cli()
        if cli_path is None:
            raise ProviderError(
                _(
                    "CLI 'codex' não encontrado. Instale com: "
                    "npm install -g @openai/codex"
                )
            )

        prompt = self._build_prompt(messages, tools)
        if not prompt.strip():
            raise ProviderError(_("Sem conteúdo para enviar ao Codex CLI."))

        try:
            # The prompt is piped via stdin (`codex exec -`), NOT passed as
            # a CLI argument — same reasoning as the Claude Code CLI
            # provider's documented Windows argv-corruption issue: on
            # Windows, a global npm install can make `codex` resolve to a
            # `.CMD` batch-file shim, and cmd.exe's own tokenizer treats
            # embedded newlines as line/command separators even inside a
            # quoted argument, which can silently corrupt a multi-line
            # prompt passed as argv. stdin has no such parsing step and is
            # the documented way to feed a prompt to `codex exec -`.
            args = [cli_path, "exec", "--json"]
            if self.model:
                # Accepts a model id, passed through as-is — the CLI
                # validates it, this provider doesn't second-guess it.
                args += ["--model", self.model]
            # --sandbox is the closest Codex analogue to Claude Code's
            # --permission-mode — see PERMISSION_MODE_READ_ONLY's docstring
            # above for what each value does. self.permission_mode defaults
            # to "read-only" (nothing ever written to disk) unless the
            # GUI's "Modo:" selector set it to something else for this
            # provider instance.
            args += ["--sandbox", self.permission_mode]
            args += ["-"]  # read the prompt from stdin
            result = subprocess.run(
                args,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
                encoding="utf-8",
                # Same reasoning as the Claude Code CLI provider: KiCat is a
                # windowed GUI process with no console attached, and
                # spawning a console-subsystem child (e.g. a `.CMD` batch
                # shim from an npm global install) from it makes Windows
                # auto-allocate a new VISIBLE console window for the child.
                # CREATE_NO_WINDOW suppresses that allocation; stdin/stdout/
                # stderr stay fully piped either way, so capture is
                # unaffected. No-op (0) on non-Windows.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            raise ProviderError(_("CLI 'codex' não encontrado: {err}").format(err=exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                _("O Codex CLI não respondeu em {timeout:.0f}s.").format(timeout=_TIMEOUT_S)
            ) from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise ProviderError(
                _("Codex CLI terminou com erro (código {code})").format(code=result.returncode)
                + (f": {detail}" if detail else ".")
            )

        # --json prints ONE JSON object per stdout line (a newline-delimited
        # event stream), not a single JSON object like Claude Code CLI's
        # --output-format json — see the module docstring's "JSON OUTPUT
        # PARSING" section for the "needs real-world verification" caveat.
        parsed_events: list[dict] = []
        last_text: str | None = None
        error_text: str | None = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            parsed_events.append(event)

            err = _extract_event_error(event)
            if err is not None:
                error_text = err

            text = _extract_event_text(event)
            if text is not None:
                last_text = text

        if not parsed_events:
            raise ProviderError(
                _(
                    "Resposta inesperada do Codex CLI (nenhuma linha de saída --json "
                    "é JSON válido)."
                )
            )

        if error_text is not None:
            raise ProviderError(_("Codex CLI reportou erro: {result}").format(result=error_text))

        if last_text is None:
            raise ProviderError(
                _(
                    "Codex CLI não devolveu texto de resposta reconhecível nos "
                    "eventos --json."
                )
            )

        remaining_text, tool_call = _extract_action_block(last_text)
        if tool_call is not None:
            return ChatResponse(
                content=remaining_text,
                tool_calls=[tool_call],
                raw=parsed_events,
                stop_reason="tool_use",
            )

        return ChatResponse(
            content=last_text,
            tool_calls=[],
            raw=parsed_events,
            stop_reason="end",
        )
