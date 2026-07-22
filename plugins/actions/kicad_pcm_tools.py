"""
Chat tools for SEARCHING and INSTALLING KiCad plugins/content — either from
KiCad's own official Plugin and Content Manager (PCM) catalog, or directly
from a GitHub repository the user names.

VERIFIED FACTS (fetched directly from KiCad's own official sources today,
never guessed — installing arbitrary third-party code is too high-stakes to
get wrong):

- The official PCM repository is published at
  https://gitlab.com/kicad/addons/repository (KiCad's own GitLab namespace).
  Its ``repository.json`` (schema_version 2) points at
  ``https://gitlab.com/kicad/addons/repository/-/raw/main/packages.json`` —
  fetched and confirmed today to be a real, live JSON document of the shape
  ``{"packages": [...]}`` with 50+ real entries.
- Each package entry has: ``name``, ``identifier`` (reverse-DNS, e.g.
  ``"com.github.pointhi.kicad-color-schemes.solarized-light"``),
  ``description``, ``description_full``, ``author``/``maintainer``
  (``{"name", "contact": {"web"/"email"}}``), ``license``, ``type`` (e.g.
  "plugin", "colortheme", "library"), optional ``tags`` (list of strings —
  the best field to match a free-text search against), ``resources`` (dict
  of link-name -> URL, e.g. ``{"Github": "..."}``), and ``versions``: a list
  of ``{"version", "status", "kicad_version", "kicad_version_max"?,
  "platforms"?, "download_url", "download_sha256", "download_size",
  "install_size"}``.
- Confirmed from this machine's OWN
  ``%APPDATA%\\kicad\\<version>\\installed_packages.json`` (real, already
  installed packages, same schema): the on-disk install folder name under
  ``Documents\\KiCad\\<version>\\3rdparty\\plugins\\`` is the package
  ``identifier`` with every ``"."`` replaced by ``"_"`` — e.g. identifier
  ``"com.github.Steffen-W.KiCad-Parasitics"`` -> folder
  ``com_github_Steffen-W_KiCad-Parasitics`` (verified against a real
  installed plugin already used elsewhere in this codebase,
  ``kicad_parasitics_tools.py``'s own ``_SIBLING_IDENTIFIER``).
- Structural assumption for the downloaded zip (not independently verified
  against a live download this session, but strongly evidenced: EVERY
  installed 3rd-party plugin folder observed on this machine contains its
  Python files DIRECTLY, e.g. ``__init__.py`` at the folder's own top level)
  — PCM zips are known (see ``kicad-plugin-dev`` skill's identity-and-install
  reference) to bundle a ``metadata.json`` + a ``plugins/`` subfolder (the
  ACTUAL package content) + optionally ``resources/``. This module therefore
  extracts the zip to a temp staging directory, looks for a ``plugins/``
  subfolder ANYWHERE inside it, and installs THAT subfolder's contents as
  the final package directory — falling back to the zip's own root if no
  ``plugins/`` subfolder is found (some simpler packages may not nest one).

SECURITY — read before changing anything here:
- Every install is a write action (``read_only=False``) and goes through
  this codebase's mandatory per-call approval dialog — the user always sees
  which plugin/identifier/source is about to be installed before it happens.
- The downloaded zip's SHA256 is ALWAYS verified against the ``download_sha256``
  declared for the chosen version, for BOTH the official catalog and a
  GitHub-direct install — a mismatch is a hard RuntimeError, never a
  soft warning, since this is the one integrity check available at all.
- IMPORTANT DISTINCTION nonetheless: for the OFFICIAL catalog, the hash
  and metadata reflect a package that has gone through KiCad's own
  addons-metadata submission process. For a GitHub-direct install, the
  hash is self-declared by the repository's own author in their own
  ``metadata.json`` — verifying against it only catches transport
  corruption/tampering AFTER that file was fetched, it is NOT independent
  third-party vetting of the plugin's actual behavior. Every GitHub-direct
  result/approval message says this explicitly — never implied to be as
  trustworthy as an official-catalog install.
- Zip extraction is zip-slip-safe: every archive member's resolved path is
  checked to stay INSIDE the staging directory before writing, rejecting
  (RuntimeError) any archive that tries to escape it via ``../`` traversal
  or an absolute path.
- Installing NEVER makes KiCad load the new plugin immediately — KiCad only
  rescans installed plugins at startup (see the ``kicad-plugin-dev`` skill).
  Every successful install result explicitly tells the user to restart
  KiCad. This module also never touches KiCad's own
  ``installed_packages.json`` (that's the real PCM's bookkeeping file) —
  only the plugin FILES themselves are placed under
  ``3rdparty\\plugins\\<identifier_with_underscores>\\``, exactly where
  KiCad's own plugin scanner looks regardless of how they got there
  (manual junction, PCM's own installer, or this tool).

Same lazy-network / no-module-scope-side-effects convention as the rest of
this codebase: nothing network-related runs at import time. i18n via the
same ``_()`` trampoline pattern used everywhere else in this repo.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from hashlib import sha256
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
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - fallback for flat/test imports
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


_OFFICIAL_PACKAGES_URL = (
    "https://gitlab.com/kicad/addons/repository/-/raw/main/packages.json"
)
_HTTP_TIMEOUT_S = 30
_CACHE_TTL_S = 24 * 60 * 60  # 24h — the official catalog changes slowly
_CACHE_PATH = Path(tempfile.gettempdir()) / "kicad_chat_assistant_pcm_cache.json"
_MAX_SEARCH_RESULTS = 20
_USER_AGENT = "KiCad-Chat-Assistant-PCM-Tool/1.0"


def _http_get(url: str) -> bytes:
    """GET ``url`` with a real User-Agent (some hosts, e.g. GitHub raw
    content, reject requests without one) and a bounded timeout. Raises
    RuntimeError (never a raw urllib exception) on any failure."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            _("Pedido HTTP falhou ({code}) para {url}: {err}").format(
                code=exc.code, url=url, err=exc
            )
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            _("Não foi possível contactar {url}: {err}").format(url=url, err=exc.reason)
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            _("Erro de rede ao contactar {url}: {err}").format(url=url, err=exc)
        ) from exc


