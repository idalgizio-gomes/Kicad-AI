"""
Tests for actions.framework — the tool-calling loop and its approval gate.

Runs without KiCad/wx/API keys: uses an in-memory fake provider that returns
pre-programmed ChatResponses. conftest.py puts ``plugins/`` on sys.path.
"""

from actions.framework import (
    ActionDefinition,
    ActionRegistry,
    execute_tool_call,
    run_tool_loop,
)
from llm_providers.base import (
    ChatMessage,
    ChatResponse,
    LLMProvider,
    ToolCall,
    ToolSpec,
)


# --- helpers ---------------------------------------------------------------


def _spec(name="echo"):
    return ToolSpec(
        name=name,
        description="Echo the given text.",
        parameters={"type": "object", "properties": {}, "required": []},
    )


def _echo_defn(name="echo"):
    return ActionDefinition(spec=_spec(name), handler=lambda args: f"echoed:{args}")


class ScriptedProvider(LLMProvider):
    """Fake provider that yields pre-programmed responses in order."""

    id = "fake"
    display_name = "Fake"

    def __init__(self, responses):
        super().__init__(api_key="x")
        self._responses = list(responses)
        self.sent = []  # captured (messages, tools) per call

    def default_model(self) -> str:
        return "fake-model"

    def send(self, messages, tools=None) -> ChatResponse:
        self.sent.append((list(messages), tools))
        if self._responses:
            return self._responses.pop(0)
        return ChatResponse(content="done", stop_reason="end")


def _yes(tool_call, defn):
    return True


def _no(tool_call, defn):
    return False


# --- registry --------------------------------------------------------------


def test_registry_register_get_specs():
    reg = ActionRegistry()
    defn = _echo_defn()
    reg.register(defn)
    assert reg.get("echo") is defn
    assert reg.get("missing") is None
    specs = reg.specs()
    assert len(specs) == 1 and specs[0].name == "echo"


def test_registry_duplicate_raises():
    reg = ActionRegistry()
    reg.register(_echo_defn())
    try:
        reg.register(_echo_defn())
        assert False, "expected ValueError on duplicate"
    except ValueError:
        pass


# --- approval gate ---------------------------------------------------------


def test_refusal_skips_handler():
    calls = []
    reg = ActionRegistry()
    reg.register(ActionDefinition(spec=_spec(), handler=lambda a: calls.append(a) or "ran"))
    tc = ToolCall(id="c1", name="echo", arguments={})
    msg = execute_tool_call(reg, tc, _no)
    assert msg.role == "tool"
    assert msg.tool_call_id == "c1"
    assert "recusada" in msg.content.lower()
    assert calls == []  # handler never executed


def test_none_callback_fails_closed():
    calls = []
    reg = ActionRegistry()
    reg.register(ActionDefinition(spec=_spec(), handler=lambda a: calls.append(a) or "ran"))
    tc = ToolCall(id="c1", name="echo", arguments={})
    msg = execute_tool_call(reg, tc, None)
    assert "recusada" in msg.content.lower()
    assert calls == []


def test_approval_runs_handler():
    reg = ActionRegistry()
    reg.register(ActionDefinition(spec=_spec(), handler=lambda a: "OK-RESULT"))
    tc = ToolCall(id="c1", name="echo", arguments={"x": 1})
    msg = execute_tool_call(reg, tc, _yes)
    assert msg.content == "OK-RESULT"
    assert msg.tool_call_id == "c1"


def test_unknown_tool():
    reg = ActionRegistry()
    tc = ToolCall(id="c9", name="ghost", arguments={})
    msg = execute_tool_call(reg, tc, _yes)
    assert "desconhecida" in msg.content.lower()
    assert msg.tool_call_id == "c9"


def test_handler_exception_becomes_tool_message():
    def boom(args):
        raise RuntimeError("kaboom")

    reg = ActionRegistry()
    reg.register(ActionDefinition(spec=_spec(), handler=boom))
    tc = ToolCall(id="c1", name="echo", arguments={})
    msg = execute_tool_call(reg, tc, _yes)
    assert msg.role == "tool"
    assert "kaboom" in msg.content
    assert "Erro ao executar echo" in msg.content


