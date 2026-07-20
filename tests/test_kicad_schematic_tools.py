"""Tests for actions/kicad_schematic_tools.py.

These run WITHOUT a real KiCad installation and WITHOUT a real LibForge/
kiutils install: `pcbnew` is faked via `sys.modules` monkeypatching
(mirroring test_kicad_tools.py), and the sibling-plugin loader (`_load`)
plus every `kiutils` submodule it bootstraps are faked (mirroring
test_libforge_tools.py's `_install_fake_kiutils`). The module itself must
import cleanly with neither present at all (asserted first).
"""

from __future__ import annotations

import sys
import types

import pytest


def test_module_imports_without_pcbnew_or_kiutils():
    sys.modules.pop("pcbnew", None)
    sys.modules.pop("actions.kicad_schematic_tools", None)
    import actions.kicad_schematic_tools as kst  # noqa: F401

    assert hasattr(kst, "register_schematic_tools")
    for name in (
        "list_schematic_wires",
        "add_schematic_wire",
        "delete_schematic_wire",
        "list_schematic_labels",
        "add_schematic_label",
        "delete_schematic_label",
        "list_schematic_symbols",
        "add_schematic_symbol",
        "delete_schematic_symbol",
    ):
        assert hasattr(kst, name)


# --------------------------------------------------------------------------- #
# sibling-not-installed discovery
# --------------------------------------------------------------------------- #
def test_find_sibling_plugins_dir_not_found(monkeypatch, tmp_path):
    sys.modules.pop("actions.kicad_schematic_tools", None)
    import actions.kicad_schematic_tools as kst

    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))
    with pytest.raises(kst.SiblingPluginNotFoundError):
        kst._find_sibling_plugins_dir()


# --------------------------------------------------------------------------- #
# Fake kiutils object model
# --------------------------------------------------------------------------- #
class FakePosition:
    def __init__(self, X=0.0, Y=0.0, angle=None):
        self.X = X
        self.Y = Y
        self.angle = angle


class FakeProperty:
    def __init__(self, key="", value="", id=None, position=None):
        self.key = key
        self.value = value
        self.id = id
        self.position = position


class FakeConnection:
    def __init__(self, type="wire", points=None, uuid=None):
        self.type = type
        self.points = points or []
        self.uuid = uuid


class FakeLocalLabel:
    def __init__(self, text="", position=None, uuid=None):
        self.text = text
        self.position = position
        self.uuid = uuid


class FakeGlobalLabel:
    def __init__(self, text="", position=None, shape="input", uuid=None):
        self.text = text
        self.position = position
        self.shape = shape
        self.uuid = uuid


class FakeSchematicSymbol:
    def __init__(
        self,
        position=None,
        uuid=None,
        inBom=False,
        onBoard=False,
        properties=None,
        pins=None,
        instances=None,
    ):
        self.position = position
        self.uuid = uuid
        self.inBom = inBom
        self.onBoard = onBoard
        self.properties = properties or []
        self.pins = pins or {}
        self.instances = instances or []
        self._libId = None

    @property
    def libId(self):
        return self._libId

    @libId.setter
    def libId(self, value):
        self._libId = value


class FakeSymbolProjectPath:
    def __init__(self, sheetInstancePath="", reference="", unit=1):
        self.sheetInstancePath = sheetInstancePath
        self.reference = reference
        self.unit = unit


class FakeSymbolProjectInstance:
    def __init__(self, name="", paths=None):
        self.name = name
        self.paths = paths or []


class FakeSchematic:
    """Stand-in for kiutils.schematic.Schematic - a plain mutable bag of
    lists, exactly like the real dataclass, plus a to_file() that just
    records the write instead of touching disk."""

    def __init__(self):
        self.graphicalItems = []
        self.labels = []
        self.globalLabels = []
        self.schematicSymbols = []
        self.libSymbols = []
        self.saved_paths = []

    def to_file(self, path=None):
        self.saved_paths.append(path)


class FakeSymbolPin:
    def __init__(self, number):
        self.number = number


class FakeSymbolDef:
    def __init__(self, entryName, libId=None, pins=None, units=None):
        self.entryName = entryName
        self.libId = libId or entryName
        self.pins = pins or []
        self.units = units or []


