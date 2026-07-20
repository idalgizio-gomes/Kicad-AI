"""
Tool-calling framework with a MANDATORY approval gate.

This module is GUI-agnostic: it never imports ``pcbnew`` or ``wx``. The chat
GUI drives the agentic loop by injecting an ``approval_callback`` (which shows
the confirmation dialog) and an optional ``on_update`` progress callback.

Design rule (non-negotiable): every tool call is gated by
``approval_callback`` BEFORE the handler runs. Fail-closed — a missing or
falsey callback means the action is refused, never silently executed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

# base.py lives in the sibling package ``llm_providers``. Inside KiCad the
# plugin is imported as a package (relative import works); the pytest suite
# inserts ``plugins/`` on sys.path and imports ``llm_providers`` as a
# top-level package (absolute import works). Support both.
try:  # pragma: no cover - import shim
    from ..llm_providers.base import (
        ChatMessage,
        ChatResponse,
        LLMProvider,
        ToolCall,
        ToolSpec,
    )
except ImportError:  # pragma: no cover - import shim
    from llm_providers.base import (  # type: ignore[no-redef]
        ChatMessage,
        ChatResponse,
        LLMProvider,
        ToolCall,
        ToolSpec,
    )

# i18n: every string literal below is ALREADY Portuguese (this module
# predates the i18n infrastructure) — wrapping in _() must not change any
# wording, only make it translatable. See chat_gui.py's `_()` docstring for
# why this is a fresh-lookup trampoline rather than `from ..i18n import _`.
try:  # pragma: no cover - import shim
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - import shim
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


@dataclass
class ActionDefinition:
    """A tool the LLM may call: its public spec + the Python handler.

    ``read_only`` is True for every action in this version; the field exists so
    future write actions (that mutate the board) can be flagged and treated
    differently by the approval UI without changing this framework's contract.
    """

    spec: ToolSpec
    handler: Callable[[dict], str]
    read_only: bool = True


class ActionRegistry:
    """Holds the available :class:`ActionDefinition` objects by tool name."""

    def __init__(self) -> None:
        self._actions: dict[str, ActionDefinition] = {}

    def register(self, defn: ActionDefinition) -> None:
        name = defn.spec.name
        if name in self._actions:
            raise ValueError(
                _("Ação duplicada: '{name}' já está registada.").format(name=name)
            )
        self._actions[name] = defn

    def get(self, name: str) -> Optional[ActionDefinition]:
        return self._actions.get(name)

    def specs(self) -> list[ToolSpec]:
        """Tool specs to hand to a provider's ``send(messages, tools)``."""
        return [defn.spec for defn in self._actions.values()]


# An approval callback receives the pending call + its definition and returns
# True only on explicit user consent. Anything else (False/None) = refused.
ApprovalCallback = Callable[[ToolCall, ActionDefinition], Optional[bool]]


def execute_tool_call(
    registry: ActionRegistry,
    tool_call: ToolCall,
    approval_callback: Optional[ApprovalCallback],
) -> ChatMessage:
    """Run a single tool call behind the approval gate.

    Always returns a role="tool" :class:`ChatMessage` carrying
    ``tool_call_id`` so the conversation stays well-formed for the provider —
    it never raises, so one failing tool cannot break the agentic loop.
    """
    defn = registry.get(tool_call.name)
    if defn is None:
        return ChatMessage(
            role="tool",
            content=_("Erro: ferramenta desconhecida '{name}'.").format(
                name=tool_call.name
            ),
            tool_call_id=tool_call.id,
        )

    # Fail-closed approval gate. A missing callback refuses; the callback is
    # asked BEFORE the handler ever runs.
    approved = False
    if approval_callback is not None:
        try:
            approved = bool(approval_callback(tool_call, defn))
        except Exception as exc:  # a broken approval UI must not execute the action
            return ChatMessage(
                role="tool",
                content=_("Ação recusada (erro no pedido de aprovação: {err}).").format(
                    err=exc
                ),
                tool_call_id=tool_call.id,
            )

    if not approved:
        return ChatMessage(
            role="tool",
            content=_("Ação recusada pelo utilizador."),
            tool_call_id=tool_call.id,
        )

    try:
        result = defn.handler(tool_call.arguments or {})
    except Exception as exc:  # handlers must never propagate to the loop/GUI
        return ChatMessage(
            role="tool",
            content=_("Erro ao executar {name}: {err}").format(
                name=tool_call.name, err=exc
            ),
            tool_call_id=tool_call.id,
        )

    return ChatMessage(
        role="tool",
        content=str(result),
        tool_call_id=tool_call.id,
    )


def run_tool_loop(
    provider: LLMProvider,
    registry: ActionRegistry,
    messages: list[ChatMessage],
    approval_callback: Optional[ApprovalCallback],
    on_update: Optional[Callable[[str], None]] = None,
    max_rounds: int = 8,
) -> list[ChatMessage]:
    """Drive the agentic conversation to completion, mutating & returning
    ``messages``.

    Each round: ask the provider, append its assistant turn, and — if it wants
    tools — execute every requested call (each behind the approval gate) and
    feed the results back. Stops on ``stop_reason`` "end"/"error" or when
    ``max_rounds`` is hit (loop guard against runaway tool use).
    """

    def _notify(text: str) -> None:
        if on_update is not None:
            try:
                on_update(text)
            except Exception:
                pass  # progress reporting must never break the loop

    # NOTE: named `_round`, NOT `_` — this module imports the real gettext
    # `_()` above, and a `for _ in range(...)` loop target would shadow it
    # for this function's ENTIRE body (Python treats a name assigned
    # anywhere in a function as local to the whole function, regardless of
    # source order), turning every `_("...")` call below into a TypeError
    # ("'int' object is not callable") the moment the loop runs once. See
    # the i18n skill guide's dedicated section on this exact bug class.
    for _round in range(max_rounds):
        response: ChatResponse = provider.send(messages, registry.specs())

        meta = {}
        if response.cost_usd is not None:
            meta["cost_usd"] = response.cost_usd
        messages.append(
            ChatMessage(
                role="assistant",
                content=response.content or "",
                tool_calls=list(response.tool_calls),
                meta=meta,
            )
        )
        if response.content:
            _notify(response.content)

        if response.stop_reason == "error":
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=_("[erro] {error}").format(
                        error=response.error or _("Erro desconhecido do provider.")
                    ),
                )
            )
            return messages

        if response.stop_reason == "tool_use" and response.tool_calls:
            for tc in response.tool_calls:
                _notify(_("[ação] {name}").format(name=tc.name))
                tool_msg = execute_tool_call(registry, tc, approval_callback)
                messages.append(tool_msg)
                _notify(
                    _("[ação] {name} → {result}").format(
                        name=tc.name, result=tool_msg.content
                    )
                )
            # loop again so the model can react to the tool results
            continue

        # stop_reason == "end" (or anything terminal without tool calls)
        return messages

    # Exhausted max_rounds without a natural end — guard against infinite loops.
    messages.append(
        ChatMessage(
            role="assistant",
            content=_(
                "[aviso] Limite de {max_rounds} rondas de ferramentas atingido; "
                "a interromper o ciclo."
            ).format(max_rounds=max_rounds),
        )
    )
    return messages