def _load_official_packages(force_refresh: bool = False) -> list[dict]:
    """Returns the official catalog's ``packages`` list, using a 24h local
    cache (in the OS temp dir) so a chat search doesn't re-fetch a
    multi-hundred-KB file on every call."""
    if not force_refresh and _CACHE_PATH.is_file():
        age_s = time.time() - _CACHE_PATH.stat().st_mtime
        if age_s < _CACHE_TTL_S:
            try:
                cached = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
                return cached.get("packages", [])
            except (OSError, ValueError):
                pass  # fall through to a fresh fetch

    raw = _http_get(_OFFICIAL_PACKAGES_URL)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(
            _("Resposta inválida (não é JSON) do catálogo oficial: {err}").format(err=exc)
        ) from exc

    try:
        _CACHE_PATH.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass  # caching is a best-effort optimization, never fatal

    return data.get("packages", [])


def _fetch_github_metadata(github_owner_repo: str) -> dict:
    """Fetch a GitHub repo's own PCM ``metadata.json`` (the package
    descriptor a plugin author places at their repo's root — same schema as
    an official-catalog entry, see this module's docstring) via GitHub's raw
    content CDN. ``github_owner_repo`` is "owner/repo" (no URL, no
    branch/tag — always resolves the repo's default branch via the "HEAD"
    alias GitHub's raw-content host supports)."""
    owner_repo = github_owner_repo.strip().strip("/")
    if "/" not in owner_repo:
        raise RuntimeError(
            _(
                "'github_owner_repo' inválido: '{value}' — use o formato "
                "'dono/repositorio'."
            ).format(value=github_owner_repo)
        )
    url = f"https://raw.githubusercontent.com/{owner_repo}/HEAD/metadata.json"
    raw = _http_get(url)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(
            _(
                "O repositório '{repo}' não tem um metadata.json válido na "
                "raiz (ou não é um pacote KiCad PCM): {err}"
            ).format(repo=owner_repo, err=exc)
        ) from exc


def _pick_version(package: dict, kicad_version: str | None) -> dict:
    """Pick the best version entry: the newest matching ``kicad_version``
    (if given, a simple string prefix/equality match against each version's
    own ``kicad_version`` field) with status "stable" if any, else just the
    first entry (the catalog lists newest-first in every example observed).
    """
    versions = package.get("versions") or []
    if not versions:
        raise RuntimeError(
            _("O pacote '{name}' não tem nenhuma versão publicada.").format(
                name=package.get("name", package.get("identifier", "?"))
            )
        )

    candidates = versions
    if kicad_version:
        matching = [
            v for v in versions if str(v.get("kicad_version", "")) == str(kicad_version)
        ]
        if matching:
            candidates = matching

    stable = [v for v in candidates if v.get("status") == "stable"]
    return stable[0] if stable else candidates[0]