class FakeSymbolLib:
    def __init__(self, symbols=None):
        self.symbols = symbols or []


def _install_fake_kiutils(monkeypatch, preset_schematic: FakeSchematic, symbols_by_path: dict):
    kiutils_pkg = types.ModuleType("kiutils")
    kiutils_items_pkg = types.ModuleType("kiutils.items")

    schematic_mod = types.SimpleNamespace()

    class FakeSchematicClass:
        @classmethod
        def from_file(cls, path):
            preset_schematic.filePath = path
            return preset_schematic

    schematic_mod.Schematic = FakeSchematicClass

    common_mod = types.SimpleNamespace(Position=FakePosition, Property=FakeProperty)
    schitems_mod = types.SimpleNamespace(
        Connection=FakeConnection,
        LocalLabel=FakeLocalLabel,
        GlobalLabel=FakeGlobalLabel,
        SchematicSymbol=FakeSchematicSymbol,
        SymbolProjectInstance=FakeSymbolProjectInstance,
        SymbolProjectPath=FakeSymbolProjectPath,
    )

    symbol_mod = types.SimpleNamespace()

    class FakeSymbolLibClass:
        @classmethod
        def from_file(cls, path):
            return FakeSymbolLib(symbols=symbols_by_path.get(str(path), []))

    symbol_mod.SymbolLib = FakeSymbolLibClass

    monkeypatch.setitem(sys.modules, "kiutils", kiutils_pkg)
    monkeypatch.setitem(sys.modules, "kiutils.items", kiutils_items_pkg)
    monkeypatch.setitem(sys.modules, "kiutils.schematic", schematic_mod)
    monkeypatch.setitem(sys.modules, "kiutils.items.common", common_mod)
    monkeypatch.setitem(sys.modules, "kiutils.items.schitems", schitems_mod)
    monkeypatch.setitem(sys.modules, "kiutils.symbol", symbol_mod)


def _make_fake_board(sch_path, tmp_path):
    board_path = tmp_path / "project.kicad_pcb"
    board_path.write_text("(kicad_pcb)", encoding="utf-8")
    board = types.SimpleNamespace()
    board.GetFileName = lambda: str(board_path)
    return board


@pytest.fixture
def kst(monkeypatch):
    sys.modules.pop("actions.kicad_schematic_tools", None)
    import actions.kicad_schematic_tools as module

    # By default the sibling plugin loader "succeeds" (returns a harmless
    # stub) - individual tests override this to simulate not-installed.
    monkeypatch.setattr(module, "_load", lambda submodule: types.SimpleNamespace())

    return module


@pytest.fixture
def schematic_env(kst, monkeypatch, tmp_path):
    """Wires up: a fake pcbnew board pointing at a real (empty) .kicad_sch
    file on disk (needed for the os.path.isfile check), a fresh FakeSchematic
    that Schematic.from_file() always returns, and fake kiutils submodules."""
    sch_path = tmp_path / "project.kicad_sch"
    sch_path.write_text("(kicad_sch)", encoding="utf-8")

    board = _make_fake_board(sch_path, tmp_path)
    fake_pcbnew = types.SimpleNamespace(GetBoard=lambda: board)
    monkeypatch.setitem(sys.modules, "pcbnew", fake_pcbnew)

    preset_schematic = FakeSchematic()
    symbols_by_path = {}
    _install_fake_kiutils(monkeypatch, preset_schematic, symbols_by_path)

    return types.SimpleNamespace(
        kst=kst,
        board=board,
        sch_path=str(sch_path),
        schematic=preset_schematic,
        symbols_by_path=symbols_by_path,
    )


# --------------------------------------------------------------------------- #
# _get_schematic_path
# --------------------------------------------------------------------------- #
def test_get_schematic_path_no_board(kst, monkeypatch):
    fake_pcbnew = types.SimpleNamespace(GetBoard=lambda: None)
    monkeypatch.setitem(sys.modules, "pcbnew", fake_pcbnew)
    with pytest.raises(RuntimeError):
        kst._get_schematic_path()


