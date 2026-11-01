"""B11 persona publishing: persistent version tokens, boundaries, consumers.

A card revision is not "in effect" until it is published. This service turns
the authoritative card into a persisted `persona_version` token and records:

* which target the publish is for (`next_turn` / `realtime` / `game`);
* the explicit safe boundary (next request / next generation / next plan);
* consumer receipts: which consumer has adopted which version;
* a bounded publish/rollback/invalidation history.

The host adapter (B08) decides how to deliver a realtime update or how to
hand a new connection over; the core only guarantees that a version is
persisted, paired with its text/projection, and never silently treated as
adopted. Old consumers of a realtime publish are marked `superseded`; a game
planner poll is paired by version and gets `reissue` on mismatch. Deletion or
correction invalidates published versions so old text cannot be consumed again
(design §9.3, §10, §17.7).

Event `persona.version_published` carries a full publish snapshot and is
replayed idempotently; an older publish never rolls a newer current version
back.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..persistence.journal import EventJournal
from ..persistence.persona_versions import (
    TARGETS,
    PersonaVersionStore,
    text_sha256,
    version_token,
)
from ..ports import Clock, StorageRoots, WritePermit
from .persona_card import PersonaCardService

#: Persisted journal event name for a publish/rollback snapshot.
EVT_PERSONA_VERSION_PUBLISHED = "persona.version_published"

REASON_NO_CURRENT = "no_published_version"
REASON_REVISION_MISMATCH = "revision_mismatch"
REASON_CARD_NOT_INITIALIZED = "card_not_initialized"
REASON_CONSUMED_VERSION = "consumed_version"
REASON_UNKNOWN_VERSION = "unknown_version"
REASON_SOURCE_DELETED = "source_deleted"
REASON_VERSION_MISMATCH = "version_mismatch"
REASON_CONSUMER_SUPERSEDED = "consumer_superseded"
REASON_NOT_GAME_TARGET = "not_game_target"

MAX_CONSUMER_NAME = 120
MAX_REASON_CHARS = 400


@dataclass(frozen=True)
class PublishRequest:
    """One publish request for a target and (optionally) a card revision."""

    target: str = "next_turn"
    revision: int | None = None
    request_id: str | None = None
    actor: str = "user"
    boundary: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class PublishSnapshot:
    """Published version view: token, card revision, text and boundary."""

    available: bool = False
    version: str = ""
    revision: int = 0
    text: str = ""
    projection: Mapping[str, str] = field(default_factory=dict)
    section_scopes: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    published_at: str | None = None
    target: str = ""
    boundary: Mapping[str, Any] = field(default_factory=dict)
    text_sha256: str = ""
    planner_version: str = ""
    request_id: str | None = None


@dataclass(frozen=True)
class PublishReceipt:
    """Result of publish/rollback; duplicate/conflict never claims adoption."""

    status: str
    version: str = ""
    revision: int = 0
    target: str = ""
    published_at: str | None = None
    boundary: Mapping[str, Any] = field(default_factory=dict)
    snapshot: PublishSnapshot | None = None
    conflict: bool = False
    duplicate: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class ConsumerReceipt:
    """Result of one consumer adopting/checking the published version."""

    status: str
    consumer: str = ""
    expected_version: str | None = None
    version: str = ""
    revision: int = 0
    snapshot: PublishSnapshot | None = None
    needs_reissue: bool = False
    reason: str | None = None
    seen_at: str | None = None


class PersonaPublishService:
    """Publish persona card revisions as persisted, target-scoped versions."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: PersonaVersionStore,
        card_service: PersonaCardService,
        journal: EventJournal,
        clock: Clock,
        permit: WritePermit,
        barrier: Any = None,
    ) -> None:
        self._roots = roots
        self._store = store
        self._card_service = card_service
        self._journal = journal
        self._clock = clock
        self._permit = permit
        self._barrier = barrier
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._journal.register_handler(
            EVT_PERSONA_VERSION_PUBLISHED, self._replay_published,
        )
        self._ensure_event_vocabulary()

    # -- wiring ---------------------------------------------------------

    @staticmethod
    def _character(character: Any) -> str:
        value = str(character or "").strip()
        if not value:
            raise ValueError("character must be nonempty")
        return value

    def _lock(self, character: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(character)
            if lock is None:
                lock = threading.RLock()
                self._locks[character] = lock
            return lock

    def _ensure_event_vocabulary(self) -> None:
        """Allow the new event on journals built before B11 (see B10 note)."""
        types = getattr(self._journal, "_event_types", None)
        if types is not None and EVT_PERSONA_VERSION_PUBLISHED not in types:
            try:
                self._journal._event_types = (
                    frozenset(types) | {EVT_PERSONA_VERSION_PUBLISHED}
                )
            except Exception:  # noqa: BLE001 - journal stays read-only for us
                pass

    # -- publish --------------------------------------------------------

    def publish(
        self,
        character: str,
        request: PublishRequest | Mapping[str, Any] | None = None,
        *,
        target: str | None = None,
        revision: int | None = None,
        request_id: str | None = None,
        actor: str | None = None,
        boundary: Mapping[str, Any] | None = None,
        reason: str | None = None,
    ) -> PublishReceipt:
        """Publish the current card revision for one target."""
        character = self._character(character)
        try:
            req = self._normalize_request(
                request, target=target, revision=revision,
                request_id=request_id, actor=actor, boundary=boundary,
                reason=reason,
            )
        except ValueError as exc:
            return PublishReceipt(status="rejected", reason=str(exc))
        with self._lock(character):
            card = self._card_service.get(character)
            if card.revision <= 0:
                return PublishReceipt(
                    status="rejected", reason=REASON_CARD_NOT_INITIALIZED,
                )
            if req["revision"] is not None and int(req["revision"]) != card.revision:
                current = self.current(character)
                return PublishReceipt(
                    status="conflict", conflict=True,
                    reason=REASON_REVISION_MISMATCH,
                    version=current.version, revision=current.revision,
                    snapshot=current,
                )
            projection = self._card_service.render_projection(character)
            text = self._card_service.render_text(character)
            token = version_token(card.revision, text)
            digest = text_sha256(text)
            boundary_doc = self._build_boundary(
                req["target"], req["boundary"], token,
            )
            previous = self._store.current(character)
            if (
                previous is not None
                and previous.get("version") == token
                and previous.get("target") == req["target"]
                and int(previous.get("revision") or 0) == card.revision
            ):
                return PublishReceipt(
                    status="duplicate", duplicate=True,
                    version=token, revision=card.revision,
                    target=req["target"],
                    published_at=previous.get("published_at"),
                    boundary=previous.get("boundary") or {},
                    snapshot=self._snapshot_from_record(character, previous),
                    reason="already_published",
                )
            record = {
                "version": token,
                "revision": card.revision,
                "target": req["target"],
                "published_at": self._now_iso(),
                "boundary": boundary_doc,
                "text_sha256": digest,
                "text": text,
                "projection": dict(projection),
                "section_scopes": {section.section_id: dict(section.scope) for section in card.sections},
                "request_id": req["request_id"],
                "actor": req["actor"],
                "reason": req["reason"],
                "previous_version": (
                    previous.get("version") if isinstance(previous, Mapping) else ""
                ),
                "planner_version": token if req["target"] == "game" else "",
                "source_refs": self._card_source_refs(character, card),
            }
            if self._sources_blocked(character, record["source_refs"]):
                return PublishReceipt(
                    status="rejected", reason=REASON_SOURCE_DELETED,
                    revision=card.revision, target=req["target"],
                )
            document = self._store.load(character)
            document["published"] = list(document.get("published") or []) + [record]
            document["current"] = copy.deepcopy(record)
            document["history"] = list(document.get("history") or []) + [{
                "action": "publish",
                "version": token,
                "revision": card.revision,
                "target": req["target"],
                "at": record["published_at"],
            }]
            saved = self._write_document(character, document, {
                "action": "publish",
                "record": record,
                "current": record,
            })
            if not isinstance(saved.get("current"), Mapping):
                return PublishReceipt(
                    status="rejected", reason=REASON_SOURCE_DELETED,
                    revision=card.revision, target=req["target"],
                )
            if req["target"] == "realtime":
                self._supersede_realtime_consumers(character, token)
            return PublishReceipt(
                status="published", version=token, revision=card.revision,
                target=req["target"], published_at=record["published_at"],
                boundary=boundary_doc,
                snapshot=self._snapshot_from_record(character, record),
            )

    # -- reads ----------------------------------------------------------

    def current(self, character: str) -> PublishSnapshot:
        """Current published version (unavailable when nothing is published)."""
        character = self._character(character)
        document = self._store.load(character)
        current = document.get("current")
        if not isinstance(current, Mapping):
            return PublishSnapshot(available=False)
        return self._snapshot_from_current(character, document, current)

    def consume(
        self,
        character: str,
        consumer: str,
        *,
        expected_version: str | None = None,
    ) -> ConsumerReceipt:
        """Adopt/check the current version for one consumer."""
        character = self._character(character)
        name = str(consumer or "").strip()[:MAX_CONSUMER_NAME]
        if not name:
            raise ValueError("consumer must be nonempty")
        with self._lock(character):
            document = self._store.load(character)
            current = document.get("current")
            if not isinstance(current, Mapping):
                return ConsumerReceipt(
                    status="unavailable", consumer=name,
                    expected_version=expected_version,
                    reason=REASON_NO_CURRENT,
                )
            snapshot = self._snapshot_from_current(character, document, current)
            version = str(current.get("version") or "")
            revision = int(current.get("revision") or 0)
            if expected_version and expected_version != version:
                entry = (document.get("consumers") or {}).get(name)
                superseded = False
                if isinstance(entry, Mapping):
                    if entry.get("status") == "superseded":
                        superseded = True
                    elif (
                        entry.get("version") == expected_version
                        and (
                            current.get("target") == "realtime"
                            or entry.get("target") == "realtime"
                        )
                    ):
                        superseded = True
                return ConsumerReceipt(
                    status="superseded" if superseded else "mismatch",
                    consumer=name,
                    expected_version=expected_version,
                    version=version,
                    revision=revision,
                    snapshot=snapshot,
                    needs_reissue=True,
                    reason=(
                        REASON_CONSUMER_SUPERSEDED if superseded
                        else REASON_VERSION_MISMATCH
                    ),
                )
            updated = self._store.mark_consumer(
                character, name, version=version, revision=revision,
                target=str(current.get("target") or ""), status="active",
            )
            seen_at = None
            entry = (updated.get("consumers") or {}).get(name)
            if isinstance(entry, Mapping):
                seen_at = entry.get("seen_at")
            return ConsumerReceipt(
                status="match", consumer=name,
                expected_version=expected_version,
                version=version, revision=revision,
                snapshot=snapshot, seen_at=seen_at,
            )

    def pair_planner(self, character: str, planner_version: str) -> dict[str, Any]:
        """Pair a game planner poll with the published game version."""
        character = self._character(character)
        document = self._store.load(character)
        current = document.get("current")
        if not isinstance(current, Mapping):
            return {
                "status": "pending", "matched": False, "version": None,
                "revision": 0, "snapshot": None, "reason": REASON_NO_CURRENT,
            }
        snapshot = self._snapshot_from_current(character, document, current)
        version = str(current.get("version") or "")
        if current.get("target") != "game":
            return {
                "status": "reissue", "matched": False, "version": version,
                "revision": int(current.get("revision") or 0),
                "snapshot": snapshot, "reason": REASON_NOT_GAME_TARGET,
            }
        if not planner_version or planner_version != version:
            return {
                "status": "reissue", "matched": False, "version": version,
                "revision": int(current.get("revision") or 0),
                "snapshot": snapshot, "reason": REASON_VERSION_MISMATCH,
            }
        return {
            "status": "matched", "matched": True, "version": version,
            "revision": int(current.get("revision") or 0),
            "snapshot": snapshot, "reason": None,
        }

    # -- invalidation / rollback ----------------------------------------

    def invalidate_for_deletion(
        self, character: str, resolved: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Invalidate published versions whose sources/text were deleted."""
        character = self._character(character)
        deleted_refs = _id_set(
            list(resolved.get("reflection_ids") or ())
            + list(resolved.get("fact_ids") or ())
            + list(resolved.get("persona_ids") or ())
            + list(resolved.get("source_keys") or ())
            + list(resolved.get("fact_source_keys") or ())
        )
        text_hashes = _id_set(resolved.get("text_hashes"))
        document = self._store.load(character)
        affected: list[str] = []
        candidates: list[Mapping[str, Any]] = []
        current = document.get("current")
        if isinstance(current, Mapping):
            candidates.append(current)
        candidates.extend(
            item for item in document.get("published") or []
            if isinstance(item, Mapping)
        )
        candidates.extend(
            item for item in document.get("history") or []
            if isinstance(item, Mapping)
        )
        for record in candidates:
            version = str(record.get("version") or "")
            if not version or version in affected:
                continue
            refs = set(_refs(record))
            digest = str(record.get("text_sha256") or "")
            if (refs & deleted_refs) or (digest and digest in text_hashes):
                affected.append(version)
        if not affected:
            return {
                "invalidated_versions": [], "versions": [],
                "current_invalidated": False,
            }
        saved = self._store.purge_invalidated(character, affected)
        ordered = sorted(affected)
        return {
            "invalidated_versions": ordered,
            "versions": ordered,
            "current_invalidated": not isinstance(saved.get("current"), Mapping),
        }

    def rollback(
        self,
        character: str,
        version: str,
        *,
        allow_consumed: bool = False,
    ) -> PublishReceipt:
        """Make an earlier published version current again (explicit op)."""
        character = self._character(character)
        wanted = str(version or "").strip()
        if not wanted:
            return PublishReceipt(status="rejected", reason=REASON_UNKNOWN_VERSION)
        with self._lock(character):
            document = self._store.load(character)
            current = document.get("current")
            if isinstance(current, Mapping) and current.get("version") == wanted:
                return PublishReceipt(
                    status="duplicate", duplicate=True,
                    version=wanted,
                    revision=int(current.get("revision") or 0),
                    target=str(current.get("target") or ""),
                    published_at=current.get("published_at"),
                    boundary=current.get("boundary") or {},
                    snapshot=self._snapshot_from_current(character, document, current),
                    reason="already_current",
                )
            record = self._find_record(character, document, wanted)
            if record is None:
                return PublishReceipt(
                    status="rejected", reason=REASON_UNKNOWN_VERSION,
                )
            target_revision = int(record.get("revision") or 0)
            consumers = document.get("consumers") or {}
            consumed_newer = any(
                isinstance(entry, Mapping)
                and entry.get("version") != wanted
                and int(entry.get("revision") or 0) > target_revision
                and entry.get("status") != "superseded"
                for entry in consumers.values()
            )
            if consumed_newer and not allow_consumed:
                return PublishReceipt(
                    status="conflict", conflict=True,
                    reason=REASON_CONSUMED_VERSION,
                    version=wanted, revision=target_revision,
                    target=str(record.get("target") or ""),
                    snapshot=self.current(character),
                )
            restored = copy.deepcopy(dict(record))
            restored["published_at"] = self._now_iso()
            document["current"] = restored
            document["history"] = list(document.get("history") or []) + [{
                "action": "rollback",
                "version": wanted,
                "revision": target_revision,
                "target": str(restored.get("target") or ""),
                "at": restored["published_at"],
            }]
            saved = self._write_document(character, document, {
                "action": "rollback",
                "record": restored,
                "current": restored,
            })
            saved_current = saved.get("current")
            if not isinstance(saved_current, Mapping):
                return PublishReceipt(
                    status="rejected", reason=REASON_SOURCE_DELETED,
                    version=wanted, revision=target_revision,
                )
            return PublishReceipt(
                status="rolled_back", version=wanted,
                revision=target_revision,
                target=str(restored.get("target") or ""),
                published_at=restored["published_at"],
                boundary=restored.get("boundary") or {},
                snapshot=self._snapshot_from_record(character, restored),
            )

    # -- internals ------------------------------------------------------

    def _now_iso(self) -> str:
        return self._clock.now().isoformat()

    def _normalize_request(
        self,
        request: PublishRequest | Mapping[str, Any] | None,
        *,
        target: str | None,
        revision: int | None,
        request_id: str | None,
        actor: str | None,
        boundary: Mapping[str, Any] | None,
        reason: str | None,
    ) -> dict[str, Any]:
        if isinstance(request, PublishRequest):
            values: dict[str, Any] = {
                "target": request.target,
                "revision": request.revision,
                "request_id": request.request_id,
                "actor": request.actor,
                "boundary": dict(request.boundary),
                "reason": request.reason,
            }
        elif isinstance(request, Mapping):
            values = dict(request)
        elif request is None:
            values = {}
        else:
            raise ValueError("invalid_request")
        for key, override in (
            ("target", target), ("revision", revision),
            ("request_id", request_id), ("actor", actor),
            ("boundary", boundary), ("reason", reason),
        ):
            if override is not None:
                values[key] = override
        chosen_target = str(values.get("target") or "next_turn").strip()
        if chosen_target not in TARGETS:
            raise ValueError("invalid_target")
        chosen_revision = values.get("revision")
        if chosen_revision is not None:
            if isinstance(chosen_revision, bool) or not isinstance(chosen_revision, int):
                raise ValueError("invalid_revision")
            if chosen_revision < 0:
                raise ValueError("invalid_revision")
        raw_boundary = values.get("boundary")
        if raw_boundary is not None and not isinstance(raw_boundary, Mapping):
            raise ValueError("invalid_boundary")
        return {
            "target": chosen_target,
            "revision": chosen_revision,
            "request_id": (
                str(values.get("request_id") or "").strip()[:120] or None
            ),
            "actor": str(values.get("actor") or "user").strip()[:64] or "user",
            "boundary": dict(raw_boundary) if isinstance(raw_boundary, Mapping) else {},
            "reason": str(values.get("reason") or "").strip()[:MAX_REASON_CHARS],
        }

    @staticmethod
    def _build_boundary(
        target: str, requested: Mapping[str, Any], token: str,
    ) -> dict[str, Any]:
        if target == "next_turn":
            return {"kind": "next_turn", "safe_boundary": "next_request"}
        if target == "realtime":
            boundary: dict[str, Any] = {
                "kind": "realtime",
                "safe_boundary": "next_generation",
                "mode": str(requested.get("mode") or "session_update")[:32],
                "requires_ack": bool(requested.get("requires_ack", True)),
            }
            session_id = requested.get("session_id")
            if isinstance(session_id, str) and session_id.strip():
                boundary["session_id"] = session_id.strip()[:120]
            return boundary
        return {
            "kind": "game",
            "safe_boundary": "next_plan",
            "planner_version": token,
        }

    def _card_source_refs(self, character: str, card: Any) -> list[str]:
        refs: list[str] = []
        for section in getattr(card, "sections", ()) or ():
            basis = getattr(section, "content_basis", {}) or {}
            for rule in basis.get("rules") or ():
                key = rule.get("source_key")
                if key and key not in refs:
                    refs.append(key)
            for growth in basis.get("growth") or ():
                for key in [growth["id"], *(growth.get("source_keys") or ())]:
                    if key and key not in refs:
                        refs.append(key)
            for ref in getattr(section, "source_refs", ()) or ():
                if isinstance(ref, str) and ref and ref not in refs:
                    refs.append(ref)
                if len(refs) >= 128:
                    return refs
        return refs

    def _sources_blocked(self, character: str, refs: Sequence[str]) -> bool:
        if self._barrier is None or not refs:
            return False
        # Any deleted source makes this card version unsafe to publish.
        for ref in refs:
            if self._barrier.blocks_source_key(character, ref):
                return True
            for target_type in ("fact", "reflection", "persona", "growth"):
                try:
                    if self._barrier.blocks_target(character, target_type, ref):
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def _write_document(
        self,
        character: str,
        document: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        view_document = copy.deepcopy(dict(document))

        def _mutate(view: Any) -> None:
            if not isinstance(view, dict):
                raise RuntimeError("[persona_publish] view is not a dict")
            view.clear()
            view.update(copy.deepcopy(view_document))

        self._journal.record_and_save(
            character,
            EVT_PERSONA_VERSION_PUBLISHED,
            payload,
            load_view=self._store.load,
            mutate_view=_mutate,
            save_view=self._store.save,
        )
        return self._store.load(character)

    def _supersede_realtime_consumers(self, character: str, version: str) -> None:
        document = self._store.load(character)
        consumers = document.get("consumers") or {}
        for name, entry in consumers.items():
            if not isinstance(entry, Mapping):
                continue
            if entry.get("status") == "superseded":
                continue
            if entry.get("target") != "realtime":
                continue
            if entry.get("version") == version:
                continue
            self._store.mark_consumer(
                character, name,
                version=str(entry.get("version") or ""),
                revision=int(entry.get("revision") or 0),
                seen_at=entry.get("seen_at"),
                status="superseded",
                target="realtime",
            )

    def _snapshot_from_current(
        self,
        character: str,
        document: Mapping[str, Any],
        current: Mapping[str, Any],
    ) -> PublishSnapshot:
        version = str(current.get("version") or "")
        target = str(current.get("target") or "")
        record = self._find_record(character, document, version, target)
        source = record if isinstance(record, Mapping) else current
        return self._snapshot_from_record(character, source)

    def _snapshot_from_record(
        self, character: str, record: Mapping[str, Any],
    ) -> PublishSnapshot:
        revision = int(record.get("revision") or 0)
        text = record.get("text")
        if not isinstance(text, str):
            text = ""
        projection = record.get("projection")
        if not isinstance(projection, Mapping):
            projection = {}
        if not text:
            try:
                card = self._card_service.get(character)
            except Exception:  # noqa: BLE001 - snapshot must stay readable
                card = None
            if card is not None and int(getattr(card, "revision", 0) or 0) == revision:
                try:
                    text = self._card_service.render_text(character)
                    projection = self._card_service.render_projection(character)
                except Exception:  # noqa: BLE001
                    text = ""
                    projection = {}
        return PublishSnapshot(
            available=True,
            version=str(record.get("version") or ""),
            revision=revision,
            text=text,
            projection={str(k): str(v) for k, v in projection.items()},
            section_scopes=record.get("section_scopes") or {},
            published_at=record.get("published_at"),
            target=str(record.get("target") or ""),
            boundary=(
                dict(record.get("boundary"))
                if isinstance(record.get("boundary"), Mapping) else {}
            ),
            text_sha256=str(record.get("text_sha256") or text_sha256(text)),
            planner_version=str(record.get("planner_version") or ""),
            request_id=record.get("request_id"),
        )

    def _find_record(
        self,
        character: str,
        document: Mapping[str, Any],
        version: str,
        target: str | None = None,
    ) -> Mapping[str, Any] | None:
        published = [
            item for item in document.get("published") or []
            if isinstance(item, Mapping) and item.get("version") == version
        ]
        if target is not None:
            for item in reversed(published):
                if item.get("target") == target:
                    return item
        if published:
            return published[-1]
        try:
            archived = self._store.load_archive(character)
        except Exception:  # noqa: BLE001 - archive is best effort
            archived = []
        matches = [
            item for item in archived
            if isinstance(item, Mapping) and item.get("version") == version
        ]
        for item in reversed(matches):
            if isinstance(item.get("text"), str) and item.get("text"):
                return item
        return matches[-1] if matches else None

    # -- replay ---------------------------------------------------------

    def _replay_published(self, character: str, payload: Mapping[str, Any]) -> bool:
        """Idempotent publish/rollback replay; older publishes never roll back."""
        if not isinstance(payload, Mapping):
            raise RuntimeError("persona.version_published payload is not an object")
        action = str(payload.get("action") or "publish")
        raw = payload.get("record")
        if not isinstance(raw, Mapping):
            raw = payload.get("current")
        if not isinstance(raw, Mapping):
            raise RuntimeError("persona.version_published payload has no record")
        version = str(raw.get("version") or "").strip()
        revision = int(raw.get("revision") or 0)
        if not version:
            raise RuntimeError("persona.version_published record has no version")
        current = self._store.current(character)
        if isinstance(current, Mapping):
            if action == "publish":
                if int(current.get("revision") or 0) > revision:
                    return False
                if (
                    current.get("version") == version
                    and current.get("target") == raw.get("target")
                ):
                    return False
            elif current.get("version") == version:
                return False
        if action == "rollback":
            self._store.set_current(character, raw, action="rollback")
        else:
            self._store.append_publish(character, raw)
        return True


# -- helpers -----------------------------------------------------------------


def _refs(row: Mapping[str, Any]) -> list[str]:
    refs = row.get("source_refs")
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        return []
    return [str(item) for item in refs if isinstance(item, str) and item]


def _id_set(values: Any) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, (str, bytes)):
        values = [values]
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    return {str(item) for item in values if isinstance(item, str) and item}


__all__ = [
    "ConsumerReceipt",
    "EVT_PERSONA_VERSION_PUBLISHED",
    "PersonaPublishService",
    "PublishReceipt",
    "PublishRequest",
    "PublishSnapshot",
]
