"""Tests for actions/kicad_write_tools.py.

These run WITHOUT a real KiCad installation: `pcbnew` is faked via
`sys.modules` monkeypatching, mirroring the exact pattern already used in
test_kicad_tools.py. The module itself must import cleanly with no
`pcbnew` present at all (that's asserted first).
"""

from __future__ import annotations

import sys
import types

import pytest


def test_module_imports_without_pcbnew():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.kicad_write_tools", None)
    import actions.kicad_write_tools as kwt  # noqa: F401

    assert hasattr(kwt, "register_kicad_write_tools")
    assert hasattr(kwt, "move_footprint")
    assert hasattr(kwt, "rotate_footprint")
    assert hasattr(kwt, "set_footprint_value")


class _FakeVector2I:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _FakePosition:
    def __init__(self, x, y):
        self.x = x
        self.y = y


def _make_fake_footprint(reference, value="10k", x_mm=1.0, y_mm=2.0, angle=0.0):
    fp = types.SimpleNamespace()
    fp._pos = _FakePosition(x_mm * 1e6, y_mm * 1e6)  # pretend nm-per-mm scale
    fp._value = value
    fp._angle = angle
    fp.GetReference = lambda: reference
    fp.GetPosition = lambda: fp._pos
    fp.SetPosition = lambda pos: setattr(fp, "_pos", pos)
    fp.GetOrientationDegrees = lambda: fp._angle
    fp.SetOrientationDegrees = lambda a: setattr(fp, "_angle", a)
    fp.GetValue = lambda: fp._value
    fp.SetValue = lambda v: setattr(fp, "_value", v)
    return fp


@pytest.fixture
def fake_pcbnew(monkeypatch):
    footprints = {"R1": _make_fake_footprint("R1", "10k", 1.0, 2.0)}
    board = types.SimpleNamespace()
    board.FindFootprintByReference = lambda ref: footprints.get(ref)

    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board
    # Simple, self-consistent unit conversion for the test: 1 mm = 1e6
    # internal units (the real scale is nm, irrelevant here — only that
    # ToMM/FromMM are exact inverses matters for these tests).
    fake_module.ToMM = lambda v: v / 1e6
    fake_module.FromMM = lambda v: v * 1e6
    fake_module.VECTOR2I = _FakeVector2I
    fake_module.Refresh = lambda: None

    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)

    sys.modules.pop("actions.kicad_write_tools", None)
    import actions.kicad_write_tools as kwt

    return kwt, fake_module, board, footprints


# --------------------------------------------------------------------------- #
# move_footprint
# --------------------------------------------------------------------------- #
def test_move_footprint_success(fake_pcbnew):
    kwt, _fake_module, _board, footprints = fake_pcbnew
    result = kwt.move_footprint({"reference": "R1", "x_mm": 5.0, "y_mm": 6.5})
    assert "R1" in result
    assert "5.000" in result and "6.500" in result
    fp = footprints["R1"]
    assert fp.GetPosition().x == pytest.approx(5.0 * 1e6)
    assert fp.GetPosition().y == pytest.approx(6.5 * 1e6)


def test_move_footprint_missing_reference(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.move_footprint({"x_mm": 1.0, "y_mm": 1.0})


def test_move_footprint_unknown_reference(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError) as excinfo:
        kwt.move_footprint({"reference": "R99", "x_mm": 1.0, "y_mm": 1.0})
    assert "R99" in str(excinfo.value)


def test_move_footprint_invalid_coordinates(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.move_footprint({"reference": "R1", "x_mm": "not-a-number", "y_mm": 1.0})


def test_move_footprint_no_board(monkeypatch):
    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: None
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)
    sys.modules.pop("actions.kicad_write_tools", None)
    import actions.kicad_write_tools as kwt

    with pytest.raises(RuntimeError):
        kwt.move_footprint({"reference": "R1", "x_mm": 1.0, "y_mm": 1.0})


# --------------------------------------------------------------------------- #
# rotate_footprint
# --------------------------------------------------------------------------- #
def test_rotate_footprint_success(fake_pcbnew):
    kwt, _fake_module, _board, footprints = fake_pcbnew
    result = kwt.rotate_footprint({"reference": "R1", "angle_deg": 90.0})
    assert "R1" in result
    assert "90.0" in result
    assert footprints["R1"].GetOrientationDegrees() == 90.0


def test_rotate_footprint_invalid_angle(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.rotate_footprint({"reference": "R1", "angle_deg": "not-a-number"})


def test_rotate_footprint_unknown_reference(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.rotate_footprint({"reference": "R99", "angle_deg": 45.0})


# --------------------------------------------------------------------------- #
# set_footprint_value
# --------------------------------------------------------------------------- #
def test_set_footprint_value_success(fake_pcbnew):
    kwt, _fake_module, _board, footprints = fake_pcbnew
    result = kwt.set_footprint_value({"reference": "R1", "value": "22k"})
    assert "10k" in result and "22k" in result
    assert footprints["R1"].GetValue() == "22k"


def test_set_footprint_value_missing_value(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.set_footprint_value({"reference": "R1"})


def test_set_footprint_value_empty_string_rejected(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.set_footprint_value({"reference": "R1", "value": ""})


def test_set_footprint_value_unknown_reference(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.set_footprint_value({"reference": "R99", "value": "1k"})


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_kicad_write_tools():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.kicad_write_tools", None)
    import actions.kicad_write_tools as kwt
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kwt.register_kicad_write_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert names == {"move_footprint", "rotate_footprint", "set_footprint_value"}

    # Every write tool must be flagged read_only=False - this is the flag
    # the approval dialog (chat_gui.py) uses to show the stronger warning.
    for name in names:
        defn = registry.get(name)
        assert defn.read_only is False
        with pytest.raises(RuntimeError):
            defn.handler({})


def test_register_kicad_write_tools_coexists_with_read_only_tools():
    """Both registries can be populated together (as chat_action.py does)
    without name collisions or one clobbering the other."""
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.kicad_write_tools", None)
    sys.modules.pop("actions.kicad_tools", None)
    import actions.kicad_write_tools as kwt
    import actions.kicad_tools as kt
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kt.register_kicad_tools(registry)
    kwt.register_kicad_write_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert names == {
        "get_project_info",
        "list_components",
        "run_drc",
        "run_erc",
        "move_footprint",
        "rotate_footprint",
        "set_footprint_value",
    }
    read_only_names = {n for n in names if registry.get(n).read_only}
    write_names = {n for n in names if not registry.get(n).read_only}
    assert read_only_names == {"get_project_info", "list_components", "run_drc", "run_erc"}
    assert write_names == {"move_footprint", "rotate_footprint", "set_footprint_value"}
