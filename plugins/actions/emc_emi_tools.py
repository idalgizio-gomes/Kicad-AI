"""
Read-only chat tool that exposes the sibling EMC-EMI Analyzer plugin's real
coupling analysis (FastHenry2 inductive, FastCap2 capacitive) against the
currently open board's copper tracks — see ``_sibling_plugin.py`` for how
its code is reached without a ``plugins``-package name collision.

Never mutates the board (read_only=True): it only reads track geometry via
the sibling's own ``board_extraction.py`` (same lazy ``pcbnew`` import
pattern as ``kicad_tools.py``) and writes solver scratch files to a
tempdir. Honest failure, not a fake result, when either external solver
binary (FastHenry2/FastCap2) isn't installed — the exact same
``SolverNotFoundError``/``SolverExecutionError``/``SolverTimeoutError`` the
EMC-EMI plugin's own GUI would show, surfaced to the LLM as plain text so it
can relay the real reason to the user instead of guessing.
"""

from __future__ import annotations

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

try:
    from ._sibling_plugin import SiblingPluginNotFoundError, load_sibling_module
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from actions._sibling_plugin import (  # type: ignore[no-redef]
        SiblingPluginNotFoundError,
        load_sibling_module,
    )

try:
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - fallback for flat/test imports
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


_PACKAGE_NAME = "_sibling_emc_emi"

# Sibling plugin install path, same reverse-DNS junction convention as this
# plugin itself (see the kicad-plugin-dev skill's identity-and-install
# guide) — resolved relative to KiCad's Documents folder at call time, not
# hardcoded to one KiCad version, so an upgrade doesn't silently break this
# tool the way it would a hardcoded 9.0/10.0 path.
_SIBLING_IDENTIFIER = "com_github_idalgizio-gomes_kicad-emc-emi"


