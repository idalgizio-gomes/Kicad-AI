"""Tests for the Gemini CLI provider.

These tests never invoke the real `gemini` binary or spend real quota:
`subprocess.run` is mocked. `conftest.py` puts `plugins/` on the path so the
imports below resolve without going through `plugins/__init__.py` (which
imports `pcbnew`).
"""

import json
import subprocess
from unittest import mock

import pytest

import llm_providers.gemini_cli_provider as gcp
from llm_providers.base import (
    Attachment,
    ChatMessage,
    ChatResponse,
    ProviderError,
    ToolCall,
    ToolSpec,
)


# --------------------------------------------------------------------------- #
# find_gemini_cli()
# --------------------------------------------------------------------------- #
def test_find_gemini_cli_prefers_shutil_which(monkeypatch):
    monkeypatch.setattr(gcp.shutil, "which", lambda name: r"C:\bin\gemini.exe")
    assert gcp.find_gemini_cli() == r"C:\bin\gemini.exe"


def test_find_gemini_cli_falls_back_to_appdata_npm(monkeypatch, tmp_path):
    monkeypatch.setattr(gcp.shutil, "which", lambda name: None)
    monkeypatch.setattr(gcp.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    npm_dir = tmp_path / "npm"
    npm_dir.mkdir()
    gemini_cmd = npm_dir / "gemini.cmd"
    gemini_cmd.write_text("@echo off", encoding="utf-8")

    assert gcp.find_gemini_cli() == str(gemini_cmd)


def test_find_gemini_cli_returns_none_when_nowhere_found(monkeypatch, tmp_path):
    monkeypatch.setattr(gcp.shutil, "which", lambda name: None)
    monkeypatch.setattr(gcp.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert gcp.find_gemini_cli() is None


# --------------------------------------------------------------------------- #
# Basic contract
# --------------------------------------------------------------------------- #
def test_identity_attributes():
    assert gcp.GeminiCLIProvider.id == "gemini_cli"
    assert gcp.GeminiCLIProvider.display_name == "Gemini CLI (conta Google)"


def test_default_model_is_empty():
    provider = gcp.GeminiCLIProvider(api_key=None)
    assert provider.default_model() == ""


def test_list_models_returns_known_gemini_cli_models():
    provider = gcp.GeminiCLIProvider(api_key=None)
    models = provider.list_models()
    assert models == gcp.KNOWN_GEMINI_CLI_MODELS
    # Returned list must be a copy - mutating it must not corrupt the
    # shared constant for the next caller.
    models.append("not-a-real-model")
    assert "not-a-real-model" not in gcp.KNOWN_GEMINI_CLI_MODELS


def test_is_configured_reflects_cli_presence(monkeypatch):
    monkeypatch.setattr(gcp, "find_gemini_cli", lambda: r"C:\bin\gemini.exe")
    assert gcp.GeminiCLIProvider(api_key=None).is_configured() is True

    monkeypatch.setattr(gcp, "find_gemini_cli", lambda: None)
    assert gcp.GeminiCLIProvider(api_key=None).is_configured() is False


# --------------------------------------------------------------------------- #
# Prompt flattening
# --------------------------------------------------------------------------- #
def test_build_prompt_includes_system_and_transcript():
    messages = [
        ChatMessage(role="system", content="regra 1"),
        ChatMessage(role="user", content="primeira pergunta"),
        ChatMessage(role="assistant", content="primeira resposta"),
        ChatMessage(role="user", content="segunda pergunta"),
    ]
    prompt = gcp.GeminiCLIProvider._build_prompt(messages)
    assert "regra 1" in prompt
    assert "Utilizador: primeira pergunta" in prompt
    assert "Assistente: primeira resposta" in prompt
    # The final user turn is appended raw (not prefixed with "Utilizador:").
    assert prompt.strip().endswith("segunda pergunta")
    assert "Utilizador: segunda pergunta" not in prompt


def test_build_prompt_empty_when_no_content():
    assert gcp.GeminiCLIProvider._build_prompt([]) == ""


def test_build_prompt_includes_attachment_path_reference():
    messages = [
        ChatMessage(
            role="user",
            content="vê este ficheiro",
            attachments=[Attachment(path=r"C:\proj\board.kicad_pcb", name="board.kicad_pcb")],
        )
    ]
    prompt = gcp.GeminiCLIProvider._build_prompt(messages)
    assert "vê este ficheiro" in prompt
    assert "board.kicad_pcb" in prompt
    assert r"C:\proj\board.kicad_pcb" in prompt


def test_build_prompt_attachment_with_no_text_content_still_rendered():
    messages = [
        ChatMessage(
            role="user",
            content="",
            attachments=[Attachment(path=r"C:\proj\board.png", name="board.png")],
        )
    ]
    prompt = gcp.GeminiCLIProvider._build_prompt(messages)
    assert "board.png" in prompt


def test_build_prompt_includes_tool_instructions_when_tools_given():
    spec = ToolSpec(
        name="move_footprint",
        description="Move um componente",
        parameters={
            "type": "object",
            "properties": {"reference": {"type": "string"}, "x_mm": {"type": "number"}},
            "required": ["reference", "x_mm"],
        },
    )
    prompt = gcp.GeminiCLIProvider._build_prompt(
        [ChatMessage(role="user", content="pergunta")], tools=[spec]
    )
    assert "```action" in prompt
    assert "move_footprint" in prompt
    assert "Move um componente" in prompt
    assert "x_mm" in prompt
    assert "required" in prompt


def test_build_prompt_omits_tool_instructions_when_no_tools():
    prompt = gcp.GeminiCLIProvider._build_prompt(
        [ChatMessage(role="user", content="pergunta")]
    )
    assert "```action" not in prompt


def test_build_prompt_renders_prior_tool_call_and_result():
    messages = [
        ChatMessage(role="user", content="move R1"),
        ChatMessage(
            role="assistant",
            content="vou mover",
            tool_calls=[ToolCall(id="t1", name="move_footprint", arguments={"reference": "R1"})],
        ),
        ChatMessage(role="tool", content="R1 movido", tool_call_id="t1"),
    ]
    prompt = gcp.GeminiCLIProvider._build_prompt(messages)
    assert "move_footprint" in prompt
    assert "R1 movido" in prompt
    assert prompt.strip().endswith("Resultado: [move_footprint] R1 movido")


# --------------------------------------------------------------------------- #
# _extract_action_block()
# --------------------------------------------------------------------------- #
def test_extract_action_block_valid():
    text = (
        "Vou mover o componente.\n\n"
        '```action\n{"name": "move_footprint", "arguments": {"reference": "R1", "x_mm": 1}}\n```\n'
        "Pronto."
    )
    remaining, tool_call = gcp._extract_action_block(text)
    assert tool_call is not None
    assert tool_call.name == "move_footprint"
    assert tool_call.arguments == {"reference": "R1", "x_mm": 1}
    assert "```action" not in remaining
    assert "Vou mover" in remaining
    assert "Pronto." in remaining


def test_extract_action_block_none_when_absent():
    text = "Só uma resposta normal, sem ferramentas."
    remaining, tool_call = gcp._extract_action_block(text)
    assert tool_call is None
    assert remaining == text


def test_extract_action_block_malformed_json_degrades_to_none():
    text = "```action\n{not valid json at all\n```"
    remaining, tool_call = gcp._extract_action_block(text)
    assert tool_call is None
    assert remaining == text


def test_extract_action_block_missing_name_degrades_to_none():
    text = '```action\n{"arguments": {"x": 1}}\n```'
    remaining, tool_call = gcp._extract_action_block(text)
    assert tool_call is None


def test_extract_action_block_non_dict_arguments_defaults_to_empty():
    text = '```action\n{"name": "foo", "arguments": "not-a-dict"}\n```'
    remaining, tool_call = gcp._extract_action_block(text)
    assert tool_call is not None
    assert tool_call.arguments == {}


def test_extract_action_block_case_insensitive_fence():
    text = '```ACTION\n{"name": "foo", "arguments": {}}\n```'
    _remaining, tool_call = gcp._extract_action_block(text)
    assert tool_call is not None
    assert tool_call.name == "foo"


def test_extract_action_block_each_call_gets_unique_id():
    text = '```action\n{"name": "foo", "arguments": {}}\n```'
    _r1, tc1 = gcp._extract_action_block(text)
    _r2, tc2 = gcp._extract_action_block(text)
    assert tc1.id != tc2.id


# --------------------------------------------------------------------------- #
# send() — subprocess invocation and JSON parsing
# --------------------------------------------------------------------------- #
def _fake_run(stdout="", stderr="", returncode=0):
    return mock.MagicMock(
        spec=subprocess.CompletedProcess,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
    )


@pytest.fixture
def cli_present(monkeypatch):
    monkeypatch.setattr(gcp, "find_gemini_cli", lambda: r"C:\bin\gemini.exe")


def test_send_raises_when_cli_missing(monkeypatch):
    monkeypatch.setattr(gcp, "find_gemini_cli", lambda: None)
    provider = gcp.GeminiCLIProvider(api_key=None)
    with pytest.raises(ProviderError) as excinfo:
        provider.send([ChatMessage(role="user", content="oi")])
    assert "npm install" in str(excinfo.value)


def test_send_raises_when_no_content(cli_present):
    provider = gcp.GeminiCLIProvider(api_key=None)
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="system", content="")])


