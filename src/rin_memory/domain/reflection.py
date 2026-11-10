"""B05 reflection, evidence, promotion and growth domain service.

This module owns the host's reflection lifecycle on top of the B05 stores
(`reflections.json`, `surfaced.json`, `synth_backoff.json`,
`growth_uses.json`, `reflection_archive/`) and the B04 fact view:

* synthesis (new evidence + existing understandings -> one local operation)
  through the injected `ModelInvoker` with explicit tier/budget/timeout; the
  prompt carries the related old rows (status, maturity, scope, counter
  evidence) and the strategies already adopted, and the model may answer
  no_change instead of inventing a conclusion;
* read-time evidence decay (reinforcement half-life 30 days, disputation 180
  days, `protected=True` -> `inf`) and daily `sub_zero_days` accounting;
* promotion: score-driven (1.0 / 2.0), time-driven when `powerful_memory`
  is off (7 + 7 days, no model) and growth (min support + evidence flag);
* surfaced/feedback recording with the host's 30 minute cooldown;
* archive sweep (terminal age + score-driven sub-zero days);
* the B03 deletion cleaner and correction invalidator (registered by the
  composition root);
* replay handlers for `reflection.evidence_updated` (full-snapshot whitelist)
  and `reflection.state_changed` (CAS on `from`); a raising handler pauses
  the reconciler so the sentinel cannot skip a lost mutation.

Safety rules kept from the host spec (B05 spec section 6.1):

* every synthesis run applies exactly *one* local operation to the existing
  understandings (add / reinforce / refine / supersede / contradict / split)
  or reports no_change, and a no-op batch is a success, not a failure
  (design section 7.2 and section 15 decision 3);
* a reflection id is content-addressed,
  `ref_<sha1(topic_key|scope_key|text_hash)[:16]>`; the old rule hashed
  the sorted source fact ids, so every new batch produced a new row and a
  reworded understanding could never meet its own predecessor. The content
  triple keeps exact re-synthesis idempotent while letting one topic hold
  several different understandings;
* the synthesis backoff key is the *full* unabsorbed id set, not the capped
  batch id, so a poisoned batch cannot starve new facts. That same key also
  carries the no_change marker, which is what makes "do not re-evaluate a
  batch when nothing changed" fall out of the key itself: new evidence changes
  the id set, hence the key, hence the entry. No extra state file and no extra
  replay handler are needed (see `_mark_no_change`);
* `withdrawn` is not a terminal/hidden status: a corrected reflection stays
  readable as a row but no longer renders or promotes. It stays reserved for
  "the sources went away" (deletion or correction barrier);
* `superseded` IS terminal, and is deliberately a *different* status from
  `withdrawn`: an understanding retired by newer evidence has a different
  cause and different replay obligations than one whose sources were deleted.
  Neither renders, promotes or gets recalled; both stay readable as history;
* before any model-backed write the barrier snapshot is re-checked and a stale
  result is discarded (never written);
* every reflection write passes the deletion barrier first.

Deliberate deviations from the host, recorded for B05 hand-off:

* promotion does not call the host merge-decision LLM. The frozen
  `PersonaService` surface is `add_reflection/add_growth` only, so a
  deterministic write is used; the model-failure "never downgrade to
  promote_fresh" rule is therefore trivially held (no model is consulted);
* a growth candidate is stored on its own reflection row (id = `ref_...`),
  merged across batches by `growth_key`; the host stores a separate
  `grow_...` row. Field names and merge semantics are the host's.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from ..contracts import MemoryBlock
from ..domain.barrier import DeletionBarrier, episode_ids_of
from ..errors import BarrierStaleError, ContentBlockedError
from ..invalidation import (
    fact_invalidated,
    is_friend_subject,
    normalize_text,
    reflection_invalidated,
    row_matches_subject,
    text_hash,
)
from ..persistence.facts_view import FactsView
from ..persistence.journal import EventJournal
from ..persistence.reflections import ReflectionStore
from ..ports import Clock, ModelInvoker, StorageRoots, WritePermit
from .prompt_voice import FIRST_PERSON_VOICE
from .experience import message_material, message_role, number_messages
from .persona_card import EXAMPLES_BANNER
from ..classification import is_extractable, metadata_of
from .barrier import message_source_keys, message_text
from .growth_evidence import (
    apply_support_removal_to_row,
    evidence_dto,
    maturity_of_row,
    merge_basis,
    recompute_support_after_removal,
    support_basis_from_facts,
    support_basis_from_row,
    summarize_basis,
)

# -- event vocabulary (host memory/event_log.py names, persisted identifiers) --

EVT_REFLECTION_EVIDENCE_UPDATED = "reflection.evidence_updated"
EVT_REFLECTION_STATE_CHANGED = "reflection.state_changed"
REFLECTION_EVENT_TYPES: tuple[str, ...] = (
    EVT_REFLECTION_EVIDENCE_UPDATED,
    EVT_REFLECTION_STATE_CHANGED,
)

#: Full-snapshot keys copied by the evidence replay handler (host
#: memory/evidence_handlers.py:48-68). Kept verbatim: replaying a subset would
#: desynchronise the evidence view from the audit log.
EVIDENCE_SNAPSHOT_KEYS: tuple[str, ...] = (
    "reinforcement",
    "disputation",
    "rein_last_signal_at",
    "disp_last_signal_at",
    "sub_zero_days",
    "sub_zero_last_increment_date",
    "user_fact_reinforce_count",
    "last_promote_attempt_at",
    "promote_attempt_count",
)

#: Hidden statuses (host _shared.REFLECTION_TERMINAL_STATUSES). `withdrawn`
#: is deliberately absent: a withdrawn row is still stored but not rendered.
REFLECTION_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "promoted", "denied", "archived", "merged", "promote_blocked", "superseded",
})
REFLECTION_ACTIVE_STATUSES: tuple[str, ...] = ("pending", "confirmed")
REFLECTION_STATUSES: frozenset[str] = frozenset({
    "pending", "confirmed", "promoted", "denied", "archived", "merged",
    "promote_blocked", "withdrawn", "superseded",
})

MEMORY_KINDS: tuple[str, ...] = ("habit", "interest", "experience", "relationship")
EVIDENCE_FLAGS: tuple[str, ...] = (
    "actual_result", "user_feedback", "both_party", "real_contact",
)
GROWTH_TERMINAL_STATUSES: frozenset[str] = frozenset({"withdrawn", "superseded"})
GROWTH_ACTIVE_STATUSES: frozenset[str] = frozenset({"pending", "confirmed", "promoted"})
USAGE_TYPES: tuple[str, ...] = ("read", "adopted", "result", "feedback")

#: Growth promotion gate: independent experiences + at least one evidence flag
#: of the matching class (host memory/growth.py:122-128).
PROMOTION_RULES: dict[str, tuple[str, ...]] = {
    "habit": ("actual_result", "user_feedback"),
    "experience": ("actual_result", "user_feedback"),
    "interest": ("real_contact",),
    "relationship": ("both_party",),
}

#: Host evidence constants (config/memory_settings.py:30-56,1014-1021).
EVIDENCE_REIN_HALF_LIFE_DAYS = 30.0
EVIDENCE_DISP_HALF_LIFE_DAYS = 180.0
USER_CONFIRM_DELTA = 1.0
USER_REBUT_DELTA = 1.0
IGNORED_REINFORCEMENT_DELTA = -0.2
#: Host initial reinforcement seeds by MAX source-fact importance.
INITIAL_REINFORCEMENT: tuple[tuple[int, float], ...] = (
    (10, 0.8), (9, 0.6), (8, 0.4), (7, 0.2),
)

MAX_REFLECTION_TEXT = 600
MAX_CLAIM_CHARS = 200
MAX_SITUATION_CHARS = 240
MAX_SOURCE_KEYS = 32
MAX_GROWTH_ITEMS = 6
GROWTH_BLOCK_MAX_TOKENS = 200
MAX_GROWTH_USES = 2000
SHARED_SCOPE_VALUES = frozenset({"shared", "global", "all"})
_SCENE_KEYS = ("plugin_id", "game_id", "session_id")

_FENCE = chr(96) * 3

#: Synthesis operations (design section 7.2): exactly one per batch.
REFLECTION_OPERATIONS: tuple[str, ...] = (
    "add",
    "reinforce",
    "refine",
    "supersede",
    "contradict",
    "split",
    "no_change",
)
#: Spellings a model may emit for the operations above.
OPERATION_ALIASES: dict[str, str] = {
    "create": "add",
    "new": "add",
    "append": "add",
    "merge": "refine",
    "update": "refine",
    "narrow": "refine",
    "adjust": "refine",
    "replace": "supersede",
    "retire": "supersede",
    "outdate": "supersede",
    "conflict": "contradict",
    "dispute": "contradict",
    "refute": "contradict",
    "split_into": "split",
    "none": "no_change",
    "noop": "no_change",
    "no_op": "no_change",
    "unchanged": "no_change",
    "keep": "no_change",
    "skip": "no_change",
}

#: Operations that maintain an existing row instead of creating one.
_MAINTENANCE_OPERATIONS: frozenset[str] = frozenset({
    "reinforce", "refine", "supersede", "contradict",
})
#: Rows a maintenance operation may act on. The promoted status is included on
#: purpose: a promoted understanding is still the current one, it was only
#: materialised into the persona card, so new evidence must be able to retire
#: it (and the derived persona entry along with it).
_TARGET_STATUSES: tuple[str, ...] = ("pending", "confirmed", "promoted")

MAX_TOPIC_CHARS = 64
#: How many existing understandings are shown to the model in one prompt.
MAX_PROMPT_OLD_ROWS = 8
MAX_PROMPT_STRATEGIES = 6
#: Minimum term containment for "these two rows are the same understanding".
SEMANTIC_MATCH_MIN = 0.45
#: refine keeps the previous wording so the change stays auditable.
MAX_REFINE_HISTORY = 5
#: Evidence deltas for the maintenance operations (maturity, design 7.4).
REINFORCE_DELTA = 0.3
CONTRADICTION_DELTA = 1.0
#: Retirement reasons; withdrawn stays reserved for source deletion.
SUPERSEDE_REASON = "superseded_by_new_evidence"
CONTRADICT_REASON = "contradicted_by_new_evidence"


# -- settings and outcomes ---------------------------------------------------


@dataclass(frozen=True)
class ReflectionSettings:
    """B05 policy; defaults mirror the host (B05 spec sections 1, 3, 6.1)."""

    #: Retained for configuration compatibility; one new experience is enough.
    min_facts_for_reflection: int = 1
    synthesis_facts_max: int = 20
    cooldown_minutes: int = 30
    confirmed_threshold: float = 1.0
    promoted_threshold: float = 2.0
    time_confirm_days: float = 7.0
    time_promote_days: float = 7.0
    max_synthesis_attempts: int = 5
    dead_letter_self_heal_seconds: float = 5 * 3600.0
    #: RO02: minimum interval between *normal* syntheses (design section 6.3).
    #: It is not the retry backoff and not the row cooldown: new evidence that
    #: arrives inside the interval is merged and waited for instead of
    #: producing another model call, while a retry after a failure keeps using
    #: `max_synthesis_attempts` and the backoff entry. Zero disables it.
    #:
    #: The interval is a *scheduling* rule for the background pass, so it is
    #: only applied when the caller asks for it (`synthesize(...,
    #: respect_interval=True)`). Direct callers -- explicit teaching,
    #: corrections, deletions and operator requests -- must never wait for it,
    #: which is exactly the carve-out section 6.3 describes.
    normal_interval_minutes: float = 30.0
    archive_after_days: float = 30.0
    sub_zero_archive_days: int = 14
    render_max_tokens: int = 2000
    growth_min_support: int = 2
    synthesis_output_tokens: int = 4096
    synthesis_input_chars: int = 24000
    synthesis_timeout_s: float = 110.0
    model_tier: str = "summary"
    powerful_memory: bool = True


@dataclass(frozen=True)
class SynthesisOutcome:
    """One synthesis attempt.

    `status` is one of `completed`, `duplicate`, `skipped`, `failed`,
    `stale`. A failed attempt increments the full-set backoff and never writes
    a reflection; `stale` means the deletion/correction barrier moved while the
    model was running and the result was discarded.
    """

    status: str
    reflection_ids: tuple[str, ...] = ()
    absorbed_fact_ids: tuple[str, ...] = ()
    growth_item_ids: tuple[str, ...] = ()
    reason: str | None = None
    operation: str | None = None


@dataclass(frozen=True)
class PromotionOutcome:
    """One promotion pass (score/time/growth paths share it)."""

    status: str
    promoted_ids: tuple[str, ...] = ()
    confirmed_ids: tuple[str, ...] = ()
    persona_ids: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class GrowthReceipt:
    """Result of recording one growth usage; version is the growth version."""

    item_id: str
    version: int
    duplicate: bool


# -- pure helpers ------------------------------------------------------------


def _now_iso(now: datetime) -> str:
    return now.isoformat()


def _parse_iso(value: Any, now: datetime | None = None) -> datetime | None:
    """Parse an ISO timestamp; naive values inherit `now`'s timezone."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        tz = now.tzinfo if now is not None and now.tzinfo is not None else timezone.utc
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def _age_days(value: Any, now: datetime) -> float:
    parsed = _parse_iso(value, now)
    if parsed is None:
        return 0.0
    delta = (now - parsed).total_seconds()
    if delta <= 0:
        return 0.0
    return delta / 86400.0


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


def operation_of(value: Any) -> str:
    """Normalize one model operation; an unknown spelling returns ""."""
    raw = _bounded(value, 40).lower().replace(" ", "_").replace("-", "_")
    if not raw:
        return ""
    if raw in REFLECTION_OPERATIONS:
        return raw
    return OPERATION_ALIASES.get(raw, "")


def _normalize_claim(value: Any) -> str:
    text = _bounded(value, MAX_CLAIM_CHARS)
    return " ".join(text.split())


def _normalize_situation(value: Any) -> str:
    text = _bounded(value, MAX_SITUATION_CHARS)
    return " ".join(text.split())


def _normalize_scene(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for key in _SCENE_KEYS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            out[key] = item.strip()[:64]
    scope = value.get("scope")
    if isinstance(scope, str) and scope.strip().lower() in SHARED_SCOPE_VALUES:
        out["scope"] = scope.strip().lower()
    return out


def _entry_applies_to_scene(entry: Any, scene: Any) -> bool:
    """Host `entry_applies_to_scene`: empty/shared applies everywhere."""
    if not isinstance(entry, Mapping):
        return False
    restriction = entry.get("applies_to")
    if not isinstance(restriction, Mapping):
        restriction = entry.get("source_scene")
    if not isinstance(restriction, Mapping) or not restriction:
        return True
    scope = restriction.get("scope")
    if isinstance(scope, str) and scope.strip().lower() in SHARED_SCOPE_VALUES:
        return True
    entry_scope = entry.get("scope")
    if isinstance(entry_scope, str) and entry_scope.strip().lower() in SHARED_SCOPE_VALUES:
        return True
    current = _normalize_scene(scene)
    if not current:
        return False
    for key in _SCENE_KEYS:
        wanted = restriction.get(key)
        if not isinstance(wanted, str) or not wanted.strip():
            continue
        if current.get(key) != wanted.strip()[:64]:
            return False
    return True


# -- topic / scope keys and scope-constrained semantic matching --------------

_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3040, 0x30FF),  # hiragana / katakana
    (0x3400, 0x4DBF),  # CJK extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0xAC00, 0xD7AF),  # hangul syllables
)
_TERM_SPLIT_RE = re.compile(
    "[^0-9a-z"
    "\u3040-\u30ff"
    "\u3400-\u4dbf"
    "\u4e00-\u9fff"
    "\uf900-\ufaff"
    "\uac00-\ud7af"
    "]+"
)
#: Filler terms dropped before overlap is measured; without this almost every
#: Chinese sentence would look similar to every other one.
_TOPIC_STOP_TERMS = frozenset({
    "用户", "主人", "我们", "自己", "觉得", "可能", "好像", "时候", "东西",
    "事情", "现在", "以前", "然后", "什么", "这个", "那个", "一个", "一次",
})


