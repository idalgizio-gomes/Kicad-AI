"""
Write chat tool wrapping the sibling "kbplacer" plugin (adamws), installed
via KiCad's own Plugin and Content Manager (PCM). Places keyboard switch
and diode footprints from a KLE (Keyboard Layout Editor) JSON layout file
— a real, well-architected, niche tool for mechanical-keyboard PCB design.

INVOCATION MECHANISM — different from every other sibling wrapper in this
package, and worth explaining: kbplacer's own ``__main__.py`` (its real,
full-fidelity CLI, confirmed by reading it in full) can NOT be run via
``load_sibling_module`` + a direct in-process function call like other
sibling tools, for two independent reasons, both verified today by
actually running it against KiCad 10.0.4's own embedded Python:

1. ``__main__.py::app()`` calls ``argparse.ArgumentParser().parse_args()``
   with NO way to inject a custom argv — it reads ``sys.argv[1:]``
   implicitly — and on invalid/conflicting input calls ``sys.exit(1)``
   directly. Calling ``app()`` IN-PROCESS (inside this chat's own KiCad
   Python interpreter) would let a ``SystemExit`` from kbplacer's own
   validation propagate up and potentially terminate the ENTIRE running
   KiCad process — unacceptable. Every other sibling tool this package
   wraps calls into functions that return normally; this one's real entry
   point does not.
2. The installed plugin's folder is named after its PCM reverse-DNS
   identifier (``com_github_adamws_kicad-kbplacer``), which contains a
   hyphen — not a valid Python module name — so it cannot be run via
   ``python -m <folder-name>`` either.

SOLUTION (verified end-to-end today, both ``--version`` and ``--help``
confirmed working against the real installed plugin): invoke KiCad's own
embedded Python as a SEPARATE, ISOLATED SUBPROCESS (exactly like
``kikit_tools.py`` already does for KiKit) with a small ``-c`` script that
registers a synthetic package pointing ``__path__`` at the plugin's real
folder (same mechanism ``_sibling_plugin.py`` uses for in-process
imports, just written inline here because this needs to run inside a
CHILD process's own ``-c`` script, not this process), sets
``__version__`` manually (kbplacer's own ``__init__.py`` sets this from a
``_version`` submodule, but that ``__init__.py`` is never executed by
this synthetic-package approach — same "never run the sibling's own
``__init__.py``" policy as every other tool in this package, just applied
inside the child process instead), then imports ``<synthetic
package>.__main__`` and calls its real ``app()`` — with the REAL CLI
arguments appended after the ``-c`` script on the command line, which
Python passes through as ``sys.argv[1:]`` inside the child process exactly
as if kbplacer's own CLI had been invoked directly. A crash/``sys.exit()``
inside kbplacer only terminates that isolated child process.

FULL FIDELITY BY DESIGN: rather than re-implementing kbplacer's several
mini-languages (element position specifiers like ``"SW{} 0 FRONT"`` or
``"D{} CUSTOM 5 -4.5 90 BACK"``, footprint identifiers like
``"path/to/lib.pretty:FootprintName"``) as separate JSON-schema fields,
this tool passes each optional argument through AS THE EXACT RAW STRING
SYNTAX kbplacer's own real CLI parsers expect (confirmed against
``__main__.py``'s own ``add_argument`` calls, read in full) — the real
plugin code does 100% of the parsing/validation itself, so there is no
risk of this wrapper subtly misinterpreting the mini-language, and any
syntax mistake surfaces as kbplacer's own real, accurate error message.

Same lazy-import + RuntimeError-not-raw-exception + i18n ``_()`` trampoline
conventions as every other tool module in this package (subprocess/pcbnew
imports are stdlib, done at module scope like kikit_tools.py — pcbnew
itself is still only ever touched inside the CHILD process, never here).
"""

from __future__ import annotations

import os
import subprocess

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


_SIBLING_IDENTIFIER = "com_github_adamws_kicad-kbplacer"
_TIMEOUT_S = 120

