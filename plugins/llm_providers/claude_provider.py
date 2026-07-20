"""
Anthropic (Claude) provider for the KiCad Chat Assistant.

This module MUST import even when the `anthropic` package is not installed —
the plugin should never crash at import time just because an optional SDK is
missing. The import is therefore wrapped in try/except and the actual failure
is deferred to `send()`, where it is surfaced as a clean `ProviderError`.
"""

from __future__ import annotations

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
# must not change any wording, only make it translatable (existing tests
# assert on exact pt substrings). See chat_gui.py's `_()` docstring for why
# this is a fresh-lookup trampoline rather than `from ..i18n import _`.
try:  # pragma: no cover - import shim
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - import shim
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


# Import the SDK lazily-tolerant: keep the module importable without it.
try:
    import anthropic  # type: ignore

    _IMPORT_ERROR: str | None = None
except ImportError as exc:  # pragma: no cover - depends on environment
    anthropic = None  # type: ignore
    _IMPORT_ERROR = str(exc)


class ClaudeProvider(LLMProvider):
    """Talk to Anthropic's Messages API and translate to/from the plugin's
    provider-agnostic dataclasses (see `llm_providers.base`)."""

    id = "claude"
    display_name = "Claude (Anthropic)"

    def default_model(self) -> str:
        return "claude-opus-4-8"

    # ------------------------------------------------------------------ #
    # Request mapping (plugin -> Anthropic)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_request(
        messages: list[ChatMessage], tools: list[ToolSpec] | None
    ) -> dict[str, Any]:
        """Translate the plugin conversation into the kwargs for
        `client.messages.create(...)`.

        - role="system" messages are concatenated into the `system` param.
        - role="user" -> {"role": "user", "content": <text>}
        - role="assistant" with tool_calls -> a content list of an optional
          text block followed by one tool_use block per ToolCall.
        - role="tool" -> a tool_result block wrapped in ONE user message;
          consecutive tool messages are merged into the same user message,
          because the API rejects tool_use without a matching tool_result and
          penalises results that are split across messages.
        """
        system_parts: list[str] = []
        api_messages: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == "system":
                if msg.content:
                    system_parts.append(msg.content)
                continue

            if msg.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content,
                }
                # Merge into the previous user message iff it is itself a
                # block-list message ending in tool_result blocks.
                if (
                    api_messages
                    and api_messages[-1]["role"] == "user"
                    and isinstance(api_messages[-1]["content"], list)
                    and all(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in api_messages[-1]["content"]
                    )
                ):
                    api_messages[-1]["content"].append(block)
                else:
                    api_messages.append({"role": "user", "content": [block]})
                continue

            if msg.role == "assistant":
                if msg.tool_calls:
                    content: list[dict[str, Any]] = []
                    if msg.content:
                        content.append({"type": "text", "text": msg.content})
                    for tc in msg.tool_calls:
                        content.append(
                            {
                                "type": "tool_use",
                                "id": tc.id,
                                "name": tc.name,
                                "input": tc.arguments,
                            }
                        )
                    api_messages.append({"role": "assistant", "content": content})
                else:
                    api_messages.append(
                        {"role": "assistant", "content": msg.content}
                    )
                continue

            # Default: treat as a user message.
            api_messages.append({"role": "user", "content": msg.content})

        request: dict[str, Any] = {"messages": api_messages}
        if system_parts:
            request["system"] = "\n\n".join(system_parts)
        if tools:
            request["tools"] = [
                {
                    "name": s.name,
                    "description": s.description,
                    "input_schema": s.parameters,
                }
                for s in tools
            ]
        return request

    # ------------------------------------------------------------------ #
    # Response mapping (Anthropic -> plugin)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_response(response: Any) -> ChatResponse:
        stop_reason_raw = getattr(response, "stop_reason", None)
        if stop_reason_raw == "refusal":
            return ChatResponse(
                content="",
                stop_reason="error",
                error=_("Pedido recusado pelos filtros de segurança"),
                raw=response,
            )

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in getattr(response, "content", None) or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        arguments=dict(getattr(block, "input", {}) or {}),
                    )
                )

        if stop_reason_raw == "tool_use":
            stop_reason = "tool_use"
        else:
            # "end_turn" | "max_tokens" | "stop_sequence" | None -> "end"
            stop_reason = "end"

        return ChatResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            raw=response,
            stop_reason=stop_reason,
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def send(
        self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None
    ) -> ChatResponse:
        if anthropic is None:
            raise ProviderError(
                _(
                    "Pacote 'anthropic' não instalado. "
                    "Instale com: pip install anthropic"
                )
            )
        if not self.is_configured():
            raise ProviderError(
                _(
                    "Chave de API da Anthropic em falta. Configure a API key "
                    "do Claude nas definições do plugin."
                )
            )

        request = self._build_request(messages, tools)

        try:
            client = anthropic.Anthropic(api_key=self.api_key)
            response = client.messages.create(
                model=self.model,
                max_tokens=16000,
                **request,
            )
        except anthropic.AuthenticationError as exc:
            raise ProviderError(
                _(
                    "Autenticação falhou: a chave de API do Claude é inválida "
                    "ou foi revogada. ({err})"
                ).format(err=exc)
            ) from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError(
                _(
                    "Limite de pedidos atingido na API do Claude. Aguarde um "
                    "momento e tente novamente. ({err})"
                ).format(err=exc)
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(
                _(
                    "Não foi possível ligar à API do Claude. Verifique a sua "
                    "ligação à internet. ({err})"
                ).format(err=exc)
            ) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                _("A API do Claude devolveu um erro: {err}").format(err=exc)
            ) from exc

        return self._parse_response(response)
