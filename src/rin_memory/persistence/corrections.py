"""Host-compatible correction store (B03, \`memory_corrections.json\`).

Explicit user preferences and fact corrections are persisted per character so
the next turn keeps responding from the corrected understanding even after the
original message left the recent window (design §9.3, §17.9.1).

On-disk shape is the host's schema 1:

    {"schema": 1, "version": <int>, "corrections": [entry, ...]}

* \`version\` is a document counter: every successful real write increments it
  once, and a duplicate active submission does not. Read paths compare the
  version before/after work to discard in-flight results (together with the
  deletion version, design §17.8).
* \`correction_id\` is deterministic (see :func:\`preference_correction_id\` and
  :func:\`fact_correction_id\`), so a retried request maps to the same entry.
* Entries keep the host field names (\`kind/action/text/replaces_text/
  target_fact_ids/target_source_keys/applies_to/derived_invalidations/...\`);
  unknown fields a newer host wrote are preserved on write.

Deviation from the host (documented in design §17.15): a damaged file or an
unknown schema raises \`CorruptStoreError\` / \`InvalidSchemaError\` instead of
reading as empty. Reading as empty would silently reactivate corrected
knowledge and let a later write replace the damaged file. Single dirty entries
also raise instead of being dropped, so a damaged store is never silently
"cleaned" into a smaller one.

The journal carries one \`core.correction_recorded\` entry per written change
(payload \`{"entry": ..., "superseded": [...]}\`); replay is idempotent and
never downgrades propagation results that were written after the event.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..contracts import MemoryBlock
from ..errors import (
    CorrectionNotFoundError,
    CorruptStoreError,
    InvalidSchemaError,
)
from ..invalidation import row_matches_subject, scene_applies
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .version_cache import VersionReader

CORRECTIONS_FILENAME = "memory_corrections.json"
CORRECTIONS_SCHEMA = 1
MAX_CORRECTIONS = 64
MAX_TEXT_CHARS = 600
MAX_ID_CHARS = 120
MAX_TARGET_KEYS = 16
MAX_TARGET_FACTS = 32
MAX_SOURCE_KEY_CHARS = 200
MAX_DERIVED_IDS = 32
MAX_VERSION_HISTORY = 8

KIND_PREFERENCE = "preference"
KIND_FACT_CORRECTION = "fact_correction"
KINDS = (KIND_PREFERENCE, KIND_FACT_CORRECTION)

STATUS_ACTIVE = "active"
STATUS_RETRACTED = "retracted"
STATUS_SUPERSEDED = "superseded"
STATUSES = (STATUS_ACTIVE, STATUS_RETRACTED, STATUS_SUPERSEDED)

ACTION_REPLACE = "replace"
ACTION_RETRACT = "retract"
ACTIONS = (ACTION_REPLACE, ACTION_RETRACT)

_SCENE_KEYS = ("plugin_id", "game_id", "session_id")
#: Default correction block budget (host \`CORRECTION_BLOCK_MAX_TOKENS\`).
DEFAULT_BLOCK_TOKENS = 200

#: One lock per correction file; created lazily, never held across an await.
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


# -- value helpers ----------------------------------------------------------


def _sha1(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()


def _iso(clock: Clock, ts: float | None = None) -> str:
    """Host timestamp format: local ISO-8601 without offset, second precision."""
    if ts is None:
        moment = clock.now().astimezone()
    else:
        moment = datetime.fromtimestamp(ts).astimezone()
    return moment.isoformat(timespec="seconds")


def _bounded_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _normalize_id_list(value: Any, limit: int) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        value = [value]
    if not isinstance(value, Iterable):
        return []
    out: list[str] = []
    for item in value:
        text = _bounded_text(item, MAX_ID_CHARS)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _parse_timestamp(value: Any) -> float | None:
    """Collapse an ISO string / epoch seconds into epoch seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _normalize_expires_at(value: Any) -> str | None:
    ts = _parse_timestamp(value)
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _normalize_scene(value: Any) -> dict[str, str]:
    """Correction scene: plugin_id/game_id/session_id plus optional scope."""
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for key in _SCENE_KEYS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            out[key] = item.strip()[:64]
    scope = value.get("scope")
    if isinstance(scope, str) and scope.strip():
        out["scope"] = scope.strip().lower()
    return out


