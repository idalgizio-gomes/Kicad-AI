"""Tests for the Claude Code CLI provider.

These tests never invoke the real `claude` binary or spend real quota:
`subprocess.run` is mocked. `conftest.py` puts `plugins/` on the path so the
imports below resolve without going through `plugins/__init__.py` (which
imports `pcbnew`).
"""

import json
import subprocess
from unittest import mock

import pytest

import llm_providers.claude_code_cli_provider as ccp
from llm_providers.base import (
    Attachment,
    ChatMessage,
    ChatResponse,
    ProviderError,
    ToolCall,
    ToolSpec,
)


# --------------------------------------------------------------------------- #
# find_claude_cli()
# --------------------------------------------------------------------------- #
def test_find_claude_cli_prefers_shutil_which(monkeypatch):
    monkeypatch.setattr(ccp.shutil, "which", lambda name: r"C:\bin\claude.exe")
    assert ccp.find_claude_cli() == r"C:\bin\claude.exe"


def test_find_claude_cli_falls_back_to_appdata_npm(monkeypatch, tmp_path):
    monkeypatch.setattr(ccp.shutil, "which", lambda name: None)
    monkeypatch.setattr(ccp.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    npm_dir = tmp_path / "npm"
    npm_dir.mkdir()
    claude_cmd = npm_dir / "claude.cmd"
    claude_cmd.write_text("@echo off", encoding="utf-8")

    assert ccp.find_claude_cli() == str(claude_cmd)


def test_find_claude_cli_returns_none_when_nowhere_found(monkeypatch, tmp_path):
    monkeypatch.setattr(ccp.shutil, "which", lambda name: None)
    monkeypatch.setattr(ccp.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert ccp.find_claude_cli() is None


# --------------------------------------------------------------------------- #
# Basic contract
# --------------------------------------------------------------------------- #
def test_identity_attributes():
    assert ccp.ClaudeCodeCLIProvider.id == "claude_cli"
    assert ccp.ClaudeCodeCLIProvider.display_name == "Claude Code (subscrição local)"


def test_default_model_is_empty():
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    assert provider.default_model() == ""


def test_list_models_returns_known_claude_models():
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    models = provider.list_models()
    assert models == ccp.KNOWN_CLAUDE_MODELS
    # Returned list must be a copy - mutating it must not corrupt the
    # shared constant for the next caller.
    models.append("not-a-real-model")
    assert "not-a-real-model" not in ccp.KNOWN_CLAUDE_MODELS


def test_is_configured_reflects_cli_presence(monkeypatch):
    monkeypatch.setattr(ccp, "find_claude_cli", lambda: r"C:\bin\claude.exe")
    assert ccp.ClaudeCodeCLIProvider(api_key=None).is_configured() is True

    monkeypatch.setattr(ccp, "find_claude_cli", lambda: None)
    assert ccp.ClaudeCodeCLIProvider(api_key=None).is_configured() is False


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
    prompt = ccp.ClaudeCodeCLIProvider._build_prompt(messages)
    assert "regra 1" in prompt
    assert "Utilizador: primeira pergunta" in prompt
    assert "Assistente: primeira resposta" in prompt
    # The final user turn is appended raw (not prefixed with "Utilizador:").
    assert prompt.strip().endswith("segunda pergunta")
    assert "Utilizador: segunda pergunta" not in prompt


def test_build_prompt_empty_when_no_content():
    assert ccp.ClaudeCodeCLIProvider._build_prompt([]) == ""


def test_build_prompt_includes_attachment_path_reference():
    # Unlike the API-key providers (which read+base64 the file), the CLI
    # provider is a full agent with real filesystem access - it's told the
    # real path and reads the file itself via its own Read tool, so the
    # prompt only needs to reference the path, never the file's content.
    messages = [
        ChatMessage(
            role="user",
            content="vê este ficheiro",
            attachments=[Attachment(path=r"C:\proj\board.kicad_pcb", name="board.kicad_pcb")],
        )
    ]
    prompt = ccp.ClaudeCodeCLIProvider._build_prompt(messages)
    assert "vê este ficheiro" in prompt
    assert "board.kicad_pcb" in prompt
    assert r"C:\proj\board.kicad_pcb" in prompt


def test_build_prompt_attachment_with_no_text_content_still_rendered():
    # An attach-only message (no typed text) must not be dropped by the
    # "if m.content" style check that predates attachments.
    messages = [
        ChatMessage(
            role="user",
            content="",
            attachments=[Attachment(path=r"C:\proj\board.png", name="board.png")],
        )
    ]
    prompt = ccp.ClaudeCodeCLIProvider._build_prompt(messages)
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
    prompt = ccp.ClaudeCodeCLIProvider._build_prompt(
        [ChatMessage(role="user", content="pergunta")], tools=[spec]
    )
    assert "```action" in prompt
    assert "move_footprint" in prompt
    assert "Move um componente" in prompt
    # The exact parameter schema (not just name/description) must be spelled
    # out - a real end-to-end test against the actual CLI showed the model
    # invents plausible-but-wrong argument names ("x" instead of "x_mm")
    # without it, since text-convention tool calling has no schema
    # enforcement of its own.
    assert "x_mm" in prompt
    assert "required" in prompt


def test_build_prompt_omits_tool_instructions_when_no_tools():
    prompt = ccp.ClaudeCodeCLIProvider._build_prompt(
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
    prompt = ccp.ClaudeCodeCLIProvider._build_prompt(messages)
    assert "move_footprint" in prompt
    assert "R1 movido" in prompt
    # The final message here is a TOOL result, not a fresh user question —
    # it must still carry its "Resultado" label (only a trailing USER turn
    # is ever rendered unprefixed).
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
    remaining, tool_call = ccp._extract_action_block(text)
    assert tool_call is not None
    assert tool_call.name == "move_footprint"
    assert tool_call.arguments == {"reference": "R1", "x_mm": 1}
    assert "```action" not in remaining
    assert "Vou mover" in remaining
    assert "Pronto." in remaining


def test_extract_action_block_none_when_absent():
    text = "Só uma resposta normal, sem ferramentas."
    remaining, tool_call = ccp._extract_action_block(text)
    assert tool_call is None
    assert remaining == text


def test_extract_action_block_malformed_json_degrades_to_none():
    text = "```action\n{not valid json at all\n```"
    remaining, tool_call = ccp._extract_action_block(text)
    assert tool_call is None
    assert remaining == text


def test_extract_action_block_missing_name_degrades_to_none():
    text = '```action\n{"arguments": {"x": 1}}\n```'
    remaining, tool_call = ccp._extract_action_block(text)
    assert tool_call is None


def test_extract_action_block_non_dict_arguments_defaults_to_empty():
    text = '```action\n{"name": "foo", "arguments": "not-a-dict"}\n```'
    remaining, tool_call = ccp._extract_action_block(text)
    assert tool_call is not None
    assert tool_call.arguments == {}


def test_extract_action_block_case_insensitive_fence():
    text = '```ACTION\n{"name": "foo", "arguments": {}}\n```'
    _remaining, tool_call = ccp._extract_action_block(text)
    assert tool_call is not None
    assert tool_call.name == "foo"


def test_extract_action_block_each_call_gets_unique_id():
    text = '```action\n{"name": "foo", "arguments": {}}\n```'
    _r1, tc1 = ccp._extract_action_block(text)
    _r2, tc2 = ccp._extract_action_block(text)
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
    monkeypatch.setattr(ccp, "find_claude_cli", lambda: r"C:\bin\claude.exe")


def test_send_raises_when_cli_missing(monkeypatch):
    monkeypatch.setattr(ccp, "find_claude_cli", lambda: None)
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    with pytest.raises(ProviderError) as excinfo:
        provider.send([ChatMessage(role="user", content="oi")])
    assert "npm install" in str(excinfo.value)


def test_send_raises_when_no_content(cli_present):
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="system", content="")])


