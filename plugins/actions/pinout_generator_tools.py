"""
Read-only chat tool wrapping the sibling Pinout Generator plugin (by
cgrassin), installed via KiCad's own Plugin and Content Manager (PCM) —
reached through ``_sibling_plugin.py``'s ``find_pcm_plugin_dir()`` /
``load_sibling_module()``, exactly like ``kicad_parasitics_tools.py``: a PCM
install has no nested ``plugins/`` subfolder, the identifier folder itself
IS the package root. See ``_sibling_plugin.py`` for why the synthetic-package
trick exists at all (the sibling's own ``__init__.py`` does
``PinoutGenerator().register()`` at import time — ``load_sibling_module``
only ever imports the specific submodule we ask for, never that
``__init__.py``).

Pinout Generator formats a footprint's pad/net connections into one of
several output formats (plain list, CSV, HTML, Markdown, C enum, C define,
Python dict, WireViz, FPGA XDC/PDC constraints) — handy for documentation
and wiring-harness work on any board.

DESIGN NOTE — no chat-facing selection/wx dialog: the sibling plugin's own
``Run()`` builds ``self.footprint_selection`` from footprints the user has
physically selected in the PCB editor, then drives per-format output through
wx dialog controls (``pinNameCB``, ``pinNameFilter``) via ``change_format()``
/``set_output()``. The actual per-format methods on ``PinoutGenerator``
(``csv_format``, ``html_format``, etc.) are plain methods taking a single
``component`` (a ``pcbnew.FOOTPRINT``) — they only read two things off
``self``: ``self.get_pin_name_filter()`` and ``self.is_pinname_not_number()``,
both bound to wx widget getters in ``Run()`` but nothing else requires that.
This tool instantiates ``PinoutGenerator()`` directly (never calling
``Run()``, so no dialog is ever shown), binds those two attributes to plain
lambdas built from chat-friendly ``pin_name_filter``/``pin_name_not_number``
args, looks up a SINGLE footprint by reference
(``board.FindFootprintByReference``, matching ``kicad_write_tools.py``'s
``_find_footprint`` idiom), and calls the matching ``_format`` method
directly — sidestepping the PCB-editor multi-select flow entirely. The
sibling plugin's own code documents c_define/fpga_xdc/fpga_pdc as "not
compatible with multiple selection"; since this tool only ever passes one
footprint, that restriction never applies here.

Never mutates the board (read_only=True): pure formatting of the board's
existing pad/net data, exactly like the plugin's own GUI would compute for
a single selected footprint — just without the manual PCB-editor selection
and wx dialog.
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


_PACKAGE_NAME = "_sibling_pinout_generator"

# PCM install identifier, exactly as it appears under
# Documents\KiCad\<version>\3rdparty\plugins\ — no nested "plugins/" folder,
# unlike our own forks (see find_pcm_plugin_dir()'s own docstring).
_SIBLING_IDENTIFIER = "com_github_cgrassin_kicad-pinout-generator"

# Chat-facing format name -> PinoutGenerator bound-method name. Same format
# name strings as the sibling plugin's own SELECTOR dict keys ("list", "csv",
# "html", "md", "c_define", "c_enum", "python_dict", "wireviz", "fpga_xdc",
# "fpga_pdc") — its integer indices are an internal wx-choice-control detail
# we don't need since we call the method directly.
_FORMAT_METHODS = {
    "list": "list_format",
    "csv": "csv_format",
    "html": "html_format",
    "md": "markdown_format",
    "c_enum": "c_enum_format",
    "c_define": "c_define_format",
    "python_dict": "python_dict_format",
    "wireviz": "wireviz_format",
    "fpga_xdc": "xdc_format",
    "fpga_pdc": "pdc_format",
}


def _load(submodule: str):
    plugins_dir = find_pcm_plugin_dir(_SIBLING_IDENTIFIER)
    return load_sibling_module(_PACKAGE_NAME, plugins_dir, submodule)


def _not_installed_message() -> str:
    return _(
        "O plugin Pinout Generator não está instalado nesta máquina — esta "
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


def _find_footprint(board, reference: str):
    fp = board.FindFootprintByReference(reference)
    if fp is None:
        raise RuntimeError(
            _("Componente '{reference}' não encontrado na placa.").format(
                reference=reference
            )
        )
    return fp


def generate_component_pinout(args: dict) -> str:
    """Generate a pinout export for a single footprint on the currently open
    board, via the sibling Pinout Generator plugin's real formatting code.

    Required args:
        reference: str — footprint reference designator (e.g. "U1").
        format: str — one of "list", "csv", "html", "md", "c_enum",
            "c_define", "python_dict", "wireviz", "fpga_xdc", "fpga_pdc".

    Optional args:
        pin_name_not_number: bool (default False) — when True, pin
            identifiers are taken from the pin/net NAME (optionally filtered
            by pin_name_filter) instead of the pad NUMBER; maps to the
            sibling plugin's own ``is_pinname_not_number()``.
        pin_name_filter: str (default "") — regex-ish filter applied to the
            pin name when pin_name_not_number is True; maps to the sibling
            plugin's own ``get_pin_name_filter()``.
    """
    args = args or {}

    reference = args.get("reference")
    if not reference:
        raise RuntimeError(_("Falta o argumento 'reference'."))

    fmt = args.get("format")
    method_name = _FORMAT_METHODS.get(fmt)
    if method_name is None:
        raise RuntimeError(
            _("Formato '{fmt}' inválido. Use um destes: {valid}.").format(
                fmt=fmt, valid=", ".join(sorted(_FORMAT_METHODS))
            )
        )

    pin_name_not_number = bool(args.get("pin_name_not_number", False))
    pin_name_filter = args.get("pin_name_filter") or ""

    _pcbnew, board = _get_board()
    fp = _find_footprint(board, reference)

    try:
        pinout_plugin_mod = _load("pinout_plugin")
    except SiblingPluginNotFoundError:
        return _not_installed_message()
    except ImportError as exc:
        return _("Erro ao carregar o Pinout Generator: {err}").format(err=exc)

    gen = pinout_plugin_mod.PinoutGenerator()
    gen.get_pin_name_filter = lambda: pin_name_filter
    gen.is_pinname_not_number = lambda: pin_name_not_number

    output_formater = getattr(gen, method_name)
    return output_formater(fp)


def register_pinout_generator_tools(registry: ActionRegistry) -> None:
    """Register the Pinout-Generator-backed tool on the given ActionRegistry.

    Safe to call even when Pinout Generator isn't installed — the handler
    itself reports that honestly at call time instead of failing
    registration (same pattern as register_kicad_parasitics_tools()/
    register_emc_emi_tools()/register_libforge_tools()). NOT wired into
    chat_action.py by this module — a separate integration pass does that.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="generate_component_pinout",
                description=(
                    "Call this when the user asks for a pinout export/table "
                    "for a specific footprint on the currently open PCB — "
                    "e.g. documentation, a wiring harness, or a firmware "
                    "pin-name header/enum. Runs the sibling Pinout Generator "
                    "plugin's real formatting code over the footprint's "
                    "actual pad/net connections. Formats: 'list' (plain "
                    "tab-separated), 'csv', 'html', 'md' (Markdown table), "
                    "'c_enum', 'c_define', 'python_dict', 'wireviz', "
                    "'fpga_xdc', 'fpga_pdc'. Works on ONE footprint at a "
                    "time (pass its reference designator, e.g. 'U1') — "
                    "there is no PCB-editor multi-select in chat. Requires "
                    "the Pinout Generator plugin to be installed; reports "
                    "honestly if it is missing."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "reference": {
                            "type": "string",
                            "description": (
                                "Footprint reference designator, e.g. 'U1'."
                            ),
                        },
                        "format": {
                            "type": "string",
                            "enum": sorted(_FORMAT_METHODS),
                            "description": "Pinout export format.",
                        },
                        "pin_name_not_number": {
                            "type": "boolean",
                            "description": (
                                "Use the pin/net NAME instead of the pad "
                                "NUMBER as the pin identifier. Default false."
                            ),
                        },
                        "pin_name_filter": {
                            "type": "string",
                            "description": (
                                "Filter applied to the pin name when "
                                "pin_name_not_number is true (e.g. extract a "
                                "trailing number after a prefix like "
                                "'GPIO'). Default empty (no filtering)."
                            ),
                        },
                    },
                    "required": ["reference", "format"],
                },
            ),
            handler=generate_component_pinout,
            read_only=True,
        )
    )
