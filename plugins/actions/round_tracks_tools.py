"""
Chat tool wrapping the sibling Round Tracks plugin (by mitxela), installed
via KiCad's own Plugin and Content Manager (PCM) — reached through
``_sibling_plugin.py``'s ``find_pcm_plugin_dir()`` exactly like
``kicad_parasitics_tools.py`` (a PCM install has no nested ``plugins/``
subfolder; the identifier folder itself IS the package root). See that
module for the general sibling-wrapping template this one follows, and
``kicad_tools.py``'s ``_get_board()`` for the lazy-``pcbnew``-import pattern
used here.

Round Tracks converts sharp 90°/45° track corners into rounded arcs
(subdivision-based track rounding) — a real, useful PCB
aesthetics/manufacturability tool applicable to any board.

MUTATES the board (adds/removes track/arc segments) — read_only=False.

DESIGN NOTE — constructing the sibling's own dialog class, headlessly:
the sibling's real rounding code lives in a method,
``RoundTracks.addIntermediateTracks(scaling, netclass, native,
onlySelection, avoid_junctions, msg)`` — but ``RoundTracks`` itself is a
subclass of ``RoundTracksDialog``, a real ``wx.Dialog`` (wxFormBuilder-
generated, see ``round_tracks_gui.py``). Its ``__init__`` genuinely builds
wx widgets (a ``DataViewListCtrl`` + three checkboxes), which requires a
live ``wx.App`` — true here since this chat runs inside KiCad's own
``wx.App`` — but this tool never calls ``.ShowModal()``/``.Show()`` on it,
only ``addIntermediateTracks()`` directly, then ``.Destroy()`` immediately
after (in a ``finally``) to clean up the never-shown dialog's widgets.
``RoundTracks.__init__`` was read in full to confirm this is safe:
  - ``self.action`` is stored but never read again inside ``__init__`` or
    ``addIntermediateTracks`` — passing ``None`` (this tool never drives
    ``ActionRoundTracks``'s own toolbar-button flow) is safe.
  - ``load_config()`` best-effort reads a sibling ``.round-tracks-config``
    text file next to the board if one exists; harmless no-op otherwise.
  - ``validate_all_data()`` runs at the end of ``__init__`` and populates
    ``self.config['classes']`` — a dict keyed by every net class name on
    the board ("Default" plus each of ``board.GetNetClasses()``) — from the
    freshly-populated ``netclasslist`` widget. This tool reads that dict's
    *keys* only (to discover which net class names exist) and otherwise
    ignores its per-class ``do_round``/``scaling``/``passes`` values — those
    reflect a possibly-stale on-disk config file's checkbox states, not this
    call's explicit ``scaling``/``native``/``avoid_junctions`` arguments,
    which are applied uniformly to every net class processed.

DESIGN NOTE — ``netclass=None`` is NOT "apply to the whole board": reading
``addIntermediateTracks`` closely, its outer loop only touches a net's
tracks when ``netclass is not None and netclass == net.GetNetClassName()``
— passing ``None`` makes the whole call a no-op for every net. The
sibling's own ``run()`` handles this by looping over every discovered net
class name and calling ``addIntermediateTracks`` once per class. This tool
does the same: with no ``netclass`` argument, it calls
``addIntermediateTracks`` once for every net class name found in
``rt.config['classes']``; with a ``netclass`` argument, it validates that
name against that same set first (a clear error listing what does exist
beats a silent no-op).

DESIGN NOTE — the ``self.prog`` progress dialog: ``addIntermediateTracks``
unconditionally calls ``self.prog.Pulse(...)`` once per net code processed
— but ``self.prog`` is only ever set inside ``run()`` (the sibling's own
"Run" button handler, itself gated on a real ``wx.ProgressDialog``), which
this tool intentionally never calls. Calling ``addIntermediateTracks``
without first setting ``self.prog`` would raise ``AttributeError``. Rather
than construct a real (and needlessly visible/modal) ``wx.ProgressDialog``,
this tool assigns a tiny no-op stand-in (``_NullProgress``, a plain Python
object with a ``Pulse(message)`` method that does nothing) to ``rt.prog``
before calling ``addIntermediateTracks`` — functionally identical from
``addIntermediateTracks``'s point of view, with no UI side effects.

DESIGN NOTE — simplifications relative to the sibling's own GUI flow (a
human reviewer may want to extend these):
  - No ``onlySelection`` support: a chat call has no PCB-editor selection
    to drive an "only these tracks" mode (same gap as
    ``kicad_parasitics_tools.py``'s two-point analysis). Pre-selecting
    tracks programmatically by net name (``track.SetSelected()``) was
    considered, but adds real complexity/risk (partial-net selection
    semantics, interaction with the "avoid junctions with >2 tracks" rule)
    for limited benefit — this tool always processes the *whole* matching
    net class(es), i.e. ``onlySelection=False``, same as unchecking the
    sibling's own "only apply to selection" auto-detection by simply
    selecting nothing on the board first.
  - No multi-pass subdivision loop: when the sibling's own GUI has "use
    native fillets" (``native``) UNCHECKED, ``run()`` calls
    ``addIntermediateTracks`` in a loop, once per configured "passes"
    count (1-5, default ``PASSES_DEFAULT = 3``), to progressively subdivide
    corners with plain straight tracks. This tool does not expose a
    ``passes`` argument and always calls ``addIntermediateTracks`` exactly
    once per net class — with ``native=False`` this yields only a single,
    coarser rounding pass rather than the sibling's default 3-pass result.
    The default ``native=True`` (see below) is unaffected by this
    simplification since it only ever needs one call per net class.
  - The KiCad 7.0.0 compatibility gate in the sibling's own
    ``ActionRoundTracks.Run()`` (``pcbnew.GetBuildVersion() == '(7.0.0)'``)
    is not repeated here: it is moot on this machine (KiCad 10.0.4) and no
    other version gate exists elsewhere in the sibling's source that would
    matter here — confirmed by reading the whole file.

Defaults mirror the sibling plugin's own GUI defaults, read directly from
``round_tracks_action.py``: ``RADIUS_DEFAULT = 2.0`` (mm, the "radius" a 90°
bend gets) for ``scaling``, and ``self.use_native.SetValue(True)`` (see
``round_tracks_gui.py``) for ``native`` — note this differs from
``addIntermediateTracks``'s own Python-level keyword default
(``native=False``); the GUI's actual default behavior, which this tool
matches, is native fillets ON.
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
        find_pcm_plugin_dir,
        load_sibling_module,
    )

try:
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - fallback for flat/test imports
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


_PACKAGE_NAME = "_sibling_round_tracks"

# PCM install identifier, exactly as it appears under
# Documents\KiCad\<version>\3rdparty\plugins\ — no nested "plugins/" folder,
# unlike our own forks (see find_pcm_plugin_dir()'s own docstring).
_SIBLING_IDENTIFIER = "com_github_mitxela_kicad-round-tracks"

# Mirrors RADIUS_DEFAULT in the sibling's own round_tracks_action.py (a 90°
# bend gets a fillet up to this many mm) so a chat call with no explicit
# `scaling` behaves the same as the sibling's own GUI default.
_DEFAULT_SCALING_MM = 2.0


class _NullProgress:
    """No-op stand-in for RoundTracks.run()'s own wx.ProgressDialog. Assigned
    as `rt.prog` so addIntermediateTracks()'s per-netcode
    `self.prog.Pulse(...)` calls (state only ever set inside run(), which
    this tool intentionally never calls) have somewhere harmless to go,
    without ever constructing or showing a real dialog."""

    def Pulse(self, message: str = "") -> None:  # noqa: N802 - matches wx's own method casing
        pass


def _load(submodule: str):
    plugins_dir = find_pcm_plugin_dir(_SIBLING_IDENTIFIER)
    return load_sibling_module(_PACKAGE_NAME, plugins_dir, submodule)


def _not_installed_message() -> str:
    return _(
        "O plugin Round Tracks não está instalado nesta máquina — esta "
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
            _("pcbnew indisponível — esta ferramenta só funciona dentro do KiCad")
        ) from exc

    board = pcbnew.GetBoard()
    if board is None:
        raise RuntimeError(_("Nenhum board KiCad está atualmente aberto"))
    return pcbnew, board


def round_pcb_tracks(args: dict) -> str:
    """Round sharp 90°/45° track corners on the currently open board into
    arcs (or subdivided approximations), via the sibling Round Tracks
    plugin's real rounding code.

    Optional args:
        scaling: number — target fillet radius in mm for a 90° bend.
            Defaults to 2.0 (the sibling plugin's own RADIUS_DEFAULT).
        netclass: str — limit rounding to a single net class name (e.g.
            "Default" or a custom class already defined on the board). If
            omitted, every net class present on the board is processed.
        native: bool — use KiCad's native arc tracks (true fillets) rather
            than a subdivision approximation. Defaults to True (the
            sibling plugin's own GUI default, "use native fillets").
        avoid_junctions: bool — skip intersections where more than two
            tracks meet. Defaults to False.
    """
    args = args or {}

    raw_scaling = args.get("scaling", _DEFAULT_SCALING_MM)
    try:
        scaling = float(raw_scaling)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(_("'scaling' deve ser um número (mm).")) from exc
    if scaling <= 0:
        raise RuntimeError(_("'scaling' deve ser maior que zero."))

    netclass_filter = args.get("netclass")
    if netclass_filter is not None:
        netclass_filter = str(netclass_filter)

    native = bool(args.get("native", True))
    avoid_junctions = bool(args.get("avoid_junctions", False))

    pcbnew, board = _get_board()

    try:
        action_mod = _load("round_tracks_action")
    except SiblingPluginNotFoundError:
        return _not_installed_message()
    except ImportError as exc:
        return _("Erro ao carregar o Round Tracks: {err}").format(err=exc)

    rt = action_mod.RoundTracks(board, None)
    try:
        available_classes = list(rt.config.get("classes", {}).keys())
        if not available_classes:
            raise RuntimeError(
                _("Não foi possível determinar as classes de net da placa.")
            )

        if netclass_filter is not None:
            if netclass_filter not in available_classes:
                raise RuntimeError(
                    _(
                        "Classe de net '{name}' não encontrada. Classes "
                        "disponíveis: {available}."
                    ).format(
                        name=netclass_filter,
                        available=", ".join(available_classes),
                    )
                )
            classes_to_process = [netclass_filter]
        else:
            classes_to_process = available_classes

        rt.prog = _NullProgress()

        before_count = len(list(board.GetTracks()))
        for class_name in classes_to_process:
            rt.addIntermediateTracks(
                scaling=scaling,
                netclass=class_name,
                native=native,
                onlySelection=False,
                avoid_junctions=avoid_junctions,
            )
        after_count = len(list(board.GetTracks()))
    finally:
        rt.Destroy()

    # Mirrors ActionRoundTracks.Run()'s own post-processing call: without
    # it, newly-created arcs are sometimes not acknowledged as tangential
    # until moved or the file is re-opened. Cosmetic/best-effort only — must
    # never break an otherwise-successful rounding pass.
    try:
        refresh = getattr(pcbnew, "UpdateUserInterface", None)
        if callable(refresh):
            refresh()
    except Exception:
        pass

    return _(
        "Arredondamento de trilhas concluído — classes de net processadas: "
        "{classes} (radius {scaling:g} mm, native={native}, "
        "avoid_junctions={avoid_junctions}). Segmentos de trilha na placa: "
        "{before} -> {after}."
    ).format(
        classes=", ".join(classes_to_process),
        scaling=scaling,
        native=native,
        avoid_junctions=avoid_junctions,
        before=before_count,
        after=after_count,
    )


def register_round_tracks_tools(registry: ActionRegistry) -> None:
    """Register the Round-Tracks-backed tool on the given ActionRegistry.

    Safe to call even when Round Tracks isn't installed — the handler
    itself reports that honestly at call time instead of failing
    registration (same pattern as register_kicad_parasitics_tools()/
    register_emc_emi_tools()). NOT wired into chat_action.py by this
    module — a separate integration pass does that.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="round_pcb_tracks",
                description=(
                    "Call this when the user asks to round sharp track "
                    "corners on the currently open PCB — e.g. 'round the "
                    "traces', 'add fillets to the tracks', or PCB "
                    "aesthetics/manufacturability cleanup. Runs the "
                    "sibling Round Tracks plugin's real rounding code, "
                    "converting 90°/45° track corners into arcs (or a "
                    "subdivided approximation). MUTATES the board — adds "
                    "and removes track/arc segments. Applies to every net "
                    "class on the board by default, or a single named net "
                    "class if 'netclass' is given. Requires the Round "
                    "Tracks plugin to be installed; reports honestly if it "
                    "is missing."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "scaling": {
                            "type": "number",
                            "description": (
                                "Target fillet radius in mm for a 90° "
                                "bend. Defaults to 2.0."
                            ),
                        },
                        "netclass": {
                            "type": "string",
                            "description": (
                                "Limit rounding to a single net class name "
                                "(e.g. 'Default'). Defaults to every net "
                                "class present on the board."
                            ),
                        },
                        "native": {
                            "type": "boolean",
                            "description": (
                                "Use KiCad's native arc tracks (true "
                                "fillets) instead of a subdivided "
                                "approximation. Defaults to true."
                            ),
                        },
                        "avoid_junctions": {
                            "type": "boolean",
                            "description": (
                                "Skip intersections where more than two "
                                "tracks meet. Defaults to false."
                            ),
                        },
                    },
                    "required": [],
                },
            ),
            handler=round_pcb_tracks,
            read_only=False,
        )
    )
