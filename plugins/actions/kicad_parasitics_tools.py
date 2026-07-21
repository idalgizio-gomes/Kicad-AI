"""
Read-only chat tool wrapping the sibling KiCad-Parasitics plugin (by
Steffen-W), installed via KiCad's own Plugin and Content Manager (PCM) —
unlike LibForge/EMC-EMI (our own forks), so it is reached through
``_sibling_plugin.py``'s ``find_pcm_plugin_dir()`` instead of a per-caller
``_find_sibling_plugins_dir()``: a PCM install has no nested ``plugins/``
subfolder, the identifier folder itself IS the package root. See
``emc_emi_tools.py`` for the general sibling-plugin wrapping template this
module otherwise follows, and ``kicad_tools.py``'s ``_get_board()`` for the
lazy-``pcbnew``-import pattern used here.

KiCad-Parasitics estimates the parasitic DC resistance and AC impedance (at
several frequencies) of the copper path between two points on the board —
this complements the EMC-EMI Analyzer's inductive/capacitive coupling
analysis with a resistive/impedance path view.

DESIGN NOTE — no chat-facing selection: the sibling plugin's own GUI (see
its ``parasitic.py::run_plugin``) takes its two analysis endpoints from
``[d for d in data.values() if d["is_selected"]]`` — the user physically
selects exactly two elements in the PCB editor before clicking the plugin's
toolbar button. A chat tool has no such selection, so this tool instead
accepts two (x, y) millimeter coordinates and resolves each to the nearest
copper element already on the board (a via/pad's own position, or a
wire/track's start-end midpoint) — see ``_nearest_element()``.

Never mutates the board (read_only=True): pure analysis over the board's
existing copper geometry and stackup, exactly like the plugin's own GUI
would compute — just without the manual PCB-editor selection step.
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


_PACKAGE_NAME = "_sibling_kicad_parasitics"

# PCM install identifier, exactly as it appears under
# Documents\KiCad\<version>\3rdparty\plugins\ — no nested "plugins/" folder,
# unlike our own forks (see find_pcm_plugin_dir()'s own docstring).
_SIBLING_IDENTIFIER = "com_github_Steffen-W_KiCad-Parasitics"

# Default frequency sweep, identical to the sibling plugin's own GUI
# (parasitic.py's run_plugin) so a chat result is directly comparable to
# what a user would see clicking the plugin's own toolbar button.
_DEFAULT_FREQUENCIES_HZ = [1e3, 10e3, 100e3, 1e6, 10e6, 100e6, 1e9]

# A point farther than this from every copper element on the board is almost
# certainly a wrong/typo'd coordinate rather than a legitimately distant
# track — 50mm comfortably exceeds realistic pad/via/track spacing on a
# hobbyist-to-mid-size board while still catching gross mistakes (e.g. a
# coordinate meant for a different sheet/origin).
_MAX_POINT_DISTANCE_M = 0.05


def _load(submodule: str):
    plugins_dir = find_pcm_plugin_dir(_SIBLING_IDENTIFIER)
    return load_sibling_module(_PACKAGE_NAME, plugins_dir, submodule)


def _not_installed_message() -> str:
    return _(
        "O plugin KiCad-Parasitics não está instalado nesta máquina — esta "
        "ferramenta precisa dele."
    )


def _get_board():
    """Lazily import pcbnew and return (pcbnew, board) for the currently
    open board. Same lazy-import + RuntimeError pattern as
    kicad_tools.py's _get_board()."""
    try:
        import pcbnew
    except ImportError as exc:
        raise RuntimeError(
            "pcbnew indisponível — esta ferramenta só funciona dentro do KiCad"
        ) from exc

    board = pcbnew.GetBoard()
    if board is None:
        raise RuntimeError("Nenhum board KiCad está atualmente aberto")
    return pcbnew, board


def _element_reference_point(elem: dict) -> tuple[float, float] | None:
    """(x, y) in meters used to measure distance to a click point: a
    via/pad/zone's own "position", or a wire/track's start-end midpoint.
    None if the element carries neither (shouldn't normally happen)."""
    position = elem.get("position")
    if position is not None:
        return (position[0], position[1])
    start = elem.get("start")
    end = elem.get("end")
    if start is not None and end is not None:
        return ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
    return None


