"""
Gemini (Google Generative AI) provider.

Translates the plugin's provider-agnostic dataclasses (ChatMessage, ToolSpec,
ToolCall, ChatResponse) to/from the google-generativeai SDK shapes.

The SDK import is guarded so this module ALWAYS imports, even when the
`google-generativeai` package is not installed — the plugin must never crash
just because an optional dependency is missing.
"""

from __future__ import annotations

try:
    import google.generativeai as genai  # type: ignore
    _IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover - depends on environment
    genai = None  # type: ignore
    _IMPORT_ERROR = exc

try:
    from .base import (
        ChatMessage,
        ChatResponse,
        LLMProvider,
        ProviderError,
        ToolCall,
        ToolSpec,
    )
except ImportError:  # pragma: no cover - test/standalone import path
    from base import (  # type: ignore
        ChatMessage,
        ChatResponse,
        LLMProvider,
        ProviderError,
        ToolCall,
        ToolSpec,
    )


# Keys that the Gemini function-declaration schema validator rejects. JSON
# Schema produced for the other providers may contain them, so strip them
# recursively before handing the schema to Gemini.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "$schema",
        "$id",
        "$ref",
        "$defs",
        "definitions",
        "default",
        "examples",
        "title",
        "const",
        "patternProperties",
        "unevaluatedProperties",
    }
)

# Only these string "format" values are understood by Gemini; anything else
# (email, uri, uuid, date-time, ...) is dropped to avoid a validation error.
_SUPPORTED_FORMATS = frozenset({"enum", "date-time"})


def _clean_schema(schema):
    """Recursively remove keys/formats not supported by Gemini's function
    declaration schema. Returns a new object; never mutates the input."""
    if isinstance(schema, dict):
        cleaned = {}
        for key, value in schema.items():
            if key in _UNSUPPORTED_SCHEMA_KEYS:
                continue
            if key == "format":
                # Keep only formats Gemini accepts.
                if value in _SUPPORTED_FORMATS:
                    cleaned[key] = value
                continue
            cleaned[key] = _clean_schema(value)
        return cleaned
    if isinstance(schema, (list, tuple)):
        return [_clean_schema(item) for item in schema]
    return schema


class GeminiProvider(LLMProvider):
    id = "gemini"
    display_name = "Gemini (Google)"

    def __init__(self, api_key: str | None, model: str | None = None) -> None:
        super().__init__(api_key, model)
        # Gemini does not return tool-call IDs, so we synthesize them and keep
        # a mapping id -> function name to translate role="tool" results back
        # to the function name the API expects in a function_response.
        self._call_id_to_name: dict[str, str] = {}
        self._call_counter = 0

    def default_model(self) -> str:
        return "gemini-2.0-flash"

    # -- outbound translation -------------------------------------------------

    def _build_history(self, messages: list[ChatMessage]):
        """Split system messages out (returned separately) and translate the
        remaining conversation into Gemini's `contents` list."""
        system_parts: list[str] = []
        history: list[dict] = []

        for msg in messages:
            if msg.role == "system":
                if msg.content:
                    system_parts.append(msg.content)
            elif msg.role == "user":
                history.append({"role": "user", "parts": [msg.content]})
            elif msg.role == "assistant":
                parts: list = []
                if msg.content:
                    parts.append(msg.content)
                for tc in msg.tool_calls:
                    parts.append(
                        {"function_call": {"name": tc.name, "args": tc.arguments}}
                    )
                    # Remember the id->name mapping in case the caller reuses
                    # ids that we generated in a previous round.
                    if tc.id:
                        self._call_id_to_name.setdefault(tc.id, tc.name)
                if not parts:
                    parts.append("")
                history.append({"role": "model", "parts": parts})
            elif msg.role == "tool":
                fn_name = self._call_id_to_name.get(msg.tool_call_id or "", "")
                history.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "function_response": {
                                    "name": fn_name,
                                    "response": {"result": msg.content},
                                }
                            }
                        ],
                    }
                )

        system_instruction = "\n\n".join(system_parts) if system_parts else None
        return system_instruction, history

    @staticmethod
    def _build_tools(tools: list[ToolSpec] | None):
        if not tools:
            return None
        return [
            {
                "function_declarations": [
                    {
                        "name": s.name,
                        "description": s.description,
                        "parameters": _clean_schema(s.parameters),
                    }
                    for s in tools
                ]
            }
        ]

    # -- send -----------------------------------------------------------------

    def send(
        self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None
    ) -> ChatResponse:
        if genai is None:
            raise ProviderError(
                "Pacote 'google-generativeai' não instalado. "
                "Instale com: pip install google-generativeai"
            )
        if not self.is_configured():
            raise ProviderError(
                "Falta a API key do Gemini. Configure-a nas definições ou "
                "na variável de ambiente GOOGLE_API_KEY / GEMINI_API_KEY."
            )

        system_instruction, history = self._build_history(messages)
        gemini_tools = self._build_tools(tools)

        try:
            genai.configure(api_key=self.api_key)
            model = genai.GenerativeModel(
                self.model,
                system_instruction=system_instruction,
                tools=gemini_tools,
            )
            response = model.generate_content(history)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - SDK raises many types
            raise ProviderError(f"Erro na API Gemini: {exc}") from exc

        return self._parse_response(response)

    # -- inbound translation --------------------------------------------------

    def _next_call_id(self) -> str:
        cid = f"gemini-call-{self._call_counter}"
        self._call_counter += 1
        return cid

    def _parse_response(self, response) -> ChatResponse:
        # Safety block on the prompt itself.
        feedback = getattr(response, "prompt_feedback", None)
        block_reason = getattr(feedback, "block_reason", None)
        if block_reason:
            return ChatResponse(
                content="",
                stop_reason="error",
                error=f"Pedido bloqueado pelos filtros de segurança do Gemini: {block_reason}",
                raw=response,
            )

        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return ChatResponse(
                content="",
                stop_reason="error",
                error="O Gemini não devolveu nenhuma resposta (candidato vazio).",
                raw=response,
            )

        candidate = candidates[0]
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) if content is not None else None
        if not parts:
            finish = getattr(candidate, "finish_reason", None)
            return ChatResponse(
                content="",
                stop_reason="error",
                error=f"Resposta do Gemini sem conteúdo (finish_reason={finish}).",
                raw=response,
            )

        text_chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        for part in parts:
            text = getattr(part, "text", None)
            if text:
                text_chunks.append(text)
            fc = getattr(part, "function_call", None)
            if fc is not None and getattr(fc, "name", None):
                cid = self._next_call_id()
                args = dict(getattr(fc, "args", {}) or {})
                self._call_id_to_name[cid] = fc.name
                tool_calls.append(ToolCall(id=cid, name=fc.name, arguments=args))

        stop_reason = "tool_use" if tool_calls else "end"
        return ChatResponse(
            content="".join(text_chunks),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw=response,
        )
