"""
wxPython chat panel for the KiCad Chat Assistant.

This module is deliberately decoupled: it imports ``wx`` (it only runs inside
an environment that has wxPython, i.e. KiCad) but it does NOT import ``pcbnew``
and it does NOT import the ``llm_providers`` or ``actions`` packages at module
top level. Everything it needs — the provider factory, the tool registry, the
agentic ``run_tool_loop`` function and the system prompt — is injected through
the constructor. That keeps this file testable/reviewable in isolation and
makes ``chat_action.py`` the single place that wires the pieces together.

The only exception is ``ChatMessage``: the dialog has to build the initial
``system`` message and each ``user`` message before handing the list to
``run_tool_loop``. It is imported lazily (relative first, absolute fallback)
inside a small helper so the module still imports without the provider package
on the path.

i18n: every user-facing string in this file goes through the module-level
``_()`` defined below. It is a small trampoline into ``i18n._`` looked up
FRESH on every call — see its docstring for why a plain
``from .i18n import _`` would silently break live language switching.
"""

from __future__ import annotations

import json
import threading

import wx
import wx.adv

try:  # pragma: no cover - import shim
    from . import i18n as _i18n
    from .i18n import SUPPORTED_LANGUAGES, current_language, setup_i18n
except ImportError:  # pragma: no cover - import shim
    import i18n as _i18n  # type: ignore
    from i18n import SUPPORTED_LANGUAGES, current_language, setup_i18n  # type: ignore

# conversation_store, like i18n, is pure stdlib (no wx/pcbnew) — safe to
# import at module scope, same precedent as the i18n import above.
try:  # pragma: no cover - import shim
    from . import conversation_store
except ImportError:  # pragma: no cover - import shim
    import conversation_store  # type: ignore


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    """Translate ``message`` using whatever language is CURRENTLY active.

    Deliberately NOT ``from .i18n import _``: that form copies whatever
    object ``i18n._`` happens to reference at import time into THIS
    module's namespace, and Python's ``from x import y`` never re-reads the
    source module afterwards. When ``setup_i18n()`` later rebinds
    ``i18n._`` (from this module, from ``chat_action.py``, or from anywhere
    else), a snapshot import here would keep calling the stale (usually
    identity) function forever — the exact "dynamic message stays in the
    old language after switching" bug the i18n skill guide calls out.
    Looking up ``_i18n._`` fresh on every call instead always sees the
    latest binding.
    """
    return _i18n._(message)


# Native display names for the language picker — NOT translated (a language's
# own name is conventionally shown in that language, e.g. "Deutsch" even in
# an English UI).
_LANGUAGE_NAMES = {
    "en": "English",
    "pt": "Português",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "it": "Italiano",
    "nl": "Nederlands",
    "pl": "Polski",
    "gl": "Galego",
    "ca": "Català",
    "zh": "中文",
}

# Visual truncation limit for long tool results shown in the history control.
_TOOL_RESULT_PREVIEW = 500


def _make_message(role, content, tool_calls=None, tool_call_id=None):
    """Build a ChatMessage, importing the dataclass lazily so this module does
    not depend on llm_providers at import time (keeps the GUI decoupled)."""
    try:
        from .llm_providers.base import ChatMessage  # type: ignore
    except Exception:  # pragma: no cover - fallback path for test/standalone use
        from llm_providers.base import ChatMessage  # type: ignore
    return ChatMessage(
        role=role,
        content=content,
        tool_calls=tool_calls or [],
        tool_call_id=tool_call_id,
    )


