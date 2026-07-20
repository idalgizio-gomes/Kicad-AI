"""Tests for actions/kikit_tools.py.

These never invoke a real kikit/KiCad install: `subprocess.run` is mocked
(same pattern as test_claude_code_cli_provider.py's `_fake_run`), and
`pcbnew` is faked via `sys.modules` monkeypatching only where needed (same
pattern as test_kicad_write_tools.py). The module itself must import
cleanly with no `pcbnew` present at all (asserted first).
"""

from __future__ import annotations

import subprocess
import sys
import types
from unittest import mock

import pytest


def test_module_imports_without_pcbnew():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.kikit_tools", None)
    import actions.kikit_tools as kt  # noqa: F401

    assert hasattr(kt, "register_kikit_tools")
    assert hasattr(kt, "panelize_board")
    assert hasattr(kt, "_find_kicad_python")


@pytest.fixture
def kikit_tools(monkeypatch):
    sys.modules.pop("actions.kikit_tools", None)
    import actions.kikit_tools as kt

    monkeypatch.setattr(kt, "_find_kicad_python", lambda: r"C:\Program Files\KiCad\10.0\bin\python.exe")
    return kt


def _fake_run(stdout="", stderr="", returncode=0):
    return mock.MagicMock(
        spec=subprocess.CompletedProcess,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
    )


# --------------------------------------------------------------------------- #
# _find_kicad_python()
# --------------------------------------------------------------------------- #
def test_find_kicad_python_prefers_newest_version(monkeypatch):
    sys.modules.pop("actions.kikit_tools", None)
    import actions.kikit_tools as kt

    def fake_isfile(path):
        return path == r"C:\Program Files\KiCad\9.0\bin\python.exe"

    monkeypatch.setattr(kt.os.path, "isfile", fake_isfile)
    assert kt._find_kicad_python() == r"C:\Program Files\KiCad\9.0\bin\python.exe"


def test_find_kicad_python_returns_none_when_nowhere_found(monkeypatch):
    sys.modules.pop("actions.kikit_tools", None)
    import actions.kikit_tools as kt

    monkeypatch.setattr(kt.os.path, "isfile", lambda path: False)
    assert kt._find_kicad_python() is None


# --------------------------------------------------------------------------- #
# panelize_board() — argument validation (no subprocess involved)
# --------------------------------------------------------------------------- #
def test_panelize_board_missing_output_path(kikit_tools, tmp_path):
    kt = kikit_tools
    input_file = tmp_path / "board.kicad_pcb"
    input_file.write_text("(kicad_pcb)", encoding="utf-8")

    with pytest.raises(RuntimeError):
        kt.panelize_board({"input_path": str(input_file), "rows": 2, "cols": 2})


def test_panelize_board_missing_input_file(kikit_tools, tmp_path):
    kt = kikit_tools
    output_file = tmp_path / "out.kicad_pcb"

    with pytest.raises(RuntimeError):
        kt.panelize_board(
            {
                "input_path": str(tmp_path / "does_not_exist.kicad_pcb"),
                "output_path": str(output_file),
                "rows": 2,
                "cols": 2,
            }
        )


def test_panelize_board_rejects_same_input_and_output(kikit_tools, tmp_path):
    kt = kikit_tools
    input_file = tmp_path / "board.kicad_pcb"
    input_file.write_text("(kicad_pcb)", encoding="utf-8")

    with pytest.raises(RuntimeError) as excinfo:
        kt.panelize_board(
            {
                "input_path": str(input_file),
                "output_path": str(input_file),
                "rows": 2,
                "cols": 2,
            }
        )
    assert "output_path" in str(excinfo.value)


def test_panelize_board_invalid_rows_cols(kikit_tools, tmp_path):
    kt = kikit_tools
    input_file = tmp_path / "board.kicad_pcb"
    input_file.write_text("(kicad_pcb)", encoding="utf-8")
    output_file = tmp_path / "out.kicad_pcb"

    with pytest.raises(RuntimeError):
        kt.panelize_board(
            {
                "input_path": str(input_file),
                "output_path": str(output_file),
                "rows": "not-a-number",
                "cols": 2,
            }
        )


def test_panelize_board_rows_below_one_rejected(kikit_tools, tmp_path):
    kt = kikit_tools
    input_file = tmp_path / "board.kicad_pcb"
    input_file.write_text("(kicad_pcb)", encoding="utf-8")
    output_file = tmp_path / "out.kicad_pcb"

    with pytest.raises(RuntimeError):
        kt.panelize_board(
            {
                "input_path": str(input_file),
                "output_path": str(output_file),
                "rows": 0,
                "cols": 2,
            }
        )


def test_panelize_board_invalid_cut_type(kikit_tools, tmp_path):
    kt = kikit_tools
    input_file = tmp_path / "board.kicad_pcb"
    input_file.write_text("(kicad_pcb)", encoding="utf-8")
    output_file = tmp_path / "out.kicad_pcb"

    with pytest.raises(RuntimeError):
        kt.panelize_board(
            {
                "input_path": str(input_file),
                "output_path": str(output_file),
                "rows": 2,
                "cols": 2,
                "cut_type": "not-a-real-cut-type",
            }
        )


