"""
Write chat tool wrapping the sibling "Freerouting" plugin (app.freerouting),
installed via KiCad's own Plugin and Content Manager (PCM), reached through
``_sibling_plugin.py``'s ``find_pcm_plugin_dir()``. Runs the real
Freerouting autorouter (a bundled Java .jar) against the currently open
board and imports the routed result back.

Unlike every other sibling plugin wrapped so far, Freerouting is NOT a pure
Python plugin — the actual routing engine is external Java, invoked via
subprocess. This module reduces the plugin's real round-trip (confirmed by
reading its ``plugin.py`` in full) to its three essential, GUI-free steps:

1. ``pcbnew.ExportSpecctraDSN(board, dsn_path)`` — a REAL, confirmed pcbnew
   API function (verified: ``ExportSpecctraDSN(BOARD aBoard, wxString
   aFullFilename) -> bool``) — exports the board to Specctra DSN format.
2. Run the bundled ``freerouting-*.jar`` (path read from the plugin's own
   ``plugin.ini`` — ``[artifact] location`` — never hardcoded, so this
   keeps working across plugin updates) via
   ``java -jar <jar> -de <dsn> -do <ses> -host KiCad``, synchronously
   (``subprocess.run`` with a real, generous timeout — routing can
   genuinely take minutes on a complex board).
3. ``pcbnew.ImportSpecctraSES(board, ses_path)`` — another REAL, confirmed
   pcbnew API function — imports the routed session file back onto the
   live board.

DELIBERATELY NOT REPLICATED: the plugin's own automatic Java-JRE-25
download/install flow (``install_java_jre_25()`` in the real source,
downloads and extracts an entire JRE from Adoptium). This tool only
DETECTS whether a suitable Java (25+) is already available (via
``shutil.which`` + parsing ``java -version``) and, if not, tells the user
to either run the sibling plugin's own toolbar button once (which offers
to install Java interactively) or install Java 25+ manually — downloading
and silently installing an entire runtime is out of scope for an
LLM-triggered chat tool, matching this codebase's general policy against
tools that fetch and run arbitrary external software without a very
deliberate, separately-reviewed design (see chat_action.py's own history
around a paused plugin-search/install feature for the same reasoning).

Same lazy-import + RuntimeError-not-raw-exception + i18n ``_()`` trampoline
conventions as every other tool module in this package.
"""

from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
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
    from ._sibling_plugin import SiblingPluginNotFoundError, find_pcm_plugin_dir
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from actions._sibling_plugin import (  # type: ignore[no-redef]
        SiblingPluginNotFoundError,
        find_pcm_plugin_dir,
    )

try:
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - fallback for flat/test imports
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


_SIBLING_IDENTIFIER = "app_freerouting_kicad-plugin"
_MIN_JAVA_MAJOR_VERSION = 25
_DEFAULT_TIMEOUT_S = 600  # autorouting a real board can genuinely take minutes
_MAX_OUTPUT_CHARS = 4000


