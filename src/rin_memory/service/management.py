"""Management endpoints reuse the console views and the service's one core."""
from __future__ import annotations

import secrets
import os
import asyncio
from dataclasses import asdict
from typing import Any, Mapping

from ..persistence.atomic import FileSystemAtomicStore
from ..persistence.persona_card import with_name_line
from .chat import ChatBridge
from .console import ApiError, RinApp, _section_view
from .memory_service import MemoryService

#: Name of the pre-card metadata file. Read once for migration, then removed.
LEGACY_NAME_FILE = "service_character.json"


class ManagementApi:
    def __init__(self, service: MemoryService, *, display_name: str = "",
                 subject: str = "private", config_missing=()) -> None:
        self.service = service
        self.subject = subject
        self.config_missing = list(config_missing)
        self.token = secrets.token_urlsafe(32)
        # One bridge per service: it reuses this service's core, model and
        # capability state, so the console conversation and `/v1/chat` cannot
        # diverge into two writers or two model configurations.
        self.chat = ChatBridge(
            core=service.core, character=service.settings.character,
            service=service,
        )
        self.console = RinApp(
            core=service.core, mode="service", root=service.settings.memory_root,
            read_only=False, demo=False, character=service.settings.character,
            display_name=display_name or service.settings.character,
            chat=self.chat,
        )
        # A name used to live in its own file, outside the card; that split made
        # it the one value no editor could reach. It is now a card section, and
        # any leftover file is folded in once and then retired.
        self._retired_metadata = service.settings.memory_root / LEGACY_NAME_FILE
        self._store = FileSystemAtomicStore()
        self._migrate_legacy_name()
        # The local console manages the configured character across its scopes.
        # A caller-supplied subject is never used to widen that authority.

    def _migrate_legacy_name(self) -> None:
        """Bring any earlier name storage onto the identity section's first line.

        Two earlier shapes exist: a name-only card section (`identity_name`)
        written by the first version of this feature, and a metadata file that
        predates the card owning a name at all. Both are folded into the
        identity line and then removed, so exactly one place holds the name.
        """
        card = self.service.core.persona_card_service.get(self.console.character)
        if not card.initial_revision:
            return
        # A retired name-only section: its text is the name unless the identity
        # line already declares one.
        stale = [section for section in card.sections
                 if section.section_id == "identity_name"]
        if stale:
            legacy_section_name = stale[0].text.strip()
            has_name = bool(self.service.core.persona_card_service.display_name(
                self.console.character))
            try:
                operations = [{"op": "delete", "section_id": "identity_name"}]
                if legacy_section_name and not has_name:
                    identity = next(
                        (section for section in card.sections
                         if section.section_id == "identity"), None,
                    )
                    if identity is not None:
                        operations.append({
                            "op": "replace", "section_id": "identity",
                            "text": with_name_line(identity.text, legacy_section_name),
                        })
                self.service.core.persona_card_service.revise(self.console.character, {
                    "base_revision": card.revision,
                    "operations": operations,
                    "reason": "name_section_folded_into_identity",
                    "origin": "user_edit", "actor": "system",
                })
            except Exception:  # noqa: BLE001 - retried on the next start
                pass

        legacy = self._store.load(self._retired_metadata)
        if not legacy:
            return
        if legacy.get("character_id") != self.service.settings.character:
            raise ValueError("character.character_id differs from this memory root")
        name = str(legacy.get("display_name") or "").strip()
        if name and not self.service.core.persona_card_service.display_name(
                self.console.character):
            try:
                self.service.core.persona_card_service.rename(
                    self.console.character, name,
                    reason="name_migrated_from_host_file",
                )
            except Exception:  # noqa: BLE001 - keep the file and retry later
                return
        if self.service.core.persona_card_service.display_name(self.console.character):
            try:
                self._retired_metadata.unlink()
            except OSError:
                pass

    def initialization(self) -> dict[str, Any]:
        card = self.service.core.persona_card_service.get(self.console.character)
        published = self.service.core.persona_publish.current(self.console.character)
        missing = []
        if not card.initial_revision:
            missing.append("initial_persona")
        if not published.available:
            missing.append("first_publish")
        self.service._refresh_chat_capability()
        return {
            "character_id": self.console.character,
            "display_name": self.console.display_name(),
            "subject": self.subject, "initialized": card.initial_revision > 0,
            "published": published.available,
            "revision": card.revision, "version": published.version,
            "missing": missing, "capabilities": self.service.state.to_dict(),
            "config_missing": self.config_missing,
        }

    def bootstrap(self) -> dict[str, Any]:
        return {"client_id": "console", "write_token": self.token,
                "initialization": self.initialization()}

    def initialize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        character = payload.get("character_id", self.console.character)
        if character != self.console.character:
            raise ApiError("scope_mismatch", 403, "character_id differs from stack configuration")
        display_name = payload.get("display_name")
        text = payload.get("text")
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 80:
            raise ApiError("invalid_argument", 400, "display_name must contain 1 to 80 characters")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ApiError("invalid_argument", 400, "text must contain 1 to 4000 characters")
        with self.console._lock:
            receipt = self.service.core.persona_card_service.import_initial(
                self.console.character, [{
                    "section_id": "identity", "kind": "identity",
                    # The name owns the first line of the identity section.
                    "text": with_name_line(text.strip(), display_name.strip()),
                    "scope": {"subject": self.subject, "applies_to": "all"},
                    "editable_by": {"user": True, "evolution": False}, "frozen": True,
                }], source="console_initialization", actor="user",
            )
            if receipt.status == "rejected":
                raise ApiError("invalid_argument", 400, receipt.reason)
            # Initialization imports the card exactly once; a retry reports the
            # duplicate and changes neither the name nor any edited section.
            result = self.console._revision_receipt(receipt)
            result["initialization"] = self.initialization()
            return result

    def identity(self, client: str, token: str) -> dict:
        if client == "console" and secrets.compare_digest(token, self.token):
            return {"client_id": client, "subjects": [self.subject], "admin": True}
        spec = self.service.settings.clients.get(client)
        if isinstance(spec, Mapping):
            expected = os.environ.get(spec.get("token_env", ""), "")
            if expected and secrets.compare_digest(token, expected):
                return {"client_id": client, "subjects": spec.get("subjects", [self.subject]),
                        "plugin_id": spec.get("plugin_id", ""), "admin": False,
                        # A limited, explicitly configured capability: the
                        # desktop host owns the local character editor and may
                        # maintain the semantic persona card. It is not the
                        # console identity and grants nothing else.
                        "host_persona": bool(spec.get("host_persona", False)),
                        # A second, independent grant: the chat client may ask
                        # for the user's own corrections/forgettings from inside
                        # a conversation. It reaches no console route, no other
                        # subject and no bulk edit, and it never implies the
                        # persona capability.
                        "memory_tools": bool(spec.get("memory_tools", False)),
                        # A third independent grant (MC04-T): the chat client may
                        # submit an *explicit teaching* from inside the
                        # conversation, and take one back. It can only point at a
                        # message the service already saved; it can never post
                        # rule text, edit another section, or publish for a
                        # consumer other than the next turn. Narrower than
                        # host_persona on purpose.
                        "persona_teach": bool(spec.get("persona_teach", False)),
                        # M5: a fourth independent grant -- record assistant
                        # output and report delivery. Narrower than all of the
                        # above: a receipt can only *confirm* what arrived, never
                        # declare undelivered text an experience.
                        "memory_outputs": bool(spec.get("memory_outputs", False)),
                        # M6-A: which plugin ids this client may cite as a saved
                        # message source. Per client on purpose (a global list
                        # would let any teaching-capable client reach every
                        # plugin's messages), and it grants nothing by itself:
                        # the conditions in _saved_user_message are AND.
                        "trusted_plugins": [
                            str(item) for item in spec.get("trusted_plugins") or ()
                            if isinstance(item, str) and item
                        ]}
        raise ApiError("unauthorized", 401, "a current client identity is required")

    def scoped(self, payload: Mapping, identity: Mapping) -> dict:
        result = dict(payload)
        character = result.get("character_id", self.console.character)
        subject = result.get("subject", identity["subjects"][0])
        if character != self.console.character or subject not in identity["subjects"]:
            raise ApiError("scope_mismatch", 403, "character or subject is outside the client identity")
        if result.get("client_id", identity["client_id"]) != identity["client_id"]:
            raise ApiError("scope_mismatch", 403, "client_id differs from authenticated identity")
        if identity.get("plugin_id") and result.get("plugin_id", identity["plugin_id"]) != identity["plugin_id"]:
            raise ApiError("scope_mismatch", 403, "plugin_id differs from authenticated identity")
        result.update(character_id=character, subject=subject, client_id=identity["client_id"])
        if identity.get("plugin_id"):
            result["plugin_id"] = identity["plugin_id"]
        return result

    def check_scope(self, payload: Mapping) -> None:
        if payload.get("character_id", self.console.character) != self.console.character:
            raise ApiError("scope_mismatch", 403, "character_id differs from stack configuration")
        if payload.get("subject", self.subject) != self.subject:
            raise ApiError("scope_mismatch", 403, "console subject differs from stack configuration")

    # -- limited host persona capability (S05-H01) -----------------------

    @staticmethod
    def require_host_persona(identity: Mapping) -> None:
        """Refuse everything unless this identity was granted the capability.

        The grant is per client in `stack.toml` (`host_persona = true`) and is
        checked here as well as in the transport, so a routing mistake cannot
        silently widen a normal client into a persona editor.
        """
        if not identity.get("host_persona"):
            raise ApiError("forbidden", 403, "this identity has no persona management capability")

    def host_persona(self) -> dict[str, Any]:
        """The card the host editor renders: sections, revision, published version.

        Deliberately the same domain reads the console uses; the host never
        reconstructs persona semantics from `characters.json`.
        """
        character = self.console.character
        card = self.service.core.persona_card_service.get(character)
        published = self.service.core.persona_publish.current(character)
        versions = self.service.core.persona_versions.load(character)
        current = versions.get("current") if isinstance(versions, Mapping) else None
        return {
            "character_id": character,
            "revision": card.revision,
            "initial_revision": card.initial_revision,
            "display_name": self.console.display_name(),
            "sections": [_section_view(section.to_mapping()) for section in card.sections],
            "published": {
                "available": bool(published.available),
                "version": published.version,
                "revision": published.revision,
                "target": str(current.get("target") or "") if isinstance(current, Mapping) else "",
                "published_at": (
                    current.get("published_at") if isinstance(current, Mapping) else None
                ),
                "stale": bool(published.available) and published.revision != card.revision,
            },
        }

    def host_revise(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Apply explicit host edits under base_revision CAS.

        A stale base revision is a real conflict: the caller keeps its draft and
        re-reads the current card. This path never falls back to a local save,
        and never re-reads the latest revision on the caller's behalf.
        """
        from .console import ApiError as _ConsoleError

        base_revision = payload.get("base_revision")
        if isinstance(base_revision, bool) or not isinstance(base_revision, int):
            raise ApiError("invalid_argument", 400, "base_revision must be an integer")
        operations = payload.get("operations")
        if not isinstance(operations, list) or not operations:
            raise ApiError("invalid_argument", 400, "operations must be a non-empty list")
        for operation in operations:
            if not isinstance(operation, Mapping) or not operation.get("op") or not operation.get("section_id"):
                raise ApiError("invalid_argument", 400, "each operation needs op and section_id")
        try:
            result = self.console.revise(dict(payload))
        except _ConsoleError as exc:
            if exc.code == "revision_conflict":
                raise ApiError(
                    "revision_conflict", 409,
                    "base_revision changed; keep the draft and refresh",
                    extra=getattr(exc, "extra", {}),
                ) from exc
            raise ApiError(exc.code, exc.status, exc.message) from exc
        if result.get("status") == "rejected":
            raise ApiError("rejected", 422, str(result.get("reason") or "operation rejected"))
        self.service._refresh_chat_capability()
        return result

    def host_publish(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Publish the revision the host is showing; never a silently newer one."""
        revision = payload.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ApiError("invalid_argument", 400, "revision must be an integer")
        target = str(payload.get("target") or "next_turn")
        if target != "next_turn":
            # The host only owns the text turn; realtime/game consumers keep
            # their own publish boundaries.
            raise ApiError("invalid_argument", 400, "the host may only publish next_turn")
        result = self.console.publish({"target": target, "revision": revision})
        if result.get("conflict"):
            raise ApiError("revision_conflict", 409, "the card changed; refresh before publishing")
        if result.get("status") == "rejected":
            raise ApiError("rejected", 422, str(result.get("reason") or "publish rejected"))
        self.service._refresh_chat_capability()
        return result


    # -- limited assistant-output capability (M5) --------------------------

    @staticmethod
    def require_memory_outputs(identity: Mapping) -> None:
        """Refuse unless this identity may record/confirm assistant output.

        The grant is per client in stack.toml (memory_outputs = true). It is the
        fourth independent capability: recording a body and confirming delivery
        cannot touch the persona card, the console surface or another subject.
        """
        if not identity.get("memory_outputs"):
            raise ApiError("forbidden", 403, "this identity has no output capability")

    def output_record(self, payload: Mapping, identity: Mapping) -> dict:
        """Persist one generated assistant body (nothing is delivered yet).

        The body is stored for a later resume; the returned record says which
        parts still have to arrive. A caller can only speak for the subject it
        is scoped to, exactly like the other conversation capabilities.
        """
        self.require_memory_outputs(identity)
        scope = self._memory_scope(payload, identity)
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ApiError("invalid_argument", 400, "text is required")
        cause = payload.get("cause")
        if cause is not None and not isinstance(cause, Mapping):
            raise ApiError("invalid_argument", 400, "cause must be an object")
        part_sizes = payload.get("part_sizes")
        if part_sizes is not None and (
            not isinstance(part_sizes, list)
            or not all(isinstance(item, int) and not isinstance(item, bool)
                       for item in part_sizes)
        ):
            raise ApiError("invalid_argument", 400, "part_sizes must be a list of integers")
        request_id = payload.get("request_id")
        if request_id is not None and not isinstance(request_id, str):
            raise ApiError("invalid_argument", 400, "request_id must be a string")
        with self.console._lock:
            result = self.service.core.output_service.create(
                self.console.character,
                text=text,
                kind=str(payload.get("kind") or "notice"),
                subject=scope["subject"],
                scene=scope["scene"],
                session_id=scope["session_id"],
                client_id=identity.get("client_id", ""),
                episode_id=payload.get("episode_id"),
                source_key=payload.get("source_key"),
                cause=dict(cause or {}),
                request_id=(request_id or None),
                part_sizes=part_sizes,
            )
        if not result.ok:
            raise ApiError(
                "invalid_argument" if result.reason not in ("unknown_output",) else "not_found",
                400 if not result.conflict else 409,
                result.reason or "output rejected",
            )
        return result.to_dict(include_text=True)

    def output_deliver(self, payload: Mapping, identity: Mapping) -> dict:
        """Confirm which parts the platform accepted; those (and only those) land.

        The receipt is the only route into the shared conversation, so a
        duplicate report is a duplicate (no second line, no double count) and an
        interrupted delivery keeps the body for a retry without writing recent.
        """
        self.require_memory_outputs(identity)
        scope = self._memory_scope(payload, identity)
        output_id = str(payload.get("output_id") or "").strip()
        if not output_id:
            raise ApiError("invalid_argument", 400, "output_id is required")
        record = self.service.core.output_service.get(
            self.console.character, output_id, include_text=False,
        )
        if record is None or str(record.get("subject") or "") != scope["subject"]:
            raise ApiError("not_found", 404, "unknown output in this scope")
        parts = payload.get("parts")
        if parts is not None and not isinstance(parts, list):
            raise ApiError("invalid_argument", 400, "parts must be a list")
        kind = str(payload.get("kind") or "sent")
        with self.console._lock:
            result = self.service.core.output_service.deliver(
                self.console.character, output_id,
                parts=parts, platform_id=str(payload.get("platform_id") or ""),
                kind=kind, reason=payload.get("reason"),
            )
        if not result.ok:
            status = 409 if result.conflict else (
                404 if result.reason == "unknown_output" else 400
            )
            raise ApiError(result.reason or "delivery rejected", status,
                           result.reason or "delivery rejected")
        return result.to_dict(include_text=False)

    def output_view(self, query: Mapping[str, list[str]], identity: Mapping) -> dict:
        """List outputs or fetch one, always scoped to this subject."""
        self.require_memory_outputs(identity)
        scope = self._memory_scope({key: values[0] for key, values in query.items() if values}, identity)
        output_id = str((query.get("output_id") or [""])[0] or "").strip()
        character = self.console.character
        if output_id:
            payload = self.service.core.output_service.get(
                character, output_id, include_text=True,
            )
            if payload is None or str(payload.get("subject") or "") != scope["subject"]:
                raise ApiError("not_found", 404, "unknown output in this scope")
            return payload
        if str((query.get("pending") or [""])[0]).lower() in ("1", "true", "yes"):
            items = self.service.core.output_service.pending(character, limit=20)
            return {"subject": scope["subject"], "items": [
                item for item in items
                if str(item.get("subject") or "") == scope["subject"]
            ]}
        items = self.service.core.output_service.list(character, limit=20)
        return {"subject": scope["subject"], "items": [
            item for item in items
            if str(item.get("subject") or "") == scope["subject"]
        ]}

    def summary_view(
        self, path: str, query: Mapping[str, list[str]], identity: Mapping,
    ) -> dict:
        """The rebuildable working summary and the follow-up resolver.

        Read-only projections over the authoritative records: the same subject
        scoping as every other conversation route, and nothing cached is served.
        """
        body = {key: values[0] for key, values in query.items() if values}
        scope = self._memory_scope(body, identity)
        character = self.console.character
        if path == "/v1/working-summary":
            from .protocol import scene_object
            return self.service.core.working_summary_service.build(
                character, subject=scope["subject"],
                scene=asdict(scene_object(scope["scene"])) if scope["scene"] else None,
                language=str(body.get("language") or "zh"),
            ).to_dict()
        return self.service.core.working_summary_service.resolve_followup(
            character, str(body.get("text") or ""),
            subject=scope["subject"], scene=scope["scene"] or None,
            language=str(body.get("language") or "zh"),
        )

    def output_invalidate(self, payload: Mapping, identity: Mapping) -> dict:
        """Retire an output whose cause was deleted (body cleared, id kept)."""
        self.require_memory_outputs(identity)
        scope = self._memory_scope(payload, identity)
        output_id = str(payload.get("output_id") or "").strip()
        if not output_id:
            raise ApiError("invalid_argument", 400, "output_id is required")
        record = self.service.core.output_service.get(
            self.console.character, output_id, include_text=False,
        )
        if record is None or str(record.get("subject") or "") != scope["subject"]:
            raise ApiError("not_found", 404, "unknown output in this scope")
        with self.console._lock:
            result = self.service.core.output_service.invalidate(
                self.console.character, output_id,
                reason=str(payload.get("reason") or "source_deleted"),
            )
        return result.to_dict(include_text=False)

    @staticmethod
    def require_persona_teach(identity: Mapping) -> None:
        """Refuse unless this identity was granted the explicit-teaching role.

        The grant is per client in `stack.toml` (`persona_teach = true`), checked
        here *and* in the transport, so a routing mistake cannot widen a normal
        chat client into a persona editor. It is deliberately separate from
        `host_persona` (the desktop persona editor) and from `memory_tools`
        (the speaker's own facts): teaching writes the card, but only through the
        narrow rule surface below.
        """
        if not identity.get("persona_teach"):
            raise ApiError(
                "forbidden", 403,
                "this identity has no persona teaching capability",
            )

    #: Content classes a teaching may cite. Companion output is her own line,
    #: never the user teaching; game/plugin notices are not style requests.
    TEACHABLE_CLASSES = frozenset({
        "chat", "preference", "life_fact", "shared_experience",
    })

    def _saved_user_message(
        self, source_key: str, *, identity: Mapping | None = None,
        scope_subject: str | None = None,
    ) -> dict:
        """Find the saved user message a `/v1/persona/teach` call cites.

        The service reads the message itself; this is the security boundary that
        makes teaching different from "post me some rules". Sources come from the
        conversation journal (authoritative) and the recent window (what the
        host and plugins actually wrote). The returned text is the *stored* one,
        never a caller-supplied string.

        **Two source families, two rules** (M6-A):

        * a host source (`host:<character>:<subject>:<event_id>`) is this
          conversation's own record. Its path is unchanged: same character, the
          configured subject, and `plugin_id is None`;
        * a plugin source is that plugin's record, which used to be refused
          outright. It is now allowed only when *all* of the following hold --
          this identity holds `persona_teach`, the source subject is one of
          *this identity's* subjects, the source character is this service's
          character, and the plugin id is in *this identity's* trusted list.

        The four conditions are AND, never OR, and the trusted list is per
        client: a global allow-list would let any client with `persona_teach`
        cite every plugin's messages, which is exactly the cross-surface leak the
        old blanket refusal was protecting against. An unconfigured client gets
        an empty list, so **an upgrade alone changes nothing** -- the QQ path
        stays refused until someone grants a specific plugin to a specific
        client in `stack.toml`.
        """
        key = str(source_key or "").strip()
        if not key:
            raise ApiError("invalid_argument", 400, "source_key is required")
        from ..event_sources import parse_source_key

        parsed = parse_source_key(key)
        if parsed is None:
            raise ApiError("invalid_argument", 400, "source_key is not a saved message reference")
        if parsed.get("character") != self.console.character:
            raise ApiError("scope_mismatch", 403, "source is outside this character")
        if parsed.get("plugin_id") is None:
            # Host source: unchanged rule (this conversation, configured subject).
            if parsed.get("subject") != self.subject:
                raise ApiError("scope_mismatch", 403, "source is outside this subject")
        else:
            self._require_trusted_plugin_source(parsed, identity or {})
            if scope_subject is not None and str(parsed.get("subject")) != str(scope_subject):
                # The call declares one subject and cites a message from another.
                # Both may individually sit inside this identity's subjects (one
                # client can be scoped to several), so each gate alone would
                # pass; only the comparison catches it. A rule derived from
                # subject A's message must never land under subject B.
                raise ApiError(
                    "forbidden", 403, "source is outside this subject",
                    extra={
                        "source_subject": parsed.get("subject"),
                        "request_subject": str(scope_subject),
                    },
                )
        found = self._journal_message(key)
        if found is None:
            raise ApiError(
                "not_found", 404,
                "no saved user message with that source_key",
                extra={"source_key": key},
            )
        return found

    @staticmethod
    def _require_trusted_plugin_source(
        parsed: Mapping[str, Any], identity: Mapping,
    ) -> None:
        """Allow a plugin source only for its own, explicitly trusting identity.

        Kept as one routine so the four conditions cannot drift apart, and so the
        refusal reason names which one failed (an operator debugging a QQ setup
        needs to know whether the capability, the subject, the character or the
        allow-list is the blocker).
        """
        if not identity.get("persona_teach"):
            raise ApiError(
                "forbidden", 403,
                "this identity has no persona teaching capability",
            )
        subjects = [
            str(item) for item in identity.get("subjects") or () if isinstance(item, str)
        ]
        if str(parsed.get("subject") or "") not in subjects:
            raise ApiError(
                "scope_mismatch", 403,
                "source is outside this identity's subjects",
                extra={"source_subject": parsed.get("subject")},
            )
        plugin_id = str(parsed.get("plugin_id") or "")
        trusted = [
            str(item) for item in identity.get("trusted_plugins") or ()
            if isinstance(item, str) and item
        ]
        if plugin_id not in trusted:
            raise ApiError(
                "forbidden", 403,
                "source plugin is not trusted by this identity",
                extra={"plugin_id": plugin_id, "trusted_plugins": trusted},
            )

    def _journal_message(self, source_key: str) -> dict | None:
        """The authoritative saved turn for a source key, or None.

        The conversation window is checked first because it is the cheap, bounded
        read; the journal is the authority for a turn committed through
        `/v1/turns` or `/v1/chat` even after the window has been compressed
        away. A message covered by a deletion tombstone is not a usable source:
        teaching must not resurrect text the user deleted.
        """
        core = self.service.core
        barrier = getattr(core, "barrier", None)
        if barrier is not None and barrier.blocks_source_key(
            self.console.character, source_key,
        ):
            raise ApiError(
                "deleted", 410, "this source message was deleted",
                extra={"source_key": source_key},
            )
        try:
            messages = core.recent.messages(self.console.character)
        except Exception:  # noqa: BLE001 - a damaged window is not a saved turn
            messages = []
        for message in messages:
            if not isinstance(message, Mapping) or message.get("type") != "human":
                continue
            data = message.get("data") if isinstance(message.get("data"), Mapping) else {}
            metadata = data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}
            keys = metadata.get("source_event_keys")
            candidates = {str(item) for item in keys or () if isinstance(item, str)}
            single = metadata.get("source_key")
            if isinstance(single, str) and single:
                candidates.add(single)
            if source_key not in candidates:
                continue
            text = str(data.get("content") or "").strip()
            if not text:
                continue
            return {
                "source_key": source_key,
                "text": text,
                "content_class": str(metadata.get("content_class") or ""),
                "subject": str(metadata.get("subject") or self.subject),
                "origin": "recent",
            }
        # The window is bounded and gets compressed; the journal still holds the
        # turn, so a teaching from an older message keeps working.
        for entry in core.journal.read_since(self.console.character, -1):
            payload = entry.payload
            if not isinstance(payload, Mapping):
                continue
            if str(payload.get("source_key") or "") != source_key:
                continue
            text = str(payload.get("text") or "").strip()
            if not text:
                continue
            if barrier is not None and barrier.blocks_source_key(
                self.console.character, source_key,
            ):
                raise ApiError(
                    "deleted", 410, "this source message was deleted",
                    extra={"source_key": source_key},
                )
            scope = payload.get("scope")
            return {
                "source_key": source_key,
                "text": text,
                "content_class": str(payload.get("content_class") or ""),
                "subject": str(scope.get("subject") or self.subject)
                if isinstance(scope, Mapping) else self.subject,
                "origin": "journal",
            }
        return None

    def persona_teach(self, payload: Mapping[str, Any], identity: Mapping) -> dict:
        """Record one explicit teaching from a saved message, then publish.

        Request fields (no rule text accepted, by design):
          source_key      - the saved user message that states the request;
          base_revision   - the card revision the caller saw (CAS);
          subject         - optional, must stay inside the identity;
          quoted_example  - optional {source_key?|text?} the rule cites.
        Response: teaching_id / intent / revision / published_version / target /
        rules / status. A stale base revision is 409 with the current card.
        """
        self.require_persona_teach(identity)
        scope = self._memory_scope(payload, identity)
        base_revision = payload.get("base_revision")
        if isinstance(base_revision, bool) or not isinstance(base_revision, int):
            raise ApiError("invalid_argument", 400, "base_revision must be an integer")
        source = self._saved_user_message(
            str(payload.get("source_key") or ""), identity=identity,
            scope_subject=scope["subject"],
        )
        if source["subject"] != scope["subject"]:
            raise ApiError("forbidden", 403, "source is outside this subject")
        content_class = source["content_class"]
        if content_class and content_class not in self.TEACHABLE_CLASSES:
            raise ApiError(
                "forbidden", 403,
                "this saved message is not the user speaking",
                extra={"content_class": content_class},
            )
        example = payload.get("quoted_example")
        if example is not None and not isinstance(example, Mapping):
            raise ApiError("invalid_argument", 400, "quoted_example must be an object")
        quoted: dict[str, Any] = {}
        # The publish boundary is part of the contract, not a hint: teaching
        # changes the next generation only (realtime/game consumers have their
        # own boundaries in B11).
        target = str(payload.get("target") or "next_turn").strip()
        if target != "next_turn":
            raise ApiError(
                "rejected", 422,
                "explicit teaching publishes on the next_turn boundary only",
                extra={"reason": "invalid_target", "target": target},
            )
        if isinstance(example, Mapping):
            example_key = str(example.get("source_key") or "").strip()
            if example_key:
                # An authorised example must also be a saved message: a client
                # cannot paste the text it wants imitated.
                found = self._saved_user_message(
                    example_key, identity=identity,
                    scope_subject=scope["subject"],
                )
                quoted = {"source_key": found["source_key"], "text": found["text"]}
            else:
                raise ApiError(
                    "invalid_argument", 400,
                    "quoted_example needs a saved source_key",
                )
        receipt = self.service.persona_teaching.teach(self.console.character, {
            "source_key": source["source_key"],
            "source_text": source["text"],
            "content_class": content_class,
            "base_revision": base_revision,
            "subject": scope["subject"],
            "client_id": str(identity.get("client_id") or ""),
            "quoted_example": quoted or None,
            "target": "next_turn",
        })
        return self._teaching_view(receipt, "teach")

    def persona_teach_revoke(self, payload: Mapping[str, Any], identity: Mapping) -> dict:
        """Withdraw one teaching ("刚才那条别学了"). Other rules stay in place.

        Accepts `teaching_id` (preferred) or `source_key` (the message that
        taught it), plus the `base_revision` the caller saw.
        """
        self.require_persona_teach(identity)
        scope = self._memory_scope(payload, identity)
        base_revision = payload.get("base_revision")
        if isinstance(base_revision, bool) or not isinstance(base_revision, int):
            raise ApiError("invalid_argument", 400, "base_revision must be an integer")
        teaching_id = str(payload.get("teaching_id") or "").strip()
        source_key = str(payload.get("source_key") or "").strip()
        if not teaching_id and not source_key:
            raise ApiError(
                "invalid_argument", 400, "teaching_id or source_key is required",
            )
        if source_key:
            from ..event_sources import parse_source_key

            parsed = parse_source_key(source_key)
            if parsed is None or parsed.get("character") != self.console.character:
                raise ApiError("invalid_argument", 400, "source_key is not a saved message reference")
        receipt = self.service.persona_teaching.revoke(
            self.console.character, {
                "teaching_id": teaching_id,
                "source_key": source_key,
                "base_revision": base_revision,
                "subject": scope["subject"],
                "reason": str(payload.get("reason") or "user_requested"),
            },
        )
        return self._teaching_view(receipt, "revoke")

    @staticmethod
    def _teaching_view(receipt, action: str) -> dict:
        """Wire view of a teaching receipt; a conflict carries the current card."""
        card = receipt.card
        view = {
            "status": receipt.status,
            "action": action,
            "teaching_id": receipt.teaching_id,
            "intent": receipt.intent,
            "revision": int(receipt.revision or 0),
            "base_revision": int(receipt.base_revision or 0),
            "published_version": receipt.published_version,
            "target": receipt.target,
            "rules": [dict(item) for item in receipt.rules],
            "reason": receipt.reason,
            "duplicate": bool(receipt.duplicate),
            "conflict": bool(receipt.conflict),
        }
        if receipt.conflict or receipt.status == "conflict":
            current = None
            if card is not None:
                current = {
                    "character": card.character,
                    "revision": int(card.revision or 0),
                    "initial_revision": int(card.initial_revision or 0),
                    "sections": [
                        _section_view(section.to_mapping())
                        for section in card.sections
                    ],
                }
            raise ApiError(
                "revision_conflict", 409,
                "base_revision changed; keep the draft and refresh",
                extra={"current": current} if current else {},
            )
        if view["status"] == "rejected":
            raise ApiError(
                "rejected", 422,
                str(receipt.reason or "teaching rejected"),
                extra={
                    "reason": receipt.reason,
                    "teaching_id": receipt.teaching_id,
                },
            )
        return view

    def persona_teachings(self, query: Mapping[str, list[str]]) -> dict:
        """What this character has been taught, newest state per record."""
        character = self.console.character
        rows = self.service.persona_teaching.teachings(character)
        active = self.service.persona_teaching.rules(character)
        first = lambda key, default="": (query.get(key) or [default])[0]
        status = str(first("status") or "").strip()
        visible = [row for row in rows if not status or row.get("status") == status]
        return {
            "character_id": character,
            "card_revision": int(self.service.core.persona_card_service.get(character).revision or 0),
            "published_version": self.service.core.persona_publish.current(character).version,
            "active_rule_ids": [str(rule.get("rule_id") or "") for rule in active],
            "items": [
                {
                    "teaching_id": str(row.get("teaching_id") or ""),
                    "status": str(row.get("status") or ""),
                    "intent": str(row.get("intent") or ""),
                    "source_key": str(row.get("source_key") or ""),
                    "rules": [
                        {"rule_id": str(rule.get("rule_id") or ""),
                         "text": str(rule.get("text") or "")}
                        for rule in row.get("rules") or ()
                    ],
                    "revision": int(row.get("revision") or 0),
                    "published_version": str(row.get("published_version") or ""),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "revoked_at": row.get("revoked_at"),
                    "superseded_at": row.get("superseded_at"),
                }
                for row in visible
            ],
        }
    def protocol_get(self, path: str, body: Mapping, *, identity: Mapping | None = None):
        core = self.service.core
        if path == "/v1/outputs":
            return self.output_view(
                {key: [str(value)] for key, value in body.items()}, identity or {},
            )
        if path == "/v1/receipts":
            return self.service.protocol.receipts(self.service.protocol.scope(body))
        if path == "/v1/receipts/summary":
            # M6-A: adoption vs actual delivery for one request, from the
            # records themselves. A read view; it writes nothing and adds no
            # field to the existing /v1/receipts rows.
            return self.service.protocol.receipt_summary(
                self.service.protocol.scope(body),
                str(body.get("request_id") or ""),
            )
        if path == "/v1/cleanup/pending":
            plugin = str(body.get("plugin_id") or "")
            if not plugin:
                raise ApiError("invalid_argument", 400, "plugin_id is required")
            return {"items": core.deletion_service.pending_plugin_deletions(self.console.character, plugin)}
        if path.startswith("/v1/cleanup/receipts/"):
            receipt = core.deletion_service.get(self.console.character, path.rsplit("/", 1)[1])
            if receipt is None:
                raise ApiError("not_found", 404, "unknown deletion")
            return self.console._deletion_receipt_view(receipt)
        return None

    # -- limited memory tools for a chat client (DM04) -------------------

    @staticmethod
    def require_memory_tools(identity: Mapping) -> None:
        """Refuse unless this identity was granted the conversation memory tools.

        The grant is per client in `stack.toml` (`memory_tools = true`). It is
        deliberately separate from `host_persona`: a chat surface may let a
        speaker correct or forget their own facts without gaining any authority
        over the persona card or the console surface.
        """
        if not identity.get("memory_tools"):
            raise ApiError("forbidden", 403, "this identity has no memory tool capability")

    def _memory_scope(self, payload: Mapping, identity: Mapping) -> dict:
        """Bind a memory-tool call to the authenticated client and one subject."""
        subject = str(payload.get("subject") or (identity.get("subjects") or ["private"])[0])
        if subject not in (identity.get("subjects") or []):
            raise ApiError("scope_mismatch", 403, "subject is outside the client identity")
        return {"subject": subject, "scene": str(payload.get("scene") or ""),
                "session_id": str(payload.get("session_id") or "")}

    def memory_entries(self, payload: Mapping, identity: Mapping) -> dict:
        """What this speaker could forget, already filtered to their subject."""
        self.require_memory_tools(identity)
        scope = self._memory_scope(payload, identity)
        text = payload.get("text")
        if text is not None and not isinstance(text, str):
            raise ApiError("invalid_argument", 400, "text must be a string")
        if not self.service.settings.memory_enabled:
            return {"subject": scope["subject"], "items": [], "reason": "memory_paused"}
        result = self.service.core.deletion_service.deletable_entries(
            self.console.character, query=(text or "").strip(),
            types=("fact", "reflection"), limit=20, subject=scope["subject"],
        )
        return {"subject": scope["subject"], "items": list(result.get("items") or ())}

    def memory_correct(self, payload: Mapping, identity: Mapping) -> dict:
        """Record the user's own correction from inside the conversation.

        The rules stay in the core (correction engine, barrier, idempotency);
        this endpoint only restricts who may ask and for which subject. A
        correction always lands in the shared scope so it reaches the web
        conversation as well as this QQ one — a personal preference is not a
        plugin-local fact.
        """
        self.require_memory_tools(identity)
        scope = self._memory_scope(payload, identity)
        old_text = payload.get("old_text")
        new_text = payload.get("new_text")
        for name, value in (("old_text", old_text), ("new_text", new_text)):
            if value is not None and not isinstance(value, str):
                raise ApiError("invalid_argument", 400, f"{name} must be a string")
        if not (old_text or "").strip() and not (new_text or "").strip():
            raise ApiError("invalid_argument", 400, "old_text or new_text is required")
        request_id = payload.get("request_id")
        if request_id is not None and not isinstance(request_id, str):
            raise ApiError("invalid_argument", 400, "request_id must be a string")
        from rin_memory.contracts import CorrectionRequest, Scope

        with self.console._lock:
            receipt = asyncio.run(self.service.core.correction_service.correct(
                CorrectionRequest(
                    scope=Scope(self.console.character, scope["subject"]),
                    action="replace",
                    old_text=(old_text or "").strip(),
                    new_text=(new_text or "").strip(),
                    request_id=(request_id or None),
                ),
            ))
        return {
            "correction_id": receipt.correction_id,
            "version": receipt.version,
            "applied": bool(receipt.applied),
            "duplicate": bool(receipt.duplicate),
            "status": receipt.status,
            "invalidated_fact_ids": list(receipt.invalidated_fact_ids),
            "replacement_fact_id": receipt.replacement_fact_id,
        }

    def memory_forget(self, payload: Mapping, identity: Mapping) -> dict:
        """Delete exactly what the speaker named, in their own subject only.

        `target` (type+id) is the precise form; free `text` is resolved against
        the subject's own entries, and several matches are reported instead of
        guessed. Nothing here reaches another subject, another plugin's keys or
        the whole store.
        """
        self.require_memory_tools(identity)
        scope = self._memory_scope(payload, identity)
        from rin_memory.contracts import DeletionRequest, DeletionTarget, Scope

        target = payload.get("target")
        source_key = payload.get("source_key")
        text = payload.get("text")
        episode_id = payload.get("episode_id")
        for name, value in (("source_key", source_key), ("text", text), ("episode_id", episode_id)):
            if value is not None and not isinstance(value, str):
                raise ApiError("invalid_argument", 400, f"{name} must be a string")
        targets: list[DeletionTarget] = []
        if isinstance(target, Mapping):
            target_type = str(target.get("type") or "")
            target_id = str(target.get("id") or "")
            if target_type not in ("fact", "reflection"):
                raise ApiError("invalid_argument", 400, "target.type must be fact or reflection")
            if not target_id:
                raise ApiError("invalid_argument", 400, "target.id is required")
            if not self.service.core.deletion_service.owns_entry(
                self.console.character, scope["subject"], target_type, target_id,
            ):
                raise ApiError("forbidden", 403, "target is outside this subject")
            targets.append(DeletionTarget(target_type, target_id))
        if (episode_id or "").strip():
            raise ApiError("forbidden", 403, "episode deletion requires the management interface")
        if (source_key or "").strip():
            from ..event_sources import parse_source_key

            parsed = parse_source_key(source_key.strip()) or {}
            if (parsed.get("character") != self.console.character
                    or parsed.get("subject") != scope["subject"]
                    or parsed.get("plugin_id") != identity.get("plugin_id")):
                raise ApiError("forbidden", 403, "source is outside this client identity")
        if not targets and not (source_key or "").strip() and not (episode_id or "").strip():
            needle = (text or "").strip()
            if not needle:
                raise ApiError("invalid_argument", 400, "target, source_key, episode_id or text is required")
            listed = self.service.core.deletion_service.deletable_entries(
                self.console.character, query=needle,
                types=("fact", "reflection"), limit=8, subject=scope["subject"],
            )
            matches = [item for item in listed.get("items") or ()
                       if needle in str(item.get("text") or "")]
            if not matches:
                raise ApiError("not_found", 404, "nothing in this conversation's memory matches that")
            if len(matches) > 1:
                # Several candidates is an answer, not a failure: the caller
                # repeats the question instead of deleting the wrong row.
                raise ApiError(
                    "ambiguous_target", 409,
                    "several memories match; ask which one",
                    extra={"candidates": [
                        {"type": item.get("type"), "id": item.get("id"),
                         "text": str(item.get("text") or "")[:200]}
                        for item in matches
                    ]},
                )
            targets.append(DeletionTarget(str(matches[0]["type"]), str(matches[0]["id"])))

        request = DeletionRequest(
            scope=Scope(self.console.character, scope["subject"]),
            mode="episode" if (episode_id or "").strip() else "entry",
            targets=tuple(targets),
            source_keys=((source_key or "").strip(),) if (source_key or "").strip() else (),
            episode_ids=((episode_id or "").strip(),) if (episode_id or "").strip() else (),
            reason="user_request_in_conversation",
        )
        with self.console._lock:
            receipt = asyncio.run(self.service.core.deletion_service.forget(request))
        view = self.console._deletion_receipt_view(receipt)
        view["subject"] = scope["subject"]
        return view

    def protocol_post(self, path: str, payload: Mapping, *, identity: Mapping | None = None):
        core = self.service.core
        if path == "/v1/segment/note":
            from ..domain.segment_note import build_segment_note
            from .protocol import scene_object
            scope = self.service.protocol.scope(payload)
            basis = self.service.protocol.basis()
            summary = core.working_summary_service.build(
                self.console.character, subject=scope["subject"],
                scene=scene_object(scope["scene"]),
            )
            threads = [dict(row, id=row.get("thread_id"),
                            source_key=next(iter(row.get("source_keys") or ()), ""))
                       for row in summary.section("open_threads").items
                       if not row.get("mention_restricted")]
            note = build_segment_note(threads, switched_at=payload.get("switched_at"),
                                      last_user_at=payload.get("last_user_at"))
            if basis != self.service.protocol.basis():
                raise ApiError("context_stale", 409, "memory changed while the note was being built")
            return note.as_dict() if note else {"text": "", "refs": []}
        if path == "/v1/outputs":
            return self.output_record(payload, identity or {})
        if path == "/v1/outputs/deliver":
            return self.output_deliver(payload, identity or {})
        if path == "/v1/outputs/invalidate":
            return self.output_invalidate(payload, identity or {})
        if path == "/v1/context/reserve":
            return self.service.protocol.reserve(payload)
        if path == "/v1/context/consume":
            self.require_memory_outputs(identity or {})
            return self.service.protocol.consume(payload, identity=identity)
        if path == "/v1/context/validate":
            context = self.service.protocol.validate(str(payload.get("context_receipt_id") or ""), self.service.protocol.scope(payload))
            return {"status": "valid", "context_receipt_id": context["context_receipt_id"], "persona_version": context["persona_version"]}
        if path == "/v1/events":
            return asyncio.run(self.service.protocol.event(payload))
        if path == "/v1/receipts":
            return self.service.protocol.receipt(payload)
        if path == "/v1/maintenance":
            result = asyncio.run(core.maintenance.run_once(self.console.character))
            return asdict(result)
        if path == "/v1/cleanup/retry":
            force = payload.get("force", False)
            if not isinstance(force, bool):
                raise ApiError("invalid_argument", 400, "force must be a boolean")
            receipt = asyncio.run(core.deletion_service.retry(self.console.character, str(payload.get("deletion_id") or ""), force=force))
            return self.console._deletion_receipt_view(receipt)
        if path == "/v1/cleanup/retract-correction":
            receipt = asyncio.run(core.correction_service.retract(self.console.character, str(payload.get("correction_id") or "")))
            return asdict(receipt)
        if path.startswith("/v1/cleanup/") and path.endswith("/ack"):
            deletion_id = path.split("/")[3]
            status = payload.get("status")
            if status == "completed":
                status = "cleaned"
            if status not in ("cleaned", "failed") or not payload.get("plugin_id"):
                raise ApiError("invalid_argument", 400, "plugin_id and a valid status are required")
            receipt = asyncio.run(core.deletion_service.record_plugin_receipt(
                self.console.character, deletion_id, payload["plugin_id"], status,
                cleaned=payload.get("cleaned", []), failed=payload.get("failed", [])))
            return self.console._deletion_receipt_view(receipt)
        return None

    def qq_persona(self, payload: Mapping[str, Any] | None = None) -> dict:
        """An independently edited public voice for QQ friend private chats."""
        core = self.service.core
        path = core.roots.character_file(self.console.character, "qq_public_persona.json")
        with self.console._lock:
            current = core.store.load(path) or {"text": "", "revision": 0}
            if payload is None:
                return dict(current)
            text, revision = payload.get("text"), payload.get("base_revision")
            if not isinstance(text, str) or len(text) > 8000 or type(revision) is not int:
                raise ApiError("invalid_argument", 400, "text and base_revision are required")
            if current["revision"] != revision:
                raise ApiError("revision_conflict", 409, "the public QQ persona changed")
            text = text.strip()
            if text == current["text"]:
                return dict(current)
            updated = {"text": text, "revision": revision + 1}
            core.permit.assert_writable("qq_public_persona", str(path))
            core.store.save(path, updated)
            return updated

    def get(self, path: str, query: Mapping[str, list[str]], *, identity: Mapping | None = None) -> Any:
        if path == "/v1/qq/persona":
            return self.qq_persona()
        if path == "/v1/host/persona":
            self.require_host_persona(identity or {})
            return self.host_persona()
        # The conversation memory tools are capability routes like the host
        # persona one: dispatched before the width check and re-checked here,
        # so a routing mistake cannot hand them to a normal client.
        if path == "/v1/memory/entries":
            return self.memory_entries({key: values[0] for key, values in query.items() if values},
                                       identity or {})
        # Teaching is a capability route like the host persona one: dispatched
        # before the width check and re-checked inside the handler.
        if path == "/v1/persona/teachings":
            self.require_persona_teach(identity or {})
            return self.persona_teachings(query)
        if path == "/v1/outputs":
            return self.output_view(query, identity or {})
        if path in ("/v1/working-summary", "/v1/followup"):
            return self.summary_view(path, query, identity or {})
        self.check_scope({key: values[0] for key, values in query.items() if values})
        result = self.protocol_get(path, {key: values[0] for key, values in query.items() if values})
        if result is not None:
            return result
        if path == "/v1/initialization":
            return self.initialization()
        if path == "/v1/bootstrap":
            return self.bootstrap()
        if path == "/v1/export":
            return self.console.export()[0]
        if path == "/v1/engine/status":
            return dict(self.console._engine_status(),
                        capabilities=self.service.state.to_dict(),
                        config_missing=self.config_missing)
        return self.console.get("/api/" + path.removeprefix("/v1/"), query)

    def post(self, path: str, payload: Mapping[str, Any], *, identity: Mapping | None = None) -> dict[str, Any]:
        if path == "/v1/qq/persona":
            if identity is not None and not identity.get("admin"):
                raise ApiError("forbidden", 403, "this operation requires the console identity")
            return self.qq_persona(payload)
        if path == "/v1/outputs":
            return self.output_record(payload, identity or {})
        if path == "/v1/outputs/deliver":
            return self.output_deliver(payload, identity or {})
        if path == "/v1/outputs/invalidate":
            return self.output_invalidate(payload, identity or {})
        if path in ("/v1/host/persona", "/v1/host/persona/publish"):
            self.require_host_persona(identity or {})
            self.check_scope(payload)
            if path == "/v1/host/persona":
                return self.host_revise(payload)
            return self.host_publish(payload)
        if path in ("/v1/memory/correct", "/v1/memory/forget"):
            if path == "/v1/memory/correct":
                return self.memory_correct(payload, identity or {})
            return self.memory_forget(payload, identity or {})
        if path in ("/v1/persona/teach", "/v1/persona/teach/revoke"):
            self.require_persona_teach(identity or {})
            self.check_scope(payload)
            if path == "/v1/persona/teach":
                return self.persona_teach(payload, identity or {})
            return self.persona_teach_revoke(payload, identity or {})
        self.check_scope(payload)
        result = self.protocol_post(path, payload)
        if result is not None:
            return result
        if path == "/v1/characters":
            return self.initialize(payload)
        if path in ("/v1/demo/reset", "/v1/persona/sync"):
            raise ApiError("not_found", 404, "route is not part of the standalone service")
        with self.console._lock:
            result = self.console.post("/api/" + path.removeprefix("/v1/"), payload)
        if result.get("conflict"):
            raise ApiError("revision_conflict", 409, "base_revision changed; keep the draft and refresh")
        if result.get("status") == "rejected":
            raise ApiError("rejected", 422, str(result.get("reason") or "operation rejected"))
        self.service._refresh_chat_capability()
        return result
