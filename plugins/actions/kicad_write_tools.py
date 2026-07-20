"""
Write actions (board mutations) for the KiCad Chat Assistant.

Deliberately separate from kicad_tools.py (all read-only) — every action
registered here has read_only=False, and the approval dialog (chat_gui.py)
visually distinguishes write actions from read-only ones so the user never
mistakes a mutation for a query.

Same lazy-import convention as kicad_tools.py: `pcbnew` is imported INSIDE
each handler, never at module scope, so this module (and its tests) import
cleanly outside KiCad. Handlers raise RuntimeError (never a raw pcbnew
exception) for the actions framework to turn into a plain error message.

Every handler mutates the LIVE board object (visible immediately in the
open PCB editor, same as an edit made by hand) but never saves to disk
automatically — the user's own Ctrl+S persists it. This is deliberate: an
LLM-driven change stays undoable via KiCad's own Undo (Ctrl+Z) until the
user actively saves, exactly like any other GUI edit.

Scope: beyond the original reposition/rotate/relabel trio for an EXISTING
footprint, this module also covers adding/removing footprints, adding/
removing tracks and vias, reassigning a pad's net, and creating a brand-new
empty board file from scratch. These DO touch net/connectivity data (track,
via, pad-net tools) — each handler's docstring says so explicitly, and the
approval dialog + read_only=False flag apply exactly the same as for the
original three tools. create_board_from_scratch is the one exception to the
"mutates the LIVE board" framing above: it never calls ``pcbnew.GetBoard()``
and writes a brand-new .kicad_pcb file to disk instead — see its own
docstring.
"""

from __future__ import annotations

try:
    from .framework import ActionDefinition, ActionRegistry
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from actions.framework import ActionDefinition, ActionRegistry

try:
    from ..llm_providers.base import ToolSpec
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from llm_providers.base import ToolSpec

# i18n: new strings written directly in Portuguese (the plugin's source
# language, see plugins/i18n/__init__.py) and wrapped in _() from the start
# — unlike kicad_tools.py, which predates i18n and still mixes English
# result strings (a known, separately-tracked follow-up).
try:
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - fallback for flat/test imports
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