def test_send_invokes_cli_with_expected_args(cli_present, monkeypatch):
    payload = {"response": "Ok, tudo bem!"}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(gcp.subprocess, "run", run_mock)

    provider = gcp.GeminiCLIProvider(api_key=None)
    resp = provider.send([ChatMessage(role="user", content="diz ok")])

    assert isinstance(resp, ChatResponse)
    assert resp.content == "Ok, tudo bem!"
    assert resp.tool_calls == []
    assert resp.stop_reason == "end"

    args, kwargs = run_mock.call_args
    cmd = args[0]
    assert cmd[0] == r"C:\bin\gemini.exe"
    assert cmd[1] == "-p"
    assert "--output-format" in cmd and "JSON" in cmd
    # The prompt must travel via stdin, never as a CLI argument.
    assert "diz ok" not in cmd
    assert kwargs["input"] == "diz ok"
    assert kwargs["timeout"] == gcp._TIMEOUT_S


def test_send_passes_model_flag_when_set(cli_present, monkeypatch):
    payload = {"response": "ok"}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(gcp.subprocess, "run", run_mock)
    provider = gcp.GeminiCLIProvider(api_key=None, model="gemini-2.5-pro")
    provider.send([ChatMessage(role="user", content="x")])
    cmd = run_mock.call_args[0][0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "gemini-2.5-pro"


def test_send_omits_model_flag_by_default(cli_present, monkeypatch):
    payload = {"response": "ok"}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(gcp.subprocess, "run", run_mock)
    provider = gcp.GeminiCLIProvider(api_key=None)
    provider.send([ChatMessage(role="user", content="x")])
    cmd = run_mock.call_args[0][0]
    assert "--model" not in cmd


def test_send_forwards_tools_into_stdin_prompt(cli_present, monkeypatch):
    """`tools` reaches the CLI via the piped stdin prompt (not argv), as
    instructions the model can act on via the ```action text convention."""
    payload = {"response": "sem ferramentas usadas"}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(gcp.subprocess, "run", run_mock)

    provider = gcp.GeminiCLIProvider(api_key=None)
    spec = ToolSpec(name="run_drc", description="d", parameters={"type": "object"})
    resp = provider.send([ChatMessage(role="user", content="x")], tools=[spec])

    assert resp.tool_calls == []
    assert resp.stop_reason == "end"
    cmd = run_mock.call_args[0][0]
    assert "run_drc" not in " ".join(cmd)  # never leaks into argv
    assert "run_drc" in run_mock.call_args.kwargs["input"]  # but is in stdin


def test_send_parses_action_block_into_tool_call(cli_present, monkeypatch):
    payload = {
        "response": (
            "Vou mover o componente.\n\n"
            '```action\n{"name": "move_footprint", "arguments": {"reference": "R1", "x_mm": 5}}\n```'
        ),
    }
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(gcp.subprocess, "run", run_mock)

    provider = gcp.GeminiCLIProvider(api_key=None)
    spec = ToolSpec(name="move_footprint", description="move", parameters={"type": "object"})
    resp = provider.send([ChatMessage(role="user", content="move R1")], tools=[spec])

    assert resp.stop_reason == "tool_use"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "move_footprint"
    assert resp.tool_calls[0].arguments == {"reference": "R1", "x_mm": 5}
    assert "```action" not in resp.content
    assert "Vou mover" in resp.content


def test_send_plain_text_reply_has_no_tool_calls(cli_present, monkeypatch):
    payload = {"response": "Só uma resposta em texto."}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(gcp.subprocess, "run", run_mock)

    provider = gcp.GeminiCLIProvider(api_key=None)
    resp = provider.send([ChatMessage(role="user", content="oi")])

    assert resp.stop_reason == "end"
    assert resp.tool_calls == []
    assert resp.content == "Só uma resposta em texto."


def test_send_nonzero_returncode_raises_generic(cli_present, monkeypatch):
    run_mock = mock.MagicMock(
        return_value=_fake_run(stdout="", stderr="algo correu mal", returncode=1)
    )
    monkeypatch.setattr(gcp.subprocess, "run", run_mock)
    provider = gcp.GeminiCLIProvider(api_key=None)
    with pytest.raises(ProviderError) as excinfo:
        provider.send([ChatMessage(role="user", content="x")])
    assert "algo correu mal" in str(excinfo.value)


def test_send_exit_code_42_raises_input_error_message(cli_present, monkeypatch):
    run_mock = mock.MagicMock(
        return_value=_fake_run(stdout="", stderr="bad input", returncode=42)
    )
    monkeypatch.setattr(gcp.subprocess, "run", run_mock)
    provider = gcp.GeminiCLIProvider(api_key=None)
    with pytest.raises(ProviderError) as excinfo:
        provider.send([ChatMessage(role="user", content="x")])
    assert "42" in str(excinfo.value)


def test_send_exit_code_53_raises_turn_limit_message(cli_present, monkeypatch):
    run_mock = mock.MagicMock(
        return_value=_fake_run(stdout="", stderr="too many turns", returncode=53)
    )
    monkeypatch.setattr(gcp.subprocess, "run", run_mock)
    provider = gcp.GeminiCLIProvider(api_key=None)
    with pytest.raises(ProviderError) as excinfo:
        provider.send([ChatMessage(role="user", content="x")])
    assert "53" in str(excinfo.value)


def test_send_invalid_json_raises(cli_present, monkeypatch):
    run_mock = mock.MagicMock(return_value=_fake_run(stdout="not json"))
    monkeypatch.setattr(gcp.subprocess, "run", run_mock)
    provider = gcp.GeminiCLIProvider(api_key=None)
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="user", content="x")])


