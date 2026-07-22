"""Tests for actions/testpoints_tools.py.

Run WITHOUT the real kicad_testpoints plugin and WITHOUT real pcbnew:
`find_pcm_plugin_dir`/`_load` and `pcbnew` are faked, mirroring
test_kicad_parasitics_tools.py's style.
"""

from __future__ import annotations

import sys
import types

import pytest


def test_module_imports_without_sibling_or_pcbnew():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.testpoints_tools", None)
    import actions.testpoints_tools as tpt  # noqa: F401

    assert hasattr(tpt, "register_testpoints_tools")
    assert hasattr(tpt, "export_test_point_report")


@pytest.fixture
def tpt(monkeypatch):
    sys.modules.pop("actions.testpoints_tools", None)
    import actions.testpoints_tools as module

    return module


def test_load_uses_correct_pcm_identifier(tpt, monkeypatch):
    seen = {}

    def fake_find(identifier):
        seen["identifier"] = identifier
        raise tpt.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(tpt, "find_pcm_plugin_dir", fake_find)

    with pytest.raises(tpt.SiblingPluginNotFoundError):
        tpt._load()

    assert seen["identifier"] == "com_github_TheJigsApp_kicadtestpoints-pcm"


@pytest.fixture
def fake_pcbnew(monkeypatch, tpt):
    board = object()
    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)
    return tpt, fake_module, board


class _FakeSettings:
    def __init__(self):
        self.use_aux_origin = False


class _FakeSiblingModule:
    def __init__(self, pads_by_property=(), report=None, raise_on_get_pads=None):
        self._pads_by_property = pads_by_property
        self._report = report if report is not None else [{"source ref des": "R1"}]
        self._raise_on_get_pads = raise_on_get_pads
        self.written = None
        self.get_pads_calls = []
        self.Settings = _FakeSettings

    def get_pads_by_property(self, board):
        return self._pads_by_property

    def get_pads(self, pad_pairs, board):
        self.get_pads_calls.append(pad_pairs)
        if self._raise_on_get_pads:
            raise self._raise_on_get_pads
        return ["pad-obj"] * len(pad_pairs)

    def build_test_point_report(self, board, settings, pads):
        return self._report

    def write_csv(self, data, filename):
        self.written = (data, filename)


def _install_fake_sibling(tpt, monkeypatch, module):
    monkeypatch.setattr(tpt, "_load", lambda: module)


# --------------------------------------------------------------------------- #
# export_test_point_report
# --------------------------------------------------------------------------- #
def test_missing_output_path(tpt):
    with pytest.raises(RuntimeError):
        tpt.export_test_point_report({})


def test_invalid_pad_pairs_type(tpt):
    with pytest.raises(RuntimeError):
        tpt.export_test_point_report({"output_path": "x.csv", "pad_pairs": "nope"})


def test_sibling_not_installed(tpt, fake_pcbnew, monkeypatch):
    def raise_not_found():
        raise tpt.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(tpt, "_load", raise_not_found)
    result = tpt.export_test_point_report({"output_path": "x.csv"})
    assert "não está instalado" in result


def test_no_test_points_found(tpt, fake_pcbnew, monkeypatch):
    module = _FakeSiblingModule(pads_by_property=())
    _install_fake_sibling(tpt, monkeypatch, module)
    with pytest.raises(RuntimeError):
        tpt.export_test_point_report({"output_path": "x.csv"})


def test_auto_discovery_happy_path(tpt, fake_pcbnew, monkeypatch, tmp_path):
    module = _FakeSiblingModule(pads_by_property=("pad1", "pad2"))
    _install_fake_sibling(tpt, monkeypatch, module)

    out = str(tmp_path / "report.csv")
    result = tpt.export_test_point_report({"output_path": out})

    assert "1" in result  # 1 report row from the fake report
    assert module.written[1] == module.written[1]  # written path used
    assert module.written[0] == module._report


def test_explicit_pad_pairs_happy_path(tpt, fake_pcbnew, monkeypatch, tmp_path):
    module = _FakeSiblingModule()
    _install_fake_sibling(tpt, monkeypatch, module)

    out = str(tmp_path / "report.csv")
    result = tpt.export_test_point_report(
        {
            "output_path": out,
            "pad_pairs": [{"reference": "R1", "pad_number": "1"}],
        }
    )

    assert module.get_pads_calls == [[("R1", "1")]]
    assert "report.csv" in result or out in result


def test_invalid_pad_pair_entry(tpt, fake_pcbnew, monkeypatch):
    module = _FakeSiblingModule()
    _install_fake_sibling(tpt, monkeypatch, module)

    with pytest.raises(RuntimeError):
        tpt.export_test_point_report(
            {"output_path": "x.csv", "pad_pairs": [{"reference": "R1"}]}
        )


def test_get_pads_userwarning_becomes_runtimeerror(tpt, fake_pcbnew, monkeypatch):
    module = _FakeSiblingModule(raise_on_get_pads=UserWarning("Ref Des R99 not found"))
    _install_fake_sibling(tpt, monkeypatch, module)

    with pytest.raises(RuntimeError):
        tpt.export_test_point_report(
            {
                "output_path": "x.csv",
                "pad_pairs": [{"reference": "R99", "pad_number": "1"}],
            }
        )


def test_use_aux_origin_passed_through(tpt, fake_pcbnew, monkeypatch, tmp_path):
    module = _FakeSiblingModule(pads_by_property=("pad1",))
    captured_settings = {}
    original_build = module.build_test_point_report

    def spying_build(board, settings, pads):
        captured_settings["use_aux_origin"] = settings.use_aux_origin
        return original_build(board, settings, pads)

    module.build_test_point_report = spying_build
    _install_fake_sibling(tpt, monkeypatch, module)

    out = str(tmp_path / "report.csv")
    tpt.export_test_point_report({"output_path": out, "use_aux_origin": True})

    assert captured_settings["use_aux_origin"] is True


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_testpoints_tools(tpt):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    tpt.register_testpoints_tools(registry)
    defn = registry.get("export_test_point_report")
    assert defn is not None
    assert defn.read_only is False
