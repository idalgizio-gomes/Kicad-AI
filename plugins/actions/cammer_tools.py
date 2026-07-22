"""
Write chat tool wrapping the sibling "SparkFun KiCad CAMmer" plugin,
installed via KiCad's own Plugin and Content Manager (PCM), reached through
``_sibling_plugin.py``'s ``find_pcm_plugin_dir()`` like every other
PCM-installed sibling. Generates a zipped set of Gerber + drill files for
fabrication, from a chosen layer/edge selection.

SAFETY FINDING (why this tool is more careful than a naive wrapper would
be) — read directly from the installed ``cammer/cammer.py`` today:
``CAMmer.startCAMmer()`` calls ``wx.MessageBox()`` DIRECTLY, twice,
whenever ``wx.GetApp() is not None`` — which is ALWAYS true here, since
this chat runs inside KiCad's own live wx.App:

1. "Do you want to save the PCB first?" — but ONLY in the branch taken
   when a live ``board`` object is passed in (the "running as a plugin"
   code path). Passing ``board=None`` plus a file ``path`` instead takes
   the OTHER branch (loads the board fresh from that file with
   ``pcbnew.LoadBoard``), which never reaches this prompt at all. This
   tool therefore ALWAYS calls ``startCAMmer`` in "path" mode, never
   "live board" mode, specifically to avoid this unavoidable-otherwise
   native dialog popping up mid-chat-turn (which our own approval-gate
   text flow has no way to intercept or answer).
2. "You are about to overwrite existing files. Are you sure?" — fires
   whenever the target zip (``<board-stem>.zip`` next to the board file)
   already exists, REGARDLESS of which mode ``startCAMmer`` is called in.
   This tool checks for that zip's existence itself BEFORE calling
   ``startCAMmer`` and raises RuntimeError if found, so this prompt is
   never reached either.

With both avoided, ``startCAMmer`` runs fully non-interactively — the only
UI moment is this codebase's own approval dialog, before the call, exactly
like every other write tool.

Other verified facts:
- ``CAMmer.args_parse(argv_list) -> argparse.Namespace`` builds the real
  args object ``startCAMmer`` expects (``-p/--path``,
  ``-l/--layers`` CSV, ``-e/--edges`` CSV) — used here instead of
  hand-building a Namespace, so argument parsing/validation stays
  identical to the plugin's own real CLI.
- ``CAMmer.startCAMmer(args, board=None, logger=None) -> (sysExit, report)``
  — ``sysExit`` 0 = success, 1 = warning, 2 = error (mirrors the plugin's
  own ``plugin.py`` interpretation, copied here). ``report`` is a
  human-readable multi-line string, returned to the user as-is.
- The requested layer/edge names must be real KiCad standard layer names
  (e.g. "F.Cu", "B.Cu", "F.SilkS", "F.Mask", "Edge.Cuts") — this tool does
  not validate them against the board's own layer set beforehand (the
  sibling's own code does that internally and reports failures via
  ``report``), consistent with not re-guessing the plugin's own validation.

Same lazy-import + RuntimeError-not-raw-exception + i18n ``_()`` trampoline
conventions as every other tool module in this package.
"""

from __future__ import annotations

import os

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


_PACKAGE_NAME = "_sibling_cammer"
_SIBLING_IDENTIFIER = "com_github_sparkfun_SparkFunKiCadCAMmer"


def _load():
    plugins_dir = find_pcm_plugin_dir(_SIBLING_IDENTIFIER)
    return load_sibling_module(_PACKAGE_NAME, plugins_dir, "cammer.cammer")


