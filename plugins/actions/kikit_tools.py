"""
Write action exposing KiKit's board panelization (grid array of the current
board, with break-away tabs/cuts) as a chat tool.

KiKit is pip-installed into KiCad's OWN embedded Python interpreter (not a
separate dev venv), and has no reliable console-script/`python -m kikit`
entrypoint — it is invoked as a LIBRARY call via `-c "from kikit.ui import
cli; cli()"`, confirmed for real against a live install:

    "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe" -c
        "from kikit.ui import cli; cli()" panelize --help

which prints kikit's real Click command tree (panelize among drc, export,
fab, modify, present, separate, stencil). `panelize`'s real options were
confirmed by reading the installed kikit/panelize_ui.py's own @click.option
decorators, not guessed: --preset/-p, --layout/-l, --source/-s, --tabs/-t,
--cuts/-c, --framing/-r, --tooling/-o, --fiducials/-f, --copperfill/-u,
--page/-P, --post/-z, --debug, --dump/-d, --plugin. Each of --layout/--cuts/
etc. takes a Section() value: a ';'-separated "key: value" list where the
first bare word (no "key:" prefix) sets the "type" key — e.g.
"grid; rows: 3; cols: 2; hspace: 2mm; vspace: 2mm" sets type=grid plus the
grid's row/col/spacing overrides (fields confirmed from
kikit/panelize_ui_sections.py + kikit/resources/panelizePresets/default.json).
INPUT/OUTPUT are positional: input .kicad_pcb, output .kicad_pcb (a NEW
file — panelize never modifies its input in place).

This tool NEVER touches the currently open live board — unlike
kicad_write_tools.py's mutations (visible immediately in the open PCB
editor), panelize_board only reads the input .kicad_pcb file from disk (by
default the currently open board's own saved file, via pcbnew) and writes a
SEPARATE new .kicad_pcb file; the user opens that output themselves. Still
registered with read_only=False (per this repo's convention) because it
writes a new file to disk and must go through the same approval gate as any
other write action — see actions/framework.py's ActionDefinition.read_only
docstring.

Same lazy-import convention as kicad_write_tools.py: `pcbnew` is imported
INSIDE the handler (only used to default `input_path` to the currently open
board's file), never at module scope, so this module imports cleanly
outside KiCad (pytest has no pcbnew).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

try:
    from .framework import ActionDefinition, ActionRegistry
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from actions.framework import ActionDefinition, ActionRegistry

try:
    from ..llm_providers.base import ToolSpec
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from llm_providers.base import ToolSpec

# i18n: new strings written directly in Portuguese (the plugin's source
# language, see plugins/i18n/__init__.py) and wrapped in _() from the start.
try:
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - fallback for flat/test imports
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


_TIMEOUT_S = 120

# kikit's own real "cuts" section type choices (confirmed via
# kikit/panelize_ui_sections.py) — validated here so an invalid value fails
# fast with a clear Portuguese message instead of a raw kikit stack trace.
_VALID_CUT_TYPES = ["none", "mousebites", "vcuts", "layer", "plugin"]

_DEFAULT_SPACE_MM = 2.0
_DEFAULT_CUT_TYPE = "mousebites"


def _find_kicad_python() -> str | None:
    """Locate the Python interpreter EMBEDDED in a KiCad install (the one
    kikit is pip-installed into), across common install layouts, newest
    KiCad version first.

    Mirrors kicad_tools.py's _find_kicad_cli() pattern exactly, but for
    python.exe instead of kicad-cli.exe — kikit has no separate dev venv,
    it lives inside KiCad's own bundled Python.
    """
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


def _default_input_path() -> str:
    """Best-effort: the currently open board's own saved file, via the same
    lazy pcbnew import convention as kicad_write_tools.py's _get_board()."""
    try:
        import pcbnew
    except ImportError as exc:
        raise RuntimeError(
            _(
                "Não foi indicado 'input_path' e o pcbnew não está disponível "
                "para o determinar automaticamente — esta ferramenta só "
                "consegue adivinhar a placa aberta dentro do KiCad."
            )
        ) from exc

    board = pcbnew.GetBoard()
    if board is None:
        raise RuntimeError(
            _(
                "Não foi indicado 'input_path' e nenhum board KiCad está "
                "atualmente aberto."
            )
        )

    file_name = board.GetFileName()
    if not file_name:
        raise RuntimeError(
            _(
                "Não foi indicado 'input_path' e a placa aberta ainda não foi "
                "guardada (sem nome de ficheiro)."
            )
        )
    return file_name


