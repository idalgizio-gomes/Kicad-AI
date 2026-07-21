"""Tests for actions/kicad_parasitics_tools.py.

These run WITHOUT a real KiCad-Parasitics installation and WITHOUT real
pcbnew: the sibling-plugin loader (`_load`, `find_pcm_plugin_dir`) and
`pcbnew` are faked via monkeypatch/`sys.modules`, mirroring the style
already used in test_libforge_tools.py (sibling loader) and
test_kicad_tools.py (pcbnew). The module itself must import cleanly with
neither present at all (asserted first).
"""

from __future__ import annotations

import sys
import types

import pytest


def test_module_imports_without_kicad_parasitics_or_pcbnew():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.kicad_parasitics_tools", None)
    import actions.kicad_parasitics_tools as kpt  # noqa: F401

    assert hasattr(kpt, "register_kicad_parasitics_tools")
    assert hasattr(kpt, "analyze_pcb_parasitics")


@pytest.fixture
def kpt(monkeypatch):
    sys.modules.pop("actions.kicad_parasitics_tools", None)
    import actions.kicad_parasitics_tools as module

    return module


# --------------------------------------------------------------------------- #
# find_pcm_plugin_dir wiring
# --------------------------------------------------------------------------- #
def test_load_uses_correct_pcm_identifier(kpt, monkeypatch):
    seen = {}

    def fake_find_pcm_plugin_dir(identifier):
        seen["identifier"] = identifier
        raise kpt.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(kpt, "find_pcm_plugin_dir", fake_find_pcm_plugin_dir)

    with pytest.raises(kpt.SiblingPluginNotFoundError):
        kpt._load("Get_PCB_Elements")

    assert seen["identifier"] == "com_github_Steffen-W_KiCad-Parasitics"


# --------------------------------------------------------------------------- #
# fake pcbnew / board plumbing
# --------------------------------------------------------------------------- #
def _make_fake_settings(version="10.0"):
    settings = types.SimpleNamespace()
    settings.GetSettingsVersion = lambda: version
    return settings


def _make_fake_board(file_name="C:/proj/board.kicad_pcb", connectivity="conn-obj"):
    board = types.SimpleNamespace()
    board.GetFileName = lambda: file_name
    board.GetConnectivity = lambda: connectivity
    return board


@pytest.fixture
def fake_pcbnew(monkeypatch, kpt):
    board = _make_fake_board()
    settings = _make_fake_settings()

    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board
    fake_module.GetSettingsManager = lambda: settings

    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)
    return kpt, fake_module, board


def _install_fake_sibling(kpt, monkeypatch, modules: dict):
    def fake_load(submodule):
        try:
            return modules[submodule]
        except KeyError:
            raise ImportError(f"no fake module registered for {submodule!r}")

    monkeypatch.setattr(kpt, "_load", fake_load)


def _make_fake_sibling_modules(item_list, cu_stack, analyze_result, format_text):
    get_pcb_elements_mod = types.SimpleNamespace(
        Get_PCB_Elements=lambda board, connect: (item_list, 1.6e-3)
    )
    connect_nets_mod = types.SimpleNamespace(Connect_Nets=lambda data: data)
    get_pcb_stackup_mod = types.SimpleNamespace(
        Get_PCB_Stackup_fun=lambda path, new_v9=True: cu_stack
    )

    calls = {}

    def fake_analyze(data, cu_stack_arg, element1, element2, frequencies=None):
        calls["data"] = data
        calls["cu_stack"] = cu_stack_arg
        calls["element1"] = element1
        calls["element2"] = element2
        calls["frequencies"] = frequencies
        return analyze_result

    parasitic_mod = types.SimpleNamespace(
        analyze_pcb_parasitic=fake_analyze,
        format_result_message=lambda result, cu_stack_arg, net_tie_info=None: format_text,
    )

    modules = {
        "Get_PCB_Elements": get_pcb_elements_mod,
        "Connect_Nets": connect_nets_mod,
        "Get_PCB_Stackup": get_pcb_stackup_mod,
        "parasitic": parasitic_mod,
    }
    return modules, calls


_VALID_ARGS = {
    "point1_x_mm": 0.0,
    "point1_y_mm": 0.0,
    "point2_x_mm": 10.0,
    "point2_y_mm": 0.0,
}


# --------------------------------------------------------------------------- #
# argument validation
# --------------------------------------------------------------------------- #
def test_missing_required_point_args(kpt):
    with pytest.raises(RuntimeError):
        kpt.analyze_pcb_parasitics({"point1_x_mm": 0.0, "point1_y_mm": 0.0})


def test_invalid_frequencies_hz(kpt, fake_pcbnew):
    with pytest.raises(RuntimeError):
        kpt.analyze_pcb_parasitics(
            {**_VALID_ARGS, "frequencies_hz": "not-a-list-of-numbers"}
        )


def test_empty_frequencies_hz(kpt, fake_pcbnew):
    with pytest.raises(RuntimeError):
        kpt.analyze_pcb_parasitics({**_VALID_ARGS, "frequencies_hz": []})


# --------------------------------------------------------------------------- #
# pcbnew / board plumbing
# --------------------------------------------------------------------------- #
def test_no_board_open(kpt, monkeypatch):
    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: None
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)

    with pytest.raises(RuntimeError):
        kpt.analyze_pcb_parasitics(_VALID_ARGS)


