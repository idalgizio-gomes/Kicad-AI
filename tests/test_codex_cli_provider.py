"""Tests for the Codex CLI provider.

These tests never invoke the real `codex` binary or spend real quota:
`subprocess.run` is mocked. `conftest.py` puts `plugins/` on the path so the
imports below resolve without going through `plugins/__init__.py` (which
imports `pcbnew`).
"""

import json
import subprocess
from unittest import mock

import pytest

import llm_providers.codex_cli_provider as cxp
from llm_providers.base import (
    Attachment,
    ChatMessage,
    ChatResponse,
    ProviderError,
    ToolCall,
    ToolSpec,
)


# --------------------------------------------------------------------------- #
# find_codex_cli()
# --------------------------------------------------------------------------- #
def test_find_codex_cli_prefers_shutil_which(monkeypatch):
    monkeypatch.setattr(cxp.shutil, "which", lambda name: r"C:\bin\codex.exe")
    assert cxp.find_codex_cli() == r"C:\bin\codex.exe"


def test_find_codex_cli_falls_back_to_appdata_npm(monkeypatch, tmp_path):
    monkeypatch.setattr(cxp.shutil, "which", lambda name: None)
    monkeypatch.setattr(cxp.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    npm_dir = tmp_path / "npm"
    npm_dir.mkdir()
    codex_cmd = npm_dir / "codex.cmd"
    codex_cmd.write_text("@echo off", encoding="utf-8")

    assert cxp.find_codex_cli() == str(codex_cmd)


def test_find_codex_cli_returns_none_when_nowhere_found(monkeypatch, tmp_path):
    monkeypatch.setattr(cxp.shutil, "which", lambda name: None)
    monkeypatch.setattr(cxp.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert cxp.find_codex_cli() is None


# --------------------------------------------------------------------------- #
# Basic contract
# --------------------------------------------------------------------------- #
def test_identity_attributes():
    assert cxp.CodexCLIProvider.id == "codex_cli"
    assert cxp.CodexCLIProvider.display_name == "Codex CLI (subscrição ChatGPT)"


def test_default_model_is_empty():
    provider = cxp.CodexCLIProvider(api_key=None)
    assert provider.default_model() == ""


def test_list_models_returns_known_codex_models():
    provider = cxp.CodexCLIProvider(api_key=None)
    models = provider.list_models()
    assert models == cxp.KNOWN_CODEX_MODELS
    # Returned list must be a copy - mutating it must not corrupt the
    # shared constant for the next caller.
    models.append("not-a-real-model")
    assert "not-a-real-model" not in cxp.KNOWN_CODEX_MODELS


def test_is_configured_reflects_cli_presence(monkeypatch):
    monkeypatch.setattr(cxp, "find_codex_cli", lambda: r"C:\bin\codex.exe")
    assert cxp.CodexCLIProvider(api_key=None).is_configured() is True

    monkeypatch.setattr(cxp, "find_codex_cli", lambda: None)
    assert cxp.CodexCLIProvider(api_key=None).is_configured() is False


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
    prompt = cxp.CodexCLIProvider._build_prompt(messages)
    assert "regra 1" in prompt
    assert "Utilizador: primeira pergunta" in prompt
    assert "Assistente: primeira resposta" in prompt
    # The final user turn is appended raw (not prefixed with "Utilizador:").
    assert prompt.strip().endswith("segunda pergunta")
    assert "Utilizador: segunda pergunta" not in prompt


def test_build_prompt_empty_when_no_content():
    assert cxp.CodexCLIProvider._build_prompt([]) == ""


def test_build_prompt_includes_attachment_path_reference():
    messages = [
        ChatMessage(
            role="user",
            content="vê este ficheiro",
            attachments=[Attachment(path=r"C:\proj\board.kicad_pcb", name="board.kicad_pcb")],
        )
    ]
    prompt = cxp.CodexCLIProvider._build_prompt(messages)
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
    prompt = cxp.CodexCLIProvider._build_prompt(messages)
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
    prompt = cxp.CodexCLIProvider._build_prompt(
        [ChatMessage(role="user", content="pergunta")], tools=[spec]
    )
    assert "```action" in prompt
    assert "move_footprint" in prompt
    assert "Move um componente" in prompt
    assert "x_mm" in prompt
    assert "required" in prompt


def test_build_prompt_omits_tool_instructions_when_no_tools():
    prompt = cxp.CodexCLIProvider._build_prompt(
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
    prompt = cxp.CodexCLIProvider._build_prompt(messages)
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
    remaining, tool_call = cxp._extract_action_block(text)
    assert tool_call is not None
    assert tool_call.name == "move_footprint"
    assert tool_call.arguments == {"reference": "R1", "x_mm": 1}
    assert "```action" not in remaining
    assert "Vou mover" in remaining
    assert "Pronto." in remaining


def test_extract_action_block_none_when_absent():
    text = "Só uma resposta normal, sem ferramentas."
    remaining, tool_call = cxp._extract_action_block(text)
    assert tool_call is None
    assert remaining == text


def test_extract_action_block_malformed_json_degrades_to_none():
    text = "```action\n{not valid json at all\n```"
    remaining, tool_call = cxp._extract_action_block(text)
    assert tool_call is None
    assert remaining == text


def test_extract_action_block_missing_name_degrades_to_none():
    text = '```action\n{"arguments": {"x": 1}}\n```'
    remaining, tool_call = cxp._extract_action_block(text)
    assert tool_call is None


def test_extract_action_block_non_dict_arguments_defaults_to_empty():
    text = '```action\n{"name": "foo", "arguments": "not-a-dict"}\n```'
    remaining, tool_call = cxp._extract_action_block(text)
    assert tool_call is not None
    assert tool_call.arguments == {}


def test_extract_action_block_case_insensitive_fence():
    text = '```ACTION\n{"name": "foo", "arguments": {}}\n```'
    _remaining, tool_call = cxp._extract_action_block(text)
    assert tool_call is not None
    assert tool_call.name == "foo"


def test_extract_action_block_each_call_gets_unique_id():
    text = '```action\n{"name": "foo", "arguments": {}}\n```'
    _r1, tc1 = cxp._extract_action_block(text)
    _r2, tc2 = cxp._extract_action_block(text)
    assert tc1.id != tc2.id


# --------------------------------------------------------------------------- #
# _extract_event_text() / _extract_event_error()
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", ["text", "message", "content", "response", "result"])
def test_extract_event_text_tries_each_plausible_key(key):
    assert cxp._extract_event_text({key: "olá"}) == "olá"


def test_extract_event_text_checks_nested_msg_object():
    assert cxp._extract_event_text({"msg": {"message": "olá aninhado"}}) == "olá aninhado"


def test_extract_event_text_none_when_nothing_plausible():
    assert cxp._extract_event_text({"type": "task_started"}) is None


def test_extract_event_text_ignores_blank_string():
    assert cxp._extract_event_text({"text": "   "}) is None


def test_extract_event_error_from_string_field():
    assert cxp._extract_event_error({"error": "falhou"}) == "falhou"


def test_extract_event_error_from_nested_message():
    assert cxp._extract_event_error({"error": {"message": "falhou também"}}) == "falhou também"


def test_extract_event_error_none_when_absent():
    assert cxp._extract_event_error({"text": "tudo bem"}) is None


# --------------------------------------------------------------------------- #
# send() — subprocess invocation and JSONL parsing
# --------------------------------------------------------------------------- #
def _fake_run(stdout="", stderr="", returncode=0):
    return mock.MagicMock(
        spec=subprocess.CompletedProcess,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
    )


def _jsonl(*events) -> str:
    return "\n".join(json.dumps(e) for e in events)


@pytest.fixture
def cli_present(monkeypatch):
    monkeypatch.setattr(cxp, "find_codex_cli", lambda: r"C:\bin\codex.exe")


def test_send_raises_when_cli_missing(monkeypatch):
    monkeypatch.setattr(cxp, "find_codex_cli", lambda: None)
    provider = cxp.CodexCLIProvider(api_key=None)
    with pytest.raises(ProviderError) as excinfo:
        provider.send([ChatMessage(role="user", content="oi")])
    assert "npm install" in str(excinfo.value)


def test_send_raises_when_no_content(cli_present):
    provider = cxp.CodexCLIProvider(api_key=None)
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="system", content="")])


