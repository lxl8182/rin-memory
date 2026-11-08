"""Whole-turn commit orchestration (S02).

`docs/standalone-service.md` §3.3/§3.4 define the order this module implements:

    validate identity/scope/turn/idempotency key
      -> committed request: replay after the current deletion/correction check
      -> new request: reserve the pending state, fixing input and occurred_at
      -> read persona snapshot, recent session, constraints
      -> generate the reply (service-generation mode)
      -> re-check that the basis was not deleted/corrected/root-switched
      -> persist the whole turn, the generation version and the extraction intent
      -> idempotently apply the message views and the extraction queue
      -> return the committed result; extraction continues in the background

The service deliberately does **not** own the model. Generation is supplied by
the caller as an async callable, so this layer stays usable from the standalone
service (S04), the host, or a test double, and never imports a provider.

Failure semantics that matter for recovery:

* a generation failure leaves the record `failed` (retryable) and the turn id
  unspent, so a retry with the same request id continues instead of duplicating;
* a basis that went stale (deletion/correction after generation) commits
  nothing and returns `context_stale`, per §3.3;
* a crash after the journal entry but before the views are written is repaired
  by `recover()`, which replays the committed snapshot and then finishes the
  missing view work exactly once.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..persistence.turns import (
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    TURN_EVENT_TYPE,
    TurnRecord,
    TurnRequestStore,
    turn_fingerprint,
)
from ..ports import Clock, EventJournal, WritePermit

#: Generation callable: (messages) -> reply text. Injected, never imported.
Generator = Callable[[Sequence[Mapping[str, Any]]], Awaitable[str]]
#: Basis check: returns True when the turn's basis is still valid.
BasisCheck = Callable[[str], bool]


@dataclass(frozen=True)
class TurnOutcome:
    """Result of one commit attempt, shaped for the §3.2 response contract."""

    request_id: str
    turn_id: str
    status: str
    durable: bool
    replayed: bool
    reply: str | None = None
    extraction_op_id: str | None = None
    persona_version: str | None = None
    processing_position: int | None = None
    reason: str | None = None
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "status": self.status,
            "durable": self.durable,
            "replayed": self.replayed,
            "processing_position": self.processing_position,
            "extraction_status": (
                "queued" if self.extraction_op_id else
                ("committed" if self.durable else "not_queued")
            ),
            "persona_version": self.persona_version,
            "reason": self.reason,
        }
        if self.reply is not None:
            payload["reply"] = self.reply
        return payload


@dataclass(frozen=True)
class TurnRequest:
    """One business turn as the caller states it (§3.2 fields)."""

    character: str
    request_id: str
    turn_id: str
    source_key: str
    content_class: str
    messages: Sequence[Mapping[str, Any]]
    subject: str = "private"
    session_id: str = ""
    episode_id: str = ""
    parent_turn_id: str = ""
    persona_version: str = ""
    card_revision: int = 0
    scene: str = ""
    applies_to: str = ""
    #: Business time of the turn, ISO-8601. Set once and reused on every retry
    #: so the source fingerprint does not change between attempts (R04).
    occurred_at: str = ""
    client_id: str = ""
    context_receipt_id: str = ""
    reply_transport: str = ""

    def fingerprint(self) -> str:
        return turn_fingerprint(
            messages=self.messages,
            content_class=self.content_class,
            source_key=self.source_key,
            episode_id=self.episode_id,
            session_id=self.session_id,
            applies_to=self.applies_to,
            scene=self.scene,
        )


class TurnCommitService:
    """Reserve -> generate -> commit -> enqueue, with replay and repair."""

    def __init__(
        self,
        *,
        turns: TurnRequestStore,
        journal: EventJournal,
        permit: WritePermit,
        clock: Clock,
        character: str = "",
        ingest: Callable[..., Awaitable[Any]] | None = None,
        generator: Generator | None = None,
        basis_check: BasisCheck | None = None,
    ) -> None:
        self._turns = turns
        self._journal = journal
        self._permit = permit
        self._clock = clock
        #: Default character; a per-call `character=` overrides it, which is
        #: what a service process receiving requests for any character needs.
        self._character = character
        self._ingest = ingest
        self._generator = generator
        self._basis_check = basis_check

    def _name(self, character: str | None) -> str:
        name = str(character or self._character or "").strip()
        if not name:
            raise ValueError("character must be nonempty")
        return name

    # -- public entry points --------------------------------------------

    async def commit_generated(
        self, request: TurnRequest, *, character: str | None = None,
    ) -> TurnOutcome:
        """Service-generation mode: this service generates and commits."""
        if self._generator is None:
            raise RuntimeError(
                "no conversation generator is configured; use commit_external "
                "for client-generated turns",
            )
        return await self._commit(self._named(request, character), generate=True)

    async def commit_external(
        self, request: TurnRequest, reply: str | None, *,
        character: str | None = None,
    ) -> TurnOutcome:
        """Client-generated mode: the caller already has the reply."""
        return await self._commit(
            self._named(request, character), generate=False, reply=reply,
        )

    def _named(self, request: TurnRequest, character: str | None) -> TurnRequest:
        name = self._name(character)
        if request.character == name:
            return request
        from dataclasses import replace

        return replace(request, character=name)

    def status(
        self, request_id: str, *, character: str | None = None,
    ) -> TurnOutcome | None:
        """Progress or replay of an already seen request (§3.1 GET semantics)."""
        record = self._turns.get(self._name(character), request_id)
        return None if record is None else self._outcome(record, replayed=False)

    def invalidate_sources(
        self, source_keys: set[str], *, character: str | None = None,
    ) -> list[str]:
        """Clear reply bodies for turns whose source was deleted/corrected."""
        return self._turns.invalidate_by_source(self._name(character), source_keys)

    def recover(self, *, character: str | None = None) -> dict[str, Any]:
        """Finish interrupted commit work for one character.

        The journal replay restores committed records; anything left `pending`
        belongs to a process that died mid-generation, so it is marked
        `interrupted` (retryable) instead of being silently trusted.
        """
        name = self._name(character)
        result = self._journal.reconcile(name)
        interrupted = self._turns.mark_interrupted_pending(name)
        return {
            "journal": {
                "applied": getattr(result, "applied", 0),
                "paused": getattr(result, "paused", False),
                "reason": getattr(result, "reason", None),
            },
            "interrupted": interrupted,
        }

    # -- core flow -------------------------------------------------------

    async def _commit(
        self,
        request: TurnRequest,
        *,
        generate: bool,
        reply: str | None = None,
    ) -> TurnOutcome:
        fingerprint = request.fingerprint()
        reservation = self._turns.reserve(
            self._character,
            request_id=request.request_id,
            fingerprint=fingerprint,
            source_key=request.source_key,
            turn_id=request.turn_id,
            content_class=request.content_class,
            subject=request.subject,
            client_id=request.client_id,
            scene=request.scene,
            context_receipt_id=request.context_receipt_id,
            session_id=request.session_id,
            episode_id=request.episode_id,
            parent_turn_id=request.parent_turn_id,
            persona_version=request.persona_version,
            card_revision=request.card_revision,
            occurred_at=request.occurred_at,
        )
        if reservation.kind == "replay":
            return self._outcome(reservation.record, replayed=True)
        if reservation.kind == "conflict":
            return self._conflict_outcome(reservation.record, request)
        if reservation.kind == "deleted":
            return self._deleted_outcome(reservation.record, request)
        if reservation.kind == "in_progress":
            return self._in_progress_outcome(reservation.record, request)

        # A duplicate business turn (another client mirrored it) must reuse the
        # committed record instead of creating a second experience.
        mirrored = self._turns.find_by_turn(self._character, request.turn_id)
        if mirrored is not None and mirrored.request_id != request.request_id:
            if mirrored.is_committed:
                self._turns.invalidate(self._character, [request.request_id],
                                       reason="mirror_of_committed_turn")
                replay = self._outcome(mirrored, replayed=True)
                return TurnOutcome(
                    request_id=request.request_id,
                    turn_id=replay.turn_id,
                    status=replay.status,
                    durable=True,
                    replayed=True,
                    reply=replay.reply,
                    extraction_op_id=replay.extraction_op_id,
                    persona_version=replay.persona_version,
                    processing_position=replay.processing_position,
                    reason="mirror_of_committed_turn",
                )

        generated = reply
        if generate:
            try:
                generated = await self._generator(list(request.messages))  # type: ignore[misc]
            except asyncio.CancelledError:
                self._turns.mark_failed(
                    self._character, request.request_id,
                    status=STATUS_INTERRUPTED, reason="generation_cancelled",
                )
                raise
            except Exception as exc:  # noqa: BLE001 - retryable, recorded
                record = self._turns.mark_failed(
                    self._character, request.request_id,
                    status=STATUS_FAILED,
                    reason=f"generation_failed:{type(exc).__name__}",
                )
                return self._outcome(record, replayed=False)

        # Re-check the basis *after* generation: a deletion or correction while
        # the model ran invalidates this attempt, and nothing is committed.
        if self._basis_check is not None and not self._basis_check(self._character):
            record = self._turns.mark_failed(
                self._character, request.request_id,
                status=STATUS_FAILED, reason="context_stale",
            )
            outcome = self._outcome(record, replayed=False)
            return TurnOutcome(
                request_id=outcome.request_id, turn_id=outcome.turn_id,
                status="context_stale", durable=False, replayed=False,
                reason="context_stale", retryable=True,
            )

        return await self._persist(request, generated)

    def _turn_messages(
        self, request: TurnRequest, reply: str | None,
    ) -> list[dict[str, Any]]:
        """The messages stored for this turn, including the generated reply.

        R05: `/chat` committed only the human input, so the assistant side of
        the conversation never reached `recent.json` -- the next turn had no
        idea what it had just said. A service-generated reply is appended as an
        `ai` message; an externally finished turn already carries its own
        assistant messages, so nothing is appended for it (the caller's batch
        is complete by definition).
        """
        messages = [dict(item) for item in request.messages]
        if request.reply_transport:
            messages = [message for message in messages if message.get("type") != "ai"]
            if reply:
                messages.append({"type": "ai", "data": {"content": "", "metadata": {"reply_transport": request.reply_transport}}})
        elif reply and str(reply).strip() and not any(
            str(item.get("type") or "").lower() in ("ai", "assistant")
            for item in messages
        ):
            # The caller supplied the assistant side itself.
            data = {"content": str(reply).strip()}
            reasoning = getattr(reply, "reasoning", "")
            if reasoning:
                data["additional_kwargs"] = {"reasoning_content": reasoning}
            messages.append({"type": "ai", "data": data})
        for message in messages:
            data = dict(message.get("data") or {})
            meta = dict(data.get("metadata") or message.get("metadata") or {})
            meta.update(subject=request.subject, scene=request.scene, session_id=request.session_id,
                        source_key=request.source_key, source_event_keys=[request.source_key],
                        turn_id=request.turn_id, request_id=request.request_id,
                        client_id=request.client_id, content_class=request.content_class)
            applies = request.applies_to or request.scene
            kind, _, value = applies.partition(":")
            if kind in ("game", "plugin", "run", "session") and value:
                meta["applies_to"] = {{"game": "game_id", "plugin": "plugin_id"}.get(kind, "session_id"): value}
            data["metadata"] = meta
            message["data"] = data
        return messages

    def _occurred_at(self, request: TurnRequest):
        """The turn's business time, stable across retries and restarts (R04).

        The source fingerprint includes `occurred_at`, so deriving it from the
        clock on each attempt made a retry look like different content and fail
        as a source conflict. Preference order: what the caller stated, then
        what the first attempt stored, then "now" for a genuinely new turn.
        """
        from datetime import datetime

        candidate = (request.occurred_at or "").strip()
        if not candidate:
            record = self._turns.get(self._character, request.request_id)
            if record is not None:
                candidate = (getattr(record, "occurred_at", "") or "").strip()
        if candidate:
            try:
                return datetime.fromisoformat(candidate)
            except ValueError:
                pass
        return None

    async def _persist(
        self, request: TurnRequest, reply: str | None,
    ) -> TurnOutcome:
        """Commit the whole turn: queue the extraction, then journal the turn.

        Order matters for recovery. The extraction op is enqueued first because
        it is the only step that can be retried from its own queue; the turn
        record is then written *through the journal* (`record_and_save`), so the
        committed state and its replayable event are one durable step. A crash
        after the journal fsync but before the view save leaves an entry that
        `recover()` replays into the record -- there is no window in which the
        caller was told "committed" while the record is missing.
        """
        extraction_op_id = None
        if self._ingest is not None:
            try:
                ingested = await self._ingest(
                    self._character,
                    self._turn_messages(request, reply),
                    source_key=request.source_key,
                    content_class=request.content_class,
                    episode_id=request.episode_id or None,
                    session_id=request.session_id or None,
                    inline=False,
                    occurred_at=self._occurred_at(request),
                )
            except Exception as exc:  # noqa: BLE001 - must stay retryable
                # The raw turn may already be durable while the extraction op
                # is not. Leaving the record `pending` made the retry look like
                # a concurrent attempt (`turn_in_progress`, R04), so the failure
                # is recorded as retryable and the caller may continue the same
                # request id.
                record = self._turns.mark_failed(
                    self._character, request.request_id,
                    status=STATUS_FAILED,
                    reason=f"ingest_failed:{type(exc).__name__}",
                )
                return self._outcome(record, replayed=False)
            extraction_op_id = getattr(ingested, "op_id", None)
            if getattr(ingested, "status", "") in ("rejected", "deleted"):
                record = self._turns.mark_failed(
                    self._character, request.request_id,
                    status=STATUS_FAILED,
                    reason=str(getattr(ingested, "reason", "ingest_rejected")),
                )
                return self._outcome(record, replayed=False)

        payload = self._committed_payload(request, reply, extraction_op_id)
        try:
            entry = self._journal.record_and_save(
                self._character,
                TURN_EVENT_TYPE,
                payload,
                load_view=lambda character: self._turns.load_view(character),
                mutate_view=lambda view: self._turns.apply_snapshot(view, payload),
                save_view=lambda character, view: self._turns.save_view(
                    character, view,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a retryable failure
            record = self._turns.mark_failed(
                self._character, request.request_id,
                status=STATUS_FAILED,
                reason=f"commit_failed:{type(exc).__name__}",
            )
            return self._outcome(record, replayed=False)
        record = self._turns.get(self._character, request.request_id)
        if record is not None and entry is not None and record.processing_position is None:
            # The journal position is assigned by the append, so it is known
            # only after the commit; record it for the §3.2 response and for
            # support ("which entry is this turn?"). A failure here must not
            # undo a committed turn.
            try:
                record = self._turns.attach_processing_position(
                    self._character, request.request_id, entry.position,
                )
            except Exception:  # noqa: BLE001 - the commit itself succeeded
                pass
        if record is None:  # pragma: no cover - the view save is synchronous
            raise RuntimeError("turn record disappeared after a successful commit")
        return self._outcome(record, replayed=False)

    def _committed_payload(
        self,
        request: TurnRequest,
        reply: str | None,
        extraction_op_id: str | None,
    ) -> dict[str, Any]:
        """The journal snapshot of a committed turn (see turns.py)."""
        from ..persistence.turns import _record_snapshot  # local: keeps the API tight

        now = self._clock.now().isoformat()
        record = TurnRecord(
            request_id=request.request_id,
            status="committed",
            fingerprint=request.fingerprint(),
            source_key=request.source_key,
            turn_id=request.turn_id,
            content_class=request.content_class,
            subject=request.subject,
            client_id=request.client_id,
            scene=request.scene,
            context_receipt_id=request.context_receipt_id,
            session_id=request.session_id,
            episode_id=request.episode_id,
            parent_turn_id=request.parent_turn_id,
            persona_version=request.persona_version,
            card_revision=int(request.card_revision or 0),
            reply=reply,
            extraction_op_id=extraction_op_id,
            reason=None,
            processing_position=None,
            created_at=now,
            updated_at=now,
            occurred_at=request.occurred_at or now,
        )
        return dict(_record_snapshot(record))

    # -- outcome helpers -------------------------------------------------

    def _outcome(self, record: TurnRecord | None, *, replayed: bool) -> TurnOutcome:
        if record is None:  # pragma: no cover - defensive
            raise ValueError("turn record is required")
        reply = record.reply if record.body_available else None
        return TurnOutcome(
            request_id=record.request_id,
            turn_id=record.turn_id,
            status=record.status,
            durable=record.is_committed,
            replayed=replayed,
            reply=reply,
            extraction_op_id=record.extraction_op_id,
            persona_version=record.persona_version or None,
            processing_position=record.processing_position,
            reason=record.reason,
        )

    def _conflict_outcome(
        self, record: TurnRecord | None, request: TurnRequest,
    ) -> TurnOutcome:
        return TurnOutcome(
            request_id=request.request_id,
            turn_id=request.turn_id,
            status="conflict",
            durable=True,
            replayed=False,
            reason="request_id_reused_with_different_input",
            retryable=False,
        )

    def _deleted_outcome(
        self, record: TurnRecord | None, request: TurnRequest,
    ) -> TurnOutcome:
        """§3.2: an invalidated result returns 410-ish state, never the body."""
        return TurnOutcome(
            request_id=request.request_id,
            turn_id=request.turn_id,
            status="deleted",
            durable=True,
            replayed=False,
            reason=(record.reason if record is not None else None) or "deleted",
            retryable=False,
        )

    def _in_progress_outcome(
        self, record: TurnRecord | None, request: TurnRequest,
    ) -> TurnOutcome:
        return TurnOutcome(
            request_id=request.request_id,
            turn_id=request.turn_id,
            status="in_progress",
            durable=True,
            replayed=False,
            reason="turn_in_progress",
            retryable=True,
        )


def build_turn_service(
    *,
    turns: TurnRequestStore,
    journal: EventJournal,
    permit: WritePermit,
    clock: Clock,
    character: str,
    ingest: Callable[..., Awaitable[Any]] | None = None,
    generator: Generator | None = None,
    basis_check: BasisCheck | None = None,
) -> TurnCommitService:
    """Construct the service for one character (explicit root, single writer)."""
    return TurnCommitService(
        turns=turns, journal=journal, permit=permit, clock=clock,
        character=character, ingest=ingest, generator=generator,
        basis_check=basis_check,
    )