def test_get_schematic_path_missing_schematic(kst, monkeypatch, tmp_path):
    board_path = tmp_path / "project.kicad_pcb"
    board_path.write_text("(kicad_pcb)", encoding="utf-8")
    board = types.SimpleNamespace(GetFileName=lambda: str(board_path))
    fake_pcbnew = types.SimpleNamespace(GetBoard=lambda: board)
    monkeypatch.setitem(sys.modules, "pcbnew", fake_pcbnew)
    with pytest.raises(RuntimeError):
        kst._get_schematic_path()


# --------------------------------------------------------------------------- #
# sibling / LibForge not installed
# --------------------------------------------------------------------------- #
def test_list_schematic_wires_sibling_not_installed(schematic_env, monkeypatch):
    def raise_not_found(submodule):
        raise schematic_env.kst.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(schematic_env.kst, "_load", raise_not_found)
    with pytest.raises(RuntimeError) as excinfo:
        schematic_env.kst.list_schematic_wires({})
    assert "LibForge" in str(excinfo.value)
    assert "não está instalado" in str(excinfo.value)


def test_add_schematic_wire_sibling_not_installed(schematic_env, monkeypatch):
    def raise_not_found(submodule):
        raise schematic_env.kst.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(schematic_env.kst, "_load", raise_not_found)
    with pytest.raises(RuntimeError) as excinfo:
        schematic_env.kst.add_schematic_wire(
            {"start_x_mm": 0, "start_y_mm": 0, "end_x_mm": 10, "end_y_mm": 0}
        )
    assert "não está instalado" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# wires
# --------------------------------------------------------------------------- #
def test_list_schematic_wires_empty(schematic_env):
    result = schematic_env.kst.list_schematic_wires({})
    assert "nenhum fio" in result


def test_list_schematic_wires_skips_bus_and_polyline(schematic_env):
    schematic_env.schematic.graphicalItems.append(
        FakeConnection(type="wire", points=[FakePosition(1, 2), FakePosition(3, 4)], uuid="u1")
    )
    schematic_env.schematic.graphicalItems.append(
        FakeConnection(type="bus", points=[FakePosition(0, 0), FakePosition(1, 1)], uuid="u2")
    )
    # A bare object with no 'type' attribute at all stands in for PolyLine.
    schematic_env.schematic.graphicalItems.append(types.SimpleNamespace(points=[]))

    result = schematic_env.kst.list_schematic_wires({})
    assert "u1" in result
    assert "u2" not in result
    assert result.count("\n") == 1  # header + exactly one wire row


def test_add_schematic_wire_success(schematic_env):
    result = schematic_env.kst.add_schematic_wire(
        {"start_x_mm": 1.0, "start_y_mm": 2.0, "end_x_mm": 3.0, "end_y_mm": 4.0}
    )
    assert "1.000" in result and "4.000" in result
    assert "Feche e reabra" in result
    assert len(schematic_env.schematic.graphicalItems) == 1
    wire = schematic_env.schematic.graphicalItems[0]
    assert wire.type == "wire"
    assert wire.points[0].X == 1.0
    assert wire.points[1].Y == 4.0
    assert schematic_env.schematic.saved_paths == [schematic_env.sch_path]


def test_add_schematic_wire_missing_args(schematic_env):
    with pytest.raises(RuntimeError):
        schematic_env.kst.add_schematic_wire({"start_x_mm": 1.0})


def test_delete_schematic_wire_by_uuid(schematic_env):
    schematic_env.schematic.graphicalItems.append(
        FakeConnection(type="wire", points=[FakePosition(0, 0), FakePosition(1, 1)], uuid="keep")
    )
    schematic_env.schematic.graphicalItems.append(
        FakeConnection(type="wire", points=[FakePosition(2, 2), FakePosition(3, 3)], uuid="drop")
    )
    result = schematic_env.kst.delete_schematic_wire({"uuid": "drop"})
    assert "Feche e reabra" in result
    remaining = [w.uuid for w in schematic_env.schematic.graphicalItems]
    assert remaining == ["keep"]


def test_delete_schematic_wire_by_index(schematic_env):
    schematic_env.schematic.graphicalItems.append(
        FakeConnection(type="wire", points=[FakePosition(0, 0), FakePosition(1, 1)], uuid="first")
    )
    schematic_env.schematic.graphicalItems.append(
        FakeConnection(type="wire", points=[FakePosition(2, 2), FakePosition(3, 3)], uuid="second")
    )
    schematic_env.kst.delete_schematic_wire({"index": 1})
    remaining = [w.uuid for w in schematic_env.schematic.graphicalItems]
    assert remaining == ["second"]


