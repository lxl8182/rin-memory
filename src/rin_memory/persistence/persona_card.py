"""Host-independent current persona card storage (B10).

The persona card is the authoritative semantic persona for one character: an
initial card (imported once), the current card (sections that may be revised)
and bounded revision history. `characters.json` keeps host resources and a
revision-stamped projection; this store never writes the host file and never
reads it back as authority (design §8.3).

On-disk contract (design §8.1, roadmap B10)
-------------------------------------------
`<character_dir>/persona_card.json`:

```json
{"schema_version": 1,
 "character": "demo",
 "initial_revision": 1,
 "active_revision": 3,
 "initial_sections": [<section>, ...],
 "sections": [<section>, ...],
 "revisions": [<revision record>, ...]}
```

Section row (unknown fields round-trip verbatim):

`section_id / kind / text / scope {subject?, applies_to?} /
 editable_by {user, evolution} / frozen / origin / source_refs /
 revision / updated_at / history[] / invalidated_sources? / needs_rebuild?`

Revision record: `id / revision / base_revision / operations / reason /
origin / actor / at / result / source_refs / sections_snapshot`.

A missing file is the empty card
`{schema_version: 1, initial_revision: 0, active_revision: 0,
  sections: [], revisions: []}`. Invalid JSON, a non-object root, an
unsupported schema, non-list sections/revisions, a non-object section/revision
or a section without `section_id` raises `CorruptStoreError` /
`InvalidSchemaError` and the bytes are left as found; `save` refuses to
replace a file its own reader rejects (B02 fail-closed rule).

Writes run under one process-wide per-file RLock and follow
`WritePermit.assert_writable -> barrier filter -> AtomicJsonStore.save`.
`barrier` (optional) drops sections whose section id/source refs are
tombstoned before the write, so a deleted source can never be re-persisted.

History is bounded in the card (`history_limit`, default 20 revisions);
older revision records are appended to `persona_card_archive/` shards through
the shared `ArchiveShardStore` (500 rows/shard, fail-closed reads).

All methods are synchronous blocking I/O; call them through
`asyncio.to_thread` from async code.
"""

from __future__ import annotations

import copy
import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..errors import CorruptStoreError, InvalidSchemaError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .shards import ArchiveShardStore, is_blank_file
from .persona_content import filter_owned_tree

CARD_FILENAME = "persona_card.json"
CARD_ARCHIVE_DIRNAME = "persona_card_archive"
CARD_SCHEMA_VERSION = 1
DEFAULT_HISTORY_LIMIT = 20

#: The display name is the first line of the identity section, written as
#: `名字: <name>`. One line in the one place the persona already describes who
#: she is, instead of a second field that can disagree with it. Parsing is
#: one-way: the line is authored text, so a card without it simply has no name.
NAME_IDENTITY_SECTION_ID = "identity"
NAME_LINE_PREFIXES = ("名字:", "名字：", "姓名:", "姓名：")
MAX_NAME_CHARS = 80

#: Lock per physical card file, shared process-wide.
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


def empty_card(character: str = "") -> dict[str, Any]:
    """Host-required empty card (no character key, per the B10 task contract)."""
    del character
    return {
        "schema_version": CARD_SCHEMA_VERSION,
        "initial_revision": 0,
        "active_revision": 0,
        "sections": [],
        "revisions": [],
    }


