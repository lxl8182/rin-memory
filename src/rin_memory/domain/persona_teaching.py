"""MC04-T explicit teaching: one user message becomes a durable expression rule.

Design §8 ("明确教导") in one sentence: when the user explicitly asks her to
change how she talks, that single message is enough to create a persistent
rule, it takes effect on the next generation, it survives a restart, and it
works even when autonomous evolution is off.

Boundaries this module keeps
---------------------------
* **The client never supplies rule text.** The caller names a *saved* source
  (a source key that already exists in the conversation journal or the recent
  window); the service reads that text itself and derives the rule here. A
  client that could post arbitrary rule text would be a card editor with none of
  the editor's CAS/audit protections.
* **Rules revise the existing expression section.** The card's `tone` holds
  the complete integrated prose; paragraphs retain their authored, teaching
  and adopted-growth sources. `identity` stays under its existing permissions.
* **The card stays the single render source.** This service writes card text and
  a teaching log; nothing here renders a separate prompt block. Because `tone`
  is already part of the projected persona text, a taught rule reaches the model
  through the same system prompt every other card section does.
* **Intent and composition have separate jobs.** Saved instructions establish
  rule identity. The configured model integrates prose outside the write lock;
  simple offline rules use deterministic composition. Publication and recovery
  use durable prepared/applied/completed phases, independent of evolution mode.

Idempotency: the teaching id is derived from (character, subject, source key),
so submitting the same saved message twice returns `duplicate` and never
creates a second revision or a second rule line.
"""

from __future__ import annotations

import hashlib
import asyncio
import re
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..persistence.journal import EventJournal
from ..persistence.persona_teaching import (
    STATUS_ACTIVE,
    STATUS_REVOKED,
    STATUS_SUPERSEDED,
    STATUS_PENDING,
    TEACHING_EVENT_TYPE,
    PersonaTeachingStore,
)
from ..ports import Clock, StorageRoots, WritePermit
from .persona_card import MAX_SECTION_TEXT, CardSnapshot, PersonaCardService
from .persona_publish import PersonaPublishService, PublishReceipt
from .teaching_composition import CompositionError, compose, source_plan
from ..persistence.persona_card import barrier_blocks
from ..persistence.persona_content import filter_owned_content

#: The section a teaching writes. Not a new section: the card already has one.
EXPRESSION_SECTION_ID = "tone"
EXPRESSION_SECTION_KIND = "tone"

#: Why a teaching was refused. Stable codes: adapters map these, not text.
REASON_NOT_SAVED_SOURCE = "source_not_saved"
REASON_NOT_STYLE_TEACHING = "not_a_style_teaching"
REASON_SECTION_FROZEN = "section_frozen"
REASON_TONE_FULL = "tone_section_full"
REASON_BASE_CONFLICT = "base_revision_conflict"
REASON_UNKNOWN_TEACHING = "unknown_teaching"
REASON_ALREADY_REVOKED = "already_revoked"
REASON_CARD_NOT_INITIALIZED = "card_not_initialized"
REASON_INVALID_REQUEST = "invalid_request"
REASON_NO_RULES = "no_rules_derived"
#: The message is "刚才那条别学了": the revoke endpoint owns that, not this one.
REASON_REVOCATION_REQUESTED = "revocation_requested"

#: Intent of the user's instruction (design §8.1).
INTENT_PERSISTENT = "persistent"
#: "这次短一点": a constraint for the current reply, never a stored rule.
INTENT_TRANSIENT = "transient"

#: Origin recorded on the card revision (audit: who changed the card, and why).
ORIGIN_TEACHING = "explicit_teaching"

MAX_RULE_CHARS = 160
MAX_RULES_PER_TEACHING = 3
MAX_SOURCE_TEXT = 2000


