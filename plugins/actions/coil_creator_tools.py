"""
Write chat tool wrapping the sibling Coil Creator plugin (by
DIaLOGIKa-GmbH), installed via KiCad's own Plugin and Content Manager
(PCM) — like ``kicad_parasitics_tools.py``, not one of our own forks, so it
is reached through ``_sibling_plugin.py``'s ``find_pcm_plugin_dir()``
instead of a per-fork ``_find_sibling_plugins_dir()``: a PCM install has no
nested ``plugins/`` subfolder, the identifier folder itself IS the package
root. See ``kicad_parasitics_tools.py`` for the general sibling-plugin
wrapping template this module otherwise follows, and ``kicad_tools.py``'s
``_get_board()`` for the lazy-``pcbnew``-import pattern used here.

Coil Creator generates a spiral PCB coil (wireless charging, RFID, small
transformers, antennas) as real track/via/pad geometry. Unlike
KiCad-Parasitics, this is a WRITE tool (``read_only=False``): it adds a
brand-new footprint to the currently open board.

Sibling plugin integration point — read directly from
``lib/coilgenerator.py`` and ``plugin.py`` before writing this module:
``lib/coilgenerator.py::generate(...)`` is a pure, GUI-free function. It
does NOT return a list of track/via objects, nor raw fragments needing
manual assembly — it reads the shipped ``dynamic/template.kicad_mod`` and
substitutes NAME/LINES/ARCS/VIAS/PADS/UUID/NET_TIE placeholders, returning
one complete, well-formed ``.kicad_mod`` FOOTPRINT file as S-expression
TEXT. The sibling's own GUI (``plugin.py::CoilGeneratorUI``) never adds
this to the live board via the pcbnew Python API either: its "Generate
Coil" button copies the text to the clipboard and simulates Ctrl+V into the
PCB editor window (relying on KiCad's own paste-as-footprint clipboard
handling), and its "Save as Project Footprint" button writes it to a
``pcb_coils/<name>.kicad_mod`` file and registers a new project footprint
library — neither path is scriptable from chat.

DESIGN NOTE — board integration via FootprintLoad, exactly like
``kicad_write_tools.py``'s own ``add_footprint()``: reading
``generate_coil_spiral()``/``generate_pads()`` confirms every point the
generator builds is a radius around the origin (0, 0) — the whole coil,
including its breakout pads, is FOOTPRINT-LOCAL geometry centered on
origin. That means placing it at an arbitrary board position needs NO
manual S-expression coordinate math: this tool writes the generated text
to a ``.kicad_mod`` file inside a throwaway temporary ``.pretty`` library
folder (KiCad's plugin-type autodetection for ``pcbnew.FootprintLoad()``
keys off the ``.pretty`` path suffix — see ``PCB_IO_MGR.GuessPluginTypeFromLibPath``
in the installed ``pcbnew.py`` stub), loads it back with
``pcbnew.FootprintLoad()`` (the very same API ``add_footprint`` already
uses for user-supplied libraries), ``board.Add()``s the resulting
FOOTPRINT, and calls ``fp.SetPosition()`` — which rigidly translates every
one of its child items (pads/fp_lines/fp_arcs) together, exactly like
moving any hand-placed footprint. The temporary library folder is deleted
immediately afterward; only the FOOTPRINT object pcbnew already parsed
into memory survives on the board. This turns what the task brief flagged
as a possible awkward limitation into a clean, exact reuse of an existing
pattern — no coordinate transform of the S-expression text is needed at
all.

Two different "names" are involved and must not be confused: the raw,
UNSANITIZED ``coil_name`` argument becomes the footprint's internal
"{NAME}" template substitution (its Value text), exactly as the sibling's
own ``generate()`` uses it, while a SANITIZED version (alphanumerics plus
space/dot/underscore kept, everything else stripped) is used only as the
throwaway ``.kicad_mod`` filename passed to ``FootprintLoad``. The
sanitizing rule replicates ``plugin.py``'s own tiny ``get_safe_name()``
helper rather than importing it directly — ``plugin.py`` imports ``wx``
and ``pcbnew`` at module scope, a side effect this module's lazy-import
convention (see ``_get_board()``) must not trigger just to reuse a
one-line helper.

Reference designator: the generated footprint keeps the sibling's own
template default ("REF**", the same placeholder KiCad assigns to any
just-pasted footprint before annotation) — this tool does not invent a
synthetic "COIL1"/"COIL2" numbering scheme absent from the real plugin's
own output, so placing more than one coil on a board leaves duplicate
"REF**" references exactly like a manual clipboard paste would. The result
message says so explicitly rather than hiding it.

Same lazy-import + RuntimeError-only-failures conventions as the rest of
this codebase: nothing sibling-plugin- or ``pcbnew``-related is imported at
module scope, so this module (and its tests) import cleanly with neither
present at all.
"""

