"""SQLite time index: the host's raw tables plus FTS5 and a B02 schema gate.

Invariants
----------
* The on-disk tables keep the host's names and columns, so a host reader
  (`memory/timeindex.py`) can open this file unchanged:
  `time_indexed_original` / `time_indexed_compressed` (`id`,
  `session_id`, `message`, plus the `timestamp` column the host backfills
  on open and its `idx_<table>_timestamp` index), `facts_fts_v2`
  (`fts5(fact_id UNINDEXED, content, tokenize='unicode61')`) and
  `facts_fts_meta` (`key`, `value`).
* The host carries NO schema version, so B02 adds one: a `schema_meta`
  table with row `('schema_version', '1')`.
* Opening a brand-new file creates the schema and the version row. Opening an
  existing host database is an ADDITIVE migration: it creates what is missing
  (tables, the `timestamp` column, its index, the FTS tables, `schema_meta`)
  and preserves every row; it never drops a table and never rewrites data.
* A database whose `schema_meta` declares another version is refused with
  `InvalidSchemaError` before any write. A file that is not a SQLite database
  is refused with `CorruptStoreError` and left byte-for-byte untouched.
* Every schema or data write goes through the injected `WritePermit`; reads
  do not. An open that needs no schema work performs no write at all (it does
  not even switch the journal mode), so a read-only open under a rejecting
  permit cannot mutate the file.
* One connection per instance, `check_same_thread=False`, and every access is
  serialized by this instance's `threading.Lock`. One instance owns one
  character's database, so that lock is the per-character lock.

Deliberate B02 gaps (documented, owned by later batches)
--------------------------------------------------------
* `facts_fts_v2.content` stores the record's message verbatim. The host stores
  pre-tokenized text produced by `fts_tokens` (script folding + the recall
  tokenizer + stop-name config); that tokenizer belongs to B04/B06, so B02
  keeps the column shape but not its host-side content format. Rows written
  here are readable by the host's `unicode61` reader for whitespace-delimited
  scripts.
* `facts_fts_meta` is created but not populated: the host writes its
  `backfilled_at` marker after a facts backfill, which is a B04 step.
* The legacy v1 `facts_fts` table is left untouched. The host drops it on its
  own path (privacy erasure wants a single index); B02 never drops tables.
* `insert_original` is the only B02 write path, so it also indexes the row's
  text in `facts_fts_v2` under `fact_id = str(rowid)`; the fact-id mapping
  arrives with B04.
"""

from __future__ import annotations

import sqlite3
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import CorruptStoreError, InvalidSchemaError
from ..ports import Clock, StorageRoots, WritePermit
from .recent import merge_qq_reply_parts, message_source_keys

#: File name inside `<character_dir>`; the host uses this exact name.
DB_FILENAME = "time_indexed.db"

#: Host table names (`config/memory_settings.py`, `memory/timeindex.py`).
ORIGINAL_TABLE = "time_indexed_original"
COMPRESSED_TABLE = "time_indexed_compressed"
FACTS_FTS_TABLE = "facts_fts_v2"
FACTS_FTS_META_TABLE = "facts_fts_meta"

#: B02 addition: the version row the host does not have.
SCHEMA_META_TABLE = "schema_meta"
SCHEMA_VERSION = 1
_SCHEMA_VERSION_KEY = "schema_version"

#: Host reader/writer tolerance for a second process holding the file.
_BUSY_TIMEOUT_MS = 5000

#: Every valid SQLite database starts with this 16-byte header.
_SQLITE_HEADER = b"SQLite format 3\x00"

#: Columns a caller may set; `id` is generated and `timestamp` is nullable.
_RECORD_KEYS = frozenset({"session_id", "message", "timestamp"})

#: Result of the one-time FTS5 probe; `None` means "not probed yet".
_FTS5_AVAILABLE: bool | None = None