def _get_board():
    """Lazily import pcbnew and return the currently open board.

    Raises RuntimeError with a clear message if pcbnew (or an open board)
    is unavailable, instead of letting an ImportError/AttributeError bubble
    up to the LLM tool-calling loop.
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
    return pcbnew, board


def _find_footprint(board, reference: str):
    fp = board.FindFootprintByReference(reference)
    if fp is None:
        raise RuntimeError(
            _("Componente '{reference}' não encontrado na placa.").format(
                reference=reference
            )
        )
    return fp


def _refresh(pcbnew_module) -> None:
    """Best-effort PCB editor redraw after a mutation. Never lets a failed
    redraw fail the mutation itself — the board change already happened."""
    try:
        pcbnew_module.Refresh()
    except Exception:
        pass


def _require_reference(args: dict) -> str:
    reference = (args or {}).get("reference")
    if not reference:
        raise RuntimeError(_("Falta o argumento 'reference'."))
    return reference


def move_footprint(args: dict) -> str:
    """Move a footprint to an absolute position on the board, in mm."""
    pcbnew, board = _get_board()
    args = args or {}
    reference = _require_reference(args)
    try:
        x_mm = float(args["x_mm"])
        y_mm = float(args["y_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumentos 'x_mm'/'y_mm' inválidos: {err}").format(err=exc)
        ) from exc

    fp = _find_footprint(board, reference)
    old_pos = fp.GetPosition()
    old_x_mm = pcbnew.ToMM(old_pos.x)
    old_y_mm = pcbnew.ToMM(old_pos.y)

    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm)))
    _refresh(pcbnew)

    return _(
        "{reference} movido de ({old_x:.3f}, {old_y:.3f}) mm para "
        "({new_x:.3f}, {new_y:.3f}) mm. Guarde a placa (Ctrl+S) para "
        "persistir a alteração."
    ).format(reference=reference, old_x=old_x_mm, old_y=old_y_mm, new_x=x_mm, new_y=y_mm)


def rotate_footprint(args: dict) -> str:
    """Set a footprint's absolute rotation, in degrees."""
    pcbnew, board = _get_board()
    args = args or {}
    reference = _require_reference(args)
    try:
        angle_deg = float(args["angle_deg"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumento 'angle_deg' inválido: {err}").format(err=exc)
        ) from exc

    fp = _find_footprint(board, reference)
    old_angle = fp.GetOrientationDegrees()
    fp.SetOrientationDegrees(angle_deg)
    _refresh(pcbnew)

    return _(
        "{reference} rodado de {old_angle:.1f}° para {new_angle:.1f}°. "
        "Guarde a placa (Ctrl+S) para persistir a alteração."
    ).format(reference=reference, old_angle=old_angle, new_angle=angle_deg)


def set_footprint_value(args: dict) -> str:
    """Change a footprint's displayed value text (e.g. '10k' -> '22k')."""
    pcbnew, board = _get_board()
    args = args or {}
    reference = _require_reference(args)
    value = args.get("value")
    if value is None or value == "":
        raise RuntimeError(_("Falta o argumento 'value'."))

    fp = _find_footprint(board, reference)
    old_value = fp.GetValue()
    fp.SetValue(str(value))
    _refresh(pcbnew)

    return _(
        "Valor de {reference} alterado de '{old_value}' para '{new_value}'. "
        "Guarde a placa (Ctrl+S) para persistir a alteração."
    ).format(reference=reference, old_value=old_value, new_value=value)


def _require_layer_id(board, args: dict) -> int:
    layer_name = (args or {}).get("layer")
    if not layer_name:
        raise RuntimeError(_("Falta o argumento 'layer'."))
    layer_id = board.GetLayerID(layer_name)
    if layer_id is None or layer_id < 0:
        raise RuntimeError(
            _("Layer '{layer}' inválido.").format(layer=layer_name)
        )
    return layer_id


def _find_or_create_net(pcbnew_module, board, net_name: str):
    """Look up an existing net by name, creating it if it doesn't exist yet.

    Never silently substitutes/guesses a net for a typo — the caller always
    surfaces ``net_name`` back to the user in the result message, so a typo
    that unintentionally creates a brand-new net is visible either way.
    """
    net = board.FindNet(net_name)
    if net is None:
        net = pcbnew_module.NETINFO_ITEM(board, net_name)
        board.Add(net)
    return net


def add_footprint(args: dict) -> str:
    """Load a footprint from a library folder (.pretty) and add it to the
    live board at an absolute position, under a new (not-yet-used)
    reference designator."""
    pcbnew, board = _get_board()
    args = args or {}

    library_path = args.get("library_path")
    if not library_path:
        raise RuntimeError(_("Falta o argumento 'library_path'."))
    footprint_name = args.get("footprint_name")
    if not footprint_name:
        raise RuntimeError(_("Falta o argumento 'footprint_name'."))
    reference = _require_reference(args)

    if board.FindFootprintByReference(reference) is not None:
        raise RuntimeError(
            _("Já existe um componente com a referência '{reference}' na placa.").format(
                reference=reference
            )
        )

    try:
        x_mm = float(args["x_mm"])
        y_mm = float(args["y_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumentos 'x_mm'/'y_mm' inválidos: {err}").format(err=exc)
        ) from exc

    try:
        rotation_deg = float(args.get("rotation_deg", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumento 'rotation_deg' inválido: {err}").format(err=exc)
        ) from exc

    try:
        fp = pcbnew.FootprintLoad(library_path, footprint_name)
    except Exception as exc:
        raise RuntimeError(
            _("Erro ao carregar o footprint '{name}' de '{path}': {err}").format(
                name=footprint_name, path=library_path, err=exc
            )
        ) from exc
    if fp is None:
        raise RuntimeError(
            _("Footprint '{name}' não encontrado em '{path}'.").format(
                name=footprint_name, path=library_path
            )
        )

    board.Add(fp)
    fp.SetReference(reference)
    value = args.get("value")
    if value:
        fp.SetValue(str(value))
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm)))
    fp.SetOrientationDegrees(rotation_deg)
    _refresh(pcbnew)

    return _(
        "Componente '{reference}' ({footprint_name}) adicionado à placa em "
        "({x:.3f}, {y:.3f}) mm. Guarde a placa (Ctrl+S) para persistir a "
        "alteração."
    ).format(reference=reference, footprint_name=footprint_name, x=x_mm, y=y_mm)