def _is_cjk(char: str) -> bool:
    if not char:
        return False
    code = ord(char[0])
    return any(start <= code <= end for start, end in _CJK_RANGES)


def _content_tokens(text: Any) -> set[str]:
    """CJK 2/3-grams plus Latin words, minus filler terms.

    A deliberate local copy of `domain.recall.tokenize`: `recall` imports
    `REFLECTION_TERMINAL_STATUSES` from this module (lazily, inside `_collect_pool`),
    so importing `recall` at module scope here would close a cycle. Keeping the
    tokenizer next to its only consumer also keeps the matching rule of design
    section 7.2 readable in one place.
    """
    raw = normalize_text(text)
    tokens: set[str] = set()
    for segment in _TERM_SPLIT_RE.split(raw):
        if not segment:
            continue
        if segment.isascii():
            if len(segment) >= 2:
                tokens.add(segment)
            continue
        if sum(1 for char in segment if _is_cjk(char)) > len(segment) // 2:
            for size in (2, 3):
                for index in range(len(segment) - size + 1):
                    tokens.add(segment[index:index + size])
        elif len(segment) >= 2:
            tokens.add(segment)
    return {token for token in tokens if token not in _TOPIC_STOP_TERMS}


def _semantic_overlap(left: Any, right: Any) -> float:
    """Containment of the shorter term set in the longer one (0.0 .. 1.0)."""
    first = _content_tokens(left)
    second = _content_tokens(right)
    if not first or not second:
        return 0.0
    return len(first & second) / float(min(len(first), len(second)))


def topic_key(value: Any) -> str:
    """Stable *grouping* key for one topic; never a uniqueness constraint.

    One topic may hold several different understandings (design section 7.2:
    a stable per-topic key plus scope-constrained semantic candidate matching).
    The key only decides which rows are shown together; whether two of them
    are the *same* understanding is decided by `_semantic_overlap` plus an
    explicit `target_reflection_id` from the model.
    """
    base = "".join(normalize_text(value)[: MAX_TOPIC_CHARS * 2].split())
    if not base:
        return ""
    return "tp_" + hashlib.sha1(base.encode("utf-8", "replace")).hexdigest()[:16]


def scope_key(applies_to: Any = None) -> str:
    """Hash of the normalized scene; "" means the row applies everywhere."""
    scene = _normalize_scene(applies_to)
    if not scene:
        return ""
    seed = json.dumps(
        {key: scene[key] for key in sorted(scene)}, ensure_ascii=False,
    )
    return "sc_" + hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:16]


def _row_scope_key(row: Any) -> str:
    if not isinstance(row, Mapping):
        return ""
    value = row.get("scope_key")
    if isinstance(value, str) and value:
        return value
    return scope_key(row.get("applies_to"))


def _scope_compatible(row: Any, wanted: str) -> bool:
    """A row with no recorded scope stays eligible in every scope."""
    mine = _row_scope_key(row)
    if not mine or not wanted:
        return True
    return mine == wanted


def reflection_id_from_topic(
    topic: Any, applies_to: Any = None, text: Any = "",
) -> str:
    """Content-addressed reflection id: topic + scope + exact text hash.

    The old id hashed the batch's fact ids, so every new batch produced a new
    row and two synonymous understandings could never meet. Addressing by
    content keeps re-synthesizing the same understanding idempotent (same
    topic, same scope, same text -> same id) while leaving room for one topic
    to hold several different understandings.
    """
    seed = "|".join((
        topic_key(topic) or "tp_unspecified",
        scope_key(applies_to) or "sc_shared",
        text_hash(text),
    ))
    return "ref_" + hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:16]


def reflection_id_from_facts(source_fact_ids: Sequence[str]) -> str:
    """Deterministic host reflection id: `ref_<sha256(sorted ids + NUL)>[:16]`."""
    digest = hashlib.sha256()
    for fact_id in sorted(str(item) for item in source_fact_ids):
        digest.update(fact_id.encode("utf-8"))
        digest.update(b"\x00")
    return "ref_" + digest.hexdigest()[:16]


