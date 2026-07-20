#!/usr/bin/env python3
"""
Compile locale/<lang>/LC_MESSAGES/kicad_chat_assistant.po -> .mo for every
language that has at least one non-empty, non-fuzzy translation.

Deliberately SKIPS languages with zero real translations: gettext's
fallback=True (see plugins/i18n/__init__.py) already returns the original
(Portuguese) string when no .mo file exists at all for a language, which is
exactly the desired "falls back to the source language until translated"
behaviour. Shipping a .mo full of empty msgstr entries would instead make
every message show up blank for that language — worse than not compiling it.

Usage: python tools/i18n_compile.py
"""

from __future__ import annotations

from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALE_DIR = REPO_ROOT / "plugins" / "locale"  # must live inside plugins/ - that's what ships to KiCad
DOMAIN = "kicad_chat_assistant"


def _has_real_translations(catalog) -> bool:
    return any(message.string and not message.fuzzy for message in catalog if message.id)


def main() -> None:
    compiled = []
    skipped = []

    for po_path in sorted(LOCALE_DIR.glob(f"*/LC_MESSAGES/{DOMAIN}.po")):
        lang = po_path.parent.parent.name
        with open(po_path, "rb") as f:
            catalog = read_po(f, locale=lang, domain=DOMAIN)

        if not _has_real_translations(catalog):
            skipped.append(lang)
            continue

        mo_path = po_path.with_suffix(".mo")
        with open(mo_path, "wb") as f:
            write_mo(f, catalog)
        compiled.append(lang)

    print(f"Compiled .mo for: {', '.join(compiled) if compiled else '(none)'}")
    print(f"Skipped (no real translations yet, falls back to pt): {', '.join(skipped) if skipped else '(none)'}")


if __name__ == "__main__":
    main()
