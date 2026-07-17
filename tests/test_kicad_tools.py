"""Tests for actions/kicad_tools.py.

These run WITHOUT a real KiCad installation: `pcbnew` is faked via
`sys.modules` monkeypatching. The module itself must import cleanly with no
`pcbnew` present at all (that's asserted first).
"""

from __future__ import annotations

import sys
import types

import pytest


def test_module_imports_without_pcbnew():
    # pcbnew must not be required merely to import the module.
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.kicad_tools", None)
    import actions.kicad_tools as kt  # noqa: F401

    assert hasattr(kt, "register_kicad_tools")
    assert hasattr(kt, "get_project_info")
    assert hasattr(kt, "list_components")
    assert hasattr(kt, "run_drc")
    assert hasattr(kt, "run_erc")


def _make_fake_footprint(reference, value, fp_id="Resistor_SMD:R_0603", layer="F.Cu"):
    fp = types.SimpleNamespace()
    fp.GetReference = lambda: reference
    fp.GetValue = lambda: value
    fp.GetFPIDAsString = lambda: fp_id
    fp.GetLayerName = lambda: layer
    return fp


def _make_fake_board(footprints, file_name="C:/proj/board.kicad_pcb"):
    board = types.SimpleNamespace()
    board.GetFileName = lambda: file_name
    board.GetFootprints = lambda: list(footprints)
    board.GetNetCount = lambda: 42
    board.GetCopperLayerCount = lambda: 4
    return board


@pytest.fixture
def fake_pcbnew(monkeypatch):
    footprints = [
        _make_fake_footprint("R1", "10k"),
        _make_fake_footprint("C1", "100nF"),
        _make_fake_footprint("U1", "ATmega328"),
    ]
    board = _make_fake_board(footprints)

    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board
    fake_module.EDA_UNITS_MM = 1
    fake_module.WriteDRCReport = None  # overridden per-test when needed

    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)

    sys.modules.pop("actions.kicad_tools", None)
    import actions.kicad_tools as kt

    return kt, fake_module, board


def test_get_project_info(fake_pcbnew):
    kt, _fake_module, _board = fake_pcbnew
    result = kt.get_project_info({})
    assert "board.kicad_pcb" in result
    assert "Footprints: 3" in result
    assert "Nets: 42" in result
    assert "Copper layers: 4" in result


def test_list_components_no_filter(fake_pcbnew):
    kt, _fake_module, _board = fake_pcbnew
    result = kt.list_components({})
    assert "R1" in result
    assert "C1" in result
    assert "U1" in result


def test_list_components_with_filter(fake_pcbnew):
    kt, _fake_module, _board = fake_pcbnew
    result = kt.list_components({"filter": "10k"})
    assert "R1" in result
    assert "C1" not in result
    assert "U1" not in result


def test_list_components_truncation(monkeypatch):
    footprints = [_make_fake_footprint(f"R{i}", "10k") for i in range(250)]
    board = _make_fake_board(footprints)

    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)

    sys.modules.pop("actions.kicad_tools", None)
    import actions.kicad_tools as kt

    result = kt.list_components({})
    assert "truncado" in result
    # Only the first 200 rows + header + truncation note should be present.
    line_count = result.count("\n") + 1
    assert line_count <= 202


def test_get_project_info_no_board(monkeypatch):
    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: None
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)

    sys.modules.pop("actions.kicad_tools", None)
    import actions.kicad_tools as kt

    with pytest.raises(RuntimeError):
        kt.get_project_info({})


def test_run_drc_missing_pcbnew_function(fake_pcbnew):
    kt, fake_module, _board = fake_pcbnew
    # Remove WriteDRCReport entirely to simulate an older/newer KiCad API.
    del fake_module.WriteDRCReport
    result = kt.run_drc({})
    assert "não está disponível" in result or "não disponível" in result.lower()


def test_run_drc_writes_and_reads_report(fake_pcbnew, tmp_path):
    kt, fake_module, _board = fake_pcbnew

    def fake_write_drc(board, path, units, flag):
        with open(path, "w", encoding="utf-8") as f:
            f.write("** Drc report **\nNo errors found\n")

    fake_module.WriteDRCReport = fake_write_drc
    result = kt.run_drc({})
    assert "No errors found" in result


def test_run_erc_no_kicad_cli(fake_pcbnew, monkeypatch, tmp_path):
    kt, _fake_module, board = fake_pcbnew

    sch_path = tmp_path / "board.kicad_sch"
    sch_path.write_text("(kicad_sch)", encoding="utf-8")
    pcb_path = tmp_path / "board.kicad_pcb"
    board.GetFileName = lambda: str(pcb_path)

    monkeypatch.setattr(kt, "_find_kicad_cli", lambda: None)

    result = kt.run_erc({})
    assert "kicad-cli não encontrado" in result


def test_run_erc_missing_schematic(fake_pcbnew, tmp_path):
    kt, _fake_module, board = fake_pcbnew
    pcb_path = tmp_path / "no_sch_here.kicad_pcb"
    board.GetFileName = lambda: str(pcb_path)

    result = kt.run_erc({})
    assert "não encontrado" in result


def test_register_kicad_tools():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.kicad_tools", None)
    import actions.kicad_tools as kt
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kt.register_kicad_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert names == {"get_project_info", "list_components", "run_drc", "run_erc"}

    # Each registered handler should raise the graceful RuntimeError when
    # pcbnew is unavailable, rather than an ImportError/AttributeError.
    for name in names:
        defn = registry.get(name)
        with pytest.raises(RuntimeError):
            defn.handler({})
