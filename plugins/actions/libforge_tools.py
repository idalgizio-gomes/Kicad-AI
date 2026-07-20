"""
Chat tools that expose the sibling LibForge plugin's library-management
utilities — see ``_sibling_plugin.py`` for how its code is reached without a
``plugins``-package name collision, and ``emc_emi_tools.py`` for the
structural template this module follows.

Two capabilities are exposed:

- ``scan_library_folder_for_duplicates`` (read-only): scans a folder for
  loose KiCad symbol/footprint/3D-model files (LibForge's
  ``generic_scan.py``) and classifies each candidate against a destination
  library only (LibForge's ``duplicate_index.py`` / ``matching.py``) as
  NEW/DUPLICATE/AMBIGUOUS. Deliberately does NOT touch the official-library
  or library_sources SQLite caches — those require a slow local index build
  this tool has no business doing on a single chat call; a chat user asking
  "are these duplicates of what's already in my library" wants an answer
  against their OWN destination library, not a multi-minute cache rebuild.

- ``generate_component_symbol`` / ``generate_component_footprint``
  (read-only=False — both WRITE a file to disk): thin wrappers around
  LibForge's ``symbol_generation.py`` / ``footprint_generation.py``, which
  are pure, programmatic generators (a pinout table -> a simple two-sided
  symbol body; a pin-count/pitch/pad-size spec -> a dual-row or quad-perimeter
  footprint). Both modules already only ever PRODUCE a candidate
  symbol/footprint object (or write it to a path the caller supplies) —
  there is no live GUI pin-editor step to route around, so a direct
  programmatic tool call is a faithful integration, not a workaround.
"""

from __future__ import annotations

from pathlib import Path

try:
    from .framework import ActionDefinition, ActionRegistry
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from actions.framework import ActionDefinition, ActionRegistry

try:
    from ..llm_providers.base import ToolSpec
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from llm_providers.base import ToolSpec

try:
    from ._sibling_plugin import SiblingPluginNotFoundError, load_sibling_module
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from actions._sibling_plugin import (  # type: ignore[no-redef]
        SiblingPluginNotFoundError,
        load_sibling_module,
    )

try:
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - fallback for flat/test imports
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


_PACKAGE_NAME = "_sibling_libforge"

# Sibling plugin install path, same reverse-DNS junction convention as this
# plugin itself and as emc_emi_tools.py — resolved relative to KiCad's
# Documents folder at call time, not hardcoded to one KiCad version.
_SIBLING_IDENTIFIER = "com_github_idalgizio-gomes_kicad-libforge"

_MAX_CANDIDATE_LINES = 100