def _normalize_subject(value: Any) -> Any:
    """Correction subject: default private; a group subject keeps its mapping."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key in ("subject_kind", "kind", "subject_id", "scope"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                out[key] = item.strip()[:120]
        return out or "private"
    text = _bounded_text(value, 120)
    return text or "private"


def _normalize_source_key(value: Any, fallback_seed: str) -> str:
    text = _bounded_text(value, MAX_SOURCE_KEY_CHARS)
    if text:
        return text
    return "host:correction:" + _sha1(fallback_seed)[:16]


def _normalize_derived(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, Mapping):
        return {"fact_ids": [], "reflection_ids": [], "persona_ids": []}
    return {
        "fact_ids": _normalize_id_list(raw.get("fact_ids"), MAX_DERIVED_IDS),
        "reflection_ids": _normalize_id_list(raw.get("reflection_ids"), MAX_DERIVED_IDS),
        "persona_ids": _normalize_id_list(raw.get("persona_ids"), MAX_DERIVED_IDS),
    }


def _merge_derived(
    current: Any, incoming: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Union two derived-invalidation documents (order preserved, bounded)."""
    base = _normalize_derived(current)
    extra = _normalize_derived(incoming)
    merged: dict[str, list[str]] = {}
    for name in ("fact_ids", "reflection_ids", "persona_ids"):
        values = list(base[name])
        for value in extra[name]:
            if value not in values:
                values.append(value)
        merged[name] = values[:MAX_DERIVED_IDS]
    return merged


# -- deterministic ids ------------------------------------------------------


def preference_correction_id(source_key: str, text: str) -> str:
    """Host id for an explicit preference: \`corr_ + sha1(...)[:16]\`."""
    return "corr_" + _sha1(f"preference|{source_key}|{text}")[:16]


def fact_correction_id(
    source_key: str,
    target_fact_ids: Sequence[str],
    target_source_keys: Sequence[str],
    old_text: str,
    new_text: str,
    action: str,
) -> str:
    """Host id for a fact correction (targets sorted, fixed field order)."""
    return "corr_" + _sha1(
        "correction|"
        + source_key
        + "|"
        + ",".join(sorted(target_fact_ids))
        + "|"
        + ",".join(sorted(target_source_keys))
        + "|"
        + old_text
        + "|"
        + new_text
        + "|"
        + action
    )[:16]


