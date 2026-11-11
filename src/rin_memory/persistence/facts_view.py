"""Raw read/write view of the host `facts.json` / `facts_archive.json` pair.

This adapter is deliberately dumb: it stores and returns exactly the objects
the host writes. Fact extraction, dedup, absorb/invalidate and every other
domain rule belongs to B04/B05 and must not leak in here.

Invariants
----------
* On-disk shape stays byte-compatible with the host: both files are a BARE
  JSON ARRAY of objects. No schema envelope is ever written, so a host reader
  keeps working after this adapter wrote a file.
* Reading accepts the bare host array and the versioned envelope
  `{"schema_version": 1, "facts": [...]}` (or `"archive": [...]`) through
  `persistence.schema.load_legacy_list`, so a future migration can move a
  file forward without a second reader. A JSON object without an integer
  `schema_version` is treated as a versioned store this build cannot read
  (`InvalidSchemaError`); any other top-level shape, unparsable JSON, or a
  non-object item is corruption (`CorruptStoreError`). A corrupt or unknown
  file is never rewritten, emptied or "repaired".
* Every object round-trips verbatim: unknown fields are neither filtered nor
  reordered and nothing is defaulted.
* `commit()` writes the archive file BEFORE the active file (see below).
* Every write is preceded by `WritePermit.assert_writable` (single-file
  writes) or wrapped in `WritePermit.transaction` (the two-file commit), so a
  rejecting permit leaves every file untouched.
* Read-modify-write pairs are serialized per character by this instance's
  `threading.Lock`; there is no global data lock.
"""

from __future__ import annotations

import threading
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .schema import load_legacy_list, require_objects

#: File names inside `<character_dir>`; the host uses these exact names.
FACTS_FILE = "facts.json"
ARCHIVE_FILE = "facts_archive.json"

#: Envelope keys used by the versioned read branch (`schema.load_legacy_list`).
_FACTS_KEY = "facts"
_ARCHIVE_KEY = "archive"


def fact_search_aliases(value: Any) -> list[str]:
    """Bound optional retrieval names from extraction; old facts have none."""
    if not isinstance(value, (list, tuple)):
        return []
    names = {}
    for item in value[:12]:
        if not isinstance(item, str):
            continue
        name = " ".join(unicodedata.normalize("NFKC", item).split())
        if 2 <= len(name) <= 64 and all(c.isalnum() or c in " -.:·'’()" for c in name):
            names.setdefault(name.casefold(), name)
    return list(names.values())[:4]


class FactsView:
    """Raw two-file view over the active facts and the archive.

    One instance serves every character; the per-character lock is created on
    first use. Callers must not mutate the dicts while a save is in flight
    (the JSON encoder walks them), but the adapter adds no ownership rules of
    its own: B04 owns when a fact moves between the two files.

    `clock` is part of the frozen B02 wiring contract; this adapter writes no
    timestamps of its own (host stamps are preserved verbatim), so it is not
    consulted yet.
    """

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
    ) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- reads ----------------------------------------------------------

    def load(self, character: str) -> list[dict[str, Any]]:
        """Return the active facts exactly as stored (empty list when absent)."""
        path = self._path(character, FACTS_FILE)
        items = load_legacy_list(self._store, path, key=_FACTS_KEY)
        return require_objects(items, path=path, what=FACTS_FILE)

    def load_archive(self, character: str) -> list[dict[str, Any]]:
        """Return the archived facts exactly as stored (empty list when absent)."""
        path = self._path(character, ARCHIVE_FILE)
        items = load_legacy_list(self._store, path, key=_ARCHIVE_KEY)
        return require_objects(items, path=path, what=ARCHIVE_FILE)

    def count(self, character: str) -> int:
        """Number of active facts; the archive is not counted."""
        return len(self.load(character))

    # -- writes ---------------------------------------------------------

    def save(self, character: str, facts: Sequence[Mapping[str, Any]]) -> None:
        """Atomically replace `facts.json` with the bare-array payload.

        The payload shape is validated first: writing a non-object row would
        produce a file this adapter's own reader rejects as corrupt, so a
        caller bug fails here instead of on the next load.
        """
        path = self._path(character, FACTS_FILE)
        payload = self._validated(facts, what=FACTS_FILE)
        with self._lock_for(character):
            self._permit.assert_writable("save_facts", str(path))
            self._store.save(path, payload)

    def save_archive(
        self, character: str, archive: Sequence[Mapping[str, Any]],
    ) -> None:
        """Atomically replace `facts_archive.json` with the bare-array payload."""
        path = self._path(character, ARCHIVE_FILE)
        payload = self._validated(archive, what=ARCHIVE_FILE)
        with self._lock_for(character):
            self._permit.assert_writable("save_facts_archive", str(path))
            self._store.save(path, payload)

    def commit(
        self,
        character: str,
        *,
        active: Sequence[Mapping[str, Any]],
        archive: Sequence[Mapping[str, Any]],
    ) -> None:
        """Persist both files under one write permit, ARCHIVE FIRST.

        Order matters because the host's two-file commit must fail towards
        "the row is in both files", never towards "the row is in neither":

        * archive first, active second: a crash in between leaves the moved
          rows in the archive *and* still in `facts.json`. Readers converge
          by id and keep the active copy, and the next archive pass re-merges
          idempotently, so nothing is lost.
        * active first, archive second: a crash in between removes the rows
          from `facts.json` before they reached the archive, losing them
          permanently (including daily-import fingerprints, which would then
          re-import whole days).

        Both payloads are validated before the permit is taken, and the permit
        is checked before either file is touched.
        """
        active_path = self._path(character, FACTS_FILE)
        archive_path = self._path(character, ARCHIVE_FILE)
        active_payload = self._validated(active, what=FACTS_FILE)
        archive_payload = self._validated(archive, what=ARCHIVE_FILE)
        with self._lock_for(character):
            # The commit spans two files in one character directory; the
            # directory is the target the permit fences.
            with self._permit.transaction("commit_facts", str(active_path.parent)):
                self._store.save(archive_path, archive_payload)
                self._store.save(active_path, active_payload)

    # -- internals ------------------------------------------------------

    def _path(self, character: str, filename: str) -> Path:
        """Resolve `<character_dir>/<filename>` without creating anything.

        The atomic store creates the parent directory on write, so a read of
        an absent character leaves no directory behind and no filesystem
        write can happen outside the write permit.
        """
        return self._roots.character_dir(character, create=False) / filename

    def _lock_for(self, character: str) -> threading.Lock:
        """Return the lock that serializes this character's read/write pairs."""
        with self._locks_guard:
            lock = self._locks.get(character)
            if lock is None:
                lock = threading.Lock()
                self._locks[character] = lock
            return lock

    @staticmethod
    def _validated(
        facts: Sequence[Mapping[str, Any]],
        *,
        what: str,
    ) -> list[Mapping[str, Any]]:
        """Return a shallow copy of the payload after a shape check.

        Raises `ValueError` (not `CorruptStoreError`) because the caller's
        argument is wrong; the file on disk is not.
        """
        if isinstance(facts, (str, bytes)) or not isinstance(facts, Sequence):
            raise ValueError(f"{what}: expected a sequence of JSON objects")
        payload = list(facts)
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise ValueError(
                    f"{what}[{index}]: expected a JSON object, "
                    f"found {type(item).__name__}"
                )
        return payload
