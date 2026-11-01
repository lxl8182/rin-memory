"""B10 current persona card service: revisions, freeze, projection, deletion.

The card is the authoritative semantic persona (design §8.1-§8.4). This
service owns the lifecycle:

* `import_initial` imports the host card exactly once (repeating is a
  duplicate, never a rewrite);
* `get` returns the current card with deleted/invalidated sections filtered;
* `revise` applies local `add/replace/merge/delete` operations under a
  `base_revision` compare-and-swap, refuses frozen or non-editable sections
  for automatic revisions, and appends one bounded revision record;
* `freeze` toggles one section (manual control; freeze only constrains
  automatic revisions);
* `restore_initial` restores the imported sections as a new revision while
  keeping the revision history;
* `render_projection` returns a `{section_id: text}` map for the host
  `characters.json` projection. It never writes the host file and never reads
  it back as authority (design §8.3);
* the deletion cleaner and correction invalidator narrow/withdraw sections and
  revision snapshots without resurrecting deleted sources;
* `core.persona_card_revised` journal replay is an idempotent full-card
  snapshot (older revisions never roll a newer card back).

Automatic card revision (model-driven proposals, semantic review, publishing)
belongs to B12/B11; this batch only exposes the deterministic edit surface and
keeps auto-evolution closed.
"""

from __future__ import annotations

import copy
import re
import threading
from ..persistence.persona_content import filter_owned_tree
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..invalidation import text_hash
from ..persistence.journal import EventJournal
from ..persistence.persona_card import (
    CARD_SCHEMA_VERSION,
    PersonaCardStore,
    barrier_blocks,
    normalize_card,
    parse_name_line,
    text_is_tombstoned,
    with_name_line,
)
from ..ports import Clock, StorageRoots, WritePermit

#: Persisted journal event name for a full-card snapshot.
EVT_PERSONA_CARD_REVISED = "core.persona_card_revised"

#: Origins treated as automatic revisions (subject to frozen/editable_by).
AUTO_ORIGINS: frozenset[str] = frozenset({"evolution", "auto"})

REASON_BASE_CONFLICT = "base_revision_conflict"
REASON_SECTION_FROZEN = "section_frozen"
REASON_NOT_EDITABLE = "section_not_editable"
REASON_UNKNOWN_SECTION = "unknown_section"
REASON_DUPLICATE_SECTION = "duplicate_section"
REASON_INVALID_OPERATION = "invalid_operation"
REASON_SOURCE_DELETED = "source_deleted"
REASON_NOT_INITIALIZED = "not_initialized"
REASON_NO_INITIAL_CARD = "no_initial_card"
REASON_EMPTY_SECTIONS = "empty_sections"
REASON_NO_CHANGE = "no_change"
REASON_ALREADY_IMPORTED = "already_imported"
REASON_INVALID_SECTION_ID = "invalid_section_id"
REASON_SECTION_ID_TOO_LONG = "section_id_too_long"

MAX_SECTION_ID = 120
MAX_SECTION_TEXT = 4000
MAX_KIND_CHARS = 64
MAX_REASON_CHARS = 400
MAX_SOURCE_REFS = 32
MAX_OPERATIONS = 16

_OPERATIONS = frozenset({"add", "replace", "merge", "delete"})

#: A section id is a stable identifier, never a sentence: letters (any script,
#: CJK included), digits, underscore, hyphen and dot. Whitespace, path
#: separators, quotes and punctuation are refused with a stable reason instead
#: of being silently truncated, because a shortened id would address a
#: *different* section on the next revision. Host-imported ids such as
#: `host_persona_<key>` keep working: \w is Unicode-aware in Python.
_SECTION_ID_RE = re.compile(r"^[\w.\-]+$")

#: kind=examples is a first-class section kind, not a special case of prose: it
#: renders into the fixed persona text marked as a sample, and it is never a
#: source of user facts, shared experiences or growth evidence (design §8.2).
EXAMPLES_KIND = "examples"

#: Written above an examples section wherever the card renders it. Plain prose on
#: purpose: it is part of the system prompt the model reads, so a bracketed
#: machine tag would read as another instruction layer.
EXAMPLES_BANNER = "以下是表达示范，情境和台词只是示范，不是用户事实或共同经历。"

#: Creation-time default *field templates*. They carry no storage of their own:
#: a caller that uses one simply sends these fields in an ordinary `add`
#: operation, so the preset goes through the same revision, publish, rollback
#: and freeze path as any other section. A preset is a convenience, never a
#: whitelist — any other section_id/kind is equally valid.
SECTION_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "section_id": "about_owner",
        "kind": "profile",
        "label": "关于主人",
        "editable_by": {"user": True, "evolution": True},
        "frozen": False,
    },
    {
        "section_id": "dialogue_examples",
        "kind": EXAMPLES_KIND,
        "label": "对话示例",
        "editable_by": {"user": True, "evolution": False},
        "frozen": True,
    },
)


class _Rejected(Exception):
    """Internal rejection signal carrying a stable reason code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class InvalidSectionId(ValueError):
    """A section id that cannot be a stable key; `reason` is the stable code.

    Public because both the card service and the evolution validator refuse the
    same ids for the same reasons: a shared rule with one message beats two
    copies that can drift apart.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# -- dataclasses -------------------------------------------------------------