def normalize_correction_spec(spec: Any, *, now: str) -> dict[str, Any] | None:
    """Collapse a structured correction request into the stored spec.

    Two host shapes are produced: a \`preference\` (no targets/old text, just a
    new explicit preference) and a \`fact_correction\` (targets and/or old text,
    optionally a replacement). Returns None when nothing usable was requested.
    """
    if not isinstance(spec, Mapping):
        return None
    action = _bounded_text(spec.get("action"), 16) or ACTION_REPLACE
    if action not in ACTIONS:
        action = ACTION_REPLACE
    target_fact_ids = _normalize_id_list(spec.get("target_fact_ids"), MAX_TARGET_FACTS)
    target_source_keys = _normalize_id_list(
        spec.get("target_source_keys"), MAX_TARGET_KEYS,
    )
    old_text = _bounded_text(spec.get("old_text"), MAX_TEXT_CHARS)
    new_text = _bounded_text(spec.get("new_text"), MAX_TEXT_CHARS)
    if not new_text and action == ACTION_REPLACE:
        new_text = _bounded_text(spec.get("text"), MAX_TEXT_CHARS)
    if action == ACTION_RETRACT:
        new_text = ""
    kind = _bounded_text(spec.get("kind"), 32)
    if kind not in KINDS:
        kind = (
            KIND_PREFERENCE
            if not target_fact_ids and not target_source_keys and not old_text
            else KIND_FACT_CORRECTION
        )
    if kind == KIND_PREFERENCE:
        if not new_text:
            return None
        source_key = _normalize_source_key(
            spec.get("source_key"), f"preference:{new_text}",
        )
        correction_id = preference_correction_id(source_key, new_text)
    else:
        if not (target_fact_ids or target_source_keys or old_text or new_text):
            return None
        source_key = _normalize_source_key(
            spec.get("source_key"),
            "|".join(
                [action, old_text, new_text,
                 *sorted(target_fact_ids), *sorted(target_source_keys)]
            ),
        )
        correction_id = spec.get("operation_id") or fact_correction_id(
            source_key, target_fact_ids, target_source_keys,
            old_text, new_text, action,
        )
    return {
        "correction_id": correction_id,
        "kind": kind,
        "action": action,
        "text": new_text,
        "replaces_text": old_text or None,
        "target_fact_ids": target_fact_ids,
        "target_source_keys": target_source_keys,
        "source_key": source_key,
        "applies_to": _normalize_scene(
            spec.get("applies_to") or spec.get("scene"),
        ),
        "expires_at": _normalize_expires_at(spec.get("expires_at")),
        "subject": _normalize_subject(spec.get("subject")),
        "created_at": _bounded_text(spec.get("created_at"), 40) or now,
    }


# -- normalization (strict read) --------------------------------------------


def _normalize_entry(raw: Any, path: Path) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError("correction entry is not an object", path=path)
    correction_id = _bounded_text(raw.get("correction_id"), MAX_ID_CHARS)
    if not correction_id:
        raise CorruptStoreError("correction entry has no correction_id", path=path)
    kind = _bounded_text(raw.get("kind"), 32)
    if kind not in KINDS:
        raise CorruptStoreError(
            f"correction {correction_id!r} has unknown kind {kind!r}", path=path,
        )
    status = _bounded_text(raw.get("status"), 16) or STATUS_ACTIVE
    if status not in STATUSES:
        raise CorruptStoreError(
            f"correction {correction_id!r} has unknown status {status!r}", path=path,
        )
    action = _bounded_text(raw.get("action"), 16) or ACTION_REPLACE
    if action not in ACTIONS:
        raise CorruptStoreError(
            f"correction {correction_id!r} has unknown action {action!r}", path=path,
        )
    entry = dict(raw)
    entry.update({
        "correction_id": correction_id,
        "kind": kind,
        "subject": _normalize_subject(raw.get("subject")),
        "text": _bounded_text(raw.get("text"), MAX_TEXT_CHARS),
        "replaces_text": _bounded_text(raw.get("replaces_text"), MAX_TEXT_CHARS) or None,
        "action": action,
        "target_source_keys": _normalize_id_list(
            raw.get("target_source_keys"), MAX_TARGET_KEYS,
        ),
        "target_fact_ids": _normalize_id_list(raw.get("target_fact_ids"), MAX_TARGET_FACTS),
        "applies_to": _normalize_scene(raw.get("applies_to")),
        "created_at": _bounded_text(raw.get("created_at"), 40) or _iso_placeholder(),
        "valid_from": _bounded_text(raw.get("valid_from"), 40),
        "expires_at": _normalize_expires_at(raw.get("expires_at")),
        "status": status,
        "source_key": _bounded_text(raw.get("source_key"), MAX_SOURCE_KEY_CHARS),
        "derived_invalidations": _normalize_derived(raw.get("derived_invalidations")),
        "invalidated_source_keys": _normalize_id_list(
            raw.get("invalidated_source_keys"), MAX_TARGET_KEYS,
        ),
        "replacement_fact_id": _bounded_text(raw.get("replacement_fact_id"), MAX_ID_CHARS) or None,
        "superseded_by": _bounded_text(raw.get("superseded_by"), MAX_ID_CHARS) or None,
        "superseded_at": _bounded_text(raw.get("superseded_at"), 40) or None,
        "retracted_at": _bounded_text(raw.get("retracted_at"), 40) or None,
    })
    if not entry["valid_from"]:
        entry["valid_from"] = entry["created_at"]
    return entry


