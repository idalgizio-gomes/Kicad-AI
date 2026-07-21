"""Tests for actions/board2pdf_tools.py.

These run WITHOUT a real Board2Pdf installation and WITHOUT real pcbnew:
the sibling-plugin loader (`_load`), `find_pcm_plugin_dir`, and `pcbnew`
itself are all faked, mirroring the `sys.modules`/monkeypatch style
already used in test_libforge_tools.py and test_kicad_tools.py. The
module itself must import cleanly with no Board2Pdf/pcbnew present at
all (asserted first).
"""

from __future__ import annotations

import sys
import types

import pytest


def test_module_imports_without_board2pdf():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.board2pdf_tools", None)
    import actions.board2pdf_tools as b2p  # noqa: F401

    assert hasattr(b2p, "register_board2pdf_tools")
    assert hasattr(b2p, "export_board_to_pdf")


@pytest.fixture
def b2p(monkeypatch):
    sys.modules.pop("actions.board2pdf_tools", None)
    import actions.board2pdf_tools as module

    return module


# --------------------------------------------------------------------------- #
# fake pcbnew
# --------------------------------------------------------------------------- #
def _make_fake_board(file_name):
    board = types.SimpleNamespace()
    board.GetFileName = lambda: file_name
    return board


def _install_fake_pcbnew(monkeypatch, board=None, current_board="_missing_"):
    """`current_board`: object returned by pcbnew.GetBoard() (None means
    "no board open"; `"_missing_"` sentinel means "leave it as a
    reasonable default of None" is NOT what we want for LoadBoard-only
    tests, so callers that don't care about GetBoard() pass board=None
    explicitly via a dedicated helper instead."""
    fake_module = types.SimpleNamespace()
    fake_module.LoadBoard = lambda path: _make_fake_board(path)
    fake_module.GetBoard = lambda: current_board if current_board != "_missing_" else None
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)
    return fake_module


# --------------------------------------------------------------------------- #
# fake sibling: persistence + plot
# --------------------------------------------------------------------------- #
class FakePersistence:
    """Records the configfile path it was constructed with; `load()`
    returns a fixed, full-ish config dict (mirrors the real
    persistence.Persistence.load() return shape)."""

    last_configfile = None

    def __init__(self, configfile):
        FakePersistence.last_configfile = configfile
        self.configfile = configfile

    def load(self):
        return {
            "templates": {},
            "output_path": "plot",
            "enabled_templates": [],
            "disabled_templates": [],
            "create_svg": False,
            "del_temp_files": True,
            "del_single_page_files": True,
            "assembly_file_extension": "__Assembly",
            "page_info": "",
            "info_variable": "0",
        }


def _make_fake_persistence_module():
    mod = types.SimpleNamespace()
    mod.Persistence = FakePersistence
    return mod


def _make_fake_plot_module(success=True, prints="All done!", capture_kwargs=None):
    def plot_pdfs(board, **kwargs):
        if capture_kwargs is not None:
            capture_kwargs.update(kwargs)
        if prints:
            print(prints)
        return success

    mod = types.SimpleNamespace()
    mod.plot_pdfs = plot_pdfs
    return mod


def _install_fake_sibling(b2p, monkeypatch, modules: dict):
    def fake_load(submodule):
        try:
            return modules[submodule]
        except KeyError:
            raise ImportError(f"no fake module registered for {submodule!r}")

    monkeypatch.setattr(b2p, "_load", fake_load)


# --------------------------------------------------------------------------- #
# board resolution
# --------------------------------------------------------------------------- #
def test_export_missing_board_file(b2p, tmp_path):
    missing = tmp_path / "does-not-exist.kicad_pcb"
    result = b2p.export_board_to_pdf({"board_path": str(missing)})
    assert str(missing) in result


def test_export_no_pcbnew(b2p, monkeypatch):
    sys.modules.pop("pcbnew", None)
    with pytest.raises(RuntimeError):
        b2p.export_board_to_pdf({})


