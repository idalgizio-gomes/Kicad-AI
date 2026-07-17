import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from llm_providers.base import ChatMessage, ProviderError, ToolCall, ToolSpec
import llm_providers.openai_provider as openai_provider
from llm_providers.openai_provider import OpenAIProvider


def make_response(content=None, tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def make_tool_call_obj(id_, name, arguments_dict):
    return SimpleNamespace(
        id=id_,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments_dict)),
    )


def test_default_model():
    provider = OpenAIProvider(api_key="abc")
    assert provider.default_model() == "gpt-4o"
    assert provider.model == "gpt-4o"


def test_is_configured():
    assert OpenAIProvider(api_key="abc").is_configured() is True
    assert OpenAIProvider(api_key=None).is_configured() is False


def test_send_without_package_raises_provider_error(monkeypatch):
    monkeypatch.setattr(openai_provider, "openai", None)
    provider = OpenAIProvider(api_key="abc")
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="user", content="hi")])


def test_send_without_api_key_raises_provider_error(monkeypatch):
    fake_openai = MagicMock()
    monkeypatch.setattr(openai_provider, "openai", fake_openai)
    provider = OpenAIProvider(api_key=None)
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="user", content="hi")])


def test_send_basic_text_response(monkeypatch):
    fake_openai = MagicMock()
    fake_client = MagicMock()
    fake_openai.OpenAI.return_value = fake_client
    fake_client.chat.completions.create.return_value = make_response(
        content="Hello there", finish_reason="stop"
    )
    monkeypatch.setattr(openai_provider, "openai", fake_openai)

    provider = OpenAIProvider(api_key="abc")
    messages = [
        ChatMessage(role="system", content="You are helpful."),
        ChatMessage(role="user", content="Hi"),
    ]
    resp = provider.send(messages)

    assert resp.content == "Hello there"
    assert resp.stop_reason == "end"
    assert resp.tool_calls == []

    sent_kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert sent_kwargs["messages"][0] == {"role": "system", "content": "You are helpful."}
    assert sent_kwargs["messages"][1] == {"role": "user", "content": "Hi"}
    assert "tools" not in sent_kwargs


def test_send_with_tools_and_tool_use_response(monkeypatch):
    fake_openai = MagicMock()
    fake_client = MagicMock()
    fake_openai.OpenAI.return_value = fake_client

    tc_obj = make_tool_call_obj("call_1", "get_project_info", {"x": 1})
    fake_client.chat.completions.create.return_value = make_response(
        content=None, tool_calls=[tc_obj], finish_reason="tool_calls"
    )
    monkeypatch.setattr(openai_provider, "openai", fake_openai)

    provider = OpenAIProvider(api_key="abc")
    tools = [
        ToolSpec(
            name="get_project_info",
            description="desc",
            parameters={"type": "object", "properties": {}, "required": []},
        )
    ]
    resp = provider.send([ChatMessage(role="user", content="do it")], tools)

    assert resp.stop_reason == "tool_use"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0] == ToolCall(id="call_1", name="get_project_info", arguments={"x": 1})

    sent_kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert sent_kwargs["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_project_info",
                "description": "desc",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]


def test_assistant_message_with_tool_calls_serialized(monkeypatch):
    fake_openai = MagicMock()
    fake_client = MagicMock()
    fake_openai.OpenAI.return_value = fake_client
    fake_client.chat.completions.create.return_value = make_response(
        content="ok", finish_reason="stop"
    )
    monkeypatch.setattr(openai_provider, "openai", fake_openai)

    provider = OpenAIProvider(api_key="abc")
    messages = [
        ChatMessage(role="user", content="do it"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call_1", name="list_components", arguments={"filter": "R"})],
        ),
        ChatMessage(role="tool", content="R1, R2", tool_call_id="call_1"),
    ]
    provider.send(messages)

    sent_kwargs = fake_client.chat.completions.create.call_args.kwargs
    assistant_msg = sent_kwargs["messages"][1]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["content"] is None
    assert assistant_msg["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "list_components",
                "arguments": json.dumps({"filter": "R"}),
            },
        }
    ]

    tool_msg = sent_kwargs["messages"][2]
    assert tool_msg == {"role": "tool", "tool_call_id": "call_1", "content": "R1, R2"}


def test_content_filter_returns_error_response(monkeypatch):
    fake_openai = MagicMock()
    fake_client = MagicMock()
    fake_openai.OpenAI.return_value = fake_client
    fake_client.chat.completions.create.return_value = make_response(
        content="", finish_reason="content_filter"
    )
    monkeypatch.setattr(openai_provider, "openai", fake_openai)

    provider = OpenAIProvider(api_key="abc")
    resp = provider.send([ChatMessage(role="user", content="hi")])

    assert resp.stop_reason == "error"
    assert resp.error is not None


def test_invalid_tool_call_arguments_raise_provider_error(monkeypatch):
    fake_openai = MagicMock()
    fake_client = MagicMock()
    fake_openai.OpenAI.return_value = fake_client

    bad_tc = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="broken", arguments="{not json"),
    )
    fake_client.chat.completions.create.return_value = make_response(
        content=None, tool_calls=[bad_tc], finish_reason="tool_calls"
    )
    monkeypatch.setattr(openai_provider, "openai", fake_openai)

    provider = OpenAIProvider(api_key="abc")
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="user", content="hi")])


def test_sdk_exceptions_wrapped_in_provider_error(monkeypatch):
    class FakeAuthError(Exception):
        pass

    class FakeRateLimitError(Exception):
        pass

    class FakeConnError(Exception):
        pass

    class FakeStatusError(Exception):
        pass

    fake_openai = MagicMock()
    fake_openai.AuthenticationError = FakeAuthError
    fake_openai.RateLimitError = FakeRateLimitError
    fake_openai.APIConnectionError = FakeConnError
    fake_openai.APIStatusError = FakeStatusError

    fake_client = MagicMock()
    fake_openai.OpenAI.return_value = fake_client
    fake_client.chat.completions.create.side_effect = FakeAuthError("bad key")
    monkeypatch.setattr(openai_provider, "openai", fake_openai)

    provider = OpenAIProvider(api_key="abc")
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="user", content="hi")])