def test_panelize_board_no_kicad_python_found(monkeypatch, tmp_path):
    sys.modules.pop("actions.kikit_tools", None)
    import actions.kikit_tools as kt

    monkeypatch.setattr(kt, "_find_kicad_python", lambda: None)

    input_file = tmp_path / "board.kicad_pcb"
    input_file.write_text("(kicad_pcb)", encoding="utf-8")
    output_file = tmp_path / "out.kicad_pcb"

    with pytest.raises(RuntimeError):
        kt.panelize_board(
            {
                "input_path": str(input_file),
                "output_path": str(output_file),
                "rows": 2,
                "cols": 2,
            }
        )


# --------------------------------------------------------------------------- #
# panelize_board() — default input_path via pcbnew.GetBoard()
# --------------------------------------------------------------------------- #
def test_panelize_board_defaults_input_path_from_open_board(monkeypatch, tmp_path):
    sys.modules.pop("actions.kikit_tools", None)
    import actions.kikit_tools as kt

    monkeypatch.setattr(kt, "_find_kicad_python", lambda: r"C:\Program Files\KiCad\10.0\bin\python.exe")

    input_file = tmp_path / "open_board.kicad_pcb"
    input_file.write_text("(kicad_pcb)", encoding="utf-8")
    output_file = tmp_path / "out.kicad_pcb"

    board = types.SimpleNamespace(GetFileName=lambda: str(input_file))
    fake_pcbnew = types.SimpleNamespace(GetBoard=lambda: board)
    monkeypatch.setitem(sys.modules, "pcbnew", fake_pcbnew)

    def fake_run(argv, **kwargs):
        output_file.write_text("(kicad_pcb panelized)", encoding="utf-8")
        return _fake_run(returncode=0)

    monkeypatch.setattr(kt.subprocess, "run", fake_run)

    result = kt.panelize_board({"output_path": str(output_file), "rows": 2, "cols": 3})
    assert str(input_file) in result or "painelizada" in result


def test_panelize_board_no_input_path_no_pcbnew(kikit_tools, tmp_path):
    sys.modules.pop("pcbnew", None)
    kt = kikit_tools
    output_file = tmp_path / "out.kicad_pcb"

    with pytest.raises(RuntimeError):
        kt.panelize_board({"output_path": str(output_file), "rows": 2, "cols": 2})


def test_panelize_board_no_input_path_no_open_board(monkeypatch, tmp_path):
    sys.modules.pop("actions.kikit_tools", None)
    import actions.kikit_tools as kt

    fake_pcbnew = types.SimpleNamespace(GetBoard=lambda: None)
    monkeypatch.setitem(sys.modules, "pcbnew", fake_pcbnew)

    output_file = tmp_path / "out.kicad_pcb"
    with pytest.raises(RuntimeError):
        kt.panelize_board({"output_path": str(output_file), "rows": 2, "cols": 2})


# --------------------------------------------------------------------------- #
# panelize_board() — subprocess invocation shape
# --------------------------------------------------------------------------- #
def test_panelize_board_invokes_expected_argv(kikit_tools, tmp_path):
    kt = kikit_tools
    input_file = tmp_path / "board.kicad_pcb"
    input_file.write_text("(kicad_pcb)", encoding="utf-8")
    output_file = tmp_path / "out.kicad_pcb"

    def fake_run(argv, **kwargs):
        output_file.write_text("(kicad_pcb panelized)", encoding="utf-8")
        return _fake_run(returncode=0)

    run_mock = mock.MagicMock(side_effect=fake_run)
    kt.subprocess.run = run_mock

    result = kt.panelize_board(
        {
            "input_path": str(input_file),
            "output_path": str(output_file),
            "rows": 3,
            "cols": 2,
            "h_space_mm": 1.5,
            "v_space_mm": 2.5,
            "cut_type": "vcuts",
        }
    )

    assert run_mock.call_count == 1
    args, kwargs = run_mock.call_args
    argv = args[0]

    assert argv[0] == r"C:\Program Files\KiCad\10.0\bin\python.exe"
    assert argv[1] == "-c"
    assert argv[2] == "from kikit.ui import cli; cli()"
    assert argv[3] == "panelize"
    assert "--preset" in argv
    assert argv[argv.index("--preset") + 1] == "default"
    assert "--layout" in argv
    layout_value = argv[argv.index("--layout") + 1]
    assert "grid" in layout_value
    assert "rows: 3" in layout_value
    assert "cols: 2" in layout_value
    assert "hspace: 1.5mm" in layout_value
    assert "vspace: 2.5mm" in layout_value
    assert "--cuts" in argv
    assert argv[argv.index("--cuts") + 1] == "vcuts"
    assert argv[-2] == str(input_file)
    assert argv[-1] == str(output_file)

    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("timeout") == kt._TIMEOUT_S
    assert "creationflags" in kwargs

    assert "painelizada" in result
    assert str(output_file) in result