def test_pcbnew_unavailable(kpt, monkeypatch):
    # Real pcbnew is not installed in this test environment (verified: a
    # plain `import pcbnew` raises ModuleNotFoundError) — just make sure it
    # isn't left cached in sys.modules from another test's fake.
    monkeypatch.delitem(sys.modules, "pcbnew", raising=False)

    with pytest.raises(RuntimeError):
        kpt.analyze_pcb_parasitics(_VALID_ARGS)


def test_board_without_file_path(kpt, monkeypatch):
    board = _make_fake_board(file_name="")
    fake_module = types.SimpleNamespace()
    fake_module.GetBoard = lambda: board
    monkeypatch.setitem(sys.modules, "pcbnew", fake_module)

    with pytest.raises(RuntimeError):
        kpt.analyze_pcb_parasitics(_VALID_ARGS)


def test_sibling_not_installed(kpt, fake_pcbnew, monkeypatch):
    def raise_not_found(submodule):
        raise kpt.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(kpt, "_load", raise_not_found)

    result = kpt.analyze_pcb_parasitics(_VALID_ARGS)
    assert "não está instalado" in result


# --------------------------------------------------------------------------- #
# _nearest_element / _element_reference_point
# --------------------------------------------------------------------------- #
def test_nearest_element_empty_data_raises(kpt):
    with pytest.raises(RuntimeError):
        kpt._nearest_element({}, (0.0, 0.0))


def test_nearest_element_too_far_raises(kpt):
    data = {1: {"type": "VIA", "position": (0.0, 0.0)}}
    with pytest.raises(RuntimeError):
        kpt._nearest_element(data, (1.0, 1.0))  # 1m away, way past 50mm


def test_nearest_element_picks_closest_via_or_pad(kpt):
    data = {
        1: {"type": "VIA", "position": (0.0, 0.0)},
        2: {"type": "PAD", "position": (0.01, 0.0)},  # 10mm away
    }
    elem, dist = kpt._nearest_element(data, (0.011, 0.0))
    assert elem["type"] == "PAD"
    assert dist == pytest.approx(0.001)


def test_nearest_element_uses_wire_midpoint(kpt):
    data = {
        1: {"type": "WIRE", "start": (0.0, 0.0), "end": (0.02, 0.0)},
    }
    elem, dist = kpt._nearest_element(data, (0.01, 0.0))
    assert elem["type"] == "WIRE"
    assert dist == pytest.approx(0.0)


def test_element_reference_point_none_for_bare_dict(kpt):
    assert kpt._element_reference_point({"type": "ZONE"}) is None


# --------------------------------------------------------------------------- #
# end-to-end analyze_pcb_parasitics (fully faked sibling + pcbnew)
# --------------------------------------------------------------------------- #
def test_analyze_success_path(kpt, fake_pcbnew, monkeypatch):
    item_list = {
        1: {
            "type": "VIA",
            "position": (0.0, 0.0),
            "layer": [0],
            "net_start": {0: 1},
            "net_end": {0: 1},
        },
        2: {
            "type": "VIA",
            "position": (0.01, 0.0),
            "layer": [0],
            "net_start": {0: 1},
            "net_end": {0: 1},
        },
    }
    cu_stack = {0: {"thickness": 3.5e-5, "name": "F.Cu", "abs_height": 0.0}}
    analyze_result = {"error": None, "resistance_dc": 0.01}
    modules, calls = _make_fake_sibling_modules(
        item_list, cu_stack, analyze_result, "FORMATTED RESULT TEXT"
    )
    _install_fake_sibling(kpt, monkeypatch, modules)

    result = kpt.analyze_pcb_parasitics(_VALID_ARGS)

    assert "FORMATTED RESULT TEXT" in result
    assert "VIA" in result
    # default frequency sweep passed through unchanged
    assert calls["frequencies"] == kpt._DEFAULT_FREQUENCIES_HZ
    assert calls["element1"]["position"] == (0.0, 0.0)
    assert calls["element2"]["position"] == (0.01, 0.0)


def test_analyze_custom_frequencies_passed_through(kpt, fake_pcbnew, monkeypatch):
    item_list = {
        1: {"type": "VIA", "position": (0.0, 0.0), "layer": [0]},
        2: {"type": "VIA", "position": (0.01, 0.0), "layer": [0]},
    }
    cu_stack = {}
    analyze_result = {"error": None}
    modules, calls = _make_fake_sibling_modules(
        item_list, cu_stack, analyze_result, "OK"
    )
    _install_fake_sibling(kpt, monkeypatch, modules)

    kpt.analyze_pcb_parasitics({**_VALID_ARGS, "frequencies_hz": [1e6, 2e6]})

    assert calls["frequencies"] == [1e6, 2e6]


def test_analyze_no_copper_elements_on_board(kpt, fake_pcbnew, monkeypatch):
    modules, _calls = _make_fake_sibling_modules({}, {}, {"error": None}, "OK")
    _install_fake_sibling(kpt, monkeypatch, modules)

    with pytest.raises(RuntimeError):
        kpt.analyze_pcb_parasitics(_VALID_ARGS)


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_kicad_parasitics_tools(kpt):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kpt.register_kicad_parasitics_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert names == {"analyze_pcb_parasitics"}
    assert registry.get("analyze_pcb_parasitics").read_only is True


def test_register_coexists_with_other_registries(kpt):
    sys.modules.pop("actions.kicad_tools", None)
    sys.modules.pop("actions.kicad_write_tools", None)
    import actions.kicad_tools as kt
    import actions.kicad_write_tools as kwt
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kt.register_kicad_tools(registry)
    kwt.register_kicad_write_tools(registry)
    kpt.register_kicad_parasitics_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert "analyze_pcb_parasitics" in names
