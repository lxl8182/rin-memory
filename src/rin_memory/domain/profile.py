"""Interest evidence for the existing `about_owner` section (RO03).

The user picture is *not* a module, a page or a generated profile any more: it
converges on the editable `about_owner` persona section (RO01). This module is
the evidence layer underneath that section, and it does exactly two things:

1. **Records signals from user utterances only.** A signal says what kind of
   preference the user expressed and which utterances express it:

   | signal    | means                                    | feeds `about_owner` |
   |-----------|------------------------------------------|----------------------|
   | `like`    | an explicit, lasting liking              | yes (stable)         |
   | `dislike` | an explicit, lasting dislike             | yes (avoid)          |
   | `curious` | something new the user wants to try soon | **no** -- it is a    |
   |           |                                          | near-term interest,  |
   |           |                                          | not a trait          |
   | `paused`  | the user asked to drop the topic         | no -- it is a        |
   |           |                                          | constraint           |

   "最近想试胶片" is therefore `curious` and stays out of the section;
   "我一直喜欢胶片摄影" is `like` and may become part of it. Merging the two is
   the failure this split exists to prevent: a passing curiosity written into
   the persona would be recited as a trait for months.

2. **Projects the stable part as *material*, never as prose.** `material()`
   returns stable likes / dislikes / lasting preferences with their source
   references so the section can be written (by the user or by evolution, RO01
   capability) from evidence. This module never edits the section text itself:
   the projection is not a second source of truth.

Every row is bound to the utterances that support it, and a row whose sources
are all gone is dropped on read, not just on delete. Two protections matter more
than the rest:

* **no source, no signal.** A candidate whose `evidence_refs` do not resolve to
  user utterances is discarded. An unattributed preference is worse than no
  preference: it cannot be corrected, deleted or audited, and it is exactly what
  would end up recited as a lasting trait;
* **one voice, one signal.** Rows are deduped by `(subject, topic, signal)` and
  the supporting keys are merged, so a mirror turn, a re-extraction of the same
  sentence or a summary of the same utterance adds no extra support.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from ..invalidation import normalize_text

#: Signal kinds. The vocabulary is closed: an unknown kind is dropped rather
#: than stored, so a model cannot invent a fifth meaning for a stored row.
SIGNAL_LIKE = "like"
SIGNAL_CURIOUS = "curious"
SIGNAL_DISLIKE = "dislike"
SIGNAL_PAUSED = "paused"
SIGNAL_KINDS = (SIGNAL_LIKE, SIGNAL_CURIOUS, SIGNAL_DISLIKE, SIGNAL_PAUSED)

#: Kinds that may feed the `about_owner` section (stable traits only).
SECTION_KINDS = (SIGNAL_LIKE, SIGNAL_DISLIKE)

#: How long a `curious` signal stays "recent". "最近想试胶片" stops being news
#: after a month; a lasting `like` has no such window.
CURIOUS_WINDOW_DAYS = 30

#: Caps. A topic/keyword list is prompt material, not a document.
MAX_TOPIC_CHARS = 120
MAX_KEYWORD_CHARS = 60
MAX_KEYWORDS = 8
MAX_SOURCE_KEYS = 32
MAX_SECTION_ITEMS = 12

#: Row statuses. Only `active` rows are ever projected; the others are kept so a
#: deletion/correction stays auditable instead of vanishing silently.
STATUS_ACTIVE = "active"
STATUS_INVALIDATED = "invalidated"

#: Appended to the extraction prompt (both the batched and the single-turn
#: path). It is deliberately explicit about the four kinds and about citations:
#: a signal without a citation is discarded by the service, so the prompt must
#: ask for one instead of letting the model omit it.
SIGNAL_PROMPT = (
    "\n\n我同时整理对方表达的兴趣信号。最终输出采用 JSON 对象："
    '{"facts":[上述事实],"interest_signals":[{"topic":"具体主题",'
    '"keywords":["关键词"],"signal":"like|curious|dislike|paused",'
    '"evidence_refs":[原话编号]}]}；有事项时同样保留 state_updates 字段。'
    "like 表示对方明确表达的长期喜爱；curious 表示最近想试、刚产生的兴趣；"
    "dislike 表示明确的长期厌恶；paused 表示对方希望暂停这个话题。"
    "我依据对方在本轮/本批的直接表达选择信号，"
    "兴趣信号对应对方本人明确表达的喜好、厌恶和探索意愿；"
    "我把展示作品、叙述经历或询问我的感受保留在事实与经历中，我自己的好奇和选择记为 neko 的表达。"
    "evidence_refs 填写实际支持它的原话编号；其他材料帮助我理解话题背景。"
    "兴趣信号为空时使用空数组。单轮原话编号为 1。"
)


# -- ids and time -------------------------------------------------------------


def signal_id(subject: str, topic: str) -> str:
    """Stable id for `(subject, normalized topic)`.

    Dedup is by topic, not by row id, so the id must not include the source: a
    second utterance about the same topic has to land on the same row and merge
    its keys. This is what makes "the same sentence said twice" one signal.
    """
    seed = json.dumps(
        [str(subject or "private"), normalize_text(topic)], ensure_ascii=False,
    )
    return "sig_" + hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:20]


def _now_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def _parse_stamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _now_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _now_utc(datetime.fromisoformat(value.strip()))
    except ValueError:
        return None


# -- service ------------------------------------------------------------------


class InterestService:
    """Record, invalidate and project interest signals for one character.

    `barrier` is the deletion barrier: a row whose sources are covered by a
    tombstone is never projected, and `cleanup` marks rows invalidated when the
    deletion reaches them, so the file itself stops claiming support.
    """

    def __init__(
        self,
        *,
        store: Any,
        barrier: Any = None,
        clock: Any = None,
        facts: Any = None,
    ) -> None:
        self.store = store
        self._barrier = barrier
        self._clock = clock
        self._facts = facts

    # -- writing ---------------------------------------------------------

    def record(
        self,
        character: str,
        signals: Any,
        utterances: Sequence[Mapping[str, Any]],
    ) -> int:
        """Bind model-nominated signals to the utterances that support them.

        `utterances` is the same per-utterance list extraction already builds
        for facts (`index` / `has_user` / `source_keys` / `occurred_at` /
        `metadata`), so a signal and a fact cite the window the same way.
        Returns how many rows were written; a candidate with no resolvable
        user utterance, no subject or no topic is dropped, never downgraded to
        a source-less row.
        """
        if not isinstance(signals, Sequence) or isinstance(signals, (str, bytes)):
            return 0
        by_index: dict[int, dict[str, Any]] = {}
        by_key: dict[str, dict[str, Any]] = {}
        for item in utterances or ():
            if not isinstance(item, Mapping) or not item.get("has_user"):
                continue
            row = dict(item)
            try:
                by_index[int(row["index"])] = row
            except (KeyError, TypeError, ValueError):
                pass
            for key in row.get("source_keys") or ():
                if isinstance(key, str) and key.strip():
                    by_key.setdefault(key.strip(), row)
        if not by_index and not by_key:
            return 0
        rows: list[dict[str, Any]] = []
        for raw in signals:
            row = self._bound_row(character, raw, by_index, by_key)
            if row is not None:
                rows.append(row)
        if not rows:
            return 0
        return int(self.store.put(character, self._merged(character, rows)) or 0)

    def _merged(
        self, character: str, rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Fold new candidates into the stored row of the same id.

        Two things must not become two supports: a candidate repeated inside one
        call, and a candidate that restates a topic already recorded. Both are
        merged by id, unioning the source keys, so one topic keeps one row whose
        keys are exactly the distinct utterances that ever stated it -- a mirror
        turn, a summary and a re-extraction of the same sentence all land on the
        same key.

        A row that was retired is *not* revived by a replay of its own old
        material: the union only grows through genuinely new keys, and the read
        path still drops anything a tombstone covers. A later, unprompted
        restatement also arrives with a new key, which is a new voice and is
        allowed to speak again.
        """
        stored = {}
        try:
            stored = self.store.load(character).get("items") or {}
        except Exception:  # noqa: BLE001 - a damaged store still accepts new rows
            stored = {}
        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            existing = merged.get(str(row["id"]))
            previous = stored.get(str(row["id"]))
            base = dict(existing or previous or row)
            keys: list[str] = []
            for source in (previous or {}, existing or {}, row):
                for key in source.get("source_event_keys") or ():
                    if isinstance(key, str) and key.strip() and key.strip() not in keys:
                        keys.append(key.strip())
            base["source_event_keys"] = keys[:MAX_SOURCE_KEYS]
            if previous is not None:
                # Keep what the row already knew: its first sighting, its
                # keywords and its invalidation history.
                base.setdefault("keywords", row.get("keywords") or [])
                if not base.get("keywords"):
                    base["keywords"] = row.get("keywords") or []
                base["recorded_at"] = previous.get("recorded_at") or base.get("recorded_at")
                if previous.get("status") == STATUS_INVALIDATED and not _new_keys(
                    previous, row,
                ):
                    base["status"] = STATUS_INVALIDATED
                    base["invalidated_at"] = previous.get("invalidated_at")
                    base["invalidation_reason"] = previous.get("invalidation_reason")
            stamps = [
                stamp for stamp in (
                    base.get("occurred_at"), row.get("occurred_at"),
                ) if isinstance(stamp, str) and stamp
            ]
            if stamps:
                base["occurred_at"] = max(stamps)
            merged[str(row["id"])] = base
        return list(merged.values())

    def _bound_row(
        self,
        character: str,
        raw: Any,
        by_index: Mapping[int, Mapping[str, Any]],
        by_key: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any] | None:
        """One validated row, or None when it cannot be attributed."""
        if not isinstance(raw, Mapping):
            return None
        topic = _clean_topic(raw.get("topic"))
        kind = str(raw.get("signal") or "").strip().lower()
        if not topic or kind not in SIGNAL_KINDS:
            return None
        refs = raw.get("evidence_refs")
        if not isinstance(refs, (list, tuple)) or not refs:
            # No citation at all: discard, do not guess a source.
            return None
        cited: list[dict[str, Any]] = []
        for ref in refs:
            if isinstance(ref, bool):
                continue
            hit: Mapping[str, Any] | None = None
            if isinstance(ref, int):
                hit = by_index.get(ref)
            elif isinstance(ref, str):
                stripped = ref.strip()
                hit = by_key.get(stripped)
                if hit is None and stripped.isdigit():
                    hit = by_index.get(int(stripped))
            if hit is not None and hit not in cited:
                cited.append(dict(hit))
        if not cited:
            return None
        keys: list[str] = []
        for item in cited:
            for key in item.get("source_keys") or ():
                if isinstance(key, str) and key.strip() and key.strip() not in keys:
                    keys.append(key.strip())
        keys = keys[:MAX_SOURCE_KEYS]
        if not keys:
            return None
        if self._blocked(character, keys, topic):
            return None
        metadata = dict(cited[-1].get("metadata") or {})
        subject = str(metadata.get("subject") or "private")
        occurred = max(
            (_parse_stamp(item.get("occurred_at")) for item in cited),
            key=lambda value: value.timestamp() if value else 0.0,
            default=None,
        )
        keywords: list[str] = []
        for word in raw.get("keywords") or ():
            if isinstance(word, str) and word.strip():
                cleaned = " ".join(word.split())[:MAX_KEYWORD_CHARS]
                if cleaned not in keywords:
                    keywords.append(cleaned)
        return {
            "id": signal_id(subject, topic),
            "topic": topic,
            "keywords": keywords[:MAX_KEYWORDS],
            "signal": kind,
            "subject": subject,
            "applies_to": dict(metadata.get("applies_to") or {}),
            "source_event_keys": keys,
            "status": STATUS_ACTIVE,
            "occurred_at": occurred.isoformat() if occurred else None,
            "recorded_at": self._now_iso(),
        }

    def _now_iso(self) -> str | None:
        try:
            return self._now().isoformat()
        except Exception:  # noqa: BLE001 - a broken clock must not lose a signal
            return None

    def _now(self) -> datetime:
        clock = self._clock
        if clock is not None and hasattr(clock, "now"):
            try:
                return _now_utc(clock.now())
            except Exception:  # noqa: BLE001 - fall back to the wall clock
                pass
        return datetime.now(timezone.utc)

    def _blocked(
        self, character: str, keys: Sequence[str], topic: str,
    ) -> bool:
        """Whether a tombstone already covers this support.

        A source key is the authoritative test; the topic text is checked too,
        because a deletion may have been registered against the sentence rather
        than against the key.
        """
        barrier = self._barrier
        if barrier is None:
            return False
        try:
            for key in keys:
                if barrier.blocks_source_key(character, key):
                    return True
            return bool(barrier.blocks_text(character, topic))
        except Exception:  # noqa: BLE001 - barrier failure keeps the write
            return False

    # -- invalidation ----------------------------------------------------

    def invalidate(
        self,
        character: str,
        fact_ids: Sequence[str],
        *,
        invalidated_at: str | None = None,
        reason: str = "user_correction",
        correction_id: str = "",
    ) -> dict[str, Any]:
        """Correction hook: retire signals that rested on the replaced facts.

        Registered as a derived invalidator, so it is called with the fact ids a
        correction has just invalidated. Signals are looked up through those
        facts' source keys, which is the only link between a preference and the
        fact it was stated as.
        """
        keys: list[str] = []
        for fact_id in fact_ids or ():
            for row in self._fact_rows(character):
                if str(row.get("id") or "") != str(fact_id):
                    continue
                for key in row.get("source_event_keys") or ():
                    if isinstance(key, str) and key.strip() and key.strip() not in keys:
                        keys.append(key.strip())
        return self._retire(
            character,
            keys,
            at=invalidated_at or self._now_iso(),
            reason=reason,
            correction_id=correction_id,
        )

    def cleanup(self, character: str, resolved: Mapping[str, Any]) -> dict[str, Any]:
        """Deletion cleaner: retire signals whose support the deletion removed.

        Runs inside the deletion's persona step, which is where every derived
        row is retired. The barrier has already been registered at that point,
        so reads have stopped serving these signals; this step makes the file
        agree with the barrier instead of leaving rows that only *look* alive.
        """
        keys: list[str] = []
        for name in ("fact_source_keys", "original_source_keys", "source_keys"):
            for key in (resolved or {}).get(name) or ():
                if isinstance(key, str) and key.strip() and key.strip() not in keys:
                    keys.append(key.strip())
        for episode_key in ("deletion_id",):
            value = (resolved or {}).get(episode_key)
            if isinstance(value, str) and value.strip():
                keys.append(value.strip())
        outcome = self._retire(
            character, keys, at=self._now_iso(), reason="user_deletion",
        )
        return {"status": "completed", "detail": outcome}

    def _retire(
        self,
        character: str,
        keys: Sequence[str],
        *,
        at: str | None,
        reason: str,
        correction_id: str = "",
    ) -> dict[str, Any]:
        """Mark matching active rows invalidated; never deletes the text."""
        wanted = {key for key in keys if isinstance(key, str) and key.strip()}
        document = self.store.load(character)
        changed: list[dict[str, Any]] = []
        for row in (document.get("items") or {}).values():
            if str(row.get("status") or STATUS_ACTIVE) != STATUS_ACTIVE:
                continue
            sources = {
                key for key in row.get("source_event_keys") or ()
                if isinstance(key, str) and key.strip()
            }
            if not (sources & wanted):
                continue
            retired = dict(row)
            retired["status"] = STATUS_INVALIDATED
            retired["invalidated_at"] = at
            retired["invalidation_reason"] = reason
            if correction_id:
                retired["invalidated_by"] = correction_id
            changed.append(retired)
        if not changed:
            return {"interest_signals": 0}
        self.store.put(character, changed)
        return {"interest_signals": len(changed)}

    # -- projection ------------------------------------------------------

    def snapshot(
        self,
        character: str,
        *,
        subject: str = "private",
        scene: Any = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """The current signals for one subject/scene, as the service reports them.

        Read path rules, in order: the row must be active, in this subject, not
        covered by a tombstone, in this scene, and (for `curious`) still recent.
        The returned `version` is the data version that was read, so a caller
        can tell two reads of the same generation turn apart from two reads
        across a write.
        """
        document = self.store.load(character)
        items = [
            dict(row) for row in (document.get("items") or {}).values()
            if self._visible(character, row, subject, scene, now)
        ]
        items.sort(key=lambda row: (
            _KIND_ORDER.get(str(row.get("signal") or ""), 9),
            str(row.get("occurred_at") or ""),
        ), reverse=False)
        return {
            "version": int(document.get("version") or 0),
            "items": items,
            "stable_interests": [
                row for row in items if row.get("signal") == SIGNAL_LIKE
            ],
            "recent_interests": [
                row for row in items if row.get("signal") == SIGNAL_CURIOUS
            ],
            "avoid_topics": [
                row for row in items
                if row.get("signal") in (SIGNAL_DISLIKE, SIGNAL_PAUSED)
            ],
        }

    def material(
        self,
        character: str,
        *,
        subject: str = "private",
        scene: Any = None,
        language: str = "zh",
    ) -> dict[str, Any]:
        """Structured material for the `about_owner` section (never prose).

        Only stable kinds appear (`like` / `dislike`): a near-term curiosity or
        a "do not mention this" constraint is not a trait, so it is reported in
        the snapshot but never offered as section material. The caller writes
        the section; this returns the evidence it should be written from.
        """
        snapshot = self.snapshot(character, subject=subject, scene=scene)
        stable: list[dict[str, Any]] = []
        for row in snapshot["items"]:
            if row.get("signal") not in SECTION_KINDS:
                continue
            stable.append({
                "id": row.get("id"),
                "topic": row.get("topic"),
                "keywords": list(row.get("keywords") or ()),
                "signal": row.get("signal"),
                "source_event_keys": list(row.get("source_event_keys") or ()),
                "occurred_at": row.get("occurred_at"),
            })
        return {
            "version": snapshot["version"],
            "section_id": "about_owner",
            "source": "interest_signals",
            "items": stable[:MAX_SECTION_ITEMS],
            "excluded": {
                "curious": len(snapshot["recent_interests"]),
                "avoid_topics": len(snapshot["avoid_topics"]),
            },
            "language": str(language or "zh"),
        }

    def paused_topics(
        self, character: str, *, subject: str = "private", scene: Any = None,
    ) -> list[str]:
        """Topics the user asked to drop.

        Reported separately from `dislike` because it is a *constraint*, not a
        preference: discovery/proactive sharing (T6/T7) must be able to consult
        it to stay quiet about a paused topic without treating the topic as
        something the user hates.
        """
        return [
            str(row.get("topic") or "")
            for row in self.snapshot(character, subject=subject, scene=scene)["items"]
            if row.get("signal") == SIGNAL_PAUSED and str(row.get("topic") or "")
        ]

    def _visible(
        self,
        character: str,
        row: Any,
        subject: str,
        scene: Any,
        now: datetime | None,
    ) -> bool:
        if not isinstance(row, Mapping):
            return False
        if str(row.get("status") or STATUS_ACTIVE) != STATUS_ACTIVE:
            return False
        if str(row.get("subject") or "private") != str(subject or "private"):
            return False
        if not _clean_topic(row.get("topic")):
            return False
        if self._blocked(character, list(row.get("source_event_keys") or ()),
                         str(row.get("topic") or "")):
            return False
        if not self._in_scene(row, scene):
            return False
        if str(row.get("signal") or "") == SIGNAL_CURIOUS:
            occurred = _parse_stamp(row.get("occurred_at") or row.get("recorded_at"))
            if occurred is None:
                # Without a time the window cannot be checked; a curiosity
                # cannot be shown to be recent, so it is not shown at all.
                return False
            if occurred < self._now() - timedelta(days=CURIOUS_WINDOW_DAYS):
                return False
        return True

    @staticmethod
    def _in_scene(row: Mapping[str, Any], scene: Any) -> bool:
        applies_to = row.get("applies_to")
        if not isinstance(applies_to, Mapping) or not applies_to:
            return True
        try:
            from .recall import entry_applies_to_scene
        except Exception:  # noqa: BLE001 - a missing helper must not hide a row
            return True
        return bool(entry_applies_to_scene(row, scene))

    def _fact_rows(self, character: str) -> list[dict[str, Any]]:
        if self._facts is None:
            return []
        try:
            return [dict(row) for row in self._facts.load(character) or ()
                    if isinstance(row, Mapping)]
        except Exception:  # noqa: BLE001
            return []

    # -- deletion barrier hooks ------------------------------------------

    def blocks_row(self, character: str, row: Any) -> bool:
        """Whether a signal row is covered by a tombstone (barrier shape)."""
        if not isinstance(row, Mapping):
            return True
        return self._blocked(
            character,
            list(row.get("source_event_keys") or ()),
            str(row.get("topic") or ""),
        )


#: Projection order: stable preference first, then recent curiosity, then the
#: constraints. Readers that take the head of the list get the part that may
#: become a trait.
_KIND_ORDER = {
    SIGNAL_LIKE: 0,
    SIGNAL_DISLIKE: 1,
    SIGNAL_CURIOUS: 2,
    SIGNAL_PAUSED: 3,
}


def _empty_snapshot() -> dict[str, Any]:
    return {
        "version": 0,
        "items": [],
        "stable_interests": [],
        "recent_interests": [],
        "avoid_topics": [],
    }


def _new_keys(
    previous: Mapping[str, Any], candidate: Mapping[str, Any],
) -> bool:
    """Whether a candidate cites a source the retired row never had."""
    old = {
        key for key in previous.get("source_event_keys") or ()
        if isinstance(key, str) and key.strip()
    }
    return any(
        isinstance(key, str) and key.strip() and key.strip() not in old
        for key in candidate.get("source_event_keys") or ()
    )


def _clean_topic(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:MAX_TOPIC_CHARS]


__all__ = [
    "CURIOUS_WINDOW_DAYS",
    "InterestService",
    "SECTION_KINDS",
    "SIGNAL_CURIOUS",
    "SIGNAL_DISLIKE",
    "SIGNAL_KINDS",
    "SIGNAL_LIKE",
    "SIGNAL_PAUSED",
    "SIGNAL_PROMPT",
    "signal_id",
]
