"""
File attachment classification and reading — shared by every LLM provider's
request-building code (each translates the classified result into its own
API's native shape) and by chat_gui.py (the "Anexar ficheiro" picker).

"Anexar qualquer tipo de ficheiro" is honoured honestly, not by faking
support: a file is classified by actually inspecting it (extension for
images/PDF, a real decode attempt for text), and each provider degrades
gracefully for kinds it can't natively embed — see each provider's
_build_request/_build_prompt. A genuinely unsupported binary (a .zip, a
compiled .exe, a 3D model) becomes a clear "could not include this file"
note in the conversation, never silently dropped and never sent as garbage
bytes pretending to be text.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

try:
    from . import i18n as _i18n
except ImportError:  # pragma: no cover - fallback for flat/test imports
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


# Conservative per-attachment caps — avoid blowing context/cost on a single
# huge file. Well under every provider's own hard limits (e.g. Claude's
# ~5MB/image, ~32MB/PDF via the API) so a request never fails purely on
# size after already having read/encoded the file.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 50_000  # roughly 12-15k tokens - generous for one file

_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@dataclass
class ClassifiedAttachment:
    kind: str  # "image" | "pdf" | "text" | "unsupported" | "error"
    name: str
    media_type: str | None = None
    data_b64: str | None = None  # populated for kind in ("image", "pdf")
    text: str | None = None  # populated for kind == "text"
    truncated: bool = False
    reason: str | None = None  # populated for kind in ("unsupported", "error")


# Keyed by (path, mtime) so a changed file on disk invalidates the cache
# entry instead of serving stale content — cheap correctness win over a
# plain path-only cache. Every provider's _build_request/_build_prompt
# replays the FULL conversation on every send() call (the APIs are
# stateless), which would otherwise re-read and re-base64-encode the same
# attachment from disk on every turn of a multi-turn conversation.
_classify_cache: dict[tuple[str, float], ClassifiedAttachment] = {}


def classify_attachment(path: str) -> ClassifiedAttachment:
    """Reads and classifies a single file. Never raises — any failure
    (missing file, permission error, decode error) comes back as a
    ClassifiedAttachment with kind="error"/"unsupported" and a translated
    `reason`, for the caller to render as a normal (non-crashing) message."""
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        mtime = None  # missing/inaccessible - fall through, let the real
        # logic below produce the proper "error" result; never cached
        # (nothing stable to key on), so a file that starts existing later
        # is picked up on the very next call instead of staying "missing".

    if mtime is not None:
        cached = _classify_cache.get((path, mtime))
        if cached is not None:
            return cached

    result = _classify_attachment_uncached(path)

    if mtime is not None and result.kind not in ("error",):
        _classify_cache[(path, mtime)] = result
    return result


def _classify_attachment_uncached(path: str) -> ClassifiedAttachment:
    p = Path(path)
    name = p.name

    try:
        if not p.is_file():
            return ClassifiedAttachment(
                kind="error", name=name, reason=_("Ficheiro não encontrado.")
            )

        suffix = p.suffix.lower()
        size = p.stat().st_size

        if suffix in _IMAGE_MEDIA_TYPES:
            if size > MAX_IMAGE_BYTES:
                return ClassifiedAttachment(
                    kind="unsupported",
                    name=name,
                    reason=_(
                        "Imagem demasiado grande ({size} KB, limite {limit} KB)."
                    ).format(size=size // 1024, limit=MAX_IMAGE_BYTES // 1024),
                )
            data = p.read_bytes()
            return ClassifiedAttachment(
                kind="image",
                name=name,
                media_type=_IMAGE_MEDIA_TYPES[suffix],
                data_b64=base64.b64encode(data).decode("ascii"),
            )

        if suffix == ".pdf":
            if size > MAX_PDF_BYTES:
                return ClassifiedAttachment(
                    kind="unsupported",
                    name=name,
                    reason=_(
                        "PDF demasiado grande ({size} KB, limite {limit} KB)."
                    ).format(size=size // 1024, limit=MAX_PDF_BYTES // 1024),
                )
            data = p.read_bytes()
            return ClassifiedAttachment(
                kind="pdf",
                name=name,
                media_type="application/pdf",
                data_b64=base64.b64encode(data).decode("ascii"),
            )

        # Anything else: try as text rather than maintaining an exhaustive
        # extension allowlist — covers source code, KiCad files (.kicad_pcb/
        # .kicad_sch are plain S-expression text), logs, CSV, markdown,
        # JSON, etc. in one path.
        #
        # IMPORTANT: latin-1 decodes EVERY byte sequence without error (it's
        # a single-byte encoding covering all 256 values) — trying it as a
        # blind fallback after a failed UTF-8 decode would happily "succeed"
        # on a genuinely binary file (a .zip, a compiled .exe, a 3D model)
        # and send its garbled raw bytes to the model as if it were text.
        # Sniffing for a NUL byte first is a cheap, reliable binary
        # detector — essentially no real text format contains one, and
        # essentially every real binary format does — so latin-1 is only
        # even attempted on content that has already passed that check.
        raw = p.read_bytes()
        if b"\x00" in raw[:8192]:
            return ClassifiedAttachment(
                kind="unsupported",
                name=name,
                reason=_(
                    "Ficheiro binário não suportado (não é imagem, PDF, nem "
                    "texto legível)."
                ),
            )

        text = None
        for encoding in ("utf-8", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue

        if text is not None:
            # Universal newlines, matching what Path.read_text() would have
            # done — reading raw bytes + decoding manually (needed for the
            # NUL-byte sniff above) bypasses that normalization, so a file
            # with Windows CRLF line endings would otherwise carry literal
            # "\r\n" into the model's input instead of "\n".
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            truncated = len(text) > MAX_TEXT_CHARS
            if truncated:
                text = text[:MAX_TEXT_CHARS]
            return ClassifiedAttachment(
                kind="text", name=name, text=text, truncated=truncated
            )

        return ClassifiedAttachment(
            kind="unsupported",
            name=name,
            reason=_(
                "Ficheiro binário não suportado (não é imagem, PDF, nem "
                "texto legível)."
            ),
        )
    except Exception as exc:
        return ClassifiedAttachment(kind="error", name=name, reason=str(exc))
