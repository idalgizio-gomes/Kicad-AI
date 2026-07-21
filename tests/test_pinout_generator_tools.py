"""Tests for actions/pinout_generator_tools.py.

These run WITHOUT a real Pinout Generator installation and WITHOUT real
pcbnew: the sibling-plugin loader (`_load`, `find_pcm_plugin_dir`) and
`pcbnew` are faked via monkeypatch/`sys.modules`, mirroring the style
already used in test_kicad_parasitics_tools.py. The module itself must
import cleanly with neither present at all (asserted first).
"""

from __future__ import annotations

import sys
import types

import pytest


def test_module_imports_without_pinout_generator_or_pcbnew():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.pinout_generator_tools", None)
    import actions.pinout_generator_tools as pgt  # noqa: F401

    assert hasattr(pgt, "register_pinout_generator_tools")
    assert hasattr(pgt, "generate_component_pinout")


@pytest.fixture
def pgt(monkeypatch):
    sys.modules.pop("actions.pinout_generator_tools", None)
    import actions.pinout_generator_tools as module

    return module


# --------------------------------------------------------------------------- #
# find_pcm_plugin_dir wiring
# --------------------------------------------------------------------------- #
def test_load_uses_correct_pcm_identifier(pgt, monkeypatch):
    seen = {}

    def fake_find_pcm_plugin_dir(identifier):
        seen["identifier"] = identifier
        raise pgt.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(pgt, "find_pcm_plugin_dir", fake_find_pcm_plugin_dir)

    with pytest.raises(pgt.SiblingPluginNotFoundError):
        pgt._load("pinout_plugin")

    assert seen["identifier"] == "com_github_cgrassin_kicad-pinout-generator"


# --------------------------------------------------------------------------- #
# fake pcbnew / board plumbing
# --------------------------------------------------------------------------- #
def _make_fake_footprint(reference="U1"):
    fp = types.SimpleNamespace()
    fp.GetReference = lambda: reference
    return fp


def _make_fake_board(footprints=None):
    footprints = footprints or {}
    board = types.SimpleNamespace()
    board.FindFootprintByReference = lambda ref: footprints.get(ref)
    return board


@pytest.fixture
def fake_pcbnew(monkeypatch, pgt):
    fp = _make_fake_footprint("U1")
    board = _make_fake_board({"U1": fp})

    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board

    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)
    return pgt, fake_module, board, fp


def _install_fake_sibling(pgt, monkeypatch, modules: dict):
    def fake_load(submodule):
        try:
            return modules[submodule]
        except KeyError:
            raise ImportError(f"no fake module registered for {submodule!r}")

    monkeypatch.setattr(pgt, "_load", fake_load)


class _FakePinoutGenerator:
    """Fakes the real PinoutGenerator's plain formatting methods, capturing
    what each one is called with and what get_pin_name_filter()/
    is_pinname_not_number() report at call time."""

    def __init__(self):
        self.calls = []

    def _record(self, method_name, component):
        self.calls.append(
            {
                "method": method_name,
                "component": component,
                "pin_name_filter": self.get_pin_name_filter(),
                "pinname_not_number": self.is_pinname_not_number(),
            }
        )
        return f"{method_name}:{component.GetReference()}"

    def list_format(self, component):
        return self._record("list_format", component)

    def csv_format(self, component):
        return self._record("csv_format", component)

    def html_format(self, component):
        return self._record("html_format", component)

    def markdown_format(self, component):
        return self._record("markdown_format", component)

    def c_enum_format(self, component):
        return self._record("c_enum_format", component)

    def c_define_format(self, component):
        return self._record("c_define_format", component)

    def python_dict_format(self, component):
        return self._record("python_dict_format", component)

    def wireviz_format(self, component):
        return self._record("wireviz_format", component)

    def xdc_format(self, component):
        return self._record("xdc_format", component)

    def pdc_format(self, component):
        return self._record("pdc_format", component)


def _make_fake_sibling_modules():
    instances = []

    def make_generator():
        gen = _FakePinoutGenerator()
        instances.append(gen)
        return gen

    pinout_plugin_mod = types.SimpleNamespace(PinoutGenerator=make_generator)
    return {"pinout_plugin": pinout_plugin_mod}, instances