from __future__ import annotations

import tempfile
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
    from ._sibling_plugin import (
        SiblingPluginNotFoundError,
        find_pcm_plugin_dir,
        load_sibling_module,
    )
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from actions._sibling_plugin import (  # type: ignore[no-redef]
        SiblingPluginNotFoundError,
        find_pcm_plugin_dir,
        load_sibling_module,
    )

try:
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - fallback for flat/test imports
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


_PACKAGE_NAME = "_sibling_coil_creator"

# PCM install identifier, exactly as it appears under
# Documents\KiCad\<version>\3rdparty\plugins\ — no nested "plugins/" folder,
# unlike our own forks (see find_pcm_plugin_dir()'s own docstring).
_SIBLING_IDENTIFIER = "com_github_DIaLOGIKa-GmbH_kicad-coil-creator"


def _load(submodule: str):
    plugins_dir = find_pcm_plugin_dir(_SIBLING_IDENTIFIER)
    return load_sibling_module(_PACKAGE_NAME, plugins_dir, submodule)


def _not_installed_message() -> str:
    return _(
        "O plugin Coil Creator não está instalado nesta máquina — esta "
        "ferramenta precisa dele."
    )


def _get_board():
    """Lazily import pcbnew and return (pcbnew, board) for the currently
    open board. Same lazy-import + RuntimeError pattern as
    kicad_tools.py's/kicad_write_tools.py's _get_board()."""
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


def _refresh(pcbnew_module) -> None:
    """Best-effort PCB editor redraw after a mutation. Never lets a failed
    redraw fail the mutation itself — the board change already happened."""
    try:
        pcbnew_module.Refresh()
    except Exception:
        pass


def _get_safe_name(name: str, keepcharacters=(" ", ".", "_")) -> str:
    """Replicates plugin.py's own get_safe_name() rule exactly (module
    docstring explains why this is a local copy, not an import)."""
    return "".join(c for c in name if c.isalnum() or c in keepcharacters).rstrip()


def _default_layer_names(layer_count: int) -> list[str]:
    """Same naming scheme plugin.py's own _handle_coil_generation() builds
    before calling generate(): every layer defaults to "In{index}.Cu", then
    the first is overwritten to "F.Cu" and — if there is more than one
    layer — the last to "B.Cu". Reproduced here at exactly ``layer_count``
    length since this tool has no live board to read a real copper layer
    count from (the sibling's own GUI instead sizes this array from
    ``board.GetCopperLayerCount()``, which is always >= 2 in practice)."""
    names = [f"In{i}.Cu" for i in range(layer_count)]
    names[0] = "F.Cu"
    if layer_count > 1:
        names[-1] = "B.Cu"
    return names


