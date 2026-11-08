"""Durable, body-free memory-consumer references (QP05).

`/v1/context/reserve` already pins one generated turn's identity and version
basis. This store adds the missing half of the deletion loop: *which plugin
session actually consumed which memory ids*. Nothing here is derived from the
fact's own source key — a fact created on the web has no plugin segment — so
without this table a QQ plugin that recalled and injected it can never be told
to clean its copy.

Shape per character (`memory_consumer_refs.json`), versioned like every other
core-owned store:

    {"schema_version": 1, "refs": [ ... ]}

One reference:

    {"plugin_id", "session_id", "request_id", "character", "subject",
     "memory_ids": [...], "source_ids": [...], "persona_version",
     "context_generation", "deletion_version", "reserved_at"}

Rules:

* the dedup key is `plugin_id + session_id + request_id`; the same key with the
  same content is a no-op, the same key with different content keeps the newer
  row (a retried reserve must not add a second reference);
* only ids, versions and timestamps are stored — never text, never a rendered
  prompt, so the table is safe to keep after the content is deleted;
* bounded like the other service-side tables (2048 rows): the oldest
  `reserved_at` is dropped first;
* a reference is retired only when its owning plugin *acknowledged* the cleanup
  (`prune_cleaned`) or by capacity. Retiring it any earlier would make the
  pending notification disappear before the copy is actually gone.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from .schema import load_versioned, wrap

CONSUMER_REFS_FILENAME = "memory_consumer_refs.json"
MAX_CONSUMER_REFS = 2048
MAX_REF_IDS = 64
MAX_ID_CHARS = 200
MAX_NAME_CHARS = 256
MAX_PLUGIN_CHARS = 64
MAX_VERSION_CHARS = 120

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


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _ids(value: Any, *, limit: int = MAX_REF_IDS) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    out: list[str] = []
    for item in list(value)[:limit]:
        text = _text(item, MAX_ID_CHARS)
        if text and text not in out:
            out.append(text)
    return out


def _key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("plugin_id") or ""),
        str(row.get("session_id") or ""),
        str(row.get("request_id") or ""),
    )


def normalize_ref(row: Any) -> dict[str, Any] | None:
    """Validate one client-supplied reference; None when it cannot be used.

    A reference without a plugin id is useless for the deletion loop (nothing
    to notify), so it is dropped instead of stored half-empty.
    """
    if not isinstance(row, Mapping):
        return None
    plugin_id = _text(row.get("plugin_id"), MAX_PLUGIN_CHARS)
    request_id = _text(row.get("request_id"), MAX_ID_CHARS)
    if not plugin_id or not request_id:
        return None
    generation = row.get("context_generation")
    if isinstance(generation, (int, float)) and not isinstance(generation, bool):
        generation = str(generation)
    return {
        "plugin_id": plugin_id,
        "session_id": _text(row.get("session_id"), MAX_ID_CHARS),
        "request_id": request_id,
        "character": _text(row.get("character"), MAX_NAME_CHARS),
        "subject": _text(row.get("subject"), MAX_NAME_CHARS) or "private",
        "memory_ids": _ids(row.get("memory_ids")),
        "source_ids": _ids(row.get("source_ids")),
        "persona_version": _text(row.get("persona_version"), MAX_VERSION_CHARS),
        "context_generation": _text(generation, MAX_VERSION_CHARS),
        "deletion_version": _as_int(row.get("deletion_version")),
        "phase": "reserved" if row.get("phase") == "reserved" else "adopted",
        "reserved_at": _text(row.get("reserved_at"), 40),
    }


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, int(value))


def _same(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return {key: value for key, value in left.items() if key != "reserved_at"} == {
        key: value for key, value in right.items() if key != "reserved_at"
    }


class ConsumerRefStore:
    """Idempotent, bounded record of which plugin session saw which memories."""

    def __init__(self, *, roots, store, clock, permit) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit

    # -- paths -----------------------------------------------------------

    def _path(self, character: str) -> Path:
        return self._roots.character_file(character, CONSUMER_REFS_FILENAME)

    # -- reads -----------------------------------------------------------

    def load(self, character: str) -> dict[str, Any]:
        path = self._path(character)
        document = load_versioned(
            self._store, path, supported=frozenset({1}), default={"refs": []},
        )
        rows = document.get("refs")
        if not isinstance(rows, list):
            rows = []
        cleaned = [row for row in (normalize_ref(item) for item in rows) if row]
        return {"refs": cleaned}

    def refs(self, character: str, *, plugin_id: str = "") -> list[dict[str, Any]]:
        wanted = _text(plugin_id, MAX_PLUGIN_CHARS)
        rows = self.load(character)["refs"]
        if not wanted:
            return rows
        return [row for row in rows if row["plugin_id"] == wanted]

    def plugin_ids_for(
        self, character: str, memory_ids: Sequence[str],
    ) -> list[str]:
        """Plugins whose recorded references intersect these memory ids."""
        wanted = {text for text in (_text(item, MAX_ID_CHARS) for item in memory_ids) if text}
        if not wanted:
            return []
        out: list[str] = []
        for row in self.load(character)["refs"]:
            if row["plugin_id"] in out:
                continue
            if wanted.intersection(row["memory_ids"]):
                out.append(row["plugin_id"])
        return out

    def memory_ids_for(self, character: str, plugin_id: str) -> list[str]:
        out: list[str] = []
        for row in self.refs(character, plugin_id=plugin_id):
            for item in row["memory_ids"]:
                if item not in out:
                    out.append(item)
        return out

    # -- writes ----------------------------------------------------------

    def record(self, character: str, row: Any) -> bool:
        """Store one reference; returns True when the table changed."""
        normalized = normalize_ref(row)
        if normalized is None:
            return False
        path = self._path(character)
        with _lock_for(path):
            document = self.load(character)
            rows = document["refs"]
            key = _key(normalized)
            existing = next((item for item in rows if _key(item) == key), None)
            if existing is not None and existing["phase"] == "adopted" and normalized["phase"] == "reserved":
                return False
            if existing is not None and _same(existing, normalized):
                return False
            if not normalized["reserved_at"]:
                normalized["reserved_at"] = self._clock.now().isoformat()
            if existing is None:
                rows.append(normalized)
            else:
                rows[rows.index(existing)] = normalized
            if len(rows) > MAX_CONSUMER_REFS:
                rows.sort(key=lambda item: str(item.get("reserved_at") or ""))
                del rows[: len(rows) - MAX_CONSUMER_REFS]
            self._permit.assert_writable("consumer_refs", str(path))
            self._store.save(path, wrap({"refs": rows}))
            return True

    def prune_cleaned(
        self, character: str, plugin_id: str, memory_ids: Sequence[str],
    ) -> int:
        """Retire the references an acknowledged deletion has made irrelevant.

        Only the ids the deletion names are removed: one reference may cover
        several memories, and the surviving ones must still be able to trigger
        a later cleanup. References left without memory ids are dropped whole.
        """
        wanted = _text(plugin_id, MAX_PLUGIN_CHARS)
        retired = {text for text in (_text(item, MAX_ID_CHARS) for item in memory_ids) if text}
        if not wanted or not retired:
            return 0
        path = self._path(character)
        with _lock_for(path):
            rows = self.load(character)["refs"]
            changed = 0
            kept: list[dict[str, Any]] = []
            for row in rows:
                if row["plugin_id"] != wanted:
                    kept.append(row)
                    continue
                remaining = [item for item in row["memory_ids"] if item not in retired]
                if len(remaining) == len(row["memory_ids"]):
                    kept.append(row)
                    continue
                changed += 1
                if remaining:
                    row = dict(row, memory_ids=remaining)
                    kept.append(row)
            if not changed:
                return 0
            self._permit.assert_writable("consumer_refs", str(path))
            self._store.save(path, wrap({"refs": kept}))
            return changed


__all__ = [
    "CONSUMER_REFS_FILENAME",
    "ConsumerRefStore",
    "MAX_CONSUMER_REFS",
    "MAX_REF_IDS",
    "normalize_ref",
]
