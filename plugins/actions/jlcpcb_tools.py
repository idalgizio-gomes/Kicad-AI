"""
Write action wrapping the sibling JLC-Plugin-for-KiCad ("Fabrication
Toolkit" by bennymeg) — a THIRD-PARTY plugin installed via KiCad's own
Plugin and Content Manager (PCM), not one of our own forks — see
``_sibling_plugin.py``'s ``find_pcm_plugin_dir()`` for how its code is
reached without a ``plugins``-package name collision, and
``emc_emi_tools.py``/``libforge_tools.py`` for the structural template this
module follows.

Exposes ``generate_jlcpcb_fabrication_files``: generates JLCPCB production
files (Gerbers, drill files, IPC-356D netlist, BOM, CPL/position file, all
zipped into one archive) from the CURRENTLY OPEN board, using the sibling
plugin's REAL ``process.py::ProcessManager`` methods.

IMPORTANT — this does NOT instantiate the sibling's ``thread.py::
ProcessThread``: constructing it immediately spawns a background ``Thread``
(it calls ``self.start()`` at the end of its own ``__init__``), which does
not mix with this codebase's synchronous tool-handler convention (no clean
way to wait for/report its result back to the chat). Instead, this module
calls ``ProcessManager``'s public methods directly, in EXACTLY the sequence
``ProcessThread.run()`` uses them (confirmed by reading that method in
full), and then reproduces ``ProcessThread.run()``'s own POST-processing —
renaming the archive/CSV files from a custom ``archive_name`` (with KiCad
title-block variable expansion), moving everything into the project's
``production`` folder next to the board file, and the optional timestamped
backup copy — since that logic lives in ``ProcessThread.run()`` itself, not
in ``ProcessManager``, and is what actually determines the FINAL output
path this tool must report back to the user.

Two verified deviations from a first, at-a-glance reading of the sibling's
source, worth flagging explicitly:

1. ``BACKUP_OPT`` is INVERTED relative to its own GUI label. ``plugin.py``'s
   checkbox is labelled "Generate backup files" and defaults to True, but
   ``thread.py::ProcessThread.run()`` contains:

       # Make a backup as long as the BACKUP_OPT flag is set.
       if not self.options[BACKUP_OPT]:
           ...creates a timestamped zip under production/backups/...

   i.e. the comment says the opposite of what the code does — a timestamped
   backup copy is only ever written when ``BACKUP_OPT`` is FALSE, and the
   default/True case silently skips it. This tool wraps the sibling
   plugin's REAL behavior rather than a "corrected" guess of what it should
   do, so ``backup=True`` (this tool's default, matching the checkbox's own
   default) does NOT produce an extra timestamped copy — only
   ``backup=False`` does. Documented again at the call site below and in
   the tool's own parameter description so this doesn't surprise callers.

2. That same rarely-exercised branch calls ``shutil.make_archive(os.path.
   join(output_path, "backups", backup_name), "zip", temp_dir)`` without
   ever creating the ``backups`` subfolder first — ``zipfile.ZipFile``
   opened in write mode raises ``FileNotFoundError`` if its parent
   directory doesn't exist yet, so a first-ever run down this branch would
   crash upstream too. This module creates that subfolder defensively
   before calling into it (a directory-creation safety net, not a change to
   what gets produced or where).

Nothing pcbnew- or sibling-plugin-related is imported at module scope, so
this module (and its tests) import cleanly outside KiCad. Every failure
path raises RuntimeError (never a raw exception) except for the expected
"sibling plugin not installed" case, which returns a plain honest message
like ``register_emc_emi_tools``/``register_libforge_tools`` already do.
"""

from __future__ import annotations

import datetime
import os
import shutil
import tempfile

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


_PACKAGE_NAME = "_sibling_jlcpcb"

# PCM install identifier (the folder name directly under
# Documents\KiCad\<version>\3rdparty\plugins\ — no nested "plugins/"
# subfolder for a PCM-installed third-party plugin, unlike our own forks).
_SIBLING_IDENTIFIER = "com_github_bennymeg_JLC-Plugin-for-KiCad"


def _load(submodule: str):
    plugins_dir = find_pcm_plugin_dir(_SIBLING_IDENTIFIER)
    return load_sibling_module(_PACKAGE_NAME, plugins_dir, submodule)


def _not_installed_message() -> str:
    return _(
        "O plugin JLC-Plugin-for-KiCad (Fabrication Toolkit) não está "
        "instalado nesta máquina — esta ferramenta precisa dele para gerar "
        "os ficheiros de fabrico para a JLCPCB."
    )