def _iso_placeholder() -> str:
    """Deterministic fallback for a legacy row without timestamps."""
    return "1970-01-01T00:00:00"


def _empty_doc() -> dict[str, Any]:
    return {"schema": CORRECTIONS_SCHEMA, "version": 0, "corrections": []}


def _normalize_doc(raw: Any, path: Path) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"expected a JSON object, found {type(raw).__name__}", path=path,
        )
    schema = raw.get("schema")
    if schema is None:
        schema = raw.get("schema_version")
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise InvalidSchemaError(
            f"correction store has no integer schema: {schema!r}", path=path,
        )
    if schema != CORRECTIONS_SCHEMA:
        raise InvalidSchemaError(
            f"unsupported correction schema {schema}; this build supports "
            f"{CORRECTIONS_SCHEMA}", path=path,
        )
    doc = _empty_doc()
    version = raw.get("version")
    if version is not None:
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise CorruptStoreError(
                f"correction store version must be a nonnegative integer, "
                f"found {version!r}", path=path,
            )
        doc["version"] = version
    rows = raw.get("corrections")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise CorruptStoreError("correction store 'corrections' must be a list", path=path)
    for row in rows:
        doc["corrections"].append(_normalize_entry(row, path))
    _rotate_doc(doc)
    return doc


def _rotate_doc(doc: dict[str, Any]) -> None:
    """Keep ≤64 entries: drop retracted/superseded first, then oldest active."""
    rows = doc.get("corrections")
    if not isinstance(rows, list) or len(rows) <= MAX_CORRECTIONS:
        return
    rows.sort(key=lambda item: (
        1 if item.get("status") == STATUS_ACTIVE else 0,
        str(item.get("created_at") or ""),
    ))
    del rows[: len(rows) - MAX_CORRECTIONS]


def _find_entry(doc: Mapping[str, Any], correction_id: str) -> dict[str, Any] | None:
    for entry in doc.get("corrections") or ():
        if isinstance(entry, dict) and entry.get("correction_id") == correction_id:
            return entry
    return None


def _is_expired(entry: Mapping[str, Any], now: float) -> bool:
    expires_at = _parse_timestamp(entry.get("expires_at"))
    return expires_at is not None and expires_at <= now


# -- rendering --------------------------------------------------------------

_RENDER_LABELS: dict[str, dict[str, str]] = {
    "zh": {"header": "【当前认识与偏好】", "preference": "偏好", "correction": "更正", "withdrawn": "已撤回"},
    "zh-TW": {"header": "【目前認識與偏好】", "preference": "偏好", "correction": "更正", "withdrawn": "已撤回"},
    "en": {"header": "[Current understanding and preferences]", "preference": "Preference", "correction": "Correction", "withdrawn": "Withdrawn"},
    "ja": {"header": "【現在の理解と好み】", "preference": "好み", "correction": "訂正", "withdrawn": "撤回済み"},
    "ko": {"header": "【현재 이해와 선호】", "preference": "선호", "correction": "정정", "withdrawn": "철회됨"},
    "ru": {"header": "[Текущее понимание и предпочтения]", "preference": "Предпочтение", "correction": "Поправка", "withdrawn": "Отозвано"},
    "es": {"header": "[Comprensión y preferencias actuales]", "preference": "Preferencia", "correction": "Corrección", "withdrawn": "Retirado"},
    "pt": {"header": "[Entendimento e preferências atuais]", "preference": "Preferência", "correction": "Correção", "withdrawn": "Retirado"},
}