def delete_footprint(args: dict) -> str:
    """Remove an existing footprint from the board. Destructive — gated by
    the same approval dialog as every other write tool (read_only=False)."""
    pcbnew, board = _get_board()
    args = args or {}
    reference = _require_reference(args)
    fp = _find_footprint(board, reference)
    board.Remove(fp)
    _refresh(pcbnew)

    return _(
        "Componente '{reference}' removido da placa. Guarde a placa (Ctrl+S) "
        "para persistir a alteração."
    ).format(reference=reference)


def add_track(args: dict) -> str:
    """Add a new copper track segment between two absolute points, in mm,
    on a given copper layer. If 'net_name' is given and doesn't exist yet
    on the board, it is created first."""
    pcbnew, board = _get_board()
    args = args or {}

    try:
        start_x_mm = float(args["start_x_mm"])
        start_y_mm = float(args["start_y_mm"])
        end_x_mm = float(args["end_x_mm"])
        end_y_mm = float(args["end_y_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumentos de coordenadas da trilha inválidos: {err}").format(err=exc)
        ) from exc

    layer_id = _require_layer_id(board, args)

    try:
        width_mm = float(args.get("width_mm", 0.25) or 0.25)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumento 'width_mm' inválido: {err}").format(err=exc)
        ) from exc

    net_name = args.get("net_name")

    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(start_x_mm), pcbnew.FromMM(start_y_mm)))
    track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(end_x_mm), pcbnew.FromMM(end_y_mm)))
    track.SetWidth(pcbnew.FromMM(width_mm))
    track.SetLayer(layer_id)
    if net_name:
        net = _find_or_create_net(pcbnew, board, net_name)
        track.SetNet(net)

    board.Add(track)
    _refresh(pcbnew)

    uuid_str = track.m_Uuid.AsString()
    return _(
        "Trilha adicionada em '{layer}' de ({sx:.3f}, {sy:.3f}) mm a "
        "({ex:.3f}, {ey:.3f}) mm, largura {width:.3f} mm (uuid: {uuid}). "
        "Guarde a placa (Ctrl+S) para persistir a alteração."
    ).format(
        layer=args.get("layer"),
        sx=start_x_mm,
        sy=start_y_mm,
        ex=end_x_mm,
        ey=end_y_mm,
        width=width_mm,
        uuid=uuid_str,
    )


def delete_track(args: dict) -> str:
    """Delete a track segment or via by its stable UUID (see list_tracks).
    Never rely on a positional index — board.GetTracks() does not preserve
    insertion order."""
    pcbnew, board = _get_board()
    args = args or {}
    uuid = args.get("uuid")
    if not uuid:
        raise RuntimeError(_("Falta o argumento 'uuid'."))

    target = None
    for item in board.GetTracks():
        try:
            item_uuid = item.m_Uuid.AsString()
        except Exception:
            continue
        if item_uuid == uuid:
            target = item
            break

    if target is None:
        raise RuntimeError(
            _("Nenhuma trilha/via encontrada com uuid '{uuid}'.").format(uuid=uuid)
        )

    try:
        is_via = target.GetClass() == "PCB_VIA"
    except Exception:
        is_via = False
    kind = _("Via") if is_via else _("Trilha")

    board.Remove(target)
    _refresh(pcbnew)

    return _(
        "{kind} com uuid '{uuid}' removida da placa. Guarde a placa (Ctrl+S) "
        "para persistir a alteração."
    ).format(kind=kind, uuid=uuid)


