"""Console conversation bridge: talk to the memory core without a host.

S05's C package wires N.E.K.O as the conversation generator, but the console
needs a working chat entry *before* that host exists: the same page that edits
the persona should be able to hold a conversation with it, and every turn must
be committed through the same whole-turn path the service uses (`/v1/chat`) so
the transcript, the extraction queue and the deletion barrier stay coherent.

This module therefore adds **no domain rules**. It

* reuses `MemoryApi.chat` (S02/S04: reserve -> generate -> basis re-check ->
  journal commit -> enqueue extraction), so replay, `context_stale`, capacity
  and the `service_closing` refusal behave exactly as documented;
* reuses the service's own `MemoryService` when one exists (stack mode), and
  otherwise wraps the console's core in a lock-less service object, so the
  WebUI keeps exactly one `MemoryCore` and one writer;
* reads the visible transcript from `recent.json`, which is what the turn commit
  actually wrote, instead of keeping a second copy of the conversation.

Credentials stay in the environment: `ModelSettings.api_key_env` is a variable
*name* and `status()` only ever reports whether a credential is present.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from ..adapters.model_http import ModelSettings
from ..event_sources import build_host_source_key
from ..persistence.recent import message_text
from .memory_service import MemoryService, ServiceSettings
from .server import ApiError, MemoryApi

#: Status codes for the console's own errors (mirrors the `/v1` envelope).
_STATUS_FOR_CODE = {
    "invalid_argument": 400,
    "forbidden": 403,
    "read_only": 403,
    "request_conflict": 409,
    "context_stale": 409,
    "deleted": 410,
    "capacity": 429,
    "chat_unavailable": 503,
    "storage_unavailable": 503,
    "service_closing": 503,
    "model_timeout": 504,
}


class ChatError(Exception):
    """A chat failure a console transport can render without guessing."""

    def __init__(
        self, code: str, message: str, *, retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = _STATUS_FOR_CODE.get(code, 500)
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


class DemoChatModel:
    """Deterministic offline stand-in so the console chat works without a key.

    Only the demo root uses it. It never claims to be a real model: the console
    labels the transcript accordingly, because a canned reply that looked like
    a generated one would make N04's real-model acceptance meaningless.
    """

    available_tier = "conversation"

    def __init__(self, name: str = "离线演示") -> None:
        self.name = name

    def available(self, tier: str) -> bool:
        return tier == self.available_tier

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        del tier, max_output_tokens, timeout_s, purpose
        said = ""
        for item in reversed(list(messages)):
            if item.get("role") == "user":
                said = str(item.get("content") or "").strip()
                break
        remembered = 0
        for item in messages:
            if item.get("role") == "system":
                for line in str(item.get("content") or "").splitlines():
                    if line[:1].isdigit() and ". " in line:
                        remembered += 1
                break
        prefix = f"（离线演示模型）我听到你说「{said}」。" if said else "（离线演示模型）我在听。"
        if remembered:
            return f"{prefix}这一轮我带着 {remembered} 条相关记忆在回答。"
        return f"{prefix}配置模型后，这里会换成真实回复。"


class ChatBridge:
    """One conversation path over an existing core (console and stack)."""

    def __init__(
        self,
        *,
        core: Any,
        character: str,
        model: Any = None,
        service: MemoryService | None = None,
        memory_root: Any = None,
        settings: ServiceSettings | None = None,
        max_in_flight_chat: int = 4,
        writable: bool = True,
        demo_model: bool = False,
    ) -> None:
        self._core = core
        self._character = str(character or "").strip()
        if not self._character:
            raise ValueError("character must be nonempty")
        self._writable = bool(writable)
        self._history_lock = threading.RLock()
        self.demo_model = bool(demo_model)
        if service is None:
            base = settings or ServiceSettings(
                memory_root=memory_root, character=self._character,
                max_in_flight_chat=max_in_flight_chat,
            )
            # No lock: the console already owns (or deliberately did not take)
            # the root, and a second acquisition in this process would fail.
            service = MemoryService(base, core=core, lock=None, model=model)
            service.state.storage_ready = True
            service._refresh_chat_capability()
        self._service = service
        self._api = MemoryApi(service, max_in_flight_chat=max_in_flight_chat)
        self._settings = service.settings.model

    def _settings_model(self):
        """The model settings currently in force (console edits replace them)."""
        return self._service.settings.model

    def reconfigure(self, settings) -> dict[str, Any]:
        """Apply a new model configuration without rebuilding the core.

        The console edits provider settings at runtime, so the invoker has to be
        swapped in place: the core's extraction/reflection/evolution services
        keep the invoker they were built with, which is why the *same*
        `HttpModelInvoker` object is reused and only its settings are replaced
        (see `ModelSettings` being frozen -- the invoker holds a mutable slot).

        Refusing here would be wrong for a transiently invalid entry: the page
        reports the validation error and the previous configuration keeps
        working.
        """
        from dataclasses import replace as _replace

        from ..adapters.model_http import HttpModelInvoker

        service = self._service
        service.settings = _replace(service.settings, model=settings)
        invoker = getattr(service, "model", None)
        if isinstance(invoker, HttpModelInvoker):
            invoker.update_settings(settings)
        elif settings.configured and not self.demo_model:
            service.model = HttpModelInvoker(settings)
            self._rebind_core_model(service.model)
        service._refresh_chat_capability()
        return self.status()

    def _rebind_core_model(self, model) -> None:
        """Point the core's own model consumers at a replacement invoker.

        The background services (fact extraction, reflections, evolution) hold
        the invoker they were constructed with. Replacing only
        `service.model` would update chat while leaving those on the old
        provider, so the console's one configuration would silently apply to
        half the system.
        """
        for name in ("extraction", "recall", "compression", "reflection_service",
                     "evolution_service", "persona_service"):
            holder = getattr(self._core, name, None)
            if holder is not None and hasattr(holder, "_model"):
                try:
                    object.__setattr__(holder, "_model", model)
                except (AttributeError, TypeError):
                    try:
                        holder._model = model  # type: ignore[attr-defined]
                    except Exception:  # noqa: BLE001 - best effort rebinding
                        continue

    # -- capability ------------------------------------------------------

    def status(self) -> dict[str, Any]:
        service = self._service
        settings = self._settings_model()
        card = None
        published = None
        try:
            card = self._core.persona_card_service.get(self._character)
            published = self._core.persona_publish.current(self._character)
        except Exception:  # noqa: BLE001 - an empty root is a normal state
            card = None
            published = None
        reason = None
        if not self._writable:
            reason = "记忆根是只读的：写入对话需要 --allow-write"
        elif not service.state.chat_ready:
            reason = service.state.chat_reason or "对话模型不可用"
        return {
            "ready": reason is None,
            "writable": self._writable,
            "reason": reason,
            "character": self._character,
            "display_name": getattr(card, "character", self._character) or self._character,
            "card_revision": int(getattr(card, "revision", 0) or 0),
            "persona_version": (
                str(getattr(published, "version", "") or "")
                if published is not None and getattr(published, "available", False)
                else ""
            ),
            "model": {
                "configured": bool(getattr(settings, "configured", False)),
                "tier": "conversation",
                "name": settings.models.get("conversation", ""),
                "demo": self.demo_model,
                "last_error": service.state.last_model_error,
                "reasoning_effort": getattr(settings, "reasoning_effort", ""),
                "api_key_source": (
                    getattr(settings, "api_key_source", lambda: "none")()
                    if not self.demo_model else "none"
                ),
            },
            "capabilities": service.state.to_dict(),
        }

    # -- transcript ------------------------------------------------------

    def history(self, *, limit: int = 80) -> dict[str, Any]:
        """The conversation as it was committed, plus the turn ledger.

        `recent.json` is written by the same commit that produces the reply, so
        it is the authoritative transcript for both console-generated and
        host-generated turns. A compression memo (a `system` message) is not a
        bubble and is skipped.
        """
        stored = self._core.recent.messages(self._character)
        messages: list[dict[str, Any]] = []
        for index, item in enumerate(stored):
            if not isinstance(item, Mapping):
                continue
            metadata = item.get("metadata") or (item.get("data") or {}).get("metadata") or {}
            if metadata.get("subject", "private") != "private" or metadata.get("scene", ""):
                continue
            if metadata.get("session_id", "console") != "console":
                continue
            kind = str(item.get("type") or item.get("role") or "").lower()
            if kind in ("system", "summary"):
                continue
            text = message_text(item).strip()
            if not text:
                continue
            messages.append({
                "id": self._message_id(index, item),
                "role": "assistant" if kind in ("ai", "assistant") else "user",
                "text": text,
                "reasoning": str(((item.get("data") or {}).get("additional_kwargs") or {}).get("reasoning_content") or ""),
            })
        bounded = max(1, min(500, int(limit or 80)))
        return {
            "messages": messages[-bounded:],
            "total": len(messages),
            "turns": self._turns(limit=bounded),
            "status": self.status(),
        }

    def _turns(self, *, limit: int) -> list[dict[str, Any]]:
        view = self._core.turn_requests.load_view(self._character)
        receipts = list(self._service.protocol._load()["receipts"].values())
        rows = [
            {
                "request_id": str(record.get("request_id") or key),
                "status": str(record.get("status") or ""),
                "occurred_at": str(record.get("occurred_at") or ""),
                "persona_version": str(record.get("persona_version") or ""),
                "has_reply": bool(record.get("reply")),
                "reason": record.get("reason"),
                "delivered": any(row["request_id"] == key and row["kind"] == "delivered" for row in receipts),
            }
            for key, record in view.items()
            if isinstance(record, Mapping) and record.get("subject", "private") == "private"
            and record.get("session_id", "console") == "console" and not record.get("scene")
            and not str(record.get("reason") or "").startswith("console_history_")
        ]
        rows.sort(key=lambda row: row["occurred_at"], reverse=True)
        return rows[:limit]

    @staticmethod
    def _console_message(item) -> bool:
        meta = (item.get("data") or {}).get("metadata") or item.get("metadata") or {}
        return meta.get("subject", "private") == "private" and not meta.get("scene") and meta.get("session_id", "console") == "console"

    @staticmethod
    def _message_id(index, item) -> str:
        value = f"{index}:{item.get('type', item.get('role', ''))}:{message_text(item)}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

    def _history_writable(self) -> None:
        if not self._writable:
            raise ChatError("read_only", "当前对话记录是只读的")

    def _invalidate_history_turns(self, sources=None, *, reason: str) -> None:
        records = self._core.turn_requests.load_view(self._character)
        ids = [key for key, row in records.items() if row.get("subject", "private") == "private"
               and row.get("session_id", "console") == "console" and not row.get("scene")
               and (sources is None or row.get("source_key") in sources)]
        self._core.turn_requests.invalidate(self._character, ids, reason=reason)

    def clear_history(self) -> dict[str, Any]:
        self._history_writable()
        with self._history_lock:
            self._core.recent.update(self._character, lambda rows: [row for row in rows if not self._console_message(row)])
            self._invalidate_history_turns(reason="console_history_cleared")
        return {"ok": True}

    def edit_message(self, message_id: str, text: str) -> dict[str, Any]:
        self._history_writable()
        with self._history_lock:
            sources = set()
            def edit(rows):
                for index, row in enumerate(rows):
                    if self._console_message(row) and self._message_id(index, row) == message_id:
                        data = dict(row.get("data") or {})
                        sources.add((data.get("metadata") or row.get("metadata") or {}).get("source_key"))
                        data["content"] = text
                        extra = dict(data.get("additional_kwargs") or {})
                        extra.pop("reasoning_content", None)
                        data["additional_kwargs"] = extra
                        rows[index] = {**row, "data": data}
                        return rows
                raise ChatError("request_conflict", "消息已变化，请刷新对话后重试")
            self._core.recent.update(self._character, edit)
            self._invalidate_history_turns(sources, reason="console_history_edited")
        return {"ok": True}

    def regenerate(self, message_id: str, request_id: str) -> dict[str, Any]:
        self._history_writable()
        with self._history_lock:
            prior = self._core.turn_requests.get(self._character, request_id)
            if prior and prior.is_committed and prior.session_id == "console" and prior.subject == "private" and not prior.scene:
                return {"status": "committed", "reply": prior.reply, "request_id": request_id, "replayed": True}
            original = self._core.recent.messages(self._character)
            visible = [(i, row) for i, row in enumerate(original) if self._console_message(row)
                       and str(row.get("type") or row.get("role")) not in ("system", "summary")]
            position = next((p for p, (i, row) in enumerate(visible) if self._message_id(i, row) == message_id), -1)
            if position < 1 or str(visible[position][1].get("type") or visible[position][1].get("role")) not in ("ai", "assistant"):
                raise ChatError("request_conflict", "回复已变化，请刷新对话后重试")
            user_index, user = visible[position - 1]
            if str(user.get("type") or user.get("role")) not in ("human", "user"):
                raise ChatError("invalid_argument", "这条回复前没有用户消息")
            removed = []
            def truncate(rows):
                if rows[:visible[position][0] + 1] != original[:visible[position][0] + 1]:
                    raise ChatError("request_conflict", "对话已变化，请刷新后重试")
                removed.extend(row for i, row in enumerate(rows) if i >= user_index and self._console_message(row))
                return [row for i, row in enumerate(rows) if i < user_index or not self._console_message(row)]
            self._core.recent.update(self._character, truncate)
            try:
                result = self.send_sync(message_text(user), request_id=request_id)
                if result.get("status") != "committed":
                    raise ChatError("chat_unavailable", "重新生成未完成，请重试", retryable=True)
            except Exception:
                self._core.recent.update(self._character, lambda rows: [row for row in rows if not self._console_message(row)]
                                         + [row for row in original if self._console_message(row)])
                raise
            sources = {(row.get("data") or {}).get("metadata", {}).get("source_key") for row in removed}
            self._invalidate_history_turns(sources, reason="console_history_regenerated")
            return result

    # -- sending ---------------------------------------------------------

    async def send(
        self,
        text: Any,
        *,
        request_id: str = "",
        session_id: str = "console",
        subject: str = "private",
        scene: str = "",
        content_class: str = "chat",
        client_id: str = "",
    ) -> dict[str, Any]:
        """Commit one console turn and return the reply.

        `request_id` is the caller's idempotency key: the page keeps it across a
        retry so a lost response replays the committed result instead of
        producing a second turn.
        """
        message = str(text or "").strip()
        if not message:
            raise ChatError("invalid_argument", "消息不能为空")
        if not self._writable:
            raise ChatError("read_only", "记忆根是只读的：请用 --allow-write 启动")
        request = str(request_id or "").strip() or f"chat-{uuid.uuid4().hex}"
        event_id = request.replace(":", "_").replace(" ", "_")[:120]
        body = {
            "request_id": request,
            "turn_id": "turn-" + uuid.uuid5(uuid.NAMESPACE_URL, f"{self._character}:{subject}:{session_id}:{request}").hex,
            "source_key": build_host_source_key(self._character, subject, event_id),
            "content_class": content_class,
            "messages": [{"role": "user", "content": message}],
            "subject": subject,
            "session_id": session_id,
            "scene": scene,
            "client_id": client_id,
        }
        existing = self._core.turn_requests.get(self._character, request)
        body["occurred_at"] = existing.occurred_at if existing else self._core.clock.now().isoformat()
        try:
            outcome = await self._api.chat(body)
        except ApiError as exc:
            raise self._as_chat_error(exc) from exc
        except Exception as exc:  # noqa: BLE001 - surfaced, never a traceback
            raise ChatError(
                "internal", f"{type(exc).__name__}: {exc}", retryable=True,
            ) from exc
        payload = dict(outcome)
        payload["request_id"] = request
        payload["messages"] = [
            {"role": "user", "text": message},
            {"role": "assistant", "text": str(payload.get("reply") or "")},
        ]
        return payload

    def send_sync(self, text: Any, **kwargs: Any) -> dict[str, Any]:
        """Blocking wrapper for the stdlib threaded transports."""
        with self._history_lock:
            return run_coroutine(self.send(text, **kwargs))

    def _as_chat_error(self, exc: ApiError) -> ChatError:
        """Give the console the real reason a generation failed.

        `TurnCommitService` records `generation_failed:<exception type>` as the
        turn's reason, which is right for the ledger (it must not depend on a
        provider's vocabulary) but loses the failure kind. The service keeps
        that kind on `state.last_model_error` for the retryable cases (R06), so
        the console reads it from there instead of reporting every model
        problem as a closed capability.
        """
        kind = getattr(self._service.state, "last_model_error", None)
        # A fatal failure (config/auth) closes the capability, so a surviving
        # `chat_ready` is exactly the "this was transient" signal. Without this
        # guard a stale `last_model_error` from an earlier timeout would relabel
        # a later credential failure as retryable.
        if exc.code == "chat_unavailable" and kind and self._service.state.chat_ready:
            if kind == "timeout":
                return ChatError(
                    "model_timeout", f"模型调用超时（{kind}），可以重试",
                    retryable=True,
                )
            if kind in ("rate_limit", "transport"):
                return ChatError(
                    "chat_unavailable", f"模型暂时不可用（{kind}），可以重试",
                    retryable=True,
                )
        return ChatError(exc.code, exc.message, retryable=exc.retryable)


def run_coroutine(coro: Any) -> Any:
    """Run one coroutine to completion (these servers run one per thread)."""
    return asyncio.run(coro)


__all__ = [
    "ChatBridge",
    "ChatError",
    "DemoChatModel",
    "ModelSettings",
    "run_coroutine",
]
