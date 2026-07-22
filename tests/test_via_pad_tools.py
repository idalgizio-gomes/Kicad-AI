"""Tests for actions/via_pad_tools.py.

Run WITHOUT real sibling plugins and WITHOUT real pcbnew: `find_pcm_plugin_dir`
and `pcbnew` are faked via monkeypatch/`sys.modules`, mirroring
test_kicad_parasitics_tools.py's style.
"""

from __future__ import annotations

import sys
import types

import pytest


def test_module_imports_without_siblings_or_pcbnew():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.via_pad_tools", None)
    import actions.via_pad_tools as vpt  # noqa: F401

    assert hasattr(vpt, "register_via_pad_tools")
    assert hasattr(vpt, "replace_via_with_thermal_relief_pad")
    assert hasattr(vpt, "set_all_pad_hole_diameters")


@pytest.fixture
def vpt(monkeypatch):
    sys.modules.pop("actions.via_pad_tools", None)
    import actions.via_pad_tools as module

    return module


def test_find_pcm_plugin_dir_identifiers(vpt, monkeypatch):
    seen = []

    def fake_find(identifier):
        seen.append(identifier)
        raise vpt.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(vpt, "find_pcm_plugin_dir", fake_find)

    with pytest.raises(vpt.SiblingPluginNotFoundError):
        vpt._thermal_relief_installed()
    with pytest.raises(vpt.SiblingPluginNotFoundError):
        vpt._load_set_hole_diameter()

    assert seen == [
        "com_github_JohnHryb_ThermalReliefVia",
        "com_github_seigedigital_setholediameterpluginforkicad",
    ]


# --------------------------------------------------------------------------- #
# fake pcbnew plumbing
# --------------------------------------------------------------------------- #
class _FakeVector2I:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _FakeUuid:
    def __init__(self, s):
        self._s = s

    def AsString(self):
        return self._s


class _FakePad:
    def __init__(self):
        self.shape = None
        self.size = None
        self.drill = None
        self.attribute = None
        self.net_code = None

    def SetShape(self, shape):
        self.shape = shape

    def SetSize(self, size):
        self.size = size

    def SetDrillSize(self, size):
        self.drill = size

    def SetAttribute(self, attr):
        self.attribute = attr

    def SetNetCode(self, net):
        self.net_code = net


class _FakeFootprint:
    def __init__(self, board=None):
        self.board = board
        self.position = None
        self.pads = []
        self._pad_count = 0

    def SetPosition(self, pos):
        self.position = pos

    def Add(self, pad):
        self.pads.append(pad)

    def GetPadCount(self):
        return self._pad_count


class _FakeVia:
    def __init__(self, uuid_str, position=(1, 2), drill=300000, width=600000, net=5):
        self.m_Uuid = _FakeUuid(uuid_str)
        self._position = position
        self._drill = drill
        self._width = width
        self._net = net

    def GetClass(self):
        return "PCB_VIA"

    def GetPosition(self):
        return self._position

    def GetDrillValue(self):
        return self._drill

    def GetWidth(self):
        return self._width

    def GetNetCode(self):
        return self._net


class _FakeBoard:
    def __init__(self, tracks=None, footprints=None):
        self._tracks = tracks or []
        self._footprints = footprints or []
        self.added = []
        self.removed = []

    def GetTracks(self):
        return self._tracks

    def GetFootprints(self):
        return self._footprints

    def Add(self, item):
        self.added.append(item)

    def Remove(self, item):
        self.removed.append(item)


@pytest.fixture
def fake_pcbnew(monkeypatch, vpt):
    board = _FakeBoard()
    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board
    fake_module.FOOTPRINT = lambda parent: _FakeFootprint(parent)
    fake_module.PAD = lambda footprint: _FakePad()
    fake_module.VECTOR2I = lambda x, y: _FakeVector2I(x, y)
    fake_module.PAD_SHAPE_CIRCLE = "circle"
    fake_module.PAD_ATTRIB_PTH = "pth"
    fake_module.Refresh = lambda: None
    fake_module.ToMM = lambda nm: nm / 1_000_000

    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)
    return vpt, fake_module, board


# --------------------------------------------------------------------------- #
# replace_via_with_thermal_relief_pad
# --------------------------------------------------------------------------- #
def test_replace_via_missing_uuid(vpt):
    with pytest.raises(RuntimeError):
        vpt.replace_via_with_thermal_relief_pad({})


def test_replace_via_sibling_not_installed(vpt, fake_pcbnew, monkeypatch):
    def raise_not_found():
        raise vpt.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(vpt, "_thermal_relief_installed", raise_not_found)
    result = vpt.replace_via_with_thermal_relief_pad({"uuid": "abc"})
    assert "não está instalado" in result


def test_replace_via_not_found(vpt, fake_pcbnew, monkeypatch):
    monkeypatch.setattr(vpt, "_thermal_relief_installed", lambda: None)
    with pytest.raises(RuntimeError):
        vpt.replace_via_with_thermal_relief_pad({"uuid": "missing"})


def test_replace_via_happy_path(vpt, fake_pcbnew, monkeypatch):
    _vpt, pcbnew_mod, board = fake_pcbnew
    via = _FakeVia("via-1")
    board._tracks = [via]
    monkeypatch.setattr(vpt, "_thermal_relief_installed", lambda: None)

    result = vpt.replace_via_with_thermal_relief_pad({"uuid": "via-1"})

    assert "via-1" in result
    assert len(board.added) == 1
    assert board.removed == [via]
    fp = board.added[0]
    assert fp.pads[0].net_code == 5


# --------------------------------------------------------------------------- #
# set_all_pad_hole_diameters
# --------------------------------------------------------------------------- #
def test_set_hole_diameter_missing_arg(vpt):
    with pytest.raises(RuntimeError):
        vpt.set_all_pad_hole_diameters({})


def test_set_hole_diameter_non_positive(vpt):
    with pytest.raises(RuntimeError):
        vpt.set_all_pad_hole_diameters({"diameter_mm": 0})


def test_set_hole_diameter_sibling_not_installed(vpt, fake_pcbnew, monkeypatch):
    def raise_not_found():
        raise vpt.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(vpt, "_load_set_hole_diameter", raise_not_found)
    result = vpt.set_all_pad_hole_diameters({"diameter_mm": 0.8})
    assert "não está instalado" in result


def test_set_hole_diameter_happy_path(vpt, fake_pcbnew, monkeypatch):
    _vpt, pcbnew_mod, board = fake_pcbnew
    fp = _FakeFootprint()
    fp._pad_count = 3
    board._footprints = [fp]

    calls = []

    def fake_set_hole_diameter(pcb, diameter):
        calls.append((pcb, diameter))

    module = types.SimpleNamespace(set_hole_diameter=fake_set_hole_diameter)
    monkeypatch.setattr(vpt, "_load_set_hole_diameter", lambda: module)

    result = vpt.set_all_pad_hole_diameters({"diameter_mm": 0.8})

    assert calls == [(board, 0.8)]
    assert "0.800" in result
    assert "3" in result


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_via_pad_tools(vpt):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    vpt.register_via_pad_tools(registry)

    thermal = registry.get("replace_via_with_thermal_relief_pad")
    assert thermal is not None
    assert thermal.read_only is False

    hole = registry.get("set_all_pad_hole_diameters")
    assert hole is not None
    assert hole.read_only is False
