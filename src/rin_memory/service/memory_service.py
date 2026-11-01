"""Standalone service assembly: one authoritative core, one root owner (S04).

`docs/standalone-service.md` §2.1/§4.2 fix the shape of this module:

* the service process is **the** writable owner of one data root, so it takes
  the S01 root lock before assembling anything and releases it last;
* startup order is load config -> take the lock -> assemble the core -> register
  recovery handlers -> recover barriers/journal/turn records/outbox -> become
  ready -> start background tasks -> open the interfaces;
* capability state is reported separately for storage, chat and background, so
  one missing model neither hides working memory nor claims a working `/chat`.

This module deliberately contains no HTTP. `service/server.py` is the transport
and the only place that knows about status codes, so the assembly can be tested
(and reused by the WebUI) without a socket.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..adapters.composition import MemoryCore, build_memory_core
from ..adapters.model_http import HttpModelInvoker, ModelSettings
from ..domain.persona_teaching import PersonaTeachingService
from ..domain.turns import TurnCommitService
from ..domain.evolution import EvolutionSettings
from ..domain.extraction import ExtractionSettings
from ..errors import RootLockConflict
from ..persistence.ownership import RootLock, acquire_root_lock
from ..persistence.persona_teaching import PersonaTeachingStore

#: Capability states reported by `/v1/health` and the status block.
READY = "ready"
DEGRADED = "degraded"
UNAVAILABLE = "unavailable"


class ServiceStartupError(RuntimeError):
    """A configuration problem that must stop the process before it listens."""

    def __init__(self, message: str, *, code: str = "startup_failed") -> None:
        super().__init__(message)
        self.code = code


class ServiceClosing(RuntimeError):
    """The service is shutting down, or this request lost its generation.

    Raised before persistence, so a request admitted before `stop()` can never
    commit after it (R02).
    """

    code = "service_closing"


@dataclass(frozen=True)
class ServiceSettings:
    """Everything the service needs to know, resolved from one config object."""

    memory_root: Path
    local_state_root: Path | None = None
    character: str = ""
    host: str = "127.0.0.1"
    port: int = 8788
    #: Background maintenance cadence (§4.3: default 60s).
    maintenance_interval_s: float = 60.0
    #: Hard cap on a single `/chat` generation, seconds.
    chat_timeout_s: float = 60.0
    #: Context building budget (§6.1: the 1000 ms figure is the context budget).
    context_budget_ms: int = 1000
    #: Bounded request body size (§4.3 capacity limits).
    max_body_bytes: int = 2 * 1024 * 1024
    #: Refuse new chat work above this many in-flight requests.
    max_in_flight_chat: int = 4
    #: How long `stop()` waits for in-flight requests before abandoning them.
    shutdown_timeout_s: float = 30.0
    model: ModelSettings = field(default_factory=ModelSettings)
    allow_origin: str = ""
    require_published_persona: bool = False
    clients: Mapping[str, Any] = field(default_factory=dict)
    max_pending_events: int = 1024
    #: RO02 batch extraction. A partial group of ordinary turns waits this long
    #: after the last utterance before it is extracted. 0.0 = "extract
    #: immediately on the next maintenance round" (the historical behaviour);
    #: 120.0 = the cost-optimizing production policy. Neither loses a turn: the
    #: raw turn is persisted before it is queued.
    batch_idle_seconds: float = 0.0
    #: Turns per batch; five is the design's figure (section 6.2).
    batch_max_turns: int = 5
    #: Input budget for one batch call; a bigger group is split before the call.
    batch_max_messages: int = 16


@dataclass
class ServiceState:
    """Live capability state, updated by startup and by real calls."""

    storage_ready: bool = False
    chat_ready: bool = False
    background_ready: bool = False
    storage_reason: str | None = None
    chat_reason: str | None = None
    background_reason: str | None = None
    recovery: Mapping[str, Any] = field(default_factory=dict)
    started_at: str = ""
    #: Most recent *recoverable* model failure kind (R06). Recorded without
    #: closing the capability, so an operator sees the degradation and the next
    #: request can still succeed.
    last_model_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "storage": _capability(self.storage_ready, self.storage_reason),
            "chat": _capability(self.chat_ready, self.chat_reason),
            "background": _capability(
                self.background_ready, self.background_reason,
            ),
            "recovery": dict(self.recovery),
            "started_at": self.started_at,
        }
        if self.last_model_error:
            payload["last_model_error"] = self.last_model_error
        return payload

    def degrade_chat(self, reason: str) -> None:
        self.chat_ready = False
        self.chat_reason = reason


def _capability(ready: bool, reason: str | None) -> dict[str, Any]:
    if ready:
        return {"status": READY}
    return {"status": UNAVAILABLE if reason else DEGRADED, "reason": reason}


def _with_console_model_config(settings: "ServiceSettings") -> "ServiceSettings":
    """Overlay the console's saved provider settings onto the startup config.

    Kept out of the constructor on purpose: reading a file is a startup step,
    and a service assembled directly in a test must not depend on one. A broken
    file is ignored here -- the console reports it as `problem`, and refusing to
    start over a malformed settings file would make the page unreachable.
    """
    from dataclasses import replace as _replace

    from .model_config import apply_model_config, load_model_config

    local = settings.local_state_root
    if local is None:
        local = Path(settings.memory_root).parent / "local_state"
    try:
        stored = load_model_config(local)
    except Exception:  # noqa: BLE001 - startup must not depend on this file
        return settings
    if stored.get("problem") or not any(
        stored.get(key) for key in ("base_url", "api_key", "models")
    ):
        return settings
    return _replace(settings, model=apply_model_config(settings.model, stored))


class MemoryService:
    """One authoritative core plus its ownership lock and turn commits."""

    def __init__(
        self,
        settings: ServiceSettings,
        *,
        core: MemoryCore,
        lock: RootLock | None,
        model: HttpModelInvoker | None,
    ) -> None:
        self.settings = settings
        self.core = core
        self._lock = lock
        self.model = model
        self.state = ServiceState()
        self._maintenance_task: asyncio.Task | None = None
        self._closed = False
        #: In-flight bookkeeping for `stop()` (R02). The generation invalidates
        #: requests admitted before a shutdown; `_drained` wakes the drain wait.
        self._guard = threading.Lock()
        self._current_generation = 0
        self._in_flight = 0
        self._drained = threading.Event()
        self._drained.set()
        self._last_drain: dict[str, Any] = {}
        self._background_stopped = False
        # MC04-T: explicit teaching is assembled here, *before* any recovery, so
        # its journal replay handler is registered before the first reconcile.
        # A teach event written by an older process must be replayable on
        # startup; an unknown event type would otherwise pause the whole replay.
        # It needs no new core field: the store and the card/publish services it
        # writes through all already exist on the core.
        self.persona_teaching = self._build_persona_teaching(core, model)
        from .protocol import ServiceProtocol
        self.protocol = ServiceProtocol(self)

    @staticmethod
    def _build_persona_teaching(core: MemoryCore, model=None) -> PersonaTeachingService:
        core.persona_teaching._model = model
        return core.persona_teaching

    # -- lifecycle -------------------------------------------------------

    @classmethod
    def start(cls, settings: ServiceSettings) -> "MemoryService":
        """Assemble the core in the documented order; raise before listening."""
        if not str(settings.character or "").strip():
            raise ServiceStartupError(
                "an explicit character is required", code="missing_character",
            )
        root = Path(settings.memory_root).expanduser()
        if not root.exists():
            raise ServiceStartupError(
                f"memory root does not exist: {str(root)!r}", code="missing_root",
            )
        # Console-saved provider settings are applied before the invoker exists,
        # so a configuration typed on the page survives a restart instead of
        # silently reverting to whatever stack.toml said.
        settings = _with_console_model_config(settings)
        model = HttpModelInvoker(settings.model) if settings.model.configured else None
        try:
            lock = acquire_root_lock(root, mode="standalone")
        except RootLockConflict as exc:
            raise ServiceStartupError(
                f"another writer owns {str(root)!r}: {exc.detail.get('owner') or ''}",
                code="root_lock_conflict",
            ) from exc
        try:
            core = build_memory_core(
                memory_root=root,
                local_state_root=settings.local_state_root,
                model=model,
                evolution_settings=EvolutionSettings(enabled=True),
                extraction_settings=ExtractionSettings(
                    batch_idle_seconds=settings.batch_idle_seconds,
                    batch_max_turns=settings.batch_max_turns,
                    batch_max_messages=settings.batch_max_messages,
                ),
            )
            service = cls(settings, core=core, lock=lock, model=model)
            recovery = core.recover(settings.character)
        except BaseException:
            lock.release()
            raise
        service.state.storage_ready = True
        service.state.started_at = core.clock.now().isoformat()
        service.state.recovery = {
            "applied": getattr(recovery, "applied", 0),
            "paused": getattr(recovery, "paused", False),
            "reason": getattr(recovery, "reason", None),
        }
        service._refresh_chat_capability()
        return service

    def _refresh_chat_capability(self) -> None:
        """`/chat` needs a persona card and a usable conversation tier."""
        if self.model is None:
            self.state.degrade_chat("no conversation model configured")
            return
        if not self.model.available("conversation"):
            self.state.degrade_chat(
                "conversation tier is not configured or has no credential",
            )
            return
        try:
            card = self.core.persona_card_service.get(self.settings.character)
        except Exception as exc:  # noqa: BLE001 - surfaced as a capability
            self.state.degrade_chat(f"persona unavailable: {type(exc).__name__}")
            return
        if not card.sections:
            self.state.degrade_chat("persona card is not initialized")
            return
        if self.settings.require_published_persona and not self.core.persona_publish.current(
            self.settings.character,
        ).available:
            self.state.degrade_chat("persona card is not published")
            return
        self.state.chat_ready = True
        self.state.chat_reason = None

    async def start_background(self) -> None:
        """Start the per-character maintenance loop (§4.3)."""
        maintenance = getattr(self.core, "maintenance", None)
        if maintenance is None:
            self.state.background_reason = "maintenance is not wired"
            return
        self._maintenance_task = await maintenance.start(
            self.settings.character,
            interval_seconds=float(self.settings.maintenance_interval_s),
        )
        self.state.background_ready = True
        self.state.background_reason = None

    async def stop(self, *, drain_timeout_s: float | None = None) -> None:
        """Stop accepting work, drain in-flight requests, then release the lock.

        Order matters (R02): `stop()` used to release the root lock while a
        suspended `/chat` was still inside its model call, so a new instance
        could take ownership and the old request could still commit -- two
        writers touching one root.

        Now: (1) mark the service closed so no new request is admitted and any
        in-flight request loses its generation, (2) wait for the request
        threads to finish (bounded; overdue handlers retain ownership), (3)
        stop the background loop and release ownership after the last handler.
        """
        if self._closed:
            return
        # (1) Close the entrance first: new requests are refused from here on,
        # and a request that already entered will fail its generation check
        # instead of committing.
        with self._guard:
            self._closed = True
            self._current_generation += 1
        # (2) Drain what is already inside; nothing new can start.
        deadline = (
            self.settings.shutdown_timeout_s
            if drain_timeout_s is None else float(drain_timeout_s)
        )
        drained = await self._drain(deadline)
        self._last_drain = dict(drained)
        # (3) Background loop, then ownership.
        maintenance = getattr(self.core, "maintenance", None)
        if maintenance is not None:
            try:
                await maintenance.stop()
            except Exception:  # noqa: BLE001 - shutdown must still release
                pass
        with self._guard:
            self._background_stopped = True
            self._release_if_drained()
        self.state.storage_ready = False
        self.state.chat_ready = False
        self.state.background_ready = False

    @property
    def closed(self) -> bool:
        return self._closed

    # -- in-flight tracking (R02) ---------------------------------------

    def begin_request(self) -> int:
        """Register one in-flight request; returns the generation it holds.

        A request that captured an older generation must not commit after a
        `stop()`: `assert_current()` is called right before persistence, which
        is the last point where refusing costs nothing.
        """
        with self._guard:
            if self._closed:
                raise ServiceClosing("service is shutting down")
            generation = self._current_generation
            self._in_flight += 1
            # The event starts SET (no work to wait for); the first admit must
            # clear it or `stop()` would think the drain finished instantly.
            self._drained.clear()
            return generation

    def end_request(self, generation: int) -> None:
        with self._guard:
            if self._in_flight > 0:
                self._in_flight -= 1
            if self._in_flight <= 0:
                self._drained.set()
                self._release_if_drained()

    def _release_if_drained(self) -> None:
        """Called under _guard: a timed-out handler still owns its root."""
        if self._closed and self._background_stopped and self._in_flight == 0 and self._lock is not None:
            self._lock.release()
            self._lock = None

    def assert_current(self, generation: int) -> None:
        """Refuse work from a request that lost its generation to `stop()`."""
        with self._guard:
            if self._closed or generation != self._current_generation:
                raise ServiceClosing(
                    "the service stopped while this request was in flight",
                )

    @property
    def in_flight(self) -> int:
        with self._guard:
            return self._in_flight

    async def _drain(self, timeout_s: float) -> dict[str, Any]:
        """Wait for in-flight requests to finish, bounded by `timeout_s`.

        The wait is on a `threading.Event` in a worker thread: request work runs
        on handler threads, and blocking the event loop here would stop the
        very callbacks that are finishing. On timeout the remaining work is
        reported, not silently ignored (a leaked writer must be visible).
        """
        started = time.monotonic()
        with self._guard:
            pending_at_stop = self._in_flight
        if pending_at_stop <= 0:
            return {"waited_s": 0.0, "drained": True, "abandoned": 0}
        finished = await asyncio.to_thread(self._drained.wait, max(0.0, timeout_s))
        waited = round(time.monotonic() - started, 3)
        with self._guard:
            remaining = self._in_flight
        return {
            "waited_s": waited,
            "drained": bool(finished) and remaining <= 0,
            "abandoned": remaining,
        }

    async def close(self) -> dict[str, Any]:
        """Alias used by tests/transports: stop and report the drain outcome."""
        await self.stop()
        return dict(self._last_drain)

    # -- background status ----------------------------------------------

    def background_status(self) -> dict[str, Any]:
        maintenance = getattr(self.core, "maintenance", None)
        if maintenance is None:
            return {"status": UNAVAILABLE, "reason": "maintenance is not wired"}
        task = self._maintenance_task
        result = None
        try:
            result = maintenance.last_result(self.settings.character)
        except Exception:  # noqa: BLE001 - observability only
            result = None
        payload: dict[str, Any] = {
            "status": READY if self.state.background_ready else UNAVAILABLE,
            "running": task is not None and not task.done(),
        }
        if result is not None:
            payload["last_status"] = getattr(result, "status", None)
        outbox = self.core.outbox.pending(self.settings.character, limit=1000)
        payload["outbox_pending"] = len(outbox)
        return payload
