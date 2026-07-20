"""
Read-only KiCad tools exposed to the LLM as callable actions.

IMPORTANT: `pcbnew` is imported lazily, INSIDE each handler function, never
at module scope. This lets the module (and its tests) import cleanly
outside of KiCad. If `pcbnew` is unavailable at call time, handlers raise
`RuntimeError`, which the actions framework (framework.py) turns into a
plain error message returned to the LLM instead of crashing the plugin.

All four tools are read-only:
- get_project_info: board/file summary (footprint count, net count, layers)
- list_components: footprint list, with optional substring filter
- run_drc: Design Rule Check report via pcbnew's DRC writer
- run_erc: Electrical Rule Check report via `kicad-cli sch erc`
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from .framework import ActionDefinition, ActionRegistry
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from actions.framework import ActionDefinition, ActionRegistry

try:
    from ..llm_providers.base import ToolSpec
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from llm_providers.base import ToolSpec

# i18n: this module predates the i18n infrastructure and its EXISTING result
# strings are left as untranslated Portuguese (a separately-tracked
# follow-up) — but NEW additions (list_tracks) wrap in _() from the start,
# using the exact same import-shim pattern as kicad_write_tools.py: never
# `from ..i18n import _` directly, that breaks live language switching.
try:
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - fallback for flat/test imports
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


_MAX_COMPONENT_LINES = 200
_MAX_DRC_CHARS = 8000
_MAX_ERC_CHARS = 8000
_MAX_TRACK_LINES = 200


def _get_board():
    """Lazily import pcbnew and return the currently open board.

    Raises RuntimeError with a clear message if pcbnew (or an open board)
    is unavailable, instead of letting an ImportError/AttributeError bubble
    up to the LLM tool-calling loop.
    """
    try:
        import pcbnew
    except ImportError as exc:
        raise RuntimeError(
            "pcbnew indisponível — esta ferramenta só funciona dentro do KiCad"
        ) from exc

    board = pcbnew.GetBoard()
    if board is None:
        raise RuntimeError("Nenhum board KiCad está atualmente aberto")
    return pcbnew, board


def get_project_info(args: dict) -> str:
    """Return a human-readable summary of the currently open PCB project."""
    pcbnew, board = _get_board()

    try:
        file_name = board.GetFileName() or "(não guardado)"
    except Exception:
        file_name = "(desconhecido)"

    try:
        footprint_count = len(list(board.GetFootprints()))
    except Exception:
        footprint_count = -1

    try:
        net_count = board.GetNetCount()
    except Exception:
        net_count = -1

    try:
        copper_layers = board.GetCopperLayerCount()
    except Exception:
        copper_layers = -1

    lines = [
        "Project info:",
        f"- File: {file_name}",
        f"- Footprints: {footprint_count}",
        f"- Nets: {net_count}",
        f"- Copper layers: {copper_layers}",
    ]
    return "\n".join(lines)


def list_components(args: dict) -> str:
    """List footprints on the board (reference, value, footprint id, layer).

    Optional args:
        filter: case-insensitive substring matched against reference or value.
    """
    pcbnew, board = _get_board()

    filter_text = (args or {}).get("filter")
    filter_lower = filter_text.lower() if filter_text else None

    rows = []
    for fp in board.GetFootprints():
        try:
            reference = fp.GetReference()
        except Exception:
            reference = "?"
        try:
            value = fp.GetValue()
        except Exception:
            value = "?"
        try:
            fp_id = fp.GetFPIDAsString()
        except Exception:
            fp_id = "?"
        try:
            layer = fp.GetLayerName()
        except Exception:
            layer = "?"

        if filter_lower is not None:
            haystack = f"{reference} {value}".lower()
            if filter_lower not in haystack:
                continue

        rows.append(f"{reference}\t{value}\t{fp_id}\t{layer}")

    truncated = False
    if len(rows) > _MAX_COMPONENT_LINES:
        rows = rows[:_MAX_COMPONENT_LINES]
        truncated = True

    header = "Reference\tValue\tFootprint\tLayer"
    body = "\n".join(rows) if rows else "(nenhum componente encontrado)"
    result = f"{header}\n{body}"
    if truncated:
        result += f"\n... (truncado a {_MAX_COMPONENT_LINES} linhas)"
    return result


def run_drc(args: dict) -> str:
    """Run KiCad's Design Rule Check and return the report text."""
    pcbnew, board = _get_board()

    if not hasattr(pcbnew, "WriteDRCReport"):
        return (
            "A função WriteDRCReport não está disponível nesta versão do "
            "KiCad — não é possível correr o DRC a partir deste plugin."
        )

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".rpt")
        os.close(fd)

        units = getattr(pcbnew, "EDA_UNITS_MM", None)
        try:
            if units is not None:
                pcbnew.WriteDRCReport(board, tmp_path, units, True)
            else:
                pcbnew.WriteDRCReport(board, tmp_path, True)
        except TypeError:
            # Signature differs across KiCad versions; try a minimal call.
            pcbnew.WriteDRCReport(board, tmp_path)

        text = Path(tmp_path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Erro ao correr o DRC: {exc}"
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    if len(text) > _MAX_DRC_CHARS:
        text = text[:_MAX_DRC_CHARS] + f"\n... (truncado a {_MAX_DRC_CHARS} caracteres)"
    return text


def _find_kicad_cli() -> str | None:
    """Locate the kicad-cli executable across common install layouts."""
    candidate = shutil.which("kicad-cli")
    if candidate:
        return candidate

    try:
        exe_dir = Path(sys.executable).resolve().parent
        candidate_path = exe_dir / "kicad-cli.exe"
        if candidate_path.is_file():
            return str(candidate_path)
    except Exception:
        pass

    common_paths = [
        r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe",
        r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
    ]
    for path in common_paths:
        if os.path.isfile(path):
            return path

    return None


def run_erc(args: dict) -> str:
    """Run KiCad's Electrical Rule Check via `kicad-cli sch erc`."""
    pcbnew, board = _get_board()

    try:
        board_path = board.GetFileName()
    except Exception:
        board_path = None

    if not board_path:
        return "Não foi possível determinar o caminho do projeto para localizar o esquemático."

    sch_path = str(Path(board_path).with_suffix(".kicad_sch"))
    if not os.path.isfile(sch_path):
        return f"Esquemático não encontrado: {sch_path}"

    cli_path = _find_kicad_cli()
    if not cli_path:
        return (
            "kicad-cli não encontrado — não é possível correr o ERC. "
            "Verifique se o KiCad está corretamente instalado."
        )

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".rpt")
        os.close(fd)

        result = subprocess.run(
            [
                cli_path,
                "sch",
                "erc",
                "--output",
                tmp_path,
                "--format",
                "report",
                sch_path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        report_text = ""
        if os.path.isfile(tmp_path):
            report_text = Path(tmp_path).read_text(encoding="utf-8", errors="replace")

        if not report_text.strip():
            report_text = (result.stdout or "") + (result.stderr or "")
            if not report_text.strip():
                report_text = f"(sem saída; código de saída {result.returncode})"
    except subprocess.TimeoutExpired:
        return "O ERC excedeu o tempo limite (120s)."
    except Exception as exc:
        return f"Erro ao correr o ERC: {exc}"
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    if len(report_text) > _MAX_ERC_CHARS:
        report_text = report_text[:_MAX_ERC_CHARS] + f"\n... (truncado a {_MAX_ERC_CHARS} caracteres)"
    return report_text


def list_tracks(args: dict) -> str:
    """List copper tracks and vias on the board (uuid, kind, net, layer,
    geometry). ``board.GetTracks()`` returns tracks AND vias mixed together
    (told apart via ``GetClass()``) and does NOT preserve insertion order —
    the UUID is the only stable way to refer back to a specific item, e.g.
    for delete_track.

    Optional args:
        net_filter: case-insensitive substring matched against the net name.
    """
    pcbnew, board = _get_board()

    filter_text = (args or {}).get("net_filter")
    filter_lower = filter_text.lower() if filter_text else None

    rows = []
    for item in board.GetTracks():
        try:
            uuid_str = item.m_Uuid.AsString()
        except Exception:
            uuid_str = "?"
        try:
            is_via = item.GetClass() == "PCB_VIA"
        except Exception:
            is_via = False
        kind = "via" if is_via else "track"
        try:
            net_name = item.GetNetname()
        except Exception:
            net_name = "?"
        try:
            layer_name = board.GetLayerName(item.GetLayer())
        except Exception:
            layer_name = "?"

        if filter_lower is not None:
            if filter_lower not in (net_name or "").lower():
                continue

        if is_via:
            try:
                pos = item.GetPosition()
                geometry = "pos=({x:.3f}, {y:.3f}) mm".format(
                    x=pcbnew.ToMM(pos.x), y=pcbnew.ToMM(pos.y)
                )
            except Exception:
                geometry = "?"
        else:
            try:
                start = item.GetStart()
                end = item.GetEnd()
                geometry = (
                    "start=({sx:.3f}, {sy:.3f}) mm end=({ex:.3f}, {ey:.3f}) mm"
                ).format(
                    sx=pcbnew.ToMM(start.x),
                    sy=pcbnew.ToMM(start.y),
                    ex=pcbnew.ToMM(end.x),
                    ey=pcbnew.ToMM(end.y),
                )
            except Exception:
                geometry = "?"

        rows.append(f"{uuid_str}\t{kind}\t{net_name}\t{layer_name}\t{geometry}")

    truncated = False
    if len(rows) > _MAX_TRACK_LINES:
        rows = rows[:_MAX_TRACK_LINES]
        truncated = True

    header = "UUID\tKind\tNet\tLayer\tGeometry"
    body = "\n".join(rows) if rows else _("(nenhuma trilha/via encontrada)")
    result = f"{header}\n{body}"
    if truncated:
        result += _("\n... (truncado a {max_lines} linhas)").format(
            max_lines=_MAX_TRACK_LINES
        )
    return result


def register_kicad_tools(registry: ActionRegistry) -> None:
    """Register all read-only KiCad tools on the given ActionRegistry."""

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="get_project_info",
                description=(
                    "Call this when the user asks general questions about the "
                    "currently open PCB project, such as its file name, how "
                    "many footprints/components it has, how many nets, or how "
                    "many copper layers the board uses."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            handler=get_project_info,
            read_only=True,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="list_components",
                description=(
                    "Call this when the user asks what components/footprints "
                    "are on the board, or wants to search for a specific "
                    "reference or value (e.g. 'list all resistors', 'find C12'). "
                    "Optionally pass a 'filter' substring to narrow the results."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "filter": {
                            "type": "string",
                            "description": (
                                "Case-insensitive substring to match against "
                                "component reference or value."
                            ),
                        }
                    },
                    "required": [],
                },
            ),
            handler=list_components,
            read_only=True,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="run_drc",
                description=(
                    "Call this when the user asks to check the board for "
                    "design rule violations, clearance errors, or wants a "
                    "Design Rule Check (DRC) report of the currently open PCB."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            handler=run_drc,
            read_only=True,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="run_erc",
                description=(
                    "Call this when the user asks to check the schematic for "
                    "electrical rule violations or wants an Electrical Rule "
                    "Check (ERC) report of the project's schematic."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            handler=run_erc,
            read_only=True,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="list_tracks",
                description=(
                    "Call this when the user asks what tracks/vias are on "
                    "the board, wants to inspect routing, or needs a "
                    "track/via's UUID before deleting it with delete_track. "
                    "Optionally pass a 'net_filter' substring to narrow "
                    "results to a specific net."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "net_filter": {
                            "type": "string",
                            "description": (
                                "Case-insensitive substring to match against "
                                "the net name."
                            ),
                        }
                    },
                    "required": [],
                },
            ),
            handler=list_tracks,
            read_only=True,
        )
    )
