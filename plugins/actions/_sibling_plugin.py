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
import os
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


def find_pcm_plugin_dir(identifier: str) -> Path:
    """Resolve a THIRD-PARTY (PCM-installed, not our own fork) sibling
    plugin's directory across installed KiCad versions, newest first.

    Different layout than ``_find_sibling_plugins_dir()``-style helpers used
    for our own forks (LibForge, EMC-EMI): those live in a git repo with a
    nested ``plugins/`` subfolder that a junction points at. A plugin
    installed via KiCad's own Plugin and Content Manager (PCM) has no such
    nesting — ``Documents\\KiCad\\<version>\\3rdparty\\plugins\\<identifier>\\``
    IS the package root (its ``__init__.py`` sits directly in it). Passing
    that folder itself as ``plugins_dir`` to ``load_sibling_module`` works
    identically either way — the synthetic-package trick only cares that
    ``__path__`` points at a real directory containing importable submodules.
    """
    documents = Path(os.path.expanduser("~")) / "Documents" / "KiCad"
    if not documents.is_dir():
        raise SiblingPluginNotFoundError(str(documents))

    candidates = sorted(
        (p for p in documents.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    for version_dir in candidates:
        plugin_dir = version_dir / "3rdparty" / "plugins" / identifier
        if plugin_dir.is_dir():
            return plugin_dir
    raise SiblingPluginNotFoundError(
        f"PCM plugin '{identifier}' not found under {documents}"
    )


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