def test_send_invokes_cli_with_expected_args(cli_present, monkeypatch):
    stdout = _jsonl({"type": "task_started"}, {"text": "Ok, tudo bem!"})
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=stdout))
    monkeypatch.setattr(cxp.subprocess, "run", run_mock)

    provider = cxp.CodexCLIProvider(api_key=None)
    resp = provider.send([ChatMessage(role="user", content="diz ok")])

    assert isinstance(resp, ChatResponse)
    assert resp.content == "Ok, tudo bem!"
    assert resp.tool_calls == []
    assert resp.stop_reason == "end"

    args, kwargs = run_mock.call_args
    cmd = args[0]
    assert cmd[0] == r"C:\bin\codex.exe"
    assert cmd[1] == "exec"
    assert "--json" in cmd
    assert cmd[-1] == "-"
    # The prompt must travel via stdin, never as a CLI argument.
    assert "diz ok" not in cmd
    assert kwargs["input"] == "diz ok"
    assert kwargs["timeout"] == cxp._TIMEOUT_S


def test_send_passes_model_flag_when_set(cli_present, monkeypatch):
    stdout = _jsonl({"text": "ok"})
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=stdout))
    monkeypatch.setattr(cxp.subprocess, "run", run_mock)
    provider = cxp.CodexCLIProvider(api_key=None, model="gpt-5-codex")
    provider.send([ChatMessage(role="user", content="x")])
    cmd = run_mock.call_args[0][0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "gpt-5-codex"


def test_send_omits_model_flag_by_default(cli_present, monkeypatch):
    stdout = _jsonl({"text": "ok"})
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=stdout))
    monkeypatch.setattr(cxp.subprocess, "run", run_mock)
    provider = cxp.CodexCLIProvider(api_key=None)
    provider.send([ChatMessage(role="user", content="x")])
    cmd = run_mock.call_args[0][0]
    assert "--model" not in cmd