def _not_installed_message() -> str:
    return _(
        "O plugin SparkFun KiCad CAMmer não está instalado nesta máquina — "
        "esta ferramenta precisa dele."
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


def generate_gerber_zip(args: dict) -> str:
    """Generate a zipped set of Gerber + drill files for the currently
    open board, via the sibling SparkFun KiCad CAMmer plugin's real
    generator.

    Required args:
        layers: array of str — KiCad standard layer names to include
            (e.g. ["F.Cu", "B.Cu", "F.SilkS", "F.Mask", "B.Mask"]).
        edges: array of str — outline/keep-out layer names (usually just
            ["Edge.Cuts"]).

    The board must already be saved to disk (output goes to a zip next to
    the board file). Refuses (RuntimeError) if that zip already exists —
    see this module's docstring for why (avoids an otherwise-unavoidable
    native "overwrite?" dialog popping up mid-chat-turn).
    """
    args = args or {}
    layers = args.get("layers")
    edges = args.get("edges")
    if not layers and not edges:
        raise RuntimeError(
            _("Indique pelo menos um de 'layers' ou 'edges' (não podem estar ambos vazios).")
        )
    if layers is not None and not isinstance(layers, list):
        raise RuntimeError(_("'layers' deve ser uma lista de nomes de layer."))
    if edges is not None and not isinstance(edges, list):
        raise RuntimeError(_("'edges' deve ser uma lista de nomes de layer."))

    _pcbnew, board = _get_board()
    board_path = board.GetFileName()
    if not board_path:
        raise RuntimeError(
            _(
                "A placa aberta ainda não foi guardada em disco — guarde-a "
                "antes de gerar os ficheiros Gerber."
            )
        )

    zip_path = os.path.splitext(board_path)[0] + ".zip"
    if os.path.isfile(zip_path):
        raise RuntimeError(
            _(
                "Já existe um ficheiro '{path}' — apague-o ou mova-o antes "
                "de gerar de novo (esta ferramenta nunca sobrescreve "
                "silenciosamente, para evitar um diálogo de confirmação do "
                "próprio plugin que o chat não consegue responder)."
            ).format(path=zip_path)
        )

    try:
        cammer_mod = _load()
    except SiblingPluginNotFoundError:
        return _not_installed_message()
    except ImportError as exc:
        return _("Erro ao carregar o CAMmer: {err}").format(err=exc)

    argv = ["-p", board_path]
    if layers:
        argv.extend(["-l", ",".join(str(x) for x in layers)])
    if edges:
        argv.extend(["-e", ",".join(str(x) for x in edges)])

    cammer = cammer_mod.CAMmer()
    parsed_args = cammer.args_parse(argv)
    # ALWAYS board=None here — passing the live board object takes the
    # "running as a plugin" code path, which prompts "save PCB first?" via
    # a raw wx.MessageBox we cannot intercept. Passing None + a saved file
    # path takes the other, non-interactive path instead (see docstring).
    sys_exit, report = cammer.startCAMmer(parsed_args, board=None, logger=None)

    if sys_exit >= 2:
        raise RuntimeError(
            _("CAMmer falhou (código {code}):\n{report}").format(
                code=sys_exit, report=report
            )
        )

    prefix = (
        _("CAMmer terminou com avisos (código {code}):\n").format(code=sys_exit)
        if sys_exit == 1
        else _("CAMmer concluído com sucesso.\n")
    )
    return prefix + report


def register_cammer_tools(registry: ActionRegistry) -> None:
    """Register the CAMmer-backed tool on the given ActionRegistry.

    Safe to call even when the sibling plugin isn't installed — the
    handler reports that honestly at call time, matching every other
    sibling-plugin wrapper in this package.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="generate_gerber_zip",
                description=(
                    "Call this when the user wants Gerber + drill files "
                    "generated and zipped for fabrication, for a chosen "
                    "set of copper/silk/mask layers and outline (edge) "
                    "layers. Writes a NEW zip file next to the board file "
                    "— never overwrites an existing one (refuses instead). "
                    "This MODIFIES the filesystem and requires explicit "
                    "user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "layers": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "KiCad standard layer names to include, "
                                "e.g. ['F.Cu', 'B.Cu', 'F.SilkS', "
                                "'F.Mask', 'B.Mask']."
                            ),
                        },
                        "edges": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Outline/keep-out layer names, usually "
                                "['Edge.Cuts']."
                            ),
                        },
                    },
                    "required": [],
                },
            ),
            handler=generate_gerber_zip,
            read_only=False,
        )
    )
