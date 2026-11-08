"""The three rebuildable working summaries plus the episode view (M5).

Design section 5 says the working summaries are *projections*: "they should all
be rebuildable from the authoritative records, and must be rebuilt when a source
goes away. Do not let a summary become a second fact source that cannot be
corrected."

That sentence decides the whole shape of this module:

* every section is derived from records that already exist (published teaching
  rules, growth/reflection rows, active facts, companion state, mention
  directives). Nothing here is stored as truth;
* the read path re-derives on every call, so a deleted source disappears from
  the summary in the same breath as from the store -- there is no cache to go
  stale and nothing to resurrect;
* the on-disk 'working_summary.json' is an audit cache only (see
  persistence.working_summary): it records what the last background round
  computed and carries the 'rebuild_key' of its inputs, but no read path
  consumes it.

Three sections, matching design section 10.1's "current working summary" layer:

    growth          already-formed changes, shared idioms, important
                    relationship nodes -- from published rules, growth and
                    reflection rows, and relationship persona entries
    important_facts what is useful to keep in mind about this person, scope
                    filtered -- from the currently valid facts
    open_threads    what is being worked on, agreements, progress, follow-up
                    time and mention restrictions -- from companion state

The fourth projection, 'episode', is what design section 11.1 calls "one
experience": participants, both sides' behaviour, the actual result, the
terminal state, the next follow-up time and any mention restriction. It is
derived from the facts that carry an episode id plus recent messages, state and
directives rather than written at extraction time, because those are precisely
the fields that form *over later turns* -- an extraction-time snapshot could
only ever record what the first message saw.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..invalidation import normalize_text
from ..ports import Clock
from .recall import (
    HIDDEN_STATUSES,
    content_terms,
    episode_id_of,
    entry_applies_to_scene,
    normalize_scene,
)

#: Section names, in prompt order. 'episode' is the per-experience view.
SECTION_NAMES = ("growth", "important_facts", "open_threads", "episode")

#: Statuses that mean "this row no longer describes the present".
_DEAD_STATUSES = frozenset(HIDDEN_STATUSES) | frozenset({
    "withdrawn", "invalidated", "superseded", "archived", "denied",
})

#: Relationship entities that belong in the growth/relationship summary.
_RELATIONSHIP_ENTITIES = frozenset({"relationship", "shared", "us", "we"})
#: Growth kinds rendered as "what changed between us" rather than a fact.
_GROWTH_KINDS = frozenset({"habit", "interest", "experience", "relationship"})

#: Evidence kinds that count as "something actually happened" for an episode.
_RESULT_KINDS = frozenset({"observed_action", "tool_result", "explicit_feedback"})
_ASSISTANT_KINDS = frozenset({"agent_expression", "quoted_example"})

#: State kinds that carry an unfinished thing worth continuing.
_OPEN_STATE_KINDS = ("progress", "concerns", "thoughts")
#: Kinds whose items may be waiting on someone (vs. plain thoughts).
_WAITING_KINDS = frozenset({"progress", "concerns"})

_MAX_ITEMS_PER_SECTION = 6
#: Facts offered to one generated turn. Three is the ceiling, not a target: a
#: turn that shares no terms with anything stored gets none (RO03).
_TURN_FACT_LIMIT = 3
_MAX_EPISODES = 3
_MAX_ITEMS_PER_EPISODE = 6
_TEXT_LIMIT = 240

_LABELS = {
    "zh": {
        "growth": "相处与成长",
        "important_facts": "重要事实",
        "open_threads": "未完话题",
        "episode": "这段经历",
        "from_rule": "约好",
        "from_growth": "变化",
        "from_reflection": "认识",
        "from_relation": "关系",
        "next": "下一步",
        "open": "进行中",
        "waiting": "等消息",
        "done": "已结束",
        "participants": "参与者",
        "user_side": "主人这边",
        "assistant_side": "我这边",
        "result": "结果",
        "state": "终态",
        "followup": "后续时间",
        "restrict": "别提",
        "sep": "：",
        "comma": "，",
        "none": "",
    },
    "en": {
        "growth": "How we are together",
        "important_facts": "What matters about you",
        "open_threads": "Still open",
        "episode": "That episode",
        "from_rule": "agreed",
        "from_growth": "changed",
        "from_reflection": "learned",
        "from_relation": "between us",
        "next": "next",
        "open": "open",
        "waiting": "waiting",
        "done": "done",
        "participants": "who",
        "user_side": "your side",
        "assistant_side": "my side",
        "result": "result",
        "state": "state",
        "followup": "follow up",
        "restrict": "do not mention",
        "sep": ": ",
        "comma": ", ",
        "none": "",
    },
}


@dataclass(frozen=True)
class WorkingSummarySection:
    """One rendered section: a compact block plus the rows it came from."""

    name: str
    block: str = ""
    items: tuple[dict[str, Any], ...] = ()
    source_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "block": self.block,
            "items": [dict(item) for item in self.items],
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class WorkingSummary:
    """All sections for one (character, subject, scene) and their rebuild key."""

    character: str
    subject: str
    scene: Mapping[str, str] = field(default_factory=dict)
    sections: Mapping[str, WorkingSummarySection] = field(default_factory=dict)
    rebuild_key: str = ""
    source_versions: Mapping[str, Any] = field(default_factory=dict)
    built_at: str | None = None
    episode: Mapping[str, Any] | None = None

    def section(self, name: str) -> WorkingSummarySection:
        found = self.sections.get(name)
        return found if found is not None else WorkingSummarySection(name)

    @property
    def block(self) -> str:
        """The three summary sections as one prompt fragment (episode excluded)."""
        parts = [
            self.section(name).block for name in SECTION_NAMES
            if name != "episode"
        ]
        return "\n".join(part for part in parts if part).strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "character": self.character,
            "subject": self.subject,
            "scene": dict(self.scene),
            "sections": {
                name: section.to_dict()
                for name, section in self.sections.items()
            },
            "block": self.block,
            "rebuild_key": self.rebuild_key,
            "source_versions": dict(self.source_versions),
            "built_at": self.built_at,
            "episode": dict(self.episode) if self.episode else None,
        }


# -- text helpers -----------------------------------------------------------


def _clean(value: Any, limit: int = _TEXT_LIMIT) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text[:limit]


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _labels(language: Any) -> Mapping[str, str]:
    key = str(language or "zh").strip().lower()
    if key in _LABELS:
        return _LABELS[key]
    if key.startswith("en"):
        return _LABELS["en"]
    return _LABELS["zh"]


def _subject_of(row: Mapping[str, Any]) -> str:
    return str(row.get("subject") or "private")


def _in_subject(row: Mapping[str, Any], subjects: tuple[str, ...] | None) -> bool:
    if subjects is None:
        return True
    return _subject_of(row) in subjects


def _status_alive(row: Mapping[str, Any]) -> bool:
    status = str(row.get("status") or "").strip().lower()
    if status and status in _DEAD_STATUSES:
        return False
    if row.get("invalidated_at") or row.get("needs_rebuild"):
        return False
    return True


def _stamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


class WorkingSummaryService:
    """Derive the three working summaries and the episode view from records.

    Every dependency is optional so the projector is usable (and testable) in
    isolation; a missing source simply leaves its section empty rather than
    raising. The service is read-only: it never writes a store, never calls a
    model, and never raises into the caller.
    """

    def __init__(
        self,
        *,
        character: str = "",
        clock: Clock | None = None,
        facts: Any = None,
        reflections: Any = None,
        persona: Any = None,
        state: Any = None,
        teaching: Any = None,
        directives: Any = None,
        recent: Any = None,
        barrier: Any = None,
        card: Any = None,
        settings: Any = None,
    ) -> None:
        self._character = str(character or "")
        self._clock = clock
        self._facts = facts
        self._reflections = reflections
        self._persona = persona
        self._state = state
        self._teaching = teaching
        self._directives = directives
        self._recent = recent
        self._barrier = barrier
        self._card = card
        self._settings = settings

    # -- public API ------------------------------------------------------

    def build(
        self,
        character: Any,
        *,
        subject: Any = "private",
        scene: Any = None,
        language: str = "zh",
        max_items: int | None = None,
        query: str | None = None,
    ) -> WorkingSummary:
        """Derive every section for one turn (never raises, always returns).

        `query` is the turn's own words (RO03). It changes **only** the
        `important_facts` section, and it is the difference between the two
        callers that exist:

        * `query is None` -- the administrative view (`/v1/working-summary`,
          the background audit cache): "what do we know about this person",
          importance ordered, `max_items` rows;
        * `query` is a string -- the per-turn view: "what of that is relevant to
          *this* message". Relevance is a gate, not a tiebreak, so a turn with
          no shareable terms (a bare "在吗", an image-only turn) gets **zero**
          facts instead of the three most important ones. That substitution is
          the bug this parameter exists to fix: "主人给了我服务器" used to be
          injected into every turn, including a plain picture, and the plugin
          renders any fact it has not seen as "[重要事实]".

        The other sections keep their existing contract: `open_threads` still
        carries the mention restriction, and corrections/directives are read by
        recall as their own blocks, so no limit here can drop a correction, a
        retraction or a "do not mention this" rule.
        """
        name = str(character or self._character or "").strip()
        subject_key = str(subject or "private").strip() or "private"
        labels = _labels(language)
        limit = max(1, int(max_items or _MAX_ITEMS_PER_SECTION))
        versions = self._source_versions(name, subject_key)
        try:
            sections = {
                "growth": self._growth_section(
                    name, subject_key, scene, labels, limit,
                ),
                "important_facts": self._facts_section(
                    name, subject_key, scene, labels,
                    min(limit, _TURN_FACT_LIMIT) if query is not None else limit,
                    query=query,
                ),
                "open_threads": self._threads_section(
                    name, subject_key, scene, labels, limit,
                ),
            }
        except Exception:  # noqa: BLE001 - a summary must never fail a turn
            sections = {name_: WorkingSummarySection(name_, "", (), ())
                        for name_ in ("growth", "important_facts", "open_threads")}
        episodes = self._episodes(name, subject_key, scene, labels)
        episode_section = self._episode_section(episodes, labels)
        sections["episode"] = episode_section
        return WorkingSummary(
            character=name,
            subject=subject_key,
            scene=normalize_scene(scene) if isinstance(scene, Mapping) else {},
            sections=sections,
            rebuild_key=self._rebuild_key(versions),
            source_versions=versions,
            built_at=self._now_iso(),
            episode=(dict(episodes[0]) if episodes else None),
        )

    def growth_candidates(self, character, subject, scene):
        return [dict(row, _tier="growth", text=str(row.get("claim") or row.get("text") or ""))
                for row in self._growth_rows(character, subject, scene)]

    def selected(self, character, subject, scene, language, items, *, episode=None):
        """Project the turn selection; the administrative build stays complete."""
        labels = _labels(language)
        versions = self._source_versions(character, subject)
        sections = {}
        for name, kind in (("important_facts", "fact"), ("open_threads", "thread"),
                           ("growth", "growth"), ("episode", "experience")):
            rows = [dict(item) for item in items if item["kind"] == kind]
            for row in rows:
                if kind == "thread":
                    row["thread_id"] = row["id"]
            lines = [f"- {row['text']}" for row in rows]
            block = "\n".join([labels[name], *lines]) if lines else ""
            refs = tuple(dict.fromkeys(ref for row in rows for ref in row["source_keys"]))
            sections[name] = WorkingSummarySection(name, block, tuple(rows), refs)
        return WorkingSummary(
            character=character, subject=subject, sections=sections,
            scene=normalize_scene(scene) if isinstance(scene, Mapping) else {},
            rebuild_key=self._rebuild_key(versions), source_versions=versions, built_at=self._now_iso(),
            episode=episode,
        )

    def resolve_followup(
        self,
        character: Any,
        query: Any,
        *,
        subject: Any = "private",
        scene: Any = None,
        language: str = "zh",
    ) -> dict[str, Any]:
        """Pick the open thread a continuation question is about.

        "后来怎么样" is answered by the most recently active open thread, but
        when several threads are equally plausible the answer is *which ones*,
        not a guess: the caller gets the candidates plus the field that
        distinguishes them (design section 10.4: "several equally reasonable
        items -> clarify naturally").
        """
        summary = self.build(
            character, subject=subject, scene=scene, language=language,
        )
        threads = [dict(item) for item in summary.section("open_threads").items if not item.get("mention_restricted")]
        text = str(query or "")
        terms = _terms(text)
        scored: list[tuple[float, dict[str, Any]]] = []
        for thread in threads:
            overlap = len(terms & _terms(thread.get("text")))
            boost = 1.0 if thread.get("state") == "waiting" else 0.0
            recency = 1.0 / (1.0 + abs(_as_int(thread.get("age_days"), 0)))
            scored.append((overlap * 2.0 + boost + recency, thread))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if not scored:
            return {"status": "miss", "thread": None, "candidates": [],
                    "clarify_basis": []}
        best_score, best = scored[0]
        rivals = [
            thread for score, thread in scored[1:]
            if score >= best_score - 1.0 and score > 0
        ]
        if best_score <= 0:
            return {"status": "miss", "thread": None, "candidates": [],
                    "clarify_basis": []}
        if rivals:
            candidates = [best, *rivals][:3]
            return {
                "status": "ambiguous",
                "thread": best,
                "candidates": candidates,
                "clarify_basis": [
                    {
                        "thread_id": item.get("thread_id"),
                        "text": item.get("text"),
                        "state": item.get("state"),
                        "distinguisher": item.get("distinguisher"),
                    }
                    for item in candidates
                ],
            }
        return {"status": "resolved", "thread": best, "candidates": [best],
                "clarify_basis": []}

    # -- sections --------------------------------------------------------

    def _growth_section(
        self, character: str, subject: str, scene: Any,
        labels: Mapping[str, str], limit: int,
    ) -> WorkingSummarySection:
        """Published rules + growth/reflection rows + relationship nodes."""
        items: list[dict[str, Any]] = []
        refs: list[str] = []
        for rule in self._teaching_rules(character):
            text = _clean(rule.get("text"))
            if not text:
                continue
            items.append({
                "kind": "rule",
                "id": str(rule.get("rule_id") or ""),
                "teaching_id": str(rule.get("teaching_id") or ""),
                "text": text,
                "label": labels["from_rule"],
                "source_keys": [],
            })
            refs.append(str(rule.get("rule_id") or ""))
        for row in self._growth_rows(character, subject, scene):
            row_kind = str(row.get("memory_kind") or "")
            text = _clean(row.get("claim") or row.get("text"))
            if not text:
                continue
            items.append({
                "kind": "growth" if row_kind in _GROWTH_KINDS else "reflection",
                "id": str(row.get("id") or ""),
                "text": text,
                "label": labels["from_growth"] if row_kind in _GROWTH_KINDS
                else labels["from_reflection"],
                "memory_kind": row_kind or None,
                "support_count": _as_int(row.get("support_count"), 0),
                "status": str(row.get("status") or ""),
                "source_keys": _source_keys(row),
            })
            refs.extend(_source_keys(row))
            refs.append(str(row.get("id") or ""))
        for node in self._relationship_nodes(character, subject, scene):
            text = _clean(node.get("text"))
            if not text:
                continue
            items.append({
                "kind": "relationship",
                "id": str(node.get("id") or ""),
                "text": text,
                "label": labels["from_relation"],
                "entity": str(node.get("entity") or ""),
                "source_keys": _source_keys(node),
            })
            refs.extend(_source_keys(node))
        items = _dedupe(items)[:limit]
        lines = [
            f"- {item['label']}{labels['sep']}{item['text']}" for item in items
        ]
        block = ""
        if lines:
            block = labels["growth"] + "\n" + "\n".join(lines)
        return WorkingSummarySection(
            "growth", block, tuple(items), tuple(dict.fromkeys(r for r in refs if r)),
        )

    def _facts_section(
        self, character: str, subject: str, scene: Any,
        labels: Mapping[str, str], limit: int,
        *, query: str | None = None,
    ) -> WorkingSummarySection:
        """Currently valid facts for this person, in one of two selection modes.

        Administrative (`query is None`): importance first, capped at `limit`.

        Per turn (`query` given): scope + validity + **relevance**. A fact is a
        candidate only when it shares a content term with what the user just
        said; candidates are then ordered by shared terms, then importance. No
        overlap means no facts, which is a normal answer for small talk and for
        an image with no caption -- not a reason to fall back to importance.
        """
        matters = str(subject or "private")
        gate = None if query is None else _shareable_terms(query)
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in self._fact_rows(character):
            if not isinstance(row, Mapping) or not _status_alive(row):
                continue
            if _subject_of(row) != matters:
                continue
            if not entry_applies_to_scene(row, scene, max_chars=64):
                continue
            text = _clean(row.get("text"))
            if not text:
                continue
            overlap = 0
            if gate is not None:
                overlap = len(gate & _shareable_terms(text))
                if overlap <= 0:
                    continue
            scored.append((overlap, dict(row)))
        if gate is not None and not scored:
            # Nothing in this turn's words matches anything stored. Empty is the
            # honest answer; filling the section with the highest-importance
            # rows is exactly the unrelated-injection bug (RO03).
            return WorkingSummarySection("important_facts", "", (), ())
        scored.sort(
            key=lambda item: (
                item[0],
                _as_int(item[1].get("importance"), 5),
                _stamp(item[1].get("last_confirmed_at") or item[1].get("created_at")),
            ),
            reverse=True,
        )
        rows = [item[1] for item in scored]
        # Relevance first, importance second: a shared term is what makes a
        # fact about *this* turn; importance only orders equally relevant rows.
        items = [
            {
                "kind": "fact",
                "id": str(row.get("id") or ""),
                "text": _clean(row.get("text")),
                "importance": _as_int(row.get("importance"), 5),
                "evidence_kind": row.get("evidence_kind"),
                "applies_to": dict(row.get("applies_to") or {}),
                "source_keys": _source_keys(row),
                # Why this row was offered to this turn: the terms the message
                # and the fact share. An administrative build reports none.
                "matched_terms": sorted(
                    _shareable_terms(_clean(row.get("text"))) & gate
                ) if gate is not None else [],
            }
            for row in rows[:limit]
        ]
        lines = [f"- {item['text']}" for item in items]
        block = ""
        if lines:
            block = labels["important_facts"] + "\n" + "\n".join(lines)
        refs = [key for item in items for key in item["source_keys"]]
        return WorkingSummarySection("important_facts", block, tuple(items), tuple(refs))

    def _threads_section(
        self, character: str, subject: str, scene: Any,
        labels: Mapping[str, str], limit: int,
    ) -> WorkingSummarySection:
        """Open/waiting items from companion state, newest first."""
        items: list[dict[str, Any]] = []
        refs: list[str] = []
        restrictions = self._mention_restrictions(character)
        episode_index = self._episode_index(character, subject)
        for kind, row in self._state_items(character, subject, scene):
            text = _clean(row.get("text") or row.get("label"))
            if not text:
                continue
            state_name = "waiting" if kind in _WAITING_KINDS else "open"
            if str(row.get("state") or "").lower() in ("waiting", "blocked"):
                state_name = "waiting"
            thread_id = str(row.get("id") or "")
            item = {
                "kind": "thread",
                "thread_id": thread_id,
                "state": state_name,
                "state_kind": kind,
                "text": text,
                "next_step": _clean(row.get("next_step")),
                "why": _clean(row.get("why")),
                "age_days": _age_days(row.get("updated_at"), self._now()),
                "updated_at": row.get("updated_at") or row.get("created_at"),
                "followup_at": row.get("followup_at") or row.get("next_at"),
                "agreed_followup": bool(row.get("agreed_followup")),
                "expires_at": row.get("expires_at"),
                "source_keys": [str(value) for value in row.get("source_event_ids") or ()],
                "distinguisher": _distinguisher(text, row),
                "mention_restricted": _restricted(text, restrictions),
                # The experience this thread belongs to, derived through the
                # facts carrying the item's own evidence keys. Recall uses it to
                # continue the right episode instead of guessing from words.
                "episode_id": _episode_for_item(row, episode_index),
            }
            items.append(item)
            refs.extend(item["source_keys"])
        items.sort(key=lambda item: (
            0 if item["state"] == "waiting" else 1,
            _as_int(item["age_days"], 0),
        ))
        items = items[:limit]
        lines = []
        for item in items:
            marker = labels["waiting"] if item["state"] == "waiting" else labels["open"]
            line = f"- [{marker}] {item['text']}"
            if item["next_step"]:
                line += f"{labels['comma']}{labels['next']}{labels['sep']}{item['next_step']}"
            if item["mention_restricted"]:
                line += f"{labels['comma']}{labels['restrict']}"
            lines.append(line)
        block = ""
        if lines:
            block = labels["open_threads"] + "\n" + "\n".join(lines)
        return WorkingSummarySection(
            "open_threads", block, tuple(items), tuple(dict.fromkeys(r for r in refs if r)),
        )

    def _episode_section(
        self, episodes: Sequence[Mapping[str, Any]], labels: Mapping[str, str],
    ) -> WorkingSummarySection:
        if not episodes:
            return WorkingSummarySection("episode", "", (), ())
        first = episodes[0]
        lines = [
            f"{labels['participants']}{labels['sep']}"
            + labels["comma"].join(first.get("participants") or ()),
        ]
        if first.get("user_behaviour"):
            lines.append(
                f"{labels['user_side']}{labels['sep']}"
                + labels["comma"].join(first["user_behaviour"])
            )
        if first.get("assistant_behaviour"):
            lines.append(
                f"{labels['assistant_side']}{labels['sep']}"
                + labels["comma"].join(first["assistant_behaviour"])
            )
        if first.get("result"):
            lines.append(f"{labels['result']}{labels['sep']}{first['result']}")
        lines.append(f"{labels['state']}{labels['sep']}{labels[first.get('state') or 'open']}")
        if first.get("followup_at"):
            lines.append(f"{labels['followup']}{labels['sep']}{first['followup_at']}")
        if first.get("mention_restricted"):
            lines.append(labels["restrict"])
        block = labels["episode"] + "\n" + "\n".join(lines)
        items = [
            {
                "kind": "episode",
                "episode_id": episode.get("episode_id"),
                "state": episode.get("state"),
                "result": episode.get("result"),
                "participants": list(episode.get("participants") or ()),
                "followup_at": episode.get("followup_at"),
                "mention_restricted": episode.get("mention_restricted"),
                "source_keys": list(episode.get("source_keys") or ()),
            }
            for episode in episodes
        ]
        refs = [key for item in items for key in item["source_keys"]]
        return WorkingSummarySection("episode", block, tuple(items), tuple(refs))

    # -- episode derivation ----------------------------------------------

    def _episodes(
        self, character: str, subject: str, scene: Any, labels: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        """Group this subject's facts by episode and derive what happened.

        Every field is derived, never read from an extraction-time snapshot:
        participants come from who produced the evidence, both sides' behaviour
        from the evidence kinds, the result from the evidence that records
        something actually happening, the terminal state from companion state,
        the follow-up time from the state item, and the mention restriction
        from this subject's active directives.
        """
        restrictions = self._mention_restrictions(character)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in self._fact_rows(character):
            if not isinstance(row, Mapping) or not _status_alive(row):
                continue
            if _subject_of(row) != subject:
                continue
            episode_id = episode_id_of(row)
            if not episode_id:
                continue
            if not entry_applies_to_scene(row, scene, max_chars=64):
                continue
            grouped.setdefault(episode_id, []).append(dict(row))
        state_by_episode = self._state_by_episode(
            character, subject, facts_by_episode=grouped,
        )
        episodes: list[dict[str, Any]] = []
        for episode_id, rows in grouped.items():
            rows.sort(key=lambda item: _stamp(
                item.get("event_start_at") or item.get("created_at"),
            ))
            user_side: list[str] = []
            assistant_side: list[str] = []
            results: list[str] = []
            keys: list[str] = []
            for row in rows:
                text = _clean(row.get("text"))
                if not text:
                    continue
                kind = str(row.get("evidence_kind") or "")
                if kind in _ASSISTANT_KINDS:
                    assistant_side.append(text)
                else:
                    user_side.append(text)
                if kind in _RESULT_KINDS:
                    results.append(text)
                keys.extend(_source_keys(row))
            state_row = state_by_episode.get(episode_id) or {}
            state_name = "open"
            if state_row.get("closed") or str(state_row.get("terminal") or "") in (
                "done", "cancelled", "closed", "completed",
            ):
                state_name = "done"
            elif state_row.get("state") == "waiting":
                state_name = "waiting"
            joined = " ".join(row.get("text") or "" for row in rows)
            episodes.append({
                "episode_id": episode_id,
                "participants": list(dict.fromkeys(
                    ["master"] + (["assistant"] if assistant_side else [])
                )),
                "user_behaviour": user_side[:_MAX_ITEMS_PER_EPISODE],
                "assistant_behaviour": assistant_side[:_MAX_ITEMS_PER_EPISODE],
                "result": (results[-1] if results else None),
                "state": state_name,
                "followup_at": state_row.get("followup_at") or state_row.get("expires_at"),
                "last_activity_at": _stamp(rows[-1].get("last_confirmed_at")
                                           or rows[-1].get("created_at")),
                "mention_restricted": _restricted(joined, restrictions),
                "source_keys": list(dict.fromkeys(key for key in keys if key)),
                "item_count": len(rows),
            })
        episodes.sort(key=lambda item: item.get("last_activity_at") or "", reverse=True)
        return episodes[:_MAX_EPISODES]

    def _state_by_episode(
        self, character: str, subject: str,
        facts_by_episode: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Index state items by the episode their source events belong to.

        A state item is not written with an episode id: it names the evidence
        that produced it (source_event_ids). The link to an episode therefore
        runs through the facts carrying those same source keys, so the state of
        an experience is derived rather than stored twice.
        """
        by_key: dict[str, str] = {}
        for episode_id, rows in (facts_by_episode or {}).items():
            for row in rows:
                for key in _source_keys(row):
                    by_key.setdefault(key, episode_id)
        index: dict[str, dict[str, Any]] = {}
        for kind, row in self._state_items(character, subject, None):
            keys = [str(value) for value in row.get("source_event_ids") or ()]
            episode_id = str(row.get("episode_id") or "")
            if not episode_id:
                for key in keys:
                    found = by_key.get(key) or by_key.get(
                        "host:%s:%s:%s" % (character, subject, key)
                    )
                    if found:
                        episode_id = found
                        break
            if not episode_id:
                continue
            index[episode_id] = {
                "state": "waiting" if kind in _WAITING_KINDS else "open",
                "followup_at": row.get("followup_at") or row.get("next_at"),
                "expires_at": row.get("expires_at"),
                "keys": keys,
            }
        return index

    def _episode_index(
        self, character: str, subject: str,
    ) -> dict[str, str]:
        """Map every source key this subject's facts carry to its episode id."""
        index: dict[str, str] = {}
        for row in self._fact_rows(character):
            if not isinstance(row, Mapping) or not _status_alive(row):
                continue
            if _subject_of(row) != subject:
                continue
            episode_id = episode_id_of(row)
            if not episode_id:
                continue
            for key in _source_keys(row):
                index.setdefault(key, episode_id)
                # A state item names the *local* event id it came from, while a
                # fact names the full source key; both spellings must resolve or
                # an item written by the host would never find its episode.
                local = key.rsplit(":", 1)[-1].strip()
                if local:
                    index.setdefault(local, episode_id)
                index.setdefault(f"host:{character}:{subject}:{local}", episode_id)
        return index

    # -- source access (each isolated; a missing source is empty) ---------

    def _now(self) -> datetime | None:
        try:
            return self._clock.now() if self._clock is not None else None
        except Exception:  # noqa: BLE001 - a broken clock must not break a summary
            return None

    def _now_iso(self) -> str | None:
        now = self._now()
        return None if now is None else now.isoformat()

    def _fact_rows(self, character: str) -> list[dict[str, Any]]:
        if self._facts is None:
            return []
        try:
            return [dict(row) for row in self._facts.load(character) or ()
                    if isinstance(row, Mapping)]
        except Exception:  # noqa: BLE001
            return []

    def _growth_rows(
        self, character: str, subject: str, scene: Any,
    ) -> list[dict[str, Any]]:
        if self._reflections is None:
            return []
        try:
            rows = self._reflections.load(character) or ()
        except Exception:  # noqa: BLE001
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("status") or "") not in ("confirmed", "promoted"):
                continue
            if row.get("needs_rebuild") or row.get("invalidated_at"):
                continue
            if not _in_subject(row, None) or _subject_of(row) != subject:
                continue
            if not entry_applies_to_scene(row, scene, max_chars=64):
                continue
            if self._barrier is not None:
                try:
                    if self._barrier.blocks_row(character, row):
                        continue
                except Exception:  # noqa: BLE001
                    pass
            out.append(dict(row))
        return out

    def _relationship_nodes(
        self, character: str, subject: str, scene: Any,
    ) -> list[dict[str, Any]]:
        if self._persona is None:
            return []
        try:
            entries = self._persona.entries(character) or ()
        except Exception:  # noqa: BLE001
            return []
        out: list[dict[str, Any]] = []
        for row in entries:
            if not isinstance(row, Mapping):
                continue
            entity = str(row.get("entity") or "").strip().lower()
            if entity not in _RELATIONSHIP_ENTITIES:
                continue
            if row.get("suppress") or row.get("protected"):
                continue
            text = _clean(row.get("text"))
            if not text:
                continue
            if _subject_of(row) not in (subject, "private"):
                continue
            if not entry_applies_to_scene(row, scene, max_chars=64):
                continue
            out.append(dict(text=text, id=row.get("id"),
                            entity=row.get("entity"), source_keys=_source_keys(row)))
        return out

    def _teaching_rules(self, character: str) -> list[dict[str, Any]]:
        """Only the rules still visible in the current projection are 'published'."""
        if self._teaching is None:
            return []
        try:
            rules = list(self._teaching.rules(character) or ())
        except Exception:  # noqa: BLE001
            return []
        active_text = self._projection_text(character, "tone")
        out: list[dict[str, Any]] = []
        for rule in rules:
            if not isinstance(rule, Mapping):
                continue
            line = str(rule.get("line") or rule.get("text") or "").strip()
            if not line:
                continue
            if active_text is not None and line not in active_text:
                # The user edited this rule away by hand: it is not in force,
                # so it must not be reported as a formed part of the relationship.
                continue
            out.append(dict(rule))
        return out

    def _projection_text(self, character: str, section_id: str) -> str | None:
        if self._card is None:
            return None
        try:
            projection = self._card.render_projection(character) or {}
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(projection, Mapping):
            return None
        return str(projection.get(section_id) or "")

    def _state_items(
        self, character: str, subject: str, scene: Any,
    ) -> list[tuple[str, dict[str, Any]]]:
        if self._state is None:
            return []
        try:
            snapshot = self._state.read(character, subject=subject)
        except Exception:  # noqa: BLE001
            return []
        state = getattr(snapshot, "state", None)
        if not isinstance(state, Mapping):
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        for kind in _OPEN_STATE_KINDS:
            for item in state.get(kind) or ():
                if not isinstance(item, Mapping):
                    continue
                if str(item.get("state") or "").lower() in ("done", "cancelled", "closed"):
                    continue
                if scene is not None:
                    item_scene = item.get("scene")
                    if isinstance(item_scene, Mapping) and item_scene:
                        if not entry_applies_to_scene(
                            {"applies_to": item_scene}, scene, max_chars=64,
                        ):
                            continue
                out.append((kind, dict(item)))
        return out

    def _mention_restrictions(self, character: str) -> list[str]:
        if self._directives is None:
            return []
        try:
            rows = self._directives.active(character) or ()
        except Exception:  # noqa: BLE001
            return []
        terms: list[str] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            term = str(row.get("term") or "").strip()
            if term:
                terms.append(term)
        return terms

    # -- rebuild key -----------------------------------------------------

    def _source_versions(self, character: str, subject: str) -> dict[str, Any]:
        """Versions of *every* input, so a changed source changes the key."""
        versions: dict[str, Any] = {
            "deletion_version": 0,
            "correction_version": 0,
            "facts": [0, ""],
            "reflections": [0, ""],
            "state_version": 0,
            "state_updated_at": None,
            "rules": [0, 0],
            "persona": [0, ""],
            "directives": 0,
        }
        if self._barrier is not None:
            try:
                state = self._barrier.state(character)
                versions["deletion_version"] = int(
                    getattr(state, "deletion_version", 0) or 0
                )
                versions["correction_version"] = int(
                    getattr(state, "correction_version", 0) or 0
                )
            except Exception:  # noqa: BLE001
                pass
        facts = self._fact_rows(character)
        versions["facts"] = [
            len(facts),
            max((_stamp(row.get("last_confirmed_at") or row.get("created_at"))
                 for row in facts), default=""),
        ]
        if self._reflections is not None:
            try:
                rows = [row for row in self._reflections.load(character) or ()
                        if isinstance(row, Mapping)]
            except Exception:  # noqa: BLE001
                rows = []
            versions["reflections"] = [
                len(rows),
                max((_stamp(row.get("updated_at") or row.get("created_at"))
                     for row in rows), default=""),
            ]
        if self._state is not None:
            try:
                snapshot = self._state.read(character, subject=subject)
                versions["state_version"] = int(getattr(snapshot, "version", 0) or 0)
                versions["state_updated_at"] = getattr(snapshot, "updated_at", None)
            except Exception:  # noqa: BLE001
                pass
        if self._teaching is not None:
            try:
                rules = list(self._teaching.rules(character) or ())
            except Exception:  # noqa: BLE001
                rules = []
            versions["rules"] = [len(rules), self._card_revision(character)]
        if self._persona is not None:
            try:
                entries = [row for row in self._persona.entries(character) or ()
                           if isinstance(row, Mapping)]
            except Exception:  # noqa: BLE001
                entries = []
            versions["persona"] = [
                len(entries),
                max((_stamp(row.get("updated_at") or row.get("created_at"))
                     for row in entries), default=""),
            ]
        versions["directives"] = len(self._mention_restrictions(character))
        return versions

    def _card_revision(self, character: str) -> int:
        if self._card is None:
            return 0
        try:
            return int(getattr(self._card.get(character), "revision", 0) or 0)
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _rebuild_key(versions: Mapping[str, Any]) -> str:
        blob = json.dumps(versions, ensure_ascii=False, sort_keys=True, default=str)
        return "ws_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]

    # -- cache reconciliation (used by maintenance only) -----------------

    def refresh_cache(
        self, character: Any, store: Any, *,
        subject: Any = "private", scene: Any = None, language: str = "zh",
    ) -> dict[str, Any]:
        """Recompute and write the audit row; never raises into maintenance."""
        name = str(character or self._character or "").strip()
        summary = self.build(
            name, subject=subject, scene=scene, language=language,
        )
        payload = {
            "subject": summary.subject,
            "rebuild_key": summary.rebuild_key,
            "built_at": summary.built_at,
            "sections": {
                key: {
                    "block": section.block,
                    "items": [dict(item) for item in section.items],
                    "source_refs": list(section.source_refs),
                }
                for key, section in summary.sections.items()
            },
            "source_versions": dict(summary.source_versions),
        }
        store.record(name, summary.subject, payload)
        return payload


