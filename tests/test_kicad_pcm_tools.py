"""Tests for actions/kicad_pcm_tools.py.

Run WITHOUT real network access: `_http_get` is monkeypatched to return
canned JSON/bytes. Filesystem operations (cache, install target) use
pytest's tmp_path, never the real KiCad install.
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile

import pytest


def test_module_imports_cleanly():
    sys.modules.pop("actions.kicad_pcm_tools", None)
    import actions.kicad_pcm_tools as kpt  # noqa: F401

    assert hasattr(kpt, "register_kicad_pcm_tools")
    assert hasattr(kpt, "search_kicad_plugins")
    assert hasattr(kpt, "install_kicad_plugin")


@pytest.fixture
def kpt(monkeypatch, tmp_path):
    sys.modules.pop("actions.kicad_pcm_tools", None)
    import actions.kicad_pcm_tools as module

    # Never touch the real OS temp cache file across test runs.
    monkeypatch.setattr(module, "_CACHE_PATH", tmp_path / "pcm_cache.json")
    return module


_SAMPLE_PACKAGE = {
    "name": "Example Plugin",
    "identifier": "com.github.someone.example-plugin",
    "description": "Does an example thing",
    "license": "MIT",
    "type": "plugin",
    "tags": ["example", "demo"],
    "versions": [
        {
            "version": "1.0.0",
            "status": "stable",
            "kicad_version": "9.0",
            "download_url": "https://example.com/example-plugin-1.0.0.zip",
            "download_sha256": None,  # filled in per-test with a real hash
            "download_size": 123,
            "install_size": 456,
        }
    ],
}


def _packages_json_bytes(packages):
    return json.dumps({"packages": packages}).encode("utf-8")


def _make_zip_bytes(files: dict) -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# search_kicad_plugins — official catalog
# --------------------------------------------------------------------------- #
def test_search_official_matches_query(kpt, monkeypatch):
    monkeypatch.setattr(
        kpt, "_http_get", lambda url: _packages_json_bytes([_SAMPLE_PACKAGE])
    )

    result = kpt.search_kicad_plugins({"query": "example"})
    assert "Example Plugin" in result
    assert "com.github.someone.example-plugin" in result


def test_search_official_no_match(kpt, monkeypatch):
    monkeypatch.setattr(
        kpt, "_http_get", lambda url: _packages_json_bytes([_SAMPLE_PACKAGE])
    )

    result = kpt.search_kicad_plugins({"query": "totally-unrelated-xyz"})
    assert "Nenhum pacote encontrado" in result


def test_search_official_empty_query_returns_everything(kpt, monkeypatch):
    monkeypatch.setattr(
        kpt, "_http_get", lambda url: _packages_json_bytes([_SAMPLE_PACKAGE])
    )

    result = kpt.search_kicad_plugins({"query": ""})
    assert "Example Plugin" in result


def test_search_uses_cache_on_second_call(kpt, monkeypatch):
    calls = {"count": 0}

    def fake_http_get(url):
        calls["count"] += 1
        return _packages_json_bytes([_SAMPLE_PACKAGE])

    monkeypatch.setattr(kpt, "_http_get", fake_http_get)

    kpt.search_kicad_plugins({"query": "example"})
    kpt.search_kicad_plugins({"query": "example"})

    assert calls["count"] == 1


def test_search_invalid_source(kpt):
    with pytest.raises(RuntimeError):
        kpt.search_kicad_plugins({"query": "x", "source": "not-a-real-source"})


# --------------------------------------------------------------------------- #
# search_kicad_plugins — github
# --------------------------------------------------------------------------- #
def test_search_github_missing_owner_repo(kpt):
    with pytest.raises(RuntimeError):
        kpt.search_kicad_plugins({"query": "x", "source": "github"})


def test_search_github_happy_path(kpt, monkeypatch):
    def fake_http_get(url):
        assert "raw.githubusercontent.com" in url
        return json.dumps(_SAMPLE_PACKAGE).encode("utf-8")

    monkeypatch.setattr(kpt, "_http_get", fake_http_get)

    result = kpt.search_kicad_plugins(
        {"query": "", "source": "github", "github_owner_repo": "someone/example-plugin"}
    )
    assert "Example Plugin" in result
    assert "NÃO passa pelo catálogo oficial" in result


def test_search_github_invalid_owner_repo_format(kpt, monkeypatch):
    monkeypatch.setattr(kpt, "_http_get", lambda url: b"{}")
    with pytest.raises(RuntimeError):
        kpt.search_kicad_plugins(
            {"query": "", "source": "github", "github_owner_repo": "not-a-repo-path"}
        )


# --------------------------------------------------------------------------- #
# install_kicad_plugin — validation
# --------------------------------------------------------------------------- #
def test_install_requires_exactly_one_source(kpt):
    with pytest.raises(RuntimeError):
        kpt.install_kicad_plugin({})
    with pytest.raises(RuntimeError):
        kpt.install_kicad_plugin(
            {"identifier": "x", "github_owner_repo": "owner/repo"}
        )


def test_install_unknown_identifier(kpt, monkeypatch):
    monkeypatch.setattr(kpt, "_http_get", lambda url: _packages_json_bytes([]))
    with pytest.raises(RuntimeError):
        kpt.install_kicad_plugin({"identifier": "com.github.nobody.nothing"})


# --------------------------------------------------------------------------- #
# install_kicad_plugin — full happy path (official + github)
# --------------------------------------------------------------------------- #
def test_install_official_happy_path(kpt, monkeypatch, tmp_path):
    zip_bytes = _make_zip_bytes(
        {
            "metadata.json": "{}",
            "plugins/__init__.py": "X().register()",
            "plugins/action.py": "print('hi')",
        }
    )
    digest = hashlib.sha256(zip_bytes).hexdigest()

    package = dict(_SAMPLE_PACKAGE)
    package["versions"] = [dict(_SAMPLE_PACKAGE["versions"][0], download_sha256=digest)]

    def fake_http_get(url):
        if url == kpt._OFFICIAL_PACKAGES_URL:
            return _packages_json_bytes([package])
        assert url == package["versions"][0]["download_url"]
        return zip_bytes

    monkeypatch.setattr(kpt, "_http_get", fake_http_get)

    kicad_dir = tmp_path / "KiCad" / "9.0"
    kicad_dir.mkdir(parents=True)
    monkeypatch.setattr(kpt, "_newest_kicad_documents_dir", lambda: kicad_dir)

    result = kpt.install_kicad_plugin({"identifier": package["identifier"]})

    target = kicad_dir / "3rdparty" / "plugins" / "com_github_someone_example-plugin"
    assert target.is_dir()
    assert (target / "action.py").is_file()
    assert "REINICIE" in result


def test_install_sha256_mismatch_raises(kpt, monkeypatch, tmp_path):
    zip_bytes = _make_zip_bytes({"plugins/action.py": "x"})

    package = dict(_SAMPLE_PACKAGE)
    package["versions"] = [
        dict(_SAMPLE_PACKAGE["versions"][0], download_sha256="0" * 64)
    ]

    def fake_http_get(url):
        if url == kpt._OFFICIAL_PACKAGES_URL:
            return _packages_json_bytes([package])
        return zip_bytes

    monkeypatch.setattr(kpt, "_http_get", fake_http_get)
    kicad_dir = tmp_path / "KiCad" / "9.0"
    kicad_dir.mkdir(parents=True)
    monkeypatch.setattr(kpt, "_newest_kicad_documents_dir", lambda: kicad_dir)

    with pytest.raises(RuntimeError):
        kpt.install_kicad_plugin({"identifier": package["identifier"]})


def test_install_missing_sha256_refuses_official(kpt, monkeypatch, tmp_path):
    package = dict(_SAMPLE_PACKAGE)
    package["versions"] = [
        dict(_SAMPLE_PACKAGE["versions"][0], download_sha256=None)
    ]

    def fake_http_get(url):
        return _packages_json_bytes([package])

    monkeypatch.setattr(kpt, "_http_get", fake_http_get)
    kicad_dir = tmp_path / "KiCad" / "9.0"
    kicad_dir.mkdir(parents=True)
    monkeypatch.setattr(kpt, "_newest_kicad_documents_dir", lambda: kicad_dir)

    with pytest.raises(RuntimeError):
        kpt.install_kicad_plugin({"identifier": package["identifier"]})


def test_install_refuses_when_already_installed(kpt, monkeypatch, tmp_path):
    package = dict(_SAMPLE_PACKAGE)
    package["versions"] = [
        dict(_SAMPLE_PACKAGE["versions"][0], download_sha256="a" * 64)
    ]
    monkeypatch.setattr(
        kpt, "_http_get", lambda url: _packages_json_bytes([package])
    )

    kicad_dir = tmp_path / "KiCad" / "9.0"
    existing = kicad_dir / "3rdparty" / "plugins" / "com_github_someone_example-plugin"
    existing.mkdir(parents=True)
    monkeypatch.setattr(kpt, "_newest_kicad_documents_dir", lambda: kicad_dir)

    with pytest.raises(RuntimeError):
        kpt.install_kicad_plugin({"identifier": package["identifier"]})


def test_install_github_source_no_sha_check_required(kpt, monkeypatch, tmp_path):
    zip_bytes = _make_zip_bytes({"plugins/action.py": "x"})

    package = dict(_SAMPLE_PACKAGE)
    package["versions"] = [
        dict(_SAMPLE_PACKAGE["versions"][0], download_sha256=None)
    ]

    def fake_http_get(url):
        if "raw.githubusercontent.com" in url:
            return json.dumps(package).encode("utf-8")
        return zip_bytes

    monkeypatch.setattr(kpt, "_http_get", fake_http_get)
    kicad_dir = tmp_path / "KiCad" / "9.0"
    kicad_dir.mkdir(parents=True)
    monkeypatch.setattr(kpt, "_newest_kicad_documents_dir", lambda: kicad_dir)

    result = kpt.install_kicad_plugin({"github_owner_repo": "someone/example-plugin"})
    assert "REINICIE" in result


# --------------------------------------------------------------------------- #
# _safe_extract — zip-slip protection
# --------------------------------------------------------------------------- #
def test_safe_extract_rejects_path_traversal(kpt, tmp_path):
    import io
    import zipfile as zf_module

    zip_path = tmp_path / "evil.zip"
    buf = io.BytesIO()
    with zf_module.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.py", "malicious")
    zip_path.write_bytes(buf.getvalue())

    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises(RuntimeError):
        kpt._safe_extract(zip_path, dest)


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_register_kicad_pcm_tools(kpt):
    from actions.framework import ActionRegistry

    registry = ActionRegistry()
    kpt.register_kicad_pcm_tools(registry)

    search = registry.get("search_kicad_plugins")
    assert search is not None
    assert search.read_only is True

    install = registry.get("install_kicad_plugin")
    assert install is not None
    assert install.read_only is False