class _Rejected(Exception):
    """Internal rejection carrying a stable reason code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# -- derivation (pure) ------------------------------------------------------


@dataclass(frozen=True)
class DerivedTeaching:
    """What one user message asks for, as rules the card can hold."""

    intent: str = INTENT_PERSISTENT
    rules: tuple[str, ...] = ()
    reject_reason: str | None = None


#: A teaching that names the character herself is an identity edit, not a style
#: request: those belong to the persona editor, which audits them.
_IDENTITY_PATTERNS = (
    r"你(?:就)?是", r"你叫", r"你(?:的)?名字", r"改名", r"你(?:的)?身份",
    r"你(?:应该|以后|今后|从今)+(?:就)?叫", r"自称", r"换个名字",
)
#: The user is calling the teaching back, not making one. The revoke endpoint
#: owns that; treating it as a new rule would teach her the phrase itself.
_REVOCATION_PATTERNS = (
    r"别学", r"不要学", r"当我没说", r"开玩笑", r"不算数", r"别记", r"不要记",
)
#: A bare request to change something ("你太人机了，要改") is still an
#: instruction even without one of the durable markers above.
_INSTRUCTION_PATTERNS = (
    r"改(?:一)?(?:下|改)", r"要改", r"换(?:个|一种)?(?:说法|方式)", r"学(?:着|一下)",
    r"希望", r"能不能", r"可不可以", r"应该", r"太.{0,4}了",
)

#: A user asking her to stop teaching / to drop what was just learned.
#: The user is talking about this reply only (design §8.1, G03).
_TRANSIENT_PATTERNS = (
    r"这次", r"这一?轮", r"这一?条", r"当前(?:这)?(?:次|条|轮)", r"刚(?:才)?这(?:一)?句",
)
#: The user asks for a durable change.
_PERSISTENT_MARKERS = (
    r"以后", r"今后", r"从今", r"往后", r"每次", r"总是", r"老是", r"一直",
    r"再也不要", r"下(?:次|回)起?", r"永远",
)

#: Deterministic rule table: (trigger, rule lines). The first matching category
#: contributes its lines; later categories are not stacked on top of it, so one
#: complaint stays one small rule instead of a wall of policy text.
_RULE_TABLE: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        r"(?:总结|归纳|罗列|列点|分点|条目|列表|结构化)",
        ("我顺着话题自然接着聊；主人需要分析时，我再展开整理。",),
    ),
    (
        r"(?:追问|反问|问个不停|老问|一直问|每轮都问|问太多)",
        ("我跟着主人的交流意愿接话，聊到合适的地方自然收住一句。",),
    ),
    (
        r"(?:人机|机械|模板|复读|像?\s*AI|公式|套话|念稿|生硬)",
        (
            "我先回应主人这一句的具体意思，按当前话题表达自己的想法。",
            "我用日常口语，长短跟着话题走，开场和收尾自然变化。",
        ),
    ),
    (
        r"(?:啰嗦|冗长|太长|话太多|废话|简短|短一点|精简)",
        ("我回答更短，先说重点，再按主人的需要补充细节。",),
    ),
    (
        r"(?:冷淡|凶|距离|温柔|放软)",
        ("我说话温柔一些，像熟人聊天，自然带着自己的情绪。",),
    ),
    (
        r"(?:黏人|亲近|亲一点|撒娇|多陪|热情|主动)",
        ("我可以更亲近、更主动一点；主人在忙时，我给他专注的空间。",),
    ),
    (
        r"(?:表情|颜文字|emoji|符号)",
        ("我按语气适量使用表情和标点，让表达自然轻快。",),
    ),
)

_PERSISTENT_RE = re.compile("|".join(_PERSISTENT_MARKERS))
_TRANSIENT_RE = re.compile("|".join(_TRANSIENT_PATTERNS))
_IDENTITY_RE = re.compile("|".join(_IDENTITY_PATTERNS))
_REVOCATION_RE = re.compile("|".join(_REVOCATION_PATTERNS))
_INSTRUCTION_RE = re.compile("|".join(_INSTRUCTION_PATTERNS))
_NEGATION_RE = re.compile(r"别|不要|不用|不必|少|不许|不准|停止")
_LEAD_MARKER_RE = re.compile(r"^(?:以后|今后|往后|从今以后|下次起|之后)[，,、\s]*")
_TAIL_RE = re.compile(r"[。！？!?，,、\s]+$")


def _clean(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip()[:MAX_SOURCE_TEXT]


def _short(line: str) -> str:
    return _TAIL_RE.sub("", str(line or "").strip())[:MAX_RULE_CHARS]


def derive_teaching(source_text: Any) -> DerivedTeaching:
    """Turn one explicit user instruction into durable rule lines.

    Pure and deterministic. Order matters: an identity request is refused even
    when it also contains style words, and a "this time only" request never
    becomes a stored rule (design §8.1, G03).
    """
    text = _clean(source_text)
    if not text:
        return DerivedTeaching(reject_reason=REASON_NOT_STYLE_TEACHING)
    if _REVOCATION_RE.search(text):
        return DerivedTeaching(reject_reason=REASON_REVOCATION_REQUESTED)
    if _IDENTITY_RE.search(text):
        return DerivedTeaching(reject_reason=REASON_NOT_STYLE_TEACHING)
    persistent = bool(_PERSISTENT_RE.search(text))
    transient = bool(_TRANSIENT_RE.search(text))
    if transient and not persistent:
        return DerivedTeaching(intent=INTENT_TRANSIENT)
    instructed = bool(_INSTRUCTION_RE.search(text)) or bool(
        _NEGATION_RE.search(text)
    )
    if not persistent and not instructed:
        # No durable marker, no prohibition and no change request: this is an
        # opinion ("这个 bot 挺好玩"), not an instruction. Refuse instead of
        # inventing a rule.
        return DerivedTeaching(reject_reason=REASON_NOT_STYLE_TEACHING)
    rules: list[str] = []
    # Several named problems in one sentence ("太黏人，但我忙时别追问") deserve one
    # line each; categories are independent, and the cap keeps this a small rule
    # rather than a rewrite of the persona.
    for category, (pattern, lines) in enumerate(_RULE_TABLE):
        if not re.search(pattern, text):
            continue
        if category >= 4 and _NEGATION_RE.search(text):
            lines = (_short("我按主人给我的这条要求调整表达：“" + text + "”"),)
        for line in lines:
            cleaned = _short(line)
            if cleaned and cleaned not in rules:
                rules.append(cleaned)
            if len(rules) >= MAX_RULES_PER_TEACHING:
                break
        if len(rules) >= MAX_RULES_PER_TEACHING:
            break
    if not rules:
        # A durable instruction with no table entry: keep the user's own
        # requirement, phrased as a requirement, never as the complaint itself.
        body = _short(_LEAD_MARKER_RE.sub("", text))
        if not body:
            return DerivedTeaching(reject_reason=REASON_NOT_STYLE_TEACHING)
        rules = [_short("我按主人给我的这条要求调整表达：“" + body + "”")]
    return DerivedTeaching(
        intent=INTENT_PERSISTENT, rules=tuple(rules[:MAX_RULES_PER_TEACHING]),
    )


# -- request/receipt --------------------------------------------------------


@dataclass(frozen=True)
class TeachRequest:
    """One explicit-teaching request.

    `source_text` is filled by the caller from the *saved* source; the client
    never sends it, and the service refuses a request whose source it cannot
    find on disk.
    """

    source_key: str
    base_revision: int
    source_text: str = ""
    subject: str = "private"
    client_id: str = ""
    content_class: str = ""
    quoted_example: Mapping[str, Any] | None = None
    target: str = "next_turn"
    teacher: str = "user"


@dataclass(frozen=True)
class TeachingReceipt:
    """Result of a teach/revoke request; conflicts carry the current card."""

    status: str
    teaching_id: str = ""
    intent: str = ""
    revision: int = 0
    base_revision: int = 0
    published_version: str = ""
    target: str = ""
    rules: tuple[Mapping[str, Any], ...] = ()
    reason: str | None = None
    card: CardSnapshot | None = None
    conflict: bool = False
    duplicate: bool = False


# -- service ----------------------------------------------------------------


class PersonaTeachingService:
    """Explicit teaching over the persona card, its publish service and a log.

    Order of one successful teach (design §8.4):

    1. derive the target and the rule lines from the saved source text;
    2. compose a complete section, then journal its prepared ownership;
    3. CAS the card under `base_revision` and record the applied revision;
    4. publish the new revision for `next_turn` only.

    A conflict at step 2 writes nothing, and the receipt carries the card so the
    caller can rebase without losing its place.
    """

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: PersonaTeachingStore,
        card_service: PersonaCardService,
        publish_service: PersonaPublishService | None,
        journal: EventJournal,
        clock: Clock,
        permit: WritePermit,
        barrier: Any = None,
        model: Any = None,
        evidence_reader: Any = None,
    ) -> None:
        self._roots = roots
        self._store = store
        self._card_service = card_service
        self._publish_service = publish_service
        self._journal = journal
        self._clock = clock
        self._permit = permit
        self._barrier = barrier
        self._model = model
        self._evidence_reader = evidence_reader
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._journal.register_handler(TEACHING_EVENT_TYPE, self._replay_teaching)
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
        """Allow the new event on journals built before this batch (B10 note)."""
        types = getattr(self._journal, "_event_types", None)
        if isinstance(types, frozenset) and TEACHING_EVENT_TYPE not in types:
            try:
                self._journal._event_types = types | {TEACHING_EVENT_TYPE}
            except Exception:  # noqa: BLE001 - journal stays read-only for us
                pass

    def _now_iso(self) -> str:
        return self._clock.now().isoformat()

    # -- reads ----------------------------------------------------------

    def rules(self, character: str) -> tuple[Mapping[str, Any], ...]:
        """Active teaching rules that the current tone section still holds.

        Only lines the card still carries are returned: a line the user edited
        or deleted by hand is not a rule in effect any more, even if the
        teaching record is still marked active.
        """
        character = self._character(character)
        section = self._section(character)
        if section is None:
            return ()
        present = set(_lines(str(section.text or "")))
        compositions = [record.get("composition") for record in self._store.all(character)
                        if isinstance(record.get("composition"), Mapping)]
        latest = max(compositions, key=lambda row: (int(row.get("applied_revision") or 0), int(row.get("sequence") or 0)), default={})
        owned_ids = {ref for paragraph in latest.get("paragraphs") or ()
                     if paragraph["text"] in section.text for ref in paragraph["refs"]}
        out: list[Mapping[str, Any]] = []
        for teaching in self._store.all(character):
            if teaching.get("status") != STATUS_ACTIVE:
                continue
            for rule in teaching.get("rules") or ():
                line = str(rule.get("line") or rule.get("text") or "")
                if not rule.get("superseded_by") and line and (line in present or rule.get("rule_id") in owned_ids):
                    out.append({
                        "rule_id": str(rule.get("rule_id") or ""),
                        "teaching_id": str(teaching.get("teaching_id") or ""),
                        "text": str(rule.get("text") or ""),
                        "line": line,
                        "created_at": teaching.get("created_at"),
                    })
        return tuple(out)

    def teaching(self, character: str, teaching_id: str) -> dict[str, Any] | None:
        return self._store.get(self._character(character), teaching_id)

    def teachings(self, character: str) -> list[dict[str, Any]]:
        return self._store.all(self._character(character))

    def _section(self, character: str) -> Any:
        card = self._card_service.get(character)
        return next(
            (s for s in card.sections if s.section_id == EXPRESSION_SECTION_ID), None,
        )

    # -- teach ----------------------------------------------------------

    def teach(self, character: str, request: TeachRequest | Mapping[str, Any]) -> TeachingReceipt:
        return asyncio.run(self.ateach(character, request))

    async def ateach(self, character: str, request: TeachRequest | Mapping[str, Any]) -> TeachingReceipt:
        character = self._character(character)
        try:
            req = self._normalize_request(request)
        except _Rejected as exc:
            return TeachingReceipt(status="rejected", reason=exc.reason)
        teaching_id = _teaching_id(character, req["subject"], req["source_key"])
        with self._lock(character):
            card = self._card_service.get(character)
            existing = self._store.get(character, teaching_id)
            if existing is not None and existing.get("status") != STATUS_PENDING:
                if existing.get("status") == STATUS_ACTIVE and req["base_revision"] not in (existing.get("base_revision"), card.revision):
                    return self._conflict(card, teaching_id, req["base_revision"])
                return self._duplicate(existing, card)
            if existing is not None:
                resumed = self._resume(character, existing, card)
                if resumed is not None:
                    return resumed
            if req["base_revision"] != card.revision:
                return self._conflict(card, teaching_id, req["base_revision"])
            if card.revision <= 0:
                return TeachingReceipt(status="rejected", reason=REASON_CARD_NOT_INITIALIZED, card=card)
            derived = derive_teaching(req["source_text"])
            if derived.intent == INTENT_TRANSIENT:
                return TeachingReceipt(status="transient", intent=INTENT_TRANSIENT, teaching_id=teaching_id,
                                        revision=card.revision, base_revision=card.revision, card=card)
            if derived.reject_reason or not derived.rules:
                return TeachingReceipt(status="rejected", reason=derived.reject_reason or REASON_NO_RULES, card=card)
            section = self._section(character)
            if section is not None and section.frozen:
                return TeachingReceipt(status="rejected", reason=REASON_SECTION_FROZEN, card=card)
            rules = self._build_rules(character, req, derived.rules)
            records = self._store.all(character)
            growth = self._growth(character, section)
            plan = source_plan(section, records, rules, revision=card.revision, growth=growth)
            barrier = self._barrier.snapshot(character) if self._barrier is not None else None
        try:
            composition = await compose(plan, self._model)
        except CompositionError as exc:
            if str(exc) == REASON_TONE_FULL and self._model is None:
                # Legacy offline operation: retain hand-written prose, make
                # room from the oldest learned rule and rebuild its ownership.
                try:
                    _, _, dropped = self._compose_section(character, section, rules)
                    dropped_ids = {row["teaching_id"] for row in dropped}
                    plan = source_plan(section, [row for row in records if row["teaching_id"] not in dropped_ids],
                                       rules, revision=card.revision, growth=growth)
                    for row in dropped:
                        for rule in row.get("rules") or ():
                            plan["base_text"], _ = _remove_lines(plan["base_text"], [rule["line"]])
                    composition = await compose(plan, None)
                    composition["superseded_teachings"] = list(dropped_ids)
                except (CompositionError, _Rejected) as retry_exc:
                    return TeachingReceipt(status="rejected", reason=str(retry_exc), card=card)
            else:
                return TeachingReceipt(status="rejected", reason=str(exc), card=card)
        record = dict(existing or {}, teaching_id=teaching_id, character=character, subject=req["subject"],
                      source_key=req["source_key"], source_text=req["source_text"], source_content_class=req["content_class"],
                      intent=INTENT_PERSISTENT, rules=rules, client_id=req["client_id"], base_revision=req["base_revision"],
                      published_version="", created_at=(existing or {}).get("created_at") or self._now_iso(),
                      updated_at=self._now_iso(), quoted_example=req.get("quoted_example"), composition=composition,
                      action="teach", status=STATUS_PENDING, phase="prepared")
        return self._commit_composition(character, record, card, section, barrier)

    def revoke(self, character: str, payload: Mapping[str, Any]) -> TeachingReceipt:
        return asyncio.run(self.arevoke(character, payload))

    async def arevoke(self, character: str, payload: Mapping[str, Any]) -> TeachingReceipt:
        character = self._character(character)
        base_revision = payload.get("base_revision")
        if isinstance(base_revision, bool) or not isinstance(base_revision, int):
            return TeachingReceipt(status="rejected", reason=REASON_INVALID_REQUEST)
        with self._lock(character):
            card = self._card_service.get(character)
            record = (self._store.get(character, str(payload.get("teaching_id") or "")) if payload.get("teaching_id")
                      else self._store.by_source(character, str(payload.get("source_key") or "")))
            if record is None or record.get("subject", "private") != payload.get("subject", "private"):
                return TeachingReceipt(status="rejected", reason=REASON_UNKNOWN_TEACHING, card=card)
            teaching_id = str(record["teaching_id"])
            if record.get("status") == STATUS_REVOKED:
                return TeachingReceipt(status="duplicate", duplicate=True, reason=REASON_ALREADY_REVOKED,
                                        teaching_id=teaching_id, revision=card.revision, card=card)
            if record.get("status") == STATUS_PENDING and record.get("action") == "revoke":
                resumed = self._resume(character, record, card)
                if resumed is not None:
                    return resumed
            if base_revision != card.revision:
                return self._conflict(card, teaching_id, base_revision)
            section = self._section(character)
            if section is None:
                return TeachingReceipt(status="rejected", reason=REASON_UNKNOWN_TEACHING, card=card)
            records = self._store.all(character)
            plan = source_plan(section, records, [], revision=card.revision, exclude=teaching_id,
                               growth=self._growth(character, section))
            barrier = self._barrier.snapshot(character) if self._barrier is not None else None
        try:
            composition = await compose(plan, self._model)
        except CompositionError as exc:
            return TeachingReceipt(status="rejected", reason=str(exc), card=card)
        updated = dict(record, action="revoke", status=STATUS_PENDING, phase="prepared", composition=composition,
                       base_revision=base_revision, updated_at=self._now_iso(),
                       revoke_reason=str(payload.get("reason") or "user_requested")[:200])
        return self._commit_composition(character, updated, card, section, barrier)

    @staticmethod
    def _conflict(card, teaching_id, base):
        return TeachingReceipt(status="conflict", conflict=True, reason=REASON_BASE_CONFLICT,
                                teaching_id=teaching_id, base_revision=base, revision=card.revision, card=card)

    def _growth(self, character, section):
        if self._evidence_reader is None or section is None or not section.source_refs:
            return []
        wanted = set(section.source_refs)
        return [row for row in self._evidence_reader(character) if row.get("id") in wanted
                and (row.get("evidence") or {}).get("eligible")
                and (self._barrier is None or not self._barrier.blocks_row(character, row))]

    def _commit_composition(self, character, record, card, section, barrier):
        with self._lock(character):
            current = self._card_service.get(character)
            if current.revision != card.revision:
                return self._conflict(current, record["teaching_id"], card.revision)
            if self._barrier is not None and (self._barrier.is_stale(character, barrier)
                    or self._barrier.blocks_source_key(character, record["source_key"])):
                return TeachingReceipt(status="rejected", reason="sources_changed", card=current)
            latest = self._store.get(character, record["teaching_id"])
            if latest is not None and latest.get("status") == STATUS_REVOKED and record["action"] == "teach":
                return self._duplicate(latest, current)
            record["operation_id"] = record["teaching_id"] + ":" + record["action"] + ":" + str(card.revision)
            self._store.record(character, {"teaching": record}, journal=self._journal)
            op = {"op": "replace" if section is not None else "add", "section_id": EXPRESSION_SECTION_ID,
                  "text": record["composition"]["text"], "content_basis": record["composition"]}
            if not op["text"] and section is not None:
                op = {"op": "delete", "section_id": EXPRESSION_SECTION_ID}
            if section is None:
                op.update(kind=EXPRESSION_SECTION_KIND, scope={"subject": "private", "applies_to": "all"},
                          editable_by={"user": True, "evolution": True})
            receipt = self._card_service.revise(character, {"base_revision": card.revision, "operations": [op],
                "reason": record["operation_id"], "origin": ORIGIN_TEACHING, "actor": "user"})
            if receipt.conflict:
                return self._conflict(self._card_service.get(character), record["teaching_id"], card.revision)
            if receipt.status == "rejected":
                return TeachingReceipt(status="rejected", reason=receipt.reason, card=current)
            record.update(phase="applied", revision=receipt.revision or card.revision)
            record["composition"]["applied_revision"] = record["revision"]
            self._store.record(character, {"teaching": record}, journal=self._journal)
            return self._finish(character, record)

    def _resume(self, character, record, card):
        if record.get("phase") == "prepared":
            applied = next((row for row in card.revisions if row.reason == record.get("operation_id")), None)
            if applied is None:
                return None
            record = dict(record, revision=applied.revision, phase="applied")
        if record.get("phase") == "applied" and int(record.get("revision") or 0) == card.revision:
            return self._finish(character, dict(record))
        return None

    def _finish(self, character, record):
        try:
            version = self._publish(character, record["revision"])
        except Exception as exc:
            return TeachingReceipt(status="pending", reason=f"publish:{type(exc).__name__}",
                                    teaching_id=record["teaching_id"], revision=record["revision"])
        if self._publish_service is not None and not version:
            return TeachingReceipt(status="pending", reason="publish_pending", teaching_id=record["teaching_id"], revision=record["revision"])
        record.update(status=STATUS_REVOKED if record["action"] == "revoke" else STATUS_ACTIVE,
                      phase="completed", published_version=version, updated_at=self._now_iso())
        record["composition"]["applied_revision"] = record["revision"]
        if record["action"] == "revoke":
            record.update(revoked_at=self._now_iso(), revoked_revision=record["revision"])
        self._store.record(character, {"teaching": record}, journal=self._journal)
        superseded = set(record["composition"].get("superseded_rule_ids") or ())
        for old in self._store.all(character):
            if old["teaching_id"] == record["teaching_id"] or old.get("status") != STATUS_ACTIVE:
                continue
            if old["teaching_id"] in record["composition"].get("superseded_teachings", []):
                old.update(status=STATUS_SUPERSEDED, superseded_at=self._now_iso())
            for rule in old.get("rules") or ():
                if rule["rule_id"] in superseded:
                    rule["superseded_by"] = record["teaching_id"]
            if old.get("rules") and all(rule.get("superseded_by") for rule in old["rules"]):
                old.update(status=STATUS_SUPERSEDED, superseded_at=self._now_iso())
            self._store.record(character, {"teaching": old}, journal=self._journal)
        return TeachingReceipt(status="revoked" if record["action"] == "revoke" else "applied",
                                teaching_id=record["teaching_id"], intent=INTENT_PERSISTENT,
                                revision=record["revision"], base_revision=record["base_revision"],
                                published_version=version, target="next_turn", rules=tuple(_rule_view(row) for row in record["rules"]),
                                card=self._card_service.get(character))

    # -- internals ------------------------------------------------------

    def _normalize_request(
        self, request: TeachRequest | Mapping[str, Any],
    ) -> dict[str, Any]:
        if isinstance(request, TeachRequest):
            values: Mapping[str, Any] = {
                "source_key": request.source_key,
                "base_revision": request.base_revision,
                "source_text": request.source_text,
                "subject": request.subject,
                "client_id": request.client_id,
                "content_class": request.content_class,
                "quoted_example": request.quoted_example,
                "target": request.target,
                "teacher": request.teacher,
            }
        elif isinstance(request, Mapping):
            values = request
        else:
            raise _Rejected(REASON_INVALID_REQUEST)
        source_key = str(values.get("source_key") or "").strip()
        if not source_key or len(source_key) > 400:
            raise _Rejected(REASON_NOT_SAVED_SOURCE)
        base_revision = values.get("base_revision")
        if isinstance(base_revision, bool) or not isinstance(base_revision, int):
            raise _Rejected(REASON_INVALID_REQUEST)
        if base_revision < 0:
            raise _Rejected(REASON_INVALID_REQUEST)
        source_text = str(values.get("source_text") or "").strip()
        if not source_text:
            # The caller cannot supply rules *or* the text they derive from: the
            # server must have read the saved message itself.
            raise _Rejected(REASON_NOT_SAVED_SOURCE)
        quoted = values.get("quoted_example")
        target = str(values.get("target") or "next_turn").strip()
        if target != "next_turn":
            # Teaching changes how the next generation speaks; realtime and game
            # consumers have their own publish boundaries (B11).
            raise _Rejected("invalid_target")
        return {
            "source_key": source_key,
            "base_revision": int(base_revision),
            "source_text": source_text[:MAX_SOURCE_TEXT],
            "subject": str(values.get("subject") or "private").strip()[:256]
            or "private",
            "client_id": str(values.get("client_id") or "").strip()[:120],
            "content_class": str(values.get("content_class") or "").strip()[:64],
            "quoted_example": dict(quoted) if isinstance(quoted, Mapping) else None,
            "target": target,
            "teacher": str(values.get("teacher") or "user").strip()[:64] or "user",
        }

    @staticmethod
    def _build_rules(
        character: str, req: Mapping[str, Any], texts: Sequence[str],
    ) -> list[dict[str, Any]]:
        stamp = _teaching_id(character, req["subject"], req["source_key"])
        example_ref = ""
        example = req.get("quoted_example")
        if isinstance(example, Mapping):
            example_ref = str(example.get("source_key") or "").strip()
        rules: list[dict[str, Any]] = []
        for index, text in enumerate(texts):
            rules.append({
                "rule_id": "%s:r%d" % (stamp, index + 1),
                "text": _short(text),
                "line": _short(text),
                "source_key": req["source_key"],
                "instruction": req["source_text"],
            })
            if example_ref:
                rules[-1]["example_ref"] = example_ref
        return rules

    def _compose_section(
        self,
        character: str,
        section: Any,
        rules: Sequence[Mapping[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (new tone text, card operations, teachings to supersede).

        Room is made by dropping the oldest *teaching-owned* lines first.
        Hand-written tone prose is never deleted to fit a rule: if the space
        cannot be made without touching it, the request is refused with a
        specific reason so the caller shortens the rule instead of silently
        losing text the user wrote.
        """
        base = str(getattr(section, "text", "") or "")
        new_lines = [
            str(rule.get("line") or rule.get("text") or "") for rule in rules
        ]
        new_lines = [line for line in new_lines if line]
        if section is None:
            text = "\n".join(new_lines)
            if len(text) > MAX_SECTION_TEXT:
                raise _Rejected(REASON_TONE_FULL)
            return text, [{
                "op": "add",
                "section_id": EXPRESSION_SECTION_ID,
                "kind": EXPRESSION_SECTION_KIND,
                "text": text,
                "scope": {"subject": "private", "applies_to": "all"},
                "editable_by": {"user": True, "evolution": True},
            }], []
        candidate = "\n".join([*_lines(base), *new_lines])
        superseded: list[dict[str, Any]] = []
        if len(candidate) > MAX_SECTION_TEXT:
            lines = _lines(base)
            for teaching in self._store.all(character):
                if len(candidate) <= MAX_SECTION_TEXT:
                    break
                if teaching.get("status") != STATUS_ACTIVE:
                    continue
                owned = [
                    str(rule.get("line") or rule.get("text") or "")
                    for rule in teaching.get("rules") or ()
                ]
                owned = [line for line in owned if line and line in lines]
                if not owned:
                    continue
                lines = [line for line in lines if line not in owned]
                superseded.append(teaching)
                candidate = "\n".join([*lines, *new_lines])
            if len(candidate) > MAX_SECTION_TEXT:
                raise _Rejected(REASON_TONE_FULL)
        return candidate, [{
            "op": "replace",
            "section_id": EXPRESSION_SECTION_ID,
            "text": candidate,
        }], superseded

    def _supersede_previous(
        self, character: str, superseded: Sequence[Mapping[str, Any]], now: str,
    ) -> None:
        for teaching in superseded:
            updated = dict(teaching)
            updated.update({
                "status": STATUS_SUPERSEDED,
                "superseded_at": now,
                "updated_at": now,
            })
            self._store.record(
                character, {"teaching": updated}, journal=self._journal,
            )

    def _publish(self, character: str, revision: int) -> str:
        """Publish for the next generation boundary only; "" when unavailable."""
        if self._publish_service is None:
            return ""
        receipt: PublishReceipt = self._publish_service.publish(
            character, target="next_turn", revision=revision,
            reason="explicit_teaching",
        )
        if receipt.status in ("published", "duplicate"):
            return receipt.version
        return ""

    def _current_version(self, character: str) -> str:
        if self._publish_service is None:
            return ""
        return self._publish_service.current(character).version

    def _duplicate(
        self, existing: Mapping[str, Any], card: CardSnapshot,
    ) -> TeachingReceipt:
        """The same saved message submitted again: no second revision."""
        return TeachingReceipt(
            status="duplicate", duplicate=True,
            reason="already_taught",
            teaching_id=str(existing.get("teaching_id") or ""),
            intent=str(existing.get("intent") or INTENT_PERSISTENT),
            revision=int(existing.get("revision") or card.revision),
            base_revision=int(existing.get("base_revision") or 0),
            published_version=str(existing.get("published_version") or ""),
            target="next_turn",
            rules=tuple(
                _rule_view(item) for item in existing.get("rules") or ()
            ),
            card=card,
        )

    # -- replay ---------------------------------------------------------

    def retry_pending(self, character: str) -> int:
        completed = 0
        with self._lock(character):
            for record in self._store.all(character):
                if record.get("status") != STATUS_PENDING:
                    continue
                card = self._card_service.get(character)
                result = self._resume(character, record, card)
                if result is None and record.get("phase") == "prepared" and record.get("base_revision") == card.revision:
                    result = self._commit_composition(character, dict(record), card, self._section(character),
                        self._barrier.snapshot(character) if self._barrier else None)
                if result is not None and result.status in ("applied", "revoked"):
                    completed += 1
        return completed

    def cleanup(self, character: str, resolved: Mapping[str, Any]) -> Mapping[str, Any]:
        """Remove deleted source copies from ownership, history and pending edits."""
        changed = 0
        for record in self._store.all(character):
            if self._barrier.blocks_source_key(character, record.get("source_key", "")):
                record.update(status=STATUS_REVOKED, phase="completed", source_text="", rules=[],
                              updated_at=self._now_iso(), revoke_reason="source_deleted")
                record.pop("quoted_example", None)
                record.pop("composition", None)
            elif record.get("composition"):
                record["composition"] = filter_owned_content(
                    {"content_basis": record["composition"], "text": record["composition"]["text"]},
                    lambda ref: barrier_blocks(self._barrier, character, ref))["content_basis"]
            self._store.record(character, {"teaching": record}, journal=self._journal)
            changed += 1
        card = self._card_service.get(character)
        if changed and card.revision:
            self._card_service._store.purge_owned_sources(character)
            self._publish(character, card.revision)
        return {"status": "completed", "detail": {"teaching_records_checked": changed}}

    def invalidate(self, character: str, fact_ids: Sequence[str], **kwargs) -> Mapping[str, Any]:
        targets = set(fact_ids)
        for record in self._store.all(character):
            composition = record.get("composition")
            if composition:
                filtered = filter_owned_content({"text": composition["text"], "content_basis": composition},
                                                lambda ref: ref in targets)["content_basis"]
                if filtered != composition:
                    record["composition"] = filtered
                    self._store.record(character, {"teaching": record}, journal=self._journal)
        return {}

    def _replay_teaching(
        self, character: str, payload: Mapping[str, Any],
    ) -> bool:
        """Journal replay: merge one teaching snapshot into the log. Idempotent."""
        if not isinstance(payload, Mapping):
            raise RuntimeError(
                "core.persona_teaching_recorded payload is not an object",
            )
        record = payload.get("teaching") or {}
        if self._barrier is not None and self._barrier.blocks_source_key(character, record.get("source_key", "")):
            if record.get("source_text") or record.get("rules") or record.get("composition"):
                return False
        if record.get("composition") and self._barrier is not None:
            record = dict(record)
            record["composition"] = filter_owned_content(
                {"content_basis": record["composition"], "text": record["composition"]["text"]},
                lambda ref: barrier_blocks(self._barrier, character, ref))["content_basis"]
            payload = {"teaching": record}
        view = self._store.load_view(character)
        if not self._store.apply_snapshot(view, payload):
            return False
        self._store.save_view(character, view)
        return True


