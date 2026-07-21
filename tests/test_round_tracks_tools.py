"""Tests for actions/round_tracks_tools.py.

These run WITHOUT a real Round Tracks installation and WITHOUT real
pcbnew/wx: the sibling-plugin loader (`_load`, `find_pcm_plugin_dir`) and
`pcbnew` are faked via monkeypatch/`sys.modules`, and the sibling's own
`RoundTracks` class is replaced with a plain-Python fake that mimics just
the interface this module actually uses (`.config['classes']`,
`.addIntermediateTracks(...)`, `.prog.Pulse(...)`, `.Destroy()`) — mirroring
the style already used in test_kicad_parasitics_tools.py. The module itself
must import cleanly with neither present at all (asserted first).
"""

from __future__ import annotations

import sys
import types

import pytest


def test_module_imports_without_round_tracks_or_pcbnew():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.round_tracks_tools", None)
    import actions.round_tracks_tools as rtt  # noqa: F401

    assert hasattr(rtt, "register_round_tracks_tools")
    assert hasattr(rtt, "round_pcb_tracks")


@pytest.fixture
def rtt(monkeypatch):
    sys.modules.pop("actions.round_tracks_tools", None)
    import actions.round_tracks_tools as module

    return module


# --------------------------------------------------------------------------- #
# find_pcm_plugin_dir wiring
# --------------------------------------------------------------------------- #
def test_load_uses_correct_pcm_identifier(rtt, monkeypatch):
    seen = {}

    def fake_find_pcm_plugin_dir(identifier):
        seen["identifier"] = identifier
        raise rtt.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(rtt, "find_pcm_plugin_dir", fake_find_pcm_plugin_dir)

    with pytest.raises(rtt.SiblingPluginNotFoundError):
        rtt._load("round_tracks_action")

    assert seen["identifier"] == "com_github_mitxela_kicad-round-tracks"


# --------------------------------------------------------------------------- #
# fake pcbnew / board plumbing
# --------------------------------------------------------------------------- #
class _FakeBoard:
    def __init__(self, file_name="C:/proj/board.kicad_pcb", tracks=None):
        self._file_name = file_name
        self._tracks = list(tracks) if tracks is not None else [object()] * 5

    def GetFileName(self):
        return self._file_name

    def GetTracks(self):
        return self._tracks


@pytest.fixture
def fake_pcbnew(monkeypatch, rtt):
    board = _FakeBoard()

    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board

    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)
    return rtt, fake_module, board


# --------------------------------------------------------------------------- #
# fake sibling RoundTracks class
# --------------------------------------------------------------------------- #
class _FakeRoundTracks:
    """Mimics only what round_tracks_tools.py actually touches on the real
    RoundTracks(RoundTracksDialog) instance."""

    instances = []

    def __init__(self, board, action):
        self.board = board
        self.action = action
        self.config = {
            "classes": {
                "Default": {"do_round": True, "scaling": 2.0, "passes": 3},
                "PowerNet": {"do_round": True, "scaling": 2.0, "passes": 3},
            }
        }
        self.prog = None
        self.destroyed = False
        self.calls = []
        _FakeRoundTracks.instances.append(self)

    def addIntermediateTracks(
        self, scaling, netclass, native, onlySelection, avoid_junctions, msg=""
    ):
        # Real addIntermediateTracks() calls self.prog.Pulse(...) once per
        # netcode regardless of match — exercise that here too, so a
        # missing/None self.prog surfaces as a test failure exactly like it
        # would for the real class.
        self.prog.Pulse(f"netclass {netclass}")
        self.calls.append(
            {
                "scaling": scaling,
                "netclass": netclass,
                "native": native,
                "onlySelection": onlySelection,
                "avoid_junctions": avoid_junctions,
            }
        )
        # simulate mutating the board: two new track objects per call
        self.board._tracks.append(object())
        self.board._tracks.append(object())

    def Destroy(self):
        self.destroyed = True


@pytest.fixture
def fake_sibling(monkeypatch, rtt):
    _FakeRoundTracks.instances = []
    fake_action_mod = types.SimpleNamespace(RoundTracks=_FakeRoundTracks)

    def fake_load(submodule):
        assert submodule == "round_tracks_action"
        return fake_action_mod

    monkeypatch.setattr(rtt, "_load", fake_load)
    return fake_action_mod


