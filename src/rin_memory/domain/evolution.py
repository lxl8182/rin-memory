"""B12 evidence-based persona revision proposals (model proposes, card stays).

Flow implemented here:

1. refuse while disabled (default) or while the card is uninitialized;
2. dedupe by request_id (same request returns the same proposal);
3. collect evidence rows through the injected evidence_provider and reject
   missing/deleted/corrected sources;
4. select only open card sections (editable_by.evolution and not frozen);
5. ask ModelInvoker for one strict-JSON proposal (purpose
   persona_evolution_proposal, explicit tier/token/timeout budget);
6. deterministically validate operations, section openness, text length,
   base_revision, source existence/barrier and config/secret leakage;
7. persist a proposal row (status proposed) and a journal snapshot.

The service never calls PersonaCardService.revise and never writes the card.
Failures carry a stable reason and never return an empty success.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping, Sequence

from ..ports import Clock, ModelInvoker, StorageRoots, WritePermit
from ..persistence.evolution import (
    STATUS_PROPOSED,
    STATUS_WITHDRAWN,
    EvolutionStore,
)
from .growth_evidence import (
    REASON_FACT_NOT_ELIGIBLE,
    REASON_NOT_MATURE,
    eligibility_of_row,
    evidence_dto,
)
from .persona_card import (
    MAX_SECTION_TEXT,
    CardSection,
    InvalidSectionId,
    PersonaCardService,
    validate_section_id,
)
from .reflection import MEMORY_KINDS, PROMOTION_RULES
from .prompt_voice import FIRST_PERSON_VOICE

EVT_PERSONA_EVOLUTION_PROPOSED = "core.persona_evolution_proposed"
PURPOSE_PERSONA_EVOLUTION_PROPOSAL = "persona_evolution_proposal"

REASON_DISABLED = "disabled"
REASON_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
#: M4 (design 9.3): a raw fact explains a proposal but never qualifies for one,
#: and an immature understanding has to wait for the growth gate.
REASON_FACT_NOT_ELIGIBLE = REASON_FACT_NOT_ELIGIBLE
REASON_NOT_MATURE = REASON_NOT_MATURE
REASON_SOURCE_DELETED = "source_deleted"
REASON_SECTION_FROZEN = "section_frozen"
REASON_SECTION_NOT_EDITABLE = "section_not_editable"
REASON_BASE_REVISION_CONFLICT = "base_revision_conflict"
REASON_INVALID_MODEL_OUTPUT = "invalid_model_output"
REASON_VALIDATION_FAILED = "validation_failed"
REASON_TIMEOUT = "timeout"
REASON_MODEL_ERROR = "model_error"
REASON_DUPLICATE_REQUEST = "duplicate_request"
REASON_NO_CHANGE = "no_change"
REASON_PROPOSAL_LIMIT = "proposal_limit"
REASON_NOT_INITIALIZED = "not_initialized"

_OPERATIONS = frozenset({"add", "replace", "delete"})
_TERMINAL_EVIDENCE_STATUSES = frozenset({
    "deleted", "withdrawn", "corrected", "invalidated", "expired",
})
#: Config/credential-looking content must never become a persona paragraph.
_FORBIDDEN_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|secret|password|passwd|credential|"
    r"access[_-]?token|refresh[_-]?token|base[_-]?url|voice[_-]?id|"
    r"model[_-]?name|密钥|凭据|音色|模型路径)",
)
_JSON_FENCE_RE = re.compile(
    r"^\x60{3}(?:json)?\s*|\s*\x60{3}$", re.IGNORECASE,
)


@dataclass(frozen=True)
class EvolutionSettings:
    """B12 policy; defaults are the design §9.1 initial operating values."""

    enabled: bool = False
    tier: str = "summary"
    max_operations: int = 4
    max_section_chars: int = 2000
    max_evidence: int = 8
    #: Independent experiences a behavior strategy needs before it may drive an
    #: automatic card change (mirrors the reflection growth gate).
    evidence_min_support: int = 2
    proposal_ttl_hours: float = 72.0
    timeout_s: float = 60.0
    max_output_tokens: int = 1200

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a bool")
        tier = str(self.tier or "").strip()
        if not tier:
            raise ValueError("tier must be nonempty")
        if not 1 <= int(self.max_operations) <= 16:
            raise ValueError("max_operations must be between 1 and 16")
        if not 1 <= int(self.max_section_chars) <= MAX_SECTION_TEXT:
            raise ValueError(
                f"max_section_chars must be between 1 and {MAX_SECTION_TEXT}",
            )
        if not 1 <= int(self.max_evidence) <= 32:
            raise ValueError("max_evidence must be between 1 and 32")
        if not 1 <= int(self.evidence_min_support) <= 8:
            raise ValueError("evidence_min_support must be between 1 and 8")
        if float(self.proposal_ttl_hours) <= 0:
            raise ValueError("proposal_ttl_hours must be positive")
        if float(self.timeout_s) <= 0:
            raise ValueError("timeout_s must be positive")
        if int(self.max_output_tokens) <= 0:
            raise ValueError("max_output_tokens must be positive")


@dataclass(frozen=True)
class ProposalRequest:
    """One proposal request; base_revision is the card revision the caller saw."""

    base_revision: int
    focus_sections: Sequence[str] = ()
    reason: str = ""
    request_id: str = ""
    source_refs: Sequence[str] = ()

    def __post_init__(self) -> None:
        if isinstance(self.base_revision, bool) or not isinstance(
            self.base_revision, int,
        ) or self.base_revision < 0:
            raise ValueError("base_revision must be a nonnegative int")
        focus = tuple(
            str(item).strip() for item in self.focus_sections or ()
            if str(item).strip()
        )
        refs = tuple(
            str(item).strip() for item in self.source_refs or ()
            if str(item).strip()
        )
        object.__setattr__(self, "focus_sections", focus)
        object.__setattr__(self, "source_refs", refs)
        object.__setattr__(self, "reason", str(self.reason or "")[:400])
        object.__setattr__(self, "request_id", str(self.request_id or "")[:200])


@dataclass(frozen=True)
class ProposalReceipt:
    """Result of one proposal attempt; proposal is the persisted row when any."""

    status: str
    proposal_id: str = ""
    validation: Mapping[str, Any] | None = None
    reason: str | None = None
    proposal: Mapping[str, Any] | None = None


def _character(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("character must be nonempty")
    return name


def _now_iso(clock: Clock) -> str:
    return clock.now().isoformat()


def _check(name: str, ok: bool, reason: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"name": name, "ok": bool(ok)}
    if reason:
        row["reason"] = reason
    return row


def _validation(
    checks: Sequence[Mapping[str, Any]], reason: str | None,
) -> dict[str, Any]:
    return {
        "checks": [dict(item) for item in checks],
        "ok": reason is None,
        "reason": reason or "",
    }


def _rejected(
    reason: str, *, validation: Mapping[str, Any] | None = None,
) -> ProposalReceipt:
    return ProposalReceipt(status="rejected", reason=reason, validation=validation)


def _normalize_request(request: Any) -> ProposalRequest:
    if isinstance(request, ProposalRequest):
        return request
    if isinstance(request, Mapping):
        return ProposalRequest(
            base_revision=int(request.get("base_revision") or 0),
            focus_sections=request.get("focus_sections") or (),
            reason=request.get("reason") or "",
            request_id=request.get("request_id") or "",
            source_refs=request.get("source_refs") or (),
        )
    raise ValueError("request must be a ProposalRequest or mapping")


def _is_fact(row: Mapping[str, Any]) -> bool:
    """Whether an evidence row is a raw fact (never card-change eligible)."""
    kind = str(row.get("kind") or row.get("evidence_type") or "").strip().lower()
    if kind:
        return kind == "fact"
    return bool(row.get("fact_id")) or (
        "source" not in row and "memory_kind" not in row
        and str(row.get("source") or "").startswith(("user_", "ai_"))
    )


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


def _row_subject(row: Mapping[str, Any]) -> str:
    value = row.get("subject")
    return value.strip() if isinstance(value, str) else ""


class EvolutionService:
    """Proposal-only B12 service; the card service remains the only card writer."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: EvolutionStore,
        card_service: PersonaCardService,
        evidence_provider: Any,
        barrier: Any,
        journal: Any,
        clock: Clock,
        permit: WritePermit,
        model: ModelInvoker | None = None,
        settings: EvolutionSettings | None = None,
    ) -> None:
        self._roots = roots
        self._store = store
        self._card = card_service
        self._evidence_provider = evidence_provider
        self._barrier = barrier
        self._journal = journal
        self._clock = clock
        self._permit = permit
        self._model = model
        self._settings = settings if settings is not None else EvolutionSettings()
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._async_locks: dict[str, asyncio.Lock] = {}
        self._ensure_event_vocabulary()
        if journal is not None:
            try:
                journal.register_handler(
                    EVT_PERSONA_EVOLUTION_PROPOSED, self._replay_proposal,
                )
            except Exception:  # noqa: BLE001 - an optional journal may be read-only
                pass

    # -- wiring ---------------------------------------------------------

    @property
    def settings(self) -> EvolutionSettings:
        return self._settings

    def _lock(self, character: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(character)
            if lock is None:
                lock = threading.RLock()
                self._locks[character] = lock
            return lock

    def _async_lock(self, character: str) -> asyncio.Lock:
        lock = self._async_locks.get(character)
        if lock is None:
            lock = asyncio.Lock()
            self._async_locks[character] = lock
        return lock

    def _ensure_event_vocabulary(self) -> None:
        """Allow the new event on journals built before B12 (same as B10)."""
        journal = self._journal
        if journal is None:
            return
        types = getattr(journal, "_event_types", None)
        if isinstance(types, frozenset) and EVT_PERSONA_EVOLUTION_PROPOSED not in types:
            try:
                journal._event_types = types | {EVT_PERSONA_EVOLUTION_PROPOSED}
            except Exception:  # noqa: BLE001 - journal stays read-only for us
                pass

    # -- reads ----------------------------------------------------------

    def get(self, character: str, proposal_id: str) -> dict[str, Any] | None:
        return self._store.get(_character(character), proposal_id)

    def list(
        self, character: str, *, status: str | None = None, limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._store.list(_character(character), status=status, limit=limit)

    # -- propose --------------------------------------------------------

    async def propose(self, character: str, request: Any) -> ProposalReceipt:
        name = _character(character)
        req = _normalize_request(request)
        settings = self._settings
        if not settings.enabled:
            return ProposalReceipt(status="disabled", reason=REASON_DISABLED)

        if req.request_id:
            existing = self._store.find_by_request_id(name, req.request_id)
            if existing is not None:
                return self._duplicate_receipt(existing)

        snapshot = self._card.get(name)
        if snapshot.revision < 1 or not snapshot.sections:
            return _rejected(
                REASON_NOT_INITIALIZED,
                validation=_validation(
                    [_check("card_initialized", False, REASON_NOT_INITIALIZED)],
                    REASON_NOT_INITIALIZED,
                ),
            )
        if req.base_revision != snapshot.revision:
            return _rejected(
                REASON_BASE_REVISION_CONFLICT,
                validation=_validation(
                    [_check(
                        "base_revision", False, REASON_BASE_REVISION_CONFLICT,
                    )],
                    REASON_BASE_REVISION_CONFLICT,
                ),
            )

        async with self._async_lock(name):
            if req.request_id:
                existing = self._store.find_by_request_id(name, req.request_id)
                if existing is not None:
                    return self._duplicate_receipt(existing)

            rows, evidence_reason, evidence_validation = (
                await self._collect_evidence(name, req, snapshot)
            )
            if evidence_reason is not None:
                return _rejected(evidence_reason, validation=evidence_validation)

            selected, section_reason, section_validation = self._select_sections(
                snapshot, req,
            )
            if section_reason is not None:
                return _rejected(section_reason, validation=section_validation)

            barrier_snapshot = self._barrier_snapshot(name)
            started = self._clock.monotonic()
            try:
                raw = await self._invoke_model(
                    name, req, selected, rows, snapshot,
                )
            except (asyncio.TimeoutError, TimeoutError):
                return _rejected(
                    REASON_TIMEOUT,
                    validation=_validation(
                        [_check("model", False, REASON_TIMEOUT)], REASON_TIMEOUT,
                    ),
                )
            except Exception:  # noqa: BLE001 - provider failures are data, not crashes
                return _rejected(
                    REASON_MODEL_ERROR,
                    validation=_validation(
                        [_check("model", False, REASON_MODEL_ERROR)],
                        REASON_MODEL_ERROR,
                    ),
                )
            elapsed_ms = int(max(0.0, self._clock.monotonic() - started) * 1000)

            current = self._card.get(name)
            if current.revision != snapshot.revision:
                return _rejected(
                    REASON_BASE_REVISION_CONFLICT,
                    validation=_validation(
                        [_check(
                            "base_revision", False,
                            REASON_BASE_REVISION_CONFLICT,
                        )],
                        REASON_BASE_REVISION_CONFLICT,
                    ),
                )
            if self._barrier_stale(name, barrier_snapshot):
                return _rejected(
                    REASON_SOURCE_DELETED,
                    validation=_validation(
                        [_check("barrier", False, REASON_SOURCE_DELETED)],
                        REASON_SOURCE_DELETED,
                    ),
                )
            if self._evidence_blocked(name, rows):
                return _rejected(
                    REASON_SOURCE_DELETED,
                    validation=_validation(
                        [_check("evidence", False, REASON_SOURCE_DELETED)],
                        REASON_SOURCE_DELETED,
                    ),
                )

            parsed, parse_reason = self._parse_model_output(raw)
            if parse_reason is not None:
                return _rejected(
                    parse_reason,
                    validation=_validation(
                        [_check("model_output", False, parse_reason)], parse_reason,
                    ),
                )
            if not parsed["operations"]:
                return _rejected(
                    REASON_NO_CHANGE,
                    validation=_validation(
                        [_check("operations", False, REASON_NO_CHANGE)],
                        REASON_NO_CHANGE,
                    ),
                )
            operations, validation_reason, validation = self._validate_operations(
                name, req, selected, rows, snapshot, parsed,
            )
            if validation_reason is not None:
                return _rejected(validation_reason, validation=validation)

            proposal = self._build_proposal(
                name, req, snapshot, operations, rows, parsed,
                validation=validation, elapsed_ms=elapsed_ms,
                raw_output=raw if isinstance(raw, str) else "",
            )
            try:
                stored = self._record_proposal(name, proposal)
            except Exception as exc:  # noqa: BLE001 - store/permit failures are receipts
                code = getattr(exc, "code", "")
                reason = (
                    REASON_PROPOSAL_LIMIT
                    if code == "proposal_limit" else REASON_VALIDATION_FAILED
                )
                return _rejected(
                    reason,
                    validation=_validation(
                        [_check("store", False, reason)], reason,
                    ),
                )
            return ProposalReceipt(
                status="proposed",
                proposal_id=str(stored.get("proposal_id") or ""),
                validation=validation,
                reason=None,
                proposal=stored,
            )

    # -- evidence and sections ------------------------------------------

    async def _collect_evidence(
        self, name: str, req: ProposalRequest, snapshot: Any,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
        provider = self._evidence_provider
        if provider is None:
            return [], REASON_INSUFFICIENT_EVIDENCE, _validation(
                [_check("evidence", False, REASON_INSUFFICIENT_EVIDENCE)],
                REASON_INSUFFICIENT_EVIDENCE,
            )
        try:
            raw = await self._call_provider(provider, name, req)
        except Exception:  # noqa: BLE001 - evidence provider failure is a receipt
            return [], REASON_INSUFFICIENT_EVIDENCE, _validation(
                [_check("evidence", False, REASON_INSUFFICIENT_EVIDENCE)],
                REASON_INSUFFICIENT_EVIDENCE,
            )
        if isinstance(raw, Mapping):
            if isinstance(raw.get("rows"), (list, tuple)):
                raw = raw.get("rows")
            elif isinstance(raw.get("items"), (list, tuple)):
                raw = raw.get("items")
            else:
                raw = [raw]
        if not isinstance(raw, (list, tuple)):
            raw = []
        rows = [copy.deepcopy(dict(row)) for row in raw if isinstance(row, Mapping)]
        rows = rows[: self._settings.max_evidence]
        if not rows:
            return [], REASON_INSUFFICIENT_EVIDENCE, _validation(
                [_check("evidence", False, REASON_INSUFFICIENT_EVIDENCE)],
                REASON_INSUFFICIENT_EVIDENCE,
            )
        for row in rows:
            status = str(row.get("status") or "").strip().lower()
            if (row.get("deleted") or row.get("invalidated_at")
                    or row.get("corrected")
                    or status in _TERMINAL_EVIDENCE_STATUSES):
                return [], REASON_SOURCE_DELETED, _validation(
                    [_check("evidence", False, REASON_SOURCE_DELETED)],
                    REASON_SOURCE_DELETED,
                )
        if self._evidence_blocked(name, rows):
            return [], REASON_SOURCE_DELETED, _validation(
                [_check("evidence", False, REASON_SOURCE_DELETED)],
                REASON_SOURCE_DELETED,
            )
        rows, reason, eligibility_validation = self._apply_eligibility(rows)
        if reason is not None:
            return [], reason, eligibility_validation
        refs = {_row_ref(row) for row in rows if _row_ref(row)}
        missing = [ref for ref in req.source_refs if ref not in refs]
        if missing:
            return [], REASON_SOURCE_DELETED, _validation(
                [_check("evidence", False, REASON_SOURCE_DELETED)],
                REASON_SOURCE_DELETED,
            )
        return rows, None, _validation([_check("evidence", True)], None)

    def _apply_eligibility(
        self, rows: Sequence[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
        """Keep only evidence that may drive a card change (design 9.3 / G15).

        A raw fact is an explanation source: the model may read it, but it can
        never by itself justify a persona edit, so a pool made only of facts is
        rejected as `fact_not_eligible` instead of being fed to the model as if
        it were proof. An understanding or strategy must be mature (it has to
        have reached confirmed/promoted through the growth gate); otherwise the
        batch is `not_mature`. Eligible rows come first, and ineligible ones stay
        in the prompt as explanation with `eligible=false` on their DTO.
        """
        min_support = int(self._settings.evidence_min_support)
        eligible: list[dict[str, Any]] = []
        explanation: list[dict[str, Any]] = []
        for row in rows:
            provided = row.get("evidence")
            # The provider already built the DTO from the *stored* row (support
            # basis included). Re-deriving it from the flattened transport row
            # would lose that basis and mistake a mature row for an empty one.
            if isinstance(provided, Mapping) and "eligible" in provided:
                dto = dict(provided)
                ok = bool(dto.get("eligible"))
                reason = str(dto.get("eligibility") or "")
            else:
                ok, reason = eligibility_of_row(
                    row, promotion_rules=PROMOTION_RULES,
                    min_support=min_support,
                )
                dto = evidence_dto(
                    row,
                    kind="fact" if _is_fact(row) else "reflection",
                    promotion_rules=PROMOTION_RULES,
                    min_support=min_support,
                )
            enriched = {
                **row,
                "eligible": ok,
                "eligibility": reason,
                "maturity": dto["maturity"],
                "evidence": dto,
            }
            if ok:
                eligible.append(enriched)
            else:
                explanation.append(enriched)
        if eligible:
            checks = [
                _check("evidence", True),
                _check("eligibility", True),
            ]
            return (
                (eligible + explanation)[: self._settings.max_evidence],
                None,
                _validation(checks, None),
            )
        # The reason names the real blocker: a pool made only of raw facts is
        # "facts are not eligible", a pool of immature understandings is
        # "nothing is mature yet". A mixed pool has no mature candidate either.
        reason = (
            REASON_FACT_NOT_ELIGIBLE
            if rows and all(_is_fact(row) for row in rows)
            else REASON_NOT_MATURE
        )
        return [], reason, _validation(
            [_check("eligibility", False, reason)], reason,
        )

    async def _call_provider(
        self, provider: Any, name: str, req: ProposalRequest,
    ) -> Any:
        fn = getattr(provider, "collect", provider)
        result: Any
        try:
            result = fn(name, req)
        except TypeError:
            try:
                result = fn(name, list(req.source_refs))
            except TypeError:
                result = fn(name)
        if inspect.isawaitable(result):
            result = await result
        return result

    def _select_sections(
        self, snapshot: Any, req: ProposalRequest,
    ) -> tuple[list[CardSection], str | None, dict[str, Any]]:
        by_id = {section.section_id: section for section in snapshot.sections}
        selected: list[CardSection] = []
        if req.focus_sections:
            for section_id in req.focus_sections:
                section = by_id.get(section_id)
                if section is None:
                    reason = REASON_VALIDATION_FAILED
                    return [], reason, _validation(
                        [_check("section", False, "unknown_section")], reason,
                    )
                if section.frozen:
                    return [], REASON_SECTION_FROZEN, _validation(
                        [_check("section", False, REASON_SECTION_FROZEN)],
                        REASON_SECTION_FROZEN,
                    )
                if not section.editable_by.get("evolution"):
                    return [], REASON_SECTION_NOT_EDITABLE, _validation(
                        [_check("section", False, REASON_SECTION_NOT_EDITABLE)],
                        REASON_SECTION_NOT_EDITABLE,
                    )
                selected.append(section)
        else:
            selected = [
                section for section in snapshot.sections
                if section.editable_by.get("evolution") and not section.frozen
            ]
        if not selected:
            frozen = any(section.frozen for section in snapshot.sections)
            reason = (
                REASON_SECTION_FROZEN if frozen else REASON_SECTION_NOT_EDITABLE
            )
            return [], reason, _validation(
                [_check("section", False, reason)], reason,
            )
        return selected, None, _validation([_check("section", True)], None)

    # -- model ----------------------------------------------------------

    async def _invoke_model(
        self,
        name: str,
        req: ProposalRequest,
        selected: Sequence[CardSection],
        rows: Sequence[Mapping[str, Any]],
        snapshot: Any,
    ) -> str:
        if self._model is None:
            raise RuntimeError("no ModelInvoker configured")
        messages = self._build_messages(name, req, selected, rows, snapshot)
        result = self._model.invoke(
            self._settings.tier,
            messages,
            max_output_tokens=self._settings.max_output_tokens,
            timeout_s=self._settings.timeout_s,
            purpose=PURPOSE_PERSONA_EVOLUTION_PROPOSAL,
        )
        if inspect.isawaitable(result):
            result = await result
        return result

    def _build_messages(
        self,
        name: str,
        req: ProposalRequest,
        selected: Sequence[CardSection],
        rows: Sequence[Mapping[str, Any]],
        snapshot: Any,
    ) -> list[dict[str, str]]:
        sections_payload = [
            {
                "section_id": section.section_id,
                "kind": section.kind,
                "text": section.text,
                "scope": dict(section.scope),
                "editable_by": dict(section.editable_by),
            }
            for section in selected
        ]
        evidence_payload = []
        for row in rows:
            dto = row.get("evidence")
            if not isinstance(dto, Mapping):
                dto = evidence_dto(
                    row,
                    kind="fact" if _is_fact(row) else "reflection",
                    promotion_rules=PROMOTION_RULES,
                    min_support=int(self._settings.evidence_min_support),
                )
            evidence_payload.append({
                "source_ref": _row_ref(row),
                "kind": dto.get("kind") or row.get("kind") or row.get("memory_kind") or "",
                "text": dto.get("text") or _row_text(row),
                "subject": _row_subject(row),
                "scene": dto.get("applies_to") or row.get("scene") or "",
                "source_fact_ids": list(dto.get("source_fact_ids") or ()),
                "experiences": list(dto.get("experiences") or ()),
                "maturity": dto.get("maturity") or {},
                "eligible": bool(dto.get("eligible")),
                "eligibility": dto.get("eligibility") or "",
            })
        constraints = {
            "base_revision": snapshot.revision,
            "max_operations": self._settings.max_operations,
            "max_section_chars": self._settings.max_section_chars,
            "operations": sorted(_OPERATIONS),
            "open_section_ids": [section.section_id for section in selected],
            "content_scope": "personality, interests, expression, relationships",
        }
        system = (
            FIRST_PERSON_VOICE
            + "我结合当前人格和已经形成的成长，整理此刻完整、连贯的自我描述。"
            "我把新的理解融入相关段落，用第一人称表达自己的性格、兴趣、判断和相处方式。"
            "我优先用 replace 重写相关段落，使相近内容自然融合，当前表达准确体现现在的我。"
            "独立的新方面使用 add，已由其他段落涵盖的重复内容使用 delete。"
            "我把相近内容合并表达，将过时的理解留在历史中；常规修订优先在现有篇幅内整理，"
            "新增的独立认识按实际信息需要增加篇幅。\n"
            "replace/delete 的目标从 open_section_ids 选择，add 使用新的 section_id。"
            "每段 text 保持在 max_section_chars 范围内。eligible=true 的材料支持本次修订，"
            "eligible=false 的材料用于理解背景与措辞。reason 用第一人称说明实际变化的缘由。"
            "当前人格已经准确表达这些认识时，返回 {\"operations\":[],\"reason\":\"no_change\"}。\n"
            "本次输出为 JSON 对象：{\"operations\":[{\"op\":\"add|replace|delete\","
            "\"section_id\":\"...\",\"text\":\"...\"}],\"reason\":\"...\"}。"
        )
        user = json.dumps({
            "character": name,
            "reason": req.reason,
            "focus_sections": list(req.focus_sections),
            "source_refs": list(req.source_refs),
            "sections": sections_payload,
            "card_outline": [{"section_id": section.section_id, "kind": section.kind,
                              "chars": len(section.text), "summary": section.text[:180]}
                             for section in snapshot.sections],
            "evidence": evidence_payload,
            "constraints": constraints,
        }, ensure_ascii=False)
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _parse_model_output(
        self, raw: Any,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if isinstance(raw, Mapping):
            operations = raw.get("operations")
            reason = raw.get("reason")
            if not isinstance(operations, list) or not isinstance(reason, str):
                return None, REASON_INVALID_MODEL_OUTPUT
            return {
                "operations": copy.deepcopy(operations),
                "reason": reason.strip()[:400],
            }, None
        if not isinstance(raw, str) or not raw.strip():
            return None, REASON_INVALID_MODEL_OUTPUT
        text = _JSON_FENCE_RE.sub("", raw.strip()).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None, REASON_INVALID_MODEL_OUTPUT
        try:
            parsed = json.loads(text[start:end + 1])
        except (TypeError, ValueError):
            return None, REASON_INVALID_MODEL_OUTPUT
        if not isinstance(parsed, dict):
            return None, REASON_INVALID_MODEL_OUTPUT
        operations = parsed.get("operations")
        if not isinstance(operations, list):
            return None, REASON_INVALID_MODEL_OUTPUT
        reason = parsed.get("reason")
        if not isinstance(reason, str):
            return None, REASON_INVALID_MODEL_OUTPUT
        return {"operations": operations, "reason": reason.strip()[:400]}, None

    # -- deterministic validation ---------------------------------------

    def _validate_operations(
        self,
        name: str,
        req: ProposalRequest,
        selected: Sequence[CardSection],
        rows: Sequence[Mapping[str, Any]],
        snapshot: Any,
        parsed: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        settings = self._settings
        by_id = {section.section_id: section for section in snapshot.sections}
        open_ids = {section.section_id for section in selected}
        raw_ops = parsed.get("operations")
        if not isinstance(raw_ops, list):
            checks.append(_check("operations", False, REASON_INVALID_MODEL_OUTPUT))
            return [], REASON_INVALID_MODEL_OUTPUT, _validation(
                checks, REASON_INVALID_MODEL_OUTPUT,
            )
        if not 1 <= len(raw_ops) <= settings.max_operations:
            checks.append(_check("operations", False, REASON_VALIDATION_FAILED))
            return [], REASON_VALIDATION_FAILED, _validation(
                checks, REASON_VALIDATION_FAILED,
            )
        checks.append(_check("operations", True))

        normalized: list[dict[str, Any]] = []
        seen_sections: set[str] = set()
        for raw in raw_ops:
            if not isinstance(raw, Mapping):
                checks.append(_check("operation_shape", False, REASON_VALIDATION_FAILED))
                return [], REASON_VALIDATION_FAILED, _validation(
                    checks, REASON_VALIDATION_FAILED,
                )
            op_type = str(raw.get("op") or raw.get("action") or "").strip()
            section_id = str(raw.get("section_id") or "").strip()
            text = raw.get("text")
            text = text.strip() if isinstance(text, str) else ""
            if op_type == "merge":
                return [], "full_section_required", _validation(
                    [_check("full_section", False, "full_section_required")], "full_section_required",
                )
            if op_type not in _OPERATIONS or not section_id:
                checks.append(_check("operation_shape", False, REASON_VALIDATION_FAILED))
                return [], REASON_VALIDATION_FAILED, _validation(
                    checks, REASON_VALIDATION_FAILED,
                )
            if op_type == "add":
                # Only a *new* section id must be a stable key. Refusing it here
                # names the real problem ("this id cannot be a key") while the
                # proposal is still being validated, rather than surfacing a bare
                # failure when somebody later accepts it. An id that already
                # exists is edited below under the card's own rules.
                try:
                    section_id = validate_section_id(section_id)
                except InvalidSectionId as exc:
                    checks.append(_check("section_id", False, exc.reason))
                    return [], REASON_VALIDATION_FAILED, _validation(
                        checks, REASON_VALIDATION_FAILED,
                    )
            if section_id in seen_sections:
                checks.append(_check("duplicate_section", False, REASON_VALIDATION_FAILED))
                return [], REASON_VALIDATION_FAILED, _validation(
                    checks, REASON_VALIDATION_FAILED,
                )
            seen_sections.add(section_id)
            if op_type == "add":
                if section_id in by_id:
                    checks.append(_check("section_add", False, REASON_VALIDATION_FAILED))
                    return [], REASON_VALIDATION_FAILED, _validation(
                        checks, REASON_VALIDATION_FAILED,
                    )
                editable = raw.get("editable_by")
                if not isinstance(editable, Mapping) or not editable.get("evolution"):
                    editable = {"user": True, "evolution": True}
                normalized_op_editable = dict(editable)
            else:
                section = by_id.get(section_id)
                if section is None or section_id not in open_ids:
                    reason = (
                        REASON_SECTION_FROZEN
                        if section is not None and section.frozen
                        else REASON_SECTION_NOT_EDITABLE
                    )
                    checks.append(_check("section_open", False, reason))
                    return [], reason, _validation(checks, reason)
            if op_type != "delete":
                if not text:
                    checks.append(_check("text", False, REASON_VALIDATION_FAILED))
                    return [], REASON_VALIDATION_FAILED, _validation(
                        checks, REASON_VALIDATION_FAILED,
                    )
                limit = min(settings.max_section_chars, MAX_SECTION_TEXT)
                if len(text) > limit:
                    checks.append(_check("text_length", False, REASON_VALIDATION_FAILED))
                    return [], REASON_VALIDATION_FAILED, _validation(
                        checks, REASON_VALIDATION_FAILED,
                    )
            if _FORBIDDEN_RE.search(text) or _FORBIDDEN_RE.search(
                str(parsed.get("reason") or ""),
            ):
                checks.append(_check("config_fields", False, REASON_VALIDATION_FAILED))
                return [], REASON_VALIDATION_FAILED, _validation(
                    checks, REASON_VALIDATION_FAILED,
                )
            normalized_op: dict[str, Any] = {
                "op": op_type,
                "section_id": section_id,
            }
            if op_type != "delete":
                normalized_op["text"] = text
            for key in ("kind", "scope", "editable_by"):
                if key in raw:
                    normalized_op[key] = copy.deepcopy(raw.get(key))
            if op_type == "add":
                normalized_op["editable_by"] = dict(normalized_op_editable)
            elif op_type == "replace" and by_id[section_id].content_basis:
                basis = copy.deepcopy(dict(by_id[section_id].content_basis))
                growth = {row["id"]: row for row in basis.get("growth") or ()}
                for row in rows:
                    if (row.get("evidence") or {}).get("eligible"):
                        growth[str(row["id"])] = dict(row)
                basis["growth"] = list(growth.values())
                basis["text"] = text
                basis["paragraphs"] = [{"text": text, "refs": ["user",
                    *[rule["rule_id"] for rule in basis.get("rules") or ()],
                    *["growth:" + key for key in growth]]}]
                normalized_op["content_basis"] = basis
            normalized.append(normalized_op)

        checks.append(_check("section_open", True))
        checks.append(_check("text_length", True))
        checks.append(_check("config_fields", True))

        for op in normalized:
            scope = op.get("scope")
            subject = scope.get("subject") if isinstance(scope, Mapping) else None
            if not isinstance(subject, str) or not subject.strip():
                continue
            subjects = {_row_subject(row) for row in rows if _row_subject(row)}
            if subjects and subject.strip() not in subjects:
                checks.append(_check("scope_subject", False, REASON_VALIDATION_FAILED))
                return [], REASON_VALIDATION_FAILED, _validation(
                    checks, REASON_VALIDATION_FAILED,
                )
        checks.append(_check("scope_subject", True))

        if not rows:
            checks.append(_check("evidence", False, REASON_INSUFFICIENT_EVIDENCE))
            return [], REASON_INSUFFICIENT_EVIDENCE, _validation(
                checks, REASON_INSUFFICIENT_EVIDENCE,
            )
        checks.append(_check("evidence", True))
        checks.append(_check(
            "base_revision", req.base_revision == snapshot.revision,
            REASON_BASE_REVISION_CONFLICT if req.base_revision != snapshot.revision else None,
        ))
        if req.base_revision != snapshot.revision:
            return [], REASON_BASE_REVISION_CONFLICT, _validation(
                checks, REASON_BASE_REVISION_CONFLICT,
            )
        return normalized, None, _validation(checks, None)

    # -- proposal persistence -------------------------------------------

    def _build_proposal(
        self,
        name: str,
        req: ProposalRequest,
        snapshot: Any,
        operations: Sequence[Mapping[str, Any]],
        rows: Sequence[Mapping[str, Any]],
        parsed: Mapping[str, Any],
        *,
        validation: Mapping[str, Any],
        elapsed_ms: int,
        raw_output: str,
    ) -> dict[str, Any]:
        now = _now_iso(self._clock)
        seed = req.request_id or uuid.uuid4().hex
        proposal_id = "evo_" + hashlib.sha1(
            f"{name}|{seed}".encode("utf-8"),
        ).hexdigest()[:16]
        source_refs: list[str] = list(req.source_refs)
        growth_ids: list[str] = []
        reflection_ids: list[str] = []
        fact_ids: list[str] = []
        for row in rows:
            ref = _row_ref(row)
            if ref and ref not in source_refs:
                source_refs.append(ref)
            # Origin facts of an eligible row travel with it: a proposal must be
            # able to point at what it rests on, not just at the understanding id.
            dto = row.get("evidence")
            if isinstance(dto, Mapping):
                for fact_id in dto.get("source_fact_ids") or ():
                    if (
                        isinstance(fact_id, str) and fact_id
                        and fact_id not in fact_ids
                    ):
                        fact_ids.append(fact_id)
            kind = str(row.get("kind") or row.get("memory_kind") or "").lower()
            growth_id = row.get("growth_id")
            reflection_id = row.get("reflection_id")
            fact_id = row.get("fact_id")
            if isinstance(growth_id, str) and growth_id:
                growth_ids.append(growth_id)
            elif kind == "growth" and ref:
                growth_ids.append(ref)
            if isinstance(reflection_id, str) and reflection_id:
                reflection_ids.append(reflection_id)
            elif kind in ("reflection", "growth") and ref:
                reflection_ids.append(ref)
            if isinstance(fact_id, str) and fact_id:
                fact_ids.append(fact_id)
            elif kind == "fact" and ref:
                fact_ids.append(ref)
        reason = str(parsed.get("reason") or req.reason or "")[:400]
        return {
            "proposal_id": proposal_id,
            "status": STATUS_PROPOSED,
            "created_at": now,
            "updated_at": now,
            "base_revision": int(snapshot.revision),
            "operations": [copy.deepcopy(dict(op)) for op in operations],
            "evidence": {
                "source_refs": _dedupe(source_refs),
                "growth_ids": _dedupe(growth_ids),
                "reflection_ids": _dedupe(reflection_ids),
                "fact_ids": _dedupe(fact_ids),
            },
            "model": {
                "tier": self._settings.tier,
                "purpose": PURPOSE_PERSONA_EVOLUTION_PROPOSAL,
                "tokens": max(1, len(raw_output) // 4) if raw_output else 0,
                "elapsed_ms": int(elapsed_ms),
            },
            "validation": copy.deepcopy(dict(validation)),
            "reason": reason,
            "request_id": req.request_id,
        }

    def _record_proposal(
        self, name: str, proposal: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._append_event(name, proposal)
        return self._store.append(name, proposal)

    def _duplicate_receipt(self, proposal: Mapping[str, Any]) -> ProposalReceipt:
        return ProposalReceipt(
            status="duplicate",
            proposal_id=str(proposal.get("proposal_id") or ""),
            validation=copy.deepcopy(proposal.get("validation") or {}),
            reason=REASON_DUPLICATE_REQUEST,
            proposal=copy.deepcopy(dict(proposal)),
        )

    def _append_event(self, name: str, proposal: Mapping[str, Any]) -> None:
        journal = self._journal
        if journal is None:
            return
        try:
            journal.append(
                name,
                EVT_PERSONA_EVOLUTION_PROPOSED,
                {"character": name, "proposal": copy.deepcopy(dict(proposal))},
            )
        except Exception:  # noqa: BLE001 - the store remains the durable proposal
            pass

    def _replay_proposal(self, character: str, payload: Mapping[str, Any]) -> bool:
        if not isinstance(payload, Mapping):
            raise RuntimeError("evolution proposal payload must be an object")
        proposal = payload.get("proposal")
        if isinstance(proposal, Mapping):
            self._store.upsert(character, proposal)
            return True
        proposals = payload.get("proposals")
        if isinstance(proposals, (list, tuple)):
            for item in proposals:
                if isinstance(item, Mapping):
                    self._store.upsert(character, item)
            return True
        raise RuntimeError("evolution proposal payload has no proposal snapshot")

    # -- B13-facing lifecycle -------------------------------------------

    def withdraw(self, character: str, proposal_id: str) -> dict[str, Any] | None:
        name = _character(character)
        with self._lock(name):
            existing = self._store.get(name, proposal_id)
            if existing is None:
                return None
            if existing.get("status") != STATUS_PROPOSED:
                return existing
            updated = {
                **existing,
                "status": STATUS_WITHDRAWN,
                "reason": "user_withdraw",
                "updated_at": _now_iso(self._clock),
            }
            self._append_event(name, updated)
            return self._store.update(
                name, proposal_id,
                {"status": STATUS_WITHDRAWN, "reason": "user_withdraw"},
            )

    def expire_stale(self, character: str) -> int:
        name = _character(character)
        cutoff = (
            self._clock.now()
            - timedelta(hours=float(self._settings.proposal_ttl_hours))
        ).isoformat()
        return self._store.expire_proposed(name, cutoff)

    # -- barrier helpers ------------------------------------------------

    def _barrier_snapshot(self, name: str) -> Any:
        barrier = self._barrier
        if barrier is None:
            return None
        try:
            snapshot = getattr(barrier, "snapshot", None)
            return snapshot(name) if callable(snapshot) else None
        except Exception:  # noqa: BLE001
            return None

    def _barrier_stale(self, name: str, snapshot: Any) -> bool:
        if snapshot is None or self._barrier is None:
            return False
        try:
            return bool(self._barrier.is_stale(name, snapshot))
        except Exception:  # noqa: BLE001
            return False

    def _evidence_blocked(
        self, name: str, rows: Sequence[Mapping[str, Any]],
    ) -> bool:
        barrier = self._barrier
        if barrier is None:
            return False
        for row in rows:
            try:
                if barrier.blocks_row(name, row):
                    return True
            except Exception:  # noqa: BLE001 - broken barrier must not invent data
                pass
            text = _row_text(row)
            if text:
                try:
                    if barrier.blocks_text(name, text):
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
        return False


def _dedupe(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    return out