# Inline synthetic-package bootstrap, run INSIDE the child process (see
# module docstring for why this can't be a direct in-process call). The
# plugin directory path is substituted in via .format() at call time —
# safe because it comes from find_pcm_plugin_dir(), never user input.
_BOOTSTRAP_SCRIPT = (
    "import sys, types, importlib\n"
    "pkg = types.ModuleType('kbplacer_runtime')\n"
    "pkg.__path__ = [{plugin_dir!r}]\n"
    "pkg.__version__ = 'unknown'\n"
    "sys.modules['kbplacer_runtime'] = pkg\n"
    "main_mod = importlib.import_module('kbplacer_runtime.__main__')\n"
    "main_mod.app()\n"
)


def _find_kicad_python() -> str | None:
    """Locate the Python interpreter EMBEDDED in a KiCad install. Mirrors
    kikit_tools.py's _find_kicad_python() exactly — kbplacer needs pcbnew,
    same as kikit, and lives in the same embedded interpreter."""
    common_paths = [
        r"C:\Program Files\KiCad\10.0\bin\python.exe",
        r"C:\Program Files\KiCad\9.0\bin\python.exe",
        r"C:\Program Files\KiCad\8.0\bin\python.exe",
        r"C:\Program Files\KiCad\7.0\bin\python.exe",
    ]
    for path in common_paths:
        if os.path.isfile(path):
            return path
    return None


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


def _bool_flag(args: dict, key: str) -> bool:
    try:
        return bool(args.get(key, False))
    except Exception:
        return False


