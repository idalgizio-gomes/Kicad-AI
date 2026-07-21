"""
Chat tool that wraps the sibling Board2Pdf plugin (dennevi, GitLab,
GPLv3, installed via KiCad's own Plugin and Content Manager — NOT one of
our own forks) to export the currently open (or an explicitly given) PCB
to a merged, colorized assembly PDF — see ``_sibling_plugin.py`` for how
its code is reached without a ``plugins``-package name collision, and
``find_pcm_plugin_dir`` (also in that module) for why a PCM-installed
sibling needs a different directory-resolution helper than our own forks
(LibForge, EMC-EMI): there is no nested ``plugins/`` subfolder to junction
into, ``Documents\\KiCad\\<version>\\3rdparty\\plugins\\<identifier>\\`` IS
the package root.

Mirrors ``board2pdf-cli.py``'s own ``cli()`` function almost exactly:
``pcbnew.LoadBoard(path)`` -> a full config dict from
``persistence.Persistence(configfile).load()`` -> a handful of CLI-style
keyword overrides -> ``plot.plot_pdfs(board, **config_vars)``. Calling
``plot_pdfs`` without a ``dlg`` argument (its default is ``None``) makes
it run in the same "headless/CLI" mode the real CLI script uses — status
and error messages go through ``print()`` instead of ``wx`` dialogs. We
capture that output via ``contextlib.redirect_stdout`` and relay it to
the LLM instead of discarding it.

NOT read_only: it writes at least one PDF file (and, depending on the
resolved config, temporary per-layer PDFs/SVGs, cleaned up unless the
config says otherwise) to disk under the configured output directory.
Honest failure (a returned message, or in a few cases a raised
RuntimeError for caller-supplied invalid input — same split used by
``libforge_tools.py``), never a fake result, when the sibling plugin
isn't installed, the board/config file can't be found or loaded, or
``plot_pdfs`` itself reports failure.

Known limitation inherited from the real plugin, not something this
wrapper can route around: a handful of ``plot.py``'s own internal error
paths (e.g. ``io_file_error_msg``/``exception_msg``) call
``wx.MessageBox`` directly regardless of the headless ``dlg=None`` mode,
falling back to ``print()`` only if no ``wx.App`` exists
(``wx._core.PyNoAppError``). Since this tool runs inside KiCad's own
``wx.App``, such a failure could still pop up a native dialog instead of
only appearing in the text this tool returns.
"""

from __future__ import annotations

import contextlib
import io
import os
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


_PACKAGE_NAME = "_sibling_board2pdf"

# Third-party, PCM-installed sibling — resolved via find_pcm_plugin_dir(),
# not the _find_sibling_plugins_dir()-style helper used for our own forks
# (LibForge, EMC-EMI), because this plugin's install folder has no nested
# ``plugins/`` subfolder: it IS the package root.
_SIBLING_IDENTIFIER = "com_gitlab_dennevi_Board2Pdf"


def _load(submodule: str):
    plugins_dir = find_pcm_plugin_dir(_SIBLING_IDENTIFIER)
    return load_sibling_module(_PACKAGE_NAME, plugins_dir, submodule)


def _not_installed_message() -> str:
    return _(
        "O plugin Board2Pdf não está instalado nesta máquina — esta "
        "ferramenta precisa dele para exportar a placa para PDF."
    )