def test_send_invokes_cli_with_expected_args(cli_present, monkeypatch):
    payload = {"is_error": False, "result": "Ok, tudo bem!"}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)

    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    resp = provider.send([ChatMessage(role="user", content="diz ok")])

    assert isinstance(resp, ChatResponse)
    assert resp.content == "Ok, tudo bem!"
    assert resp.tool_calls == []
    assert resp.stop_reason == "end"

    args, kwargs = run_mock.call_args
    cmd = args[0]
    assert cmd[0] == r"C:\bin\claude.exe"
    assert cmd[1] == "-p"
    assert "--output-format" in cmd and "json" in cmd
    # The prompt must travel via stdin, never as a CLI argument (a
    # multi-line prompt corrupts cmd.exe's tokenization of the `claude.CMD`
    # batch-file shim on Windows and silently drops later flags).
    assert "diz ok" not in cmd
    assert kwargs["input"] == "diz ok"
    assert kwargs["timeout"] == ccp._TIMEOUT_S


def test_send_passes_model_flag_when_set(cli_present, monkeypatch):
    payload = {"is_error": False, "result": "ok"}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)
    provider = ccp.ClaudeCodeCLIProvider(api_key=None, model="opus")
    provider.send([ChatMessage(role="user", content="x")])
    cmd = run_mock.call_args[0][0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_send_omits_model_flag_by_default(cli_present, monkeypatch):
    payload = {"is_error": False, "result": "ok"}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    provider.send([ChatMessage(role="user", content="x")])
    cmd = run_mock.call_args[0][0]
    assert "--model" not in cmd


# --------------------------------------------------------------------------- #
# permission mode / tool scoping
# --------------------------------------------------------------------------- #
def test_default_permission_mode_is_manual():
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    assert provider.permission_mode == ccp.PERMISSION_MODE_MANUAL


def test_send_always_scopes_tools_and_passes_permission_mode(cli_present, monkeypatch):
    payload = {"is_error": False, "result": "ok"}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    provider.send([ChatMessage(role="user", content="x")])
    cmd = run_mock.call_args[0][0]

    assert "--tools" in cmd
    assert cmd[cmd.index("--tools") + 1] == ccp._ENABLED_BUILTIN_TOOLS
    # Bash must never be part of the enabled set - file/folder access only.
    assert "Bash" not in cmd[cmd.index("--tools") + 1]

    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == ccp.PERMISSION_MODE_MANUAL


def test_send_uses_whatever_permission_mode_is_set_on_the_instance(cli_present, monkeypatch):
    payload = {"is_error": False, "result": "ok"}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    provider.permission_mode = ccp.PERMISSION_MODE_AUTO
    provider.send([ChatMessage(role="user", content="x")])
    cmd = run_mock.call_args[0][0]
    assert cmd[cmd.index("--permission-mode") + 1] == ccp.PERMISSION_MODE_AUTO


def test_permission_modes_constant_has_exactly_three_values():
    # manual (safe default), plan (describe, don't execute), acceptEdits
    # (auto-approve file edits) - the three exposed by the GUI's "Modo:"
    # selector. Not auto/bypassPermissions/dontAsk, which are broader than
    # what was asked for and were never tested against the real CLI.
    assert ccp.PERMISSION_MODES == ["manual", "plan", "acceptEdits"]


def test_send_forwards_tools_into_stdin_prompt(cli_present, monkeypatch):
    """`tools` reaches the CLI via the piped stdin prompt (not argv — see
    the stdin-vs-argv note elsewhere in this file), as instructions the
    model can act on via the ```action text convention (module docstring)."""
    payload = {"is_error": False, "result": "sem ferramentas usadas"}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)

    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    spec = ToolSpec(name="run_drc", description="d", parameters={"type": "object"})
    resp = provider.send([ChatMessage(role="user", content="x")], tools=[spec])

    assert resp.tool_calls == []
    assert resp.stop_reason == "end"
    cmd = run_mock.call_args[0][0]
    assert "run_drc" not in " ".join(cmd)  # never leaks into argv
    assert "run_drc" in run_mock.call_args.kwargs["input"]  # but is in stdin