def _nearest_element(data: dict, point_m: tuple[float, float]) -> tuple[dict, float]:
    """Nearest element in ``data`` (post-Connect_Nets) to ``point_m``
    (meters), by Euclidean distance to ``_element_reference_point()``.

    Returns (element_dict, distance_m). Raises RuntimeError if ``data`` is
    empty or every element is farther than ``_MAX_POINT_DISTANCE_M``.
    """
    px, py = point_m
    best_elem = None
    best_dist = float("inf")
    for elem in data.values():
        ref = _element_reference_point(elem)
        if ref is None:
            continue
        dx, dy = ref[0] - px, ref[1] - py
        dist = (dx * dx + dy * dy) ** 0.5
        if dist < best_dist:
            best_dist = dist
            best_elem = elem

    if best_elem is None:
        raise RuntimeError(
            _("Nenhum elemento de cobre encontrado na placa aberta.")
        )
    if best_dist > _MAX_POINT_DISTANCE_M:
        raise RuntimeError(
            _(
                "Nenhum elemento de cobre encontrado a menos de {max_mm:g} mm "
                "do ponto ({x:.3f}, {y:.3f}) mm — verifique se as coordenadas "
                "estão corretas."
            ).format(
                max_mm=_MAX_POINT_DISTANCE_M * 1000,
                x=px * 1000,
                y=py * 1000,
            )
        )
    return best_elem, best_dist


def analyze_pcb_parasitics(args: dict) -> str:
    """Estimate parasitic DC resistance and AC impedance of the copper path
    between two points on the currently open board, via the sibling
    KiCad-Parasitics plugin's real analysis code.

    Required args:
        point1_x_mm, point1_y_mm, point2_x_mm, point2_y_mm: number — the two
            analysis endpoints, in millimeters (board coordinates). Each is
            resolved to the nearest copper element (via/pad by its own
            position, wire/track by its start-end midpoint) already present
            on the board; there is no chat-facing PCB-editor selection to
            drive this from directly (see module docstring).

    Optional args:
        frequencies_hz: list of numbers — AC analysis frequencies in Hz.
            Defaults to the sibling plugin's own GUI sweep
            ([1kHz, 10kHz, 100kHz, 1MHz, 10MHz, 100MHz, 1GHz]) so results are
            directly comparable to what the plugin's own toolbar button
            would show.
    """
    args = args or {}

    try:
        p1x = float(args["point1_x_mm"])
        p1y = float(args["point1_y_mm"])
        p2x = float(args["point2_x_mm"])
        p2y = float(args["point2_y_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            _(
                "Faltam ou são inválidas as coordenadas obrigatórias: "
                "point1_x_mm, point1_y_mm, point2_x_mm, point2_y_mm."
            )
        ) from exc

    raw_frequencies = args.get("frequencies_hz")
    if raw_frequencies is not None:
        try:
            frequencies_hz = [float(f) for f in raw_frequencies]
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                _("'frequencies_hz' deve ser uma lista de números.")
            ) from exc
        if not frequencies_hz:
            raise RuntimeError(_("'frequencies_hz' não pode estar vazia."))
    else:
        frequencies_hz = list(_DEFAULT_FREQUENCIES_HZ)

    pcbnew, board = _get_board()

    try:
        board_path = board.GetFileName()
    except Exception:
        board_path = None
    if not board_path:
        raise RuntimeError(
            _("Não foi possível determinar o caminho do ficheiro da placa.")
        )

    try:
        get_pcb_elements_mod = _load("Get_PCB_Elements")
        connect_nets_mod = _load("Connect_Nets")
        get_pcb_stackup_mod = _load("Get_PCB_Stackup")
        parasitic_mod = _load("parasitic")
    except SiblingPluginNotFoundError:
        return _not_installed_message()
    except ImportError as exc:
        return _("Erro ao carregar o KiCad-Parasitics: {err}").format(err=exc)

    connect = board.GetConnectivity()
    item_list, _board_thickness = get_pcb_elements_mod.Get_PCB_Elements(board, connect)

    # Stackup format changed at KiCad 9.0 (see the sibling plugin's own
    # __init__.py::Run()) — mirror its exact version-sniffing so we parse
    # the .kicad_pcb stackup section the same way the plugin's own GUI does.
    try:
        settings = pcbnew.GetSettingsManager()
        new_v9 = int(str(settings.GetSettingsVersion()).split(".")[0]) >= 9
    except Exception:
        new_v9 = True

    cu_stack = get_pcb_stackup_mod.Get_PCB_Stackup_fun(Path(board_path), new_v9=new_v9)

    data = connect_nets_mod.Connect_Nets(item_list)
    if not data:
        raise RuntimeError(
            _(
                "Nenhum elemento de cobre (trilha/via/pad) encontrado na "
                "placa aberta."
            )
        )

    point1_m = (p1x / 1000.0, p1y / 1000.0)
    point2_m = (p2x / 1000.0, p2y / 1000.0)

    element1, dist1 = _nearest_element(data, point1_m)
    element2, dist2 = _nearest_element(data, point2_m)

    result = parasitic_mod.analyze_pcb_parasitic(
        data, cu_stack, element1, element2, frequencies=frequencies_hz
    )

    header = _(
        "Ponto 1 ({x1:.3f}, {y1:.3f}) mm -> elemento {type1} mais próximo a "
        "{d1:.3f} mm; Ponto 2 ({x2:.3f}, {y2:.3f}) mm -> elemento {type2} "
        "mais próximo a {d2:.3f} mm."
    ).format(
        x1=p1x,
        y1=p1y,
        type1=element1.get("type", "?"),
        d1=dist1 * 1000,
        x2=p2x,
        y2=p2y,
        type2=element2.get("type", "?"),
        d2=dist2 * 1000,
    )

    body = parasitic_mod.format_result_message(result, cu_stack)

    return f"{header}\n\n{body}"


