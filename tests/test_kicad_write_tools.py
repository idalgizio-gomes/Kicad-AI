"""Tests for actions/kicad_write_tools.py.

These run WITHOUT a real KiCad installation: `pcbnew` is faked via
`sys.modules` monkeypatching, mirroring the exact pattern already used in
test_kicad_tools.py. The module itself must import cleanly with no
`pcbnew` present at all (that's asserted first).
"""

from __future__ import annotations

import itertools
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
    assert hasattr(kwt, "add_footprint")
    assert hasattr(kwt, "delete_footprint")
    assert hasattr(kwt, "add_track")
    assert hasattr(kwt, "delete_track")
    assert hasattr(kwt, "add_via")
    assert hasattr(kwt, "set_pad_net")
    assert hasattr(kwt, "create_board_from_scratch")


class _FakeVector2I:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _FakePosition:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _FakeUuid:
    def __init__(self, s):
        self._s = s

    def AsString(self):
        return self._s


class _FakePad:
    def __init__(self, number):
        self._number = number
        self._net = None

    def GetNumber(self):
        return self._number

    def SetNumber(self, n):
        self._number = n

    def SetNet(self, net):
        self._net = net

    def GetNet(self):
        return self._net


def _make_fake_footprint(reference, value="10k", x_mm=1.0, y_mm=2.0, angle=0.0, pads=None):
    fp = types.SimpleNamespace()
    fp._kind = "footprint"
    fp._pos = _FakePosition(x_mm * 1e6, y_mm * 1e6)  # pretend nm-per-mm scale
    fp._value = value
    fp._angle = angle
    fp._reference = reference
    fp._pads = {p._number: p for p in (pads or [])}
    fp.GetReference = lambda: fp._reference
    fp.SetReference = lambda r: setattr(fp, "_reference", r)
    fp.GetPosition = lambda: fp._pos
    fp.SetPosition = lambda pos: setattr(fp, "_pos", pos)
    fp.GetOrientationDegrees = lambda: fp._angle
    fp.SetOrientationDegrees = lambda a: setattr(fp, "_angle", a)
    fp.GetValue = lambda: fp._value
    fp.SetValue = lambda v: setattr(fp, "_value", v)
    fp.FindPadByNumber = lambda num: fp._pads.get(num)
    return fp


