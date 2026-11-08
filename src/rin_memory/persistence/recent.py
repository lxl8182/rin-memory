"""Raw read/write view of the host recent.json conversation window.

This adapter is deliberately dumb: it stores and returns exactly the message
objects the host writes, in the order they were appended, and never sorts,
dedups or stamps anything of its own. Compression, extraction and deletion
*policy* belong to B04/B05 callers and must not leak in here.

On-disk contract (host-compatible, design §17.15.5 and B04 recent spec §1)
----------------------------------------------------------------------------
* The file is a BARE JSON ARRAY of langchain-shaped messages:
  {"type": "human|ai|system", "data": {"content": str | list,
  "additional_kwargs"?: dict, "metadata"?: dict}}.
  There is no schema envelope, no top-level role/text/ts field.
* Every object round-trips verbatim: unknown keys and nested unknown metadata
  are preserved, nothing is filtered, reordered or defaulted.
* Writes use indent=2 and ensure_ascii=False through the injected
  AtomicJsonStore, so a host reader keeps working after this adapter wrote a
  file.
* A missing file is an empty history. A historical *empty* file (the host used
  to write one and read it as []) is also an empty history; that is the only
  parse failure tolerated. Invalid JSON, a non-array root, or a non-object
  element raises CorruptStoreError and the bytes are left exactly as found --
  the store never "repairs", empties or rewrites a corrupt or unreadable file
  (read-modify-write callers load first, so they fail before touching it).
* Every whole-file write passes the injected WritePermit, then the optional
  write_guard(character, messages), then the atomic replace, all inside the
  same per-file critical section. A raising guard or permit therefore leaves
  the file untouched.

Concurrency
-----------
All reads and read-modify-write sequences run under one threading.RLock per
physical file path, shared by every store instance in the process (the host
has several in-process writers over one recent file, so a per-instance lock is
not enough). The document is never cached: each load re-reads the file. The
guard must not call back into this store (the lock is reentrant, but a nested
write would replace the payload the outer call is about to write).

I/O model
---------
All methods are synchronous blocking file I/O; call them through
asyncio.to_thread from async code, exactly like the other stores in this
package.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..errors import CorruptStoreError
from ..invalidation import text_matches
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit

#: File name inside <character_dir>; the host uses this exact name.
RECENT_FILENAME = "recent.json"

#: Called with the final payload before each whole-file replace; raising aborts
#: the write. B04 wires the deletion barrier / write registration here.
WriteGuard = Callable[[str, Sequence[Mapping[str, Any]]], None]

#: One lock per physical recent file, created lazily and shared process-wide.
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()

#: Sentence boundaries used when pruning a memo; punctuation stays on the
#: segment it terminates (host aapply_deletion_filter rule).
_SUMMARY_SEGMENT_RE = re.compile(r"(?<=[。！？!?；;\n])")


def _lock_for(path: Path) -> threading.RLock:
    """Return the process-wide lock that serializes one physical recent file."""
    key = os.path.normcase(str(path))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


# -- host message helpers (mirror of domain.barrier; no domain import here) ----


def _message_metadata(message: Any) -> Mapping[str, Any]:
    """Host metadata: data.metadata first, then a top-level fallback."""
    if not isinstance(message, Mapping):
        return {}
    data = message.get("data")
    if isinstance(data, Mapping):
        metadata = data.get("metadata")
        if isinstance(metadata, Mapping):
            return metadata
    metadata = message.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def message_text(message: Any) -> str:
    """Text of one host message; multimodal content lists concatenate their parts."""
    if not isinstance(message, Mapping):
        return ""
    data = message.get("data")
    content = data.get("content") if isinstance(data, Mapping) else message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                value = part.get("text") or part.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts)
    return ""


def message_source_keys(message: Any) -> set[str]:
    """Every source key a host message carries (event and mirror)."""
    metadata = _message_metadata(message)
    keys: set[str] = set()
    companion = metadata.get("companion_event")
    if isinstance(companion, Mapping):
        value = companion.get("source_key")
        if isinstance(value, str) and value.strip():
            keys.add(value.strip())
    mirror = metadata.get("mirror")
    if isinstance(mirror, Mapping):
        event = mirror.get("event")
        if isinstance(event, Mapping):
            value = event.get("source_key")
            if isinstance(value, str) and value.strip():
                keys.add(value.strip())
    value = metadata.get("source_key")
    if isinstance(value, str) and value.strip():
        keys.add(value.strip())
    return keys


def message_episode_ids(message: Any) -> set[str]:
    """Every episode id a host message carries (top-level and companion_event)."""
    metadata = _message_metadata(message)
    out: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            out.add(value.strip())
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    out.add(item.strip())

    _add(metadata.get("episode_id"))
    _add(metadata.get("episode_ids"))
    companion = metadata.get("companion_event")
    if isinstance(companion, Mapping):
        _add(companion.get("episode_id"))
        _add(companion.get("episode_ids"))
    return out


def is_summary_message(message: Any) -> bool:
    """Whether a message is the head memo slot (a system message)."""
    return isinstance(message, Mapping) and message.get("type") == "system"


# -- payload and pruning helpers ----------------------------------------------


def _iter_strings(values: Any) -> Iterable[str]:
    """Yield string items from a scalar/list-like id or text argument."""
    if values is None:
        return
    if isinstance(values, (str, bytes)):
        values = [values]
    if not isinstance(values, Iterable):
        return
    for value in values:
        if isinstance(value, str):
            yield value


def _clean_id_set(values: Any) -> set[str]:
    """Collapse an id collection into a set of nonempty stripped strings."""
    return {value.strip() for value in _iter_strings(values) if value.strip()}


def _clean_texts(values: Any) -> list[str]:
    """Ordered, stripped, nonempty match texts (duplicates are harmless)."""
    return [value.strip() for value in _iter_strings(values) if value.strip()]


def _validated_messages(messages: Any, *, what: str) -> list[dict[str, Any]]:
    """Copy a message sequence after a shape check.

    Raises ValueError (not CorruptStoreError): the caller's argument is wrong,
    the file on disk is not. A plain string is a Sequence too, so it is
    rejected explicitly instead of being split into characters.
    """
    if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence):
        raise ValueError(f"{what}: expected a sequence of message objects")
    payload: list[dict[str, Any]] = []
    for index, item in enumerate(messages):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"{what}[{index}]: expected a message object, "
                f"found {type(item).__name__}"
            )
        payload.append(dict(item))
    return payload


def _validated_summary(summary: Any) -> dict[str, Any]:
    """The head memo message; a plain string is wrapped as a system message."""
    if isinstance(summary, str):
        return {"type": "system", "data": {"content": summary}}
    if not isinstance(summary, Mapping):
        raise ValueError("summary: expected a message object or memo text")
    return dict(summary)


def _strip_summary(content: str, texts: Sequence[str]) -> str:
    """Remove memo sentences that express a deleted text.

    Returns content unchanged when no target occurs, the remaining text when
    some sentences were pruned, and "" when the whole memo must be dropped. The
    empty result also covers a hit crossing a sentence boundary: a partial memo
    that still contains the deleted text is not safe to keep.
    """
    if not content or not texts:
        return content
    if not any(text_matches(content, text) for text in texts):
        return content
    kept = [
        segment
        for segment in _SUMMARY_SEGMENT_RE.split(content)
        if segment.strip() and not any(text_matches(segment, text) for text in texts)
    ]
    joined = "".join(kept).strip()
    if any(text_matches(joined, text) for text in texts):
        return ""
    return joined


def _with_message_text(message: Mapping[str, Any], text: str) -> dict[str, Any]:
    """Return a copy of a message whose content is replaced by text."""
    updated = dict(message)
    data = updated.get("data")
    if isinstance(data, Mapping):
        data_copy = dict(data)
        data_copy["content"] = text
        updated["data"] = data_copy
    else:
        updated["content"] = text
    return updated


def merge_qq_reply_parts(message: dict[str, Any], source_key: str, parts: Mapping[int, str]) -> bool:
    """Fill a QQ history placeholder with the bubbles accepted by the transport."""
    if (message.get("type") != "ai" or _message_metadata(message).get("reply_transport") != "qq"
            or source_key not in message_source_keys(message)):
        return False
    data = message["data"]
    metadata = data["metadata"]
    previous = metadata.get("qq_reply_parts") or {}
    merged = dict(previous)
    for index, text in parts.items():
        existing = merged.setdefault(str(index), text)
        if existing != text:
            raise ValueError("QQ reply part changed at the same index")
    if merged == previous:
        return False
    chunks = []
    last = -1
    for index, text in sorted((int(key), value) for key, value in merged.items()):
        if chunks and index != last + 1:
            chunks.append("\n")
        chunks.append(text)
        last = index
    metadata["qq_reply_parts"] = merged
    data["content"] = "".join(chunks).strip()
    return True


def _is_blank_file(path: Path) -> bool:
    """Whether the file exists and holds only whitespace.

    The host historically wrote an empty recent.json and read it as []. The
    atomic store reports that file as "empty file is not valid JSON", so the
    load path special-cases exactly this state and nothing else.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return not text.strip()