def _find_sibling_plugins_dir() -> Path:
    """Best-effort discovery of the LibForge plugin's ``plugins/`` directory
    across installed KiCad versions (junction target), newest first."""
    import os

    documents = Path(os.path.expanduser("~")) / "Documents" / "KiCad"
    if not documents.is_dir():
        raise SiblingPluginNotFoundError(str(documents))

    candidates = sorted(
        (p for p in documents.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    for version_dir in candidates:
        plugin_dir = version_dir / "3rdparty" / "plugins" / _SIBLING_IDENTIFIER
        if plugin_dir.is_dir():
            return plugin_dir
    raise SiblingPluginNotFoundError(
        f"LibForge plugin not found under {documents}"
    )


def _load(submodule: str):
    plugins_dir = _find_sibling_plugins_dir()
    return load_sibling_module(_PACKAGE_NAME, plugins_dir, submodule)


def _not_installed_message() -> str:
    return _(
        "O plugin LibForge não está instalado nesta máquina — esta "
        "ferramenta precisa dele."
    )


# --------------------------------------------------------------------------- #
# scan_library_folder_for_duplicates
# --------------------------------------------------------------------------- #
def _candidate_name(candidate) -> str:
    stems = (candidate.raw_properties or {}).get("matched_stems") or []
    if stems:
        return stems[0]
    for path in (candidate.symbol_path, candidate.footprint_path, candidate.model_path):
        if path is not None:
            return Path(str(path)).stem
    return "?"


def _signature_for_symbol(matching_mod, symbol_lib_cls, symbol_path):
    """Parse the candidate's own symbol file and build its pin signature via
    LibForge's matching.symbol_signature(), or None if unparsable (in which
    case classify_candidate() falls back to name-only matching)."""
    try:
        lib = symbol_lib_cls().from_file(str(symbol_path))
    except Exception:
        return None
    if not lib.symbols:
        return None
    return matching_mod.symbol_signature(lib, lib.symbols[0].entryName)


def _signature_for_footprint(matching_mod, footprint_cls, footprint_path):
    try:
        fp = footprint_cls().from_file(str(footprint_path))
    except Exception:
        return None
    return matching_mod.footprint_signature(fp)


def _format_decision(decision, matches, decision_enum) -> str:
    label = decision.name
    if decision is decision_enum.NEW or not matches:
        return label
    shown = matches[:3]
    matched = ", ".join(f"{m.origin}:{m.name}" for m in shown)
    if len(matches) > len(shown):
        matched += ", ..."
    return f"{label} ({matched})"


def scan_library_folder_for_duplicates(args: dict) -> str:
    """Scan a folder recursively for loose KiCad symbol/footprint/3D-model
    files and classify each grouped candidate against a destination library
    (NEW/DUPLICATE/AMBIGUOUS), using LibForge's own scanning and
    duplicate-classification code.

    Required args:
        folder_path: str — folder to scan recursively.
        destination_library_path: str — folder containing the destination
            library's *.kicad_sym files and *.pretty footprint folders to
            check candidates against. There is no safe default (it depends
            entirely on the user's own personal library layout), so this is
            required rather than guessed.

    Only the destination library is checked — NOT the official KiCad
    libraries or any configured library_sources — those require slow SQLite
    caches this tool has no business building on a single chat call.
    """
    args = args or {}
    folder_path = args.get("folder_path")
    if not folder_path:
        raise RuntimeError(_("Falta o argumento 'folder_path'."))
    destination_library_path = args.get("destination_library_path")
    if not destination_library_path:
        raise RuntimeError(_("Falta o argumento 'destination_library_path'."))

    folder = Path(folder_path)
    if not folder.is_dir():
        return _("Pasta não encontrada: {path}").format(path=folder_path)

    try:
        generic_scan = _load("generic_scan")
        duplicate_index_mod = _load("duplicate_index")
        matching_mod = _load("matching")
    except SiblingPluginNotFoundError:
        return _not_installed_message()
    except ImportError as exc:
        return _("Erro ao carregar o LibForge: {err}").format(err=exc)

    # kiutils is added to sys.path as a side effect of importing matching.py
    # (see matching.py's own _KIUTILS_SRC sys.path.insert) — safe to import
    # directly now, same convention duplicate_index.py itself relies on.
    try:
        from kiutils.footprint import Footprint
        from kiutils.symbol import SymbolLib
    except ImportError as exc:
        return _("Erro ao carregar o kiutils do LibForge: {err}").format(err=exc)

    candidates = generic_scan.scan_generic_folder(folder)
    if not candidates:
        return _(
            "Nenhum candidato (símbolo/footprint/modelo 3D) encontrado em {path}."
        ).format(path=folder_path)

    index = duplicate_index_mod.DuplicateIndex(Path(destination_library_path))
    Decision = matching_mod.Decision

    truncated = len(candidates) > _MAX_CANDIDATE_LINES
    shown_candidates = candidates[:_MAX_CANDIDATE_LINES]

    lines = [
        _("Análise de {n} candidato(s) em {path} contra {dest}.").format(
            n=len(candidates), path=folder_path, dest=destination_library_path
        ),
        "",
    ]

    for i, candidate in enumerate(shown_candidates, start=1):
        name = _candidate_name(candidate)
        stems = sorted((candidate.raw_properties or {}).get("matched_stems") or [name])
        lines.append(f"[{i}] {name} (stems: {', '.join(stems)})")

        if candidate.symbol_path is not None:
            sig = _signature_for_symbol(matching_mod, SymbolLib, candidate.symbol_path)
            decision, matches = index.classify_candidate(name, "symbol", sig)
            lines.append(f"    {_('símbolo')}: {_format_decision(decision, matches, Decision)}")
        else:
            lines.append(f"    {_('símbolo')}: {_('ausente')}")

        if candidate.footprint_path is not None:
            sig = _signature_for_footprint(matching_mod, Footprint, candidate.footprint_path)
            decision, matches = index.classify_candidate(name, "footprint", sig)
            lines.append(f"    {_('footprint')}: {_format_decision(decision, matches, Decision)}")
        else:
            lines.append(f"    {_('footprint')}: {_('ausente')}")

        if candidate.model_path is not None:
            lines.append(
                f"    {_('modelo 3D')}: {_('presente (verificação de duplicados não suportada para modelos 3D)')}"
            )
        else:
            lines.append(f"    {_('modelo 3D')}: {_('ausente')}")

    if truncated:
        lines.append("")
        lines.append(
            _("... (truncado a {n} candidatos)").format(n=_MAX_CANDIDATE_LINES)
        )

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# generate_component_symbol
# --------------------------------------------------------------------------- #
def generate_component_symbol(args: dict) -> str:
    """Generate a simple two-sided KiCad symbol from a pinout table and
    write it to a .kicad_sym file, via LibForge's symbol_generation.py.

    Required args:
        entry_name: str — the symbol's name in the generated library.
        pins: list of {number: str, name: str, electrical_type?: str} —
            electrical_type defaults to "passive" if omitted; must be one
            of the KiCad pin electrical types (input/output/bidirectional/
            tri_state/passive/free/unspecified/power_in/power_out/
            open_collector/open_emitter/no_connect).
        dest_path: str — absolute path to write the resulting .kicad_sym
            file to. Overwrites any existing file at that path.

    Optional args: reference_prefix (default "U"), footprint (default ""),
    datasheet (default "~").

    This is a STARTING POINT the user reviews/edits in KiCad's symbol
    editor, not a certified generator — mirrors LibForge's own framing of
    symbol_generation.py.
    """
    args = args or {}
    entry_name = args.get("entry_name")
    if not entry_name:
        raise RuntimeError(_("Falta o argumento 'entry_name'."))
    raw_pins = args.get("pins")
    if not raw_pins or not isinstance(raw_pins, list):
        raise RuntimeError(_("Falta o argumento 'pins' (lista de pinos)."))
    dest_path = args.get("dest_path")
    if not dest_path:
        raise RuntimeError(_("Falta o argumento 'dest_path'."))

    try:
        symbol_generation = _load("symbol_generation")
    except SiblingPluginNotFoundError:
        return _not_installed_message()
    except ImportError as exc:
        return _("Erro ao carregar o LibForge: {err}").format(err=exc)

    pins = []
    for i, raw in enumerate(raw_pins):
        if not isinstance(raw, dict):
            raise RuntimeError(
                _("Pino inválido no índice {i}: esperava um objeto.").format(i=i)
            )
        try:
            number = str(raw["number"])
            name = str(raw["name"])
        except KeyError as exc:
            raise RuntimeError(
                _("Pino inválido no índice {i}: falta '{field}'.").format(
                    i=i, field=exc.args[0]
                )
            ) from exc
        electrical_type = str(raw.get("electrical_type", "passive"))
        try:
            pins.append(
                symbol_generation.PinSpec(
                    number=number, name=name, electrical_type=electrical_type
                )
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    reference_prefix = args.get("reference_prefix") or "U"
    footprint = args.get("footprint") or ""
    datasheet = args.get("datasheet") or "~"

    try:
        result = symbol_generation.generate_symbol_file(
            entry_name=entry_name,
            pins=pins,
            dest_path=Path(dest_path),
            reference_prefix=reference_prefix,
            footprint=footprint,
            datasheet=datasheet,
        )
    except ValueError as exc:
        return _("Erro ao gerar o símbolo: {err}").format(err=exc)
    except OSError as exc:
        return _("Erro ao escrever o ficheiro do símbolo: {err}").format(err=exc)

    lines = [
        _("Símbolo '{name}' gerado e guardado em {path}.").format(
            name=entry_name, path=dest_path
        )
    ]
    if result.warnings:
        lines.append(_("Avisos:"))
        lines.extend(f"  - {w}" for w in result.warnings)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# generate_component_footprint
# --------------------------------------------------------------------------- #
def generate_component_footprint(args: dict) -> str:
    """Generate a simple dual-row (SOIC/DIP-style) or quad-perimeter
    (QFN/QFP-style) SMD footprint and write it to a .kicad_mod file, via
    LibForge's footprint_generation.py.

    Required args:
        entry_name: str — the footprint's name.
        dest_path: str — absolute path to write the resulting .kicad_mod
            file to. Overwrites any existing file at that path.
        pin_count: int — total number of pins/pads (>= 2; must be
            divisible by 4 for package_type "quad").
        pitch: number — distance between adjacent pad centers, mm.
        pad_width: number — pad width, mm.
        pad_height: number — pad height, mm.
        row_spacing: number — distance between the two pad rows (dual) or
            from center to each side (quad), mm.

    Optional args: package_type ("dual" default, or "quad").

    Not an IPC-7351-certified generator — a starting point the user
    reviews/adjusts in KiCad's footprint editor, mirrors LibForge's own
    framing of footprint_generation.py.
    """
    args = args or {}
    entry_name = args.get("entry_name")
    if not entry_name:
        raise RuntimeError(_("Falta o argumento 'entry_name'."))
    dest_path = args.get("dest_path")
    if not dest_path:
        raise RuntimeError(_("Falta o argumento 'dest_path'."))

    try:
        pin_count = int(args["pin_count"])
        pitch = float(args["pitch"])
        pad_width = float(args["pad_width"])
        pad_height = float(args["pad_height"])
        row_spacing = float(args["row_spacing"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumentos numéricos inválidos ou em falta: {err}").format(err=exc)
        ) from exc

    package_type = args.get("package_type") or "dual"

    try:
        footprint_generation = _load("footprint_generation")
    except SiblingPluginNotFoundError:
        return _not_installed_message()
    except ImportError as exc:
        return _("Erro ao carregar o LibForge: {err}").format(err=exc)

    try:
        spec = footprint_generation.PackageSpec(
            pin_count=pin_count,
            pitch=pitch,
            pad_width=pad_width,
            pad_height=pad_height,
            row_spacing=row_spacing,
            package_type=package_type,
        )
        result = footprint_generation.generate_footprint_file(
            entry_name=entry_name, spec=spec, dest_path=Path(dest_path)
        )
    except ValueError as exc:
        return _("Erro ao gerar o footprint: {err}").format(err=exc)
    except OSError as exc:
        return _("Erro ao escrever o ficheiro do footprint: {err}").format(err=exc)

    lines = [
        _("Footprint '{name}' gerado e guardado em {path}.").format(
            name=entry_name, path=dest_path
        )
    ]
    if result.warnings:
        lines.append(_("Avisos:"))
        lines.extend(f"  - {w}" for w in result.warnings)
    return "\n".join(lines)


def register_libforge_tools(registry: ActionRegistry) -> None:
    """Register the LibForge-backed tools on the given ActionRegistry.

    Safe to call even when LibForge isn't installed — handlers report that
    honestly at call time instead of failing registration (same pattern as
    register_emc_emi_tools()). NOT wired into chat_action.py by this module
    — a separate pass does that.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="scan_library_folder_for_duplicates",
                description=(
                    "Call this when the user wants to scan a folder of loose "
                    "KiCad symbol/footprint/3D-model files (e.g. a messy "
                    "personal library folder or a vendor export) and find out "
                    "which components are NEW vs already present (DUPLICATE) "
                    "vs uncertain (AMBIGUOUS) compared to a destination "
                    "library. Uses the sibling LibForge plugin's real "
                    "scanning and duplicate-classification logic. Only "
                    "checks the given destination library — not the official "
                    "KiCad libraries or any configured external sources."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "folder_path": {
                            "type": "string",
                            "description": "Absolute path to the folder to scan recursively.",
                        },
                        "destination_library_path": {
                            "type": "string",
                            "description": (
                                "Absolute path to the destination library "
                                "folder (containing *.kicad_sym files and "
                                "*.pretty footprint folders) to check "
                                "candidates against."
                            ),
                        },
                    },
                    "required": ["folder_path", "destination_library_path"],
                },
            ),
            handler=scan_library_folder_for_duplicates,
            read_only=True,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="generate_component_symbol",
                description=(
                    "Call this when the user wants help creating a KiCad "
                    "symbol for a NEW component from a pinout table (e.g. "
                    "copy-pasted/parsed from a datasheet). Generates a "
                    "simple two-sided symbol body and WRITES it to a "
                    ".kicad_sym file at the given path (overwriting any "
                    "existing file there) via the sibling LibForge plugin's "
                    "generator. A starting point to review/edit in KiCad's "
                    "symbol editor, not a final polished symbol. This "
                    "MODIFIES the filesystem and requires explicit user "
                    "approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "entry_name": {
                            "type": "string",
                            "description": "Symbol name, e.g. 'MCP6002'.",
                        },
                        "pins": {
                            "type": "array",
                            "description": "Pinout table.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "number": {"type": "string"},
                                    "name": {"type": "string"},
                                    "electrical_type": {
                                        "type": "string",
                                        "description": (
                                            "One of: input, output, "
                                            "bidirectional, tri_state, "
                                            "passive, free, unspecified, "
                                            "power_in, power_out, "
                                            "open_collector, open_emitter, "
                                            "no_connect. Defaults to "
                                            "'passive' if omitted."
                                        ),
                                    },
                                },
                                "required": ["number", "name"],
                            },
                        },
                        "dest_path": {
                            "type": "string",
                            "description": (
                                "Absolute path to write the generated "
                                ".kicad_sym file to (overwrites if it "
                                "already exists)."
                            ),
                        },
                        "reference_prefix": {
                            "type": "string",
                            "description": "Reference designator prefix (default 'U').",
                        },
                        "footprint": {
                            "type": "string",
                            "description": "Footprint field value (default '').",
                        },
                        "datasheet": {
                            "type": "string",
                            "description": "Datasheet field value (default '~').",
                        },
                    },
                    "required": ["entry_name", "pins", "dest_path"],
                },
            ),
            handler=generate_component_symbol,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="generate_component_footprint",
                description=(
                    "Call this when the user wants help creating a KiCad SMD "
                    "footprint for a NEW component with a dual-row "
                    "(SOIC/DIP-style) or quad-perimeter (QFN/QFP-style) "
                    "package, given pin count, pitch and pad dimensions from "
                    "a datasheet. WRITES the result to a .kicad_mod file at "
                    "the given path (overwriting any existing file there) "
                    "via the sibling LibForge plugin's generator. A starting "
                    "point to review/adjust in KiCad's footprint editor, not "
                    "an IPC-7351-certified footprint. This MODIFIES the "
                    "filesystem and requires explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "entry_name": {
                            "type": "string",
                            "description": "Footprint name.",
                        },
                        "dest_path": {
                            "type": "string",
                            "description": (
                                "Absolute path to write the generated "
                                ".kicad_mod file to (overwrites if it "
                                "already exists)."
                            ),
                        },
                        "pin_count": {
                            "type": "integer",
                            "description": (
                                "Total number of pins/pads (>= 2; must be "
                                "divisible by 4 for package_type 'quad')."
                            ),
                        },
                        "pitch": {
                            "type": "number",
                            "description": "Distance between adjacent pad centers, mm.",
                        },
                        "pad_width": {
                            "type": "number",
                            "description": "Pad width, mm.",
                        },
                        "pad_height": {
                            "type": "number",
                            "description": "Pad height, mm.",
                        },
                        "row_spacing": {
                            "type": "number",
                            "description": (
                                "Distance between the two pad rows (dual) "
                                "or center-to-side distance (quad), mm."
                            ),
                        },
                        "package_type": {
                            "type": "string",
                            "description": "'dual' (default) or 'quad'.",
                        },
                    },
                    "required": [
                        "entry_name",
                        "dest_path",
                        "pin_count",
                        "pitch",
                        "pad_width",
                        "pad_height",
                        "row_spacing",
                    ],
                },
            ),
            handler=generate_component_footprint,
            read_only=False,
        )
    )
