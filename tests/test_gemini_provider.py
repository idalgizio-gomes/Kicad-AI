"""Tests for the Gemini provider. No pcbnew/wx/real API key required; the
google-generativeai SDK is mocked via monkeypatch."""

from types import SimpleNamespace

import pytest

import llm_providers.gemini_provider as gp
from llm_providers.base import ChatMessage, ProviderError, ToolCall, ToolSpec


# --------------------------------------------------------------------------
# basic contract
# --------------------------------------------------------------------------

def test_default_model_and_identity():
    prov = gp.GeminiProvider(api_key="k")
    assert prov.default_model() == "gemini-2.0-flash"
    assert prov.model == "gemini-2.0-flash"
    assert prov.id == "gemini"
    assert prov.display_name == "Gemini (Google)"


def test_model_override():
    prov = gp.GeminiProvider(api_key="k", model="gemini-1.5-pro")
    assert prov.model == "gemini-1.5-pro"


def test_is_configured():
    assert gp.GeminiProvider(api_key="k").is_configured() is True
    assert gp.GeminiProvider(api_key=None).is_configured() is False
    assert gp.GeminiProvider(api_key="").is_configured() is False


def test_send_raises_when_package_missing(monkeypatch):
    monkeypatch.setattr(gp, "genai", None)
    prov = gp.GeminiProvider(api_key="k")
    with pytest.raises(ProviderError) as exc:
        prov.send([ChatMessage(role="user", content="hi")])
    assert "google-generativeai" in str(exc.value)


def test_send_raises_when_not_configured(monkeypatch):
    monkeypatch.setattr(gp, "genai", object())  # package "present"
    prov = gp.GeminiProvider(api_key=None)
    with pytest.raises(ProviderError):
        prov.send([ChatMessage(role="user", content="hi")])


# --------------------------------------------------------------------------
# _clean_schema
# --------------------------------------------------------------------------

def test_clean_schema_removes_unsupported_keys():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "MyTool",
        "properties": {
            "name": {"type": "string", "default": "x", "format": "email"},
            "when": {"type": "string", "format": "date-time"},
            "nested": {
                "type": "object",
                "additionalProperties": True,
                "properties": {"a": {"type": "integer", "default": 1}},
            },
        },
        "required": ["name"],
    }
    cleaned = gp._clean_schema(schema)
    assert "additionalProperties" not in cleaned
    assert "$schema" not in cleaned
    assert "title" not in cleaned
    name = cleaned["properties"]["name"]
    assert "default" not in name
    assert "format" not in name  # email dropped
    assert cleaned["properties"]["when"]["format"] == "date-time"  # kept
    nested = cleaned["properties"]["nested"]
    assert "additionalProperties" not in nested
    assert "default" not in nested["properties"]["a"]
    # original untouched
    assert schema["additionalProperties"] is False


def test_clean_schema_handles_lists():
    schema = {"anyOf": [{"type": "string", "default": "a"}, {"type": "null"}]}
    cleaned = gp._clean_schema(schema)
    assert cleaned["anyOf"][0] == {"type": "string"}


# --------------------------------------------------------------------------
# outbound translation
# --------------------------------------------------------------------------

def test_build_history_system_and_roles():
    prov = gp.GeminiProvider(api_key="k")
    msgs = [
        ChatMessage(role="system", content="sysA"),
        ChatMessage(role="system", content="sysB"),
        ChatMessage(role="user", content="hello"),
        ChatMessage(
            role="assistant",
            content="thinking",
            tool_calls=[ToolCall(id="gemini-call-0", name="do_it", arguments={"x": 1})],
        ),
        ChatMessage(role="tool", content="result text", tool_call_id="gemini-call-0"),
    ]
    system, history = prov._build_history(msgs)
    assert system == "sysA\n\nsysB"
    assert history[0] == {"role": "user", "parts": ["hello"]}
    model_turn = history[1]
    assert model_turn["role"] == "model"
    assert model_turn["parts"][0] == "thinking"
    assert model_turn["parts"][1] == {
        "function_call": {"name": "do_it", "args": {"x": 1}}
    }
    tool_turn = history[2]
    assert tool_turn["role"] == "user"
    fr = tool_turn["parts"][0]["function_response"]
    assert fr["name"] == "do_it"  # id translated back to function name
    assert fr["response"] == {"result": "result text"}


def test_build_tools_none():
    prov = gp.GeminiProvider(api_key="k")
    assert prov._build_tools(None) is None
    assert prov._build_tools([]) is None


def test_build_tools_shape():
    prov = gp.GeminiProvider(api_key="k")
    spec = ToolSpec(
        name="ping",
        description="ping the board",
        parameters={"type": "object", "additionalProperties": False, "properties": {}},
    )
    out = prov._build_tools([spec])
    decl = out[0]["function_declarations"][0]
    assert decl["name"] == "ping"
    assert decl["description"] == "ping the board"
    assert "additionalProperties" not in decl["parameters"]


# --------------------------------------------------------------------------
# inbound translation via a mocked SDK
# --------------------------------------------------------------------------