def test_send_parses_action_block_into_tool_call(cli_present, monkeypatch):
    payload = {
        "is_error": False,
        "result": (
            "Vou mover o componente.\n\n"
            '```action\n{"name": "move_footprint", "arguments": {"reference": "R1", "x_mm": 5}}\n```'
        ),
    }
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)

    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    spec = ToolSpec(name="move_footprint", description="move", parameters={"type": "object"})
    resp = provider.send([ChatMessage(role="user", content="move R1")], tools=[spec])

    assert resp.stop_reason == "tool_use"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "move_footprint"
    assert resp.tool_calls[0].arguments == {"reference": "R1", "x_mm": 5}
    assert "```action" not in resp.content
    assert "Vou mover" in resp.content


def test_send_plain_text_reply_has_no_tool_calls(cli_present, monkeypatch):
    payload = {"is_error": False, "result": "Só uma resposta em texto."}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)

    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    resp = provider.send([ChatMessage(role="user", content="oi")])

    assert resp.stop_reason == "end"
    assert resp.tool_calls == []
    assert resp.content == "Só uma resposta em texto."


def test_send_nonzero_returncode_raises(cli_present, monkeypatch):
    run_mock = mock.MagicMock(
        return_value=_fake_run(stdout="", stderr="algo correu mal", returncode=1)
    )
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    with pytest.raises(ProviderError) as excinfo:
        provider.send([ChatMessage(role="user", content="x")])
    assert "algo correu mal" in str(excinfo.value)


def test_send_invalid_json_raises(cli_present, monkeypatch):
    run_mock = mock.MagicMock(return_value=_fake_run(stdout="not json"))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="user", content="x")])


def test_send_is_error_payload_raises(cli_present, monkeypatch):
    payload = {"is_error": True, "result": "quota excedida"}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    with pytest.raises(ProviderError) as excinfo:
        provider.send([ChatMessage(role="user", content="x")])
    assert "quota excedida" in str(excinfo.value)


def test_send_missing_result_field_raises(cli_present, monkeypatch):
    payload = {"is_error": False}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="user", content="x")])


def test_send_timeout_raises(cli_present, monkeypatch):
    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(ccp.subprocess, "run", _raise_timeout)
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    with pytest.raises(ProviderError) as excinfo:
        provider.send([ChatMessage(role="user", content="x")])
    assert "180" in str(excinfo.value)


def test_send_populates_cost_usd(cli_present, monkeypatch):
    payload = {"is_error": False, "result": "ok", "total_cost_usd": 0.0776229}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    resp = provider.send([ChatMessage(role="user", content="x")])
    assert resp.cost_usd == 0.0776229


def test_send_missing_cost_usd_is_none(cli_present, monkeypatch):
    payload = {"is_error": False, "result": "ok"}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    resp = provider.send([ChatMessage(role="user", content="x")])
    assert resp.cost_usd is None


def test_send_raw_payload_preserved(cli_present, monkeypatch):
    payload = {"is_error": False, "result": "ok", "total_cost_usd": 0.01}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    resp = provider.send([ChatMessage(role="user", content="x")])
    assert resp.raw == payload