def test_export_no_board_open(b2p, monkeypatch):
    _install_fake_pcbnew(monkeypatch, current_board=None)
    with pytest.raises(RuntimeError):
        b2p.export_board_to_pdf({})


# --------------------------------------------------------------------------- #
# sibling not installed
# --------------------------------------------------------------------------- #
def test_export_sibling_not_installed(b2p, monkeypatch, tmp_path):
    board_file = tmp_path / "board.kicad_pcb"
    board_file.write_text("(kicad_pcb)")
    _install_fake_pcbnew(monkeypatch)

    def raise_not_found(submodule):
        raise b2p.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(b2p, "_load", raise_not_found)

    result = b2p.export_board_to_pdf({"board_path": str(board_file)})
    assert "Board2Pdf" in result
    assert "não está instalado" in result


# --------------------------------------------------------------------------- #
# config resolution
# --------------------------------------------------------------------------- #
def test_export_explicit_config_missing(b2p, monkeypatch, tmp_path):
    board_file = tmp_path / "board.kicad_pcb"
    board_file.write_text("(kicad_pcb)")
    _install_fake_pcbnew(monkeypatch)
    _install_fake_sibling(
        b2p,
        monkeypatch,
        {
            "persistence": _make_fake_persistence_module(),
            "plot": _make_fake_plot_module(),
        },
    )

    missing_ini = tmp_path / "does-not-exist.ini"
    result = b2p.export_board_to_pdf(
        {"board_path": str(board_file), "config_ini_path": str(missing_ini)}
    )
    assert str(missing_ini) in result