class _FakeModel:
    def __init__(self, response):
        self._response = response

    def generate_content(self, contents):
        self.contents = contents
        return self._response


def _install_fake_genai(monkeypatch, response, capture=None):
    def fake_configure(api_key=None):
        if capture is not None:
            capture["api_key"] = api_key

    def fake_model(model, system_instruction=None, tools=None):
        if capture is not None:
            capture["model"] = model
            capture["system_instruction"] = system_instruction
            capture["tools"] = tools
        return _FakeModel(response)

    fake = SimpleNamespace(configure=fake_configure, GenerativeModel=fake_model)
    monkeypatch.setattr(gp, "genai", fake)
    return fake


def test_send_text_response(monkeypatch):
    part = SimpleNamespace(text="hi there", function_call=None)
    content = SimpleNamespace(parts=[part])
    candidate = SimpleNamespace(content=content, finish_reason="STOP")
    response = SimpleNamespace(candidates=[candidate], prompt_feedback=None)

    capture = {}
    _install_fake_genai(monkeypatch, response, capture)

    prov = gp.GeminiProvider(api_key="secret")
    resp = prov.send(
        [
            ChatMessage(role="system", content="be nice"),
            ChatMessage(role="user", content="hello"),
        ]
    )
    assert resp.content == "hi there"
    assert resp.stop_reason == "end"
    assert resp.tool_calls == []
    assert capture["api_key"] == "secret"
    assert capture["system_instruction"] == "be nice"


def test_send_function_call_response(monkeypatch):
    fc = SimpleNamespace(name="list_components", args={"filter": "R"})
    part = SimpleNamespace(text=None, function_call=fc)
    content = SimpleNamespace(parts=[part])
    candidate = SimpleNamespace(content=content, finish_reason="STOP")
    response = SimpleNamespace(candidates=[candidate], prompt_feedback=None)

    _install_fake_genai(monkeypatch, response)
    prov = gp.GeminiProvider(api_key="k")
    resp = prov.send([ChatMessage(role="user", content="what's on the board?")])

    assert resp.stop_reason == "tool_use"
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.name == "list_components"
    assert tc.arguments == {"filter": "R"}
    assert tc.id == "gemini-call-0"
    # synthetic id recorded so a later tool result maps back to the name
    assert prov._call_id_to_name["gemini-call-0"] == "list_components"


def test_send_prompt_blocked(monkeypatch):
    feedback = SimpleNamespace(block_reason="SAFETY")
    response = SimpleNamespace(candidates=[], prompt_feedback=feedback)
    _install_fake_genai(monkeypatch, response)
    prov = gp.GeminiProvider(api_key="k")
    resp = prov.send([ChatMessage(role="user", content="bad")])
    assert resp.stop_reason == "error"
    assert "seguran" in resp.error.lower()


def test_send_empty_candidates(monkeypatch):
    response = SimpleNamespace(candidates=[], prompt_feedback=None)
    _install_fake_genai(monkeypatch, response)
    prov = gp.GeminiProvider(api_key="k")
    resp = prov.send([ChatMessage(role="user", content="x")])
    assert resp.stop_reason == "error"


def test_send_candidate_without_parts(monkeypatch):
    content = SimpleNamespace(parts=[])
    candidate = SimpleNamespace(content=content, finish_reason="MAX_TOKENS")
    response = SimpleNamespace(candidates=[candidate], prompt_feedback=None)
    _install_fake_genai(monkeypatch, response)
    prov = gp.GeminiProvider(api_key="k")
    resp = prov.send([ChatMessage(role="user", content="x")])
    assert resp.stop_reason == "error"


def test_send_wraps_sdk_exception(monkeypatch):
    def boom(api_key=None):
        raise RuntimeError("network down")

    fake = SimpleNamespace(configure=boom, GenerativeModel=lambda *a, **k: None)
    monkeypatch.setattr(gp, "genai", fake)
    prov = gp.GeminiProvider(api_key="k")
    with pytest.raises(ProviderError) as exc:
        prov.send([ChatMessage(role="user", content="x")])
    assert "network down" in str(exc.value)


def test_roundtrip_tool_call_then_result(monkeypatch):
    """A tool_use response followed by a tool result must translate the
    synthetic id back to the function name in the next history build."""
    fc = SimpleNamespace(name="run_drc", args={})
    part = SimpleNamespace(text=None, function_call=fc)
    content = SimpleNamespace(parts=[part])
    candidate = SimpleNamespace(content=content, finish_reason="STOP")
    response = SimpleNamespace(candidates=[candidate], prompt_feedback=None)
    _install_fake_genai(monkeypatch, response)

    prov = gp.GeminiProvider(api_key="k")
    resp = prov.send([ChatMessage(role="user", content="run drc")])
    tc = resp.tool_calls[0]

    followup = [
        ChatMessage(role="user", content="run drc"),
        ChatMessage(role="assistant", content="", tool_calls=[tc]),
        ChatMessage(role="tool", content="0 violations", tool_call_id=tc.id),
    ]
    _system, history = prov._build_history(followup)
    fr = history[-1]["parts"][0]["function_response"]
    assert fr["name"] == "run_drc"