@dataclass(frozen=True)
class CardSection:
    """One semantic persona section (design §8.1)."""

    section_id: str
    text: str = ""
    kind: str = "identity"
    scope: Mapping[str, Any] = field(default_factory=dict)
    editable_by: Mapping[str, bool] = field(
        default_factory=lambda: {"user": True, "evolution": False},
    )
    frozen: bool = False
    origin: str = "imported"
    source_refs: tuple[str, ...] = ()
    revision: int = 0
    updated_at: str | None = None
    history: tuple[Mapping[str, Any], ...] = ()
    invalidated_at: str | None = None
    invalidated_sources: tuple[str, ...] = ()
    needs_rebuild: bool = False
    content_basis: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "CardSection":
        editable = row.get("editable_by")
        if not isinstance(editable, Mapping):
            editable = {"user": True, "evolution": False}
        history = row.get("history")
        scope = row.get("scope")
        refs = row.get("source_refs")
        invalidated = row.get("invalidated_sources")
        return cls(
            section_id=str(row.get("section_id") or ""),
            text=str(row.get("text") or ""),
            kind=str(row.get("kind") or "identity"),
            scope=dict(scope) if isinstance(scope, Mapping) else {},
            editable_by={
                "user": bool(editable.get("user", True)),
                "evolution": bool(editable.get("evolution", False)),
            },
            frozen=bool(row.get("frozen")),
            origin=str(row.get("origin") or "imported"),
            source_refs=tuple(
                str(item) for item in refs or ()
                if isinstance(item, str) and item
            ) if isinstance(refs, (list, tuple)) else (),
            revision=_as_int(row.get("revision"), 0),
            updated_at=row.get("updated_at") if isinstance(row.get("updated_at"), str) else None,
            history=tuple(
                dict(item) for item in history or ()
                if isinstance(item, Mapping)
            ) if isinstance(history, (list, tuple)) else (),
            invalidated_at=(
                row.get("invalidated_at")
                if isinstance(row.get("invalidated_at"), str) else None
            ),
            invalidated_sources=tuple(
                str(item) for item in invalidated or ()
                if isinstance(item, str) and item
            ) if isinstance(invalidated, (list, tuple)) else (),
            needs_rebuild=bool(row.get("needs_rebuild")),
            content_basis=dict(row.get("content_basis") or {}),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "content_basis": copy.deepcopy(dict(self.content_basis)),
            "section_id": self.section_id,
            "text": self.text,
            "kind": self.kind,
            "scope": copy.deepcopy(dict(self.scope)),
            "editable_by": dict(self.editable_by),
            "frozen": self.frozen,
            "origin": self.origin,
            "source_refs": list(self.source_refs),
            "revision": self.revision,
            "updated_at": self.updated_at,
            "history": [dict(item) for item in self.history],
            "invalidated_at": self.invalidated_at,
            "invalidated_sources": list(self.invalidated_sources),
            "needs_rebuild": self.needs_rebuild,
        }


@dataclass(frozen=True)
class CardRevision:
    """One applied card revision record (base version, operations, actor)."""

    revision: int
    base_revision: int = 0
    operations: tuple[Mapping[str, Any], ...] = ()
    reason: str = ""
    origin: str = "user_edit"
    actor: str = "user"
    at: str | None = None
    result: str = "applied"
    source_refs: tuple[str, ...] = ()
    invalidated_at: str | None = None

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "CardRevision":
        operations = row.get("operations")
        refs = row.get("source_refs")
        return cls(
            revision=_as_int(row.get("revision"), 0),
            base_revision=_as_int(row.get("base_revision"), 0),
            operations=tuple(
                dict(item) for item in operations or ()
                if isinstance(item, Mapping)
            ) if isinstance(operations, (list, tuple)) else (),
            reason=str(row.get("reason") or ""),
            origin=str(row.get("origin") or "user_edit"),
            actor=str(row.get("actor") or "user"),
            at=row.get("at") if isinstance(row.get("at"), str) else None,
            result=str(row.get("result") or "applied"),
            source_refs=tuple(
                str(item) for item in refs or ()
                if isinstance(item, str) and item
            ) if isinstance(refs, (list, tuple)) else (),
            invalidated_at=(
                row.get("invalidated_at")
                if isinstance(row.get("invalidated_at"), str) else None
            ),
        )


@dataclass(frozen=True)
class CardSnapshot:
    """Public card view: current revision plus visible sections/revisions."""

    character: str
    revision: int = 0
    initial_revision: int = 0
    sections: tuple[CardSection, ...] = ()
    revisions: tuple[CardRevision, ...] = ()
    initial_sections: tuple[CardSection, ...] = ()


@dataclass(frozen=True)
class RevisionRequest:
    """One local card revision request."""

    base_revision: int
    operations: Sequence[Mapping[str, Any]] = ()
    reason: str = ""
    origin: str = "user_edit"
    actor: str = "user"
    source_refs: Sequence[str] = ()


@dataclass(frozen=True)
class RevisionReceipt:
    """Result of import/revise/freeze/restore; conflicts carry the snapshot."""

    status: str
    revision: int = 0
    base_revision: int = 0
    applied_ops: int = 0
    reason: str | None = None
    card: CardSnapshot | None = None
    conflict: bool = False
    duplicate: bool = False
    archived: int = 0
    changed_sections: tuple[str, ...] = ()


@dataclass(frozen=True)
class FreezeRequest:
    """Freeze/unfreeze one section (manual control)."""

    section_id: str
    frozen: bool = True
    actor: str = "user"


# -- service -----------------------------------------------------------------


