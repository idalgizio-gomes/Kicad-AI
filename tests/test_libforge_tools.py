"""Tests for actions/libforge_tools.py.

These run WITHOUT a real LibForge installation: the sibling-plugin loader
(`_load`) and the `kiutils` submodules it relies on are faked, mirroring
the `sys.modules`/monkeypatch style already used in test_kicad_write_tools.py
and test_claude_code_cli_provider.py. The module itself must import cleanly
with no LibForge/kiutils present at all (asserted first).
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

import pytest


def test_module_imports_without_libforge():
    sys.modules.pop("actions.libforge_tools", None)
    import actions.libforge_tools as lft  # noqa: F401

    assert hasattr(lft, "register_libforge_tools")
    assert hasattr(lft, "scan_library_folder_for_duplicates")
    assert hasattr(lft, "generate_component_symbol")
    assert hasattr(lft, "generate_component_footprint")


@pytest.fixture
def lft(monkeypatch):
    sys.modules.pop("actions.libforge_tools", None)
    import actions.libforge_tools as module

    return module


# --------------------------------------------------------------------------- #
# sibling-not-installed discovery
# --------------------------------------------------------------------------- #
def test_find_sibling_plugins_dir_not_found(lft, tmp_path, monkeypatch):
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))
    with pytest.raises(lft.SiblingPluginNotFoundError):
        lft._find_sibling_plugins_dir()


# --------------------------------------------------------------------------- #
# Fake LibForge modules, wired in through a monkeypatched `_load`
# --------------------------------------------------------------------------- #
class FakeDecision(Enum):
    NEW = auto()
    DUPLICATE = auto()
    AMBIGUOUS = auto()


@dataclass
class FakeMatchResult:
    origin: str
    name: str
    name_match: bool
    property_score: float


@dataclass
class FakeComponentCandidate:
    symbol_path: Path | None = None
    footprint_path: Path | None = None
    model_path: Path | None = None
    raw_properties: dict = field(default_factory=dict)


def _make_fake_matching_module():
    mod = types.SimpleNamespace()
    mod.Decision = FakeDecision
    mod.symbol_signature = lambda lib, entry_name: frozenset()
    mod.footprint_signature = lambda fp: frozenset()
    return mod


def _make_fake_generic_scan_module(candidates):
    mod = types.SimpleNamespace()
    mod.scan_generic_folder = lambda folder: candidates
    return mod


def _make_fake_duplicate_index_module(classify_map):
    """classify_map: {(name, kind): (FakeDecision, [FakeMatchResult, ...])}"""

    class FakeDuplicateIndex:
        def __init__(self, destination_path, official_index=None, source_index_paths=None):
            self.destination_path = destination_path

        def classify_candidate(self, name, kind, candidate_signature=None, **kw):
            return classify_map.get((name, kind), (FakeDecision.NEW, []))

    mod = types.SimpleNamespace()
    mod.DuplicateIndex = FakeDuplicateIndex
    return mod


def _install_fake_kiutils(monkeypatch):
    class FakeSymbol:
        def __init__(self, entry_name):
            self.entryName = entry_name
            self.pins = []

    class FakeSymbolLib:
        def __init__(self):
            self.symbols = []

        def from_file(self, path):
            self.symbols = [FakeSymbol("FAKE_ENTRY")]
            return self

    class FakeFootprint:
        def __init__(self):
            self.pads = []

        def from_file(self, path):
            return self

    symbol_mod = types.SimpleNamespace(SymbolLib=FakeSymbolLib)
    footprint_mod = types.SimpleNamespace(Footprint=FakeFootprint)
    kiutils_pkg = types.ModuleType("kiutils")

    monkeypatch.setitem(sys.modules, "kiutils", kiutils_pkg)
    monkeypatch.setitem(sys.modules, "kiutils.symbol", symbol_mod)
    monkeypatch.setitem(sys.modules, "kiutils.footprint", footprint_mod)


def _install_fake_sibling(lft, monkeypatch, modules: dict):
    def fake_load(submodule):
        try:
            return modules[submodule]
        except KeyError:
            raise ImportError(f"no fake module registered for {submodule!r}")

    monkeypatch.setattr(lft, "_load", fake_load)


# --------------------------------------------------------------------------- #
# scan_library_folder_for_duplicates
# --------------------------------------------------------------------------- #
def test_scan_missing_folder_path(lft):
    with pytest.raises(RuntimeError):
        lft.scan_library_folder_for_duplicates({"destination_library_path": "C:/dest"})


def test_scan_missing_destination_path(lft):
    with pytest.raises(RuntimeError):
        lft.scan_library_folder_for_duplicates({"folder_path": "C:/scan"})


def test_scan_folder_not_found(lft, tmp_path):
    missing = tmp_path / "does-not-exist"
    result = lft.scan_library_folder_for_duplicates(
        {"folder_path": str(missing), "destination_library_path": str(tmp_path)}
    )
    assert str(missing) in result


def test_scan_sibling_not_installed(lft, monkeypatch, tmp_path):
    def raise_not_found(submodule):
        raise lft.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(lft, "_load", raise_not_found)
    result = lft.scan_library_folder_for_duplicates(
        {"folder_path": str(tmp_path), "destination_library_path": str(tmp_path)}
    )
    assert "LibForge" in result
    assert "não está instalado" in result


def test_scan_no_candidates_found(lft, monkeypatch, tmp_path):
    _install_fake_kiutils(monkeypatch)
    _install_fake_sibling(
        lft,
        monkeypatch,
        {
            "KiCadImport.generic_scan": _make_fake_generic_scan_module([]),
            "KiCadImport.duplicate_index": _make_fake_duplicate_index_module({}),
            "KiCadImport.matching": _make_fake_matching_module(),
        },
    )
    result = lft.scan_library_folder_for_duplicates(
        {"folder_path": str(tmp_path), "destination_library_path": str(tmp_path)}
    )
    assert "Nenhum candidato" in result


def test_scan_classifies_candidates(lft, monkeypatch, tmp_path):
    _install_fake_kiutils(monkeypatch)

    candidates = [
        FakeComponentCandidate(
            symbol_path=tmp_path / "A.kicad_sym",
            raw_properties={"matched_stems": ["A"]},
        ),
        FakeComponentCandidate(
            footprint_path=tmp_path / "B.kicad_mod",
            model_path=tmp_path / "B.step",
            raw_properties={"matched_stems": ["B"]},
        ),
    ]
    classify_map = {
        ("A", "symbol"): (FakeDecision.NEW, []),
        ("B", "footprint"): (
            FakeDecision.DUPLICATE,
            [FakeMatchResult(origin="destination", name="B", name_match=True, property_score=0.95)],
        ),
    }
    _install_fake_sibling(
        lft,
        monkeypatch,
        {
            "KiCadImport.generic_scan": _make_fake_generic_scan_module(candidates),
            "KiCadImport.duplicate_index": _make_fake_duplicate_index_module(classify_map),
            "KiCadImport.matching": _make_fake_matching_module(),
        },
    )

    result = lft.scan_library_folder_for_duplicates(
        {"folder_path": str(tmp_path), "destination_library_path": str(tmp_path)}
    )

    assert "[1] A" in result
    assert "NEW" in result
    assert "[2] B" in result
    assert "DUPLICATE" in result
    assert "destination:B" in result
    assert "ausente" in result  # A has no footprint, B has no symbol


def test_scan_truncates_long_candidate_lists(lft, monkeypatch, tmp_path):
    _install_fake_kiutils(monkeypatch)
    candidates = [
        FakeComponentCandidate(
            symbol_path=tmp_path / f"C{i}.kicad_sym",
            raw_properties={"matched_stems": [f"C{i}"]},
        )
        for i in range(lft._MAX_CANDIDATE_LINES + 5)
    ]
    _install_fake_sibling(
        lft,
        monkeypatch,
        {
            "KiCadImport.generic_scan": _make_fake_generic_scan_module(candidates),
            "KiCadImport.duplicate_index": _make_fake_duplicate_index_module({}),
            "KiCadImport.matching": _make_fake_matching_module(),
        },
    )
    result = lft.scan_library_folder_for_duplicates(
        {"folder_path": str(tmp_path), "destination_library_path": str(tmp_path)}
    )
    assert "truncado" in result
    assert f"[{lft._MAX_CANDIDATE_LINES + 1}]" not in result


# --------------------------------------------------------------------------- #
# generate_component_symbol
# --------------------------------------------------------------------------- #
def _make_fake_symbol_generation_module():
    @dataclass
    class FakePinSpec:
        number: str
        name: str
        electrical_type: str = "passive"

        def __post_init__(self):
            if self.electrical_type not in {"passive", "input", "output"}:
                raise ValueError(f"Unknown electrical_type '{self.electrical_type}'")

    @dataclass
    class FakeSymbolGenerationResult:
        entry_name: str
        warnings: list = field(default_factory=list)

    def generate_symbol_file(entry_name, pins, dest_path, reference_prefix="U", footprint="", datasheet="~"):
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text("fake symbol lib")
        warnings = []
        seen = set()
        for p in pins:
            if p.number in seen:
                warnings.append(f"Duplicate pin number '{p.number}'")
            seen.add(p.number)
        return FakeSymbolGenerationResult(entry_name=entry_name, warnings=warnings)

    mod = types.SimpleNamespace()
    mod.PinSpec = FakePinSpec
    mod.generate_symbol_file = generate_symbol_file
    return mod


def test_generate_symbol_missing_args(lft):
    with pytest.raises(RuntimeError):
        lft.generate_component_symbol({"pins": [], "dest_path": "x"})
    with pytest.raises(RuntimeError):
        lft.generate_component_symbol({"entry_name": "X", "dest_path": "y"})
    with pytest.raises(RuntimeError):
        lft.generate_component_symbol({"entry_name": "X", "pins": [{"number": "1", "name": "A"}]})


def test_generate_symbol_not_installed(lft, monkeypatch):
    def raise_not_found(submodule):
        raise lft.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(lft, "_load", raise_not_found)
    result = lft.generate_component_symbol(
        {"entry_name": "X", "pins": [{"number": "1", "name": "A"}], "dest_path": "x.kicad_sym"}
    )
    assert "não está instalado" in result


def test_generate_symbol_invalid_electrical_type(lft, monkeypatch):
    _install_fake_sibling(
        lft, monkeypatch, {"KiCadImport.symbol_generation": _make_fake_symbol_generation_module()}
    )
    with pytest.raises(RuntimeError):
        lft.generate_component_symbol(
            {
                "entry_name": "X",
                "pins": [{"number": "1", "name": "A", "electrical_type": "not-a-real-type"}],
                "dest_path": "x.kicad_sym",
            }
        )


def test_generate_symbol_success(lft, monkeypatch, tmp_path):
    _install_fake_sibling(
        lft, monkeypatch, {"KiCadImport.symbol_generation": _make_fake_symbol_generation_module()}
    )
    dest = tmp_path / "MCP6002.kicad_sym"
    result = lft.generate_component_symbol(
        {
            "entry_name": "MCP6002",
            "pins": [
                {"number": "1", "name": "OUT"},
                {"number": "2", "name": "IN-"},
                {"number": "3", "name": "IN+"},
            ],
            "dest_path": str(dest),
        }
    )
    assert "MCP6002" in result
    assert str(dest) in result
    assert dest.is_file()


def test_generate_symbol_reports_warnings(lft, monkeypatch, tmp_path):
    _install_fake_sibling(
        lft, monkeypatch, {"KiCadImport.symbol_generation": _make_fake_symbol_generation_module()}
    )
    dest = tmp_path / "DUP.kicad_sym"
    result = lft.generate_component_symbol(
        {
            "entry_name": "DUP",
            "pins": [{"number": "1", "name": "A"}, {"number": "1", "name": "B"}],
            "dest_path": str(dest),
        }
    )
    assert "Avisos" in result
    assert "Duplicate pin number" in result


# --------------------------------------------------------------------------- #
# generate_component_footprint
# --------------------------------------------------------------------------- #
def _make_fake_footprint_generation_module():
    @dataclass
    class FakePackageSpec:
        pin_count: int
        pitch: float
        pad_width: float
        pad_height: float
        row_spacing: float
        package_type: str = "dual"

        def __post_init__(self):
            if self.package_type not in ("dual", "quad"):
                raise ValueError(f"Unknown package_type '{self.package_type}'")
            if self.pin_count < 2:
                raise ValueError("pin_count must be at least 2")
            if self.package_type == "quad" and self.pin_count % 4 != 0:
                raise ValueError("quad package_type requires pin_count divisible by 4")

    @dataclass
    class FakeFootprintGenerationResult:
        entry_name: str
        warnings: list = field(default_factory=list)

    def generate_footprint_file(entry_name, spec, dest_path):
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text("fake footprint")
        return FakeFootprintGenerationResult(entry_name=entry_name, warnings=[])

    mod = types.SimpleNamespace()
    mod.PackageSpec = FakePackageSpec
    mod.generate_footprint_file = generate_footprint_file
    return mod


def test_generate_footprint_missing_args(lft):
    with pytest.raises(RuntimeError):
        lft.generate_component_footprint({"dest_path": "x"})
    with pytest.raises(RuntimeError):
        lft.generate_component_footprint(
            {"entry_name": "X", "pin_count": 8, "pitch": 1.27, "pad_width": 0.6, "pad_height": 1.5, "row_spacing": 5.0}
        )


def test_generate_footprint_invalid_numeric_args(lft):
    with pytest.raises(RuntimeError):
        lft.generate_component_footprint(
            {
                "entry_name": "X",
                "dest_path": "x.kicad_mod",
                "pin_count": "not-a-number",
                "pitch": 1.27,
                "pad_width": 0.6,
                "pad_height": 1.5,
                "row_spacing": 5.0,
            }
        )


def test_generate_footprint_not_installed(lft, monkeypatch):
    def raise_not_found(submodule):
        raise lft.SiblingPluginNotFoundError("nope")

    monkeypatch.setattr(lft, "_load", raise_not_found)
    result = lft.generate_component_footprint(
        {
            "entry_name": "X",
            "dest_path": "x.kicad_mod",
            "pin_count": 8,
            "pitch": 1.27,
            "pad_width": 0.6,
            "pad_height": 1.5,
            "row_spacing": 5.0,
        }
    )
    assert "não está instalado" in result


def test_generate_footprint_invalid_package_type(lft, monkeypatch):
    _install_fake_sibling(
        lft, monkeypatch, {"KiCadImport.footprint_generation": _make_fake_footprint_generation_module()}
    )
    result = lft.generate_component_footprint(
        {
            "entry_name": "X",
            "dest_path": "x.kicad_mod",
            "pin_count": 8,
            "pitch": 1.27,
            "pad_width": 0.6,
            "pad_height": 1.5,
            "row_spacing": 5.0,
            "package_type": "triangle",
        }
    )
    assert "Erro ao gerar o footprint" in result


def test_generate_footprint_success(lft, monkeypatch, tmp_path):
    _install_fake_sibling(
        lft, monkeypatch, {"KiCadImport.footprint_generation": _make_fake_footprint_generation_module()}
    )
    dest = tmp_path / "SOIC8.kicad_mod"
    result = lft.generate_component_footprint(
        {
            "entry_name": "SOIC8",
            "dest_path": str(dest),
            "pin_count": 8,
            "pitch": 1.27,
            "pad_width": 0.6,
            "pad_height": 1.5,
            "row_spacing": 5.0,
        }
    )
    assert "SOIC8" in result
    assert str(dest) in result
    assert dest.is_file()


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_libforge_tools(lft):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    lft.register_libforge_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert names == {
        "scan_library_folder_for_duplicates",
        "generate_component_symbol",
        "generate_component_footprint",
    }

    assert registry.get("scan_library_folder_for_duplicates").read_only is True
    assert registry.get("generate_component_symbol").read_only is False
    assert registry.get("generate_component_footprint").read_only is False


def test_register_libforge_tools_coexists_with_other_registries(lft):
    """Registering alongside kicad_tools/kicad_write_tools must not collide
    (distinct tool names) — mirrors the equivalent coexistence check in
    test_kicad_write_tools.py."""
    sys.modules.pop("actions.kicad_tools", None)
    sys.modules.pop("actions.kicad_write_tools", None)
    import actions.kicad_tools as kt
    import actions.kicad_write_tools as kwt
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kt.register_kicad_tools(registry)
    kwt.register_kicad_write_tools(registry)
    lft.register_libforge_tools(registry)

    names = {spec.name for spec in registry.specs()}
    assert "scan_library_folder_for_duplicates" in names
    assert "generate_component_symbol" in names
    assert "generate_component_footprint" in names
    # 5 (kicad_tools) + 10 (kicad_write_tools) + 3 (libforge_tools), all unique
    assert len(names) == 18