def normalize_language(language: Any) -> str:
    """Map a host language tag onto the eight supported block languages."""
    raw = str(language or "").strip().replace("_", "-").lower()
    if raw.startswith("zh-tw") or raw.startswith("zh-hant"):
        return "zh-TW"
    if raw.startswith("zh"):
        return "zh"
    base = raw.split("-")[0]
    return base if base in ("en", "ja", "ko", "ru", "es", "pt") else "en"


def _estimate_tokens(text: str) -> int:
    """Cheap CJK-aware token estimate (B06 replaces it with TokenCounter)."""
    cjk = 0
    other = 0
    for char in text:
        if "\u4e00" <= char <= "\u9fff" or "\u3040" <= char <= "\u30ff":
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


def render_block(
    items: Sequence[Mapping[str, Any]], language: Any, limit_tokens: int,
    *, counter: Any = None,
) -> str:
    """Render active corrections as a localized block within the token budget.

    `counter` is the injected B06 TokenCounter port; without it the legacy
    CJK estimate is used (B03 behaviour) so direct callers stay compatible.
    """
    if not items or limit_tokens <= 0:
        return ""
    labels = _RENDER_LABELS[normalize_language(language)]
    lines: list[str] = [labels["header"]]
    for entry in items:
        text = str(entry.get("text") or "").strip()
        if entry.get("kind") == KIND_PREFERENCE:
            if text:
                lines.append(f"{len(lines)}. {labels['preference']}: {text}")
            continue
        action = entry.get("action") or ACTION_REPLACE
        replaces = str(entry.get("replaces_text") or "").strip()
        if action == ACTION_RETRACT:
            if text:
                lines.append(f"{len(lines)}. {labels['withdrawn']}: {text}")
            continue
        if replaces and text:
            lines.append(f"{len(lines)}. {labels['correction']}: {replaces} -> {text}")
        elif text:
            lines.append(f"{len(lines)}. {labels['correction']}: {text}")
    if len(lines) <= 1:
        return ""
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = counter.count(line) if counter is not None else _estimate_tokens(line)
        if kept and used + cost > limit_tokens:
            break
        kept.append(line)
        used += cost
    if len(kept) <= 1:
        return ""
    return "\n".join(kept).strip()


# -- prepared write ---------------------------------------------------------


@dataclass(frozen=True)
class PreparedCorrection:
    """One correction write; \`payload\` is the journal snapshot to append."""

    entry: Mapping[str, Any]
    payload: Mapping[str, Any] | None = None
    duplicate: bool = False
    revived: bool = False


