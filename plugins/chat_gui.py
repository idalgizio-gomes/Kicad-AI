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

try:  # pragma: no cover - import shim
    from . import i18n as _i18n
    from .i18n import SUPPORTED_LANGUAGES, current_language, setup_i18n
except ImportError:  # pragma: no cover - import shim
    import i18n as _i18n  # type: ignore
    from i18n import SUPPORTED_LANGUAGES, current_language, setup_i18n  # type: ignore


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
        self._messages = [_make_message("system", system_prompt)]

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

    # ------------------------------------------------------------------ UI ---
    def _build_ui(self):
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

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
        top.Add(self._model_input, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

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

        # Status + cumulative session cost, side by side.
        status_row = wx.BoxSizer(wx.HORIZONTAL)
        self._status = wx.StaticText(panel, label=_("Pronto."))
        status_row.Add(self._status, 1, wx.ALIGN_CENTER_VERTICAL)
        self._cost_label = wx.StaticText(panel, label="")
        status_row.Add(self._cost_label, 0, wx.ALIGN_CENTER_VERTICAL)
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
        self._append_line(_("Você:") + " " + text)
        self._messages.append(_make_message("user", text))

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
        """Render a ChatMessage produced by the tool loop into the history."""
        role = getattr(msg, "role", "")
        content = getattr(msg, "content", "") or ""
        tool_calls = getattr(msg, "tool_calls", None) or []

        if role == "assistant":
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