def test_delete_schematic_wire_missing_args(schematic_env):
    with pytest.raises(RuntimeError):
        schematic_env.kst.delete_schematic_wire({})


def test_delete_schematic_wire_bad_uuid(schematic_env):
    with pytest.raises(RuntimeError):
        schematic_env.kst.delete_schematic_wire({"uuid": "does-not-exist"})


def test_delete_schematic_wire_bad_index(schematic_env):
    with pytest.raises(RuntimeError):
        schematic_env.kst.delete_schematic_wire({"index": 5})


# --------------------------------------------------------------------------- #
# labels
# --------------------------------------------------------------------------- #
def test_list_schematic_labels_empty(schematic_env):
    result = schematic_env.kst.list_schematic_labels({})
    assert "nenhuma etiqueta" in result


def test_list_schematic_labels_both_kinds(schematic_env):
    schematic_env.schematic.labels.append(FakeLocalLabel(text="NET1", position=FakePosition(1, 2)))
    schematic_env.schematic.globalLabels.append(
        FakeGlobalLabel(text="VCC", position=FakePosition(3, 4), shape="power_in")
    )
    result = schematic_env.kst.list_schematic_labels({})
    assert "NET1" in result and "local" in result
    assert "VCC" in result and "global" in result


def test_add_schematic_label_local_default(schematic_env):
    result = schematic_env.kst.add_schematic_label({"text": "NET1", "x_mm": 1.0, "y_mm": 2.0})
    assert "Feche e reabra" in result
    assert len(schematic_env.schematic.labels) == 1
    assert schematic_env.schematic.labels[0].text == "NET1"
    assert schematic_env.schematic.globalLabels == []


def test_add_schematic_label_global_with_shape(schematic_env):
    schematic_env.kst.add_schematic_label(
        {"text": "VCC", "x_mm": 1.0, "y_mm": 2.0, "kind": "global", "shape": "output"}
    )
    assert len(schematic_env.schematic.globalLabels) == 1
    label = schematic_env.schematic.globalLabels[0]
    assert label.text == "VCC"
    assert label.shape == "output"


def test_add_schematic_label_missing_text(schematic_env):
    with pytest.raises(RuntimeError):
        schematic_env.kst.add_schematic_label({"x_mm": 1.0, "y_mm": 2.0})


def test_add_schematic_label_invalid_kind(schematic_env):
    with pytest.raises(RuntimeError):
        schematic_env.kst.add_schematic_label(
            {"text": "X", "x_mm": 1.0, "y_mm": 2.0, "kind": "nonsense"}
        )


def test_delete_schematic_label_local(schematic_env):
    schematic_env.schematic.labels.append(FakeLocalLabel(text="NET1", position=FakePosition(0, 0)))
    result = schematic_env.kst.delete_schematic_label({"kind": "local", "index": 1})
    assert "Feche e reabra" in result
    assert schematic_env.schematic.labels == []


def test_delete_schematic_label_global(schematic_env):
    schematic_env.schematic.globalLabels.append(
        FakeGlobalLabel(text="VCC", position=FakePosition(0, 0))
    )
    schematic_env.kst.delete_schematic_label({"kind": "global", "index": 1})
    assert schematic_env.schematic.globalLabels == []


def test_delete_schematic_label_missing_args(schematic_env):
    with pytest.raises(RuntimeError):
        schematic_env.kst.delete_schematic_label({"kind": "local"})
    with pytest.raises(RuntimeError):
        schematic_env.kst.delete_schematic_label({"index": 1})


def test_delete_schematic_label_bad_index(schematic_env):
    with pytest.raises(RuntimeError):
        schematic_env.kst.delete_schematic_label({"kind": "local", "index": 99})


# --------------------------------------------------------------------------- #
# symbols
# --------------------------------------------------------------------------- #
def _add_placed_symbol(schematic, reference, value="10k", lib_id="Device:R", x=1.0, y=2.0):
    sym = FakeSchematicSymbol(
        position=FakePosition(x, y),
        uuid=f"uuid-{reference}",
        properties=[
            FakeProperty(key="Reference", value=reference, id=0),
            FakeProperty(key="Value", value=value, id=1),
        ],
    )
    sym.libId = lib_id
    schematic.schematicSymbols.append(sym)
    return sym


