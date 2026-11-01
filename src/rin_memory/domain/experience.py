"""Stable message identities shared by extraction and reflection inputs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from ..classification import metadata_of
from .barrier import message_source_keys, message_text


def message_role(message: Mapping[str, Any]) -> str:
    role = str(message.get("type") or message.get("role") or "").lower()
    return {"human": "user", "ai": "assistant", "function": "tool"}.get(role, role)


def number_messages(
    messages: Sequence[Mapping[str, Any]],
    source_keys: Sequence[str] = (),
    occurred_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Keep original ids/positions across windows, batches and raw-index reads."""
    result = []
    for position, message in enumerate(messages):
        row = dict(message)
        data = dict(row.get("data") or {})
        meta = dict(metadata_of(row))
        keys = sorted(message_source_keys(row)) or list(source_keys)
        key = str(meta.get("source_key") or (keys[0] if keys else "turn"))
        message_id = row.get("id") or data.get("id") or meta.get("message_id")
        meta.setdefault("message_position", position)
        meta.setdefault("message_ref", f"{key}:{message_id or 'm' + str(position)}")
        if occurred_at is not None:
            meta.setdefault("occurred_at", occurred_at.isoformat())
        data["metadata"] = meta
        row["data"] = data
        if "metadata" in row:
            row["metadata"] = meta
        result.append(row)
    return result


def message_material(message: Mapping[str, Any]) -> dict[str, Any]:
    """A model-visible line plus its durable source binding."""
    meta = metadata_of(message)
    return {
        "message_ref": meta["message_ref"],
        "source_key": meta.get("source_key") or next(iter(sorted(message_source_keys(message))), ""),
        "role": message_role(message),
        "position": meta["message_position"],
        "occurred_at": meta.get("occurred_at") or message.get("timestamp"),
        "text": message_text(message).strip(),
    }