class CorrectionStore:
    """Per-character \`memory_corrections.json\` with a strict read gate."""

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
        #: `version()` is called on every barrier predicate; run the strict
        #: parser once per unchanged file signature instead of once per call.
        self._versions = VersionReader(store, self._parse_version)

    # -- paths -----------------------------------------------------------

    def _path(self, character: str) -> Path:
        return self._roots.character_dir(character, create=False).joinpath(
            CORRECTIONS_FILENAME,
        )

    # -- document IO -----------------------------------------------------

    def load_view(self, character: str) -> dict[str, Any]:
        path = self._path(character)
        with _lock_for(path):
            raw = self._store.load(path)
            if raw is None:
                return _empty_doc()
            return _normalize_doc(raw, path)

    def save_view(self, character: str, view: Mapping[str, Any]) -> None:
        path = self._path(character)
        with _lock_for(path):
            doc = _normalize_doc(view, path)
            self._permit.assert_writable("save_corrections", str(path))
            self._store.save(path, {
                "schema": CORRECTIONS_SCHEMA,
                "version": int(doc.get("version") or 0),
                "corrections": list(doc.get("corrections") or []),
            })
            self._versions.invalidate(path)

    def version(self, character: str) -> int:
        """Current monotonic document version; 0 for a fresh store.

        The document is parsed with the same strict `_normalize_doc` as every
        other read (so corruption and unknown schemas still fail closed), but
        the result is reused while the file signature is unchanged: the barrier
        asks for this version on every predicate.
        """
        return self._versions.version(self._path(character))

    @staticmethod
    def _parse_version(raw: Any, path: Path) -> int:
        return int(_normalize_doc(raw, path).get("version") or 0)

    def snapshot(self, character: str) -> dict[str, Any]:
        return json.loads(json.dumps(self.load_view(character)))

    # -- writes ----------------------------------------------------------

    def prepare(self, character: str, spec: Any) -> PreparedCorrection:
        """Validate one correction write and build the replay payload.

        The caller appends \`payload\` to the journal, applies it through
        \`apply_snapshot\` and saves; propagation results are written afterwards
        by :meth:\`set_derived\`. A duplicate active submission returns the
        stored entry without a payload (no version bump).
        """
        now = _iso(self._clock)
        normalized = normalize_correction_spec(spec, now=now)
        if normalized is None:
            raise ValueError("correction request has no usable target or text")
        path = self._path(character)
        with _lock_for(path):
            raw = self._store.load(path)
            doc = _empty_doc() if raw is None else _normalize_doc(raw, path)
            existing = _find_entry(doc, normalized["correction_id"])
            if existing is not None and existing.get("status") == STATUS_ACTIVE:
                return PreparedCorrection(entry=dict(existing), duplicate=True)
            superseded: list[str] = []
            if normalized["kind"] == KIND_PREFERENCE:
                for entry in doc.get("corrections") or ():
                    if (
                        isinstance(entry, dict)
                        and entry.get("kind") == KIND_PREFERENCE
                        and entry.get("status") == STATUS_ACTIVE
                        and entry.get("source_key") == normalized["source_key"]
                        and entry.get("correction_id") != normalized["correction_id"]
                    ):
                        superseded.append(str(entry["correction_id"]))
            if existing is not None:
                # A retracted/superseded entry re-expressed: revive in place
                # (same id) instead of adding a duplicate row.
                entry = dict(existing)
                entry.update({
                    "status": STATUS_ACTIVE,
                    "text": normalized["text"] or normalized["text"],
                    "replaces_text": normalized["replaces_text"],
                    "action": normalized["action"],
                    "applies_to": normalized["applies_to"],
                    "valid_from": now,
                    "expires_at": normalized["expires_at"],
                    "retracted_at": None,
                    "superseded_at": None,
                    "superseded_by": None,
                })
                revived = True
            else:
                entry = {
                    "correction_id": normalized["correction_id"],
                    "kind": normalized["kind"],
                    "subject": normalized["subject"],
                    "text": normalized["text"],
                    "replaces_text": normalized["replaces_text"],
                    "action": normalized["action"],
                    "target_source_keys": list(normalized["target_source_keys"]),
                    "target_fact_ids": list(normalized["target_fact_ids"]),
                    "applies_to": dict(normalized["applies_to"]),
                    "created_at": normalized["created_at"],
                    "valid_from": now,
                    "expires_at": normalized["expires_at"],
                    "status": STATUS_ACTIVE,
                    "source_key": normalized["source_key"],
                    "derived_invalidations": {
                        "fact_ids": [], "reflection_ids": [], "persona_ids": [],
                    },
                    "invalidated_source_keys": [],
                    "replacement_fact_id": None,
                    "superseded_by": None,
                    "superseded_at": None,
                    "retracted_at": None,
                }
                revived = False
            payload = {"entry": dict(entry), "superseded": superseded}
            return PreparedCorrection(
                entry=dict(entry), payload=payload, revived=revived,
            )

    def apply_snapshot(self, view: dict[str, Any], payload: Any) -> bool:
        """Idempotently merge a journal payload into a loaded view."""
        if not isinstance(view, dict) or not isinstance(payload, Mapping):
            return False
        raw_entry = payload.get("entry")
        if not isinstance(raw_entry, Mapping):
            return False
        try:
            entry = _normalize_entry(raw_entry, Path("<payload>"))
        except CorruptStoreError:
            return False
        changed = False
        existing = _find_entry(view, str(entry["correction_id"]))
        if existing is None:
            view.setdefault("corrections", []).append(entry)
            changed = True
        else:
            merged = dict(entry)
            # Replaying the write event must not downgrade propagation results
            # that were written after the event: derived invalidations only
            # grow, and a non-empty local set survives an empty payload.
            current_derived = _normalize_derived(existing.get("derived_invalidations"))
            if not _normalize_derived(entry.get("derived_invalidations"))["fact_ids"] and (
                current_derived["fact_ids"]
                or current_derived["reflection_ids"]
                or current_derived["persona_ids"]
            ):
                merged["derived_invalidations"] = current_derived
                merged["invalidated_source_keys"] = list(
                    existing.get("invalidated_source_keys") or [],
                )
                merged["replacement_fact_id"] = existing.get("replacement_fact_id")
            if existing != merged:
                view["corrections"] = [
                    merged if item is existing else item
                    for item in view.get("corrections") or []
                ]
                changed = True
        for superseded_id in payload.get("superseded") or ():
            target = _find_entry(view, str(superseded_id))
            if target is None or target.get("status") != STATUS_ACTIVE:
                continue
            target["status"] = STATUS_SUPERSEDED
            target["superseded_by"] = entry["correction_id"]
            target["superseded_at"] = entry.get("valid_from") or entry.get("created_at")
            changed = True
        if not changed:
            return False
        view["version"] = int(view.get("version") or 0) + 1
        _rotate_doc(view)
        return True

    def replay_snapshot(self, character: str, payload: Any) -> bool:
        """Journal replay handler: load → apply → save; idempotent."""
        path = self._path(character)
        with _lock_for(path):
            raw = self._store.load(path)
            view = _empty_doc() if raw is None else _normalize_doc(raw, path)
            if not self.apply_snapshot(view, payload):
                return False
            self._permit.assert_writable("replay_corrections", str(path))
            self._store.save(path, {
                "schema": CORRECTIONS_SCHEMA,
                "version": int(view.get("version") or 0),
                "corrections": list(view.get("corrections") or []),
            })
            self._versions.invalidate(path)
            return True

    def set_derived(
        self,
        character: str,
        correction_id: str,
        *,
        derived: Mapping[str, Any] | None = None,
        invalidated_source_keys: Sequence[str] | None = None,
        replacement_fact_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist propagation results for one correction (idempotent union)."""
        path = self._path(character)
        with _lock_for(path):
            doc = self._load_doc(character)
            entry = _find_entry(doc, str(correction_id))
            if entry is None:
                raise CorrectionNotFoundError(
                    f"unknown correction {correction_id!r}", path=path,
                )
            changed = False
            merged_derived = _merge_derived(
                entry.get("derived_invalidations"), derived or {},
            )
            if merged_derived != _normalize_derived(entry.get("derived_invalidations")):
                entry["derived_invalidations"] = merged_derived
                changed = True
            keys = list(entry.get("invalidated_source_keys") or [])
            for key in _normalize_id_list(invalidated_source_keys, MAX_TARGET_KEYS):
                if key not in keys:
                    keys.append(key)
                    changed = True
            if changed:
                entry["invalidated_source_keys"] = keys[:MAX_TARGET_KEYS]
            if replacement_fact_id and entry.get("replacement_fact_id") != replacement_fact_id:
                entry["replacement_fact_id"] = _bounded_text(replacement_fact_id, MAX_ID_CHARS)
                changed = True
            if changed:
                doc["version"] = int(doc.get("version") or 0) + 1
                _rotate_doc(doc)
                self._save_doc(character, doc)
            return dict(entry)

    def retract(self, character: str, correction_id: str) -> dict[str, Any]:
        """Withdraw an active correction; invalidations already applied stay."""
        path = self._path(character)
        with _lock_for(path):
            doc = self._load_doc(character)
            entry = _find_entry(doc, str(correction_id))
            if entry is None:
                raise CorrectionNotFoundError(
                    f"unknown correction {correction_id!r}", path=path,
                )
            if entry.get("status") != STATUS_ACTIVE:
                return {
                    "retracted": False,
                    "version": int(doc.get("version") or 0),
                    "replacement_fact_id": entry.get("replacement_fact_id"),
                }
            entry["status"] = STATUS_RETRACTED
            entry["retracted_at"] = _iso(self._clock)
            doc["version"] = int(doc.get("version") or 0) + 1
            self._save_doc(character, doc)
            return {
                "retracted": True,
                "version": int(doc.get("version") or 0),
                "replacement_fact_id": entry.get("replacement_fact_id"),
            }

    # -- reads -----------------------------------------------------------

    def get(self, character: str, correction_id: str) -> dict[str, Any] | None:
        doc = self.load_view(character)
        entry = _find_entry(doc, str(correction_id))
        return None if entry is None else dict(entry)

    def active(
        self,
        character: str,
        *,
        subject: Any = None,
        scene: Any = None,
        now: float | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Active, unexpired, scene/subject-matching entries, newest first."""
        doc = self.load_view(character)
        version = int(doc.get("version") or 0)
        current = self._clock.now().timestamp() if now is None else now
        items: list[dict[str, Any]] = []
        for entry in doc.get("corrections") or ():
            if not isinstance(entry, dict) or entry.get("status") != STATUS_ACTIVE:
                continue
            if _is_expired(entry, current):
                continue
            if not row_matches_subject(entry, subject):
                continue
            if not scene_applies(entry.get("applies_to"), scene):
                continue
            items.append(dict(entry))
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        if limit is not None:
            items = items[: max(0, limit)]
        return {"version": version, "items": items}

    def block(
        self,
        character: str,
        *,
        subject: Any = None,
        scene: Any = None,
        language: Any = "zh",
        limit_tokens: int = DEFAULT_BLOCK_TOKENS,
    ) -> MemoryBlock:
        """Corrections rendered as the \`{block, items, version}\` prompt block."""
        active = self.active(character, subject=subject, scene=scene)
        text = render_block(active["items"], language, limit_tokens)
        return MemoryBlock(
            block=text,
            items=tuple(active["items"]),
            version=int(active["version"]),
        )

    def all_entries(self, character: str) -> list[dict[str, Any]]:
        """Every stored correction (diagnostics/tests); copies, not live rows."""
        return [dict(entry) for entry in self.load_view(character).get("corrections") or ()]

    # -- internals -------------------------------------------------------

    def _load_doc(self, character: str) -> dict[str, Any]:
        path = self._path(character)
        raw = self._store.load(path)
        if raw is None:
            return _empty_doc()
        return _normalize_doc(raw, path)

    def _save_doc(self, character: str, doc: Mapping[str, Any]) -> None:
        path = self._path(character)
        self._permit.assert_writable("save_corrections", str(path))
        self._store.save(path, {
            "schema": CORRECTIONS_SCHEMA,
            "version": int(doc.get("version") or 0),
            "corrections": list(doc.get("corrections") or []),
        })
        self._versions.invalidate(path)


__all__ = [
    "ACTION_REPLACE",
    "ACTION_RETRACT",
    "CORRECTIONS_FILENAME",
    "CORRECTIONS_SCHEMA",
    "CorrectionStore",
    "DEFAULT_BLOCK_TOKENS",
    "KIND_FACT_CORRECTION",
    "KIND_PREFERENCE",
    "MAX_CORRECTIONS",
    "PreparedCorrection",
    "STATUS_ACTIVE",
    "STATUS_RETRACTED",
    "STATUS_SUPERSEDED",
    "fact_correction_id",
    "normalize_correction_spec",
    "normalize_language",
    "preference_correction_id",
    "render_block",
]
