"""
Write chat tool wrapping the sibling "kicad_testpoints" plugin (TheJigsApp),
installed via KiCad's own Plugin and Content Manager (PCM), reached through
``_sibling_plugin.py``'s ``find_pcm_plugin_dir()`` like every other
PCM-installed sibling in this package.

Exports a test-point report (CSV) — pad reference/number/net/side/x/y/pad
type — for building a bed-of-nails test fixture. Directly useful for a real
manufactured board (e.g. this project's own nRF52 wearable), not just a
demo capability.

IMPORTANT — the sibling's own ``__init__.py`` has TWO import-time side
effects beyond the usual ``plugin.register()``: it also spawns a daemon
``threading.Thread`` (``check_for_button``) that polls every second looking
for the pcbnew window to manually inject a legacy toolbar button (only on
non-Linux platforms lacking native toolbar-icon support). Exactly like every
other sibling plugin wrapped in this package, ``load_sibling_module`` never
executes the disk ``__init__.py`` at all (the synthetic-package trick), so
this thread is never spawned by this tool — only the specific,
GUI-free ``kicad_testpoints_.py`` submodule is ever imported.

VERIFIED FACTS (read directly from ``kicad_testpoints_.py`` today — a real,
clean, GUI-free module, unlike its own ``__init__.py``/``plugin.py``):

- ``Settings()`` — plain class, one field: ``use_aux_origin: bool`` (default
  False). When True, coordinates are relative to the board's auxiliary
  origin (``board.GetDesignSettings().GetAuxOrigin()``) instead of (0, 0).
- ``get_pads_by_property(board) -> tuple[PAD]`` — auto-discovers every pad
  whose KiCad "Fabrication property" is set to Test Point (the plugin's own
  source hardcodes this as the raw property value ``4`` — used exactly as
  written, not re-derived, to match the installed plugin's real behavior
  even if that differs from a symbolic pcbnew constant name in some
  version).
- ``get_pads(pad_pair: tuple[(ref_des, pad_num)], board) -> tuple[PAD]`` —
  alternative to auto-discovery: an explicit list of (reference,
  pad_number) pairs. Raises ``UserWarning`` (not RuntimeError) for an
  unknown reference/pad — this module catches that and re-raises as
  RuntimeError, matching this codebase's own convention.
- ``build_test_point_report(board, settings, pads) -> list[dict]`` — the
  real report builder; each row has keys: "source ref des", "source pad",
  "net", "net class", "side" (TOP/BOTTOM), "x", "y" (mm, board-relative or
  aux-origin-relative), "pad type" (THRU/SMT), "footprint side"
  (TOP/BOTTOM).
- ``write_csv(data, filename)`` — writes the report list-of-dicts to a real
  CSV file (comma-delimited, quoted non-numeric fields).

This tool never touches the LIVE board — it only reads pad/board data and
writes a new CSV file, exactly like ``jlcpcb_tools.py``'s fabrication-file
export. Still registered read_only=False (this codebase's convention: any
tool that writes a NEW file to disk requires the same approval gate as a
board mutation — see ``kikit_tools.py``'s own docstring for the same
reasoning).
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
        load_sibling_module,
        find_pcm_plugin_dir,
    )

try:
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - fallback for flat/test imports
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


_PACKAGE_NAME = "_sibling_testpoints"
_SIBLING_IDENTIFIER = "com_github_TheJigsApp_kicadtestpoints-pcm"


def _load():
    plugins_dir = find_pcm_plugin_dir(_SIBLING_IDENTIFIER)
    return load_sibling_module(_PACKAGE_NAME, plugins_dir, "kicad_testpoints_")


def _not_installed_message() -> str:
    return _(
        "O plugin kicad_testpoints não está instalado nesta máquina — esta "
        "ferramenta precisa dele."
    )


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


def export_test_point_report(args: dict) -> str:
    """Export a test-point report (CSV) for building a bed-of-nails test
    fixture, via the sibling kicad_testpoints plugin's real report builder.

    Required args:
        output_path: str — absolute path to write the CSV file to
            (overwrites if it already exists).

    Optional args:
        pad_pairs: array of {reference, pad_number} — explicit pads to
            include. If omitted, auto-discovers every pad whose KiCad
            "Fabrication property" is set to Test Point (the same
            auto-discovery the plugin's own GUI uses when no manual
            selection is made).
        use_aux_origin: bool, default False — report coordinates relative
            to the board's auxiliary origin instead of (0, 0).

    Never touches the live board — only reads pad/board data and writes a
    new CSV file.
    """
    args = args or {}
    output_path = args.get("output_path")
    if not output_path:
        raise RuntimeError(_("Falta o argumento 'output_path'."))

    raw_pad_pairs = args.get("pad_pairs")
    if raw_pad_pairs is not None and not isinstance(raw_pad_pairs, list):
        raise RuntimeError(_("'pad_pairs' deve ser uma lista de {reference, pad_number}."))

    try:
        use_aux_origin = bool(args.get("use_aux_origin", False))
    except Exception:
        use_aux_origin = False

    try:
        mod = _load()
    except SiblingPluginNotFoundError:
        return _not_installed_message()
    except ImportError as exc:
        return _("Erro ao carregar o kicad_testpoints: {err}").format(err=exc)

    _pcbnew, board = _get_board()

    if raw_pad_pairs:
        pad_pairs = []
        for i, entry in enumerate(raw_pad_pairs):
            if not isinstance(entry, dict) or "reference" not in entry or "pad_number" not in entry:
                raise RuntimeError(
                    _(
                        "'pad_pairs[{i}]' inválido: esperava um objeto com "
                        "'reference' e 'pad_number'."
                    ).format(i=i)
                )
            pad_pairs.append((str(entry["reference"]), str(entry["pad_number"])))
        try:
            pads = mod.get_pads(pad_pairs, board)
        except UserWarning as exc:
            raise RuntimeError(str(exc)) from exc
    else:
        pads = mod.get_pads_by_property(board)
        if not pads:
            raise RuntimeError(
                _(
                    "Nenhum pad marcado como 'Test Point' foi encontrado na "
                    "placa, e nenhuma lista 'pad_pairs' foi indicada. Marque "
                    "pads como Test Point no KiCad, ou indique 'pad_pairs' "
                    "explicitamente."
                )
            )

    settings = mod.Settings()
    settings.use_aux_origin = use_aux_origin

    report = mod.build_test_point_report(board, settings, tuple(pads))

    try:
        mod.write_csv(report, Path(output_path))
    except OSError as exc:
        raise RuntimeError(
            _("Erro ao escrever o relatório em {path}: {err}").format(
                path=output_path, err=exc
            )
        ) from exc

    return _(
        "Relatório de {n} test point(s) gerado em: {path}"
    ).format(n=len(report), path=output_path)


def register_testpoints_tools(registry: ActionRegistry) -> None:
    """Register the kicad_testpoints-backed tool on the given
    ActionRegistry.

    Safe to call even when the sibling plugin isn't installed — the
    handler reports that honestly at call time, matching every other
    sibling-plugin wrapper in this package.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="export_test_point_report",
                description=(
                    "Call this when the user wants a test-point report/CSV "
                    "for building a bed-of-nails test fixture for the "
                    "currently open board. By default auto-discovers every "
                    "pad marked as 'Test Point' (KiCad's own Fabrication "
                    "property); pass 'pad_pairs' to specify exact pads "
                    "instead. Writes a NEW CSV file — never touches the "
                    "live board. This MODIFIES the filesystem and requires "
                    "explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "output_path": {
                            "type": "string",
                            "description": (
                                "Absolute path to write the CSV report to "
                                "(overwrites if it already exists)."
                            ),
                        },
                        "pad_pairs": {
                            "type": "array",
                            "description": (
                                "Explicit pads to include, overriding "
                                "auto-discovery of Test-Point-marked pads."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "reference": {"type": "string"},
                                    "pad_number": {"type": "string"},
                                },
                                "required": ["reference", "pad_number"],
                            },
                        },
                        "use_aux_origin": {
                            "type": "boolean",
                            "description": (
                                "Report coordinates relative to the board's "
                                "auxiliary origin instead of (0, 0). "
                                "Default false."
                            ),
                        },
                    },
                    "required": ["output_path"],
                },
            ),
            handler=export_test_point_report,
            read_only=False,
        )
    )