def add_via(args: dict) -> str:
    """Add a new through-hole via at an absolute position, in mm. Blind/
    buried/microvia support is out of scope — through-hole only. If
    'net_name' is given and doesn't exist yet on the board, it is created
    first."""
    pcbnew, board = _get_board()
    args = args or {}

    try:
        x_mm = float(args["x_mm"])
        y_mm = float(args["y_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumentos 'x_mm'/'y_mm' inválidos: {err}").format(err=exc)
        ) from exc

    try:
        drill_mm = float(args.get("drill_mm", 0.3) or 0.3)
        width_mm = float(args.get("width_mm", 0.6) or 0.6)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumentos 'drill_mm'/'width_mm' inválidos: {err}").format(err=exc)
        ) from exc

    net_name = args.get("net_name")

    via = pcbnew.PCB_VIA(board)
    via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm)))
    via.SetDrill(pcbnew.FromMM(drill_mm))
    via.SetWidth(pcbnew.FromMM(width_mm))
    if net_name:
        net = _find_or_create_net(pcbnew, board, net_name)
        via.SetNet(net)

    board.Add(via)
    _refresh(pcbnew)

    uuid_str = via.m_Uuid.AsString()
    return _(
        "Via adicionada em ({x:.3f}, {y:.3f}) mm, furo {drill:.3f} mm, "
        "diâmetro {width:.3f} mm (uuid: {uuid}). Guarde a placa (Ctrl+S) "
        "para persistir a alteração."
    ).format(x=x_mm, y=y_mm, drill=drill_mm, width=width_mm, uuid=uuid_str)


def set_pad_net(args: dict) -> str:
    """Reassign which net a single pad of an existing footprint belongs to.
    Implemented as reassigning ONE pad's net — NOT a full ratsnest/airwire
    rebuild (out of scope). The result message always tells the user to
    re-run DRC and visually check the ratsnest afterwards."""
    pcbnew, board = _get_board()
    args = args or {}
    reference = _require_reference(args)
    pad_number = args.get("pad_number")
    if not pad_number:
        raise RuntimeError(_("Falta o argumento 'pad_number'."))
    net_name = args.get("net_name")
    if not net_name:
        raise RuntimeError(_("Falta o argumento 'net_name'."))

    fp = _find_footprint(board, reference)
    pad = fp.FindPadByNumber(str(pad_number))
    if pad is None:
        raise RuntimeError(
            _("Pad '{pad}' não encontrado no componente '{reference}'.").format(
                pad=pad_number, reference=reference
            )
        )

    net = _find_or_create_net(pcbnew, board, net_name)
    pad.SetNet(net)
    _refresh(pcbnew)

    return _(
        "Pad {pad} de {reference} ligado à net '{net}'. Guarde a placa "
        "(Ctrl+S) para persistir a alteração. Recomenda-se correr novamente "
        "o DRC e verificar visualmente o ratsnest — esta ferramenta não "
        "reconstrói automaticamente a visualização de conectividade."
    ).format(pad=pad_number, reference=reference, net=net_name)


def create_board_from_scratch(args: dict) -> str:
    """Create a brand-new, empty .kicad_pcb file on disk with a rectangular
    board outline.

    IMPORTANT — unlike every other tool in this module, this does NOT touch
    the currently open board in the live PCB editor at all (no
    ``pcbnew.GetBoard()`` call): it creates a completely separate new file
    on disk via ``pcbnew.NewBoard()``. The user must open the new file
    themselves afterwards (File > Open Board) — this tool never replaces or
    reloads whatever is currently open in the editor.
    """
    try:
        import pcbnew
    except ImportError as exc:
        raise RuntimeError(
            _("pcbnew indisponível — esta ferramenta só funciona dentro do KiCad")
        ) from exc

    args = args or {}
    path = args.get("path")
    if not path:
        raise RuntimeError(_("Falta o argumento 'path'."))

    import os

    if os.path.exists(path):
        raise RuntimeError(
            _(
                "Já existe um ficheiro em '{path}' — esta ferramenta não "
                "substitui uma placa existente."
            ).format(path=path)
        )

    try:
        width_mm = float(args["width_mm"])
        height_mm = float(args["height_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumentos 'width_mm'/'height_mm' inválidos: {err}").format(err=exc)
        ) from exc

    new_board = pcbnew.NewBoard(path)

    shape = pcbnew.PCB_SHAPE(new_board)
    shape.SetShape(pcbnew.SHAPE_T_RECT)
    shape.SetLayer(new_board.GetLayerID("Edge.Cuts"))
    shape.SetStart(pcbnew.VECTOR2I(0, 0))
    shape.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(width_mm), pcbnew.FromMM(height_mm)))
    new_board.Add(shape)

    pcbnew.SaveBoard(path, new_board)

    return _(
        "Nova placa criada em '{path}' com contorno de {width:.3f} x "
        "{height:.3f} mm. Esta operação NÃO afeta a placa atualmente aberta "
        "no editor — abra o novo ficheiro manualmente (File > Open Board) "
        "para o editar."
    ).format(path=path, width=width_mm, height=height_mm)


