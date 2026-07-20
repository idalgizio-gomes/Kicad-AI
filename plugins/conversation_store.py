"""
Persistence for chat conversations — open/close/list/delete — so a chat
session isn't lost when the dialog closes, and multiple conversations can
coexist and be resumed later.

Deliberately kept free of `wx`/`pcbnew` (pure stdlib: json/pathlib/time/re)
so it's testable outside KiCad, same convention as llm_providers/__init__.py.
Each conversation is one JSON file under
~/.kicad_chat_assistant/conversations/<id>.json — simple, human-inspectable,
and consistent with the plugin's existing config.json location.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from pathlib import Path

try:
    from .llm_providers.base import ChatMessage, ToolCall
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from llm_providers.base import ChatMessage, ToolCall  # type: ignore

_TITLE_MAX_LEN = 60


def get_conversations_dir() -> Path:
    """~/.kicad_chat_assistant/conversations — sibling of the plugin's
    existing config.json (see llm_providers/__init__.py::get_config_path)."""
    return Path.home() / ".kicad_chat_assistant" / "conversations"


def _safe_path_for_id(conversation_id: str) -> Path:
    """Sanitizes conversation_id before it becomes part of a filesystem
    path. IDs are normally generated internally (new_conversation_id()) and
    never untrusted, but every function that turns an id into a path uses
    this — cheap, consistent defense against a future caller passing
    something like "../../evil" through, rather than relying on every
    call site remembering to sanitize individually."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", conversation_id)
    if not safe:
        raise ValueError(f"ID de conversa inválido: {conversation_id!r}")
    return get_conversations_dir() / f"{safe}.json"


def new_conversation_id() -> str:
    """Millisecond timestamp is unique enough for a single-user local
    plugin — no need for uuid overhead here, and it sorts chronologically
    by string comparison as a bonus."""
    return f"conv-{int(time.time() * 1000)}"


def derive_title(messages: list[ChatMessage]) -> str:
    """First user message, trimmed — used when the user hasn't named the
    conversation explicitly. Falls back to a generic label for an
    empty/system-only conversation (nothing worth saving yet, but callers
    that DO save an empty one still get a sane label instead of "")."""
    for m in messages:
        if m.role == "user" and m.content.strip():
            text = " ".join(m.content.strip().split())
            if len(text) > _TITLE_MAX_LEN:
                text = text[: _TITLE_MAX_LEN - 1] + "…"
            return text
    return "Nova conversa"


def _messages_to_json(messages: list[ChatMessage]) -> list[dict]:
    return [asdict(m) for m in messages]


def _messages_from_json(data: list) -> list[ChatMessage]:
    """Tolerant of a missing/malformed field per message (a hand-edited or
    partially-written file shouldn't make the WHOLE conversation
    unloadable) — a message that can't be reconstructed is skipped, never
    raises."""
    out: list[ChatMessage] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            tool_calls = [
                ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    arguments=tc.get("arguments", {}) or {},
                )
                for tc in (item.get("tool_calls") or [])
                if isinstance(tc, dict)
            ]
            out.append(
                ChatMessage(
                    role=item.get("role", "user"),
                    content=item.get("content", "") or "",
                    tool_calls=tool_calls,
                    tool_call_id=item.get("tool_call_id"),
                    meta=item.get("meta") or {},
                )
            )
        except Exception:
            continue
    return out


def save_conversation(
    conversation_id: str, messages: list[ChatMessage], title: str | None = None
) -> None:
    """Write (or overwrite) a conversation to disk. `title` defaults to
    derive_title(messages) when not given explicitly."""
    path = _safe_path_for_id(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The stored "id" is the SANITIZED filename stem, not the raw input —
    # keeps list_conversations()'s reported id always consistent with the
    # actual file on disk (round-tripping load_conversation(that_id) always
    # resolves to the same file, even if the original caller passed
    # something that needed sanitizing).
    payload = {
        "id": path.stem,
        "title": title if title is not None else derive_title(messages),
        "updated_at": time.time(),
        "messages": _messages_to_json(messages),
    }
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    # Atomic-ish replace: a crash mid-write leaves the old file intact
    # instead of a half-written, corrupted conversation.
    tmp_path.replace(path)


def load_conversation(conversation_id: str) -> tuple[str, list[ChatMessage]]:
    """Returns (title, messages). Raises FileNotFoundError/ValueError for
    the caller to turn into a user-facing error — unlike list_conversations
    (which tolerates corruption by skipping), an explicit "open this one"
    request should surface a real problem rather than silently doing
    nothing."""
    path = _safe_path_for_id(conversation_id)
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"Ficheiro de conversa inválido: {path}")
    title = payload.get("title") or conversation_id
    messages = _messages_from_json(payload.get("messages") or [])
    return title, messages


def list_conversations() -> list[dict]:
    """[{"id", "title", "updated_at"}, ...], newest first. A corrupted
    individual file is skipped (not a reason to hide every OTHER saved
    conversation) rather than raising."""
    directory = get_conversations_dir()
    if not directory.exists():
        return []

    items = []
    for path in directory.glob("*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                continue
            items.append(
                {
                    "id": payload.get("id") or path.stem,
                    "title": payload.get("title") or path.stem,
                    "updated_at": payload.get("updated_at") or 0,
                }
            )
        except (OSError, json.JSONDecodeError, ValueError):
            continue

    items.sort(key=lambda it: it["updated_at"], reverse=True)
    return items


def delete_conversation(conversation_id: str) -> None:
    """Never raises for a conversation that's already gone — deleting
    something twice (e.g. a stale picker list) should be a no-op, not an
    error the user has to make sense of."""
    try:
        path = _safe_path_for_id(conversation_id)
    except ValueError:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
