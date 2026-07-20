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
"""

from __future__ import annotations

import gettext
import json
import threading

import wx

_ = gettext.gettext

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

    def __init__(
        self,
        parent,
        provider_factory,
        provider_ids,
        provider_labels,
        registry,
        run_tool_loop,
        system_prompt,
    ):
        super().__init__(
            parent,
            title=_("KiCad Chat Assistant"),
            size=(720, 560),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )

        self._provider_factory = provider_factory
        self._provider_ids = list(provider_ids)
        self._provider_labels = dict(provider_labels)
        self._registry = registry
        self._run_tool_loop = run_tool_loop

        # Conversation state: always starts with the injected system prompt.
        self._messages = [_make_message("system", system_prompt)]

        # Current provider id + instance (created lazily / on switch).
        self._provider_id = self._provider_ids[0] if self._provider_ids else None
        self._provider = None
        self._busy = False

        self._build_ui()
        self._create_provider(self._provider_id)

    # ------------------------------------------------------------------ UI ---
    def _build_ui(self):
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        # Top row: provider chooser.
        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(
            wx.StaticText(panel, label=_("Provider:")),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            6,
        )
        self._provider_choice = wx.Choice(
            panel,
            choices=[self._provider_labels.get(pid, pid) for pid in self._provider_ids],
        )
        if self._provider_ids:
            self._provider_choice.SetSelection(0)
        self._provider_choice.Bind(wx.EVT_CHOICE, self._on_provider_change)
        top.Add(self._provider_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        # Model override — generic across every provider (each one already
        # reads its own self.model). Empty means "provider's own default".
        # Applied on Enter so a typo doesn't recreate the provider on every
        # keystroke.
        top.Add(
            wx.StaticText(panel, label=_("Model:")),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            6,
        )
        self._model_input = wx.TextCtrl(
            panel, style=wx.TE_PROCESS_ENTER, size=(160, -1)
        )
        self._model_input.SetHint(_("(provider default)"))
        self._model_input.Bind(wx.EVT_TEXT_ENTER, self._on_model_change)
        top.Add(self._model_input, 0, wx.ALIGN_CENTER_VERTICAL)
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
        self._send_btn = wx.Button(panel, label=_("Send"))
        self._send_btn.Bind(wx.EVT_BUTTON, self._on_send)
        bottom.Add(self._send_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(bottom, 0, wx.EXPAND | wx.ALL, 8)

        # Status line.
        self._status = wx.StaticText(panel, label=_("Ready."))
        outer.Add(self._status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        panel.SetSizer(outer)
        self._input.SetFocus()

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
                _("Could not initialise provider '{name}': {err}").format(
                    name=self._provider_labels.get(provider_id, provider_id),
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
                _("Switched to {name}.").format(
                    name=self._provider_labels.get(provider_id, provider_id)
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
                _("Model set to {model}.").format(
                    model=model or _("(provider default)")
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
                _("No provider is configured. Check your API key / installation.")
            )
            return

        self._input.SetValue("")
        self._append_line(_("You:") + " " + text)
        self._messages.append(_make_message("user", text))

        self._set_busy(True)
        self._set_status(_("Thinking..."))

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
            wx.CallAfter(self._set_status, _("Error."))

    def _finish_turn(self, updated_messages, pre_turn_len):
        """Main-thread: reconcile the message list and render new assistant /
        tool output produced during the turn."""
        # Render only the messages appended by the tool loop (assistant/tool)
        # — i.e. everything from the length captured before the turn started.
        old_len = pre_turn_len
        self._messages = updated_messages
        for msg in updated_messages[old_len:]:
            self._render_message(msg)
        self._set_busy(False)
        self._set_status(_("Ready."))

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
                message = _(
                    "The assistant wants to run the action:\n\n"
                    "  {name}\n\n"
                    "{description}\n\n"
                    "Arguments:\n{args}\n\n"
                    "Allow this action?"
                ).format(
                    name=tool_call.name,
                    description=description,
                    args=args,
                )
                dlg = wx.MessageDialog(
                    self,
                    message,
                    _("Approve action?"),
                    wx.YES_NO | wx.ICON_QUESTION | wx.NO_DEFAULT,
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
                self._append_line(_("Assistant:") + " " + content.strip())
            elif not tool_calls:
                # Never render nothing at all for a completed turn — an
                # empty reply with no tool calls is a real (if rare)
                # provider outcome, not a bug in the GUI, and staying
                # silent here previously made it indistinguishable from
                # the turn never having happened.
                self._append_line(_("[warning] Empty response from provider."))
            for tc in tool_calls:
                self._append_line(
                    _("[action] proposing: {name}").format(name=tc.name)
                )
            meta = getattr(msg, "meta", None) or {}
            cost_usd = meta.get("cost_usd")
            if isinstance(cost_usd, (int, float)):
                self._append_line(
                    _("[cost] this call: ${amount}").format(amount=f"{cost_usd:.4f}")
                )
        elif role == "tool":
            preview = content.strip()
            if len(preview) > _TOOL_RESULT_PREVIEW:
                preview = preview[:_TOOL_RESULT_PREVIEW] + _(" ...[truncated]")
            self._append_line(_("[action] result") + " -> " + preview)

    def _append_line(self, text):
        self._history.AppendText(text + "\n")

    def _append_error(self, text):
        self._append_line(_("[error]") + " " + text)

    def _set_status(self, text):
        self._status.SetLabel(text)

    def _set_busy(self, busy):
        self._busy = busy
        self._send_btn.Enable(not busy)
        self._input.Enable(not busy)
        self._provider_choice.Enable(not busy)