# --------------------------------------------------------------------------- #
# permission mode (--sandbox)
# --------------------------------------------------------------------------- #
def test_default_permission_mode_is_read_only():
    provider = cxp.CodexCLIProvider(api_key=None)
    assert provider.permission_mode == cxp.PERMISSION_MODE_READ_ONLY


def test_send_passes_sandbox_flag(cli_present, monkeypatch):
    stdout = _jsonl({"text": "ok"})
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=stdout))
    monkeypatch.setattr(cxp.subprocess, "run", run_mock)
    provider = cxp.CodexCLIProvider(api_key=None)
    provider.send([ChatMessage(role="user", content="x")])
    cmd = run_mock.call_args[0][0]
    assert "--sandbox" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == cxp.PERMISSION_MODE_READ_ONLY


def test_send_uses_whatever_permission_mode_is_set_on_the_instance(cli_present, monkeypatch):
    stdout = _jsonl({"text": "ok"})
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=stdout))
    monkeypatch.setattr(cxp.subprocess, "run", run_mock)
    provider = cxp.CodexCLIProvider(api_key=None)
    provider.permission_mode = cxp.PERMISSION_MODE_DANGER_FULL_ACCESS
    provider.send([ChatMessage(role="user", content="x")])
    cmd = run_mock.call_args[0][0]
    assert cmd[cmd.index("--sandbox") + 1] == cxp.PERMISSION_MODE_DANGER_FULL_ACCESS


def test_permission_modes_constant_has_exactly_three_values():
    assert cxp.PERMISSION_MODES == ["read-only", "workspace-write", "danger-full-access"]


def test_send_forwards_tools_into_stdin_prompt(cli_present, monkeypatch):
    stdout = _jsonl({"text": "sem ferramentas usadas"})
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=stdout))
    monkeypatch.setattr(cxp.subprocess, "run", run_mock)

    provider = cxp.CodexCLIProvider(api_key=None)
    spec = ToolSpec(name="run_drc", description="d", parameters={"type": "object"})
    resp = provider.send([ChatMessage(role="user", content="x")], tools=[spec])

    assert resp.tool_calls == []
    assert resp.stop_reason == "end"
    cmd = run_mock.call_args[0][0]
    assert "run_drc" not in " ".join(cmd)  # never leaks into argv
    assert "run_drc" in run_mock.call_args.kwargs["input"]  # but is in stdin


def test_send_parses_action_block_into_tool_call(cli_present, monkeypatch):
    text = (
        "Vou mover o componente.\n\n"
        '```action\n{"name": "move_footprint", "arguments": {"reference": "R1", "x_mm": 5}}\n```'
    )
    stdout = _jsonl({"type": "task_started"}, {"text": text})
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=stdout))
    monkeypatch.setattr(cxp.subprocess, "run", run_mock)

    provider = cxp.CodexCLIProvider(api_key=None)
    spec = ToolSpec(name="move_footprint", description="move", parameters={"type": "object"})
    resp = provider.send([ChatMessage(role="user", content="move R1")], tools=[spec])

    assert resp.stop_reason == "tool_use"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "move_footprint"
    assert resp.tool_calls[0].arguments == {"reference": "R1", "x_mm": 5}
    assert "```action" not in resp.content
    assert "Vou mover" in resp.content


