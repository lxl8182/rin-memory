"""B05 persona entries: evidence, merge, corrections, render, deletion, replay.

Migrated from the host's memory/persona/{facts,corrections,persistence,rendering}.py
(design §17.16.5). The store (persistence/persona.py) owns the host-shaped
persona.json / persona_corrections.json / persona_archive/ files; this module
owns the semantics:

* an entry is derived from a reflection (prom_<reflection_id>) or a growth item
  (prom_growth_<sha1(item_id|claim)[:16]>) or a manual/correction write
  (manual_<YYYYmmddHHMMSS>_<sha256(salt+text)[:8]>), and keeps the provenance
  edges (source_id / merged_from_ids) that corrections and deletions walk;
* evidence is read-time decayed (30/180-day half-lives, protected = inf) and
  the three persona.* journal events replay as full whitelist snapshots;
* the correction queue keeps its four host actions (merge / keep_new /
  keep_old / keep_both) and a bounded version_history (5 rows);
* every derived write asks the B03 deletion barrier first, and every read drops
  invalidated or tombstoned rows, so deleted content cannot come back.

Write order follows the host (RFC §3.9.6): the evidence snapshot event is
appended before the entry-rewrite event, so a crash between the two leaves the
merge retryable instead of orphaning the evidence signal. Replay handlers raise
on failure (never return a silent False) so the journal sentinel stops instead
of skipping a mutation forever.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import uuid
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from ..invalidation import (
    persona_invalidated,
    row_matches_subject,
    scene_applies,
    source_ids_of_persona,
    source_ids_of_reflection,
    text_hash,
)
from .barrier import DeletionBarrier, episode_ids_of
from .prompt_voice import FIRST_PERSON_VOICE

if TYPE_CHECKING:  # pragma: no cover - the stores land in the same batch
    from ..persistence.persona import PersonaStore
    from ..persistence.reflections import ReflectionStore
    from ..ports import Clock, EventJournal, StorageRoots, WritePermit

# -- frozen event names (host vocabulary, memory/event_log.py:72-113) ---------

PERSONA_EVIDENCE_EVENT = "persona.evidence_updated"
PERSONA_ENTRY_EVENT = "persona.entry_updated"
PERSONA_FACT_ADDED_EVENT = "persona.fact_added"

#: Version-chain cap, host config.PERSONA_VERSION_HISTORY_MAX.
PERSONA_VERSION_HISTORY_MAX = 5

#: Evidence fields a full-snapshot event may overwrite (host
#: _EVIDENCE_SNAPSHOT_KEYS, memory/evidence_handlers.py:48-68).
PERSONA_EVIDENCE_FIELDS: tuple[str, ...] = (
    "reinforcement",
    "disputation",
    "rein_last_signal_at",
    "disp_last_signal_at",
    "sub_zero_days",
    "sub_zero_last_increment_date",
    "user_fact_reinforce_count",
)
#: persona.entry_updated also replays the merge audit list and event window.
PERSONA_ENTRY_FIELDS: tuple[str, ...] = PERSONA_EVIDENCE_FIELDS + (
    "merged_from_ids",
    "event_when_raw",
    "event_start_at",
    "event_end_at",
)

#: Sources whose entries are fixed by the character card / settings: they are
#: never suppressed, merged into, or rewritten by a correction (host
#: source == 'character_card' + protected=True).
PERSONA_PROTECTED_SOURCES: frozenset[str] = frozenset({"character_card", "settings"})
#: Terminal sources a reflection may not rewrite: a contradiction becomes a
#: rejected card conflict instead of an in-place edit.
PERSONA_TERMINAL_SOURCES: frozenset[str] = frozenset({"character_card"})

#: Four correction actions (host _VALID_CORRECTION_ACTIONS).
CORRECTION_ACTIONS: frozenset[str] = frozenset({
    "merge", "keep_new", "keep_old", "keep_both",
})
#: Deterministic fallback when no model is wired: the new observation wins.
DEFAULT_CORRECTION_ACTION = "keep_new"
#: Frozen model purpose for the correction review call.
CORRECTION_MODEL_PURPOSE = "persona_correction_resolution"
#: Model tier the host uses for background review work.
CORRECTION_MODEL_TIER = "summary"

#: Evidence half-lives (host memory/evidence.py, config defaults).
REINFORCEMENT_HALF_LIFE_DAYS = 30.0
DISPUTATION_HALF_LIFE_DAYS = 180.0

#: Correction queue rows processed per resolve batch (host default 10).
CORRECTION_BATCH_LIMIT = 10
#: Dead-letter threshold for the queue liveness counter (host config); the core
#: never drops a row on parse failure, it only stops counting.
CORRECTION_MAX_ATTEMPTS = 5

#: Host queue dedupe key (memory/persona/corrections.py:203-215): the same
#: contradiction queued twice must not create a second row. `created_at` and the
#: generated `correction_id` are deliberately excluded: they change on every
#: call and are not part of the observation's identity.
_CORRECTION_DEDUPE_FIELDS = (
    "old_text", "new_text", "entity", "scope", "subject_kind", "subject_id",
    "new_event_when_raw", "new_event_start_at", "new_event_end_at",
)

_CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]",
)
_MANUAL_ID_STAMP_FORMAT = "%Y%m%d%H%M%S"
_FENCE = chr(96) * 3
_NEWLINE = chr(10)
_SECTION_HEADERS = {
    "zh": {"master": "关于主人", "neko": "关于{character}", "relationship": "关系动态"},
    "en": {"master": "About the user", "neko": "About {character}", "relationship": "Relationship dynamics"},
}


class PersonaReplayError(RuntimeError):
    """A persona.* journal event cannot be applied to the current view.

    Raised (never swallowed) so the reconciler pauses with the sentinel behind
    the event instead of marking a mutation applied that the view never got.
    """


class PersonaStateError(RuntimeError):
    """A persona write could not find its target between load and mutate."""


# -- small pure helpers ------------------------------------------------------


def _now_iso(clock: Any) -> str:
    return clock.now().isoformat()


def _sha256_text(text: Any) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return int(value)


def _id_set(values: Any) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        return set()
    return {
        str(value).strip() for value in values
        if isinstance(value, str) and str(value).strip()
    }


def _str_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        return []
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value not in out:
            out.append(value)
    return out


def estimate_tokens(text: Any) -> int:
    """CJK-aware token estimate (B06 swaps in the real TokenCounter).

    One token per CJK/kana/hangul character, four Latin characters per token.
    Deliberately conservative: over-counting trims a line, under-counting would
    overflow the host prompt budget.
    """
    if not isinstance(text, str) or not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    rest = len(text) - cjk
    return cjk + (rest + 3) // 4


def _age_days(ts: Any, now: Any) -> float:
    if not isinstance(ts, str) or not ts:
        return 0.0
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    delta = (now - parsed).total_seconds()
    return 0.0 if delta <= 0 else delta / 86400.0


def evidence_score(entry: Mapping[str, Any], now: Any) -> float:
    """Net decayed evidence (+rein -disp); protected entries score +inf."""
    if entry.get("protected"):
        return float("inf")
    rein = _as_float(entry.get("reinforcement"))
    disp = _as_float(entry.get("disputation"))
    if rein:
        rein *= 0.5 ** (
            _age_days(entry.get("rein_last_signal_at"), now)
            / REINFORCEMENT_HALF_LIFE_DAYS
        )
    if disp:
        disp *= 0.5 ** (
            _age_days(entry.get("disp_last_signal_at"), now)
            / DISPUTATION_HALF_LIFE_DAYS
        )
    return rein - disp


def _default_entry() -> dict[str, Any]:
    """Host entry defaults (_normalize_entry, memory/persona/facts.py:85-139)."""
    return {
        "id": "",
        "text": "",
        "source": "unknown",
        "source_id": None,
        "recent_mentions": [],
        "suppress": False,
        "suppressed_at": None,
        "protected": False,
        "reinforcement": 0.0,
        "disputation": 0.0,
        "rein_last_signal_at": None,
        "disp_last_signal_at": None,
        "sub_zero_days": 0,
        "sub_zero_last_increment_date": None,
        "user_fact_reinforce_count": 0,
        "merged_from_ids": [],
        "token_count": None,
        "token_count_text_sha256": None,
        "token_count_tokenizer": None,
        "version_history": [],
        "event_when_raw": None,
        "event_start_at": None,
        "event_end_at": None,
        "invalidated_at": None,
        "invalidated_by": None,
        "invalidated_reason": None,
        "invalidated_sources": [],
        "needs_rebuild": False,
        "embedding": None,
        "embedding_text_sha256": None,
        "embedding_model_id": None,
        "last_refine_cluster_hash": None,
        "last_refine_at": None,
    }


def _entry_with_defaults(row: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a stored row onto the default shape, keeping unknown keys."""
    entry = dict(row)
    for key, value in _default_entry().items():
        if key not in entry:
            entry[key] = value
    if not isinstance(entry.get("merged_from_ids"), list):
        entry["merged_from_ids"] = _str_list(entry.get("merged_from_ids"))
    if not isinstance(entry.get("version_history"), list):
        entry["version_history"] = []
    return entry