@pytest.fixture
def fake_pcbnew(monkeypatch):
    footprints = {"R1": _make_fake_footprint("R1", "10k", 1.0, 2.0, pads=[_FakePad("1"), _FakePad("2")])}
    added_footprints = []
    tracks = []
    nets = {}
    net_codes = itertools.count(1)
    uuid_counter = itertools.count(1)
    layers = {"F.Cu": 0, "B.Cu": 2, "In1.Cu": 1, "Edge.Cuts": 44}

    def make_uuid():
        return _FakeUuid(f"uuid-{next(uuid_counter)}")

    def find_footprint_by_reference(ref):
        if ref in footprints:
            return footprints[ref]
        for fp in added_footprints:
            if fp.GetReference() == ref:
                return fp
        return None

    def board_add(item):
        kind = getattr(item, "_kind", None)
        if kind == "footprint":
            added_footprints.append(item)
        elif kind in ("track", "via"):
            tracks.append(item)
        elif kind == "net":
            nets[item._name] = item

    def board_remove(item):
        kind = getattr(item, "_kind", None)
        if kind == "footprint":
            if item in added_footprints:
                added_footprints.remove(item)
            else:
                for ref, fp in list(footprints.items()):
                    if fp is item:
                        del footprints[ref]
        elif kind in ("track", "via"):
            if item in tracks:
                tracks.remove(item)

    board = types.SimpleNamespace()
    board.FindFootprintByReference = find_footprint_by_reference
    board.Add = board_add
    board.Remove = board_remove
    board.GetTracks = lambda: list(tracks)
    board.FindNet = lambda name: nets.get(name)
    board.GetLayerID = lambda name: layers.get(name, -1)
    board.GetLayerName = lambda lid: next(
        (n for n, v in layers.items() if v == lid), "?"
    )

    class _FakeTrack:
        _kind = "track"

        def __init__(self, _board):
            self.m_Uuid = make_uuid()
            self._start = None
            self._end = None
            self._width = None
            self._layer = None
            self._net = None

        def SetStart(self, pos):
            self._start = pos

        def GetStart(self):
            return self._start

        def SetEnd(self, pos):
            self._end = pos

        def GetEnd(self):
            return self._end

        def SetWidth(self, w):
            self._width = w

        def SetLayer(self, layer_id):
            self._layer = layer_id

        def GetLayer(self):
            return self._layer

        def SetNet(self, net):
            self._net = net

        def GetNetname(self):
            return self._net._name if self._net else ""

        def GetClass(self):
            return "PCB_TRACK"

    class _FakeVia:
        _kind = "via"

        def __init__(self, _board):
            self.m_Uuid = make_uuid()
            self._pos = None
            self._drill = None
            self._width = None
            self._net = None

        def SetPosition(self, pos):
            self._pos = pos

        def GetPosition(self):
            return self._pos

        def SetDrill(self, d):
            self._drill = d

        def SetWidth(self, w):
            self._width = w

        def GetLayer(self):
            return 0

        def SetNet(self, net):
            self._net = net

        def GetNetname(self):
            return self._net._name if self._net else ""

        def GetClass(self):
            return "PCB_VIA"

    class _FakeNetInfo:
        _kind = "net"

        def __init__(self, _board, name):
            self._name = name
            self._code = next(net_codes)

        def GetNetCode(self):
            return self._code

    def fake_footprint_load(library_path, footprint_name, preserveUUID=False):
        if footprint_name == "DOES_NOT_EXIST":
            return None
        return _make_fake_footprint(reference="", value="", x_mm=0.0, y_mm=0.0)

    class _FakeShape:
        _kind = "shape"

        def __init__(self, _board):
            self._shape = None
            self._layer = None
            self._start = None
            self._end = None

        def SetShape(self, s):
            self._shape = s

        def SetLayer(self, layer_id):
            self._layer = layer_id

        def SetStart(self, pos):
            self._start = pos

        def SetEnd(self, pos):
            self._end = pos

    save_calls = []

    def fake_new_board(path):
        new_board = types.SimpleNamespace()
        new_board.GetLayerID = lambda name: layers.get(name, -1)
        new_board.Add = lambda item: None
        new_board._path = path
        return new_board

    def fake_save_board(path, board_obj, aSkipSettings=False):
        save_calls.append((path, board_obj))
        return True

    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board
    # Simple, self-consistent unit conversion for the test: 1 mm = 1e6
    # internal units (the real scale is nm, irrelevant here — only that
    # ToMM/FromMM are exact inverses matters for these tests).
    fake_module.ToMM = lambda v: v / 1e6
    fake_module.FromMM = lambda v: v * 1e6
    fake_module.VECTOR2I = _FakeVector2I
    fake_module.Refresh = lambda: None
    fake_module.FootprintLoad = fake_footprint_load
    fake_module.PCB_TRACK = _FakeTrack
    fake_module.PCB_VIA = _FakeVia
    fake_module.NETINFO_ITEM = _FakeNetInfo
    fake_module.NewBoard = fake_new_board
    fake_module.PCB_SHAPE = _FakeShape
    fake_module.SHAPE_T_RECT = "RECT"
    fake_module.SaveBoard = fake_save_board
    fake_module._save_calls = save_calls

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
# add_footprint
# --------------------------------------------------------------------------- #
def test_add_footprint_success(fake_pcbnew):
    kwt, _fake_module, board, footprints = fake_pcbnew
    result = kwt.add_footprint(
        {
            "library_path": "C:/libs/Resistor_SMD.pretty",
            "footprint_name": "R_0603_1608Metric",
            "reference": "R5",
            "x_mm": 10.0,
            "y_mm": 20.0,
            "value": "22k",
            "rotation_deg": 90.0,
        }
    )
    assert "R5" in result
    assert "10.000" in result and "20.000" in result
    fp = board.FindFootprintByReference("R5")
    assert fp is not None
    assert fp.GetValue() == "22k"
    assert fp.GetOrientationDegrees() == 90.0
    assert fp.GetPosition().x == pytest.approx(10.0 * 1e6)


