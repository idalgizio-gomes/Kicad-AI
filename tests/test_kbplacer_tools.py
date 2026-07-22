"""Tests for actions/kbplacer_tools.py.

Run WITHOUT the real kbplacer plugin, WITHOUT KiCad's embedded Python, and
WITHOUT real pcbnew: `find_pcm_plugin_dir`, `_find_kicad_python`,
`subprocess.run`, and `pcbnew` are all faked via monkeypatch.
"""

from __future__ import annotations

import sys
import types

import pytest


def test_module_imports_without_sibling_or_pcbnew():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.kbplacer_tools", None)
    import actions.kbplacer_tools as kpt  # noqa: F401

    assert hasattr(kpt, "register_kbplacer_tools")
    assert hasattr(kpt, "place_keyboard_switches_and_diodes")


@pytest.fixture
def kpt(monkeypatch):
    sys.modules.pop("actions.kbplacer_tools", None)
    import actions.kbplacer_tools as module

    return module


@pytest.fixture
def fake_pcbnew(monkeypatch, kpt, tmp_path):
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text("x")
    board = types.SimpleNamespace(GetFileName=lambda: str(board_path))
    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)
    return kpt, fake_module, board, board_path


def _stub_environment(kpt, monkeypatch, tmp_path, run_result=None):
    plugin_dir = tmp_path / "kbplacer_plugin"
    plugin_dir.mkdir()
    monkeypatch.setattr(kpt, "find_pcm_plugin_dir", lambda identifier: plugin_dir)
    monkeypatch.setattr(kpt, "_find_kicad_python", lambda: "fake_python.exe")

    captured = {}

    def fake_run(argv, capture_output, text, timeout, creationflags=0):
        captured["argv"] = argv
        return run_result or types.SimpleNamespace(
            returncode=0, stdout="done", stderr=""
        )

    monkeypatch.setattr(kpt.subprocess, "run", fake_run)
    return captured


# --------------------------------------------------------------------------- #
# argument validation
# --------------------------------------------------------------------------- #
def test_missing_layout_path(kpt):
    with pytest.raises(RuntimeError):
        kpt.place_keyboard_switches_and_diodes({})


def test_board_not_saved_and_no_explicit_pcb_path(kpt, monkeypatch):
    board = types.SimpleNamespace(GetFileName=lambda: "")
    fake_module = types.SimpleNamespace(GetBoard=lambda: board)
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)

    with pytest.raises(RuntimeError):
        kpt.place_keyboard_switches_and_diodes({"layout_path": "layout.json"})


def test_explicit_pcb_path_does_not_need_board(kpt, monkeypatch, tmp_path):
    captured = _stub_environment(kpt, monkeypatch, tmp_path)
    sys.modules.pop("pcbnew", None)

    result = kpt.place_keyboard_switches_and_diodes(
        {"layout_path": "layout.json", "pcb_file_path": "explicit.kicad_pcb"}
    )
    assert "sucesso" in result
    assert "explicit.kicad_pcb" in captured["argv"]


def test_sibling_not_installed(kpt, fake_pcbnew, monkeypatch):
    def raise_not_found(identifier):
        raise kpt.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(kpt, "find_pcm_plugin_dir", raise_not_found)
    result = kpt.place_keyboard_switches_and_diodes({"layout_path": "layout.json"})
    assert "não está instalado" in result


def test_kicad_python_not_found(kpt, fake_pcbnew, monkeypatch, tmp_path):
    monkeypatch.setattr(kpt, "find_pcm_plugin_dir", lambda identifier: tmp_path)
    monkeypatch.setattr(kpt, "_find_kicad_python", lambda: None)

    with pytest.raises(RuntimeError):
        kpt.place_keyboard_switches_and_diodes({"layout_path": "layout.json"})


# --------------------------------------------------------------------------- #
# CLI argument construction (the core fidelity property of this tool)
# --------------------------------------------------------------------------- #
def test_default_uses_open_board_path(kpt, fake_pcbnew, monkeypatch, tmp_path):
    _kpt, _pcbnew_mod, _board, board_path = fake_pcbnew
    captured = _stub_environment(kpt, monkeypatch, tmp_path)

    kpt.place_keyboard_switches_and_diodes({"layout_path": "layout.json"})

    argv = captured["argv"]
    assert "--pcb-file" in argv
    assert str(board_path) in argv
    assert "--layout" in argv
    assert "layout.json" in argv


