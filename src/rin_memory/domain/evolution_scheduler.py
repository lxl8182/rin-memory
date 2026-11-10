"""C03 background evolution scheduler.

The scheduler is the only place that turns a new independent evidence set into
a background proposal and, when the user selected the explicit auto mode, into
an accepted card revision and a published persona version.

Modes (single durable source in PersonaAdminStore):

* off      -> no model call, no proposal, no run;
* propose  -> create one proposal for new evidence and stop; a human accepts;
* auto     -> create the proposal, re-check it, accept it through B13 and
              publish the new card revision for the next turn.

The scheduler does not write the card or the publish store directly: it calls
EvolutionService.propose, PersonaAdminService.accept_proposal and
PersonaPublishService.publish. Idempotency is by trigger_key (evidence ids +
target sections + base revision) plus the evolution request_id. Runs persist in
evolution_runs.json so a restart can retry a failed proposal/publish.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from ..ports import Clock, StorageRoots, WritePermit
from ..invalidation import is_friend_subject
from ..persistence.clock import SystemClock
from ..persistence.evolution_runs import (
    RUN_ACCEPTED,
    RUN_FAILED,
    RUN_PROPOSED,
    RUN_PUBLISHED,
    RUN_REJECTED,
    RUN_SKIPPED,
    RUN_TRIGGERED,
    EvolutionRunStore,
)
from ..persistence.persona_admin import AUTO_MODES
from .evolution import EvolutionService, ProposalRequest, prioritize_evidence
from .persona_admin import PersonaAdminService
from .persona_card import CardSection, PersonaCardService

REASON_OFF = "off"
REASON_NO_EVIDENCE = "no_evidence"
REASON_DUPLICATE = "duplicate"
REASON_DUPLICATE_EVIDENCE = "duplicate_evidence"
REASON_EVIDENCE_ERROR = "evidence_error"
REASON_COOLDOWN = "cooldown"
REASON_BUDGET = "budget"
REASON_SECTION_FROZEN = "section_frozen"
REASON_SECTION_NOT_EDITABLE = "section_not_editable"
REASON_SOURCE_DELETED = "source_deleted"
REASON_NOT_INITIALIZED = "not_initialized"
REASON_MODE_INVALID = "mode_invalid"
REASON_RUN_LIMIT = "run_limit"
REASON_PROPOSAL_MISSING = "proposal_missing"
REASON_PROPOSAL_INVALID = "proposal_invalid"
REASON_PROPOSAL_EXPIRED = "proposal_expired"
REASON_BASE_CONFLICT = "base_revision_conflict"
REASON_ACCEPT_FAILED = "accept_failed"
REASON_PUBLISH_FAILED = "publish_failed"
REASON_ATTEMPTS_EXHAUSTED = "attempts_exhausted"
REASON_DISABLED = "disabled"
REASON_TIMEOUT = "timeout"
REASON_MODEL_ERROR = "model_error"
REASON_INVALID_MODEL_OUTPUT = "invalid_model_output"

#: Proposal failures worth another background attempt (model/transport/race).
_RETRYABLE = frozenset({
    REASON_TIMEOUT, REASON_MODEL_ERROR, REASON_INVALID_MODEL_OUTPUT,
    REASON_BASE_CONFLICT, REASON_PUBLISH_FAILED, REASON_ACCEPT_FAILED,
})
_TERMINAL_EVIDENCE = frozenset({
    "deleted", "withdrawn", "corrected", "invalidated", "expired",
})


@dataclass(frozen=True)
class SchedulerSettings:
    """Bounded background policy; the durable on/off/mode lives in admin."""

    cooldown_minutes: float = 30.0
    max_attempts: int = 3
    daily_budget: int = 8
    proposal_ttl_hours: float = 72.0
    publish_target: str = "next_turn"
    max_evidence: int = 8

    def __post_init__(self) -> None:
        if float(self.cooldown_minutes) < 0:
            raise ValueError("cooldown_minutes must be nonnegative")
        if int(self.max_attempts) < 1:
            raise ValueError("max_attempts must be positive")
        if int(self.daily_budget) < 1:
            raise ValueError("daily_budget must be positive")
        if float(self.proposal_ttl_hours) <= 0:
            raise ValueError("proposal_ttl_hours must be positive")
        if not str(self.publish_target or "").strip():
            raise ValueError("publish_target must be nonempty")
        if int(self.max_evidence) < 1:
            raise ValueError("max_evidence must be positive")


@dataclass(frozen=True)
class SchedulerResult:
    """Result of one evaluate/retry call; run is the persisted row when any."""

    status: str
    run_id: str = ""
    reason: str | None = None
    proposal_id: str = ""
    published_version: str = ""
    run: Mapping[str, Any] | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "reason": self.reason,
            "proposal_id": self.proposal_id,
            "published_version": self.published_version,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_mapping()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_mapping().get(key, default)


def _character(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("character must be nonempty")
    return name


def _text(value: Any, limit: int = 0) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value[:limit] if limit else value


def _row_text(row: Mapping[str, Any]) -> str:
    for key in ("text", "claim", "summary", "statement"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _row_ref(row: Mapping[str, Any]) -> str:
    for key in ("source_ref", "source_key", "id", "growth_id",
                "reflection_id", "fact_id"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    refs = row.get("source_refs")
    if isinstance(refs, (list, tuple)):
        for item in refs:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def _now(clock: Clock) -> datetime:
    value = clock.now()
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _parse_iso(value: Any, now: datetime) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=now.tzinfo)


class EvolutionScheduler:
    """Background trigger/auto-accept loop over existing B10-B13 services."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        runs_store: EvolutionRunStore,
        evolution_service: EvolutionService,
        admin_service: PersonaAdminService,
        publish_service: Any,
        card_service: PersonaCardService,
        evidence_provider: Any,
        barrier: Any = None,
        clock: Clock | None = None,
        permit: WritePermit | None = None,
        settings: SchedulerSettings | None = None,
    ) -> None:
        self._roots = roots
        self._runs = runs_store
        self._evolution = evolution_service
        self._admin = admin_service
        self._publish = publish_service
        self._card = card_service
        self._evidence_provider = evidence_provider
        self._barrier = barrier
        self._clock = (
            clock or getattr(runs_store, "_clock", None) or SystemClock()
        )
        self._permit = permit or getattr(runs_store, "_permit", None)
        self._settings = settings if settings is not None else SchedulerSettings()
        self._async_locks: dict[str, asyncio.Lock] = {}

    @property
    def settings(self) -> SchedulerSettings:
        return self._settings

    # -- reads ----------------------------------------------------------

    def pending(self, character: str) -> list[dict[str, Any]]:
        return self._runs.pending(_character(character))

    def list_runs(
        self, character: str, *, status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._runs.list(_character(character), status=status, limit=limit)

    # -- evaluate -------------------------------------------------------

    async def evaluate(self, character: str) -> SchedulerResult:
        name = _character(character)
        mode = self._mode(name)
        if mode not in AUTO_MODES:
            return SchedulerResult("skipped", reason=REASON_MODE_INVALID)
        if mode == "off":
            return SchedulerResult("skipped", reason=REASON_OFF)
        snapshot = self._card.get(name)
        if snapshot.revision < 1 or not snapshot.sections:
            return SchedulerResult("skipped", reason=REASON_NOT_INITIALIZED)
        selected, section_reason = self._open_sections(snapshot)
        if section_reason is not None:
            return SchedulerResult("skipped", reason=section_reason)

        async with self._async_lock(name):
            mode = self._mode(name)
            if mode == "off":
                return SchedulerResult("skipped", reason=REASON_OFF)
            rows, evidence_reason = await self._collect_evidence(name)
            if evidence_reason == REASON_EVIDENCE_ERROR:
                return self._record_evidence_error(
                    name, snapshot, selected, mode,
                )
            if evidence_reason is not None:
                return SchedulerResult("skipped", reason=evidence_reason)
            snapshot = self._card.get(name)
            selected, section_reason = self._open_sections(snapshot)
            if section_reason is not None:
                return SchedulerResult("skipped", reason=section_reason)

            ids = self._evidence_ids(rows)
            sections = [section.section_id for section in selected]
            consumed = self._find_consumed_evidence(name, ids, sections)
            if consumed is not None:
                return SchedulerResult(
                    "skipped", run_id=str(consumed.get("run_id") or ""),
                    reason=REASON_DUPLICATE_EVIDENCE, run=consumed,
                )
            trigger_key = self._trigger_key(ids, sections, snapshot.revision)
            existing = self._runs.find_trigger(name, trigger_key)
            if existing is not None:
                return SchedulerResult(
                    "skipped", run_id=str(existing.get("run_id") or ""),
                    reason=REASON_DUPLICATE, run=existing,
                )
            latest = self._latest_run(name)
            if latest is not None and self._in_cooldown(latest):
                return SchedulerResult(
                    "skipped", run_id=str(latest.get("run_id") or ""),
                    reason=REASON_COOLDOWN, run=latest,
                )
            if self._budget_exhausted(name):
                return SchedulerResult("skipped", reason=REASON_BUDGET)

            now = _now(self._clock).isoformat()
            run = {
                "run_id": "evr_" + trigger_key[:20],
                "trigger_key": trigger_key,
                "mode": mode,
                "evidence": {
                    "ids": ids,
                    "hashes": [self._hash_text(_row_text(row)) for row in rows],
                },
                "sections": sections,
                "base_revision": int(snapshot.revision),
                "status": RUN_TRIGGERED,
                "reason": "",
                "attempts": 0,
                "cooldown_until": None,
                "created_at": now,
                "updated_at": now,
                "proposal_id": "",
                "published_version": "",
            }
            try:
                stored = self._runs.append(name, run)
            except Exception as exc:  # noqa: BLE001 - bounded store refusal
                code = getattr(exc, "code", "")
                return SchedulerResult(
                    "skipped",
                    reason=REASON_RUN_LIMIT if code == "run_limit" else "store_error",
                )
            if stored.get("run_id") != run["run_id"]:
                # Concurrent append with the same trigger key.
                return SchedulerResult(
                    "skipped", run_id=str(stored.get("run_id") or ""),
                    reason=REASON_DUPLICATE, run=stored,
                )
            return await self._execute(name, stored, mode, snapshot, rows)

    # -- retry ----------------------------------------------------------

    async def retry_pending(self, character: str) -> int:
        name = _character(character)
        retried = 0
        for run in self.pending(name):
            mode = self._mode(name)
            status = str(run.get("status") or "")
            accepted = self._run_has_accepted_proposal(name, run)
            if mode == "off" and not accepted:
                self._finish(name, run, RUN_SKIPPED, REASON_OFF)
                continue
            if self._in_cooldown(run):
                continue
            retried += 1
            snapshot = self._card.get(name)
            if snapshot.revision < 1:
                self._finish(name, run, RUN_SKIPPED, REASON_NOT_INITIALIZED)
                continue
            if accepted:
                await self._publish_accepted(name, run, mode)
                continue
            if status == RUN_PROPOSED and mode == "auto":
                await self._auto(name, run, mode)
                continue
            if status == RUN_PROPOSED and mode == "propose":
                self._finish(name, run, RUN_PROPOSED, None)
                continue
            rows, evidence_reason = await self._collect_evidence(name)
            if evidence_reason == REASON_EVIDENCE_ERROR:
                self._fail(
                    name, run, REASON_EVIDENCE_ERROR, retryable=True,
                )
                continue
            if evidence_reason is not None:
                self._finish(name, run, RUN_SKIPPED, evidence_reason)
                continue
            await self._execute(name, run, mode, snapshot, rows)
        return retried

    # -- execution ------------------------------------------------------

    async def _execute(
        self,
        name: str,
        run: Mapping[str, Any],
        mode: str,
        snapshot: Any,
        rows: Sequence[Mapping[str, Any]],
    ) -> SchedulerResult:
        run_id = str(run.get("run_id") or "")
        trigger_key = str(run.get("trigger_key") or "")
        sections = [str(item) for item in run.get("sections") or []]
        evidence_ids = self._evidence_ids(rows)
        # A retry may have been recorded before evidence was readable; refresh
        # the audit fields now that the real evidence set is known.
        self._runs.update(name, run_id, {
            "evidence": {
                "ids": evidence_ids,
                "hashes": [self._hash_text(_row_text(row)) for row in rows],
            },
            "sections": sections,
            "base_revision": int(snapshot.revision),
        })
        # Include the base revision so a retry after a user edit is a new
        # proposal instead of replaying the stale one through request_id dedupe.
        request_id = f"c03:{trigger_key}:{int(snapshot.revision)}"
        receipt = await self._evolution.propose(
            name,
            ProposalRequest(
                base_revision=int(snapshot.revision),
                focus_sections=tuple(sections),
                reason="background evolution",
                request_id=request_id,
                source_refs=tuple(self._evidence_ids(rows)),
            ),
        )
        raw_proposal = _field(receipt, "proposal", None)
        proposal = raw_proposal if isinstance(raw_proposal, Mapping) else None
        receipt_status = str(_field(receipt, "status", "") or "")
        if receipt_status == "duplicate" and proposal is not None:
            receipt_status = "proposed"
        if self._mode(name) != mode:
            updated = self._finish(name, run, RUN_SKIPPED, REASON_OFF)
            return SchedulerResult(
                "skipped", run_id=run_id, reason=REASON_OFF, run=updated,
            )
        if receipt_status == "proposed" and proposal is not None:
            proposal_id = str(
                proposal.get("proposal_id")
                or _field(receipt, "proposal_id", "")
                or ""
            )
            updated = self._runs.update(name, run_id, {
                "status": RUN_PROPOSED,
                "proposal_id": proposal_id,
                "reason": "",
            })
            if mode != "auto":
                return SchedulerResult(
                    "proposed", run_id=run_id, proposal_id=proposal_id,
                    run=updated,
                )
            return await self._auto(name, updated or run, mode)
        reason = str(_field(receipt, "reason", None) or receipt_status or "unknown")
        if reason in _RETRYABLE or receipt_status == "disabled":
            retryable = reason in _RETRYABLE
            updated = self._fail(name, run, reason, retryable=retryable)
            return SchedulerResult(
                "failed" if retryable else "skipped",
                run_id=run_id, reason=reason, run=updated,
            )
        updated = self._finish(name, run, RUN_SKIPPED, reason)
        return SchedulerResult(
            "skipped", run_id=run_id, reason=reason, run=updated,
        )

    async def _auto(
        self, name: str, run: Mapping[str, Any], mode: str,
    ) -> SchedulerResult:
        run_id = str(run.get("run_id") or "")
        if mode != "auto" or self._mode(name) != "auto":
            self._finish(name, run, RUN_SKIPPED, REASON_OFF)
            return SchedulerResult("skipped", run_id=run_id, reason=REASON_OFF)
        proposal_id = str(run.get("proposal_id") or "")
        proposal = self._evolution.get(name, proposal_id) if proposal_id else None
        if not isinstance(proposal, Mapping):
            updated = self._fail(name, run, REASON_PROPOSAL_MISSING, retryable=True)
            return SchedulerResult(
                "failed", run_id=run_id, reason=REASON_PROPOSAL_MISSING,
                run=updated,
            )
        if proposal.get("status") == "accepted":
            return await self._publish_accepted(name, run, mode)
        if proposal.get("status") != "proposed":
            updated = self._finish(
                name, run, RUN_REJECTED,
                str(proposal.get("status") or "proposal_not_proposed"),
            )
            return SchedulerResult(
                "rejected", run_id=run_id, proposal_id=proposal_id,
                reason=str(proposal.get("status") or "proposal_not_proposed"),
                run=updated,
            )
        validation = proposal.get("validation")
        if not isinstance(validation, Mapping) or not validation.get("ok"):
            updated = self._finish(name, run, RUN_REJECTED, REASON_PROPOSAL_INVALID)
            return SchedulerResult(
                "rejected", run_id=run_id, proposal_id=proposal_id,
                reason=REASON_PROPOSAL_INVALID, run=updated,
            )
        if self._proposal_expired(proposal):
            updated = self._finish(name, run, RUN_SKIPPED, REASON_PROPOSAL_EXPIRED)
            return SchedulerResult(
                "skipped", run_id=run_id, proposal_id=proposal_id,
                reason=REASON_PROPOSAL_EXPIRED, run=updated,
            )
        snapshot = self._card.get(name)
        if int(snapshot.revision) != int(proposal.get("base_revision") or 0):
            updated = self._fail(name, run, REASON_BASE_CONFLICT, retryable=True)
            return SchedulerResult(
                "failed", run_id=run_id, proposal_id=proposal_id,
                reason=REASON_BASE_CONFLICT, run=updated,
            )
        section_reason = self._sections_still_open(snapshot, proposal)
        if section_reason is not None:
            updated = self._finish(name, run, RUN_SKIPPED, section_reason)
            return SchedulerResult(
                "skipped", run_id=run_id, proposal_id=proposal_id,
                reason=section_reason, run=updated,
            )
        decision = self._admin.accept_proposal(
            name, proposal_id, actor="evolution", reason="auto evolution",
        )
        decision_status = str(_field(decision, "status", "") or "")
        decision_reason = _field(decision, "reason", None)
        if decision_status != "accepted":
            retryable = bool(_field(decision, "conflict", False)) or decision_status == "conflict"
            updated = self._fail(
                name, run,
                str(decision_reason or REASON_ACCEPT_FAILED),
                retryable=retryable,
            )
            return SchedulerResult(
                "failed" if retryable else "rejected",
                run_id=run_id, proposal_id=proposal_id,
                reason=str(decision_reason or REASON_ACCEPT_FAILED), run=updated,
            )
        updated = self._runs.update(name, run_id, {
            "status": RUN_ACCEPTED,
            "proposal_id": proposal_id,
            "reason": "",
        })
        return await self._publish_accepted(name, updated or run, mode)

    async def _publish_accepted(
        self, name: str, run: Mapping[str, Any], mode: str,
    ) -> SchedulerResult:
        run_id = str(run.get("run_id") or "")
        proposal_id = str(run.get("proposal_id") or "")
        proposal = self._evolution.get(name, proposal_id) if proposal_id else None
        if not isinstance(proposal, Mapping) or proposal.get("status") != "accepted":
            updated = self._fail(name, run, REASON_PROPOSAL_MISSING, retryable=True)
            return SchedulerResult(
                "failed", run_id=run_id, proposal_id=proposal_id,
                reason=REASON_PROPOSAL_MISSING, run=updated,
            )
        accepted_revision = int(proposal.get("accepted_revision") or 0)
        receipt = self._publish.publish(
            name,
            target=self._settings.publish_target,
            revision=accepted_revision or None,
        )
        status = str(_field(receipt, "status", "") or "")
        if status in ("published", "duplicate"):
            version = str(_field(receipt, "version", "") or "")
            updated = self._runs.update(name, run_id, {
                "status": RUN_PUBLISHED,
                "published_version": version,
                "reason": "",
                "cooldown_until": None,
            })
            self._runs.clear_pending(name, run_id)
            return SchedulerResult(
                "published", run_id=run_id, proposal_id=proposal_id,
                published_version=version, run=updated,
            )
        reason = str(_field(receipt, "reason", "") or REASON_PUBLISH_FAILED)
        updated = self._fail(name, run, reason, retryable=True)
        return SchedulerResult(
            "failed", run_id=run_id, proposal_id=proposal_id,
            reason=reason, run=updated,
        )

    # -- run state helpers ----------------------------------------------

    def _fail(
        self, name: str, run: Mapping[str, Any], reason: str,
        *, retryable: bool,
    ) -> dict[str, Any] | None:
        attempts = int(run.get("attempts") or 0) + 1
        cooldown = _now(self._clock) + timedelta(
            minutes=float(self._settings.cooldown_minutes),
        )
        status = RUN_FAILED if retryable else RUN_REJECTED
        if retryable and attempts >= int(self._settings.max_attempts):
            status = RUN_REJECTED
            reason = REASON_ATTEMPTS_EXHAUSTED
        updated = self._runs.update(name, str(run.get("run_id") or ""), {
            "status": status,
            "reason": reason,
            "attempts": attempts,
            "cooldown_until": cooldown.isoformat() if status == RUN_FAILED else None,
        })
        if status == RUN_FAILED:
            self._runs.mark_pending(name, str(run.get("run_id") or ""))
        else:
            self._runs.clear_pending(name, str(run.get("run_id") or ""))
        return updated

    def _finish(
        self, name: str, run: Mapping[str, Any], status: str,
        reason: str | None,
    ) -> dict[str, Any] | None:
        updated = self._runs.update(name, str(run.get("run_id") or ""), {
            "status": status,
            "reason": reason or "",
            "cooldown_until": None,
        })
        self._runs.clear_pending(name, str(run.get("run_id") or ""))
        return updated

    # -- evidence -------------------------------------------------------

    async def _collect_evidence(
        self, name: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        provider = self._evidence_provider
        if provider is None:
            return [], REASON_NO_EVIDENCE
        try:
            raw = await self._call_provider(provider, name)
        except Exception:  # noqa: BLE001 - surfaced as retryable evidence_error
            return [], REASON_EVIDENCE_ERROR
        if isinstance(raw, Mapping):
            if isinstance(raw.get("rows"), (list, tuple)):
                raw = raw.get("rows")
            elif isinstance(raw.get("items"), (list, tuple)):
                raw = raw.get("items")
            else:
                raw = [raw]
        if not isinstance(raw, (list, tuple)):
            raw = []
        rows: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            row = copy.deepcopy(dict(item))
            if is_friend_subject(row.get("subject")):
                continue
            if row.get("absorbed_into") or row.get("absorbed_at"):
                continue
            if _row_ref(row):
                rows.append(row)
        rows = prioritize_evidence(rows)[: int(self._settings.max_evidence)]
        for row in rows:
            status = str(row.get("status") or "").strip().lower()
            if (row.get("deleted") or row.get("invalidated_at")
                    or row.get("corrected")
                    or status in _TERMINAL_EVIDENCE):
                return [], REASON_SOURCE_DELETED
            if self._row_blocked(name, row):
                return [], REASON_SOURCE_DELETED
        if not rows:
            return [], REASON_NO_EVIDENCE
        return rows, None

    async def _call_provider(self, provider: Any, name: str) -> Any:
        fn = getattr(provider, "collect", provider)
        result: Any
        try:
            result = fn(name)
        except TypeError:
            try:
                result = fn(name, ProposalRequest(base_revision=0))
            except TypeError:
                result = fn(name, [])
        if inspect.isawaitable(result):
            result = await result
        return result

    def _row_blocked(self, name: str, row: Mapping[str, Any]) -> bool:
        barrier = self._barrier
        if barrier is None:
            return False
        try:
            if barrier.blocks_row(name, row):
                return True
        except Exception:  # noqa: BLE001
            pass
        ref = _row_ref(row)
        if ref:
            for target_type in ("fact", "reflection", "growth", "persona"):
                try:
                    if barrier.blocks_target(name, target_type, ref):
                        return True
                except Exception:  # noqa: BLE001
                    pass
        text = _row_text(row)
        if text:
            try:
                if barrier.blocks_text(name, text):
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    @staticmethod
    def _evidence_ids(rows: Sequence[Mapping[str, Any]]) -> list[str]:
        out: list[str] = []
        for row in rows:
            ref = _row_ref(row)
            if ref and ref not in out:
                out.append(ref)
        return out

    @staticmethod
    def _hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def _trigger_key(ids: Sequence[str], sections: Sequence[str], base: int) -> str:
        payload = json.dumps({
            "ids": sorted(ids),
            "sections": sorted(sections),
            "base_revision": int(base),
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # -- card/cooldown/budget ------------------------------------------

    def _open_sections(
        self, snapshot: Any,
    ) -> tuple[list[CardSection], str | None]:
        open_sections = [
            section for section in snapshot.sections
            if section.editable_by.get("evolution") and not section.frozen
        ]
        if open_sections:
            return open_sections, None
        if any(section.frozen for section in snapshot.sections):
            return [], REASON_SECTION_FROZEN
        return [], REASON_SECTION_NOT_EDITABLE

    def _sections_still_open(
        self, snapshot: Any, proposal: Mapping[str, Any],
    ) -> str | None:
        by_id = {section.section_id: section for section in snapshot.sections}
        for operation in proposal.get("operations") or []:
            if not isinstance(operation, Mapping):
                return "invalid_operation"
            section_id = str(operation.get("section_id") or "")
            op = str(operation.get("op") or "")
            section = by_id.get(section_id)
            if op == "add":
                if section is not None:
                    return "duplicate_section"
                continue
            if section is None:
                return REASON_SECTION_NOT_EDITABLE
            if section.frozen:
                return REASON_SECTION_FROZEN
            if not section.editable_by.get("evolution"):
                return REASON_SECTION_NOT_EDITABLE
        return None

    def _mode(self, name: str) -> str:
        try:
            mode = str(self._admin.get_mode(name) or "")
        except Exception:  # noqa: BLE001 - unknown/broken mode fails closed
            return "invalid"
        return mode if mode in AUTO_MODES else "invalid"

    def _record_evidence_error(
        self, name: str, snapshot: Any, selected: Sequence[CardSection],
        mode: str,
    ) -> SchedulerResult:
        sections = [section.section_id for section in selected]
        now = _now(self._clock).isoformat()
        trigger_key = hashlib.sha256(
            f"evidence_error|{sorted(sections)}|{int(snapshot.revision)}"
            .encode("utf-8"),
        ).hexdigest()
        run = {
            "run_id": "evr_" + trigger_key[:20],
            "trigger_key": trigger_key,
            "mode": mode,
            "evidence": {"ids": [], "hashes": []},
            "sections": sections,
            "base_revision": int(snapshot.revision),
            "status": RUN_TRIGGERED,
            "reason": "",
            "attempts": 0,
            "cooldown_until": None,
            "created_at": now,
            "updated_at": now,
            "proposal_id": "",
            "published_version": "",
        }
        try:
            stored = self._runs.append(name, run)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", "")
            return SchedulerResult(
                "skipped",
                reason=REASON_RUN_LIMIT if code == "run_limit" else "store_error",
            )
        updated = self._fail(
            name, stored, REASON_EVIDENCE_ERROR, retryable=True,
        )
        return SchedulerResult(
            "failed", run_id=str(stored.get("run_id") or ""),
            reason=REASON_EVIDENCE_ERROR, run=updated,
        )

    def _run_has_accepted_proposal(
        self, name: str, run: Mapping[str, Any],
    ) -> bool:
        if str(run.get("status") or "") == RUN_ACCEPTED:
            return True
        proposal_id = str(run.get("proposal_id") or "")
        if not proposal_id:
            return False
        proposal = self._evolution.get(name, proposal_id)
        return (
            isinstance(proposal, Mapping)
            and str(proposal.get("status") or "") == "accepted"
        )

    def _find_consumed_evidence(
        self, name: str, ids: Sequence[str], sections: Sequence[str],
    ) -> dict[str, Any] | None:
        wanted_ids = sorted(str(item) for item in ids)
        wanted_sections = sorted(str(item) for item in sections)
        for run in self._runs.list(name):
            status = str(run.get("status") or "")
            if status not in (RUN_ACCEPTED, RUN_PUBLISHED) and not (
                self._run_has_accepted_proposal(name, run)
            ):
                continue
            evidence = run.get("evidence")
            evidence = evidence if isinstance(evidence, Mapping) else {}
            run_ids = sorted(
                str(item) for item in evidence.get("ids") or []
            )
            run_sections = sorted(
                str(item) for item in run.get("sections") or []
            )
            if run_ids == wanted_ids and run_sections == wanted_sections:
                return run
        return None

    def _latest_run(self, name: str) -> dict[str, Any] | None:
        runs = self._runs.list(name, limit=1)
        return runs[0] if runs else None

    def _in_cooldown(self, run: Mapping[str, Any]) -> bool:
        now = _now(self._clock)
        cooldown_until = _parse_iso(run.get("cooldown_until"), now)
        if cooldown_until is not None:
            return cooldown_until > now
        updated = _parse_iso(run.get("updated_at"), now)
        if updated is None:
            return False
        return now - updated < timedelta(minutes=float(self._settings.cooldown_minutes))

    def _budget_exhausted(self, name: str) -> bool:
        today = _now(self._clock).date()
        used = 0
        for run in self._runs.list(name):
            created = _parse_iso(run.get("created_at"), _now(self._clock))
            if created is not None and created.date() == today:
                used += 1
        return used >= int(self._settings.daily_budget)

    def _proposal_expired(self, proposal: Mapping[str, Any]) -> bool:
        now = _now(self._clock)
        created = _parse_iso(proposal.get("created_at"), now)
        if created is None:
            return False
        return now - created > timedelta(hours=float(self._settings.proposal_ttl_hours))

    def _async_lock(self, character: str) -> asyncio.Lock:
        lock = self._async_locks.get(character)
        if lock is None:
            lock = asyncio.Lock()
            self._async_locks[character] = lock
        return lock


__all__ = [
    "EvolutionScheduler",
    "SchedulerResult",
    "SchedulerSettings",
]