# -- small helpers ----------------------------------------------------------


def _dedupe(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("text") or "")
        norm = normalize_text(key)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(dict(item))
    return out


def _source_keys(row: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in ("source_event_keys", "source_keys", "source_event_ids", "source_key"):
        value = row.get(field)
        if isinstance(value, str):
            if value.strip():
                keys.append(value.strip())
        elif isinstance(value, (list, tuple)):
            keys.extend(str(item).strip() for item in value
                        if isinstance(item, str) and item.strip())
    return list(dict.fromkeys(keys))


def _shareable_terms(text: Any) -> set[str]:
    """Terms that may justify injecting a fact for one turn (RO03).

    Deliberately the recall tokenizer's *content* terms rather than this
    module's `_terms`: "在吗" / "看看这张图" / "今天天气不错" must not match a
    stored fact through a function word or a 2-gram accident. An empty result
    means "this turn gives nothing to select on", which is a legitimate answer
    and must not be widened into "then show the most important rows".
    """
    return content_terms(text)


def _terms(text: Any) -> set[str]:
    norm = normalize_text(text)
    if not norm:
        return set()
    out: set[str] = set()
    for size in (2, 3):
        out.update(norm[index:index + size]
                   for index in range(max(0, len(norm) - size + 1)))
    return out


def _age_days(value: Any, now: datetime | None) -> int:
    if now is None or not value:
        return 0
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return 0
    if parsed.tzinfo is None and now.tzinfo is not None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    delta = (now - parsed).total_seconds()
    return 0 if delta <= 0 else int(delta // 86400)


def _distinguisher(text: str, row: Mapping[str, Any]) -> str:
    """The field a clarification question can point at.

    Two open threads can look identical in the summary ("准备面试" and
    "准备答辩" are both "那个"); what tells them apart is what the thread is
    waiting on and when it was last touched.
    """
    next_step = _clean(row.get("next_step"), 60)
    if next_step:
        return next_step
    why = _clean(row.get("why"), 60)
    if why:
        return why
    return text[:60]


def _episode_for_item(
    row: Mapping[str, Any], index: Mapping[str, str],
) -> str | None:
    """The episode a state item belongs to, via its own evidence keys."""
    for key in row.get("source_event_ids") or ():
        text = str(key or "").strip()
        if not text:
            continue
        found = index.get(text)
        if found:
            return found
    return None


def _restricted(text: str, terms: Sequence[str]) -> bool:
    haystack = normalize_text(text)
    if not haystack:
        return False
    return any(normalize_text(term) and normalize_text(term) in haystack
               for term in terms)


__all__ = [
    "SECTION_NAMES",
    "WorkingSummary",
    "WorkingSummarySection",
    "WorkingSummaryService",
]