def test_add_footprint_missing_required_arg(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.add_footprint(
            {
                "footprint_name": "R_0603_1608Metric",
                "reference": "R5",
                "x_mm": 1.0,
                "y_mm": 1.0,
            }
        )


def test_add_footprint_reference_already_exists(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.add_footprint(
            {
                "library_path": "C:/libs/Resistor_SMD.pretty",
                "footprint_name": "R_0603_1608Metric",
                "reference": "R1",
                "x_mm": 1.0,
                "y_mm": 1.0,
            }
        )


def test_add_footprint_not_found_in_library(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.add_footprint(
            {
                "library_path": "C:/libs/Resistor_SMD.pretty",
                "footprint_name": "DOES_NOT_EXIST",
                "reference": "R5",
                "x_mm": 1.0,
                "y_mm": 1.0,
            }
        )


# --------------------------------------------------------------------------- #
# delete_footprint
# --------------------------------------------------------------------------- #
def test_delete_footprint_success(fake_pcbnew):
    kwt, _fake_module, board, footprints = fake_pcbnew
    result = kwt.delete_footprint({"reference": "R1"})
    assert "R1" in result
    assert board.FindFootprintByReference("R1") is None


def test_delete_footprint_missing_reference(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.delete_footprint({})


def test_delete_footprint_unknown_reference(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.delete_footprint({"reference": "R99"})


# --------------------------------------------------------------------------- #
# add_track / delete_track
# --------------------------------------------------------------------------- #
def test_add_track_success(fake_pcbnew):
    kwt, _fake_module, board, _footprints = fake_pcbnew
    result = kwt.add_track(
        {
            "start_x_mm": 1.0,
            "start_y_mm": 1.0,
            "end_x_mm": 5.0,
            "end_y_mm": 1.0,
            "layer": "F.Cu",
            "width_mm": 0.3,
            "net_name": "GND",
        }
    )
    assert "uuid" in result.lower() or "uuid-" in result
    tracks = board.GetTracks()
    assert len(tracks) == 1
    assert tracks[0].GetClass() == "PCB_TRACK"
    assert tracks[0].GetNetname() == "GND"


def test_add_track_missing_required_arg(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.add_track(
            {"start_x_mm": 1.0, "start_y_mm": 1.0, "end_x_mm": 5.0, "end_y_mm": 1.0}
        )


def test_add_track_invalid_layer(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.add_track(
            {
                "start_x_mm": 1.0,
                "start_y_mm": 1.0,
                "end_x_mm": 5.0,
                "end_y_mm": 1.0,
                "layer": "Not.A.Layer",
            }
        )


def test_delete_track_success(fake_pcbnew):
    kwt, _fake_module, board, _footprints = fake_pcbnew
    kwt.add_track(
        {
            "start_x_mm": 1.0,
            "start_y_mm": 1.0,
            "end_x_mm": 5.0,
            "end_y_mm": 1.0,
            "layer": "F.Cu",
        }
    )
    added_uuid = board.GetTracks()[0].m_Uuid.AsString()
    result = kwt.delete_track({"uuid": added_uuid})
    assert added_uuid in result
    assert board.GetTracks() == []


def test_delete_via_reports_via_kind(fake_pcbnew):
    kwt, _fake_module, board, _footprints = fake_pcbnew
    kwt.add_via({"x_mm": 1.0, "y_mm": 1.0})
    added_uuid = board.GetTracks()[0].m_Uuid.AsString()
    result = kwt.delete_track({"uuid": added_uuid})
    assert "via" in result.lower()
    assert board.GetTracks() == []


def test_delete_track_missing_uuid(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.delete_track({})


def test_delete_track_unknown_uuid(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.delete_track({"uuid": "does-not-exist"})


# --------------------------------------------------------------------------- #
# add_via
# --------------------------------------------------------------------------- #
def test_add_via_success(fake_pcbnew):
    kwt, _fake_module, board, _footprints = fake_pcbnew
    result = kwt.add_via({"x_mm": 3.0, "y_mm": 4.0, "drill_mm": 0.25, "width_mm": 0.5})
    assert "3.000" in result and "4.000" in result
    vias = board.GetTracks()
    assert len(vias) == 1
    assert vias[0].GetClass() == "PCB_VIA"


def test_add_via_with_new_net(fake_pcbnew):
    kwt, _fake_module, board, _footprints = fake_pcbnew
    kwt.add_via({"x_mm": 1.0, "y_mm": 1.0, "net_name": "VCC"})
    assert board.FindNet("VCC") is not None
    assert board.GetTracks()[0].GetNetname() == "VCC"


def test_add_via_missing_required_arg(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.add_via({"x_mm": 1.0})


# --------------------------------------------------------------------------- #
# set_pad_net
# --------------------------------------------------------------------------- #
def test_set_pad_net_success(fake_pcbnew):
    kwt, _fake_module, board, footprints = fake_pcbnew
    result = kwt.set_pad_net({"reference": "R1", "pad_number": "1", "net_name": "GND"})
    assert "GND" in result
    pad = footprints["R1"].FindPadByNumber("1")
    assert pad.GetNet().GetNetCode() is not None
    assert board.FindNet("GND") is not None


def test_set_pad_net_missing_required_arg(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.set_pad_net({"reference": "R1", "pad_number": "1"})


def test_set_pad_net_unknown_reference(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.set_pad_net({"reference": "R99", "pad_number": "1", "net_name": "GND"})


def test_set_pad_net_unknown_pad(fake_pcbnew):
    kwt, *_ = fake_pcbnew
    with pytest.raises(RuntimeError):
        kwt.set_pad_net({"reference": "R1", "pad_number": "99", "net_name": "GND"})


# --------------------------------------------------------------------------- #
# create_board_from_scratch
# --------------------------------------------------------------------------- #
def test_create_board_from_scratch_success(fake_pcbnew, tmp_path):
    kwt, fake_module, _board, _footprints = fake_pcbnew
    target = tmp_path / "new_board.kicad_pcb"
    result = kwt.create_board_from_scratch(
        {"path": str(target), "width_mm": 100.0, "height_mm": 80.0}
    )
    assert str(target) in result
    assert "100.000" in result and "80.000" in result
    assert len(fake_module._save_calls) == 1
    saved_path, _saved_board = fake_module._save_calls[0]
    assert saved_path == str(target)


def test_create_board_from_scratch_missing_required_arg(fake_pcbnew, tmp_path):
    kwt, *_ = fake_pcbnew
    target = tmp_path / "new_board.kicad_pcb"
    with pytest.raises(RuntimeError):
        kwt.create_board_from_scratch({"path": str(target), "width_mm": 100.0})


def test_create_board_from_scratch_already_exists(fake_pcbnew, tmp_path):
    kwt, *_ = fake_pcbnew
    target = tmp_path / "existing_board.kicad_pcb"
    target.write_text("(kicad_pcb)", encoding="utf-8")
    with pytest.raises(RuntimeError):
        kwt.create_board_from_scratch(
            {"path": str(target), "width_mm": 100.0, "height_mm": 80.0}
        )


def test_create_board_from_scratch_no_pcbnew(monkeypatch, tmp_path):
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.kicad_write_tools", None)
    import actions.kicad_write_tools as kwt

    target = tmp_path / "new_board.kicad_pcb"
    with pytest.raises(RuntimeError):
        kwt.create_board_from_scratch(
            {"path": str(target), "width_mm": 100.0, "height_mm": 80.0}
        )


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
_ALL_WRITE_TOOL_NAMES = {
    "move_footprint",
    "rotate_footprint",
    "set_footprint_value",
    "add_footprint",
    "delete_footprint",
    "add_track",
    "delete_track",
    "add_via",
    "set_pad_net",
    "create_board_from_scratch",
}


def test_register_kicad_write_tools():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.kicad_write_tools", None)
    import actions.kicad_write_tools as kwt
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kwt.register_kicad_write_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert names == _ALL_WRITE_TOOL_NAMES

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
        "list_tracks",
    } | _ALL_WRITE_TOOL_NAMES
    read_only_names = {n for n in names if registry.get(n).read_only}
    write_names = {n for n in names if not registry.get(n).read_only}
    assert read_only_names == {
        "get_project_info",
        "list_components",
        "run_drc",
        "run_erc",
        "list_tracks",
    }
    assert write_names == _ALL_WRITE_TOOL_NAMES
