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

Scope, kept deliberately small for a first version: reposition, rotate, and
change the displayed value of an EXISTING footprint. None of these touch
net/connectivity data (no track/via/zone edits, no adding or deleting
footprints) — that keeps the blast radius of a wrong LLM-proposed action
bounded to "a component is in the wrong place/orientation/labelled wrong",
never "a connection silently changed" or "a component vanished".
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