def register_kicad_parasitics_tools(registry: ActionRegistry) -> None:
    """Register the KiCad-Parasitics-backed tool on the given ActionRegistry.

    Safe to call even when KiCad-Parasitics isn't installed — the handler
    itself reports that honestly at call time instead of failing
    registration (same pattern as register_emc_emi_tools()/
    register_libforge_tools()). NOT wired into chat_action.py by this
    module — a separate integration pass does that.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="analyze_pcb_parasitics",
                description=(
                    "Call this when the user asks about parasitic DC "
                    "resistance or AC impedance of a copper path between "
                    "two points on the currently open PCB — e.g. signal "
                    "integrity, IR drop, or 'how resistive is the trace "
                    "between these two points'. Runs the sibling "
                    "KiCad-Parasitics plugin's real resistance/impedance "
                    "solver over the board's actual copper geometry and "
                    "stackup. Since there is no PCB-editor selection in "
                    "chat, pass two (x, y) millimeter coordinates — each is "
                    "resolved to the nearest existing copper element "
                    "(via/pad/track) on the board. Requires the "
                    "KiCad-Parasitics plugin to be installed; reports "
                    "honestly if it is missing."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "point1_x_mm": {
                            "type": "number",
                            "description": "First point's X coordinate, mm.",
                        },
                        "point1_y_mm": {
                            "type": "number",
                            "description": "First point's Y coordinate, mm.",
                        },
                        "point2_x_mm": {
                            "type": "number",
                            "description": "Second point's X coordinate, mm.",
                        },
                        "point2_y_mm": {
                            "type": "number",
                            "description": "Second point's Y coordinate, mm.",
                        },
                        "frequencies_hz": {
                            "type": "array",
                            "items": {"type": "number"},
                            "description": (
                                "AC analysis frequencies in Hz. Defaults to "
                                "[1e3, 10e3, 100e3, 1e6, 10e6, 100e6, 1e9] — "
                                "the sibling plugin's own GUI sweep."
                            ),
                        },
                    },
                    "required": [
                        "point1_x_mm",
                        "point1_y_mm",
                        "point2_x_mm",
                        "point2_y_mm",
                    ],
                },
            ),
            handler=analyze_pcb_parasitics,
            read_only=True,
        )
    )