def test_boolean_and_string_flags_pass_through_raw(kpt, fake_pcbnew, monkeypatch, tmp_path):
    captured = _stub_environment(kpt, monkeypatch, tmp_path)

    kpt.place_keyboard_switches_and_diodes(
        {
            "layout_path": "layout.json",
            "route_switches_with_diodes": True,
            "switch": "SW{} 90 BACK",
            "diode": "D{} CUSTOM 5 -4.5 90 BACK",
            "additional_elements": "ST{} CUSTOM 0 0 180 BACK;LED{} RELATIVE",
            "key_distance": "19.05 19.05",
            "outline_delta": 1.5,
            "create_pcb_file": True,
            "start_index": 0,
            "add_stabilizers": False,
        }
    )

    argv = captured["argv"]
    assert "--route-switches-with-diodes" in argv
    assert "--switch" in argv and "SW{} 90 BACK" in argv
    assert "--diode" in argv and "D{} CUSTOM 5 -4.5 90 BACK" in argv
    assert (
        "--additional-elements" in argv
        and "ST{} CUSTOM 0 0 180 BACK;LED{} RELATIVE" in argv
    )
    assert "--key-distance" in argv and "19.05 19.05" in argv
    assert "--outline-delta" in argv and "1.5" in argv
    assert "--create-pcb-file" in argv
    assert "--start-index" in argv and "0" in argv
    assert "--no-stabilizers" in argv


def test_add_stabilizers_true_does_not_add_no_stabilizers_flag(
    kpt, fake_pcbnew, monkeypatch, tmp_path
):
    captured = _stub_environment(kpt, monkeypatch, tmp_path)

    kpt.place_keyboard_switches_and_diodes(
        {"layout_path": "layout.json", "add_stabilizers": True}
    )

    assert "--no-stabilizers" not in captured["argv"]


def test_invalid_outline_delta(kpt, fake_pcbnew, monkeypatch, tmp_path):
    _stub_environment(kpt, monkeypatch, tmp_path)
    with pytest.raises(RuntimeError):
        kpt.place_keyboard_switches_and_diodes(
            {"layout_path": "layout.json", "outline_delta": "not-a-number"}
        )


def test_invalid_start_index(kpt, fake_pcbnew, monkeypatch, tmp_path):
    _stub_environment(kpt, monkeypatch, tmp_path)
    with pytest.raises(RuntimeError):
        kpt.place_keyboard_switches_and_diodes(
            {"layout_path": "layout.json", "start_index": "not-an-int"}
        )


# --------------------------------------------------------------------------- #
# subprocess result handling
# --------------------------------------------------------------------------- #
def test_nonzero_exit_code_raises(kpt, fake_pcbnew, monkeypatch, tmp_path):
    _stub_environment(
        kpt,
        monkeypatch,
        tmp_path,
        run_result=types.SimpleNamespace(returncode=1, stdout="", stderr="bad layout"),
    )
    with pytest.raises(RuntimeError):
        kpt.place_keyboard_switches_and_diodes({"layout_path": "layout.json"})


def test_timeout_expired(kpt, fake_pcbnew, monkeypatch, tmp_path):
    plugin_dir = tmp_path / "kbplacer_plugin"
    plugin_dir.mkdir()
    monkeypatch.setattr(kpt, "find_pcm_plugin_dir", lambda identifier: plugin_dir)
    monkeypatch.setattr(kpt, "_find_kicad_python", lambda: "fake_python.exe")

    def fake_run(argv, capture_output, text, timeout, creationflags=0):
        import subprocess as real_subprocess

        raise real_subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(kpt.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError):
        kpt.place_keyboard_switches_and_diodes({"layout_path": "layout.json"})


def test_success_message_mentions_reload(kpt, fake_pcbnew, monkeypatch, tmp_path):
    _stub_environment(kpt, monkeypatch, tmp_path)
    result = kpt.place_keyboard_switches_and_diodes({"layout_path": "layout.json"})
    assert "Feche e reabra" in result


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_kbplacer_tools(kpt):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kpt.register_kbplacer_tools(registry)
    defn = registry.get("place_keyboard_switches_and_diodes")
    assert defn is not None
    assert defn.read_only is False