# --------------------------------------------------------------------------- #
# argument validation
# --------------------------------------------------------------------------- #
def test_invalid_scaling(rtt, fake_pcbnew, fake_sibling):
    with pytest.raises(RuntimeError):
        rtt.round_pcb_tracks({"scaling": "not-a-number"})


def test_zero_scaling_rejected(rtt, fake_pcbnew, fake_sibling):
    with pytest.raises(RuntimeError):
        rtt.round_pcb_tracks({"scaling": 0})


def test_negative_scaling_rejected(rtt, fake_pcbnew, fake_sibling):
    with pytest.raises(RuntimeError):
        rtt.round_pcb_tracks({"scaling": -1.0})


# --------------------------------------------------------------------------- #
# pcbnew / board plumbing
# --------------------------------------------------------------------------- #
def test_no_board_open(rtt, monkeypatch):
    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: None
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)

    with pytest.raises(RuntimeError):
        rtt.round_pcb_tracks({})


def test_pcbnew_unavailable(rtt, monkeypatch):
    monkeypatch.delitem(sys.modules, "pcbnew", raising=False)

    with pytest.raises(RuntimeError):
        rtt.round_pcb_tracks({})


def test_sibling_not_installed(rtt, fake_pcbnew, monkeypatch):
    def raise_not_found(submodule):
        raise rtt.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(rtt, "_load", raise_not_found)

    result = rtt.round_pcb_tracks({})
    assert "não está instalado" in result


# --------------------------------------------------------------------------- #
# end-to-end round_pcb_tracks (fully faked sibling + pcbnew)
# --------------------------------------------------------------------------- #
def test_default_processes_every_netclass(rtt, fake_pcbnew, fake_sibling):
    result = rtt.round_pcb_tracks({})

    rt = _FakeRoundTracks.instances[-1]
    assert rt.destroyed is True
    assert isinstance(rt.prog, rtt._NullProgress)

    processed = {c["netclass"] for c in rt.calls}
    assert processed == {"Default", "PowerNet"}
    for c in rt.calls:
        assert c["scaling"] == pytest.approx(2.0)
        assert c["native"] is True
        assert c["onlySelection"] is False
        assert c["avoid_junctions"] is False

    assert "Default" in result
    assert "PowerNet" in result


def test_custom_scaling_native_avoid_junctions_passed_through(
    rtt, fake_pcbnew, fake_sibling
):
    rtt.round_pcb_tracks(
        {"scaling": 0.5, "native": False, "avoid_junctions": True}
    )

    rt = _FakeRoundTracks.instances[-1]
    for c in rt.calls:
        assert c["scaling"] == pytest.approx(0.5)
        assert c["native"] is False
        assert c["avoid_junctions"] is True


def test_netclass_filter_limits_to_one_class(rtt, fake_pcbnew, fake_sibling):
    rtt.round_pcb_tracks({"netclass": "PowerNet"})

    rt = _FakeRoundTracks.instances[-1]
    assert [c["netclass"] for c in rt.calls] == ["PowerNet"]


def test_unknown_netclass_raises_and_still_destroys(rtt, fake_pcbnew, fake_sibling):
    with pytest.raises(RuntimeError):
        rtt.round_pcb_tracks({"netclass": "NoSuchClass"})

    rt = _FakeRoundTracks.instances[-1]
    assert rt.destroyed is True
    assert rt.calls == []


def test_track_count_reported_before_and_after(rtt, fake_pcbnew, fake_sibling):
    board = fake_pcbnew[2]
    before = len(board._tracks)

    result = rtt.round_pcb_tracks({"netclass": "Default"})

    after = len(board._tracks)
    assert after == before + 2
    assert str(before) in result
    assert str(after) in result


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_round_tracks_tools(rtt):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    rtt.register_round_tracks_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert names == {"round_pcb_tracks"}
    assert registry.get("round_pcb_tracks").read_only is False


def test_register_coexists_with_other_registries(rtt):
    sys.modules.pop("actions.kicad_tools", None)
    sys.modules.pop("actions.kicad_write_tools", None)
    import actions.kicad_tools as kt
    import actions.kicad_write_tools as kwt
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kt.register_kicad_tools(registry)
    kwt.register_kicad_write_tools(registry)
    rtt.register_round_tracks_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert "round_pcb_tracks" in names
