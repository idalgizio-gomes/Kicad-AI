"""Tests for the Claude (Anthropic) provider.

These tests never require the real `anthropic` package, a network connection,
or an API key: the SDK client is mocked. `conftest.py` puts `plugins/` on the
path so the imports below resolve without going through `plugins/__init__.py`
(which imports `pcbnew`).
"""

import sys
import types
from unittest import mock

import pytest

import llm_providers.claude_provider as cp
from llm_providers.base import (
    ChatMessage,
    ChatResponse,
    ProviderError,
    ToolCall,
    ToolSpec,
)


# --------------------------------------------------------------------------- #
# Helpers to fake the anthropic SDK objects
# --------------------------------------------------------------------------- #
def _block(**kwargs):
    """A simple attribute bag standing in for an Anthropic content block."""
    return types.SimpleNamespace(**kwargs)


def _response(content, stop_reason):
    return types.SimpleNamespace(content=content, stop_reason=stop_reason)


class _FakeAnthropicModule:
    """Minimal stand-in for the `anthropic` module.

    Exposes the exception classes the provider catches plus an `Anthropic`
    client factory whose `messages.create` is a mock we can inspect.
    """

    class AuthenticationError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    class APIStatusError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    def __init__(self, response=None, raises=None, models=None, models_raises=None):
        self._response = response
        self._raises = raises
        self._models = models if models is not None else []
        self._models_raises = models_raises
        self.create = mock.MagicMock(side_effect=self._create)
        self.captured_kwargs = None

    def _create(self, **kwargs):
        self.captured_kwargs = kwargs
        if self._raises is not None:
            raise self._raises
        return self._response

    def _list_models(self):
        if self._models_raises is not None:
            raise self._models_raises
        return [types.SimpleNamespace(id=m) for m in self._models]

    def Anthropic(self, api_key=None):  # noqa: N802 - mimic SDK class name
        self.api_key = api_key
        return types.SimpleNamespace(
            messages=types.SimpleNamespace(create=self.create),
            models=types.SimpleNamespace(list=self._list_models),
        )


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Install a fake anthropic module into the provider and return it."""

    def _install(response=None, raises=None, models=None, models_raises=None):
        fake = _FakeAnthropicModule(
            response=response, raises=raises, models=models, models_raises=models_raises
        )
        monkeypatch.setattr(cp, "anthropic", fake)
        return fake

    return _install


# --------------------------------------------------------------------------- #
# Basic contract
# --------------------------------------------------------------------------- #
def test_default_model():
    provider = cp.ClaudeProvider(api_key="k")
    assert provider.default_model() == "claude-opus-4-8"
    assert provider.model == "claude-opus-4-8"


def test_identity_attributes():
    assert cp.ClaudeProvider.id == "claude"
    assert cp.ClaudeProvider.display_name == "Claude (Anthropic)"


def test_is_configured():
    assert cp.ClaudeProvider(api_key="k").is_configured() is True
    assert cp.ClaudeProvider(api_key=None).is_configured() is False
    assert cp.ClaudeProvider(api_key="").is_configured() is False


def test_custom_model_overrides_default():
    provider = cp.ClaudeProvider(api_key="k", model="claude-sonnet-5")
    assert provider.model == "claude-sonnet-5"


# --------------------------------------------------------------------------- #
# list_models()
# --------------------------------------------------------------------------- #
def test_list_models_returns_live_ids_when_available(fake_anthropic):
    fake_anthropic(models=["claude-opus-4-8", "claude-sonnet-5"])
    provider = cp.ClaudeProvider(api_key="k")
    assert provider.list_models() == ["claude-opus-4-8", "claude-sonnet-5"]


def test_list_models_falls_back_to_known_list_on_api_error(fake_anthropic):
    fake_anthropic(models_raises=RuntimeError("network down"))
    provider = cp.ClaudeProvider(api_key="k")
    from llm_providers.claude_code_cli_provider import KNOWN_CLAUDE_MODELS

    assert provider.list_models() == KNOWN_CLAUDE_MODELS


def test_list_models_falls_back_when_no_api_key():
    provider = cp.ClaudeProvider(api_key=None)
    from llm_providers.claude_code_cli_provider import KNOWN_CLAUDE_MODELS

    assert provider.list_models() == KNOWN_CLAUDE_MODELS


def test_list_models_falls_back_when_package_missing(monkeypatch):
    monkeypatch.setattr(cp, "anthropic", None)
    provider = cp.ClaudeProvider(api_key="k")
    from llm_providers.claude_code_cli_provider import KNOWN_CLAUDE_MODELS

    assert provider.list_models() == KNOWN_CLAUDE_MODELS


def test_list_models_falls_back_when_live_list_is_empty(fake_anthropic):
    fake_anthropic(models=[])
    provider = cp.ClaudeProvider(api_key="k")
    from llm_providers.claude_code_cli_provider import KNOWN_CLAUDE_MODELS

    assert provider.list_models() == KNOWN_CLAUDE_MODELS


# --------------------------------------------------------------------------- #
# Missing package / missing key guards
# --------------------------------------------------------------------------- #
def test_send_raises_when_package_missing(monkeypatch):
    monkeypatch.setattr(cp, "anthropic", None)
    provider = cp.ClaudeProvider(api_key="k")
    with pytest.raises(ProviderError) as excinfo:
        provider.send([ChatMessage(role="user", content="oi")])
    assert "pip install anthropic" in str(excinfo.value)


def test_send_raises_when_not_configured(fake_anthropic):
    fake_anthropic(response=_response([_block(type="text", text="hi")], "end_turn"))
    provider = cp.ClaudeProvider(api_key=None)
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="user", content="oi")])


def test_module_imports_without_package():
    # The module object exists and is importable regardless of SDK presence.
    assert "llm_providers.claude_provider" in sys.modules


# --------------------------------------------------------------------------- #
# Request mapping (plugin -> Anthropic)
# --------------------------------------------------------------------------- #
def test_system_messages_extracted_and_concatenated(fake_anthropic):
    fake = fake_anthropic(
        response=_response([_block(type="text", text="ok")], "end_turn")
    )
    provider = cp.ClaudeProvider(api_key="k")
    provider.send(
        [
            ChatMessage(role="system", content="regra 1"),
            ChatMessage(role="system", content="regra 2"),
            ChatMessage(role="user", content="olá"),
        ]
    )
    kwargs = fake.captured_kwargs
    assert kwargs["system"] == "regra 1\n\nregra 2"
    # System messages must not leak into the messages list.
    assert kwargs["messages"] == [{"role": "user", "content": "olá"}]
    assert kwargs["model"] == "claude-opus-4-8"
    assert kwargs["max_tokens"] == 16000


def test_assistant_with_tool_calls_maps_to_content_blocks(fake_anthropic):
    fake = fake_anthropic(
        response=_response([_block(type="text", text="ok")], "end_turn")
    )
    provider = cp.ClaudeProvider(api_key="k")
    provider.send(
        [
            ChatMessage(role="user", content="lista componentes"),
            ChatMessage(
                role="assistant",
                content="vou usar a ferramenta",
                tool_calls=[
                    ToolCall(id="tu_1", name="list_components", arguments={"filter": "R"})
                ],
            ),
            ChatMessage(
                role="tool",
                content="R1, R2, R3",
                tool_call_id="tu_1",
            ),
        ]
    )
    msgs = fake.captured_kwargs["messages"]
    # user, assistant(text+tool_use), user(tool_result)
    assert msgs[0] == {"role": "user", "content": "lista componentes"}
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"][0] == {"type": "text", "text": "vou usar a ferramenta"}
    assert msgs[1]["content"][1] == {
        "type": "tool_use",
        "id": "tu_1",
        "name": "list_components",
        "input": {"filter": "R"},
    }
    assert msgs[2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "R1, R2, R3"}
        ],
    }


def test_assistant_tool_call_without_text(fake_anthropic):
    fake = fake_anthropic(
        response=_response([_block(type="text", text="ok")], "end_turn")
    )
    provider = cp.ClaudeProvider(api_key="k")
    provider.send(
        [
            ChatMessage(role="user", content="x"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="tu_9", name="get_project_info", arguments={})],
            ),
        ]
    )
    assistant = fake.captured_kwargs["messages"][1]
    # No text block when content is empty; only the tool_use block.
    assert assistant["content"] == [
        {"type": "tool_use", "id": "tu_9", "name": "get_project_info", "input": {}}
    ]


def test_consecutive_tool_messages_merge_into_one_user_message(fake_anthropic):
    fake = fake_anthropic(
        response=_response([_block(type="text", text="ok")], "end_turn")
    )
    provider = cp.ClaudeProvider(api_key="k")
    provider.send(
        [
            ChatMessage(role="user", content="x"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="a", name="t", arguments={}),
                    ToolCall(id="b", name="t", arguments={}),
                ],
            ),
            ChatMessage(role="tool", content="r1", tool_call_id="a"),
            ChatMessage(role="tool", content="r2", tool_call_id="b"),
        ]
    )
    msgs = fake.captured_kwargs["messages"]
    # Both tool results must live in the SAME user message.
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"] == [
        {"type": "tool_result", "tool_use_id": "a", "content": "r1"},
        {"type": "tool_result", "tool_use_id": "b", "content": "r2"},
    ]


def test_tools_mapped_to_input_schema(fake_anthropic):
    fake = fake_anthropic(
        response=_response([_block(type="text", text="ok")], "end_turn")
    )
    provider = cp.ClaudeProvider(api_key="k")
    spec = ToolSpec(
        name="run_drc",
        description="Run DRC on the board",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    provider.send([ChatMessage(role="user", content="drc")], tools=[spec])
    tools = fake.captured_kwargs["tools"]
    assert tools == [
        {
            "name": "run_drc",
            "description": "Run DRC on the board",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        }
    ]


def test_no_tools_key_when_none(fake_anthropic):
    fake = fake_anthropic(
        response=_response([_block(type="text", text="ok")], "end_turn")
    )
    provider = cp.ClaudeProvider(api_key="k")
    provider.send([ChatMessage(role="user", content="oi")])
    assert "tools" not in fake.captured_kwargs
    assert "system" not in fake.captured_kwargs


# --------------------------------------------------------------------------- #
# Response mapping (Anthropic -> plugin)
# --------------------------------------------------------------------------- #
def test_parse_text_response(fake_anthropic):
    fake_anthropic(
        response=_response(
            [_block(type="text", text="Olá "), _block(type="text", text="mundo")],
            "end_turn",
        )
    )
    provider = cp.ClaudeProvider(api_key="k")
    resp = provider.send([ChatMessage(role="user", content="oi")])
    assert isinstance(resp, ChatResponse)
    assert resp.content == "Olá mundo"
    assert resp.stop_reason == "end"
    assert resp.tool_calls == []
    assert resp.raw is not None


def test_parse_tool_use_response(fake_anthropic):
    fake_anthropic(
        response=_response(
            [
                _block(type="text", text="a usar ferramenta"),
                _block(
                    type="tool_use",
                    id="tu_x",
                    name="list_components",
                    input={"filter": "C"},
                ),
            ],
            "tool_use",
        )
    )
    provider = cp.ClaudeProvider(api_key="k")
    resp = provider.send([ChatMessage(role="user", content="componentes?")])
    assert resp.stop_reason == "tool_use"
    assert resp.content == "a usar ferramenta"
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "tu_x"
    assert tc.name == "list_components"
    assert tc.arguments == {"filter": "C"}


@pytest.mark.parametrize("raw", ["end_turn", "max_tokens", "stop_sequence"])
def test_stop_reasons_map_to_end(fake_anthropic, raw):
    fake_anthropic(response=_response([_block(type="text", text="x")], raw))
    provider = cp.ClaudeProvider(api_key="k")
    resp = provider.send([ChatMessage(role="user", content="oi")])
    assert resp.stop_reason == "end"


def test_refusal_returns_error_response(fake_anthropic):
    fake_anthropic(response=_response([], "refusal"))
    provider = cp.ClaudeProvider(api_key="k")
    resp = provider.send([ChatMessage(role="user", content="algo")])
    assert resp.stop_reason == "error"
    assert resp.error is not None
    assert "segurança" in resp.error


# --------------------------------------------------------------------------- #
# SDK exceptions wrapped in ProviderError
# --------------------------------------------------------------------------- #
def test_authentication_error_wrapped(fake_anthropic):
    fake = _FakeAnthropicModule()
    provider = cp.ClaudeProvider(api_key="k")
    fake_anthropic(raises=fake.AuthenticationError("bad key"))
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="user", content="oi")])


def test_rate_limit_error_wrapped(fake_anthropic):
    fake = fake_anthropic(raises=None)
    err = fake.RateLimitError("429")
    fake_anthropic(raises=err)
    provider = cp.ClaudeProvider(api_key="k")
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="user", content="oi")])


def test_connection_error_wrapped(fake_anthropic):
    fake = _FakeAnthropicModule()
    provider = cp.ClaudeProvider(api_key="k")
    fake_anthropic(raises=fake.APIConnectionError("no net"))
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="user", content="oi")])


def test_api_status_error_wrapped(fake_anthropic):
    fake = _FakeAnthropicModule()
    provider = cp.ClaudeProvider(api_key="k")
    fake_anthropic(raises=fake.APIStatusError("500"))
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="user", content="oi")])
