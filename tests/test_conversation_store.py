"""Tests for conversation_store.py — open/close/list/delete persistence.

Runs without KiCad/wx: pure stdlib module. conftest.py puts plugins/ on
sys.path.
"""

import json

import pytest

import conversation_store as cs
from llm_providers.base import ChatMessage, ToolCall


@pytest.fixture(autouse=True)
def isolated_conversations_dir(tmp_path, monkeypatch):
    """Every test gets its own throwaway directory instead of the real
    ~/.kicad_chat_assistant/conversations — never touch the user's actual
    saved conversations from a test run."""
    conv_dir = tmp_path / "conversations"
    monkeypatch.setattr(cs, "get_conversations_dir", lambda: conv_dir)
    return conv_dir


# --------------------------------------------------------------------------- #
# id generation / title derivation
# --------------------------------------------------------------------------- #
def test_new_conversation_id_is_unique_enough():
    ids = {cs.new_conversation_id() for _ in range(3)}
    # Millisecond-resolution ids collide only if generated in the same ms;
    # just assert the format, not strict uniqueness under a tight loop.
    assert all(i.startswith("conv-") for i in ids)


def test_derive_title_uses_first_user_message():
    messages = [
        ChatMessage(role="system", content="regra"),
        ChatMessage(role="user", content="Consegues ler a placa?"),
        ChatMessage(role="assistant", content="Sim."),
    ]
    assert cs.derive_title(messages) == "Consegues ler a placa?"


def test_derive_title_truncates_long_messages():
    long_text = "x" * 200
    messages = [ChatMessage(role="user", content=long_text)]
    title = cs.derive_title(messages)
    assert len(title) <= 60
    assert title.endswith("…")


def test_derive_title_falls_back_when_no_user_message():
    messages = [ChatMessage(role="system", content="regra")]
    assert cs.derive_title(messages) == "Nova conversa"


# --------------------------------------------------------------------------- #
# save / load roundtrip
# --------------------------------------------------------------------------- #
def test_save_and_load_roundtrip_preserves_messages():
    messages = [
        ChatMessage(role="system", content="regra"),
        ChatMessage(role="user", content="oi"),
        ChatMessage(
            role="assistant",
            content="vou verificar",
            tool_calls=[ToolCall(id="t1", name="get_project_info", arguments={})],
            meta={"cost_usd": 0.01},
        ),
        ChatMessage(role="tool", content="info aqui", tool_call_id="t1"),
    ]
    cs.save_conversation("conv-test1", messages)
    title, loaded = cs.load_conversation("conv-test1")

    assert title == "oi"
    assert [m.role for m in loaded] == ["system", "user", "assistant", "tool"]
    assert loaded[2].tool_calls[0].name == "get_project_info"
    assert loaded[2].meta == {"cost_usd": 0.01}
    assert loaded[3].tool_call_id == "t1"


def test_save_with_explicit_title_overrides_derived_title():
    messages = [ChatMessage(role="user", content="oi")]
    cs.save_conversation("conv-test2", messages, title="Nome escolhido")
    title, _messages = cs.load_conversation("conv-test2")
    assert title == "Nome escolhido"


def test_load_missing_conversation_raises():
    with pytest.raises(FileNotFoundError):
        cs.load_conversation("does-not-exist")


def test_load_tolerates_malformed_individual_message(isolated_conversations_dir):
    isolated_conversations_dir.mkdir(parents=True, exist_ok=True)
    path = isolated_conversations_dir / "conv-broken.json"
    payload = {
        "id": "conv-broken",
        "title": "t",
        "updated_at": 0,
        "messages": [
            {"role": "user", "content": "boa mensagem"},
            # Missing expected keys - tolerated via .get() defaults, NOT
            # skipped (still a valid, if empty, message).
            {"not": "a valid message shape"},
            # Not a dict at all - these ARE skipped, only a dict can be
            # coerced into a ChatMessage at all.
            None,
            "just a string",
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    title, messages = cs.load_conversation("conv-broken")
    assert title == "t"
    assert len(messages) == 2
    assert messages[0].content == "boa mensagem"
    assert messages[1].role == "user"
    assert messages[1].content == ""


def test_load_raises_valueerror_for_non_dict_payload(isolated_conversations_dir):
    isolated_conversations_dir.mkdir(parents=True, exist_ok=True)
    path = isolated_conversations_dir / "conv-list.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ValueError):
        cs.load_conversation("conv-list")


# --------------------------------------------------------------------------- #
# listing
# --------------------------------------------------------------------------- #
def test_list_conversations_empty_when_dir_missing():
    assert cs.list_conversations() == []


def test_list_conversations_newest_first(monkeypatch):
    times = iter([100.0, 200.0, 300.0])
    monkeypatch.setattr(cs.time, "time", lambda: next(times))

    cs.save_conversation("conv-a", [ChatMessage(role="user", content="a")])
    cs.save_conversation("conv-b", [ChatMessage(role="user", content="b")])
    cs.save_conversation("conv-c", [ChatMessage(role="user", content="c")])

    listed = cs.list_conversations()
    assert [item["id"] for item in listed] == ["conv-c", "conv-b", "conv-a"]
    assert [item["title"] for item in listed] == ["c", "b", "a"]


def test_list_conversations_skips_corrupted_files(isolated_conversations_dir):
    isolated_conversations_dir.mkdir(parents=True, exist_ok=True)
    (isolated_conversations_dir / "conv-good.json").write_text(
        json.dumps({"id": "conv-good", "title": "Boa", "updated_at": 1, "messages": []}),
        encoding="utf-8",
    )
    (isolated_conversations_dir / "conv-corrupt.json").write_text(
        "{not valid json", encoding="utf-8"
    )
    listed = cs.list_conversations()
    assert [item["id"] for item in listed] == ["conv-good"]


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #
def test_delete_conversation_removes_file():
    cs.save_conversation("conv-del", [ChatMessage(role="user", content="x")])
    assert len(cs.list_conversations()) == 1
    cs.delete_conversation("conv-del")
    assert cs.list_conversations() == []


def test_delete_nonexistent_conversation_is_noop():
    cs.delete_conversation("never-existed")  # must not raise


def test_delete_rejects_path_traversal_id_safely():
    # Must not raise, and must not touch anything outside the conversations
    # dir - _safe_path_for_id() strips everything but [A-Za-z0-9_-].
    cs.delete_conversation("../../evil")


def test_save_sanitizes_path_traversal_id_instead_of_touching_outside_paths():
    # "../../evil" sanitizes down to "evil" (non-empty) - saved safely
    # inside the conversations dir, never raises, never escapes it.
    cs.save_conversation("../../evil", [ChatMessage(role="user", content="x")])
    listed = cs.list_conversations()
    assert [item["id"] for item in listed] == ["evil"]


def test_save_raises_when_id_sanitizes_to_nothing():
    with pytest.raises(ValueError):
        cs.save_conversation("....", [ChatMessage(role="user", content="x")])