def _build_layout_section(rows: int, cols: int, h_space_mm: float, v_space_mm: float) -> str:
    return (
        f"grid; rows: {rows}; cols: {cols}; "
        f"hspace: {h_space_mm}mm; vspace: {v_space_mm}mm"
    )


def panelize_board(args: dict) -> str:
    """Panelize a board into an N x M grid array with break-away tabs/cuts,
    via KiKit's real `panelize` command, writing a NEW .kicad_pcb file.

    Required args:
        output_path: path for the NEW panelized .kicad_pcb file. This tool
            NEVER overwrites the input.
        rows: number of rows in the panel grid (int).
        cols: number of columns in the panel grid (int).
    Optional args:
        input_path: the .kicad_pcb to panelize. Defaults to the currently
            open board's own saved file.
        h_space_mm: horizontal spacing between board copies, in mm
            (default 2.0).
        v_space_mm: vertical spacing between board copies, in mm
            (default 2.0).
        cut_type: one of "none", "mousebites", "vcuts", "layer", "plugin"
            (default "mousebites").
    """
    args = args or {}

    input_path = args.get("input_path") or _default_input_path()
    if not os.path.isfile(input_path):
        raise RuntimeError(
            _("Ficheiro de entrada não encontrado: {path}").format(path=input_path)
        )

    output_path = args.get("output_path")
    if not output_path:
        raise RuntimeError(_("Falta o argumento 'output_path'."))

    same_path = (
        os.path.exists(output_path) and os.path.samefile(input_path, output_path)
    ) or Path(input_path).resolve() == Path(output_path).resolve()
    if same_path:
        raise RuntimeError(
            _(
                "'output_path' tem de ser diferente de 'input_path' — esta "
                "ferramenta nunca sobrescreve o ficheiro de entrada."
            )
        )

    try:
        rows = int(args["rows"])
        cols = int(args["cols"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumentos 'rows'/'cols' inválidos: {err}").format(err=exc)
        ) from exc
    if rows < 1 or cols < 1:
        raise RuntimeError(_("'rows' e 'cols' têm de ser inteiros >= 1."))

    try:
        h_space_mm = float(args.get("h_space_mm", _DEFAULT_SPACE_MM))
        v_space_mm = float(args.get("v_space_mm", _DEFAULT_SPACE_MM))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumentos 'h_space_mm'/'v_space_mm' inválidos: {err}").format(err=exc)
        ) from exc

    cut_type = args.get("cut_type") or _DEFAULT_CUT_TYPE
    if cut_type not in _VALID_CUT_TYPES:
        raise RuntimeError(
            _(
                "'cut_type' inválido: '{cut_type}'. Valores aceites: {valid}."
            ).format(cut_type=cut_type, valid=", ".join(_VALID_CUT_TYPES))
        )

    kicad_python = _find_kicad_python()
    if kicad_python is None:
        raise RuntimeError(
            _(
                "Não foi encontrado o Python embutido do KiCad (necessário "
                "para correr o KiKit). Verifique se o KiCad está "
                "corretamente instalado."
            )
        )

    layout_section = _build_layout_section(rows, cols, h_space_mm, v_space_mm)

    argv = [
        kicad_python,
        "-c",
        "from kikit.ui import cli; cli()",
        "panelize",
        "--preset",
        ":default",
        "--layout",
        layout_section,
        "--cuts",
        cut_type,
        input_path,
        output_path,
    ]

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            # KiCad is a windowed GUI process with no console attached;
            # spawning a console-subsystem child (the embedded python.exe)
            # from it makes Windows auto-allocate a new VISIBLE console
            # window for the child, which flashes open and closed. See
            # claude_code_cli_provider.py's identical use of
            # CREATE_NO_WINDOW for the same reasoning. No-op (0) on
            # non-Windows, where there is no console to allocate.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            _("Não foi possível iniciar o Python do KiCad: {err}").format(err=exc)
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            _("O KiKit não respondeu em {timeout}s.").format(timeout=_TIMEOUT_S)
        ) from exc

    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()

    if result.returncode != 0:
        if "ModuleNotFoundError" in stderr and "kikit" in stderr.lower():
            raise RuntimeError(
                _(
                    "O KiKit não está instalado no Python do KiCad. Instale "
                    "com: \"{python}\" -m pip install kikit"
                ).format(python=kicad_python)
            )
        detail = stderr or stdout or _("(sem detalhe)")
        raise RuntimeError(
            _(
                "O KiKit terminou com erro (código {code}): {detail}"
            ).format(code=result.returncode, detail=detail)
        )

    if not os.path.isfile(output_path):
        raise RuntimeError(
            _(
                "O KiKit terminou sem erro mas o ficheiro de saída não foi "
                "criado: {path}"
            ).format(path=output_path)
        )

    return _(
        "Placa painelizada com sucesso: {rows}x{cols}, espaçamento "
        "{h_space}mm x {v_space}mm, cortes '{cut_type}'. Ficheiro novo "
        "gravado em: {output_path}. A placa aberta atualmente NÃO foi "
        "alterada — abra o ficheiro novo no KiCad para o ver."
    ).format(
        rows=rows,
        cols=cols,
        h_space=h_space_mm,
        v_space=v_space_mm,
        cut_type=cut_type,
        output_path=output_path,
    )


