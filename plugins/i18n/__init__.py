"""
gettext wrapper — mesma convenção do EMC-EMI Analyzer (ver skill
kicad-plugin-dev): catalogos `locale/<lang>/LC_MESSAGES/<domain>.mo` dentro
de plugins/, lidos diretamente em runtime (sem passo de build no caminho de
instalacao do KiCad). `_()` e reatribuido em runtime por setup_i18n() (troca
de idioma ao vivo).

Portugues (pt) e a lingua-fonte de TODAS as strings deste plugin desde o
inicio — a maioria do backend (llm_providers/*.py, actions/framework.py) ja
estava em portugues antes de existir qualquer infraestrutura de i18n, e
chat_gui.py/chat_action.py foram traduzidos para portugues especificamente
para alinhar com essa convencao. Por isso "en" NAO entra no encadeamento de
fallback como lingua intermedia (ao contrario do LibForge, que herdou
strings em ingles do fork original e teve de corrigir isto a posteriori) —
"pt" e a unica lingua garantida a existir sempre (msgid == texto pt). Regra
critica: TODAS as mensagens visiveis ao utilizador — nao so labels estaticos
da GUI, tambem erros/logs gerados dinamicamente pelos providers/framework —
tem de chamar `_()` no momento da construcao, nunca pre-traduzidas.
"""

from __future__ import annotations

import gettext
import locale as _locale_module
from pathlib import Path

LOCALE_DIR = Path(__file__).resolve().parent.parent / "locale"
DOMAIN = "kicad_chat_assistant"

SUPPORTED_LANGUAGES = ["en", "pt", "es", "fr", "de", "it", "nl", "pl", "gl", "ca", "zh"]
DEFAULT_LANGUAGE = "pt"

_current_language = DEFAULT_LANGUAGE


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    """Placeholder before setup_i18n() runs — identity function. Overwritten
    (module-level name rebound) by setup_i18n() below."""
    return message


def detect_system_language() -> str:
    try:
        lang_code, _encoding = _locale_module.getlocale()
    except Exception:
        return DEFAULT_LANGUAGE

    if not lang_code:
        try:
            lang_code, _encoding = _locale_module.getdefaultlocale()  # type: ignore[attr-defined]
        except Exception:
            return DEFAULT_LANGUAGE

    if not lang_code:
        return DEFAULT_LANGUAGE

    short = lang_code.split("_")[0].split("-")[0].lower()
    return short if short in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def setup_i18n(lang: str | None = None) -> str:
    """(Re)bind the module-level `_()` to a gettext translation for `lang`.

    Safe to call again later to switch language at runtime — every NEW call
    to `_()` after this returns picks up the new language immediately.

    Fallback: any language other than "pt" falls back to the raw msgid
    (which IS Portuguese, the source language) via `fallback=True` once no
    .mo is compiled yet for that language — this is the plain, un-chained
    fallback (unlike LibForge, which needed a "pt" intermediate link because
    its msgids were English but new strings were authored in Portuguese;
    here msgid already equals the source language for every string).
    """
    global _, _current_language

    if lang is None:
        lang = detect_system_language()
    if lang not in SUPPORTED_LANGUAGES:
        lang = DEFAULT_LANGUAGE

    translation = gettext.translation(
        DOMAIN, str(LOCALE_DIR), languages=[lang], fallback=True
    )
    _ = translation.gettext
    _current_language = lang
    return lang


def current_language() -> str:
    return _current_language
