"""B03 forget engine: barrier first, bounded cleanup second, receipt last.

The order is the contract (design §17.9.1, §17.15):

1. **Register the barrier durably.** The deletion record and its tombstones are
   appended to the journal and saved before any content is touched, so a crash
   at any later point leaves the barrier active and cleanup retryable.
2. **Stop using the content.** The barrier cache refreshes immediately, so
   reads and replays stop serving the deleted rows even while cleanup runs.
3. **Clean bounded stores** step by step (\`resolve → facts → reflections →
   persona → recent → cache → plugin\`). Every step is idempotent and recorded
   in the deletion document; a failure leaves \`pending/failed\` steps and the
   receipt stays retryable.
4. **Report.** \`stopped_using\` is true once the barrier is durable;
   \`content_cleaned\` requires all host content steps terminal; an offline
   plugin keeps the whole deletion \`pending\` (never \`completed\`).

Stores this batch owns: facts + archive (raw view), the time index (episode
session rows) and the barrier. Reflection/persona/recent stores arrive in
B04/B05/B10; their steps report \`skipped\` with a batch note until a cleaner is
registered, and the deletion can be retried afterwards.

Entry vs episode scope keeps the host data ranges: an entry deletion removes
facts/archive rows but never original conversation messages; an episode
deletion also removes the time index's conversation rows and fails visibly
when that store is unavailable (it must not pretend the original text is
gone). Resolution results are persisted as tombstones for **both** scopes —
the host only did that for episodes, which let a retried entry deletion miss
derived rows resolved before a failed step.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from ..contracts import DeletionReceipt, DeletionRequest
from ..errors import DeletionConflictError, DeletionNotFoundError
from ..invalidation import normalize_text, row_matches_subject, text_hash
from ..persistence.deletions import STEP_NAMES, DeletionStore
from ..persistence.facts_view import FactsView
from ..persistence.source_ledger import SourceLedger
from ..persistence.timeindex import TimeIndexStore
from ..ports import Clock, EventJournal, OutboxStore, StorageRoots, WritePermit
from .barrier import DeletionBarrier, episode_ids_of

#: Host event name; written exactly once per registered deletion and replayed
#: by the deletion store (it restores the barrier, not the cleanup).
DELETION_EVENT_TYPE = "memory.deletion_registered"
#: Steps that clean host content; plugin receipts are tracked separately.
RUNNABLE_STEPS = ("resolve", "facts", "reflections", "persona", "recent", "cache")
#: Query types supported by \`deletable_entries\` in B03.
LISTABLE_TYPES = ("fact", "reflection", "persona")
MAX_LIST_LIMIT = 50


@dataclass(frozen=True)
class DeletionServices:
    """Optional cleaners that later batches register (B04/B05/B10/S02)."""

    reflections: Any = None
    persona: Any = None
    recent: Any = None
    #: S02/R01: invalidates state keyed by *source* rather than by character
    #: (whole-turn request results). Called with `(character, resolved)` on
    #: every deletion; returning `{"status": "failed", ...}` pauses the step
    #: like the other cleaners.
    turn_results: Any = None
    #: QP05: durable record of which plugin session consumed which memory ids.
    #: Used to find the plugins that hold a copy of a *web-created* fact, whose
    #: source key carries no plugin segment, and to retire that record when the
    #: plugin acknowledges the cleanup. Absent means "no consumer tracking".
    consumer_refs: Any = None


class DeletionService:
    """Independent \`forget\` entry point over one data root (single writer)."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        journal: EventJournal,
        deletions: DeletionStore,
        barrier: DeletionBarrier,
        facts: FactsView,
        clock: Clock,
        permit: WritePermit,
        timeindex_factory: Callable[[str], TimeIndexStore] | None = None,
        services: DeletionServices | None = None,
        cache_invalidators: Sequence[Callable[[str], None]] = (),
        ledger: SourceLedger | None = None,
        outbox: OutboxStore | None = None,
    ) -> None:
        self._roots = roots
        self._journal = journal
        self._deletions = deletions
        self._barrier = barrier
        self._facts = facts
        self._clock = clock
        self._permit = permit
        self._timeindex_factory = timeindex_factory
        # B02 stores that remember deleted sources: the ledger hint must not
        # survive a deletion, and queued work for deleted content must not run.
        self._ledger = ledger
        self._outbox = outbox
        self._services = services or DeletionServices()
        self._cache_invalidators = tuple(cache_invalidators)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        # A journal entry this build writes must be replayable; registering the
        # handler before the first write is the B02 recovery order.
        journal.register_handler(DELETION_EVENT_TYPE, deletions.replay_snapshot)

    # -- public API ------------------------------------------------------

    async def forget(self, request: DeletionRequest) -> DeletionReceipt:
        """Register the barrier, clean the owned stores and return the receipt."""
        return await asyncio.to_thread(self._forget_sync, request)

    async def retry(self, character: str, deletion_id: str, *, force: bool = False) -> DeletionReceipt:
        """Re-run pending/failed cleanup steps of an existing deletion."""
        return await asyncio.to_thread(self._retry_sync, character, deletion_id, force)

    async def record_plugin_receipt(
        self,
        character: str,
        deletion_id: str,
        plugin_id: str,
        status: str,
        *,
        cleaned: Sequence[str] | None = None,
        failed: Sequence[str] | None = None,
        detail: str | None = None,
    ) -> DeletionReceipt:
        """Record one plugin copy's cleanup receipt; keeps the deletion pending."""
        return await asyncio.to_thread(
            self._record_plugin_receipt_sync,
            character, deletion_id, plugin_id, status,
            cleaned, failed, detail,
        )

    def get(self, character: str, deletion_id: str) -> DeletionReceipt | None:
        receipt = self._deletions.get(character, deletion_id)
        return None if receipt is None else DeletionReceipt.from_mapping(receipt)

    def list(
        self, character: str, *, status: str | None = None, limit: int = 20,
    ) -> dict[str, Any]:
        return self._deletions.list(character, status=status, limit=limit)

    def pending_plugin_deletions(
        self, character: str, plugin_id: str, limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Deletions this plugin still has to clean.

        Besides the plugins named by the record itself, the ones whose recorded
        consumer references intersect a deletion's targets are included (QP05):
        a fact created on the web has no plugin segment in its source key, so
        the plugin that recalled and injected it is only discoverable this way.
        """
        consumed: list[str] = []
        refs: list[Mapping[str, Any]] = []
        if self._services.consumer_refs is not None:
            try:
                refs = self._services.consumer_refs.refs(character, plugin_id=plugin_id)
            except Exception:  # noqa: BLE001 - the queue must stay readable
                refs = []
            for row in refs:
                for item in row.get("memory_ids") or ():
                    if item not in consumed:
                        consumed.append(item)
        pending = self._deletions.pending_plugin_deletions(
            character, plugin_id, limit, memory_ids=consumed,
        )
        for row in pending:
            # The sessions that consumed these ids are the ones whose private
            # copies must go; naming them saves the plugin from guessing, and a
            # web-created fact carries no plugin source key to guess from. The
            # key is only added when there is something to say, so a client that
            # never opted in keeps seeing exactly the old item shape.
            wanted = set(row.get("memory_ids") or ())
            sessions = [
                str(ref.get("session_id") or "") for ref in refs
                if wanted.intersection(ref.get("memory_ids") or ())
            ]
            sessions = [text for text in dict.fromkeys(sessions) if text][:16]
            if sessions:
                row["consumer_sessions"] = sessions
        return pending

    def version(self, character: str) -> int:
        return self._deletions.version(character)

    def deletable_entries(
        self,
        character: str,
        *,
        query: str = "",
        types: Sequence[str] | None = None,
        limit: int = 20,
        subject: str | None = None,
    ) -> dict[str, Any]:
        """Entries the user could delete, newest first (host query contract).

        `subject` narrows the list to rows that belong to it. A chat client
        asking "what could I forget" must not see another subject's rows, so
        the filter is applied to the rows themselves rather than to the view.
        """
        return self._deletable_entries_sync(character, query, types, limit, subject)

    # -- forget flow -----------------------------------------------------

    def _forget_sync(self, request: DeletionRequest) -> DeletionReceipt:
        character = request.scope.character
        spec = self._spec_from_request(request)
        with self._lock(character):
            prepared = self._deletions.prepare_registration(character, spec)
            if prepared.conflict:
                raise DeletionConflictError(
                    f"deletion {prepared.deletion_id!r} already exists with a "
                    f"different payload",
                )
            if prepared.duplicate:
                receipt = self._deletions.get(character, prepared.deletion_id)
                return self._receipt(
                    receipt, request_id=request.request_id, duplicate=True,
                )
            payload = prepared.payload
            if payload is None:  # pragma: no cover - prepare guarantees a payload
                raise RuntimeError("prepared registration is missing its payload")
            # Journal first: a crash after this line replays the barrier; a
            # crash before it leaves nothing registered and nothing cleaned.
            self._journal.record_and_save(
                character,
                DELETION_EVENT_TYPE,
                payload,
                load_view=self._deletions.load_view,
                mutate_view=lambda view: self._deletions.apply_snapshot(view, payload),
                save_view=self._deletions.save_view,
            )
            if prepared.target_texts:
                # Bounded derived texts for plugin cleanup are not part of the
                # journal payload (the event log must not carry original text).
                self._deletions.add_target_texts(
                    character, prepared.deletion_id, list(prepared.target_texts),
                )
            # Barrier is durable: reads stop serving the content from here on.
            self._barrier.refresh(character)
            self._run_steps(character, prepared.deletion_id)
            receipt = self._deletions.get(character, prepared.deletion_id)
            return self._receipt(receipt, request_id=request.request_id)

    def _retry_sync(self, character: str, deletion_id: str, force: bool = False) -> DeletionReceipt:
        with self._lock(character):
            record = self._raw_record(character, deletion_id)
            if record is None:
                raise DeletionNotFoundError(f"unknown deletion {deletion_id!r}")
            if record.get("status") == "completed" and not force:
                return self._receipt(
                    self._deletions.get(character, deletion_id), duplicate=True,
                )
            steps = [
                name for name in RUNNABLE_STEPS
                if force or (record.get("steps") or {}).get(name, {}).get("status")
                not in ("completed", "skipped")
            ]
            plugin_step = (record.get("steps") or {}).get("plugin", {})
            if (
                record.get("plugin_ids")
                and plugin_step.get("status") not in ("completed", "skipped")
            ):
                steps.append("plugin")
            self._run_steps(character, deletion_id, steps=steps or None)
            return self._receipt(self._deletions.get(character, deletion_id))

    def _record_plugin_receipt_sync(
        self,
        character: str,
        deletion_id: str,
        plugin_id: str,
        status: str,
        cleaned: Sequence[str] | None,
        failed: Sequence[str] | None,
        detail: str | None,
    ) -> DeletionReceipt:
        with self._lock(character):
            receipt = self._deletions.record_plugin_receipt(
                character, deletion_id, plugin_id, status,
                cleaned=cleaned, failed=failed, detail=detail,
            )
            if status == "cleaned" and self._services.consumer_refs is not None:
                # The copy is gone: the reference that triggered this cleanup
                # has served its purpose. Only a *cleaned* receipt retires it —
                # a failed one must keep the pending notification alive.
                target_ids = self._deletions.plugin_target_ids(character, deletion_id)
                try:
                    self._services.consumer_refs.prune_cleaned(
                        character, plugin_id, target_ids,
                    )
                except Exception:  # noqa: BLE001 - the receipt is already written
                    pass
            self._barrier.refresh(character)
            return self._receipt(receipt)

    # -- steps -----------------------------------------------------------

    def _run_steps(
        self,
        character: str,
        deletion_id: str,
        *,
        steps: Sequence[str] | None = None,
    ) -> None:
        record = self._raw_record(character, deletion_id)
        if record is None:
            raise DeletionNotFoundError(f"unknown deletion {deletion_id!r}")
        wanted = set(steps or STEP_NAMES)
        resolved: dict[str, Any] = {
            "fact_ids": [],
            "reflection_ids": [],
            "persona_ids": [],
            "episode_ids": list(record.get("episode_ids") or []),
            "text_hashes": [],
            "fact_source_keys": [],
            "texts": [],
            "missing": [],
        }
        if "resolve" in wanted:
            try:
                resolved = self._resolve(character, deletion_id, record)
                self._deletions.update_step(
                    character, deletion_id, "resolve", "completed",
                    {
                        "facts": len(resolved["fact_ids"]),
                        "reflections": len(resolved["reflection_ids"]),
                        "persona": len(resolved["persona_ids"]),
                        "episodes": len(resolved["episode_ids"]),
                        "missing": len(resolved["missing"]),
                        # B05 narrowing hints: retries must not need to re-derive
                        # them from rows the earlier steps already changed.
                        "narrowed_reflection_ids": list(
                            resolved.get("narrowed_reflection_ids") or ()
                        ),
                        "narrowed_persona_ids": list(
                            resolved.get("narrowed_persona_ids") or ()
                        ),
                    },
                )
            except Exception as exc:  # noqa: BLE001 - visible failure, retryable
                self._deletions.update_step(
                    character, deletion_id, "resolve", "failed",
                    {"error": type(exc).__name__},
                )
        else:
            # Retry without a resolve step: use what the record persisted.
            resolved = self._resolved_from_record(record)
        # Cleaners need the scope: entry deletion only prunes summaries, an
        # episode deletion also removes original messages/time-index rows.
        resolved["scope"] = str(record.get("scope") or "entry")
        resolved["subject"] = str(record.get("subject") or "private")
        resolved["original_source_keys"] = list(record.get("source_keys") or ())

        if "facts" in wanted:
            self._run_step(
                character, deletion_id, "facts",
                lambda: self._delete_facts(character, resolved),
            )
        if "reflections" in wanted:
            self._run_cleaner(
                character, deletion_id, "reflections",
                self._services.reflections, resolved,
                unavailable_reason="reflection_store_not_migrated",
            )
        if "persona" in wanted:
            self._run_cleaner(
                character, deletion_id, "persona",
                self._services.persona, resolved,
                unavailable_reason="persona_store_not_migrated",
            )
        if "recent" in wanted:
            self._run_step(
                character, deletion_id, "recent",
                lambda: self._cleanup_original_text(character, record, resolved),
            )
        if "cache" in wanted:
            self._run_step(
                character, deletion_id, "cache",
                lambda: self._cleanup_caches(character),
            )
            # R01: whole-turn request results are keyed by source, so they need
            # the resolved payload rather than just the character. The step is
            # separate so a failure here is visible instead of hiding inside
            # the cache cleanup.
            self._run_cleaner(
                character, deletion_id, "turn_results",
                self._services.turn_results, resolved,
                unavailable_reason="turn_results_not_wired",
            )
        if "plugin" in wanted:
            self._attach_consumers(character, deletion_id, resolved)
            self._sync_plugin_step(character, deletion_id, self._raw_record(character, deletion_id) or record)
        # Step writes bumped the deletion version; keep the read fence current.
        self._barrier.refresh(character)

    def _run_step(
        self,
        character: str,
        deletion_id: str,
        step: str,
        action: Callable[[], dict[str, Any]],
    ) -> None:
        try:
            outcome = action()
        except Exception as exc:  # noqa: BLE001 - recorded as a visible failure
            self._deletions.update_step(
                character, deletion_id, step, "failed",
                {"error": type(exc).__name__},
            )
            return
        status = outcome.get("status", "completed")
        self._deletions.update_step(
            character, deletion_id, step, status, outcome.get("detail"),
        )

    def _run_cleaner(
        self,
        character: str,
        deletion_id: str,
        step: str,
        cleaner: Any,
        resolved: Mapping[str, Any],
        *,
        unavailable_reason: str,
    ) -> None:
        if cleaner is None:
            self._deletions.update_step(
                character, deletion_id, step, "skipped",
                {"reason": unavailable_reason},
            )
            return
        try:
            outcome = cleaner.cleanup(character, dict(resolved))
        except Exception as exc:  # noqa: BLE001
            self._deletions.update_step(
                character, deletion_id, step, "failed",
                {"error": type(exc).__name__},
            )
            return
        status = outcome.get("status", "completed") if isinstance(outcome, Mapping) else "completed"
        detail = outcome.get("detail") if isinstance(outcome, Mapping) else None
        self._deletions.update_step(character, deletion_id, step, status, detail)

    # -- resolve ---------------------------------------------------------

    def _resolve(
        self, character: str, deletion_id: str, record: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Resolve explicit targets into stored rows and persist the tombstones."""
        subject = record.get("subject") or "private"
        source_keys = set(record.get("source_keys") or ())
        episode_wanted = set(record.get("episode_ids") or ())
        targets = list(record.get("targets") or ()) + list(
            record.get("resolved_targets") or (),
        )
        explicit_facts = {
            str(item.get("id")) for item in targets
            if item.get("type") == "fact" and item.get("id")
        }
        explicit_reflections = [
            str(item.get("id")) for item in targets
            if item.get("type") == "reflection" and item.get("id")
        ]
        explicit_persona = [
            str(item.get("id")) for item in targets
            if item.get("type") == "persona" and item.get("id")
        ]
        target_hashes = {
            str(item.get("text_hash")) for item in targets if item.get("text_hash")
        }
        facts_rows = list(self._facts.load(character)) + list(
            self._facts.load_archive(character),
        )
        facts_by_id: dict[str, Mapping[str, Any]] = {}
        for row in facts_rows:
            row_id = row.get("id")
            if isinstance(row_id, str) and row_id:
                facts_by_id[row_id] = row
        matched_ids: list[str] = []
        matched_episodes: set[str] = set(episode_wanted)
        matched_hashes: set[str] = set(target_hashes)
        # The request's own source keys are part of the deletion scope even
        # when no stored fact row currently references them (the ledger hint
        # and queued work must still be cleaned).
        fact_source_keys: list[str] = list(record.get("source_keys") or ())
        texts: list[str] = []
        requested_keys = {str(key) for key in (record.get("source_keys") or ())}
        for row in facts_rows:
            row_id = row.get("id")
            row_keys = {
                key for key in row.get("source_event_keys") or ()
                if isinstance(key, str)
            }
            row_episodes = episode_ids_of(row)
            row_text = row.get("text")
            row_hash = text_hash(row_text) if isinstance(row_text, str) else ""
            hard = bool(
                (isinstance(row_id, str) and row_id in explicit_facts)
                or (episode_wanted and (row_episodes & episode_wanted))
                or (target_hashes and row_hash in target_hashes)
            )
            hit = hard
            if (
                not hit
                and not explicit_facts
                and source_keys
                and (source_keys & row_keys)
            ):
                if subject in ("", "private") or row_matches_subject(row, subject):
                    hit = True
            if not hit:
                continue
            if not hard and (row_keys - requested_keys):
                # Incidental match on a fact that still rests on another
                # source: the row survives, so nothing about it may become a
                # tombstone. Recording its id or text hash here would delete it
                # from recall even though the row itself was kept (G08).
                continue
            if isinstance(row_id, str) and row_id and row_id not in matched_ids:
                matched_ids.append(row_id)
            matched_episodes.update(row_episodes)
            if row_hash:
                matched_hashes.add(row_hash)
            for key in row_keys:
                if key not in fact_source_keys:
                    fact_source_keys.append(key)
            text = row.get("text")
            if isinstance(text, str) and text.strip() and text not in texts:
                texts.append(text)
        missing = [fact_id for fact_id in explicit_facts if fact_id not in facts_by_id]
        resolved = {
            "fact_ids": matched_ids,
            # Hard targets: the caller named this fact (or its exact text) or
            # deleted the episode it belongs to. A row matched only through a
            # source key may still stand on another source.
            "explicit_fact_ids": sorted(set(explicit_facts)),
            "target_text_hashes": sorted(target_hashes),
            # Only the keys the request actually named. `fact_source_keys` below
            # also collects keys found on the matched rows (those feed the plugin
            # tombstone); using that union as the deletion key set would shed a
            # surviving row remaining support too (G08).
            "requested_source_keys": sorted([str(key) for key in (record.get("source_keys") or ())]),
            "reflection_ids": explicit_reflections,
            "persona_ids": explicit_persona,
            "episode_ids": sorted(matched_episodes),
            "text_hashes": sorted(matched_hashes),
            "fact_source_keys": fact_source_keys,
            "texts": texts[:4],
            "missing": missing,
        }
        # B05: resolve derived rows BEFORE the facts step deletes their basis.
        # Once a reflection row is gone, persona cannot map fact ids back to its
        # source_id, so the explicit derived ids must be persisted in the record
        # now; otherwise the persona step would silently keep a stale entry.
        self._resolve_derived_targets(character, resolved)
        # Persist what resolution found: a retried deletion must not depend on
        # resolving again, and a restored old copy must still be blocked by
        # these tombstones (host gap fixed for entry scope too).
        self._deletions.set_resolved_targets(
            character,
            deletion_id,
            [
                {"type": "fact", "id": fact_id} for fact_id in matched_ids
            ] + [
                {"type": "reflection", "id": item} for item in explicit_reflections
            ] + [
                {"type": "persona", "id": item} for item in explicit_persona
            ],
            episode_ids=resolved["episode_ids"],
            text_hashes=resolved["text_hashes"],
        )
        if fact_source_keys:
            self._deletions.set_plugin_sources(
                character, deletion_id, fact_source_keys,
            )
        if texts:
            self._deletions.add_target_texts(
                character, deletion_id,
                [{"type": "derived", "id": "", "text": text} for text in texts[:4]],
            )
        return resolved

    def _resolve_derived_targets(
        self, character: str, resolved: dict[str, Any],
    ) -> None:
        """Ask the reflection/persona cleaners for the derived rows to delete.

        The services expose `resolve_ids` once their stores are migrated; a
        missing method means that store is not wired yet and its cleanup step
        stays `skipped`. A raising resolver fails the resolve step (visible,
        retryable) instead of quietly missing derived content.
        """
        reflections_resolver = getattr(self._services.reflections, "resolve_ids", None)
        if callable(reflections_resolver):
            extra = reflections_resolver(character, dict(resolved))
            if isinstance(extra, Mapping):
                resolved["reflection_ids"] = _unique_ids(
                    list(resolved.get("reflection_ids") or [])
                    + list(extra.get("reflection_ids") or ()),
                )
                resolved["narrowed_reflection_ids"] = _unique_ids(
                    list(resolved.get("narrowed_reflection_ids") or [])
                    + list(extra.get("narrowed_reflection_ids") or ()),
                )
        persona_resolver = getattr(self._services.persona, "resolve_ids", None)
        if callable(persona_resolver):
            extra = persona_resolver(
                character, dict(resolved), list(resolved.get("reflection_ids") or ()),
            )
            if isinstance(extra, Mapping):
                resolved["persona_ids"] = _unique_ids(
                    list(resolved.get("persona_ids") or [])
                    + list(extra.get("persona_ids") or ()),
                )
                resolved["narrowed_persona_ids"] = _unique_ids(
                    list(resolved.get("narrowed_persona_ids") or [])
                    + list(extra.get("narrowed_persona_ids") or ()),
                )

    def _resolved_from_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        targets = list(record.get("targets") or ()) + list(
            record.get("resolved_targets") or (),
        )
        resolve_detail = (record.get("steps") or {}).get("resolve", {}).get(
            "detail", {},
        )
        narrowed_reflections = [
            str(item) for item in resolve_detail.get("narrowed_reflection_ids") or ()
            if isinstance(item, str) and item
        ]
        narrowed_persona = [
            str(item) for item in resolve_detail.get("narrowed_persona_ids") or ()
            if isinstance(item, str) and item
        ]
        return {
            "fact_ids": [
                str(item.get("id")) for item in targets
                if item.get("type") == "fact" and item.get("id")
            ],
            "reflection_ids": [
                str(item.get("id")) for item in targets
                if item.get("type") == "reflection" and item.get("id")
            ],
            "persona_ids": [
                str(item.get("id")) for item in targets
                if item.get("type") == "persona" and item.get("id")
            ],
            "episode_ids": list(record.get("episode_ids") or ()),
            "text_hashes": [
                str(item.get("text_hash")) for item in targets
                if item.get("text_hash")
            ],
            "fact_source_keys": list(record.get("source_keys") or ()),
            "requested_source_keys": list(record.get("source_keys") or ()),
            "explicit_fact_ids": [
                str(item.get("id")) for item in targets
                if item.get("type") == "fact" and item.get("id")
            ],
            "target_text_hashes": [
                str(item.get("text_hash")) for item in targets
                if item.get("text_hash")
            ],
            "narrowed_reflection_ids": narrowed_reflections,
            "narrowed_persona_ids": narrowed_persona,
            "texts": [],
            "missing": [],
        }

    # -- cleanup steps ---------------------------------------------------

    def _delete_facts(
        self, character: str, resolved: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Remove resolved facts/archive rows; archive-first two-file commit."""
        fact_ids = {str(value) for value in resolved.get("fact_ids") or ()}
        text_hashes = {str(value) for value in resolved.get("text_hashes") or ()}
        # Match and shed only on the keys the request named; `fact_source_keys`
        # is the wider set persisted for plugin cleanup.
        requested = resolved.get("requested_source_keys")
        source_keys = {
            str(value) for value in (
                requested if requested is not None
                else resolved.get("fact_source_keys") or ()
            )
        }
        episode_ids = {str(value) for value in resolved.get("episode_ids") or ()}
        if not (fact_ids or text_hashes or source_keys or episode_ids):
            return {"status": "skipped", "detail": {"deleted": 0, "archive": 0}}

        def _matched(row: Mapping[str, Any]) -> bool:
            row_id = row.get("id")
            if isinstance(row_id, str) and row_id in fact_ids:
                return True
            text = row.get("text")
            if isinstance(text, str) and text_hash(text) in text_hashes:
                return True
            keys = {key for key in row.get("source_event_keys") or () if isinstance(key, str)}
            if keys & source_keys:
                return True
            return bool(episode_ids_of(row) & episode_ids)

        def _shed_support(row: dict[str, Any]) -> bool:
            """Drop the deleted keys from a row that still has other sources.

            A fact stated twice keeps existing when only one source is deleted;
            only a row left with no support at all is removed. Returns True when
            the row was modified in place.
            """
            keys = [
                str(key) for key in row.get("source_event_keys") or ()
                if isinstance(key, str)
            ]
            kept = [key for key in keys if key not in source_keys]
            if kept == keys:
                return False
            row["source_event_keys"] = kept
            if "source_bindings" in row:
                row["source_bindings"] = [binding for binding in row["source_bindings"]
                                          if binding.get("source_key") not in source_keys]
                row["message_refs"] = [binding["message_ref"] for binding in row["source_bindings"]]
            if "reflection_source_keys" in row:
                row["reflection_source_keys"] = [key for key in row["reflection_source_keys"] if key not in source_keys]
            return True

        hard_ids = {str(value) for value in resolved.get("explicit_fact_ids") or ()}
        hard_hashes = {str(value) for value in resolved.get("target_text_hashes") or ()}

        def _survives(row: Mapping[str, Any]) -> bool:
            if not _matched(row):
                return True
            # A row the caller named, or whose episode was deleted, always goes.
            row_id = row.get("id")
            if isinstance(row_id, str) and row_id in hard_ids:
                return False
            text = row.get("text")
            if isinstance(text, str) and text_hash(text) in hard_hashes:
                return False
            if episode_ids_of(row) & episode_ids:
                return False
            # Otherwise only the deleted source is shed; the row survives while
            # it still rests on another real source (G08).
            if isinstance(row, dict) and _shed_support(row):
                return bool(row.get("source_event_keys"))
            return False

        active = list(self._facts.load(character))
        archive = list(self._facts.load_archive(character))
        before_active = [(row.get("id"), tuple(row.get("source_event_keys") or ()))
                         for row in active if isinstance(row, dict)]
        kept_active = [row for row in active if _survives(row)]
        before_archive = [(row.get("id"), tuple(row.get("source_event_keys") or ()))
                          for row in archive if isinstance(row, dict)]
        kept_archive = [row for row in archive if _survives(row)]
        deleted_active = len(active) - len(kept_active)
        deleted_archive = len(archive) - len(kept_archive)
        # A surviving row whose support shrank is still a real change: committing
        # only when a row disappeared silently discarded the support removal.
        after_active = [(row.get("id"), tuple(row.get("source_event_keys") or ()))
                        for row in kept_active if isinstance(row, dict)]
        after_archive = [(row.get("id"), tuple(row.get("source_event_keys") or ()))
                         for row in kept_archive if isinstance(row, dict)]
        if deleted_active or deleted_archive or before_active != after_active or before_archive != after_archive:
            self._facts.commit(
                character, active=kept_active, archive=kept_archive,
            )
        detail: dict[str, Any] = {
            "deleted": deleted_active,
            "archive": deleted_archive,
            "missing": len(fact_ids),
        }
        # Ledger hints and queued work are part of the deletion scope even
        # when no fact row matched (e.g. an episode whose facts never existed).
        if self._ledger is not None and source_keys:
            try:
                detail["ledger_removed"] = self._ledger.remove(
                    character, sorted(source_keys),
                )
            except Exception as exc:  # noqa: BLE001 - visible but not fatal
                detail["ledger_error"] = type(exc).__name__
        if self._outbox is not None:
            try:
                detail["outbox_cancelled"] = self._cancel_queued_work(
                    character, source_keys, episode_ids,
                )
            except Exception as exc:  # noqa: BLE001
                detail["outbox_error"] = type(exc).__name__
        status = "completed" if (deleted_active or deleted_archive) else "skipped"
        return {"status": status, "detail": detail}

    def _cancel_queued_work(
        self,
        character: str,
        source_keys: set[str],
        episode_ids: set[str],
    ) -> int:
        """Mark queued outbox ops that target deleted content as done (never run).

        Extraction/revision ops are at-least-once; deleting their input while
        they are still queued must drop them, otherwise they would write facts
        from deleted content. Matching is deliberately conservative: an op is
        dropped when its serialized payload mentions a deleted source key or
        episode id.
        """
        import json as _json

        cancelled = 0
        for op in self._outbox.pending(character, limit=200):
            try:
                text = _json.dumps(dict(op.payload), ensure_ascii=False)
            except (TypeError, ValueError):
                continue
            if any(key and key in text for key in source_keys) or any(
                episode_id and episode_id in text for episode_id in episode_ids
            ):
                self._outbox.complete(character, op.op_id)
                cancelled += 1
        return cancelled

    def _cleanup_original_text(
        self, character: str, record: Mapping[str, Any], resolved: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Explicit sources/episodes remove original rows; fact entries prune summaries.

        B04 registers `DeletionServices.recent`; without it an entry deletion
        skips the step (the host rule) and an episode deletion still fails
        visibly because original text must not survive. When a recent store is
        registered, the episode deletion requires both it and the time index to
        succeed before the step is `completed` (host two-store rule).
        """
        scope = str(record.get("scope") or "entry")
        cleaner = self._services.recent
        original_sources = list(record.get("source_keys") or ())
        if scope != "episode" and not original_sources:
            if cleaner is None:
                return {
                    "status": "skipped",
                    "detail": {"reason": "entry_scope_keeps_original_messages"},
                }
            outcome = cleaner.cleanup(character, dict(resolved))
            if isinstance(outcome, Mapping):
                if outcome.get("status") == "failed":
                    return dict(outcome)
                return {
                    "status": outcome.get("status") or "completed",
                    "detail": outcome.get("detail") or {},
                }
            return {"status": "completed", "detail": {}}
        episode_ids = [
            str(value) for value in resolved.get("episode_ids") or ()
        ] or [str(value) for value in record.get("episode_ids") or ()]
        session_ids = self._session_ids_for(episode_ids)
        detail: dict[str, Any] = {"episodes": len(episode_ids)}
        if cleaner is not None:
            outcome = cleaner.cleanup(character, dict(resolved))
            if isinstance(outcome, Mapping):
                if outcome.get("status") == "failed":
                    return dict(outcome)
                detail.update(outcome.get("detail") or {})
        if self._timeindex_factory is None:
            return {
                "status": "failed",
                "detail": {"reason": "timeindex_unavailable", **detail},
            }
        store = self._timeindex_factory(character)
        try:
            removed = store.delete_sessions(session_ids) if scope == "episode" else store.delete_sources(original_sources)
        finally:
            store.close()
        detail["conversations_removed"] = removed
        return {"status": "completed", "detail": detail}

    def _cleanup_caches(self, character: str) -> dict[str, Any]:
        cleared: list[str] = []
        self._barrier.invalidate(character)
        for invalidator in self._cache_invalidators:
            name = getattr(invalidator, "__name__", "cache")
            invalidator(character)
            cleared.append(name)
        cleared.append("deletion_barrier")
        return {"status": "completed", "detail": {"cleared": cleared}}

    def _attach_consumers(
        self, character: str, deletion_id: str, resolved: Mapping[str, Any],
    ) -> None:
        """Add the plugins known to have consumed the deleted ids (QP05).

        The ids come from the record itself (targets plus what resolution
        found), so a plugin that only ever saw a derived copy is still reached.
        Appending is idempotent; a failure here must not fail the deletion, so
        the step keeps whatever the source keys already established.
        """
        store = self._services.consumer_refs
        if store is None:
            return
        target_ids = list(self._deletions.plugin_target_ids(character, deletion_id))
        for extra in (
            resolved.get("fact_ids") or (),
            resolved.get("reflection_ids") or (),
            resolved.get("persona_ids") or (),
        ):
            for item in extra:
                text = str(item or "").strip()
                if text and text not in target_ids:
                    target_ids.append(text)
        if not target_ids:
            return
        try:
            plugin_ids = store.plugin_ids_for(character, target_ids)
        except Exception:  # noqa: BLE001 - the queue stays readable
            return
        if plugin_ids:
            self._deletions.add_plugin_ids(character, deletion_id, plugin_ids)

    def _sync_plugin_step(
        self, character: str, deletion_id: str, record: Mapping[str, Any],
    ) -> None:
        plugin_ids = list(record.get("plugin_ids") or ())
        current = (record.get("steps") or {}).get("plugin", {}).get("status")
        if plugin_ids:
            if current != "completed":
                self._deletions.update_step(
                    character, deletion_id, "plugin", "pending",
                    {"plugin_ids": plugin_ids},
                )
            return
        if current in ("pending", "failed"):
            self._deletions.update_step(character, deletion_id, "plugin", "skipped")

    def owns_entry(self, character: str, subject: str, kind: str, entry_id: str) -> bool:
        """Check an explicit client deletion target before resolving its effects."""
        if kind == "fact":
            rows = list(self._facts.load(character)) + list(self._facts.load_archive(character))
        elif kind == "reflection" and callable(getattr(self._services.reflections, "load", None)):
            rows = self._services.reflections.load(character) or ()
        else:
            return False
        return any(isinstance(row, Mapping) and row.get("id") == entry_id
                   and row_matches_subject(row, subject) for row in rows)

    # -- deletable entries ----------------------------------------------

    def _deletable_entries_sync(
        self, character: str, query: str, types: Sequence[str] | None, limit: int,
        subject: str | None = None,
    ) -> dict[str, Any]:
        wanted = {
            name for name in (types or LISTABLE_TYPES) if name in LISTABLE_TYPES
        } or set(LISTABLE_TYPES)
        cap = max(1, min(int(limit or 20), MAX_LIST_LIMIT))
        needle = normalize_text(query)
        # A caller that names a subject only ever sees rows that belong to it.
        # Rows without a subject read as `private`, so the default view of a
        # private caller is unchanged; an explicit subject never widens.
        scoped_subject = None if not subject else str(subject)
        items: list[dict[str, Any]] = []
        if "fact" in wanted:
            rows = list(self._facts.load(character)) + list(
                self._facts.load_archive(character),
            )
            for row in rows:
                if self._barrier.blocks_row(character, row):
                    continue
                if scoped_subject and not row_matches_subject(row, scoped_subject):
                    continue
                row_id = row.get("id")
                text = row.get("text")
                if not isinstance(row_id, str) or not row_id or not isinstance(text, str):
                    continue
                if needle and needle not in normalize_text(text):
                    continue
                source_keys = [
                    key for key in row.get("source_event_keys") or ()
                    if isinstance(key, str)
                ][:8]
                item: dict[str, Any] = {
                    "type": "fact",
                    "id": row_id,
                    "text": text[:600],
                    "source_keys": source_keys,
                    "created_at": row.get("created_at") or row.get("date") or "",
                    "importance": row.get("importance"),
                    "invalidated": bool(row.get("invalidated_at")),
                }
                items.append(item)
        reflections = self._services.reflections
        if "reflection" in wanted and callable(getattr(reflections, "load", None)):
            for row in reflections.load(character) or ():
                if not isinstance(row, Mapping):
                    continue
                if self._barrier.blocks_row(character, row):
                    continue
                if scoped_subject and not row_matches_subject(row, scoped_subject):
                    continue
                row_id = row.get("id")
                text = row.get("text")
                if not isinstance(row_id, str) or not row_id or not isinstance(text, str):
                    continue
                if needle and needle not in normalize_text(text):
                    continue
                items.append({
                    "type": "reflection",
                    "id": row_id,
                    "text": text[:600],
                    "source_keys": [
                        key for key in row.get("source_event_keys") or ()
                        if isinstance(key, str)
                    ][:8],
                    "created_at": row.get("created_at") or "",
                    "status": row.get("status"),
                    "invalidated": bool(
                        row.get("invalidated_sources") or row.get("needs_rebuild")
                    ),
                })
        persona = self._services.persona
        if "persona" in wanted and callable(getattr(persona, "entries", None)):
            for row in persona.entries(character) or ():
                if not isinstance(row, Mapping):
                    continue
                if scoped_subject and not row_matches_subject(row, scoped_subject):
                    continue
                row_id = row.get("id")
                text = row.get("text")
                if not isinstance(row_id, str) or not row_id or not isinstance(text, str):
                    continue
                if needle and needle not in normalize_text(text):
                    continue
                items.append({
                    "type": "persona",
                    "id": row_id,
                    "text": text[:600],
                    "source_keys": [],
                    "created_at": row.get("created_at") or row.get("updated_at") or "",
                    "status": "suppressed" if row.get("suppress") else "active",
                    "invalidated": bool(row.get("invalidated_at")),
                })
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {"ok": True, "items": items[:cap]}

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _spec_from_request(request: DeletionRequest) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "scope": request.mode,
            "subject": request.scope.subject,
            "reason": request.reason,
            "targets": [target.as_payload() for target in request.targets],
            "source_keys": list(request.source_keys),
            "episode_ids": list(request.episode_ids),
        }
        if request.deletion_id:
            spec["deletion_id"] = request.deletion_id
        return spec

    @staticmethod
    def _session_ids_for(episode_ids: Sequence[str]) -> list[str]:
        """Session ids a time-index row may use for these episodes."""
        sessions: list[str] = []
        for episode_id in episode_ids:
            value = str(episode_id or "").strip()
            if not value:
                continue
            if value not in sessions:
                sessions.append(value)
            parts = value.split(":")
            if len(parts) >= 4 and parts[0] == "ep":
                session = parts[-1]
                if session and session not in sessions:
                    sessions.append(session)
        return sessions

    def _receipt(
        self,
        receipt: Mapping[str, Any] | None,
        *,
        request_id: str | None = None,
        duplicate: bool = False,
    ) -> DeletionReceipt:
        if receipt is None:
            raise DeletionNotFoundError("deletion receipt disappeared after write")
        data = dict(receipt)
        if duplicate:
            data["duplicate"] = True
        return DeletionReceipt.from_mapping(data, request_id=request_id)

    def _raw_record(
        self, character: str, deletion_id: str,
    ) -> dict[str, Any] | None:
        document = self._deletions.snapshot(character)
        for record in document.get("deletions") or ():
            if isinstance(record, dict) and record.get("deletion_id") == deletion_id:
                return record
        return None

    def _lock(self, character: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(character)
            if lock is None:
                lock = threading.RLock()
                self._locks[character] = lock
            return lock


def _unique_ids(values: Sequence[Any]) -> list[str]:
    """Stable, de-duplicated string ids (order of first appearance)."""
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value and value not in out:
            out.append(value)
    return out


__all__ = [
    "DELETION_EVENT_TYPE",
    "DeletionService",
    "DeletionServices",
    "LISTABLE_TYPES",
    "RUNNABLE_STEPS",
]