def test_broken_approval_callback_refuses():
    def broken(tc, defn):
        raise ValueError("ui crashed")

    ran = []
    reg = ActionRegistry()
    reg.register(ActionDefinition(spec=_spec(), handler=lambda a: ran.append(1) or "ran"))
    tc = ToolCall(id="c1", name="echo", arguments={})
    msg = execute_tool_call(reg, tc, broken)
    assert "recusada" in msg.content.lower()
    assert ran == []


# --- full loop -------------------------------------------------------------


def test_full_loop_tool_use_then_end():
    reg = ActionRegistry()
    reg.register(ActionDefinition(spec=_spec(), handler=lambda a: "component list"))
    provider = ScriptedProvider(
        [
            ChatResponse(
                content="let me check",
                tool_calls=[ToolCall(id="t1", name="echo", arguments={})],
                stop_reason="tool_use",
            ),
            ChatResponse(content="here you go", stop_reason="end"),
        ]
    )
    messages = [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hi")]
    out = run_tool_loop(provider, reg, messages, _yes)
    roles = [m.role for m in out]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert out[3].content == "component list"
    assert out[3].tool_call_id == "t1"
    assert out[-1].content == "here you go"


def test_loop_refusal_feeds_refusal_back():
    ran = []
    reg = ActionRegistry()
    reg.register(ActionDefinition(spec=_spec(), handler=lambda a: ran.append(1) or "x"))
    provider = ScriptedProvider(
        [
            ChatResponse(
                content="",
                tool_calls=[ToolCall(id="t1", name="echo", arguments={})],
                stop_reason="tool_use",
            ),
            ChatResponse(content="ok, skipped", stop_reason="end"),
        ]
    )
    messages = [ChatMessage(role="user", content="do it")]
    out = run_tool_loop(provider, reg, messages, _no)
    assert ran == []  # handler never ran
    tool_msgs = [m for m in out if m.role == "tool"]
    assert "recusada" in tool_msgs[0].content.lower()


def test_loop_error_stop_reason():
    reg = ActionRegistry()
    provider = ScriptedProvider(
        [ChatResponse(content="", stop_reason="error", error="auth failed")]
    )
    messages = [ChatMessage(role="user", content="hi")]
    out = run_tool_loop(provider, reg, messages, _yes)
    assert any("auth failed" in m.content for m in out)
    assert out[-1].content.startswith("[erro]")


def test_loop_max_rounds_guard():
    reg = ActionRegistry()
    reg.register(ActionDefinition(spec=_spec(), handler=lambda a: "again"))
    # provider always asks for a tool -> would loop forever without the guard
    provider = ScriptedProvider([])

    def always_tool(messages, tools=None):
        return ChatResponse(
            content="loop",
            tool_calls=[ToolCall(id="t", name="echo", arguments={})],
            stop_reason="tool_use",
        )

    provider.send = always_tool  # type: ignore[assignment]
    messages = [ChatMessage(role="user", content="hi")]
    out = run_tool_loop(provider, reg, messages, _yes, max_rounds=3)
    assert "[aviso]" in out[-1].content
    assert "Limite de 3" in out[-1].content


def test_cost_usd_propagates_into_assistant_meta():
    reg = ActionRegistry()
    provider = ScriptedProvider(
        [ChatResponse(content="olá", stop_reason="end", cost_usd=0.0776229)]
    )
    messages = [ChatMessage(role="user", content="oi")]
    out = run_tool_loop(provider, reg, messages, _yes)
    assert out[-1].meta.get("cost_usd") == 0.0776229


def test_no_cost_usd_leaves_meta_empty():
    reg = ActionRegistry()
    provider = ScriptedProvider([ChatResponse(content="olá", stop_reason="end")])
    messages = [ChatMessage(role="user", content="oi")]
    out = run_tool_loop(provider, reg, messages, _yes)
    assert out[-1].meta == {}


def test_on_update_called():
    seen = []
    reg = ActionRegistry()
    reg.register(ActionDefinition(spec=_spec(), handler=lambda a: "res"))
    provider = ScriptedProvider(
        [
            ChatResponse(
                content="thinking",
                tool_calls=[ToolCall(id="t1", name="echo", arguments={})],
                stop_reason="tool_use",
            ),
            ChatResponse(content="final", stop_reason="end"),
        ]
    )
    run_tool_loop(
        provider,
        reg,
        [ChatMessage(role="user", content="hi")],
        _yes,
        on_update=seen.append,
    )
    assert any("thinking" in s for s in seen)
    assert any("[ação]" in s for s in seen)