_VALID_ARGS = {"reference": "U1", "format": "csv"}


# --------------------------------------------------------------------------- #
# argument validation
# --------------------------------------------------------------------------- #
def test_missing_reference(pgt):
    with pytest.raises(RuntimeError):
        pgt.generate_component_pinout({"format": "csv"})


def test_missing_format(pgt, fake_pcbnew):
    with pytest.raises(RuntimeError):
        pgt.generate_component_pinout({"reference": "U1"})


def test_invalid_format(pgt, fake_pcbnew):
    with pytest.raises(RuntimeError):
        pgt.generate_component_pinout({"reference": "U1", "format": "yaml"})


# --------------------------------------------------------------------------- #
# pcbnew / board plumbing
# --------------------------------------------------------------------------- #
def test_no_board_open(pgt, monkeypatch):
    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: None
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)

    with pytest.raises(RuntimeError):
        pgt.generate_component_pinout(_VALID_ARGS)


def test_pcbnew_unavailable(pgt, monkeypatch):
    monkeypatch.delitem(sys.modules, "pcbnew", raising=False)

    with pytest.raises(RuntimeError):
        pgt.generate_component_pinout(_VALID_ARGS)


def test_footprint_not_found(pgt, fake_pcbnew):
    with pytest.raises(RuntimeError):
        pgt.generate_component_pinout({"reference": "U99", "format": "csv"})


def test_sibling_not_installed(pgt, fake_pcbnew, monkeypatch):
    def raise_not_found(submodule):
        raise pgt.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(pgt, "_load", raise_not_found)

    result = pgt.generate_component_pinout(_VALID_ARGS)
    assert "não está instalado" in result


# --------------------------------------------------------------------------- #
# end-to-end generate_component_pinout (fully faked sibling + pcbnew)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fmt,expected_method",
    [
        ("list", "list_format"),
        ("csv", "csv_format"),
        ("html", "html_format"),
        ("md", "markdown_format"),
        ("c_enum", "c_enum_format"),
        ("c_define", "c_define_format"),
        ("python_dict", "python_dict_format"),
        ("wireviz", "wireviz_format"),
        ("fpga_xdc", "xdc_format"),
        ("fpga_pdc", "pdc_format"),
    ],
)
def test_generate_dispatches_every_format(
    pgt, fake_pcbnew, monkeypatch, fmt, expected_method
):
    modules, instances = _make_fake_sibling_modules()
    _install_fake_sibling(pgt, monkeypatch, modules)

    result = pgt.generate_component_pinout({"reference": "U1", "format": fmt})

    assert result == f"{expected_method}:U1"
    assert instances[0].calls[0]["method"] == expected_method
    assert instances[0].calls[0]["component"].GetReference() == "U1"


def test_generate_default_pin_name_options(pgt, fake_pcbnew, monkeypatch):
    modules, instances = _make_fake_sibling_modules()
    _install_fake_sibling(pgt, monkeypatch, modules)

    pgt.generate_component_pinout(_VALID_ARGS)

    call = instances[0].calls[0]
    assert call["pin_name_filter"] == ""
    assert call["pinname_not_number"] is False


def test_generate_custom_pin_name_options_passed_through(pgt, fake_pcbnew, monkeypatch):
    modules, instances = _make_fake_sibling_modules()
    _install_fake_sibling(pgt, monkeypatch, modules)

    pgt.generate_component_pinout(
        {
            "reference": "U1",
            "format": "c_enum",
            "pin_name_not_number": True,
            "pin_name_filter": "GPIO",
        }
    )

    call = instances[0].calls[0]
    assert call["pin_name_filter"] == "GPIO"
    assert call["pinname_not_number"] is True


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_pinout_generator_tools(pgt):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    pgt.register_pinout_generator_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert names == {"generate_component_pinout"}
    assert registry.get("generate_component_pinout").read_only is True


def test_register_coexists_with_other_registries(pgt):
    sys.modules.pop("actions.kicad_tools", None)
    sys.modules.pop("actions.kicad_write_tools", None)
    import actions.kicad_tools as kt
    import actions.kicad_write_tools as kwt
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kt.register_kicad_tools(registry)
    kwt.register_kicad_write_tools(registry)
    pgt.register_pinout_generator_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert "generate_component_pinout" in names
