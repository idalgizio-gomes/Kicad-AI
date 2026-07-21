"""Tests for actions/jlcpcb_tools.py.

These run WITHOUT a real JLC-Plugin-for-KiCad installation and WITHOUT a
real `pcbnew`: the sibling-plugin loader (`_load`) is faked exactly like
test_libforge_tools.py, and a minimal fake `pcbnew` module (+ fake board)
is injected into `sys.modules` only for the tests that need to get past
`_get_board()`. The module itself must import cleanly with none of that
present (asserted first).
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import types
from pathlib import Path

import pytest


def test_module_imports_without_jlcpcb():
    sys.modules.pop("actions.jlcpcb_tools", None)
    import actions.jlcpcb_tools as jlt  # noqa: F401

    assert hasattr(jlt, "register_jlcpcb_tools")
    assert hasattr(jlt, "generate_jlcpcb_fabrication_files")


@pytest.fixture
def jlt(monkeypatch):
    sys.modules.pop("actions.jlcpcb_tools", None)
    import actions.jlcpcb_tools as module

    return module


# --------------------------------------------------------------------------- #
# _get_board() / board-state failures (no fake pcbnew installed yet)
# --------------------------------------------------------------------------- #
def test_generate_pcbnew_unavailable(jlt, monkeypatch):
    monkeypatch.delitem(sys.modules, "pcbnew", raising=False)
    with pytest.raises(RuntimeError):
        jlt.generate_jlcpcb_fabrication_files({})


# --------------------------------------------------------------------------- #
# Fake pcbnew + board plumbing, reused across the remaining tests
# --------------------------------------------------------------------------- #
class FakeTitleBlock:
    def __init__(self, title="MyBoard", revision="A", company="ACME", date="2026-01-01"):
        self._title = title
        self._revision = revision
        self._company = company
        self._date = date

    def GetTitle(self):
        return self._title

    def GetRevision(self):
        return self._revision

    def GetCompany(self):
        return self._company

    def GetDate(self):
        return self._date

    def GetComment(self, index):
        return ""


class FakeBoard:
    def __init__(self, path):
        self._path = path

    def GetFileName(self):
        return self._path

    def GetTitleBlock(self):
        return FakeTitleBlock()


def _install_fake_pcbnew(monkeypatch, board):
    fake_pcbnew = types.ModuleType("pcbnew")
    fake_pcbnew.GetBoard = lambda: board
    monkeypatch.setitem(sys.modules, "pcbnew", fake_pcbnew)


def test_generate_board_not_open(jlt, monkeypatch):
    _install_fake_pcbnew(monkeypatch, None)
    with pytest.raises(RuntimeError):
        jlt.generate_jlcpcb_fabrication_files({})


def test_generate_board_not_saved(jlt, monkeypatch):
    _install_fake_pcbnew(monkeypatch, FakeBoard(""))
    with pytest.raises(RuntimeError):
        jlt.generate_jlcpcb_fabrication_files({})


def test_generate_sibling_not_installed(jlt, monkeypatch, tmp_path):
    board_path = str(tmp_path / "myboard.kicad_pcb")
    _install_fake_pcbnew(monkeypatch, FakeBoard(board_path))

    def raise_not_found(submodule):
        raise jlt.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(jlt, "_load", raise_not_found)
    result = jlt.generate_jlcpcb_fabrication_files({})
    assert "não está instalado" in result


# --------------------------------------------------------------------------- #
# Fake sibling plugin submodules (process / config / options)
# --------------------------------------------------------------------------- #
def _make_fake_options_module():
    return types.SimpleNamespace(
        AUTO_TRANSLATE_OPT="AUTO TRANSLATE",
        AUTO_FILL_OPT="AUTO FILL",
        EXCLUDE_DNP_OPT="EXCLUDE DNP",
        EXTEND_EDGE_CUT_OPT="EXTEND_EDGE_CUT",
        ALTERNATIVE_EDGE_CUT_OPT="ALTERNATIVE_EDGE_CUT",
        ALL_ACTIVE_LAYERS_OPT="ALL_ACTIVE_LAYERS",
        ARCHIVE_NAME="ARCHIVE_NAME",
        EXTRA_LAYERS="EXTRA_LAYERS",
        BACKUP_OPT="BACKUP_OPT",
    )


def _make_fake_config_module():
    return types.SimpleNamespace(
        designatorsFileName="designators.csv",
        placementFileName="positions.csv",
        bomFileName="bom.csv",
        outputFolder="production",
    )


def _make_fake_process_module(calls: list):
    class FakeProcessManager:
        def __init__(self, board=None):
            self.board = board
            self.bom = []
            self.components = []

        @staticmethod
        def normalize_filename(filename):
            return re.sub(r"[^\w\s\.\-]", "", filename)

        def update_zone_fills(self):
            calls.append("update_zone_fills")

        def generate_gerber(self, temp_dir, extra_layers, extend_edge_cuts, alternative_edge_cuts, all_active_layers):
            calls.append(("generate_gerber", extra_layers, extend_edge_cuts, alternative_edge_cuts, all_active_layers))
            Path(temp_dir, "board.gtl").write_text("gerber")

        def generate_drills(self, temp_dir):
            calls.append("generate_drills")
            Path(temp_dir, "board.drl").write_text("drill")

        def generate_netlist(self, temp_dir):
            calls.append("generate_netlist")
            Path(temp_dir, "netlist.ipc").write_text("netlist")

        def generate_tables(self, temp_dir, auto_translate, exclude_dnp):
            calls.append(("generate_tables", auto_translate, exclude_dnp))
            Path(temp_dir, "designators.csv").write_text("designators")

        def generate_positions(self, temp_dir):
            calls.append("generate_positions")
            Path(temp_dir, "positions.csv").write_text("positions")

        def generate_bom(self, temp_dir):
            calls.append("generate_bom")
            Path(temp_dir, "bom.csv").write_text("bom")

        def generate_archive(self, temp_dir, temp_file):
            calls.append("generate_archive")
            archive = shutil.make_archive(temp_file, "zip", temp_dir)
            archive = shutil.move(archive, temp_dir)
            for item in os.listdir(temp_dir):
                if not item.endswith((".zip", ".csv", ".ipc")):
                    os.remove(os.path.join(temp_dir, item))
            return archive

    mod = types.SimpleNamespace()
    mod.ProcessManager = FakeProcessManager
    return mod


def _install_fake_sibling(jlt, monkeypatch, modules: dict):
    def fake_load(submodule):
        try:
            return modules[submodule]
        except KeyError:
            raise ImportError(f"no fake module registered for {submodule!r}")

    monkeypatch.setattr(jlt, "_load", fake_load)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_generate_success_default_options(jlt, monkeypatch, tmp_path):
    board_path = str(tmp_path / "myboard.kicad_pcb")
    _install_fake_pcbnew(monkeypatch, FakeBoard(board_path))

    calls: list = []
    _install_fake_sibling(
        jlt,
        monkeypatch,
        {
            "process": _make_fake_process_module(calls),
            "config": _make_fake_config_module(),
            "options": _make_fake_options_module(),
        },
    )

    result = jlt.generate_jlcpcb_fabrication_files({})

    output_path = tmp_path / "production"
    assert output_path.is_dir()
    assert str(output_path) in result

    produced = {p.name for p in output_path.iterdir()}
    assert any(name.endswith(".zip") for name in produced)
    assert "designators.csv" in produced or any("designators" in n for n in produced)
    assert "positions.csv" in produced or any("positions" in n for n in produced)
    assert "bom.csv" in produced or any("bom" in n for n in produced)

    # default backup=True must NOT create the extra timestamped backup
    # folder (see this tool's own docstring on the inverted BACKUP_OPT).
    assert not (output_path / "backups").exists()

    assert "update_zone_fills" not in calls  # auto_fill defaults to False


def test_generate_auto_fill_calls_update_zone_fills(jlt, monkeypatch, tmp_path):
    board_path = str(tmp_path / "myboard.kicad_pcb")
    _install_fake_pcbnew(monkeypatch, FakeBoard(board_path))

    calls: list = []
    _install_fake_sibling(
        jlt,
        monkeypatch,
        {
            "process": _make_fake_process_module(calls),
            "config": _make_fake_config_module(),
            "options": _make_fake_options_module(),
        },
    )

    jlt.generate_jlcpcb_fabrication_files({"auto_fill": True})
    assert "update_zone_fills" in calls


def test_generate_backup_false_creates_extra_timestamped_copy(jlt, monkeypatch, tmp_path):
    board_path = str(tmp_path / "myboard.kicad_pcb")
    _install_fake_pcbnew(monkeypatch, FakeBoard(board_path))

    calls: list = []
    _install_fake_sibling(
        jlt,
        monkeypatch,
        {
            "process": _make_fake_process_module(calls),
            "config": _make_fake_config_module(),
            "options": _make_fake_options_module(),
        },
    )

    result = jlt.generate_jlcpcb_fabrication_files({"backup": False})

    output_path = tmp_path / "production"
    backups_dir = output_path / "backups"
    assert backups_dir.is_dir()
    assert any(p.suffix == ".zip" for p in backups_dir.iterdir())
    assert "Cópia de segurança adicional" in result


def test_generate_custom_archive_name_renames_files(jlt, monkeypatch, tmp_path):
    board_path = str(tmp_path / "myboard.kicad_pcb")
    _install_fake_pcbnew(monkeypatch, FakeBoard(board_path))

    calls: list = []
    _install_fake_sibling(
        jlt,
        monkeypatch,
        {
            "process": _make_fake_process_module(calls),
            "config": _make_fake_config_module(),
            "options": _make_fake_options_module(),
        },
    )

    result = jlt.generate_jlcpcb_fabrication_files({"archive_name": "${TITLE}_${REVISION}"})

    output_path = tmp_path / "production"
    produced = {p.name for p in output_path.iterdir()}
    assert "MyBoard_A.zip" in produced
    assert "MyBoard_A_designators.csv" in produced
    assert "MyBoard_A_positions.csv" in produced
    assert "MyBoard_A_bom.csv" in produced
    assert "MyBoard_A.zip" in result


def test_generate_exclude_dnp_and_extra_layers_forwarded(jlt, monkeypatch, tmp_path):
    board_path = str(tmp_path / "myboard.kicad_pcb")
    _install_fake_pcbnew(monkeypatch, FakeBoard(board_path))

    calls: list = []
    _install_fake_sibling(
        jlt,
        monkeypatch,
        {
            "process": _make_fake_process_module(calls),
            "config": _make_fake_config_module(),
            "options": _make_fake_options_module(),
        },
    )

    jlt.generate_jlcpcb_fabrication_files(
        {
            "exclude_dnp": True,
            "extra_layers": "Dwgs.User,Cmts.User",
            "extend_edge_cuts": True,
            "alternative_edge_cuts": True,
            "all_active_layers": True,
        }
    )

    gerber_call = next(c for c in calls if isinstance(c, tuple) and c[0] == "generate_gerber")
    assert gerber_call == (
        "generate_gerber",
        "Dwgs.User,Cmts.User",
        True,
        True,
        True,
    )
    tables_call = next(c for c in calls if isinstance(c, tuple) and c[0] == "generate_tables")
    assert tables_call == ("generate_tables", False, True)


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_jlcpcb_tools(jlt):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    jlt.register_jlcpcb_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert names == {"generate_jlcpcb_fabrication_files"}
    assert registry.get("generate_jlcpcb_fabrication_files").read_only is False


def test_register_jlcpcb_tools_coexists_with_other_registries(jlt):
    """Registering alongside kicad_tools/kicad_write_tools/libforge_tools
    must not collide (distinct tool names)."""
    sys.modules.pop("actions.kicad_tools", None)
    sys.modules.pop("actions.kicad_write_tools", None)
    sys.modules.pop("actions.libforge_tools", None)
    import actions.kicad_tools as kt
    import actions.kicad_write_tools as kwt
    import actions.libforge_tools as lft
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kt.register_kicad_tools(registry)
    kwt.register_kicad_write_tools(registry)
    lft.register_libforge_tools(registry)
    jlt.register_jlcpcb_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert "generate_jlcpcb_fabrication_files" in names
    # 5 (kicad_tools) + 10 (kicad_write_tools) + 3 (libforge_tools) + 1 (jlcpcb_tools)
    assert len(names) == 19