def _is_protected(entry: Mapping[str, Any]) -> bool:
    return bool(entry.get("protected")) or str(
        entry.get("source") or "",
    ) in PERSONA_PROTECTED_SOURCES


def _iter_entries(doc: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for entity, section in (doc or {}).items():
        if not isinstance(section, Mapping):
            continue
        facts = section.get("facts")
        if not isinstance(facts, list):
            continue
        for row in facts:
            if isinstance(row, dict):
                # The live row (not a copy): journal replay handlers mutate it
                # in place before the view is saved.
                out.append((str(entity), row))
    return out


def _find_entry(
    doc: Mapping[str, Any], persona_id: str, entity: str | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    if not persona_id:
        return None, None
    for entity_key, row in _iter_entries(doc):
        if entity is not None and entity_key != entity:
            continue
        if row.get("id") == persona_id:
            return entity_key, row
    return None, None


def _section_facts(doc: dict[str, Any], entity: str) -> list[Any]:
    section = doc.get(entity)
    if not isinstance(section, dict):
        section = {"facts": []}
        doc[entity] = section
    facts = section.get("facts")
    if not isinstance(facts, list):
        facts = []
        section["facts"] = facts
    return facts


def _row_source_keys(row: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    for field_name in ("source_key", "source_event_keys", "source_keys"):
        value = row.get(field_name)
        if isinstance(value, str) and value.strip():
            out.add(value.strip())
        elif isinstance(value, (list, tuple)):
            out.update(
                item.strip() for item in value
                if isinstance(item, str) and item.strip()
            )
    return out


def _correction_natural_identity(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Observation identity of a queue row (see _CORRECTION_DEDUPE_FIELDS)."""
    return tuple(repr(row.get(field)) for field in _CORRECTION_DEDUPE_FIELDS)


def _truncate_history(history: Any) -> list[dict[str, Any]]:
    rows = [dict(item) for item in history or () if isinstance(item, Mapping)]
    return rows[-PERSONA_VERSION_HISTORY_MAX:]


def _history_row(
    text: str, now_iso: str, reason: str, **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "text": str(text or ""),
        "replaced_at": now_iso,
        "reason": reason,
        "source_fact_id": None,
    }
    row.update(extra)
    return row


def _invalidate_token_caches(entry: dict[str, Any]) -> None:
    for key in (
        "token_count", "token_count_text_sha256", "token_count_tokenizer",
        "embedding", "embedding_text_sha256", "embedding_model_id",
    ):
        entry[key] = None


def _parse_correction_decision(
    raw: Any, row: Mapping[str, Any],
) -> tuple[str, str] | None:
    """Strict parse of the model reply; None means "leave the row queued"."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith(_FENCE):
        text = text.strip(chr(96))
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, Mapping):
        return None
    action = data.get("action")
    if not isinstance(action, str):
        return None
    action = action.strip().lower()
    if action not in CORRECTION_ACTIONS:
        return None
    merged_text = data.get("text")
    if not isinstance(merged_text, str) or not merged_text.strip():
        merged_text = str(row.get("new_text") or "")
    if not merged_text:
        return None
    return action, merged_text


# -- service -----------------------------------------------------------------


class PersonaService:
    """Persona entry writes, evidence replay, corrections and rendering."""

    def __init__(
        self,
        *,
        roots: "StorageRoots",
        store: "PersonaStore",
        reflections: "ReflectionStore | None" = None,
        barrier: DeletionBarrier,
        journal: "EventJournal",
        clock: "Clock",
        permit: "WritePermit",
        model: Any = None,
        render_max_tokens: int = 2000,
    ) -> None:
        self._roots = roots
        self._store = store
        self._reflections = reflections
        self._barrier = barrier
        self._journal = journal
        self._clock = clock
        self._permit = permit
        self._model = model
        self._render_max_tokens = int(render_max_tokens or 2000)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._cleaner = PersonaDeletionCleaner(
            store=store, reflections=reflections, barrier=barrier, clock=clock,
        )
        self._invalidator = PersonaInvalidator(
            store=store, reflections=reflections, barrier=barrier, clock=clock,
        )
        # Recovery order (design §17.15): handlers registered in the
        # constructor, so reconcile can repair these views before any write.
        journal.register_handler(PERSONA_EVIDENCE_EVENT, self._replay_evidence)
        journal.register_handler(PERSONA_ENTRY_EVENT, self._replay_entry)
        journal.register_handler(PERSONA_FACT_ADDED_EVENT, self._replay_fact_added)

    # -- read paths ------------------------------------------------------

    def entries(
        self, character: str, *, entity: str | None = None,
    ) -> list[dict[str, Any]]:
        """Valid entries: not invalidated, not tombstoned (host read contract)."""
        doc = self._load(character)
        out: list[dict[str, Any]] = []
        for entity_key, row in _iter_entries(doc):
            if entity is not None and entity_key != entity:
                continue
            entry = _entry_with_defaults(row)
            if persona_invalidated(entry):
                continue
            if self._barrier.blocks_row(character, entry):
                continue
            out.append(entry)
        return out

    def render_block(
        self,
        character: str,
        *,
        language: Any = "zh",
        limit_tokens: int | None = None,
        subject: Any = None,
        scene: Any = None,
    ) -> str:
        """Score-ordered persona block, trimmed to the token budget.

        Suppressed entries are remembered but never volunteered (host rule);
        invalidated/tombstoned rows never render. Token accounting is the B05
        CJK estimate; B06 replaces it with the injected TokenCounter.
        """
        now = self._clock.now()
        budget = self._render_max_tokens if limit_tokens is None else int(limit_tokens)
        budget = max(0, budget)
        lang = "zh" if str(language or "").lower().startswith("zh") else "en"
        selected: list[tuple[float, str, dict[str, Any]]] = []
        for entity_key, row in _iter_entries(self._load(character)):
            entry = _entry_with_defaults(row)
            if not str(entry.get("text") or "").strip():
                continue
            if entry.get("suppress"):
                continue
            if persona_invalidated(entry):
                continue
            if self._barrier.blocks_row(character, entry):
                continue
            if subject is not None and not row_matches_subject(entry, subject):
                continue
            if scene is not None and not scene_applies(entry.get("applies_to"), scene):
                continue
            selected.append((evidence_score(entry, now), entity_key, entry))
        selected.sort(key=lambda item: item[0], reverse=True)
        used = 0
        groups: dict[str, list[str]] = {}
        for _score, entity_key, entry in selected:
            line = f"- {entry.get('text')}"
            cost = estimate_tokens(line) + 1
            if entity_key not in groups:
                header = self._section_header(entity_key, lang, character)
                header_cost = estimate_tokens(header) + 1
                if used + header_cost + cost > budget:
                    continue
                used += header_cost
                groups[entity_key] = []
            if used + cost > budget:
                continue
            groups[entity_key].append(line)
            used += cost
        blocks: list[str] = []
        for entity_key, lines in groups.items():
            if not lines:
                continue
            blocks.append(
                "### " + self._section_header(entity_key, lang, character)
                + _NEWLINE + _NEWLINE.join(lines),
            )
        return (_NEWLINE + _NEWLINE).join(blocks)

    def version(self, character: str) -> int:
        """Stable entry-count view version for in-flight block checks.

        Not persisted: the count moves whenever entries are added or removed,
        which is what B06's in-flight check needs. A same-count content edit is
        not detected by this value; callers that need exactness must also
        compare the rendered block text (documented limitation, Lead decision).
        """
        return sum(1 for _ in _iter_entries(self._load(character)))

    def resolve_ids(
        self,
        character: str,
        resolved: Mapping[str, Any],
        reflection_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Persona ids a deletion resolution must target (B03 resolve step).

        The deletion engine resolves facts, then asks the reflection cleaner for
        the reflection ids it will remove; those ids no longer exist afterwards,
        so the persona rows that depended on them must be resolved now.

        Two output buckets, because the two reflection buckets have different
        meanings:

        * explicit targets / source keys / text hashes / episode ids, and rows
          whose every source is in the fully-deleted reflection set -> deleted;
        * rows that also depend on a *surviving* (narrowed) reflection -> only
          narrowed: the hit sources are dropped, the row keeps its history and
          gets invalidated_sources + suppress + needs_rebuild.

        A non-private subject filter keeps one subject's deletion from touching
        another subject's rows.
        """
        data = dict(resolved or {})
        explicit = _id_set(data.get("persona_ids"))
        aff_full = _id_set(reflection_ids) or _id_set(data.get("reflection_ids"))
        aff_narrow = _id_set(data.get("narrowed_reflection_ids"))
        source_keys = _id_set(
            data.get("fact_source_keys") or data.get("source_keys"),
        )
        text_hashes = _id_set(data.get("text_hashes"))
        episode_ids = _id_set(data.get("episode_ids"))
        subject = data.get("subject")
        deleted: list[str] = []
        narrowed: list[str] = []
        for _entity_key, entry in _iter_entries(self._load(character)):
            row = _entry_with_defaults(entry)
            row_id = str(row.get("id") or "")
            if not row_id:
                continue
            if subject not in (None, "", "private") and not row_matches_subject(
                row, subject,
            ):
                continue
            explicit_hit = row_id in explicit
            if not explicit_hit and source_keys:
                explicit_hit = bool(_row_source_keys(row) & source_keys)
            if not explicit_hit and text_hashes:
                text = row.get("text")
                explicit_hit = (
                    isinstance(text, str) and text_hash(text) in text_hashes
                )
            if not explicit_hit and episode_ids:
                explicit_hit = bool(episode_ids_of(row) & episode_ids)
            if explicit_hit:
                if row_id not in deleted:
                    deleted.append(row_id)
                continue
            sources = source_ids_of_persona(row)
            if not sources:
                continue
            if aff_full and sources <= aff_full:
                if row_id not in deleted:
                    deleted.append(row_id)
                continue
            if sources & (aff_full | aff_narrow):
                if row_id not in narrowed:
                    narrowed.append(row_id)
        return {"persona_ids": deleted, "narrowed_persona_ids": narrowed}

    def corrections(self, character: str) -> list[dict[str, Any]]:
        """Pending queue rows whose old/new text is not tombstoned."""
        out: list[dict[str, Any]] = []
        for row in self._load_corrections(character):
            if not isinstance(row, Mapping):
                continue
            if self._queue_row_blocked(character, row):
                continue
            out.append(dict(row))
        return out

    # -- write paths -----------------------------------------------------

    def add_reflection(
        self, character: str, reflection_row: Mapping[str, Any],
    ) -> str:
        """Promote one reflection into a persona entry (prom_<reflection_id>)."""
        raw_id = str(reflection_row.get("id") or "").strip()
        source_id = str(reflection_row.get("source_id") or "").strip()
        reflection_id = source_id or raw_id
        if not reflection_id:
            raise ValueError("reflection row is missing its id")
        # B05 reflection service passes the pre-built persona row (id already
        # prom_<reflection_id>, source_id = reflection id); direct callers pass
        # the reflection row itself. Accept both without double-prefixing.
        persona_id = raw_id if raw_id.startswith("prom_") else f"prom_{reflection_id}"
        entity = str(
            reflection_row.get("entity") or "relationship",
        ).strip() or "relationship"
        entry = self._entry_from_source(
            persona_id,
            str(reflection_row.get("text") or ""),
            entity=entity,
            source="reflection",
            source_id=reflection_id,
            source_row=reflection_row,
        )
        return self._append_entry(character, entity, entry)

    def add_growth(self, character: str, growth_row: Mapping[str, Any]) -> str:
        """Promote one growth item (prom_growth_<sha1(item_id|claim)[:16]>)."""
        raw_id = str(growth_row.get("id") or "").strip()
        upstream_id = str(growth_row.get("source_id") or "").strip()
        claim = str(growth_row.get("claim") or growth_row.get("text") or "").strip()
        if not claim:
            raise ValueError("growth row needs a claim")
        if upstream_id and raw_id.startswith("prom_growth_"):
            # Pre-built persona row from the reflection service: its id is the
            # canonical host-derived hash, keep it instead of re-hashing.
            item_id = upstream_id
            persona_id = raw_id
        else:
            item_id = str(
                upstream_id
                or growth_row.get("item_id")
                or raw_id
                or growth_row.get("growth_key")
                or "",
            ).strip()
            if not item_id:
                raise ValueError("growth row needs an item id")
            digest = hashlib.sha1(
                f"{item_id}|{claim}".encode("utf-8"),
            ).hexdigest()[:16]
            persona_id = f"prom_growth_{digest}"
        entity = str(
            growth_row.get("entity") or "relationship",
        ).strip() or "relationship"
        entry = self._entry_from_source(
            persona_id,
            claim,
            entity=entity,
            source="growth",
            source_id=item_id,
            source_row=growth_row,
        )
        return self._append_entry(character, entity, entry)

    def add_manual(
        self,
        character: str,
        text: str,
        *,
        entity: str = "relationship",
        source: str = "manual",
        source_id: str = "",
    ) -> str:
        """Append a manual/correction-derived entry (host manual_... id)."""
        body = str(text or "").strip()
        if not body:
            raise ValueError("manual persona text is empty")
        entity = str(entity or "relationship").strip() or "relationship"
        stamp = self._clock.now().strftime(_MANUAL_ID_STAMP_FORMAT)
        salt = f"{entity}|{source_id}|"
        digest = hashlib.sha256((salt + body).encode("utf-8")).hexdigest()[:8]
        persona_id = f"manual_{stamp}_{digest}"
        entry = _entry_with_defaults({
            "id": persona_id,
            "text": body,
            "source": source,
            "source_id": source_id or None,
            "protected": source in PERSONA_PROTECTED_SOURCES,
            "suppress": False,
            "merged_from_ids": [],
            "version_history": [],
        })
        return self._append_entry(character, entity, entry)

    def apply_evidence(
        self, character: str, persona_id: str, fields: Mapping[str, Any],
    ) -> bool:
        """Overwrite whitelisted evidence fields through one journal event."""
        if not isinstance(fields, Mapping):
            raise TypeError("evidence fields must be a mapping")
        clean = {
            key: fields[key] for key in PERSONA_EVIDENCE_FIELDS if key in fields
        }
        if not clean:
            return False
        with self._lock(character):
            doc = self._load(character)
            entity, entry = _find_entry(doc, persona_id)
            if entry is None:
                return False
            current = _entry_with_defaults(entry)
            self._barrier.assert_row_allowed(character, current)
            if all(current.get(key) == value for key, value in clean.items()):
                return False
            payload = {
                "character": character,
                "persona_id": persona_id,
                "entity": entity,
                "fields": clean,
            }
            self._journal.record_and_save(
                character,
                PERSONA_EVIDENCE_EVENT,
                payload,
                load_view=self._load,
                mutate_view=lambda view: self._apply_evidence_payload(view, payload),
                save_view=self._save_view,
            )
            return True

    def merge(
        self, character: str, source_id: str, target_id: str, text: str,
    ) -> dict[str, Any]:
        """Merge a reflection into an entry (evidence max + audit list + history)."""
        merged_text = str(text or "").strip()
        if not merged_text:
            raise ValueError("merged text is empty")
        with self._lock(character):
            doc = self._load(character)
            entity, target = _find_entry(doc, target_id)
            if target is None or entity is None:
                return {"status": "not_found", "persona_id": target_id}
            current = _entry_with_defaults(target)
            if _is_protected(current):
                return {
                    "status": "rejected",
                    "persona_id": target_id,
                    "reason": "protected_entry",
                }
            self._barrier.assert_row_allowed(character, current)
            merged_from = _str_list(current.get("merged_from_ids"))
            if source_id and source_id in merged_from:
                return {
                    "status": "noop",
                    "persona_id": target_id,
                    "entity": entity,
                    "merged_from_ids": merged_from,
                }
            source_row = self._reflection_row(character, source_id)
            reinforcement = max(
                _as_float(current.get("reinforcement")),
                _as_float(source_row.get("reinforcement")) if source_row else 0.0,
            )
            disputation = max(
                _as_float(current.get("disputation")),
                _as_float(source_row.get("disputation")) if source_row else 0.0,
            )
            now_iso = _now_iso(self._clock)
            new_merged_from = list(merged_from)
            if source_id and source_id not in new_merged_from:
                new_merged_from.append(source_id)
            history = _truncate_history(
                list(current.get("version_history") or []) + [
                    _history_row(
                        str(current.get("text") or ""), now_iso, "merge",
                        source_reflection_ids=[source_id] if source_id else [],
                    ),
                ],
            )
            evidence_fields = {
                "reinforcement": reinforcement,
                "disputation": disputation,
                "rein_last_signal_at": now_iso,
                "disp_last_signal_at": now_iso,
                "sub_zero_days": 0,
            }
            evidence_payload = {
                "character": character,
                "persona_id": target_id,
                "entity": entity,
                "fields": dict(evidence_fields),
            }
            # Event 1: evidence snapshot (no merged_from_ids sentinel yet, so a
            # crash between the two appends leaves the merge retryable).
            self._journal.record_and_save(
                character,
                PERSONA_EVIDENCE_EVENT,
                evidence_payload,
                load_view=self._load,
                mutate_view=lambda view: self._apply_evidence_payload(
                    view, evidence_payload,
                ),
                save_view=self._save_view,
            )
            entry_payload = {
                "character": character,
                "persona_id": target_id,
                "entity": entity,
                "rewrite_text_sha256": _sha256_text(merged_text),
                "text": merged_text,
                "merged_from_ids": new_merged_from,
                "version_history": history,
                "fields": dict(evidence_fields),
            }
            self._journal.record_and_save(
                character,
                PERSONA_ENTRY_EVENT,
                entry_payload,
                load_view=self._load,
                mutate_view=lambda view: self._apply_entry_payload(view, entry_payload),
                save_view=self._save_view,
            )
            return {
                "status": "merged",
                "persona_id": target_id,
                "entity": entity,
                "merged_from_ids": new_merged_from,
                "version_history": history,
                "reinforcement": reinforcement,
                "disputation": disputation,
            }

    def enqueue_correction(
        self, character: str, row: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Queue a contradiction for review (dedup by correction_id/legacy key)."""
        if not isinstance(row, Mapping):
            raise TypeError("correction row must be a mapping")
        item = dict(row)
        old_text = str(item.get("old_text") or "")
        new_text = str(item.get("new_text") or "")
        if not new_text.strip():
            return {"status": "rejected", "reason": "missing_new_text"}
        item["entity"] = str(
            item.get("entity") or "relationship",
        ).strip() or "relationship"
        explicit_id = str(item.get("correction_id") or "").strip()
        item.setdefault("created_at", _now_iso(self._clock))
        if not explicit_id:
            # Generated only after the dedupe decision below: it must not become
            # the identity of a caller-supplied observation.
            item["correction_id"] = uuid.uuid4().hex
        if self._queue_row_blocked(character, item):
            return {"status": "rejected", "reason": "deleted_content"}
        with self._lock(character):
            queue = [
                dict(existing) for existing in self._load_corrections(character)
            ]
            natural = _correction_natural_identity(item)
            for existing in queue:
                if explicit_id:
                    matched = str(existing.get("correction_id") or "") == explicit_id
                else:
                    matched = _correction_natural_identity(existing) == natural
                if not matched:
                    continue
                changed = self._merge_queue_provenance(existing, item)
                if changed:
                    self._save_corrections(character, queue)
                return {
                    "status": "duplicate",
                    "correction_id": str(existing.get("correction_id") or ""),
                    "queue": len(queue),
                }
            queue.append(item)
            self._save_corrections(character, queue)
            return {
                "status": "queued",
                "correction_id": item["correction_id"],
                "queue": len(queue),
            }

    def resolve_corrections(
        self, character: str, limit: int = CORRECTION_BATCH_LIMIT,
    ) -> dict[str, Any]:
        """Apply at most limit queued corrections with the four host actions."""
        cap = max(1, int(limit or CORRECTION_BATCH_LIMIT))
        with self._lock(character):
            queue = [
                dict(row) for row in self._load_corrections(character)
                if isinstance(row, Mapping)
            ]
            doc = self._load(character)
            doc_dirty = False
            applied = 0
            dropped = 0
            skipped = 0
            queue_dirty = False
            actions: dict[str, int] = {}
            remaining: list[dict[str, Any]] = []
            processed = 0
            for row in queue:
                if self._queue_row_blocked(character, row):
                    # The text is tombstoned: never apply it, drop the row.
                    dropped += 1
                    continue
                if processed >= cap:
                    remaining.append(row)
                    continue
                processed += 1
                decision = self._decide_correction(character, row)
                if decision is None:
                    row["resolve_attempts"] = _as_int(row.get("resolve_attempts")) + 1
                    remaining.append(row)
                    skipped += 1
                    queue_dirty = True
                    continue
                action, merged_text = decision
                try:
                    changed = self._apply_correction(
                        character, doc, row, action, merged_text,
                    )
                except Exception:  # noqa: BLE001 - row stays queued for a retry
                    row["resolve_attempts"] = _as_int(row.get("resolve_attempts")) + 1
                    remaining.append(row)
                    skipped += 1
                    queue_dirty = True
                    continue
                doc_dirty = doc_dirty or changed
                applied += 1
                actions[action] = actions.get(action, 0) + 1
            if doc_dirty:
                self._store.save(character, doc)
            if applied or dropped or queue_dirty:
                self._save_corrections(character, remaining)
            return {
                "resolved": applied,
                "dropped": dropped,
                "skipped": skipped,
                "remaining": len(remaining),
                "actions": actions,
            }

    def retract_source(
        self,
        character: str,
        *,
        source: str,
        source_id: str,
        reason: str = "user_retract",
    ) -> list[str]:
        """Mark entries derived from one retracted source (growth retraction).

        Rows are not physically deleted: they keep their history and become
        suppressed + invalidated, so the read paths stop serving them. Idempotent
        (an already-marked row is not counted again) and barrier-checked.
        """
        wanted_source = str(source or "").strip()
        wanted_id = str(source_id or "").strip()
        if not wanted_id:
            return []
        with self._lock(character):
            doc = self._load(character)
            now_iso = _now_iso(self._clock)
            changed = False
            marked: list[str] = []
            for _entity_key, entry in _iter_entries(doc):
                row = _entry_with_defaults(entry)
                row_id = str(row.get("id") or "")
                if not row_id:
                    continue
                if wanted_source and str(row.get("source") or "") != wanted_source:
                    continue
                sources = source_ids_of_persona(row)
                if wanted_id not in sources:
                    continue
                already = _str_list(row.get("invalidated_sources"))
                if row.get("invalidated_at") and wanted_id in already:
                    continue  # idempotent: already marked by an earlier retract
                if self._barrier.blocks_row(character, row):
                    continue  # deleted content is the cleaner's job
                row["invalidated_at"] = now_iso
                row["invalidated_by"] = wanted_id
                row["invalidated_reason"] = reason
                row["invalidated_sources"] = sorted(set(already) | {wanted_id})
                row["suppress"] = True
                row["suppressed_at"] = now_iso
                row["needs_rebuild"] = bool(sources - {wanted_id})
                row["version_history"] = _truncate_history(
                    list(row.get("version_history") or []) + [
                        _history_row(
                            "", now_iso, reason,
                            correction_id=wanted_id,
                            source_reflection_ids=[wanted_id],
                        ),
                    ],
                )
                entry.update(row)
                marked.append(row_id)
                changed = True
            if changed:
                self._store.save(character, doc)
            return marked

    # -- B03 delegation (composition registers the service itself) -------

    def cleanup(
        self, character: str, resolved: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Deletion-engine cleaner contract (DeletionServices.persona)."""
        return self._cleaner.cleanup(character, resolved)

    def invalidate(
        self,
        character: str,
        fact_ids: Sequence[str],
        *,
        invalidated_at: str,
        reason: str,
        correction_id: str,
    ) -> dict[str, Any]:
        """Correction derived-invalidator contract (_run_invalidator)."""
        return self._invalidator.invalidate(
            character,
            fact_ids,
            invalidated_at=invalidated_at,
            reason=reason,
            correction_id=correction_id,
        )

    # -- async wrappers (blocking file IO off the event loop) ------------

    async def resolve_corrections_async(
        self, character: str, limit: int = CORRECTION_BATCH_LIMIT,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.resolve_corrections, character, limit)

    async def merge_async(
        self, character: str, source_id: str, target_id: str, text: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.merge, character, source_id, target_id, text,
        )

    async def apply_evidence_async(
        self, character: str, persona_id: str, fields: Mapping[str, Any],
    ) -> bool:
        return await asyncio.to_thread(
            self.apply_evidence, character, persona_id, fields,
        )

    # -- journal replay handlers ----------------------------------------

    def _replay_evidence(self, character: str, payload: Mapping[str, Any]) -> bool:
        """Full-snapshot evidence replay (whitelist overwrite, idempotent)."""
        if not isinstance(payload, Mapping):
            raise PersonaReplayError(
                "persona.evidence_updated payload is not an object",
            )
        persona_id = str(
            payload.get("persona_id") or payload.get("entry_id") or "",
        ).strip()
        if not persona_id:
            return False
        entity = payload.get("entity") or payload.get("entity_key")
        fields = payload.get("fields")
        clean: dict[str, Any] = {}
        if isinstance(fields, Mapping):
            clean.update({k: fields[k] for k in PERSONA_EVIDENCE_FIELDS if k in fields})
        # Host-written logs carry the snapshot at the top level.
        clean.update({k: payload[k] for k in PERSONA_EVIDENCE_FIELDS if k in payload})
        if not clean:
            return False
        doc = self._load(character)
        _found, entry = _find_entry(
            doc, persona_id, str(entity) if entity else None,
        )
        if entry is None:
            return False
        current = _entry_with_defaults(entry)
        if self._barrier.blocks_row(character, current):
            return False
        changed = False
        for key, value in clean.items():
            if entry.get(key) != value:
                entry[key] = value
                changed = True
        if changed:
            self._store.save(character, doc)
        return changed

    def _replay_entry(self, character: str, payload: Mapping[str, Any]) -> bool:
        """Text rewrite + merge audit replay; a sha drift raises to pause replay."""
        if not isinstance(payload, Mapping):
            raise PersonaReplayError(
                "persona.entry_updated payload is not an object",
            )
        persona_id = str(
            payload.get("persona_id") or payload.get("entry_id") or "",
        ).strip()
        if not persona_id:
            return False
        entity = payload.get("entity") or payload.get("entity_key")
        expected = payload.get("rewrite_text_sha256")
        body = payload.get("text")
        doc = self._load(character)
        _found, entry = _find_entry(
            doc, persona_id, str(entity) if entity else None,
        )
        if entry is None:
            return False
        current = _entry_with_defaults(entry)
        if self._barrier.blocks_row(character, current):
            return False
        changed = False
        if isinstance(expected, str) and expected:
            if body is not None:
                if _sha256_text(body) != expected:
                    raise PersonaReplayError(
                        f"{character}/{persona_id}: persona.entry_updated payload "
                        f"text sha256 does not match rewrite_text_sha256",
                    )
                if _sha256_text(entry.get("text")) != expected:
                    entry["text"] = str(body)
                    _invalidate_token_caches(entry)
                    changed = True
            elif _sha256_text(entry.get("text")) != expected:
                raise PersonaReplayError(
                    f"{character}/{persona_id}: persona.entry_updated view text "
                    f"drifted from the log (sha256 mismatch); manual inspection "
                    f"required",
                )
        # Host-written payloads also carry the evidence snapshot at top level.
        fields = payload.get("fields")
        snapshot: dict[str, Any] = {}
        if isinstance(fields, Mapping):
            snapshot.update({k: fields[k] for k in PERSONA_ENTRY_FIELDS if k in fields})
        snapshot.update({k: payload[k] for k in PERSONA_ENTRY_FIELDS if k in payload})
        for key, value in snapshot.items():
            if key == "merged_from_ids":
                merged = _str_list(entry.get("merged_from_ids"))
                for item in _str_list(value):
                    if item not in merged:
                        merged.append(item)
                if merged != _str_list(entry.get("merged_from_ids")):
                    entry["merged_from_ids"] = merged
                    changed = True
                continue
            if entry.get(key) != value:
                entry[key] = value
                changed = True
        history = payload.get("version_history")
        if isinstance(history, list) and history != entry.get("version_history"):
            entry["version_history"] = _truncate_history(history)
            changed = True
        if changed:
            self._store.save(character, doc)
        return changed

    def _replay_fact_added(self, character: str, payload: Mapping[str, Any]) -> bool:
        """Archive replay: heal the named shard first, then drop the main row.

        A corrupt shard raises (the replay pauses) and the main view keeps the
        only remaining copy; the shard is never silently skipped.
        """
        if not isinstance(payload, Mapping):
            raise PersonaReplayError("persona.fact_added payload is not an object")
        shard = payload.get("archive_shard_path")
        if not isinstance(shard, str) or not shard.strip():
            return False  # Not an archive event: forward fact_added is not event-sourced.
        persona_id = str(
            payload.get("persona_id") or payload.get("entry_id") or "",
        ).strip()
        if not persona_id:
            return False
        snapshot = payload.get("entry_snapshot")
        shard_changed = False
        if isinstance(snapshot, Mapping):
            # May raise CorruptStoreError: the main view must survive that.
            shard_changed = bool(
                self._store.ensure_archive_entry(character, dict(snapshot), shard),
            )
        doc = self._load(character)
        removed = False
        for _entity_key, section in list(doc.items()):
            if not isinstance(section, dict):
                continue
            facts = section.get("facts")
            if not isinstance(facts, list):
                continue
            kept = [
                row for row in facts
                if not (isinstance(row, Mapping) and row.get("id") == persona_id)
            ]
            if len(kept) != len(facts):
                section["facts"] = kept
                removed = True
        if removed:
            self._store.save(character, doc)
        return bool(shard_changed or removed)

    # -- internals: writes ----------------------------------------------

    def _append_entry(
        self, character: str, entity: str, entry: dict[str, Any],
    ) -> str:
        persona_id = str(entry.get("id") or "")
        with self._lock(character):
            doc = self._load(character)
            if _find_entry(doc, persona_id)[1] is not None:
                return persona_id
            self._barrier.assert_row_allowed(character, entry)
            _section_facts(doc, entity).append(entry)
            self._store.save(character, doc)
            return persona_id

    def _entry_from_source(
        self,
        persona_id: str,
        text: str,
        *,
        entity: str,
        source: str,
        source_id: str,
        source_row: Mapping[str, Any],
    ) -> dict[str, Any]:
        body = str(text or "").strip()
        if not body:
            raise ValueError("persona entry text is empty")
        return _entry_with_defaults({
            "id": persona_id,
            "text": body,
            "source": source,
            "source_id": source_id,
            "protected": False,
            "suppress": False,
            "merged_from_ids": [],
            "version_history": [],
            "reinforcement": _as_float(source_row.get("reinforcement")),
            "disputation": _as_float(source_row.get("disputation")),
            "rein_last_signal_at": source_row.get("rein_last_signal_at"),
            "disp_last_signal_at": source_row.get("disp_last_signal_at"),
            "sub_zero_days": _as_int(source_row.get("sub_zero_days")),
            "sub_zero_last_increment_date": source_row.get(
                "sub_zero_last_increment_date",
            ),
            "user_fact_reinforce_count": _as_int(
                source_row.get("user_fact_reinforce_count"),
            ),
            "event_when_raw": source_row.get("event_when_raw"),
            "event_start_at": source_row.get("event_start_at"),
            "event_end_at": source_row.get("event_end_at"),
            "recent_mentions": list(source_row.get("recent_mentions") or []),
            "applies_to": dict(source_row.get("applies_to") or {}),
            "created_at": source_row.get("created_at"),
            "updated_at": source_row.get("updated_at"),
        })

    def _apply_evidence_payload(
        self, doc: dict[str, Any], payload: Mapping[str, Any],
    ) -> None:
        persona_id = str(payload.get("persona_id") or payload.get("entry_id") or "")
        entity = payload.get("entity") or payload.get("entity_key")
        _found, entry = _find_entry(
            doc, persona_id, str(entity) if entity else None,
        )
        if entry is None:
            raise PersonaStateError(
                f"persona entry {persona_id!r} disappeared before the evidence write",
            )
        fields = payload.get("fields")
        clean: dict[str, Any] = {}
        if isinstance(fields, Mapping):
            clean.update({k: fields[k] for k in PERSONA_EVIDENCE_FIELDS if k in fields})
        clean.update({k: payload[k] for k in PERSONA_EVIDENCE_FIELDS if k in payload})
        for key, value in clean.items():
            entry[key] = value

    def _apply_entry_payload(
        self, doc: dict[str, Any], payload: Mapping[str, Any],
    ) -> None:
        persona_id = str(payload.get("persona_id") or payload.get("entry_id") or "")
        entity = payload.get("entity") or payload.get("entity_key")
        _found, entry = _find_entry(
            doc, persona_id, str(entity) if entity else None,
        )
        if entry is None:
            raise PersonaStateError(
                f"persona entry {persona_id!r} disappeared before the entry write",
            )
        body = payload.get("text")
        expected = payload.get("rewrite_text_sha256")
        if isinstance(body, str):
            if (
                isinstance(expected, str) and expected
                and _sha256_text(body) != expected
            ):
                raise PersonaStateError(
                    "entry rewrite payload sha256 does not match text",
                )
            entry["text"] = body
            _invalidate_token_caches(entry)
        merged = _str_list(entry.get("merged_from_ids"))
        for item in _str_list(payload.get("merged_from_ids")):
            if item not in merged:
                merged.append(item)
        if merged:
            entry["merged_from_ids"] = merged
        history = payload.get("version_history")
        if isinstance(history, list):
            entry["version_history"] = _truncate_history(history)
        fields = payload.get("fields")
        clean: dict[str, Any] = {}
        if isinstance(fields, Mapping):
            clean.update({k: fields[k] for k in PERSONA_EVIDENCE_FIELDS if k in fields})
        for key, value in clean.items():
            entry[key] = value

    def _save_view(self, character: str, view: Any) -> None:
        self._store.save(character, view)

    # -- internals: corrections -----------------------------------------

    def _decide_correction(
        self, character: str, row: Mapping[str, Any],
    ) -> tuple[str, str] | None:
        if self._model is None:
            new_text = str(row.get("new_text") or "").strip()
            return (DEFAULT_CORRECTION_ACTION, new_text) if new_text else None
        try:
            raw = asyncio.run(self._ainvoke_correction(character, row))
        except Exception:  # noqa: BLE001 - transport/parse failure keeps the row
            return None
        return _parse_correction_decision(raw, row)

    async def _ainvoke_correction(
        self, character: str, row: Mapping[str, Any],
    ) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    FIRST_PERSON_VOICE
                    + "我对照新的观察和原有记忆，整理自己现在的理解，并选择一个动作："
                    "merge（我重写原条目）、keep_new（我采用新观察替换原条目）、"
                    "keep_old（我保留原条目，将新观察放入历史）、keep_both（我保留两种有各自情境的认识）。"
                    '本次输出为 JSON：{"action": "merge|keep_new|keep_old|keep_both", '
                    '"text": "我用第一人称整合的正文（merge 时填写）"}'
                ),
            },
            {
                "role": "user",
                "content": json.dumps({
                    "character": character,
                    "entity": row.get("entity"),
                    "old_text": row.get("old_text"),
                    "new_text": row.get("new_text"),
                }, ensure_ascii=False),
            },
        ]
        return await self._model.invoke(
            CORRECTION_MODEL_TIER,
            messages,
            max_output_tokens=256,
            timeout_s=30.0,
            purpose=CORRECTION_MODEL_PURPOSE,
        )

    def _apply_correction(
        self,
        character: str,
        doc: dict[str, Any],
        row: Mapping[str, Any],
        action: str,
        merged_text: str,
    ) -> bool:
        entity = str(row.get("entity") or "relationship").strip() or "relationship"
        old_text = str(row.get("old_text") or "")
        new_text = str(row.get("new_text") or "")
        now_iso = _now_iso(self._clock)
        subject = self._queue_subject(row)
        facts = _section_facts(doc, entity)

        def _matches(candidate: Any) -> bool:
            if not isinstance(candidate, dict):
                return False
            if str(candidate.get("text") or "") != old_text:
                return False
            if subject is not None and not row_matches_subject(candidate, subject):
                return False
            return True

        matched = [index for index, candidate in enumerate(facts) if _matches(candidate)]
        changed = False
        if action == "merge":
            if matched:
                target = _entry_with_defaults(facts[matched[0]])
                if _is_protected(target):
                    raise PermissionError("protected persona entry cannot be merged")
                target["version_history"] = _truncate_history(
                    list(target.get("version_history") or []) + [
                        _history_row(old_text, now_iso, "correction"),
                    ],
                )
                target["text"] = merged_text
                _invalidate_token_caches(target)
                facts[matched[0]] = target
                changed = True
            else:
                self._append_correction_entry(character, facts, merged_text, row)
                changed = True
        elif action == "keep_new":
            if matched:
                removed_history = [_history_row(old_text, now_iso, "superseded")]
                matched_set = set(matched)
                facts[:] = [
                    candidate for index, candidate in enumerate(facts)
                    if index not in matched_set
                ]
                self._append_correction_entry(
                    character, facts, new_text, row, history=removed_history,
                )
                changed = True
            else:
                self._append_correction_entry(character, facts, new_text, row)
                changed = True
        elif action == "keep_old":
            if matched:
                target = _entry_with_defaults(facts[matched[0]])
                target["version_history"] = _truncate_history(
                    list(target.get("version_history") or []) + [
                        _history_row(
                            new_text, now_iso, "correction_rejected_observation",
                            correction_id=str(row.get("correction_id") or ""),
                        ),
                    ],
                )
                facts[matched[0]] = target
                changed = True
            else:
                self._append_correction_entry(character, facts, new_text, row)
                changed = True
        else:  # keep_both
            if not any(
                str(candidate.get("text") or "") == new_text for candidate in facts
            ):
                self._append_correction_entry(character, facts, new_text, row)
                changed = True
        return changed

    def _append_correction_entry(
        self,
        character: str,
        facts: list[Any],
        text: str,
        row: Mapping[str, Any],
        *,
        history: Sequence[Mapping[str, Any]] = (),
    ) -> str:
        body = str(text or "").strip()
        if not body:
            raise ValueError("correction text is empty")
        now = self._clock.now()
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
        persona_id = f"manual_{now.strftime(_MANUAL_ID_STAMP_FORMAT)}_{digest}"
        entry = _entry_with_defaults({
            "id": persona_id,
            "text": body,
            "source": "manual",
            "source_id": None,
            "protected": False,
            "suppress": False,
            "merged_from_ids": [],
            "version_history": _truncate_history(history),
        })
        self._barrier.assert_row_allowed(character, entry)
        facts.append(entry)
        return persona_id

    @staticmethod
    def _queue_subject(row: Mapping[str, Any]) -> Any:
        subject = row.get("subject")
        if isinstance(subject, Mapping):
            return subject
        kind = row.get("subject_kind")
        subject_id = row.get("subject_id")
        if isinstance(kind, str) and kind and isinstance(subject_id, str) and subject_id:
            return {"subject_kind": kind, "subject_id": subject_id}
        return None

    @staticmethod
    def _merge_queue_provenance(
        existing: dict[str, Any], incoming: Mapping[str, Any],
    ) -> bool:
        """Fold duplicate-row provenance conservatively (host queue dedupe)."""
        changed = False
        for prefix in ("old", "new"):
            mixed_key = f"{prefix}_speaker_provenance_mixed"
            if incoming.get(mixed_key) is True and existing.get(mixed_key) is not True:
                existing[mixed_key] = True
                for key in ("speaker_id", "speaker_trust"):
                    existing.pop(f"{prefix}_{key}", None)
                changed = True
            trust = incoming.get(f"{prefix}_speaker_trust")
            if isinstance(trust, (int, float)) and not isinstance(trust, bool):
                current = existing.get(f"{prefix}_speaker_trust")
                if not isinstance(current, (int, float)) or isinstance(current, bool):
                    existing[f"{prefix}_speaker_trust"] = float(trust)
                    changed = True
                elif float(trust) < float(current):
                    existing[f"{prefix}_speaker_trust"] = float(trust)
                    changed = True
        return changed

    def _queue_row_blocked(self, character: str, row: Mapping[str, Any]) -> bool:
        for field_name in ("old_text", "new_text"):
            text = row.get(field_name)
            if (
                isinstance(text, str) and text.strip()
                and self._barrier.blocks_text(character, text)
            ):
                return True
        return False

    def _reflection_row(
        self, character: str, reflection_id: str,
    ) -> Mapping[str, Any] | None:
        if not reflection_id or self._reflections is None:
            return None
        try:
            rows = self._reflections.load(character)
        except Exception:  # noqa: BLE001 - evidence stays 0 when unreadable
            return None
        for row in rows or ():
            if isinstance(row, Mapping) and row.get("id") == reflection_id:
                return row
        return None

    # -- internals: load/save -------------------------------------------

    def _load(self, character: str) -> dict[str, Any]:
        doc = self._store.load(character)
        if not isinstance(doc, dict):
            raise PersonaStateError(
                f"{character}: persona view is not an object "
                f"({type(doc).__name__})",
            )
        return doc

    def _load_corrections(self, character: str) -> list[dict[str, Any]]:
        rows = self._store.load_corrections(character)
        return [dict(row) for row in rows or () if isinstance(row, Mapping)]

    def _save_corrections(
        self, character: str, rows: Sequence[Mapping[str, Any]],
    ) -> None:
        self._store.save_corrections(character, [dict(row) for row in rows])

    def _section_header(self, entity: str, language: str, character: str) -> str:
        table = _SECTION_HEADERS.get(language, _SECTION_HEADERS["en"])
        template = table.get(entity)
        if template is None:
            return entity
        return template.format(character=character)

    def _lock(self, character: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(character)
            if lock is None:
                lock = threading.RLock()
                self._locks[character] = lock
            return lock


# -- deletion cleaner --------------------------------------------------------


class PersonaDeletionCleaner:
    """Deletion-engine cleaner for persona.json + persona_archive/.

    Full-source removal discards the entry; partial removal narrows
    merged_from_ids and marks invalidated_sources + suppress + needs_rebuild so
    a rebuild (B06/B07) can repair it later. Nothing is ever re-added: the
    cleaner only drops or narrows.
    """

    def __init__(
        self,
        *,
        store: "PersonaStore",
        reflections: "ReflectionStore | None" = None,
        barrier: DeletionBarrier | None = None,
        clock: Any = None,
    ) -> None:
        self._store = store
        self._reflections = reflections
        self._barrier = barrier
        self._clock = clock

    def cleanup(
        self, character: str, resolved: Mapping[str, Any],
    ) -> dict[str, Any]:
        data = dict(resolved or {})
        persona_ids = _id_set(data.get("persona_ids"))
        narrowed_persona_ids = _id_set(data.get("narrowed_persona_ids"))
        fact_ids = _id_set(data.get("fact_ids"))
        aff_full = _id_set(data.get("reflection_ids"))
        aff_narrow = _id_set(data.get("narrowed_reflection_ids"))
        source_keys = _id_set(
            data.get("fact_source_keys") or data.get("source_keys"),
        )
        episode_ids = _id_set(data.get("episode_ids"))
        subject = data.get("subject")
        now_iso = self._clock.now().isoformat() if self._clock is not None else ""
        # The engine resolves reflection rows before they are deleted; the
        # fact_ids -> source_fact_ids mapping stays as the fallback for callers
        # that only pass fact ids (older records / direct use).
        affected = self._affected_reflections(character, fact_ids)
        doc = self._store.load(character)
        if not isinstance(doc, dict):
            raise PersonaStateError("persona view is not an object during cleanup")
        removed = 0
        narrowed = 0
        removed_ids: list[str] = []
        for entity_key, section in list(doc.items()):
            if not isinstance(section, Mapping):
                continue
            facts = section.get("facts")
            if not isinstance(facts, list):
                continue
            kept: list[Any] = []
            for raw in facts:
                if not isinstance(raw, Mapping):
                    kept.append(raw)
                    continue
                row = _entry_with_defaults(raw)
                if subject not in (None, "", "private") and not row_matches_subject(
                    row, subject,
                ):
                    kept.append(raw)
                    continue
                row_id = str(row.get("id") or "")
                hit_barrier = self._blocks(
                    character, row, source_keys, episode_ids, subject,
                )
                if row_id in persona_ids or hit_barrier:
                    removed += 1
                    if row_id:
                        removed_ids.append(row_id)
                    continue
                sources = source_ids_of_persona(row)
                if sources and aff_full and sources <= aff_full:
                    removed += 1
                    if row_id:
                        removed_ids.append(row_id)
                    continue
                resolved_hits = sources & (aff_full | aff_narrow)
                if (
                    row_id in narrowed_persona_ids
                    or (resolved_hits and not sources <= aff_full)
                ):
                    self._narrow(row, resolved_hits, now_iso)
                    narrowed += 1
                    kept.append(row)
                    continue
                hits = sources & affected
                if not hits:
                    kept.append(raw)
                    continue
                if sources <= affected:
                    removed += 1
                    if row_id:
                        removed_ids.append(row_id)
                    continue
                self._narrow(row, hits, now_iso)
                narrowed += 1
                kept.append(row)
            new_section = dict(section) if isinstance(section, Mapping) else {}
            new_section["facts"] = kept
            doc[entity_key] = new_section
        if removed or narrowed:
            self._store.save(character, doc)
        archive_removed = 0
        if removed_ids:
            archive_removed = int(
                self._store.delete_from_archive(character, removed_ids),
            )
        return {
            "status": "completed",
            "detail": {
                "persona_removed": removed,
                "persona_narrowed": narrowed,
                "archive_removed": archive_removed,
            },
        }

    def _blocks(
        self,
        character: str,
        row: Mapping[str, Any],
        source_keys: set[str],
        episode_ids: set[str],
        subject: Any,
    ) -> bool:
        if self._barrier is not None and self._barrier.blocks_row(
            character, row, subject=subject,
        ):
            return True
        if source_keys and (_row_source_keys(row) & source_keys):
            return True
        if episode_ids and (episode_ids_of(row) & episode_ids):
            return True
        return False

    @staticmethod
    def _narrow(row: dict[str, Any], hits: set[str], now_iso: str) -> None:
        """Host narrowing rule (memory/persona/persistence.py:597-651)."""
        merged = [
            source for source in _str_list(row.get("merged_from_ids"))
            if source not in hits
        ]
        source_id = row.get("source_id")
        if isinstance(source_id, str) and source_id in hits:
            row["source_id"] = merged[0] if merged else None
            row["merged_from_ids"] = merged[1:] if merged else []
        else:
            row["merged_from_ids"] = merged
        row["invalidated_sources"] = sorted(
            set(_str_list(row.get("invalidated_sources"))) | hits,
        )
        row["needs_rebuild"] = True
        row["suppress"] = True
        row["suppressed_at"] = now_iso
        row["invalidated_at"] = now_iso
        row["invalidated_reason"] = "deletion"

    def _affected_reflections(
        self, character: str, fact_ids: set[str],
    ) -> set[str]:
        if not fact_ids or self._reflections is None:
            return set()
        rows = self._reflections.load(character) or ()
        out: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id:
                continue
            if source_ids_of_reflection(row) & fact_ids:
                out.add(row_id)
        return out


# -- correction invalidator --------------------------------------------------


class PersonaInvalidator:
    """Correction derived-invalidator: reflection ids -> persona entries."""

    def __init__(
        self,
        *,
        store: "PersonaStore",
        reflections: "ReflectionStore | None" = None,
        barrier: DeletionBarrier | None = None,
        clock: Any = None,
    ) -> None:
        self._store = store
        self._reflections = reflections
        self._barrier = barrier
        self._clock = clock

    def invalidate(
        self,
        character: str,
        fact_ids: Sequence[str],
        *,
        invalidated_at: str,
        reason: str,
        correction_id: str,
    ) -> dict[str, Any]:
        affected = self._affected_reflections(character, _id_set(fact_ids))
        if not affected:
            return {"persona_ids": []}
        doc = self._store.load(character)
        if not isinstance(doc, dict):
            raise PersonaStateError(
                "persona view is not an object during invalidation",
            )
        changed = False
        persona_ids: list[str] = []
        for _entity_key, section in list(doc.items()):
            if not isinstance(section, Mapping):
                continue
            facts = section.get("facts")
            if not isinstance(facts, list):
                continue
            for index, raw in enumerate(facts):
                if not isinstance(raw, Mapping):
                    continue
                row = _entry_with_defaults(raw)
                sources = source_ids_of_persona(row)
                hits = sources & affected
                if not hits:
                    continue
                if self._barrier is not None and self._barrier.blocks_row(
                    character, row,
                ):
                    continue  # deleted content is the cleaner's job
                row["invalidated_at"] = invalidated_at
                row["invalidated_by"] = correction_id
                row["invalidated_reason"] = reason
                row["invalidated_sources"] = sorted(
                    set(_str_list(row.get("invalidated_sources"))) | hits,
                )
                row["version_history"] = _truncate_history(
                    list(row.get("version_history") or []) + [
                        _history_row(
                            "", invalidated_at, reason,
                            correction_id=correction_id,
                            source_reflection_ids=sorted(hits),
                        ),
                    ],
                )
                row["suppress"] = True
                row["suppressed_at"] = invalidated_at
                row["needs_rebuild"] = bool(sources - affected)
                facts[index] = row
                changed = True
                row_id = str(row.get("id") or "")
                if row_id:
                    persona_ids.append(row_id)
        if changed:
            self._store.save(character, doc)
        return {"persona_ids": persona_ids}

    def _affected_reflections(
        self, character: str, fact_ids: set[str],
    ) -> set[str]:
        if not fact_ids or self._reflections is None:
            return set()
        rows = self._reflections.load(character) or ()
        out: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id:
                continue
            if source_ids_of_reflection(row) & fact_ids:
                out.add(row_id)
        return out


__all__ = [
    "CORRECTION_ACTIONS",
    "CORRECTION_BATCH_LIMIT",
    "CORRECTION_MAX_ATTEMPTS",
    "CORRECTION_MODEL_PURPOSE",
    "CORRECTION_MODEL_TIER",
    "DEFAULT_CORRECTION_ACTION",
    "DISPUTATION_HALF_LIFE_DAYS",
    "PERSONA_ENTRY_EVENT",
    "PERSONA_ENTRY_FIELDS",
    "PERSONA_EVIDENCE_EVENT",
    "PERSONA_EVIDENCE_FIELDS",
    "PERSONA_FACT_ADDED_EVENT",
    "PERSONA_PROTECTED_SOURCES",
    "PERSONA_TERMINAL_SOURCES",
    "PERSONA_VERSION_HISTORY_MAX",
    "PersonaDeletionCleaner",
    "PersonaInvalidator",
    "PersonaReplayError",
    "PersonaService",
    "PersonaStateError",
    "REINFORCEMENT_HALF_LIFE_DAYS",
    "estimate_tokens",
    "evidence_score",
]