def generate_pcb_coil(args: dict) -> str:
    """Generate a spiral PCB coil via the sibling Coil Creator plugin's
    real (pure, GUI-free) generator and add it as a new footprint to the
    currently open board at an absolute position.

    Required args:
        layer_count: int — number of coil layers (>= 1).
        turns_per_layer: int — minimum turns per layer (>= 1); connecting
            to vias may introduce up to one more turn.
        trace_width_mm, trace_spacing_mm: number — coil trace geometry, mm.
        via_diameter_mm, via_drill_mm: number — connecting-via geometry, mm.
        outer_diameter_mm: number — desired outer coil diameter, mm. Coil
            generation proceeds from outside to inside; too small a value
            for the requested turns/vias may make generation fail.
        coil_name: str — becomes the footprint's Value text (the sibling's
            own {NAME} template substitution), unsanitized.
        x_mm, y_mm: number — absolute board position for the coil's center
            (the generated geometry is centered on its own local origin, so
            this just becomes the footprint's position, same as placing any
            other footprint).

    Optional args:
        wrap_clockwise: bool — coil winding direction. Default True.
        layer_names: list of str — KiCad layer names, length >=
            layer_count. Defaults to the sibling's own GUI naming scheme
            ("F.Cu", "In1.Cu", ..., "B.Cu") — see _default_layer_names().

    Never auto-saves the board — the user's own Ctrl+S persists the
    change, same as every other write tool in this codebase. Requires the
    Coil Creator plugin to be installed; reports honestly if it is missing.
    """
    args = args or {}

    try:
        layer_count = int(args["layer_count"])
        turns_per_layer = int(args["turns_per_layer"])
        trace_width_mm = float(args["trace_width_mm"])
        trace_spacing_mm = float(args["trace_spacing_mm"])
        via_diameter_mm = float(args["via_diameter_mm"])
        via_drill_mm = float(args["via_drill_mm"])
        outer_diameter_mm = float(args["outer_diameter_mm"])
        x_mm = float(args["x_mm"])
        y_mm = float(args["y_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            _(
                "Faltam ou são inválidos argumentos obrigatórios: "
                "layer_count, turns_per_layer, trace_width_mm, "
                "trace_spacing_mm, via_diameter_mm, via_drill_mm, "
                "outer_diameter_mm, x_mm, y_mm."
            )
        ) from exc

    coil_name = args.get("coil_name")
    if not coil_name or not str(coil_name).strip():
        raise RuntimeError(_("Falta o argumento 'coil_name'."))
    coil_name = str(coil_name)

    if layer_count < 1:
        raise RuntimeError(_("'layer_count' deve ser >= 1."))
    if turns_per_layer < 1:
        raise RuntimeError(_("'turns_per_layer' deve ser >= 1."))

    wrap_clockwise = bool(args.get("wrap_clockwise", True))

    raw_layer_names = args.get("layer_names")
    if raw_layer_names is not None:
        layer_names = [str(n) for n in raw_layer_names]
        if len(layer_names) < layer_count:
            raise RuntimeError(
                _(
                    "'layer_names' tem {given} nome(s), mas 'layer_count' "
                    "pede {needed}."
                ).format(given=len(layer_names), needed=layer_count)
            )
    else:
        layer_names = _default_layer_names(layer_count)

    safe_name = _get_safe_name(coil_name)
    if not safe_name:
        raise RuntimeError(
            _(
                "'coil_name' não contém nenhum carácter válido para nome de "
                "ficheiro (letras, números, espaço, '.', '_')."
            )
        )

    pcbnew, board = _get_board()

    try:
        coilgenerator_mod = _load("lib.coilgenerator")
    except SiblingPluginNotFoundError:
        return _not_installed_message()
    except ImportError as exc:
        return _("Erro ao carregar o Coil Creator: {err}").format(err=exc)

    try:
        footprint_text = coilgenerator_mod.generate(
            layer_count,
            wrap_clockwise,
            turns_per_layer,
            trace_width_mm,
            trace_spacing_mm,
            via_diameter_mm,
            via_drill_mm,
            outer_diameter_mm,
            coil_name,
            layer_names,
        )
    except Exception as exc:
        raise RuntimeError(
            _(
                "Erro ao gerar a geometria da bobina — verifique os "
                "parâmetros (ex.: diâmetro exterior demasiado pequeno para "
                "o número de voltas/vias pedido): {err}"
            ).format(err=exc)
        ) from exc

    with tempfile.TemporaryDirectory(suffix=".pretty") as tmp_dir:
        footprint_path = Path(tmp_dir) / f"{safe_name}.kicad_mod"
        footprint_path.write_text(footprint_text, encoding="utf-8")

        try:
            fp = pcbnew.FootprintLoad(tmp_dir, safe_name)
        except Exception as exc:
            raise RuntimeError(
                _(
                    "Erro ao carregar a bobina gerada como footprint: {err}"
                ).format(err=exc)
            ) from exc

    if fp is None:
        raise RuntimeError(
            _("Não foi possível carregar a bobina gerada como footprint.")
        )

    board.Add(fp)
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm)))
    _refresh(pcbnew)

    return _(
        "Bobina '{name}' adicionada à placa em ({x:.3f}, {y:.3f}) mm "
        "({layers} camada(s), {turns} volta(s)/camada, diâmetro exterior "
        "{outer:.3f} mm). A referência ficou 'REF**' (mesmo comportamento "
        "do plugin original) — anote/renumere manualmente se colidir com "
        "outra bobina já na placa. Guarde a placa (Ctrl+S) para persistir "
        "a alteração."
    ).format(
        name=coil_name,
        x=x_mm,
        y=y_mm,
        layers=layer_count,
        turns=turns_per_layer,
        outer=outer_diameter_mm,
    )


