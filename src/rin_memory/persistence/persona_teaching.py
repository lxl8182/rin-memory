"""MC04-T explicit-teaching records: durable teaching log + rule-line registry.

One user message can become one durable *teaching*: a small set of expression
rules appended to the existing tone card section. This store keeps the
authoritative record of what was taught, from which saved source, which rule
lines it owns, and whether it is still active.

Why a separate store (design §5: no second source of truth)
-----------------------------------------------------------
The *text* lives in the persona card, exactly once, in the section the user
and the host already edit (tone). This store never renders text into a prompt;
it answers three questions the card cannot:

* which saved source produced which rule line (so "刚才那条别学了" can revoke
  exactly that teaching and nothing else);
* whether a teaching is active, revoked by the user, or superseded because the
  tone section ran out of room;
* which rule lines are teaching-owned at all, so a read path can drop a line
  whose teaching is no longer active while leaving hand-written tone text
  alone.

On-disk contract: <character_dir>/persona_teachings.json

    {"schema_version": 1, "character": "demo",
     "teachings": {"teach_<id>": <teaching record>, ...}}

Teaching record (unknown fields round-trip verbatim):

    teaching_id / character / subject / status / source_key / source_text /
    source_content_class / quoted_example {source_key?, text?} / rules
    [{rule_id, text, line, example_ref?}] / intent / client_id /
    base_revision / revision / published_version / created_at / updated_at /
    revoked_at / revoke_reason / superseded_at / superseded_by

A missing file is an empty log. Invalid JSON, a non-object root, an
unsupported schema, a non-object "teachings" map or a record without
"teaching_id" raises CorruptStoreError / InvalidSchemaError and the bytes are
left as found (fail-closed, same rule as every other core store).

Writes go through WritePermit and the journal (record_and_save), under one
process-wide RLock per physical file, so a crash between the card revision and
this record is repaired by replay instead of leaving a half-written log.
"""

from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from typing import Any, Mapping

from ..errors import CorruptStoreError, InvalidSchemaError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .schema import SCHEMA_VERSION, load_versioned

#: File name inside <character_dir>.
TEACHINGS_FILENAME = "persona_teachings.json"
TEACHINGS_SCHEMA = SCHEMA_VERSION

#: Lifecycle of one teaching.
STATUS_ACTIVE = "active"
#: The user said "刚才那条别学了": the rule lines were removed from the card.
STATUS_REVOKED = "revoked"
#: The tone section ran out of room and the oldest rules made way.
STATUS_SUPERSEDED = "superseded"
STATUS_PENDING = "pending"
STATUSES = frozenset({STATUS_ACTIVE, STATUS_REVOKED, STATUS_SUPERSEDED, STATUS_PENDING})

#: Bounded log: the newest records survive a trim.
MAX_TEACHINGS = 200
MAX_ID_CHARS = 120
MAX_TEXT_CHARS = 4000
MAX_RULES = 8

#: Journal event name for one teaching snapshot (persisted identifier).
TEACHING_EVENT_TYPE = "core.persona_teaching_recorded"

#: Lock per physical teaching file, shared process-wide.
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