def _bounded_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _bounded_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_section(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Copy one section row, filling the required identity fields only."""
    section = copy.deepcopy(dict(raw))
    section["section_id"] = _bounded_text(section.get("section_id"), 120)
    if not section["section_id"]:
        raise CorruptStoreError("persona_card section is missing section_id")
    if "text" not in section:
        section["text"] = ""
    if not isinstance(section.get("text"), str):
        section["text"] = str(section.get("text") or "")
    if "history" not in section:
        section["history"] = []
    if not isinstance(section.get("history"), list):
        raise CorruptStoreError(
            f"persona_card section {section['section_id']!r}.history is not a list",
        )
    section["revision"] = _bounded_int(section.get("revision"), 0)
    if "source_refs" not in section:
        section["source_refs"] = []
    if not isinstance(section.get("source_refs"), list):
        raise CorruptStoreError(
            f"persona_card section {section['section_id']!r}.source_refs is not a list",
        )
    return section


def _normalize_revision(raw: Mapping[str, Any]) -> dict[str, Any]:
    revision = copy.deepcopy(dict(raw))
    revision["revision"] = _bounded_int(revision.get("revision"), 0)
    revision["base_revision"] = _bounded_int(revision.get("base_revision"), 0)
    if "operations" not in revision:
        revision["operations"] = []
    if not isinstance(revision.get("operations"), list):
        raise CorruptStoreError(
            f"persona_card revision {revision['revision']}: operations is not a list",
        )
    if "source_refs" not in revision:
        revision["source_refs"] = []
    if not isinstance(revision.get("source_refs"), list):
        raise CorruptStoreError(
            f"persona_card revision {revision['revision']}: source_refs is not a list",
        )
    if "sections_snapshot" in revision and not isinstance(
        revision.get("sections_snapshot"), list,
    ):
        raise CorruptStoreError(
            f"persona_card revision {revision['revision']}: sections_snapshot is not a list",
        )
    revision.setdefault("id", f"cardrev:{revision['revision']}")
    return revision


def normalize_card(raw: Any, character: str = "") -> dict[str, Any]:
    """Validate and copy a card document; unknown fields round-trip verbatim."""
    if raw is None:
        return empty_card(character)
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"persona_card: expected a JSON object, found {type(raw).__name__}",
        )
    schema = raw.get("schema_version", CARD_SCHEMA_VERSION)
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise InvalidSchemaError(
            f"persona_card: missing or non-integer schema_version: {schema!r}",
        )
    if schema != CARD_SCHEMA_VERSION:
        raise InvalidSchemaError(
            f"unsupported persona_card schema {schema}; this build supports "
            f"{CARD_SCHEMA_VERSION}",
        )
    card = copy.deepcopy(dict(raw))
    card["schema_version"] = CARD_SCHEMA_VERSION
    card["initial_revision"] = _bounded_int(card.get("initial_revision"), 0)
    card["active_revision"] = _bounded_int(card.get("active_revision"), 0)
    sections = card.get("sections", [])
    if not isinstance(sections, list):
        raise CorruptStoreError(
            f"persona_card.sections: expected a list, found {type(sections).__name__}",
        )
    normalized_sections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(sections):
        if not isinstance(item, Mapping):
            raise CorruptStoreError(
                f"persona_card.sections[{index}]: expected an object, "
                f"found {type(item).__name__}",
            )
        section = _normalize_section(item)
        if section["section_id"] in seen:
            raise CorruptStoreError(
                f"persona_card.sections: duplicate section_id "
                f"{section['section_id']!r}",
            )
        seen.add(section["section_id"])
        normalized_sections.append(section)
    card["sections"] = normalized_sections
    revisions = card.get("revisions", [])
    if not isinstance(revisions, list):
        raise CorruptStoreError(
            f"persona_card.revisions: expected a list, found {type(revisions).__name__}",
        )
    normalized_revisions: list[dict[str, Any]] = []
    for index, item in enumerate(revisions):
        if not isinstance(item, Mapping):
            raise CorruptStoreError(
                f"persona_card.revisions[{index}]: expected an object, "
                f"found {type(item).__name__}",
            )
        normalized_revisions.append(_normalize_revision(item))
    card["revisions"] = normalized_revisions
    if "initial_sections" in card:
        initial = card.get("initial_sections")
        if not isinstance(initial, list):
            raise CorruptStoreError("persona_card.initial_sections is not a list")
        card["initial_sections"] = [
            _normalize_section(item) for item in initial
            if isinstance(item, Mapping)
        ]
    if character:
        card.setdefault("character", character)
    return card


def _validated_card(card: Any, character: str) -> dict[str, Any]:
    """Caller payload shape check; raises ValueError (disk stays untouched)."""
    if not isinstance(card, Mapping):
        raise ValueError("persona_card: document must be a mapping")
    payload = normalize_card(card, character)
    if not isinstance(payload.get("sections"), list):
        raise ValueError("persona_card.sections must be a list")
    if not isinstance(payload.get("revisions"), list):
        raise ValueError("persona_card.revisions must be a list")
    return payload


def _refs_of(row: Mapping[str, Any]) -> list[str]:
    refs = row.get("source_refs")
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        return []
    out: list[str] = []
    for item in refs:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(item.strip())
    return out


class PersonaCardStore:
    """Per-character persona_card.json with strict reads and archive history."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
        barrier: Any = None,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        archive: ArchiveShardStore | None = None,
    ) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        self._barrier = barrier
        self._history_limit = max(1, int(history_limit or DEFAULT_HISTORY_LIMIT))
        self._archive = archive if archive is not None else ArchiveShardStore(
            roots=roots, store=store, clock=clock, permit=permit,
            dirname=CARD_ARCHIVE_DIRNAME,
        )

    # -- paths ----------------------------------------------------------

    def _path(self, character: str, *, create: bool = False) -> Path:
        return self._roots.character_dir(character, create=create) / CARD_FILENAME

    @property
    def history_limit(self) -> int:
        return self._history_limit

    # -- reads ----------------------------------------------------------

    def load(self, character: str) -> dict[str, Any]:
        """Return the normalized card; corruption raises, never empty."""
        path = self._path(character)
        with _lock_for(path):
            return self._load_card(character)

    def _load_card(self, character: str) -> dict[str, Any]:
        path = self._path(character)
        try:
            raw = self._store.load(path)
        except CorruptStoreError:
            if is_blank_file(path):
                return empty_card(character)
            raise
        return normalize_card(raw, character)

    # -- barrier filter -------------------------------------------------

    def _section_blocked(self, character: str, section: Mapping[str, Any]) -> bool:
        barrier = self._barrier
        if barrier is None:
            return False
        section_id = str(section.get("section_id") or "")
        for target_type in ("persona", "reflection", "fact", "card"):
            if section_id and barrier.blocks_target(character, target_type, section_id):
                return True
        try:
            if barrier.blocks_row(character, section):
                return True
        except Exception:  # noqa: BLE001 - barrier failures must not invent data
            return False
        for ref in _refs_of(section):
            for target_type in ("fact", "reflection", "persona", "growth"):
                if barrier.blocks_target(character, target_type, ref):
                    return True
        return False

    def _filter_card(self, character: str, card: Mapping[str, Any]) -> dict[str, Any]:
        filtered = copy.deepcopy(dict(card))
        if self._barrier is None:
            return filtered
        filtered = filter_owned_tree(filtered, lambda ref: barrier_blocks(self._barrier, character, ref))
        filtered["sections"] = [
            section for section in filtered.get("sections") or []
            if not self._section_blocked(character, section)
        ]
        if isinstance(filtered.get("initial_sections"), list):
            filtered["initial_sections"] = [
                section for section in filtered["initial_sections"]
                if not self._section_blocked(character, section)
            ]
        kept_revisions: list[dict[str, Any]] = []
        for revision in filtered.get("revisions") or []:
            refs = _refs_of(revision)
            if refs and all(
                barrier_blocks(self._barrier, character, ref) for ref in refs
            ):
                continue
            kept_revisions.append(revision)
        filtered["revisions"] = kept_revisions
        return filtered

    def filter_card(self, character: str, card: Mapping[str, Any]) -> dict[str, Any]:
        """Public barrier filter used by the service and replay handler."""
        return self._filter_card(character, card)

    def section_blocked(
        self, character: str, section: Mapping[str, Any],
    ) -> bool:
        """Public single-section tombstone check."""
        return self._section_blocked(character, section)

    # -- writes ---------------------------------------------------------

    def save(self, character: str, card: Mapping[str, Any]) -> None:
        """Atomically replace the card after permit + deletion-barrier filtering."""
        payload = _validated_card(card, character)
        payload = self._filter_card(character, payload)
        path = self._path(character)
        with _lock_for(path):
            self._load_card(character)  # refuse to replace a corrupt file
            self._permit.assert_writable("save_persona_card", str(path))
            self._store.save(path, payload)

    # -- archive --------------------------------------------------------

    def archive_history(
        self, character: str, revisions: Sequence[Mapping[str, Any]],
    ) -> str:
        """Append overflow revision records to the archive; return shard name."""
        rows = [dict(item) for item in revisions]
        if not rows:
            return ""
        return self._archive.append(character, rows)

    def load_archive(self, character: str) -> list[dict[str, Any]]:
        """Return every archived revision record across shards."""
        rows = self._archive.read_all(character)
        if self._barrier is not None:
            rows = filter_owned_tree(rows, lambda ref: barrier_blocks(self._barrier, character, ref))
        return rows

    def purge_owned_sources(self, character: str) -> int:
        rows = self._archive.read_all(character)
        affected = [row["id"] for row in rows if filter_owned_tree(
            row, lambda ref: barrier_blocks(self._barrier, character, ref)) != row]
        return self._archive.delete_ids(character, affected) if affected else 0

    def delete_from_archive(self, character: str, ids: Sequence[str]) -> int:
        """Delete archived rows by id; returns the removed count."""
        return self._archive.delete_ids(character, ids)

    # -- projection -----------------------------------------------------

    def visible_sections(self, card: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Sections that may be projected (no tombstone, no invalidated row)."""
        out: list[dict[str, Any]] = []
        for section in card.get("sections") or []:
            if not isinstance(section, Mapping):
                continue
            if section.get("invalidated_at") or section.get("needs_rebuild"):
                continue
            text = str(section.get("text") or "").strip()
            if not text:
                continue
            out.append(dict(section))
        return out

    def render_projection(self, card: Mapping[str, Any]) -> dict[str, str]:
        """Host characters.json compatible projection: ordered section_id->text.

        This is a read-only string map; the adapter (B08) decides where to write
        it. It never reads or overwrites the host configuration file.
        """
        projection: dict[str, str] = {}
        for section in self.visible_sections(card):
            section_id = str(section.get("section_id") or "")
            text = str(section.get("text") or "").strip()
            if section_id and text:
                projection[section_id] = text
        return projection

    def display_name(self, card: Mapping[str, Any]) -> str:
        """The name line at the top of the identity section, or empty."""
        for section in self.visible_sections(card):
            if str(section.get("section_id") or "") != NAME_IDENTITY_SECTION_ID:
                continue
            return parse_name_line(str(section.get("text") or ""))
        return ""

    def projection_text(self, card: Mapping[str, Any]) -> str:
        """Join the visible projection with blank lines (host prompt shape)."""
        return "\n\n".join(self.render_projection(card).values())


def parse_name_line(text: Any) -> str:
    """Read the name from the identity section's first non-empty line.

    The line must start with one of `NAME_LINE_PREFIXES`; anything else means
    the card was written before the convention and has no name line, which the
    caller reports rather than guessing from free prose.
    """
    if not isinstance(text, str):
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        for prefix in NAME_LINE_PREFIXES:
            if line.startswith(prefix):
                return line[len(prefix):].strip()[:MAX_NAME_CHARS]
        return ""
    return ""


def with_name_line(text: Any, name: Any) -> str:
    """Return `identity` text whose first line is `名字: <name>`.

    Everything after the original name line is preserved verbatim; when the
    card has no name line yet, the new line is prepended and the existing text
    is kept below it.
    """
    value = str(name or "").strip()[:MAX_NAME_CHARS]
    body = text if isinstance(text, str) else ""
    lines = body.splitlines()
    if lines and any(lines[0].strip().startswith(prefix) for prefix in NAME_LINE_PREFIXES):
        rest = lines[1:]
    else:
        rest = lines
    if not value:
        return "\n".join(rest).lstrip("\n")
    return "\n".join(["名字: " + value, *rest])


def barrier_blocks(barrier: Any, character: str, ref: str) -> bool:
    """Try one source ref against every id space the barrier may know."""
    if ref and barrier.blocks_source_key(character, ref):
        return True
    for target_type in ("fact", "reflection", "persona", "growth"):
        try:
            if barrier.blocks_target(character, target_type, ref):
                return True
        except Exception:  # noqa: BLE001 - defensive filter only
            continue
    try:
        if barrier.blocks_text(character, ref):
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def text_is_tombstoned(barrier: Any, character: str, text: Any) -> bool:
    if barrier is None or not isinstance(text, str) or not text.strip():
        return False
    try:
        return bool(barrier.blocks_text(character, text))
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "CARD_ARCHIVE_DIRNAME",
    "CARD_FILENAME",
    "CARD_SCHEMA_VERSION",
    "DEFAULT_HISTORY_LIMIT",
    "MAX_NAME_CHARS",
    "NAME_IDENTITY_SECTION_ID",
    "NAME_LINE_PREFIXES",
    "PersonaCardStore",
    "barrier_blocks",
    "empty_card",
    "normalize_card",
    "parse_name_line",
    "text_is_tombstoned",
    "with_name_line",
]