def register_coil_creator_tools(registry: ActionRegistry) -> None:
    """Register the Coil Creator-backed tool on the given ActionRegistry.

    Safe to call even when Coil Creator isn't installed — the handler
    itself reports that honestly at call time instead of failing
    registration (same pattern as register_kicad_parasitics_tools()/
    register_emc_emi_tools()/register_libforge_tools()). NOT wired into
    chat_action.py by this module — a separate integration pass does that.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="generate_pcb_coil",
                description=(
                    "Call this when the user asks to generate/add a spiral "
                    "PCB coil — for wireless charging, RFID, a small "
                    "transformer, or an antenna — as real track/via "
                    "geometry on the currently open board. Runs the "
                    "sibling Coil Creator plugin's real (pure) coil "
                    "generator and adds the result as a new footprint at "
                    "the given (x_mm, y_mm) board position. Mutates the "
                    "live board (does not auto-save). Requires the Coil "
                    "Creator plugin to be installed; reports honestly if "
                    "it is missing."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "layer_count": {
                            "type": "integer",
                            "description": "Number of coil layers (>= 1).",
                        },
                        "turns_per_layer": {
                            "type": "integer",
                            "description": (
                                "Minimum turns per layer (>= 1); connecting "
                                "to vias may add up to one more turn."
                            ),
                        },
                        "trace_width_mm": {
                            "type": "number",
                            "description": "Coil trace width, mm.",
                        },
                        "trace_spacing_mm": {
                            "type": "number",
                            "description": "Spacing between coil traces, mm.",
                        },
                        "via_diameter_mm": {
                            "type": "number",
                            "description": "Connecting via outer diameter, mm.",
                        },
                        "via_drill_mm": {
                            "type": "number",
                            "description": "Connecting via drill diameter, mm.",
                        },
                        "outer_diameter_mm": {
                            "type": "number",
                            "description": (
                                "Desired outer coil diameter, mm. Generation "
                                "goes outside-in; too small a value for the "
                                "requested turns/vias may fail."
                            ),
                        },
                        "coil_name": {
                            "type": "string",
                            "description": (
                                "Coil name — becomes the footprint's Value "
                                "text."
                            ),
                        },
                        "x_mm": {
                            "type": "number",
                            "description": "Board X position for the coil's center, mm.",
                        },
                        "y_mm": {
                            "type": "number",
                            "description": "Board Y position for the coil's center, mm.",
                        },
                        "wrap_clockwise": {
                            "type": "boolean",
                            "description": "Coil winding direction. Default true.",
                        },
                        "layer_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "KiCad layer names, length >= layer_count. "
                                "Defaults to the sibling plugin's own GUI "
                                "scheme (F.Cu, In1.Cu, ..., B.Cu)."
                            ),
                        },
                    },
                    "required": [
                        "layer_count",
                        "turns_per_layer",
                        "trace_width_mm",
                        "trace_spacing_mm",
                        "via_diameter_mm",
                        "via_drill_mm",
                        "outer_diameter_mm",
                        "coil_name",
                        "x_mm",
                        "y_mm",
                    ],
                },
            ),
            handler=generate_pcb_coil,
            read_only=False,
        )
    )
