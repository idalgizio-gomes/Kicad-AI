"""Tests for actions/freerouting_tools.py.

Run WITHOUT the real Freerouting plugin, WITHOUT real Java, and WITHOUT
real pcbnew: `find_pcm_plugin_dir`, `shutil.which`/`subprocess.run`, and
`pcbnew` are faked via monkeypatch/`sys.modules`.
"""

from __future__ import annotations

import sys
import types

import pytest


def test_module_imports_without_sibling_or_pcbnew():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.freerouting_tools", None)
    import actions.freerouting_tools as ft  # noqa: F401

    assert hasattr(ft, "register_freerouting_tools")
    assert hasattr(ft, "run_freerouting_autoroute")


@pytest.fixture
def ft(monkeypatch):
    sys.modules.pop("actions.freerouting_tools", None)
    import actions.freerouting_tools as module

    return module


@pytest.fixture
def fake_pcbnew(monkeypatch, ft, tmp_path):
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text("x")
    board = types.SimpleNamespace(GetFileName=lambda: str(board_path))
    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board
    fake_module.ExportSpecctraDSN = lambda b, path: True
    fake_module.ImportSpecctraSES = lambda b, path: True
    fake_module.Refresh = lambda: None
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)
    return ft, fake_module, board, tmp_path


def _make_plugin_dir(tmp_path, jar_relative="jar/freerouting-2.2.4.jar"):
    plugin_dir = tmp_path / "freerouting_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "jar").mkdir()
    (plugin_dir / jar_relative).write_text("fake jar")
    (plugin_dir / "plugin.ini").write_text(
        f"[java]\npath = java\n\n[artifact]\nlocation = {jar_relative}\n"
    )
    return plugin_dir


# --------------------------------------------------------------------------- #
# argument validation
# --------------------------------------------------------------------------- #
def test_invalid_timeout(ft, fake_pcbnew):
    with pytest.raises(RuntimeError):
        ft.run_freerouting_autoroute({"timeout_seconds": 0})


def test_board_not_saved(ft, monkeypatch):
    board = types.SimpleNamespace(GetFileName=lambda: "")
    fake_module = types.SimpleNamespace(GetBoard=lambda: board)
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)

    with pytest.raises(RuntimeError):
        ft.run_freerouting_autoroute({})


def test_sibling_not_installed(ft, fake_pcbnew, monkeypatch):
    def raise_not_found(identifier):
        raise ft.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(ft, "find_pcm_plugin_dir", raise_not_found)
    result = ft.run_freerouting_autoroute({})
    assert "não está instalado" in result


def test_missing_jar_raises(ft, fake_pcbnew, monkeypatch, tmp_path):
    plugin_dir = tmp_path / "freerouting_plugin_missing"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.ini").write_text(
        "[artifact]\nlocation = jar/does-not-exist.jar\n"
    )
    monkeypatch.setattr(ft, "find_pcm_plugin_dir", lambda identifier: plugin_dir)

    with pytest.raises(RuntimeError):
        ft.run_freerouting_autoroute({})


def test_java_not_found(ft, fake_pcbnew, monkeypatch, tmp_path):
    plugin_dir = _make_plugin_dir(tmp_path)
    monkeypatch.setattr(ft, "find_pcm_plugin_dir", lambda identifier: plugin_dir)
    monkeypatch.setattr(ft, "_find_suitable_java", lambda: None)

    with pytest.raises(RuntimeError):
        ft.run_freerouting_autoroute({})


# --------------------------------------------------------------------------- #
# happy path / subprocess behavior
# --------------------------------------------------------------------------- #
def test_happy_path(ft, fake_pcbnew, monkeypatch, tmp_path):
    _ft, pcbnew_mod, board, tmp = fake_pcbnew
    plugin_dir = _make_plugin_dir(tmp_path)
    monkeypatch.setattr(ft, "find_pcm_plugin_dir", lambda identifier: plugin_dir)
    monkeypatch.setattr(ft, "_find_suitable_java", lambda: "java")

    def fake_export(board_arg, path):
        with open(path, "w") as f:
            f.write("dsn")
        return True

    def fake_run(command, capture_output, text, timeout):
        # Simulate freerouting writing the .ses output file (-do arg).
        do_index = command.index("-do")
        ses_path = command[do_index + 1]
        with open(ses_path, "w") as f:
            f.write("ses")
        return types.SimpleNamespace(returncode=0, stdout="done", stderr="")

    pcbnew_mod.ExportSpecctraDSN = fake_export
    monkeypatch.setattr(ft.subprocess, "run", fake_run)

    result = ft.run_freerouting_autoroute({})
    assert "concluído" in result


