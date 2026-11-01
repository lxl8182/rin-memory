"""Shared console views over an explicitly supplied core; no core assembly."""
from __future__ import annotations

import asyncio
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote

from ..persistence.recent import message_text as _message_text

APP_NAME = "铃芽"
ABBREVIATION = "RIN"
VERSION = "0.1.0"
CHARACTER = "suzume"
DISPLAY_NAME = "铃芽"

class ApiError(Exception):
    def __init__(self, code: str, status: int, message: str, *, extra: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code, self.status, self.message = code, status, message
        #: Optional body fields a caller needs in order to act on the refusal
        #: (a revision conflict reports the current revision, never a draft).
        self.extra = dict(extra or {})

# -- small helpers -----------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _scene_label(scene: Any) -> str:
    if not isinstance(scene, Mapping):
        return "host:chat"
    game_id = str(scene.get("game_id") or "")
    if game_id:
        return f"game:{game_id}"
    plugin_id = str(scene.get("plugin_id") or "")
    if plugin_id:
        return f"plugin:{plugin_id}"
    session_id = str(scene.get("session_id") or "chat")
    return f"host:{session_id}"


def _section_scope(raw: Mapping[str, Any]) -> dict[str, Any]:
    scope = raw.get("scope")
    scope = dict(scope) if isinstance(scope, Mapping) else {}
    subject = scope.get("subject")
    applies_to = scope.get("applies_to")
    if not isinstance(applies_to, Mapping) or not applies_to:
        applies_to = "all"
    return {
        "subject": subject if isinstance(subject, str) and subject else "private",
        "applies_to": applies_to,
    }


def _section_view(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "section_id": str(raw.get("section_id") or ""),
        "kind": str(raw.get("kind") or "identity"),
        "text": str(raw.get("text") or ""),
        "scope": _section_scope(raw),
        "editable_by": {
            "user": bool((raw.get("editable_by") or {}).get("user", True))
            if isinstance(raw.get("editable_by"), Mapping) else True,
            "evolution": bool((raw.get("editable_by") or {}).get("evolution", False))
            if isinstance(raw.get("editable_by"), Mapping) else False,
        },
        "frozen": bool(raw.get("frozen")),
        "origin": str(raw.get("origin") or "imported"),
        "revision": _int(raw.get("revision")),
        "updated_at": raw.get("updated_at"),
        "source_refs": [str(item) for item in raw.get("source_refs") or () if isinstance(item, str)],
        "invalidated_at": raw.get("invalidated_at"),
        "needs_rebuild": bool(raw.get("needs_rebuild")),
    }


def _fact_view(raw: Mapping[str, Any]) -> dict[str, Any]:
    confidence = _float(raw.get("confidence"), float("nan"))
    return {
        "id": str(raw.get("id") or ""),
        "text": str(raw.get("text") or ""),
        "importance": _int(raw.get("importance"), 5),
        "absorbed": bool(raw.get("absorbed")),
        "confidence": confidence if 0 <= confidence <= 1 else None,
        "source_keys": [
            str(item) for item in raw.get("source_event_keys") or ()
            if isinstance(item, str)
        ],
        "created_at": raw.get("created_at"),
        "invalidated_at": raw.get("invalidated_at"),
    }


def _revision_view(revision: Any) -> dict[str, Any]:
    operations = [dict(item) for item in getattr(revision, "operations", ()) if isinstance(item, Mapping)]
    changed: list[str] = []
    for operation in operations:
        section_id = operation.get("section_id")
        if isinstance(section_id, str) and section_id and section_id not in changed:
            changed.append(section_id)
        for section_id in operation.get("section_ids") or ():
            if isinstance(section_id, str) and section_id and section_id not in changed:
                changed.append(section_id)
    return {
        "revision": _int(getattr(revision, "revision", 0)),
        "base_revision": _int(getattr(revision, "base_revision", 0)),
        "origin": str(getattr(revision, "origin", "") or "user_edit"),
        "actor": str(getattr(revision, "actor", "") or "user"),
        "at": getattr(revision, "at", None),
        "reason": str(getattr(revision, "reason", "") or ""),
        "result": str(getattr(revision, "result", "") or "applied"),
        "operations_count": len(operations),
        "changed_sections": changed,
        "source_refs": [str(item) for item in getattr(revision, "source_refs", ()) if isinstance(item, str)],
        "operations": operations,
    }


# -- the application ---------------------------------------------------------


class RinApp:
    """One core plus the JSON views and write endpoints over it."""

    def __init__(
        self,
        *,
        core,
        mode: str,
        root: Path,
        read_only: bool,
        demo: bool,
        temporary_root: bool = False,
        character: str = CHARACTER,
        display_name: str = DISPLAY_NAME,
        close_core=None,
        reset_core=None,
        chat=None,
    ) -> None:
        self.core = core
        self.mode = mode
        self.root = root
        self.read_only = read_only
        self.demo = demo
        self.character = character
        #: Only a fallback for a card whose identity section has no name line.
        #: The authoritative name is that first line, read live by
        #: `display_name()` so every editor writes the one value.
        self._fallback_display_name = display_name
        self._close_core = close_core
        self._reset_core = reset_core
        self.temporary_root = temporary_root
        self._lock = threading.RLock()
        #: Direct console conversation (S05 C preview). `None` keeps the page's
        #: chat tab in its "not wired" state instead of inventing a reply.
        self.chat = chat

    # -- identity --------------------------------------------------------

    def display_name(self) -> str:
        """The card's name section, read live; the fallback only covers a
        card that has no name section yet."""
        try:
            name = self.core.persona_card_service.display_name(self.character)
        except Exception:  # noqa: BLE001 - an empty root has no card
            name = ""
        return name or self._fallback_display_name

    # -- lifecycle ------------------------------------------------------

    def close(self) -> None:
        # A writable live console holds the root ownership lock; every write
        # task has finished by the time the HTTP loop is down.
        if self._close_core is not None:
            self._close_core(self.core)
        if self.temporary_root:
            shutil.rmtree(self.root, ignore_errors=True)

    # -- loading --------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        core = self.core
        character = self.character
        fact_rows = core.facts.load(character)
        reflection_rows = core.reflections.load(character)
        facts = [_fact_view(row) for row in fact_rows]
        reflections_raw = [row for row in reflection_rows if not row.get("memory_kind")]
        growth_raw = [row for row in reflection_rows if row.get("memory_kind")]
        events = self._events(core, character, fact_rows, reflection_rows)
        card = core.persona_card_service.get(character)
        versions = core.persona_versions.load(character)
        proposals = core.evolution_service.list(character)
        deletion_doc = core.deletions.snapshot(character)
        corrections = core.corrections.all_entries(character)
        state = core.state_service.read(character)
        auto = core.persona_admin_service.auto_evolution(character)
        projection = core.persona_card_service.render_projection(character)
        return {
            "events": events,
            "facts": facts,
            "fact_rows": fact_rows,
            "reflections": reflections_raw,
            "growth": growth_raw,
            "reflection_rows": reflection_rows,
            "card": card,
            "versions": versions,
            "proposals": proposals,
            "deletions": deletion_doc,
            "corrections": corrections,
            "state": state,
            "auto_evolution": auto,
            "projection": projection,
        }

    def _events(
        self,
        core,
        character: str,
        fact_rows: list[Mapping[str, Any]],
        reflection_rows: list[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        barrier = getattr(core, "barrier", None)
        entries = core.journal.read_since(character, -1)
        fact_ids_by_key: dict[str, list[str]] = {}
        fact_event_keys: dict[str, list[str]] = {}
        for row in fact_rows:
            fact_id = str(row.get("id") or "")
            keys = [str(item) for item in row.get("source_event_keys") or () if isinstance(item, str)]
            fact_event_keys[fact_id] = keys
            for key in keys:
                bucket = fact_ids_by_key.setdefault(key, [])
                if fact_id and fact_id not in bucket:
                    bucket.append(fact_id)
        reflection_ids_by_key: dict[str, list[str]] = {}
        for row in reflection_rows:
            reflection_id = str(row.get("id") or "")
            if not reflection_id or row.get("memory_kind"):
                continue  # growth rows are not reflections
            keys = [str(item) for item in row.get("source_event_keys") or () if isinstance(item, str)]
            for fact_id in row.get("source_fact_ids") or ():
                keys.extend(fact_event_keys.get(str(fact_id), ()))
            for key in keys:
                bucket = reflection_ids_by_key.setdefault(key, [])
                if reflection_id not in bucket:
                    bucket.append(reflection_id)

        events: list[dict[str, Any]] = []
        for entry in entries:
            if entry.event_type != "core.source_recorded":
                continue
            payload = entry.payload
            if not isinstance(payload, Mapping):
                continue
            source_key = str(payload.get("source_key") or "")
            text = str(payload.get("text") or "")
            if self._event_hidden(barrier, character, payload, source_key, text):
                continue
            event_id = payload.get("event_id") or entry.event_id
            events.append({
                "id": str(event_id),
                "event_id": str(event_id),
                "text": text,
                "kind": str(payload.get("event_type") or ""),
                "occurred_at": str(payload.get("occurred_at") or ""),
                "scene": _scene_label(payload.get("scene")),
                "source_key": source_key,
                "subject": str((payload.get("scope") or {}).get("subject") or "private")
                if isinstance(payload.get("scope"), Mapping) else "private",
                "content_class": payload.get("content_class") or "",
                "episode_id": payload.get("episode_id"),
                "received_at": payload.get("received_at"),
                "fact_ids": list(fact_ids_by_key.get(source_key, [])),
                "reflection_ids": list(reflection_ids_by_key.get(source_key, [])),
            })
        events.sort(key=lambda item: str(item.get("occurred_at") or ""), reverse=True)
        return events

    @staticmethod
    def _event_hidden(
        barrier, character: str, payload: Mapping[str, Any],
        source_key: str, text: str,
    ) -> bool:
        if barrier is None:
            return False
        try:
            if source_key and barrier.blocks_source_key(character, source_key):
                return True
            episode_id = payload.get("episode_id")
            if isinstance(episode_id, str) and episode_id and barrier.blocks_episode(character, episode_id):
                return True
            if text and barrier.blocks_text(character, text):
                return True
        except Exception:  # noqa: BLE001 - a broken barrier must not invent events
            return False
        return False

    # -- views ----------------------------------------------------------

    def _reflection_view(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        evidence = [
            {
                "source_key": str(item.get("source_key") or ""),
                "weight": _float(item.get("weight"), 1.0),
            }
            for item in raw.get("evidence") or ()
            if isinstance(item, Mapping)
        ]
        if not evidence:
            evidence = [
                {"source_key": str(key), "weight": 1.0}
                for key in raw.get("source_event_keys") or ()
                if isinstance(key, str)
            ]
        strength = raw.get("evidence_strength")
        if not isinstance(strength, (int, float)) or isinstance(strength, bool):
            try:
                strength = self.core.reflection_service.evidence_score(self.character, raw)
            except Exception:  # noqa: BLE001 - fall back to the stored value
                strength = _float(raw.get("reinforcement"), 0.0) - _float(raw.get("disputation"), 0.0)
        if not isinstance(strength, (int, float)) or strength != strength:
            strength = 0.0
        if strength == float("inf"):
            strength = 3.0
        persona_ids = [
            str(item) for item in raw.get("persona_ids") or ()
            if isinstance(item, str)
        ]
        absorbed_into = raw.get("absorbed_into")
        if isinstance(absorbed_into, str) and absorbed_into and absorbed_into not in persona_ids:
            persona_ids.append(absorbed_into)
        needs_rebuild = bool(raw.get("needs_rebuild"))
        store_status = str(raw.get("status") or "")
        if needs_rebuild:
            status = "needs_rebuild"
        elif store_status == "archived":
            status = "archived"
        else:
            status = "active"
        return {
            "id": str(raw.get("id") or ""),
            "text": str(raw.get("text") or ""),
            "status": status,
            "store_status": store_status,
            "entity": raw.get("entity"),
            "evidence_strength": round(float(strength), 2),
            "evidence_count": len(evidence),
            "half_life_days": 30,
            "created_at": raw.get("created_at"),
            "promoted": store_status == "promoted" or bool(absorbed_into),
            "persona_ids": persona_ids,
            "needs_rebuild": needs_rebuild,
            "invalidated_at": raw.get("invalidated_at"),
            "evidence": evidence,
            "source_fact_ids": [str(item) for item in raw.get("source_fact_ids") or () if isinstance(item, str)],
            "reinforcement": _float(raw.get("reinforcement"), 0.0),
        }

    def _growth_view(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        store_status = str(raw.get("status") or "")
        status = "active" if store_status in ("pending", "confirmed", "promoted") else store_status
        source_refs = [
            str(item) for item in raw.get("source_event_keys") or ()
            if isinstance(item, str)
        ] or [
            str(item) for item in raw.get("support_event_keys") or ()
            if isinstance(item, str)
        ]
        return {
            "id": str(raw.get("id") or ""),
            "text": str(raw.get("text") or raw.get("claim") or ""),
            "claim": raw.get("claim"),
            "support_count": _int(raw.get("support_count")),
            "status": status,
            "store_status": store_status,
            "memory_kind": raw.get("memory_kind"),
            "created_at": raw.get("created_at"),
            "source_refs": source_refs,
        }

    def _meta(self, load: Mapping[str, Any]) -> dict[str, Any]:
        card = load["card"]
        versions = load["versions"]
        proposals = load["proposals"]
        deletion_doc = load["deletions"]
        return {
            "name": APP_NAME,
            "abbreviation": ABBREVIATION,
            "version": VERSION,
            "character": self.character,
            "display_name": self.display_name(),
            "mode": self.mode,
            "memory_root": str(self.root),
            "read_only": bool(self.read_only),
            "generated_at": _now_iso(),
            "counts": {
                "events": len(load["events"]),
                "facts": len(load["facts"]),
                "reflections": len(load["reflections"]),
                "growth": len(load["growth"]),
                "revisions": len(card.revisions),
                "versions": len(versions.get("published") or []),
                "proposals": len(proposals),
                "deletions": len(deletion_doc.get("deletions") or []),
                "corrections": len(load["corrections"]),
            },
        }

    def _persona(self, load: Mapping[str, Any]) -> dict[str, Any]:
        card = load["card"]
        versions = load["versions"]
        current = versions.get("current")
        current_view: dict[str, Any] | None = None
        if isinstance(current, Mapping):
            current_view = {
                "available": True,
                "version": str(current.get("version") or ""),
                "revision": _int(current.get("revision")),
                "target": str(current.get("target") or ""),
                "published_at": current.get("published_at"),
                "boundary": dict(current.get("boundary") or {})
                if isinstance(current.get("boundary"), Mapping) else {},
                "text_sha256": str(current.get("text_sha256") or ""),
            }
        history = [
            {
                "version": str(row.get("version") or ""),
                "revision": _int(row.get("revision")),
                "target": str(row.get("target") or ""),
                "published_at": row.get("published_at"),
            }
            for row in versions.get("published") or ()
            if isinstance(row, Mapping)
        ]
        history.sort(key=lambda row: str(row.get("published_at") or ""), reverse=True)
        projection_status = self._projection_status()
        return {
            "card": {
                "character": card.character,
                "revision": card.revision,
                "initial_revision": card.initial_revision,
                "sections": [_section_view(section.to_mapping()) for section in card.sections],
                "initial_sections": [
                    _section_view(section.to_mapping())
                    for section in card.initial_sections
                ],
            },
            "revisions": [_revision_view(revision) for revision in reversed(card.revisions)],
            "versions": {"current": current_view, "history": history},
            "projection": dict(load["projection"]),
            "consumers": dict(projection_status.get("consumers") or {}),
            "pending": list(projection_status.get("pending") or []),
            "failed": list(projection_status.get("failed") or []),
            "stale": bool(projection_status.get("stale")),
            "published": bool(projection_status.get("published")),
        }

    def _evolution(self, load: Mapping[str, Any]) -> dict[str, Any]:
        proposals = []
        bases = {}
        for row in load["proposals"]:
            if not isinstance(row, Mapping):
                continue
            base_revision = _int(row.get("base_revision"))
            if base_revision not in bases:
                bases[base_revision] = self.core.persona_admin_service.revision_sections(self.character, base_revision)
            base = bases[base_revision]
            sections = {section["section_id"]: section for section in base or ()}
            changes = []
            for operation in row.get("operations") or ():
                section_id = operation["section_id"]
                section = sections.get(section_id, {})
                available = base is not None and (operation["op"] == "add" or section_id in sections)
                before = str(section.get("text") or "") if available else None
                after = str(operation.get("text") or "") if operation["op"] != "delete" else ""
                changes.append({"section_id": section_id, "op": operation["op"],
                    "before": before, "after": after, "before_available": available,
                    "noop": available and operation["op"] == "replace" and before == after
                    and all(operation[key] == section.get(key) for key in ("kind", "scope", "editable_by") if key in operation)})
            proposals.append({
                "proposal_id": str(row.get("proposal_id") or ""),
                "status": str(row.get("status") or ""),
                "base_revision": _int(row.get("base_revision")),
                "accepted_revision": _int(row.get("accepted_revision")),
                "changes": changes,
                "reason": str(row.get("reason") or ""),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "origin": "evolution",
                "operations": [
                    dict(item) for item in row.get("operations") or ()
                    if isinstance(item, Mapping)
                ],
                "evidence": dict(row.get("evidence") or {})
                if isinstance(row.get("evidence"), Mapping) else {},
                "validation": dict(row.get("validation") or {})
                if isinstance(row.get("validation"), Mapping) else {},
                "model": dict(row.get("model") or {})
                if isinstance(row.get("model"), Mapping) else {},
                "request_id": row.get("request_id"),
            })
        proposals.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        auto = load["auto_evolution"]
        return {
            "enabled": bool(auto.enabled),
            "mode": str(getattr(auto, "mode", "off") or "off"),
            "modes": ["off", "propose", "auto"],
            "auto_evolution": auto.to_mapping(),
            "proposals": proposals,
        }

    def _cleanup(self, load: Mapping[str, Any]) -> dict[str, Any]:
        document = load["deletions"]
        deletions = []
        for row in document.get("deletions") or ():
            if not isinstance(row, Mapping):
                continue
            targets = [dict(item) for item in row.get("targets") or () if isinstance(item, Mapping)]
            resolved = [dict(item) for item in row.get("resolved_targets") or () if isinstance(item, Mapping)]
            source_keys = [str(item) for item in row.get("source_keys") or () if isinstance(item, str)]
            episode_ids = [str(item) for item in row.get("episode_ids") or () if isinstance(item, str)]
            first = (targets or resolved or [{}])[0]
            target_type = str(first.get("type") or ("source_key" if source_keys else "episode"))
            target_id = str(
                first.get("id")
                or (source_keys[0] if source_keys else "")
                or (episode_ids[0] if episode_ids else "")
            )
            status = str(row.get("status") or "pending")
            deletions.append({
                "id": str(row.get("deletion_id") or ""),
                "status": status,
                "target_type": target_type,
                "target_id": target_id,
                "source_keys": source_keys,
                "episode_ids": episode_ids,
                "reason": str(row.get("reason") or ""),
                "created_at": row.get("created_at"),
                "completed_at": row.get("updated_at") if status == "completed" else None,
                "deletion_id": str(row.get("deletion_id") or ""),
                "plugin_receipts": dict(row.get("plugin_receipts") or {}),
                "steps": {
                    str(name): str((step or {}).get("status") or "")
                    for name, step in (row.get("steps") or {}).items()
                    if isinstance(step, Mapping)
                },
            })
        corrections = []
        for row in load["corrections"]:
            if not isinstance(row, Mapping):
                continue
            derived = row.get("derived_invalidations")
            derived = derived if isinstance(derived, Mapping) else {}
            store_status = str(row.get("status") or "active")
            corrections.append({
                "id": str(row.get("correction_id") or ""),
                "old_text": str(row.get("replaces_text") or ""),
                "new_text": str(row.get("text") or ""),
                "status": "completed" if store_status == "active" else store_status,
                "store_status": store_status,
                "kind": row.get("kind"),
                "created_at": row.get("created_at"),
                "invalidated_fact_ids": [
                    str(item) for item in derived.get("fact_ids") or ()
                    if isinstance(item, str)
                ],
                "invalidated_source_keys": [
                    str(item) for item in row.get("invalidated_source_keys") or ()
                    if isinstance(item, str)
                ],
            })
        tombstones = []
        version = _int(document.get("version"))
        for index, row in enumerate(document.get("tombstones") or ()):
            if not isinstance(row, Mapping):
                continue
            target_type = str(row.get("type") or "")
            target_id = str(
                row.get("id") or row.get("episode_id") or row.get("text_hash") or ""
            )
            deletion_id = str(row.get("deletion_id") or "")
            tombstones.append({
                "id": f"{deletion_id or 'tomb'}:{target_type}:{index}",
                "target_type": target_type,
                "target_id": target_id,
                "version": version,
                "deletion_id": deletion_id,
                "at": row.get("at"),
            })
        view = {
            "deletions": deletions,
            "corrections": corrections,
            "tombstones": tombstones,
        }
        try:
            result = self.core.deletion_service.deletable_entries(
                self.character, limit=40,
            )
            view["deletable"] = list(result.get("items") or [])
        except Exception:  # noqa: BLE001 - cleanup stays readable without it
            view["deletable"] = []
        view["deletable_total"] = len(view["deletable"])
        return view

    def _state(self, load: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = load["state"]
        state = snapshot.state if isinstance(snapshot.state, Mapping) else {}
        moods = list(state.get("moods") or ())
        mood: dict[str, Any] | None = None
        if moods and isinstance(moods[0], Mapping):
            top = moods[0]
            mood = {
                "label": str(top.get("label") or ""),
                "intensity": _float(top.get("intensity"), 0.0),
                "effective_intensity": _float(top.get("effective_intensity"), 0.0),
                "cause": str(top.get("cause") or ""),
            }
        energy = 0.72
        if mood is not None:
            energy = round(0.5 + mood["effective_intensity"] / 2.0, 2)
        return {
            "state": {
                "version": snapshot.version,
                "character": self.character,
                "subject": snapshot.subject,
                "updated_at": snapshot.updated_at,
                "mood": mood or {
                    "label": "平静", "intensity": 0.7,
                    "effective_intensity": 0.7, "cause": "",
                },
                "energy": energy,
                "thoughts": list(state.get("thoughts") or ()),
                "moods": moods,
                "concerns": list(state.get("concerns") or ()),
                "progress": list(state.get("progress") or ()),
                "state": dict(state),
                "items": [dict(item) for item in snapshot.items],
                "block": snapshot.block,
            },
            "lease": dict(snapshot.lease) if isinstance(snapshot.lease, Mapping) else None,
        }

    def _overview(self, load: Mapping[str, Any]) -> dict[str, Any]:
        events = load["events"]
        facts = load["facts"]
        reflections = load["reflections"]
        growth = load["growth"]
        card = load["card"]
        versions = load["versions"]
        proposals = load["proposals"]
        dates: dict[str, int] = {}
        for event in events:
            occurred = _parse_iso(event.get("occurred_at"))
            if occurred is None:
                continue
            key = occurred.astimezone(timezone.utc).date().isoformat()
            dates[key] = dates.get(key, 0) + 1
        today = datetime.now(timezone.utc).date()
        streak = 0
        cursor = today
        while cursor.isoformat() in dates:
            streak += 1
            cursor -= timedelta(days=1)
        activity = []
        for offset in range(20, -1, -1):
            day = (today - timedelta(days=offset)).isoformat()
            activity.append({"date": day, "count": dates.get(day, 0)})
        state_view = self._state(load)["state"]
        mood = _float((state_view.get("mood") or {}).get("effective_intensity"), 0.7)
        current = versions.get("current")
        current = current if isinstance(current, Mapping) else {}
        latest_quote = {"text": "", "at": ""}
        for event in events:
            if event.get("kind") == "user_message":
                latest_quote = {
                    "text": str(event.get("text") or ""),
                    "at": str(event.get("occurred_at") or ""),
                }
                break
        return {
            "stats": {
                "events": len(events),
                "facts": len(facts),
                "reflections": len(reflections),
                "growth": len(growth),
                "active_days": len(dates),
                "streak": streak,
                "mood": round(mood, 2),
                "temperature": 0,
                "persona_revision": card.revision,
                "version": str(current.get("version") or ""),
                "pending_proposals": sum(
                    1 for row in proposals
                    if isinstance(row, Mapping) and row.get("status") == "proposed"
                ),
            },
            "recent_events": events[:8],
            "activity": activity,
            "persona": {
                "revision": card.revision,
                "sections": [
                    {"section_id": section.section_id, "text": section.text}
                    for section in card.sections
                ],
            },
            "version": {
                "version": str(current.get("version") or ""),
                "revision": _int(current.get("revision")),
                "target": str(current.get("target") or ""),
                "published_at": current.get("published_at") or "",
            },
            "latest_quote": latest_quote,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            load = self._load()
            return {
                "meta": self._meta(load),
                "overview": self._overview(load),
                "timeline": {"events": load["events"], "total": len(load["events"])},
                "facts": {"facts": load["facts"], "total": len(load["facts"])},
                "reflections": {
                    "reflections": [self._reflection_view(row) for row in load["reflections"]],
                    "growth": [self._growth_view(row) for row in load["growth"]],
                },
                "persona": self._persona(load),
                "evolution": self._evolution(load),
                "cleanup": self._cleanup(load),
                "state": self._state(load),
            }

    # -- read endpoints --------------------------------------------------

    def meta(self) -> dict[str, Any]:
        with self._lock:
            return self._meta(self._load())

    def overview(self) -> dict[str, Any]:
        with self._lock:
            return self._overview(self._load())

    def timeline(self, query: Mapping[str, list[str]]) -> dict[str, Any]:
        with self._lock:
            load = self._load()
            limit = max(1, min(500, _int((query.get("limit") or ["200"])[0], 200)))
            needle = (query.get("q") or [""])[0].strip().lower()
            scene = (query.get("scene") or [""])[0].strip().lower()
            events = load["events"]
            if needle:
                events = [event for event in events if needle in str(event.get("text") or "").lower()]
            if scene:
                events = [
                    event for event in events
                    if scene in str(event.get("scene") or "").lower()
                ]
            return {"events": events[:limit], "total": len(events)}

    def event(self, event_id: str) -> dict[str, Any]:
        with self._lock:
            load = self._load()
            target = None
            for event in load["events"]:
                if event_id in (event.get("id"), event.get("event_id"), event.get("source_key")):
                    target = event
                    break
            if target is None:
                raise ApiError("not_found", 404, f"unknown event {event_id!r}")
            fact_by_id = {fact["id"]: fact for fact in load["facts"]}
            reflection_by_id = {
                str(row.get("id")): self._reflection_view(row)
                for row in load["reflections"]
            }
            return {
                "event": target,
                "facts": [
                    fact_by_id[fact_id] for fact_id in target["fact_ids"]
                    if fact_id in fact_by_id
                ],
                "reflections": [
                    reflection_by_id[reflection_id]
                    for reflection_id in target["reflection_ids"]
                    if reflection_id in reflection_by_id
                ],
            }

    def facts(self, query: Mapping[str, list[str]]) -> dict[str, Any]:
        with self._lock:
            load = self._load()
            needle = (query.get("q") or [""])[0].strip().lower()
            rows = load["facts"]
            if needle:
                rows = [row for row in rows if needle in str(row.get("text") or "").lower()]
            return {"facts": rows, "total": len(rows)}

    def reflections(self) -> dict[str, Any]:
        with self._lock:
            load = self._load()
            return {
                "reflections": [self._reflection_view(row) for row in load["reflections"]],
                "growth": [self._growth_view(row) for row in load["growth"]],
            }

    def persona(self) -> dict[str, Any]:
        with self._lock:
            return self._persona(self._load())

    def evolution(self) -> dict[str, Any]:
        with self._lock:
            return self._evolution(self._load())

    def cleanup(self, query: Mapping[str, list[str]] | None = None) -> dict[str, Any]:
        with self._lock:
            view = self._cleanup(self._load())
            query = query or {}
            q = (query.get("q") or [""])[0].strip()
            try:
                result = self.core.deletion_service.deletable_entries(
                    self.character, query=q, limit=40,
                )
                view["deletable"] = list(result.get("items") or [])
            except Exception:  # noqa: BLE001 - cleanup stays readable without it
                view["deletable"] = []
            view["deletable_total"] = len(view["deletable"])
            return view

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._state(self._load())

    def search(self, query: Mapping[str, list[str]]) -> dict[str, Any]:
        with self._lock:
            load = self._load()
            needle = (query.get("q") or [""])[0].strip().lower()
            if not needle:
                return {"events": [], "facts": [], "reflections": [], "sections": []}
            events = [
                event for event in load["events"]
                if needle in str(event.get("text") or "").lower()
            ][:30]
            facts = [
                fact for fact in load["facts"]
                if needle in str(fact.get("text") or "").lower()
            ][:30]
            reflections = [
                view for view in (
                    self._reflection_view(row) for row in load["reflections"]
                )
                if needle in str(view.get("text") or "").lower()
            ][:30]
            sections = [
                {"section_id": section.section_id, "text": section.text}
                for section in load["card"].sections
                if needle in str(section.text or "").lower()
            ][:30]
            return {
                "events": events,
                "facts": facts,
                "reflections": reflections,
                "sections": sections,
            }

    def export(self) -> tuple[dict[str, Any], str]:
        with self._lock:
            snapshot = self.snapshot()
            exported_at = _now_iso()
            filename = "rin-memory-{}.json".format(
                datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
            )
            return {
                "exported_at": exported_at,
                "meta": snapshot["meta"],
                "snapshot": snapshot,
            }, filename

    # -- write endpoints -------------------------------------------------

    def _projection_status(self) -> dict[str, Any]:
        if self.mode == "service":
            current = self.core.persona_publish.current(self.character)
            document = self.core.persona_versions.load(self.character)
            saved = document.get("consumers") or {}
            consumers = {}
            for name in dict.fromkeys(("chat", "active", "realtime", "game", *saved)):
                row = dict(saved.get(name) or {})
                adopted = current.available and row.get("version") == current.version and row.get("status") == "active"
                if name == "chat":
                    # The console conversation runs in this process and reads
                    # the current published card on every send, so an older
                    # recorded version is not something anyone has to adopt:
                    # it is what the next message will use anyway. Reporting it
                    # as pending raised an alert the user could not clear.
                    status = "adopted" if adopted else "inline"
                else:
                    status = ("not_connected" if not row else "adopted" if adopted
                              else "failed" if row.get("status") == "failed" else "pending")
                consumers[name] = dict(row, status=status, version=row.get("version") or "")
            return {"consumers": consumers, "published": current.available,
                    "stale": self.core.persona_card_service.get(self.character).revision != current.revision,
                    "pending": [name for name,row in consumers.items()
                                if row["status"] == "pending" and name != "chat"],
                    "failed": [name for name,row in consumers.items() if row["status"] == "failed"]}
        service = getattr(self.core, "persona_projection_service", None)
        if service is None:
            return {}
        try:
            return service.status(self.character)
        except Exception:  # noqa: BLE001 - projection is optional for older roots
            return {}

    def _engine_status(self) -> dict[str, Any]:
        card = self.core.persona_card_service.get(self.character)
        versions = self.core.persona_versions.load(self.character)
        current = versions.get("current")
        current = current if isinstance(current, Mapping) else {}
        auto = self.core.persona_admin_service.auto_evolution(self.character)
        projection = self._projection_status()
        return {
            "ok": True,
            "mode": self.mode,
            "read_only": bool(self.read_only),
            "demo": bool(self.demo),
            "root": str(self.root),
            "character": self.character,
            "card_revision": int(getattr(card, "revision", 0) or 0),
            "published_version": str(current.get("version") or ""),
            "evolution_mode": str(getattr(auto, "mode", "off") or "off"),
            "consumers": dict(projection.get("consumers") or {}),
            "pending": list(projection.get("pending") or []),
            "failed": list(projection.get("failed") or []),
            "stale": bool(projection.get("stale")),
            "generated_at": _now_iso(),
        }

    def _assert_writable(self) -> None:
        if self.read_only:
            raise ApiError(
                "read_only", 403,
                "this memory root is read-only; start the server with --allow-write",
            )

    def _card_view(self) -> dict[str, Any]:
        card = self.core.persona_card_service.get(self.character)
        return {
            "character": card.character,
            "revision": card.revision,
            "initial_revision": card.initial_revision,
            "sections": [_section_view(section.to_mapping()) for section in card.sections],
            "initial_sections": [
                _section_view(section.to_mapping()) for section in card.initial_sections
            ],
        }

    @staticmethod
    def _revision_receipt(receipt) -> dict[str, Any]:
        card = receipt.card
        return {
            "status": receipt.status,
            "revision": receipt.revision,
            "base_revision": receipt.base_revision,
            "applied_ops": receipt.applied_ops,
            "reason": receipt.reason,
            "conflict": bool(receipt.conflict),
            "duplicate": bool(receipt.duplicate),
            "changed_sections": list(receipt.changed_sections),
            "card": None if card is None else {
                "character": card.character,
                "revision": card.revision,
                "initial_revision": card.initial_revision,
                "sections": [_section_view(section.to_mapping()) for section in card.sections],
            },
        }

    def revise(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._assert_writable()
        base_revision = payload.get("base_revision")
        if isinstance(base_revision, bool) or not isinstance(base_revision, int):
            raise ApiError("invalid_request", 400, "base_revision must be an integer")
        operations = payload.get("operations")
        if not isinstance(operations, list) or not operations:
            raise ApiError("invalid_request", 400, "operations must be a non-empty list")
        for operation in operations:
            if not isinstance(operation, Mapping) or not operation.get("op") or not operation.get("section_id"):
                raise ApiError("invalid_request", 400, "each operation needs op and section_id")
        with self._lock:
            receipt = self.core.persona_card_service.revise(self.character, {
                "base_revision": base_revision,
                "operations": [dict(item) for item in operations],
                "reason": str(payload.get("reason") or ""),
                "actor": "user",
                "origin": "user_edit",
            })
            view = self._revision_receipt(receipt)
        if view.get("conflict"):
            # Only the service's current card travels back with a conflict; the
            # caller keeps its own draft and decides whether to rebase.
            current = view.get("card") or self._card_view()
            raise ApiError(
                "revision_conflict", 409,
                "base_revision changed; keep the draft and refresh",
                extra={"current": current},
            )
        return view

    def freeze(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._assert_writable()
        section_id = payload.get("section_id")
        if not isinstance(section_id, str) or not section_id.strip():
            raise ApiError("invalid_request", 400, "section_id is required")
        frozen = payload.get("frozen", True)
        if not isinstance(frozen, bool):
            raise ApiError("invalid_request", 400, "frozen must be a boolean")
        with self._lock:
            receipt = self.core.persona_card_service.freeze(
                self.character, section_id.strip(), frozen,
            )
            return self._revision_receipt(receipt)

    def restore(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        del payload
        self._assert_writable()
        with self._lock:
            receipt = self.core.persona_card_service.restore_initial(self.character)
            return self._revision_receipt(receipt)

    @staticmethod
    def _publish_receipt(receipt) -> dict[str, Any]:
        return {
            "status": receipt.status,
            "version": receipt.version,
            "revision": receipt.revision,
            "target": receipt.target,
            "published_at": receipt.published_at,
            "boundary": dict(receipt.boundary or {}),
            "conflict": bool(receipt.conflict),
            "duplicate": bool(receipt.duplicate),
            "reason": receipt.reason,
        }

    def publish(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._assert_writable()
        target = payload.get("target", "next_turn")
        if target not in ("next_turn", "realtime", "game"):
            raise ApiError("invalid_request", 400, "target must be next_turn, realtime or game")
        revision = payload.get("revision")
        if revision is not None and (
            isinstance(revision, bool) or not isinstance(revision, int)
        ):
            raise ApiError("invalid_request", 400, "revision must be an integer")
        with self._lock:
            receipt = self.core.persona_publish.publish(
                self.character, target=target, revision=revision,
            )
            return self._publish_receipt(receipt)

    def rollback(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._assert_writable()
        version = payload.get("version")
        if not isinstance(version, str) or not version.strip():
            raise ApiError("invalid_request", 400, "version is required")
        allow_consumed = payload.get("allow_consumed", False)
        if not isinstance(allow_consumed, bool):
            raise ApiError("invalid_request", 400, "allow_consumed must be a boolean")
        with self._lock:
            receipt = self.core.persona_publish.rollback(
                self.character, version.strip(), allow_consumed=allow_consumed,
            )
            return self._publish_receipt(receipt)

    def evolution_settings(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._assert_writable()
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise ApiError("invalid_request", 400, "enabled must be a boolean")
        with self._lock:
            settings = self.core.persona_admin_service.set_auto_evolution(
                self.character, enabled,
            )
            return settings.to_mapping()

    def evolution_mode(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._assert_writable()
        mode = payload.get("mode")
        if mode not in ("off", "propose", "auto"):
            raise ApiError("invalid_request", 400, "mode must be off, propose or auto")
        with self._lock:
            settings = self.core.persona_admin_service.set_mode(
                self.character, mode,
            )
            return {
                "status": "applied",
                "mode": settings.mode,
                "enabled": bool(settings.enabled),
                "auto_evolution": settings.to_mapping(),
            }

    def evolution_evaluate(self) -> dict[str, Any]:
        self._assert_writable()
        scheduler = getattr(self.core, "evolution_scheduler", None)
        if scheduler is None:
            raise ApiError("not_configured", 501, "evolution scheduler is not wired")
        with self._lock:
            result = asyncio.run(scheduler.evaluate(self.character))
            return {
                "status": result.status,
                "reason": result.reason,
                "run": result.run,
                "proposal_id": result.proposal_id,
                "published_version": result.published_version,
                "pending": scheduler.pending(self.character),
            }

    def evolution_retry(self) -> dict[str, Any]:
        self._assert_writable()
        scheduler = getattr(self.core, "evolution_scheduler", None)
        if scheduler is None:
            raise ApiError("not_configured", 501, "evolution scheduler is not wired")
        with self._lock:
            retried = asyncio.run(scheduler.retry_pending(self.character))
            return {
                "status": "retried" if retried else "idle",
                "retried": retried,
                "run": None,
                "pending": scheduler.pending(self.character),
            }

    @staticmethod
    def _adopt_view(receipt) -> dict[str, Any]:
        return {
            "status": receipt.status,
            "consumer": receipt.consumer,
            "expected_version": receipt.expected_version,
            "version": receipt.version,
            "revision": receipt.revision,
            "projection": dict(receipt.projection or {}),
            "text": receipt.text,
            "needs_reissue": bool(receipt.needs_reissue),
            "reason": receipt.reason,
            "consumer_state": None if receipt.consumer_state is None
            else dict(receipt.consumer_state),
        }

    def persona_adopt(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self.mode == "service":
            raise ApiError("invalid_request", 400, "adoption is recorded by a committed generation usage receipt")
        self._assert_writable()
        consumer = payload.get("consumer")
        version = payload.get("version")
        receipt_id = payload.get("receipt_id")
        if not isinstance(consumer, str) or not consumer.strip():
            raise ApiError("invalid_request", 400, "consumer is required")
        if not isinstance(version, str) or not version.strip():
            raise ApiError("invalid_request", 400, "version is required")
        if receipt_id is not None and not isinstance(receipt_id, str):
            raise ApiError("invalid_request", 400, "receipt_id must be a string")
        service = getattr(self.core, "persona_projection_service", None)
        if service is None:
            raise ApiError("not_configured", 501, "persona projection is not wired")
        with self._lock:
            receipt = service.adopt(
                self.character, consumer.strip(), version.strip(),
                receipt_id=receipt_id,
                status=str(payload.get("status") or "adopted"),
            )
            return self._adopt_view(receipt)

    def persona_sync(self) -> dict[str, Any]:
        self._assert_writable()
        service = getattr(self.core, "persona_projection_service", None)
        if service is None:
            raise ApiError("not_configured", 501, "persona projection is not wired")
        with self._lock:
            synced = bool(service.sync_from_card(self.character))
            return {
                "status": "synced" if synced else "unchanged",
                "synced": synced,
                "projection": self._projection_status(),
            }

    @staticmethod
    def _deletion_receipt_view(receipt) -> dict[str, Any]:
        return {
            "deletion_id": receipt.deletion_id,
            "version": receipt.version,
            "scope": receipt.scope,
            "status": receipt.status,
            "stopped_using": bool(receipt.stopped_using),
            "content_cleaned": bool(receipt.content_cleaned),
            "targets": [dict(item) for item in receipt.targets],
            "resolved_targets": [dict(item) for item in receipt.resolved_targets],
            "episode_ids": list(receipt.episode_ids),
            "source_keys": list(receipt.source_keys),
            "completed": list(receipt.completed),
            "pending": list(receipt.pending),
            "failed": list(receipt.failed),
            "created_at": receipt.created_at,
            "updated_at": receipt.updated_at,
            "steps": {
                str(name): dict(step)
                for name, step in (receipt.steps or {}).items()
            },
            "plugin_receipts": {
                str(plugin): dict(value)
                for plugin, value in (receipt.plugin_receipts or {}).items()
            },
            "duplicate": bool(receipt.duplicate),
            "request_id": receipt.request_id,
        }

    def cleanup_delete(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._assert_writable()
        from rin_memory.contracts import (
            DeletionRequest,
            DeletionTarget,
            Scope,
        )

        target_type = payload.get("type")
        target_id = payload.get("id")
        source_key = payload.get("source_key")
        text = payload.get("text")
        reason = payload.get("reason")
        request_id = payload.get("request_id")
        if target_type is not None and (
            not isinstance(target_type, str)
            or target_type not in ("fact", "reflection", "persona", "episode")
        ):
            raise ApiError("invalid_request", 400, "unsupported target type")
        for name, value in (
            ("id", target_id), ("source_key", source_key),
            ("text", text), ("reason", reason), ("request_id", request_id),
        ):
            if value is not None and not isinstance(value, str):
                raise ApiError("invalid_request", 400, f"{name} must be a string")
        if target_type and not target_id:
            raise ApiError("invalid_request", 400, "id is required with type")

        deletable: list[dict[str, Any]] = []
        try:
            result = self.core.deletion_service.deletable_entries(
                self.character, limit=64,
            )
            deletable = [dict(item) for item in result.get("items") or ()]
        except Exception:  # noqa: BLE001 - lookup is best-effort
            deletable = []

        targets: list[Any] = []
        episode_ids: list[str] = []
        if target_id:
            resolved_type = target_type
            if not resolved_type:
                for item in deletable:
                    if item.get("id") == target_id:
                        resolved_type = str(item.get("type") or "")
                        break
                resolved_type = resolved_type or "fact"
            if resolved_type == "episode":
                episode_ids.append(target_id)
            else:
                targets.append(DeletionTarget(resolved_type, target_id))
        elif text:
            needle = text.strip().lower()
            for item in deletable:
                item_text = str(item.get("text") or "").lower()
                item_type = str(item.get("type") or "")
                if needle and needle in item_text and item_type in (
                    "fact", "reflection", "persona",
                ):
                    targets.append(DeletionTarget(item_type, str(item.get("id"))))
                    if len(targets) >= 64:
                        break
            if not targets:
                raise ApiError(
                    "not_found", 404,
                    "no deletable entry matched the given text",
                )
        source_keys: list[str] = [source_key] if source_key else []
        if not (targets or source_keys or episode_ids):
            raise ApiError(
                "invalid_request", 400,
                "one of type+id, id, source_key or text is required",
            )
        request = DeletionRequest(
            scope=Scope(self.character),
            mode="episode" if episode_ids else "entry",
            targets=tuple(targets),
            source_keys=tuple(source_keys),
            episode_ids=tuple(episode_ids),
            reason=(reason or "").strip(),
            request_id=(request_id or None),
        )
        with self._lock:
            receipt = asyncio.run(self.core.deletion_service.forget(request))
            return self._deletion_receipt_view(receipt)

    def evolution_propose(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._assert_writable()
        model = getattr(self.core.evolution_service, "_model", None)
        if not self.demo and model is None:
            raise ApiError(
                "model_unavailable", 501,
                "live mode has no model configured; proposals are demo-only",
            )
        base_revision = payload.get("base_revision")
        if isinstance(base_revision, bool) or not isinstance(base_revision, int):
            raise ApiError("invalid_request", 400, "base_revision must be an integer")
        source_refs = payload.get("source_refs") or []
        focus_sections = payload.get("focus_sections") or []
        if not isinstance(source_refs, list) or not all(isinstance(item, str) for item in source_refs):
            raise ApiError("invalid_request", 400, "source_refs must be a list of strings")
        if not isinstance(focus_sections, list) or not all(isinstance(item, str) for item in focus_sections):
            raise ApiError("invalid_request", 400, "focus_sections must be a list of strings")
        request_id = payload.get("request_id")
        if request_id is not None and not isinstance(request_id, str):
            raise ApiError("invalid_request", 400, "request_id must be a string")
        with self._lock:
            receipt = asyncio.run(self.core.evolution_service.propose(
                self.character,
                {
                    "base_revision": base_revision,
                    "source_refs": list(source_refs),
                    "focus_sections": list(focus_sections),
                    "reason": str(payload.get("reason") or ""),
                    "request_id": request_id or "",
                },
            ))
            return {
                "status": receipt.status,
                "proposal_id": receipt.proposal_id,
                "reason": receipt.reason,
                "validation": dict(receipt.validation or {}),
                "proposal": receipt.proposal,
            }

    @staticmethod
    def _decision_receipt(receipt) -> dict[str, Any]:        return {
            "status": receipt.status,
            "proposal_id": receipt.proposal_id,
            "action": receipt.action,
            "decision_id": receipt.decision_id,
            "revision_before": receipt.revision_before,
            "revision_after": receipt.revision_after,
            "reason": receipt.reason,
            "conflict": bool(receipt.conflict),
            "duplicate": bool(receipt.duplicate),
        }

    def _decide(self, action: str, proposal_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._assert_writable()
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ApiError("invalid_request", 400, "reason must be a string")
        with self._lock:
            service = self.core.persona_admin_service
            if action == "accept":
                receipt = service.accept_proposal(
                    self.character, proposal_id, reason=reason or "",
                )
            elif action == "reject":
                receipt = service.reject_proposal(
                    self.character, proposal_id, reason=reason or "",
                )
            else:
                receipt = service.withdraw_proposal(
                    self.character, proposal_id, reason=reason or "",
                )
            return self._decision_receipt(receipt)

    def correct(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._assert_writable()
        old_text = payload.get("old_text")
        new_text = payload.get("new_text")
        if old_text is not None and not isinstance(old_text, str):
            raise ApiError("invalid_request", 400, "old_text must be a string")
        if new_text is not None and not isinstance(new_text, str):
            raise ApiError("invalid_request", 400, "new_text must be a string")
        if not (old_text or "").strip() and not (new_text or "").strip():
            raise ApiError("invalid_request", 400, "old_text or new_text is required")
        from rin_memory.contracts import CorrectionRequest, Scope

        with self._lock:
            receipt = asyncio.run(self.core.correction_service.correct(
                CorrectionRequest(
                    scope=Scope(self.character),
                    action="replace",
                    old_text=(old_text or "").strip(),
                    new_text=(new_text or "").strip(),
                ),
            ))
            return {
                "correction_id": receipt.correction_id,
                "version": receipt.version,
                "applied": bool(receipt.applied),
                "duplicate": bool(receipt.duplicate),
                "status": receipt.status,
                "invalidated_fact_ids": list(receipt.invalidated_fact_ids),
                "invalidated_source_keys": list(receipt.invalidated_source_keys),
                "invalidated_reflection_ids": list(receipt.invalidated_reflection_ids),
                "invalidated_persona_ids": list(receipt.invalidated_persona_ids),
                "replacement_fact_id": receipt.replacement_fact_id,
            }

    # -- direct conversation (S05 C preview) -----------------------------

    def _chat_bridge(self):
        if self.chat is None:
            raise ApiError(
                "chat_unavailable", 503,
                "this console has no conversation model configured",
            )
        return self.chat

    def chat_status(self) -> dict[str, Any]:
        if self.chat is None:
            return {
                "ready": False,
                "reason": "管理台未配置对话模型",
                "character": self.character,
                "display_name": self.display_name(),
                "demo": bool(self.demo),
            }
        state = self.chat.status()
        state["display_name"] = self.display_name() or state.get("display_name", "")
        return state

    # -- provider settings (console-editable) ----------------------------

    def _local_state_root(self) -> Path:
        roots = getattr(self.core, "roots", None)
        local = getattr(roots, "local_state_root", None)
        if local is not None:
            return Path(local)
        return self.root / "local_state"

    def _model_settings(self):
        if self.chat is None:
            return None
        return self.chat._settings_model()

    def model_config(self) -> dict[str, Any]:
        """Provider configuration for the settings page; never the credential."""
        from .model_config import load_model_config, public_model_config

        settings = self._model_settings()
        if settings is None:
            return {
                "available": False,
                "reason": "这个管理台没有装配对话模型",
                "editable": False,
            }
        stored = load_model_config(self._local_state_root())
        payload = public_model_config(settings, stored)
        payload["available"] = True
        payload["demo"] = bool(getattr(self.chat, "demo_model", False))
        # A demo stand-in answers regardless of these fields; say so instead of
        # letting the page imply a saved key would be used.
        payload["in_use"] = not payload["demo"]
        payload["file"] = str(
            self._local_state_root() / "model_config.json",
        )
        return payload

    def _replace_demo_model(self) -> None:
        """Drop the demo stand-in once real provider settings exist.

        The demo model exists so a fresh demo root can be clicked through; it
        must not keep answering after the user configures a real provider, or
        the replies would look generated while the provider is never contacted.
        """
        bridge = self.chat
        if bridge is None or not getattr(bridge, "demo_model", False):
            return
        bridge.demo_model = False
        service = bridge._service
        service.model = None

    def save_model_config(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Persist the page's provider settings and apply them in place."""
        from .model_config import (
            ModelConfigError, apply_model_config, load_model_config,
            save_model_config,
        )

        bridge = self._chat_bridge()
        # Configuring a provider is a write: a read-only console (a real root
        # opened without --allow-write) must not leave a file behind or swap the
        # model out from under whoever does own the root.
        if self.read_only:
            raise ApiError(
                "read_only", 403,
                "记忆根是只读的：用 --allow-write 启动后才能修改模型配置",
            )
        current = self._model_settings()
        stored = load_model_config(self._local_state_root())
        values = dict(stored)
        for key in ("base_url", "reasoning_effort"):
            if key in payload:
                values[key] = payload.get(key)
        if "models" in payload:
            values["models"] = payload.get("models")
        # An absent/blank `api_key` means "keep what is stored": the page never
        # receives the value, so it cannot echo it back on an unrelated save.
        if "api_key" in payload and payload.get("api_key") not in (None, ""):
            values["api_key"] = payload.get("api_key")
        elif payload.get("clear_api_key"):
            values["api_key"] = ""
        try:
            saved = save_model_config(self._local_state_root(), values)
        except ModelConfigError as exc:
            raise ApiError("invalid_argument", 400, exc.message) from exc
        # A real provider replaces the demo stand-in; otherwise saving settings
        # would leave the labelled offline model answering.
        if saved.get("base_url") or saved.get("models"):
            self._replace_demo_model()
        bridge.reconfigure(apply_model_config(current, saved))
        return self.model_config()

    def test_model_config(self) -> dict[str, Any]:
        """One real call against the configured provider.

        Used by the page's "测试连接" button: it reports the failure kind the
        adapter classified instead of a stack trace, and it is the only way to
        tell "saved but the key is wrong" from "saved and working".
        """
        from ..adapters.model_http import ModelCallError

        bridge = self._chat_bridge()
        if getattr(bridge, "demo_model", False):
            # The demo stand-in always answers, so a green result here would
            # claim the *provider* works when nothing was contacted.
            return {
                "ok": False, "kind": "config", "retryable": False,
                "message": "当前是离线演示模型；保存真实接口后才会真正连接服务商",
            }
        model = getattr(bridge._service, "model", None)
        if model is None:
            return {"ok": False, "kind": "config", "message": "尚未配置模型"}
        try:
            reply = asyncio.run(model.invoke(
                "conversation",
                [
                    {"role": "system", "content": "只回复「OK」两个字母。"},
                    {"role": "user", "content": "连接测试"},
                ],
                max_output_tokens=16, timeout_s=20.0, purpose="connection_test",
            ))
        except ModelCallError as exc:
            return {"ok": False, "kind": exc.kind, "retryable": exc.retryable,
                    "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - the page shows the kind only
            return {"ok": False, "kind": "transport", "retryable": True,
                    "message": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "kind": "", "reply": str(reply or "").strip()[:80]}

    def chat_history(self, query: Mapping[str, list[str]] | None = None) -> dict[str, Any]:
        """Transcript plus capability; readable even without a model.

        The history is served from `recent.json` so a root whose model is not
        configured still shows what the host wrote, and the page can explain
        why a new message cannot be sent yet.
        """
        query = query or {}
        limit = _int((query.get("limit") or ["80"])[0], 80)
        if self.chat is None:
            try:
                stored = self.core.recent.messages(self.character)
            except Exception:  # noqa: BLE001 - unreadable window reads as empty
                stored = []
            messages = []
            for item in stored:
                if not isinstance(item, Mapping):
                    continue
                kind = str(item.get("type") or item.get("role") or "").lower()
                if kind in ("system", "summary"):
                    continue
                text = _message_text(item).strip()
                if text:
                    messages.append({
                        "role": "assistant" if kind in ("ai", "assistant") else "user",
                        "text": text,
                    })
            bounded = max(1, min(500, limit))
            return {
                "messages": messages[-bounded:], "total": len(messages),
                "turns": [], "status": self.chat_status(),
            }
        return self.chat.history(limit=limit)

    def chat_history_payload(self, query: Mapping[str, list[str]]) -> dict[str, Any]:
        """History with the console's own display name on the status block."""
        payload = self.chat_history(query)
        status = dict(payload.get("status") or {})
        if self.chat is not None:
            status["display_name"] = self.display_name() or status.get("display_name", "")
        payload["status"] = status
        return payload

    def chat_send(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        bridge = self._chat_bridge()
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ApiError("invalid_argument", 400, "text must be a nonempty string")
        if len(text) > 8000:
            raise ApiError("invalid_argument", 400, "text is limited to 8000 characters")
        request_id = payload.get("request_id")
        if request_id is not None and not isinstance(request_id, str):
            raise ApiError("invalid_argument", 400, "request_id must be a string")
        session = payload.get("session_id")
        session_id = session.strip() if isinstance(session, str) and session.strip() else "console"
        try:
            outcome = bridge.send_sync(
                text,
                request_id=request_id or "",
                session_id=session_id,
                subject=str(payload.get("subject") or "private"),
                scene=str(payload.get("scene") or ""),
                client_id=str(payload.get("client_id") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - ChatError carries the contract
            code = getattr(exc, "code", "internal")
            raise ApiError(
                code, getattr(exc, "status", 500),
                getattr(exc, "message", str(exc)),
            ) from exc
        return outcome

    def chat_change(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        bridge = self._chat_bridge()
        if action == "clear":
            return bridge.clear_history()
        message_id = payload.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise ApiError("invalid_argument", 400, "message_id is required")
        if action == "edit":
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 8000:
                raise ApiError("invalid_argument", 400, "消息须为 1–8000 个字符")
            return bridge.edit_message(message_id, text.strip())
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ApiError("invalid_argument", 400, "request_id is required")
        return bridge.regenerate(message_id, request_id)

    def reset_demo(self) -> dict[str, Any]:
        if not self.demo:
            raise ApiError("demo_only", 403, "demo reset is only available in demo mode")
        with self._lock:
            self.core = self._reset_core(self.root, reset=True)
            return {"ok": True}

    # -- dispatch --------------------------------------------------------

    def get(self, path: str, query: Mapping[str, list[str]]) -> dict[str, Any]:
        if path == "/api/meta":
            return self.meta()
        if path == "/api/engine/status":
            return self._engine_status()
        if path == "/api/chat/history":
            return self.chat_history_payload(query)
        if path == "/api/model/config":
            return self.model_config()
        if path == "/api/snapshot":
            return self.snapshot()
        if path == "/api/overview":
            return self.overview()
        if path == "/api/timeline":
            return self.timeline(query)
        if path.startswith("/api/events/"):
            return self.event(unquote(path[len("/api/events/"):]))
        if path == "/api/facts":
            return self.facts(query)
        if path == "/api/reflections":
            return self.reflections()
        if path == "/api/persona":
            return self.persona()
        if path == "/api/evolution":
            return self.evolution()
        if path == "/api/cleanup":
            return self.cleanup(query)
        if path == "/api/state":
            return self.state()
        if path == "/api/search":
            return self.search(query)
        raise ApiError("not_found", 404, f"unknown route {path!r}")

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if path == "/api/persona/revise":
            return self.revise(payload)
        if path == "/api/persona/freeze":
            return self.freeze(payload)
        if path == "/api/persona/restore":
            return self.restore(payload)
        if path == "/api/persona/publish":
            return self.publish(payload)
        if path == "/api/persona/rollback":
            return self.rollback(payload)
        if path == "/api/chat/send":
            return self.chat_send(payload)
        if path in ("/api/chat/clear", "/api/chat/edit", "/api/chat/regenerate"):
            try:
                return self.chat_change(path.rsplit("/", 1)[1], payload)
            except Exception as exc:
                if not hasattr(exc, "status"):
                    raise
                raise ApiError(getattr(exc, "code", "internal"), exc.status, str(exc)) from exc
        if path == "/api/model/config":
            return self.save_model_config(payload)
        if path == "/api/model/test":
            return self.test_model_config()
        if path == "/api/evolution/settings":
            return self.evolution_settings(payload)
        if path == "/api/evolution/mode":
            return self.evolution_mode(payload)
        if path == "/api/evolution/evaluate":
            return self.evolution_evaluate()
        if path == "/api/evolution/retry":
            return self.evolution_retry()
        if path == "/api/evolution/propose":
            return self.evolution_propose(payload)
        if path == "/api/persona/adopt":
            return self.persona_adopt(payload)
        if path == "/api/persona/sync":
            return self.persona_sync()
        if path == "/api/cleanup/correct":
            return self.correct(payload)
        if path == "/api/cleanup/delete":
            return self.cleanup_delete(payload)
        if path == "/api/demo/reset":
            return self.reset_demo()
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "evolution"]:
            action = parts[3]
            if action in ("accept", "reject", "withdraw"):
                return self._decide(action, parts[2], payload)
        raise ApiError("not_found", 404, f"unknown route {path!r}")