def growth_key(
    kind: Any, claim: Any, situation: Any = "", applies_to: Any = None,
) -> str:
    """Host growth key: sha1(kind | normalized claim | situation | scene)."""
    scene = _normalize_scene(applies_to)
    seed = json.dumps(
        {
            "kind": _bounded(kind, 32).lower(),
            "claim": _normalize_claim(claim),
            "situation": _normalize_situation(situation),
            "scene": {key: scene[key] for key in sorted(scene)},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "grow_" + hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:16]


def _initial_reinforcement(importance: Any) -> float:
    value = _as_int(importance, 5)
    for threshold, seed in INITIAL_REINFORCEMENT:
        if value >= threshold:
            return seed
    return 0.0


def _importance_of(fact: Mapping[str, Any]) -> int:
    value = fact.get("importance")
    if isinstance(value, bool) or not isinstance(value, int):
        value = 5
    return max(1, min(10, value))


def _parse_json_object(raw: Any) -> dict[str, Any] | None:
    """Parse a model JSON object; tolerate a fenced code block."""
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
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return dict(data) if isinstance(data, Mapping) else None


def _find_row(rows: Sequence[Mapping[str, Any]], row_id: Any) -> dict[str, Any] | None:
    if not isinstance(row_id, str) or not row_id:
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("id") == row_id:
            return row
    return None


def _row_source_keys(row: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field_name in ("source_event_keys", "source_keys"):
        value = row.get(field_name)
        if isinstance(value, str) and value.strip():
            keys.add(value.strip())
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    keys.add(item.strip())
    return keys


def _apply_support_removal(
    row: dict[str, Any],
    invalidated_keys: set[str],
    *,
    now_iso: str,
    invalidated_fact_ids: Sequence[str] = (),
    min_support: int = 2,
) -> bool:
    """Drop lost support and recompute count/flags/maturity by the M4 rule.

    This used to recompute `support_count = len(kept)`, i.e. one support per
    retained evidence key, while creation counted one support per independent
    experience. Counted per episode when writing and per message when deleting,
    the gate drifts twice: a row could be promoted on one conversation and then
    survive a deletion that removed its only experience. The recompute is now the
    very same routine used to build the support basis in the first place
    (`domain.growth_evidence.apply_support_removal_to_row`): the removed keys and
    deleted fact ids are dropped from the stored basis, then the count, the
    evidence flags and the row's support fields are derived from what is left.
    The deletion-barrier semantics are unchanged: no support left -> withdrawn,
    below the growth gate -> narrowed.
    """
    return apply_support_removal_to_row(
        row,
        invalidated_keys,
        invalidated_fact_ids=invalidated_fact_ids,
        now_iso=now_iso,
        min_support=min_support,
    )


# -- service -----------------------------------------------------------------


class ReflectionService:
    """Reflection/evidence/growth entry points over one character directory.

    The constructor is the frozen composition contract (adapters/composition.py);
    `persona` is the B05 PersonaService or `None` when the persona store is not
    wired (promotion then reports `blocked/persona_unavailable` instead of
    pretending success).
    """

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: ReflectionStore,
        facts: FactsView,
        barrier: DeletionBarrier,
        journal: EventJournal,
        clock: Clock,
        permit: WritePermit,
        model: ModelInvoker | None = None,
        settings: ReflectionSettings | None = None,
        persona: Any = None,
        persona_reader: Callable[[str], Any] | None = None,
        timeindex_factory: Any = None,
        recent: Any = None,
    ) -> None:
        self._roots = roots
        self._store = store
        self._facts = facts
        self._barrier = barrier
        self._journal = journal
        self._clock = clock
        self._permit = permit
        self._model = model
        self._settings = settings or ReflectionSettings()
        self._persona = persona
        self._persona_reader = persona_reader
        self._timeindex_factory = timeindex_factory
        self._recent = recent
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        # Replay handlers must be registered before any business write
        # (B05 spec section 5.3); the journal refuses unhandled event types.
        self._journal.register_handler(
            EVT_REFLECTION_EVIDENCE_UPDATED, self._apply_evidence_updated,
        )
        self._journal.register_handler(
            EVT_REFLECTION_STATE_CHANGED, self._apply_state_changed,
        )

    # -- wiring ---------------------------------------------------------

    @property
    def settings(self) -> ReflectionSettings:
        return self._settings

    def load(self, character: str) -> list[dict[str, Any]]:
        """Every stored row, terminal statuses included (test/inspection hook)."""
        return self._store.load(self._character(character))

    def _lock_for(self, character: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(character)
            if lock is None:
                lock = threading.RLock()
                self._locks[character] = lock
            return lock

    @staticmethod
    def _character(character: Any) -> str:
        value = str(character or "").strip()
        if not value:
            raise ValueError("character must be nonempty")
        return value

    # -- evidence -------------------------------------------------------

    def evidence_score(self, character: str, entry: Mapping[str, Any]) -> float:
        """Read-time decayed score (+rein -disp); `protected` returns inf.

        `sub_zero_days` is maintained by :meth:`archive` (at most one
        increment per calendar day), not computed here.
        """
        self._character(character)
        if not isinstance(entry, Mapping):
            return 0.0
        if entry.get("protected"):
            return float("inf")
        now = self._clock.now()
        reinforcement = _as_float(entry.get("reinforcement"), 0.0)
        disputation = _as_float(entry.get("disputation"), 0.0)
        if reinforcement != 0.0:
            reinforcement *= 0.5 ** (
                _age_days(entry.get("rein_last_signal_at"), now)
                / EVIDENCE_REIN_HALF_LIFE_DAYS
            )
        if disputation != 0.0:
            disputation *= 0.5 ** (
                _age_days(entry.get("disp_last_signal_at"), now)
                / EVIDENCE_DISP_HALF_LIFE_DAYS
            )
        return reinforcement - disputation

    # -- visible / block ------------------------------------------------

    def visible(
        self,
        character: str,
        *,
        subject: Any = None,
        scene: Any = None,
    ) -> list[dict[str, Any]]:
        """Renderable rows: hidden terminal states, barrier and invalid rows out."""
        character = self._character(character)
        rows = self._store.load(character)
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("status") or "") in REFLECTION_TERMINAL_STATUSES:
                continue
            if row.get("needs_rebuild"):
                continue
            if subject is not None and not row_matches_subject(row, subject):
                continue
            if scene is not None and not _entry_applies_to_scene(row, scene):
                continue
            if self._barrier.blocks_row(character, row):
                continue
            if reflection_invalidated(row):
                continue
            out.append(dict(row))
        return out

    def block(
        self,
        character: str,
        *,
        subject: Any = None,
        scene: Any = None,
        language: str = "zh",
        limit_tokens: int | None = None,
    ) -> MemoryBlock:
        """Pending+confirmed rows rendered to the host reflection budget."""
        rows = [
            row for row in self.visible(character, subject=subject, scene=scene)
            if str(row.get("status") or "") in REFLECTION_ACTIVE_STATUSES
        ]
        now = self._clock.now()
        rows.sort(
            key=lambda row: (
                -self.evidence_score(character, row),
                str(row.get("created_at") or ""),
            ),
        )
        budget = self._settings.render_max_tokens if limit_tokens is None else int(limit_tokens)
        lines: list[str] = []
        used = 0
        for row in rows:
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            cost = max(1, len(text) // 2)
            if used + cost > budget:
                break
            lines.append("- " + text)
            used += cost
        del language  # host rendering is plain text lines; label stays external
        return MemoryBlock(block="\n".join(lines), items=tuple(rows), version=0)

    # -- synthesis ------------------------------------------------------

    async def synthesize(
        self, character: str, *, respect_interval: bool = False,
    ) -> SynthesisOutcome:
        """Process one coherent topic, retaining exact sources and retry state."""
        character = self._character(character)
        settings = self._settings
        with self._lock_for(character):
            unabsorbed = self._unabsorbed_facts(character)
            if not unabsorbed:
                return SynthesisOutcome("skipped", reason="insufficient_facts")
            if respect_interval and not self._normal_synthesis_due(character, settings):
                return SynthesisOutcome("skipped", reason="normal_interval")
            rows = self._store.load(character)
            backoff = self._store.load_backoff(character)
            settled = False
            for group in self._topic_groups(unabsorbed):
                capped = self._cap_facts(group)
                topic = self._batch_topic(capped)
                scene = self._batch_scene(capped)
                relevant_rows = self._scope_rows(rows, capped)
                candidates = self._candidate_rows(relevant_rows, topic, capped)
                strategies = self._prompt_strategies(relevant_rows)
                persona = self._persona_context(character, capped)
                backoff_key = self._material_key(capped, candidates, strategies, persona)
                entry = backoff.get(backoff_key) or {}
                if self._no_change_settled(entry):
                    settled = True
                    continue
                if (_as_int(entry.get("n"), 0) >= settings.max_synthesis_attempts
                        and not self._dead_letter_healed(entry)):
                    continue
                break
            else:
                return SynthesisOutcome(
                    "no_change" if settled else "skipped",
                    reason="already_settled" if settled else "dead_letter",
                    operation="no_change" if settled else None,
                )
            if self._model is None:
                self._bump_backoff(character, backoff_key)
                return SynthesisOutcome("failed", reason="model_unavailable")
            snapshot = self._barrier.snapshot(character)
            material = self._experience_context(character, capped, persona)
            prompt = _build_synthesis_prompt(capped, candidates, strategies, material)
            # Spend the budget on this event first; related history and persona
            # detail can shrink without dropping a source id from the contract.
            while len(prompt) > settings.synthesis_input_chars:
                if material["related_experiences"]:
                    material["related_experiences"].pop()
                elif material["exchanges"]:
                    last = material["exchanges"][-1]
                    excess = len(prompt) - settings.synthesis_input_chars
                    if len(last["text"]) > excess:
                        last["text"] = last["text"][:-excess]
                    else:
                        material["exchanges"].pop()
                elif strategies:
                    strategies = strategies[:-1]
                elif candidates:
                    candidates = candidates[:-1]
                elif material["persona"]["sections"]:
                    key = next(reversed(material["persona"]["sections"]))
                    material["persona"]["sections"].pop(key)
                else:
                    break
                prompt = _build_synthesis_prompt(capped, candidates, strategies, material)
        try:
            raw = await self._model.invoke(
                settings.model_tier,
                [{"role": "system", "content": prompt}],
                max_output_tokens=settings.synthesis_output_tokens,
                timeout_s=settings.synthesis_timeout_s,
                purpose="reflection_synthesis",
            )
        except Exception as exc:  # transport failures keep this topic retryable
            self._bump_backoff(character, backoff_key)
            return SynthesisOutcome("failed", reason=f"transport:{type(exc).__name__}")
        parsed = _parse_json_object(raw)
        if parsed is None:
            self._bump_backoff(character, backoff_key)
            return SynthesisOutcome("failed", reason="parse")
        text = _bounded(parsed.get("reflection"), MAX_REFLECTION_TEXT)
        operation = operation_of(parsed.get("operation")) or ("add" if text else "no_change")
        with self._lock_for(character):
            try:
                self._barrier.assert_fresh(character, snapshot)
            except BarrierStaleError:
                return SynthesisOutcome("stale", reason="barrier_stale")
            if self._persona_context(character, capped) != persona:
                return SynthesisOutcome("stale", reason="persona_changed")
            current = {str(fact.get("id")): fact for fact in self._unabsorbed_facts(character)}
            # This includes text/identity/binding changes made while generation
            # was in flight, including corrections with no global fence bump.
            if any(current.get(str(fact["id"])) != fact for fact in capped):
                return SynthesisOutcome("stale", reason="sources_changed")
            rows = self._store.load(character)
            relevant_rows = self._scope_rows(rows, capped)
            current_candidates = self._candidate_rows(relevant_rows, topic, capped)
            if self._material_key(capped, current_candidates, self._prompt_strategies(relevant_rows), persona) != backoff_key:
                return SynthesisOutcome("stale", reason="understandings_changed")
            if operation == "no_change" or not text and operation != "split":
                self._mark_no_change(character, backoff_key)
                self._mark_normal_synthesis(character)
                return SynthesisOutcome("no_change", reason="nothing_new", operation="no_change")
            available = {str(fact["id"]): fact for fact in capped}
            selected = parsed.get("source_fact_ids", list(available))
            if operation == "split" and isinstance(parsed.get("parts"), list):
                parts = parsed["parts"]
                if any(isinstance(part, Mapping) and "source_fact_ids" in part for part in parts):
                    selected = []
                    for part in parts:
                        refs = part.get("source_fact_ids") if isinstance(part, Mapping) else None
                        if not self._valid_fact_refs(refs, available):
                            self._bump_backoff(character, backoff_key)
                            return SynthesisOutcome("failed", reason="unresolved_sources")
                        selected.extend(refs)
            if not self._valid_fact_refs(selected, available):
                self._bump_backoff(character, backoff_key)
                return SynthesisOutcome("failed", reason="unresolved_sources")
            usable = sorted(set(selected))
            facts = [available[fact_id] for fact_id in usable]
            # Identity and applicability are owned by the input, not generated.
            parsed["subject"] = str(facts[0].get("subject") or "private")
            parsed["applies_to"] = scene
            parsed["persona_version"] = persona.get("version", "")
            parsed["synthesis_key"] = backoff_key
            evidence_text = " ".join(str(fact.get("text") or "") for fact in facts)
            declared_topic = _bounded(parsed.get("topic"), MAX_TOPIC_CHARS) or topic
            target = (self._resolve_target(character, relevant_rows, parsed, evidence_text,
                                          text, declared_topic, scene, candidates)
                      if operation in _MAINTENANCE_OPERATIONS else None)
            if target is None and operation in _MAINTENANCE_OPERATIONS:
                operation = "add"
            if target is not None:
                target = _find_row(rows, str(target["id"]))
            if operation == "split":
                outcome = self._apply_split(character, rows, parsed, usable, facts, text,
                                            declared_topic, scene, backoff_key)
            elif operation in ("reinforce", "refine") and target is not None:
                outcome = self._apply_maintain(character, rows, target, operation, parsed, text,
                                               usable, facts, scene, backoff_key)
            else:
                outcome = self._apply_new(character, rows, parsed, text, operation, usable, facts,
                                          declared_topic, scene, target, backoff_key)
            if outcome.status == "completed":
                self._mark_normal_synthesis(character)
            return outcome

    # -- operation helpers ----------------------------------------------

    @staticmethod
    def _valid_fact_refs(refs: Any, available: Mapping[str, Any]) -> bool:
        return (isinstance(refs, list) and bool(refs)
                and all(isinstance(ref, str) and ref in available for ref in refs))

    def _topic_groups(self, facts: Sequence[Mapping[str, Any]]) -> list[list[dict[str, Any]]]:
        """Identity first, then explicit topic/episode links and term overlap."""
        groups: list[list[dict[str, Any]]] = []
        for fact in sorted(facts, key=lambda row: (-_importance_of(row), str(row.get("created_at") or ""), str(row["id"]))):
            for group in groups:
                seed = group[0]
                if (str(seed.get("subject") or "private") != str(fact.get("subject") or "private")
                        or scope_key(seed.get("applies_to")) != scope_key(fact.get("applies_to"))):
                    continue
                same_topic = bool(fact.get("topic_key")) and fact.get("topic_key") == seed.get("topic_key")
                same_episode = bool(fact.get("episode_id")) and fact.get("episode_id") == seed.get("episode_id")
                same_exchange = bool(_row_source_keys(seed) & _row_source_keys(fact))
                overlap = _semantic_overlap(seed.get("text"), fact.get("text"))
                if same_topic or overlap >= (0.05 if same_episode or same_exchange else 0.3):
                    group.append(dict(fact))
                    break
            else:
                groups.append([dict(fact)])
        return groups

    def _scope_rows(self, rows: Sequence[Mapping[str, Any]], facts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        subject = str(facts[0].get("subject") or "private")
        scene = self._batch_scene(facts)
        return [row for row in rows if row_matches_subject(row, subject)
                and not reflection_invalidated(row) and not row.get("needs_rebuild")
                and _entry_applies_to_scene(row, scene)]

    def _persona_context(self, character: str, facts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        snapshot = self._persona_reader(character) if self._persona_reader is not None else None
        if snapshot is None or not snapshot.available:
            return {"character": character, "version": "", "sections": {}}
        subject = str(facts[0].get("subject") or "private")
        scene = self._batch_scene(facts)
        sections = {key: text[:4000] for key, text in snapshot.projection.items()
                    if not text.startswith(EXAMPLES_BANNER)
                    and row_matches_subject(snapshot.section_scopes.get(key, {}), subject)
                    and _entry_applies_to_scene(snapshot.section_scopes.get(key, {}), scene)}
        remaining = self._settings.synthesis_input_chars // 4
        for key in list(sections):
            sections[key] = sections[key][:remaining]
            remaining -= len(sections[key])
            if not sections[key]:
                del sections[key]
        return {"character": character, "version": snapshot.version, "sections": sections}

    @staticmethod
    def _material_key(facts: Sequence[Mapping[str, Any]], candidates: Sequence[Mapping[str, Any]],
                      strategies: Sequence[Mapping[str, Any]], persona: Mapping[str, Any]) -> str:
        fact_fields = ("id", "text", "entity", "source", "evidence_kind", "source_event_keys", "source_bindings",
                       "subject", "applies_to", "created_at", "last_confirmed_at", "episode_id", "topic_key")
        row_fields = ("id", "text", "status", "claim", "situation", "source_fact_ids", "updated_at", "applies_to")
        seed = [[{key: fact.get(key) for key in fact_fields} for fact in facts],
                [{key: row.get(key) for key in row_fields} for row in candidates],
                [{key: row.get(key) for key in row_fields} for row in strategies], persona]
        return "fg02:" + hashlib.sha256(json.dumps(seed, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]

    def _experience_context(self, character: str, facts: Sequence[Mapping[str, Any]],
                            persona: Mapping[str, Any]) -> dict[str, Any]:
        subject = str(facts[0].get("subject") or "private")
        scene = self._batch_scene(facts)
        current_ids = {str(fact["id"]) for fact in facts}
        related = [row for row in self._facts.load(character)
                   if str(row.get("id")) not in current_ids and row_matches_subject(row, subject)
                   and _entry_applies_to_scene(row, scene) and not fact_invalidated(row)
                   and not self._barrier.blocks_row(character, row)
                   and max((_semantic_overlap(row.get("text"), fact.get("text")) for fact in facts), default=0) >= 0.3]
        related = self._cap_facts(related)[:5]
        corrected_keys = {key for row in self._facts.load(character)
                          if fact_invalidated(row) and row_matches_subject(row, subject)
                          and _entry_applies_to_scene(row, scene) for key in _row_source_keys(row)}
        keys = sorted({key for row in facts for key in _row_source_keys(row)})
        old_keys = sorted({key for row in related for key in _row_source_keys(row)} - set(keys))
        exchanges = []
        if self._timeindex_factory is not None:
            with self._timeindex_factory(character) as index:
                for source_keys in (keys, old_keys):
                    exchanges.extend(json.loads(row["message"]) for row in index.original_for_sources(source_keys))
        if self._recent is not None:
            indexed_keys = {key for message in exchanges for key in message_source_keys(message)}
            exchanges.extend(row for row in self._recent.load(character)
                             if message_source_keys(row) & set(keys + old_keys)
                             and not message_source_keys(row) & indexed_keys)
        visible = []
        seen = set()
        budget = self._settings.synthesis_input_chars // 2
        # Raw messages are already numbered at ingest. Legacy rows get their
        # fixed position within each source event before filtering.
        positions: dict[str, int] = {}
        for message in exchanges:
            meta = metadata_of(message)
            source = str(meta.get("source_key") or next(iter(sorted(message_source_keys(message))), ""))
            position = positions.get(source, 0)
            positions[source] = position + 1
            if "message_position" not in meta:
                message = dict(message, metadata=dict(meta, message_position=position,
                                                     message_ref=f"{source}:m{position}"))
            numbered = number_messages([message])[0]
            line = message_material(numbered)
            if (line["message_ref"] in seen or message_role(message) not in ("user", "assistant", "tool")
                    or not row_matches_subject(meta, subject) or not _entry_applies_to_scene(meta, scene)
                    or not is_extractable(meta.get("content_class"))
                    or message_source_keys(message) & corrected_keys
                    or self._barrier.blocks_message(character, message)):
                continue
            seen.add(line["message_ref"])
            size = len(line["text"])
            if budget <= 0:
                break
            line["text"] = line["text"][:budget]
            visible.append(line)
            budget -= min(size, budget)
        return {"persona": dict(persona, sections=dict(persona["sections"])), "subject": subject, "scene": scene,
                "exchanges": visible, "related_experiences": related}

    @staticmethod
    def _batch_topic(facts: Sequence[Mapping[str, Any]]) -> str:
        """Stable per-batch topic: the shared subject, else private.

        The topic is a *grouping* key, never a uniqueness constraint: one topic
        may hold several different understandings (design section 7.2). It only
        decides which existing rows are shown to the model together.
        """
        subjects = {
            _bounded(fact.get("subject"), 64)
            for fact in facts
            if _bounded(fact.get("subject"), 64)
        }
        if len(subjects) == 1:
            return subjects.pop()
        return "private"

    @staticmethod
    def _batch_scene(facts: Sequence[Mapping[str, Any]]) -> dict[str, str]:
        for fact in facts:
            scene = fact.get("applies_to")
            if isinstance(scene, Mapping) and scene:
                return _normalize_scene(scene)
        return {}

    def _candidate_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
        topic: str,
        facts: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Existing understandings worth showing the model for this batch.

        Deterministic and scope-constrained: active rows only, ranked by
        same-topic, then term overlap with the batch, then recency.
        """
        wanted_topic = topic_key(topic)
        batch_text = " ".join(str(fact.get("text") or "") for fact in facts)
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("status") or "") not in _TARGET_STATUSES:
                continue
            if row.get("needs_rebuild"):
                continue
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            same_topic = bool(wanted_topic) and (
                topic_key(row.get("topic") or "") == wanted_topic
            )
            overlap = _semantic_overlap(text, batch_text)
            if not same_topic and overlap <= 0.0:
                continue
            scored.append((
                (2.0 if same_topic else 0.0) + overlap,
                str(row.get("created_at") or ""),
                dict(row),
            ))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in scored[: MAX_PROMPT_OLD_ROWS]]

    @staticmethod
    def _prompt_strategies(
        rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Strategies already adopted: confirmed or promoted growth rows."""
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("memory_kind") not in MEMORY_KINDS:
                continue
            if str(row.get("status") or "") not in ("confirmed", "promoted"):
                continue
            if row.get("needs_rebuild"):
                continue
            out.append(dict(row))
        out.sort(
            key=lambda item: (
                -_as_int(item.get("support_count"), 0),
                str(item.get("updated_at") or ""),
            ),
        )
        return out[:MAX_PROMPT_STRATEGIES]

    def _resolve_target(
        self,
        character: str,
        rows: Sequence[Mapping[str, Any]],
        parsed: Mapping[str, Any],
        evidence_text: str,
        text: str,
        topic: str,
        scene: Mapping[str, str],
        candidates: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any] | None:
        """Pick the row an operation applies to, scope-constrained.

        The fallback scores candidates against the incoming *evidence*, never
        against the freshly written wording: supersede and contradict word the
        new understanding differently on purpose, so comparing the two
        sentences finds nothing. (The old tea/oolong probe scored exactly 0.0
        that way and the stale row was never retired.)

        An explicit `target_reflection_id` from the model is honoured only when
        the row exists, is active, is not blocked by the deletion barrier, and
        shares the topic or the scope. Otherwise a deterministic fallback picks
        the best semantic candidate, and a batch that matches nothing becomes a
        plain add.
        """
        wanted_scope = scope_key(scene)
        wanted_id = _bounded(parsed.get("target_reflection_id"), 64)
        if wanted_id:
            row = _find_row(rows, wanted_id)
            if (
                row is not None
                and str(row.get("status") or "") in _TARGET_STATUSES
                and not row.get("needs_rebuild")
                and not self._barrier.blocks_row(character, row)
            ):
                same_topic = topic_key(row.get("topic") or "") == topic_key(topic)
                if same_topic or _scope_compatible(row, wanted_scope):
                    return row
            return None
        best: tuple[float, dict[str, Any]] | None = None
        for row in candidates:
            if not isinstance(row, Mapping):
                continue
            if not _scope_compatible(row, wanted_scope):
                continue
            score = max(
                _semantic_overlap(str(row.get("text") or ""), evidence_text),
                _semantic_overlap(str(row.get("text") or ""), text),
            )
            if best is None or score > best[0]:
                best = (score, dict(row))
        # Topic grouping first: the topic key is what tells the code that two
        # differently-worded rows are about the same thing. Semantic overlap
        # only disambiguates when one topic holds several understandings.
        wanted_topic = topic_key(topic)
        same_topic: list[dict[str, Any]] = []
        if wanted_topic:
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                if str(row.get("status") or "") not in _TARGET_STATUSES:
                    continue
                if row.get("needs_rebuild"):
                    continue
                if not _scope_compatible(row, wanted_scope):
                    continue
                if topic_key(row.get("topic") or "") == wanted_topic:
                    same_topic.append(dict(row))
        if len(same_topic) == 1:
            # The batch is about one topic that already holds exactly one
            # understanding: that is the row the operation maintains, even if
            # the new wording shares almost no terms with the old one ("used to
            # drink black tea" vs "drinks oolong now" is zero-overlap text).
            return same_topic[0]
        if len(same_topic) > 1:
            ranked = sorted(
                same_topic,
                key=lambda row: -max(
                    _semantic_overlap(str(row.get("text") or ""), evidence_text),
                    _semantic_overlap(str(row.get("text") or ""), text),
                ),
            )
            return ranked[0]
        if best is None or best[0] < SEMANTIC_MATCH_MIN:
            return None
        return best[1]

    def _apply_new(
        self,
        character: str,
        rows: list[dict[str, Any]],
        parsed: Mapping[str, Any],
        text: str,
        operation: str,
        usable: Sequence[str],
        facts: Sequence[Mapping[str, Any]],
        topic: str,
        scene: Mapping[str, str],
        target: dict[str, Any] | None,
        backoff_key: str,
    ) -> SynthesisOutcome:
        """add / supersede / contradict: write a new row, retire the target."""
        scene_map = _normalize_scene(scene) or _normalize_scene(
            parsed.get("applies_to"),
        )
        reflection_id = reflection_id_from_topic(topic, scene_map, text)
        if parsed.get("subject") not in (None, "", "private"):
            reflection_id += "_" + hashlib.sha256(parsed["subject"].encode()).hexdigest()[:8]
        existing = _find_row(rows, reflection_id)
        if existing is not None:
            if parsed.get("synthesis_key") not in (existing.get("synthesis_keys") or ()):
                return self._apply_maintain(character, rows, existing, "reinforce", parsed, text,
                                            usable, facts, scene, backoff_key)
            absorbed = self._mark_absorbed(character, usable)
            self._clear_backoff(character, backoff_key)
            growth_ids = (
                (str(existing.get("id")),)
                if existing.get("memory_kind") in MEMORY_KINDS else ()
            )
            return SynthesisOutcome(
                "duplicate", reflection_ids=(reflection_id,),
                absorbed_fact_ids=tuple(absorbed),
                growth_item_ids=growth_ids,
                reason="already_synthesized",
                operation=operation,
            )
        row = self._new_reflection_row(
            reflection_id, usable, facts, parsed, text,
            topic=topic, scene=scene_map, operation=operation,
        )
        if target is not None:
            row["supersedes"] = [str(target.get("id"))]
        try:
            self._barrier.assert_row_allowed(character, row)
        except ContentBlockedError:
            return SynthesisOutcome("stale", reason="blocked_row")
        growth_ids: tuple[str, ...] = ()
        candidate = self._growth_candidate(parsed, usable, facts, text)
        appended = False
        if candidate is not None:
            growth_ids, appended = self._merge_or_append_growth(
                character, rows, row, candidate,
            )
        else:
            rows.append(row)
            appended = True
        self._store.save(character, rows)
        absorbed = self._mark_absorbed(character, usable)
        self._clear_backoff(character, backoff_key)
        # Retirement runs after the save on purpose: it re-loads the view, so
        # retiring first and writing the local rows afterwards would silently
        # drop the status change (the old row stayed active and kept rendering).
        retired: list[str] = []
        if target is not None:
            retired = self._retire_target(character, target, operation, row)
        return SynthesisOutcome(
            "completed",
            reflection_ids=(reflection_id,) if appended else (),
            absorbed_fact_ids=tuple(absorbed),
            growth_item_ids=growth_ids,
            reason=None if not retired else "retired:" + ",".join(retired),
            operation=operation,
        )

    def _apply_maintain(
        self,
        character: str,
        rows: list[dict[str, Any]],
        target: dict[str, Any],
        operation: str,
        parsed: Mapping[str, Any],
        text: str,
        usable: Sequence[str],
        facts: Sequence[Mapping[str, Any]],
        scene: Mapping[str, str],
        backoff_key: str,
    ) -> SynthesisOutcome:
        """reinforce (same understanding, more support) or refine (narrow it)."""
        target_id = str(target.get("id"))
        if backoff_key in (target.get("synthesis_keys") or ()):
            absorbed = self._mark_absorbed(character, usable)
            self._clear_backoff(character, backoff_key)
            return SynthesisOutcome("duplicate", reflection_ids=(target_id,),
                                    absorbed_fact_ids=tuple(absorbed), operation=operation)
        now_iso = _now_iso(self._clock.now())
        sources = [
            str(item) for item in target.get("source_fact_ids") or ()
            if isinstance(item, str)
        ]
        for fact_id in usable:
            if fact_id not in sources and len(sources) < MAX_SOURCE_KEYS:
                sources.append(fact_id)
        target["source_fact_ids"] = sources
        if operation == "refine":
            # Keep the previous wording so the change stays auditable.
            history: list[str] = [
                str(item) for item in target.get("previous_texts") or ()
                if isinstance(item, str)
            ]
            old_text = str(target.get("text") or "")
            if old_text and old_text != text:
                history.append(old_text)
            target["previous_texts"] = history[-MAX_REFINE_HISTORY:]
            target["text"] = text
            target["refined_at"] = now_iso
            narrowed = _normalize_scene(parsed.get("applies_to")) or _normalize_scene(scene)
            if narrowed:
                target["applies_to"] = narrowed
                target["scope_key"] = scope_key(narrowed)
            new_topic = _bounded(parsed.get("topic"), MAX_TOPIC_CHARS)
            if new_topic:
                target["topic"] = new_topic
        target["synthesis_keys"] = (list(target.get("synthesis_keys") or ()) + [backoff_key])[-32:]
        target["persona_version"] = parsed.get("persona_version", "")
        before_support = summarize_basis(support_basis_from_row(target))["support_count"]
        merged = merge_basis(support_basis_from_row(target), support_basis_from_facts(list(facts)))
        target.update(summarize_basis(merged))
        target["source_event_keys"] = sorted(set(target.get("source_event_keys") or ())
                                             | {key for fact in facts for key in _row_source_keys(fact)})
        if (target.get("memory_kind") in MEMORY_KINDS
                and str(target.get("status") or "") in ("pending", "narrowed")
                and self._growth_promotion_ready(target)):
            target["status"] = "confirmed"
            target["confirmed_at"] = now_iso
        target["updated_at"] = now_iso
        self._store.save(character, rows)
        self._record_evidence(
            character,
            target_id,
            {
                "reinforcement": _as_float(target.get("reinforcement"), 0.0)
                + (REINFORCE_DELTA if target["support_count"] > before_support else 0.0),
                "rein_last_signal_at": now_iso,
            },
        )
        absorbed = self._mark_absorbed(character, usable)
        self._clear_backoff(character, backoff_key)
        growth_ids = (
            (target_id,) if target.get("memory_kind") in MEMORY_KINDS else ()
        )
        return SynthesisOutcome(
            "completed",
            reflection_ids=(target_id,),
            absorbed_fact_ids=tuple(absorbed),
            growth_item_ids=growth_ids,
            operation=operation,
        )

    def _apply_split(
        self,
        character: str,
        rows: list[dict[str, Any]],
        parsed: Mapping[str, Any],
        usable: Sequence[str],
        facts: Sequence[Mapping[str, Any]],
        text: str,
        topic: str,
        scene: Mapping[str, str],
        backoff_key: str,
    ) -> SynthesisOutcome:
        """Split one over-broad understanding into independently supported rows.

        Every part keeps its *own* source_fact_ids: parts must not share one
        support set, otherwise per-experience counting would count the same
        experience once per part (design section 7.4).
        """
        parts: list[tuple[str, dict[str, str]]] = []
        explicit_sources: list[list[str] | None] = []
        raw_parts = parsed.get("parts")
        if isinstance(raw_parts, (list, tuple)):
            for item in raw_parts:
                if isinstance(item, Mapping):
                    part_text = _bounded(item.get("reflection"), MAX_REFLECTION_TEXT)
                    part_scene = _normalize_scene(item.get("applies_to")) or dict(scene)
                else:
                    part_text = _bounded(item, MAX_REFLECTION_TEXT)
                    part_scene = dict(scene)
                if part_text:
                    parts.append((part_text, part_scene))
                    explicit_sources.append(item.get("source_fact_ids") if isinstance(item, Mapping) else None)
        if len(parts) < 2:
            return SynthesisOutcome(
                "skipped", reason="split_needs_two_parts", operation="split",
            )
        fact_by_id = {
            str(fact.get("id")): fact for fact in facts
            if isinstance(fact.get("id"), str)
        }
        assigned: list[list[str]] = [[] for _ in parts]
        if all(refs is not None for refs in explicit_sources):
            assigned = [list(dict.fromkeys(refs)) for refs in explicit_sources]
        for fact_id in (() if all(refs is not None for refs in explicit_sources) else usable):
            fact = fact_by_id.get(fact_id)
            fact_text = str((fact or {}).get("text") or "")
            best_index = 0
            best_score = -1.0
            for index, (part_text, _scene) in enumerate(parts):
                score = _semantic_overlap(fact_text, part_text)
                if score > best_score:
                    best_score = score
                    best_index = index
            assigned[best_index].append(fact_id)
        created: list[str] = []
        absorbed: list[str] = []
        for index, (part_text, part_scene) in enumerate(parts):
            part_sources = list(assigned[index])
            if not part_sources:
                # An unsupported part is skipped, never given a borrowed set.
                continue
            part_id = reflection_id_from_topic(topic, part_scene, part_text)
            if parsed.get("subject") not in (None, "", "private"):
                part_id += "_" + hashlib.sha256(parsed["subject"].encode()).hexdigest()[:8]
            existing = _find_row(rows, part_id)
            if existing is not None:
                if backoff_key in (existing.get("synthesis_keys") or ()):
                    created.append(part_id)
                    absorbed.extend(part_sources)
                continue
            part_facts = [
                fact_by_id[item] for item in part_sources if item in fact_by_id
            ]
            part_parsed = dict(parsed, applies_to=part_scene)
            part_data = raw_parts[index]
            if isinstance(part_data, Mapping):
                for key in ("entity", "claim", "situation", "memory_kind"):
                    if key in part_data:
                        part_parsed[key] = part_data[key]
            row = self._new_reflection_row(
                part_id, part_sources, part_facts, part_parsed, part_text,
                topic=topic, scene=part_scene, operation="split",
            )
            try:
                self._barrier.assert_row_allowed(character, row)
            except ContentBlockedError:
                continue
            rows.append(row)
            created.append(part_id)
            absorbed.extend(part_sources)
        if not created:
            return SynthesisOutcome(
                "skipped", reason="split_no_eligible_parts", operation="split",
            )
        self._store.save(character, rows)
        marked = self._mark_absorbed(character, sorted(set(absorbed)))
        self._clear_backoff(character, backoff_key)
        target = _find_row(rows, str(parsed.get("target_reflection_id") or ""))
        if target is not None and str(target["id"]) not in created:
            self._retire_target(character, target, "split", {"id": created[0]})
        return SynthesisOutcome(
            "completed",
            reflection_ids=tuple(created),
            absorbed_fact_ids=tuple(marked),
            reason="split:" + str(len(created)),
            operation="split",
        )

    def _retire_target(
        self,
        character: str,
        target: dict[str, Any],
        operation: str,
        new_row: Mapping[str, Any],
    ) -> list[str]:
        """Retire an understanding the new evidence replaced or contradicted.

        `superseded` is used, never `withdrawn`: `withdrawn` means "the sources went
        away" and is what the deletion/correction barrier writes, whereas this
        is a content judgement recorded next to the new row. A contradicting
        batch also raises `disputation`, so the maturity model keeps the
        counter-evidence instead of only flipping a status.
        """
        target_id = str(target.get("id"))
        new_id = str(new_row.get("id") or "")
        reason = (
            CONTRADICT_REASON if operation == "contradict" else SUPERSEDE_REASON
        )
        if operation == "contradict":
            self._record_evidence(
                character,
                target_id,
                {
                    "disputation": _as_float(target.get("disputation"), 0.0)
                    + CONTRADICTION_DELTA,
                    "disp_last_signal_at": _now_iso(self._clock.now()),
                },
            )
        status = str(target.get("status") or "pending")
        if status in _TARGET_STATUSES:
            self._retire_superseded(character, target_id, status, new_id, reason)
        retired = [target_id]
        # Already materialised into the persona card? Then the derived persona
        # entry has to be retracted too, otherwise the retired understanding
        # keeps shaping replies through the persona block.
        retired.extend(self._retract_persona_for_reflection(
            character, target_id, reason,
        ))
        if target.get("memory_kind") in MEMORY_KINDS:
            self._retire_growth_target(character, target_id, reason)
            retired.extend(self._retract_persona_for_growth(
                character, target_id, reason,
            ))
        return retired

    def _retire_superseded(
        self,
        character: str,
        reflection_id: str,
        from_status: str,
        superseded_by: str,
        reason: str,
    ) -> bool:
        """Status transition to `superseded`, journaled like any state change."""
        now_iso = _now_iso(self._clock.now())
        payload: dict[str, Any] = {
            "reflection_id": reflection_id,
            "from": from_status,
            "to": "superseded",
            "ts": now_iso,
            "reason": reason,
            "superseded_by": superseded_by,
        }
        applied = {"ok": False}

        def _mutate(view: Any) -> None:
            row = _find_row(view, reflection_id)
            if row is None or str(row.get("status") or "") != from_status:
                return
            row["status"] = "superseded"
            row["superseded_at"] = now_iso
            row["superseded_by"] = superseded_by
            row["superseded_reason"] = reason
            applied["ok"] = True

        self._journal.record_and_save(
            character,
            EVT_REFLECTION_STATE_CHANGED,
            payload,
            load_view=self._store.load,
            mutate_view=_mutate,
            save_view=self._store.save,
        )
        return bool(applied["ok"])

    def _retire_growth_target(
        self, character: str, item_id: str, reason: str,
    ) -> None:
        """Withdraw the derived growth item so it stops rendering as a strategy."""
        rows = self._store.load(character)
        row = _find_row(rows, item_id)
        if row is None or row.get("memory_kind") not in MEMORY_KINDS:
            return
        if str(row.get("status") or "") in GROWTH_TERMINAL_STATUSES:
            return
        now_iso = _now_iso(self._clock.now())
        row["status"] = "withdrawn"
        row["withdrawn_at"] = now_iso
        row["withdrawn_reason"] = reason
        row["updated_at"] = now_iso
        self._store.save(character, rows)
        doc = self._store.load_growth_uses(character)
        doc["version"] = _as_int(doc.get("version"), 0) + 1
        self._store.save_growth_uses(character, doc)

    # -- no_change marker -------------------------------------------------

    @staticmethod
    def _no_change_settled(entry: Mapping[str, Any]) -> bool:
        """Whether this exact evidence batch already reported no_change.

        The marker rides on the synthesis backoff entry, whose key is a hash of
        the full unabsorbed fact id set. That is the whole trick: new evidence
        changes the id set, so the key changes, so the entry (and its marker)
        no longer applies and the batch is evaluated again. It also means the
        marker needs no extra file, no extra replay handler and no special
        deletion invalidation: once the sources are gone the key no longer
        matches anything.
        """
        return bool(entry.get("nc"))

    def _mark_no_change(self, character: str, key: str) -> None:
        """Record "this batch is settled"; deliberately keeps n at zero."""
        doc = self._store.load_backoff(character)
        doc[key] = {
            "n": 0,
            "at": None,
            "nc": key,
            "nc_at": _now_iso(self._clock.now()),
            "result": "no_change",
        }
        self._store.save_backoff(character, doc)

    def _unabsorbed_facts(self, character: str) -> list[dict[str, Any]]:
        facts = self._facts.load(character)
        out: list[dict[str, Any]] = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            if is_friend_subject(fact.get("subject")):
                continue
            fact_id = fact.get("id")
            if not isinstance(fact_id, str) or not fact_id:
                continue
            if fact.get("absorbed"):
                continue
            if fact_invalidated(fact):
                continue
            if self._barrier.blocks_row(character, fact):
                continue
            if self._barrier.blocks_target(character, "fact", fact_id):
                continue
            out.append(fact)
        return out

    def _cap_facts(self, facts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(
            facts,
            key=lambda fact: (
                -_importance_of(fact),
                str(fact.get("created_at") or ""),
            ),
        )
        selected = []
        used = 0
        for fact in ordered[: self._settings.synthesis_facts_max]:
            size = len(str(fact.get("text") or "")[:600]) + len(str(fact.get("message_refs") or [])[:500]) + 200
            if selected and used + size > self._settings.synthesis_input_chars // 2:
                break
            selected.append(dict(fact))
            used += size
        return selected

    def _new_reflection_row(
        self,
        reflection_id: str,
        source_fact_ids: Sequence[str],
        facts: Sequence[Mapping[str, Any]],
        parsed: Mapping[str, Any],
        text: str,
        *,
        topic: Any = None,
        scene: Any = None,
        operation: str | None = None,
    ) -> dict[str, Any]:
        now = self._clock.now()
        now_iso = _now_iso(now)
        max_importance = max(
            (_importance_of(fact) for fact in facts), default=5,
        )
        initial = _initial_reinforcement(max_importance)
        entity = parsed.get("entity")
        if entity not in ("master", "neko", "relationship"):
            entity = "relationship"
        relation_type = parsed.get("relation_type")
        if not isinstance(relation_type, str):
            relation_type = None
        temporal_scope = parsed.get("temporal_scope")
        if not isinstance(temporal_scope, str):
            temporal_scope = None
        subject = parsed.get("subject")
        if not isinstance(subject, str):
            subject = None
        row: dict[str, Any] = {
            "id": reflection_id,
            "text": text,
            "entity": entity,
            "status": "pending",
            "source_fact_ids": list(source_fact_ids),
            "created_at": now_iso,
            "feedback": None,
            "next_eligible_at": (
                now + timedelta(minutes=self._settings.cooldown_minutes)
            ).isoformat(),
            "reinforcement": initial,
            "rein_last_signal_at": now_iso if initial > 0 else None,
            "disputation": 0.0,
            "disp_last_signal_at": None,
            "sub_zero_days": 0,
            "sub_zero_last_increment_date": None,
            "user_fact_reinforce_count": 0,
            "absorbed_into": None,
            "last_promote_attempt_at": None,
            "promote_attempt_count": 0,
            "promote_blocked_reason": None,
            "recent_mentions": [],
            "suppress": False,
            "suppressed_at": None,
            "protected": False,
            "relation_type": relation_type,
            "temporal_scope": temporal_scope,
            "subject": subject,
            "event_when_raw": None,
            "event_start_at": now_iso,
            "event_end_at": None,
            "schema_version": 2,
            "embedding": None,
            "embedding_text_sha256": None,
            "embedding_model_id": None,
            "last_refine_cluster_hash": None,
            "last_refine_at": None,
            "topic": _bounded(parsed.get("topic"), MAX_TOPIC_CHARS) or (
                _bounded(topic, MAX_TOPIC_CHARS) or None
            ),
            "topic_key": topic_key(
                _bounded(parsed.get("topic"), MAX_TOPIC_CHARS) or topic
            ),
            "scope_key": scope_key(scene),
            "applies_to": _normalize_scene(scene),
            "operation": operation,
            "superseded_by": None,
            "superseded_at": None,
            "superseded_reason": None,
            "supersedes": None,
            "previous_texts": [],
            "persona_version": parsed.get("persona_version", ""),
            "synthesis_keys": [parsed["synthesis_key"]] if parsed.get("synthesis_key") else [],
            "source_event_keys": sorted({key for fact in facts for key in _row_source_keys(fact)}),
            **summarize_basis(support_basis_from_facts(facts)),
        }
        for key in (
            "speaker_id", "speaker_trust", "speaker_label",
            "speaker_provenance_mixed",
        ):
            for fact in facts:
                value = fact.get(key)
                if value is not None:
                    row[key] = value
                    break
        return row

    def _growth_candidate(
        self,
        parsed: Mapping[str, Any],
        source_fact_ids: Sequence[str],
        facts: Sequence[Mapping[str, Any]],
        text: str,
    ) -> dict[str, Any] | None:
        raw_kind = parsed.get("memory_kind")
        kind = raw_kind.strip().lower() if isinstance(raw_kind, str) else ""
        if kind not in MEMORY_KINDS:
            return None
        claim = _normalize_claim(parsed.get("claim")) or _normalize_claim(text)
        if not claim:
            return None
        situation = _normalize_situation(parsed.get("situation"))
        raw_scene = parsed.get("applies_to")
        if isinstance(raw_scene, Mapping):
            applies_to = _normalize_scene(raw_scene)
        else:
            applies_to = {}
            for fact in facts:
                scene = fact.get("applies_to")
                if isinstance(scene, Mapping) and scene:
                    applies_to = _normalize_scene(scene)
                    break
        # Support is one slot per *independent experience* (episode), not one
        # per message: five facts of one conversation are one experience
        # (design section 7.4, G09).
        basis = support_basis_from_facts(list(facts))
        summary = summarize_basis(basis)
        return {
            "kind": kind,
            "claim": claim,
            "situation": situation,
            "applies_to": applies_to,
            "growth_key": growth_key(kind, claim, situation, applies_to),
            "source_event_keys": list(summary["support_event_keys"]),
            "source_fact_ids": [str(item) for item in source_fact_ids],
            "support_basis": basis,
        }

    def _merge_or_append_growth(
        self,
        character: str,
        rows: list[dict[str, Any]],
        row: dict[str, Any],
        candidate: Mapping[str, Any],
    ) -> tuple[tuple[str, ...], bool]:
        """Merge into an existing growth row (same key) or append the new row."""
        key = str(candidate["growth_key"])
        target: dict[str, Any] | None = None
        for item in rows:
            if (
                isinstance(item, dict)
                and item.get("memory_kind") in MEMORY_KINDS
                and item.get("growth_key") == key
                and row_matches_subject(item, row.get("subject"))
                and str(item.get("status") or "") not in GROWTH_TERMINAL_STATUSES
            ):
                target = item
                break
        now_iso = _now_iso(self._clock.now())
        basis = list(candidate.get("support_basis") or ())
        if target is None:
            row["memory_kind"] = candidate["kind"]
            row["claim"] = candidate["claim"]
            row["situation"] = candidate["situation"]
            row["applies_to"] = dict(candidate.get("applies_to") or {})
            row["growth_key"] = key
            # The evidence flags come from each fact's real `evidence_kind`, so
            # a batch of bare user statements can no longer satisfy the
            # `actual_result`/`user_feedback` gates on its own.
            row.update(summarize_basis(basis))
            row["valid_from"] = now_iso
            row["superseded_by"] = None
            rows.append(row)
            return (str(row["id"]),), True
        # Merge by experience: re-synthesizing the same evidence adds nothing,
        # a genuinely new conversation adds exactly one support.
        merged = merge_basis(support_basis_from_row(target), basis)
        after = summarize_basis(merged)
        # Only *new fact ids* count as a change: a retry that re-synthesizes the
        # same evidence must not touch the row (design 7.4, "re-synthesis adds no
        # support"), while a new independent experience must.
        source_facts = [
            str(item) for item in target.get("source_fact_ids") or ()
            if isinstance(item, str)
        ]
        fresh = [
            str(item) for item in candidate.get("source_fact_ids") or ()
            if isinstance(item, str) and item not in source_facts
        ]
        if fresh:
            for fact_id in fresh:
                if len(source_facts) < MAX_SOURCE_KEYS:
                    source_facts.append(fact_id)
            target["source_fact_ids"] = source_facts
            target.update(after)
            target["updated_at"] = now_iso
            if (
                str(target.get("status") or "") in ("pending", "narrowed")
                and self._growth_promotion_ready(target)
            ):
                target["status"] = "confirmed"
                target["confirmed_at"] = now_iso
        del character
        return (str(target.get("id")),), False

    # -- promotion ------------------------------------------------------

    async def promote(self, character: str) -> PromotionOutcome:
        """Advance pending/confirmed rows via score, time or growth rules."""
        character = self._character(character)
        settings = self._settings
        confirmed: list[str] = []
        promoted: list[str] = []
        persona_ids: list[str] = []
        errors = 0
        persona_needed = False
        with self._lock_for(character):
            rows = self._store.load(character)
            now = self._clock.now()
            now_iso = _now_iso(now)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if row.get("memory_kind") not in MEMORY_KINDS:
                    continue
                status = str(row.get("status") or "")
                row_id = row.get("id")
                if not isinstance(row_id, str) or not row_id:
                    continue
                if status in ("pending", "narrowed"):
                    if self._growth_promotion_ready(row) and self._change_status(
                        character, row_id, status, "confirmed",
                        reason="growth_threshold",
                    ):
                        confirmed.append(row_id)
                        status = "confirmed"
                if status != "confirmed":
                    continue
                if not _entry_applies_to_scene(row, None):
                    continue
                persona_needed = True
                if self._persona is None:
                    continue
                if self._barrier.blocks_row(character, row):
                    continue
                try:
                    persona_id = self._persona.add_growth(
                        character, self._growth_persona_row(row, now_iso),
                    )
                except Exception:  # noqa: BLE001 - keep confirmed for a retry
                    errors += 1
                    continue
                if not isinstance(persona_id, str) or not persona_id:
                    errors += 1
                    continue
                if self._change_status(
                    character, row_id, "confirmed", "promoted",
                    absorbed_into=persona_id,
                ):
                    promoted.append(row_id)
                    persona_ids.append(persona_id)

            # Non-growth rows: score-driven when powerful memory is on, the
            # host's zero-LLM time-driven fallback when it is off.
            rows = self._store.load(character)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if row.get("memory_kind") in MEMORY_KINDS:
                    continue
                status = str(row.get("status") or "")
                row_id = row.get("id")
                if not isinstance(row_id, str) or not row_id:
                    continue
                if self._barrier.blocks_row(character, row):
                    continue
                if settings.powerful_memory:
                    if status == "pending":
                        if self.evidence_score(character, row) >= settings.confirmed_threshold:
                            if self._change_status(
                                character, row_id, "pending", "confirmed",
                                reason="evidence_confirmed",
                            ):
                                confirmed.append(row_id)
                    elif status == "confirmed":
                        if self.evidence_score(character, row) >= settings.promoted_threshold:
                            persona_needed = True
                            if self._persona is None:
                                continue
                            try:
                                persona_id = self._persona.add_reflection(
                                    character,
                                    self._reflection_persona_row(row, now_iso),
                                )
                            except Exception:  # noqa: BLE001
                                errors += 1
                                continue
                            if not isinstance(persona_id, str) or not persona_id:
                                errors += 1
                                continue
                            if self._change_status(
                                character, row_id, "confirmed", "promoted",
                                absorbed_into=persona_id,
                            ):
                                promoted.append(row_id)
                                persona_ids.append(persona_id)
                else:
                    if status == "pending":
                        age = _age_days(row.get("created_at"), now)
                        if age >= settings.time_confirm_days:
                            if self._change_status(
                                character, row_id, "pending", "confirmed",
                                reason="time_driven",
                            ):
                                confirmed.append(row_id)
                    elif status == "confirmed":
                        age = _age_days(row.get("confirmed_at"), now)
                        if age >= settings.time_promote_days:
                            persona_needed = True
                            if self._persona is None:
                                continue
                            try:
                                persona_id = self._persona.add_reflection(
                                    character,
                                    self._reflection_persona_row(row, now_iso),
                                )
                            except Exception:  # noqa: BLE001
                                errors += 1
                                continue
                            if not isinstance(persona_id, str) or not persona_id:
                                errors += 1
                                continue
                            if self._change_status(
                                character, row_id, "confirmed", "promoted",
                                absorbed_into=persona_id,
                            ):
                                promoted.append(row_id)
                                persona_ids.append(persona_id)
        if persona_needed and self._persona is None:
            status, reason = "blocked", "persona_unavailable"
        elif errors:
            status, reason = "failed", "persona_error"
        else:
            status, reason = "completed", None
        return PromotionOutcome(
            status,
            promoted_ids=tuple(promoted),
            confirmed_ids=tuple(confirmed),
            persona_ids=tuple(persona_ids),
            reason=reason,
        )

    def _growth_promotion_ready(self, row: Mapping[str, Any]) -> bool:
        """Gate: independent experiences + one evidence flag of the right kind.

        The count and the flags are read from the row's support basis rather than
        from a stored counter, so a legacy row cannot pass on a `support_count`
        that was inflated per message, and a row whose evidence was filed under a
        made-up `actual_result` cannot pass either.
        """
        kind = str(row.get("memory_kind") or "")
        if kind not in MEMORY_KINDS:
            return False
        maturity = maturity_of_row(
            row,
            promotion_rules=PROMOTION_RULES,
            min_support=int(self._settings.growth_min_support),
        )
        return bool(maturity["promotion_ready"])

    @staticmethod
    def _reflection_persona_row(
        row: Mapping[str, Any], now_iso: str,
    ) -> dict[str, Any]:
        reflection_id = str(row.get("id") or "")
        # PersonaService.add_reflection owns the prom_<reflection_id> id and
        # derives source_id from this row; passing a prefixed id would produce
        # prom_prom_ref_... (integration defect found by the Lead).
        return {
            "id": reflection_id,
            "entity": row.get("entity") or "relationship",
            "text": str(row.get("text") or ""),
            "source": "reflection",
            "source_id": reflection_id,
            "merged_from_ids": [],
            "protected": False,
            "suppress": False,
            "reinforcement": _as_float(row.get("reinforcement"), 0.0),
            "disputation": _as_float(row.get("disputation"), 0.0),
            "created_at": now_iso,
            "updated_at": now_iso,
            "version_history": [],
        }

    @staticmethod
    def _growth_persona_row(
        row: Mapping[str, Any], now_iso: str,
    ) -> dict[str, Any]:
        item_id = str(row.get("id") or "")
        claim = _normalize_claim(row.get("claim") or row.get("text"))
        kind = str(row.get("memory_kind") or "")
        entity = "relationship" if kind == "relationship" else "neko"
        # PersonaService.add_growth derives
        # prom_growth_<sha1(item_id|claim)[:16]> from this row; it reads the
        # item id from "id"/"item_id", so the raw reflection id must be passed.
        return {
            "id": item_id,
            "entity": entity,
            "text": claim,
            "source": "growth",
            "source_id": item_id,
            "merged_from_ids": [],
            "applies_to": dict(row.get("applies_to") or {}),
            "created_at": now_iso,
            "updated_at": now_iso,
            "version_history": [{
                "text": claim,
                "replaced_at": now_iso,
                "reason": "growth_promotion",
                "source_reflection_ids": [item_id],
            }],
            "growth_key": row.get("growth_key"),
        }

    # -- surfaced / feedback -------------------------------------------

    async def record_surfaced(
        self, character: str, reflection_id: str,
    ) -> dict[str, Any]:
        """Record a delivered proactive mention and refresh its cooldown."""
        character = self._character(character)
        reflection_id = str(reflection_id or "").strip()
        if not reflection_id:
            raise ValueError("reflection_id must be nonempty")
        with self._lock_for(character):
            rows = self._store.load(character)
            row = _find_row(rows, reflection_id)
            if row is None:
                return {"status": "skipped", "reason": "unknown_reflection"}
            if self._barrier.blocks_row(character, row):
                return {"status": "skipped", "reason": "deleted"}
            if str(row.get("status") or "") in REFLECTION_TERMINAL_STATUSES:
                return {"status": "skipped", "reason": "terminal"}
            now = self._clock.now()
            now_iso = _now_iso(now)
            next_eligible = (
                now + timedelta(minutes=self._settings.cooldown_minutes)
            ).isoformat()
            row["next_eligible_at"] = next_eligible
            self._store.save(character, rows)
            surfaced = self._store.load_surfaced(character)
            text = str(row.get("text") or "")
            existing = next(
                (item for item in surfaced
                 if item.get("reflection_id") == reflection_id),
                None,
            )
            if existing is None:
                surfaced.append({
                    "reflection_id": reflection_id,
                    "text": text,
                    "surfaced_at": now_iso,
                    "feedback": None,
                })
            else:
                existing["surfaced_at"] = now_iso
                existing["text"] = text
                existing["feedback"] = None
            self._store.save_surfaced(character, surfaced)
            return {
                "status": "completed",
                "reflection_id": reflection_id,
                "next_eligible_at": next_eligible,
                "surfaced": True,
            }

    async def record_feedback(
        self, character: str, reflection_id: str, feedback: str,
    ) -> dict[str, Any]:
        """Write back confirmed/denied/ignored and dispatch the evidence delta."""
        character = self._character(character)
        reflection_id = str(reflection_id or "").strip()
        if not reflection_id:
            raise ValueError("reflection_id must be nonempty")
        value = str(feedback or "").strip().lower()
        if value not in ("confirmed", "denied", "ignored"):
            raise ValueError(f"invalid feedback {feedback!r}")
        with self._lock_for(character):
            rows = self._store.load(character)
            row = _find_row(rows, reflection_id)
            if row is None:
                return {"status": "skipped", "reason": "unknown_reflection"}
            if self._barrier.blocks_row(character, row):
                return {"status": "skipped", "reason": "deleted"}
            now = self._clock.now()
            now_iso = _now_iso(now)
            surfaced = self._store.load_surfaced(character)
            target = next(
                (item for item in surfaced
                 if item.get("reflection_id") == reflection_id),
                None,
            )
            if target is None:
                surfaced.append({
                    "reflection_id": reflection_id,
                    "text": str(row.get("text") or ""),
                    "surfaced_at": now_iso,
                    "feedback": value,
                    "feedback_at": now_iso,
                })
            else:
                target["feedback"] = value
                target["feedback_at"] = now_iso
            self._store.save_surfaced(character, surfaced)
            if value == "confirmed":
                updates = {
                    "reinforcement": _as_float(row.get("reinforcement"), 0.0)
                    + USER_CONFIRM_DELTA,
                    "rein_last_signal_at": now_iso,
                }
            elif value == "denied":
                updates = {
                    "disputation": _as_float(row.get("disputation"), 0.0)
                    + USER_REBUT_DELTA,
                    "disp_last_signal_at": now_iso,
                }
            else:
                updates = {
                    "reinforcement": _as_float(row.get("reinforcement"), 0.0)
                    + IGNORED_REINFORCEMENT_DELTA,
                    "rein_last_signal_at": now_iso,
                }
            self._record_evidence(character, reflection_id, updates)
            if value == "denied":
                current = str(row.get("status") or "")
                if current in REFLECTION_ACTIVE_STATUSES:
                    self._change_status(
                        character, reflection_id, current, "denied",
                        reason="user_denied",
                    )
            return {
                "status": "completed",
                "reflection_id": reflection_id,
                "feedback": value,
            }

    # -- archive --------------------------------------------------------

    async def archive(self, character: str) -> dict[str, Any]:
        """Daily sub-zero accounting plus terminal/score-driven archival."""
        character = self._character(character)
        settings = self._settings
        archived = 0
        incremented = 0
        protected = 0
        with self._lock_for(character):
            now = self._clock.now()
            today = now.date().isoformat()
            rows = self._store.load(character)
            for row in list(rows):
                if not isinstance(row, dict):
                    continue
                row_id = row.get("id")
                if not isinstance(row_id, str) or not row_id:
                    continue
                if row.get("protected"):
                    protected += 1
                    continue
                if self._barrier.blocks_row(character, row):
                    continue
                effective = _as_int(row.get("sub_zero_days"), 0)
                if (
                    self.evidence_score(character, row) < 0.0
                    and row.get("sub_zero_last_increment_date") != today
                ):
                    if self._record_evidence(
                        character, row_id,
                        {
                            "sub_zero_days": effective + 1,
                            "sub_zero_last_increment_date": today,
                        },
                    ):
                        incremented += 1
                        effective += 1
                if effective >= settings.sub_zero_archive_days:
                    if self._archive_row(character, row_id, reason="sub_zero"):
                        archived += 1
            # Age-based archival of terminal rows (host prepare_save_reflections).
            rows = self._store.load(character)
            cutoff = now - timedelta(days=settings.archive_after_days)
            for row in list(rows):
                if not isinstance(row, dict):
                    continue
                if str(row.get("status") or "") not in ("promoted", "denied"):
                    continue
                row_id = row.get("id")
                if not isinstance(row_id, str) or not row_id:
                    continue
                if row.get("protected"):
                    continue
                if self._barrier.blocks_row(character, row):
                    continue
                timestamp = (
                    row.get("promoted_at")
                    or row.get("denied_at")
                    or row.get("created_at")
                )
                stamp = _parse_iso(timestamp, now)
                if stamp is None or stamp >= cutoff:
                    continue
                if self._archive_row(character, row_id, reason="age"):
                    archived += 1
        return {
            "status": "completed",
            "archived": archived,
            "sub_zero_incremented": incremented,
            "protected_skipped": protected,
        }

    def _archive_row(self, character: str, reflection_id: str, *, reason: str) -> bool:
        rows = self._store.load(character)
        row = _find_row(rows, reflection_id)
        if row is None or row.get("protected"):
            return False
        if self._barrier.blocks_row(character, row):
            return False
        now_iso = _now_iso(self._clock.now())
        archive_entry = dict(row)
        archive_entry["status"] = "archived"
        archive_entry["archived_at"] = now_iso
        archive_entry["archive_shard_path"] = ""
        archive_ids = {
            item.get("id") for item in self._store.load_archive(character)
            if isinstance(item, dict)
        }
        shard = ""
        if reflection_id not in archive_ids:
            shard = self._store.append_archive(character, [archive_entry])
        archive_entry["archive_shard_path"] = shard or None
        payload = {
            "reflection_id": reflection_id,
            "from": row.get("status"),
            "to": "archived",
            "ts": now_iso,
            "archived_at": now_iso,
            "archive_shard_path": shard or None,
            "entry_snapshot": archive_entry,
            "reason": reason,
        }
        def _mutate(view: Any) -> None:
            view[:] = [
                item for item in view
                if not (isinstance(item, dict) and item.get("id") == reflection_id)
            ]

        try:
            self._journal.record_and_save(
                character,
                EVT_REFLECTION_STATE_CHANGED,
                payload,
                load_view=self._store.load,
                mutate_view=_mutate,
                save_view=self._store.save,
            )
        except Exception:
            # The shard already holds the row: restore the active copy so the
            # next sweep retries the removal instead of losing the row.
            if reflection_id not in archive_ids:
                rows = self._store.load(character)
                if _find_row(rows, reflection_id) is None:
                    rows.append(dict(row))
                    self._store.save(character, rows)
            raise
        return True

    # -- growth ---------------------------------------------------------

    def growth_items(
        self, character: str, *, scene: Any = None,
    ) -> list[dict[str, Any]]:
        """Confirmed/promoted growth rows usable in this scene."""
        character = self._character(character)
        rows = self._store.load(character)
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("memory_kind") not in MEMORY_KINDS:
                continue
            if str(row.get("status") or "") not in ("confirmed", "promoted"):
                continue
            if row.get("needs_rebuild"):
                continue
            if not _entry_applies_to_scene(row, scene):
                continue
            if self._barrier.blocks_row(character, row):
                continue
            if reflection_invalidated(row):
                continue
            out.append(dict(row))
        out.sort(
            key=lambda item: (
                _as_int(item.get("support_count"), 0),
                str(item.get("updated_at") or ""),
            ),
            reverse=True,
        )
        return out[:MAX_GROWTH_ITEMS]

    def growth_block(
        self,
        character: str,
        *,
        scene: Any = None,
        language: str = "zh",
        limit_tokens: int | None = None,
    ) -> MemoryBlock:
        """Render the growth block; `version` is the growth_uses version."""
        character = self._character(character)
        items = self.growth_items(character, scene=scene)
        version = _as_int(
            self._store.load_growth_uses(character).get("version"), 0,
        )
        budget = GROWTH_BLOCK_MAX_TOKENS if limit_tokens is None else int(limit_tokens)
        block, used = _render_growth(items, language=language, limit_tokens=budget)
        return MemoryBlock(block=block, items=tuple(used), version=version)

    async def record_growth_usage(
        self,
        character: str,
        item_id: str,
        usage: str,
        *,
        source_key: str = "",
        scene: Any = None,
        outcome: str | None = None,
        feedback: str | None = None,
    ) -> GrowthReceipt:
        """Record one growth use; identity (item, version, usage, source_key)."""
        character = self._character(character)
        item_id = _bounded(item_id, 200)
        if not item_id:
            raise ValueError("item_id must be nonempty")
        value = _bounded(usage, 16).lower()
        if value not in USAGE_TYPES:
            raise ValueError(f"invalid growth usage {usage!r}")
        source = _bounded(source_key, 600)
        with self._lock_for(character):
            doc = self._store.load_growth_uses(character)
            version = _as_int(doc.get("version"), 0)
            uses = [dict(row) for row in doc.get("uses") or ()
                    if isinstance(row, Mapping)]
            # Identity is (item, version, usage, source_key); the version is the
            # growth version at the time of the use. Because every new use
            # bumps the version, the same (item, usage, source_key) tuple is
            # recognised regardless of the row's historical version, which
            # keeps a retried call idempotent (host growth.py:800-863).
            for row in uses:
                if (
                    row.get("item_id") == item_id
                    and row.get("usage") == value
                    and str(row.get("source_key") or "") == source
                ):
                    return GrowthReceipt(item_id, version, True)
            entry: dict[str, Any] = {
                "item_id": item_id,
                "version": version,
                "usage": value,
                "source_key": source,
                "scene": _normalize_scene(scene),
                "at": _now_iso(self._clock.now()),
            }
            if outcome:
                entry["outcome"] = _bounded(outcome, 200)
            if feedback:
                entry["feedback"] = _bounded(feedback, 200)
            uses.append(entry)
            if len(uses) > MAX_GROWTH_USES:
                uses = uses[-MAX_GROWTH_USES:]
            doc["uses"] = uses
            doc["version"] = version + 1
            self._store.save_growth_uses(character, doc)
            return GrowthReceipt(item_id, version + 1, False)

    async def retract_growth(
        self, character: str, item_id: str, *, reason: str = "user_retract",
    ) -> dict[str, Any]:
        """Withdraw a growth row and bump the growth version."""
        character = self._character(character)
        item_id = _bounded(item_id, 200)
        if not item_id:
            raise ValueError("item_id must be nonempty")
        with self._lock_for(character):
            rows = self._store.load(character)
            row = _find_row(rows, item_id)
            if row is None or row.get("memory_kind") not in MEMORY_KINDS:
                return {"ok": False, "reason": "unknown_growth_item"}
            version = _as_int(
                self._store.load_growth_uses(character).get("version"), 0,
            )
            if str(row.get("status") or "") == "withdrawn":
                # Idempotent retry: the persona walk is itself idempotent and
                # returns only entries it newly removed, so a crash between the
                # row write and the persona write is repaired here.
                return {
                    "ok": True,
                    "duplicate": True,
                    "item": _growth_explanation(
                        row, version,
                        promotion_rules=PROMOTION_RULES,
                        min_support=int(self._settings.growth_min_support),
                    ),
                    "version": version,
                    "persona_retracted": self._retract_persona_for_growth(
                        character, item_id, "user_retract",
                    ),
                }
            now_iso = _now_iso(self._clock.now())
            row["status"] = "withdrawn"
            row["withdrawn_at"] = now_iso
            row["withdrawn_reason"] = _bounded(reason, 200) or "user_retract"
            row["updated_at"] = now_iso
            self._store.save(character, rows)
            doc = self._store.load_growth_uses(character)
            doc["version"] = version + 1
            self._store.save_growth_uses(character, doc)
            return {
                "ok": True,
                "duplicate": False,
                "item": _growth_explanation(
                    row, version + 1,
                    promotion_rules=PROMOTION_RULES,
                    min_support=int(self._settings.growth_min_support),
                ),
                "version": version + 1,
                "persona_retracted": self._retract_persona_for_growth(
                    character, item_id, row["withdrawn_reason"],
                ),
            }

    async def reconcile_growth_support(self, character: str) -> dict[str, Any]:
        """Recompute growth support after corrections/deletions."""
        character = self._character(character)
        with self._lock_for(character):
            rows = self._store.load(character)
            invalidated_keys = set(self._barrier.state(character).index.source_keys)
            # A corrected/deleted fact also invalidates by *id*, so an experience
            # whose only fact went away is dropped even when its key is still
            # referenced by something else.
            invalidated_fact_ids: set[str] = set()
            for fact in self._facts.load(character):
                if not isinstance(fact, dict) or not fact.get("invalidated_at"):
                    continue
                fact_id = fact.get("id")
                if isinstance(fact_id, str) and fact_id:
                    invalidated_fact_ids.add(fact_id)
                for key in fact.get("source_event_keys") or ():
                    if isinstance(key, str) and key:
                        invalidated_keys.add(key)
            if not invalidated_keys and not invalidated_fact_ids:
                return {
                    "ok": True, "changed": 0, "narrowed": 0,
                    "withdrawn": 0, "persona_retracted": [],
                }
            now_iso = _now_iso(self._clock.now())
            changed = 0
            narrowed: list[str] = []
            withdrawn: list[str] = []
            persona_retracted: list[str] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if row.get("memory_kind") not in MEMORY_KINDS:
                    continue
                if str(row.get("status") or "") in GROWTH_TERMINAL_STATUSES:
                    continue
                before_status = str(row.get("status") or "")
                if not _apply_support_removal(
                    row,
                    invalidated_keys,
                    now_iso=now_iso,
                    invalidated_fact_ids=sorted(invalidated_fact_ids),
                    min_support=int(self._settings.growth_min_support),
                ):
                    continue
                changed += 1
                row_id = str(row.get("id") or "")
                if row.get("status") == "withdrawn":
                    withdrawn.append(row_id)
                elif row.get("status") == "narrowed":
                    narrowed.append(row_id)
                if before_status == "promoted" and row.get("status") in (
                    "withdrawn", "narrowed",
                ):
                    persona_retracted.extend(
                        self._retract_persona_for_growth(
                            character, row_id,
                            str(row.get("withdrawn_reason")
                                or row.get("narrowed_reason")
                                or "sources_reduced"),
                        ),
                    )
            if changed:
                self._store.save(character, rows)
                doc = self._store.load_growth_uses(character)
                doc["version"] = _as_int(doc.get("version"), 0) + 1
                self._store.save_growth_uses(character, doc)
            return {
                "ok": True,
                "changed": changed,
                "narrowed": len(narrowed),
                "withdrawn": len(withdrawn),
                "persona_retracted": persona_retracted,
            }

    def _retract_persona_for_reflection(
        self, character: str, reflection_id: str, reason: str,
    ) -> list[str]:
        """Retract persona entries derived from one reflection (idempotent)."""
        return self._retract_persona_source(
            character, "reflection", reflection_id, reason,
        )

    def _retract_persona_for_growth(
        self, character: str, item_id: str, reason: str,
    ) -> list[str]:
        """Retract persona entries derived from one growth item (idempotent)."""
        return self._retract_persona_source(
            character, "growth", item_id, reason,
        )

    def _retract_persona_source(
        self, character: str, source: str, source_id: str, reason: str,
    ) -> list[str]:
        """Mark persona entries derived from one source row as invalidated."""
        if self._persona is None:
            return []
        method = getattr(self._persona, "retract_source", None)
        if not callable(method):
            return []
        try:
            result = method(
                character, source=source, source_id=source_id,
                reason=_bounded(reason, 200) or "user_retract",
            )
        except Exception:  # noqa: BLE001 - row withdrawal is the durable part
            return []
        if not isinstance(result, (list, tuple)):
            return []
        return [str(item) for item in result if isinstance(item, str) and item]

    # -- B03 integration: deletion cleaner / correction invalidator -------

    def resolve_ids(
        self, character: str, resolved: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Deletion-engine resolve delegate: every row the request covers.

        The engine resolves before the fact step so the persona step gets
        explicit `persona_ids` even after the reflection rows it would walk are
        gone. All statuses are returned, terminal rows included: a deletion
        must cover archived/hidden rows too. Matching rules: explicit
        `reflection_ids`; `source_fact_ids` against `fact_ids`; row source
        keys against `fact_source_keys`/`source_keys`; normalized text hash;
        episode ids; and the subject when it is not private.
        """
        character = self._character(character)
        explicit = {
            str(item) for item in resolved.get("reflection_ids") or ()
            if isinstance(item, str) and item
        }
        fact_ids = {
            str(item) for item in resolved.get("fact_ids") or ()
            if isinstance(item, str) and item
        }
        source_keys = {
            str(item)
            for item in (
                resolved.get("fact_source_keys")
                or resolved.get("source_keys")
                or ()
            )
            if isinstance(item, str) and item
        }
        text_hashes = {
            str(item) for item in resolved.get("text_hashes") or ()
            if isinstance(item, str) and item
        }
        episode_ids = {
            str(item) for item in resolved.get("episode_ids") or ()
            if isinstance(item, str) and item
        }
        subject = str(resolved.get("subject") or "")
        subject_scope = subject not in ("", "private")
        matched: list[str] = []
        narrowed: list[str] = []
        for row in self._store.load(character):
            if not isinstance(row, dict):
                continue
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id:
                continue
            if row_id in explicit:
                matched.append(row_id)
                continue
            if text_hashes and text_hash(row.get("text")) in text_hashes:
                matched.append(row_id)
                continue
            if episode_ids and (episode_ids_of(row) & episode_ids):
                matched.append(row_id)
                continue
            if subject_scope and row_matches_subject(row, subject):
                matched.append(row_id)
                continue
            if self._barrier is not None and (
                self._barrier.blocks_target(character, "reflection", row_id)
                or self._barrier.blocks_row(character, row)
            ):
                matched.append(row_id)
                continue
            # A row only counts as removed when every dependency is gone; a
            # partially covered row is narrowed by the cleaner instead (its
            # derived persona entry is retracted there, not resolved here).
            sources = {
                str(item) for item in row.get("source_fact_ids") or ()
                if isinstance(item, str) and item
            }
            row_keys = _row_source_keys(row)
            deleted_sources = sources & fact_ids
            deleted_keys = row_keys & source_keys
            if not deleted_sources and not deleted_keys:
                continue
            remaining_sources = sources - fact_ids
            support = {
                str(item) for item in row.get("support_event_keys") or ()
                if isinstance(item, str) and item
            }
            remaining_support = support - source_keys
            is_growth = row.get("memory_kind") in MEMORY_KINDS
            fully_dependent = bool(sources) and not remaining_sources
            if is_growth and support and not remaining_support:
                fully_dependent = True
            if not sources and not support:
                fully_dependent = True
            if fully_dependent:
                if row_id not in matched:
                    matched.append(row_id)
            elif row_id not in narrowed:
                narrowed.append(row_id)
        return {
            "reflection_ids": matched,
            "narrowed_reflection_ids": narrowed,
        }

    def cleanup(self, character: str, resolved: Mapping[str, Any]) -> Mapping[str, Any]:
        """Deletion-engine cleaner delegate (B03 `DeletionServices.reflections`)."""
        return self._cleaner().cleanup(character, resolved)

    def invalidate(
        self,
        character: str,
        fact_ids: Sequence[str],
        *,
        invalidated_at: str,
        reason: str,
        correction_id: str,
    ) -> Mapping[str, Any]:
        """Correction-service invalidator delegate (derived invalidation walker)."""
        return self._invalidator().invalidate(
            character,
            fact_ids,
            invalidated_at=invalidated_at,
            reason=reason,
            correction_id=correction_id,
        )

    def _cleaner(self) -> "ReflectionDeletionCleaner":
        return ReflectionDeletionCleaner(
            store=self._store, barrier=self._barrier, persona=self._persona,
        )

    def _invalidator(self) -> "ReflectionInvalidator":
        return ReflectionInvalidator(store=self._store, barrier=self._barrier)

    # -- evidence/state write helpers ------------------------------------

    def _record_evidence(
        self,
        character: str,
        reflection_id: str,
        updates: Mapping[str, Any],
    ) -> bool:
        rows = self._store.load(character)
        row = _find_row(rows, reflection_id)
        if row is None:
            return False
        if self._barrier.blocks_row(character, row):
            return False
        snapshot = {
            "reinforcement": _as_float(row.get("reinforcement"), 0.0),
            "disputation": _as_float(row.get("disputation"), 0.0),
            "rein_last_signal_at": row.get("rein_last_signal_at"),
            "disp_last_signal_at": row.get("disp_last_signal_at"),
            "sub_zero_days": _as_int(row.get("sub_zero_days"), 0),
            "sub_zero_last_increment_date": row.get(
                "sub_zero_last_increment_date",
            ),
            "user_fact_reinforce_count": _as_int(
                row.get("user_fact_reinforce_count"), 0,
            ),
            "last_promote_attempt_at": row.get("last_promote_attempt_at"),
            "promote_attempt_count": _as_int(row.get("promote_attempt_count"), 0),
        }
        for key, value in updates.items():
            if key in EVIDENCE_SNAPSHOT_KEYS:
                snapshot[key] = value
        payload = {"reflection_id": reflection_id, **snapshot}
        payload["source"] = "reflection_service"

        def _mutate(view: Any) -> None:
            target = _find_row(view, reflection_id)
            if target is None:
                return
            for key in EVIDENCE_SNAPSHOT_KEYS:
                target[key] = snapshot[key]

        self._journal.record_and_save(
            character,
            EVT_REFLECTION_EVIDENCE_UPDATED,
            payload,
            load_view=self._store.load,
            mutate_view=_mutate,
            save_view=self._store.save,
        )
        return True

    def _change_status(
        self,
        character: str,
        reflection_id: str,
        from_status: str,
        to_status: str,
        *,
        absorbed_into: str | None = None,
        reason: str | None = None,
        reject_explanation: str | None = None,
    ) -> bool:
        rows = self._store.load(character)
        row = _find_row(rows, reflection_id)
        if row is None or str(row.get("status") or "") != from_status:
            return False
        if self._barrier.blocks_row(character, row):
            return False
        now_iso = _now_iso(self._clock.now())
        payload: dict[str, Any] = {
            "reflection_id": reflection_id,
            "from": from_status,
            "to": to_status,
            "ts": now_iso,
        }
        if absorbed_into is not None:
            payload["absorbed_into"] = absorbed_into
        if reason is not None:
            payload["reason"] = reason
        if reject_explanation is not None:
            payload["reject_explanation"] = reject_explanation
        applied = {"ok": False}

        def _mutate(view: Any) -> None:
            target = _find_row(view, reflection_id)
            if target is None or str(target.get("status") or "") != from_status:
                return
            target["status"] = to_status
            target[f"{to_status}_at"] = now_iso
            if absorbed_into is not None:
                target["absorbed_into"] = absorbed_into
            if reason is not None:
                if to_status == "promote_blocked":
                    target["promote_blocked_reason"] = reason
                elif to_status == "denied":
                    target["denied_reason"] = reason
            if reject_explanation is not None:
                target["reject_reason"] = reject_explanation
            applied["ok"] = True

        self._journal.record_and_save(
            character,
            EVT_REFLECTION_STATE_CHANGED,
            payload,
            load_view=self._store.load,
            mutate_view=_mutate,
            save_view=self._store.save,
        )
        return applied["ok"]

    # -- backoff --------------------------------------------------------

    #: Reserved backoff entry that records when the last *normal* synthesis
    #: happened. It lives in synth_backoff.json because that document is
    #: already journaled, capped and restored on restart -- a separate file
    #: would need its own replay handler for one timestamp. The key can never
    #: collide with a real batch: real keys are hashes of fact id sets, and
    #: this one is a fixed marker that `reflection_id_from_facts` never emits.
    _NORMAL_INTERVAL_KEY = "__normal_interval__"

    def _normal_synthesis_due(self, character: str, settings: Any) -> bool:
        """Whether the normal synthesis interval has elapsed.

        Retry backoff is deliberately not consulted here: a failed batch is
        retried as soon as the caller is allowed to, and its attempt count is
        tracked on its own entry.
        """
        minutes = float(getattr(settings, "normal_interval_minutes", 0.0) or 0.0)
        if minutes <= 0.0:
            return True
        doc = self._store.load_backoff(character)
        entry = doc.get(self._NORMAL_INTERVAL_KEY) or {}
        last = _parse_iso(entry.get("at"), self._clock.now())
        if last is None:
            return True
        elapsed = (self._clock.now() - last).total_seconds()
        return elapsed >= minutes * 60.0

    def _mark_normal_synthesis(self, character: str) -> None:
        """Record a normal synthesis so the interval starts now."""
        with self._lock_for(character):
            doc = self._store.load_backoff(character)
            entry = dict(doc.get(self._NORMAL_INTERVAL_KEY) or {})
            entry["n"] = _as_int(entry.get("n"), 0) + 1
            entry["at"] = _now_iso(self._clock.now())
            doc[self._NORMAL_INTERVAL_KEY] = entry
            self._store.save_backoff(character, doc)

    def _bump_backoff(self, character: str, key: str) -> None:
        with self._lock_for(character):
            doc = self._store.load_backoff(character)
            entry = doc.get(key) or {}
            doc[key] = {
                "n": _as_int(entry.get("n"), 0) + 1,
                "at": _now_iso(self._clock.now()),
            }
            self._store.save_backoff(character, doc)

    def _clear_backoff(self, character: str, key: str) -> None:
        doc = self._store.load_backoff(character)
        if key.startswith("fg02:"):
            doc[key] = {"n": 0, "at": None, "result": "completed", "completed_at": _now_iso(self._clock.now())}
            self._store.save_backoff(character, doc)
            return
        if key in doc:
            doc.pop(key, None)
            self._store.save_backoff(character, doc)

    def _dead_letter_healed(self, entry: Mapping[str, Any]) -> bool:
        last = _parse_iso(entry.get("at"), self._clock.now())
        if last is None:
            return False
        elapsed = (self._clock.now() - last).total_seconds()
        return elapsed >= self._settings.dead_letter_self_heal_seconds

    def _mark_absorbed(
        self, character: str, source_fact_ids: Sequence[str],
    ) -> list[str]:
        wanted = {str(item) for item in source_fact_ids if str(item or "").strip()}
        facts = self._facts.load(character)
        marked: list[str] = []
        changed = False
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            fact_id = fact.get("id")
            if not isinstance(fact_id, str) or fact_id not in wanted:
                continue
            if self._barrier.blocks_row(character, fact):
                continue
            marked.append(fact_id)
            if fact.get("absorbed") is not True:
                fact["absorbed"] = True
                fact["reflection_source_keys"] = sorted(_row_source_keys(fact))
                changed = True
        if changed:
            self._facts.save(character, facts)
        return sorted(marked)

    # -- journal replay handlers ----------------------------------------

    def _apply_evidence_updated(
        self, character: str, payload: Mapping[str, Any],
    ) -> bool:
        """Full-snapshot whitelist replay; a load failure propagates (pause)."""
        reflection_id = payload.get("reflection_id")
        if not isinstance(reflection_id, str) or not reflection_id:
            return False
        rows = self._store.load(character)
        row = _find_row(rows, reflection_id)
        if row is None:
            return False
        if self._barrier.blocks_row(character, row):
            return True
        changed = False
        for key in EVIDENCE_SNAPSHOT_KEYS:
            if key in payload and row.get(key) != payload[key]:
                row[key] = payload[key]
                changed = True
        if changed:
            self._store.save(character, rows)
        return changed

    def _apply_state_changed(
        self, character: str, payload: Mapping[str, Any],
    ) -> bool:
        """CAS on `from`; archived rows self-heal their shard."""
        reflection_id = payload.get("reflection_id")
        to_status = payload.get("to")
        if not isinstance(reflection_id, str) or not reflection_id:
            return False
        if not isinstance(to_status, str) or not to_status:
            return False
        from_status = payload.get("from")
        rows = self._store.load(character)
        row = _find_row(rows, reflection_id)
        if row is not None and isinstance(from_status, str) and from_status:
            if str(row.get("status") or "") != from_status:
                # A newer signal won; drop the stale transition but advance.
                return True
        if to_status == "archived":
            return self._replay_archived(
                character, reflection_id, payload, rows, row,
            )
        if row is None:
            return False
        if self._barrier.blocks_row(character, row):
            return True
        changed = False
        if row.get("status") != to_status:
            row["status"] = to_status
            changed = True
        timestamp = payload.get("ts")
        if isinstance(timestamp, str) and timestamp:
            key = f"{to_status}_at"
            if row.get(key) != timestamp:
                row[key] = timestamp
                changed = True
        absorbed_into = payload.get("absorbed_into")
        if absorbed_into is not None and row.get("absorbed_into") != absorbed_into:
            row["absorbed_into"] = absorbed_into
            changed = True
        superseded_by = payload.get("superseded_by")
        if superseded_by is not None and row.get("superseded_by") != superseded_by:
            row["superseded_by"] = superseded_by
            changed = True
        if to_status == "superseded" and isinstance(reason, str) and reason:
            if row.get("superseded_reason") != reason:
                row["superseded_reason"] = reason
                changed = True
        reason = payload.get("reason")
        if isinstance(reason, str) and reason:
            if to_status == "promote_blocked" and row.get("promote_blocked_reason") != reason:
                row["promote_blocked_reason"] = reason
                changed = True
            elif to_status == "denied" and row.get("denied_reason") != reason:
                row["denied_reason"] = reason
                changed = True
        explanation = payload.get("reject_explanation")
        if isinstance(explanation, str) and row.get("reject_reason") != explanation:
            row["reject_reason"] = explanation
            changed = True
        if changed:
            self._store.save(character, rows)
        return changed

    def _replay_archived(
        self,
        character: str,
        reflection_id: str,
        payload: Mapping[str, Any],
        rows: list[dict[str, Any]],
        row: dict[str, Any] | None,
    ) -> bool:
        changed = False
        snapshot = payload.get("entry_snapshot")
        if isinstance(snapshot, Mapping) and not self._barrier.blocks_row(
            character, snapshot,
        ):
            archived_ids = {
                item.get("id") for item in self._store.load_archive(character)
                if isinstance(item, dict)
            }
            if snapshot.get("id") not in archived_ids:
                shard = payload.get("archive_shard_path")
                if not isinstance(shard, str) or not shard:
                    shard = snapshot.get("archive_shard_path")
                if isinstance(shard, str) and shard:
                    self._store.ensure_archive_entry(
                        character, dict(snapshot), shard,
                    )
                else:
                    self._store.append_archive(character, [dict(snapshot)])
                changed = True
        if row is not None:
            kept = [item for item in rows if item.get("id") != reflection_id]
            self._store.save(character, kept)
            changed = True
        return changed


# -- deletion cleaner --------------------------------------------------------


class ReflectionDeletionCleaner:
    """B03 `DeletionServices.reflections` cleaner over reflections/surfaced.

    Full dependency (every source fact deleted, or the row itself is a
    tombstone target) discards the row; a partial dependency narrows
    `source_fact_ids`, records `invalidated_sources` and sets
    `needs_rebuild=True`. Archived shards are rewritten by id.
    """

    def __init__(
        self,
        *,
        store: ReflectionStore,
        barrier: DeletionBarrier | None = None,
        persona: Any = None,
    ) -> None:
        self._store = store
        self._barrier = barrier
        self._persona = persona

    def cleanup(
        self, character: str, resolved: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        character = str(character or "").strip()
        if not character:
            raise ValueError("character must be nonempty")
        reflection_ids = {
            str(item) for item in resolved.get("reflection_ids") or ()
            if isinstance(item, str) and item
        }
        fact_ids = {
            str(item) for item in resolved.get("fact_ids") or ()
            if isinstance(item, str) and item
        }
        source_keys = {
            str(item)
            for item in (
                resolved.get("source_keys")
                or resolved.get("fact_source_keys")
                or ()
            )
            if isinstance(item, str) and item
        }
        episode_ids = {
            str(item) for item in resolved.get("episode_ids") or ()
            if isinstance(item, str) and item
        }
        text_hashes = {
            str(item) for item in resolved.get("text_hashes") or ()
            if isinstance(item, str) and item
        }
        subject = str(resolved.get("subject") or "")
        subject_scope = subject not in ("", "private")
        rows = self._store.load(character)
        kept: list[dict[str, Any]] = []
        removed: list[str] = []
        narrowed = 0
        for row in rows:
            if not isinstance(row, dict):
                kept.append(row)
                continue
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id:
                kept.append(row)
                continue
            if row_id in reflection_ids:
                removed.append(row_id)
                continue
            if self._barrier is not None:
                if self._barrier.blocks_target(character, "reflection", row_id):
                    removed.append(row_id)
                    continue
                if self._barrier.blocks_row(character, row):
                    removed.append(row_id)
                    continue
            if episode_ids and (episode_ids_of(row) & episode_ids):
                removed.append(row_id)
                continue
            if text_hashes and text_hash(row.get("text")) in text_hashes:
                removed.append(row_id)
                continue
            if subject_scope and row_matches_subject(row, subject):
                removed.append(row_id)
                continue
            sources = [
                str(item) for item in row.get("source_fact_ids") or ()
                if isinstance(item, str) and item
            ]
            deleted_sources = [item for item in sources if item in fact_ids]
            row_keys = _row_source_keys(row)
            deleted_keys = row_keys & source_keys
            if not deleted_sources and not deleted_keys:
                kept.append(row)
                continue
            remaining_sources = [item for item in sources if item not in fact_ids]
            is_growth = row.get("memory_kind") in MEMORY_KINDS
            support = [
                str(item) for item in row.get("support_event_keys") or ()
                if isinstance(item, str) and item
            ]
            remaining_support = [item for item in support if item not in source_keys]
            if deleted_sources or deleted_keys:
                # Same recompute as the growth reconcile and the creation path:
                # support is per independent experience, not per message. The
                # cleaner owns row removal / needs_rebuild, the helper owns the
                # numbers, so a narrowed row reports the same maturity the gate
                # would compute for it.
                recompute_support_after_removal(
                    row,
                    deleted_keys,
                    invalidated_fact_ids=deleted_sources,
                )
                remaining_support = [
                    str(item) for item in row.get("support_event_keys") or ()
                    if isinstance(item, str) and item
                ]
            fully_dependent = bool(sources) and not remaining_sources
            if is_growth and support and not remaining_support:
                fully_dependent = True
            if not sources and not support and (deleted_sources or deleted_keys):
                fully_dependent = True
            if fully_dependent:
                removed.append(row_id)
                continue
            invalidated = {
                str(item) for item in row.get("invalidated_sources") or ()
                if isinstance(item, str)
            }
            invalidated.update(deleted_sources)
            invalidated.update(deleted_keys)
            row["source_fact_ids"] = remaining_sources
            row["invalidated_sources"] = sorted(invalidated)
            row["needs_rebuild"] = True
            narrowed += 1
            kept.append(row)
        if removed or narrowed:
            self._store.save(character, kept)

        surfaced = self._store.load_surfaced(character)
        removed_set = set(removed)
        kept_surfaced: list[dict[str, Any]] = []
        for item in surfaced:
            if not isinstance(item, dict):
                continue
            if item.get("reflection_id") in removed_set:
                continue
            text = item.get("text")
            if (
                self._barrier is not None
                and isinstance(text, str)
                and self._barrier.blocks_text(character, text)
            ):
                continue
            kept_surfaced.append(item)
        surfaced_removed = len(surfaced) - len(kept_surfaced)
        if surfaced_removed:
            self._store.save_surfaced(character, kept_surfaced)

        archive_removed = 0
        if removed:
            archive_removed = self._store.delete_from_archive(
                character, sorted(removed),
            )
        # A derived persona entry must stop rendering as soon as its reflection
        # is removed or narrowed (the persona cleaner also drops entries when
        # the deletion engine resolved explicit persona_ids first).
        persona_retracted: list[str] = []
        for row_id in removed:
            persona_retracted.extend(
                self._retract_persona(character, row_id, "deletion"),
            )
        for row in kept:
            if not isinstance(row, dict) or not row.get("needs_rebuild"):
                continue
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id:
                continue
            if not (
                row.get("absorbed_into")
                or str(row.get("status") or "") in ("confirmed", "promoted")
            ):
                continue
            persona_retracted.extend(
                self._retract_persona(character, row_id, "deletion_narrowed"),
            )
        return {
            "status": "completed",
            "detail": {
                "reflections_removed": len(removed),
                "reflections_narrowed": narrowed,
                "surfaced_removed": surfaced_removed,
                "archive_removed": archive_removed,
                "persona_retracted": persona_retracted,
            },
        }

    def _retract_persona(
        self, character: str, reflection_id: str, reason: str,
    ) -> list[str]:
        """Idempotently retract persona entries derived from one reflection."""
        if self._persona is None:
            return []
        method = getattr(self._persona, "retract_source", None)
        if not callable(method):
            return []
        try:
            result = method(
                character, source="reflection", source_id=reflection_id,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 - the reflection row write is durable
            return []
        if not isinstance(result, (list, tuple)):
            return []
        return [str(item) for item in result if isinstance(item, str) and item]


# -- correction invalidator --------------------------------------------------


class ReflectionInvalidator:
    """Correction-service derived walker for reflections.json (main view only).

    A fully invalidated row becomes `withdrawn` (and `needs_rebuild` is
    cleared); a partially invalidated row keeps its remaining sources, records
    `invalidated_sources` and `needs_rebuild=True`. Only rows changed by this
    call are returned, so a retried correction is idempotent.
    """

    def __init__(
        self,
        *,
        store: ReflectionStore,
        barrier: DeletionBarrier | None = None,
    ) -> None:
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
        targets = {
            str(item) for item in fact_ids or ()
            if isinstance(item, str) and item
        }
        if not targets:
            return {"reflection_ids": []}
        rows = self._store.load(character)
        changed_ids: list[str] = []
        changed = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id:
                continue
            sources = {
                str(item) for item in row.get("source_fact_ids") or ()
                if isinstance(item, str) and item
            }
            hit = sources & targets
            if not hit:
                continue
            if self._barrier is not None and self._barrier.blocks_row(
                character, row,
            ):
                continue
            # A correction removes the same support a deletion would, so the
            # count/flags are recomputed by the same routine (M4). Skipping it
            # here would leave a corrected row claiming evidence it lost.
            # `source_fact_ids` stays: it is durable provenance, and the persona
            # invalidator that runs after this one maps persona entries back to
            # their reflection through exactly those ids.
            recompute_support_after_removal(
                row, (), invalidated_fact_ids=sorted(hit), drop_fact_ids=False,
            )
            if sources and sources <= targets:
                if (
                    str(row.get("status") or "") == "withdrawn"
                    and not row.get("needs_rebuild")
                ):
                    continue
                row["status"] = "withdrawn"
                row["withdrawn_at"] = invalidated_at
                row["withdrawn_reason"] = reason
                row["correction_id"] = correction_id
                row["needs_rebuild"] = False
                changed_ids.append(row_id)
                changed = True
                continue
            existing = {
                str(item) for item in row.get("invalidated_sources") or ()
                if isinstance(item, str)
            }
            added = hit - existing
            if not added:
                continue
            row["invalidated_sources"] = sorted(existing | hit)
            row["needs_rebuild"] = True
            changed_ids.append(row_id)
            changed = True
        if changed:
            self._store.save(character, rows)
        return {"reflection_ids": changed_ids}


# -- growth helpers ----------------------------------------------------------


def _growth_explanation(
    row: Mapping[str, Any],
    version: int,
    *,
    promotion_rules: Mapping[str, Sequence[str]] | None = None,
    min_support: int = 2,
) -> dict[str, Any]:
    """Growth item for the API/admin surface, with the unified evidence DTO.

    Downstream consumers (management console, evolution, DSH rendering) read the
    same fields the reflection layer derived, so an item cannot show one support
    count here and another one in the evolution prompt.
    """
    rules = PROMOTION_RULES if promotion_rules is None else promotion_rules
    dto = evidence_dto(
        row, kind="growth", promotion_rules=rules, min_support=min_support,
    )
    return {
        "id": row.get("id"),
        "kind": row.get("memory_kind"),
        "claim": row.get("claim") or row.get("text"),
        "situation": row.get("situation") or "",
        "applies_to": dict(row.get("applies_to") or {}),
        "status": row.get("status"),
        "support_count": dto["maturity"]["independent_experiences"],
        "source_event_keys": list(row.get("source_event_keys") or []),
        "support_event_keys": list(row.get("support_event_keys") or []),
        "source_fact_ids": list(row.get("source_fact_ids") or []),
        # M4: origin, scope, maturity and eligibility in one shape.
        "experiences": list(dto["experiences"]),
        "maturity": dto["maturity"],
        "eligible": dto["eligible"],
        "eligibility": dto["eligibility"],
        "evidence": dto,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "valid_from": row.get("valid_from"),
        "superseded_by": row.get("superseded_by"),
        "version": int(version or 0),
    }


_GROWTH_LABELS: dict[str, dict[str, str]] = {
    "zh": {
        "header": "[成长与经验]",
        "habit": "习惯",
        "interest": "兴趣",
        "experience": "经历",
        "relationship": "关系",
        "situation": "情境",
        "support": "支持",
    },
    "en": {
        "header": "[Growth and experience]",
        "habit": "Habit",
        "interest": "Interest",
        "experience": "Experience",
        "relationship": "Relationship",
        "situation": "When",
        "support": "support",
    },
}


def _normalize_language(language: Any) -> str:
    raw = str(language or "").strip().replace("_", "-").lower()
    if raw.startswith("zh"):
        return "zh"
    if raw.startswith("en"):
        return "en"
    return "zh"


def _render_growth(
    items: Sequence[Mapping[str, Any]],
    *,
    language: Any,
    limit_tokens: int,
) -> tuple[str, list[dict[str, Any]]]:
    labels = _GROWTH_LABELS[_normalize_language(language)]
    budget = max(0, int(limit_tokens or 0))
    if not items or budget <= 0:
        return "", []
    lines = [labels["header"]]
    used = max(1, len(lines[0]) // 2)
    rendered: list[dict[str, Any]] = []
    for item in items:
        kind = str(item.get("memory_kind") or "")
        claim = _normalize_claim(item.get("claim") or item.get("text"))
        if not claim:
            continue
        situation = _normalize_situation(item.get("situation"))
        kind_label = labels.get(kind) or labels["experience"]
        line = f"- {kind_label}: {claim}"
        if situation:
            line += f" ({labels['situation']}: {situation})"
        support = _as_int(item.get("support_count"), 0)
        if support > 1:
            line += f" [{labels['support']} {support}]"
        cost = max(1, len(line) // 2)
        if used + cost > budget:
            break
        lines.append(line)
        used += cost
        rendered.append(dict(item))
    if len(lines) <= 1:
        return "", []
    return "\n".join(lines), rendered


# -- prompt ------------------------------------------------------------------


def _render_old_rows(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """One prompt line per existing understanding (design section 7.2)."""
    lines: list[str] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        parts = [f"[{row.get('id')}] {text}"]
        status = str(row.get("status") or "pending")
        supports = _as_int(row.get("support_count"), 0) or len(
            [item for item in row.get("source_fact_ids") or ()
             if isinstance(item, str)]
        )
        parts.append(
            f"status={status} support={supports} "
            f"rein={_as_float(row.get('reinforcement'), 0.0):.2f} "
            f"disp={_as_float(row.get('disputation'), 0.0):.2f}"
        )
        scene = _normalize_scene(row.get("applies_to"))
        if scene:
            parts.append(
                "scope=" + ",".join(f"{key}:{scene[key]}" for key in sorted(scene))
            )
        topic = str(row.get("topic") or "").strip()
        if topic:
            parts.append(f"topic={topic}")
        lines.append(" | ".join(parts))
    return lines


def _render_strategies(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """One prompt line per strategy already adopted (confirmed/promoted)."""
    lines: list[str] = []
    for row in rows:
        claim = _normalize_claim(row.get("claim")) or str(row.get("text") or "").strip()
        if not claim:
            continue
        line = f"[{row.get('id')}] {claim}"
        situation = _normalize_situation(row.get("situation"))
        if situation:
            line += f" (when: {situation})"
        kind = str(row.get("memory_kind") or "")
        if kind:
            line += f" kind={kind}"
        lines.append(line)
    return lines


def _build_synthesis_prompt(
    facts: Sequence[Mapping[str, Any]],
    old_rows: Sequence[Mapping[str, Any]] = (),
    strategies: Sequence[Mapping[str, Any]] = (),
    material: Mapping[str, Any] | None = None,
) -> str:
    """First-person reflection over new facts and existing understandings."""
    lines = [
        f"[{fact.get('id')}] {str(fact.get('text') or '').strip()[:600]} "
        f"(importance: {_importance_of(fact)}, entity={fact.get('entity')}, source={fact.get('source')}, "
        f"time={fact.get('created_at')}, message_refs={(fact.get('message_refs') or [])[:4]})"
        for fact in facts
        if str(fact.get("text") or "").strip()
    ]
    body = "\n".join(lines)
    old_block = _render_old_rows(old_rows)
    strategy_block = _render_strategies(strategies)
    old_section = "\n".join(old_block) if old_block else "(这个主题等待我形成认识)"
    strategy_section = (
        "\n".join(strategy_block) if strategy_block
        else "(当前采用的相处方式为空)"
    )
    return (
        FIRST_PERSON_VOICE
        + "我带着【现在的我】回看【这段经历】与【相关旧经历】，记录中的角色与消息引用保留原说话者身份。\n"
        + "默认选择 no_change。只有材料让我形成具体的新判断、看见一种做法的效果、修正了旧认识，"
        "或给同一认识补上独立证据，才选择其他操作。我写清是哪句原话、哪个行为或什么结果支持这一点。"
        "把事实换个说法、说出它已经包含的因果，或加一句我记住了，仍然只是复述。"
        "对方曾费力做一件事，只说明当时的经历。我的客套、设想和自我表态也不能当成新的性格、义务或承诺。\n"
        "判断示例：对方说以前布置房间忙了一下午，我回应辛苦了，这批只增加一条往事，输出"
        " {\"operation\":\"no_change\",\"reflection\":null,\"source_fact_ids\":[]}。"
        "我实际试过先复述需求，对方指出这样及时发现了一个具体误解，这次可以记下这个做法在澄清需求时的效果。\n"
        "我对照【已有认识】和【已采用策略】，判断本次材料能支持哪个操作：\n"
        "- add：我形成了一个新的、有具体意义的认识；\n"
        "- reinforce：我获得了独立支持，保留原有认识的表达；\n"
        "- refine：我补充条件，让原有认识更准确；\n"
        "- supersede：我用新的理解替代已经过时的认识；\n"
        "- contradict：我发现新事实与旧判断相反，修正原有理解；\n"
        "- split：我按情境分别理解原来的认识，用 parts 写出至少两条；\n"
        "- no_change：闲聊、回顾往事、复述事实或只有情绪表达，且没有为旧认识补上独立证据。\n"
        "我用一两句平常的话写下具体发现，保持原话里的人、事件和适用情境。"
        "感受或计划有当事人的原话才记录；一时的互动按当时情境写，不扩展成长期性格或相处规则。"
        "reflection、claim 和 parts 中的正文都采用我的第一人称，situation 按实际适用情境填写。\n"
        "本次输出为 JSON 对象：{\"operation\": \"add|reinforce|refine|supersede|"
        "contradict|split|no_change\", \"reflection\": \"我的理解，no_change 时为 null\", "
        "\"target_reflection_id\": \"要改动的已有认识 id，add 时为 null\", "
        "\"topic\": \"这条认识的主题，简短名词\", "
        "\"entity\": \"master|neko|relationship\", "
        "\"relation_type\": null, \"temporal_scope\": null, "
        "\"applies_to\": null|{\"plugin_id\": null, \"game_id\": null, "
        "\"session_id\": null, \"scope\": null}, "
        "\"memory_kind\": null|habit|interest|experience|relationship, "
        "\"claim\": null, \"situation\": null, "
        "\"source_fact_ids\": [实际采用的新事实id], "
        "\"parts\": null|[{\"reflection\": \"...\", \"applies_to\": null, \"source_fact_ids\": [本条采用的事实id]}]}。\n"
        "我选择实际支持这条理解的事实 ID。split 中每条理解分别选择自己的来源。"
        "原始轮次和旧经历帮助理解本次主题，认识的支持依据使用本次提供的新事实。\n\n"
        f"【现在的我、这段经历、相关旧经历】\n{json.dumps(material or {}, ensure_ascii=False)}\n\n"
        f"【已有认识】\n{old_section}\n\n"
        f"【已采用策略】\n{strategy_section}\n\n"
        f"【新事实】\n{body}"
    )


__all__ = [
    "EVIDENCE_SNAPSHOT_KEYS",
    "MAX_PROMPT_OLD_ROWS",
    "MAX_TOPIC_CHARS",
    "OPERATION_ALIASES",
    "REFLECTION_OPERATIONS",
    "REFLECTION_STATUSES",
    "SEMANTIC_MATCH_MIN",
    "SUPERSEDE_REASON",
    "CONTRADICT_REASON",
    "operation_of",
    "reflection_id_from_topic",
    "scope_key",
    "topic_key",
    "EVT_REFLECTION_EVIDENCE_UPDATED",
    "EVT_REFLECTION_STATE_CHANGED",
    "GrowthReceipt",
    "MEMORY_KINDS",
    "PROMOTION_RULES",
    "PromotionOutcome",
    "REFLECTION_EVENT_TYPES",
    "REFLECTION_TERMINAL_STATUSES",
    "ReflectionDeletionCleaner",
    "ReflectionInvalidator",
    "ReflectionService",
    "ReflectionSettings",
    "SynthesisOutcome",
    "growth_key",
    "reflection_id_from_facts",
]