def _timestamp_index_name(table: str) -> str:
    """Host index name, including its implicit rowid tie-breaker."""
    return f"idx_{table}_timestamp"


def _require_fts5() -> None:
    """Raise `RuntimeError` when this Python's sqlite3 build lacks FTS5.

    There is no degraded mode: without FTS5 the fact index cannot be created
    or read, so silently returning empty search results would hide the loss.
    """
    global _FTS5_AVAILABLE
    if _FTS5_AVAILABLE is None:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
            _FTS5_AVAILABLE = True
        except sqlite3.OperationalError:
            _FTS5_AVAILABLE = False
        finally:
            probe.close()
    if not _FTS5_AVAILABLE:
        raise RuntimeError(
            "this Python's sqlite3 build lacks FTS5; the fact index "
            "(facts_fts_v2) cannot be created or searched"
        )


def _assert_sqlite_header(path: Path) -> None:
    """Reject a non-SQLite file before connecting, so nothing can touch it."""
    if not path.exists():
        return
    try:
        size = path.stat().st_size
        if size == 0:
            return  # an empty file is a valid empty database
        with open(path, "rb") as handle:
            header = handle.read(len(_SQLITE_HEADER))
    except OSError as exc:
        raise CorruptStoreError(
            f"time index is not a readable file: {exc}", path=path, cause=exc,
        ) from exc
    if header != _SQLITE_HEADER:
        raise CorruptStoreError(
            "file is not a SQLite database", path=path,
        )


def _encode_timestamp(value: Any) -> str | None:
    """Store a timestamp the way the host's readers parse it."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, str) and value:
        return value
    raise ValueError("timestamp must be a datetime, an ISO-8601 string or None")


def _create_table_sql(table: str) -> str:
    """The DDL the host's SQLAlchemy model emits for both message tables."""
    return (
        f"CREATE TABLE IF NOT EXISTS {table} ("
        "id INTEGER NOT NULL, session_id VARCHAR, message TEXT, "
        "PRIMARY KEY (id))"
    )