def _get_current_board_path() -> str:
    """Lazily import pcbnew and return the currently open board's file
    path — same lazy-import + RuntimeError pattern as kicad_tools.py's
    ``_get_board()``. This tool only ever needs the FILE PATH: it always
    (re)loads the board via ``pcbnew.LoadBoard``, exactly like
    board2pdf-cli.py's own ``cli()`` function, rather than plotting the
    live in-memory board object directly.
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

    board_path = board.GetFileName()
    if not board_path:
        raise RuntimeError(
            _(
                "A placa aberta ainda não foi guardada em disco — não há um "
                "ficheiro .kicad_pcb para exportar"
            )
        )
    return board_path


def _resolve_config_path(board_dir: Path, explicit: str | None) -> "tuple[Path, None] | tuple[None, str]":
    """Resolve the ``board2pdf.config.ini`` to use, same order described
    for this tool: explicit path > ``board2pdf.config.ini`` sitting next
    to the board file > the plugin's own bundled ``default_config.ini``.

    (board2pdf-cli.py's own ``main()`` additionally checks a "globally
    saved" ``board2pdf.config.ini`` next to the plugin's own code, between
    those last two steps — that file is only ever created by the GUI
    dialog's own "save settings" action, which this headless tool has no
    equivalent of, so it would never exist here in practice; omitted for
    that reason.)

    Returns ``(path, None)`` on success or ``(None, error_message)`` on
    failure — a plain tuple rather than an exception, since "config file
    not found" is a normal/expected input problem the LLM should be told
    about in plain text (mirrors ``scan_library_folder_for_duplicates``'s
    "folder not found" handling in libforge_tools.py), not a hard
    programming-error RuntimeError.
    """
    if explicit:
        explicit_path = Path(explicit)
        if not explicit_path.is_file():
            return None, _("Ficheiro de configuração não encontrado: {path}").format(
                path=explicit_path
            )
        return explicit_path, None

    local_ini = board_dir / "board2pdf.config.ini"
    if local_ini.is_file():
        return local_ini, None

    try:
        plugins_dir = find_pcm_plugin_dir(_SIBLING_IDENTIFIER)
    except SiblingPluginNotFoundError:
        return None, _not_installed_message()

    default_ini = plugins_dir / "default_config.ini"
    if default_ini.is_file():
        return default_ini, None

    return None, _(
        "Nenhum ficheiro de configuração encontrado (nem junto à placa em "
        "{board_dir}, nem o default_config.ini do Board2Pdf)."
    ).format(board_dir=board_dir)


def export_board_to_pdf(args: dict) -> str:
    """Export the currently open (or an explicitly given) PCB's layers to
    a merged, colorized assembly PDF, using the sibling Board2Pdf
    plugin's real plotting pipeline (``plot.plot_pdfs``).

    Optional args:
        board_path: str — path to a .kicad_pcb file to export instead of
            the currently open board.
        config_ini_path: str — explicit Board2Pdf config .ini to use
            instead of the usual resolution order (a
            ``board2pdf.config.ini`` next to the board file, then
            Board2Pdf's own bundled ``default_config.ini``).
        colorize_lib: str — which library colorizes layers ("pypdf",
            "pymupdf"/"fitz", or "kicad" to use KiCad's own color-theme
            system instead). Overrides the config file's setting.
        merge_lib: str — which library merges the per-layer PDFs
            ("pypdf" or "pymupdf"). Overrides the config file's setting.
        output_file_name: str — exact output PDF filename, overriding the
            config's assembly-file naming entirely (same as
            board2pdf-cli.py's ``--output``, which "takes precedent over
            --ext" — i.e. this replaces the whole final filename, not
            just its extension/suffix).

    Writes the assembly PDF (and whatever intermediate files the
    resolved config calls for) under the config's configured output
    directory, relative to the board's own folder.
    """
    args = args or {}

    board_path_arg = args.get("board_path")
    if board_path_arg:
        board_filepath = str(Path(board_path_arg))
        if not Path(board_filepath).is_file():
            return _("Ficheiro de placa não encontrado: {path}").format(
                path=board_filepath
            )
    else:
        board_filepath = _get_current_board_path()

    board_dir = Path(board_filepath).parent

    try:
        persistence = _load("persistence")
        plot = _load("plot")
    except SiblingPluginNotFoundError:
        return _not_installed_message()
    except ImportError as exc:
        return _("Erro ao carregar o Board2Pdf: {err}").format(err=exc)

    config_path, error = _resolve_config_path(board_dir, args.get("config_ini_path"))
    if error is not None:
        return error

    try:
        import pcbnew
    except ImportError as exc:
        raise RuntimeError(
            _("pcbnew indisponível — esta ferramenta só funciona dentro do KiCad")
        ) from exc

    try:
        board = pcbnew.LoadBoard(board_filepath)
    except Exception as exc:
        return _("Erro ao carregar a placa {path}: {err}").format(
            path=board_filepath, err=exc
        )

    config = persistence.Persistence(str(config_path))
    config_vars = config.load()

    if args.get("colorize_lib"):
        config_vars["colorize_lib"] = str(args["colorize_lib"])
    if args.get("merge_lib"):
        config_vars["merge_lib"] = str(args["merge_lib"])
    if args.get("output_file_name"):
        config_vars["assembly_file_output"] = str(args["output_file_name"])

    original_cwd = os.getcwd()
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            success = plot.plot_pdfs(board, **config_vars)
    except Exception as exc:
        return _("Erro ao exportar a placa para PDF: {err}\n{log}").format(
            err=exc, log=captured.getvalue()
        )
    finally:
        # plot_pdfs() does os.chdir(dirname(board file)) internally and
        # never restores it — avoid leaking that into the rest of this
        # (long-lived, single-process) plugin's session.
        try:
            os.chdir(original_cwd)
        except OSError:
            pass

    log_text = captured.getvalue().strip()

    if not success:
        lines = [
            _(
                "Falha ao exportar a placa para PDF (ficheiro de "
                "configuração: {cfg})."
            ).format(cfg=config_path)
        ]
        if log_text:
            lines.append("")
            lines.append(log_text)
        return "\n".join(lines)

    lines = [
        _(
            "Placa exportada para PDF com sucesso (ficheiro de "
            "configuração: {cfg})."
        ).format(cfg=config_path)
    ]
    if log_text:
        lines.append("")
        lines.append(log_text)
    return "\n".join(lines)


def register_board2pdf_tools(registry: ActionRegistry) -> None:
    """Register the Board2Pdf-backed export tool on the given
    ActionRegistry. Safe to call even when Board2Pdf isn't installed —
    the handler itself reports that honestly at call time instead of
    failing registration (same pattern as ``register_libforge_tools`` /
    ``register_emc_emi_tools``). NOT wired into chat_action.py by this
    module — a separate integration pass does that.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="export_board_to_pdf",
                description=(
                    "Call this when the user wants to export the PCB (the "
                    "currently open board, or a specific .kicad_pcb file) "
                    "to a documentation/assembly PDF: merged, colorized "
                    "per-layer plots combined into one file. Runs the "
                    "sibling Board2Pdf plugin's real plotting pipeline and "
                    "WRITES a PDF file (and possibly temporary files) to "
                    "disk. Requires the Board2Pdf plugin to be installed; "
                    "reports honestly if it isn't. This MODIFIES the "
                    "filesystem and requires explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "board_path": {
                            "type": "string",
                            "description": (
                                "Path to a .kicad_pcb file to export "
                                "instead of the currently open board."
                            ),
                        },
                        "config_ini_path": {
                            "type": "string",
                            "description": (
                                "Explicit Board2Pdf config .ini to use "
                                "instead of the default resolution order "
                                "(next to the board file, then "
                                "Board2Pdf's own bundled default)."
                            ),
                        },
                        "colorize_lib": {
                            "type": "string",
                            "description": (
                                "PDF colorize library override: 'pypdf', "
                                "'pymupdf' (or 'fitz'), or 'kicad' (use "
                                "KiCad's own color-theme system)."
                            ),
                        },
                        "merge_lib": {
                            "type": "string",
                            "description": (
                                "PDF merge library override: 'pypdf' or "
                                "'pymupdf'."
                            ),
                        },
                        "output_file_name": {
                            "type": "string",
                            "description": (
                                "Exact output PDF filename, overriding "
                                "the config's assembly-file naming "
                                "entirely."
                            ),
                        },
                    },
                    "required": [],
                },
            ),
            handler=export_board_to_pdf,
            read_only=False,
        )
    )
