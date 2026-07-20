"""Tests for attachments.py — file classification for chat attachments.

Runs without KiCad/wx: pure stdlib + i18n (also pure stdlib). conftest.py
puts plugins/ on sys.path.
"""

import base64

import attachments as att


def test_image_classified_and_base64_encoded(tmp_path):
    path = tmp_path / "photo.png"
    raw = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    path.write_bytes(raw)

    result = att.classify_attachment(str(path))

    assert result.kind == "image"
    assert result.name == "photo.png"
    assert result.media_type == "image/png"
    assert base64.b64decode(result.data_b64) == raw


def test_jpeg_extension_variants_both_map_to_image_jpeg(tmp_path):
    for suffix in (".jpg", ".jpeg"):
        path = tmp_path / f"photo{suffix}"
        path.write_bytes(b"fake-jpeg")
        result = att.classify_attachment(str(path))
        assert result.kind == "image"
        assert result.media_type == "image/jpeg"


def test_oversized_image_is_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(att, "MAX_IMAGE_BYTES", 10)
    path = tmp_path / "big.png"
    path.write_bytes(b"x" * 100)
    result = att.classify_attachment(str(path))
    assert result.kind == "unsupported"
    assert "grande" in result.reason.lower()


def test_pdf_classified_and_base64_encoded(tmp_path):
    path = tmp_path / "datasheet.pdf"
    raw = b"%PDF-1.4 fake pdf bytes"
    path.write_bytes(raw)

    result = att.classify_attachment(str(path))

    assert result.kind == "pdf"
    assert result.media_type == "application/pdf"
    assert base64.b64decode(result.data_b64) == raw


def test_oversized_pdf_is_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(att, "MAX_PDF_BYTES", 10)
    path = tmp_path / "big.pdf"
    path.write_bytes(b"x" * 100)
    result = att.classify_attachment(str(path))
    assert result.kind == "unsupported"


def test_text_file_classified_with_content(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("Olá, mundo!\nSegunda linha.", encoding="utf-8")

    result = att.classify_attachment(str(path))

    assert result.kind == "text"
    assert result.text == "Olá, mundo!\nSegunda linha."
    assert result.truncated is False


def test_unusual_extension_with_real_text_content_still_classified_as_text(tmp_path):
    path = tmp_path / "board.kicad_pcb"
    path.write_text('(kicad_pcb (version 20241229) (generator "pcbnew"))', encoding="utf-8")
    result = att.classify_attachment(str(path))
    assert result.kind == "text"
    assert "kicad_pcb" in result.text


def test_long_text_file_is_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(att, "MAX_TEXT_CHARS", 10)
    path = tmp_path / "long.txt"
    path.write_text("x" * 100, encoding="utf-8")
    result = att.classify_attachment(str(path))
    assert result.kind == "text"
    assert result.truncated is True
    assert len(result.text) == 10


def test_latin1_text_file_still_classified_as_text(tmp_path):
    path = tmp_path / "legacy.txt"
    # A byte sequence invalid as UTF-8 but valid latin-1.
    path.write_bytes(b"caf\xe9")
    result = att.classify_attachment(str(path))
    assert result.kind == "text"
    assert result.text == "café"


def test_binary_file_with_null_byte_is_unsupported(tmp_path):
    # bytes(range(256)) includes \x00 - the NUL-byte sniff must catch this
    # BEFORE any decode is attempted, since latin-1 would otherwise decode
    # every single byte value without error and silently garble real binary
    # content (executables, archives, images with an unrecognized
    # extension) into fake "text" sent to the model.
    path = tmp_path / "archive.zip"
    path.write_bytes(bytes(range(256)))
    result = att.classify_attachment(str(path))
    assert result.kind == "unsupported"
    assert result.text is None


def test_binary_without_null_byte_but_invalid_utf8_falls_back_to_latin1(tmp_path):
    # No NUL byte present -> passes the binary sniff -> UTF-8 fails (0xFF is
    # never valid UTF-8 on its own) -> latin-1 rescues it. This is the
    # legitimate use of the latin-1 fallback: a real legacy-encoded text
    # file, not a binary blob.
    path = tmp_path / "legacy_no_null.txt"
    path.write_bytes(b"caf\xe9 sem null bytes")
    result = att.classify_attachment(str(path))
    assert result.kind == "text"
    assert result.text == "café sem null bytes"


def test_missing_file_is_error(tmp_path):
    result = att.classify_attachment(str(tmp_path / "does-not-exist.png"))
    assert result.kind == "error"
    assert "não encontrado" in result.reason.lower() or "encontrado" in result.reason.lower()


def test_directory_path_is_error(tmp_path):
    result = att.classify_attachment(str(tmp_path))
    assert result.kind == "error"


# --------------------------------------------------------------------------- #
# caching (avoids re-reading/re-encoding the same attachment on every turn
# of a multi-turn conversation, since providers replay full history)
# --------------------------------------------------------------------------- #
def test_repeated_classify_uses_cache(tmp_path, monkeypatch):
    path = tmp_path / "notes.txt"
    path.write_text("conteudo original", encoding="utf-8")

    calls = []
    orig = att._classify_attachment_uncached

    def _spy(p):
        calls.append(p)
        return orig(p)

    monkeypatch.setattr(att, "_classify_attachment_uncached", _spy)

    first = att.classify_attachment(str(path))
    second = att.classify_attachment(str(path))

    assert first.text == "conteudo original"
    assert second.text == "conteudo original"
    assert len(calls) == 1  # second call served from cache, no re-read


def test_cache_invalidates_when_file_mtime_changes(tmp_path, monkeypatch):
    path = tmp_path / "notes.txt"
    path.write_text("versao 1", encoding="utf-8")

    first = att.classify_attachment(str(path))
    assert first.text == "versao 1"

    # Force a distinct mtime (some filesystems have coarse mtime
    # resolution - bump it explicitly rather than relying on real time
    # passing between the two writes).
    import os
    import time

    path.write_text("versao 2", encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))

    second = att.classify_attachment(str(path))
    assert second.text == "versao 2"


def test_error_results_are_never_cached(tmp_path):
    missing = tmp_path / "nope.txt"
    first = att.classify_attachment(str(missing))
    assert first.kind == "error"
    # Create the file after the first (missing) lookup - the error result
    # must not have been cached under a key that would shadow this.
    missing.write_text("agora existe", encoding="utf-8")
    second = att.classify_attachment(str(missing))
    assert second.kind == "text"
    assert second.text == "agora existe"
