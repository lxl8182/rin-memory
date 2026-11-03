"""Pure invalidation rules shared by corrections, deletion and later recall.

Migrated from the host's `memory/correction_propagation.py` (design §17.10):
only the deterministic predicates live here, no storage and no model calls.
Two layers use them:

* correction propagation marks a fact `invalidated_at` and then walks
  reflections/persona by source ids (B05/B10 register those walkers);
* read paths (B04 recall, B06 context) drop invalidated rows with
  :func:`filter_valid_entries` instead of re-implementing the field rules.

Field contract (kept host-compatible so existing files keep working):

* fact: non-empty `invalidated_at` means "stopped using";
* reflection: `status == 'withdrawn'`, or `invalidated_sources` covers every
  `source_fact_ids` entry;
* persona: `invalidated_at` and no surviving source (`source_id` /
  `merged_from_ids` all listed in `invalidated_sources`).

Tolerant by design: a row without these fields is valid, and a row that makes
the predicate raise is treated as valid (dropping a whole pool because of one
dirty row is worse than injecting it). Non-mapping rows are dropped.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

#: Fact/archive field written by correction propagation and deletion checks.
FACT_INVALIDATED_AT = "invalidated_at"
#: Reflection terminal state: fully derived from invalidated sources.
REFLECTION_WITHDRAWN = "withdrawn"
#: Scene scopes that mean "applies everywhere" in host rows.
_SHARED_SCOPES = frozenset({"shared", "global", "all"})

_WHITESPACE_RE = re.compile(r"\s+")


# -- text normalization and matching ----------------------------------------


def normalize_text(value: Any) -> str:
    """NFKC + casefold + whitespace folding; non-strings become "".

    Chinese text is unchanged by casefold; English case differences count as
    the same sentence. Punctuation is kept: only the general shape is folded.
    """
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.casefold()
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def text_hash(value: Any) -> str:
    """sha256 of the normalized text; the host tombstone digest format."""
    return hashlib.sha256(
        normalize_text(value).encode("utf-8", "replace"),
    ).hexdigest()


def text_matches(candidate: Any, target: Any, *, substring: bool = True) -> bool:
    """Whether `candidate` expresses `target` (equal, or target is a substring).

    Used when the user quotes an old statement: the full sentence or one
    fragment of it appearing in a fact counts as the same thing. An empty
    target never matches.
    """
    wanted = normalize_text(target)
    if not wanted:
        return False
    actual = normalize_text(candidate)
    if not actual:
        return False
    if actual == wanted:
        return True
    if substring and wanted in actual:
        return True
    # Quoting often inserts spaces inside CJK text ("以前住在 东京"): compare
    # once more with every space removed.
    actual_compact = actual.replace(" ", "")
    wanted_compact = wanted.replace(" ", "")
    if not wanted_compact:
        return False
    if actual_compact == wanted_compact:
        return True
    return substring and wanted_compact in actual_compact


# -- subject scope ----------------------------------------------------------


def subject_key(value: Any) -> str:
    """Collapse a stored subject into one comparable key ("" means private).

    Host rows store either a bare string or a `{subject_kind, subject_id,
    scope}` mapping; both shapes map onto the same key so a correction written
    by the core still matches host-created rows.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        subject_id = value.get("subject_id") or value.get("id") or value.get("key")
        if not isinstance(subject_id, str) or not subject_id.strip():
            return ""
        kind = value.get("subject_kind") or value.get("kind")
        if isinstance(kind, str) and kind.strip():
            return f"{kind.strip()}:{subject_id.strip()}"
        return subject_id.strip()
    return ""


def is_friend_subject(value: Any) -> bool:
    """QQ friend subjects use facts without owner state or persona growth."""
    return subject_key(value).startswith("qq-friend:")


def row_matches_subject(row: Any, subject: Any) -> bool:
    """Whether a stored row belongs to `subject`; private covers unmarked rows.

    A mismatch (or a dirty row) returns False: corrections and deletions must
    not leak across subjects, so "cannot tell" is treated as "not this one".
    """
    if not isinstance(row, Mapping):
        return False
    stored = subject_key(row.get("subject"))
    wanted = subject_key(subject)
    if not wanted or wanted == "private":
        return stored in ("", "private")
    return stored == wanted