def test_send_plain_text_reply_has_no_tool_calls(cli_present, monkeypatch):
    stdout = _jsonl({"text": "Só uma resposta em texto."})
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=stdout))
    monkeypatch.setattr(cxp.subprocess, "run", run_mock)

    provider = cxp.CodexCLIProvider(api_key=None)
    resp = provider.send([ChatMessage(role="user", content="oi")])

    assert resp.stop_reason == "end"
    assert resp.tool_calls == []
    assert resp.content == "Só uma resposta em texto."


def test_send_uses_last_event_with_recognizable_text(cli_present, monkeypatch):
    # Newline-delimited event stream: earlier events without a plausible
    # text-bearing key must be ignored; the LAST one that has one wins.
    stdout = _jsonl(
        {"type": "task_started"},
        {"text": "resposta antiga"},
        {"type": "token_count"},
        {"message": "resposta final"},
    )
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=stdout))
    monkeypatch.setattr(cxp.subprocess, "run", run_mock)

    provider = cxp.CodexCLIProvider(api_key=None)
    resp = provider.send([ChatMessage(role="user", content="x")])
    assert resp.content == "resposta final"


def test_send_nonzero_returncode_raises(cli_present, monkeypatch):
    run_mock = mock.MagicMock(
        return_value=_fake_run(stdout="", stderr="algo correu mal", returncode=1)
    )
    monkeypatch.setattr(cxp.subprocess, "run", run_mock)
    provider = cxp.CodexCLIProvider(api_key=None)
    with pytest.raises(ProviderError) as excinfo:
        provider.send([ChatMessage(role="user", content="x")])
    assert "algo correu mal" in str(excinfo.value)


def test_send_no_valid_json_lines_raises(cli_present, monkeypatch):
    run_mock = mock.MagicMock(return_value=_fake_run(stdout="not json\nalso not json"))
    monkeypatch.setattr(cxp.subprocess, "run", run_mock)
    provider = cxp.CodexCLIProvider(api_key=None)
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="user", content="x")])


def test_send_event_error_field_raises(cli_present, monkeypatch):
    stdout = _jsonl({"type": "task_started"}, {"error": "quota excedida"})
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=stdout))
    monkeypatch.setattr(cxp.subprocess, "run", run_mock)
    provider = cxp.CodexCLIProvider(api_key=None)
    with pytest.raises(ProviderError) as excinfo:
        provider.send([ChatMessage(role="user", content="x")])
    assert "quota excedida" in str(excinfo.value)


def test_send_no_recognizable_text_field_raises(cli_present, monkeypatch):
    stdout = _jsonl({"type": "task_started"}, {"type": "token_count", "tokens": 42})
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=stdout))
    monkeypatch.setattr(cxp.subprocess, "run", run_mock)
    provider = cxp.CodexCLIProvider(api_key=None)
    with pytest.raises(ProviderError):
        provider.send([ChatMessage(role="user", content="x")])


def test_send_timeout_raises(cli_present, monkeypatch):
    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(cxp.subprocess, "run", _raise_timeout)
    provider = cxp.CodexCLIProvider(api_key=None)
    with pytest.raises(ProviderError) as excinfo:
        provider.send([ChatMessage(role="user", content="x")])
    assert "180" in str(excinfo.value)


def test_send_raw_payload_preserves_parsed_events(cli_present, monkeypatch):
    events = [{"type": "task_started"}, {"text": "ok"}]
    stdout = _jsonl(*events)
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=stdout))
    monkeypatch.setattr(cxp.subprocess, "run", run_mock)
    provider = cxp.CodexCLIProvider(api_key=None)
    resp = provider.send([ChatMessage(role="user", content="x")])
    assert resp.raw == events


def test_send_ignores_blank_lines_in_stdout(cli_present, monkeypatch):
    stdout = "\n".join([json.dumps({"type": "task_started"}), "", json.dumps({"text": "ok"}), ""])
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=stdout))
    monkeypatch.setattr(cxp.subprocess, "run", run_mock)
    provider = cxp.CodexCLIProvider(api_key=None)
    resp = provider.send([ChatMessage(role="user", content="x")])
    assert resp.content == "ok"