class PersonaCardService:
    """Authoritative persona card reads/revisions over one data root."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: PersonaCardStore,
        journal: EventJournal,
        clock: Clock,
        permit: WritePermit,
        barrier: Any = None,
    ) -> None:
        self._roots = roots
        self._store = store
        self._journal = journal
        self._clock = clock
        self._permit = permit
        self._barrier = barrier
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._cleaner = PersonaCardDeletionCleaner(
            store=store, barrier=barrier,
        )
        self._invalidator = PersonaCardInvalidator(
            store=store, barrier=barrier,
        )
        self._journal.register_handler(
            EVT_PERSONA_CARD_REVISED, self._replay_card,
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
        """Allow the new core event on journals built before B10.

        The journal's public API has no `register_event_type`; B08 can pass
        `event_types=KNOWN_EVENT_TYPES | {EVT_PERSONA_CARD_REVISED}` instead.
        Until then extending the private vocabulary is the only way to keep the
        write path replayable (whitelist forbids editing journal.py here).
        """
        types = getattr(self._journal, "_event_types", None)
        if isinstance(types, frozenset) and EVT_PERSONA_CARD_REVISED not in types:
            try:
                self._journal._event_types = types | {EVT_PERSONA_CARD_REVISED}
            except Exception:  # noqa: BLE001 - journal remains read-only for us
                pass

    # -- read -----------------------------------------------------------

    def get(self, character: str) -> CardSnapshot:
        """Current card with tombstoned/invalidated sections filtered out."""
        character = self._character(character)
        card = self._store.load(character)
        return self._snapshot(character, card)

    def render_projection(self, character: str) -> dict[str, str]:
        """`{section_id: text}` projection for the host characters.json.

        An `examples` section is rendered with a plain-prose lead-in marking it
        as expression samples. The mark belongs in the fixed persona text: it is
        read by the same model that reads the samples, and without it the samples
        would look like recalled user facts. The section's own text is otherwise
        untouched, so the projection still maps one section id to one string.
        """
        snapshot = self.get(character)
        return {
            section.section_id: _render_section_text(section)
            for section in snapshot.sections
            if section.text.strip()
        }

    def render_text(self, character: str) -> str:
        """Whole-card prompt text (blank-line joined projection)."""
        return "\n\n".join(self.render_projection(character).values())

    def display_name(self, character: str) -> str:
        """The name line at the top of the identity section, or empty."""
        for section in self.get(character).sections:
            if section.section_id == "identity":
                return parse_name_line(section.text)
        return ""

    def rename(self, character: str, name: str, *, reason: str = "rename") -> bool:
        """Set the name line on the identity section; true when it changed.

        The name is not a separate field: it is the first line of the section
        that already describes her, so a rename is one ordinary card revision
        and every reader (console, host, game) sees the same value.
        """
        character = self._character(character)
        name = str(name or "").strip()
        card = self.get(character)
        identity = next(
            (section for section in card.sections if section.section_id == "identity"),
            None,
        )
        if identity is None or not name:
            return False
        updated = with_name_line(identity.text, name)
        if updated == identity.text:
            return False
        self.revise(character, {
            "base_revision": card.revision,
            "operations": [{"op": "replace", "section_id": "identity", "text": updated}],
            "reason": reason, "origin": "user_edit", "actor": "user",
        })
        return True

    # -- import ---------------------------------------------------------

    def import_initial(
        self,
        character: str,
        sections: Sequence[Mapping[str, Any] | CardSection],
        *,
        source: str = "initial_import",
        actor: str = "system",
    ) -> RevisionReceipt:
        """Import the initial card exactly once; a second call is duplicate."""
        character = self._character(character)
        with self._lock(character):
            card = self._store.load(character)
            if int(card.get("initial_revision") or 0) > 0 or card.get("sections"):
                return self._receipt(
                    "duplicate", character, card,
                    reason=REASON_ALREADY_IMPORTED, duplicate=True,
                )
            try:
                rows = self._normalize_sections(
                    sections, revision=1, origin="imported",
                )
            except _Rejected as exc:
                return self._receipt("rejected", character, card, reason=exc.reason)
            if not rows:
                return self._receipt(
                    "rejected", character, card, reason=REASON_EMPTY_SECTIONS,
                )
            now = self._now_iso()
            imported = [section for section in rows
                        if not self._section_blocked(character, section)]
            revision_record = {
                "id": "cardrev:1",
                "revision": 1,
                "base_revision": 0,
                "operations": [{
                    "op": "import",
                    "section_ids": [s["section_id"] for s in imported],
                }],
                "reason": _bounded(source, MAX_REASON_CHARS),
                "origin": "imported",
                "actor": _bounded(actor, 64) or "system",
                "at": now,
                "result": "applied",
                "source_refs": [],
                "sections_snapshot": copy.deepcopy(imported),
            }
            card = {
                "schema_version": CARD_SCHEMA_VERSION,
                "character": character,
                "initial_revision": 1,
                "active_revision": 1,
                "initial_sections": copy.deepcopy(imported),
                "sections": copy.deepcopy(imported),
                "revisions": [revision_record],
            }
            saved = self._write_card(character, card)
            return self._receipt(
                "completed", character, saved,
                applied_ops=len(imported),
                changed_sections=tuple(s["section_id"] for s in imported),
            )

    # -- revise ---------------------------------------------------------

    def revise(
        self, character: str, request: RevisionRequest | Mapping[str, Any],
    ) -> RevisionReceipt:
        """Apply one local revision under base_revision CAS."""
        character = self._character(character)
        try:
            req = self._normalize_revision_request(request)
        except _Rejected as exc:
            return RevisionReceipt(status="rejected", reason=exc.reason)
        with self._lock(character):
            card = self._store.load(character)
            current_revision = int(card.get("active_revision") or 0)
            if int(card.get("initial_revision") or 0) <= 0:
                return self._receipt(
                    "rejected", character, card, reason=REASON_NOT_INITIALIZED,
                )
            if int(req["base_revision"]) != current_revision:
                return self._receipt(
                    "conflict", character, card,
                    reason=REASON_BASE_CONFLICT, conflict=True,
                    base_revision=int(req["base_revision"]),
                )
            sections = copy.deepcopy(card.get("sections") or [])
            by_id = {
                str(section.get("section_id")): section
                for section in sections if isinstance(section, dict)
            }
            now = self._now_iso()
            changed: list[str] = []
            try:
                for op in req["operations"]:
                    touched = self._apply_operation(
                        character, sections, by_id, op, req, now,
                    )
                    if touched is not None and touched not in changed:
                        changed.append(touched)
            except _Rejected as exc:
                return self._receipt("rejected", character, card, reason=exc.reason)
            if not changed:
                return self._receipt(
                    "duplicate", character, card,
                    reason=REASON_NO_CHANGE, duplicate=True,
                )
            new_revision = current_revision + 1
            for section in sections:
                if str(section.get("section_id")) in changed:
                    section["revision"] = new_revision
                    section["updated_at"] = now
            card["sections"] = sections
            card["active_revision"] = new_revision
            card["character"] = character
            revision_record = {
                "id": f"cardrev:{new_revision}",
                "revision": new_revision,
                "base_revision": current_revision,
                "operations": copy.deepcopy(list(req["operations"])),
                "reason": req["reason"],
                "origin": req["origin"],
                "actor": req["actor"],
                "at": now,
                "result": "applied",
                "source_refs": list(req["source_refs"]),
                "sections_snapshot": copy.deepcopy(sections),
            }
            archived = self._append_revision(character, card, revision_record)
            saved = self._write_card(character, card)
            return self._receipt(
                "completed", character, saved,
                applied_ops=len(changed), revision=new_revision,
                base_revision=current_revision, archived=archived,
                changed_sections=tuple(changed),
            )

    # -- freeze ---------------------------------------------------------

    def freeze(
        self,
        character: str,
        section_id: str | FreezeRequest,
        frozen: bool = True,
        *,
        actor: str = "user",
    ) -> RevisionReceipt:
        """Freeze/unfreeze one section; manual control, never auto-blocked."""
        character = self._character(character)
        if isinstance(section_id, FreezeRequest):
            request = section_id
            section_id = request.section_id
            frozen = request.frozen
            actor = request.actor
        section_id = _bounded(section_id, MAX_SECTION_ID)
        if not section_id:
            return RevisionReceipt(status="rejected", reason=REASON_UNKNOWN_SECTION)
        with self._lock(character):
            card = self._store.load(character)
            if int(card.get("initial_revision") or 0) <= 0:
                return self._receipt(
                    "rejected", character, card, reason=REASON_NOT_INITIALIZED,
                )
            sections = copy.deepcopy(card.get("sections") or [])
            target = next(
                (s for s in sections if s.get("section_id") == section_id), None,
            )
            if target is None:
                return self._receipt(
                    "rejected", character, card, reason=REASON_UNKNOWN_SECTION,
                )
            if bool(target.get("frozen")) == bool(frozen):
                return self._receipt(
                    "duplicate", character, card,
                    reason=REASON_NO_CHANGE, duplicate=True,
                )
            current_revision = int(card.get("active_revision") or 0)
            new_revision = current_revision + 1
            now = self._now_iso()
            target["frozen"] = bool(frozen)
            target["revision"] = new_revision
            target["updated_at"] = now
            card["sections"] = sections
            card["active_revision"] = new_revision
            card["character"] = character
            revision_record = {
                "id": f"cardrev:{new_revision}",
                "revision": new_revision,
                "base_revision": current_revision,
                "operations": [{
                    "op": "freeze",
                    "section_id": section_id,
                    "frozen": bool(frozen),
                }],
                "reason": "freeze" if frozen else "unfreeze",
                "origin": "user_edit",
                "actor": _bounded(actor, 64) or "user",
                "at": now,
                "result": "applied",
                "source_refs": [],
                "sections_snapshot": copy.deepcopy(sections),
            }
            archived = self._append_revision(character, card, revision_record)
            saved = self._write_card(character, card)
            return self._receipt(
                "completed", character, saved,
                applied_ops=1, revision=new_revision,
                base_revision=current_revision, archived=archived,
                changed_sections=(section_id,),
            )

    # -- restore --------------------------------------------------------

    def restore_initial(self, character: str, *, actor: str = "user") -> RevisionReceipt:
        """Restore the imported sections as a new revision (history kept)."""
        character = self._character(character)
        with self._lock(character):
            card = self._store.load(character)
            initial = self._initial_sections(card)
            if not initial:
                return self._receipt(
                    "rejected", character, card, reason=REASON_NO_INITIAL_CARD,
                )
            current_revision = int(card.get("active_revision") or 0)
            new_revision = current_revision + 1
            now = self._now_iso()
            old_by_id = {
                str(section.get("section_id")): section
                for section in card.get("sections") or []
                if isinstance(section, dict)
            }
            restored: list[dict[str, Any]] = []
            for raw in initial:
                section = copy.deepcopy(dict(raw))
                old = old_by_id.get(str(section.get("section_id")))
                history = list(section.get("history") or [])
                if old is not None and old.get("text") != section.get("text"):
                    history.append({
                        "revision": _as_int(old.get("revision"), 0),
                        "text": str(old.get("text") or ""),
                        "replaced_at": now,
                        "reason": "restore_initial",
                        "origin": "user_edit",
                    })
                section["history"] = history[-self._store.history_limit:]
                if old is not None:
                    section["frozen"] = bool(old.get("frozen"))
                section["revision"] = new_revision
                section["updated_at"] = now
                section["origin"] = "imported"
                restored.append(section)
            card["sections"] = restored
            card["active_revision"] = new_revision
            card["character"] = character
            revision_record = {
                "id": f"cardrev:{new_revision}",
                "revision": new_revision,
                "base_revision": current_revision,
                "operations": [{
                    "op": "restore_initial",
                    "section_ids": [s["section_id"] for s in restored],
                }],
                "reason": "restore_initial",
                "origin": "user_edit",
                "actor": _bounded(actor, 64) or "user",
                "at": now,
                "result": "applied",
                "source_refs": [],
                "sections_snapshot": copy.deepcopy(restored),
            }
            archived = self._append_revision(character, card, revision_record)
            saved = self._write_card(character, card)
            return self._receipt(
                "completed", character, saved,
                applied_ops=1, revision=new_revision,
                base_revision=current_revision, archived=archived,
                changed_sections=tuple(s["section_id"] for s in restored),
            )

    # -- B03 integration -------------------------------------------------

    def cleanup(self, character: str, resolved: Mapping[str, Any]) -> Mapping[str, Any]:
        """Deletion-engine cleaner delegate."""
        return self._cleaner.cleanup(character, resolved)

    def invalidate(
        self,
        character: str,
        fact_ids: Sequence[str],
        *,
        invalidated_at: str,
        reason: str,
        correction_id: str,
    ) -> Mapping[str, Any]:
        """Correction-service derived invalidator delegate."""
        return self._invalidator.invalidate(
            character,
            fact_ids,
            invalidated_at=invalidated_at,
            reason=reason,
            correction_id=correction_id,
        )

    # -- internals ------------------------------------------------------

    def _now_iso(self) -> str:
        return self._clock.now().isoformat()

    def _section_blocked(self, character: str, section: Mapping[str, Any]) -> bool:
        return self._store.section_blocked(character, section)

    def _snapshot(self, character: str, card: Mapping[str, Any]) -> CardSnapshot:
        sections = []
        for raw in card.get("sections") or []:
            if not isinstance(raw, Mapping):
                continue
            if raw.get("invalidated_at") or raw.get("needs_rebuild"):
                continue
            if self._section_blocked(character, raw):
                continue
            sections.append(CardSection.from_mapping(raw))
        revisions = []
        for raw in card.get("revisions") or []:
            if not isinstance(raw, Mapping):
                continue
            if raw.get("invalidated_at"):
                continue
            revisions.append(CardRevision.from_mapping(raw))
        initial = []
        for raw in card.get("initial_sections") or []:
            if not isinstance(raw, Mapping):
                continue
            if self._section_blocked(character, raw):
                continue
            initial.append(CardSection.from_mapping(raw))
        return CardSnapshot(
            character=character,
            revision=int(card.get("active_revision") or 0),
            initial_revision=int(card.get("initial_revision") or 0),
            sections=tuple(sections),
            revisions=tuple(revisions),
            initial_sections=tuple(initial),
        )

    def _receipt(
        self,
        status: str,
        character: str,
        card: Mapping[str, Any],
        *,
        reason: str | None = None,
        applied_ops: int = 0,
        revision: int | None = None,
        base_revision: int | None = None,
        conflict: bool = False,
        duplicate: bool = False,
        archived: int = 0,
        changed_sections: Sequence[str] = (),
    ) -> RevisionReceipt:
        snapshot = self._snapshot(character, card)
        return RevisionReceipt(
            status=status,
            revision=int(revision if revision is not None else snapshot.revision),
            base_revision=int(
                base_revision if base_revision is not None else snapshot.revision,
            ),
            applied_ops=applied_ops,
            reason=reason,
            card=snapshot,
            conflict=conflict,
            duplicate=duplicate,
            archived=archived,
            changed_sections=tuple(changed_sections),
        )

    def _write_card(self, character: str, card: Mapping[str, Any]) -> dict[str, Any]:
        """Journal the full-card snapshot, then persist the view."""
        payload = {"character": character, "card": copy.deepcopy(dict(card))}
        view_card = copy.deepcopy(dict(card))

        def _mutate(view: Any) -> None:
            if not isinstance(view, dict):
                raise RuntimeError("[persona_card] view is not a dict")
            view.clear()
            view.update(copy.deepcopy(view_card))

        self._journal.record_and_save(
            character,
            EVT_PERSONA_CARD_REVISED,
            payload,
            load_view=self._store.load,
            mutate_view=_mutate,
            save_view=self._store.save,
        )
        # The store applies the deletion barrier on write; reload the saved card
        # so receipts/snapshots never expose a row that was filtered.
        return self._store.load(character)

    def _append_revision(
        self, character: str, card: dict[str, Any], record: Mapping[str, Any],
    ) -> int:
        revisions = list(card.get("revisions") or [])
        revisions.append(dict(record))
        limit = self._store.history_limit
        overflow: list[dict[str, Any]] = []
        if len(revisions) > limit:
            overflow = [dict(item) for item in revisions[:-limit]]
            revisions = revisions[-limit:]
        card["revisions"] = revisions
        if overflow:
            self._store.archive_history(character, overflow)
        return len(overflow)

    def _initial_sections(self, card: Mapping[str, Any]) -> list[dict[str, Any]]:
        initial = card.get("initial_sections")
        if isinstance(initial, list) and initial:
            return [dict(item) for item in initial if isinstance(item, Mapping)]
        target = int(card.get("initial_revision") or 0)
        for record in card.get("revisions") or []:
            if not isinstance(record, Mapping):
                continue
            if _as_int(record.get("revision"), 0) != target:
                continue
            snapshot = record.get("sections_snapshot")
            if isinstance(snapshot, list) and snapshot:
                return [dict(item) for item in snapshot if isinstance(item, Mapping)]
        return []

    def _normalize_sections(
        self,
        sections: Sequence[Mapping[str, Any] | CardSection],
        *,
        revision: int,
        origin: str,
    ) -> list[dict[str, Any]]:
        if isinstance(sections, (str, bytes)) or not isinstance(sections, Sequence):
            raise _Rejected("invalid_request")
        now = self._now_iso()
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in sections:
            mapping = raw.to_mapping() if isinstance(raw, CardSection) else raw
            if not isinstance(mapping, Mapping):
                raise _Rejected("invalid_request")
            section = self._section_from_input(
                mapping, revision=revision, origin=origin, now=now,
            )
            if section["section_id"] in seen:
                raise _Rejected(REASON_DUPLICATE_SECTION)
            seen.add(section["section_id"])
            rows.append(section)
        return rows

    def _section_from_input(
        self,
        raw: Mapping[str, Any],
        *,
        revision: int,
        origin: str,
        now: str,
    ) -> dict[str, Any]:
        section_id = _bounded(raw.get("section_id"), MAX_SECTION_ID)
        if not section_id:
            raise _Rejected("invalid_request")
        text = str(raw.get("text") or "")
        if len(text) > MAX_SECTION_TEXT:
            raise _Rejected("text_too_long")
        editable = _normalize_editable(raw.get("editable_by"))
        return {
            "section_id": section_id,
            "kind": _bounded(raw.get("kind"), MAX_KIND_CHARS) or "identity",
            "text": text,
            "scope": _normalize_scope(raw.get("scope")),
            "editable_by": editable,
            "frozen": bool(raw.get("frozen")),
            "origin": _bounded(origin, 64) or "imported",
            "source_refs": _normalize_refs(raw.get("source_refs")),
            "revision": int(revision),
            "updated_at": now,
            "history": [],
        }

    def _normalize_revision_request(
        self, request: RevisionRequest | Mapping[str, Any],
    ) -> dict[str, Any]:
        if isinstance(request, RevisionRequest):
            mapping: Mapping[str, Any] = {
                "base_revision": request.base_revision,
                "operations": list(request.operations),
                "reason": request.reason,
                "origin": request.origin,
                "actor": request.actor,
                "source_refs": list(request.source_refs),
            }
        elif isinstance(request, Mapping):
            mapping = request
        else:
            raise _Rejected("invalid_request")
        base = mapping.get("base_revision")
        if isinstance(base, bool) or not isinstance(base, int) or base < 0:
            raise _Rejected("invalid_request")
        operations_raw = mapping.get("operations") or []
        if not isinstance(operations_raw, (list, tuple)) or len(operations_raw) > MAX_OPERATIONS:
            raise _Rejected("invalid_request")
        operations: list[dict[str, Any]] = []
        for raw in operations_raw:
            if not isinstance(raw, Mapping):
                raise _Rejected("invalid_request")
            op_type = str(raw.get("op") or "").strip().lower()
            if op_type not in _OPERATIONS:
                raise _Rejected(REASON_INVALID_OPERATION)
            op: dict[str, Any] = {"op": op_type}
            # Length and emptiness are refused here; the character rule is
            # decided in _apply_operation, where the card is known. A legacy or
            # host-imported id must stay editable even when it would not be
            # accepted as a new one today, but an over-long id is never truncated
            # into a *different* section: it is a naming mistake at the source.
            raw_id = raw.get("section_id")
            section_id = raw_id.strip() if isinstance(raw_id, str) else ""
            if not section_id:
                raise _Rejected("invalid_request")
            if len(section_id) > MAX_SECTION_ID:
                raise _Rejected(REASON_SECTION_ID_TOO_LONG)
            op["section_id"] = section_id
            if op_type != "delete":
                text = str(raw.get("text") or "")
                if len(text) > MAX_SECTION_TEXT:
                    raise _Rejected("text_too_long")
                op["text"] = text
            for key in ("kind", "scope", "editable_by", "frozen", "content_basis"):
                if key in raw:
                    op[key] = copy.deepcopy(raw[key])
            op["source_refs"] = _normalize_refs(raw.get("source_refs"))
            operations.append(op)
        origin = _bounded(mapping.get("origin"), 64) or "user_edit"
        return {
            "base_revision": int(base),
            "operations": operations,
            "reason": _bounded(mapping.get("reason"), MAX_REASON_CHARS),
            "origin": origin,
            "actor": _bounded(mapping.get("actor"), 64) or "user",
            "source_refs": _normalize_refs(mapping.get("source_refs")),
        }

    def _check_sources(
        self, character: str, refs: Sequence[str], section_id: str,
    ) -> None:
        if self._barrier is None:
            return
        for ref in refs:
            if barrier_blocks(self._barrier, character, ref):
                raise _Rejected(REASON_SOURCE_DELETED)
        if section_id:
            for target_type in ("persona", "reflection", "fact", "card"):
                if self._barrier.blocks_target(character, target_type, section_id):
                    raise _Rejected(REASON_SOURCE_DELETED)

    def _apply_operation(
        self,
        character: str,
        sections: list[dict[str, Any]],
        by_id: dict[str, dict[str, Any]],
        op: Mapping[str, Any],
        req: Mapping[str, Any],
        now: str,
    ) -> str | None:
        op_type = op["op"]
        section_id = op["section_id"]
        refs = list(op.get("source_refs") or ()) + list(req.get("source_refs") or ())
        self._check_sources(character, refs, section_id)
        auto = req["origin"] in AUTO_ORIGINS
        existing = by_id.get(section_id)
        if op_type == "add":
            if existing is not None:
                # A second card with the same id would silently overwrite the
                # first: the caller gets `duplicate_section` and edits instead.
                raise _Rejected(REASON_DUPLICATE_SECTION)
            # Only *new* ids are held to the identifier rule. An id that already
            # exists (imported from the host, or written before this rule) stays
            # editable through replace/merge/delete below: refusing to edit
            # existing text would strand it.
            _checked_section_id(section_id)
            kind = _bounded(op.get("kind"), MAX_KIND_CHARS) or "identity"
            editable = _normalize_editable(op.get("editable_by"))
            if auto and not editable.get("evolution"):
                raise _Rejected(REASON_NOT_EDITABLE)
            frozen = bool(op.get("frozen"))
            if kind == EXAMPLES_KIND and "frozen" not in op:
                # A dialogue sample is hand-picked expression: it starts frozen
                # and closed to evolution so an automatic revision cannot
                # rewrite the very style the user chose (design §4.2). Both
                # defaults are overridable by an explicit field in the same op.
                frozen = True
            section = {
                "section_id": section_id,
                "kind": kind,
                "text": str(op.get("text") or ""),
                "scope": _normalize_scope(op.get("scope")),
                "editable_by": editable,
                "frozen": frozen,
                "origin": req["origin"],
                "source_refs": refs[:MAX_SOURCE_REFS],
                "revision": 0,
                "updated_at": now,
                "history": [],
                "content_basis": copy.deepcopy(op.get("content_basis") or {}),
            }
            sections.append(section)
            by_id[section_id] = section
            return section_id
        if existing is None:
            raise _Rejected(REASON_UNKNOWN_SECTION)
        if auto:
            if existing.get("frozen"):
                raise _Rejected(REASON_SECTION_FROZEN)
            editable = existing.get("editable_by") or {}
            if not editable.get("evolution"):
                raise _Rejected(REASON_NOT_EDITABLE)
        if op_type == "delete":
            sections.remove(existing)
            by_id.pop(section_id, None)
            return section_id
        old_text = str(existing.get("text") or "")
        # Permission/scope changes are ordinary revisions of an existing
        # section: freezing a section by hand, opening it to evolution, or
        # binding it to another subject must not require inventing a text change
        # (that would write a revision whose only real purpose was a flag).
        old_basis = copy.deepcopy(existing.get("content_basis") or {})
        metadata_changed = self._apply_metadata(existing, op)
        if op_type == "replace":
            new_text = str(op.get("text") or "")
        else:  # merge
            new_text = old_text + "\n" + str(op.get("text") or "")
        if section_id == "identity":
            # The name lives on this section's first line. An edit that rewrote
            # the body without it would silently drop the display name, so the
            # existing name line is carried over instead.
            name = parse_name_line(old_text)
            if name and not parse_name_line(new_text):
                new_text = with_name_line(new_text, name)
        if new_text == old_text:
            # Text unchanged: the revision still counts when it changed what the
            # card *permits* (editable_by/scope/frozen/kind); otherwise it is a
            # no-op and the caller gets `no_change`.
            return section_id if metadata_changed else None
        history = list(existing.get("history") or [])
        history.append({
            "revision": _as_int(existing.get("revision"), 0),
            "text": old_text,
            "replaced_at": now,
            "reason": req.get("reason") or "",
            "origin": req["origin"],
            "content_basis": old_basis,
        })
        existing["history"] = history[-self._store.history_limit:]
        existing["text"] = new_text
        existing["origin"] = req["origin"]
        merged_refs = list(existing.get("source_refs") or [])
        for ref in refs:
            if ref not in merged_refs and len(merged_refs) < MAX_SOURCE_REFS:
                merged_refs.append(ref)
        existing["source_refs"] = merged_refs
        return section_id

    @staticmethod
    def _apply_metadata(section: dict[str, Any], op: Mapping[str, Any]) -> bool:
        """Apply kind/scope/editable_by/frozen; true when anything changed.

        These four are the section's *permissions and reach*, not its prose. They
        are applied even when the caller sends no text, so "freeze this section",
        "let evolution touch it" and "bind it to this subject" are ordinary
        revisions with an audit record instead of side effects of a text edit.
        """
        changed = False
        if "content_basis" in op:
            changed = section.get("content_basis") != op["content_basis"]
            section["content_basis"] = copy.deepcopy(op["content_basis"])
        if "kind" in op:
            kind = _bounded(op.get("kind"), MAX_KIND_CHARS) or section.get("kind")
            if kind != section.get("kind"):
                section["kind"] = kind
                changed = True
        if "scope" in op:
            scope = _normalize_scope(op.get("scope"))
            if scope != dict(section.get("scope") or {}):
                section["scope"] = scope
                changed = True
        if "editable_by" in op:
            editable = _normalize_editable(op.get("editable_by"))
            if editable != dict(section.get("editable_by") or {}):
                section["editable_by"] = editable
                changed = True
        if "frozen" in op:
            frozen = bool(op.get("frozen"))
            if frozen != bool(section.get("frozen")):
                section["frozen"] = frozen
                changed = True
        return changed

    # -- replay ---------------------------------------------------------

    def _replay_card(self, character: str, payload: Mapping[str, Any]) -> bool:
        """Idempotent full-card replay; an older revision never rolls back."""
        if not isinstance(payload, Mapping):
            raise RuntimeError("core.persona_card_revised payload is not an object")
        raw = payload.get("card")
        if not isinstance(raw, Mapping):
            raw = payload
        card = normalize_card(raw, character)
        current = self._store.load(character)
        if int(card.get("active_revision") or 0) < int(
            current.get("active_revision") or 0,
        ):
            return False
        filtered = self._store.filter_card(character, card)
        if filtered == current:
            return False
        self._store.save(character, filtered)
        return True


# -- deletion cleaner --------------------------------------------------------


class PersonaCardDeletionCleaner:
    """Deletion-engine cleaner: drop/narrow sections and revision snapshots."""

    def __init__(self, *, store: PersonaCardStore, barrier: Any = None) -> None:
        self._store = store
        self._barrier = barrier

    def cleanup(
        self, character: str, resolved: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        character = str(character or "").strip()
        if not character:
            raise ValueError("character must be nonempty")
        explicit = _id_set(
            resolved.get("card_ids") or resolved.get("section_ids"),
        )
        deleted_refs = _id_set(
            list(resolved.get("reflection_ids") or ())
            + list(resolved.get("fact_ids") or ())
            + list(resolved.get("persona_ids") or ())
            + list(resolved.get("source_keys") or ())
            + list(resolved.get("fact_source_keys") or ()),
        )
        text_hashes = _id_set(resolved.get("text_hashes"))
        subject = str(resolved.get("subject") or "")
        card = self._store.load(character)
        removed_sections: list[str] = []
        narrowed = 0

        def classify(section: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
            section_id = str(section.get("section_id") or "")
            if section_id in explicit:
                return "remove", None
            if self._barrier is not None and self._store.section_blocked(
                character, section,
            ):
                return "remove", None
            text = section.get("text")
            if text_is_tombstoned(self._barrier, character, text):
                return "remove", None
            if text_hashes and isinstance(text, str) and text_hash(text) in text_hashes:
                return "remove", None
            if subject not in ("", "private"):
                scope = section.get("scope")
                if isinstance(scope, Mapping) and str(scope.get("subject") or "") == subject:
                    return "remove", None
            refs = _refs(section)
            hit = set(refs) & deleted_refs
            if not hit:
                return "keep", None
            remaining = [ref for ref in refs if ref not in deleted_refs]
            if refs and not remaining:
                return "remove", None
            narrowed_row = copy.deepcopy(dict(section))
            narrowed_row["source_refs"] = remaining
            invalidated = _id_set(narrowed_row.get("invalidated_sources"))
            narrowed_row["invalidated_sources"] = sorted(invalidated | hit)
            narrowed_row["needs_rebuild"] = True
            return "narrow", narrowed_row

        sections: list[dict[str, Any]] = []
        for raw in card.get("sections") or []:
            if not isinstance(raw, Mapping):
                continue
            action, replacement = classify(raw)
            if action == "remove":
                removed_sections.append(str(raw.get("section_id") or ""))
                continue
            if action == "narrow":
                narrowed += 1
                sections.append(replacement or dict(raw))
                continue
            sections.append(dict(raw))
        card["sections"] = sections
        if isinstance(card.get("initial_sections"), list):
            initial: list[dict[str, Any]] = []
            for raw in card["initial_sections"]:
                if not isinstance(raw, Mapping):
                    continue
                action, replacement = classify(raw)
                if action == "remove":
                    continue
                initial.append(replacement if action == "narrow" else dict(raw))
            card["initial_sections"] = initial

        removed_revisions: list[str] = []
        revisions_narrowed = 0
        revisions: list[dict[str, Any]] = []
        for raw in card.get("revisions") or []:
            if not isinstance(raw, Mapping):
                continue
            record = copy.deepcopy(dict(raw))
            snapshot = record.get("sections_snapshot")
            if isinstance(snapshot, list):
                kept: list[dict[str, Any]] = []
                changed = False
                for item in snapshot:
                    if not isinstance(item, Mapping):
                        continue
                    action, replacement = classify(item)
                    if action == "remove":
                        changed = True
                        continue
                    if action == "narrow":
                        changed = True
                        kept.append(replacement or dict(item))
                    else:
                        kept.append(dict(item))
                if changed:
                    if not kept:
                        removed_revisions.append(str(record.get("id") or ""))
                        continue
                    record["sections_snapshot"] = kept
                    record["needs_rebuild"] = True
                    revisions_narrowed += 1
            refs = _refs(record)
            hit = set(refs) & deleted_refs
            if hit:
                remaining = [ref for ref in refs if ref not in deleted_refs]
                if refs and not remaining:
                    removed_revisions.append(str(record.get("id") or ""))
                    continue
                record["source_refs"] = remaining
                record["invalidated_sources"] = sorted(
                    _id_set(record.get("invalidated_sources")) | hit,
                )
                record["needs_rebuild"] = True
                revisions_narrowed += 1
            revisions.append(record)
        card["revisions"] = revisions
        self._store.save(character, card)
        archive_removed = 0
        archive_ids = [item for item in removed_revisions if item]
        if archive_ids:
            archive_removed = self._store.delete_from_archive(character, archive_ids)
        return {
            "status": "completed",
            "detail": {
                "sections_removed": len(removed_sections),
                "sections_narrowed": narrowed,
                "revisions_removed": len(removed_revisions),
                "revisions_narrowed": revisions_narrowed,
                "archive_removed": archive_removed,
            },
        }


# -- correction invalidator --------------------------------------------------


class PersonaCardInvalidator:
    """Correction derived-invalidator: mark sections/revisions needs_rebuild."""

    def __init__(self, *, store: PersonaCardStore, barrier: Any = None) -> None:
        self._store = store
        self._barrier = barrier

    def invalidate(
        self,
        character: str,
        fact_ids: Sequence[str],
        *,
        invalidated_at: str,
        reason: str,
        correction_id: str,
    ) -> Mapping[str, Any]:
        character = str(character or "").strip()
        if not character:
            raise ValueError("character must be nonempty")
        targets = _id_set(fact_ids)
        if not targets:
            return {"card_ids": []}
        card = self._store.load(character)
        filtered = filter_owned_tree(card, lambda ref: ref in targets)
        changed_ids = [str(section["section_id"]) for section in card.get("sections") or []
                       if section != next((row for row in filtered.get("sections") or [] if row["section_id"] == section["section_id"]), section)]
        card = filtered
        affected_section_ids: set[str] = set()
        sections: list[dict[str, Any]] = []
        for raw in card.get("sections") or []:
            if not isinstance(raw, Mapping):
                continue
            section = copy.deepcopy(dict(raw))
            section_id = str(section.get("section_id") or "")
            refs = _refs(section)
            hit = set(refs) & targets
            if not hit:
                sections.append(section)
                continue
            affected_section_ids.add(section_id)
            was_marked = bool(section.get("invalidated_at"))
            invalidated = _id_set(section.get("invalidated_sources"))
            remaining = [ref for ref in refs if ref not in targets]
            section["source_refs"] = remaining
            section["invalidated_sources"] = sorted(invalidated | hit)
            section["needs_rebuild"] = True
            if refs and not remaining:
                section["invalidated_at"] = invalidated_at
                section["invalidated_reason"] = reason
                section["correction_id"] = correction_id
            if not was_marked:
                changed_ids.append(section_id)
            sections.append(section)
        card["sections"] = sections
        if isinstance(card.get("initial_sections"), list):
            initial: list[dict[str, Any]] = []
            for raw in card["initial_sections"]:
                if not isinstance(raw, Mapping):
                    continue
                section = copy.deepcopy(dict(raw))
                refs = _refs(section)
                hit = set(refs) & targets
                if hit:
                    section["invalidated_sources"] = sorted(
                        _id_set(section.get("invalidated_sources")) | hit,
                    )
                    section["needs_rebuild"] = True
                initial.append(section)
            card["initial_sections"] = initial
        revisions: list[dict[str, Any]] = []
        for raw in card.get("revisions") or []:
            if not isinstance(raw, Mapping):
                continue
            record = copy.deepcopy(dict(raw))
            snapshot = record.get("sections_snapshot")
            hit_revision = False
            if isinstance(snapshot, list):
                for item in snapshot:
                    if not isinstance(item, Mapping):
                        continue
                    if str(item.get("section_id") or "") in affected_section_ids:
                        item["needs_rebuild"] = True
                        hit_revision = True
            if hit_revision:
                record["needs_rebuild"] = True
                record["invalidated_at"] = invalidated_at
                record["invalidated_reason"] = reason
            revisions.append(record)
        card["revisions"] = revisions
        self._store.save(character, card)
        return {"card_ids": changed_ids}


# -- helpers -----------------------------------------------------------------


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bounded(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _render_section_text(section: CardSection) -> str:
    """Section text as it enters the fixed persona prompt."""
    text = section.text.strip()
    if section.kind == EXAMPLES_KIND and text:
        return EXAMPLES_BANNER + "\n" + text
    return text


def validate_section_id(value: Any) -> str:
    """Return a usable section_id or raise `InvalidSectionId` with a reason.

    Three failures get three reasons, because a caller has to act differently on
    each: an over-long id is shortened at the *source* (the client picked a bad
    label), an illegal-character id must be renamed (it would not survive as a
    key), and an empty id means the operation itself is malformed. Nothing is
    silently truncated: a truncated id addresses a different section.
    """
    section_id = value.strip() if isinstance(value, str) else ""
    if not section_id:
        raise InvalidSectionId(REASON_INVALID_OPERATION)
    if len(section_id) > MAX_SECTION_ID:
        raise InvalidSectionId(REASON_SECTION_ID_TOO_LONG)
    if not _SECTION_ID_RE.match(section_id):
        raise InvalidSectionId(REASON_INVALID_SECTION_ID)
    return section_id


def _checked_section_id(value: Any) -> str:
    """`validate_section_id` for the card service's internal rejection path."""
    try:
        return validate_section_id(value)
    except InvalidSectionId as exc:
        raise _Rejected(exc.reason) from exc


