"""Tests for actions/coil_generators_tools.py.

Run WITHOUT a real Coil Generators install and WITHOUT real pcbnew: the
sibling-plugin loader (`_load`, `find_pcm_plugin_dir`) and `pcbnew` are
faked via monkeypatch/`sys.modules`, mirroring test_kicad_parasitics_tools.py.
"""

from __future__ import annotations

import sys
import types

import pytest


def test_module_imports_without_sibling_or_pcbnew():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.coil_generators_tools", None)
    import actions.coil_generators_tools as cgt  # noqa: F401

    assert hasattr(cgt, "register_coil_generators_tools")
    assert hasattr(cgt, "generate_coil_wizard_footprint")


@pytest.fixture
def cgt(monkeypatch):
    sys.modules.pop("actions.coil_generators_tools", None)
    import actions.coil_generators_tools as module

    return module


def test_load_uses_correct_pcm_identifier(cgt, monkeypatch):
    seen = {}

    def fake_find_pcm_plugin_dir(identifier):
        seen["identifier"] = identifier
        raise cgt.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(cgt, "find_pcm_plugin_dir", fake_find_pcm_plugin_dir)

    with pytest.raises(cgt.SiblingPluginNotFoundError):
        cgt._load("coil_generator")

    assert seen["identifier"] == (
        "com_github_SK-Electronics-Consulting_kicad-coil-generators"
    )


# --------------------------------------------------------------------------- #
# fake pcbnew / board plumbing
# --------------------------------------------------------------------------- #
class _FakeFootprint:
    def __init__(self):
        self.reference = None
        self.position = None
        self.orientation = None

    def SetReference(self, ref):
        self.reference = ref

    def SetPosition(self, pos):
        self.position = pos

    def SetOrientationDegrees(self, deg):
        self.orientation = deg


class _FakeBoard:
    def __init__(self, existing_reference=None):
        self.added = []
        self._existing_reference = existing_reference

    def FindFootprintByReference(self, reference):
        if reference == self._existing_reference:
            return object()
        return None

    def Add(self, footprint):
        self.added.append(footprint)


@pytest.fixture
def fake_pcbnew(monkeypatch, cgt):
    board = _FakeBoard()
    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board
    fake_module.VECTOR2I = lambda x, y: (x, y)
    fake_module.FromMM = lambda mm: mm * 1_000_000
    fake_module.Refresh = lambda: None

    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)
    return cgt, fake_module, board


def _install_fake_sibling(cgt, monkeypatch, modules: dict):
    def fake_load(submodule):
        try:
            return modules[submodule]
        except KeyError:
            raise ImportError(f"no fake module registered for {submodule!r}")

    monkeypatch.setattr(cgt, "_load", fake_load)


class _FakeParam:
    def __init__(self, page, name, units, default):
        self.page = page
        self.name = name
        self.units = units
        self.default = default
        self.set_calls = []

    def SetValue(self, value):
        self.set_calls.append(value)


class _FakeWizard:
    def __init__(self):
        self.params = [
            _FakeParam("Coil specs", "Total Turns", "integer", 15),
            _FakeParam("Install Info", "Inside Diameter, Radius", "mm", 30),
        ]
        self.module = _FakeFootprint()
        self.buildmessages = ""
        self._errors = False

    def BuildFootprint(self):
        pass

    def AnyErrors(self):
        return self._errors


def _make_fake_coil_generator_module(wizard_factory):
    module = types.SimpleNamespace()
    module.CoilGeneratorID2L = wizard_factory
    module.CoilGenerator1L1T = wizard_factory
    return module


# --------------------------------------------------------------------------- #
# generate_coil_wizard_footprint
# --------------------------------------------------------------------------- #
def test_missing_coil_type(cgt):
    with pytest.raises(RuntimeError):
        cgt.generate_coil_wizard_footprint(
            {"reference": "L1", "x_mm": 1, "y_mm": 1}
        )


def test_invalid_coil_type(cgt):
    with pytest.raises(RuntimeError):
        cgt.generate_coil_wizard_footprint(
            {"coil_type": "not_real", "reference": "L1", "x_mm": 1, "y_mm": 1}
        )


def test_missing_reference(cgt):
    with pytest.raises(RuntimeError):
        cgt.generate_coil_wizard_footprint(
            {"coil_type": "dual_layer_id", "x_mm": 1, "y_mm": 1}
        )


def test_missing_coordinates(cgt):
    with pytest.raises(RuntimeError):
        cgt.generate_coil_wizard_footprint(
            {"coil_type": "dual_layer_id", "reference": "L1"}
        )


def test_sibling_not_installed(cgt, fake_pcbnew, monkeypatch):
    def raise_not_found(submodule):
        raise cgt.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(cgt, "_load", raise_not_found)
    result = cgt.generate_coil_wizard_footprint(
        {"coil_type": "dual_layer_id", "reference": "L1", "x_mm": 1, "y_mm": 1}
    )
    assert "não está instalado" in result


def test_reference_already_exists(cgt, fake_pcbnew):
    _cgt, _pcbnew_mod, board = fake_pcbnew
    board._existing_reference = "L1"
    with pytest.raises(RuntimeError):
        cgt.generate_coil_wizard_footprint(
            {"coil_type": "dual_layer_id", "reference": "L1", "x_mm": 1, "y_mm": 1}
        )


def test_happy_path_dual_layer(cgt, fake_pcbnew, monkeypatch):
    _cgt, pcbnew_mod, board = fake_pcbnew
    wizard = _FakeWizard()
    module = _make_fake_coil_generator_module(lambda: wizard)

    def fake_load(submodule):
        assert submodule == "coil_generator"
        return module

    monkeypatch.setattr(cgt, "_load", fake_load)

    result = cgt.generate_coil_wizard_footprint(
        {
            "coil_type": "dual_layer_id",
            "reference": "L1",
            "x_mm": 10,
            "y_mm": 20,
            "rotation_deg": 45,
            "parameters": {"Coil specs": {"Total Turns": 8}},
        }
    )

    assert "L1" in result
    assert wizard.params[0].set_calls == ["8"]
    assert board.added == [wizard.module]
    assert wizard.module.reference == "L1"
    assert wizard.module.orientation == 45


def test_unknown_parameter(cgt, fake_pcbnew, monkeypatch):
    _cgt, pcbnew_mod, board = fake_pcbnew
    wizard = _FakeWizard()
    module = _make_fake_coil_generator_module(lambda: wizard)
    monkeypatch.setattr(cgt, "_load", lambda submodule: module)

    with pytest.raises(RuntimeError):
        cgt.generate_coil_wizard_footprint(
            {
                "coil_type": "dual_layer_id",
                "reference": "L1",
                "x_mm": 1,
                "y_mm": 1,
                "parameters": {"Coil specs": {"Not A Real Param": 1}},
            }
        )


def test_build_errors_raise(cgt, fake_pcbnew, monkeypatch):
    _cgt, pcbnew_mod, board = fake_pcbnew
    wizard = _FakeWizard()
    wizard._errors = True
    wizard.buildmessages = "bad params"
    module = _make_fake_coil_generator_module(lambda: wizard)
    monkeypatch.setattr(cgt, "_load", lambda submodule: module)

    with pytest.raises(RuntimeError):
        cgt.generate_coil_wizard_footprint(
            {"coil_type": "dual_layer_id", "reference": "L1", "x_mm": 1, "y_mm": 1}
        )


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_coil_generators_tools(cgt):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    cgt.register_coil_generators_tools(registry)
    defn = registry.get("generate_coil_wizard_footprint")
    assert defn is not None
    assert defn.read_only is False
