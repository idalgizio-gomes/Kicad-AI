#!/usr/bin/env python3
"""
Extract every _()-wrapped string across plugins/ into
locale/kicad_chat_assistant.pot, then update (or create) each supported
language's .po file from it.

Wraps Babel (pure Python, no GNU gettext-tools needed on Windows) — run this
after adding/removing any _() call site, before i18n_compile.py. Portugues
(pt) e a lingua-fonte deste plugin: o .po de "pt" gerado aqui fica com
msgstr == msgid para cada entrada (traducao identidade), correto porque o
texto-fonte JA esta em portugues — nao e um placeholder por preencher como
seria nos outros idiomas.

Usage: python tools/i18n_extract.py
"""

from __future__ import annotations

import copy
from pathlib import Path

from babel.messages.catalog import Catalog
from babel.messages.extract import extract_from_dir
from babel.messages.pofile import read_po, write_po

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / "plugins"
LOCALE_DIR = PLUGINS_DIR / "locale"  # must live inside plugins/ - that's what ships to KiCad
DOMAIN = "kicad_chat_assistant"

SUPPORTED_LANGUAGES = ["en", "pt", "es", "fr", "de", "it", "nl", "pl", "gl", "ca", "zh"]
SOURCE_LANGUAGE = "pt"


def _build_pot() -> Catalog:
    catalog = Catalog(project="KiCad Chat Assistant", domain=DOMAIN)
    for filename, lineno, message, comments, context in extract_from_dir(
        str(PLUGINS_DIR),
        method_map=[("**.py", "python")],
    ):
        catalog.add(message, None, [(str(filename), lineno)], auto_comments=comments)
    return catalog


def main() -> None:
    LOCALE_DIR.mkdir(parents=True, exist_ok=True)
    pot_catalog = _build_pot()

    pot_path = LOCALE_DIR / f"{DOMAIN}.pot"
    with open(pot_path, "wb") as f:
        write_po(f, pot_catalog, omit_header=False)
    print(f"Wrote {pot_path} ({len(list(pot_catalog))} messages)")

    for lang in SUPPORTED_LANGUAGES:
        po_dir = LOCALE_DIR / lang / "LC_MESSAGES"
        po_dir.mkdir(parents=True, exist_ok=True)
        po_path = po_dir / f"{DOMAIN}.po"

        # Catalog.update() reuses the SAME Message objects from its template
        # argument for any brand-new entry (no clone - see babel's
        # Catalog.update()/__setitem__ source), so mutating a message's
        # .string after update() would corrupt the shared pot_catalog and
        # leak into every language processed afterwards in this loop; a
        # fresh deepcopy per language keeps each catalog's messages private.
        template = copy.deepcopy(pot_catalog)

        if po_path.exists():
            with open(po_path, "rb") as f:
                existing = read_po(f, locale=lang, domain=DOMAIN)
            existing.update(template)
            catalog_to_write = existing
            action = "Updated"
        else:
            catalog_to_write = Catalog(locale=lang, project="KiCad Chat Assistant", domain=DOMAIN)
            catalog_to_write.update(template)
            action = "Created"

        if lang == SOURCE_LANGUAGE:
            # msgid ja esta em portugues - a traducao "correta" e identidade,
            # em vez de ficar em branco a espera de um tradutor humano.
            for message in catalog_to_write:
                if message.id and not message.string:
                    message.string = message.id
                    message.flags.discard("fuzzy")

        with open(po_path, "wb") as f:
            write_po(f, catalog_to_write, omit_header=False)
        print(f"{action} {po_path}")


if __name__ == "__main__":
    main()