def _normalize_editable(value: Any) -> dict[str, bool]:
    editable: dict[str, bool] = {"user": True, "evolution": False}
    if isinstance(value, Mapping):
        editable["user"] = bool(value.get("user", True))
        editable["evolution"] = bool(value.get("evolution", False))
    return editable


def _normalize_scope(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    scope = copy.deepcopy(dict(value))
    subject = scope.get("subject")
    if isinstance(subject, str) and subject.strip():
        scope["subject"] = subject.strip()[:256]
    else:
        scope.pop("subject", None)
    applies_to = scope.get("applies_to")
    if isinstance(applies_to, Mapping):
        scene: dict[str, str] = {}
        for key in ("plugin_id", "game_id", "session_id"):
            item = applies_to.get(key)
            if isinstance(item, str) and item.strip():
                scene[key] = item.strip()[:64]
        scope["applies_to"] = scene
    elif "applies_to" in scope:
        scope.pop("applies_to", None)
    return scope


def _normalize_refs(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(item.strip()[:600])
        if len(out) >= MAX_SOURCE_REFS:
            break
    return out


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
    "AUTO_ORIGINS",
    "EXAMPLES_BANNER",
    "EXAMPLES_KIND",
    "InvalidSectionId",
    "SECTION_PRESETS",
    "validate_section_id",
    "CardRevision",
    "CardSection",
    "CardSnapshot",
    "EVT_PERSONA_CARD_REVISED",
    "FreezeRequest",
    "PersonaCardDeletionCleaner",
    "PersonaCardInvalidator",
    "PersonaCardService",
    "RevisionReceipt",
    "RevisionRequest",
]