def _get_board():
    """Lazily import pcbnew and return the currently open board.

    Raises RuntimeError with a clear message if pcbnew (or an open board)
    is unavailable, instead of letting an ImportError/AttributeError bubble
    up to the LLM tool-calling loop. Mirrors kicad_tools.py's
    ``_get_board()`` (duplicated here, not imported, so this module stays a
    self-contained sibling wrapper like emc_emi_tools.py/libforge_tools.py).
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


def _expand_text_variables(pcbnew_mod, board, text: str) -> str:
    """Mirrors ``ProcessThread.expandTextVariables()``: substitutes KiCad
    title-block variables (``${TITLE}``, ``${REVISION}``, ``${COMMENT1}``,
    ...) and, when available, the board's own project text variables via
    ``pcbnew.ExpandTextVars()``."""
    title_block = board.GetTitleBlock()
    variables = {
        "ISSUE_DATE": title_block.GetDate(),
        "CURRENT_DATE": datetime.datetime.now().strftime("%Y-%m-%d"),
        "REVISION": title_block.GetRevision(),
        "TITLE": title_block.GetTitle(),
        "COMPANY": title_block.GetCompany(),
    }
    for comment_index in range(9):
        variables[f"COMMENT{comment_index + 1}"] = title_block.GetComment(comment_index)

    for var, val in variables.items():
        text = text.replace(f"${{{var}}}", val)

    if hasattr(board, "GetProject") and hasattr(pcbnew_mod, "ExpandTextVars"):
        text = pcbnew_mod.ExpandTextVars(text, board.GetProject())

    return text


def generate_jlcpcb_fabrication_files(args: dict) -> str:
    """Generate JLCPCB production files (Gerbers, drill files, IPC-356D
    netlist, BOM, CPL/position file, all zipped into one archive) from the
    currently open board, using the sibling JLC-Plugin-for-KiCad's real
    ``ProcessManager`` methods.

    Optional args (all mirror the sibling plugin's own GUI/CLI options):
        auto_translate: bool (default False) — apply the plugin's built-in
            per-footprint position/rotation translations database
            (transformations.csv) when generating the position table.
        auto_fill: bool (default False) — refill all copper zones on the
            board first (``update_zone_fills()``).
        exclude_dnp: bool (default False) — exclude Do-Not-Populate
            components from the generated BOM.
        extend_edge_cuts: bool (default False) — also plot User.1 together
            with Edge.Cuts (for V-Cut lines defined on User.1).
        alternative_edge_cuts: bool (default False) — use User.2 as an
            alternative Edge.Cuts layer instead of the real one.
        all_active_layers: bool (default False) — export every active
            layer instead of only the commonly-used subset.
        archive_name: str, optional — custom base name for the generated
            archive/CSV files (may include title-block variables like
            ``${TITLE}``/``${REVISION}``/``${COMMENT1}``). Defaults to
            "<board title or filename> <revision>" when omitted.
        extra_layers: str, optional — comma-separated extra layer names to
            plot in addition to the standard set.
        backup: bool (default True) — see this module's docstring: due to
            the sibling plugin's own (seemingly inverted) logic, this only
            controls an EXTRA timestamped backup copy under
            production/backups/ — False is what creates that extra copy;
            True (the default) skips it and only refreshes the main
            production folder.

    Uses the currently open board directly (must already be saved to disk,
    since the output goes to a "production" folder next to the board file).
    """
    args = args or {}

    def _flag(key: str, default: bool) -> bool:
        try:
            return bool(args.get(key, default))
        except Exception:
            return default

    auto_translate = _flag("auto_translate", False)
    auto_fill = _flag("auto_fill", False)
    exclude_dnp = _flag("exclude_dnp", False)
    extend_edge_cuts = _flag("extend_edge_cuts", False)
    alternative_edge_cuts = _flag("alternative_edge_cuts", False)
    all_active_layers = _flag("all_active_layers", False)
    backup = _flag("backup", True)

    archive_name = args.get("archive_name") or None
    if archive_name is not None:
        archive_name = str(archive_name)
    extra_layers = args.get("extra_layers") or None
    if extra_layers is not None:
        extra_layers = str(extra_layers)

    pcbnew, board = _get_board()
    board_path = board.GetFileName()
    if not board_path:
        raise RuntimeError(
            _(
                "A placa aberta ainda não foi guardada em disco — guarde-a "
                "antes de gerar os ficheiros de fabrico para a JLCPCB."
            )
        )

    try:
        process_mod = _load("process")
        config_mod = _load("config")
        options_mod = _load("options")
    except SiblingPluginNotFoundError:
        return _not_installed_message()
    except ImportError as exc:
        return _("Erro ao carregar o JLC-Plugin-for-KiCad: {err}").format(err=exc)

    options = {
        options_mod.AUTO_TRANSLATE_OPT: auto_translate,
        options_mod.AUTO_FILL_OPT: auto_fill,
        options_mod.EXCLUDE_DNP_OPT: exclude_dnp,
        options_mod.EXTEND_EDGE_CUT_OPT: extend_edge_cuts,
        options_mod.ALTERNATIVE_EDGE_CUT_OPT: alternative_edge_cuts,
        options_mod.ALL_ACTIVE_LAYERS_OPT: all_active_layers,
        options_mod.ARCHIVE_NAME: archive_name,
        options_mod.EXTRA_LAYERS: extra_layers,
        options_mod.BACKUP_OPT: backup,
    }

    process_manager = process_mod.ProcessManager(board)

    project_directory = os.path.dirname(board_path)
    temp_dir = tempfile.mkdtemp(prefix="jlcpcb_chat_")
    temp_dir_gerber = temp_dir + "_g"
    os.makedirs(temp_dir_gerber, exist_ok=True)
    fd, temp_file = tempfile.mkstemp()
    # Unlike thread.py (which leaves this fd open for the rest of the
    # run), close it immediately: an open handle can block the later
    # os.rename()/shutil.move() of this same path on Windows.
    os.close(fd)

    try:
        # Sequence below matches ProcessThread.run() exactly (read in full
        # from the installed plugin), just called synchronously here.
        if options[options_mod.AUTO_FILL_OPT]:
            process_manager.update_zone_fills()

        process_manager.generate_gerber(
            temp_dir_gerber,
            options[options_mod.EXTRA_LAYERS],
            options[options_mod.EXTEND_EDGE_CUT_OPT],
            options[options_mod.ALTERNATIVE_EDGE_CUT_OPT],
            options[options_mod.ALL_ACTIVE_LAYERS_OPT],
        )
        process_manager.generate_drills(temp_dir_gerber)
        process_manager.generate_netlist(temp_dir)
        process_manager.generate_tables(
            temp_dir,
            options[options_mod.AUTO_TRANSLATE_OPT],
            options[options_mod.EXCLUDE_DNP_OPT],
        )
        process_manager.generate_positions(temp_dir)
        process_manager.generate_bom(temp_dir)

        temp_file = process_manager.generate_archive(temp_dir_gerber, temp_file)
        shutil.move(temp_file, temp_dir)
        shutil.rmtree(temp_dir_gerber, ignore_errors=True)
        temp_file = os.path.join(temp_dir, os.path.basename(temp_file))
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        shutil.rmtree(temp_dir_gerber, ignore_errors=True)
        raise RuntimeError(
            _("Erro ao gerar os ficheiros de fabrico para a JLCPCB: {err}").format(
                err=exc
            )
        ) from exc

    # --- Post-processing: mirrors ProcessThread.run()'s placement logic
    # (renaming + moving into the project's "production" folder + optional
    # timestamped backup) — ProcessManager alone only produces files inside
    # the temp dirs above, so this is what determines the FINAL path. ---
    title_block = board.GetTitleBlock()
    title = title_block.GetTitle()
    revision = title_block.GetRevision()

    if hasattr(board, "GetProject") and hasattr(pcbnew, "ExpandTextVars"):
        project = board.GetProject()
        title = pcbnew.ExpandTextVars(title, project)
        revision = pcbnew.ExpandTextVars(revision, project)

    filename = os.path.splitext(os.path.basename(board_path))[0]
    output_path = os.path.join(project_directory, config_mod.outputFolder)
    os.makedirs(output_path, exist_ok=True)

    archive_name_opt = options[options_mod.ARCHIVE_NAME]
    if archive_name_opt:
        base_name = _expand_text_variables(pcbnew, board, archive_name_opt)
    else:
        base_name = "{} {}".format(title or filename, revision or "")

    gerber_archive_name = process_mod.ProcessManager.normalize_filename(
        "_".join((base_name.strip() + ".zip").split())
    )
    os.rename(temp_file, os.path.join(temp_dir, gerber_archive_name))

    if archive_name_opt:
        for src_name, suffix in (
            (config_mod.designatorsFileName, "_designators.csv"),
            (config_mod.placementFileName, "_positions.csv"),
            (config_mod.bomFileName, "_bom.csv"),
        ):
            src = os.path.join(temp_dir, src_name)
            if os.path.exists(src):
                dst_name = process_mod.ProcessManager.normalize_filename(
                    "_".join((base_name.strip() + suffix).split())
                )
                os.rename(src, os.path.join(temp_dir, dst_name))

    # NOTE: `not options[BACKUP_OPT]` — matches ProcessThread.run() EXACTLY
    # as read from the installed plugin (its own comment says "Make a
    # backup as long as the BACKUP_OPT flag is set", but the code does the
    # opposite). See this module's docstring for why this is deliberate.
    backup_path = None
    if not options[options_mod.BACKUP_OPT]:
        # Defensive addition (not present upstream): the plugin's own code
        # would raise FileNotFoundError here on a first-ever run since it
        # never creates this subfolder before calling shutil.make_archive.
        os.makedirs(os.path.join(output_path, "backups"), exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        backup_name = process_mod.ProcessManager.normalize_filename(
            "_".join(("{} {}".format(base_name, timestamp).strip()).split())
        )
        backup_path = shutil.make_archive(
            os.path.join(output_path, "backups", backup_name), "zip", temp_dir
        )

    shutil.copytree(temp_dir, output_path, dirs_exist_ok=True)
    shutil.rmtree(temp_dir, ignore_errors=True)

    try:
        produced_files = sorted(
            f
            for f in os.listdir(output_path)
            if os.path.isfile(os.path.join(output_path, f))
        )
    except OSError:
        produced_files = []

    lines = [
        _("Ficheiros de fabrico JLCPCB gerados em: {path}").format(path=output_path),
        _("Arquivo principal (Gerbers + drill + netlist): {name}").format(
            name=gerber_archive_name
        ),
    ]
    if produced_files:
        lines.append(
            _("Ficheiros na pasta de produção: {files}").format(
                files=", ".join(produced_files)
            )
        )
    if backup_path:
        lines.append(
            _("Cópia de segurança adicional criada em: {path}").format(path=backup_path)
        )

    return "\n".join(lines)


def register_jlcpcb_tools(registry: ActionRegistry) -> None:
    """Register the JLCPCB fabrication-files tool on the given
    ActionRegistry.

    Safe to call even when JLC-Plugin-for-KiCad isn't installed — the
    handler itself reports that honestly at call time instead of failing
    registration (same pattern as ``register_emc_emi_tools()``/
    ``register_libforge_tools()``). NOT wired into chat_action.py by this
    module — a separate integration pass does that.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="generate_jlcpcb_fabrication_files",
                description=(
                    "Call this when the user wants to order the currently "
                    "open PCB from JLCPCB (or asks for Gerbers/drill files/"
                    "BOM/CPL position file for fabrication). Runs the "
                    "sibling JLC-Plugin-for-KiCad (Fabrication Toolkit) "
                    "plugin's real generation code and writes the result "
                    "(a zipped Gerber+drill+netlist archive, plus BOM and "
                    "position CSV files) into a 'production' folder next "
                    "to the board file. The board must already be saved "
                    "to disk. Requires the JLC-Plugin-for-KiCad plugin to "
                    "be installed; reports honestly if it is missing. "
                    "MODIFIES the filesystem and requires explicit user "
                    "approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "auto_translate": {
                            "type": "boolean",
                            "description": (
                                "Apply the plugin's built-in per-footprint "
                                "position/rotation translations database "
                                "when generating the position table "
                                "(default false)."
                            ),
                        },
                        "auto_fill": {
                            "type": "boolean",
                            "description": (
                                "Refill all copper zones on the board "
                                "before generating files (default false)."
                            ),
                        },
                        "exclude_dnp": {
                            "type": "boolean",
                            "description": (
                                "Exclude Do-Not-Populate components from "
                                "the generated BOM (default false)."
                            ),
                        },
                        "extend_edge_cuts": {
                            "type": "boolean",
                            "description": (
                                "Also plot User.1 layer together with "
                                "Edge.Cuts, for V-Cut lines defined on "
                                "User.1 (default false)."
                            ),
                        },
                        "alternative_edge_cuts": {
                            "type": "boolean",
                            "description": (
                                "Use User.2 as an alternative Edge.Cuts "
                                "layer instead of the board's real one "
                                "(default false)."
                            ),
                        },
                        "all_active_layers": {
                            "type": "boolean",
                            "description": (
                                "Export every active layer instead of "
                                "only the commonly-used subset (default "
                                "false)."
                            ),
                        },
                        "archive_name": {
                            "type": "string",
                            "description": (
                                "Custom base name for the generated "
                                "archive/CSV files; may include KiCad "
                                "title-block variables like ${TITLE}, "
                                "${REVISION}, ${COMMENT1}. Defaults to "
                                "'<board title or filename> <revision>' "
                                "when omitted."
                            ),
                        },
                        "extra_layers": {
                            "type": "string",
                            "description": (
                                "Comma-separated extra layer names to "
                                "plot in addition to the standard set."
                            ),
                        },
                        "backup": {
                            "type": "boolean",
                            "description": (
                                "Default true. NOTE: due to the installed "
                                "plugin's own logic, this only controls an "
                                "EXTRA timestamped backup copy under "
                                "production/backups/ — counterintuitively, "
                                "false is what creates that extra copy; "
                                "true (the default) skips it and only "
                                "refreshes the main production folder."
                            ),
                        },
                    },
                    "required": [],
                },
            ),
            handler=generate_jlcpcb_fabrication_files,
            read_only=False,
        )
    )
