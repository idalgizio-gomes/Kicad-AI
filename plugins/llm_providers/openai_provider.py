"""
OpenAI (ChatGPT) provider implementation.

Translates the plugin's provider-agnostic ChatMessage/ToolSpec/ChatResponse
dataclasses (see llm_providers.base) to and from the OpenAI Chat Completions
API shapes. The `openai` package is imported lazily/defensively so this
module always imports cleanly even when the SDK is not installed — the
plugin as a whole must never crash just because an optional provider
dependency is missing.
"""

from __future__ import annotations

import json

try:
    import openai
except ImportError:
    openai = None

try:
    from .base import (
        ChatMessage,
        ChatResponse,
        LLMProvider,
        ProviderError,
        ToolCall,
        ToolSpec,
    )
except ImportError:  # pragma: no cover - fallback for test/import layouts
    from base import (  # type: ignore
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


class OpenAIProvider(LLMProvider):
    id = "chatgpt"
    display_name = "ChatGPT (OpenAI)"

    def default_model(self) -> str:
        return "gpt-4o"

    def list_models(self) -> list[str]:
        """Live GET /v1/models — no verified static fallback list is
        maintained here (unlike Claude's), since guessing OpenAI model ids
        risks showing stale/wrong names; an empty list is the honest result
        when the live call fails (missing key, network, SDK differences),
        and the GUI falls back to free-text entry in that case. Chat
        Completions models are (heuristically) those whose id contains
        "gpt" — the endpoint also lists embedding/whisper/tts/moderation
        models this provider can't use for chat, and the API gives no
        cleaner capability flag to filter on."""
        if openai is None or not self.api_key:
            return []
        try:
            client = openai.OpenAI(api_key=self.api_key)
            page = client.models.list()
            ids = sorted(m.id for m in page if "gpt" in m.id.lower())
            return ids
        except Exception:
            return []

    def send(
        self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None
    ) -> ChatResponse:
        if openai is None:
            raise ProviderError(
                _("Pacote 'openai' não instalado. Instale com: pip install openai")
            )
        if not self.is_configured():
            raise ProviderError(
                _("Chave de API da OpenAI em falta. Configure a API key para usar o ChatGPT.")
            )

        api_messages = self._to_api_messages(messages)
        api_tools = self._to_api_tools(tools) if tools else None

        client = openai.OpenAI(api_key=self.api_key)

        kwargs: dict = {
            "model": self.model,
            "messages": api_messages,
        }
        if api_tools:
            kwargs["tools"] = api_tools

        try:
            response = client.chat.completions.create(**kwargs)
        except openai.AuthenticationError as exc:
            raise ProviderError(
                _("Falha de autenticação na OpenAI — verifique a API key. ({err})").format(
                    err=exc
                )
            ) from exc
        except openai.RateLimitError as exc:
            raise ProviderError(
                _(
                    "Limite de pedidos da OpenAI excedido. Tente novamente mais tarde. ({err})"
                ).format(err=exc)
            ) from exc
        except openai.APIConnectionError as exc:
            raise ProviderError(
                _("Falha de ligação à OpenAI. Verifique a rede. ({err})").format(err=exc)
            ) from exc
        except openai.APIStatusError as exc:
            raise ProviderError(_("Erro da API OpenAI: {err}").format(err=exc)) from exc

        return self._from_api_response(response)

    # -- message mapping --------------------------------------------------

    def _to_api_messages(self, messages: list[ChatMessage]) -> list[dict]:
        api_messages: list[dict] = []
        for msg in messages:
            if msg.role == "system":
                api_messages.append({"role": "system", "content": msg.content})
            elif msg.role == "user":
                api_messages.append({"role": "user", "content": msg.content})
            elif msg.role == "assistant":
                entry: dict = {
                    "role": "assistant",
                    "content": msg.content if msg.content else None,
                }
                if msg.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                api_messages.append(entry)
            elif msg.role == "tool":
                api_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id,
                        "content": msg.content,
                    }
                )
        return api_messages

    def _to_api_tools(self, tools: list[ToolSpec]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    def _from_api_response(self, response) -> ChatResponse:
        choice = response.choices[0]
        message = choice.message
        finish_reason = choice.finish_reason

        content = message.content or ""
        tool_calls: list[ToolCall] = []

        if message.tool_calls:
            for tc in message.tool_calls:
                raw_args = tc.function.arguments or "{}"
                try:
                    arguments = json.loads(raw_args)
                except json.JSONDecodeError as exc:
                    raise ProviderError(
                        _(
                            "Resposta da OpenAI com argumentos de ferramenta inválidos: {err}"
                        ).format(err=exc)
                    ) from exc
                tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, arguments=arguments)
                )

        if finish_reason == "content_filter":
            return ChatResponse(
                content=content,
                tool_calls=tool_calls,
                raw=response,
                stop_reason="error",
                error=_("Pedido recusado pelos filtros de conteúdo da OpenAI"),
            )

        if finish_reason == "tool_calls":
            stop_reason = "tool_use"
        elif finish_reason in ("stop", "length"):
            stop_reason = "end"
        else:
            stop_reason = "end"

        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            raw=response,
            stop_reason=stop_reason,
        )