def _bounded(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def empty_document(character: str = "") -> dict[str, Any]:
    """Fresh log document; a missing file reads as this."""
    return {
        "schema_version": TEACHINGS_SCHEMA,
        "character": character,
        "teachings": {},
    }


def normalize_rule(raw: Any) -> dict[str, Any]:
    """Validate one rule row (text is the rule; line is what the card holds)."""
    if not isinstance(raw, Mapping):
        raise CorruptStoreError("persona_teaching rule is not an object")
    rule_id = _bounded(raw.get("rule_id"), MAX_ID_CHARS)
    text = _bounded(raw.get("text"), MAX_TEXT_CHARS)
    if not rule_id or not text:
        raise CorruptStoreError("persona_teaching rule needs rule_id and text")
    rule: dict[str, Any] = {
        "rule_id": rule_id,
        "text": text,
        "line": _bounded(raw.get("line"), MAX_TEXT_CHARS) or text,
    }
    if raw.get("superseded_by"):
        rule["superseded_by"] = _bounded(raw["superseded_by"], MAX_ID_CHARS)
    for key in ("instruction", "source_key"):
        if raw.get(key):
            rule[key] = _bounded(raw[key], MAX_TEXT_CHARS)
    example_ref = raw.get("example_ref")
    if isinstance(example_ref, str) and example_ref.strip():
        rule["example_ref"] = example_ref.strip()[:MAX_TEXT_CHARS]
    return rule


def normalize_teaching(raw: Any) -> dict[str, Any]:
    """Validate one stored teaching record; unknown fields round-trip."""
    if not isinstance(raw, Mapping):
        raise CorruptStoreError("persona_teaching record is not an object")
    record = copy.deepcopy(dict(raw))
    teaching_id = _bounded(record.get("teaching_id"), MAX_ID_CHARS)
    if not teaching_id:
        raise CorruptStoreError("persona_teaching record has no teaching_id")
    record["teaching_id"] = teaching_id
    status = _bounded(record.get("status"), 32) or STATUS_ACTIVE
    if status not in STATUSES:
        raise CorruptStoreError(
            f"persona_teaching {teaching_id!r} has unknown status {status!r}",
        )
    record["status"] = status
    record["source_key"] = _bounded(record.get("source_key"), MAX_TEXT_CHARS)
    record["subject"] = _bounded(record.get("subject"), 256) or "private"
    record["source_text"] = _bounded(record.get("source_text"), MAX_TEXT_CHARS)
    rules_raw = record.get("rules") or []
    if not isinstance(rules_raw, (list, tuple)):
        raise CorruptStoreError(
            f"persona_teaching {teaching_id!r}: rules is not a list",
        )
    record["rules"] = [normalize_rule(item) for item in rules_raw][:MAX_RULES]
    example = record.get("quoted_example")
    if isinstance(example, Mapping):
        record["quoted_example"] = {
            "source_key": _bounded(example.get("source_key"), MAX_TEXT_CHARS),
            "text": _bounded(example.get("text"), MAX_TEXT_CHARS),
        }
    else:
        record.pop("quoted_example", None)
    for key in ("base_revision", "revision"):
        try:
            record[key] = int(record.get(key) or 0)
        except (TypeError, ValueError):
            record[key] = 0
    record["published_version"] = _bounded(record.get("published_version"), 200)
    return record


def normalize_document(raw: Any, character: str = "") -> dict[str, Any]:
    """Validate and copy a log document."""
    if raw is None:
        return empty_document(character)
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            "persona_teachings: expected a JSON object, found "
            f"{type(raw).__name__}",
        )
    schema = raw.get("schema_version", TEACHINGS_SCHEMA)
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise InvalidSchemaError(
            f"persona_teachings: missing or non-integer schema_version: {schema!r}",
        )
    if schema != TEACHINGS_SCHEMA:
        raise InvalidSchemaError(
            "unsupported persona_teachings schema "
            f"{schema}; this build supports {TEACHINGS_SCHEMA}",
        )
    document = copy.deepcopy(dict(raw))
    document["schema_version"] = TEACHINGS_SCHEMA
    if character:
        document.setdefault("character", character)
    teachings = document.get("teachings", {})
    if not isinstance(teachings, Mapping):
        raise CorruptStoreError("persona_teachings.teachings is not an object")
    checked: dict[str, Any] = {}
    for key, item in teachings.items():
        record = normalize_teaching(item)
        if record["teaching_id"] != key:
            raise CorruptStoreError(
                f"persona_teaching {key!r} carries teaching_id "
                f"{record['teaching_id']!r}",
            )
        checked[key] = record
    document["teachings"] = checked
    return document


def _trim(document: dict[str, Any]) -> dict[str, Any]:
    """Keep the newest MAX_TEACHINGS records."""
    teachings = document.get("teachings") or {}
    if len(teachings) <= MAX_TEACHINGS:
        return document

    def _stamp(item: Mapping[str, Any]) -> str:
        return str(item.get("updated_at") or item.get("created_at") or "")

    ordered = sorted(
        teachings.items(), key=lambda pair: _stamp(pair[1]), reverse=True,
    )
    document["teachings"] = dict(ordered[:MAX_TEACHINGS])
    return document


