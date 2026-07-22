"""
Write chat tools wrapping two small, self-contained third-party KiCad
plugins — "Thermal Relief Via" (John Hryb) and "Set Hole Diameter"
(seigedigital) — both installed via KiCad's own Plugin and Content Manager
(PCM), reached through ``_sibling_plugin.py``'s ``find_pcm_plugin_dir()``
like every other PCM-installed sibling in this package. Bundled into one
module because both are tiny (a single Python file each, no wx-tangled
logic) and operate on the same conceptual area (pads/vias).

VERIFIED FACTS (read directly from each installed plugin's single source
file today):

- ThermalReliefVia (``ThermalReliefViaAction.py``): its ``Run()`` loops
  every ``PCB_VIA`` on the board (``item.GetClass() == "PCB_VIA"``,
  the exact string this codebase already relies on elsewhere — see
  ``kicad_write_tools.py``) and, for every one with ``item.IsSelected()``,
  replaces it with a plated-through-hole PAD footprint of the same
  position/diameter/net (via its own ``_makeThPad`` helper), then removes
  the original via. Like every other GUI-selection-driven plugin already
  wrapped in this codebase, "selected" has no chat equivalent — this tool
  instead accepts the via's ``uuid`` (from ``list_tracks``, which already
  returns UUIDs for both tracks and vias) and operates on that ONE via.
  ``_makeThPad`` itself is a plain, non-wx method with no other hidden
  state dependency — reimplemented here directly (copying its exact 8-line
  body) rather than instantiating the plugin's own
  ``pcbnew.ActionPlugin`` subclass, since ``_makeThPad`` is an instance
  method with no constructor-set state it depends on.
- SetHoleDiameter (``plugin.py``): its module-level ``set_hole_diameter(pcb,
  diameter)`` function is a plain, GUI-free function (imported directly via
  ``_load``, never touching the file's ``DiameterDialog``/
  ``SetHoleDiameterPlugin`` classes) that sets EVERY pad's drill size on the
  WHOLE BOARD to the same ``diameter`` (mm), via
  ``pad.SetDrillSize(pcbnew.VECTOR2I_MM(diameter, diameter))`` — a real,
  intentionally blunt, board-wide operation, not per-footprint or
  per-selection. This tool's description says so explicitly and in strong
  terms — an LLM must never call this expecting it to only affect one
  component; it will resize every hole on the board, including on
  components where that is very likely wrong (e.g. mounting holes,
  connectors with fixed mechanical pad sizes).

Same lazy-import + RuntimeError-not-raw-exception + i18n ``_()`` trampoline
conventions as every other tool module in this package.
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

try:
    from ._sibling_plugin import (
        SiblingPluginNotFoundError,
        find_pcm_plugin_dir,
        load_sibling_module,
    )
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from actions._sibling_plugin import (  # type: ignore[no-redef]
        SiblingPluginNotFoundError,
        load_sibling_module,
        find_pcm_plugin_dir,
    )

try:
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - fallback for flat/test imports
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


_THERMAL_RELIEF_PACKAGE = "_sibling_thermal_relief_via"
_THERMAL_RELIEF_IDENTIFIER = "com_github_JohnHryb_ThermalReliefVia"

_SET_HOLE_DIAMETER_PACKAGE = "_sibling_set_hole_diameter"
_SET_HOLE_DIAMETER_IDENTIFIER = "com_github_seigedigital_setholediameterpluginforkicad"


def _thermal_relief_installed() -> None:
    """Raises SiblingPluginNotFoundError if not installed — no submodule
    needs importing (the replacement logic is reimplemented directly, see
    module docstring), this is purely an honesty check before mutating the
    board so the tool never silently "succeeds" using logic that doesn't
    actually match the credited plugin's own presence on this machine."""
    find_pcm_plugin_dir(_THERMAL_RELIEF_IDENTIFIER)


def _load_set_hole_diameter():
    plugins_dir = find_pcm_plugin_dir(_SET_HOLE_DIAMETER_IDENTIFIER)
    return load_sibling_module(_SET_HOLE_DIAMETER_PACKAGE, plugins_dir, "plugin")


def _not_installed_message(name: str) -> str:
    return _(
        "O plugin '{name}' não está instalado nesta máquina — esta "
        "ferramenta precisa dele."
    ).format(name=name)


def _get_board():
    """Lazily import pcbnew and return (pcbnew, board). Mirrors
    kicad_tools.py's _get_board()."""
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


def _find_via_by_uuid(board, uuid_str: str):
    for item in board.GetTracks():
        if item.GetClass() == "PCB_VIA" and item.m_Uuid.AsString() == uuid_str:
            return item
    return None