def register_kicad_write_tools(registry: ActionRegistry) -> None:
    """Register all board-mutating KiCad tools on the given ActionRegistry.

    Kept as a SEPARATE opt-in call from register_kicad_tools() (read-only) —
    see chat_action.py::run_chat() for where both are wired together — so a
    future caller that wants read-only-only chat can still get that by
    calling just the one function.
    """

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="move_footprint",
                description=(
                    "Call this to MOVE a component/footprint to a new absolute "
                    "position on the board, in millimeters. This MODIFIES the "
                    "board and requires explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "reference": {
                            "type": "string",
                            "description": "Reference designator, e.g. 'R1'.",
                        },
                        "x_mm": {
                            "type": "number",
                            "description": "Absolute X position in mm.",
                        },
                        "y_mm": {
                            "type": "number",
                            "description": "Absolute Y position in mm.",
                        },
                    },
                    "required": ["reference", "x_mm", "y_mm"],
                },
            ),
            handler=move_footprint,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="rotate_footprint",
                description=(
                    "Call this to ROTATE a component/footprint to an absolute "
                    "angle in degrees. This MODIFIES the board and requires "
                    "explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "reference": {
                            "type": "string",
                            "description": "Reference designator, e.g. 'R1'.",
                        },
                        "angle_deg": {
                            "type": "number",
                            "description": "Absolute rotation angle in degrees.",
                        },
                    },
                    "required": ["reference", "angle_deg"],
                },
            ),
            handler=rotate_footprint,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="set_footprint_value",
                description=(
                    "Call this to CHANGE a component's displayed value text "
                    "(e.g. a resistor's resistance, a capacitor's capacitance). "
                    "This MODIFIES the board and requires explicit user "
                    "approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "reference": {
                            "type": "string",
                            "description": "Reference designator, e.g. 'R1'.",
                        },
                        "value": {
                            "type": "string",
                            "description": "New value text, e.g. '10k'.",
                        },
                    },
                    "required": ["reference", "value"],
                },
            ),
            handler=set_footprint_value,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="add_footprint",
                description=(
                    "Call this to ADD a new component/footprint to the board, "
                    "loaded from a footprint library folder (.pretty), under a "
                    "new reference designator. This MODIFIES the board and "
                    "requires explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "library_path": {
                            "type": "string",
                            "description": (
                                "Path to the footprint library folder, ending "
                                "in '.pretty'."
                            ),
                        },
                        "footprint_name": {
                            "type": "string",
                            "description": (
                                "Name of the footprint inside the library, "
                                "e.g. 'R_0603_1608Metric'."
                            ),
                        },
                        "reference": {
                            "type": "string",
                            "description": (
                                "Reference designator for the new component, "
                                "e.g. 'R5'. Must not already exist on the "
                                "board."
                            ),
                        },
                        "x_mm": {
                            "type": "number",
                            "description": "Absolute X position in mm.",
                        },
                        "y_mm": {
                            "type": "number",
                            "description": "Absolute Y position in mm.",
                        },
                        "value": {
                            "type": "string",
                            "description": "Optional value text, e.g. '10k'.",
                        },
                        "rotation_deg": {
                            "type": "number",
                            "description": "Optional rotation in degrees (default 0).",
                        },
                    },
                    "required": [
                        "library_path",
                        "footprint_name",
                        "reference",
                        "x_mm",
                        "y_mm",
                    ],
                },
            ),
            handler=add_footprint,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="delete_footprint",
                description=(
                    "Call this to DELETE/REMOVE an existing component/footprint "
                    "from the board. This is a DESTRUCTIVE operation, MODIFIES "
                    "the board and requires explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "reference": {
                            "type": "string",
                            "description": (
                                "Reference designator of the component to "
                                "remove, e.g. 'R1'."
                            ),
                        },
                    },
                    "required": ["reference"],
                },
            ),
            handler=delete_footprint,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="add_track",
                description=(
                    "Call this to ADD a new copper track segment between two "
                    "absolute points, on a given copper layer. This MODIFIES "
                    "the board and requires explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "start_x_mm": {
                            "type": "number",
                            "description": "Start X position in mm.",
                        },
                        "start_y_mm": {
                            "type": "number",
                            "description": "Start Y position in mm.",
                        },
                        "end_x_mm": {
                            "type": "number",
                            "description": "End X position in mm.",
                        },
                        "end_y_mm": {
                            "type": "number",
                            "description": "End Y position in mm.",
                        },
                        "layer": {
                            "type": "string",
                            "description": (
                                "Copper layer name, e.g. 'F.Cu', 'B.Cu', "
                                "'In1.Cu'."
                            ),
                        },
                        "width_mm": {
                            "type": "number",
                            "description": "Track width in mm (default 0.25).",
                        },
                        "net_name": {
                            "type": "string",
                            "description": (
                                "Optional net name. Created if it doesn't yet "
                                "exist on the board."
                            ),
                        },
                    },
                    "required": [
                        "start_x_mm",
                        "start_y_mm",
                        "end_x_mm",
                        "end_y_mm",
                        "layer",
                    ],
                },
            ),
            handler=add_track,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="delete_track",
                description=(
                    "Call this to DELETE an existing track segment or via, "
                    "identified by its UUID (see list_tracks). This is a "
                    "DESTRUCTIVE operation, MODIFIES the board and requires "
                    "explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "uuid": {
                            "type": "string",
                            "description": (
                                "Full UUID string of the track/via, as "
                                "returned by list_tracks or add_track/add_via."
                            ),
                        },
                    },
                    "required": ["uuid"],
                },
            ),
            handler=delete_track,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="add_via",
                description=(
                    "Call this to ADD a new through-hole via at an absolute "
                    "position. This MODIFIES the board and requires explicit "
                    "user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "x_mm": {
                            "type": "number",
                            "description": "Absolute X position in mm.",
                        },
                        "y_mm": {
                            "type": "number",
                            "description": "Absolute Y position in mm.",
                        },
                        "drill_mm": {
                            "type": "number",
                            "description": "Drill diameter in mm (default 0.3).",
                        },
                        "width_mm": {
                            "type": "number",
                            "description": (
                                "Via outer diameter in mm (default 0.6)."
                            ),
                        },
                        "net_name": {
                            "type": "string",
                            "description": (
                                "Optional net name. Created if it doesn't yet "
                                "exist on the board."
                            ),
                        },
                    },
                    "required": ["x_mm", "y_mm"],
                },
            ),
            handler=add_via,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="set_pad_net",
                description=(
                    "Call this to REASSIGN which net a single component pad "
                    "is connected to. This MODIFIES the board and requires "
                    "explicit user approval. Does NOT rebuild the ratsnest "
                    "visualization automatically — re-run DRC / refresh "
                    "visually afterwards."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "reference": {
                            "type": "string",
                            "description": (
                                "Reference designator of the component, "
                                "e.g. 'U1'."
                            ),
                        },
                        "pad_number": {
                            "type": "string",
                            "description": "Pad number/name, e.g. '1', 'A1'.",
                        },
                        "net_name": {
                            "type": "string",
                            "description": (
                                "Net name to assign. Created if it doesn't "
                                "yet exist on the board."
                            ),
                        },
                    },
                    "required": ["reference", "pad_number", "net_name"],
                },
            ),
            handler=set_pad_net,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="create_board_from_scratch",
                description=(
                    "Call this to CREATE a brand-new .kicad_pcb file on disk "
                    "with a rectangular board outline. IMPORTANT: this does "
                    "NOT touch or modify the currently open board in the live "
                    "PCB editor — it creates a separate new file that the "
                    "user must open themselves afterwards (File > Open "
                    "Board). Fails if a file already exists at the target "
                    "path. Requires explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Target file path for the new .kicad_pcb "
                                "file. Must not already exist."
                            ),
                        },
                        "width_mm": {
                            "type": "number",
                            "description": "Board outline width in mm.",
                        },
                        "height_mm": {
                            "type": "number",
                            "description": "Board outline height in mm.",
                        },
                    },
                    "required": ["path", "width_mm", "height_mm"],
                },
            ),
            handler=create_board_from_scratch,
            read_only=False,
        )
    )