class PersonaTeachingStore:
    """Per-character persona_teachings.json (see the module docstring)."""

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

    def _path(self, character: str, *, create: bool = False) -> Path:
        return self._roots.character_dir(character, create=create) / TEACHINGS_FILENAME

    def _now_iso(self) -> str:
        return self._clock.now().isoformat()

    # -- reads ----------------------------------------------------------

    def load(self, character: str) -> dict[str, Any]:
        """Return the normalized document; corruption raises, never empty."""
        path = self._path(character)
        with _lock_for(path):
            return self._load_document(character)

    def _load_document(self, character: str) -> dict[str, Any]:
        path = self._path(character)
        document = load_versioned(
            self._store, path, supported=frozenset({TEACHINGS_SCHEMA}),
            default=empty_document(character),
        )
        return normalize_document(document, character)

    def get(self, character: str, teaching_id: str) -> dict[str, Any] | None:
        record = self.load(character)["teachings"].get(str(teaching_id or ""))
        return None if record is None else copy.deepcopy(record)

    def by_source(self, character: str, source_key: str) -> dict[str, Any] | None:
        """The teaching recorded for one saved source, newest first."""
        wanted = str(source_key or "")
        if not wanted:
            return None
        matches = [
            item for item in self.load(character)["teachings"].values()
            if item.get("source_key") == wanted
        ]
        if not matches:
            return None
        matches.sort(key=lambda item: str(item.get("created_at") or ""))
        return copy.deepcopy(matches[-1])

    def all(self, character: str) -> list[dict[str, Any]]:
        """Every teaching, oldest first."""
        rows = [
            copy.deepcopy(item)
            for item in self.load(character)["teachings"].values()
        ]
        rows.sort(key=lambda item: str(item.get("created_at") or ""))
        return rows

    # -- journal view API ----------------------------------------------

    def load_view(self, character: str) -> dict[str, Any]:
        """Mutable view for journal.record_and_save."""
        return self.load(character)

    def save_view(self, character: str, view: Mapping[str, Any]) -> None:
        path = self._path(character, create=True)
        self._permit.assert_writable("save_persona_teachings", str(path))
        self._store.save(path, _trim(normalize_document(view, character)))

    def apply_snapshot(
        self, view: dict[str, Any], payload: Mapping[str, Any],
    ) -> bool:
        """Merge one journaled teaching snapshot; True when it changed.

        Never downgrades: a revoked teaching stays revoked, a superseded one is
        not resurrected, and a record whose updated_at is older than the stored
        one is ignored (replaying an old entry must not undo a later decision).
        """
        raw = payload.get("teaching")
        if not isinstance(raw, Mapping):
            raise CorruptStoreError("persona_teaching snapshot has no teaching")
        record = normalize_teaching(raw)
        teachings = view.setdefault("teachings", {})
        existing = teachings.get(record["teaching_id"])
        if existing is not None:
            stored = normalize_teaching(existing)
            if stored["status"] == STATUS_REVOKED and record["status"] != STATUS_REVOKED:
                return False
            if stored["status"] == STATUS_SUPERSEDED and record["status"] == STATUS_ACTIVE:
                return False
            if str(record.get("updated_at") or "") < str(stored.get("updated_at") or ""):
                return False
            if stored == record:
                return False
        teachings[record["teaching_id"]] = record
        return True

    def record(
        self, character: str, payload: Mapping[str, Any], *, journal,
    ) -> None:
        """Journal one teaching snapshot, then persist the view."""
        view_payload = {"teaching": dict(payload.get("teaching") or {})}
        journal.record_and_save(
            character, TEACHING_EVENT_TYPE, view_payload,
            load_view=self.load_view,
            mutate_view=lambda view: self.apply_snapshot(view, view_payload),
            save_view=self.save_view,
        )


__all__ = [
    "MAX_RULES",
    "MAX_TEACHINGS",
    "STATUSES",
    "STATUS_ACTIVE",
    "STATUS_REVOKED",
    "STATUS_SUPERSEDED",
    "TEACHINGS_FILENAME",
    "TEACHINGS_SCHEMA",
    "TEACHING_EVENT_TYPE",
    "PersonaTeachingStore",
    "empty_document",
    "normalize_document",
    "normalize_teaching",
]
