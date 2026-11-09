"""Host-compatible deletion barrier store (B03, design §17.15).

\`memory_deletions.json\` keeps three things per character:

* \`deletions[]\` — one bounded operation record per user delete request, with
  its receipt steps, plugin receipts and (temporarily) bounded derived texts;
* \`tombstones[]\` — long-lived barriers: source keys, text hashes, episode ids
  and entry ids that must never come back, **without any original text**;
* \`version\` — a monotonic counter every successful write increments; readers
  use it to discard results derived from a state that has since moved.

On-disk shape is the host's (schema 1) so the N.E.K.O build keeps reading and
writing the file:

    {"schema": 1, "version": <int>, "deletions": [...], "tombstones": [...]}

Deviation from the host, deliberate and documented in design §17.15: the host
reads a damaged or unknown-schema file as an empty document. This store raises
\`CorruptStoreError\` / \`InvalidSchemaError\` instead, because "empty" reopens
every barrier and would let deleted content back into reads and replays. The
file is never rewritten on a failed read.

Write order for a new deletion (used by \`domain/deletions.py\`):
\`prepare_registration\` (no write) → journal append of the replay payload →
\`apply_snapshot\` into the loaded view → \`save_view\`. A crash between the
journal append and the view save replays the payload on the next open; a crash
before the append leaves nothing registered (no barrier, no receipt).

All methods are synchronous and take a per-file \`threading.RLock\`; call them
through \`asyncio.to_thread\`. Every write passes the injected \`WritePermit\`
first. Original text is never persisted in a tombstone or in the journal
payload: the only bounded exception is \`target_texts\`, which exists while a
plugin still has to clean its private copy and is scrubbed afterwards.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts import (
    MAX_DELETION_EPISODES,
    MAX_DELETION_SOURCE_KEYS,
    MAX_DELETION_TARGETS,
)
from ..errors import CorruptStoreError, DeletionNotFoundError, InvalidSchemaError
from ..event_sources import is_valid_source_key, plugin_id_of
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .version_cache import VersionReader

DELETIONS_FILENAME = "memory_deletions.json"
DELETIONS_SCHEMA = 1
MAX_DELETIONS = 128
MAX_TOMBSTONES = 4000
MAX_TARGETS = MAX_DELETION_TARGETS
MAX_SOURCE_KEYS = MAX_DELETION_SOURCE_KEYS
MAX_ID_CHARS = 200
MAX_TEXT_CHARS = 600
MAX_TARGET_TEXTS = 4
MAX_PLUGIN_IDS = 8
MAX_DETAIL_CHARS = 400
MAX_EPISODES = MAX_DELETION_EPISODES
MAX_PENDING_EPISODE_IDS = 4
MAX_RESOLVED_TEXT_HASHES = 64

TARGET_TYPES = ("fact", "reflection", "persona")
EPISODE_TARGET_TYPE = "episode"
ALL_TARGET_TYPES = TARGET_TYPES + (EPISODE_TARGET_TYPE,)
STEP_NAMES = (
    "barrier", "resolve", "facts", "reflections", "persona",
    "recent", "cache", "turn_results", "plugin",
)
#: Steps that must be terminal before \`content_cleaned\` can be true. The
#: plugin copy is tracked separately so an offline plugin keeps the receipt
#: pending without pretending host content is still there.
#: \`turn_results\` is included: a deleted source whose committed reply is still
#: served has not really been cleaned (R01).
CONTENT_STEPS = (
    "resolve", "facts", "reflections", "persona", "recent", "cache",
    "turn_results",
)
TERMINAL_STEP_STATUSES = ("completed", "skipped")
STEP_STATUSES = frozenset({"pending", "completed", "failed", "skipped"})
DELETION_STATUSES = frozenset({"pending", "completed", "failed"})
PLUGIN_RECEIPT_STATUSES = frozenset({"cleaned", "failed"})

#: One lock per deletion file; created lazily and never held across an await.
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


# -- small bounded value helpers (host limits, core error policy) ------------


def _bounded_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _bounded_id_list(value: Any, *, limit: int, max_chars: int = MAX_ID_CHARS) -> list[str]:
    if isinstance(value, (str, bytes)):
        return []
    if not isinstance(value, Sequence):
        return []
    out: list[str] = []
    for item in list(value)[:limit]:
        text = _bounded_text(item, max_chars)
        if text and text not in out:
            out.append(text)
    return out


def _bounded_detail(detail: Any) -> dict[str, Any] | None:
    """Keep a bounded, JSON-safe detail object (host \`_bounded_detail\` shape)."""
    if not isinstance(detail, Mapping):
        return None
    cleaned: dict[str, Any] = {}
    for key, value in list(detail.items())[:16]:
        name = _bounded_text(key, 64)
        if not name:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            cleaned[name] = value if not isinstance(value, str) else value[:MAX_DETAIL_CHARS]
        elif isinstance(value, (list, tuple)):
            cleaned[name] = [
                item[:MAX_DETAIL_CHARS] if isinstance(item, str) else item
                for item in list(value)[:32]
                if isinstance(item, (str, int, float, bool))
            ]
    return cleaned or None


def _now_iso(clock: Clock) -> str:
    return clock.now().isoformat()


# -- normalization (strict on read, host shape on write) ---------------------


def _normalize_step(raw: Any, path: Path, name: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(f"deletion step {name!r} is not an object", path=path)
    status = _bounded_text(raw.get("status"), 16) or "pending"
    if status not in STEP_STATUSES:
        raise CorruptStoreError(
            f"deletion step {name!r} has unknown status {status!r}", path=path,
        )
    step: dict[str, Any] = {"status": status}
    at = _bounded_text(raw.get("at"), 40)
    if at:
        step["at"] = at
    detail = _bounded_detail(raw.get("detail"))
    if detail:
        step["detail"] = detail
    return step


def _normalize_target(raw: Any, path: Path) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError("deletion target is not an object", path=path)
    target_type = _bounded_text(raw.get("type"), 32).lower()
    target_id = _bounded_text(raw.get("id"), MAX_ID_CHARS)
    if target_type not in ALL_TARGET_TYPES or not target_id:
        raise CorruptStoreError(
            f"invalid deletion target {raw!r}", path=path,
        )
    target: dict[str, Any] = {"type": target_type, "id": target_id}
    digest = _bounded_text(raw.get("text_hash"), 64)
    if digest:
        if len(digest) != 64:
            raise CorruptStoreError(
                f"deletion target {target_id!r} has a malformed text_hash", path=path,
            )
        target["text_hash"] = digest
    return target


def _normalize_plugin_receipt(raw: Any, path: Path, plugin_id: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"plugin receipt {plugin_id!r} is not an object", path=path,
        )
    status = _bounded_text(raw.get("status"), 16)
    if status not in PLUGIN_RECEIPT_STATUSES:
        raise CorruptStoreError(
            f"plugin receipt {plugin_id!r} has unknown status {status!r}", path=path,
        )
    receipt: dict[str, Any] = {
        "status": status,
        "cleaned": _bounded_id_list(raw.get("cleaned"), limit=32, max_chars=120),
        "failed": _bounded_id_list(raw.get("failed"), limit=32, max_chars=120),
        "at": _bounded_text(raw.get("at"), 40),
    }
    detail = _bounded_text(raw.get("detail"), MAX_DETAIL_CHARS)
    if detail:
        receipt["detail"] = detail
    return receipt


def _recompute_record(record: dict[str, Any]) -> None:
    """Recompute completed/pending/failed, stopped_using and content_cleaned."""
    steps = record.get("steps") or {}
    completed: list[str] = []
    pending: list[str] = []
    failed: list[str] = []
    for name in STEP_NAMES:
        status = (steps.get(name) or {}).get("status")
        if status in TERMINAL_STEP_STATUSES:
            completed.append(name)
        elif status == "failed":
            failed.append(name)
        else:
            pending.append(name)
    record["completed"] = completed
    record["pending"] = pending
    record["failed"] = failed
    record["stopped_using"] = (steps.get("barrier") or {}).get("status") in TERMINAL_STEP_STATUSES
    record["content_cleaned"] = all(
        (steps.get(name) or {}).get("status") in TERMINAL_STEP_STATUSES
        for name in CONTENT_STEPS
    )
    if failed:
        record["status"] = "failed"
    elif pending:
        record["status"] = "pending"
    else:
        record["status"] = "completed"


def _normalize_record(raw: Any, path: Path) -> dict[str, Any]:
    """Validate one stored deletion record; structural damage raises."""
    if not isinstance(raw, Mapping):
        raise CorruptStoreError("deletion record is not an object", path=path)
    deletion_id = _bounded_text(raw.get("deletion_id"), 120)
    if not deletion_id:
        raise CorruptStoreError("deletion record has no deletion_id", path=path)
    scope = _bounded_text(raw.get("scope"), 32).lower() or "entry"
    if scope not in ("entry", "episode"):
        raise CorruptStoreError(
            f"deletion {deletion_id!r} has unknown scope {scope!r}", path=path,
        )
    status = _bounded_text(raw.get("status"), 16) or "pending"
    if status not in DELETION_STATUSES:
        raise CorruptStoreError(
            f"deletion {deletion_id!r} has unknown status {status!r}", path=path,
        )
    record: dict[str, Any] = {
        "deletion_id": deletion_id,
        "scope": scope,
        "subject": _bounded_text(raw.get("subject"), 256) or "private",
        "reason": _bounded_text(raw.get("reason"), MAX_DETAIL_CHARS),
        "created_at": _bounded_text(raw.get("created_at"), 40),
        "updated_at": _bounded_text(raw.get("updated_at"), 40),
        "targets": [],
        "resolved_targets": [],
        "episode_ids": [],
        "source_keys": [],
        "plugin_source_keys": [],
        "plugin_ids": [],
        "target_texts": [],
        "plugin_receipts": {},
        "steps": {},
    }
    for item in raw.get("targets") or ():
        if len(record["targets"]) >= MAX_TARGETS:
            break
        record["targets"].append(_normalize_target(item, path))
    for item in raw.get("resolved_targets") or ():
        if len(record["resolved_targets"]) >= MAX_TARGETS:
            break
        record["resolved_targets"].append(_normalize_target(item, path))
    record["source_keys"] = [
        key for key in _bounded_id_list(
            raw.get("source_keys"), limit=MAX_SOURCE_KEYS, max_chars=600,
        )
        if is_valid_source_key(key)
    ]
    record["plugin_source_keys"] = [
        key for key in _bounded_id_list(
            raw.get("plugin_source_keys"), limit=MAX_SOURCE_KEYS, max_chars=600,
        )
        if is_valid_source_key(key)
    ]
    record["episode_ids"] = _bounded_id_list(
        raw.get("episode_ids"), limit=MAX_EPISODES, max_chars=MAX_ID_CHARS,
    )
    record["plugin_ids"] = _bounded_id_list(
        raw.get("plugin_ids"), limit=MAX_PLUGIN_IDS, max_chars=64,
    )
    for item in raw.get("target_texts") or ():
        if len(record["target_texts"]) >= MAX_TARGET_TEXTS:
            break
        if not isinstance(item, Mapping):
            raise CorruptStoreError(
                f"deletion {deletion_id!r} has a malformed target_text", path=path,
            )
        text = _bounded_text(item.get("text"), MAX_TEXT_CHARS)
        if not text:
            continue
        record["target_texts"].append({
            "type": _bounded_text(item.get("type"), 32),
            "id": _bounded_text(item.get("id"), MAX_ID_CHARS),
            "text": text,
        })
    receipts = raw.get("plugin_receipts")
    if isinstance(receipts, Mapping):
        for plugin_id, value in list(receipts.items())[:MAX_PLUGIN_IDS]:
            plugin_text = _bounded_text(plugin_id, 64)
            if plugin_text:
                record["plugin_receipts"][plugin_text] = _normalize_plugin_receipt(
                    value, path, plugin_text,
                )
    steps = raw.get("steps")
    if isinstance(steps, Mapping):
        for name in STEP_NAMES:
            if name in steps:
                record["steps"][name] = _normalize_step(steps[name], path, name)
    for name in STEP_NAMES:
        record["steps"].setdefault(name, {"status": "pending"})
    _recompute_record(record)
    return record


def _normalize_tombstone(raw: Any, path: Path) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError("tombstone is not an object", path=path)
    tombstone_type = _bounded_text(raw.get("type"), 32)
    if not tombstone_type:
        raise CorruptStoreError("tombstone has no type", path=path)
    item: dict[str, Any] = {
        "deletion_id": _bounded_text(raw.get("deletion_id"), 120),
        "type": tombstone_type,
        "at": _bounded_text(raw.get("at"), 40),
    }
    if tombstone_type == "source_key":
        key = _bounded_text(raw.get("source_key"), 600)
        if not is_valid_source_key(key):
            raise CorruptStoreError(
                f"tombstone has an invalid source key {key!r}", path=path,
            )
        item["source_key"] = key
    elif tombstone_type == "text":
        digest = _bounded_text(raw.get("text_hash"), 64)
        if len(digest) != 64:
            raise CorruptStoreError("text tombstone has no valid text_hash", path=path)
        item["text_hash"] = digest
    elif tombstone_type == EPISODE_TARGET_TYPE:
        episode_id = _bounded_text(
            raw.get("episode_id") or raw.get("id"), MAX_ID_CHARS,
        )
        if not episode_id:
            raise CorruptStoreError("episode tombstone has no episode_id", path=path)
        item["episode_id"] = episode_id
    elif tombstone_type in TARGET_TYPES:
        target_id = _bounded_text(raw.get("id"), MAX_ID_CHARS)
        if not target_id:
            raise CorruptStoreError(
                f"{tombstone_type} tombstone has no id", path=path,
            )
        item["id"] = target_id
    else:
        raise CorruptStoreError(
            f"tombstone has unknown type {tombstone_type!r}", path=path,
        )
    return item


def _empty_doc() -> dict[str, Any]:
    return {"schema": DELETIONS_SCHEMA, "version": 0, "deletions": [], "tombstones": []}


def _tombstone_key(tombstone: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        tombstone.get("deletion_id"),
        tombstone.get("type"),
        tombstone.get("id")
        or tombstone.get("episode_id")
        or tombstone.get("source_key")
        or tombstone.get("text_hash"),
    )


def _rotate_doc(doc: dict[str, Any]) -> None:
    """Bound the document: completed operations first, then oldest tombstones."""
    records = doc.get("deletions")
    if isinstance(records, list) and len(records) > MAX_DELETIONS:
        records.sort(
            key=lambda item: (
                0 if item.get("status") == "completed" else 1,
                str(item.get("created_at") or ""),
            ),
        )
        del records[: len(records) - MAX_DELETIONS]
    tombstones = doc.get("tombstones")
    if isinstance(tombstones, list) and len(tombstones) > MAX_TOMBSTONES:
        tombstones.sort(key=lambda item: str(item.get("at") or ""))
        del tombstones[: len(tombstones) - MAX_TOMBSTONES]


def _normalize_doc(raw: Any, path: Path) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"expected a JSON object, found {type(raw).__name__}", path=path,
        )
    version = raw.get("schema")
    if version is None:
        version = raw.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise InvalidSchemaError(
            f"deletion store has no integer schema: {version!r}", path=path,
        )
    if version != DELETIONS_SCHEMA:
        raise InvalidSchemaError(
            f"unsupported deletion schema {version}; this build supports "
            f"{DELETIONS_SCHEMA}", path=path,
        )
    doc = _empty_doc()
    raw_version = raw.get("version")
    if raw_version is not None:
        if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 0:
            raise CorruptStoreError(
                f"deletion store version must be a nonnegative integer, "
                f"found {raw_version!r}", path=path,
            )
        doc["version"] = raw_version
    rows = raw.get("deletions")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise CorruptStoreError("deletion store 'deletions' must be a list", path=path)
    for row in rows:
        doc["deletions"].append(_normalize_record(row, path))
    tombstones = raw.get("tombstones")
    if tombstones is None:
        tombstones = []
    if not isinstance(tombstones, list):
        raise CorruptStoreError("deletion store 'tombstones' must be a list", path=path)
    for row in tombstones:
        doc["tombstones"].append(_normalize_tombstone(row, path))
    _rotate_doc(doc)
    return doc


# -- public projections ------------------------------------------------------


def _public_record(record: Mapping[str, Any], version: int) -> dict[str, Any]:
    """Receipt projection: no original text (target_texts never leak here)."""
    steps = record.get("steps") or {}
    return {
        "deletion_id": record.get("deletion_id"),
        "version": int(version or 0),
        "scope": record.get("scope") or "entry",
        "status": record.get("status") or "pending",
        "stopped_using": bool(record.get("stopped_using")),
        "content_cleaned": bool(record.get("content_cleaned")),
        "targets": [
            {"type": item.get("type"), "id": item.get("id")}
            for item in (record.get("targets") or [])
            if isinstance(item, Mapping)
        ],
        "resolved_targets": [
            {"type": item.get("type"), "id": item.get("id")}
            for item in (record.get("resolved_targets") or [])
            if isinstance(item, Mapping)
        ],
        "episode_ids": list(record.get("episode_ids") or []),
        "source_keys": list(record.get("source_keys") or []),
        "completed": list(record.get("completed") or []),
        "pending": list(record.get("pending") or []),
        "failed": list(record.get("failed") or []),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "steps": {
            name: {key: value for key, value in dict(step or {}).items()
                   if key in ("status", "at", "detail")}
            for name, step in steps.items()
            if isinstance(step, Mapping)
        },
        "plugin_receipts": {
            plugin_id: dict(receipt)
            for plugin_id, receipt in (record.get("plugin_receipts") or {}).items()
            if isinstance(receipt, Mapping)
        },
    }


def _record_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    """Journal replay payload: the record without temporary derived texts."""
    return {key: value for key, value in dict(record).items() if key != "target_texts"}


def _fingerprint(record: Mapping[str, Any]) -> str:
    """Payload fingerprint for duplicate detection; time fields excluded."""
    return json.dumps(
        {
            "scope": record.get("scope"),
            "targets": [
                [item.get("type"), item.get("id")]
                for item in (record.get("targets") or [])
                if isinstance(item, Mapping)
            ],
            "episode_ids": list(record.get("episode_ids") or []),
            "source_keys": list(record.get("source_keys") or []),
            "subject": record.get("subject"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _tombstones_for_record(record: Mapping[str, Any], *, now: str) -> list[dict[str, Any]]:
    """Build the barrier rows of one record (IDs/hashes only, never text)."""
    tombstones: list[dict[str, Any]] = []
    deletion_id = record.get("deletion_id")
    for target in record.get("targets") or ():
        if not isinstance(target, Mapping):
            continue
        target_type = target.get("type")
        target_id = target.get("id")
        if target_type == EPISODE_TARGET_TYPE and target_id:
            tombstones.append({
                "deletion_id": deletion_id,
                "type": EPISODE_TARGET_TYPE,
                "episode_id": target_id,
                "at": now,
            })
        elif target_type in TARGET_TYPES and target_id:
            tombstones.append({
                "deletion_id": deletion_id,
                "type": target_type,
                "id": target_id,
                "at": now,
            })
        digest = target.get("text_hash")
        if digest:
            tombstones.append({
                "deletion_id": deletion_id,
                "type": "text",
                "text_hash": digest,
                "at": now,
            })
    for episode_id in record.get("episode_ids") or ():
        episode_id = _bounded_text(episode_id, MAX_ID_CHARS)
        if episode_id:
            tombstones.append({
                "deletion_id": deletion_id,
                "type": EPISODE_TARGET_TYPE,
                "episode_id": episode_id,
                "at": now,
            })
    for key in record.get("source_keys") or ():
        if is_valid_source_key(key):
            tombstones.append({
                "deletion_id": deletion_id,
                "type": "source_key",
                "source_key": key,
                "at": now,
            })
    return tombstones


def _plugin_ids_from_keys(source_keys: Sequence[str]) -> list[str]:
    plugin_ids: list[str] = []
    for key in source_keys:
        plugin_id = plugin_id_of(key)
        if plugin_id and plugin_id not in plugin_ids:
            plugin_ids.append(plugin_id)
    return plugin_ids


def _plugin_ids_from_episodes(episode_ids: Sequence[str]) -> list[str]:
    """Extract plugin ids from \`ep:plugin:<plugin_id>:<session_id>\` episodes."""
    plugin_ids: list[str] = []
    for episode_id in episode_ids or ():
        parts = str(episode_id or "").strip().split(":")
        if len(parts) < 3 or parts[0] != "ep" or parts[1] != "plugin":
            continue
        plugin_id = _bounded_text(parts[2], 64)
        if plugin_id and plugin_id not in plugin_ids:
            plugin_ids.append(plugin_id)
    return plugin_ids


def _target_ids_of(record: Mapping[str, Any]) -> set[str]:
    """Entry ids a deletion addresses (targets plus resolved targets)."""
    out: set[str] = set()
    for item in list(record.get("targets") or ()) + list(
        record.get("resolved_targets") or (),
    ):
        if not isinstance(item, Mapping):
            continue
        target_id = item.get("id")
        if isinstance(target_id, str) and target_id:
            out.add(target_id)
    return out


def _scrub_target_texts(record: dict[str, Any]) -> None:
    """Drop temporary derived texts once the plugin copy and host content are done."""
    plugin_step = (record.get("steps") or {}).get("plugin") or {}
    plugin_ids = list(record.get("plugin_ids") or [])
    receipts = record.get("plugin_receipts") or {}
    all_cleaned = bool(plugin_ids) and all(
        (receipts.get(plugin_id) or {}).get("status") == "cleaned"
        for plugin_id in plugin_ids
    )
    if record.get("content_cleaned") and (
        plugin_step.get("status") in TERMINAL_STEP_STATUSES or all_cleaned
    ):
        record["target_texts"] = []


# -- normalization of external specs ----------------------------------------


def normalize_spec(spec: Any, *, now: str) -> dict[str, Any] | None:
    """Collapse an external delete request into the stored spec (host rules).

    The deterministic \`deletion_id\` makes a retried request a duplicate: the
    same scope/targets/source keys/subject always map to the same id. Returns
    None when nothing addressable was requested (never register an empty
    deletion that looks like it deleted something).
    """
    if not isinstance(spec, Mapping):
        return None
    scope = _bounded_text(spec.get("scope"), 32).lower() or "entry"
    if scope not in ("entry", "episode"):
        return None
    targets: list[dict[str, Any]] = []
    seen_targets: set[tuple[str, str]] = set()
    raw_targets = spec.get("targets")
    raw_target_list = (
        list(raw_targets)[:MAX_TARGETS]
        if isinstance(raw_targets, Sequence)
        and not isinstance(raw_targets, (str, bytes, Mapping))
        else []
    )
    for item in raw_target_list:
        if not isinstance(item, Mapping):
            continue
        target_type = _bounded_text(item.get("type"), 32).lower()
        target_id = _bounded_text(item.get("id"), MAX_ID_CHARS)
        if target_type not in ALL_TARGET_TYPES or not target_id:
            continue
        identity = (target_type, target_id)
        if identity in seen_targets:
            continue
        seen_targets.add(identity)
        target: dict[str, Any] = {"type": target_type, "id": target_id}
        digest = _bounded_text(item.get("text_hash"), 64)
        if digest:
            if len(digest) != 64:
                digest = ""
            else:
                target["text_hash"] = digest
        targets.append(target)
    source_keys: list[str] = []
    for key in _bounded_id_list(spec.get("source_keys"), limit=MAX_SOURCE_KEYS, max_chars=600):
        if is_valid_source_key(key) and key not in source_keys:
            source_keys.append(key)
    episode_ids: list[str] = []
    for value in _bounded_id_list(
        spec.get("episode_ids"), limit=MAX_EPISODES, max_chars=MAX_ID_CHARS,
    ):
        if value not in episode_ids:
            episode_ids.append(value)
    single_episode = _bounded_text(spec.get("episode_id"), MAX_ID_CHARS)
    if single_episode and single_episode not in episode_ids:
        episode_ids.insert(0, single_episode)
    for target in targets:
        if target["type"] == EPISODE_TARGET_TYPE and target["id"] not in episode_ids:
            episode_ids.append(target["id"])
    episode_ids = episode_ids[:MAX_EPISODES]
    # The receipt explicitly marks episode targets so the deleted range can be
    # checked from the receipt alone (host P4 contract).
    for episode_id in episode_ids:
        identity = (EPISODE_TARGET_TYPE, episode_id)
        if identity in seen_targets or len(targets) >= MAX_TARGETS:
            continue
        seen_targets.add(identity)
        targets.append({"type": EPISODE_TARGET_TYPE, "id": episode_id})
    subject = _bounded_text(spec.get("subject"), 256) or "private"
    reason = _bounded_text(spec.get("reason"), MAX_DETAIL_CHARS)
    deletion_id = _bounded_text(spec.get("deletion_id"), 120)
    if not deletion_id:
        seed = json.dumps(
            {
                "scope": scope,
                "targets": [[target["type"], target["id"]] for target in targets],
                "episode_ids": episode_ids,
                "source_keys": source_keys,
                "subject": subject,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        deletion_id = "del_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    target_texts: list[dict[str, str]] = []
    for item in raw_target_list:
        if not isinstance(item, Mapping):
            continue
        target_type = _bounded_text(item.get("type"), 32).lower()
        target_id = _bounded_text(item.get("id"), MAX_ID_CHARS)
        text = _bounded_text(item.get("text"), MAX_TEXT_CHARS)
        if not text or (target_type, target_id) not in seen_targets:
            continue
        if len(target_texts) >= MAX_TARGET_TEXTS:
            break
        target_texts.append({"type": target_type, "id": target_id, "text": text})
    if not targets and not source_keys and not episode_ids:
        return None
    return {
        "deletion_id": deletion_id,
        "scope": scope,
        "targets": targets,
        "episode_ids": episode_ids,
        "source_keys": source_keys,
        "subject": subject,
        "reason": reason,
        "created_at": _bounded_text(spec.get("created_at"), 40) or now,
        "target_texts": target_texts,
    }


@dataclass(frozen=True)
class PreparedRegistration:
    """One registration attempt; \`payload\` is the journal snapshot to append."""

    deletion_id: str
    version: int
    payload: Mapping[str, Any] | None = None
    duplicate: bool = False
    conflict: bool = False
    target_texts: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class BarrierIndex:
    """What the barrier needs from one store snapshot (IDs/hashes only)."""

    version: int = 0
    source_keys: frozenset[str] = frozenset()
    text_hashes: frozenset[str] = frozenset()
    episode_ids: frozenset[str] = frozenset()
    target_ids: Mapping[str, frozenset[str]] = field(default_factory=dict)
    deletion_ids: frozenset[str] = frozenset()

    def blocks_target(self, target_type: str, target_id: str) -> bool:
        return target_id in self.target_ids.get(target_type, frozenset())


class DeletionStore:
    """Per-character \`memory_deletions.json\` with a strict read gate."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
    ) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        #: `version()` is called on every barrier predicate; run the strict
        #: parser once per unchanged file signature instead of once per call.
        self._versions = VersionReader(store, self._parse_version)

    # -- paths -----------------------------------------------------------

    def _path(self, character: str) -> Path:
        return self._roots.character_dir(character, create=False).joinpath(
            DELETIONS_FILENAME,
        )

    # -- document IO -----------------------------------------------------

    def load_view(self, character: str) -> dict[str, Any]:
        """Load the document as a mutable view; never writes."""
        path = self._path(character)
        with _lock_for(path):
            raw = self._store.load(path)
            if raw is None:
                return _empty_doc()
            return _normalize_doc(raw, path)

    def save_view(self, character: str, view: Mapping[str, Any]) -> None:
        """Persist a journal view (validated, rotated, permit-checked)."""
        path = self._path(character)
        with _lock_for(path):
            doc = _normalize_doc(view, path)
            self._permit.assert_writable("save_deletions", str(path))
            self._store.save(path, {
                "schema": DELETIONS_SCHEMA,
                "version": int(doc.get("version") or 0),
                "deletions": list(doc.get("deletions") or []),
                "tombstones": list(doc.get("tombstones") or []),
            })
            self._versions.invalidate(path)

    def version(self, character: str) -> int:
        """Current monotonic document version; 0 for a fresh store.

        The document is parsed with the same strict `_normalize_doc` as every
        other read (so corruption and unknown schemas still fail closed), but
        the result is reused while the file signature is unchanged: the barrier
        asks for this version on every predicate, and re-parsing a 28 KB store
        ~950 times per projection read dominated the request time.
        """
        return self._versions.version(self._path(character))

    @staticmethod
    def _parse_version(raw: Any, path: Path) -> int:
        return int(_normalize_doc(raw, path).get("version") or 0)

    def snapshot(self, character: str) -> dict[str, Any]:
        """Read-only copy of the whole document (for diagnostics/tests)."""
        return json.loads(json.dumps(self.load_view(character)))

    # -- registration ----------------------------------------------------

    def prepare_registration(self, character: str, spec: Any) -> PreparedRegistration:
        """Validate a request and build the replay payload (no write yet).

        The caller appends the payload to the journal and applies it through
        \`apply_snapshot\` + \`save_view\`; doing it in that order makes the
        barrier durable exactly once even if the process dies in between.
        """
        now = _now_iso(self._clock)
        normalized = normalize_spec(spec, now=now)
        if normalized is None:
            raise ValueError("deletion request has no addressable target")
        path = self._path(character)
        with _lock_for(path):
            raw = self._store.load(path)
            doc = _empty_doc() if raw is None else _normalize_doc(raw, path)
            existing = _find_record(doc, normalized["deletion_id"])
            if existing is not None:
                if _fingerprint(existing) != _fingerprint(normalized):
                    return PreparedRegistration(
                        deletion_id=normalized["deletion_id"],
                        version=int(doc.get("version") or 0),
                        conflict=True,
                    )
                return PreparedRegistration(
                    deletion_id=normalized["deletion_id"],
                    version=int(doc.get("version") or 0),
                    duplicate=True,
                )
            record = self._new_record(normalized, now=now)
            tombstones = _tombstones_for_record(record, now=now)
            return PreparedRegistration(
                deletion_id=normalized["deletion_id"],
                version=int(doc.get("version") or 0) + 1,
                payload={"record": _record_payload(record), "tombstones": tombstones},
                target_texts=tuple(
                    dict(item) for item in normalized.get("target_texts") or ()
                ),
            )

    def _new_record(self, normalized: Mapping[str, Any], *, now: str) -> dict[str, Any]:
        plugin_ids = _plugin_ids_from_keys(normalized["source_keys"])
        for plugin_id in _plugin_ids_from_episodes(normalized["episode_ids"]):
            if plugin_id not in plugin_ids and len(plugin_ids) < MAX_PLUGIN_IDS:
                plugin_ids.append(plugin_id)
        record: dict[str, Any] = {
            "deletion_id": normalized["deletion_id"],
            "scope": normalized["scope"],
            "subject": normalized["subject"],
            "reason": normalized["reason"],
            "created_at": normalized["created_at"],
            "updated_at": now,
            "status": "pending",
            "targets": [dict(item) for item in normalized["targets"]],
            "episode_ids": list(normalized["episode_ids"]),
            "source_keys": list(normalized["source_keys"]),
            "plugin_source_keys": list(normalized["source_keys"]),
            "plugin_ids": plugin_ids,
            "target_texts": [dict(item) for item in normalized["target_texts"]],
            "plugin_receipts": {},
            "steps": {
                "barrier": {"status": "completed", "at": now},
                "resolve": {"status": "pending"},
                "facts": {"status": "pending"},
                "reflections": {"status": "pending"},
                "persona": {"status": "pending"},
                "recent": {"status": "pending"},
                "cache": {"status": "pending"},
                # With plugin copies to clean the step waits for receipts; the
                # temporary target texts must survive until then.
                "plugin": {"status": "pending" if plugin_ids else "skipped"},
            },
        }
        _recompute_record(record)
        return record

    def apply_snapshot(
        self, view: dict[str, Any], payload: Any,
    ) -> bool:
        """Idempotently merge a replay payload into a loaded view; returns changed.

        Used both by the live path (after the journal append) and by the
        replay handler. Never raises for a stale/duplicate snapshot: the
        barrier must keep the *newest* state, so an older \`updated_at\` cannot
        roll a record back.
        """
        if not isinstance(view, dict) or not isinstance(payload, Mapping):
            return False
        record_raw = payload.get("record")
        if not isinstance(record_raw, Mapping):
            return False
        try:
            record = _normalize_record(record_raw, Path("<payload>"))
        except CorruptStoreError:
            return False
        tombstones: list[dict[str, Any]] = []
        tombstones_raw = payload.get("tombstones")
        if isinstance(tombstones_raw, list):
            for row in tombstones_raw:
                try:
                    tombstones.append(_normalize_tombstone(row, Path("<payload>")))
                except CorruptStoreError:
                    continue
        changed = False
        existing = _find_record(view, str(record["deletion_id"]))
        if existing is None:
            view.setdefault("deletions", []).append(record)
            changed = True
        elif str(existing.get("updated_at") or "") <= str(record.get("updated_at") or ""):
            if existing != record:
                view["deletions"] = [
                    record if item is existing else item
                    for item in view.get("deletions") or []
                ]
                changed = True
        existing_keys = {
            _tombstone_key(item)
            for item in view.get("tombstones") or ()
            if isinstance(item, Mapping)
        }
        for tombstone in tombstones:
            key = _tombstone_key(tombstone)
            if key in existing_keys:
                continue
            view.setdefault("tombstones", []).append(tombstone)
            existing_keys.add(key)
            changed = True
        if not changed:
            return False
        view["version"] = int(view.get("version") or 0) + 1
        _rotate_doc(view)
        return True

    def replay_snapshot(self, character: str, payload: Any) -> bool:
        """Journal replay handler: load → apply → save; idempotent."""
        path = self._path(character)
        with _lock_for(path):
            raw = self._store.load(path)
            view = _empty_doc() if raw is None else _normalize_doc(raw, path)
            if not self.apply_snapshot(view, payload):
                return False
            self._permit.assert_writable("replay_deletions", str(path))
            self._store.save(path, {
                "schema": DELETIONS_SCHEMA,
                "version": int(view.get("version") or 0),
                "deletions": list(view.get("deletions") or []),
                "tombstones": list(view.get("tombstones") or []),
            })
            self._versions.invalidate(path)
            return True

    def add_target_texts(
        self, character: str, deletion_id: str, items: Sequence[Mapping[str, Any]],
    ) -> None:
        """Add bounded derived texts for plugin cleanup (best-effort)."""
        path = self._path(character)
        with _lock_for(path):
            doc = self._load_doc(character)
            record = _require_record(doc, deletion_id, path)
            current = record.get("target_texts") or []
            seen = {
                (item.get("type"), item.get("id"), item.get("text"))
                for item in current if isinstance(item, Mapping)
            }
            changed = False
            for item in list(items or ())[:MAX_TARGET_TEXTS]:
                if not isinstance(item, Mapping):
                    continue
                text = _bounded_text(item.get("text"), MAX_TEXT_CHARS)
                target_type = _bounded_text(item.get("type"), 32)
                target_id = _bounded_text(item.get("id"), MAX_ID_CHARS)
                if not text or len(current) >= MAX_TARGET_TEXTS:
                    continue
                identity = (target_type, target_id, text)
                if identity in seen:
                    continue
                seen.add(identity)
                current.append({"type": target_type, "id": target_id, "text": text})
                changed = True
            if not changed:
                return
            record["target_texts"] = current
            record["updated_at"] = _now_iso(self._clock)
            doc["version"] = int(doc.get("version") or 0) + 1
            self._save_doc(character, doc)

    def set_plugin_sources(
        self, character: str, deletion_id: str, source_keys: Sequence[str],
        *, notify_plugins: bool = True,
    ) -> None:
        """Fence the source history and optionally register original copies for cleanup."""
        keys = [
            key for key in _bounded_id_list(
                source_keys, limit=MAX_SOURCE_KEYS, max_chars=600,
            )
            if is_valid_source_key(key)
        ]
        if not keys:
            return
        path = self._path(character)
        with _lock_for(path):
            doc = self._load_doc(character)
            record = _require_record(doc, deletion_id, path)
            current = list(record.get("plugin_source_keys") or [])
            changed = False
            for key in keys:
                if key not in current and len(current) < MAX_SOURCE_KEYS:
                    current.append(key)
                    changed = True
            plugin_ids = list(record.get("plugin_ids") or [])
            if notify_plugins:
                for plugin_id in _plugin_ids_from_keys(current):
                    if plugin_id not in plugin_ids and len(plugin_ids) < MAX_PLUGIN_IDS:
                        plugin_ids.append(plugin_id)
                        changed = True
            if not changed:
                return
            record["plugin_source_keys"] = current
            record["plugin_ids"] = plugin_ids
            record["updated_at"] = _now_iso(self._clock)
            doc["version"] = int(doc.get("version") or 0) + 1
            self._save_doc(character, doc)

    def add_plugin_ids(
        self, character: str, deletion_id: str, plugin_ids: Sequence[str],
    ) -> list[str]:
        """Record extra plugins that own a copy of this deletion's targets.

        QP05: a fact created on the web has no plugin segment in its source key,
        so it can only be traced to a plugin through the consumer references
        that plugin registered when it recalled the fact. Those plugin ids are
        appended here — never inserted — so the existing order is preserved and
        a retried call is a no-op (the same deletion still notifies the same
        plugin once).
        """
        wanted = [
            text for text in (
                _bounded_text(item, 64) for item in list(plugin_ids or ())
            ) if text
        ]
        if not wanted:
            return []
        path = self._path(character)
        with _lock_for(path):
            doc = self._load_doc(character)
            record = _require_record(doc, deletion_id, path)
            current = list(record.get("plugin_ids") or [])
            added: list[str] = []
            for plugin_id in wanted:
                if plugin_id in current or len(current) >= MAX_PLUGIN_IDS:
                    continue
                current.append(plugin_id)
                added.append(plugin_id)
            if not added:
                return []
            record["plugin_ids"] = current
            # A plugin discovered now still has to clean its copy: a step that
            # was skipped (no plugin at registration) or completed (an earlier
            # plugin answered) must become pending again.
            steps = record.setdefault("steps", {})
            plugin_step = steps.get("plugin") or {}
            if plugin_step.get("status") in TERMINAL_STEP_STATUSES:
                steps["plugin"] = {"status": "pending", "at": _now_iso(self._clock)}
            record["updated_at"] = _now_iso(self._clock)
            doc["version"] = int(doc.get("version") or 0) + 1
            _recompute_record(record)
            self._save_doc(character, doc)
            return added

    def plugin_target_ids(
        self, character: str, deletion_id: str,
    ) -> list[str]:
        """Entry ids a deletion addresses (targets plus resolved targets)."""
        doc = self.load_view(character)
        record = _find_record(doc, str(deletion_id))
        if record is None:
            return []
        return sorted(_target_ids_of(record))

    def set_resolved_targets(
        self,
        character: str,
        deletion_id: str,
        targets: Sequence[Mapping[str, Any]] | None = None,
        *,
        episode_ids: Sequence[str] | None = None,
        text_hashes: Sequence[str] | None = None,
    ) -> dict[str, int]:
        """Persist resolution results as tombstones (IDs/episodes/hashes only).

        Needed for episode deletion and retry: the entries an episode contains
        are only known after resolution, and a retry must not depend on
        resolving them again. Idempotent; returns the added counts.
        """
        new_targets: list[dict[str, Any]] = []
        for item in list(targets or ())[:MAX_TARGETS]:
            if not isinstance(item, Mapping):
                continue
            target_type = _bounded_text(item.get("type"), 32).lower()
            target_id = _bounded_text(item.get("id"), MAX_ID_CHARS)
            if target_type in TARGET_TYPES and target_id:
                new_targets.append({"type": target_type, "id": target_id})
        new_episodes = _bounded_id_list(
            episode_ids, limit=MAX_EPISODES, max_chars=MAX_ID_CHARS,
        )
        new_hashes = [
            digest for digest in _bounded_id_list(
                text_hashes, limit=MAX_RESOLVED_TEXT_HASHES, max_chars=64,
            )
            if len(digest) == 64
        ]
        if not new_targets and not new_episodes and not new_hashes:
            return {"added_targets": 0, "added_episodes": 0, "added_text_hashes": 0}
        path = self._path(character)
        with _lock_for(path):
            doc = self._load_doc(character)
            record = _require_record(doc, deletion_id, path)
            now = _now_iso(self._clock)
            existing_targets = {
                (item.get("type"), item.get("id"))
                for item in list(record.get("targets") or [])
                + list(record.get("resolved_targets") or [])
                if isinstance(item, Mapping)
            }
            existing_episodes = set(record.get("episode_ids") or ())
            added_targets = 0
            added_target_list: list[dict[str, Any]] = []
            for target in new_targets:
                identity = (target["type"], target["id"])
                if identity in existing_targets:
                    continue
                if len(record.get("resolved_targets") or []) >= MAX_TARGETS:
                    break
                record.setdefault("resolved_targets", []).append(dict(target))
                added_target_list.append(dict(target))
                existing_targets.add(identity)
                added_targets += 1
            added_episodes = 0
            for episode_id in new_episodes:
                if episode_id in existing_episodes:
                    continue
                if len(record.get("episode_ids") or []) >= MAX_EPISODES:
                    break
                record.setdefault("episode_ids", []).append(episode_id)
                existing_episodes.add(episode_id)
                added_episodes += 1
            tombstones = doc.setdefault("tombstones", [])
            existing_keys = {
                _tombstone_key(item)
                for item in tombstones
                if isinstance(item, Mapping)
            }
            for target in added_target_list:
                tombstone = {
                    "deletion_id": str(deletion_id),
                    "type": target["type"],
                    "id": target["id"],
                    "at": now,
                }
                key = _tombstone_key(tombstone)
                if key not in existing_keys:
                    tombstones.append(tombstone)
                    existing_keys.add(key)
            for episode_id in new_episodes:
                tombstone = {
                    "deletion_id": str(deletion_id),
                    "type": EPISODE_TARGET_TYPE,
                    "episode_id": episode_id,
                    "at": now,
                }
                key = _tombstone_key(tombstone)
                if key not in existing_keys:
                    tombstones.append(tombstone)
                    existing_keys.add(key)
            added_hashes = 0
            for digest in new_hashes:
                tombstone = {
                    "deletion_id": str(deletion_id),
                    "type": "text",
                    "text_hash": digest,
                    "at": now,
                }
                key = _tombstone_key(tombstone)
                if key in existing_keys:
                    continue
                tombstones.append(tombstone)
                existing_keys.add(key)
                added_hashes += 1
            if not added_targets and not added_episodes and not added_hashes:
                return {"added_targets": 0, "added_episodes": 0, "added_text_hashes": 0}
            record["updated_at"] = now
            doc["version"] = int(doc.get("version") or 0) + 1
            _recompute_record(record)
            _scrub_target_texts(record)
            _rotate_doc(doc)
            self._save_doc(character, doc)
            return {
                "added_targets": added_targets,
                "added_episodes": added_episodes,
                "added_text_hashes": added_hashes,
            }

    # -- step and receipt updates ---------------------------------------

    def update_step(
        self,
        character: str,
        deletion_id: str,
        step: str,
        status: str,
        detail: Any = None,
    ) -> dict[str, Any]:
        """Set one step's status; the same status is idempotent (no version bump)."""
        if step not in STEP_NAMES:
            raise ValueError(f"unknown deletion step {step!r}")
        if status not in STEP_STATUSES:
            raise ValueError(f"unknown step status {status!r}")
        path = self._path(character)
        with _lock_for(path):
            doc = self._load_doc(character)
            record = _require_record(doc, deletion_id, path)
            steps = record.setdefault("steps", {})
            current = steps.get(step) or {"status": "pending"}
            detail_payload = _bounded_detail(detail)
            if step == "plugin" and status == "pending" and detail_payload:
                plugin_ids = [
                    text for text in (
                        _bounded_text(item, 64)
                        for item in list(detail_payload.get("plugin_ids") or ())
                    ) if text
                ]
                if plugin_ids:
                    record["plugin_ids"] = plugin_ids[:MAX_PLUGIN_IDS]
            if current.get("status") == status:
                if detail_payload and current.get("detail") != detail_payload:
                    current["detail"] = detail_payload
                    current["at"] = _now_iso(self._clock)
                    record["updated_at"] = current["at"]
                    doc["version"] = int(doc.get("version") or 0) + 1
                    _recompute_record(record)
                    _scrub_target_texts(record)
                    self._save_doc(character, doc)
                return _public_record(record, int(doc.get("version") or 0))
            now = _now_iso(self._clock)
            updated: dict[str, Any] = {"status": status, "at": now}
            if detail_payload:
                updated["detail"] = detail_payload
            steps[step] = updated
            record["updated_at"] = now
            doc["version"] = int(doc.get("version") or 0) + 1
            _recompute_record(record)
            _scrub_target_texts(record)
            self._save_doc(character, doc)
            return _public_record(record, int(doc.get("version") or 0))

    def record_plugin_receipt(
        self,
        character: str,
        deletion_id: str,
        plugin_id: str,
        status: str,
        cleaned: Sequence[str] | None = None,
        failed: Sequence[str] | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        """Record one plugin copy's cleanup receipt (idempotent per plugin)."""
        plugin_text = _bounded_text(plugin_id, 64)
        receipt_status = _bounded_text(status, 16).lower()
        if not plugin_text or receipt_status not in PLUGIN_RECEIPT_STATUSES:
            raise ValueError("invalid plugin receipt")
        cleaned_list = _bounded_id_list(cleaned, limit=32, max_chars=120)
        failed_list = _bounded_id_list(failed, limit=32, max_chars=120)
        path = self._path(character)
        with _lock_for(path):
            doc = self._load_doc(character)
            record = _require_record(doc, deletion_id, path)
            receipts = record.setdefault("plugin_receipts", {})
            existing = receipts.get(plugin_text)
            if (
                isinstance(existing, Mapping)
                and existing.get("status") == receipt_status
                and list(existing.get("cleaned") or []) == cleaned_list
                and list(existing.get("failed") or []) == failed_list
            ):
                return _public_record(record, int(doc.get("version") or 0))
            receipt: dict[str, Any] = {
                "status": receipt_status,
                "cleaned": cleaned_list,
                "failed": failed_list,
                "at": _now_iso(self._clock),
            }
            detail_text = _bounded_text(detail, MAX_DETAIL_CHARS)
            if detail_text:
                receipt["detail"] = detail_text
            receipts[plugin_text] = receipt
            plugin_ids = list(record.get("plugin_ids") or [])
            if plugin_text not in plugin_ids and len(plugin_ids) < MAX_PLUGIN_IDS:
                plugin_ids.append(plugin_text)
            record["plugin_ids"] = plugin_ids
            steps = record.setdefault("steps", {})
            if receipt_status == "failed":
                steps["plugin"] = {
                    "status": "failed",
                    "at": _now_iso(self._clock),
                    "detail": {"plugin_id": plugin_text, "failed": failed_list[:8]},
                }
            elif all(
                (receipts.get(pid) or {}).get("status") == "cleaned"
                for pid in plugin_ids
            ):
                steps["plugin"] = {
                    "status": "completed",
                    "at": _now_iso(self._clock),
                    "detail": {"plugin_ids": plugin_ids},
                }
            else:
                steps["plugin"] = {"status": "pending", "at": _now_iso(self._clock)}
            record["updated_at"] = _now_iso(self._clock)
            doc["version"] = int(doc.get("version") or 0) + 1
            _recompute_record(record)
            _scrub_target_texts(record)
            self._save_doc(character, doc)
            return _public_record(record, int(doc.get("version") or 0))

    # -- reads -----------------------------------------------------------

    def get(self, character: str, deletion_id: str) -> dict[str, Any] | None:
        """Receipt projection of one deletion; None when unknown."""
        doc = self.load_view(character)
        record = _find_record(doc, str(deletion_id))
        if record is None:
            return None
        return _public_record(record, int(doc.get("version") or 0))

    def list(
        self,
        character: str,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Receipt projections, newest first, plus the pending plugin queue."""
        if limit < 0:
            raise ValueError("limit must be nonnegative")
        doc = self.load_view(character)
        version = int(doc.get("version") or 0)
        items: list[dict[str, Any]] = []
        for record in doc.get("deletions") or ():
            if status is not None and record.get("status") != status:
                continue
            items.append(_public_record(record, version))
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        pending_plugins = [
            _pending_plugin_item(record, version)
            for record in doc.get("deletions") or ()
            if (record.get("steps") or {}).get("plugin", {}).get("status") == "pending"
        ]
        return {
            "version": version,
            "items": items[:limit],
            "plugin_pending": pending_plugins[:limit],
        }

    def pending_plugin_deletions(
        self,
        character: str,
        plugin_id: str,
        limit: int = 8,
        memory_ids: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Deletions this plugin still has to clean (failed ones retry too).

        Two ways one deletion can concern this plugin:

        * the record already names it (`plugin_ids`, derived from the source
          keys/episodes or appended when a consumer reference was found) — the
          original rule, unchanged;
        * QP05: `memory_ids` are the ids this plugin is known to have consumed
          (recorded by `/v1/context/reserve`) and they intersect the deletion's
          targets. A fact created on the web carries no plugin segment, so this
          is the only way the plugin that injected it can hear about the
          deletion.

        Each record is emitted at most once, and the result order stays the
        store order, so a plugin that was already notified keeps its place.
        """
        plugin_text = _bounded_text(plugin_id, 64)
        if not plugin_text:
            return []
        cap = max(1, min(int(limit or 8), 32))
        wanted_ids = {
            text for text in (_bounded_text(item, MAX_ID_CHARS) for item in memory_ids) if text
        }
        doc = self.load_view(character)
        version = int(doc.get("version") or 0)
        out: list[dict[str, Any]] = []
        for record in doc.get("deletions") or ():
            resolved = (record.get("steps") or {}).get("resolve") or {}
            if resolved.get("status") not in TERMINAL_STEP_STATUSES:
                continue
            listed = plugin_text in (record.get("plugin_ids") or [])
            consumer_matched = False
            if not listed and wanted_ids:
                consumer_matched = bool(
                    wanted_ids.intersection(_target_ids_of(record)),
                )
            if not listed and not consumer_matched:
                continue
            receipt = (record.get("plugin_receipts") or {}).get(plugin_text) or {}
            if receipt.get("status") == "cleaned":
                continue
            if listed:
                plugin_step = (record.get("steps") or {}).get("plugin") or {}
                if plugin_step.get("status") in TERMINAL_STEP_STATUSES:
                    continue
            # A consumer-only match is not part of the recorded plugin set yet:
            # its copy still exists even after the registered plugins answered,
            # so it keeps its own turn (its ack records the receipt).
            out.append(_pending_plugin_item(record, version))
            if len(out) >= cap:
                break
        return out

    def event_payload(self, character: str, deletion_id: str) -> dict[str, Any] | None:
        """Journal replay payload for one deletion (no original text)."""
        doc = self.load_view(character)
        record = _find_record(doc, str(deletion_id))
        if record is None:
            return None
        tombstones = [
            dict(item)
            for item in doc.get("tombstones") or ()
            if isinstance(item, Mapping) and item.get("deletion_id") == str(deletion_id)
        ]
        return {"record": _record_payload(record), "tombstones": tombstones}

    def barrier_index(self, character: str) -> BarrierIndex:
        """Load the barrier view: version, tombstone sets and target ids."""
        doc = self.load_view(character)
        source_keys: set[str] = set()
        text_hashes: set[str] = set()
        episode_ids: set[str] = set()
        target_ids: dict[str, set[str]] = {name: set() for name in TARGET_TYPES}
        deletion_ids: set[str] = set()
        for tombstone in doc.get("tombstones") or ():
            if not isinstance(tombstone, Mapping):
                continue
            deletion_id = tombstone.get("deletion_id")
            if isinstance(deletion_id, str) and deletion_id:
                deletion_ids.add(deletion_id)
            tombstone_type = tombstone.get("type")
            if tombstone_type == "source_key":
                key = tombstone.get("source_key")
                if isinstance(key, str) and key:
                    source_keys.add(key)
            elif tombstone_type == "text":
                digest = tombstone.get("text_hash")
                if isinstance(digest, str) and digest:
                    text_hashes.add(digest)
            elif tombstone_type == EPISODE_TARGET_TYPE:
                episode_id = tombstone.get("episode_id")
                if isinstance(episode_id, str) and episode_id:
                    episode_ids.add(episode_id)
            elif tombstone_type in target_ids:
                target_id = tombstone.get("id")
                if isinstance(target_id, str) and target_id:
                    target_ids[tombstone_type].add(target_id)
        return BarrierIndex(
            version=int(doc.get("version") or 0),
            source_keys=frozenset(source_keys),
            text_hashes=frozenset(text_hashes),
            episode_ids=frozenset(episode_ids),
            target_ids={name: frozenset(ids) for name, ids in target_ids.items()},
            deletion_ids=frozenset(deletion_ids),
        )

    # -- internals -------------------------------------------------------

    def _load_doc(self, character: str) -> dict[str, Any]:
        path = self._path(character)
        raw = self._store.load(path)
        if raw is None:
            return _empty_doc()
        return _normalize_doc(raw, path)

    def _save_doc(self, character: str, doc: Mapping[str, Any]) -> None:
        path = self._path(character)
        self._permit.assert_writable("save_deletions", str(path))
        self._store.save(path, {
            "schema": DELETIONS_SCHEMA,
            "version": int(doc.get("version") or 0),
            "deletions": list(doc.get("deletions") or []),
            "tombstones": list(doc.get("tombstones") or []),
        })
        self._versions.invalidate(path)


def _find_record(doc: Mapping[str, Any], deletion_id: str) -> dict[str, Any] | None:
    for record in doc.get("deletions") or ():
        if isinstance(record, dict) and record.get("deletion_id") == deletion_id:
            return record
    return None


def _require_record(
    doc: Mapping[str, Any], deletion_id: str, path: Path,
) -> dict[str, Any]:
    record = _find_record(doc, str(deletion_id))
    if record is None:
        raise DeletionNotFoundError(
            f"unknown deletion {deletion_id!r}", path=path,
        )
    return record


def _pending_plugin_item(record: Mapping[str, Any], version: int) -> dict[str, Any]:
    texts: list[str] = []
    for item in (record.get("target_texts") or []):
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("text") or "")
        if text and text not in texts:
            texts.append(text)
    return {
        "deletion_id": record.get("deletion_id"),
        "version": int(version or 0),
        "scope": record.get("scope") or "entry",
        "source_keys": (list(record.get("plugin_source_keys") or [])
                        if record.get("source_keys") or record.get("scope") == "episode" else []),
        "episode_ids": list(record.get("episode_ids") or [])[:MAX_PENDING_EPISODE_IDS],
        "target_texts": texts,
        "memory_ids": list(dict.fromkeys(str(item["id"]) for item in
            list(record.get("targets") or ()) + list(record.get("resolved_targets") or ())
            if isinstance(item, Mapping) and item.get("id"))),
        "persona_versions": list(((record.get("steps") or {}).get("persona") or {}).get("detail", {}).get("invalidated_versions") or ()),
        "reason": record.get("reason") or "",
    }


__all__ = [
    "BarrierIndex",
    "CONTENT_STEPS",
    "DELETIONS_FILENAME",
    "DELETIONS_SCHEMA",
    "DeletionStore",
    "EPISODE_TARGET_TYPE",
    "MAX_DELETIONS",
    "MAX_EPISODES",
    "MAX_PLUGIN_IDS",
    "MAX_SOURCE_KEYS",
    "MAX_TARGETS",
    "MAX_TOMBSTONES",
    "PLUGIN_RECEIPT_STATUSES",
    "PreparedRegistration",
    "STEP_NAMES",
    "STEP_STATUSES",
    "TARGET_TYPES",
    "normalize_spec",
]