# -- store ---------------------------------------------------------------------


class RecentStore:
    """Host-shaped recent.json window: read, append, replace, compress, clean.

    One instance serves every character; the per-path lock is shared
    process-wide. clock is part of the frozen wiring contract but is not
    consulted yet: recent.json carries no timestamps of its own.
    """

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
        write_guard: WriteGuard | None = None,
    ) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        self._write_guard = write_guard

    # -- paths and document -------------------------------------------------

    def _path(self, character: str) -> Path:
        """Resolve <character_dir>/recent.json without creating anything.

        The atomic store creates the parent directory on write, so a read of an
        absent character leaves no directory behind.
        """
        return self._roots.character_dir(character, create=False) / RECENT_FILENAME

    def _load_document(self, path: Path) -> list[dict[str, Any]]:
        """Load and shape-check the bare message array; never writes."""
        try:
            raw = self._store.load(path)
        except CorruptStoreError:
            # The host's historical empty file is the only tolerated "parse
            # failure"; everything else must be inspected by a human.
            if _is_blank_file(path):
                return []
            raise
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise CorruptStoreError(
                f"expected a message array, found {type(raw).__name__}", path=path,
            )
        messages: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                raise CorruptStoreError(
                    f"message[{index}] is not a JSON object", path=path,
                )
            messages.append(dict(item))
        return messages

    def _write_locked(
        self,
        character: str,
        path: Path,
        messages: list[dict[str, Any]],
        *,
        operation: str,
    ) -> None:
        """Permit -> write guard -> atomic replace, inside the file critical section.

        The guard receives the exact payload about to be written, so a barrier
        registration can reject it while the file is still unchanged.
        """
        self._permit.assert_writable(operation, str(path))
        if self._write_guard is not None:
            self._write_guard(character, messages)
        self._store.save(path, messages)

    # -- reads --------------------------------------------------------------

    def load(self, character: str) -> list[dict[str, Any]]:
        """Return the whole recent history exactly as stored (empty when absent)."""
        path = self._path(character)
        with _lock_for(path):
            return self._load_document(path)

    def messages(self, character: str) -> list[dict[str, Any]]:
        """Alias of load(); the prompt window is the stored message list."""
        return self.load(character)

    def count(self, character: str) -> int:
        """Number of stored messages (summaries included)."""
        return len(self.load(character))

    # -- writes -------------------------------------------------------------

    def append(
        self, character: str, messages: Sequence[Mapping[str, Any]],
    ) -> int:
        """Append messages in order and return the resulting total count."""
        new_messages = _validated_messages(messages, what="messages")
        path = self._path(character)
        with _lock_for(path):
            current = self._load_document(path)
            payload = current + new_messages
            self._write_locked(
                character, path, payload, operation="append_recent",
            )
            return len(payload)

    def add_qq_reply_parts(self, character: str, source_key: str, parts: Mapping[int, str]) -> None:
        path = self._path(character)
        with _lock_for(path):
            rows = self._load_document(path)
            changed = False
            for row in rows:
                changed = merge_qq_reply_parts(row, source_key, parts) or changed
            if changed:
                self._write_locked(character, path, rows, operation="qq_reply_parts")

    def replace(
        self, character: str, messages: Sequence[Mapping[str, Any]],
    ) -> None:
        """Atomically replace the whole window with messages.

        The current file is loaded first: a corrupt or unreadable file is never
        silently overwritten by a full replacement.
        """
        payload = _validated_messages(messages, what="messages")
        path = self._path(character)
        with _lock_for(path):
            self._load_document(path)
            self._write_locked(
                character, path, payload, operation="replace_recent",
            )

    def update(self, character: str, transform: Callable) -> list[dict[str, Any]]:
        """Read, transform and write under one file lock, preserving concurrent appends."""
        path = self._path(character)
        with _lock_for(path):
            payload = _validated_messages(transform(self._load_document(path)), what="messages")
            self._write_locked(character, path, payload, operation="update_recent")
            return payload

    # -- compression --------------------------------------------------------

    def apply_compression(
        self,
        character: str,
        *,
        snapshot: Sequence[Mapping[str, Any]],
        summary: Mapping[str, Any] | str,
    ) -> bool:
        """Splice a memo onto the head iff snapshot is still the full prefix.

        Returns False (moot) without writing when the history was cleared, the
        snapshot is empty, the file is shorter than the snapshot, or any
        snapshot message changed while the summary model ran. On success the
        new history is [summary] + current[len(snapshot):], written under the
        same lock after the write guard.
        """
        snapshot_messages = _validated_messages(snapshot, what="snapshot")
        summary_message = _validated_summary(summary)
        path = self._path(character)
        with _lock_for(path):
            current = self._load_document(path)
            if not snapshot_messages or not current:
                # Cleared by the user: reinserting a memo would resurrect
                # content the deletion path already removed.
                return False
            if len(current) < len(snapshot_messages):
                return False
            for index, message in enumerate(snapshot_messages):
                if current[index] != message:
                    return False
            payload = [summary_message, *current[len(snapshot_messages):]]
            self._write_locked(
                character, path, payload, operation="compress_recent",
            )
            return True

    # -- deletion cleanup ---------------------------------------------------

    def delete_episode(
        self,
        character: str,
        *,
        episode_ids: Iterable[str],
        source_keys: Iterable[str],
        match_texts: Iterable[str] = (),
    ) -> dict[str, int]:
        """Remove one episode's original messages and prune derived summaries.

        Original (non-system) messages are removed whole when they carry one of
        the episode ids or source keys; matching is never done on their text.
        Summary messages are removed whole when their metadata matches, and are
        otherwise pruned sentence by sentence by match_texts (a fully matched
        memo is removed). Out-of-scope messages are left untouched.

        Returns {messages_removed, summaries_cleaned, summaries_removed}; the
        file is only rewritten when something changed.
        """
        ids = _clean_id_set(episode_ids)
        keys = _clean_id_set(source_keys)
        texts = _clean_texts(match_texts)
        counts = {
            "messages_removed": 0,
            "summaries_cleaned": 0,
            "summaries_removed": 0,
        }
        if not ids and not keys and not texts:
            return counts
        path = self._path(character)
        with _lock_for(path):
            current = self._load_document(path)
            kept: list[dict[str, Any]] = []
            changed = False
            for message in current:
                metadata_match = bool(
                    (message_episode_ids(message) & ids)
                    or (message_source_keys(message) & keys)
                )
                if is_summary_message(message):
                    if metadata_match:
                        counts["summaries_removed"] += 1
                        changed = True
                        continue
                    content = message_text(message)
                    pruned = _strip_summary(content, texts)
                    if pruned != content:
                        changed = True
                        if pruned:
                            counts["summaries_cleaned"] += 1
                            kept.append(_with_message_text(message, pruned))
                        else:
                            counts["summaries_removed"] += 1
                        continue
                    kept.append(message)
                    continue
                if metadata_match:
                    counts["messages_removed"] += 1
                    changed = True
                    continue
                kept.append(message)
            if changed:
                self._write_locked(
                    character, path, kept, operation="delete_episode_recent",
                )
            return counts

    def filter_summaries(
        self,
        character: str,
        *,
        match_texts: Iterable[str],
        deleted_source_keys: Iterable[str] = (),
    ) -> dict[str, int]:
        """Entry-scope cleanup: prune derived summaries, never original messages.

        Summaries carrying one of deleted_source_keys are removed whole (a
        derived copy of a deleted source must not survive); every other summary
        is pruned sentence by sentence by match_texts. Non-system messages are
        copied through untouched, including ones whose text matches.

        Returns {summaries_cleaned, summaries_removed}.
        """
        texts = _clean_texts(match_texts)
        keys = _clean_id_set(deleted_source_keys)
        counts = {"summaries_cleaned": 0, "summaries_removed": 0}
        if not texts and not keys:
            return counts
        path = self._path(character)
        with _lock_for(path):
            current = self._load_document(path)
            kept: list[dict[str, Any]] = []
            changed = False
            for message in current:
                if not is_summary_message(message):
                    kept.append(message)
                    continue
                if keys and (message_source_keys(message) & keys):
                    counts["summaries_removed"] += 1
                    changed = True
                    continue
                content = message_text(message)
                pruned = _strip_summary(content, texts)
                if pruned != content:
                    changed = True
                    if pruned:
                        counts["summaries_cleaned"] += 1
                        kept.append(_with_message_text(message, pruned))
                    else:
                        counts["summaries_removed"] += 1
                    continue
                kept.append(message)
            if changed:
                self._write_locked(
                    character, path, kept, operation="filter_recent_summaries",
                )
            return counts


__all__ = [
    "RECENT_FILENAME",
    "RecentStore",
    "WriteGuard",
    "is_summary_message",
    "message_episode_ids",
    "message_source_keys",
    "message_text",
]