def scene_applies(applies_to: Any, scene: Any) -> bool:
    """Whether a correction/row scope applies to the current scene.

    `applies_to` is a Scene-like mapping (plugin_id/game_id/session_id) and
    may carry `scope: shared|global|all`; empty fields match anything.
    """
    if applies_to is None:
        return True
    if not isinstance(applies_to, Mapping):
        return True
    if subject_key(applies_to.get("scope")) in _SHARED_SCOPES:
        return True
    if scene is None:
        return True
    actual = scene if isinstance(scene, Mapping) else getattr(scene, "__dict__", {})
    for key in ("plugin_id", "game_id", "session_id"):
        required = applies_to.get(key)
        if not isinstance(required, str) or not required:
            continue
        if actual.get(key) != required:
            return False
    return True


# -- source ids -------------------------------------------------------------


def _id_set(values: Any) -> set[str]:
    """Collapse an id list into a set of nonempty strings (tolerates scalars)."""
    if values is None:
        return set()
    if isinstance(values, (str, bytes)):
        values = [values]
    if not isinstance(values, Iterable):
        return set()
    out: set[str] = set()
    for value in values:
        if isinstance(value, str) and value.strip():
            out.add(value.strip())
    return out


def source_ids_of_reflection(entry: Any) -> set[str]:
    """The `source_fact_ids` a reflection was synthesized from."""
    if not isinstance(entry, Mapping):
        return set()
    return _id_set(entry.get("source_fact_ids"))


def source_ids_of_persona(entry: Any) -> set[str]:
    """The reflection ids a persona entry was derived from."""
    if not isinstance(entry, Mapping):
        return set()
    return _id_set(entry.get("source_id")) | _id_set(entry.get("merged_from_ids"))


def sources_fully_invalidated(sources: Iterable[str], invalidated: Iterable[str]) -> bool:
    """Whether every source is invalidated.

    An empty source set returns False: "no source" is not proof that the entry
    depends only on deleted content, so callers keep it instead of withdrawing
    something of unknown origin.
    """
    source_set = {str(item) for item in sources if str(item or "").strip()}
    if not source_set:
        return False
    invalidated_set = {str(item) for item in invalidated if str(item or "").strip()}
    return source_set <= invalidated_set


# -- invalidation predicates ------------------------------------------------


def fact_invalidated(entry: Mapping[str, Any]) -> bool:
    """Whether a fact row was invalidated by a correction or deletion."""
    return bool(entry.get(FACT_INVALIDATED_AT)) or entry.get("invalidated") is True


def reflection_invalidated(entry: Mapping[str, Any]) -> bool:
    """Withdrawn, or every source fact invalidated (partial stays valid)."""
    status = str(entry.get("status") or "").strip().lower()
    if status == REFLECTION_WITHDRAWN:
        return True
    sources = source_ids_of_reflection(entry)
    if not sources:
        return False
    return sources <= _id_set(entry.get("invalidated_sources"))


def persona_invalidated(entry: Mapping[str, Any]) -> bool:
    """Invalidated persona entry whose sources are all gone."""
    if not entry.get("invalidated_at"):
        return False
    sources = source_ids_of_persona(entry)
    if not sources:
        return True
    return not (sources - _id_set(entry.get("invalidated_sources")))


def _infer_tier(entry: Mapping[str, Any]) -> str:
    for key in ("_tier", "tier", "target_type"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    if "source_fact_ids" in entry:
        return "reflection"
    if "source_id" in entry or "merged_from_ids" in entry:
        return "persona"
    return "fact"


def entry_is_invalidated(entry: Any) -> bool:
    """Whether this memory row must stop entering reads.

    Non-mapping rows are dropped (True). A predicate failure keeps the row
    (False): one dirty row must not wipe a whole pool.
    """
    if not isinstance(entry, Mapping):
        return True
    try:
        tier = _infer_tier(entry)
        if tier in ("fact", "fact_archive"):
            return fact_invalidated(entry)
        if tier == "reflection":
            return reflection_invalidated(entry)
        if tier == "persona":
            return persona_invalidated(entry)
        return fact_invalidated(entry)
    except Exception:  # noqa: BLE001 - dirty row stays usable
        return False


def filter_valid_entries(entries: Any) -> list[dict[str, Any]]:
    """Drop invalidated/non-mapping rows, keeping the original order."""
    out: list[dict[str, Any]] = []
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        if entry_is_invalidated(entry):
            continue
        out.append(entry)
    return out


__all__ = [
    "FACT_INVALIDATED_AT",
    "REFLECTION_WITHDRAWN",
    "entry_is_invalidated",
    "fact_invalidated",
    "filter_valid_entries",
    "is_friend_subject",
    "normalize_text",
    "persona_invalidated",
    "reflection_invalidated",
    "row_matches_subject",
    "scene_applies",
    "source_ids_of_persona",
    "source_ids_of_reflection",
    "sources_fully_invalidated",
    "subject_key",
    "text_hash",
    "text_matches",
]
