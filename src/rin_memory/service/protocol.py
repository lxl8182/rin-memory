"""Service context leases and durable consumer receipts over the existing core."""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Mapping

from ..classification import CONTENT_CLASSES
from ..contracts import Scene, Scope
from ..domain.persona_teaching import EXPRESSION_SECTION_ID
from ..domain.memory_awareness import memory_awareness
from ..event_sources import parse_source_key, sanitize_component
from ..invalidation import row_matches_subject, scene_applies
from ..persistence.records import event_from_snapshot, fingerprint
from ..persistence.consumer_receipts import ConsumerReceiptStore, RECEIPT_EVENT
from ..persistence.atomic import FileSystemAtomicStore



def scene_object(value: Any) -> Scene:
    if isinstance(value, Mapping):
        return Scene(**{k: str(value.get(k) or "") for k in ("plugin_id", "game_id", "session_id")})
    kind, _, name = str(value or "").partition(":")
    return Scene(**{ {"game": "game_id", "plugin": "plugin_id"}.get(kind, "session_id"): name}) if name else Scene()


class ServiceProtocol:
    def __init__(self, service) -> None:
        self.service = service
        self.core = service.core
        self.character = service.settings.character
        self._lock = threading.RLock()
        self._contexts: OrderedDict[str, dict] = OrderedDict()
        self._sessions: set[tuple] = set()
        self._pending_uploads = 0
        self._store = self.core.consumer_receipts
        self._reservations_path = self.core.roots.local_state_root / "host_turn_contexts.json"
        self._atomic = FileSystemAtomicStore()

    def _load(self) -> dict:
        return self._store.load(self.character)

    def _save(self, document) -> None:
        self._store.save(self.character, document)

    _apply = staticmethod(ConsumerReceiptStore.apply)

    def basis(self) -> dict:
        snapshot = self.core.barrier.snapshot(self.character)
        # The basis is the deletion/correction fence and nothing else: `validate`
        # compares it to decide a context receipt went stale, so adding an
        # unrelated version here would invalidate every live receipt on the next
        # interest write. The interest version is reported as its own layer key.
        return {
            "deletion_version": snapshot.deletion_version,
            "correction_version": snapshot.correction_version,
        }

    @staticmethod
    def scope(body: Mapping) -> dict:
        return {k: str(body.get(k) or default) for k, default in (
            ("subject", "private"), ("scene", ""), ("session_id", ""), ("client_id", ""))}

    @contextmanager
    def session(self, scope: Mapping):
        from .server import ApiError
        key = (scope["subject"], scope["session_id"])
        # Callers without a session still have request-level deduplication.
        if not key[1]:
            yield
            return
        with self._lock:
            if key in self._sessions:
                raise ApiError("turn_in_progress", "this session already has an in-flight turn")
            self._sessions.add(key)
        try:
            yield
        finally:
            with self._lock:
                self._sessions.remove(key)

    def recent(self, scope: Mapping) -> list[dict]:
        rows = []
        for message in self.core.barrier.filter_messages(self.character, self.core.recent.messages(self.character)):
            data = message.get("data") or {}
            meta = message.get("metadata") or data.get("metadata") or {}
            if not row_matches_subject(meta, scope["subject"]):
                continue
            if meta.get("scene", "") != scope["scene"]:
                continue
            if scope["session_id"] and meta.get("session_id", "") != scope["session_id"]:
                continue
            if message.get("type") in ("human", "ai"):
                rows.append(message)
        return rows[-20:]

    @staticmethod
    def context_layers(recall: Mapping[str, Any]) -> dict:
        """M5 additive layers of /v1/context (existing keys are untouched).

        'working_summary' is the three rebuildable projections, 'state' is the
        real companion_state snapshot the recall read (same version as the block
        the model saw), and 'episode' is the derived experience view. They are
        reported separately from 'context' so a consumer written against B06
        keeps reading exactly the same fields it always did.
        """
        state = recall.get("companion_state")
        summary = recall.get("working_summary")
        episode = recall.get("episode_summary")
        signals = recall.get("interest_signals")
        return {
            "working_summary": dict(summary) if isinstance(summary, Mapping) else None,
            "state": dict(state) if isinstance(state, Mapping) else dict(
                {"block": "", "version": 0, "updated_at": None, "items": []}
            ),
            "state_version": int(recall.get("companion_state_version") or 0),
            "episode": dict(episode) if isinstance(episode, Mapping) else None,
            "threads_source": str(recall.get("threads_source") or "none"),
            # RO03: interest evidence as *data*, never as prose, plus the
            # version of that data. A caller can show what the `about_owner`
            # section may be written from, and two reads inside one generation
            # turn report the same version (a new turn sees the new one).
            "interest_signals": dict(signals) if isinstance(signals, Mapping) else {},
            "interest_version": int(signals.get("version") or 0)
            if isinstance(signals, Mapping) else 0,
        }

    def issue(self, scope: Mapping, recall: dict, *, basis: dict | None = None) -> dict:
        from .server import ApiError
        basis = basis if basis is not None else self.basis()
        card = self.core.persona_card_service.get(self.character)
        published = self.core.persona_publish.current(self.character)
        if self.service.settings.require_published_persona and not published.available:
            raise ApiError("chat_unavailable", "publish the initial persona before requesting context")
        projection = dict(published.projection) if published.available else self.core.persona_card_service.render_projection(self.character)
        sections = {section.section_id: section.scope for section in card.sections}
        section_scopes = published.section_scopes or sections
        projection = {key: text for key, text in projection.items() if key in sections
                      and row_matches_subject(section_scopes.get(key, {}), scope["subject"])
                      and scene_applies(section_scopes.get(key, {}).get("applies_to"), scene_object(scope["scene"]))}
        correction = self.core.correction_service.block(
            self.character, subject=scope["subject"], scene=scene_object(scope["scene"]),
        )
        client_id = scope.get("client_id", "")
        client = self.service.settings.clients.get(client_id) or {}
        web = client_id in ("", "console")
        awareness = memory_awareness(
            correction=web or client.get("memory_tools", False),
            teaching=client.get("persona_teach", False), web=web,
            reflection=self.core.maintenance.settings.enable_reflection and self.core.reflection_service._model is not None,
            mode=self.core.persona_admin_service.get_mode(self.character),
        )
        # Required constraints are read independently of the optional recall budget.
        receipt_id = "ctx_" + uuid.uuid4().hex
        result = {
            "character_id": self.character, "persona_version": published.version if published.available else "",
            "card_revision": published.revision if published.available else card.revision,
            "projection": projection, "persona": "\n\n".join(projection.values()),
            "expression": self.expression(projection, card, published),
            "memory_awareness": awareness,
            "context": recall, "corrections": {"block": correction.block, "version": correction.version},
            "recent_turns": self.recent(scope), "context_receipt_id": receipt_id,
            "expires_in_s": 300,
            "basis": basis,
            "mention_preferences": [row["term"] for row in self.core.directives.active(self.character)]
            if scope["subject"] == "private" else [],
            # M5 additive layers; the keys above keep their exact shape.
            **self.context_layers(recall),
        }
        with self._lock:
            self._contexts[receipt_id] = {"scope": dict(scope), "basis": basis,
                "expires": time.monotonic() + 300, "result": result}
            while len(self._contexts) > 2048:
                self._contexts.popitem(last=False)
        return result

    def expression(self, projection: Mapping, card, published) -> dict:
        """The taught expression rules that are actually in this projection.

        Additive field (MC04-T): it names the *projection* of the existing tone
        section, not a new section. A rule is reported only when its line is
        still in the tone text the model is about to read, so a rule the user
        edited away by hand is not claimed as active. The payload carries ids and
        versions, never the quoted example a rule may cite.
        """
        text = str(projection.get(EXPRESSION_SECTION_ID) or "")
        if not text:
            return {"section_id": EXPRESSION_SECTION_ID, "rules": [],
                    "tone_revision": 0, "tone_version": ""}
        present = {line.strip() for line in text.splitlines() if line.strip()}
        rules = [
            {
                "rule_id": str(rule.get("rule_id") or ""),
                "teaching_id": str(rule.get("teaching_id") or ""),
                "text": str(rule.get("text") or ""),
            }
            for rule in self.service.persona_teaching.rules(self.character)
            if str(rule.get("line") or "") in present
        ]
        section_revision = 0
        for section in getattr(card, "sections", ()) or ():
            if section.section_id == EXPRESSION_SECTION_ID:
                section_revision = int(getattr(section, "revision", 0) or 0)
                break
        tone_version = ""
        if getattr(published, "available", False) and (
            int(getattr(published, "revision", 0) or 0) == int(card.revision)
        ):
            tone_version = str(getattr(published, "version", "") or "")
        return {
            "section_id": EXPRESSION_SECTION_ID,
            "rules": rules,
            "tone_revision": section_revision,
            "tone_version": tone_version,
        }

    def reserve(self, body: Mapping, *, identity: Mapping | None = None) -> dict:
        """Pin a generated turn's basis before generation; resume after restart.

        Only version identifiers are retained. The reservation cannot be used
        to read context or submit another request, and never bypasses a barrier.

        QP05: an optional `consumer_refs` object additionally records which
        memories this client actually consumed, as ids only. Omitting it keeps
        the previous behaviour and the previous response, byte for byte.
        """
        from .server import ApiError
        scope = self.scope(body)
        request_id = str(body.get("request_id") or "")
        if not request_id or len(request_id) > 200:
            raise ApiError("invalid_argument", "request_id is required")
        receipt_id = str(body.get("context_receipt_id") or "")
        with self._lock:
            context = self.validate(receipt_id, scope)
            document = self._atomic.load(self._reservations_path) or {"items": {}}
            items = document["items"]
            previous = items.get(request_id)
            record = {"scope": dict(scope), "context_receipt_id": receipt_id,
                      "basis": context["basis"], "persona_version": context["persona_version"],
                      "card_revision": context["card_revision"]}
            if previous and previous != record:
                raise ApiError("request_conflict", "request already reserved with another context")
            if not previous:
                # Committed reservations are dispensable: normal turn replay
                # uses the durable request ledger, without a context ticket.
                for key in list(items):
                    turn = self.core.turn_requests.get(self.character, key)
                    if items[key]["basis"] != self.basis() or turn and (turn.is_committed or turn.status == "deleted"):
                        del items[key]
                if len(items) >= 2048:
                    raise ApiError("capacity", "host turn reservation capacity is full")
                items[request_id] = record
                self._atomic.save(self._reservations_path, document)
            recorded = None
            if "consumer_refs" in body:
                recorded = self._record_consumer_ref(
                    body, scope, context, request_id, identity=identity,
                )
        result = {"status": "reserved", "request_id": request_id, "durable": True}
        if recorded is not None:
            # Only a caller that opted in sees the new field, so a client that
            # does not track consumption gets exactly the previous response.
            result["consumer_refs"] = "recorded" if recorded else "ignored"
        return result

    def _record_consumer_ref(
        self, body: Mapping, scope: Mapping, context: Mapping, request_id: str,
        *, identity: Mapping | None = None,
    ) -> bool:
        """Persist which memories this plugin session actually consumed (QP05).

        The ids are taken from the *validated* context result, never from the
        caller: a client must not be able to claim another row was consumed and
        then receive that row's cleanup notification. Everything stored here is
        an id, a version or a timestamp — never text.
        """
        from .server import ApiError
        store = getattr(self.core, "consumer_refs", None)
        # The authenticated plugin id wins: a client must not be able to file a
        # reference under another plugin's name and then be the one told to
        # clean up (or be skipped for a cleanup that is really its copy).
        authenticated = str((identity or {}).get("plugin_id") or "").strip()[:64]
        plugin_id = authenticated or str(body.get("plugin_id") or "").strip()[:64]
        declared = body.get("consumer_refs")
        if not isinstance(declared, Mapping):
            raise ApiError("invalid_argument", "consumer_refs must be an object")
        claimed = str(declared.get("plugin_id") or "").strip()
        if claimed and plugin_id and claimed != plugin_id:
            raise ApiError("forbidden", "consumer_refs.plugin_id differs from this identity")
        plugin_id = plugin_id or claimed[:64]
        if store is None or not plugin_id:
            return False
        served = context.get("context") or {}
        served_ids = [str(item) for item in served.get("memory_ids") or () if item]
        served_sources = [str(item) for item in served.get("source_ids") or () if item]
        summary = context.get("working_summary") or {}
        for section in (summary.get("sections") or {}).values():
            served_sources.extend(str(key) for key in section.get("refs") or () if key)
            for item in section.get("items") or ():
                if item.get("id"):
                    served_ids.append(str(item["id"]))
                served_sources.extend(str(key) for key in item.get("source_keys") or () if key)
        served_ids = list(dict.fromkeys(served_ids))
        served_sources = list(dict.fromkeys(served_sources))
        wanted = declared.get("memory_ids")
        if isinstance(wanted, list) and wanted:
            claimed_ids = {str(item) for item in wanted}
            # The claim is a *filter*, not a source of truth: it may only narrow
            # what this receipt really carried, and an empty intersection falls
            # back to the served rows so a client bug cannot hide a copy from
            # the deletion loop (over-notifying costs one extra session check,
            # under-notifying leaves text in the chat history).
            memory_ids = [item for item in served_ids if item in claimed_ids] or served_ids
        else:
            memory_ids = served_ids
        source_ids = [str(item) for item in declared.get("source_ids") or ()
                      if item and str(item) in served_sources] or served_sources
        generation = str(declared.get("context_generation") or "")[:120]
        if not generation:
            generation = "%s:%s" % (
                context.get("persona_version") or "", context.get("card_revision") or 0,
            )
        store.record(self.character, {
            "plugin_id": plugin_id,
            "session_id": str(declared.get("session_id") or scope.get("session_id") or ""),
            "request_id": request_id,
            "character": self.character,
            "subject": str(scope.get("subject") or "private"),
            "memory_ids": memory_ids,
            "source_ids": source_ids,
            "persona_version": str(context.get("persona_version") or ""),
            "context_generation": generation,
            "deletion_version": int((context.get("basis") or {}).get("deletion_version") or 0),
        })
        return True

    def consume(self, body: Mapping, *, identity: Mapping | None = None) -> dict:
        """Adopt an output generation's context without reserving a chat turn."""
        from .server import ApiError
        request_id = str(body.get("request_id") or "").strip()
        if not request_id:
            raise ApiError("invalid_argument", "request_id is required")
        context = self.validate(str(body.get("context_receipt_id") or ""), self.scope(body))
        recorded = self._record_consumer_ref(body, self.scope(body), context,
                                             request_id, identity=identity)
        adoption = self.core.persona_publish.consume(self.character, "dsh_qq_contact",
                                                     expected_version=context["persona_version"])
        return {"status": "consumed", "request_id": request_id,
                "context_receipt_id": context["context_receipt_id"],
                "consumer_refs": "recorded" if recorded else "ignored",
                "persona_version": context["persona_version"],
                "adoption_status": adoption.status}

    def validate(self, receipt_id: str, scope: Mapping, *, request_id: str = "") -> dict:
        from .server import ApiError
        with self._lock:
            if request_id:
                document = self._atomic.load(self._reservations_path) or {"items": {}}
                reserved = document["items"].get(request_id)
                if reserved and reserved["context_receipt_id"] == receipt_id and reserved["scope"] == dict(scope) and reserved["basis"] == self.basis():
                    return reserved
                if reserved:
                    raise ApiError("context_stale", "reserved turn identity or deletion/correction basis changed")
            record = self._contexts.get(receipt_id)
        if not record or record["expires"] <= time.monotonic() or record["scope"] != dict(scope) or record["basis"] != self.basis():
            raise ApiError("context_stale", "context expired or its identity/deletion/correction basis changed")
        return record["result"]

    def validate_source(self, source_key: str, scope: Mapping, plugin_id: str = "") -> dict:
        from .server import ApiError
        parsed = parse_source_key(source_key)
        if not parsed or parsed.get("character") != sanitize_component(self.character) or parsed.get("subject") != sanitize_component(scope["subject"]):
            raise ApiError("forbidden", "source_key does not belong to this character and subject")
        if plugin_id and parsed.get("plugin_id") != plugin_id:
            raise ApiError("forbidden", "source_key does not belong to this plugin")
        return parsed

    def check_capacity(self, source_key: str, scope: Mapping) -> None:
        from .server import ApiError
        if self.core.barrier.blocks_source_key(self.character, source_key):
            raise ApiError("deleted", "event source was deleted")
        if self.core.ledger.lookup(Scope(self.character, scope["subject"]), source_key) is not None:
            return
        if len(self.core.outbox.pending(self.character, limit=self.service.settings.max_pending_events)) + self._pending_uploads >= self.service.settings.max_pending_events:
            raise ApiError("capacity", "pending event capacity is full; run maintenance and retry")

    @contextmanager
    def ingest_slot(self, source_key: str, scope: Mapping):
        with self._lock:
            self.check_capacity(source_key, scope)
            self._pending_uploads += 1
        try:
            yield
        finally:
            with self._lock:
                self._pending_uploads -= 1

    async def event(self, body: Mapping) -> dict:
        from .server import ApiError
        scope = self.scope(body)
        source = str(body.get("source_key") or "")
        parsed = self.validate_source(source, scope, str(body.get("plugin_id") or ""))
        classification = body.get("content_class") or "shared_experience"
        if classification not in CONTENT_CLASSES:
            raise ApiError("invalid_argument", "unknown content_class")
        event = event_from_snapshot({
            "scope": {"character": self.character, "subject": scope["subject"]},
            "source_key": source, "event_type": body.get("event_type", "scene_experience"),
            "text": body.get("text", ""), "occurred_at": body.get("occurred_at"),
            "scene": asdict(scene_object(body.get("scene") or {"plugin_id": parsed.get("plugin_id", "")})),
            "applies_to": asdict(scene_object(body.get("applies_to", body.get("scene") or {"plugin_id": parsed.get("plugin_id", "")}))),
            "content_class": classification, "payload": body.get("payload", {}),
            "event_id": body.get("event_id"), "episode_id": body.get("episode_id"),
        })
        with self.ingest_slot(source, scope):
            ingested = await self.core.extraction.ingest_turn(
                self.character, [{"type": "human", "data": {"content": event.text}}],
                source_key=source, content_class=classification, episode_id=event.episode_id,
                session_id=scope["session_id"], occurred_at=event.occurred_at, inline=False, event=event,
            )
        if ingested.status == "deleted":
            raise ApiError("deleted", "event source was deleted")
        if ingested.status == "rejected":
            raise ApiError("request_conflict", ingested.reason or "event rejected")
        return {"status": ingested.status, "durable": True, "source_key": source,
                "event_id": event.event_id, "extraction_op_id": ingested.op_id,
                "reason": ingested.reason}

    #: Receipt kinds and what each one is evidence for. `sent` means the
    #: platform accepted the text (a transport fact); `delivered` stays the
    #: display/playback fact of a UI consumer. They are never interchangeable:
    #: a QQ bridge can prove `sent` but not that the user read it.
    RECEIPT_KINDS = ("used", "sent", "delivered", "interrupted", "failed")
    RECEIPT_TRANSPORTS = ("qq_official", "qq_napcat", "console", "host")
    #:
    #: **The two vocabularies, and how they now agree (M6-A).** Nothing below
    #: changes what an existing kind means; it names the relation explicitly so
    #: a consumer stops guessing:
    #:
    #: | question | the one authoritative answer |
    #: |---|---|
    #: | did she *adopt* the persona this turn used? | `used` (a usage receipt; it
    #:   carries `adoption_status` from the publish boundary) |
    #: | did the platform *accept* the text? | `sent` for a whole turn reply,
    #:   and the per-part delivery receipt on the output record for a body that
    #:   was generated before anyone spoke (`/v1/outputs/deliver`) |
    #: | did a UI actually show / play it? | `delivered` (display/playback fact) |
    #:
    #: So "adopted" and "delivered" were never the same axis and still are not:
    #: **adoption is about the persona version, delivery is about the text.** The
    #: closure added here is an optional `output_id` on a receipt, plus a read
    #: view (`receipt_summary`) that answers both questions for one request from
    #: the records that really exist, instead of the caller having to join two
    #: tables itself.

    def receipt(self, body: Mapping) -> dict:
        from .server import ApiError
        kind = body.get("kind")
        if kind not in self.RECEIPT_KINDS:
            raise ApiError("invalid_argument", "kind must be used, sent, delivered, interrupted or failed")
        scope = self.scope(body)
        request_id = str(body.get("request_id") or "")
        record = self.core.turn_requests.get(self.character, request_id)
        if record is None or record.subject != scope["subject"] or record.client_id != scope["client_id"] or record.session_id != scope["session_id"] or record.scene != scope["scene"]:
            raise ApiError("not_found", "unknown request in this scope")
        if record.status == "deleted":
            raise ApiError("deleted", "request result was invalidated")
        if not record.is_committed:
            raise ApiError("request_conflict", "request has not committed")
        receipt_id = str(body.get("receipt_id") or "")
        if not receipt_id:
            raise ApiError("invalid_argument", "receipt_id is required")
        payload = {"receipt_id": receipt_id, "request_id": request_id, "kind": kind,
            "scope": scope, "consumer": str(body.get("consumer") or "chat"),
            "persona_version": record.persona_version, "source_key": record.source_key,
            "received_at": self.core.clock.now().isoformat()}
        if kind in ("delivered", "interrupted"):
            count = body.get("delivered_chars", len(record.reply or "") if kind == "delivered" else 0)
            if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= len(record.reply or ""):
                raise ApiError("invalid_argument", "delivered_chars is outside the committed reply")
            payload["delivered_chars"] = count
        # M6-A: a receipt may name the output record it is evidence about. It is
        # additive (absent -> the row is byte-for-byte the old shape) and it is
        # validated against this subject, so naming someone else output is a
        # 404 rather than a silent cross-scope link.
        output_id = str(body.get("output_id") or "").strip()
        if output_id:
            outputs = getattr(self.core, "output_service", None)
            known = (
                outputs.get(self.character, output_id, include_text=False)
                if outputs is not None else None
            )
            if known is None or str(known.get("subject") or "") != scope["subject"]:
                raise ApiError("not_found", "unknown output in this scope")
            payload["output_id"] = output_id
        if kind == "sent":
            # A transport acceptance is its own fact: which platform took the
            # text, and which platform message ids carry it. Splitting a reply
            # means several ids; only the failed segments are retried, so the
            # record must name the parts that actually landed.
            transport = str(body.get("transport") or "").strip()
            if transport not in self.RECEIPT_TRANSPORTS:
                raise ApiError("invalid_argument", "sent receipts need a known transport")
            parts = body.get("parts")
            if not isinstance(parts, list) or not parts:
                raise ApiError("invalid_argument", "sent receipts need the accepted parts")
            accepted: list[dict] = []
            sent_chars = 0
            for part in parts:
                if not isinstance(part, Mapping):
                    raise ApiError("invalid_argument", "each accepted part must be an object")
                count = part.get("chars")
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ApiError("invalid_argument", "a part's chars must be a non-negative integer")
                sent_chars += count
                accepted.append({
                    "index": len(accepted),
                    "platform_id": str(part.get("platform_id") or "")[:200],
                    "chars": count,
                })
            if sent_chars > len(record.reply or ""):
                raise ApiError("invalid_argument", "accepted characters exceed the committed reply")
            payload.update(transport=transport, parts=accepted, sent_chars=sent_chars)
        with self._lock:
            previous = self._load()["receipts"].get(receipt_id)
            if previous:
                if fingerprint({k:v for k,v in previous.items() if k not in ("received_at", "adoption_status")}) != fingerprint({k:v for k,v in payload.items() if k != "received_at"}):
                    raise ApiError("request_conflict", "receipt_id reused with different content")
                return dict(previous, durable=True, duplicate=True)
            # A usage receipt follows a committed generation, never a context read.
            if kind == "used" and record.persona_version:
                adoption = self.core.persona_publish.consume(self.character, payload["consumer"], expected_version=record.persona_version)
                payload["adoption_status"] = adoption.status
            self.core.journal.record_and_save(self.character, RECEIPT_EVENT, payload,
                load_view=lambda _: self._load(), mutate_view=lambda doc: self._apply(doc, payload),
                save_view=lambda _, doc: self._save(doc))
        return dict(payload, durable=True, duplicate=False)

    def used(self, scope: Mapping, payload: dict) -> dict:
        if payload.get("status") != "committed":
            return payload
        receipt = self.receipt(dict(scope, request_id=payload["request_id"], kind="used",
                                    receipt_id="used:" + scope["client_id"] + ":" + payload["request_id"], consumer="chat"))
        return dict(payload, usage_receipt_id=receipt["receipt_id"])

    def receipt_summary(self, scope: Mapping, request_id: str) -> dict:
        """One request adoption and delivery, answered from the records.

        The plain read (`/v1/receipts`) lists receipts for a scope, which forces
        a caller to filter by request and to know that a turn reply and a
        pre-generated body are delivered through two different mechanisms. This
        view answers the two questions separately and reports which record each
        answer came from, so the caller never has to join them:

        * **adopted** -- the persona version this turn was generated with, and
          whether the chat consumer adopted it (the `used` receipt);
        * **delivered** -- how much of the text really arrived, from the `sent`
          parts of a turn reply and/or the per-part receipts of an output record.

        Nothing here is inferred: every field is read from a stored row, and an
        unknown request in this scope is a 404 like the rest of the surface.
        """
        from .server import ApiError

        wanted = str(request_id or "").strip()
        if not wanted:
            raise ApiError("invalid_argument", "request_id is required")
        record = self.core.turn_requests.get(self.character, wanted)
        if record is None or record.subject != scope["subject"]:
            raise ApiError("not_found", "unknown request in this scope")
        with self._lock:
            rows = [
                row for row in self._load()["receipts"].values()
                if row["scope"] == dict(scope)
                and str(row.get("request_id")) == wanted
            ]
        used = [row for row in rows if str(row.get("kind")) == "used"]
        sent = [row for row in rows if str(row.get("kind")) == "sent"]
        delivered = [row for row in rows if str(row.get("kind")) == "delivered"]
        reply = record.reply or ""
        outputs = getattr(self.core, "output_service", None)
        output_rows: list[dict] = []
        for row in (outputs.list(self.character, limit=512) if outputs else []):
            if str(row.get("subject") or "") != scope["subject"]:
                continue
            cause = row.get("cause") if isinstance(row.get("cause"), Mapping) else {}
            if str(cause.get("request_id") or "") != wanted:
                continue
            output_rows.append(row)
        delivered_chars = max(
            [int(row.get("delivered_chars") or 0) for row in delivered] or [0],
        )
        return {
            "request_id": wanted,
            "subject": scope["subject"],
            "status": "invalidated" if record.status == "deleted" else "recorded",
            "adopted": {
                "persona_version": record.persona_version,
                "adoption_status": next(
                    (row.get("adoption_status") for row in used
                     if row.get("adoption_status")),
                    None,
                ),
                "receipt_ids": [str(row.get("receipt_id")) for row in used],
            },
            "delivered": {
                "text_chars": len(reply),
                "sent_chars": sum(int(row.get("sent_chars") or 0) for row in sent),
                "delivered_chars": delivered_chars,
                "complete": bool(reply) and delivered_chars >= len(reply),
                "transport_parts": [
                    part for row in sent for part in row.get("parts") or ()
                ],
            },
            "outputs": [
                {
                    "output_id": str(row.get("output_id") or ""),
                    "status": str(row.get("status") or ""),
                    "parts_total": int(row.get("parts_total") or 0),
                    # parts_delivered is a *list* of receipt rows in the M5
                    # contract; only its length is meaningful here.
                    "parts_delivered": len(row.get("parts_delivered") or ()),
                    "delivered_chars": int(row.get("delivered_chars") or 0),
                    "pending_indices": list(row.get("pending_indices") or ()),
                    "resumable": bool(row.get("resumable")),
                }
                for row in output_rows
            ],
            "output_delivered_chars": sum(
                int(row.get("delivered_chars") or 0) for row in output_rows
            ),
            "receipt_kinds": sorted({str(row.get("kind")) for row in rows}),
        }

    def receipts(self, scope: Mapping) -> dict:
        with self._lock:
            rows = [row for row in self._load()["receipts"].values() if row["scope"] == dict(scope)]
        visible = []
        for row in rows:
            record = self.core.turn_requests.get(self.character, row["request_id"])
            visible.append(dict(row, status="invalidated" if record is None or record.status == "deleted" else "recorded"))
        return {"items": visible}
