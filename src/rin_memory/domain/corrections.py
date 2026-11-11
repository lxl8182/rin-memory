"""B03 correction engine: record the new understanding, then propagate.

Order (design §17.9.1): register the correction version first, then invalidate
the facts it replaces, then propagate along source relations to reflections and
persona (B05/B10 register those walkers). Recording first means a read that
started before the correction can be discarded by version; propagation is
idempotent so a retried request repairs a crash between the two writes.

A correction entry is hidden from the prompt block when the deletion barrier
covers its text, targets or source keys. That closes the host gap where a
deleted fact stayed alive inside the correction block (design §17.15).

Read side: :meth:\`CorrectionService.block\` returns the \`{block, items, version}\`
triple; callers capture \`version()\` before their work and compare after it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping, Sequence

from ..contracts import (
    CorrectionReceipt,
    CorrectionRequest,
    MemoryBlock,
)
from ..errors import CorrectionNotFoundError
from ..invalidation import row_matches_subject, text_matches
from ..persistence.corrections import (
    ACTION_RETRACT,
    KIND_FACT_CORRECTION,
    STATUS_ACTIVE,
    CorrectionStore,
    render_block,
)
from ..persistence.facts_view import FactsView
from ..ports import Clock, EventJournal, StorageRoots, WritePermit
from .barrier import DeletionBarrier

#: Core-owned journal event name for correction writes.
CORRECTION_EVENT_TYPE = "core.correction_recorded"
#: Fields a fact row gets when a correction invalidates it (host field names).
FACT_INVALIDATED_BY = "invalidated_by"
FACT_INVALIDATED_REASON = "invalidated_reason"
FACT_INVALIDATED_AT = "invalidated_at"


class CorrectionService:
    """Independent \`correct\` entry point over one data root (single writer)."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        journal: EventJournal,
        corrections: CorrectionStore,
        barrier: DeletionBarrier,
        facts: FactsView,
        clock: Clock,
        permit: WritePermit,
        derived_invalidators: Sequence[Any] = (),
        token_counter: Any = None,
    ) -> None:
        self._roots = roots
        self._journal = journal
        self._corrections = corrections
        self._barrier = barrier
        self._facts = facts
        self._clock = clock
        self._permit = permit
        self._derived_invalidators = tuple(derived_invalidators)
        self._token_counter = token_counter
        journal.register_handler(CORRECTION_EVENT_TYPE, corrections.replay_snapshot)

    # -- public API ------------------------------------------------------

    async def correct(self, request: CorrectionRequest) -> CorrectionReceipt:
        """Record a preference/fact correction and invalidate what it replaces."""
        return await asyncio.to_thread(self._correct_sync, request)

    def correct_extracted(self, request: CorrectionRequest, *, search_aliases: Sequence[str] = ()) -> CorrectionReceipt:
        """Apply a source-bound extraction operation under the commit fence."""
        return self._correct_sync(request, operation_id="corr_" + request.request_id, search_aliases=search_aliases)

    def extracted(self, character, source_keys, *, subject, repair=False):
        """Previously journaled extraction corrections from these utterances."""
        keys = set(source_keys)
        rows = [row for row in self.active(character, subject=subject)["items"]
                if row.get("source_key") in keys and row["correction_id"].startswith("corr_extract_")]
        if repair:
            for row in rows:
                if self._needs_propagation(row):
                    self._propagate(character, row, duplicate=True)
        return rows

    def repair_replacements(self, character: str) -> None:
        with self._barrier.write_lock(character):
            for entry in self._corrections.all_entries(character):
                if (entry.get("status") == STATUS_ACTIVE and self._can_materialize(entry)
                        and not entry.get("replacement_fact_id")
                        and not self._barrier.blocks_correction_entry(character, entry)):
                    self._propagate(character, entry, duplicate=True)

    async def retract(self, character: str, correction_id: str) -> CorrectionReceipt:
        """Withdraw an active correction (applied invalidations are kept)."""
        return await asyncio.to_thread(self._retract_sync, character, correction_id)

    def version(self, character: str) -> int:
        return self._corrections.version(character)

    def block(
        self,
        character: str,
        *,
        subject: Any = None,
        scene: Any = None,
        language: Any = "zh",
        limit_tokens: int = 200,
    ) -> MemoryBlock:
        """Correction prompt block with deleted content filtered out."""
        active = self._corrections.active(character, subject=subject, scene=scene)
        items = self._barrier.filter_correction_entries(character, active["items"])
        if any(item.get("replacement_fact_id") for item in items):
            invalidated = {row.get("id") for row in self._facts.load(character) + self._facts.load_archive(character)
                           if row.get(FACT_INVALIDATED_AT)}
            items = [item for item in items if item.get("replacement_fact_id") not in invalidated]
        return MemoryBlock(
            block=render_block(
                items, language, limit_tokens, counter=self._token_counter,
            ),
            items=tuple(items),
            version=int(active["version"]),
        )

    def active(
        self,
        character: str,
        *,
        subject: Any = None,
        scene: Any = None,
    ) -> dict[str, Any]:
        active = self._corrections.active(character, subject=subject, scene=scene)
        return {
            "version": active["version"],
            "items": self._barrier.filter_correction_entries(
                character, active["items"],
            ),
        }

    def get(self, character: str, correction_id: str) -> dict[str, Any] | None:
        return self._corrections.get(character, correction_id)

    # -- write flow ------------------------------------------------------

    def _correct_sync(
        self, request: CorrectionRequest, *, operation_id: str = "", search_aliases: Sequence[str] = (),
    ) -> CorrectionReceipt:
        character = request.scope.character
        spec = self._spec_from_request(request)
        if operation_id:
            spec["operation_id"] = operation_id
        if search_aliases:
            spec["search_aliases"] = search_aliases
        with self._barrier.write_lock(character):
            prepared = self._corrections.prepare(character, spec)
            if prepared.duplicate:
                receipt = self._receipt_from_entry(
                    prepared.entry,
                    self._corrections.version(character),
                    duplicate=True,
                    request_id=request.request_id,
                )
                if self._needs_propagation(prepared.entry):
                    # Repair window: a crash between the journal append and
                    # propagation leaves an active entry with empty derived
                    # ids; re-running propagation is idempotent.
                    receipt = self._propagate(
                        character, prepared.entry,
                        request_id=request.request_id, duplicate=True,
                    )
                return receipt
            payload = prepared.payload
            if payload is None:  # pragma: no cover - prepare guarantees a payload
                raise RuntimeError("prepared correction is missing its payload")
            self._journal.record_and_save(
                character,
                CORRECTION_EVENT_TYPE,
                payload,
                load_view=self._corrections.load_view,
                mutate_view=lambda view: self._corrections.apply_snapshot(view, payload),
                save_view=self._corrections.save_view,
            )
            # The correction version moved: in-flight reads must be discarded.
            self._barrier.invalidate(character)
            return self._propagate(
                character, prepared.entry, request_id=request.request_id,
            )

    def _retract_sync(self, character: str, correction_id: str) -> CorrectionReceipt:
        with self._barrier.write_lock(character):
            entry = self._corrections.get(character, correction_id)
            if entry is None:
                raise CorrectionNotFoundError(
                    f"unknown correction {correction_id!r}",
                )
            if entry.get("status") == STATUS_ACTIVE and entry.get("replacement_fact_id"):
                self._invalidate_facts(character, {
                    "correction_id": correction_id,
                    "target_fact_ids": [entry["replacement_fact_id"]],
                }, reason="correction_retracted")
            outcome = self._corrections.retract(character, correction_id)
            self._barrier.invalidate(character)
            entry = self._corrections.get(character, correction_id) or entry
            return self._receipt_from_entry(
                entry,
                int(outcome.get("version") or 0),
                duplicate=not bool(outcome.get("retracted")),
            )

    def _propagate(
        self,
        character: str,
        entry: Mapping[str, Any],
        *,
        request_id: str | None = None,
        duplicate: bool = False,
    ) -> CorrectionReceipt:
        """Invalidate matching facts, then walk registered derived stores."""
        correction_id = str(entry.get("correction_id") or "")
        derived = {"fact_ids": [], "reflection_ids": [], "persona_ids": []}
        invalidated_source_keys: list[str] = []
        replacement = None
        if entry.get("kind") == KIND_FACT_CORRECTION:
            invalidated, source_keys = self._invalidate_facts(character, entry)
            derived["fact_ids"] = invalidated
            invalidated_source_keys = source_keys
            if invalidated:
                for invalidator in self._derived_invalidators:
                    outcome = self._run_invalidator(
                        invalidator, character, invalidated, correction_id,
                    )
                    for name in ("reflection_ids", "persona_ids"):
                        values = outcome.get(name) or ()
                        derived[name] = _merge_ids(derived[name], values)
            if self._can_materialize(entry):
                replacement = self._materialize(character, entry, invalidated)
        self._corrections.set_derived(
            character,
            correction_id,
            derived=derived,
            invalidated_source_keys=invalidated_source_keys,
            replacement_fact_id=replacement,
        )
        stored = self._corrections.get(character, correction_id) or dict(entry)
        return self._receipt_from_entry(
            stored, self._corrections.version(character),
            duplicate=duplicate, request_id=request_id,
        )

    @staticmethod
    def _can_materialize(entry: Mapping[str, Any]) -> bool:
        return bool(entry.get("kind") == KIND_FACT_CORRECTION and entry.get("text")
                    and entry.get("action") != ACTION_RETRACT and not entry.get("expires_at"))

    def _needs_propagation(self, entry: Mapping[str, Any]) -> bool:
        return (not self._has_derived(entry)
                or self._can_materialize(entry) and not entry.get("replacement_fact_id"))

    def _materialize(self, character: str, entry: Mapping[str, Any], invalidated: Sequence[str]) -> str | None:
        replacement_id = "fact_" + entry["correction_id"]
        active = self._facts.load(character)
        archive = self._facts.load_archive(character)
        previous = active + archive
        if self._barrier.blocks_correction_entry(character, entry):
            return None
        existing = next((row for row in previous if row.get("id") == replacement_id), None)
        if existing is not None:
            if existing.get(FACT_INVALIDATED_REASON) == "correction_retracted":
                for field in (FACT_INVALIDATED_AT, FACT_INVALIDATED_BY, FACT_INVALIDATED_REASON):
                    existing.pop(field, None)
                self._facts.commit(character, active=active, archive=archive)
            return replacement_id
        target_ids = set(entry.get("target_fact_ids") or ()) | set(invalidated)
        targets = [row for row in previous if row.get("id") in target_ids]
        for target in targets:
            target_ids.update(target.get("correction_target_ids") or ())
        source = entry.get("source_key")
        keys = [source] if source else []
        text = entry["text"]
        created = entry.get("created_at") or self._clock.now().isoformat()
        row = {
            "id": replacement_id,
            "text": text,
            "importance": max((target["importance"] for target in targets
                               if isinstance(target.get("importance"), int)), default=5),
            "entity": next((target["entity"] for target in targets if target.get("entity")), "master"),
            "subject": entry["subject"],
            "applies_to": dict(entry.get("applies_to") or {}),
            "created_at": created,
            "retention_class": "durable",
            "schema_version": 2,
            "source": "user_correction",
            "evidence_kind": "user_assertion",
            "source_event_keys": keys,
            "support_groups": [keys] if keys else [],
            "correction_target_ids": sorted(target_ids),
            "absorbed": True,
            "signal_processed": True,
        }
        if entry.get("search_aliases"):
            row["search_aliases"] = list(entry["search_aliases"])
        if self._barrier.blocks_row(character, row):
            return None
        self._facts.save(character, active + [row])
        return replacement_id

    def _invalidate_facts(
        self, character: str, entry: Mapping[str, Any], *, reason: str = "user_correction",
    ) -> tuple[list[str], list[str]]:
        """Mark matching facts/archive rows invalidated; return ids and sources."""
        target_ids = {
            str(value) for value in entry.get("target_fact_ids") or ()
        }
        target_keys = {
            str(value) for value in entry.get("target_source_keys") or ()
        }
        old_text = str(entry.get("replaces_text") or "")
        subject = entry.get("subject")
        correction_scene = entry.get("applies_to") or {}
        rows: list[dict[str, Any]] = []
        active = list(self._facts.load(character))
        archive = list(self._facts.load_archive(character))
        invalidated_ids: list[str] = []
        invalidated_keys: list[str] = []
        changed_active = False
        changed_archive = False
        now = self._clock.now().isoformat()

        def _mark(row: dict[str, Any]) -> bool:
            if self._barrier.blocks_row(character, row):
                return False
            row_id = row.get("id")
            hit = isinstance(row_id, str) and row_id in target_ids
            if not hit and target_keys:
                keys = {
                    key for key in row.get("source_event_keys") or ()
                    if isinstance(key, str)
                }
                hit = bool(keys & target_keys)
            if not hit and old_text and text_matches(row.get("text"), old_text):
                if row_matches_subject(row, subject) and _scene_matches_fact(
                    row, correction_scene,
                ):
                    hit = True
            if not hit:
                return False
            changed = row.get(FACT_INVALIDATED_BY) != entry.get("correction_id")
            if changed:
                row[FACT_INVALIDATED_AT] = now
                row[FACT_INVALIDATED_BY] = str(entry.get("correction_id") or "")
                row[FACT_INVALIDATED_REASON] = reason
                row["signal_processed"] = True
                history = row.get("version_history")
                if not isinstance(history, list):
                    history = []
                history.append({
                    "text": str(row.get("text") or "")[:600],
                    "replaced_at": now,
                    "reason": "user_correction",
                    "source_fact_id": None,
                })
                row["version_history"] = history[-8:]
            row_id_text = row.get("id")
            if isinstance(row_id_text, str) and row_id_text and row_id_text not in invalidated_ids:
                invalidated_ids.append(row_id_text)
            for key in row.get("source_event_keys") or ():
                if isinstance(key, str) and key and key not in invalidated_keys:
                    invalidated_keys.append(key)
            return changed

        for row in active:
            if _mark(row):
                changed_active = True
        for row in archive:
            if _mark(row):
                changed_archive = True
        if changed_active or changed_archive:
            self._facts.commit(character, active=active, archive=archive)
        return invalidated_ids, invalidated_keys

    def _run_invalidator(
        self,
        invalidator: Any,
        character: str,
        fact_ids: Sequence[str],
        correction_id: str,
    ) -> Mapping[str, Any]:
        """Call one registered derived invalidator; a failure is not fatal.

        Facts are already invalidated at this point. A failing derived store
        leaves the correction entry with the fact ids it did invalidate, so a
        retried request can repair the derived part; the receipt still reports
        the correction as applied for the facts.
        """
        try:
            method = getattr(invalidator, "invalidate", None)
            if callable(method):
                outcome = method(
                    character,
                    list(fact_ids),
                    invalidated_at=self._clock.now().isoformat(),
                    reason="user_correction",
                    correction_id=correction_id,
                )
            else:
                outcome = invalidator(character, list(fact_ids))
        except Exception:  # noqa: BLE001 - repair path is a retried correction
            return {}
        return outcome if isinstance(outcome, Mapping) else {}

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _spec_from_request(request: CorrectionRequest) -> dict[str, Any]:
        kind = (
            KIND_FACT_CORRECTION
            if request.target_fact_ids
            or request.target_source_keys
            or request.old_text
            else "preference"
        )
        spec: dict[str, Any] = {
            "kind": kind,
            "action": request.action,
            "target_fact_ids": list(request.target_fact_ids),
            "target_source_keys": list(request.target_source_keys),
            "old_text": request.old_text,
            "new_text": request.new_text,
            "source_key": request.source_key,
            "applies_to": {
                "plugin_id": request.applies_to.plugin_id,
                "game_id": request.applies_to.game_id,
                "session_id": request.applies_to.session_id,
            },
            "expires_at": request.expires_at,
            "subject": request.scope.subject,
        }
        if request.action == ACTION_RETRACT:
            spec["new_text"] = ""
        return spec

    @staticmethod
    def _has_derived(entry: Mapping[str, Any]) -> bool:
        derived = entry.get("derived_invalidations") or {}
        if not isinstance(derived, Mapping):
            return False
        return any(derived.get(name) for name in ("fact_ids", "reflection_ids", "persona_ids"))

    @staticmethod
    def _receipt_from_entry(
        entry: Mapping[str, Any],
        version: int,
        *,
        duplicate: bool,
        request_id: str | None = None,
    ) -> CorrectionReceipt:
        derived = entry.get("derived_invalidations") or {}
        if not isinstance(derived, Mapping):
            derived = {}
        status = str(entry.get("status") or STATUS_ACTIVE)
        return CorrectionReceipt(
            correction_id=str(entry.get("correction_id") or ""),
            version=int(version or 0),
            applied=status == STATUS_ACTIVE,
            duplicate=duplicate,
            status="active" if status == STATUS_ACTIVE else "retracted",
            invalidated_fact_ids=tuple(derived.get("fact_ids") or ()),
            invalidated_source_keys=tuple(entry.get("invalidated_source_keys") or ()),
            invalidated_reflection_ids=tuple(derived.get("reflection_ids") or ()),
            invalidated_persona_ids=tuple(derived.get("persona_ids") or ()),
            replacement_fact_id=entry.get("replacement_fact_id"),
            request_id=request_id,
        )


def _scene_matches_fact(row: Mapping[str, Any], correction_scene: Any) -> bool:
    """Host rule: shared/global/all scopes compare equal to "no restriction"."""
    fact_scene = row.get("applies_to") or row.get("source_scene")
    fact_scene = _plain_scene(fact_scene)
    wanted = _plain_scene(correction_scene)
    if fact_scene.get("scope") in {"shared", "global", "all"}:
        fact_scene = {}
    if wanted.get("scope") in {"shared", "global", "all"}:
        wanted = {}
    return fact_scene == wanted


def _plain_scene(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for key in ("plugin_id", "game_id", "session_id", "scope"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            out[key] = item.strip()
    return out


def _merge_ids(current: Sequence[str], incoming: Any) -> list[str]:
    values = list(current)
    for item in incoming or ():
        if isinstance(item, str) and item and item not in values:
            values.append(item)
    return values[:32]


__all__ = [
    "CORRECTION_EVENT_TYPE",
    "CorrectionService",
]