def test_send_error_field_raises(cli_present, monkeypatch):
    payload = {"response": "", "error": "quota excedida"}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(gcp.subprocess, "run", run_mock)
    provider = gcp.GeminiCLIProvider(api_key=None)
    with pytest.raises(ProviderError) as excinfo:
        provider.send([ChatMessage(role="user", content="x")])
    assert "quota excedida" in str(excinfo.value)


def test_send_missing_response_field_raises(cli_present, monkeypatch):
    payload = {"stats": {}}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(gcp.subprocess, "run", run_mock)
    provider = gcp.GeminiCLIProvider(api_key=None)
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="user", content="x")])


def test_send_timeout_raises(cli_present, monkeypatch):
    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(gcp.subprocess, "run", _raise_timeout)
    provider = gcp.GeminiCLIProvider(api_key=None)
    with pytest.raises(ProviderError) as excinfo:
        provider.send([ChatMessage(role="user", content="x")])
    assert "180" in str(excinfo.value)


def test_send_raw_payload_preserved(cli_present, monkeypatch):
    payload = {"response": "ok", "stats": {"tokens": 12}}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(gcp.subprocess, "run", run_mock)
    provider = gcp.GeminiCLIProvider(api_key=None)
    resp = provider.send([ChatMessage(role="user", content="x")])
    assert resp.raw == payload