def test_export_uses_local_config_next_to_board(b2p, monkeypatch, tmp_path):
    board_file = tmp_path / "board.kicad_pcb"
    board_file.write_text("(kicad_pcb)")
    local_ini = tmp_path / "board2pdf.config.ini"
    local_ini.write_text("[main]\n")

    _install_fake_pcbnew(monkeypatch)
    _install_fake_sibling(
        b2p,
        monkeypatch,
        {
            "persistence": _make_fake_persistence_module(),
            "plot": _make_fake_plot_module(),
        },
    )
    # find_pcm_plugin_dir must NOT be needed when a local ini is present.
    monkeypatch.setattr(
        b2p,
        "find_pcm_plugin_dir",
        lambda identifier: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    result = b2p.export_board_to_pdf({"board_path": str(board_file)})
    assert "sucesso" in result
    assert str(local_ini) == FakePersistence.last_configfile


def test_export_falls_back_to_bundled_default_config(b2p, monkeypatch, tmp_path):
    board_file = tmp_path / "board.kicad_pcb"
    board_file.write_text("(kicad_pcb)")

    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    default_ini = plugin_dir / "default_config.ini"
    default_ini.write_text("[main]\n")

    _install_fake_pcbnew(monkeypatch)
    _install_fake_sibling(
        b2p,
        monkeypatch,
        {
            "persistence": _make_fake_persistence_module(),
            "plot": _make_fake_plot_module(),
        },
    )
    monkeypatch.setattr(b2p, "find_pcm_plugin_dir", lambda identifier: plugin_dir)

    result = b2p.export_board_to_pdf({"board_path": str(board_file)})
    assert "sucesso" in result
    assert str(default_ini) == FakePersistence.last_configfile


def test_export_no_config_found_anywhere(b2p, monkeypatch, tmp_path):
    board_file = tmp_path / "board.kicad_pcb"
    board_file.write_text("(kicad_pcb)")

    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()  # no default_config.ini inside

    _install_fake_pcbnew(monkeypatch)
    _install_fake_sibling(
        b2p,
        monkeypatch,
        {
            "persistence": _make_fake_persistence_module(),
            "plot": _make_fake_plot_module(),
        },
    )
    monkeypatch.setattr(b2p, "find_pcm_plugin_dir", lambda identifier: plugin_dir)

    result = b2p.export_board_to_pdf({"board_path": str(board_file)})
    assert "Nenhum ficheiro de configuração encontrado" in result


# --------------------------------------------------------------------------- #
# success / overrides / failure paths
# --------------------------------------------------------------------------- #
def _setup_success(monkeypatch, b2p, tmp_path, plot_module):
    board_file = tmp_path / "board.kicad_pcb"
    board_file.write_text("(kicad_pcb)")
    local_ini = tmp_path / "board2pdf.config.ini"
    local_ini.write_text("[main]\n")

    _install_fake_pcbnew(monkeypatch)
    _install_fake_sibling(
        b2p,
        monkeypatch,
        {
            "persistence": _make_fake_persistence_module(),
            "plot": plot_module,
        },
    )
    return board_file


def test_export_success(b2p, monkeypatch, tmp_path):
    board_file = _setup_success(
        monkeypatch, b2p, tmp_path, _make_fake_plot_module(success=True, prints="All done!")
    )
    result = b2p.export_board_to_pdf({"board_path": str(board_file)})
    assert "sucesso" in result
    assert "All done!" in result


def test_export_failure(b2p, monkeypatch, tmp_path):
    board_file = _setup_success(
        monkeypatch, b2p, tmp_path, _make_fake_plot_module(success=False, prints="Failed to write")
    )
    result = b2p.export_board_to_pdf({"board_path": str(board_file)})
    assert "Falha" in result
    assert "Failed to write" in result


def test_export_plot_raises(b2p, monkeypatch, tmp_path):
    def raising_plot_pdfs(board, **kwargs):
        raise ValueError("boom")

    plot_module = types.SimpleNamespace(plot_pdfs=raising_plot_pdfs)
    board_file = _setup_success(monkeypatch, b2p, tmp_path, plot_module)
    result = b2p.export_board_to_pdf({"board_path": str(board_file)})
    assert "Erro ao exportar" in result
    assert "boom" in result


def test_export_passes_through_overrides(b2p, monkeypatch, tmp_path):
    captured: dict = {}
    board_file = _setup_success(
        monkeypatch,
        b2p,
        tmp_path,
        _make_fake_plot_module(success=True, capture_kwargs=captured),
    )
    result = b2p.export_board_to_pdf(
        {
            "board_path": str(board_file),
            "colorize_lib": "pymupdf",
            "merge_lib": "pypdf",
            "output_file_name": "custom_output.pdf",
        }
    )
    assert "sucesso" in result
    assert captured["colorize_lib"] == "pymupdf"
    assert captured["merge_lib"] == "pypdf"
    assert captured["assembly_file_output"] == "custom_output.pdf"


def test_export_restores_cwd_after_plot(b2p, monkeypatch, tmp_path):
    import os

    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()

    def plot_pdfs(board, **kwargs):
        os.chdir(other_dir)  # mimic plot.py's real os.chdir() side effect
        return True

    plot_module = types.SimpleNamespace(plot_pdfs=plot_pdfs)
    board_file = _setup_success(monkeypatch, b2p, tmp_path, plot_module)

    original_cwd = os.getcwd()
    try:
        result = b2p.export_board_to_pdf({"board_path": str(board_file)})
        assert "sucesso" in result
        assert os.getcwd() == original_cwd
    finally:
        os.chdir(original_cwd)


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_board2pdf_tools(b2p):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    b2p.register_board2pdf_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert names == {"export_board_to_pdf"}
    assert registry.get("export_board_to_pdf").read_only is False


def test_register_board2pdf_tools_coexists_with_other_registries(b2p):
    """Registering alongside kicad_tools/kicad_write_tools must not
    collide (distinct tool names) — mirrors the equivalent coexistence
    check in test_libforge_tools.py."""
    sys.modules.pop("actions.kicad_tools", None)
    sys.modules.pop("actions.kicad_write_tools", None)
    import actions.kicad_tools as kt
    import actions.kicad_write_tools as kwt
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kt.register_kicad_tools(registry)
    kwt.register_kicad_write_tools(registry)
    b2p.register_board2pdf_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert "export_board_to_pdf" in names
    # 5 (kicad_tools) + 10 (kicad_write_tools) + 1 (board2pdf_tools), all unique
    assert len(names) == 16