def _newest_kicad_documents_dir() -> Path:
    """Newest ``Documents\\KiCad\\<version>`` directory on this machine —
    where a newly installed plugin's files should land, mirroring how every
    OTHER sibling-plugin resolver in this codebase picks the newest
    installed KiCad version first (see ``_sibling_plugin.py``'s
    ``find_pcm_plugin_dir``)."""
    documents = Path(os.path.expanduser("~")) / "Documents" / "KiCad"
    if not documents.is_dir():
        raise RuntimeError(
            _("Pasta de dados do KiCad não encontrada: {path}").format(path=documents)
        )
    candidates = sorted(
        (p for p in documents.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError(
            _("Nenhuma versão do KiCad encontrada em {path}").format(path=documents)
        )
    return candidates[0]


def _safe_extract(zip_path: Path, dest_dir: Path) -> None:
    """Extract ``zip_path`` into ``dest_dir``, rejecting (RuntimeError) any
    archive member whose resolved path would land outside ``dest_dir``
    ("zip-slip" — a path like "../../evil.py" or an absolute path)."""
    dest_dir_resolved = dest_dir.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.namelist():
            target = (dest_dir_resolved / member).resolve()
            if dest_dir_resolved not in target.parents and target != dest_dir_resolved:
                raise RuntimeError(
                    _(
                        "Arquivo rejeitado por segurança: entrada '{member}' "
                        "tentaria escrever fora da pasta de destino."
                    ).format(member=member)
                )
        archive.extractall(dest_dir_resolved)


def _find_package_root(staging_dir: Path) -> Path:
    """Real PCM zips bundle a ``plugins/`` subfolder as the actual package
    content (alongside ``metadata.json``/``resources/`` at the zip root) —
    every 3rd-party plugin already installed on this machine confirms this
    shape (its files sit directly under ``3rdparty\\plugins\\<id>\\``, not
    nested one level deeper). Search for a ``plugins`` directory anywhere
    under the extracted archive; fall back to the archive's own root if none
    is found (a simpler/older package layout)."""
    for candidate in staging_dir.rglob("plugins"):
        if candidate.is_dir():
            return candidate
    return staging_dir


def search_kicad_plugins(args: dict) -> str:
    """Search for installable KiCad plugins/content, either in KiCad's own
    official PCM catalog or in a specific GitHub repository.

    Required args:
        query: str — case-insensitive substring matched against the
            package's name/description/identifier/tags. Ignored (returns
            everything, capped) if empty and source="official"; REQUIRED to
            be a real repo when source="github" (see github_owner_repo).

    Optional args:
        source: "official" (default) or "github".
        github_owner_repo: str, required when source="github" — "owner/repo".
    """
    args = args or {}
    source = (args.get("source") or "official").strip().lower()
    query = (args.get("query") or "").strip().lower()

    if source == "github":
        github_owner_repo = args.get("github_owner_repo")
        if not github_owner_repo:
            raise RuntimeError(
                _("Falta o argumento 'github_owner_repo' para source='github'.")
            )
        package = _fetch_github_metadata(github_owner_repo)
        versions = package.get("versions") or []
        latest = versions[0] if versions else {}
        lines = [
            _("Pacote encontrado em github.com/{repo}:").format(
                repo=github_owner_repo
            ),
            _("  Nome: {name}").format(name=package.get("name", "?")),
            _("  Identificador: {identifier}").format(
                identifier=package.get("identifier", "?")
            ),
            _("  Descrição: {description}").format(
                description=package.get("description", "?")
            ),
            _("  Licença: {license}").format(license=package.get("license", "?")),
            _("  Última versão: {version} (KiCad >= {kicad_version})").format(
                version=latest.get("version", "?"),
                kicad_version=latest.get("kicad_version", "?"),
            ),
            _(
                "  AVISO: este pacote NÃO passa pelo catálogo oficial do "
                "KiCad — o hash de integridade é auto-declarado pelo "
                "próprio autor do repositório, não verificado de forma "
                "independente."
            ),
        ]
        return "\n".join(lines)

    if source != "official":
        raise RuntimeError(
            _("Argumento 'source' inválido: '{source}' (use 'official' ou 'github').").format(
                source=source
            )
        )

    packages = _load_official_packages()

    def _matches(pkg: dict) -> bool:
        if not query:
            return True
        haystack = " ".join(
            [
                str(pkg.get("name", "")),
                str(pkg.get("description", "")),
                str(pkg.get("identifier", "")),
                " ".join(pkg.get("tags") or []),
            ]
        ).lower()
        return query in haystack

    matches = [pkg for pkg in packages if _matches(pkg)]
    truncated = len(matches) > _MAX_SEARCH_RESULTS
    shown = matches[:_MAX_SEARCH_RESULTS]

    if not shown:
        return _(
            "Nenhum pacote encontrado no catálogo oficial do KiCad para '{query}'."
        ).format(query=query)

    lines = [
        _("{n} pacote(s) encontrado(s) no catálogo oficial do KiCad para '{query}':").format(
            n=len(matches), query=query
        ),
        "",
    ]
    for pkg in shown:
        latest = (pkg.get("versions") or [{}])[0]
        lines.append(
            "- {name} ({identifier}) [{type}] — {description}\n"
            "    {_v}: {version} | {_lic}: {license}".format(
                name=pkg.get("name", "?"),
                identifier=pkg.get("identifier", "?"),
                type=pkg.get("type", "?"),
                description=pkg.get("description", "?"),
                _v=_("versão"),
                version=latest.get("version", "?"),
                _lic=_("licença"),
                license=pkg.get("license", "?"),
            )
        )
    if truncated:
        lines.append("")
        lines.append(
            _("... (truncado a {n} resultados; refine a pesquisa)").format(
                n=_MAX_SEARCH_RESULTS
            )
        )
    return "\n".join(lines)


def install_kicad_plugin(args: dict) -> str:
    """Download and install a KiCad plugin/content package into this
    machine's 3rd-party plugins folder, from EITHER the official PCM
    catalog OR a specific GitHub repository.

    Exactly one of these two is required:
        identifier: str — an exact package identifier from the official
            catalog (as returned by search_kicad_plugins with
            source="official"), e.g.
            "com.github.pointhi.kicad-color-schemes.solarized-light".
        github_owner_repo: str — "owner/repo" of a GitHub repository with
            its own metadata.json at the repo root (as returned by
            search_kicad_plugins with source="github"). NOT vetted by
            KiCad's own catalog — see this module's docstring.

    Optional args:
        kicad_version: str — prefer a version entry matching this KiCad
            version string; defaults to the newest "stable" entry.

    NEVER makes KiCad load the plugin immediately — the user MUST restart
    KiCad afterward (KiCad only rescans plugins at startup). This tool
    refuses (RuntimeError) rather than overwrite an already-installed
    plugin at the same target folder.
    """
    args = args or {}
    identifier_arg = args.get("identifier")
    github_owner_repo = args.get("github_owner_repo")
    kicad_version = args.get("kicad_version")

    if bool(identifier_arg) == bool(github_owner_repo):
        raise RuntimeError(
            _(
                "Indique exatamente um de 'identifier' (catálogo oficial) "
                "ou 'github_owner_repo' (instalação direta do GitHub), "
                "nunca os dois nem nenhum."
            )
        )

    is_github = bool(github_owner_repo)

    if is_github:
        package = _fetch_github_metadata(github_owner_repo)
        source_label = _("GitHub ({repo}, NÃO verificado pelo catálogo oficial)").format(
            repo=github_owner_repo
        )
    else:
        packages = _load_official_packages()
        package = next(
            (p for p in packages if p.get("identifier") == identifier_arg), None
        )
        if package is None:
            raise RuntimeError(
                _(
                    "Identificador '{identifier}' não encontrado no catálogo "
                    "oficial — use search_kicad_plugins primeiro para confirmar "
                    "o identificador exato."
                ).format(identifier=identifier_arg)
            )
        source_label = _("catálogo oficial do KiCad")

    identifier = package.get("identifier")
    if not identifier:
        raise RuntimeError(_("O pacote não tem um 'identifier' válido."))

    version_entry = _pick_version(package, kicad_version)
    download_url = version_entry.get("download_url")
    expected_sha256 = version_entry.get("download_sha256")
    if not download_url:
        raise RuntimeError(
            _("A versão escolhida de '{identifier}' não tem download_url.").format(
                identifier=identifier
            )
        )

    target_dir_name = identifier.replace(".", "_")
    kicad_dir = _newest_kicad_documents_dir()
    target_dir = kicad_dir / "3rdparty" / "plugins" / target_dir_name
    if target_dir.exists():
        raise RuntimeError(
            _(
                "Já existe algo instalado em {path} — desinstale primeiro "
                "(Gestor de Conteúdo e Plug-ins do KiCad) antes de reinstalar."
            ).format(path=target_dir)
        )

    raw_zip = _http_get(download_url)

    if expected_sha256:
        actual_sha256 = sha256(raw_zip).hexdigest()
        if actual_sha256.lower() != str(expected_sha256).lower():
            raise RuntimeError(
                _(
                    "Verificação de integridade falhou para '{identifier}': "
                    "SHA256 esperado {expected}, obtido {actual}. A "
                    "transferência pode estar corrompida ou adulterada — a "
                    "instalação foi cancelada."
                ).format(
                    identifier=identifier,
                    expected=expected_sha256,
                    actual=actual_sha256,
                )
            )
    elif not is_github:
        # Every official-catalog version entry observed has a download_sha256
        # — its absence here is unexpected enough to refuse rather than
        # silently install unverified content from what should be a vetted
        # source.
        raise RuntimeError(
            _(
                "A versão escolhida de '{identifier}' não declara um "
                "download_sha256 — recusando instalar sem verificação de "
                "integridade."
            ).format(identifier=identifier)
        )

    staging_dir = Path(tempfile.mkdtemp(prefix="kicad_pcm_install_"))
    try:
        zip_path = staging_dir / "package.zip"
        zip_path.write_bytes(raw_zip)
        extract_dir = staging_dir / "extracted"
        extract_dir.mkdir()
        _safe_extract(zip_path, extract_dir)

        package_root = _find_package_root(extract_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(package_root, target_dir)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    return _(
        "Pacote '{name}' ({identifier}) instalado a partir de {source} em:\n"
        "{path}\n\n"
        "REINICIE o KiCad para o plugin ficar disponível — os plugins só são "
        "detetados no arranque."
    ).format(
        name=package.get("name", identifier),
        identifier=identifier,
        source=source_label,
        path=target_dir,
    )


def register_kicad_pcm_tools(registry: ActionRegistry) -> None:
    """Register the plugin search/install tools on the given ActionRegistry.

    Both tools work purely over HTTP + the local filesystem — neither
    depends on any sibling plugin being installed, unlike every other
    ``register_*_tools`` in this package.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="search_kicad_plugins",
                description=(
                    "Call this when the user wants to find a KiCad plugin "
                    "for some capability (e.g. 'is there a plugin for X?'). "
                    "Searches KiCad's own official Plugin and Content "
                    "Manager (PCM) catalog by default, or a specific GitHub "
                    "repository if the user names one directly (pass "
                    "source='github' and github_owner_repo). Read-only — "
                    "never installs anything."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Case-insensitive search text (matched "
                                "against name/description/identifier/tags "
                                "for the official catalog; ignored for "
                                "source='github')."
                            ),
                        },
                        "source": {
                            "type": "string",
                            "description": "'official' (default) or 'github'.",
                        },
                        "github_owner_repo": {
                            "type": "string",
                            "description": (
                                "'owner/repo' — required when source='github'."
                            ),
                        },
                    },
                    "required": ["query"],
                },
            ),
            handler=search_kicad_plugins,
            read_only=True,
        )
    )

    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="install_kicad_plugin",
                description=(
                    "Call this ONLY after the user explicitly confirms they "
                    "want a SPECIFIC plugin installed (found via "
                    "search_kicad_plugins first). Downloads, verifies the "
                    "SHA256 integrity hash, and installs the plugin's files "
                    "under this machine's 3rd-party KiCad plugins folder. "
                    "Pass EXACTLY ONE of 'identifier' (official catalog) or "
                    "'github_owner_repo' (direct GitHub install — NOT "
                    "vetted by KiCad's own catalog, warn the user of this "
                    "clearly). NEVER loads the plugin immediately — the "
                    "user must restart KiCad afterward. This MODIFIES the "
                    "filesystem and requires explicit user approval; make "
                    "sure the user has clearly agreed to installing THIS "
                    "specific plugin before calling it, given it runs "
                    "arbitrary third-party code inside KiCad once restarted."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "identifier": {
                            "type": "string",
                            "description": (
                                "Exact package identifier from the official "
                                "catalog, e.g. "
                                "'com.github.author.plugin-name'."
                            ),
                        },
                        "github_owner_repo": {
                            "type": "string",
                            "description": (
                                "'owner/repo' for a direct GitHub install, "
                                "not vetted by KiCad's own catalog."
                            ),
                        },
                        "kicad_version": {
                            "type": "string",
                            "description": (
                                "Prefer a version matching this KiCad "
                                "version string; defaults to the newest "
                                "stable version."
                            ),
                        },
                    },
                    "required": [],
                },
            ),
            handler=install_kicad_plugin,
            read_only=False,
        )
    )