def register_kikit_tools(registry: ActionRegistry) -> None:
    """Register the KiKit panelization tool on the given ActionRegistry.

    Same shape as register_kicad_write_tools() — a single opt-in call the
    caller wires in alongside the other register_*_tools() functions (see
    chat_action.py::run_chat()).
    """

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="panelize_board",
                description=(
                    "Call this when the user asks to PANELIZE a board — "
                    "arrange multiple copies of the current PCB into a grid "
                    "array with break-away tabs/cuts for manufacturing, "
                    "using KiKit. This WRITES A NEW .kicad_pcb file to disk "
                    "(never overwrites the input) and requires explicit "
                    "user approval. It does NOT modify the currently open "
                    "live board — the user opens the new panelized file "
                    "themselves."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "input_path": {
                            "type": "string",
                            "description": (
                                "Path to the .kicad_pcb to panelize. "
                                "Defaults to the currently open board's own "
                                "saved file if omitted."
                            ),
                        },
                        "output_path": {
                            "type": "string",
                            "description": (
                                "Path for the NEW panelized .kicad_pcb file. "
                                "Required; must differ from input_path."
                            ),
                        },
                        "rows": {
                            "type": "integer",
                            "description": "Number of rows in the panel grid.",
                        },
                        "cols": {
                            "type": "integer",
                            "description": "Number of columns in the panel grid.",
                        },
                        "h_space_mm": {
                            "type": "number",
                            "description": (
                                "Horizontal spacing between board copies, "
                                "in mm (default 2.0)."
                            ),
                        },
                        "v_space_mm": {
                            "type": "number",
                            "description": (
                                "Vertical spacing between board copies, "
                                "in mm (default 2.0)."
                            ),
                        },
                        "cut_type": {
                            "type": "string",
                            "enum": _VALID_CUT_TYPES,
                            "description": (
                                "Type of break-away cut between board "
                                "copies (default 'mousebites')."
                            ),
                        },
                    },
                    "required": ["output_path", "rows", "cols"],
                },
            ),
            handler=panelize_board,
            read_only=False,
        )
    )