def test_export_dsn_failure(ft, fake_pcbnew, monkeypatch, tmp_path):
    _ft, pcbnew_mod, board, tmp = fake_pcbnew
    plugin_dir = _make_plugin_dir(tmp_path)
    monkeypatch.setattr(ft, "find_pcm_plugin_dir", lambda identifier: plugin_dir)
    monkeypatch.setattr(ft, "_find_suitable_java", lambda: "java")
    pcbnew_mod.ExportSpecctraDSN = lambda b, path: False

    with pytest.raises(RuntimeError):
        ft.run_freerouting_autoroute({})


def test_timeout_expired(ft, fake_pcbnew, monkeypatch, tmp_path):
    _ft, pcbnew_mod, board, tmp = fake_pcbnew
    plugin_dir = _make_plugin_dir(tmp_path)
    monkeypatch.setattr(ft, "find_pcm_plugin_dir", lambda identifier: plugin_dir)
    monkeypatch.setattr(ft, "_find_suitable_java", lambda: "java")

    def fake_export(board_arg, path):
        with open(path, "w") as f:
            f.write("dsn")
        return True

    def fake_run(command, capture_output, text, timeout):
        import subprocess as real_subprocess

        raise real_subprocess.TimeoutExpired(cmd=command, timeout=timeout)

    pcbnew_mod.ExportSpecctraDSN = fake_export
    monkeypatch.setattr(ft.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError):
        ft.run_freerouting_autoroute({})


def test_missing_ses_output_raises(ft, fake_pcbnew, monkeypatch, tmp_path):
    _ft, pcbnew_mod, board, tmp = fake_pcbnew
    plugin_dir = _make_plugin_dir(tmp_path)
    monkeypatch.setattr(ft, "find_pcm_plugin_dir", lambda identifier: plugin_dir)
    monkeypatch.setattr(ft, "_find_suitable_java", lambda: "java")

    def fake_export(board_arg, path):
        with open(path, "w") as f:
            f.write("dsn")
        return True

    def fake_run(command, capture_output, text, timeout):
        # Deliberately does NOT create the .ses file.
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")

    pcbnew_mod.ExportSpecctraDSN = fake_export
    monkeypatch.setattr(ft.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError):
        ft.run_freerouting_autoroute({})


def test_import_ses_failure(ft, fake_pcbnew, monkeypatch, tmp_path):
    _ft, pcbnew_mod, board, tmp = fake_pcbnew
    plugin_dir = _make_plugin_dir(tmp_path)
    monkeypatch.setattr(ft, "find_pcm_plugin_dir", lambda identifier: plugin_dir)
    monkeypatch.setattr(ft, "_find_suitable_java", lambda: "java")

    def fake_export(board_arg, path):
        with open(path, "w") as f:
            f.write("dsn")
        return True

    def fake_run(command, capture_output, text, timeout):
        do_index = command.index("-do")
        ses_path = command[do_index + 1]
        with open(ses_path, "w") as f:
            f.write("ses")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    pcbnew_mod.ExportSpecctraDSN = fake_export
    pcbnew_mod.ImportSpecctraSES = lambda b, path: False
    monkeypatch.setattr(ft.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError):
        ft.run_freerouting_autoroute({})


# --------------------------------------------------------------------------- #
# _find_suitable_java
# --------------------------------------------------------------------------- #
def test_find_suitable_java_none_when_not_on_path(ft, monkeypatch):
    monkeypatch.setattr(ft.shutil, "which", lambda name: None)
    assert ft._find_suitable_java() is None


def test_find_suitable_java_none_when_too_old(ft, monkeypatch):
    monkeypatch.setattr(ft.shutil, "which", lambda name: "/usr/bin/java")
    monkeypatch.setattr(ft, "_get_java_version", lambda path: "17")
    assert ft._find_suitable_java() is None


def test_find_suitable_java_found(ft, monkeypatch):
    monkeypatch.setattr(ft.shutil, "which", lambda name: "/usr/bin/java")
    monkeypatch.setattr(ft, "_get_java_version", lambda path: "25")
    assert ft._find_suitable_java() == "/usr/bin/java"


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_freerouting_tools(ft):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    ft.register_freerouting_tools(registry)
    defn = registry.get("run_freerouting_autoroute")
    assert defn is not None
    assert defn.read_only is False
