"""B04 fact extraction: classify, persist the raw turn, extract, dedupe, fence.

Pipeline: durable event receipt -> raw turn to recent.json + time index ->
plugin-only classes skip extraction -> outbox extract_facts op ->
window filter (human facts, scoped reference dialogue, class/legacy/deletion gates)
-> injected model with explicit budgets -> JSON parse -> SHA-256 dedupe ->
facts commit.

Failure semantics are the host's: None is a terminal failure (kept pending for
retry), [] is a successful zero-fact window; merging them would skip a window
forever. Before persisting, the window source keys are re-checked against the
deletion barrier (the host only checked the candidate's own keys, which let
rewritten late results slip through; fixed here).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from ..classification import (
    CHAT, classify_message, filter_messages_for_extraction, is_extractable, metadata_of,
)
from ..contracts import CorrectionRequest, MemoryEvent, Scene, Scope
from ..domain.barrier import DeletionBarrier, message_source_keys, message_text
from ..event_sources import parse_source_key
from ..invalidation import filter_valid_entries, is_friend_subject, row_matches_subject, scene_applies, subject_key
from ..persistence.facts_view import FactsView
from ..persistence.gates import SuppressionGate
from ..persistence.outbox import OutboxStore
from ..persistence.recent import RecentStore
from ..ports import Clock, EventSink, ModelInvoker, StorageRoots, WritePermit
from .profile import SIGNAL_PROMPT
from .recall import RETENTION_CLASSES, bm25_scores
from .prompt_voice import FIRST_PERSON_VOICE
from .experience import confirmation_time, fact_support_groups, message_material, message_role, number_messages, user_confirmation_bindings
from .persona_card import EXAMPLES_BANNER

DEFAULT_TIER = "summary"
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_OUTPUT_TOKENS = 4096
#: Processing-position op string: never rename (old outbox.ndjson replays it).
EXTRACT_OP_TYPE = "extract_facts"
MAX_CANDIDATES = 32
MAX_FACT_TEXT = 600
MAX_WINDOW_MESSAGES = 16
FACT_SOURCES = ("user_observation", "ai_disclosure", "shared_experience", "user_correction")
FACT_ENTITIES = ("master", "neko", "relationship")
SHARED_EXPERIENCE = "shared_experience"
PREFERENCE = "preference"

#: Evidence kinds (growth design section 6.2). They say what a fact rests on,
#: which is what the reflection/growth gates must read instead of guessing
#: from the source string. `context_refs` is deliberately not one of these:
#: earlier dialogue only disambiguates a reference and never supports a fact.
EVIDENCE_KINDS = (
    "user_assertion",
    "explicit_feedback",
    "observed_action",
    "tool_result",
    "agent_expression",
    "quoted_example",
)
#: Default kind per fact source, used when the model omits or invents one.
EVIDENCE_BY_SOURCE = {
    "user_observation": "user_assertion",
    "user_correction": "user_assertion",
    "ai_disclosure": "agent_expression",
    "shared_experience": "observed_action",
}
#: Cap on retained evidence keys per fact (mirrors reflection.MAX_SOURCE_KEYS).
MAX_FACT_SOURCE_KEYS = 32
_EXPLICIT_MEMORY = re.compile(
    r"^(?:(?:以后|长期)?(?:请|帮我|麻烦你)?记住(?:这条|这一点|一下)?[\s：:，,]*"
    r"(?P<zh>我[^？！?!\n]+)|(?:please\s+)?remember(?:\s+that|\s*:)?\s+(?P<en>I\b[^?!\n]+))$",
    re.IGNORECASE,
)
_TEMPORARY_MEMORY = re.compile(r"今天|明天|后天|待会|一会|下周|提醒我|[Tt]oday|[Tt]omorrow|[Nn]ext week")
_UNCERTAIN_MEMORY = re.compile(r"^(?:我(?:想知道|不确定|怀疑|猜|是不是|可能)|I (?:wonder|might|may|guess)\b)", re.I)
_PRIORITY_MEMORY = re.compile(
    r"^(?:你(?:刚才)?记错了|纠正一下|更正一下|我对[^，。！？]{1,24}过敏|"
    r"我(?:一直|长期|从来)(?:都)?(?:不能|不吃|不喝)|我不能(?:吃|喝)|"
    r"correction:|you misremembered\b|i am allergic to\b)", re.I,
)
_OLD_FACT_LIMIT = 12
_OLD_FACT_CHARS = 3600
#: Upper bound for a full outbox drain (the store requires a limit).
_DRAIN_LIMIT = 1000



@dataclass(frozen=True)
class ExtractionSettings:
    """B04 extraction policy; defaults mirror the host per-turn path."""

    tier: str = DEFAULT_TIER
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    #: OFF-mode per-turn extraction runs the queued op immediately. B07 owns
    #: the background scheduler; RO02 adds queued batch extraction below.
    inline: bool = True
    max_window_messages: int = MAX_WINDOW_MESSAGES
    max_candidates: int = MAX_CANDIDATES
    #: RO02 batch extraction (design section 6.2). A queued group of ordinary
    #: turns in one scope is extracted with one model call once it holds
    #: `batch_max_turns` turns, or once the last utterance has been idle for
    #: `batch_idle_seconds`. `batch_max_messages` is the input budget: a group
    #: that would exceed it is split before the call instead of being queued
    #: longer. Setting `batch_enabled=False` restores one call per turn.
    #:
    #: Batch key = character + subject + client + session + scene; a batch never
    #: crosses a session or a subject.
    batch_enabled: bool = True
    batch_max_turns: int = 5
    #: Idle wait after the newest utterance of a partial group.
    #:
    #: `0.0` and `120.0` are two different policies, not two spellings of one:
    #:
    #: * `0.0` -- "flush whatever is queued on this drain". A group is closed
    #:   on the turn count (`batch_max_turns`) or the input budget; everything
    #:   still open runs immediately. Because the background pass polls about
    #:   once a minute, turns that arrive inside one polling cycle still merge,
    #:   but nothing ever waits for a quiet period. This is the historical
    #:   single-turn latency and the default, so an existing deployment does not
    #:   silently change how fast a fact becomes available.
    #: * `120.0` -- the design's cost-optimizing policy: in addition to the
    #:   above, a partial group waits until the last utterance has been idle for
    #:   the window. Fewer, larger calls; a fact may take up to ~2 minutes
    #:   longer to appear.
    #:
    #: Neither value loses a turn: the raw turn is persisted before it is
    #: queued, so a longer window only delays extraction, never drops it.
    #: Production sets 120 explicitly in stack.toml / the plugin patch; see
    #: docs/rin-next-optimization-plan.md 6.2.
    batch_idle_seconds: float = 0.0
    batch_max_messages: int = MAX_WINDOW_MESSAGES
    enabled: bool = True


@dataclass(frozen=True)
class IngestResult:
    """Outcome of one chat turn entering the memory pipeline."""

    status: str
    source_key: str
    content_class: str
    episode_id: str | None = None
    facts_written: int = 0
    op_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ExtractionOutcome:
    """One extraction attempt; status == "failed" keeps the op retryable.

    `batch_id` / `source_keys` describe the source set this outcome covers:
    a batched run answers one model call for several queued turns, so the
    caller needs to know exactly which turns that answer applies to (RO02).
    """

    status: str
    facts_written: int = 0
    reason: str | None = None
    candidates: tuple[Mapping[str, Any], ...] = ()
    #: Stable hash of the batch's sources and their input texts.
    batch_id: str = ""
    #: The queued turns this outcome completes, in event order.
    source_keys: tuple[str, ...] = ()
    #: Conversation-state operations applied from this batch.
    state_applied: int = 0
    #: Interest signals written from this batch (RO03); 0 when none, when the
    #: layer is unwired, or when every candidate lacked a resolvable citation.
    signals_written: int = 0


class ExtractionService:
    """Independent ingest/extract entry over one data root (single writer)."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        sink: EventSink,
        outbox: OutboxStore,
        facts: FactsView,
        recent: RecentStore,
        barrier: DeletionBarrier,
        clock: Clock,
        permit: WritePermit,
        model: ModelInvoker | None = None,
        settings: ExtractionSettings | None = None,
        gate: SuppressionGate | None = None,
        timeindex_factory: Any = None,
        corrections: Any = None,
        conversation_state: Any = None,
        proactive: Any = None,
        interests: Any = None,
        persona_reader: Any = None,
    ) -> None:
        self._roots = roots
        self._sink = sink
        self._outbox = outbox
        self._facts = facts
        self._recent = recent
        self._barrier = barrier
        self._clock = clock
        self._permit = permit
        self._model = model
        self._settings = settings or ExtractionSettings()
        self._gate = gate
        self._timeindex_factory = timeindex_factory
        self._corrections = corrections
        self._conversation_state = conversation_state
        self._proactive = proactive
        self._interests = interests
        self._persona_reader = persona_reader
        self.notify_priority = None

    # -- ingest ----------------------------------------------------------

    async def ingest_turn(
        self,
        character: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        source_key: str,
        content_class: str | None = None,
        episode_id: str | None = None,
        session_id: str | None = None,
        language: str = "zh",
        inline: bool | None = None,
        occurred_at: datetime | None = None,
        event: MemoryEvent | None = None,
    ) -> IngestResult:
        """Persist one turn, then (in OFF mode) extract facts from it.

        `occurred_at` is the business time of the turn and is part of the source
        fingerprint. A caller retrying the same turn MUST pass the value it used
        the first time: deriving it from `clock.now()` on every attempt made a
        retry look like a different payload and fail as `source_key_reused_with_
        different_payload` (R04). Leaving it None keeps the old behaviour for
        callers that have no retry semantics.
        """
        character = str(character or "").strip()
        if not character:
            raise ValueError("character must be nonempty")
        clean = [dict(message) for message in messages or () if isinstance(message, Mapping)]
        if not clean:
            return IngestResult("no_content", source_key, "unclassified", reason="no_messages")
        parsed = parse_source_key(source_key)
        if parsed is None:
            return IngestResult("rejected", source_key, "unclassified", reason="invalid_source_key")
        subject = str(parsed.get("subject") or "private")
        scene = Scene(
            plugin_id=str(parsed.get("plugin_id") or ""),
            session_id=str(parsed.get("session_id") or ""),
        )
        user_text = next(
            (message_text(message) for message in clean if _is_human_message(message)), "",
        )
        classification = content_class or classify_message(clean[-1], default=CHAT)
        now = occurred_at if occurred_at is not None else self._clock.now()
        origin = (clean[0].get("data") or {}).get("metadata") or clean[0].get("metadata") or {}
        subject = origin.get("subject") or subject
        kind, _, value = str(origin.get("scene") or "").partition(":")
        if value:
            scene = replace(scene, **{{"game": "game_id", "plugin": "plugin_id"}.get(kind, "session_id"): value})
        structured_event = event is not None and event.event_type == "scene_experience"
        event = event or MemoryEvent(
            scope=Scope(character=character, subject=subject),
            source_key=source_key,
            event_type="user_message",
            text=user_text,
            occurred_at=now,
            received_at=now,
            scene=scene,
            applies_to=Scene(**{k: v for k, v in (origin.get("applies_to") or {}).items() if k in ("game_id", "plugin_id", "session_id")}),
            content_class=classification,
            episode_id=episode_id,
        )
        subject = event.scope.subject
        scene = event.scene
        for message in clean:
            data = dict(message.get("data") or {})
            metadata = dict(data.get("metadata") or message.get("metadata") or {})
            metadata.setdefault("subject", subject)
            metadata.setdefault("source_key", source_key)
            metadata.setdefault("source_event_keys", [source_key])
            metadata.setdefault("applies_to", {k: v for k, v in event.applies_to.__dict__.items() if v})
            metadata.setdefault("scene", "game:" + scene.game_id if scene.game_id else "plugin:" + scene.plugin_id if scene.plugin_id else "")
            metadata.setdefault("session_id", session_id or scene.session_id)
            metadata.setdefault("content_class", classification)
            if (classification == SHARED_EXPERIENCE and parsed.get("kind") == "plugin" and len(clean) == 1
                    and (structured_event or scene.game_id)):
                metadata["structured_experience"] = True
            # Batch scope (RO02): every message carries the identity the batch
            # key is built from, so a queued op is self-describing even when the
            # payload predates the batch fields below.
            metadata.setdefault("client_id", str(origin.get("client_id") or ""))
            # Business time per message: batch anchors are per utterance, never
            # the batch head/tail or the processing time.
            metadata.setdefault("occurred_at", now.isoformat())
            data["metadata"] = metadata
            message["data"] = data
            if "metadata" in message:
                message["metadata"] = metadata
        clean = number_messages(clean, [source_key], now)
        remember = _EXPLICIT_MEMORY.fullmatch(user_text.strip()) if is_extractable(classification) else None
        explicit = (remember.group("zh") or remember.group("en")).strip() if remember else ""
        if explicit and (_TEMPORARY_MEMORY.search(explicit) or _UNCERTAIN_MEMORY.search(explicit)
                         or re.search(r"[。；;]", explicit.rstrip("。")) or len(explicit) > MAX_FACT_TEXT - 24):
            explicit = ""
        if self._barrier.blocks_event(event):
            return IngestResult("deleted", source_key, classification, episode_id,
                                reason="deleted_source")
        receipt = await self._sink.append(event)
        if receipt.status == "duplicate":
            if explicit and self._settings.enabled:
                self._save_explicit(character, clean, explicit, source_key, episode_id, now)
                return IngestResult("duplicate", source_key, classification, episode_id, reason="duplicate_source")
            # A retry after a crash between "raw turn saved" and "outbox op
            # enqueued" used to return here and leave the turn permanently
            # unextractable. The retry carries the same messages, so the window
            # can still be rebuilt; only enqueue when no op exists for this
            # source, otherwise a second op would extract the same window twice.
            repaired = self._repair_missing_extraction_op(
                character, source_key, classification, episode_id, language,
                messages=clean, occurred_at=now,
            )
            return IngestResult("duplicate", source_key, classification, episode_id,
                                op_id=repaired, reason="duplicate_source")
        if receipt.status != "accepted":
            return IngestResult("rejected", source_key, classification, episode_id,
                                reason=receipt.reason or receipt.status)
        # §5.1 of docs/standalone-service.md: saving the turn and deciding
        # whether it may feed extraction are separate questions. Plugin-only
        # classes used to return here, before recent/time index were written,
        # so a game-stream or tool-result turn vanished from the conversation
        # entirely -- and pushing the class gate in front of storage would have
        # silently stopped ordinary chat from being recorded too. The raw turn
        # is therefore saved first for every accepted source; only the
        # extraction op below is skipped for non-extractable classes.
        kept = self._barrier.filter_messages(character, clean)
        if kept:
            self._recent.append(character, kept)
            self._store_raw(character, kept, session_id or _session_from(scene, episode_id))
        if not self._settings.enabled:
            return IngestResult("accepted", source_key, classification, episode_id,
                                reason="memory_paused")
        if self._proactive is not None and is_extractable(classification):
            self._proactive.absorb_user_turn(
                character, text=user_text, subject=subject,
                scene=str(metadata_of(clean[0]).get("scene") or "") if clean else "",
                request_id=source_key,
            )
        if not is_extractable(classification):
            return IngestResult("kept_in_plugin", source_key, classification, episode_id,
                                reason="stored_without_extraction")
        if classification == PREFERENCE and self._corrections is not None:
            await self._corrections.correct(CorrectionRequest(
                scope=Scope(character=character, subject=subject),
                action="replace", new_text=user_text, source_key=source_key,
            ))
            return IngestResult("accepted", source_key, classification, episode_id,
                                reason="preference_recorded")
        gate_state = self._gate.gate_for(character) if self._gate is not None else None
        if gate_state is not None and gate_state.suppressed:
            return IngestResult("accepted", source_key, classification, episode_id,
                                reason="gate_suppressed")
        if explicit:
            written = self._save_explicit(character, kept, explicit, source_key, episode_id, now)
            return IngestResult("extracted", source_key, classification, episode_id,
                                facts_written=written, reason="explicit_memory")
        payload = {
            "messages": kept,
            "source_keys": [source_key],
            "episode_id": episode_id or "",
            "content_class": classification,
            "language": language,
            # Business time of the turn, carried so a batch processed hours or
            # days later still resolves relative time from when it was spoken.
            "occurred_at": now.isoformat(),
            # Batch scope is part of the payload, not only of the message
            # metadata: two turns may only share one model call when all five
            # of character/subject/client/session/scene agree (design 6.2).
            "batch_scope": _batch_scope(character, metadata_of(clean[0])),
            "priority": bool(_PRIORITY_MEMORY.search(user_text.strip())),
        }
        op_id = self._outbox.enqueue(character, EXTRACT_OP_TYPE, payload)
        should_inline = self._settings.inline if inline is None else inline
        if not should_inline:
            if payload["priority"] and self.notify_priority is not None:
                self.notify_priority(character)
            return IngestResult("accepted", source_key, classification, episode_id,
                                op_id=op_id, reason="queued")
        outcome = await self.extract_facts(
            character,
            payload["messages"],
            source_keys=payload["source_keys"],
            episode_id=episode_id,
            content_class=classification,
            language=language,
            occurred_at=now,
        )
        if outcome.status == "failed":
            self._outbox.fail(character, op_id, error=outcome.reason or "extraction_failed")
            return IngestResult("extraction_failed", source_key, classification,
                                episode_id, op_id=op_id, reason=outcome.reason)
        self._outbox.complete(character, op_id)
        return IngestResult("extracted", source_key, classification, episode_id,
                            facts_written=outcome.facts_written, op_id=op_id)

    def _save_explicit(self, character, messages, text, source_key, episode_id, occurred_at):
        if self._gate is not None and self._gate.gate_for(character).suppressed:
            return 0
        with self._barrier.write_lock(character):
            kept = self._barrier.filter_messages(character, messages)
            message = next((item for item in kept if _is_human_message(item)), None)
            if message is None:
                return 0
            metadata = metadata_of(message)
            binding = message_material(message)
            binding.pop("text")
            binding.update(source_key=source_key, evidence_kind="user_assertion")
            written, _ = self._persist_candidates(
                character, [{
                    "operation": "add", "text": f"我记得对方明确说过：「{text}」",
                    "entity": "master", "source": "user_observation", "retention_class": "durable",
                    "subject": metadata.get("subject", "private"), "applies_to": metadata.get("applies_to") or {},
                    "source_event_keys": [source_key], "source_bindings": [binding],
                    "message_refs": [binding["message_ref"]],
                }], source_keys=(), window_keys=[source_key], episode_id=episode_id, occurred_at=occurred_at,
            )
            return written



    def _repair_missing_extraction_op(
        self,
        character: str,
        source_key: str,
        classification: str,
        episode_id: str | None,
        language: str,
        *,
        messages: Sequence[Mapping[str, Any]] = (),
        occurred_at: datetime | None = None,
    ) -> str | None:
        """Re-enqueue the extraction op if a duplicate turn lost it.

        Returns the new op_id, or None when the turn needs no op: a
        non-extractable class, an op that already exists for this source, or no
        usable window. Completing the interrupted path leaves the same final
        state as if enqueue had happened before the crash: one saved turn, one
        op.
        """
        if not self._settings.enabled or not is_extractable(classification):
            return None
        if self._outbox.find_by_source_key(
            character, source_key, op_type=EXTRACT_OP_TYPE,
        ):
            return None
        kept = self._barrier.filter_messages(character, list(messages))
        if not kept:
            # Without texts there is nothing to extract; leave the turn saved
            # and never fabricate a window.
            return None
        payload = {
            "messages": kept,
            "source_keys": [source_key],
            "episode_id": episode_id or "",
            "content_class": classification,
            "language": language,
            "occurred_at": (occurred_at or self._clock.now()).isoformat(),
            "batch_scope": _batch_scope(character, metadata_of(kept[0])),
            "priority": any(_is_human_message(item) and _PRIORITY_MEMORY.search(message_text(item).strip())
                            for item in kept),
        }
        op_id = self._outbox.enqueue(character, EXTRACT_OP_TYPE, payload)
        if payload["priority"] and self.notify_priority is not None:
            self.notify_priority(character)
        return op_id

    async def process_outbox(
        self, character: str, *, limit: int | None = None,
    ) -> tuple[ExtractionOutcome, ...]:
        """Run pending extraction ops; failures stay pending for the next round.

        RO02: queued ordinary turns are grouped and answered with one model
        call per batch (design section 6.2). A group is closed by the turn
        count, by the input budget, or by the idle window; a partial group
        below the turn count waits for `batch_idle_seconds`. Only turns whose
        batch scope agrees share a call -- the key is character, subject,
        client, session and scene, so nothing is ever merged across a session
        or a subject.
        """
        if not self._settings.enabled:
            return ()
        # The dispatcher's bulk contract is "drain every pending op of this
        # type", so the default is the whole queue: a partial drain would leave
        # a batch half-completed and turn the next round into a second call for
        # the same turns.
        pending = self._outbox.pending(
            character, limit=limit if limit is not None else _DRAIN_LIMIT,
        )
        ops = [op for op in pending if op.op_type == EXTRACT_OP_TYPE]
        outcomes: list[ExtractionOutcome] = []
        for group in self._batch_groups(character, ops):
            if group["immediate"]:
                # Shared-experience turns are built from the text itself (no
                # model call, no batching) and keep the one-window path.
                for op in group["ops"]:
                    outcomes.append(await self._run_single_op(character, op))
                continue
            for batch in group["batches"]:
                if len(batch) == 1:
                    # A batch of one is the pre-RO02 window. Taking the old
                    # path keeps the single-turn contract exact (prompt shape,
                    # citation-free answers, old queued payloads shipping with
                    # their original prompt) while the batching rule still
                    # decides *when* that window runs.
                    outcomes.append(await self._run_single_op(character, batch[0]))
                    continue
                try:
                    outcome = await self._extract_batch(character, batch)
                except Exception as exc:  # noqa: BLE001 - keep the ops for a retry
                    outcome = ExtractionOutcome(
                        "failed", reason=f"error:{type(exc).__name__}",
                    )
                outcomes.append(outcome)
        return tuple(outcomes)

    async def _run_single_op(self, character: str, op: Any) -> ExtractionOutcome:
        """One op, one window: the pre-RO02 path for non-batchable classes."""
        payload = dict(op.payload)
        try:
            outcome = await self.extract_facts(
                character,
                payload.get("messages") or (),
                source_keys=payload.get("source_keys") or (),
                episode_id=payload.get("episode_id") or None,
                content_class=payload.get("content_class"),
                language=payload.get("language") or "zh",
                occurred_at=_parse_iso(payload.get("occurred_at")),
            )
        except Exception as exc:  # noqa: BLE001 - keep the op for a retry
            outcome = ExtractionOutcome("failed", reason=f"error:{type(exc).__name__}")
        self._settle_op(character, [op.op_id], outcome)
        return outcome

    def _barrier_outcome_status(self, character, keys):
        return "stale" if set(keys) & self._barrier.state(character).index.source_keys else "failed"

    def _settle_op(
        self, character: str, op_ids: Sequence[str], outcome: ExtractionOutcome,
    ) -> None:
        """A failed batch keeps every one of its sources pending for a retry."""
        if outcome.status == "failed":
            for op_id in op_ids:
                self._outbox.fail(
                    character, op_id, error=outcome.reason or "extraction_failed",
                )
            return
        if len(op_ids) == 1:
            self._outbox.complete(character, op_ids[0])
            return
        complete_many = getattr(self._outbox, "complete_many", None)
        if callable(complete_many):
            complete_many(character, op_ids)
            return
        for op_id in op_ids:
            self._outbox.complete(character, op_id)

    # -- batching --------------------------------------------------------

    def _batch_groups(
        self, character: str, ops: Sequence[Any],
    ) -> list[dict[str, Any]]:
        """Group pending ops into batch scopes, then into closed batches."""
        settings = self._settings
        grouped: dict[tuple, list[Any]] = {}
        for op in ops:
            payload = dict(op.payload)
            scope = payload.get("batch_scope")
            if not isinstance(scope, (list, tuple)) or len(scope) != 5:
                # An op enqueued before RO02 (or rewritten by hand) is still
                # self-describing through its messages; fall back to those so
                # an old queue never merges two scopes by accident.
                messages = payload.get("messages") or ()
                first = messages[0] if messages else {}
                scope = _batch_scope(character, metadata_of(first))
            key = (
                tuple(str(part or "") for part in scope),
                str(payload.get("episode_id") or ""),
                str(payload.get("content_class") or ""),
            )
            grouped.setdefault(key, []).append(op)
        groups: list[dict[str, Any]] = []
        now = self._clock.now()
        for entries in grouped.values():
            content_class = str(dict(entries[0].payload).get("content_class") or "")
            if content_class == SHARED_EXPERIENCE:
                groups.append({"immediate": True, "ops": entries})
                continue
            if not settings.batch_enabled:
                groups.append({"immediate": True, "ops": entries})
                continue
            groups.append({
                "immediate": False,
                "batches": self._split_batches(entries, now),
            })
        return groups

    def _split_batches(
        self, ops: Sequence[Any], now: datetime,
    ) -> list[list[Any]]:
        """Close batches on turn count, input budget or the idle window."""
        settings = self._settings
        max_turns = max(1, int(settings.batch_max_turns))
        budget = max(1, int(settings.batch_max_messages))
        ordered = sorted(
            ops,
            key=lambda op: (
                _ts(_parse_iso(dict(op.payload).get("occurred_at"))
                    or self._clock.now()),
                str(op.op_id),
            ),
        )
        batches: list[list[Any]] = []
        current: list[Any] = []
        messages = 0
        for op in ordered:
            payload = dict(op.payload)
            if payload.get("priority"):
                if current:
                    batches.append(current)
                    current, messages = [], 0
                batches.append([op])
                continue
            count = len([
                message for message in payload.get("messages") or ()
                if isinstance(message, Mapping)
            ])
            # Input budget: split before the call instead of building a window
            # the model cannot see the end of.
            if current and messages + count > budget:
                batches.append(current)
                current, messages = [], 0
            current.append(op)
            messages += count
            if len(current) >= max_turns:
                batches.append(current)
                current, messages = [], 0
        if current:
            newest = max(
                _ts(_parse_iso(dict(op.payload).get("occurred_at"))
                    or self._clock.now()) for op in current
            )
            idle_window = float(settings.batch_idle_seconds)
            # A zero window means "flush now" and must not depend on the sign:
            # a queued turn can carry a business time ahead of the drain clock
            # (back-dated or future-dated occurrences), and a negative idle
            # would otherwise hold the batch forever.
            if idle_window <= 0.0 or _ts(now) - newest >= idle_window:
                batches.append(current)
        return batches

    async def _extract_batch(
        self, character: str, ops: Sequence[Any],
    ) -> ExtractionOutcome:
        """One model call for several queued turns, with per-turn attribution.

        Every utterance keeps its own source key, business time and order, so a
        fact written here is bound to the exact turns the model cited -- never
        to the whole batch (design section 6.2).
        """
        settings = self._settings
        op_ids = [op.op_id for op in ops]
        utterances = _batch_utterances(ops, self._clock.now())
        if not utterances:
            outcome = ExtractionOutcome(
                "completed", reason="no_user_messages",
                source_keys=tuple(key for op in ops
                                  for key in (dict(op.payload).get("source_keys") or ())),
            )
            self._settle_op(character, op_ids, outcome)
            return outcome
        batch_id = _batch_id(utterances)
        all_keys = sorted({key for item in utterances for key in item["source_keys"]})
        metadata = utterances[0]["metadata"]
        episode_id = utterances[0]["episode_id"]
        deleted_keys = set(self._barrier.state(character).index.source_keys)
        legacy_cutoff = None
        if self._gate is not None:
            legacy_cutoff = self._gate.gate_for(character).legacy_barrier
        kept, _meta = filter_messages_for_extraction(
            [message for item in utterances for message in item["messages"]],
            legacy_barrier_cutoff=legacy_cutoff,
            deleted_source_keys=deleted_keys,
        )
        kept = self._barrier.filter_messages(character, kept)
        kept = [message for message in kept if is_extractable(metadata_of(message).get("content_class"))]
        kept_refs = {metadata_of(message)["message_ref"] for message in kept}
        utterances = [dict(item, messages=[message for message in item["messages"]
                                          if metadata_of(message)["message_ref"] in kept_refs])
                      for item in utterances]
        utterances = [dict(item, has_user=any(_is_human_message(message) for message in item["messages"]))
                      for item in utterances if item["messages"]]
        kept_keys = {key for message in kept for key in message_source_keys(message)}
        if not kept_keys & set(all_keys):
            outcome = ExtractionOutcome(
                "completed", reason="filtered_out", batch_id=batch_id,
                source_keys=tuple(all_keys),
            )
            self._settle_op(character, op_ids, outcome)
            return outcome
        if self._model is None:
            outcome = ExtractionOutcome(
                "failed", reason="model_unavailable", batch_id=batch_id,
                source_keys=tuple(all_keys),
            )
            self._settle_op(character, op_ids, outcome)
            return outcome
        snapshot = self._barrier.snapshot(character)
        reference = self._batch_reference(character, utterances, all_keys)
        # Reference keys are recorded on the fact for provenance only; they are
        # never part of the evidence set, so they add no support.
        reference_keys = sorted({
            key for message in reference for key in message_source_keys(message)
        } - set(all_keys))
        identity = self._identity_prompt(character, metadata)
        old_facts = self._related_facts(character, kept, metadata, reference=reference)
        prompt = identity + _build_batch_prompt(utterances, reference)
        prompt += _old_fact_material(old_facts)
        prompt += self._applied_correction_material(character, all_keys, metadata)
        if self._conversation_state is not None:
            state = self._conversation_state.snapshot(character, metadata)
            prompt += (
                "\n\n我同时整理正在关注的事项，以第一人称写 text、next_step 和 why。输出采用 JSON 对象："
                '{"facts":[上述事实],"state_updates":[事项操作]}。'
                "事实或事项为空时分别使用空数组。我从明确计划、正在处理的事、"
                "后续进展和完成/取消记录中整理事项。只在用户明确承诺、接受、正在做或实际要求跟进时建事项。"
                "开放讨论、建议、问能力、当前一次性的请求与尚未接受的提议留在对话里。"
                "操作采用 set_progress/set_concern/close_progress/close_concern。"
                "更新或结束本批前面新建的事项时，引用同一条操作里给出的 ref；"
                "更新或结束列表中已有的旧事项时，引用它的 id。新建时填写 ref，id 留空。"
                "操作字段为 op/id/ref/text/next_step/why/sources。"
                "sources 是本操作依据的原话编号。next_step 写适合后续交流的具体下一步。"
                "明确完成或取消时使用 close_*；暂停话题保留其当前进度。"
                + _state_followup_rule(metadata) +
                "followup_when 在原话提供跟进时间时填写，格式与 event_when.start "
                "一致（offset/unit），以该操作引用的原话时间为基准；其余情况留空。"
                "\n本批原话时间：" + json.dumps(
                    [[item["index"], item["occurred_at"].isoformat()]
                     for item in utterances], ensure_ascii=False,
                )
                + "\n当前同范围事项：" + json.dumps(list(state.items)[:20], ensure_ascii=False)
            )
        if self._interests is not None and not is_friend_subject(metadata.get("subject")):
            # Only ask for what can be stored: an unwired layer must not add
            # tokens to every extraction call.
            prompt += SIGNAL_PROMPT
        try:
            raw = await self._model.invoke(
                settings.tier,
                [{"role": "system", "content": prompt}],
                max_output_tokens=settings.max_output_tokens,
                timeout_s=settings.timeout_s,
                purpose="memory_fact_extraction",
            )
        except Exception as exc:  # noqa: BLE001 - transport failure is retryable
            outcome = ExtractionOutcome(
                "failed", reason=f"transport:{type(exc).__name__}",
                batch_id=batch_id, source_keys=tuple(all_keys),
            )
            self._settle_op(character, op_ids, outcome)
            return outcome
        parsed = _parse_batch_extraction(raw, settings.max_candidates)
        if parsed is None:
            # A parse failure is not "no facts": every source stays pending and
            # the outbox backs off (design section 6.2).
            outcome = ExtractionOutcome(
                "failed", reason="parse", batch_id=batch_id,
                source_keys=tuple(all_keys),
            )
            self._settle_op(character, op_ids, outcome)
            return outcome
        if self._barrier.is_stale(character, snapshot):
            outcome = ExtractionOutcome(
                self._barrier_outcome_status(character, all_keys),
                reason="barrier_stale", batch_id=batch_id,
                source_keys=tuple(all_keys),
            )
            self._settle_op(character, op_ids, outcome)
            return outcome
        if identity != self._identity_prompt(character, metadata):
            outcome = ExtractionOutcome("stale", reason="persona_changed", batch_id=batch_id, source_keys=tuple(all_keys))
            self._settle_op(character, op_ids, outcome)
            return outcome
        raw_candidates, raw_updates, raw_signals = parsed
        raw_updates = _scoped_state_updates(raw_updates, metadata)
        # An answer without attributable sources is a failed extraction, not
        # an empty result. Keep the entire batch queued before writing any
        # facts or state so a later retry can repair the model's citations.
        if (any(_candidate_citations(row, utterances) is None for row in raw_candidates)
                or any(not _valid_batch_sources(row, utterances) for row in raw_updates)):
            outcome = ExtractionOutcome(
                "failed", reason="unresolved_sources", batch_id=batch_id,
                source_keys=tuple(all_keys),
            )
            self._settle_op(character, op_ids, outcome)
            return outcome
        candidates = [
            candidate for candidate in (
                _bind_candidate(candidate, utterances) for candidate in raw_candidates
            ) if candidate is not None
        ]
        with self._barrier.write_lock(character):
            written, persisted, error = self._apply_facts(
                character, candidates, old_facts, snapshot,
                window_keys=all_keys, context_refs=reference_keys, episode_id=episode_id,
                occurred_at=utterances[0]["occurred_at"],
            )
        if error:
            outcome = ExtractionOutcome(
                self._barrier_outcome_status(character, all_keys) if error == "barrier_stale" else "failed",
                reason=error, batch_id=batch_id, source_keys=tuple(all_keys),
            )
            self._settle_op(character, op_ids, outcome)
            return outcome
        state_applied = 0
        if self._conversation_state is not None and raw_updates:
            entries = _bind_state_updates(raw_updates, utterances)
            if entries:
                state_applied = self._conversation_state.apply_sequence(
                    character, entries, metadata=metadata,
                )
        # Interest signals are written after the barrier freshness check above,
        # so a signal can never be recorded from a window the user has since
        # deleted; the service drops any candidate whose own citations do not
        # resolve to a user utterance (no source, no signal).
        signals_written = self._record_signals(character, raw_signals, utterances)
        outcome = ExtractionOutcome(
            "completed", facts_written=written, batch_id=batch_id,
            signals_written=signals_written,
            candidates=tuple(dict(row) for row in persisted),
            source_keys=tuple(all_keys), state_applied=state_applied,
        )
        self._settle_op(character, op_ids, outcome)
        return outcome

    def _record_signals(
        self,
        character: str,
        signals: Any,
        utterances: Sequence[Mapping[str, Any]],
    ) -> int:
        """Persist the interest signals of one extraction answer (RO03).

        A write failure keeps the source batch pending; previously committed
        facts are deduplicated on retry. An invalid signal citation is rejected
        by the interest service instead of being stored without a source.
        """
        if self._interests is None or not signals or all(is_friend_subject(row.get("metadata", {}).get("subject")) for row in utterances):
            return 0
        return int(self._interests.record(character, signals, utterances) or 0)

    def _batch_reference(
        self, character: str, utterances: Sequence[Mapping[str, Any]],
        all_keys: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Earlier dialogue for reference resolution only (never evidence)."""
        origin = dict(utterances[0]["metadata"] or {})
        keys = set(all_keys)
        history: list[dict[str, Any]] = []
        if origin.get("session_id"):
            recent = self._recent.load(character)
            boundary = next(
                (index for index, row in enumerate(recent)
                 if keys & message_source_keys(row)), 0,
            )
            for row in recent[:boundary]:
                meta = metadata_of(row)
                if all(meta.get(key) == origin.get(key) for key in
                       ("subject", "session_id", "scene", "applies_to", "client_id")):
                    history.append(row)
        return [dict(row) for row in history
                if str(row.get("type") or "").lower() in ("human", "user", "ai", "assistant")
                and is_extractable(metadata_of(row).get("content_class"))
                ][-self._settings.max_window_messages:]

    # -- extraction ------------------------------------------------------

    async def extract_facts(
        self,
        character: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        source_keys: Sequence[str] = (),
        episode_id: str | None = None,
        content_class: str | None = None,
        language: str = "zh",
        occurred_at: datetime | None = None,
    ) -> ExtractionOutcome:
        """Extract and persist facts from one window (None=failure, []=empty)."""
        if not self._settings.enabled:
            return ExtractionOutcome("empty", reason="memory_paused")
        settings = self._settings
        window = [
            dict(message) for message in messages or ()
            if isinstance(message, Mapping) and message_role(message) in ("user", "assistant", "tool")
        ]
        window = number_messages(window, source_keys, occurred_at)[-settings.max_window_messages:]
        if not window:
            return ExtractionOutcome("empty", reason="no_user_messages")
        deleted_keys = set(self._barrier.state(character).index.source_keys)
        legacy_cutoff = None
        if self._gate is not None:
            legacy_cutoff = self._gate.gate_for(character).legacy_barrier
        kept, _meta = filter_messages_for_extraction(
            window,
            legacy_barrier_cutoff=legacy_cutoff,
            deleted_source_keys=deleted_keys,
        )
        kept = self._barrier.filter_messages(character, kept)
        kept = [message for message in kept if is_extractable(metadata_of(message).get("content_class"))]
        if not kept:
            return ExtractionOutcome("empty", reason="filtered_out")
        # Support vs. context (growth design section 6.2). `evidence_keys` are
        # the keys of the turn actually being extracted: they are what the fact
        # *rests on*. Earlier dialogue only disambiguates a reference, so its
        # keys go to `context_refs` and never inflate the support count. Merging
        # them (the previous behaviour) made every fact in a session carry the
        # whole session, which the growth gate then counted as independent
        # experiences.
        evidence_keys = sorted({
            key for message in kept for key in message_source_keys(message)
        } | {str(key) for key in source_keys if key})
        context_keys: list[str] = []
        metadata = metadata_of(kept[0])
        state_updates = []
        signals = []
        snapshot = self._barrier.snapshot(character)
        old_facts = {}
        if content_class == SHARED_EXPERIENCE and _structured_experience(character, kept, evidence_keys):
            candidates = [_shared_experience_candidate(kept)]
        else:
            if self._model is None:
                return ExtractionOutcome("failed", reason="model_unavailable")
            context = self._prompt_context(character, messages, kept[0], evidence_keys)
            context, _ = filter_messages_for_extraction(
                context, legacy_barrier_cutoff=legacy_cutoff, deleted_source_keys=deleted_keys,
            )
            context = self._barrier.filter_messages(character, context)
            context_keys = sorted({
                key for message in context for key in message_source_keys(message)
            } - set(evidence_keys))
            identity = self._identity_prompt(character, metadata)
            old_facts = self._related_facts(character, kept, metadata, reference=context)
            prompt = identity + _build_prompt(kept, context) + _old_fact_material(old_facts)
            prompt += self._applied_correction_material(character, evidence_keys, metadata)
            if self._conversation_state is not None:
                state = self._conversation_state.snapshot(character, metadata)
                prompt += (
                    "\n\n我同时整理正在关注的事项，以第一人称写 text、next_step 和 why。输出采用 JSON 对象："
                    '{"facts":[上述事实],"state_updates":[事项操作]}。'
                    "事实或事项为空时分别使用空数组。我从明确计划、正在处理的事、"
                    "后续进展和完成/取消记录中整理事项。只在用户明确承诺、接受、正在做或实际要求跟进时建事项。"
                    "开放讨论、建议、问能力、当前一次性的请求与尚未接受的提议留在对话里。"
                    "操作采用 set_progress/set_concern/close_progress/close_concern；"
                    "更新或结束已有事项引用下面的 id，新建时 id 留空。"
                    "操作字段为 op/id/text/next_step/why。next_step 写适合后续交流的具体下一步。"
                    "明确完成或取消时使用 close_*；暂停话题保留其当前进度。"
                    + _state_followup_rule(metadata) +
                    "followup_when 在原话提供跟进时间时填写，格式与 event_when.start "
                    "一致（offset/unit），以本轮原始时间为基准；其余情况留空。"
                    "\n本轮原始时间：" + (occurred_at or self._clock.now()).isoformat()
                    + "\n当前同范围事项：" + json.dumps(list(state.items)[:20], ensure_ascii=False)
                )
            if self._interests is not None and not is_friend_subject(metadata.get("subject")):
                # Only ask for what can be stored (see the batched path).
                prompt += SIGNAL_PROMPT
            try:
                raw = await self._model.invoke(
                    settings.tier,
                    [{"role": "system", "content": prompt}],
                    max_output_tokens=settings.max_output_tokens,
                    timeout_s=settings.timeout_s,
                    purpose="memory_fact_extraction",
                )
            except Exception as exc:  # noqa: BLE001 - transport failure is retryable
                return ExtractionOutcome("failed", reason=f"transport:{type(exc).__name__}")
            parsed = _parse_extraction(raw, settings.max_candidates)
            if parsed is None:
                return ExtractionOutcome("failed", reason="parse")
            candidates, state_updates, signals = parsed
            if self._barrier.is_stale(character, snapshot):
                return ExtractionOutcome(self._barrier_outcome_status(character, evidence_keys), reason="barrier_stale")
            if identity != self._identity_prompt(character, metadata):
                return ExtractionOutcome("stale", reason="persona_changed")
        utterances = [{
            "index": 1, "messages": kept, "metadata": metadata,
            "has_user": any(_is_human_message(message) for message in kept),
            "source_keys": evidence_keys, "episode_id": episode_id,
            "content_class": content_class,
            "occurred_at": occurred_at or self._clock.now(),
        }]
        if any(_candidate_citations(candidate, utterances) is None for candidate in candidates):
            return ExtractionOutcome("failed", reason="unresolved_sources")
        bound = [_bind_candidate(candidate, utterances) for candidate in candidates]
        candidates = [dict(row, context_refs=context_keys) for row in bound if row is not None]
        with self._barrier.write_lock(character):
            written, persisted, error = self._apply_facts(
                character, candidates, old_facts, snapshot,
                window_keys=evidence_keys, context_refs=context_keys, episode_id=episode_id,
                occurred_at=occurred_at,
            )
        if error:
            status = self._barrier_outcome_status(character, evidence_keys) if error == "barrier_stale" else "failed"
            return ExtractionOutcome(status, reason=error)
        if self._conversation_state is not None:
            try:
                self._conversation_state.apply(
                    character, _scoped_state_updates(state_updates, metadata), metadata=metadata,
                    source_keys=evidence_keys, occurred_at=occurred_at,
                )
            except ValueError as exc:
                return ExtractionOutcome("failed", reason=f"state_update:{exc}")
        # One turn is one utterance, exactly as the fact window is: the same
        # citations (`evidence_keys`) and the same business time, so a signal
        # and the fact stating it can be traced to the identical support.
        signals_written = self._record_signals(character, signals, [{
            "index": 1,
            "has_user": any(_is_human_message(message) for message in kept),
            "source_keys": list(evidence_keys),
            "metadata": metadata,
            "occurred_at": occurred_at or self._clock.now(),
        }])
        return ExtractionOutcome(
            "completed", facts_written=written,
            candidates=tuple(dict(row) for row in persisted),
            signals_written=signals_written,
        )



    def _identity_prompt(self, character: str, metadata: Mapping[str, Any]) -> str:
        if is_friend_subject(metadata.get("subject")):
            return "这次对话的对象是 QQ 朋友。我整理这位朋友和本次互动的事实，按原话记录称呼与关系。\n\n"
        snapshot = self._persona_reader(character) if self._persona_reader else None
        if snapshot is None or not snapshot.available:
            return ""
        applies = metadata.get("applies_to") or {}
        scene = Scene(**{key: str(applies.get(key) or "") for key in ("plugin_id", "game_id", "session_id")})
        sections = {key: text for key, text in snapshot.projection.items()
                    if not text.startswith(EXAMPLES_BANNER)
                    and row_matches_subject(snapshot.section_scopes.get(key, {}), metadata.get("subject", "private"))
                    and scene_applies(snapshot.section_scopes.get(key, {}).get("applies_to"), scene)}
        budget = 6000
        lines = []
        for key, text in sections.items():
            lines.append(f"{key}: {text[:budget]}")
            budget -= min(len(text), budget)
            if budget <= 0:
                break
        return "我沿用当前人格中的姓名、性格与关系称呼整理这次互动。\n【现在的我】\n" + "\n".join(lines) + "\n\n"

    def _prompt_context(
        self, character: str, messages: Sequence[Mapping[str, Any]],
        first: Mapping[str, Any], source_keys: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Earlier dialogue is reference material, scoped to this conversation."""
        origin = metadata_of(first)
        history = []
        if origin.get("session_id"):
            recent = self._recent.load(character)
            keys = set(source_keys)
            boundary = next((index for index, row in enumerate(recent)
                             if keys & message_source_keys(row)), 0)
            for row in recent[:boundary]:
                meta = metadata_of(row)
                if all(meta.get(key) == origin.get(key) for key in
                       ("subject", "session_id", "scene", "applies_to", "client_id")):
                    history.append(row)
        # Direct callers may supply several messages without a stored session.
        for row in messages:
            if row == first:
                break
            history.append(dict(row))
        return [dict(row) for row in history
                if str(row.get("type") or "").lower() in ("human", "user", "ai", "assistant")
                and is_extractable(metadata_of(row).get("content_class"))
                ][-self._settings.max_window_messages:]

    # -- persistence -----------------------------------------------------

    def _applied_correction_material(self, character, keys, metadata):
        if self._corrections is None:
            return ""
        rows = self._corrections.extracted(character, keys, subject=metadata.get("subject"))
        if not rows:
            return ""
        return "\n本批原话已应用的更正（不重复新增或确认）：\n" + json.dumps(
            [{"source_key": row["source_key"], "text": row["text"]} for row in rows], ensure_ascii=False,
        )

    def _related_facts(self, character, messages, metadata, *, reference=()):
        current = " ".join(message_text(row) for row in messages if _is_human_message(row))
        previous = " ".join(message_text(row) for row in reference if _is_human_message(row))
        query = current[:3200] + " " + previous[-800:]
        applies_to = metadata.get("applies_to") or {}
        scene_kind, _, scene_id = str(metadata.get("scene") or "").partition(":")
        scene = dict(applies_to, session_id=metadata.get("session_id") or "")
        if scene_kind in ("game", "plugin"):
            scene[scene_kind + "_id"] = scene_id
        rows = [
            row for row in self._valid_facts(character)
            if row.get("id") and row_matches_subject(row, metadata.get("subject"))
            and scene_applies(row.get("applies_to"), scene)
        ]
        result = {}
        chars = _OLD_FACT_CHARS
        for row, _, _ in bm25_scores(query, rows):
            size = len(json.dumps(_old_fact_view(row), ensure_ascii=False)) + 2
            if size > chars:
                continue
            result[row["id"]] = row
            chars -= size
            if len(result) == _OLD_FACT_LIMIT:
                break
        return result

    def _valid_facts(self, character):
        archive = [row for row in self._facts.load_archive(character)
                   if not row.get("subject_archived_at") and not row.get("arbitration_archived_at")]
        return self._barrier.filter_rows(character, filter_valid_entries(self._facts.load(character) + archive))

    def _apply_facts(self, character, candidates, old_facts, snapshot, *, window_keys,
                     context_refs, episode_id, occurred_at):
        """Validate every operation before making any of the batch's writes."""
        if self._barrier.is_stale(character, snapshot):
            return 0, [], "barrier_stale"
        subject = candidates[0].get("subject") if candidates else None
        completed = (self._corrections.extracted(character, window_keys, subject=subject, repair=True)
                     if self._corrections else ())
        latest = {row["id"]: row for row in reversed(self._valid_facts(character)) if row.get("id")}
        completed_keys = {row["source_key"] for row in completed}
        corrections = {}
        for candidate in candidates:
            operation = candidate["operation"]
            if operation == "add":
                continue
            target_id = candidate.get("target_id")
            target = old_facts.get(target_id)
            if (target is None or target != latest.get(target_id)
                    or not row_matches_subject(target, candidate.get("subject"))
                    or (target.get("entity") or "master") != (candidate.get("entity") or "master")
                    or _scope_fields(target) != _scope_fields(candidate)
                    or not user_confirmation_bindings(candidate)
                    or len(user_confirmation_bindings(candidate)) != len(candidate.get("source_bindings") or ())):
                return 0, [], "invalid_fact_target"
            if operation == "correct":
                if self._corrections is None or target_id in corrections:
                    return 0, [], "invalid_fact_target"
                source = user_confirmation_bindings(candidate)[-1]["source_key"]
                corrections[target_id] = CorrectionRequest(
                    scope=Scope(character, subject_key(candidate.get("subject")) or "private"),
                    target_fact_ids=(target_id,), source_key=source,
                    new_text=candidate["text"][:MAX_FACT_TEXT],
                    applies_to=Scene(**dict(_scope_fields(candidate))),
                    request_id="extract_" + _fact_hash(source + "|" + target_id + "|correct"),
                )
        if any(row["operation"] == "confirm" and row["target_id"] in corrections for row in candidates):
            return 0, [], "conflicting_fact_operations"
        # Corrections already own invalidation and the new belief. They share
        # this write lock with extraction and deletion so their own barrier
        # increment cannot be confused with a concurrent write.
        # Commit independent facts before corrections. A replay of a source
        # that already completed a correction has already saved its additions.
        remaining = [row for row in candidates if row["operation"] != "correct"
                     and not set(row["source_event_keys"]) <= completed_keys]
        written, persisted = self._persist_candidates(
            character, remaining, source_keys=(), window_keys=window_keys,
            context_refs=context_refs, episode_id=episode_id, occurred_at=occurred_at,
        )
        for request in corrections.values():
            self._corrections.correct_extracted(request)
        return written + len(corrections), persisted, ""

    def _persist_candidates(
        self,
        character: str,
        candidates: Sequence[Mapping[str, Any]],
        *,
        source_keys: Sequence[str],
        episode_id: str | None,
        context_refs: Sequence[str] = (),
        occurred_at: datetime | None = None,
        window_keys: Sequence[str] | None = None,
    ) -> tuple[int, list[Mapping[str, Any]]]:
        """Dedupe by hash, apply the deletion fence, then commit facts/archive.

        A fact seen again is a real second source, not a retry: the new keys are
        merged into the existing row and `last_confirmed_at` moves forward. The
        previous behaviour dropped the second sighting entirely, so deleting the
        first source deleted the fact even though the user had said it twice
        (growth design G08).
        """
        if not candidates:
            return 0, []
        deleted = self._barrier.state(character).index.source_keys
        # A batch spans several turns: the deletion fence must look at every
        # window key, while each fact only ever carries the keys it cited.
        span_keys = list(source_keys if window_keys is None else window_keys)
        # Keys shared by every candidate of this call. The per-turn path puts
        # the window keys here (one turn == one evidence span); the batched
        # path passes none, because each candidate cites its own utterances.
        evidence_span = list(source_keys)
        window_deleted = bool(set(span_keys) & deleted)
        active = list(self._facts.load(character))
        archive = list(self._facts.load_archive(character))
        def scope_hash(row, digest):
            return (digest, subject_key(row.get("subject")) or "private",
                    str(row.get("entity") or "master"), _scope_fields(row))
        by_hash: dict[tuple, dict[str, Any]] = {}
        by_id = {}
        for row in self._barrier.filter_rows(character, filter_valid_entries(active + archive)):
            by_id.setdefault(row.get("id"), row)
            digest = row.get("hash") or _fact_hash(row["text"])
            by_hash.setdefault(scope_hash(row, digest), row)
        now = self._clock.now()
        # Business time of the turn, not of the queue: a batch processed the day
        # after it was spoken must still resolve "tomorrow" against the day it
        # was said (growth design G11).
        business_time = occurred_at or now
        context_keys = [str(key) for key in context_refs or () if isinstance(key, str) and key.strip()]
        batch_hashes: set[tuple] = set()
        new_rows: list[dict[str, Any]] = []
        upgrades = 0
        for candidate in candidates:
            text = str(candidate.get("text") or "").strip()[:MAX_FACT_TEXT]
            if not text:
                continue
            digest = _fact_hash(text)
            candidate_keys = [
                str(key) for key in candidate.get("source_event_keys") or ()
                if isinstance(key, str) and key.strip()
            ]
            # Host-gap fix: a late result from a deleted window is dropped even
            # when the LLM rewrote the text and the candidate has no key.
            if window_deleted:
                continue
            probe = {
                "id": f"fact_probe_{digest[:8]}",
                "text": text,
                "source_event_keys": candidate_keys or span_keys,
            }
            if self._barrier.blocks_row(character, probe):
                continue
            key = scope_hash(candidate, digest)
            existing = (by_id[candidate["target_id"]] if candidate["operation"] == "confirm"
                        else by_hash.get(key))
            if existing is not None:
                source = _candidate_source(candidate)
                changed = False
                users = user_confirmation_bindings(candidate)
                old_keys = set(existing.get("source_event_keys") or ())
                old_refs = set(existing.get("message_refs") or ())
                new_users = [binding for binding in users if binding["source_key"] not in old_keys
                             and binding["message_ref"] not in old_refs]
                if users and existing.get("source") == "ai_disclosure" and source == "user_observation":
                    existing["source"] = "user_observation"
                    existing["signal_processed"] = False
                    _confirm_kind(existing)
                    changed = True
                independent = any(binding["evidence_kind"] in ("observed_action", "tool_result")
                                  for binding in candidate.get("source_bindings") or ())
                if users or (independent and source == SHARED_EXPERIENCE):
                    groups = fact_support_groups(existing)
                    support = sorted(set(candidate_keys) | set(evidence_span))
                    changed |= _merge_source_keys(
                        existing, support,
                    )
                    if support and set(support) <= set(existing["source_event_keys"]) and support not in groups:
                        existing["support_groups"] = groups + [support]
                        changed = True
                    for binding in candidate.get("source_bindings") or ():
                        bindings = existing.setdefault("source_bindings", [])
                        if (binding not in bindings and len(bindings) < MAX_FACT_SOURCE_KEYS
                                and binding["source_key"] in existing["source_event_keys"]):
                            bindings.append(dict(binding))
                            changed = True
                    if existing.get("source_bindings"):
                        existing["message_refs"] = list(dict.fromkeys(
                            item["message_ref"] for item in existing["source_bindings"]
                        ))
                changed |= _merge_context_refs(existing, context_keys)
                kept_users = {binding["source_key"] for binding in user_confirmation_bindings(existing)}
                new_sources = {binding["source_key"] for binding in new_users} & kept_users
                if new_sources:
                    _refresh_confirmation(existing)
                    existing["confirm_count"] = _as_count(existing.get("confirm_count")) + len(new_sources)
                    if existing.get("retention_class") not in RETENTION_CLASSES:
                        legacy_class = existing.get("fading_class")
                        existing["retention_class"] = (legacy_class if legacy_class in RETENTION_CLASSES
                                                       else candidate["retention_class"])
                    existing["absorbed"] = False
                    changed = True
                upgrades += int(changed)
                continue
            if key in batch_hashes:
                continue
            batch_hashes.add(key)
            row = self._fact_row(
                candidate, text=text, digest=digest,
                source_keys=sorted(set(candidate_keys) | set(source_keys)),
                context_refs=context_keys,
                episode_id=episode_id,
                # Per-utterance anchor: a batched candidate carries the time of
                # the utterance it cited, so relative time never falls back to
                # the batch head or the processing time.
                now=candidate.get("_anchor") or business_time,
            )
            by_hash[key] = row
            new_rows.append(row)
        if new_rows or upgrades:
            self._facts.commit(character, active=active + new_rows, archive=archive)
        return len(new_rows) + upgrades, new_rows

    def _fact_row(
        self,
        candidate: Mapping[str, Any],
        *,
        text: str,
        digest: str,
        source_keys: Sequence[str],
        episode_id: str | None,
        now: datetime,
        context_refs: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Build one host-shaped fact row (fields frozen by the spec)."""
        importance = candidate.get("importance")
        if isinstance(importance, bool) or not isinstance(importance, int):
            importance = 5
        importance = max(1, min(10, importance))
        source = _candidate_source(candidate)
        entity = candidate.get("entity")
        if entity not in FACT_ENTITIES:
            entity = "master"
        event_when = candidate.get("event_when")
        if not isinstance(event_when, Mapping):
            event_when = None
        start_at, end_at = _resolve_event_when(event_when, now)
        row: dict[str, Any] = {
            "id": f"fact_{now:%Y%m%d%H%M%S}_{digest[:8]}",
            "text": text,
            "importance": importance,
            "entity": entity,
            "source": source,
            "tags": [],
            "hash": digest,
            "created_at": now.isoformat(),
            "event_when_raw": dict(event_when) if event_when else None,
            "event_start_at": start_at,
            "event_end_at": end_at,
            "schema_version": 2,
            "absorbed": False,
            "signal_processed": source in ("ai_disclosure", "shared_experience"),
            "embedding": None,
            "embedding_text_sha256": None,
            "embedding_model_id": None,
            "source_event_keys": list(source_keys),
            "support_groups": [list(source_keys)] if source_keys else [],
            "context_refs": list(context_refs)[:MAX_FACT_SOURCE_KEYS],
            "evidence_kind": _evidence_kind(candidate, source),
            "retention_class": candidate["retention_class"],
            "confirm_count": len({binding["source_key"] for binding in user_confirmation_bindings(candidate)}),
            "message_refs": list(candidate.get("message_refs") or ()),
            "source_bindings": list(candidate.get("source_bindings") or ()),
        }
        _refresh_confirmation(row)
        row["subject"] = str(candidate.get("subject") or "private")
        row["applies_to"] = dict(candidate.get("applies_to") or {})
        scope_suffix = hashlib.sha256((row["subject"] + row["entity"] + json.dumps(row["applies_to"], sort_keys=True)).encode()).hexdigest()[:8]
        row["id"] += "_" + scope_suffix
        if candidate.get("episode_id") or episode_id:
            row["episode_id"] = str(candidate.get("episode_id") or episode_id)[:200]
        return row

    def _store_raw(
        self, character: str, messages: Sequence[Mapping[str, Any]], session_id: str,
    ) -> int:
        """Append the raw turn to the time index (the extraction source)."""
        if self._timeindex_factory is None:
            return 0
        store = self._timeindex_factory(character)
        written = 0
        try:
            store.open()
            for message in messages:
                store.insert_original({
                    "session_id": session_id,
                    "message": json.dumps(dict(message), ensure_ascii=False),
                    "timestamp": metadata_of(message).get("occurred_at") or self._clock.now(),
                })
                written += 1
        finally:
            store.close()
        return written



_FENCE = chr(96) * 3


def _is_human_message(message: Mapping[str, Any]) -> bool:
    return str(message.get("type") or "").lower() in ("human", "user")


def _candidate_source(candidate: Mapping[str, Any]) -> str:
    source = candidate.get("source")
    if isinstance(source, str) and source in FACT_SOURCES:
        return source
    return "user_observation"


# -- batching helpers (RO02) -------------------------------------------------

#: Batch key components, in the order `batch_scope` stores them.
BATCH_SCOPE_FIELDS = ("character", "subject", "client_id", "session_id", "scene")


def _batch_scope(character: str, metadata: Mapping[str, Any]) -> list[str]:
    """The five fields two turns must share before they may share a call.

    Design section 6.2: the key is character, subject, client, session and
    scene, and a batch never crosses a session or a subject. Keeping it in the
    payload (instead of deriving it at drain time from whatever recent.json
    holds now) means a queued turn keeps the scope it was ingested under.
    """
    meta = metadata or {}
    return [
        str(character or ""),
        str(meta.get("subject") or "private"),
        str(meta.get("client_id") or ""),
        str(meta.get("session_id") or ""),
        str(meta.get("scene") or ""),
    ]


def _ts(value: datetime) -> float:
    """Ordering key for business times; naive and aware never mix as datetimes."""
    try:
        return value.timestamp()
    except (OverflowError, OSError, ValueError):  # pragma: no cover - extremes
        return 0.0


def _batch_utterances(
    ops: Sequence[Any], now: datetime,
) -> list[dict[str, Any]]:
    """Flatten queued ops into numbered utterances in event order.

    Each utterance keeps its own source keys, business time and position: that
    is what makes per-utterance attribution possible downstream. Ordering is
    by `occurred_at` (not by queue order and never by processing time), with
    the op id as a stable tiebreaker.
    """
    items: list[dict[str, Any]] = []
    for op in ops:
        payload = dict(op.payload)
        occurred = _parse_iso(payload.get("occurred_at")) or now
        messages = number_messages([
            dict(message) for message in payload.get("messages") or ()
            if isinstance(message, Mapping) and message_role(message) in ("user", "assistant", "tool")
        ], payload.get("source_keys") or (), occurred)
        first = messages[0] if messages else {}
        items.append({
            "op_id": str(op.op_id),
            "messages": messages,
            # An assistant-only turn can be stored and queued, but it can never
            # support a fact about the user (design 6.2).
            "has_user": any(_is_human_message(message) for message in messages),
            "source_keys": [str(key) for key in payload.get("source_keys") or ()
                            if isinstance(key, str) and key.strip()],
            "occurred_at": occurred,
            "metadata": dict(metadata_of(first)),
            "episode_id": payload.get("episode_id") or None,
            "content_class": payload.get("content_class"),
            "texts": [message_text(message).strip() for message in messages
                      if message_text(message).strip()],
        })
    items.sort(key=lambda item: (_ts(item["occurred_at"]), item["op_id"]))
    for index, item in enumerate(items, 1):
        item["index"] = index
    return items


def _batch_id(utterances: Sequence[Mapping[str, Any]]) -> str:
    """Stable batch id from the sources and the input texts.

    Retries, a partial write and a restart all rebuild the same id from the
    same queued material, so completion records and evidence stay linked to one
    batch no matter how many times the drain runs.
    """
    seed = json.dumps(
        [
            [item.get("index"), list(item.get("source_keys") or ()),
             list(item.get("texts") or ())]
            for item in utterances
        ],
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:16]


#: Time-unit resolutions that collapse to a calendar day when compared.
_COARSE_UNITS = ("day", "week", "month", "year")


def _resolve_for_utterance(
    event_when: Mapping[str, Any], anchor: datetime,
) -> tuple[str | None, str | None]:
    return _resolve_event_when(event_when, anchor)


def _same_when(
    left: tuple[str | None, str | None], right: tuple[str | None, str | None],
    event_when: Mapping[str, Any],
) -> bool:
    """Compare two resolutions at the precision the model actually spoke in."""
    units = {
        str(((event_when.get(side) or {}) if isinstance(event_when.get(side), Mapping)
             else {}).get("unit") or "day").lower()
        for side in ("start", "end")
        if isinstance(event_when.get(side), Mapping)
    }
    coarse = units and units.issubset(_COARSE_UNITS)

    def _key(value: str | None) -> Any:
        if value is None:
            return None
        parsed = _parse_iso(value)
        if parsed is None:
            return value
        return parsed.date().isoformat() if coarse else parsed.isoformat()

    return (_key(left[0]), _key(left[1])) == (_key(right[0]), _key(right[1]))


def _valid_batch_sources(
    row: Mapping[str, Any], utterances: Sequence[Mapping[str, Any]],
) -> bool:
    refs = row.get("sources") or row.get("utterances")
    if not isinstance(refs, list) or not refs:
        return False
    users = {item["index"] for item in utterances if item.get("has_user")}
    return all(
        not isinstance(ref, bool)
        and (isinstance(ref, int) or isinstance(ref, str) and ref.isdecimal())
        and int(ref) in users
        for ref in refs
    )


def _candidate_citations(
    candidate: Mapping[str, Any], utterances: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]] | None:
    """Validate precise speakers; old answers retain user-turn attribution."""
    refs = candidate.get("sources") or candidate.get("utterances")
    by_index = {item["index"]: item for item in utterances}
    if refs is None and len(utterances) == 1 and candidate.get("_structured_experience"):
        refs = [utterances[0]["index"]]
    if not isinstance(refs, list) or not refs:
        return None
    if any(isinstance(ref, bool) or not str(ref).isdecimal() or int(ref) not in by_index for ref in refs):
        return None
    cited = [by_index[index] for index in dict.fromkeys(int(ref) for ref in refs)]
    precise = candidate.get("message_refs")
    if not isinstance(precise, list) or not precise or any(not isinstance(ref, str) for ref in precise):
        return None
    messages = {metadata_of(message)["message_ref"]: (item, message)
                for item in cited for message in item["messages"]}
    if any(ref not in messages for ref in precise):
        return None
    selected = [messages[ref] for ref in dict.fromkeys(precise)]
    if {item["index"] for item, _ in selected} != {item["index"] for item in cited}:
        return None
    roles = {message_role(message) for _, message in selected}
    source = _candidate_source(candidate)
    entity = candidate.get("entity") or "master"
    if source in ("user_observation", "user_correction"):
        valid = entity in FACT_ENTITIES and roles == {"user"}
    elif source == "ai_disclosure":
        valid = entity == "neko" and roles == {"assistant"}
    else:
        valid = entity == "relationship" and (
            "user" in roles
            or candidate.get("_structured_experience")
            or "tool" in roles
        )
    if not valid:
        return None
    bindings = []
    for item, message in selected:
        binding = message_material(message)
        binding.pop("text")
        binding["source_key"] = binding["source_key"] or next(iter(item["source_keys"]), "")
        if binding["source_key"] not in item["source_keys"] or parse_source_key(binding["source_key"]) is None:
            return None
        kind = metadata_of(message).get("evidence_kind")
        if kind not in EVIDENCE_KINDS:
            kind = "tool_result" if message_role(message) == "tool" else (
                "agent_expression" if message_role(message) == "assistant" else "user_assertion"
            )
            if candidate.get("_structured_experience"):
                kind = "observed_action"
            elif candidate.get("evidence_kind") == "explicit_feedback" and message_role(message) == "user":
                kind = "explicit_feedback"
        binding["evidence_kind"] = kind
        bindings.append(binding)
    return cited, bindings


def _bind_candidate(
    candidate: Mapping[str, Any], utterances: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Bind one model candidate to the utterances it cites.

    Only cited utterances may become evidence; a candidate that cites nothing
    resolvable is dropped rather than attached to the whole batch (that was the
    old behaviour the growth gate then counted as many independent supports).
    """
    resolved = _candidate_citations(candidate, utterances)
    if resolved is None:
        return None
    cited, bindings = resolved
    cited.sort(key=lambda item: (item["occurred_at"], item["index"]))
    keys = sorted({binding["source_key"] for binding in bindings if binding["source_key"]})
    if not keys or _candidate_source(candidate) == "ai_disclosure" and all(
        binding["evidence_kind"] == "agent_expression" for binding in bindings
    ):
        return None
    event_when = candidate.get("event_when")
    anchor = cited[-1]["occurred_at"]
    if isinstance(event_when, Mapping) and event_when:
        # Relative time resolves against the utterance that stated the fact,
        # never against the batch head/tail or the processing time.
        first = _resolve_for_utterance(event_when, cited[0]["occurred_at"])
        for item in cited[1:]:
            if not _same_when(first, _resolve_for_utterance(
                event_when, item["occurred_at"],
            ), event_when):
                # Conflicting time bases in one batch: the fact cannot be
                # anchored, so it is skipped instead of being guessed (design
                # section 6.2: conflicting times are split or skipped).
                return None
        anchor = cited[0]["occurred_at"]
    row = dict(candidate)
    row["subject"] = cited[-1]["metadata"].get("subject", "private")
    row["applies_to"] = dict(cited[-1]["metadata"].get("applies_to") or {})
    row["source_event_keys"] = keys
    row["context_refs"] = []
    row["episode_id"] = cited[-1].get("episode_id")
    if bindings:
        row["source_bindings"] = bindings
        row["message_refs"] = [binding["message_ref"] for binding in bindings]
        kinds = {binding["evidence_kind"] for binding in bindings}
        requested = candidate.get("evidence_kind")
        row["evidence_kind"] = requested if requested in kinds else next(
            (kind for kind in ("tool_result", "observed_action", "explicit_feedback", "user_assertion", "agent_expression", "quoted_example")
             if kind in kinds), "user_assertion",
        )
        if _candidate_source(candidate) == "ai_disclosure":
            row["evidence_kind"] = "agent_expression"
    # Relative time is resolved against the earliest cited utterance: when the
    # model cites several turns that agree on the relative offset, the first
    # statement is the one that was actually said with that meaning.
    row["_anchor"] = anchor
    return row


def _state_followup_rule(metadata: Mapping[str, Any]) -> str:
    if is_friend_subject(metadata.get("subject")):
        return ("新事项仅收录这位朋友明确愿意以后继续的话题或双方约好的跟进，"
                "标记 agreed_followup=true，记录具体接续点。")
    return "对方明确约定提醒或回访时设置 agreed_followup=true，其他事项使用 false。"


def _scoped_state_updates(
    updates: list[dict[str, Any]], metadata: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not is_friend_subject(metadata.get("subject")):
        return updates
    return [row for row in updates if row.get("id") or row.get("ref") or row.get("agreed_followup") is True]


def _bind_state_updates(
    updates: Sequence[Mapping[str, Any]], utterances: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Attach an event time and cited sources to each conversation-state op.

    The returned list is in original event order, which is the order the state
    layer must apply them in: within one batch "new -> progress -> close" has
    to land in that order, and a late update for an old item must not override
    the newer terminal state.
    """
    by_index = {item["index"]: item for item in utterances}
    entries: list[dict[str, Any]] = []
    for position, update in enumerate(updates):
        if not isinstance(update, Mapping):
            continue
        cited: list[dict[str, Any]] = []
        for raw in update.get("sources") or ():
            if isinstance(raw, bool):
                continue
            try:
                index = int(raw)
            except (TypeError, ValueError):
                continue
            if index in by_index and by_index[index] not in cited:
                cited.append(by_index[index])
        if not cited:
            # No citation: the op would rest on nothing. Use the newest
            # utterance as its time base but keep its sources empty so the
            # barrier check has nothing to trust.
            anchor = utterances[-1]["occurred_at"]
            keys: list[str] = []
        else:
            cited.sort(key=lambda item: (item["occurred_at"], item["index"]))
            anchor = cited[0]["occurred_at"]
            keys = sorted({key for item in cited for key in item["source_keys"]})
        entry = dict(update)
        entry["_anchor"] = anchor
        entry["_source_keys"] = keys
        entry["_order"] = (anchor, position)
        entries.append(entry)
    entries.sort(key=lambda entry: entry["_order"])
    return entries


def _build_batch_prompt(
    utterances: Sequence[Mapping[str, Any]],
    reference: Sequence[Mapping[str, Any]] = (),
) -> str:
    """One prompt for several turns, with each turn numbered and time-stamped."""
    game_dialogue = any(
        str((item["metadata"] or {}).get("scene", "")).startswith("game:")
        for item in utterances
    )
    lines: list[str] = []
    for item in utterances:
        stamp = item["occurred_at"].isoformat()
        for message in number_messages(item["messages"], item["source_keys"], item["occurred_at"]):
            text = message_text(message).strip()
            if not text:
                continue
            speaker = message_role(message)
            lines.append(f"[{item['index']}] {stamp} | {speaker} | {text} | message_ref={metadata_of(message)['message_ref']}")
    body = "\n".join(lines)
    context = "\n".join(
        f"{'user' if _is_human_message(message) else 'assistant'} | "
        f"{message_text(message).strip()[:2000]}"
        for message in reference if message_text(message).strip()
    )
    return (
        FIRST_PERSON_VOICE + _FACT_ADMISSION
        + ("这些原话来自游戏中的交流，我在这里提取游戏之外的生活事实和通用相处偏好。"
           "混合表述采用其中生活或相处的部分。\n" if game_dialogue else "")
        + "\n\n"
        f"前文（仅供指代消解）：\n{context or '（无）'}\n\n本批互动：\n{body}"
    )


def _parse_batch_extraction(
    raw: Any, limit: int,
) -> tuple[list[dict[str, Any]], list[dict], list[dict]] | None:
    """Parse the batched answer; same shape, one extra citation field."""
    return _parse_extraction(raw, limit)


def _parse_iso(value: Any) -> datetime | None:
    """Parse a queued business timestamp; an unreadable value means "now".

    A missing key is the old-payload case (ops enqueued before this field
    existed) and must keep working rather than fail the window.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _evidence_kind(candidate: Mapping[str, Any], source: str) -> str:
    """Classify what the fact rests on, defaulting by source.

    The model may name a kind when the prompt supports it; an invented or
    missing value falls back to the source's default rather than being trusted.
    """
    raw = candidate.get("evidence_kind")
    if isinstance(raw, str) and raw.strip().lower() in EVIDENCE_KINDS:
        return raw.strip().lower()
    return EVIDENCE_BY_SOURCE.get(source, "user_assertion")


def _as_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _merge_source_keys(row: dict[str, Any], keys: Sequence[str]) -> bool:
    """Union new evidence keys into a stored fact; True if anything changed.

    Retries and mirrors carry the same key, so this is a no-op for them; only a
    genuinely different message adds support.
    """
    current = [
        str(key) for key in row.get("source_event_keys") or ()
        if isinstance(key, str) and key.strip()
    ]
    added = [key for key in keys if key not in current]
    if not added:
        return False
    merged = (current + added)[:MAX_FACT_SOURCE_KEYS]
    if merged == current:
        return False
    row["source_event_keys"] = merged
    return True


def _merge_context_refs(row: dict[str, Any], keys: Sequence[str]) -> bool:
    """Record additional reference-only keys; they never count as support."""
    if not keys:
        return False
    current = [
        str(key) for key in row.get("context_refs") or ()
        if isinstance(key, str) and key.strip()
    ]
    added = [key for key in keys if key not in current and key not in (row.get("source_event_keys") or ())]
    if not added:
        return False
    row["context_refs"] = (current + added)[:MAX_FACT_SOURCE_KEYS]
    return True


def _confirm_kind(row: dict[str, Any]) -> bool:
    """Promote a weak kind when a user assertion corroborates it."""
    if row.get("evidence_kind") != "agent_expression":
        return False
    kinds = row.get("evidence_kinds")
    values = [str(item) for item in kinds or () if isinstance(item, str)]
    if "user_assertion" in values:
        return False
    row["evidence_kinds"] = values + ["user_assertion"]
    return True


def _fact_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _session_from(scene: Scene, episode_id: str | None) -> str:
    if episode_id:
        parts = str(episode_id).split(":")
        if len(parts) >= 4 and parts[0] == "ep":
            return parts[-1]
    if scene.session_id:
        return scene.session_id
    return "companion_" + hashlib.sha1(
        f"{scene.plugin_id}|{episode_id}".encode("utf-8"),
    ).hexdigest()[:16]


_FACT_ADMISSION = """
我只保存有明确后续用途的内容：有依据的个人事实、偏好、限制，以及有结果、持续影响或明确纪念意义的共同经历。
有待处理的计划、约定、进展、普通事项的完成或取消交给事项操作。只有另有独立的持续用途才额外保存事实。普通追问、接话、玩笑、表情、附件编号和控制标签留在对话里。
只针对当前回复的风格、长度、是否播放语音等指示属于本轮操作；对方明确说长期适用才记录为偏好。
提问不等于兴趣，一次行为不自动成为习惯。我的随口表达、愿望、设想、即兴扮演和未获接受的建议不形成事实或共同约定。
询问某件商品、比较选项、当次价格或货源只留在对话里。已完成的购买、明确拥有的物品、可复用的用途偏好可以记录。购物讨论中的“暂时买不到”和“先选这个吧”保留当次语境，只有用户明确说是持续限制或稳定偏好时才立事实。
已完成的购买若明确给出品名、规格和实付金额，把这些信息写在同一条事实里，保留原话中的数字和单位。比如“我买了 500 克的茶，花了 80 元”可记为“我记得对方花 80 元买了 500 克茶”。
只记录原话能支持的内容，经历有结果才写结果，关系意义须有当事人的表达。
我保留事实的适用场合、时间和语气。例如明确说“以前一直放在书桌上，现在放抽屉”时，记录当前位置和有用的变化。“这次想看某类内容”只描述这次的目标，只有用户已建立相关领域或计划时才写上该领域或计划。
同一用途的一组选择在一条里写清。若对方说自己有甲、乙，两件都适合某个用途，这一条就能同时保留明确拥有和用途偏好；拥有关系有独立用途或需单独更新时才另存。后面明确增加第三项时，结合本批证据与已确认条目识别它是补充，同一批只记一次完整偏好。
我以第一人称写 text，主体和说话者由字段及原话决定。旧事实和前文只供比较或理解指代，本批原话才是证据。
每条事实填写 text、importance（1-10）、entity（master/neko/relationship）、source（user_observation/user_correction/ai_disclosure/shared_experience）、
sources（整数轮次编号）、message_refs（逐字复制 message_ref= 后的引用），以及 retention_reason（具体后续用途）和 retention_class。
保留类别：durable 用于有明确依据的稳定身份、长期偏好或限制；shared_important 用于有明确重要结果、持续影响或纪念意义的经历；
normal 用于有后续用途的普通事实或经历；daily 用于确有短期用途的信息；transient 用于已有入口需短期保留的临时信息。
等级低也要通过准入。自己的表达和共同经历标签本身不构成长久价值。
operation 填 add、confirm 或 correct。add 的 target_id 留空，补充时只写新增的独立内容。
confirm 指向意义相同、主体和适用范围一致的旧 ID，只使用新的用户原话。角色复述与引用不构成确认。
correct 指向用户明确指出记错的旧 ID，text 只写正确的新认识，引用用户原话。过去属实、现在改变时按时间新增，用正文交代当前或历史，不纠错。
同批尚无旧 ID 的相同陈述合成一条。无合格内容时 facts=[]（或空数组）。
姓名、住处等可独立成立的陈述分别输出。每条只引用支撑该陈述的原话；多条原话合起来才能支撑的经历才合写。
对明确事件时间，可填写 event_when.start/end，格式为 null 或包含整数 offset 和 unit（minute/hour/day/week/month/year）的对象；
偏好一般省略 event_when。输出 JSON。每条 facts 对象必须显式填写 operation 和 target_id，操作示意如下：
{"operation":"add","target_id":"","retention_class":"durable","retention_reason":"以后准备饮品时优先选择这个口味",
 "text":"我记得对方长期喜欢无糖豆浆。","entity":"master","source":"user_observation","importance":5,
 "sources":[1],"message_refs":["此处替换为本批真实消息引用"]}。
confirm 或 correct 同样填写 target_id 字段，值为相关旧事实中原样的 id。
"""


def _scope_fields(row):
    scope = row.get("applies_to") or {}
    return tuple((key, scope[key]) for key in ("game_id", "plugin_id", "session_id") if scope.get(key))


def _old_fact_view(row):
    return {key: row.get(key) for key in ("id", "text", "subject", "entity", "applies_to",
                                          "created_at", "event_start_at", "evidence_kind", "retention_class")}


def _old_fact_material(rows):
    return "\n\n相关旧事实（只用于比较）：\n" + json.dumps(
        [_old_fact_view(row) for row in rows.values()], ensure_ascii=False,
    )


def _valid_operation(row):
    op = row.get("operation")
    target = row.get("target_id")
    text = row.get("text")
    reason = row.get("retention_reason")
    return (isinstance(text, str) and bool(text.strip())
            and isinstance(reason, str) and bool(reason.strip())
            and row.get("retention_class") in RETENTION_CLASSES
            and ((op == "add" and target in (None, ""))
                 or (op in ("confirm", "correct") and isinstance(target, str) and bool(target.strip()))))


def _structured_experience(character, messages, keys):
    # The structured plugin event carries one sourced result, not a chat exchange.
    source = parse_source_key(keys[0]) if len(keys) == 1 else None
    return (source and source["kind"] == "plugin" and source["character"] == character
            and len(messages) == 1 and metadata_of(messages[0]).get("structured_experience") is True
            and bool(message_text(messages[0]).strip()))


def _refresh_confirmation(row):
    confirmed = confirmation_time(row)
    if confirmed is not None:
        row["last_confirmed_at"] = confirmed.isoformat()
        row["confirmation_basis"] = "user"


def _shared_experience_candidate(messages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    text = message_text(messages[-1]) if messages else ""
    return {"text": text, "importance": 5, "source": "shared_experience", "entity": "relationship",
            "message_refs": [metadata_of(message)["message_ref"] for message in messages],
            "evidence_kind": "observed_action", "operation": "add",
            "retention_class": "normal", "_structured_experience": True}


def _build_prompt(
    messages: Sequence[Mapping[str, Any]], context: Sequence[Mapping[str, Any]] = (),
) -> str:
    game_dialogue = any(
        str(((message.get("data") or {}).get("metadata") or message.get("metadata") or {}).get("scene", "")).startswith("game:")
        for message in messages
    )
    lines = [f"[1] {metadata_of(message).get('occurred_at', '')} | {message_role(message)} | "
             f"{message_text(message).strip()} | message_ref={metadata_of(message)['message_ref']}"
             for message in number_messages(messages) if message_text(message).strip()]
    body = "\n".join(lines)
    reference = "\n".join(
        f"{'user' if _is_human_message(message) else 'assistant'} | {message_text(message).strip()[:2000]}"
        for message in context if message_text(message).strip()
    )
    return (
        FIRST_PERSON_VOICE + _FACT_ADMISSION
        + ("这些原话来自游戏中的交流，我在这里提取游戏之外的生活事实和通用相处偏好。"
           "混合表述采用其中生活或相处的部分。\n" if game_dialogue else "")
        + "\n\n"
        f"前文（仅供指代消解）：\n{reference or '（无）'}\n\n本轮互动：\n{body}"
    )


def _parse_extraction(
    raw: Any, limit: int,
) -> tuple[list[dict[str, Any]], list[dict], list[dict]] | None:
    """Accept the combined extraction result and existing fact-only arrays.

    Three shapes are valid, and the first two predate RO03:

    * a bare JSON array of facts (the original contract, and still the answer
      when no state/signal layer is wired);
    * an object with `facts` + `state_updates` (M7);
    * that object plus `interest_signals` (RO03).

    `interest_signals` is optional on purpose: a model that answers with the
    old shape is not a parse failure, it simply reported no signals. Only the
    fact list and `state_updates` stay mandatory, because a missing `facts` key
    would turn "no facts" into "unreadable answer" and keep the window pending
    forever.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith(_FENCE):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith(_FENCE):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    updates: Any = []
    signals: Any = []
    if isinstance(data, Mapping):
        updates = data.get("state_updates", [])
        signals = data.get("interest_signals", [])
        data = data.get("facts")
    if not isinstance(data, list):
        return None
    if updates is None:
        updates = []
    if not isinstance(updates, list):
        return None
    if not isinstance(signals, list):
        # A malformed signal list is dropped, not fatal: the facts in the same
        # answer are still good and the window is worth completing.
        signals = []
    if any(not isinstance(item, Mapping) for item in updates):
        return None
    candidates: list[dict[str, Any]] = []
    for item in data[:limit]:
        if not isinstance(item, Mapping):
            return None
        candidate = dict(item)
        if not _valid_operation(candidate):
            return None
        candidates.append(candidate)
    return candidates, [dict(item) for item in updates[:8]], [
        dict(item) for item in signals[:limit] if isinstance(item, Mapping)
    ]


def _parse_candidates(raw: Any, limit: int) -> list[dict[str, Any]] | None:
    parsed = _parse_extraction(raw, limit)
    return parsed[0] if parsed is not None else None


_EVENT_UNIT_SECONDS = {
    "minute": 60, "hour": 3600, "day": 86400, "week": 604800,
    "month": 2592000, "year": 31536000,
}


def _resolve_event_when(
    event_when: Mapping[str, Any] | None, now: datetime,
) -> tuple[str | None, str | None]:
    """Resolve the host relative event_when into absolute ISO timestamps."""
    if not isinstance(event_when, Mapping):
        return None, None

    def _parse(side: str) -> str | None:
        part = event_when.get(side)
        if not isinstance(part, Mapping):
            return None
        offset = part.get("offset")
        unit = part.get("unit")
        if isinstance(offset, bool) or not isinstance(offset, int):
            return None
        seconds = _EVENT_UNIT_SECONDS.get(str(unit or "day").lower())
        if seconds is None:
            return None
        return (now + timedelta(seconds=offset * seconds)).isoformat()

    start = _parse("start")
    end = _parse("end")
    if start is None and end is None:
        return None, None
    return start or end, end


__all__ = [
    "EXTRACT_OP_TYPE",
    "ExtractionOutcome",
    "ExtractionService",
    "ExtractionSettings",
    "IngestResult",
]