def test_list_schematic_symbols_empty(schematic_env):
    result = schematic_env.kst.list_schematic_symbols({})
    assert "nenhum símbolo" in result


def test_list_schematic_symbols_lists_reference_value_libid_position(schematic_env):
    _add_placed_symbol(schematic_env.schematic, "R1", value="10k", lib_id="Device:R")
    result = schematic_env.kst.list_schematic_symbols({})
    assert "R1" in result
    assert "10k" in result
    assert "Device:R" in result


def test_add_schematic_symbol_success(schematic_env, tmp_path):
    lib_path = tmp_path / "MyLib.kicad_sym"
    lib_path.write_text("(kicad_symbol_lib)", encoding="utf-8")

    sym_def = FakeSymbolDef(
        entryName="R_0603",
        libId="R_0603",
        pins=[FakeSymbolPin("1")],
        units=[types.SimpleNamespace(pins=[FakeSymbolPin("2")])],
    )
    schematic_env.symbols_by_path[str(lib_path)] = [sym_def]

    result = schematic_env.kst.add_schematic_symbol(
        {
            "symbol_library_path": str(lib_path),
            "entry_name": "R_0603",
            "reference": "R1",
            "x_mm": 10.0,
            "y_mm": 20.0,
            "value": "10k",
            "footprint": "Resistor_SMD:R_0603_1608Metric",
        }
    )
    assert "Feche e reabra" in result
    assert len(schematic_env.schematic.schematicSymbols) == 1
    placed = schematic_env.schematic.schematicSymbols[0]
    assert placed.libId == "R_0603"
    assert placed.position.X == 10.0
    assert placed.position.Y == 20.0
    assert placed.inBom is True
    assert placed.onBoard is True
    # both direct pins AND unit-nested pins collected, deduped, each with a
    # distinct generated uuid
    assert set(placed.pins.keys()) == {"1", "2"}
    assert placed.pins["1"] != placed.pins["2"]
    # symbol definition copied into libSymbols exactly once
    assert schematic_env.schematic.libSymbols == [sym_def]
    prop_values = {p.key: p.value for p in placed.properties}
    assert prop_values["Reference"] == "R1"
    assert prop_values["Value"] == "10k"
    assert prop_values["Footprint"] == "Resistor_SMD:R_0603_1608Metric"


def test_add_schematic_symbol_does_not_duplicate_lib_symbols_entry(schematic_env, tmp_path):
    lib_path = tmp_path / "MyLib.kicad_sym"
    lib_path.write_text("(kicad_symbol_lib)", encoding="utf-8")
    sym_def = FakeSymbolDef(entryName="R_0603", libId="R_0603", pins=[FakeSymbolPin("1")])
    schematic_env.symbols_by_path[str(lib_path)] = [sym_def]
    schematic_env.schematic.libSymbols.append(sym_def)  # already present

    schematic_env.kst.add_schematic_symbol(
        {
            "symbol_library_path": str(lib_path),
            "entry_name": "R_0603",
            "reference": "R2",
            "x_mm": 0.0,
            "y_mm": 0.0,
        }
    )
    assert schematic_env.schematic.libSymbols == [sym_def]


def test_add_schematic_symbol_rejects_duplicate_reference(schematic_env, tmp_path):
    _add_placed_symbol(schematic_env.schematic, "R1")
    lib_path = tmp_path / "MyLib.kicad_sym"
    lib_path.write_text("(kicad_symbol_lib)", encoding="utf-8")
    schematic_env.symbols_by_path[str(lib_path)] = [
        FakeSymbolDef(entryName="R_0603", pins=[FakeSymbolPin("1")])
    ]

    with pytest.raises(RuntimeError) as excinfo:
        schematic_env.kst.add_schematic_symbol(
            {
                "symbol_library_path": str(lib_path),
                "entry_name": "R_0603",
                "reference": "R1",
                "x_mm": 0.0,
                "y_mm": 0.0,
            }
        )
    assert "R1" in str(excinfo.value)


