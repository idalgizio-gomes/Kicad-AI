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
from llm_providers.base import ChatMessage, ChatResponse, ProviderError, ToolSpec


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


def test_send_ignores_tools_argument(cli_present, monkeypatch):
    """HONEST LIMITATION contract: `tools` is accepted (interface parity with
    every other provider) but never reaches the CLI invocation — this
    provider cannot return structured tool_use for the plugin's approval-gated
    tool loop (see module docstring)."""
    payload = {"is_error": False, "result": "sem ferramentas"}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)

    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    spec = ToolSpec(name="run_drc", description="d", parameters={"type": "object"})
    resp = provider.send([ChatMessage(role="user", content="x")], tools=[spec])

    assert resp.tool_calls == []
    assert resp.stop_reason == "end"
    cmd = run_mock.call_args[0][0]
    assert "run_drc" not in " ".join(cmd)


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


def test_send_raw_payload_preserved(cli_present, monkeypatch):
    payload = {"is_error": False, "result": "ok", "total_cost_usd": 0.01}
    run_mock = mock.MagicMock(return_value=_fake_run(stdout=json.dumps(payload)))
    monkeypatch.setattr(ccp.subprocess, "run", run_mock)
    provider = ccp.ClaudeCodeCLIProvider(api_key=None)
    resp = provider.send([ChatMessage(role="user", content="x")])
    assert resp.raw == payload