def replace_via_with_thermal_relief_pad(args: dict) -> str:
    """Replace ONE via (identified by its UUID, from list_tracks) with a
    plated-through-hole pad of the same position/diameter/net — useful for
    soldering thermal relief, via the sibling Thermal Relief Via plugin's
    real replacement logic.

    Required args:
        uuid: str — the via's UUID, exactly as returned by list_tracks.

    Removes the original via and adds a new single-pad footprint in its
    place. Mutates the LIVE board; not auto-saved (Ctrl+S persists it).
    """
    args = args or {}
    uuid_str = args.get("uuid")
    if not uuid_str:
        raise RuntimeError(_("Falta o argumento 'uuid'."))

    try:
        _thermal_relief_installed()
    except SiblingPluginNotFoundError:
        return _not_installed_message("Thermal Relief Via")

    pcbnew, board = _get_board()
    via = _find_via_by_uuid(board, uuid_str)
    if via is None:
        raise RuntimeError(
            _("Nenhuma via encontrada com uuid '{uuid}'.").format(uuid=uuid_str)
        )

    position = via.GetPosition()
    drill = via.GetDrillValue()
    width = via.GetWidth()
    net = via.GetNetCode()

    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetPosition(position)
    pad = pcbnew.PAD(footprint)
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(pcbnew.VECTOR2I(width, width))
    pad.SetDrillSize(pcbnew.VECTOR2I(drill, drill))
    pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
    pad.SetNetCode(net)
    footprint.Add(pad)
    board.Add(footprint)
    board.Remove(via)

    try:
        pcbnew.Refresh()
    except Exception:
        pass

    return _(
        "Via '{uuid}' substituída por um pad de relevo térmico "
        "({drill_mm:.3f} mm de furo, {width_mm:.3f} mm de diâmetro). "
        "Guarde a placa (Ctrl+S) para persistir a alteração."
    ).format(
        uuid=uuid_str,
        drill_mm=pcbnew.ToMM(drill),
        width_mm=pcbnew.ToMM(width),
    )


def set_all_pad_hole_diameters(args: dict) -> str:
    """Set EVERY pad's hole (drill) diameter on the WHOLE currently open
    board to the same value, via the sibling Set Hole Diameter plugin's
    real function. This is a deliberately blunt, board-wide operation —
    NOT per-footprint or per-selection — matching the sibling plugin's own
    real behavior exactly.

    Required args:
        diameter_mm: number — new hole diameter for every pad, mm.

    Mutates the LIVE board; not auto-saved (Ctrl+S persists it). Use with
    real caution: this resizes mounting holes, connector pads, and every
    other pad indiscriminately, not just a chosen component's pads.
    """
    args = args or {}
    try:
        diameter_mm = float(args["diameter_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumento 'diameter_mm' inválido: {err}").format(err=exc)
        ) from exc
    if diameter_mm <= 0:
        raise RuntimeError(_("'diameter_mm' deve ser maior que zero."))

    try:
        set_hole_diameter_mod = _load_set_hole_diameter()
    except SiblingPluginNotFoundError:
        return _not_installed_message("Set Hole Diameter")
    except ImportError as exc:
        return _("Erro ao carregar o Set Hole Diameter: {err}").format(err=exc)

    pcbnew, board = _get_board()
    pad_count = sum(fp.GetPadCount() for fp in board.GetFootprints())

    set_hole_diameter_mod.set_hole_diameter(board, diameter_mm)

    try:
        pcbnew.Refresh()
    except Exception:
        pass

    return _(
        "Diâmetro de furo alterado para {diameter_mm:.3f} mm em {pad_count} "
        "pad(s), em TODA a placa. Guarde a placa (Ctrl+S) para persistir a "
        "alteração."
    ).format(diameter_mm=diameter_mm, pad_count=pad_count)


def register_via_pad_tools(registry: ActionRegistry) -> None:
    """Register both via/pad-backed tools on the given ActionRegistry.

    Safe to call even when either sibling plugin isn't installed — each
    handler reports that honestly at call time, matching every other
    sibling-plugin wrapper in this package.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="replace_via_with_thermal_relief_pad",
                description=(
                    "Call this to replace ONE specific via (identified by "
                    "its uuid, from list_tracks) with a plated-through-hole "
                    "pad of the same position/diameter/net — useful for "
                    "thermal relief when hand-soldering. This MODIFIES the "
                    "board and requires explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "uuid": {
                            "type": "string",
                            "description": "Via uuid, from list_tracks.",
                        },
                    },
                    "required": ["uuid"],
                },
            ),
            handler=replace_via_with_thermal_relief_pad,
            read_only=False,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="set_all_pad_hole_diameters",
                description=(
                    "Call this ONLY when the user explicitly wants EVERY "
                    "pad's hole diameter on the WHOLE board changed to the "
                    "same value — this is a blunt, board-wide operation, "
                    "NOT limited to one component. Never call this for a "
                    "request about a single component's pads (use other "
                    "tools for that instead). This MODIFIES the board and "
                    "requires explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "diameter_mm": {
                            "type": "number",
                            "description": "New hole diameter for EVERY pad on the board, mm.",
                        },
                    },
                    "required": ["diameter_mm"],
                },
            ),
            handler=set_all_pad_hole_diameters,
            read_only=False,
        )
    )
