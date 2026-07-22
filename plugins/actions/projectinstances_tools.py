"""
Read-only chat tool wrapping the sibling "ProjectInstances" plugin
(officialdyray), installed via KiCad's own Plugin and Content Manager
(PCM), reached through ``_sibling_plugin.py``'s ``find_pcm_plugin_dir()``.

ProjectInstances replicates a PCB layout across every instance of a
repeated hierarchical schematic sub-sheet (e.g. the same LED-driver
sub-circuit used 4 times on one board gets the same footprint placement +
traces + zones copied to each instance automatically) — genuinely useful
functionality, NOT a toy.

SCOPE DECISION — read-only status only, the mutating "apply" side is
DELIBERATELY NOT implemented yet: the real replication logic
(``hdata.py::SheetInstance.applyBoard()``, calling into ``placement.py``'s
``FootprintTranslator``/``ReplicateContext``/``enforce_position_footprints``/
``copy_traces``/``copy_zones``) moves footprints and copies traces/zones on
the LIVE board across a hierarchical UUID-path tree — real geometric
surgery with real blast radius if the sheet/anchor-footprint matching logic
is misunderstood. This session had no real hierarchical multi-sheet KiCad
project available to verify the write path end-to-end against, unlike
every OTHER write tool in this codebase (each was checked against a real
project or a real, faithful line-by-line trace of the source). Implementing
it now would mean either guessing at correctness or shipping unverified
board-mutating code — neither acceptable per this codebase's own standard.
A follow-up session with a real hierarchical test project should add
``reapply_hierarchical_pcb_replication`` (calling ``rootInstance.load(cfg)``
+ ``applyChildren()`` with the EXISTING persisted enabled-state from
``<board>.projinst.json`` — never presenting a chat-facing selection UI,
since the plugin's own selection is a checkbox tree with no chat
equivalent; this only reapplies whatever was last configured through the
plugin's own GUI).

VERIFIED FACTS (read directly from ``hplugin.py``/``hdata.py``/
``cfgman.py`` today):

- Config file: ``<board>.projinst.json`` next to the board file (JSON,
  read via ``ConfigMan``, a simple context-manager wrapper — read on
  ``__enter__``, written on ``__exit__``; this tool never writes it back,
  only reads).
- Sheet tree root: ``SheetFile(sheetPath)`` where ``sheetPath = <board
  path>.with_suffix(".kicad_sch")`` (the ROOT schematic, not a sub-sheet).
  ``RootInstance(sheetFile)`` wraps it (calls ``sheetFile.makeRootSheet()``
  internally, then walks ``generate_subsheets()`` recursively).
- ``rootInstance.load(cfg)`` populates each ``SheetInstance``'s
  ``enabled``/anchor state from the config file (defaults: leaf sheets
  start ``enabled=False`` if never configured via the plugin's own GUI).
- Tree shape: each ``SheetInstance`` has ``.name`` (public property),
  ``.sheetFile`` (public property, itself has ``.board``/``.fpByRef``/
  ``.anchorRef`` public properties), and non-leaf instances have
  ``._subSheets`` (a list of child ``SheetInstance`` — accessed directly,
  no public accessor exists in the real source for this one field; same
  situation for ``._uuid``/``._uuidPath``, read directly since the plugin
  itself has no public property for them either). A LEAF instance is one
  whose ``sheetFile.board`` is not None (has a real, valid, footprint-
  populated PCB file next to its schematic); non-leaf instances have no
  board of their own and delegate to their children.

Same lazy-import + RuntimeError-not-raw-exception + i18n ``_()`` trampoline
conventions as every other tool module in this package.
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


_PACKAGE_NAME = "_sibling_projectinstances"
_SIBLING_IDENTIFIER = "com_github_officialdyray_projectinstances"


def _load(submodule: str):
    plugins_dir = find_pcm_plugin_dir(_SIBLING_IDENTIFIER)
    return load_sibling_module(_PACKAGE_NAME, plugins_dir, submodule)


def _not_installed_message() -> str:
    return _(
        "O plugin ProjectInstances não está instalado nesta máquina — esta "
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


def _describe_instance(instance, indent: int, lines: list[str]) -> None:
    prefix = "  " * indent
    sheet_file = instance.sheetFile
    if sheet_file.board is not None:
        enabled = _("ativo") if instance.enabled else _("inativo")
        lines.append(
            f"{prefix}- {instance.name} [{enabled}] "
            f"(âncora: {sheet_file.anchorRef or '?'})"
        )
    else:
        lines.append(f"{prefix}- {instance.name} (folha intermédia, sem placa própria)")
        for child in getattr(instance, "_subSheets", []):
            _describe_instance(child, indent + 1, lines)


def list_hierarchical_sheet_replication_status(args: dict) -> str:
    """List the hierarchical schematic sheet tree and each leaf sheet's
    current PCB-layout-replication status, via the sibling ProjectInstances
    plugin's real sheet-tree walker.

    Read-only — never mutates anything, never applies any replication.
    Reports whatever was last configured through the plugin's own GUI
    (persisted in "<board>.projinst.json" next to the board file); if that
    file doesn't exist yet, every leaf shows as inactive (the plugin's own
    real default).

    NOTE: this codebase does NOT yet offer a tool to actually APPLY the
    replication (move footprints / copy traces+zones across instances) —
    see this module's docstring for why that write path is deliberately
    deferred. Use the plugin's own toolbar button in KiCad for that.
    """
    _pcbnew, board = _get_board()
    board_path = board.GetFileName()
    if not board_path:
        raise RuntimeError(
            _("A placa aberta ainda não foi guardada em disco — não é "
              "possível localizar o esquemático/configuração associados.")
        )

    sch_path = Path(board_path).with_suffix(".kicad_sch")
    if not sch_path.is_file():
        raise RuntimeError(
            _("Esquemático não encontrado: {path}").format(path=sch_path)
        )
    config_path = Path(board_path).with_suffix(".projinst.json")

    try:
        hdata_mod = _load("hdata")
        cfgman_mod = _load("cfgman")
    except SiblingPluginNotFoundError:
        return _not_installed_message()
    except ImportError as exc:
        return _("Erro ao carregar o ProjectInstances: {err}").format(err=exc)

    try:
        sheet_file = hdata_mod.SheetFile(sch_path)
        root_instance = hdata_mod.RootInstance(sheet_file)
    except Exception as exc:
        raise RuntimeError(
            _("Erro ao ler a árvore de folhas hierárquicas: {err}").format(err=exc)
        ) from exc

    with cfgman_mod.ConfigMan(config_path) as cfg:
        hdata_mod.sheetFileManager.load_file_data(cfg)
        root_instance.load(cfg)

    lines = [
        _("Árvore de folhas hierárquicas para {sch}:").format(sch=sch_path),
        _(
            "(estado a partir de {config} — se o ficheiro não existir, "
            "todas as folhas aparecem como inativas por omissão)"
        ).format(config=config_path),
        "",
    ]
    for child in getattr(root_instance, "_subSheets", []):
        _describe_instance(child, 0, lines)

    if len(lines) == 3:
        lines.append(_("(nenhuma sub-folha encontrada — esquemático plano/sem hierarquia)"))

    return "\n".join(lines)


def register_projectinstances_tools(registry: ActionRegistry) -> None:
    """Register the ProjectInstances-backed read-only tool on the given
    ActionRegistry.

    Safe to call even when the sibling plugin isn't installed — the
    handler reports that honestly at call time, matching every other
    sibling-plugin wrapper in this package.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="list_hierarchical_sheet_replication_status",
                description=(
                    "Call this when the user asks about hierarchical "
                    "schematic sheet structure or which repeated sub-sheets "
                    "have PCB-layout replication enabled/configured (via "
                    "the sibling ProjectInstances plugin). Read-only — does "
                    "NOT apply/change any replication, only reports the "
                    "current status as last configured through the "
                    "plugin's own GUI."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            handler=list_hierarchical_sheet_replication_status,
            read_only=True,
        )
    )
