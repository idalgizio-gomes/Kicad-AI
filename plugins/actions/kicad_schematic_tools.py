"""
Schematic-editing chat tools for the KiCad Chat Assistant.

IMPORTANT — how this module is fundamentally different from kicad_tools.py /
kicad_write_tools.py: KiCad's classic Python (SWIG) bindings ("pcbnew") only
ever cover the PCB editor. There is NO equivalent live-board API for the
schematic editor (eeschema) — pcbnew.GetBoard() returns the live in-memory
PCB, but there is nothing analogous for the currently open schematic. Every
tool in this file therefore edits the SAVED ``.kicad_sch`` FILE directly, as
an S-expression document, via the ``kiutils`` library (a pure-Python
KiCad-file-format parser/writer) — never the live schematic editor state.

Consequence the user must always be told, and that every write tool's
result message repeats explicitly: if the schematic is currently open in
KiCad's schematic editor, that window does NOT see the change made here.
The user must close and reopen the schematic (or accept KiCad's own
"file changed on disk, reload?" prompt) to see it. This is a real,
unavoidable limitation of file-based editing — never hidden or downplayed.

kiutils bootstrap: kiutils is not bundled with this plugin. It lives inside
the sibling LibForge plugin's own dev tree (as a git submodule). Exactly
like ``libforge_tools.py``, we reach it by importing LibForge's own
``matching`` submodule once (via ``_load("matching")``, itself built on
``_sibling_plugin.py``'s synthetic-package loader) — that import has the
side effect of inserting kiutils's ``src/`` directory onto ``sys.path``
(see LibForge's own ``matching.py``, which does
``sys.path.insert(0, str(_KIUTILS_SRC))`` at import time). We don't use
anything else FROM the matching module; triggering its import is enough.
If the LibForge plugin isn't installed, ``_load()`` raises
``SiblingPluginNotFoundError``, which every handler turns into a clear
"LibForge não está instalado" ``RuntimeError`` — never a raw ImportError.

Same lazy-import + RuntimeError-not-raw-exception conventions as the other
tool modules: nothing schematic/kiutils/pcbnew-related is imported at
module scope, so this module (and its tests) import cleanly with none of
those available. See ``kicad_tools.py``'s ``_get_board()`` /
``run_erc()`` for the board-path-to-schematic-path derivation this module's
``_get_schematic_path()`` mirrors.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
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

# Sibling plugin install path, same reverse-DNS junction convention as
# libforge_tools.py — resolved relative to KiCad's Documents folder at call
# time, not hardcoded to one KiCad version.
_SIBLING_IDENTIFIER = "com_github_idalgizio-gomes_kicad-libforge"

_VALID_LABEL_KINDS = {"local", "global"}
_VALID_GLOBAL_LABEL_SHAPES = {"input", "output", "bidirectional", "tri_state", "passive"}


def _find_sibling_plugins_dir() -> Path:
    """Best-effort discovery of the LibForge plugin's ``plugins/`` directory
    across installed KiCad versions (junction target), newest first."""
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
        "O plugin LibForge não está instalado nesta máquina — as "
        "ferramentas de esquemático precisam dele (é através do LibForge "
        "que chegamos ao kiutils, a biblioteca que lê/escreve ficheiros "
        ".kicad_sch)."
    )


def _reload_notice() -> str:
    return _(
        "Feche e reabra o esquemático no editor do KiCad para ver esta "
        "alteração (ou aceite o pedido do KiCad para recarregar o "
        "ficheiro) — esta ferramenta edita o ficheiro .kicad_sch "
        "diretamente, não o esquemático aberto ao vivo."
    )


def _get_schematic_path() -> str:
    """Lazily import pcbnew, get the currently open board, and derive the
    path to the project's schematic file (.kicad_sch) from it — same
    derivation as kicad_tools.py's run_erc().

    NOTE: this returns a path to the SAVED schematic FILE on disk. There is
    no live in-memory schematic object to read/mutate (pcbnew's bindings
    only ever cover the PCB editor) — every tool in this module reads and
    writes that file directly via kiutils.

    Raises RuntimeError if pcbnew/board is unavailable, the board has no
    file path yet, or the derived .kicad_sch file doesn't exist on disk.
    """
    try:
        import pcbnew
    except ImportError as exc:
        raise RuntimeError(
            _("pcbnew indisponível — esta ferramenta só funciona dentro do KiCad")
        ) from exc

    board = pcbnew.GetBoard()
    if board is None:
        raise RuntimeError(_("Nenhum board KiCad está atualmente aberto"))

    try:
        board_path = board.GetFileName()
    except Exception:
        board_path = None

    if not board_path:
        raise RuntimeError(
            _(
                "Não foi possível determinar o caminho do projeto para "
                "localizar o esquemático."
            )
        )

    sch_path = str(Path(board_path).with_suffix(".kicad_sch"))
    if not os.path.isfile(sch_path):
        raise RuntimeError(_("Esquemático não encontrado: {path}").format(path=sch_path))

    return sch_path


def _open_schematic(sch_path: str):
    """Bootstrap kiutils onto sys.path (via the sibling LibForge plugin) and
    load the schematic at ``sch_path`` via ``kiutils.schematic.Schematic``.

    Raises RuntimeError (never a raw exception) if LibForge isn't installed
    or the file fails to parse.
    """
    try:
        _load("matching")
    except SiblingPluginNotFoundError:
        raise RuntimeError(_not_installed_message())

    from kiutils.schematic import Schematic

    try:
        return Schematic.from_file(sch_path)
    except Exception as exc:
        raise RuntimeError(
            _("Erro ao ler o esquemático {path}: {err}").format(path=sch_path, err=exc)
        ) from exc


def _is_wire(item) -> bool:
    return getattr(item, "type", None) == "wire"


# --------------------------------------------------------------------------- #
# wires
# --------------------------------------------------------------------------- #
def list_schematic_wires(args: dict) -> str:
    """List every wire currently in the schematic file on disk (skips buses
    and plain graphical polylines). Read-only — does not modify anything.

    Reads the SAVED .kicad_sch file via kiutils, not KiCad's live
    schematic-editor state — unsaved edits made by hand in the schematic
    editor won't show up here until the user saves.

    Returns one row per wire: 1-based index (for delete_schematic_wire),
    uuid (if present), start point (mm), end point (mm).
    """
    sch_path = _get_schematic_path()
    schematic = _open_schematic(sch_path)

    rows = []
    for i, item in enumerate(schematic.graphicalItems, start=1):
        if not _is_wire(item):
            continue
        item_uuid = item.uuid or "-"
        points = item.points
        if len(points) >= 2:
            start = points[0]
            end = points[-1]
            start_str = f"({start.X:.3f}, {start.Y:.3f})"
            end_str = f"({end.X:.3f}, {end.Y:.3f})"
        else:
            start_str = end_str = "?"
        rows.append(f"{i}\t{item_uuid}\t{start_str}\t{end_str}")

    header = _("Índice\tUUID\tInício (mm)\tFim (mm)")
    body = "\n".join(rows) if rows else _("(nenhum fio encontrado)")
    return f"{header}\n{body}"


def add_schematic_wire(args: dict) -> str:
    """Add a new wire (straight segment) to the schematic file on disk,
    from (start_x_mm, start_y_mm) to (end_x_mm, end_y_mm).

    WRITES the .kicad_sch file directly via kiutils — this is NOT a live
    edit in KiCad's schematic editor. The user MUST close and reopen the
    schematic (or accept KiCad's reload prompt) to see it.

    Required args: start_x_mm, start_y_mm, end_x_mm, end_y_mm (numbers).
    """
    args = args or {}
    try:
        start_x = float(args["start_x_mm"])
        start_y = float(args["start_y_mm"])
        end_x = float(args["end_x_mm"])
        end_y = float(args["end_y_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumentos de coordenadas inválidos ou em falta: {err}").format(err=exc)
        ) from exc

    sch_path = _get_schematic_path()
    schematic = _open_schematic(sch_path)

    from kiutils.items.common import Position
    from kiutils.items.schitems import Connection

    wire = Connection(
        type="wire",
        points=[Position(X=start_x, Y=start_y), Position(X=end_x, Y=end_y)],
    )
    schematic.graphicalItems.append(wire)
    schematic.to_file(sch_path)

    return (
        _(
            "Fio adicionado de ({sx:.3f}, {sy:.3f}) mm a ({ex:.3f}, {ey:.3f}) "
            "mm em {path}."
        ).format(sx=start_x, sy=start_y, ex=end_x, ey=end_y, path=sch_path)
        + " "
        + _reload_notice()
    )


def delete_schematic_wire(args: dict) -> str:
    """Delete a wire from the schematic file on disk, identified by its
    'uuid' (preferred if given) or its 1-based 'index' from
    list_schematic_wires.

    WRITES the .kicad_sch file directly via kiutils — the user MUST close
    and reopen the schematic in KiCad afterward to see this change.

    Args: uuid (str, optional) OR index (int, optional) — at least one is
    required; uuid wins if both are given.
    """
    args = args or {}
    wire_uuid = args.get("uuid")
    index = args.get("index")
    if not wire_uuid and index is None:
        raise RuntimeError(_("Falta o argumento 'uuid' ou 'index'."))

    sch_path = _get_schematic_path()
    schematic = _open_schematic(sch_path)

    wires = [item for item in schematic.graphicalItems if _is_wire(item)]

    target = None
    if wire_uuid:
        target = next((w for w in wires if w.uuid == wire_uuid), None)
        if target is None:
            raise RuntimeError(
                _("Nenhum fio encontrado com uuid '{uuid}'.").format(uuid=wire_uuid)
            )
    else:
        try:
            idx = int(index)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                _("Argumento 'index' inválido: {err}").format(err=exc)
            ) from exc
        if idx < 1 or idx > len(wires):
            raise RuntimeError(
                _("Índice de fio inválido: {idx} (existem {n} fios).").format(
                    idx=idx, n=len(wires)
                )
            )
        target = wires[idx - 1]

    schematic.graphicalItems.remove(target)
    schematic.to_file(sch_path)

    return (
        _("Fio removido de {path}.").format(path=sch_path)
        + " "
        + _reload_notice()
    )


# --------------------------------------------------------------------------- #
# labels
# --------------------------------------------------------------------------- #
def list_schematic_labels(args: dict) -> str:
    """List every local and global label currently in the schematic file on
    disk. Read-only — does not modify anything.

    Returns one row per label: 1-based index (within its OWN list — local
    and global labels are numbered separately, use the 'kind' column to
    disambiguate for delete_schematic_label), kind (local/global), text,
    position (mm), and shape (global labels only).
    """
    sch_path = _get_schematic_path()
    schematic = _open_schematic(sch_path)

    rows = []
    for i, label in enumerate(schematic.labels, start=1):
        pos = label.position
        rows.append(f"{i}\tlocal\t{label.text}\t({pos.X:.3f}, {pos.Y:.3f})\t-")
    for i, label in enumerate(schematic.globalLabels, start=1):
        pos = label.position
        rows.append(f"{i}\tglobal\t{label.text}\t({pos.X:.3f}, {pos.Y:.3f})\t{label.shape}")

    header = _("Índice\tTipo\tTexto\tPosição (mm)\tForma")
    body = "\n".join(rows) if rows else _("(nenhuma etiqueta encontrada)")
    return f"{header}\n{body}"


def add_schematic_label(args: dict) -> str:
    """Add a new local or global label to the schematic file on disk.

    WRITES the .kicad_sch file directly via kiutils — the user MUST close
    and reopen the schematic in KiCad afterward to see this change.

    Required args: text (str), x_mm, y_mm (numbers).
    Optional args: kind ('local' default, or 'global'); shape (only
    meaningful for kind='global' — one of input/output/bidirectional/
    tri_state/passive, default 'input').
    """
    args = args or {}
    text = args.get("text")
    if not text:
        raise RuntimeError(_("Falta o argumento 'text'."))
    try:
        x_mm = float(args["x_mm"])
        y_mm = float(args["y_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumentos 'x_mm'/'y_mm' inválidos: {err}").format(err=exc)
        ) from exc

    kind = args.get("kind") or "local"
    if kind not in _VALID_LABEL_KINDS:
        raise RuntimeError(
            _("Argumento 'kind' inválido: '{kind}' (use 'local' ou 'global').").format(
                kind=kind
            )
        )

    shape = args.get("shape") or "input"
    if kind == "global" and shape not in _VALID_GLOBAL_LABEL_SHAPES:
        raise RuntimeError(
            _("Argumento 'shape' inválido: '{shape}'.").format(shape=shape)
        )

    sch_path = _get_schematic_path()
    schematic = _open_schematic(sch_path)

    from kiutils.items.common import Position

    if kind == "local":
        from kiutils.items.schitems import LocalLabel

        label = LocalLabel(text=str(text), position=Position(X=x_mm, Y=y_mm))
        schematic.labels.append(label)
    else:
        from kiutils.items.schitems import GlobalLabel

        label = GlobalLabel(
            text=str(text), position=Position(X=x_mm, Y=y_mm), shape=shape
        )
        schematic.globalLabels.append(label)

    schematic.to_file(sch_path)

    return (
        _(
            "Etiqueta {kind} '{text}' adicionada em ({x:.3f}, {y:.3f}) mm em "
            "{path}."
        ).format(kind=kind, text=text, x=x_mm, y=y_mm, path=sch_path)
        + " "
        + _reload_notice()
    )


def delete_schematic_label(args: dict) -> str:
    """Delete a local or global label from the schematic file on disk, by
    its 'kind' and 1-based 'index' WITHIN THAT KIND's list, exactly as
    returned by list_schematic_labels.

    WRITES the .kicad_sch file directly via kiutils — the user MUST close
    and reopen the schematic in KiCad afterward to see this change.

    Required args: kind ('local' or 'global'), index (int).
    """
    args = args or {}
    kind = args.get("kind")
    if kind not in _VALID_LABEL_KINDS:
        raise RuntimeError(
            _("Argumento 'kind' inválido ou em falta (use 'local' ou 'global').")
        )
    index = args.get("index")
    if index is None:
        raise RuntimeError(_("Falta o argumento 'index'."))
    try:
        idx = int(index)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(_("Argumento 'index' inválido: {err}").format(err=exc)) from exc

    sch_path = _get_schematic_path()
    schematic = _open_schematic(sch_path)

    target_list = schematic.labels if kind == "local" else schematic.globalLabels
    if idx < 1 or idx > len(target_list):
        raise RuntimeError(
            _("Índice de etiqueta inválido: {idx} (existem {n}).").format(
                idx=idx, n=len(target_list)
            )
        )

    removed = target_list.pop(idx - 1)
    schematic.to_file(sch_path)

    return (
        _("Etiqueta {kind} '{text}' removida de {path}.").format(
            kind=kind, text=removed.text, path=sch_path
        )
        + " "
        + _reload_notice()
    )


# --------------------------------------------------------------------------- #
# symbols
# --------------------------------------------------------------------------- #
def _symbol_property(sym, key: str) -> str | None:
    for prop in sym.properties:
        if prop.key == key:
            return prop.value
    return None


def list_schematic_symbols(args: dict) -> str:
    """List every placed symbol instance in the schematic file on disk.
    Read-only — does not modify anything.

    Returns one row per symbol: reference, value, lib_id, position (mm).
    """
    sch_path = _get_schematic_path()
    schematic = _open_schematic(sch_path)

    rows = []
    for sym in schematic.schematicSymbols:
        reference = _symbol_property(sym, "Reference") or "?"
        value = _symbol_property(sym, "Value") or "?"
        pos = sym.position
        rows.append(f"{reference}\t{value}\t{sym.libId}\t({pos.X:.3f}, {pos.Y:.3f})")

    header = _("Referência\tValor\tLib ID\tPosição (mm)")
    body = "\n".join(rows) if rows else _("(nenhum símbolo encontrado)")
    return f"{header}\n{body}"


def add_schematic_symbol(args: dict) -> str:
    """Place a NEW symbol instance in the schematic file on disk: loads the
    symbol's definition from a .kicad_sym library, copies that definition
    into the schematic's own lib_symbols cache (if not already present),
    and appends the placed instance with fresh per-pin UUIDs.

    WRITES the .kicad_sch file directly via kiutils — this is NOT a live
    edit in KiCad's schematic editor. The user MUST close and reopen the
    schematic (or accept KiCad's reload prompt) to see it.

    Scope: only supports FLAT (single-sheet, non-hierarchical) schematics —
    the placed instance's project path is hardcoded to the root sheet
    ('/'), which may not be correct for a hierarchical multi-sheet project.

    Required args:
        symbol_library_path: str — path to a .kicad_sym file.
        entry_name: str — symbol name inside that library.
        reference: str — reference designator (e.g. 'R5'); must not already
            be used by another symbol in the schematic.
        x_mm, y_mm: number — placement position, mm.

    Optional args: value (defaults to entry_name), rotation_deg (default
    0), footprint (KiCad footprint id, e.g.
    'Resistor_SMD:R_0603_1608Metric').
    """
    args = args or {}
    symbol_library_path = args.get("symbol_library_path")
    if not symbol_library_path:
        raise RuntimeError(_("Falta o argumento 'symbol_library_path'."))
    entry_name = args.get("entry_name")
    if not entry_name:
        raise RuntimeError(_("Falta o argumento 'entry_name'."))
    reference = args.get("reference")
    if not reference:
        raise RuntimeError(_("Falta o argumento 'reference'."))
    try:
        x_mm = float(args["x_mm"])
        y_mm = float(args["y_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumentos 'x_mm'/'y_mm' inválidos: {err}").format(err=exc)
        ) from exc
    try:
        rotation_deg = float(args.get("rotation_deg") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumento 'rotation_deg' inválido: {err}").format(err=exc)
        ) from exc

    value = args.get("value") or entry_name
    footprint = args.get("footprint")

    if not os.path.isfile(symbol_library_path):
        raise RuntimeError(
            _("Biblioteca de símbolos não encontrada: {path}").format(
                path=symbol_library_path
            )
        )

    sch_path = _get_schematic_path()
    schematic = _open_schematic(sch_path)

    for sym in schematic.schematicSymbols:
        if _symbol_property(sym, "Reference") == reference:
            raise RuntimeError(
                _(
                    "Já existe um símbolo com a referência '{reference}' no "
                    "esquemático."
                ).format(reference=reference)
            )

    from kiutils.items.common import Position, Property
    from kiutils.items.schitems import (
        SchematicSymbol,
        SymbolProjectInstance,
        SymbolProjectPath,
    )
    from kiutils.symbol import SymbolLib

    try:
        lib = SymbolLib.from_file(symbol_library_path)
    except Exception as exc:
        raise RuntimeError(
            _("Erro ao ler a biblioteca de símbolos {path}: {err}").format(
                path=symbol_library_path, err=exc
            )
        ) from exc
    try:
        sym_def = next(s for s in lib.symbols if s.entryName == entry_name)
    except StopIteration:
        raise RuntimeError(
            _("Símbolo '{entry}' não encontrado em {path}.").format(
                entry=entry_name, path=symbol_library_path
            )
        )

    if not any(s.libId == sym_def.libId for s in schematic.libSymbols):
        schematic.libSymbols.append(sym_def)

    properties = [
        Property(
            key="Reference",
            value=reference,
            id=0,
            position=Position(X=x_mm, Y=y_mm - 5, angle=0),
        ),
        Property(
            key="Value",
            value=value,
            id=1,
            position=Position(X=x_mm, Y=y_mm + 5, angle=0),
        ),
    ]
    if footprint:
        properties.append(
            Property(
                key="Footprint",
                value=footprint,
                id=2,
                position=Position(X=x_mm, Y=y_mm, angle=0),
            )
        )

    # Collect every real pin number: a symbol's OWN direct pins, plus every
    # pin nested in each of its units (a single-unit symbol still nests its
    # pins inside one child unit) — dedupe by pin number.
    pin_numbers: list[str] = []
    seen_numbers: set[str] = set()
    for pin in sym_def.pins:
        if pin.number not in seen_numbers:
            seen_numbers.add(pin.number)
            pin_numbers.append(pin.number)
    for unit in sym_def.units:
        for pin in unit.pins:
            if pin.number not in seen_numbers:
                seen_numbers.add(pin.number)
                pin_numbers.append(pin.number)
    pins = {number: str(uuid_lib.uuid4()) for number in pin_numbers}

    project_name = Path(sch_path).stem
    schematic_symbol = SchematicSymbol(
        position=Position(X=x_mm, Y=y_mm, angle=rotation_deg),
        uuid=str(uuid_lib.uuid4()),
        inBom=True,
        onBoard=True,
        properties=properties,
        pins=pins,
        instances=[
            SymbolProjectInstance(
                name=project_name,
                paths=[
                    SymbolProjectPath(
                        sheetInstancePath="/", reference=reference, unit=1
                    )
                ],
            )
        ],
    )
    # libId is a property (backed by libraryNickname/entryName), not a plain
    # dataclass field — SchematicSymbol(libId=...) would raise TypeError, so
    # it must be set via the setter after construction.
    schematic_symbol.libId = sym_def.libId

    schematic.schematicSymbols.append(schematic_symbol)
    schematic.to_file(sch_path)

    return (
        _(
            "Símbolo '{entry}' colocado como {reference} em ({x:.3f}, "
            "{y:.3f}) mm em {path}."
        ).format(entry=entry_name, reference=reference, x=x_mm, y=y_mm, path=sch_path)
        + " "
        + _reload_notice()
    )


def delete_schematic_symbol(args: dict) -> str:
    """Delete a placed symbol instance from the schematic file on disk, by
    its reference designator (e.g. 'R5'). Leaves its cached definition in
    lib_symbols untouched even if now unused — harmless, KiCad prunes
    unused lib_symbols itself the next time it saves from the GUI.

    WRITES the .kicad_sch file directly via kiutils — the user MUST close
    and reopen the schematic in KiCad afterward to see this change.

    Required args: reference (str).
    """
    args = args or {}
    reference = args.get("reference")
    if not reference:
        raise RuntimeError(_("Falta o argumento 'reference'."))

    sch_path = _get_schematic_path()
    schematic = _open_schematic(sch_path)

    target = None
    for sym in schematic.schematicSymbols:
        if _symbol_property(sym, "Reference") == reference:
            target = sym
            break

    if target is None:
        raise RuntimeError(
            _("Símbolo com referência '{reference}' não encontrado no esquemático.").format(
                reference=reference
            )
        )

    schematic.schematicSymbols.remove(target)
    schematic.to_file(sch_path)

    return (
        _("Símbolo '{reference}' removido de {path}.").format(
            reference=reference, path=sch_path
        )
        + " "
        + _reload_notice()
    )


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def register_schematic_tools(registry: ActionRegistry) -> None:
    """Register the kiutils-backed schematic-editing tools on the given
    ActionRegistry.

    Safe to call even when LibForge/kiutils isn't installed — handlers
    report that honestly at call time (see _not_installed_message()),
    instead of failing registration (same pattern as
    register_libforge_tools()). NOT wired into chat_action.py by this
    module — a separate pass does that.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="list_schematic_wires",
                description=(
                    "Call this when the user asks what wires exist in the "
                    "schematic, or before deleting a wire (to get its index). "
                    "Reads the SAVED .kicad_sch file on disk via kiutils, not "
                    "KiCad's live schematic editor state."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            handler=list_schematic_wires,
            read_only=True,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="add_schematic_wire",
                description=(
                    "Call this to draw a new wire segment in the schematic, "
                    "between two absolute points in mm. WRITES the "
                    ".kicad_sch file directly (not a live schematic-editor "
                    "edit) — the user must close/reopen the schematic in "
                    "KiCad afterward. This MODIFIES the filesystem and "
                    "requires explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "start_x_mm": {"type": "number", "description": "Start X, mm."},
                        "start_y_mm": {"type": "number", "description": "Start Y, mm."},
                        "end_x_mm": {"type": "number", "description": "End X, mm."},
                        "end_y_mm": {"type": "number", "description": "End Y, mm."},
                    },
                    "required": ["start_x_mm", "start_y_mm", "end_x_mm", "end_y_mm"],
                },
            ),
            handler=add_schematic_wire,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="delete_schematic_wire",
                description=(
                    "Call this to delete a wire from the schematic, "
                    "identified either by its 'uuid' or by its 1-based "
                    "'index' from list_schematic_wires (uuid wins if both "
                    "given). WRITES the .kicad_sch file directly — the user "
                    "must close/reopen the schematic in KiCad afterward. "
                    "This MODIFIES the filesystem and requires explicit "
                    "user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "uuid": {
                            "type": "string",
                            "description": "Wire uuid, from list_schematic_wires.",
                        },
                        "index": {
                            "type": "integer",
                            "description": "1-based wire index, from list_schematic_wires.",
                        },
                    },
                    "required": [],
                },
            ),
            handler=delete_schematic_wire,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="list_schematic_labels",
                description=(
                    "Call this when the user asks what net labels exist in "
                    "the schematic, or before deleting one (to get its "
                    "kind + index). Reads the SAVED .kicad_sch file on disk "
                    "via kiutils, not KiCad's live schematic editor state."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            handler=list_schematic_labels,
            read_only=True,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="add_schematic_label",
                description=(
                    "Call this to add a new local or global net label to "
                    "the schematic at a given position. WRITES the "
                    ".kicad_sch file directly — the user must close/reopen "
                    "the schematic in KiCad afterward. This MODIFIES the "
                    "filesystem and requires explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Label text."},
                        "x_mm": {"type": "number", "description": "X position, mm."},
                        "y_mm": {"type": "number", "description": "Y position, mm."},
                        "kind": {
                            "type": "string",
                            "description": "'local' (default) or 'global'.",
                        },
                        "shape": {
                            "type": "string",
                            "description": (
                                "Only meaningful for kind='global': one of "
                                "input/output/bidirectional/tri_state/"
                                "passive. Default 'input'."
                            ),
                        },
                    },
                    "required": ["text", "x_mm", "y_mm"],
                },
            ),
            handler=add_schematic_label,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="delete_schematic_label",
                description=(
                    "Call this to delete a local or global label from the "
                    "schematic, identified by 'kind' ('local'/'global') and "
                    "its 1-based 'index' WITHIN that kind's list, exactly "
                    "as returned by list_schematic_labels. WRITES the "
                    ".kicad_sch file directly — the user must close/reopen "
                    "the schematic in KiCad afterward. This MODIFIES the "
                    "filesystem and requires explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "description": "'local' or 'global'.",
                        },
                        "index": {
                            "type": "integer",
                            "description": (
                                "1-based index within that kind's list, "
                                "from list_schematic_labels."
                            ),
                        },
                    },
                    "required": ["kind", "index"],
                },
            ),
            handler=delete_schematic_label,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="list_schematic_symbols",
                description=(
                    "Call this when the user asks what components are "
                    "placed in the schematic, or wants each one's "
                    "reference/value/lib_id/position. Reads the SAVED "
                    ".kicad_sch file on disk via kiutils, not KiCad's live "
                    "schematic editor state."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            handler=list_schematic_symbols,
            read_only=True,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="add_schematic_symbol",
                description=(
                    "Call this to place a NEW symbol instance in the "
                    "schematic, loading its definition from a .kicad_sym "
                    "library file. Only supports FLAT (single-sheet) "
                    "schematics. WRITES the .kicad_sch file directly — the "
                    "user must close/reopen the schematic in KiCad "
                    "afterward. This MODIFIES the filesystem and requires "
                    "explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "symbol_library_path": {
                            "type": "string",
                            "description": "Path to a .kicad_sym file.",
                        },
                        "entry_name": {
                            "type": "string",
                            "description": "Symbol name inside that library.",
                        },
                        "reference": {
                            "type": "string",
                            "description": (
                                "Reference designator, e.g. 'R5'. Must not "
                                "already be used in the schematic."
                            ),
                        },
                        "x_mm": {"type": "number", "description": "Placement X, mm."},
                        "y_mm": {"type": "number", "description": "Placement Y, mm."},
                        "value": {
                            "type": "string",
                            "description": "Value field (defaults to entry_name).",
                        },
                        "rotation_deg": {
                            "type": "number",
                            "description": "Rotation angle in degrees (default 0).",
                        },
                        "footprint": {
                            "type": "string",
                            "description": (
                                "KiCad footprint id, e.g. "
                                "'Resistor_SMD:R_0603_1608Metric'."
                            ),
                        },
                    },
                    "required": [
                        "symbol_library_path",
                        "entry_name",
                        "reference",
                        "x_mm",
                        "y_mm",
                    ],
                },
            ),
            handler=add_schematic_symbol,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="delete_schematic_symbol",
                description=(
                    "Call this to delete a placed symbol instance from the "
                    "schematic, identified by its reference designator. "
                    "WRITES the .kicad_sch file directly — the user must "
                    "close/reopen the schematic in KiCad afterward. This "
                    "MODIFIES the filesystem and requires explicit user "
                    "approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "reference": {
                            "type": "string",
                            "description": "Reference designator, e.g. 'R5'.",
                        },
                    },
                    "required": ["reference"],
                },
            ),
            handler=delete_schematic_symbol,
            read_only=False,
        )
    )
