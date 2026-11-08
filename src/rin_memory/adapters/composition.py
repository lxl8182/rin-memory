"""Explicit composition of the B03 memory core (no host imports, no globals).

\`build_memory_core\` wires one data root and returns the independent entry
points: durable event ingest (with the deletion barrier in front), correction
and deletion services, and the shared barrier/facts/journal stores. This is
what an embedder (B08) or the standalone entry (B07) constructs; the core
never discovers Documents, host config or the current character on its own.

Recovery order matches B02/B03: the journal replay handlers are registered in
the constructors (source ledger, deletion store, correction store), then
\`MemoryCore.recover\` reconciles the journal before any new write. Barrier
state is read from disk, so replayed deletion events restore the fence even
when the tail contains older source events.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..contracts import RecoveryResult
from ..domain.barrier import DeletionBarrier
from ..domain.compression import CompressionService, CompressionSettings
from ..domain.corrections import CorrectionService
from ..domain.deletions import DeletionService, DeletionServices
from ..domain.extraction import ExtractionService, ExtractionSettings
from ..domain.maintenance import MaintenanceService, MaintenanceSettings
from ..domain.outbox_dispatch import OutboxDispatcher
from ..domain.outputs import EVT_OUTPUT_RECORDED, OutputService  # noqa: F401 - event type
from ..domain.conversation_state import ConversationState
from ..domain.persona import PersonaService
from ..domain.evolution import EvolutionService, EvolutionSettings
from ..domain.growth_evidence import MEMORY_KINDS, evidence_dto
from ..domain.evolution_scheduler import EvolutionScheduler, SchedulerSettings
from ..domain.persona_admin import PersonaAdminService
from ..domain.persona_card import PersonaCardService
from ..domain.persona_projection import PersonaProjectionService
from ..domain.persona_publish import PersonaPublishService
from ..domain.persona_teaching import PersonaTeachingService
from ..persistence.persona_teaching import PersonaTeachingStore
from ..domain.proactive import EVT_PROACTIVE_CANDIDATE, ProactiveService, ProactiveSettings  # noqa: F401 - event type
from ..domain.recall import RecallService, RecallSettings
from ..domain.recent import RecentDeletionCleaner
from ..domain.turn_results import CorrectionTurnInvalidator, TurnResultCleaner
from ..domain.reflection import (
    PROMOTION_RULES,
    ReflectionService,
    ReflectionSettings,
)
from ..domain.state import StateService
from ..domain.working_summary import WorkingSummaryService
from ..domain.profile import InterestService
from ..persistence.interest_signals import InterestSignalStore
from ..persistence.atomic import FileSystemAtomicStore
from ..persistence.clock import SystemClock
from ..persistence.consumer_refs import ConsumerRefStore
from ..persistence.corrections import CorrectionStore
from ..persistence.deletions import DeletionStore
from ..persistence.directives import UserDirectiveStore
from ..persistence.facts_view import FactsView
from ..persistence.gates import SuppressionGate
from ..persistence.journal import EventJournal
from ..persistence.outbox import OutboxStore
from ..persistence.outputs import OutputStore
from ..persistence.recent import RecentStore
from ..persistence.persona import PersonaStore
from ..persistence.persona_card import PersonaCardStore
from ..persistence.evolution import EvolutionStore
from ..persistence.evolution_runs import EvolutionRunStore
from ..persistence.persona_admin import PersonaAdminStore
from ..persistence.persona_projection import PersonaProjectionStore
from ..persistence.persona_versions import PersonaVersionStore
from ..persistence.permits import NullWritePermit
from ..persistence.proactive import ProactiveStore
from ..persistence.reflections import ReflectionStore
from ..persistence.roots import FileSystemRoots
from ..persistence.shards import ArchiveShardStore
from ..persistence.source_ledger import SourceLedger
from ..persistence.state import StateStore
from ..persistence.timeindex import TimeIndexStore
from ..persistence.tombstones import ScopedTombstoneStore
from ..persistence.turns import TURN_EVENT_TYPE, TurnRequestStore
from ..persistence.working_summary import WorkingSummaryStore
from ..ports import (
    AtomicJsonStore,
    Clock,
    EmbeddingProvider,
    ModelInvoker,
    StorageRoots,
    TokenCounter,
    WritePermit,
)
from .durable import DurableEventStore


def _evolution_evidence(
    facts: FactsView,
    reflections: ReflectionStore,
    *,
    promotion_rules: Mapping[str, Sequence[str]],
    min_support: int = 2,
):
    """Default B12 evidence rows as the unified M4 evidence DTO.

    The old provider read different fields from the reflection rows than the
    reflection service writes (`source_refs`/`source_keys`, no scope, no
    maturity), so the evolution prompt could receive empty sources and a lost
    scope. Rows are now built by `domain.growth_evidence.evidence_dto`, the same
    shape the reflection layer derives: identity, origin (fact ids, event keys,
    independent experiences), scope, maturity and eligibility. Facts are carried
    as explanation only; their DTO is explicitly ineligible.
    """

    def collect(character: str, request: Any = None) -> list[dict]:
        wanted = set(getattr(request, "source_refs", ()) or ())
        rows: list[dict] = []
        for row in facts.load(character):
            fact_row = {
                "id": str(row.get("id") or ""),
                "kind": "fact",
                "text": str(row.get("text") or ""),
                "source_keys": [
                    str(item) for item in row.get("source_event_keys") or ()
                ],
                "status": str(row.get("status") or ""),
                "source": str(row.get("source") or ""),
                "applies_to": dict(row.get("applies_to") or {}),
                "invalidated_at": row.get("invalidated_at"),
                "subject": row.get("subject"),
            }
            dto = evidence_dto(
                fact_row, kind="fact",
                promotion_rules=promotion_rules, min_support=min_support,
            )
            dto["evidence_kind"] = row.get("evidence_kind")
            rows.append({**fact_row, "evidence": dto})
        for row in reflections.load(character):
            if not isinstance(row, Mapping):
                continue
            memory_kind = str(row.get("memory_kind") or "")
            kind = "growth" if memory_kind in MEMORY_KINDS else "reflection"
            source_row = dict(row)
            source_row["kind"] = kind
            dto = evidence_dto(
                source_row, kind=kind,
                promotion_rules=promotion_rules, min_support=min_support,
            )
            rows.append({
                "id": dto["id"],
                "kind": kind,
                "text": dto["text"],
                "source_keys": list(dto["source_keys"]),
                "source_fact_ids": list(dto["source_fact_ids"]),
                "status": dto["status"],
                "memory_kind": memory_kind,
                "applies_to": dict(dto["applies_to"]),
                "scope_key": dto["scope_key"],
                "invalidated_at": row.get("invalidated_at"),
                "needs_rebuild": row.get("needs_rebuild"),
                "subject": row.get("subject"),
                "evidence": dto,
            })
        if wanted:
            matched = [
                row for row in rows
                if wanted & (
                    {row["id"]}
                    | set(row["source_keys"])
                    | set(row.get("source_fact_ids") or ())
                )
            ]
            # Facts that the matched evidence rests on stay in the pool as
            # *explanation* even when the caller only named the understanding:
            # the model needs to see what the conclusion is based on, while the
            # eligibility gate still refuses to let a fact qualify on its own.
            referenced = set()
            for row in matched:
                referenced.update(row.get("source_fact_ids") or ())
                referenced.update(row.get("source_keys") or ())
            explanation: list[dict] = []
            for row in rows:
                if row.get("kind") != "fact":
                    continue
                if (
                    row in matched
                    or row["id"] in referenced
                    or set(row.get("source_keys") or ()) & referenced
                ):
                    explanation.append(row)
            rows = explanation + [
                row for row in matched if row.get("kind") != "fact"
            ]
        return rows

    return collect


class _PublishInvalidator:
    """Correction fan-out: invalidate published versions by fact ids."""

    def __init__(self, publish: PersonaPublishService) -> None:
        self._publish = publish

    def invalidate(self, character, fact_ids, **kwargs) -> dict:
        resolved = {"fact_ids": list(fact_ids or ())}
        before = self._publish.current(character)
        result = self._publish.invalidate_for_deletion(character, resolved)
        card = self._publish._card_service.get(character)
        if result.get("current_invalidated") and before.revision == card.revision and any(section.content_basis for section in card.sections):
            self._publish.publish(character, revision=card.revision, reason="source_corrected")
        return result


class _ProjectionInvalidator:
    """Correction fan-out: invalidate the projection/consumer versions."""

    def __init__(self, projection: PersonaProjectionService) -> None:
        self._projection = projection

    def invalidate(self, character, fact_ids, **kwargs) -> dict:
        return self._projection.invalidate_for_correction(
            character, list(fact_ids or ()),
        )


class _ProjectionCleaner:
    """Deletion cleaner step: invalidate projection and consumer versions."""

    def __init__(self, projection: PersonaProjectionService) -> None:
        self._projection = projection

    def cleanup(self, character: str, resolved: dict) -> dict:
        outcome = self._projection.invalidate_for_deletion(character, resolved)
        return {"status": "completed", "detail": dict(outcome or {})}


class _AdminCleaner:
    """Deletion cleaner step: withdraw proposals / filter admin decisions."""

    def __init__(self, admin: PersonaAdminService) -> None:
        self._admin = admin

    def cleanup(self, character: str, resolved: dict) -> dict:
        outcome = self._admin.cleanup(character, resolved)
        return {"status": "completed", "detail": dict(outcome or {})}


class _PublishCleaner:
    """Deletion cleaner step: invalidate published versions for the scope."""

    def __init__(self, publish: PersonaPublishService) -> None:
        self._publish = publish

    def cleanup(self, character: str, resolved: dict) -> dict:
        outcome = self._publish.invalidate_for_deletion(character, resolved)
        return {"status": "completed", "detail": dict(outcome)}


class _OutputCleaner:
    """Deletion cleaner: retire the assistant outputs a deleted cause produced.

    An output whose cause (a task result, an open thread's source) is deleted
    stops being a legitimate thing she said: the body is cleared and the record
    is kept as an invalidated shell, so the id stays addressable and a client
    holding the old list cannot resurrect the text (design section 6.4: deletion
    reaches derived records, and the summary/projection is rebuilt).
    """

    def __init__(self, outputs: Any) -> None:
        self._outputs = outputs

    def cleanup(self, character: str, resolved: dict) -> dict:
        if self._outputs is None:
            return {"status": "skipped", "detail": {"reason": "outputs_not_wired"}}
        keys = {
            str(value) for value in (
                list(resolved.get("fact_source_keys") or ())
                + list(resolved.get("original_source_keys") or ())
            ) if value
        }
        episode_ids = {
            str(value) for value in resolved.get("episode_ids") or () if value
        }
        if not keys and not episode_ids:
            return {"status": "completed", "detail": {"invalidated": 0}}
        invalidated = 0
        for row in self._outputs.list(character, limit=512):
            if str(row.get("status") or "") == "invalidated":
                continue
            cause = row.get("cause") if isinstance(row.get("cause"), Mapping) else {}
            cause_keys = {str(value) for value in cause.get("source_keys") or () if value}
            if not (cause_keys & keys):
                continue
            receipt = self._outputs.invalidate(
                character, str(row.get("output_id") or ""),
                reason="cause_deleted",
            )
            if receipt.ok or receipt.duplicate:
                invalidated += 1
        return {"status": "completed", "detail": {"invalidated": invalidated}}


class _ProactiveCleaner:
    """Deletion cleaner: retire candidates whose reason was deleted.

    A proactive candidate names the records that justify it, so a deletion that
    reaches those records must reach the candidate too -- otherwise a queued
    message would resurrect content the user deleted, which is exactly the
    failure the B03 barrier exists to prevent.
    """

    def __init__(self, proactive: Any) -> None:
        self._proactive = proactive

    def cleanup(self, character: str, resolved: dict) -> dict:
        if self._proactive is None:
            return {"status": "skipped", "detail": {"reason": "proactive_not_wired"}}
        return self._proactive.invalidate_for_deletion(character, resolved)


class _WorkingSummaryPair:
    """The projector plus its audit store, as maintenance wants them."""

    __slots__ = ("projector", "store")

    def __init__(self, projector: Any, store: Any) -> None:
        self.projector = projector
        self.store = store


class _SummaryCacheInvalidator:
    """Cache invalidator: mark the audit summary stale after a deletion.

    Only the audit row is touched: the summaries themselves are projections and
    are re-derived on the next read, so there is nothing else to invalidate.
    """

    def __init__(self, store: Any) -> None:
        self._store = store
        self.__name__ = "working_summary_cache"

    def __call__(self, character: str) -> None:
        if self._store is None:
            return
        self._store.invalidate(character, reason="deletion")


class _CombinedCleaner:
    """Run several deletion cleaners behind the one persona cleanup step."""

    def __init__(self, *cleaners: Any) -> None:
        self._cleaners = tuple(cleaners)

    def cleanup(self, character: str, resolved: dict) -> dict:
        detail: dict = {}
        statuses: list[str] = []
        for cleaner in self._cleaners:
            outcome = cleaner.cleanup(character, dict(resolved))
            if not isinstance(outcome, Mapping):
                statuses.append("completed")
                continue
            statuses.append(str(outcome.get("status") or "completed"))
            nested = outcome.get("detail")
            if isinstance(nested, Mapping):
                detail.update(dict(nested))
        if any(status == "failed" for status in statuses):
            status = "failed"
        elif any(status == "pending" for status in statuses):
            status = "pending"
        else:
            status = "completed"
        return {"status": status, "detail": detail}


@dataclass(frozen=True)
class MemoryCore:
    """One fully wired memory core over an explicit data root."""

    roots: StorageRoots
    store: AtomicJsonStore
    clock: Clock
    permit: WritePermit
    journal: EventJournal
    ledger: SourceLedger
    outbox: OutboxStore
    sink: DurableEventStore
    turn_requests: TurnRequestStore
    consumer_receipts: Any
    #: QP05: which plugin session consumed which memory ids (ids only).
    consumer_refs: Any
    deletions: DeletionStore
    corrections: CorrectionStore
    directives: UserDirectiveStore
    tombstones: ScopedTombstoneStore
    barrier: DeletionBarrier
    facts: FactsView
    recent: RecentStore
    gates: SuppressionGate
    reflections: ReflectionStore
    persona: PersonaStore
    deletion_service: DeletionService
    correction_service: CorrectionService
    extraction: ExtractionService
    compression: CompressionService
    reflection_service: ReflectionService
    persona_service: PersonaService
    persona_card: PersonaCardStore
    persona_card_service: PersonaCardService
    persona_versions: PersonaVersionStore
    persona_publish: PersonaPublishService
    persona_teaching: PersonaTeachingService
    persona_projection: PersonaProjectionStore
    persona_projection_service: PersonaProjectionService
    evolution: EvolutionStore
    evolution_service: EvolutionService
    persona_admin: PersonaAdminStore
    persona_admin_service: PersonaAdminService
    evolution_runs: EvolutionRunStore
    evolution_scheduler: EvolutionScheduler
    recall: RecallService
    state: StateStore
    state_service: StateService
    outbox_dispatcher: OutboxDispatcher
    maintenance: MaintenanceService
    timeindex_factory: Callable[[str], TimeIndexStore]
    #: M5: assistant-output records (body + cause + delivery receipts) and the
    #: rebuildable working-summary projector. Both are additive: a core built by
    #: an embedder that ignores them behaves exactly as before.
    outputs: OutputStore
    output_service: OutputService
    #: The audit cache for the working summaries. The projector that derives
    #: them is 'working_summary_service'; no read path consumes this store.
    working_summary: WorkingSummaryStore
    working_summary_service: WorkingSummaryService
    #: M6-A: durable proactive candidates (cause + source + state) and the
    #: pending-chat intent queue projector/service. Additive like M5: a core that
    #: ignores them behaves exactly as before.
    proactive: ProactiveStore
    proactive_service: ProactiveService
    #: RO03: interest evidence (see domain/profile.py) and its store, so a
    #: reader can report the data version without going through the service.
    interests: InterestService
    interest_signals: InterestSignalStore

    def recover(self, character: str) -> RecoveryResult:
        """Replay the unapplied journal tail; idempotent, call before writes.

        After the replay, S02 marks turn requests that are still `pending` as
        `interrupted`: they belong to a process that died between reserve and
        commit, and leaving them pending would make a retry look like a
        concurrent attempt forever.
        """
        self.barrier.refresh(character)
        result = self.sink.recover(character)
        if not getattr(result, "paused", False):
            self.persona_teaching.retry_pending(character)
            self.correction_service.repair_replacements(character)
        try:
            self.turn_requests.mark_interrupted_pending(character)
        except Exception:  # noqa: BLE001 - recovery must not fail on bookkeeping
            pass
        return result

    def timeindex(self, character: str) -> TimeIndexStore:
        """An unopened time-index handle for \`character\` (callers open it)."""
        return self.timeindex_factory(character)


def build_memory_core(
    *,
    memory_root: Path | str,
    local_state_root: Path | str | None = None,
    permit: WritePermit | None = None,
    clock: Clock | None = None,
    atomic_store: AtomicJsonStore | None = None,
    ttl_seconds: float | None = None,
    ttl_max_seconds: float | None = None,
    deletion_services: DeletionServices | None = None,
    derived_invalidators=(),
    cache_invalidators=(),
    model: ModelInvoker | None = None,
    extraction_settings: ExtractionSettings | None = None,
    compression_settings: CompressionSettings | None = None,
    reflection_settings: ReflectionSettings | None = None,
    persona_render_max_tokens: int = 2000,
    recall_settings: RecallSettings | None = None,
    token_counter: TokenCounter | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    maintenance_settings: MaintenanceSettings | None = None,
    evolution_settings: EvolutionSettings | None = None,
    scheduler_settings: SchedulerSettings | None = None,
    proactive_settings: ProactiveSettings | None = None,
    migration_hooks: Sequence[Callable[..., Any]] = (),
) -> MemoryCore:
    """Wire the B03 stack for an explicit data root (test/demo/embedder)."""
    roots = FileSystemRoots(memory_root, local_state_root)
    store = atomic_store if atomic_store is not None else FileSystemAtomicStore()
    clock = clock if clock is not None else SystemClock()
    permit = permit if permit is not None else NullWritePermit()
    journal = EventJournal(roots=roots, store=store, clock=clock, permit=permit)
    ledger = SourceLedger(roots=roots, store=store, clock=clock, permit=permit)
    outbox = OutboxStore(roots=roots, store=store, clock=clock, permit=permit)
    deletions = DeletionStore(roots=roots, store=store, clock=clock, permit=permit)
    # QP05: which plugin session consumed which memory ids. Ids/versions only,
    # so the table survives a restart but can never carry content. Built here
    # because the deletion service needs it to find the plugins that hold a
    # copy of a fact whose source key has no plugin segment.
    consumer_refs = ConsumerRefStore(roots=roots, store=store, clock=clock, permit=permit)
    corrections = CorrectionStore(roots=roots, store=store, clock=clock, permit=permit)
    directive_kwargs = {}
    if ttl_seconds is not None:
        directive_kwargs["ttl_seconds"] = ttl_seconds
    if ttl_max_seconds is not None:
        directive_kwargs["ttl_max_seconds"] = ttl_max_seconds
    directives = UserDirectiveStore(
        roots=roots, store=store, clock=clock, permit=permit, **directive_kwargs,
    )
    tombstones = ScopedTombstoneStore(
        roots=roots, store=store, clock=clock, permit=permit,
    )
    barrier = DeletionBarrier(
        deletions=deletions, tombstones=tombstones, corrections=corrections,
    )
    facts = FactsView(roots=roots, store=store, clock=clock, permit=permit)
    # The recent store checks the deletion barrier inside its write lock (W1).
    recent = RecentStore(
        roots=roots, store=store, clock=clock, permit=permit,
        write_guard=barrier.assert_messages_allowed,
    )
    gates = SuppressionGate(memory_root=roots.memory_root, clock=clock)
    # B07: host-shaped companion_state view plus its replay handler; the service
    # serializes commits per character (lease -> request_id -> base_version).
    state_store = StateStore(
        roots=roots, store=store, clock=clock, permit=permit,
    )
    state_service = StateService(
        roots=roots, store=state_store, journal=journal, clock=clock,
        permit=permit, barrier=barrier,
    )
    timeindex_factory = lambda character: TimeIndexStore(  # noqa: E731 - simple closure
        roots=roots, character=character, permit=permit, clock=clock,
    )
    # B05: host-shaped reflection/persona stores, guarded by the deletion barrier
    # at write time; the domain services also re-check before every derived write.
    reflection_archive = ArchiveShardStore(
        roots=roots, store=store, clock=clock, permit=permit,
        dirname="reflection_archive",
    )
    persona_archive = ArchiveShardStore(
        roots=roots, store=store, clock=clock, permit=permit,
        dirname="persona_archive",
    )
    def _guard_rows(character, rows):
        for row in rows:
            barrier.assert_row_allowed(character, row)

    reflections = ReflectionStore(
        roots=roots, store=store, clock=clock, permit=permit,
        write_guard=_guard_rows, archive=reflection_archive,
    )
    persona = PersonaStore(
        roots=roots, store=store, clock=clock, permit=permit,
        write_guard=_guard_rows, archive=persona_archive,
    )
    persona_service = PersonaService(
        roots=roots, store=persona, reflections=reflections, barrier=barrier,
        journal=journal, clock=clock, permit=permit, model=model,
        render_max_tokens=persona_render_max_tokens,
    )
    reflection_service = ReflectionService(
        roots=roots, store=reflections, facts=facts, barrier=barrier,
        journal=journal, clock=clock, permit=permit, model=model,
        settings=reflection_settings, persona=persona_service,
        persona_reader=lambda character: persona_publish.current(character),
        timeindex_factory=timeindex_factory, recent=recent,
    )
    # B10: authoritative persona card (revisions/freeze/projection); the card
    # joins the deletion and correction fan-out alongside the persona entries.
    persona_card = PersonaCardStore(
        roots=roots, store=store, clock=clock, permit=permit, barrier=barrier,
    )
    persona_card_service = PersonaCardService(
        roots=roots, store=persona_card, journal=journal, clock=clock,
        permit=permit, barrier=barrier,
    )
    persona_versions = PersonaVersionStore(
        roots=roots, store=store, clock=clock, permit=permit, barrier=barrier,
    )
    persona_publish = PersonaPublishService(
        roots=roots, store=persona_versions, card_service=persona_card_service,
        journal=journal, clock=clock, permit=permit, barrier=barrier,
    )
    persona_projection = PersonaProjectionStore(
        roots=roots, store=store, clock=clock, permit=permit, barrier=barrier,
    )
    persona_projection_service = PersonaProjectionService(
        roots=roots, store=persona_projection, clock=clock, permit=permit,
        barrier=barrier, card_service=persona_card_service,
        publish_service=persona_publish, journal=journal,
    )
    evolution = EvolutionStore(
        roots=roots, store=store, clock=clock, permit=permit, barrier=barrier,
    )
    # The service defaults to EvolutionSettings() when none is injected; read the
    # same default here so the provider and the gate cannot disagree on the
    # independent-experience threshold.
    effective_evolution_settings = evolution_settings or EvolutionSettings()
    evidence_provider = _evolution_evidence(
        facts,
        reflections,
        promotion_rules=PROMOTION_RULES,
        min_support=int(effective_evolution_settings.evidence_min_support),
    )
    evolution_service = EvolutionService(
        roots=roots, store=evolution, card_service=persona_card_service,
        evidence_provider=evidence_provider,
        barrier=barrier, journal=journal, clock=clock, permit=permit,
        model=model, settings=evolution_settings,
    )
    persona_teaching = PersonaTeachingService(
        roots=roots, store=PersonaTeachingStore(roots=roots, store=store, clock=clock, permit=permit),
        card_service=persona_card_service, publish_service=persona_publish, journal=journal,
        clock=clock, permit=permit, barrier=barrier, model=model, evidence_reader=evidence_provider,
    )
    persona_admin = PersonaAdminStore(
        roots=roots, store=store, clock=clock, permit=permit, barrier=barrier,
    )
    persona_admin_service = PersonaAdminService(
        roots=roots, store=persona_admin, card_service=persona_card_service,
        evolution_service=evolution_service, publish_service=persona_publish,
        journal=journal, clock=clock, permit=permit, barrier=barrier,
    )
    evolution_runs = EvolutionRunStore(
        roots=roots, store=store, clock=clock, permit=permit, barrier=barrier,
    )
    evolution_scheduler = EvolutionScheduler(
        roots=roots, runs_store=evolution_runs,
        evolution_service=evolution_service, admin_service=persona_admin_service,
        publish_service=persona_publish, card_service=persona_card_service,
        evidence_provider=evidence_provider, barrier=barrier, clock=clock,
        permit=permit, settings=scheduler_settings,
    )
    # M5: assistant outputs. The service owns the visibility rule -- only the
    # parts a delivery receipt names become shared conversation -- so it is
    # wired with the same recent store the turns use, and with the journal so a
    # crash between the receipt and the recent append is repaired by replay.
    outputs = OutputStore(
        roots=roots, store=store, clock=clock, permit=permit,
    )
    output_service = OutputService(
        store=outputs, recent=recent, journal=journal, barrier=barrier,
        clock=clock, permit=permit,
    )
    # M5: the three working summaries are projections over the authoritative
    # records (rules, growth/reflection rows, facts, state, directives). The
    # projector is read-only; the store behind it is an audit cache that no read
    # path consumes.
    working_summary_store = WorkingSummaryStore(
        roots=roots, store=store, clock=clock, permit=permit,
    )
    # RO03: interest evidence for the existing `about_owner` section. One
    # store, wired once here, read by both the extractor (writes) and recall
    # (reports): a second store would be a second writer.
    interest_signal_store = InterestSignalStore(
        roots=roots, store=store, permit=permit, journal=journal,
    )
    interests = InterestService(
        store=interest_signal_store, barrier=barrier, clock=clock, facts=facts,
    )
    working_summary = WorkingSummaryService(
        character="",
        clock=clock,
        facts=facts,
        reflections=reflection_service,
        persona=persona_service,
        state=state_service,
        directives=directives,
        recent=recent,
        barrier=barrier,
        card=persona_card_service,
    )
    # M6-A: proactive candidates. The service owns cause/source provenance and
    # the send -> persist -> receipt loop; the body itself lives in the M5 output
    # records, so an undelivered proactive line is never a shared experience.
    # Derivation reads state/reflections, the gates read directives and the
    # deletion barrier, and sending writes an output record.
    proactive_store = ProactiveStore(
        roots=roots, store=store, clock=clock, permit=permit,
    )
    proactive_service = ProactiveService(
        store=proactive_store,
        clock=clock,
        permit=permit,
        state=state_service,
        reflections=reflection_service,
        facts=facts,
        directives=directives,
        barrier=barrier,
        outputs=output_service,
        journal=journal,
        settings=proactive_settings,
    )
    turn_requests = TurnRequestStore(
        roots=roots, store=store, clock=clock, permit=permit,
    )
    # S02: the replay handler for the whole-turn commit event is registered
    # here, before any write, so a crash after the journal fsync is repairable.
    journal.register_handler(TURN_EVENT_TYPE, turn_requests.apply_replay)
    derived_invalidators = tuple(derived_invalidators) + (
        _ProjectionInvalidator(persona_projection_service),
        reflection_service, persona_service, persona_card_service, persona_teaching,
        _PublishInvalidator(persona_publish),
        persona_admin_service,
        # S02/R01: a committed reply must stop being served once the fact it
        # was generated from is corrected.
        CorrectionTurnInvalidator(turn_requests, facts),
        interests,
    )
    services = deletion_services or DeletionServices()
    if services.recent is None:
        services = DeletionServices(
            reflections=reflection_service
            if services.reflections is None else services.reflections,
            # M5: the assistant outputs a deleted cause produced are retired in
            # the same step as the persona derivations (they are derived content
            # exactly like a published version).
            persona=_CombinedCleaner(
                _ProjectionCleaner(persona_projection_service),
                persona_service, persona_card_service,
                _PublishCleaner(persona_publish),
                _AdminCleaner(persona_admin_service),
                _OutputCleaner(output_service),
                _ProactiveCleaner(proactive_service),
                interests,
                persona_teaching,
            ) if services.persona is None else services.persona,
            recent=RecentDeletionCleaner(recent, barrier),
            # S02/R01: invalidate committed turn results for deleted sources.
            turn_results=TurnResultCleaner(turn_requests),
            consumer_refs=services.consumer_refs,
        )
    if services.consumer_refs is None:
        # A caller that supplied its own services still gets consumer
        # tracking: without it a web-created fact's plugin copy is invisible.
        services = DeletionServices(
            reflections=services.reflections,
            persona=services.persona,
            recent=services.recent,
            turn_results=services.turn_results,
            consumer_refs=consumer_refs,
        )
    cache_invalidators = tuple(cache_invalidators) + (
        _SummaryCacheInvalidator(working_summary_store),
    )
    deletion_service = DeletionService(
        roots=roots,
        journal=journal,
        deletions=deletions,
        barrier=barrier,
        facts=facts,
        clock=clock,
        permit=permit,
        timeindex_factory=timeindex_factory,
        services=services,
        cache_invalidators=cache_invalidators,
        ledger=ledger,
        outbox=outbox,
    )
    correction_service = CorrectionService(
        roots=roots,
        journal=journal,
        corrections=corrections,
        barrier=barrier,
        facts=facts,
        clock=clock,
        permit=permit,
        derived_invalidators=derived_invalidators,
        token_counter=token_counter,
    )
    sink = DurableEventStore(
        roots=roots, journal=journal, ledger=ledger, permit=permit,
        clock=clock, barrier=barrier,
    )
    extraction = ExtractionService(
        roots=roots,
        sink=sink,
        outbox=outbox,
        facts=facts,
        recent=recent,
        barrier=barrier,
        clock=clock,
        permit=permit,
        model=model,
        settings=extraction_settings,
        gate=gates,
        timeindex_factory=timeindex_factory,
        corrections=correction_service,
        conversation_state=ConversationState(state_service, barrier, clock),
        proactive=proactive_service,
        interests=interests,
        persona_reader=lambda character: persona_publish.current(character),
    )
    compression = CompressionService(
        recent=recent, barrier=barrier, clock=clock, model=model,
        settings=compression_settings,
    )
    # B06: read-only recall over the B05 stores; token/vector providers are
    # optional ports (BM25-only fallback when no embedding is injected).
    # M5: recall also gets the state snapshot and the summary projector, so a
    # single build_turn_context serves one coherent version of both.
    recall = RecallService(
        clock=clock,
        facts=facts,
        reflections=reflection_service,
        persona=persona_service,
        corrections=correction_service,
        barrier=barrier,
        token_counter=token_counter,
        embedding=embedding_provider,
        model=model,
        settings=recall_settings,
        roots=roots,
        state=state_service,
        working_summary=working_summary,
        interests=interests,
        timeindex_factory=timeindex_factory,
    )
    # B07: outbox dispatch + one maintenance round; no background task is
    # started here (the standalone entry / host supervisor owns scheduling).
    outbox_dispatcher = OutboxDispatcher(outbox=outbox)
    outbox_dispatcher.register_bulk(
        "extract_facts",
        lambda character: extraction.process_outbox(character),
    )
    def recover_with_teachings(character):
        result = sink.recover(character)
        if not getattr(result, "paused", False):
            persona_teaching.retry_pending(character)
            correction_service.repair_replacements(character)
        return result

    maintenance = MaintenanceService(
        dispatcher=outbox_dispatcher,
        deletion_service=deletion_service,
        reflection_service=reflection_service,
        persona_service=persona_service,
        journal=journal,
        outbox=outbox,
        clock=clock,
        settings=maintenance_settings,
        recover=recover_with_teachings,
        scheduler=evolution_scheduler,
        migrations=tuple(migration_hooks or ()),
        # M5: the audit-cache refresh step. Degrades to skipped when unwired.
        working_summary=_WorkingSummaryPair(working_summary, working_summary_store),
        # M6-A: derive proactive candidates in the background pass. Deriving
        # only registers reasons; no body is generated and nothing is sent from
        # a maintenance round.
        proactive=proactive_service,
    )
    extraction.notify_priority = maintenance.wake
    from ..persistence.consumer_receipts import ConsumerReceiptStore, RECEIPT_EVENT
    consumer_receipts = ConsumerReceiptStore(roots=roots, store=store, permit=permit)
    journal.register_handler(RECEIPT_EVENT, consumer_receipts.replay)
    return MemoryCore(
        roots=roots,
        store=store,
        clock=clock,
        permit=permit,
        journal=journal,
        ledger=ledger,
        outbox=outbox,
        sink=sink,
        turn_requests=turn_requests,
        consumer_receipts=consumer_receipts,
        consumer_refs=consumer_refs,
        deletions=deletions,
        corrections=corrections,
        directives=directives,
        tombstones=tombstones,
        barrier=barrier,
        facts=facts,
        recent=recent,
        gates=gates,
        reflections=reflections,
        persona=persona,
        deletion_service=deletion_service,
        correction_service=correction_service,
        extraction=extraction,
        compression=compression,
        reflection_service=reflection_service,
        persona_service=persona_service,
        persona_card=persona_card,
        persona_card_service=persona_card_service,
        persona_versions=persona_versions,
        persona_publish=persona_publish,
        persona_teaching=persona_teaching,
        persona_projection=persona_projection,
        persona_projection_service=persona_projection_service,
        evolution=evolution,
        evolution_service=evolution_service,
        persona_admin=persona_admin,
        persona_admin_service=persona_admin_service,
        evolution_runs=evolution_runs,
        evolution_scheduler=evolution_scheduler,
        recall=recall,
        state=state_store,
        state_service=state_service,
        outbox_dispatcher=outbox_dispatcher,
        maintenance=maintenance,
        timeindex_factory=timeindex_factory,
        outputs=outputs,
        output_service=output_service,
        working_summary=working_summary_store,
        working_summary_service=working_summary,
        proactive=proactive_store,
        proactive_service=proactive_service,
        interests=interests,
        interest_signals=interest_signal_store,
    )


__all__ = ["MemoryCore", "build_memory_core"]