def _find_sibling_plugins_dir() -> Path:
    """Best-effort discovery of the EMC-EMI plugin's ``plugins/`` directory
    across installed KiCad versions (junction target), newest first."""
    import os

    documents = Path(os.path.expanduser("~")) / "Documents" / "KiCad"
    if not documents.is_dir():
        raise SiblingPluginNotFoundError(str(documents))

    candidates = sorted(
        (p for p in documents.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    for version_dir in candidates:
        plugin_dir = (
            version_dir / "3rdparty" / "plugins" / _SIBLING_IDENTIFIER
        )
        if plugin_dir.is_dir():
            return plugin_dir
    raise SiblingPluginNotFoundError(
        f"EMC-EMI Analyzer plugin not found under {documents}"
    )


def _load(submodule: str):
    plugins_dir = _find_sibling_plugins_dir()
    return load_sibling_module(_PACKAGE_NAME, plugins_dir, submodule)


def analyze_board_coupling(args: dict) -> str:
    """Runs real inductive (FastHenry2) and capacitive (FastCap2) coupling
    analysis over the currently open board's copper tracks.

    Optional args:
        selected_only: bool — analyze only tracks currently selected in the
            PCB editor (default: all tracks on the board).
        frequency_hz: number — analysis frequency for the inductive solver
            (default: 100e6, a representative digital-switching-noise band).
    """
    args = args or {}
    try:
        selected_only = bool(args.get("selected_only", False))
    except Exception:
        selected_only = False
    try:
        frequency_hz = float(args.get("frequency_hz", 100e6))
    except (TypeError, ValueError):
        frequency_hz = 100e6

    try:
        board_extraction = _load("board_extraction")
    except SiblingPluginNotFoundError:
        return _(
            "O plugin EMC-EMI Analyzer não está instalado nesta máquina — "
            "esta ferramenta precisa dele para extrair a geometria das "
            "trilhas da placa."
        )
    except ImportError as exc:
        return _("Erro ao carregar o EMC-EMI Analyzer: {err}").format(err=exc)

    try:
        records = (
            board_extraction.selected_tracks()
            if selected_only
            else board_extraction.list_tracks()
        )
    except board_extraction.BoardExtractionError as exc:
        return str(exc)

    if not records:
        return _(
            "Nenhuma trilha de cobre {scope} encontrada na placa aberta."
        ).format(scope=_("selecionada") if selected_only else _("retilínea"))

    fasthenry = _load("solvers.fasthenry_wrapper")
    fastcap = _load("solvers.fastcap_wrapper")
    process_runner = _load("solvers.process_runner")

    conductors = [
        fasthenry.Conductor(
            name=(r.net_name or f"Track{i}"),
            start_mm=(r.start_mm[0], r.start_mm[1], 0.0),
            end_mm=(r.end_mm[0], r.end_mm[1], 0.0),
            width_mm=r.width_mm,
            height_mm=board_extraction.DEFAULT_COPPER_THICKNESS_MM,
        )
        for i, r in enumerate(records, start=1)
    ]

    lines = [
        _("Análise de acoplamento sobre {n} trilha(s) (fonte: {scope}).").format(
            n=len(conductors),
            scope=_("seleção atual") if selected_only else _("toda a placa"),
        )
    ]

    with tempfile.TemporaryDirectory(prefix="emc_emi_chat_") as tmp:
        tmp_path = Path(tmp)

        lines.append("")
        lines.append(_("--- Acoplamento indutivo (FastHenry2, {f:g} Hz) ---").format(f=frequency_hz))
        try:
            coupling = fasthenry.run_fasthenry(
                conductors, frequency_hz, work_dir=tmp_path / "fasthenry"
            )
            names = coupling.conductor_names
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    k = coupling.coupling_coefficient(frequency_hz, i, j)
                    lines.append(f"  {names[i]} <-> {names[j]}: k = {k:.4f}")
        except process_runner.SolverError as exc:
            lines.append(_("  Indisponível: {err}").format(err=exc))
        except Exception as exc:  # geometry edge cases (zero-length track, etc.)
            lines.append(_("  Erro no cálculo indutivo: {err}").format(err=exc))

        plates = [
            fastcap.PlateConductor(
                name=c.name,
                x_mm=(c.start_mm[0] + c.end_mm[0]) / 2,
                y_mm=(c.start_mm[1] + c.end_mm[1]) / 2,
                z_mm=0.0,
                length_mm=max(
                    ((c.end_mm[0] - c.start_mm[0]) ** 2 + (c.end_mm[1] - c.start_mm[1]) ** 2) ** 0.5,
                    0.01,
                ),
                width_mm=c.width_mm,
            )
            for c in conductors
        ]

        lines.append("")
        lines.append(_("--- Acoplamento capacitivo (FastCap2) ---"))
        try:
            capacitance = fastcap.run_fastcap(plates, work_dir=tmp_path / "fastcap")
            names = capacitance.conductor_names
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    c_f = capacitance.mutual_capacitance_f(i, j)
                    lines.append(f"  {names[i]} <-> {names[j]}: C = {c_f * 1e12:.4f} pF")
        except process_runner.SolverError as exc:
            lines.append(_("  Indisponível: {err}").format(err=exc))
        except Exception as exc:
            lines.append(_("  Erro no cálculo capacitivo: {err}").format(err=exc))

    return "\n".join(lines)


def register_emc_emi_tools(registry: ActionRegistry) -> None:
    """Register the EMC-EMI coupling-analysis tool. Safe to call even when
    the sibling plugin isn't installed — the handler itself reports that
    honestly at call time instead of failing registration."""
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="analyze_board_coupling",
                description=(
                    "Call this when the user asks about EMC/EMI risk, "
                    "crosstalk, inductive or capacitive coupling between "
                    "copper tracks on the currently open PCB. Runs the "
                    "sibling EMC-EMI Analyzer plugin's real FastHenry2 "
                    "(inductive) and FastCap2 (capacitive) solvers against "
                    "the board's actual track geometry. Requires those "
                    "solver binaries and the EMC-EMI Analyzer plugin to be "
                    "installed; reports honestly if either is missing."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "selected_only": {
                            "type": "boolean",
                            "description": (
                                "Analyze only tracks currently selected in "
                                "the PCB editor instead of the whole board."
                            ),
                        },
                        "frequency_hz": {
                            "type": "number",
                            "description": (
                                "Analysis frequency in Hz for the inductive "
                                "solver (default 100e6)."
                            ),
                        },
                    },
                    "required": [],
                },
            ),
            handler=analyze_board_coupling,
            read_only=True,
        )
    )
