"""Stable message identities shared by extraction and reflection inputs."""

from __future__ import annotations

from datetime import datetime, timezone
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

def user_confirmation_bindings(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    keys = set(row.get("source_event_keys") or ())
    return [binding for binding in row.get("source_bindings") or ()
            if isinstance(binding, Mapping) and binding.get("role") == "user"
            and binding.get("evidence_kind") in ("user_assertion", "explicit_feedback")
            and binding.get("source_key") in keys]


def confirmation_time(row: Mapping[str, Any]) -> datetime | None:
    """Business time from surviving user evidence, independent of record usage."""
    dates = []
    for binding in user_confirmation_bindings(row):
        stamp = binding.get("occurred_at")
        if isinstance(stamp, str):
            try:
                date = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            dates.append(date.replace(tzinfo=date.tzinfo or timezone.utc))
    return max(dates, default=None)


def fact_support_groups(row: Mapping[str, Any]) -> list[list[str]]:
    """Each group is sufficient evidence, but every source in it is required."""
    if "support_groups" in row:
        return [list(group) for group in row["support_groups"]]
    keys = list(row.get("source_event_keys") or ())
    # Legacy rows did not distinguish independent confirmations from synthesis.
    return [keys] if keys else []