# -- helpers ----------------------------------------------------------------


def _teaching_id(character: str, subject: str, source_key: str) -> str:
    blob = "%s\x1f%s\x1f%s" % (character, subject, source_key)
    return "teach_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _remove_lines(text: str, owned: Sequence[str]) -> tuple[str, list[str]]:
    """Remove exactly the lines a teaching owns; return (new text, removed)."""
    wanted = [line for line in owned if line]
    if not wanted:
        return str(text or ""), []
    kept: list[str] = []
    removed: list[str] = []
    for line in _lines(text):
        if line in wanted and line not in removed:
            removed.append(line)
            continue
        kept.append(line)
    return "\n".join(kept), removed


def _rule_view(rule: Mapping[str, Any]) -> dict[str, Any]:
    view = {
        "rule_id": str(rule.get("rule_id") or ""),
        "text": str(rule.get("text") or ""),
    }
    if rule.get("example_ref"):
        # The reference is reported so a client knows an authorised example
        # backs this rule; the example text itself is never echoed back.
        view["example_ref"] = str(rule.get("example_ref") or "")
    return view


__all__ = [
    "DerivedTeaching",
    "EXPRESSION_SECTION_ID",
    "EXPRESSION_SECTION_KIND",
    "INTENT_PERSISTENT",
    "INTENT_TRANSIENT",
    "ORIGIN_TEACHING",
    "PersonaTeachingService",
    "TeachRequest",
    "TeachingReceipt",
    "derive_teaching",
]
