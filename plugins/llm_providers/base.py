"""
Common interface every LLM provider (Claude/ChatGPT/Gemini) implements, so
the chat GUI and the actions framework never need to know which provider is
active. Each concrete provider translates to/from its own SDK's message and
tool-call shapes at the edges — everything else in the plugin works only
with these dataclasses.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Attachment:
    """A file the user attached to a message — any type, per the plugin's
    "anexar qualquer tipo de ficheiro" requirement. What actually reaches
    the model depends on the provider AND the file's real content (see
    attachments.py::classify_attachment): images and PDFs go in natively
    where the provider's API supports it, text-decodable files are inlined
    as text, anything else degrades to a plain "could not include this
    file" note rather than silently dropping it or sending garbage bytes.
    """

    path: str
    name: str  # display name (basename) - kept separate from `path` so the
    # GUI/history never has to leak the user's full local filesystem layout


@dataclass
class ChatMessage:
    role: str  # "user" | "assistant" | "system" | "tool"
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # set on role="tool" messages (the result of a call)
    # Provider-specific, display-only extras (e.g. {"cost_usd": 0.08} from the
    # Claude Code CLI provider). Never read by request-building code — only
    # by the GUI, so a provider that doesn't populate it changes nothing.
    meta: dict = field(default_factory=dict)
    # Files the user attached to THIS message (role="user" only, in
    # practice — nothing stops another role from carrying one, but nothing
    # populates it either). Empty for every message that doesn't have one,
    # so existing providers/tests that never set this are unaffected.
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema, e.g. {"type": "object", "properties": {...}, "required": [...]}


@dataclass
class ChatResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None
    stop_reason: str = "end"  # "end" | "tool_use" | "error"
    error: str | None = None
    # Real per-call cost in USD, when the provider can report it (currently
    # only ClaudeCodeCLIProvider, from the CLI's own total_cost_usd). None
    # for providers that don't expose this (the API-key providers bill via
    # token counts the plugin doesn't meter itself).
    cost_usd: float | None = None


class ProviderError(Exception):
    """Raised for provider-level failures (auth, network, rate limit) —
    never let a raw SDK exception reach the GUI unwrapped."""


class LLMProvider(ABC):
    id: str  # "claude" | "chatgpt" | "gemini"
    display_name: str

    def __init__(self, api_key: str | None, model: str | None = None) -> None:
        self.api_key = api_key
        self.model = model or self.default_model()

    @abstractmethod
    def default_model(self) -> str: ...

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def list_models(self) -> list[str]:
        """Best-effort list of real, currently-usable model ids/aliases for
        this provider — used by the GUI's model picker so the user chooses
        from something that actually exists instead of typing a free-text
        guess. Default: empty (unknown) — a subclass either queries its
        provider's live /models endpoint, returns a small set of verified
        aliases, or both with the live query as primary and the verified
        set as a fallback. MUST NOT raise: any failure (missing key,
        network, SDK differences) is the caller's cue to fall back to free
        text entry, not a crash."""
        return []

    @abstractmethod
    def send(
        self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None
    ) -> ChatResponse:
        """Send the conversation so far (+ optional tool specs) and return
        the model's response. Must never raise the underlying SDK's
        exception type directly — wrap in ProviderError with a clear
        message, or return a ChatResponse with stop_reason="error"."""