def _not_installed_message() -> str:
    return _(
        "O plugin Freerouting não está instalado nesta máquina — esta "
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


def _resolve_jar_path(plugin_dir) -> str:
    config = configparser.ConfigParser()
    ini_path = plugin_dir / "plugin.ini"
    if not ini_path.is_file():
        raise RuntimeError(
            _("Ficheiro de configuração do Freerouting não encontrado: {path}").format(
                path=ini_path
            )
        )
    config.read(ini_path)
    try:
        jar_relative = config["artifact"]["location"]
    except KeyError as exc:
        raise RuntimeError(
            _("plugin.ini do Freerouting não tem a secção [artifact]/location.")
        ) from exc

    jar_path = plugin_dir / jar_relative
    if not jar_path.is_file():
        raise RuntimeError(
            _("Ficheiro .jar do Freerouting não encontrado: {path}").format(path=jar_path)
        )
    return str(jar_path)


def _get_java_version(java_path: str) -> str:
    try:
        result = subprocess.run(
            [java_path, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "0"
    java_info = result.stderr or result.stdout or ""
    match = re.search(r'"?(\d+)[\d._]*"?', java_info)
    return match.group(1) if match else "0"


def _find_suitable_java() -> str | None:
    java_path = shutil.which("java")
    if not java_path:
        return None
    try:
        major = int(_get_java_version(java_path))
    except ValueError:
        major = 0
    if major < _MIN_JAVA_MAJOR_VERSION:
        return None
    return java_path


def _truncate(text: str) -> str:
    if len(text) > _MAX_OUTPUT_CHARS:
        return text[:_MAX_OUTPUT_CHARS] + _("\n... (truncado)")
    return text


def run_freerouting_autoroute(args: dict) -> str:
    """Auto-route the currently open board via the sibling Freerouting
    plugin's real Java engine (export DSN -> run freerouting.jar -> import
    SES back onto the live board).

    Optional args:
        timeout_seconds: number, default 600 — how long to wait for the
            router before giving up. Autorouting can genuinely take
            several minutes on a complex board; raise this for large
            boards.

    Requires a Java runtime version 25+ already available on PATH — this
    tool does NOT download/install Java itself (see module docstring).
    Mutates the LIVE board (adds routed tracks/vias); not auto-saved
    (Ctrl+S persists it).
    """
    args = args or {}
    try:
        timeout_s = float(args.get("timeout_seconds") or _DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumento 'timeout_seconds' inválido: {err}").format(err=exc)
        ) from exc
    if timeout_s <= 0:
        raise RuntimeError(_("'timeout_seconds' deve ser maior que zero."))

    pcbnew, board = _get_board()
    board_path = board.GetFileName()
    if not board_path:
        raise RuntimeError(
            _(
                "A placa aberta ainda não foi guardada em disco — guarde-a "
                "antes de correr o autorouter."
            )
        )

    try:
        plugin_dir = find_pcm_plugin_dir(_SIBLING_IDENTIFIER)
    except SiblingPluginNotFoundError:
        return _not_installed_message()

    jar_path = _resolve_jar_path(plugin_dir)

    java_path = _find_suitable_java()
    if java_path is None:
        raise RuntimeError(
            _(
                "Não foi encontrado um Java {min_version}+ no PATH. Corra o "
                "botão do plugin Freerouting no KiCad uma vez (oferece "
                "instalar o Java automaticamente), ou instale manualmente "
                "a partir de https://adoptium.net/temurin/releases, e "
                "reinicie o KiCad."
            ).format(min_version=_MIN_JAVA_MAJOR_VERSION)
        )

    temp_dir = tempfile.mkdtemp(prefix="freerouting_chat_")
    dsn_path = os.path.join(temp_dir, "freerouting.dsn")
    ses_path = os.path.join(temp_dir, "freerouting.ses")

    try:
        ok = pcbnew.ExportSpecctraDSN(board, dsn_path)
        if not ok or not os.path.isfile(dsn_path):
            raise RuntimeError(_("Falha ao exportar o ficheiro DSN a partir da placa."))

        command = [
            java_path,
            "-jar",
            jar_path,
            "-de",
            dsn_path,
            "-do",
            ses_path,
            "-host",
            "KiCad",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                _(
                    "O Freerouting excedeu o tempo limite de {timeout:.0f}s. "
                    "Aumente 'timeout_seconds' para placas mais complexas."
                ).format(timeout=timeout_s)
            )

        if not os.path.isfile(ses_path):
            raise RuntimeError(
                _(
                    "O Freerouting terminou sem gerar o ficheiro de "
                    "resultado (código {code}).\n{stdout}\n{stderr}"
                ).format(
                    code=result.returncode,
                    stdout=_truncate(result.stdout or ""),
                    stderr=_truncate(result.stderr or ""),
                )
            )

        ok = pcbnew.ImportSpecctraSES(board, ses_path)
        if not ok:
            raise RuntimeError(
                _("Falha ao importar o resultado do routing para a placa.")
            )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    try:
        pcbnew.Refresh()
    except Exception:
        pass

    return _(
        "Autorouting concluído e importado para a placa. Guarde a placa "
        "(Ctrl+S) para persistir a alteração. Verifique o resultado (ex. "
        "run_drc) antes de confiar nele para produção."
    )


def register_freerouting_tools(registry: ActionRegistry) -> None:
    """Register the Freerouting-backed tool on the given ActionRegistry.

    Safe to call even when the sibling plugin isn't installed — the
    handler reports that honestly at call time, matching every other
    sibling-plugin wrapper in this package.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="run_freerouting_autoroute",
                description=(
                    "Call this when the user wants the currently open "
                    "board auto-routed via the sibling Freerouting plugin "
                    "(a real Java-based autorouter). Can take several "
                    "minutes on a complex board — warn the user before "
                    "calling, and consider raising 'timeout_seconds' for "
                    "larger boards. Requires a Java 25+ runtime already "
                    "installed (reports honestly if missing, does not "
                    "install Java itself). This MODIFIES the board "
                    "(adds routed tracks/vias) and requires explicit "
                    "user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "timeout_seconds": {
                            "type": "number",
                            "description": (
                                "Max seconds to wait for the router "
                                "(default 600)."
                            ),
                        },
                    },
                    "required": [],
                },
            ),
            handler=run_freerouting_autoroute,
            read_only=False,
        )
    )