class ModelPickerDialog(wx.Dialog):
    """Real model selection window (not free text) — populated from
    ``provider.list_models()``. Opens even when that list is empty (missing
    key, network failure, no live query for this provider) and explains why
    instead of appearing broken, since a blank picker with no explanation
    would be worse than the free-text field it's meant to improve on."""

    def __init__(self, parent, models, current_model):
        super().__init__(
            parent,
            title=_("Escolher modelo"),
            size=(420, 360),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.selected_model = None
        self._listbox = None

        outer = wx.BoxSizer(wx.VERTICAL)
        if models:
            self._listbox = wx.ListBox(self, choices=models, style=wx.LB_SINGLE)
            try:
                self._listbox.SetSelection(models.index(current_model))
            except ValueError:
                pass
            self._listbox.Bind(wx.EVT_LISTBOX_DCLICK, self._on_ok)
            outer.Add(self._listbox, 1, wx.EXPAND | wx.ALL, 8)
        else:
            msg = wx.StaticText(
                self,
                label=_(
                    "Não foi possível obter a lista de modelos deste "
                    "fornecedor (verifique a chave API ou a ligação). Pode "
                    "escrever o nome do modelo diretamente no campo "
                    "'Modelo'."
                ),
            )
            msg.Wrap(380)
            outer.Add(msg, 1, wx.EXPAND | wx.ALL, 12)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        ok_btn = wx.Button(self, wx.ID_OK, label=_("Selecionar"))
        ok_btn.Bind(wx.EVT_BUTTON, self._on_ok)
        ok_btn.Enable(bool(models))
        cancel_btn = wx.Button(self, wx.ID_CANCEL, label=_("Cancelar"))
        btn_row.Add(ok_btn, 0, wx.RIGHT, 6)
        btn_row.Add(cancel_btn, 0)
        outer.Add(btn_row, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

        self.SetSizer(outer)

    def _on_ok(self, _event):
        if self._listbox is not None:
            idx = self._listbox.GetSelection()
            if idx != wx.NOT_FOUND:
                self.selected_model = self._listbox.GetString(idx)
        self.EndModal(wx.ID_OK)


class ConversationPickerDialog(wx.Dialog):
    """Lists saved conversations (conversation_store.list_conversations())
    for opening or deleting. "Nova conversa" is a distinct outcome from
    "Abrir" — the caller (ChatDialog) tells them apart via `self.action`."""

    def __init__(self, parent):
        super().__init__(
            parent,
            title=_("Conversas"),
            size=(480, 400),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.action = None  # "open" | "new" | None (cancelled)
        self.selected_id = None

        self._items = conversation_store.list_conversations()

        outer = wx.BoxSizer(wx.VERTICAL)
        self._listbox = wx.ListBox(
            self,
            choices=[self._format_item(it) for it in self._items],
            style=wx.LB_SINGLE,
        )
        if self._items:
            self._listbox.SetSelection(0)
        self._listbox.Bind(wx.EVT_LISTBOX_DCLICK, self._on_open)
        outer.Add(self._listbox, 1, wx.EXPAND | wx.ALL, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        new_btn = wx.Button(self, label=_("Nova conversa"))
        new_btn.Bind(wx.EVT_BUTTON, self._on_new)
        btn_row.Add(new_btn, 0, wx.RIGHT, 6)

        open_btn = wx.Button(self, label=_("Abrir"))
        open_btn.Bind(wx.EVT_BUTTON, self._on_open)
        open_btn.Enable(bool(self._items))
        btn_row.Add(open_btn, 0, wx.RIGHT, 6)

        delete_btn = wx.Button(self, label=_("Eliminar"))
        delete_btn.Bind(wx.EVT_BUTTON, self._on_delete)
        delete_btn.Enable(bool(self._items))
        btn_row.Add(delete_btn, 0, wx.RIGHT, 6)

        self._open_btn = open_btn
        self._delete_btn = delete_btn

        cancel_btn = wx.Button(self, wx.ID_CANCEL, label=_("Fechar"))
        btn_row.Add(cancel_btn, 0)
        outer.Add(btn_row, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

        self.SetSizer(outer)

    @staticmethod
    def _format_item(item):
        import time as _time

        try:
            when = _time.strftime(
                "%Y-%m-%d %H:%M", _time.localtime(item.get("updated_at") or 0)
            )
        except Exception:
            when = ""
        title = item.get("title") or item.get("id") or ""
        return f"{title}   ({when})" if when else title

    def _selected_item(self):
        idx = self._listbox.GetSelection()
        if idx == wx.NOT_FOUND or idx >= len(self._items):
            return None
        return self._items[idx]

    def _on_open(self, _event):
        item = self._selected_item()
        if item is None:
            return
        self.action = "open"
        self.selected_id = item["id"]
        self.EndModal(wx.ID_OK)

    def _on_new(self, _event):
        self.action = "new"
        self.EndModal(wx.ID_OK)

    def _on_delete(self, _event):
        item = self._selected_item()
        if item is None:
            return
        confirm = wx.MessageDialog(
            self,
            _("Eliminar a conversa '{title}'? Esta ação não pode ser desfeita.").format(
                title=item.get("title") or item.get("id")
            ),
            _("Eliminar conversa?"),
            wx.YES_NO | wx.ICON_WARNING | wx.NO_DEFAULT,
        )
        if confirm.ShowModal() == wx.ID_YES:
            conversation_store.delete_conversation(item["id"])
            self._items = conversation_store.list_conversations()
            self._listbox.Set([self._format_item(it) for it in self._items])
            has_items = bool(self._items)
            self._open_btn.Enable(has_items)
            self._delete_btn.Enable(has_items)
        confirm.Destroy()


# Repo URL and author credit, shown in the "Sobre / Suporte" dialog — kept
# as module-level constants (not hardcoded inline in the dialog class) so
# they're easy to find/update in one place if the repo ever moves.
_SUPPORT_REPO_URL = "https://github.com/idalgizio-gomes/Kicad-AI"
_SUPPORT_AUTHOR = "Idalgízio Gomes"


class AboutDialog(wx.Dialog):
    """"Sobre / Suporte" — credits the author and points to the GitHub repo
    for bug reports / support, so the plugin is never anonymous to someone
    who runs into a problem with it."""

    def __init__(self, parent):
        super().__init__(
            parent,
            title=_("Sobre / Suporte"),
            size=(420, 260),
            style=wx.DEFAULT_DIALOG_STYLE,
        )

        outer = wx.BoxSizer(wx.VERTICAL)

        title_text = wx.StaticText(self, label="KiCad Chat Assistant")
        font = title_text.GetFont()
        font.MakeBold()
        font.SetPointSize(font.GetPointSize() + 2)
        title_text.SetFont(font)
        outer.Add(title_text, 0, wx.ALL, 12)

        author_text = wx.StaticText(
            self,
            label=_("Criado e mantido por {author}.").format(author=_SUPPORT_AUTHOR),
        )
        outer.Add(author_text, 0, wx.LEFT | wx.RIGHT, 12)

        support_text = wx.StaticText(
            self,
            label=_(
                "Para questões, sugestões ou reportar problemas, "
                "usa o repositório no GitHub:"
            ),
        )
        support_text.Wrap(380)
        outer.Add(support_text, 0, wx.ALL, 12)

        link = wx.adv.HyperlinkCtrl(self, wx.ID_ANY, _SUPPORT_REPO_URL, _SUPPORT_REPO_URL)
        outer.Add(link, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        close_btn = wx.Button(self, wx.ID_OK, label=_("Fechar"))
        outer.Add(close_btn, 0, wx.ALIGN_RIGHT | wx.ALL, 12)

        self.SetSizer(outer)


class ChatDialog(wx.Dialog):
    """Non-blocking chat dialog. All LLM/tool work runs on a worker thread and
    every UI mutation is marshalled back to the main thread via wx.CallAfter."""

    # Fractions of the configured limit at which a one-time warning fires.
    # 1.0 (and anything beyond) still fires exactly once — see
    # _check_cost_alerts, not repeated on every subsequent turn.
    _COST_ALERT_THRESHOLDS = (0.5, 0.8, 1.0)

    def __init__(
        self,
        parent,
        provider_factory,
        provider_ids,
        provider_labels,
        registry,
        run_tool_loop,
        system_prompt,
        cost_alert_limit_usd=None,
    ):
        super().__init__(
            parent,
            # "KiCad Chat Assistant" is the product name — deliberately not
            # wrapped in _(), a proper noun is not translated.
            title="KiCad Chat Assistant",
            size=(760, 560),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )

        self._provider_factory = provider_factory
        self._provider_ids = list(provider_ids)
        # Raw (pt-source) labels, e.g. {"claude": "Claude (Anthropic - API
        # paga)"} — llm_providers.PROVIDER_LABELS wraps each value in _() at
        # MODULE IMPORT time (before setup_i18n() typically runs), so what
        # lands here is effectively the untranslated pt msgid text. That is
        # exactly what we want: re-feeding it through the live _() below at
        # render time (see _retranslate_static_labels) translates it fresh
        # for whatever language is active then.
        self._provider_labels = dict(provider_labels)
        self._registry = registry
        self._run_tool_loop = run_tool_loop

        # Conversation state: always starts with the injected system prompt.
        self._system_prompt = system_prompt
        self._messages = [_make_message("system", system_prompt)]
        # A fresh id per dialog "session" — reassigned by _start_new_conversation
        # / _open_conversation, never reused across two DIFFERENT conversations
        # sharing one dialog instance (switching conversations mid-dialog is
        # supported, see the "Conversas" picker).
        self._conversation_id = conversation_store.new_conversation_id()

        # Current provider id + instance (created lazily / on switch).
        self._provider_id = self._provider_ids[0] if self._provider_ids else None
        self._provider = None
        self._busy = False

        # Cumulative cost-equivalent tracking for this chat session (see
        # _recompute_session_cost). None disables the limit/alerts entirely
        # but the running total is still shown whenever any provider reports
        # a cost (currently only ClaudeCodeCLIProvider). Not persisted across
        # dialog instances — a fresh chat window starts a fresh count.
        self._cost_alert_limit_usd = cost_alert_limit_usd
        self._session_cost_usd = 0.0
        self._cost_alert_hit = set()

        self._build_ui()
        self._create_provider(self._provider_id)
        self.Bind(wx.EVT_CLOSE, self._on_close)

    # ------------------------------------------------------------------ UI ---
    def _build_ui(self):
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        # Conversations row: new / open-or-manage saved conversations.
        conv_row = wx.BoxSizer(wx.HORIZONTAL)
        new_conv_btn = wx.Button(panel, label=_("Nova conversa"))
        new_conv_btn.Bind(wx.EVT_BUTTON, self._on_new_conversation)
        conv_row.Add(new_conv_btn, 0, wx.RIGHT, 6)
        conversations_btn = wx.Button(panel, label=_("Conversas..."))
        conversations_btn.Bind(wx.EVT_BUTTON, self._on_open_conversations)
        conv_row.Add(conversations_btn, 0)
        conv_row.AddStretchSpacer(1)
        about_btn = wx.Button(panel, label=_("Sobre / Suporte"))
        about_btn.Bind(wx.EVT_BUTTON, self._on_show_about)
        conv_row.Add(about_btn, 0)
        self._new_conv_btn = new_conv_btn
        self._conversations_btn = conversations_btn
        self._about_btn = about_btn
        outer.Add(conv_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # Top row: provider chooser, model override, language picker.
        top = wx.BoxSizer(wx.HORIZONTAL)
        self._provider_label = wx.StaticText(panel, label=_("Provedor:"))
        top.Add(
            self._provider_label,
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            6,
        )
        self._provider_choice = wx.Choice(
            panel,
            choices=[_(self._provider_labels.get(pid, pid)) for pid in self._provider_ids],
        )
        if self._provider_ids:
            self._provider_choice.SetSelection(0)
        self._provider_choice.Bind(wx.EVT_CHOICE, self._on_provider_change)
        top.Add(self._provider_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        # Model override — generic across every provider (each one already
        # reads its own self.model). Empty means "provider's own default".
        # Applied on Enter so a typo doesn't recreate the provider on every
        # keystroke.
        self._model_label = wx.StaticText(panel, label=_("Modelo:"))
        top.Add(
            self._model_label,
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            6,
        )
        self._model_input = wx.TextCtrl(
            panel, style=wx.TE_PROCESS_ENTER, size=(160, -1)
        )
        self._model_input.SetHint(_("(padrão do fornecedor)"))
        self._model_input.Bind(wx.EVT_TEXT_ENTER, self._on_model_change)
        top.Add(self._model_input, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)

        # Opens a real selection window listing this provider's actual
        # models (list_models()) instead of relying on free text alone —
        # the free-text field above stays available for power users /
        # providers where no live list is available.
        self._model_picker_btn = wx.Button(panel, label="...", size=(28, -1))
        self._model_picker_btn.Bind(wx.EVT_BUTTON, self._on_open_model_picker)
        top.Add(self._model_picker_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        # Language picker — see _on_language_change / _retranslate_static_labels
        # for the "re-render live" pattern (static wx widgets never
        # auto-update; each needs an explicit .SetLabel()/.SetHint() call
        # after switching).
        self._lang_label = wx.StaticText(panel, label=_("Idioma:"))
        top.Add(self._lang_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._lang_choice = wx.Choice(
            panel,
            choices=[_LANGUAGE_NAMES.get(code, code) for code in SUPPORTED_LANGUAGES],
        )
        try:
            self._lang_choice.SetSelection(SUPPORTED_LANGUAGES.index(current_language()))
        except ValueError:
            self._lang_choice.SetSelection(0)
        self._lang_choice.Bind(wx.EVT_CHOICE, self._on_language_change)
        top.Add(self._lang_choice, 0, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(top, 0, wx.EXPAND | wx.ALL, 8)

        # History (read-only, rich so we can visually distinguish speakers).
        self._history = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2 | wx.TE_AUTO_URL,
        )
        outer.Add(self._history, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # Input row: text entry + send button.
        bottom = wx.BoxSizer(wx.HORIZONTAL)
        self._input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self._input.Bind(wx.EVT_TEXT_ENTER, self._on_send)
        bottom.Add(self._input, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._send_btn = wx.Button(panel, label=_("Enviar"))
        self._send_btn.Bind(wx.EVT_BUTTON, self._on_send)
        bottom.Add(self._send_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(bottom, 0, wx.EXPAND | wx.ALL, 8)

        # Status + cumulative session cost + a way to actually set the limit
        # (previously only configurable by hand-editing config.json).
        status_row = wx.BoxSizer(wx.HORIZONTAL)
        self._status = wx.StaticText(panel, label=_("Pronto."))
        status_row.Add(self._status, 1, wx.ALIGN_CENTER_VERTICAL)
        self._cost_label = wx.StaticText(panel, label="")
        status_row.Add(self._cost_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._cost_limit_btn = wx.Button(panel, label=_("Limite..."), size=(-1, -1))
        self._cost_limit_btn.Bind(wx.EVT_BUTTON, self._on_set_cost_limit)
        status_row.Add(self._cost_limit_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(status_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        panel.SetSizer(outer)
        self._input.SetFocus()
        self._update_cost_label()

    # --------------------------------------------------------------- i18n ---
    def _on_language_change(self, _event):
        idx = self._lang_choice.GetSelection()
        if idx == wx.NOT_FOUND or idx >= len(SUPPORTED_LANGUAGES):
            return
        lang = SUPPORTED_LANGUAGES[idx]
        if lang == current_language():
            return
        setup_i18n(lang)
        self._retranslate_static_labels()

    def _retranslate_static_labels(self):
        """Re-render every STATIC widget label after a live language switch.

        ``_()`` is looked up fresh on every call (see the module-level
        trampoline above), so every NEW message generated from now on
        already comes out in the new language automatically. But wx widgets
        never "auto-update" just because the active translation changed —
        each one needs an explicit .SetLabel()/.SetHint() call with the
        freshly-translated text, done here. History content already printed
        BEFORE the switch is intentionally left in the old language — an
        accepted, documented limitation (see the i18n skill guide).
        """
        self._provider_label.SetLabel(_("Provedor:"))
        self._model_label.SetLabel(_("Modelo:"))
        self._lang_label.SetLabel(_("Idioma:"))
        self._send_btn.SetLabel(_("Enviar"))
        self._model_input.SetHint(_("(padrão do fornecedor)"))
        self._new_conv_btn.SetLabel(_("Nova conversa"))
        self._conversations_btn.SetLabel(_("Conversas..."))
        self._cost_limit_btn.SetLabel(_("Limite..."))
        self._about_btn.SetLabel(_("Sobre / Suporte"))

        # Provider choice list: rebuild from the raw pt-source labels so
        # each entry re-translates instead of staying frozen at whatever
        # language was active when the dialog was constructed.
        selection = self._provider_choice.GetSelection()
        self._provider_choice.Set(
            [_(self._provider_labels.get(pid, pid)) for pid in self._provider_ids]
        )
        if selection != wx.NOT_FOUND:
            self._provider_choice.SetSelection(selection)

        # Don't stomp a genuinely busy status ("A pensar...") mid-turn — the
        # next turn will render fresh text anyway. Idle state is the normal
        # time to switch, so re-render it explicitly.
        if not self._busy:
            self._set_status(_("Pronto."))
        self._update_cost_label()

        self.Layout()

    # -------------------------------------------------------- provider mgmt ---
    def _create_provider(self, provider_id, model=None):
        """Instantiate the provider for ``provider_id`` via the injected
        factory. Any failure is reported in the history, never raised."""
        if not provider_id:
            return
        try:
            self._provider = self._provider_factory(provider_id, model)
            self._provider_id = provider_id
        except Exception as exc:  # ProviderError or anything else
            self._provider = None
            self._append_error(
                _("Não foi possível inicializar o fornecedor '{name}': {err}").format(
                    name=_(self._provider_labels.get(provider_id, provider_id)),
                    err=exc,
                )
            )

    def _on_provider_change(self, _event):
        idx = self._provider_choice.GetSelection()
        if idx == wx.NOT_FOUND or idx >= len(self._provider_ids):
            return
        provider_id = self._provider_ids[idx]
        if provider_id == self._provider_id and self._provider is not None:
            return
        # Model namespaces don't overlap between providers (e.g. "sonnet"
        # means nothing to Gemini) — clear the field on switch rather than
        # silently carrying over a value that would just error out.
        self._model_input.SetValue("")
        # Switching keeps the existing message history.
        self._create_provider(provider_id)
        if self._provider is not None:
            self._set_status(
                _("Mudou para {name}.").format(
                    name=_(self._provider_labels.get(provider_id, provider_id))
                )
            )

    def _on_model_change(self, _event):
        """Recreate the CURRENT provider with the typed model override.
        Empty text reverts to the provider's own default."""
        if self._provider_id is None:
            return
        model = self._model_input.GetValue().strip() or None
        self._create_provider(self._provider_id, model)
        if self._provider is not None:
            self._set_status(
                _("Modelo definido como {model}.").format(
                    model=model or _("(padrão do fornecedor)")
                )
            )

    def _on_open_model_picker(self, _event):
        """Opens a real selection window (ModelPickerDialog) listing this
        provider's actual models — addresses free text potentially not
        matching any real model. list_models() may do a live network call;
        a busy cursor covers that brief wait rather than threading it (this
        is a one-off user-initiated action, not the main send() path that
        genuinely must stay async)."""
        if self._provider is None:
            self._append_error(_("Nenhum fornecedor está configurado."))
            return
        with wx.BusyCursor():
            try:
                models = self._provider.list_models()
            except Exception:
                models = []
        dlg = ModelPickerDialog(self, models, self._model_input.GetValue().strip())
        if dlg.ShowModal() == wx.ID_OK and dlg.selected_model:
            self._model_input.SetValue(dlg.selected_model)
            self._on_model_change(None)
        dlg.Destroy()

    # ------------------------------------------------------------ cost limit ---
    def _on_set_cost_limit(self, _event):
        """Lets the user actually SET the session cost-alert limit from the
        GUI — previously only configurable by hand-editing config.json's
        cost_alert_limit_usd key before the dialog was even opened."""
        current = f"{self._cost_alert_limit_usd:.2f}" if self._cost_alert_limit_usd else ""
        dlg = wx.TextEntryDialog(
            self,
            _(
                "Define o limite de custo desta sessão em USD "
                "(vazio = sem limite/alertas):"
            ),
            _("Limite de custo"),
            current,
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            text = dlg.GetValue().strip()
        finally:
            dlg.Destroy()

        if not text:
            self._cost_alert_limit_usd = None
        else:
            try:
                value = float(text.replace(",", "."))
            except ValueError:
                self._append_error(
                    _("Valor de limite inválido: '{value}'.").format(value=text)
                )
                return
            self._cost_alert_limit_usd = value if value > 0 else None

        # A new limit means the old thresholds (50%/80%/100% of the OLD
        # limit) no longer mean anything - re-arm all of them so the new
        # limit's own thresholds can fire fresh.
        self._cost_alert_hit = set()
        self._update_cost_label()
        self._check_cost_alerts()

    # -------------------------------------------------------- conversations ---
    def _save_current_conversation(self):
        """Best-effort persistence — never lets a disk/permission error
        interrupt the chat itself. Skips saving a conversation that never
        got a real user message (nothing worth keeping from an untouched
        dialog, and it would otherwise litter the picker with empty
        entries every time the chat window is opened and closed)."""
        if not any(m.role == "user" for m in self._messages):
            return
        try:
            conversation_store.save_conversation(self._conversation_id, self._messages)
        except Exception:
            pass

    def _reset_conversation_state(self, conversation_id, messages):
        """Shared by _on_new_conversation / _on_open_conversations: swap in
        a different conversation's state and fully re-render the history
        control from scratch (there is no incremental "diff" path for a
        wholesale conversation swap, unlike a normal turn)."""
        self._conversation_id = conversation_id
        self._messages = list(messages)
        self._history.Clear()
        for msg in self._messages:
            if msg.role == "system":
                continue
            self._render_message(msg)
        self._recompute_session_cost()
        self._cost_alert_hit = set()
        self._set_status(_("Pronto."))

    def _on_new_conversation(self, _event):
        self._save_current_conversation()
        self._reset_conversation_state(
            conversation_store.new_conversation_id(),
            [_make_message("system", self._system_prompt)],
        )
        self._append_line(_("[info] Nova conversa iniciada."))

    def _on_open_conversations(self, _event):
        self._save_current_conversation()
        dlg = ConversationPickerDialog(self)
        try:
            if dlg.ShowModal() != wx.ID_OK or dlg.action is None:
                return
            if dlg.action == "new":
                self._reset_conversation_state(
                    conversation_store.new_conversation_id(),
                    [_make_message("system", self._system_prompt)],
                )
                self._append_line(_("[info] Nova conversa iniciada."))
            elif dlg.action == "open" and dlg.selected_id:
                try:
                    _title, messages = conversation_store.load_conversation(dlg.selected_id)
                except Exception as exc:
                    self._append_error(
                        _("Não foi possível abrir a conversa: {err}").format(err=exc)
                    )
                    return
                if not any(m.role == "system" for m in messages):
                    messages = [_make_message("system", self._system_prompt)] + messages
                self._reset_conversation_state(dlg.selected_id, messages)
                self._append_line(_("[info] Conversa aberta."))
        finally:
            dlg.Destroy()

    def _on_close(self, event):
        self._save_current_conversation()
        event.Skip()  # let the normal close behaviour (Destroy) proceed

    def _on_show_about(self, _event):
        dlg = AboutDialog(self)
        dlg.ShowModal()
        dlg.Destroy()

    # --------------------------------------------------------------- events ---
    def _on_send(self, _event):
        if self._busy:
            return
        text = self._input.GetValue().strip()
        if not text:
            return
        if self._provider is None:
            self._append_error(
                _("Nenhum fornecedor está configurado. Verifique a sua API key / instalação.")
            )
            return

        self._input.SetValue("")
        user_msg = _make_message("user", text)
        self._render_message(user_msg)
        self._messages.append(user_msg)

        self._set_busy(True)
        self._set_status(_("A pensar..."))

        # Captured BEFORE the worker thread runs, on the main thread, while
        # self._messages still holds only what's rendered so far. Must NOT
        # be recomputed inside _finish_turn: run_tool_loop mutates this same
        # list object in place (it receives self._messages by reference, not
        # a copy), so by the time _finish_turn executes — asynchronously, via
        # wx.CallAfter, after the worker has already finished — self._messages
        # already equals updated_messages. Recomputing len(self._messages)
        # there always yields the post-mutation length, making the "new
        # messages" slice empty on every turn regardless of provider or
        # content — the bug that made every successful reply render nothing.
        pre_turn_len = len(self._messages)

        worker = threading.Thread(
            target=self._run_worker, args=(pre_turn_len,), daemon=True
        )
        worker.start()

    def _run_worker(self, pre_turn_len):
        """Runs on a background thread. Never touches wx directly except via
        CallAfter."""
        try:
            updated = self._run_tool_loop(
                self._provider,
                self._registry,
                self._messages,
                self._ask_approval,
                self._on_loop_update,
                8,
            )
            wx.CallAfter(self._finish_turn, updated, pre_turn_len)
        except Exception as exc:  # ProviderError or any unexpected failure
            wx.CallAfter(self._append_error, str(exc))
            wx.CallAfter(self._set_busy, False)
            wx.CallAfter(self._set_status, _("Erro."))

    def _finish_turn(self, updated_messages, pre_turn_len):
        """Main-thread: reconcile the message list and render new assistant /
        tool output produced during the turn."""
        # Render only the messages appended by the tool loop (assistant/tool)
        # — i.e. everything from the length captured before the turn started.
        old_len = pre_turn_len
        self._messages = updated_messages
        for msg in updated_messages[old_len:]:
            self._render_message(msg)
        self._recompute_session_cost()
        self._set_busy(False)
        self._set_status(_("Pronto."))
        # Auto-save after every completed turn, not just on explicit
        # new/open/close — a KiCad crash or an unclean close must not lose
        # a conversation that already has real content.
        self._save_current_conversation()

    def _recompute_session_cost(self):
        """Sum cost_usd across every message's meta, not an incremental
        running total — re-summing the whole (short-lived, per-dialog)
        conversation is cheap and immune to double-counting bugs if this is
        ever called more than once for the same turn."""
        total = 0.0
        for msg in self._messages:
            meta = getattr(msg, "meta", None) or {}
            cost = meta.get("cost_usd")
            if isinstance(cost, (int, float)):
                total += cost
        self._session_cost_usd = total
        self._update_cost_label()
        self._check_cost_alerts()

    def _update_cost_label(self):
        if self._session_cost_usd <= 0 and self._cost_alert_limit_usd is None:
            self._cost_label.SetLabel("")
            return
        if self._cost_alert_limit_usd:
            text = _("Custo da sessão: ${spent} / ${limit}").format(
                spent=f"{self._session_cost_usd:.4f}",
                limit=f"{self._cost_alert_limit_usd:.2f}",
            )
        else:
            text = _("Custo da sessão: ${spent}").format(
                spent=f"{self._session_cost_usd:.4f}"
            )
        self._cost_label.SetLabel("   " + text)

    def _check_cost_alerts(self):
        """Fire each threshold at most once per dialog instance (session),
        even if the total keeps climbing past it on later turns."""
        limit = self._cost_alert_limit_usd
        if not limit:
            return
        ratio = self._session_cost_usd / limit
        for threshold in self._COST_ALERT_THRESHOLDS:
            if ratio >= threshold and threshold not in self._cost_alert_hit:
                self._cost_alert_hit.add(threshold)
                pct = int(threshold * 100)
                self._append_line(
                    _(
                        "[aviso de custo] {pct}% do limite da sessão atingido "
                        "(${spent} / ${limit})."
                    ).format(
                        pct=pct,
                        spent=f"{self._session_cost_usd:.4f}",
                        limit=f"{limit:.2f}",
                    )
                )

    def _on_loop_update(self, text):
        """on_update callback passed to run_tool_loop; called from the worker
        thread. Used only for transient status text."""
        if text:
            wx.CallAfter(self._set_status, str(text))

    # ---------------------------------------------------- approval gateway ---
    def _ask_approval(self, tool_call, defn):
        """MANDATORY approval gate. Called from the worker thread. Blocks that
        thread until the user answers a modal Yes/No dialog shown on the main
        thread. Returns True only on an explicit 'Yes'."""
        result = {"ok": False}
        done = threading.Event()

        def prompt():
            try:
                description = ""
                spec = getattr(defn, "spec", None)
                if spec is not None:
                    description = getattr(spec, "description", "") or ""
                try:
                    args = json.dumps(
                        tool_call.arguments, indent=2, ensure_ascii=False
                    )
                except Exception:
                    args = str(getattr(tool_call, "arguments", ""))

                # Write actions (read_only=False) get a visibly stronger
                # warning — different title, different icon, and an explicit
                # "isto MODIFICA a placa" line up front — so the user can
                # never mistake a mutation for a read-only query at a glance,
                # even if they're skimming instead of reading every word.
                is_write = getattr(defn, "read_only", True) is False
                if is_write:
                    title = _("⚠ Aprovar ALTERAÇÃO à placa?")
                    icon = wx.ICON_WARNING
                    warning_line = _(
                        "ATENÇÃO: esta ação MODIFICA a placa aberta.\n\n"
                    )
                else:
                    title = _("Aprovar ação?")
                    icon = wx.ICON_QUESTION
                    warning_line = ""

                message = _(
                    "{warning}O assistente quer executar a ação:\n\n"
                    "  {name}\n\n"
                    "{description}\n\n"
                    "Argumentos:\n{args}\n\n"
                    "Permitir esta ação?"
                ).format(
                    warning=warning_line,
                    name=tool_call.name,
                    description=description,
                    args=args,
                )
                dlg = wx.MessageDialog(
                    self,
                    message,
                    title,
                    wx.YES_NO | icon | wx.NO_DEFAULT,
                )
                result["ok"] = dlg.ShowModal() == wx.ID_YES
                dlg.Destroy()
            finally:
                done.set()

        wx.CallAfter(prompt)
        done.wait()
        return result["ok"]

    # ------------------------------------------------------------- history ---
    def _render_message(self, msg):
        """Render a ChatMessage into the history — the ONE place that does
        this, for EVERY role, so a loaded/reopened conversation (which
        replays "user" messages too, unlike a live turn where run_tool_loop
        only ever appends assistant/tool messages) renders identically to a
        live one. Originally "Você: ..." was appended inline in _on_send
        instead of going through here, which meant _reset_conversation_state
        silently dropped every user message when reopening a saved
        conversation — this consolidation is the fix, not just a refactor."""
        role = getattr(msg, "role", "")
        content = getattr(msg, "content", "") or ""
        tool_calls = getattr(msg, "tool_calls", None) or []

        if role == "user":
            if content.strip():
                self._append_line(_("Você:") + " " + content.strip())
        elif role == "assistant":
            if content.strip():
                self._append_line(_("Assistente:") + " " + content.strip())
            elif not tool_calls:
                # Never render nothing at all for a completed turn — an
                # empty reply with no tool calls is a real (if rare)
                # provider outcome, not a bug in the GUI, and staying
                # silent here previously made it indistinguishable from
                # the turn never having happened.
                self._append_line(_("[aviso] Resposta vazia do fornecedor."))
            for tc in tool_calls:
                defn = self._registry.get(tc.name)
                is_write = defn is not None and getattr(defn, "read_only", True) is False
                if is_write:
                    self._append_line(
                        _("[ação] ⚠ a propor ALTERAÇÃO: {name}").format(name=tc.name)
                    )
                else:
                    self._append_line(
                        _("[ação] a propor: {name}").format(name=tc.name)
                    )
            meta = getattr(msg, "meta", None) or {}
            cost_usd = meta.get("cost_usd")
            if isinstance(cost_usd, (int, float)):
                self._append_line(
                    _("[custo] esta chamada: ${amount}").format(amount=f"{cost_usd:.4f}")
                )
        elif role == "tool":
            preview = content.strip()
            if len(preview) > _TOOL_RESULT_PREVIEW:
                preview = preview[:_TOOL_RESULT_PREVIEW] + _(" ...[truncado]")
            self._append_line(_("[ação] resultado") + " -> " + preview)

    def _append_line(self, text):
        self._history.AppendText(text + "\n")

    def _append_error(self, text):
        self._append_line(_("[erro]") + " " + text)

    def _set_status(self, text):
        self._status.SetLabel(text)

    def _set_busy(self, busy):
        self._busy = busy
        self._send_btn.Enable(not busy)
        self._input.Enable(not busy)
        self._provider_choice.Enable(not busy)