class TimeIndexStore:
    """One character's `time_indexed.db`, opened through the schema gate.

    `clock` is part of the frozen B02 wiring contract; this store writes no
    timestamps of its own (callers supply the host row's `timestamp`), so it
    is not consulted yet.
    """

    def __init__(
        self,
        *,
        roots: StorageRoots,
        character: str,
        permit: WritePermit,
        clock: Clock | None = None,
    ) -> None:
        self._roots = roots
        self._character = character
        self._permit = permit
        self._clock = clock
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None
        self._path: Path | None = None

    # -- lifecycle ------------------------------------------------------

    def open(self) -> "TimeIndexStore":
        """Open (creating or additively migrating) the database; idempotent."""
        with self._lock:
            if self._connection is not None:
                return self
            path = self._resolve_path()
            fresh = not path.exists()
            if fresh:
                self._permit.assert_writable("create_timeindex", str(path))
            _require_fts5()
            _assert_sqlite_header(path)
            connection: sqlite3.Connection | None = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(
                    str(path), check_same_thread=False, isolation_level=None,
                )
                connection.row_factory = sqlite3.Row
                connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
                self._verify_and_prepare(connection, path, fresh=fresh)
            except BaseException:
                if connection is not None:
                    try:
                        connection.close()
                    except sqlite3.Error:
                        pass
                raise
            self._connection = connection
            self._path = path
            return self

    def close(self) -> None:
        """Close the connection; calling it twice is a no-op."""
        with self._lock:
            connection, self._connection = self._connection, None
            self._path = None
            if connection is None:
                return
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "TimeIndexStore":
        return self.open()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    # -- writes ---------------------------------------------------------

    def insert_original(self, record: Mapping[str, Any]) -> int:
        """Insert one raw conversation row and return its rowid.

        The row is also indexed in `facts_fts_v2` (see the module docstring);
        both writes share one transaction so the index can never point at a
        row that was not committed.
        """
        return self._insert(ORIGINAL_TABLE, record, index_text=True)

    def insert_compressed(self, record: Mapping[str, Any]) -> int:
        """Insert one compressed row and return its rowid (not FTS-indexed)."""
        return self._insert(COMPRESSED_TABLE, record, index_text=False)

    # -- deletion --------------------------------------------------------

    def delete_sessions(self, session_ids: Sequence[str]) -> int:
        """Delete the conversation rows of the given sessions (original+compressed).

        Removes the FTS rows of every deleted original row in the same
        transaction, so the index can never point at a missing row. A missing
        database is zero rows and is not created by a cleanup (a read-only
        deletion step must not materialize a store). Returns the deleted row
        count; callers treat an unreadable/unwritable database as a failed
        deletion step instead of a silent zero.
        """
        ids = [value for value in session_ids or () if isinstance(value, str) and value]
        if not ids:
            return 0
        path = self._resolve_path()
        if not path.exists():
            return 0
        self.open()
        with self._lock:
            connection = self._require_open()
            self._permit.assert_writable("delete_timeindexed_sessions", str(self._path))
            placeholders = ",".join("?" for _ in ids)
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    f"SELECT id FROM {ORIGINAL_TABLE} "
                    f"WHERE session_id IN ({placeholders})",
                    ids,
                ).fetchall()
                row_ids = [int(row[0]) for row in rows]
                if row_ids:
                    row_placeholders = ",".join("?" for _ in row_ids)
                    connection.execute(
                        f"DELETE FROM {FACTS_FTS_TABLE} "
                        f"WHERE fact_id IN ({row_placeholders})",
                        [str(row_id) for row_id in row_ids],
                    )
                    connection.execute(
                        f"DELETE FROM {ORIGINAL_TABLE} "
                        f"WHERE id IN ({row_placeholders})",
                        row_ids,
                    )
                cursor = connection.execute(
                    f"DELETE FROM {COMPRESSED_TABLE} "
                    f"WHERE session_id IN ({placeholders})",
                    ids,
                )
                deleted = len(row_ids) + max(0, int(cursor.rowcount or 0))
                connection.execute("COMMIT")
            except BaseException:
                self._rollback(connection)
                raise
            return deleted

    def delete_sources(self, source_keys: Sequence[str]) -> int:
        """Remove exactly the source-linked rows and their FTS entries."""
        wanted = set(source_keys)
        if not wanted or not self._resolve_path().exists():
            return 0
        self.open()
        with self._lock:
            connection = self._require_open()
            self._permit.assert_writable("delete_timeindexed_sources", str(self._path))
            connection.execute("BEGIN IMMEDIATE")
            deleted = 0
            try:
                for table in (ORIGINAL_TABLE, COMPRESSED_TABLE):
                    ids = []
                    for row in connection.execute(f"SELECT id, message FROM {table}").fetchall():
                        try:
                            message = json.loads(row[1])
                        except json.JSONDecodeError:
                            continue  # Historical plain-text rows have no source metadata.
                        if message_source_keys(message) & wanted:
                            ids.append((int(row[0]),))
                    if ids:
                        if table == ORIGINAL_TABLE:
                            connection.executemany(f"DELETE FROM {FACTS_FTS_TABLE} WHERE fact_id=?", [(str(row[0]),) for row in ids])
                        connection.executemany(f"DELETE FROM {table} WHERE id=?", ids)
                        deleted += len(ids)
                connection.execute("COMMIT")
            except BaseException:
                self._rollback(connection)
                raise
            return deleted

    # -- reads ----------------------------------------------------------

    def get_original(self, row_id: int) -> dict[str, Any] | None:
        """Return one raw row by rowid, or None when it does not exist."""
        with self._lock:
            connection = self._require_open()
            row = connection.execute(
                f"SELECT id, session_id, message, timestamp "
                f"FROM {ORIGINAL_TABLE} WHERE id = ?",
                (int(row_id),),
            ).fetchone()
            return None if row is None else dict(row)

    def original_for_sources(self, source_keys: Sequence[str], *, limit: int = 40) -> list[dict[str, Any]]:
        """Read source-linked exchanges in business order without an FTS guess."""
        keys = sorted(set(source_keys))
        if not keys:
            return []
        placeholders = ",".join("?" for _ in keys)
        with self._lock:
            connection = self._require_open()
            rows = connection.execute(
                f"SELECT id, session_id, message, timestamp FROM {ORIGINAL_TABLE} "
                "WHERE EXISTS (SELECT 1 FROM json_tree("
                "CASE WHEN json_valid(message) THEN message ELSE '{}' END) "
                f"WHERE type='text' AND value IN ({placeholders})) "
                "ORDER BY timestamp, id LIMIT ?",
                (*keys, int(limit)),
            ).fetchall()
            # Only metadata bindings establish identity; a key quoted in text
            # is a SQL prefilter hit, not an exchange citation.
            return [dict(row) for row in rows
                    if message_source_keys(json.loads(row["message"])) & set(keys)]

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Rank `facts_fts_v2` rows against an FTS5 query, best match first.

        A blank query returns `[]` (FTS5 raises on an empty match string).
        Malformed FTS5 syntax raises `ValueError` instead of leaking a
        `sqlite3.OperationalError`: the store's own schema is guaranteed by
        `open()`, so an operational failure here means the caller's query.
        """
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        text = query.strip()
        if not text:
            return []
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            connection = self._require_open()
            try:
                rows = connection.execute(
                    f"SELECT fact_id, content, bm25({FACTS_FTS_TABLE}) AS score "
                    f"FROM {FACTS_FTS_TABLE} "
                    f"WHERE {FACTS_FTS_TABLE} MATCH ? "
                    f"ORDER BY score LIMIT ?",
                    (text, int(limit)),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError(f"invalid FTS5 query {query!r}: {exc}") from exc
            return [dict(row) for row in rows]

    def search_original(
        self, *, subjects: Sequence[str], scene: str | None, session_id: str,
        terms: Sequence[str], window: tuple[datetime, datetime] | None, limit: int,
    ) -> list[dict[str, Any]]:
        """Search stored dialogue with scope and time applied before the limit."""
        if not subjects or not (terms or window) or not self._resolve_path().is_file():
            return []
        metadata = "coalesce(json_extract(message,'$.metadata'), json_extract(message,'$.data.metadata'))"
        body = "json_extract(message,'$.data.content')"
        placeholders = ",".join("?" for _ in subjects)
        conditions = [
            f"coalesce(nullif(json_extract({metadata},'$.subject'),''),'private') IN ({placeholders})",
            "json_extract(message,'$.type') IN ('human','ai')",
            f"{body} IS NOT NULL",
        ]
        args: list[Any] = list(subjects)
        if scene is not None:
            conditions.append(f"coalesce(json_extract({metadata},'$.scene'),'') = ?")
            args.append(scene)
        if session_id:
            conditions.append("session_id = ?")
            args.append(session_id)
        if window:
            conditions.append("julianday(timestamp) >= julianday(?) AND julianday(timestamp) < julianday(?)")
            args.extend(value.astimezone().isoformat() for value in window)
        if terms:
            conditions.append("(" + " OR ".join(f"instr(lower({body}), ?) > 0" for _ in terms) + ")")
            args.extend(terms)
        with self:
            rows = self._require_open().execute(
                f"SELECT id, message, timestamp FROM {ORIGINAL_TABLE} "
                "WHERE CASE WHEN json_valid(message) THEN " + " AND ".join(conditions)
                + " ELSE 0 END ORDER BY julianday(timestamp) DESC, id DESC LIMIT ?",
                (*args, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def add_qq_reply_parts(self, session_id: str, source_key: str, parts: Mapping[int, str]) -> None:
        if not self._resolve_path().is_file():
            return
        with self:
            connection = self._require_open()
            rows = connection.execute(
                f"SELECT id, message FROM {ORIGINAL_TABLE} WHERE session_id = ? "
                "AND CASE WHEN json_valid(message) THEN json_extract(message,'$.data.metadata.source_key') = ? END",
                (session_id, source_key),
            ).fetchall()
            changes = []
            for row in rows:
                message = json.loads(row["message"])
                if merge_qq_reply_parts(message, source_key, parts):
                    changes.append((json.dumps(message, ensure_ascii=False), row["id"]))
            if not changes:
                return
            self._permit.assert_writable("qq_reply_parts", str(self._path))
            connection.execute("BEGIN IMMEDIATE")
            try:
                for message, row_id in changes:
                    connection.execute(f"UPDATE {ORIGINAL_TABLE} SET message = ? WHERE id = ?", (message, row_id))
                    connection.execute(f"DELETE FROM {FACTS_FTS_TABLE} WHERE fact_id = ?", (str(row_id),))
                    connection.execute(f"INSERT INTO {FACTS_FTS_TABLE}(fact_id, content) VALUES (?, ?)", (str(row_id), message))
                connection.execute("COMMIT")
            except BaseException:
                self._rollback(connection)
                raise

    def count_original(self) -> int:
        """Number of raw conversation rows."""
        return self._count(ORIGINAL_TABLE)

    def count_compressed(self) -> int:
        """Number of compressed rows."""
        return self._count(COMPRESSED_TABLE)

    # -- internals ------------------------------------------------------

    def _resolve_path(self) -> Path:
        """Resolve `<character_dir>/time_indexed.db` without creating anything."""
        directory = self._roots.character_dir(self._character, create=False)
        return directory / DB_FILENAME

    def _require_open(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("time index is not open; call open() first")
        return self._connection

    def _insert(
        self,
        table: str,
        record: Mapping[str, Any],
        *,
        index_text: bool,
    ) -> int:
        values = self._record_values(record)
        with self._lock:
            connection = self._require_open()
            path = self._path
            self._permit.assert_writable("insert_timeindexed", str(path))
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    f"INSERT INTO {table}(session_id, message, timestamp) "
                    f"VALUES(?, ?, ?)",
                    (values["session_id"], values["message"], values["timestamp"]),
                )
                row_id = int(cursor.lastrowid)
                if index_text:
                    connection.execute(
                        f"INSERT INTO {FACTS_FTS_TABLE}(fact_id, content) "
                        f"VALUES(?, ?)",
                        (str(row_id), values["message"]),
                    )
                connection.execute("COMMIT")
            except BaseException:
                self._rollback(connection)
                raise
            return row_id

    def _count(self, table: str) -> int:
        with self._lock:
            connection = self._require_open()
            row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            return int(row[0])

    @staticmethod
    def _record_values(record: Mapping[str, Any]) -> dict[str, Any]:
        """Validate one caller record against the host column set."""
        if not isinstance(record, Mapping):
            raise ValueError("record must be a mapping of host columns")
        unknown = set(record) - _RECORD_KEYS
        if unknown:
            raise ValueError(
                f"unknown record keys {sorted(unknown)}; expected a subset of "
                f"{sorted(_RECORD_KEYS)}"
            )
        session_id = record.get("session_id")
        message = record.get("message")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("record.session_id must be a nonempty string")
        if not isinstance(message, str):
            raise ValueError("record.message must be a string")
        return {
            "session_id": session_id,
            "message": message,
            "timestamp": _encode_timestamp(record.get("timestamp")),
        }

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _verify_and_prepare(
        self,
        connection: sqlite3.Connection,
        path: Path,
        *,
        fresh: bool,
    ) -> None:
        """Gate on the version, then add whatever the host schema is missing."""
        tables, indexes = self._inventory(connection, path)
        if SCHEMA_META_TABLE in tables:
            version = self._read_version(connection, path)
            if version != SCHEMA_VERSION:
                raise InvalidSchemaError(
                    f"unsupported time index schema_version {version}; this "
                    f"build supports {SCHEMA_VERSION}",
                    path=path,
                )
        missing = self._missing_parts(connection, tables, indexes)
        if not missing:
            return
        if not fresh:
            # A brand-new file already passed the permit check in open().
            self._permit.assert_writable("migrate_timeindex", str(path))
        # Journal mode is an engine setting, not data, but it does touch the
        # file header, so it is applied only on the write-capable open path.
        connection.execute("PRAGMA journal_mode = WAL").fetchone()
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table in (ORIGINAL_TABLE, COMPRESSED_TABLE):
                connection.execute(_create_table_sql(table))
                self._ensure_timestamp_column(connection, table)
            connection.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {FACTS_FTS_TABLE} "
                f"USING fts5(fact_id UNINDEXED, content, tokenize='unicode61')"
            )
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {FACTS_FTS_META_TABLE} "
                f"(key TEXT PRIMARY KEY, value TEXT)"
            )
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {SCHEMA_META_TABLE} "
                f"(key TEXT PRIMARY KEY, value TEXT)"
            )
            connection.execute(
                f"INSERT OR IGNORE INTO {SCHEMA_META_TABLE}(key, value) "
                f"VALUES(?, ?)",
                (_SCHEMA_VERSION_KEY, str(SCHEMA_VERSION)),
            )
            connection.execute("COMMIT")
        except BaseException:
            self._rollback(connection)
            raise

    def _inventory(
        self,
        connection: sqlite3.Connection,
        path: Path,
    ) -> tuple[set[str], set[str]]:
        """Read the table and index names; unreadable pages mean corruption."""
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            indexes = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
        except sqlite3.DatabaseError as exc:
            raise CorruptStoreError(
                f"time index cannot be read as a SQLite database: {exc}",
                path=path,
                cause=exc,
            ) from exc
        return tables, indexes

    def _read_version(self, connection: sqlite3.Connection, path: Path) -> int:
        row = connection.execute(
            f"SELECT value FROM {SCHEMA_META_TABLE} WHERE key = ?",
            (_SCHEMA_VERSION_KEY,),
        ).fetchone()
        if row is None:
            raise CorruptStoreError(
                f"{SCHEMA_META_TABLE} has no {_SCHEMA_VERSION_KEY} row", path=path,
            )
        raw = row[0]
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise CorruptStoreError(
                f"non-integer {_SCHEMA_VERSION_KEY} {raw!r}", path=path, cause=exc,
            ) from exc

    @staticmethod
    def _missing_parts(
        connection: sqlite3.Connection,
        tables: set[str],
        indexes: set[str],
    ) -> list[str]:
        """Describe the host schema pieces this database still lacks."""
        missing: list[str] = []
        for table in (ORIGINAL_TABLE, COMPRESSED_TABLE):
            if table not in tables:
                missing.append(table)
                continue
            columns = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if "timestamp" not in columns:
                missing.append(f"{table}.timestamp")
            if _timestamp_index_name(table) not in indexes:
                missing.append(f"{table}.timestamp_index")
        if FACTS_FTS_TABLE not in tables:
            missing.append(FACTS_FTS_TABLE)
        if FACTS_FTS_META_TABLE not in tables:
            missing.append(FACTS_FTS_META_TABLE)
        if SCHEMA_META_TABLE not in tables:
            missing.append(SCHEMA_META_TABLE)
        return missing

    @staticmethod
    def _ensure_timestamp_column(
        connection: sqlite3.Connection, table: str,
    ) -> None:
        """Host schema repair: add `timestamp` and its read index if absent."""
        columns = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if "timestamp" not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN timestamp DATETIME"
            )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS {_timestamp_index_name(table)} "
            f"ON {table}(timestamp)"
        )
