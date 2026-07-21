"""Tests for actions/coil_creator_tools.py.

These run WITHOUT a real Coil Creator installation and WITHOUT real
pcbnew: the sibling-plugin loader (`_load`, `find_pcm_plugin_dir`) and
`pcbnew` are faked via monkeypatch/`sys.modules`, mirroring the style
already used in test_kicad_parasitics_tools.py (sibling loader) and
test_kicad_write_tools.py (pcbnew + FootprintLoad-based add_footprint).
The module itself must import cleanly with neither present at all
(asserted first).
"""

from __future__ import annotations

import sys
import types

import pytest


def test_module_imports_without_coil_creator_or_pcbnew():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.coil_creator_tools", None)
    import actions.coil_creator_tools as cct  # noqa: F401

    assert hasattr(cct, "register_coil_creator_tools")
    assert hasattr(cct, "generate_pcb_coil")


@pytest.fixture
def cct(monkeypatch):
    sys.modules.pop("actions.coil_creator_tools", None)
    import actions.coil_creator_tools as module

    return module


# --------------------------------------------------------------------------- #
# find_pcm_plugin_dir wiring
# --------------------------------------------------------------------------- #
def test_load_uses_correct_pcm_identifier(cct, monkeypatch):
    seen = {}

    def fake_find_pcm_plugin_dir(identifier):
        seen["identifier"] = identifier
        raise cct.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(cct, "find_pcm_plugin_dir", fake_find_pcm_plugin_dir)

    with pytest.raises(cct.SiblingPluginNotFoundError):
        cct._load("lib.coilgenerator")

    assert seen["identifier"] == "com_github_DIaLOGIKa-GmbH_kicad-coil-creator"


# --------------------------------------------------------------------------- #
# _get_safe_name / _default_layer_names (pure helpers)
# --------------------------------------------------------------------------- #
def test_get_safe_name_strips_unsafe_characters(cct):
    assert cct._get_safe_name("My Coil #1 (v2).test_") == "My Coil 1 v2.test_"


def test_get_safe_name_all_unsafe_yields_empty(cct):
    assert cct._get_safe_name("###???") == ""


def test_default_layer_names_single_layer(cct):
    assert cct._default_layer_names(1) == ["F.Cu"]


def test_default_layer_names_multi_layer(cct):
    assert cct._default_layer_names(4) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


# --------------------------------------------------------------------------- #
# fake pcbnew / board plumbing (mirrors test_kicad_write_tools.py's style)
# --------------------------------------------------------------------------- #
class _FakeVector2I:
    def __init__(self, x, y):
        self.x = x
        self.y = y


def _make_fake_footprint():
    fp = types.SimpleNamespace()
    fp._pos = None
    fp.SetPosition = lambda pos: setattr(fp, "_pos", pos)
    fp.GetPosition = lambda: fp._pos
    return fp


@pytest.fixture
def fake_pcbnew(monkeypatch, cct):
    added_footprints = []
    board = types.SimpleNamespace()
    board.Add = lambda item: added_footprints.append(item)

    footprint_load_calls = []

    def fake_footprint_load(library_path, footprint_name):
        footprint_load_calls.append((library_path, footprint_name))
        if footprint_name == "MISSING":
            return None
        return _make_fake_footprint()

    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board
    fake_module.FootprintLoad = fake_footprint_load
    fake_module.VECTOR2I = _FakeVector2I
    fake_module.FromMM = lambda v: v * 1e6
    fake_module.ToMM = lambda v: v / 1e6
    fake_module.Refresh = lambda: None

    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)
    return cct, fake_module, board, added_footprints, footprint_load_calls


_VALID_ARGS = {
    "layer_count": 2,
    "turns_per_layer": 12,
    "trace_width_mm": 0.127,
    "trace_spacing_mm": 0.127,
    "via_diameter_mm": 0.6,
    "via_drill_mm": 0.3,
    "outer_diameter_mm": 12.0,
    "coil_name": "COIL_GENERATOR",
    "x_mm": 10.0,
    "y_mm": 20.0,
}


def _install_fake_sibling(cct, monkeypatch, generate_fn):
    coilgenerator_mod = types.SimpleNamespace(generate=generate_fn)

    def fake_load(submodule):
        assert submodule == "lib.coilgenerator"
        return coilgenerator_mod

    monkeypatch.setattr(cct, "_load", fake_load)
    return coilgenerator_mod


# --------------------------------------------------------------------------- #
# argument validation
# --------------------------------------------------------------------------- #
def test_missing_required_args(cct):
    with pytest.raises(RuntimeError):
        cct.generate_pcb_coil({"layer_count": 1})


def test_missing_coil_name(cct):
    with pytest.raises(RuntimeError):
        cct.generate_pcb_coil({**_VALID_ARGS, "coil_name": ""})


def test_layer_count_below_one(cct, fake_pcbnew):
    with pytest.raises(RuntimeError):
        cct.generate_pcb_coil({**_VALID_ARGS, "layer_count": 0})


def test_turns_per_layer_below_one(cct, fake_pcbnew):
    with pytest.raises(RuntimeError):
        cct.generate_pcb_coil({**_VALID_ARGS, "turns_per_layer": 0})


def test_coil_name_sanitizes_to_empty(cct, fake_pcbnew):
    with pytest.raises(RuntimeError):
        cct.generate_pcb_coil({**_VALID_ARGS, "coil_name": "###???"})


