"""One read-side fence over the deletion/correction stores (B03).

Every component that can bring content back — journal replay, event ingest,
fact extraction (B04), reflections/persona (B05/B10), recall (B06) — asks this
barrier before writing or serving a row. Keeping the checks in one object is
what makes "deleted content never comes back" auditable instead of a set of
per-module conventions (design §9.3, §17.15).

The barrier reads three stores and caches their combined state:

* \`DeletionStore\` tombstones (source keys, text hashes, episode ids, entry ids)
  and its monotonic version;
* \`CorrectionStore\` version (in-flight results are discarded when it moves);
* \`ScopedTombstoneStore\` forget/erase epochs (scoped subjects).

Single-writer rule: the cache is refreshed by :meth:\`refresh\` after this
process writes a barrier, and lazily when a store version changed. A process
that only reads a data root another process writes must call \`refresh\` itself;
the host adapter owns that lifecycle (B08).

\`BarrierSnapshot\` is what background work captures before it starts: after
the expensive step it calls :meth:\`assert_fresh\`, and a moved deletion or
correction version means the result was derived from content the user has
since removed, so it must be dropped instead of committed.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..contracts import DeletionTarget, MemoryEvent
from ..errors import BarrierStaleError, ContentBlockedError
from ..invalidation import row_matches_subject, text_hash
from ..persistence.corrections import KIND_FACT_CORRECTION, CorrectionStore
from ..persistence.deletions import BarrierIndex, DeletionStore
from ..persistence.tombstones import ScopedTombstoneStore, TombstoneState


@dataclass(frozen=True)
class BarrierSnapshot:
    """Versions and epochs an in-flight task captured before doing work."""

    deletion_version: int = 0
    correction_version: int = 0
    epochs: Mapping[str, int] = field(default_factory=dict)

    def epoch(self, subject: str) -> int:
        return int(self.epochs.get(str(subject), 0))


@dataclass(frozen=True)
class BarrierState:
    """Cached combined barrier state for one character."""

    snapshot: BarrierSnapshot
    index: BarrierIndex
    tombstones: Mapping[str, TombstoneState]

    @property
    def deletion_version(self) -> int:
        return self.snapshot.deletion_version

    @property
    def correction_version(self) -> int:
        return self.snapshot.correction_version

    def forget_epoch(self, subject: str) -> int:
        row = self.tombstones.get(str(subject))
        return 0 if row is None else row.forget_epoch

    def erased_epoch(self, subject: str) -> int:
        row = self.tombstones.get(str(subject))
        return 0 if row is None else row.erased_epoch


def episode_ids_of(row: Mapping[str, Any]) -> set[str]:
    """Every episode id a stored row may carry (host stores several layers)."""
    out: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            out.add(value.strip())
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    out.add(item.strip())

    _add(row.get("episode_id"))
    _add(row.get("episode_ids"))
    metadata = row.get("metadata")
    if isinstance(metadata, Mapping):
        _add(metadata.get("episode_id"))
        _add(metadata.get("episode_ids"))
        companion = metadata.get("companion_event")
        if isinstance(companion, Mapping):
            _add(companion.get("episode_id"))
            _add(companion.get("episode_ids"))
    return out


def _infer_row_type(row: Mapping[str, Any]) -> str:
    for key in ("target_type", "_tier", "tier", "type"):
        value = row.get(key)
        if value in ("fact", "reflection", "persona"):
            return str(value)
    if "source_fact_ids" in row:
        return "reflection"
    if "source_id" in row or "merged_from_ids" in row:
        return "persona"
    return "fact"


def message_metadata(message: Any) -> Mapping[str, Any]:
    """Host message metadata: `data.metadata` first, then a top-level fallback."""
    if not isinstance(message, Mapping):
        return {}
    data = message.get("data")
    if isinstance(data, Mapping):
        metadata = data.get("metadata")
        if isinstance(metadata, Mapping):
            return metadata
    metadata = message.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def message_text(message: Any) -> str:
    """Text of a host message; multimodal content lists concatenate their parts."""
    if not isinstance(message, Mapping):
        return ""
    data = message.get("data")
    content = data.get("content") if isinstance(data, Mapping) else message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                value = part.get("text") or part.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts)
    return ""


def message_source_keys(message: Any) -> set[str]:
    """Every source key a host message carries (event and mirror)."""
    metadata = message_metadata(message)
    keys: set[str] = set()
    companion = metadata.get("companion_event")
    if isinstance(companion, Mapping):
        value = companion.get("source_key")
        if isinstance(value, str) and value.strip():
            keys.add(value.strip())
    mirror = metadata.get("mirror")
    if isinstance(mirror, Mapping):
        event = mirror.get("event")
        if isinstance(event, Mapping):
            value = event.get("source_key")
            if isinstance(value, str) and value.strip():
                keys.add(value.strip())
    value = metadata.get("source_key")
    if isinstance(value, str) and value.strip():
        keys.add(value.strip())
    return keys


def message_episode_ids(message: Any) -> set[str]:
    """Every episode id a host message carries (top-level and companion_event)."""
    metadata = message_metadata(message)
    out: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            out.add(value.strip())
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    out.add(item.strip())

    _add(metadata.get("episode_id"))
    _add(metadata.get("episode_ids"))
    companion = metadata.get("companion_event")
    if isinstance(companion, Mapping):
        _add(companion.get("episode_id"))
        _add(companion.get("episode_ids"))
    return out


class DeletionBarrier:
    """Combined deletion/correction/epoch fence with a per-character cache."""

    def __init__(
        self,
        *,
        deletions: DeletionStore,
        tombstones: ScopedTombstoneStore,
        corrections: CorrectionStore | None = None,
    ) -> None:
        self._deletions = deletions
        self._tombstones = tombstones
        self._corrections = corrections
        self._states: dict[str, BarrierState] = {}
        self._guard = threading.Lock()
        self._write_locks: dict[str, threading.RLock] = {}

    def write_lock(self, character: str) -> threading.RLock:
        """Serialize extraction, correction and deletion commits for one character."""
        with self._guard:
            lock = self._write_locks.get(character)
            if lock is None:
                lock = self._write_locks[character] = threading.RLock()
            return lock

    # -- state -----------------------------------------------------------

    def state(self, character: str) -> BarrierState:
        """Current combined state, reloaded when a store version moved."""
        with self._guard:
            cached = self._states.get(character)
        deletion_version = self._deletions.version(character)
        correction_version = (
            0 if self._corrections is None else self._corrections.version(character)
        )
        if (
            cached is not None
            and cached.deletion_version == deletion_version
            and cached.correction_version == correction_version
        ):
            return cached
        state = self._load_state(character, deletion_version, correction_version)
        with self._guard:
            self._states[character] = state
        return state

    def _load_state(
        self, character: str, deletion_version: int, correction_version: int,
    ) -> BarrierState:
        index = self._deletions.barrier_index(character)
        tombstone_rows = self._tombstones.snapshot(character)
        epochs = {
            subject: row.erased_epoch for subject, row in tombstone_rows.items()
        }
        snapshot = BarrierSnapshot(
            deletion_version=deletion_version,
            correction_version=correction_version,
            epochs=epochs,
        )
        return BarrierState(
            snapshot=snapshot, index=index, tombstones=dict(tombstone_rows),
        )

    def refresh(self, character: str) -> BarrierState:
        """Drop the cache and reload (call after this process wrote a barrier)."""
        with self._guard:
            self._states.pop(character, None)
        return self.state(character)

    def invalidate(self, character: str | None = None) -> None:
        """Drop cached state; \`None\` drops every character."""
        with self._guard:
            if character is None:
                self._states.clear()
            else:
                self._states.pop(character, None)

    # -- snapshots and in-flight freshness -------------------------------

    def snapshot(self, character: str) -> BarrierSnapshot:
        return self.state(character).snapshot

    def is_stale(self, character: str, snapshot: BarrierSnapshot) -> bool:
        """Whether deletion/correction moved past a captured snapshot."""
        state = self.state(character)
        if state.deletion_version != snapshot.deletion_version:
            return True
        if state.correction_version != snapshot.correction_version:
            return True
        subjects = set(snapshot.epochs) | set(state.tombstones)
        return any(
            state.erased_epoch(subject) > snapshot.epoch(subject)
            for subject in subjects
        )

    def assert_fresh(
        self, character: str, snapshot: BarrierSnapshot, *, subject: str | None = None,
    ) -> None:
        """Raise \`BarrierStaleError\` when an in-flight result must be dropped."""
        if self.is_stale(character, snapshot):
            raise BarrierStaleError(
                f"{character}: deletion/correction changed while the result was "
                f"being computed; the result must be discarded",
            )
        if subject is not None:
            state = self.state(character)
            if state.erased_epoch(subject) > snapshot.epoch(subject):
                raise BarrierStaleError(
                    f"{character}/{subject}: the subject was erased while the "
                    f"result was being computed",
                )

    # -- predicates ------------------------------------------------------

    def blocks_source_key(self, character: str, source_key: Any) -> bool:
        if not isinstance(source_key, str) or not source_key:
            return False
        return source_key in self.state(character).index.source_keys

    def blocks_episode(self, character: str, episode_id: Any) -> bool:
        if not isinstance(episode_id, str) or not episode_id:
            return False
        return episode_id in self.state(character).index.episode_ids

    def blocks_target(self, character: str, target_type: str, target_id: Any) -> bool:
        if not isinstance(target_id, str) or not target_id:
            return False
        return self.state(character).index.blocks_target(str(target_type), target_id)

    def blocks_text(self, character: str, text: Any) -> bool:
        """Whether this text (normalized hash) was deleted for the character."""
        if not isinstance(text, str) or not text.strip():
            return False
        return text_hash(text) in self.state(character).index.text_hashes

    def blocks_row(
        self,
        character: str,
        row: Any,
        *,
        subject: Any = None,
        check_text: bool = True,
        _index: Any = None,
    ) -> bool:
        """Whether a stored row is covered by any tombstone.

        Checks the row id (typed by shape), every source key it carries, every
        episode id, and (optionally) its normalized text hash. A subject filter
        is applied when given, so a scoped deletion never hides another
        subject's row.
        """
        if not isinstance(row, Mapping):
            return True
        if subject is not None and not row_matches_subject(row, subject):
            return False
        index = _index if _index is not None else self.state(character).index
        row_id = row.get("id")
        if isinstance(row_id, str) and row_id:
            if index.blocks_target(_infer_row_type(row), row_id):
                return True
        if any(index.blocks_target("fact", fact_id) for fact_id in row.get("correction_target_ids") or ()):
            return True
        for key in self._source_keys_of(row):
            if key in index.source_keys:
                return True
        if episode_ids_of(row) & index.episode_ids:
            return True
        if check_text and isinstance(row.get("text"), str):
            if text_hash(row["text"]) in index.text_hashes:
                return True
        return False

    def filter_rows(self, character: str, rows: Any, *, subject: Any = None) -> list:
        """Filter one read batch against one barrier state; callers recheck freshness."""
        index = self.state(character).index
        return [row for row in rows if not self.blocks_row(character, row, subject=subject, _index=index)]

    def blocks_event(self, event: MemoryEvent) -> bool:
        """Whether an event targets deleted content (live ingest must reject it)."""
        character = event.scope.character
        if self.blocks_source_key(character, event.source_key):
            return True
        if event.episode_id and self.blocks_episode(character, event.episode_id):
            return True
        return False

    def blocks_message(self, character: str, message: Any) -> bool:
        """Whether a host-shaped recent/time-index message is covered by a tombstone."""
        if not isinstance(message, Mapping):
            return True
        index = self.state(character).index
        if message_source_keys(message) & index.source_keys:
            return True
        if message_episode_ids(message) & index.episode_ids:
            return True
        text = message_text(message)
        if text and text_hash(text) in index.text_hashes:
            return True
        return False

    def history_blocked_sources(self, character: str) -> set[str]:
        """Sources behind explicitly forgotten facts stay out of history recall."""
        return {
            key
            for record in self._deletions.load_view(character).get("deletions", ())
            if any(target.get("type") == "fact" or target.get("text_hash") for target in record.get("targets", ()))
            for key in record.get("plugin_source_keys", ())
        }

    def filter_messages(self, character: str, messages: Any) -> list[dict[str, Any]]:
        """Drop deleted messages from a read/write window, keeping the order."""
        return [
            dict(message) for message in messages or ()
            if isinstance(message, Mapping)
            and not self.blocks_message(character, message)
        ]

    def assert_messages_allowed(self, character: str, messages: Any) -> None:
        """Raise before writing messages that carry deleted content."""
        for message in messages or ():
            if self.blocks_message(character, message):
                raise ContentBlockedError(
                    f"a message for {character!r} is covered by a deletion tombstone",
                )

    def blocks_correction_entry(self, character: str, entry: Any) -> bool:
        """Whether a correction block entry would resurrect deleted content.

        Host gap this closes (design §17.15): a correction stores the old/new
        text itself, so deleting the underlying fact does not remove the text
        from the prompt unless the read path filters it. The entry is hidden
        when its text hashes, target fact ids or target source keys are
        tombstoned.
        """
        if not isinstance(entry, Mapping):
            return True
        index = self.state(character).index
        if entry.get("source_key") in index.source_keys:
            return True
        if index.blocks_target("fact", entry.get("replacement_fact_id")):
            return True
        for field_name in ("text", "replaces_text"):
            value = entry.get(field_name)
            if isinstance(value, str) and value.strip():
                if text_hash(value) in index.text_hashes:
                    return True
        if entry.get("kind") == KIND_FACT_CORRECTION:
            for fact_id in entry.get("target_fact_ids") or ():
                if isinstance(fact_id, str) and index.blocks_target("fact", fact_id):
                    return True
            for source_key in entry.get("target_source_keys") or ():
                if isinstance(source_key, str) and source_key in index.source_keys:
                    return True
            # A correction whose invalidated basis was itself deleted loses its
            # remaining support: hiding it avoids re-injecting the old text.
            derived = entry.get("derived_invalidations")
            if isinstance(derived, Mapping):
                fact_ids = [
                    value for value in derived.get("fact_ids") or ()
                    if isinstance(value, str) and value
                ]
                if fact_ids and all(
                    index.blocks_target("fact", fact_id) for fact_id in fact_ids
                ):
                    return True
        return False

    def filter_correction_entries(
        self, character: str, entries: Any,
    ) -> list[dict[str, Any]]:
        """Read-path filter: drop corrections that would resurrect deleted text."""
        return [
            dict(entry) for entry in entries or ()
            if isinstance(entry, Mapping)
            and not self.blocks_correction_entry(character, entry)
        ]

    # -- epochs ----------------------------------------------------------

    def forget_epoch(self, character: str, subject: str) -> int:
        return self.state(character).forget_epoch(subject)

    def erased_epoch(self, character: str, subject: str) -> int:
        return self.state(character).erased_epoch(subject)

    def is_erased(self, character: str, subject: str, epoch: int) -> bool:
        return self.state(character).erased_epoch(subject) >= int(epoch)

    # -- write guards ----------------------------------------------------

    def assert_event_allowed(self, event: MemoryEvent) -> None:
        """Raise \`ContentBlockedError\` for an event that targets deleted content."""
        if self.blocks_event(event):
            raise ContentBlockedError(
                f"event {event.source_key!r} targets deleted content for "
                f"{event.scope.character!r}",
            )

    def assert_row_allowed(self, character: str, row: Any, *, subject: Any = None) -> None:
        """Raise \`ContentBlockedError\` before persisting a derived/stored row."""
        if self.blocks_row(character, row, subject=subject):
            raise ContentBlockedError(
                f"row for {character!r} is covered by a deletion tombstone",
            )

    def assert_fact_write_allowed(
        self, character: str, fact: Mapping[str, Any], *, subject: Any = None,
    ) -> None:
        """Guard for fact persistence (B04 calls this before writing facts)."""
        self.assert_row_allowed(character, fact, subject=subject)

    # -- internals -------------------------------------------------------

    @staticmethod
    def _source_keys_of(row: Mapping[str, Any]) -> set[str]:
        out: set[str] = set()
        for field_name in ("source_key", "source_event_keys", "source_keys"):
            value = row.get(field_name)
            if isinstance(value, str) and value.strip():
                out.add(value.strip())
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        out.add(item.strip())
        return out


def deletion_targets_to_payload(
    targets: Any,
) -> list[dict[str, Any]]:
    """Convert typed targets (or mappings) into the host request payload."""
    payload: list[dict[str, Any]] = []
    for target in targets or ():
        if isinstance(target, DeletionTarget):
            payload.append(target.as_payload())
        elif isinstance(target, Mapping) and target.get("type") and target.get("id"):
            payload.append(dict(target))
    return payload


__all__ = [
    "BarrierSnapshot",
    "BarrierState",
    "DeletionBarrier",
    "deletion_targets_to_payload",
    "episode_ids_of",
    "message_episode_ids",
    "message_metadata",
    "message_source_keys",
    "message_text",
]
