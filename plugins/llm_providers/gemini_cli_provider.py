"""
Gemini CLI provider for the KiCad Chat Assistant.

Shells out to the user's own already-authenticated `gemini` CLI
(https://github.com/google-gemini/gemini-cli) — `gemini -p "<prompt>"
--output-format json` — instead of a paid GOOGLE_API_KEY/GEMINI_API_KEY. This
lets a user with a plain Google account (free tier) chat through their own
login, the same "shell out to an already-logged-in CLI" pattern as
claude_code_cli_provider.py.

DO NOT CONFUSE WITH gemini_provider.py: that is a completely different,
API-key based provider (id="gemini") that talks to the google-generativeai
SDK directly. This module (id="gemini_cli") never touches that SDK and never
reads GOOGLE_API_KEY/GEMINI_API_KEY — it only shells out to the `gemini`
binary and relies entirely on whatever credential that binary already has
cached from its own interactive OAuth login. If the CLI has no cached login,
it exits with an error asking for one of those env vars itself — this
provider does NOT set them or fall back to API-key behavior; that would
blur the line with gemini_provider.py, which exists precisely for that path.

This module MUST import even when the `gemini` CLI is not installed — the
plugin should never crash at import time. `subprocess` is stdlib, so there
is no optional pip dependency to guard against here (unlike the SDK-based
providers); the only thing that can be "missing" is the external binary,
checked lazily in `is_configured()`/`send()`.

TOOL CALLING (text-convention based, not native): this module uses Gemini
CLI's simple, single-JSON-object headless mode (`--output-format JSON`),
which returns one object with "response"/"stats"/optional "error" fields —
the "response" field is plain text, not a structured tool-call. A DIFFERENT,
streaming variant of `--output-format` (JSONL) does emit typed events
including real "tool_use"/"tool_result" entries — unlike Claude Code CLI,
which has NO native tool-calling mode at all. Since this implementation
deliberately stays on the simple single-JSON-object mode (to keep parsing
close to what this codebase already knows how to do for Claude Code CLI),
it uses the SAME fenced ```action text-convention hack as
claude_code_cli_provider.py (logic copied and adapted here, not
cross-imported — see that module's own docstring for the full rationale and
caveats of the text-convention approach in general).
FUTURE WORK: a later version of this provider could switch to the streaming
JSONL mode to get native tool_use/tool_result events instead of this text
convention — a real, documented capability difference in the CLI worth
revisiting, but out of scope here.

NEEDS REAL-WORLD VERIFICATION: the exact field names below ("response",
"stats", "error", exit codes 0/1/42/53) come from the Gemini CLI's official
documentation, not from a real run of the binary (not installed on this
machine). If the actual JSON shape differs once tested against a real
logged-in `gemini` CLI, this module's parsing (`send()` below) will need a
follow-up fix — flagged again in this module's PR description.
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

# Gemini CLI's documented headless exit codes — used to give clearer error
# messages than a generic "non-zero exit" (see send() below).
_EXIT_INPUT_ERROR = 42
_EXIT_TURN_LIMIT_EXCEEDED = 53


def find_gemini_cli() -> str | None:
    """Resolve the `gemini` executable.

    Gemini CLI is typically installed via `npm install -g
    @google/gemini-cli`, same distribution mechanism as `claude` — on
    Windows that places the binary under `%APPDATA%\\npm`, which is not
    always on PATH for existing shells (confirmed for `claude` on this same
    machine; mirrored here defensively since the install mechanism is the
    same). `shutil.which` is tried first because it respects whatever PATH
    the KiCad process actually has; the `%APPDATA%\\npm` fallback below only
    matters when that lookup fails.
    """
    found = shutil.which("gemini")
    if found:
        return found

    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            for name in ("gemini.cmd", "gemini.exe", "gemini"):
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
    plain text", never crash the turn. Adapted from
    claude_code_cli_provider.py's function of the same name/behavior (copied
    rather than cross-imported, per this codebase's provider-module
    isolation convention).
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
    tool_call = ToolCall(id=f"gemini_cli_{uuid.uuid4().hex[:12]}", name=name, arguments=arguments)
    return remaining, tool_call


# Real Gemini model ids the `gemini` CLI's own --model flag is known to
# accept. No live "list models" CLI subcommand is documented (mirrors
# gemini_provider.py's own list_models() reasoning: an empty/best-effort
# static list is the honest answer here rather than a guess at a live
# endpoint this CLI doesn't expose) — kept short and to models this project
# is confident actually exist, rather than a speculative long tail.
KNOWN_GEMINI_CLI_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]


class GeminiCLIProvider(LLMProvider):
    """Talk to the local `gemini` CLI in headless mode and translate its
    JSON output to the plugin's provider-agnostic dataclasses."""

    id = "gemini_cli"
    display_name = "Gemini CLI (conta Google)"

    def default_model(self) -> str:
        # Empty on purpose: `gemini -p` uses whatever default model the
        # user's own Gemini CLI install is configured for when --model is
        # not passed. Passing a guessed default here would risk overriding
        # that with a stale/incorrect id.
        return ""

    def is_configured(self) -> bool:
        return find_gemini_cli() is not None

    def list_models(self) -> list[str]:
        return list(KNOWN_GEMINI_CLI_MODELS)

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
            # name+description — same reasoning as
            # claude_code_cli_provider.py: text-convention tool calling has
            # no schema-enforcement of its own, so the instructions have to
            # spell the exact keys out or the model invents plausible-but-
            # wrong argument names.
            schema_json = json.dumps(spec.parameters, ensure_ascii=False)
            lines.append(f"- {spec.name}: {spec.description}\n  parameters: {schema_json}")
        return "\n".join(lines)

    @staticmethod
    def _build_prompt(
        messages: list[ChatMessage], tools: list[ToolSpec] | None = None
    ) -> str:
        """Flatten the conversation into one prompt string.

        Headless `gemini -p` is a one-shot call, not a multi-turn API
        conversation replayed as structured messages — each invocation
        starts a fresh session. Prior turns (including tool proposals and
        their results, for a multi-round tool loop) are rendered as plain
        transcript text ahead of the final turn so the model still has
        conversational context. Mirrors
        claude_code_cli_provider.py::_build_prompt() exactly (adapted here,
        not shared, per this codebase's provider-module isolation
        convention).
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
                    # Gemini CLI is a full agent with real filesystem access
                    # in its own working directory, but (unlike Claude Code
                    # CLI's confirmed Read-tool behavior) that hasn't been
                    # verified against this codebase's --tools scoping —
                    # telling it the real path here is the same
                    # least-surprise approach as claude_code_cli_provider.py.
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
            parts.append(GeminiCLIProvider._build_tools_instructions(tools))

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
        cli_path = find_gemini_cli()
        if cli_path is None:
            raise ProviderError(
                _(
                    "CLI 'gemini' não encontrado. Instale com: "
                    "npm install -g @google/gemini-cli"
                )
            )

        prompt = self._build_prompt(messages, tools)
        if not prompt.strip():
            raise ProviderError(_("Sem conteúdo para enviar ao Gemini CLI."))

        try:
            # The prompt is piped via stdin (`-p` with no argument), NOT
            # passed as a CLI argument — same reasoning as
            # claude_code_cli_provider.py: on Windows, `gemini` resolves to
            # an npm-shim batch file too, and cmd.exe's tokenizer mangles
            # embedded newlines in a quoted argv argument. stdin has no such
            # parsing step.
            args = [cli_path, "-p", "--output-format", "JSON"]
            if self.model:
                args += ["--model", self.model]
            result = subprocess.run(
                args,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
                encoding="utf-8",
                # See claude_code_cli_provider.py's identical use of this
                # flag: suppresses a flashing console window when spawning
                # an npm-shim batch file from KiCad's windowed (no console)
                # process on Windows. No-op (0) on non-Windows.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            raise ProviderError(_("CLI 'gemini' não encontrado: {err}").format(err=exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                _("O Gemini CLI não respondeu em {timeout:.0f}s.").format(timeout=_TIMEOUT_S)
            ) from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            if result.returncode == _EXIT_INPUT_ERROR:
                message = _("Gemini CLI reportou um erro de input (código 42)")
            elif result.returncode == _EXIT_TURN_LIMIT_EXCEEDED:
                message = _("Gemini CLI excedeu o limite de turnos (código 53)")
            else:
                message = _("Gemini CLI terminou com erro (código {code})").format(
                    code=result.returncode
                )
            raise ProviderError(message + (f": {detail}" if detail else "."))

        payload: Any
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                _(
                    "Resposta inesperada do Gemini CLI (saída não é JSON "
                    "válido): {err}"
                ).format(err=exc)
            ) from exc

        if not isinstance(payload, dict):
            raise ProviderError(
                _("Resposta inesperada do Gemini CLI (JSON não é um objeto).")
            )

        # "error" field: needs real-world verification (see module
        # docstring) — documented as optional on the single-JSON-object
        # response shape, exact structure (string vs. nested object) not yet
        # confirmed against a real run. Treated defensively: any truthy
        # value is surfaced as a failure.
        error_field = payload.get("error")
        if error_field:
            raise ProviderError(
                _("Gemini CLI reportou erro: {result}").format(result=error_field)
            )

        text = payload.get("response")
        if not isinstance(text, str):
            raise ProviderError(_("Gemini CLI não devolveu texto de resposta."))

        remaining_text, tool_call = _extract_action_block(text)
        if tool_call is not None:
            return ChatResponse(
                content=remaining_text,
                tool_calls=[tool_call],
                raw=payload,
                stop_reason="tool_use",
            )

        return ChatResponse(
            content=text,
            tool_calls=[],
            raw=payload,
            stop_reason="end",
        )
