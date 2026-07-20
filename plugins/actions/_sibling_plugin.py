"""
Loader for reaching into a SIBLING KiCad plugin's code (EMC-EMI Analyzer,
LibForge) from inside this plugin's tool handlers, without ever putting
their code on ``sys.path`` under the name ``plugins`` — every KiCad plugin
folder in this workspace is itself a top-level package literally named
``plugins``, so a naive ``sys.path.insert`` + ``import solvers.x`` would
collide with (or silently shadow) THIS plugin's own ``plugins`` package the
moment both are loaded in the same KiCad process.

Approach: construct a synthetic package object for the sibling's ``plugins/``
directory under a UNIQUE name (e.g. ``"_sibling_emc_emi"``), register it in
``sys.modules`` ourselves with ``__path__`` pointing at that real directory,
and import submodules through it via ``importlib.import_module``. This
never executes the sibling's own ``plugins/__init__.py`` (which may import
``wx``/``pcbnew`` and, for EMC-EMI/LibForge, register a classic
``pcbnew.ActionPlugin`` as an import-time side effect — registering that a
SECOND time from inside this plugin's process would be a real bug, not a
theoretical one) — only the specific submodule files this plugin actually
needs are ever imported, and their own internal relative imports (e.g.
``from .i18n import _``) resolve correctly because the synthetic package's
``__path__`` makes them real submodules as far as Python's import system is
concerned.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path


class SiblingPluginNotFoundError(RuntimeError):
    """The sibling plugin's ``plugins/`` directory does not exist on disk —
    it is simply not installed for this user, a real and expected outcome
    (not every user has EMC-EMI/LibForge installed alongside this plugin)."""


def _ensure_synthetic_package(unique_name: str, plugins_dir: Path) -> None:
    if unique_name in sys.modules:
        return
    if not plugins_dir.is_dir():
        raise SiblingPluginNotFoundError(
            f"Sibling plugin directory not found: {plugins_dir}"
        )
    pkg = types.ModuleType(unique_name)
    pkg.__path__ = [str(plugins_dir)]
    pkg.__package__ = unique_name
    sys.modules[unique_name] = pkg


def load_sibling_module(unique_package_name: str, plugins_dir: Path, submodule: str):
    """Import ``submodule`` (dotted, e.g. ``"solvers.fasthenry_wrapper"``)
    from the sibling plugin rooted at ``plugins_dir``, under the synthetic
    package ``unique_package_name`` — reused across calls for the same
    sibling (each submodule is only ever actually imported once, same as
    any normal Python import; ``sys.modules`` caches it).

    Raises ``SiblingPluginNotFoundError`` if ``plugins_dir`` doesn't exist,
    or lets a genuine ``ImportError`` from inside the sibling's own code
    propagate unchanged (e.g. a submodule that itself needs ``pcbnew`` at
    import time and isn't running inside KiCad) — callers decide how to
    turn either into a chat-facing message.
    """
    _ensure_synthetic_package(unique_package_name, plugins_dir)
    return importlib.import_module(f"{unique_package_name}.{submodule}")
