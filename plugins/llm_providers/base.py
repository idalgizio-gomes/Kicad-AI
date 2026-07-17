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
class ChatMessage:
    role: str  # "user" | "assistant" | "system" | "tool"
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # set on role="tool" messages (the result of a call)


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

    @abstractmethod
    def send(
        self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None
    ) -> ChatResponse:
        """Send the conversation so far (+ optional tool specs) and return
        the model's response. Must never raise the underlying SDK's
        exception type directly — wrap in ProviderError with a clear
        message, or return a ChatResponse with stop_reason="error"."""