def test_add_schematic_symbol_missing_args(schematic_env):
    with pytest.raises(RuntimeError):
        schematic_env.kst.add_schematic_symbol({"entry_name": "X", "reference": "R1", "x_mm": 0, "y_mm": 0})
    with pytest.raises(RuntimeError):
        schematic_env.kst.add_schematic_symbol(
            {"symbol_library_path": "x.kicad_sym", "reference": "R1", "x_mm": 0, "y_mm": 0}
        )
    with pytest.raises(RuntimeError):
        schematic_env.kst.add_schematic_symbol(
            {"symbol_library_path": "x.kicad_sym", "entry_name": "X", "x_mm": 0, "y_mm": 0}
        )


def test_add_schematic_symbol_library_not_found(schematic_env):
    with pytest.raises(RuntimeError):
        schematic_env.kst.add_schematic_symbol(
            {
                "symbol_library_path": "C:/does/not/exist.kicad_sym",
                "entry_name": "X",
                "reference": "R1",
                "x_mm": 0,
                "y_mm": 0,
            }
        )


def test_add_schematic_symbol_entry_not_found_in_library(schematic_env, tmp_path):
    lib_path = tmp_path / "MyLib.kicad_sym"
    lib_path.write_text("(kicad_symbol_lib)", encoding="utf-8")
    schematic_env.symbols_by_path[str(lib_path)] = [FakeSymbolDef(entryName="OTHER")]

    with pytest.raises(RuntimeError):
        schematic_env.kst.add_schematic_symbol(
            {
                "symbol_library_path": str(lib_path),
                "entry_name": "R_0603",
                "reference": "R1",
                "x_mm": 0,
                "y_mm": 0,
            }
        )


def test_delete_schematic_symbol_success(schematic_env):
    _add_placed_symbol(schematic_env.schematic, "R1")
    _add_placed_symbol(schematic_env.schematic, "R2")
    result = schematic_env.kst.delete_schematic_symbol({"reference": "R1"})
    assert "Feche e reabra" in result
    remaining = [
        p.value
        for sym in schematic_env.schematic.schematicSymbols
        for p in sym.properties
        if p.key == "Reference"
    ]
    assert remaining == ["R2"]


def test_delete_schematic_symbol_missing_reference(schematic_env):
    with pytest.raises(RuntimeError):
        schematic_env.kst.delete_schematic_symbol({})


def test_delete_schematic_symbol_not_found(schematic_env):
    with pytest.raises(RuntimeError):
        schematic_env.kst.delete_schematic_symbol({"reference": "R99"})


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_schematic_tools(kst):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kst.register_schematic_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert names == {
        "list_schematic_wires",
        "add_schematic_wire",
        "delete_schematic_wire",
        "list_schematic_labels",
        "add_schematic_label",
        "delete_schematic_label",
        "list_schematic_symbols",
        "add_schematic_symbol",
        "delete_schematic_symbol",
    }

    read_only_names = {n for n in names if registry.get(n).read_only}
    write_names = {n for n in names if not registry.get(n).read_only}
    assert read_only_names == {
        "list_schematic_wires",
        "list_schematic_labels",
        "list_schematic_symbols",
    }
    assert write_names == {
        "add_schematic_wire",
        "delete_schematic_wire",
        "add_schematic_label",
        "delete_schematic_label",
        "add_schematic_symbol",
        "delete_schematic_symbol",
    }


def test_register_schematic_tools_coexists_with_other_registries():
    sys.modules.pop("actions.kicad_tools", None)
    sys.modules.pop("actions.kicad_write_tools", None)
    sys.modules.pop("actions.kicad_schematic_tools", None)
    import actions.kicad_schematic_tools as kst
    import actions.kicad_tools as kt
    import actions.kicad_write_tools as kwt
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kt.register_kicad_tools(registry)
    kwt.register_kicad_write_tools(registry)
    kst.register_schematic_tools(registry)

    names = {spec.name for spec in registry.specs()}
    # 5 (kicad_tools) + 10 (kicad_write_tools) + 9 (kicad_schematic_tools), unique
    assert len(names) == 24
    assert "list_schematic_wires" in names
    assert "add_schematic_symbol" in names