def test_panelize_board_default_cut_type_and_spacing(kikit_tools, tmp_path):
    kt = kikit_tools
    input_file = tmp_path / "board.kicad_pcb"
    input_file.write_text("(kicad_pcb)", encoding="utf-8")
    output_file = tmp_path / "out.kicad_pcb"

    def fake_run(argv, **kwargs):
        output_file.write_text("(kicad_pcb panelized)", encoding="utf-8")
        return _fake_run(returncode=0)

    run_mock = mock.MagicMock(side_effect=fake_run)
    kt.subprocess.run = run_mock

    kt.panelize_board(
        {
            "input_path": str(input_file),
            "output_path": str(output_file),
            "rows": 1,
            "cols": 1,
        }
    )

    args, _kwargs = run_mock.call_args
    argv = args[0]
    assert argv[argv.index("--cuts") + 1] == "mousebites"
    layout_value = argv[argv.index("--layout") + 1]
    assert "hspace: 2.0mm" in layout_value
    assert "vspace: 2.0mm" in layout_value


# --------------------------------------------------------------------------- #
# panelize_board() — honest failure handling
# --------------------------------------------------------------------------- #
def test_panelize_board_kikit_not_installed(kikit_tools, tmp_path):
    kt = kikit_tools
    input_file = tmp_path / "board.kicad_pcb"
    input_file.write_text("(kicad_pcb)", encoding="utf-8")
    output_file = tmp_path / "out.kicad_pcb"

    kt.subprocess.run = mock.MagicMock(
        return_value=_fake_run(
            stderr="Traceback (most recent call last):\nModuleNotFoundError: No module named 'kikit'",
            returncode=1,
        )
    )

    with pytest.raises(RuntimeError) as excinfo:
        kt.panelize_board(
            {"input_path": str(input_file), "output_path": str(output_file), "rows": 2, "cols": 2}
        )
    message = str(excinfo.value)
    assert "pip install kikit" in message
    assert r"C:\Program Files\KiCad\10.0\bin\python.exe" in message


def test_panelize_board_nonzero_exit_surfaces_stderr(kikit_tools, tmp_path):
    kt = kikit_tools
    input_file = tmp_path / "board.kicad_pcb"
    input_file.write_text("(kicad_pcb)", encoding="utf-8")
    output_file = tmp_path / "out.kicad_pcb"

    kt.subprocess.run = mock.MagicMock(
        return_value=_fake_run(stderr="some kikit error detail", returncode=1)
    )

    with pytest.raises(RuntimeError) as excinfo:
        kt.panelize_board(
            {"input_path": str(input_file), "output_path": str(output_file), "rows": 2, "cols": 2}
        )
    assert "some kikit error detail" in str(excinfo.value)


def test_panelize_board_timeout(kikit_tools, tmp_path):
    kt = kikit_tools
    input_file = tmp_path / "board.kicad_pcb"
    input_file.write_text("(kicad_pcb)", encoding="utf-8")
    output_file = tmp_path / "out.kicad_pcb"

    def raise_timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kt._TIMEOUT_S)

    kt.subprocess.run = mock.MagicMock(side_effect=raise_timeout)

    with pytest.raises(RuntimeError) as excinfo:
        kt.panelize_board(
            {"input_path": str(input_file), "output_path": str(output_file), "rows": 2, "cols": 2}
        )
    assert str(kt._TIMEOUT_S) in str(excinfo.value)


def test_panelize_board_missing_output_file_after_success(kikit_tools, tmp_path):
    """Guard against a false 'success' claim: exit code 0 but no output file
    on disk must still be reported as an error, never a silent success."""
    kt = kikit_tools
    input_file = tmp_path / "board.kicad_pcb"
    input_file.write_text("(kicad_pcb)", encoding="utf-8")
    output_file = tmp_path / "out.kicad_pcb"  # never created by fake_run

    kt.subprocess.run = mock.MagicMock(return_value=_fake_run(returncode=0))

    with pytest.raises(RuntimeError):
        kt.panelize_board(
            {"input_path": str(input_file), "output_path": str(output_file), "rows": 2, "cols": 2}
        )


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_kikit_tools():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.kikit_tools", None)
    import actions.kikit_tools as kt
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kt.register_kikit_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert names == {"panelize_board"}

    defn = registry.get("panelize_board")
    assert defn.read_only is False
    with pytest.raises(RuntimeError):
        defn.handler({})


def test_register_kikit_tools_coexists_with_other_registries():
    sys.modules.pop("pcbnew", None)
    for mod in ("actions.kikit_tools", "actions.kicad_tools", "actions.kicad_write_tools"):
        sys.modules.pop(mod, None)
    import actions.kikit_tools as kt
    import actions.kicad_tools as ktools
    import actions.kicad_write_tools as kwt
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    ktools.register_kicad_tools(registry)
    kwt.register_kicad_write_tools(registry)
    kt.register_kikit_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert "panelize_board" in names
    # 5 (kicad_tools) + 10 (kicad_write_tools) + 1 (kikit_tools), all unique
    assert len(names) == 5 + 10 + 1
