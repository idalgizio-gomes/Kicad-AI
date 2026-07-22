"""Tests for actions/cammer_tools.py.

Run WITHOUT the real SparkFun CAMmer plugin and WITHOUT real pcbnew:
`find_pcm_plugin_dir`/`_load` and `pcbnew` are faked, mirroring
test_kicad_parasitics_tools.py's style.
"""

from __future__ import annotations

import sys
import types

import pytest


def test_module_imports_without_sibling_or_pcbnew():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.cammer_tools", None)
    import actions.cammer_tools as ct  # noqa: F401

    assert hasattr(ct, "register_cammer_tools")
    assert hasattr(ct, "generate_gerber_zip")


@pytest.fixture
def ct(monkeypatch):
    sys.modules.pop("actions.cammer_tools", None)
    import actions.cammer_tools as module

    return module


def test_load_uses_correct_pcm_identifier(ct, monkeypatch):
    seen = {}

    def fake_find(identifier):
        seen["identifier"] = identifier
        raise ct.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(ct, "find_pcm_plugin_dir", fake_find)

    with pytest.raises(ct.SiblingPluginNotFoundError):
        ct._load()

    assert seen["identifier"] == "com_github_sparkfun_SparkFunKiCadCAMmer"


@pytest.fixture
def fake_pcbnew(monkeypatch, ct, tmp_path):
    board_path = str(tmp_path / "board.kicad_pcb")
    board = types.SimpleNamespace(GetFileName=lambda: board_path)
    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)
    return ct, fake_module, board, board_path


class _FakeCAMmer:
    def __init__(self, sys_exit=0, report="ok"):
        self._sys_exit = sys_exit
        self._report = report
        self.parsed_argv = None
        self.start_calls = []

    def args_parse(self, argv):
        self.parsed_argv = argv
        return types.SimpleNamespace(
            path=None,
            layers=",".join(
                [a for i, a in enumerate(argv) if argv[i - 1] == "-l"]
            )
            or None,
            edges=None,
        )

    def startCAMmer(self, parsed_args, board=None, logger=None):
        self.start_calls.append((parsed_args, board, logger))
        return self._sys_exit, self._report


def _make_module(cammer):
    module = types.SimpleNamespace()
    module.CAMmer = lambda: cammer
    return module


def _install_fake_sibling(ct, monkeypatch, module):
    monkeypatch.setattr(ct, "_load", lambda: module)


# --------------------------------------------------------------------------- #
# generate_gerber_zip
# --------------------------------------------------------------------------- #
def test_missing_layers_and_edges(ct, fake_pcbnew):
    with pytest.raises(RuntimeError):
        ct.generate_gerber_zip({})


def test_invalid_layers_type(ct, fake_pcbnew):
    with pytest.raises(RuntimeError):
        ct.generate_gerber_zip({"layers": "F.Cu"})


def test_board_not_saved(ct, monkeypatch):
    board = types.SimpleNamespace(GetFileName=lambda: "")
    fake_module = types.SimpleNamespace(GetBoard=lambda: board)
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)

    with pytest.raises(RuntimeError):
        ct.generate_gerber_zip({"layers": ["F.Cu"]})


def test_refuses_when_zip_already_exists(ct, fake_pcbnew):
    _ct, _pcbnew_mod, board, board_path = fake_pcbnew
    zip_path = board_path[: -len(".kicad_pcb")] + ".zip"
    with open(zip_path, "w") as f:
        f.write("x")

    with pytest.raises(RuntimeError):
        ct.generate_gerber_zip({"layers": ["F.Cu"]})


def test_sibling_not_installed(ct, fake_pcbnew, monkeypatch):
    def raise_not_found():
        raise ct.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(ct, "_load", raise_not_found)
    result = ct.generate_gerber_zip({"layers": ["F.Cu"]})
    assert "não está instalado" in result


def test_happy_path_calls_with_board_none(ct, fake_pcbnew, monkeypatch):
    _ct, _pcbnew_mod, board, board_path = fake_pcbnew
    cammer = _FakeCAMmer(sys_exit=0, report="all good")
    _install_fake_sibling(ct, monkeypatch, _make_module(cammer))

    result = ct.generate_gerber_zip({"layers": ["F.Cu", "B.Cu"], "edges": ["Edge.Cuts"]})

    assert "all good" in result
    assert cammer.parsed_argv[0:2] == ["-p", board_path]
    assert "-l" in cammer.parsed_argv
    assert "-e" in cammer.parsed_argv
    # Critical safety property: board is ALWAYS None, never the live board.
    _parsed_args, board_arg, _logger = cammer.start_calls[0]
    assert board_arg is None


def test_warning_exit_code_does_not_raise(ct, fake_pcbnew, monkeypatch):
    cammer = _FakeCAMmer(sys_exit=1, report="warning happened")
    _install_fake_sibling(ct, monkeypatch, _make_module(cammer))

    result = ct.generate_gerber_zip({"layers": ["F.Cu"]})
    assert "warning happened" in result


def test_error_exit_code_raises(ct, fake_pcbnew, monkeypatch):
    cammer = _FakeCAMmer(sys_exit=2, report="bad things")
    _install_fake_sibling(ct, monkeypatch, _make_module(cammer))

    with pytest.raises(RuntimeError):
        ct.generate_gerber_zip({"layers": ["F.Cu"]})


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_cammer_tools(ct):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    ct.register_cammer_tools(registry)
    defn = registry.get("generate_gerber_zip")
    assert defn is not None
    assert defn.read_only is False
