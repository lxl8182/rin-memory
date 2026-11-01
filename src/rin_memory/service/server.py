"""The `/v1` JSON interface (S04): stdlib HTTP, no framework dependency.

`docs/standalone-service.md` §3.1 defines the minimum set:

    POST /v1/chat          service generates a reply and commits the turn
    POST /v1/turns         client already generated; save and enqueue only
    POST /v1/context       read persona + recent + recall; never writes
    GET  /v1/requests/{id} progress or the committed result of one request

plus `GET /v1/health` for §4.2's capability states.

Transport choices, and why:

* `http.server.ThreadingHTTPServer` keeps the core's "zero runtime dependency"
  promise; the plan explicitly allows a stdlib server provided it has bounded
  concurrency and real deadlines, which the thread pool and per-request timeout
  give;
* every response is JSON with the §3.2 error envelope
  `{code, message, retryable, request_id}`;
* the default bind is loopback and `Host`/`Origin` are checked, per §4.3
  ("default listen on loopback; also check Host/Origin"). A public deployment
  needs the separate auth/TLS design the plan defers.

One business rule lives here because it is a transport concern: a `/chat`
request whose capability is not ready is refused *before* any record is
reserved, so a failed capability check never leaves a pending turn behind.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import time
from importlib.resources import files
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from ..adapters.model_http import ModelCallError
from ..domain.turns import TurnCommitService, TurnRequest, TurnOutcome
from ..persistence.turns import TurnRecord
from .listening import ExclusiveAddrMixin
from .memory_service import (
    MemoryService,
    ServiceClosing,
    ServiceStartupError,
)

DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024
_MAX_IN_FLIGHT_CHAT = 4

#: Error codes and their HTTP status (§3.2).
_STATUS_FOR_CODE = {
    "invalid_argument": 400,
    "not_found": 404,
    "service_closing": 503,
    "unauthorized": 401,
    "forbidden": 403,
    "request_conflict": 409,
    "turn_order_conflict": 409,
    "context_stale": 409,
    "turn_in_progress": 202,
    "deleted": 410,
    "capacity": 429,
    "storage_unavailable": 503,
    "chat_unavailable": 503,
    "model_timeout": 504,
}

_RETRYABLE = frozenset({
    "turn_in_progress", "capacity", "storage_unavailable", "chat_unavailable",
    "model_timeout", "context_stale",
})


class ApiError(Exception):
    """A stable JSON error with an HTTP status and a machine code."""

    def __init__(
        self, code: str, message: str, *, request_id: str | None = None,
        retryable: bool | None = None, extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id
        self.status = _STATUS_FOR_CODE.get(code, 500)
        self.retryable = (
            code in _RETRYABLE if retryable is None else bool(retryable)
        )
        self.extra = dict(extra or {})

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "request_id": self.request_id,
        }
        if self.code == "context_stale":
            payload["needs_reissue"] = True
        payload.update(getattr(self, "extra", {}) or {})
        return payload


# -- request parsing ---------------------------------------------------------


def _require_str(body: Mapping[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ApiError("invalid_argument", f"{name} must be a nonempty string")
    return value.strip()


def _optional_str(body: Mapping[str, Any], name: str) -> str:
    value = body.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError("invalid_argument", f"{name} must be a string")
    return value.strip()


def _messages(body: Mapping[str, Any], *, required: bool = True) -> list[dict]:
    raw = body.get("messages")
    if raw is None:
        if required:
            raise ApiError("invalid_argument", "messages is required")
        return []
    if not isinstance(raw, list) or not raw:
        raise ApiError("invalid_argument", "messages must be a nonempty list")
    out: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ApiError(
                "invalid_argument", f"messages[{index}] must be an object",
            )
        role = item.get("role")
        if not isinstance(role, str) or role not in (
            "user", "human", "assistant", "ai", "system",
        ):
            raise ApiError(
                "invalid_argument",
                f"messages[{index}].role must be user/assistant/system",
            )
        content = item.get("content")
        if not isinstance(content, str):
            raise ApiError(
                "invalid_argument",
                f"messages[{index}].content must be a string",
            )
        core_role = "human" if role in ("user", "human") else (
            "ai" if role in ("assistant", "ai") else "system"
        )
        out.append({"type": core_role, "data": {"content": content}})
    return out


def _build_turn_request(
    service: MemoryService, body: Mapping[str, Any],
    scope: Mapping[str, Any] | None = None,
) -> TurnRequest:
    """One turn request, with the caller's subject/scene when supplied (R03).

    A missing subject stays `private`; it is never widened to "any subject",
    and a scene the caller did not state stays empty so recall applies its
    documented rule (unrestricted rows only).
    """
    scope = dict(scope or {})
    return TurnRequest(
        character=service.settings.character,
        request_id=_require_str(body, "request_id"),
        turn_id=_require_str(body, "turn_id"),
        source_key=_require_str(body, "source_key"),
        content_class=_optional_str(body, "content_class") or "chat",
        messages=_messages(body),
        subject=(
            _optional_str(body, "subject")
            or str(scope.get("subject") or "")
            or "private"
        ),
        session_id=(
            _optional_str(body, "session_id")
            or str(scope.get("session_id") or "")
        ),
        episode_id=_optional_str(body, "episode_id"),
        parent_turn_id=_optional_str(body, "parent_turn_id"),
        persona_version=_optional_str(body, "persona_version"),
        card_revision=int(body.get("card_revision") or 0),
        scene=_optional_str(body, "scene") or str(scope.get("scene") or ""),
        applies_to=_optional_str(body, "applies_to"),
        occurred_at=_optional_str(body, "occurred_at"),
        client_id=str(scope.get("client_id") or ""),
        context_receipt_id=_optional_str(body, "context_receipt_id"),
    )


# -- the API surface ---------------------------------------------------------


class MemoryApi:
    """Transport-free implementation of the `/v1` contract.

    Kept separate from the HTTP handler so tests drive the exact same code path
    the server does, and so a second transport (the WebUI) can reuse it.
    """

    def __init__(
        self,
        service: MemoryService,
        *,
        max_in_flight_chat: int = _MAX_IN_FLIGHT_CHAT,
    ) -> None:
        self._service = service
        if not hasattr(service, "_chat_slots"):
            service._chat_slots = threading.BoundedSemaphore(max(1, max_in_flight_chat))
        self._chat_slots = service._chat_slots
        self.protocol = service.protocol
        self._turns: dict[str, TurnCommitService] = {}
        #: Per-thread request scope. The HTTP server is threaded, and the turn
        #: service's generator/basis callbacks are invoked inside the request
        #: that owns them, so thread-local is the correct carrier: sharing one
        #: mutable slot across concurrent requests would let one request's
        #: subject leak into another's generation or replay (R03).
        self._scope = threading.local()
        self._turn_service()

    # -- request scope (R03) --------------------------------------------

    def _begin_scope(self, **scope: Any) -> dict[str, Any]:
        current = {
            "subject": str(scope.get("subject") or "private"),
            "scene": str(scope.get("scene") or ""),
            "session_id": str(scope.get("session_id") or ""),
            "client_id": str(scope.get("client_id") or ""),
        }
        self._scope.value = current
        self._scope.model_ms = 0
        return current

    def _end_scope(self) -> None:
        self._scope.value = None
        self._scope.context_result = None
        self._scope.basis = None
        self._scope.generation = None
        self._scope.model_error = None
        self._scope.model_ms = 0

    def _current_scope(self) -> dict[str, Any]:
        value = getattr(self._scope, "value", None)
        if isinstance(value, dict):
            return value
        return {"subject": "private", "scene": "", "session_id": "", "client_id": ""}

    def _scene_object(self) -> Any:
        """Build a core `Scene` from the request scope, or None when unscoped."""
        scope = self._current_scope()
        from ..contracts import Scene

        scene_text = scope.get("scene") or ""
        session = scope.get("session_id") or ""
        if not scene_text and not session:
            return None
        if not scene_text:
            return Scene(session_id=session)
        # `scene` is documented as "kind:value" (game:sts2, plugin:sts2_autoplay,
        # host:chat); anything else is treated as a session id so an unknown
        # label can never silently widen the scope.
        kind, _, value = scene_text.partition(":")
        if kind == "game" and value:
            return Scene(game_id=value)
        if kind == "plugin" and value:
            return Scene(plugin_id=value)
        if kind == "host" and value:
            return Scene(session_id=value)
        return Scene(session_id=scene_text)

    # -- helpers ---------------------------------------------------------

    def _turn_service(self) -> TurnCommitService:
        character = self._service.settings.character
        service = self._turns.get(character)
        if service is None:
            core = self._service.core
            service = TurnCommitService(
                turns=core.turn_requests,
                journal=core.journal,
                permit=core.permit,
                clock=core.clock,
                character=character,
                ingest=core.extraction.ingest_turn,
                generator=self._generate,
                basis_check=lambda name: not self._basis_moved(name),
            )
            self._turns[character] = service
        return service

    def _basis_snapshot(self, character: str) -> dict[str, int]:
        """Deletion/correction versions a generation is about to rely on."""
        return self.protocol.basis()

    def _basis_moved(self, character: str) -> bool:
        """Whether a deletion/correction landed during this turn (§3.3, R01).

        Compares the barrier versions captured before generation against the
        current ones. This used to return a constant `False`, so a deletion
        that landed mid-generation was invisible and the stale reply was
        committed anyway.
        """
        generation = getattr(self._scope, "generation", None)
        if generation is not None:
            self._service.assert_current(generation)
        captured = getattr(self._scope, "basis", None)
        if not isinstance(captured, dict):
            return False
        current = self._basis_snapshot(character)
        return any(
            int(current.get(key, 0)) != int(captured.get(key, 0))
            for key in ("deletion_version", "correction_version")
        )

    async def _generate(self, messages) -> str:
        """Compile persona + memory context and call the conversation tier.

        R05: this used to send only the persona card and the raw input, so the
        model never saw the user's memories, corrections or recent turns --
        `/chat` was a stateless chatbot wearing a persona. The turn context is
        now built first (with the request's own subject/scene, R03) and its
        rendered block is prepended to the system message.

        R06: a *recoverable* model failure (timeout, rate limit, transport) no
        longer closes the chat capability permanently. Only a configuration or
        credential problem does, because retrying those cannot succeed.
        """
        model = self._service.model
        if model is None:
            raise ModelCallError(
                "no conversation model is configured", kind="config",
                retryable=False,
            )
        context = self._scope.context_result
        persona_text = context["persona"]
        memory_block = context["context"].get("context") or ""
        corrections = context["corrections"]["block"]
        awareness = (context.get("memory_awareness") or {}).get("text", "")
        system = "\n\n".join(part for part in (persona_text, awareness, corrections, memory_block) if part)
        history = [
            {"role": "assistant" if item.get("type") == "ai" else "user",
             "content": str((item.get("data") or {}).get("content") or "")}
            for item in [*context["recent_turns"], *messages]
            if isinstance(item, Mapping)
        ]
        payload = [{"role": "system", "content": system}, *history]
        started = time.perf_counter()
        try:
            reply = await model.invoke(
                "conversation", payload,
                max_output_tokens=1024,
                timeout_s=self._service.settings.chat_timeout_s,
                purpose="chat_reply",
            )
        except ModelCallError as exc:
            self._scope.model_error = exc
            if not exc.retryable:
                # config/auth: retrying cannot help, so the capability closes
                # until an operator fixes it.
                self._service.state.degrade_chat(f"model: {exc.kind}")
            else:
                # timeout/rate_limit/transport: report degraded-but-retryable
                # and leave chat_ready alone so the next request can succeed
                # without restarting the service.
                self._service.state.last_model_error = exc.kind
            raise
        finally:
            self._scope.model_ms = (time.perf_counter() - started) * 1000
        # A successful call clears any previous transient error.
        self._service.state.last_model_error = None
        if self._service.state.chat_reason == "model: timeout":
            self._service._refresh_chat_capability()
        return reply.strip()

    async def _memory_context_block(
        self, messages, scope: Mapping[str, Any],
    ) -> str:
        """Rendered memory context for this turn, or "" when there is none.

        Failures degrade to an empty block instead of failing the turn: §3.3
        says optional recall keeps the required content and reports a degraded
        status, and a turn is still worth answering without its memories.
        """
        core = self._service.core
        character = self._service.settings.character
        current_input = ""
        for item in reversed(list(messages)):
            if isinstance(item, Mapping) and item.get("type") == "human":
                data = item.get("data")
                if isinstance(data, Mapping):
                    current_input = str(data.get("content") or "")
                    break
        if not current_input:
            return ""
        try:
            context = await core.recall.build_turn_context(
                character,
                current_input=current_input,
                subject=scope.get("subject") or None,
                scene=self._scene_object(),
                budget_ms=self._service.settings.context_budget_ms,
            )
        except Exception:  # noqa: BLE001 - recall is optional for a reply
            return ""
        block = context.get("context")
        return block.strip() if isinstance(block, str) else ""

    # -- endpoints -------------------------------------------------------

    def health(self) -> dict[str, Any]:
        state = self._service.state.to_dict()
        state["background"] = self._service.background_status()
        state["character"] = self._service.settings.character
        if getattr(self, "instance_id", ""):
            state["instance_id"] = self.instance_id
            state["management"] = {"status": "ready"}
        return state

    async def context(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """§3.1 `/v1/context`: read-only; never records a mention or a send.

        R03: the caller's `subject`/`scene`/`session_id` are carried into the
        context build. This endpoint used to read the text and nothing else, so
        a group-scoped request was served the private subject's memories.
        """
        service = self._service
        if not service.state.storage_ready:
            raise ApiError(
                "storage_unavailable",
                service.state.storage_reason or "storage is not ready",
            )
        self._validate_character(body)
        scope = self.protocol.scope(body)
        from .protocol import scene_object
        basis = self.protocol.basis()
        recall = await service.core.recall.build_turn_context(
            service.settings.character, current_input=_optional_str(body, "text"),
            recent_turns=body.get("recent_turns"), subject=scope["subject"],
            scene=scene_object(scope["scene"]), budget_ms=service.settings.context_budget_ms,
        )
        if basis != self.protocol.basis():
            raise ApiError("context_stale", "memory changed while context was being built")
        return self.protocol.issue(scope, recall, basis=basis)

    def _validate_character(self, body):
        if body.get("character_id", self._service.settings.character) != self._service.settings.character:
            raise ApiError("forbidden", "character_id differs from this service")

    def _check_record_scope(self, record, scope):
        if record and any(getattr(record, key) != scope[key] for key in ("subject", "client_id", "session_id", "scene")):
            raise ApiError("not_found", "unknown request in this scope")

    async def chat(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Generate once, commit once, and replay independently of model availability."""
        service = self._service
        self._validate_character(body)
        scope = self.protocol.scope(body)
        request = _build_turn_request(service, body, scope)
        existing = service.core.turn_requests.get(service.settings.character, request.request_id)
        self._check_record_scope(existing, scope)
        if existing and existing.status in ("committed", "deleted"):
            generation = self._admit()
            try:
                return self.protocol.used(scope, _outcome_payload(await self._turn_service().commit_generated(request)))
            finally:
                service.end_request(generation)
        self.protocol.check_capacity(request.source_key, scope)
        if not service.state.chat_ready:
            raise ApiError("chat_unavailable", service.state.chat_reason or "chat is not available")
        if not self._chat_slots.acquire(blocking=False):
            raise ApiError("capacity", "too many chat requests in flight")
        generation = None
        try:
            generation = self._admit()
            with self.protocol.ingest_slot(request.source_key, scope), self.protocol.session(scope):
                context_started = time.perf_counter()
                text = next((str((m.get("data") or {}).get("content") or "")
                             for m in reversed(list(request.messages)) if m.get("type") == "human"), "")
                context = await self.context(dict(scope, text=text))
                context_ms = (time.perf_counter() - context_started) * 1000
                self._begin_scope(**scope)
                self._scope.context_result = context
                self._scope.basis = context["basis"]
                self._scope.generation = generation
                from dataclasses import replace
                request = replace(request, persona_version=context["persona_version"],
                    card_revision=context["card_revision"], context_receipt_id=context["context_receipt_id"])
                commit_started = time.perf_counter()
                outcome = await self._turn_service().commit_generated(request)
                self._check_generation(generation)
                model_error = getattr(self._scope, "model_error", None)
                if outcome.status == "failed" and model_error is not None:
                    raise ApiError("model_timeout" if model_error.kind == "timeout" else "chat_unavailable",
                                   "model call failed: " + model_error.kind, retryable=model_error.retryable,
                                   request_id=request.request_id)
                payload = _outcome_payload(outcome)
                payload["context_receipt_id"] = context["context_receipt_id"]
                payload = self.protocol.used(scope, payload)
                model_ms = getattr(self._scope, "model_ms", 0)
                payload["timings"] = {"context_ms": round(context_ms, 3), "model_ms": round(model_ms, 3),
                    "commit_ms": round(max(0, (time.perf_counter() - commit_started) * 1000 - model_ms), 3), "queue_ms": 0}
                return payload
        finally:
            self._end_scope()
            self._chat_slots.release()
            if generation is not None:
                service.end_request(generation)

    async def turns(self, body: Mapping[str, Any]) -> dict[str, Any]:
        service = self._service
        self._validate_character(body)
        generation = self._admit()
        try:
            scope = self._begin_scope(**self.protocol.scope(body))
            request = _build_turn_request(service, body, scope)
            existing = service.core.turn_requests.get(service.settings.character, request.request_id)
            self._check_record_scope(existing, scope)
            if not existing or existing.status not in ("committed", "deleted"):
                self.protocol.check_capacity(request.source_key, scope)
            reply = body.get("reply")
            if reply is not None and not isinstance(reply, str):
                raise ApiError("invalid_argument", "reply must be a string")
            if existing and existing.is_committed and reply is not None and existing.reply != reply:
                raise ApiError("request_conflict", "external reply changed under the same request_id")
            context = None
            if not existing or existing.status not in ("committed", "deleted"):
                if request.context_receipt_id:
                    context = self.protocol.validate(request.context_receipt_id, scope, request_id=request.request_id)
                    from dataclasses import replace
                    request = replace(request, persona_version=context["persona_version"], card_revision=context["card_revision"])
                elif request.persona_version or request.card_revision:
                    raise ApiError("context_stale", "a context receipt is required for this generated version")
            self._scope.basis = context["basis"] if context is not None else self.protocol.basis()
            self._scope.generation = generation
            with self.protocol.ingest_slot(request.source_key, scope), self.protocol.session(scope):
                outcome = await self._turn_service().commit_external(request, reply)
            self._check_generation(generation)
            return _outcome_payload(outcome)
        finally:
            self._end_scope()
            service.end_request(generation)

    # -- in-flight admission (R02) --------------------------------------

    def _admit(self) -> int:
        """Register an in-flight request; raise when the service is closing."""
        try:
            return self._service.begin_request()
        except ServiceClosing:
            raise

    def _check_generation(self, generation: int) -> None:
        """Refuse to report success for a request that lost its generation."""
        try:
            self._service.assert_current(generation)
        except ServiceClosing as exc:
            raise ApiError("service_closing", str(exc)) from exc

    def request_status(
        self, request_id: str, *, subject: str = "", client_id: str | None = None,
        session_id: str | None = None, scene: str | None = None,
    ) -> dict[str, Any]:
        """§3.1 `GET /v1/requests/{id}`: progress or the committed result.

        R03: the result is only served to the scope that owns it. Two clients
        reusing one `request_id` for different subjects must not read each
        other's reply, so a subject mismatch is reported as "unknown" rather
        than revealing that the id exists elsewhere.
        """
        service = self._service
        if not service.state.storage_ready:
            raise ApiError(
                "storage_unavailable",
                service.state.storage_reason or "storage is not ready",
            )
        request_id = str(request_id or "").strip()
        if not request_id:
            raise ApiError("invalid_argument", "request_id is required")
        record: TurnRecord | None = service.core.turn_requests.get(
            service.settings.character, request_id,
        )
        if record is None:
            raise ApiError("invalid_argument", f"unknown request {request_id!r}")
        wanted = str(subject or "").strip()
        recorded = str(getattr(record, "subject", "") or "").strip()
        if wanted and recorded and wanted != recorded:
            raise ApiError("invalid_argument", f"unknown request {request_id!r}")
        if any(value is not None and getattr(record, key) != value for key, value in
               (("client_id", client_id), ("session_id", session_id), ("scene", scene))):
            raise ApiError("not_found", "unknown request in this scope")
        if record.status == "deleted":
            raise ApiError(
                "deleted", "the result was invalidated", request_id=request_id,
                retryable=False,
            )
        payload = record.to_public()
        if record.body_available and record.reply is not None:
            payload["reply"] = record.reply
        return payload


def _with_persona(service: MemoryService, request: TurnRequest) -> TurnRequest:
    """Stamp the persona version this generation actually used (§3.2)."""
    published = service.core.persona_publish.current(service.settings.character)
    card = service.core.persona_card_service.get(service.settings.character)
    from dataclasses import replace

    return replace(
        request,
        persona_version=published.version if published.available else "",
        card_revision=card.revision,
    )


def _receipt_id(
    version: str, revision: int, subject: str = "private",
    scene: str = "", session: str = "",
) -> str:
    """Context receipt bound to the scope it was issued for (R03).

    A receipt that ignored subject/scene would let a caller hand a group-scoped
    turn the private subject's receipt, so the identity boundary is part of the
    digest.
    """
    import hashlib

    blob = (
        f"{version}:{revision}:{subject}:{scene}:{session}"
    ).encode("utf-8")
    return "ctx_" + hashlib.sha256(blob).hexdigest()[:24]


def _outcome_payload(outcome: TurnOutcome) -> dict[str, Any]:
    if outcome.status == "conflict":
        raise ApiError(
            "request_conflict",
            "request_id was reused with different input",
            request_id=outcome.request_id, retryable=False,
        )
    if outcome.status == "deleted":
        raise ApiError(
            "deleted", "the result was invalidated",
            request_id=outcome.request_id, retryable=False,
        )
    if outcome.status == "in_progress":
        raise ApiError(
            "turn_in_progress", "another attempt holds this request",
            request_id=outcome.request_id,
        )
    if outcome.status == "context_stale":
        raise ApiError(
            "context_stale",
            "the turn basis changed during generation; re-read context",
            request_id=outcome.request_id,
        )
    if outcome.status in ("failed", "interrupted") and outcome.reason:
        if outcome.reason.startswith("generation_failed:ModelCallError"):
            raise ApiError(
                "model_timeout" if "timeout" in outcome.reason else "chat_unavailable",
                outcome.reason, request_id=outcome.request_id,
            )
    if outcome.status in ("failed", "interrupted"):
        raise ApiError("storage_unavailable", outcome.reason or "turn did not commit", request_id=outcome.request_id)
    payload = outcome.to_dict()
    payload["request_id"] = outcome.request_id
    payload["turn_id"] = outcome.turn_id
    return payload


# -- HTTP transport ----------------------------------------------------------


class MemoryRequestHandler(BaseHTTPRequestHandler):
    """Thin JSON transport over `MemoryApi`."""

    server_version = "rin-memory/0.1"
    protocol_version = "HTTP/1.1"

    api: MemoryApi  # injected on the server class

    # -- plumbing --------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        """Keep the default stderr access log, but never log the body."""
        super().log_message(fmt, *args)

    def _read_json(self) -> dict[str, Any]:
        length = self.headers.get("Content-Length")
        try:
            size = int(length or 0)
        except (TypeError, ValueError):
            raise ApiError("invalid_argument", "Content-Length is not a number")
        if size < 0:
            raise ApiError("invalid_argument", "Content-Length is negative")
        limit = getattr(self.server, "max_body_bytes", DEFAULT_MAX_BODY_BYTES)
        if size > limit:
            raise ApiError("capacity", f"body exceeds {limit} bytes")
        raw = self.rfile.read(size) if size else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("invalid_argument", "body is not UTF-8 JSON")
        if not isinstance(body, dict):
            raise ApiError("invalid_argument", "body must be a JSON object")
        return body

    def _send(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        origin = getattr(self.server, "allow_origin", "")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self.wfile.write(body)

    def _check_origin(self) -> None:
        """§4.3: a loopback service still checks Host/Origin."""
        host = (self.headers.get("Host") or "").strip()
        allowed = getattr(self.server, "allowed_hosts", ("127.0.0.1", "localhost"))
        request_origin = None
        if host:
            try:
                authority = urlsplit("//" + host)
                name = (authority.hostname or "").lower()
                port = authority.port or 80
                if authority.username is not None or authority.password is not None or (
                    authority.path or authority.query or authority.fragment
                ):
                    raise ValueError("invalid Host authority")
            except ValueError:
                raise ApiError("forbidden", "host is not allowed")
            if name not in allowed:
                raise ApiError("forbidden", f"host {name!r} is not allowed")
            request_origin = ("http", name, port)
        origin = (self.headers.get("Origin") or "").strip()
        allow_origin = getattr(self.server, "allow_origin", "")
        if origin and origin != allow_origin:
            try:
                parsed = urlsplit(origin)
                # SSH forwarding preserves the browser's Host, including its
                # local port; the server's listening port can be different.
                same_origin = (
                    (parsed.scheme, parsed.hostname, parsed.port or 80) == request_origin
                    and parsed.username is None and parsed.password is None
                    and not (parsed.path or parsed.query or parsed.fragment)
                )
            except ValueError:
                same_origin = False
            if not same_origin:
                raise ApiError("forbidden", "origin is not allowed")

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        cache = "no-cache" if content_type.startswith(
            ("text/html", "text/javascript", "application/javascript", "text/css")
        ) else "public, max-age=86400"
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _console_dispatch(self, method: str) -> bool:
        management = getattr(self.server, "management", None)
        if management is None:
            return False
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if method == "GET" and path == "/":
            self.send_response(302)
            self.send_header("Location", "/designs/tidebell/index-watercolor.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        if method == "GET" and path.startswith("/designs/tidebell/"):
            relative = path.removeprefix("/designs/tidebell/")
            parts = relative.split("/")
            if any(part in ("", ".", "..") or "\\" in part for part in parts):
                self._send(404, {"code": "not_found"})
                return True
            target = _ui_resource(*parts)
            if target is None or not target.is_file():
                self._send(404, {"code": "not_found"})
                return True
            mime = mimetypes.guess_type(relative)[0] or "application/octet-stream"
            if relative.endswith(".js"):
                mime = "text/javascript; charset=utf-8"
            content = target.read_bytes()
            if relative == "index-watercolor.html":
                content = content.replace(b"</head>", b"<script>window.__RIN_SERVICE__=true;</script></head>", 1)
            self._send_bytes(200, content, mime)
            return True
        if path == "/v1/health" and method == "GET":
            self._send(200, self.api.health())
            return True
        if path == "/v1/bootstrap" and method == "GET":
            self._send(200, management.bootstrap())
            return True
        if not path.startswith("/v1/"):
            return False
        identity = management.identity(self.headers.get("X-Companion-Client", ""),
                                       self.headers.get("X-Companion-Token", ""))
        if path in ("/v1/proactive/poll", "/v1/proactive/observe",
                    "/v1/proactive/complete", "/v1/proactive/release", "/v1/proactive/check", "/v1/proactive/use"):
            management.require_memory_outputs(identity)
            if method != "POST":
                self._send(405, {"code": "method_not_allowed"})
                return True
            from .proactive import handle
            import asyncio
            body = management.scoped(self._read_json(), identity)
            self._send(200, asyncio.run(handle(self.api, path.rsplit("/", 1)[1], body, identity)))
            return True
        # The host persona capability is a narrow grant: it never rides on the
        # console flag, so these two routes are dispatched before the admin
        # gate and re-checked inside `ManagementApi`.
        if path in ("/v1/host/persona", "/v1/host/persona/publish"):
            if method == "GET" and path == "/v1/host/persona":
                self._send(200, management.get(path, {}, identity=identity))
                return True
            if method == "POST":
                body = management.scoped(self._read_json(), identity)
                self._send(200, management.post(path, body, identity=identity))
                return True
            self._send(405, {"code": "method_not_allowed"})
            return True
        # The conversation memory tools are a second, independent grant: they
        # touch this speaker's own facts and nothing else, and are likewise
        # dispatched before the console gate and re-checked in the API.
        if path in ("/v1/memory/entries", "/v1/memory/correct", "/v1/memory/forget"):
            if path == "/v1/memory/entries" and method == "GET":
                # `ManagementApi.get` reads one value per key, so keep the
                # query list-shaped instead of pre-flattening it here.
                query = {key: values for key, values in parse_qs(parsed.query).items() if values}
                self._send(200, management.get(path, query, identity=identity))
                return True
            if method == "POST" and path != "/v1/memory/entries":
                raw = management.scoped(self._read_json(), identity)
                self._send(200, management.post(path, raw, identity=identity))
                return True
            self._send(405, {"code": "method_not_allowed"})
            return True
        # Explicit teaching (MC04-T) is a third independent grant, dispatched
        # before the console gate and re-checked in the API: a chat client may
        # point at one of its own saved messages, never post rule text.
        if path in ("/v1/persona/teach", "/v1/persona/teach/revoke", "/v1/persona/teachings"):
            if path == "/v1/persona/teachings":
                if method != "GET":
                    self._send(405, {"code": "method_not_allowed"})
                    return True
                query = {key: values for key, values in parse_qs(parsed.query).items() if values}
                self._send(200, management.get(path, query, identity=identity))
                return True
            if method == "POST":
                raw = management.scoped(self._read_json(), identity)
                self._send(200, management.post(path, raw, identity=identity))
                return True
            self._send(405, {"code": "method_not_allowed"})
            return True
        # M5: assistant outputs are their own capability route (memory_outputs),
        # dispatched before the console gate and before the generic body read
        # below (reading the request body twice would block on the second
        # attempt), and re-checked inside the API so a normal chat client cannot
        # reach them by a routing mistake.
        if path in ("/v1/outputs", "/v1/outputs/deliver", "/v1/outputs/invalidate"):
            management.require_memory_outputs(identity)
            if path == "/v1/outputs" and method == "GET":
                query = {key: values for key, values in parse_qs(parsed.query).items() if values}
                self._send(200, management.get(path, query, identity=identity))
                return True
            if method == "POST":
                scoped = management.scoped(self._read_json(), identity)
                self._send(200, management.post(path, scoped, identity=identity))
                return True
            self._send(405, {"code": "method_not_allowed"})
            return True
        # M5: the working summary and the follow-up resolver are read-only
        # projections available to any identified client (they only ever show
        # this subject's own records), so they ride the protocol read routes.
        if path in ("/v1/working-summary", "/v1/followup"):
            if method != "GET":
                self._send(405, {"code": "method_not_allowed"})
                return True
            query = {key: values for key, values in parse_qs(parsed.query).items() if values}
            self._send(200, management.get(path, query, identity=identity))
            return True
        raw = self._read_json() if method == "POST" else {k:v[0] for k,v in parse_qs(parsed.query).items() if v}
        body = management.scoped(raw, identity)
        if method == "POST" and path in ("/v1/chat", "/v1/turns", "/v1/context"):
            if path != "/v1/context":
                self.api.protocol.validate_source(str(body.get("source_key") or ""), self.api.protocol.scope(body), str(identity.get("plugin_id") or ""))
            call = {"/v1/chat": self.api.chat, "/v1/turns": self.api.turns, "/v1/context": self.api.context}[path]
            self._send(200, _run(call(body)))
            return True
        if method == "GET" and path == "/v1/context":
            self._send(200, _run(self.api.context(body)))
            return True
        if path.startswith("/v1/requests/"):
            if method != "GET":
                self._send(405, {"code": "method_not_allowed"})
            else:
                self._send(200, self.api.request_status(path.rsplit("/",1)[1],
                    **{k:body.get(k, "") for k in ("subject", "client_id", "session_id", "scene")}))
            return True
        if path in ("/v1/chat", "/v1/turns"):
            self._send(405, {"code": "method_not_allowed"})
            return True
        protocol_paths = path in (
            "/v1/events", "/v1/receipts", "/v1/receipts/summary",
            "/v1/context/validate", "/v1/context/reserve",
            "/v1/context/consume",
            "/v1/segment/note",
            "/v1/cleanup/pending",
        ) or path.endswith("/ack")
        if not identity["admin"] and not protocol_paths:
            raise ApiError("forbidden", "this operation requires the console identity")
        generation = self.api._service.begin_request()
        try:
            if path == "/v1/stack/stop" and method == "POST":
                self.server.stop_event.set()
                self._send(202, {"status": "stopping"})
            elif protocol_paths:
                result = management.protocol_get(path, body, identity=identity) if method == "GET" else management.protocol_post(path, body, identity=identity)
                if result is None:
                    self._send(405, {"code": "method_not_allowed"})
                else:
                    self._send(200, result)
            elif method == "GET":
                query = {key:[str(value)] for key,value in body.items()}
                self._send(200, management.get(path, query))
            else:
                self._send(200, management.post(path, body))
        finally:
            self.api._service.end_request(generation)
        return True

    def _dispatch(self, method: str) -> None:
        slots = getattr(self.server, "request_slots", None)
        admitted = slots is None or slots.acquire(blocking=False)
        try:
            if not admitted:
                raise ApiError("capacity", "HTTP request capacity is full")
            self._check_origin()
            if self._console_dispatch(method):
                return
            if method == "GET":
                path = urlsplit(self.path).path
                if path == "/v1/health":
                    self._send(200, self.api.health())
                    return
                if path.startswith("/v1/requests/"):
                    request_id = path[len("/v1/requests/"):].strip("/")
                    self._send(200, self.api.request_status(request_id))
                    return
                if path == "/v1/context":
                    # §3.1 lists `/context` as a read; a GET with no body is
                    # the natural shape for "give me the current context".
                    self._send(200, _run(self.api.context({})))
                    return
                raise ApiError("invalid_argument", f"unknown path {path!r}")
            body = self._read_json()
            path = urlsplit(self.path).path
            if path == "/v1/chat":
                self._send(200, _run(self.api.chat(body)))
                return
            if path == "/v1/turns":
                self._send(200, _run(self.api.turns(body)))
                return
            if path == "/v1/context":
                self._send(200, _run(self.api.context(body)))
                return
            raise ApiError("invalid_argument", f"unknown path {path!r}")
        except ApiError as exc:
            self.close_connection = True
            self._send(exc.status, exc.to_dict())
        except ServiceClosing as exc:
            self.close_connection = True
            self._send(503, {"code": exc.code, "message": str(exc), "retryable": True})
        except ValueError as exc:
            self.close_connection = True
            self._send(400, {"code": "invalid_argument", "message": str(exc), "retryable": False, "request_id": None})
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            from .console import ApiError as ConsoleError
            status = exc.status if isinstance(exc, ConsoleError) else 500
            self.close_connection = True
            payload = {
                "code": exc.code if isinstance(exc, ConsoleError) else "internal_error",
                "message": exc.message if isinstance(exc, ConsoleError) else type(exc).__name__,
                "retryable": status in (429, 500, 503, 504),
                "request_id": None,
            }
            if isinstance(exc, ConsoleError):
                # A refusal may carry what the caller needs to recover (the
                # current revision on a conflict); never the caller's draft.
                payload.update(exc.extra)
            self._send(status, payload)
        finally:
            if slots is not None and admitted:
                slots.release()

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802 - CORS preflight
        self.send_response(204)
        origin = getattr(self.server, "allow_origin", "")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()


def _ui_resource(*parts: str):
    """Locate one management-UI file, installed first then from the source tree.

    The wheel ships the watercolour page under `rin_memory/webui`; a
    source checkout has it under `webui/designs/tidebell` and carries no packaged
    copy. Resolving both keeps `stack.ps1 start` usable from the repository
    (which is what one-click launch does) without weakening the packaging check
    `stack.py` performs for a real install. Returns None when neither exists.
    """
    packaged = files("rin_memory").joinpath("webui", *parts)
    if packaged.is_file():
        return packaged
    try:
        # `.../src/rin_memory/service/server.py` -> repository root.
        repo = Path(__file__).resolve().parents[3]
    except IndexError:  # pragma: no cover - defensive
        return None
    candidate = repo / "webui" / "designs" / "tidebell" / Path(*parts)
    return candidate if candidate.is_file() else None


def _run(coro: Any) -> Any:
    """Run one coroutine to completion on a private loop (threaded server)."""
    import asyncio

    return asyncio.run(coro)


def build_server(
    api: MemoryApi,
    *,
    host: str = "127.0.0.1",
    port: int = 8788,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    allow_origin: str = "",
    management: Any = None,
    request_capacity: int = 16,
    request_timeout_s: float = 65.0,
) -> ThreadingHTTPServer:
    """Create (but do not start) the HTTP server for one `MemoryApi`."""
    connection_slots = threading.BoundedSemaphore(request_capacity)
    class CapacityServer(ExclusiveAddrMixin, ThreadingHTTPServer):
        def process_request(self, request, client_address):
            if not connection_slots.acquire(blocking=False):
                try:
                    request.sendall(b"HTTP/1.1 429 Too Many Requests\r\nConnection: close\r\n"
                                    b"Content-Type: application/json\r\nContent-Length: 19\r\n\r\n"
                                    b'{"code":"capacity"}')
                finally:
                    self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                connection_slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                connection_slots.release()

    handler = type("BoundHandler", (MemoryRequestHandler,), {"api": api})
    server = CapacityServer((host, int(port)), handler)
    server.api = api  # type: ignore[attr-defined]
    server.max_body_bytes = int(max_body_bytes)  # type: ignore[attr-defined]
    server.allow_origin = allow_origin  # type: ignore[attr-defined]
    server.management = management
    server.request_slots = threading.BoundedSemaphore(request_capacity)
    server.stop_event = threading.Event()
    server.allowed_hosts = (  # type: ignore[attr-defined]
        "127.0.0.1", "localhost", "::1",
    )
    server.daemon_threads = True
    # Socket reads also have a deadline; a partial body cannot hold a slot forever.
    original_get_request = server.get_request
    def get_request():
        connection, address = original_get_request()
        connection.settimeout(request_timeout_s)
        return connection, address
    server.get_request = get_request
    return server


__all__ = [
    "ApiError",
    "MemoryApi",
    "MemoryRequestHandler",
    "ServiceStartupError",
    "build_server",
]
