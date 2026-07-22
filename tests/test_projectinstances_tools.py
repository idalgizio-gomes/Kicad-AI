"""Tests for actions/projectinstances_tools.py.

Run WITHOUT the real ProjectInstances plugin and WITHOUT real pcbnew:
`find_pcm_plugin_dir`/`_load` and `pcbnew` are faked, mirroring
test_kicad_parasitics_tools.py's style.
"""

from __future__ import annotations

import sys
import types

import pytest


def test_module_imports_without_sibling_or_pcbnew():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.projectinstances_tools", None)
    import actions.projectinstances_tools as pit  # noqa: F401

    assert hasattr(pit, "register_projectinstances_tools")
    assert hasattr(pit, "list_hierarchical_sheet_replication_status")


@pytest.fixture
def pit(monkeypatch):
    sys.modules.pop("actions.projectinstances_tools", None)
    import actions.projectinstances_tools as module

    return module


def test_load_uses_correct_pcm_identifier(pit, monkeypatch):
    seen = {}

    def fake_find(identifier):
        seen["identifier"] = identifier
        raise pit.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(pit, "find_pcm_plugin_dir", fake_find)

    with pytest.raises(pit.SiblingPluginNotFoundError):
        pit._load("hdata")

    assert seen["identifier"] == "com_github_officialdyray_projectinstances"


@pytest.fixture
def fake_pcbnew(monkeypatch, pit, tmp_path):
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text("x")
    (tmp_path / "board.kicad_sch").write_text("x")
    board = types.SimpleNamespace(GetFileName=lambda: str(board_path))
    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)
    return pit, fake_module, board, tmp_path


class _FakeSheetFileInner:
    def __init__(self, board=None, anchor_ref=None):
        self.board = board
        self.anchorRef = anchor_ref


class _FakeInstance:
    def __init__(self, name, board=None, anchor_ref=None, enabled=False, children=None):
        self.name = name
        self.sheetFile = _FakeSheetFileInner(board=board, anchor_ref=anchor_ref)
        self.enabled = enabled
        self._subSheets = children or []


class _FakeSheetFileManager:
    def load_file_data(self, cfg):
        pass


class _FakeConfigMan:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _make_hdata_module(root_children):
    root = _FakeInstance("Root", board=None, children=root_children)
    root.load = lambda cfg: None

    module = types.SimpleNamespace()
    module.SheetFile = lambda sch_path: object()
    module.RootInstance = lambda sheet_file: root
    module.sheetFileManager = _FakeSheetFileManager()
    return module, root


def _make_cfgman_module():
    module = types.SimpleNamespace()
    module.ConfigMan = _FakeConfigMan
    return module


def _install_fake_sibling(pit, monkeypatch, hdata_mod, cfgman_mod):
    def fake_load(submodule):
        return {"hdata": hdata_mod, "cfgman": cfgman_mod}[submodule]

    monkeypatch.setattr(pit, "_load", fake_load)


# --------------------------------------------------------------------------- #
# list_hierarchical_sheet_replication_status
# --------------------------------------------------------------------------- #
def test_board_not_saved(pit, monkeypatch):
    board = types.SimpleNamespace(GetFileName=lambda: "")
    fake_module = types.SimpleNamespace(GetBoard=lambda: board)
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)

    with pytest.raises(RuntimeError):
        pit.list_hierarchical_sheet_replication_status({})


def test_schematic_not_found(pit, monkeypatch, tmp_path):
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text("x")
    board = types.SimpleNamespace(GetFileName=lambda: str(board_path))
    fake_module = types.SimpleNamespace(GetBoard=lambda: board)
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)

    with pytest.raises(RuntimeError):
        pit.list_hierarchical_sheet_replication_status({})


def test_sibling_not_installed(pit, fake_pcbnew, monkeypatch):
    def raise_not_found(submodule):
        raise pit.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(pit, "_load", raise_not_found)
    result = pit.list_hierarchical_sheet_replication_status({})
    assert "não está instalado" in result


def test_flat_schematic_no_subsheets(pit, fake_pcbnew, monkeypatch):
    hdata_mod, _root = _make_hdata_module([])
    cfgman_mod = _make_cfgman_module()
    _install_fake_sibling(pit, monkeypatch, hdata_mod, cfgman_mod)

    result = pit.list_hierarchical_sheet_replication_status({})
    assert "sem hierarquia" in result


def test_leaf_and_nonleaf_reported(pit, fake_pcbnew, monkeypatch):
    leaf_active = _FakeInstance("LedDriver", board="fake-board", anchor_ref="U1", enabled=True)
    leaf_inactive = _FakeInstance("PowerStage", board="fake-board", anchor_ref="U2", enabled=False)
    nonleaf = _FakeInstance("Subsystem", board=None, children=[leaf_inactive])

    hdata_mod, _root = _make_hdata_module([leaf_active, nonleaf])
    cfgman_mod = _make_cfgman_module()
    _install_fake_sibling(pit, monkeypatch, hdata_mod, cfgman_mod)

    result = pit.list_hierarchical_sheet_replication_status({})

    assert "LedDriver" in result
    assert "ativo" in result
    assert "U1" in result
    assert "Subsystem" in result
    assert "PowerStage" in result
    assert "U2" in result


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_projectinstances_tools(pit):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    pit.register_projectinstances_tools(registry)
    defn = registry.get("list_hierarchical_sheet_replication_status")
    assert defn is not None
    assert defn.read_only is True