def test_layer_names_too_short(cct, fake_pcbnew):
    with pytest.raises(RuntimeError):
        cct.generate_pcb_coil(
            {**_VALID_ARGS, "layer_count": 3, "layer_names": ["F.Cu", "B.Cu"]}
        )


# --------------------------------------------------------------------------- #
# pcbnew / board plumbing
# --------------------------------------------------------------------------- #
def test_no_board_open(cct, monkeypatch):
    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: None
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)

    with pytest.raises(RuntimeError):
        cct.generate_pcb_coil(_VALID_ARGS)


def test_pcbnew_unavailable(cct, monkeypatch):
    monkeypatch.delitem(sys.modules, "pcbnew", raising=False)

    with pytest.raises(RuntimeError):
        cct.generate_pcb_coil(_VALID_ARGS)


def test_sibling_not_installed(cct, fake_pcbnew, monkeypatch):
    def raise_not_found(submodule):
        raise cct.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(cct, "_load", raise_not_found)

    result = cct.generate_pcb_coil(_VALID_ARGS)
    assert "não está instalado" in result


def test_footprint_load_returns_none(cct, fake_pcbnew, monkeypatch):
    _install_fake_sibling(cct, monkeypatch, lambda *a, **k: "(footprint ...)")
    with pytest.raises(RuntimeError):
        cct.generate_pcb_coil({**_VALID_ARGS, "coil_name": "MISSING"})


def test_generate_error_wrapped_as_runtime_error(cct, fake_pcbnew, monkeypatch):
    def failing_generate(*args, **kwargs):
        raise ZeroDivisionError("boom")

    _install_fake_sibling(cct, monkeypatch, failing_generate)

    with pytest.raises(RuntimeError):
        cct.generate_pcb_coil(_VALID_ARGS)


# --------------------------------------------------------------------------- #
# end-to-end generate_pcb_coil (fully faked sibling + pcbnew)
# --------------------------------------------------------------------------- #
def test_generate_success_path(cct, fake_pcbnew, monkeypatch):
    kpt, fake_module, board, added_footprints, footprint_load_calls = fake_pcbnew

    seen_generate_args = {}

    def fake_generate(
        layer_count,
        wrap_clockwise,
        turns_per_layer,
        trace_width,
        trace_spacing,
        via_diameter,
        via_drill,
        outer_diameter,
        coil_name,
        layer_names,
    ):
        seen_generate_args.update(
            layer_count=layer_count,
            wrap_clockwise=wrap_clockwise,
            turns_per_layer=turns_per_layer,
            coil_name=coil_name,
            layer_names=layer_names,
        )
        return "(footprint FAKE)"

    _install_fake_sibling(cct, monkeypatch, fake_generate)

    result = cct.generate_pcb_coil(_VALID_ARGS)

    assert "COIL_GENERATOR" in result
    assert "10.000" in result and "20.000" in result
    assert "REF**" in result

    # generate() called with the real, unsanitized coil_name and the
    # sibling plugin's own default layer-naming scheme.
    assert seen_generate_args["coil_name"] == "COIL_GENERATOR"
    assert seen_generate_args["layer_names"] == ["F.Cu", "B.Cu"]
    assert seen_generate_args["wrap_clockwise"] is True

    # FootprintLoad was called with the sanitized name, and the library
    # path looks like a throwaway .pretty folder.
    assert len(footprint_load_calls) == 1
    lib_path, footprint_name = footprint_load_calls[0]
    assert footprint_name == "COIL_GENERATOR"
    assert lib_path.endswith(".pretty")

    # The loaded footprint was added to the board and positioned in mm.
    assert len(added_footprints) == 1
    fp = added_footprints[0]
    assert fp.GetPosition().x == pytest.approx(10.0 * 1e6)
    assert fp.GetPosition().y == pytest.approx(20.0 * 1e6)


def test_generate_wrap_clockwise_false_passed_through(cct, fake_pcbnew, monkeypatch):
    seen = {}

    def fake_generate(layer_count, wrap_clockwise, *rest):
        seen["wrap_clockwise"] = wrap_clockwise
        return "(footprint FAKE)"

    _install_fake_sibling(cct, monkeypatch, fake_generate)

    cct.generate_pcb_coil({**_VALID_ARGS, "wrap_clockwise": False})

    assert seen["wrap_clockwise"] is False


def test_generate_custom_layer_names_passed_through(cct, fake_pcbnew, monkeypatch):
    seen = {}

    def fake_generate(layer_count, wrap_clockwise, turns_per_layer, *rest):
        seen["layer_names"] = rest[-1]
        return "(footprint FAKE)"

    _install_fake_sibling(cct, monkeypatch, fake_generate)

    custom_layers = ["F.Cu", "In1.Cu", "B.Cu"]
    cct.generate_pcb_coil(
        {**_VALID_ARGS, "layer_count": 3, "layer_names": custom_layers}
    )

    assert seen["layer_names"] == custom_layers


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_coil_creator_tools(cct):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    cct.register_coil_creator_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert names == {"generate_pcb_coil"}
    assert registry.get("generate_pcb_coil").read_only is False


def test_register_coexists_with_other_registries(cct):
    sys.modules.pop("actions.kicad_tools", None)
    sys.modules.pop("actions.kicad_write_tools", None)
    import actions.kicad_tools as kt
    import actions.kicad_write_tools as kwt
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kt.register_kicad_tools(registry)
    kwt.register_kicad_write_tools(registry)
    cct.register_coil_creator_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert "generate_pcb_coil" in names