def place_keyboard_switches_and_diodes(args: dict) -> str:
    """Place keyboard switch/diode footprints (and optionally route them)
    from a KLE (Keyboard Layout Editor) JSON layout, via the sibling
    kbplacer plugin's real CLI. Every optional argument below is passed
    through EXACTLY as kbplacer's own real CLI syntax expects it (see
    module docstring) — this tool does not reinterpret the mini-language
    itself, so refer to kbplacer's own documentation for the exact
    grammar of 'switch'/'diode'/'additional_elements' if unsure.

    Required args:
        layout_path: str — path to a KLE JSON layout file.

    Optional args (all default to kbplacer's own real CLI defaults when
    omitted):
        pcb_file_path: str — .kicad_pcb to operate on; defaults to the
            CURRENTLY OPEN board's own saved file. This tool operates on
            the FILE on disk, not the live in-memory board (same
            file-based limitation as kikit_tools.py's panelize_board) —
            close/reopen the board in KiCad afterward to see the change.
        route_switches_with_diodes: bool
        route_rows_and_columns: bool
        switch: str — e.g. "SW{}" or "SW{} 90 BACK".
        diode: str — e.g. "D{} RELATIVE" or "D{} CUSTOM 5 -4.5 90 BACK".
        additional_elements: str — ';'-separated list, e.g.
            "ST{} CUSTOM 0 0 180 BACK;LED{} RELATIVE".
        key_distance: str — "X Y" in mm, e.g. "19.05 19.05".
        layout_offset: str — "X Y" in mm.
        encoder_adjustment: str — "X Y" in mm.
        template_path: str — controller circuit template .kicad_pcb.
        build_board_outline: bool
        outline_delta: number — mm.
        create_pcb_file: bool — create a NEW pcb_file_path instead of
            modifying an existing one; fails if it already exists.
        create_sch_file: bool
        sch_file_path: str
        switch_footprint: str — "path/to/lib.pretty:FootprintName".
        diode_footprint: str
        stabilizer_footprint: str
        encoder_footprint: str
        optimize_diodes_orientation: bool
        start_index: int
        add_stabilizers: bool — default True (kbplacer's own default);
            False maps to kbplacer's --no-stabilizers flag.
    """
    args = args or {}
    layout_path = args.get("layout_path")
    if not layout_path:
        raise RuntimeError(_("Falta o argumento 'layout_path'."))

    pcb_file_path = args.get("pcb_file_path")
    if not pcb_file_path:
        _pcbnew, board = _get_board()
        pcb_file_path = board.GetFileName()
        if not pcb_file_path:
            raise RuntimeError(
                _(
                    "A placa aberta ainda não foi guardada em disco — "
                    "guarde-a, ou indique 'pcb_file_path' explicitamente."
                )
            )

    try:
        plugin_dir = find_pcm_plugin_dir(_SIBLING_IDENTIFIER)
    except SiblingPluginNotFoundError:
        return _(
            "O plugin kbplacer não está instalado nesta máquina — esta "
            "ferramenta precisa dele."
        )

    kicad_python = _find_kicad_python()
    if kicad_python is None:
        raise RuntimeError(
            _(
                "Não foi encontrado o Python embutido do KiCad (necessário "
                "para correr o kbplacer). Verifique se o KiCad está "
                "corretamente instalado."
            )
        )

    cli_args = ["--pcb-file", pcb_file_path, "--layout", layout_path]

    if _bool_flag(args, "route_switches_with_diodes"):
        cli_args.append("--route-switches-with-diodes")
    if _bool_flag(args, "route_rows_and_columns"):
        cli_args.append("--route-rows-and-columns")
    if args.get("switch"):
        cli_args.extend(["--switch", str(args["switch"])])
    if args.get("diode"):
        cli_args.extend(["--diode", str(args["diode"])])
    if args.get("additional_elements"):
        cli_args.extend(["--additional-elements", str(args["additional_elements"])])
    if args.get("key_distance"):
        cli_args.extend(["--key-distance", str(args["key_distance"])])
    if args.get("layout_offset"):
        cli_args.extend(["--layout-offset", str(args["layout_offset"])])
    if args.get("encoder_adjustment"):
        cli_args.extend(["--encoder-adjustment", str(args["encoder_adjustment"])])
    if args.get("template_path"):
        cli_args.extend(["--template", str(args["template_path"])])
    if _bool_flag(args, "build_board_outline"):
        cli_args.append("--build-board-outline")
    if args.get("outline_delta") is not None:
        try:
            outline_delta = float(args["outline_delta"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                _("Argumento 'outline_delta' inválido: {err}").format(err=exc)
            ) from exc
        cli_args.extend(["--outline-delta", str(outline_delta)])
    if _bool_flag(args, "create_pcb_file"):
        cli_args.append("--create-pcb-file")
    if _bool_flag(args, "create_sch_file"):
        cli_args.append("--create-sch-file")
    if args.get("sch_file_path"):
        cli_args.extend(["--sch-file", str(args["sch_file_path"])])
    if args.get("switch_footprint"):
        cli_args.extend(["--switch-footprint", str(args["switch_footprint"])])
    if args.get("diode_footprint"):
        cli_args.extend(["--diode-footprint", str(args["diode_footprint"])])
    if args.get("stabilizer_footprint"):
        cli_args.extend(["--stabilizer-footprint", str(args["stabilizer_footprint"])])
    if args.get("encoder_footprint"):
        cli_args.extend(["--encoder-footprint", str(args["encoder_footprint"])])
    if _bool_flag(args, "optimize_diodes_orientation"):
        cli_args.append("--optimize-diodes-orientation")
    if args.get("start_index") is not None:
        try:
            start_index = int(args["start_index"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                _("Argumento 'start_index' inválido: {err}").format(err=exc)
            ) from exc
        cli_args.extend(["--start-index", str(start_index)])
    if "add_stabilizers" in args and not _bool_flag(args, "add_stabilizers"):
        cli_args.append("--no-stabilizers")

    script = _BOOTSTRAP_SCRIPT.format(plugin_dir=str(plugin_dir))
    argv = [kicad_python, "-c", script] + cli_args

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            # Same reasoning as kikit_tools.py: prevent a flashing console
            # window from the embedded python.exe child process.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            _("Não foi possível iniciar o Python do KiCad: {err}").format(err=exc)
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            _("O kbplacer excedeu o tempo limite de {timeout}s.").format(
                timeout=_TIMEOUT_S
            )
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            _("kbplacer falhou (código {code}):\n{stdout}\n{stderr}").format(
                code=result.returncode,
                stdout=(result.stdout or "").strip(),
                stderr=(result.stderr or "").strip(),
            )
        )

    return _(
        "kbplacer concluído com sucesso em {path}.\n{output}\n\n"
        "Feche e reabra a placa no KiCad para ver a alteração (esta "
        "ferramenta escreve o ficheiro .kicad_pcb diretamente, não a "
        "placa aberta ao vivo)."
    ).format(path=pcb_file_path, output=(result.stdout or "").strip())


def register_kbplacer_tools(registry: ActionRegistry) -> None:
    """Register the kbplacer-backed tool on the given ActionRegistry.

    Safe to call even when the sibling plugin isn't installed — the
    handler reports that honestly at call time, matching every other
    sibling-plugin wrapper in this package.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="place_keyboard_switches_and_diodes",
                description=(
                    "Call this ONLY for mechanical/custom KEYBOARD PCB "
                    "design — places switch and diode footprints (and "
                    "optionally routes them) from a KLE (Keyboard Layout "
                    "Editor) JSON layout file, via the sibling kbplacer "
                    "plugin's real CLI. Writes directly to a .kicad_pcb "
                    "FILE (defaults to the currently open board's own "
                    "file) — the user must close/reopen the board in "
                    "KiCad to see the change. This MODIFIES the "
                    "filesystem and requires explicit user approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "layout_path": {
                            "type": "string",
                            "description": "Path to a KLE JSON layout file.",
                        },
                        "pcb_file_path": {
                            "type": "string",
                            "description": (
                                "Target .kicad_pcb file; defaults to the "
                                "currently open board's own file."
                            ),
                        },
                        "route_switches_with_diodes": {"type": "boolean"},
                        "route_rows_and_columns": {"type": "boolean"},
                        "switch": {
                            "type": "string",
                            "description": (
                                "e.g. 'SW{}' or 'SW{} 90 BACK' — kbplacer's "
                                "own raw syntax, see tool docstring."
                            ),
                        },
                        "diode": {
                            "type": "string",
                            "description": (
                                "e.g. 'D{} RELATIVE' or "
                                "'D{} CUSTOM 5 -4.5 90 BACK'."
                            ),
                        },
                        "additional_elements": {
                            "type": "string",
                            "description": (
                                "';'-separated list, e.g. "
                                "'ST{} CUSTOM 0 0 180 BACK;LED{} RELATIVE'."
                            ),
                        },
                        "key_distance": {
                            "type": "string",
                            "description": "'X Y' in mm, e.g. '19.05 19.05'.",
                        },
                        "layout_offset": {
                            "type": "string",
                            "description": "'X Y' in mm.",
                        },
                        "encoder_adjustment": {
                            "type": "string",
                            "description": "'X Y' in mm.",
                        },
                        "template_path": {
                            "type": "string",
                            "description": "Controller circuit template .kicad_pcb.",
                        },
                        "build_board_outline": {"type": "boolean"},
                        "outline_delta": {
                            "type": "number",
                            "description": "Board outline inflate/deflate, mm.",
                        },
                        "create_pcb_file": {
                            "type": "boolean",
                            "description": (
                                "Create a NEW pcb_file_path instead of "
                                "modifying an existing one; fails if it "
                                "already exists."
                            ),
                        },
                        "create_sch_file": {"type": "boolean"},
                        "sch_file_path": {"type": "string"},
                        "switch_footprint": {
                            "type": "string",
                            "description": "'path/to/lib.pretty:FootprintName'.",
                        },
                        "diode_footprint": {"type": "string"},
                        "stabilizer_footprint": {"type": "string"},
                        "encoder_footprint": {"type": "string"},
                        "optimize_diodes_orientation": {"type": "boolean"},
                        "start_index": {"type": "integer"},
                        "add_stabilizers": {
                            "type": "boolean",
                            "description": "Default true (kbplacer's own default).",
                        },
                    },
                    "required": ["layout_path"],
                },
            ),
            handler=place_keyboard_switches_and_diodes,
            read_only=False,
        )
    )
