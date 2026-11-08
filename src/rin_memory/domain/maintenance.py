"""B07 maintenance: one ordered, exception-isolated pass plus a loop wrapper.

Host startup and background loops live in app/memory_server/runtime.py and
evidence_loops.py; the core owns the pass body and a single asyncio task
(design section 17.6.3). The frozen order is:

    reconcile -> outbox replay -> one-shot migrations -> reflection synthesis
    -> promotion -> archive -> persona corrections -> growth reconcile ->
    deletion retry -> optional journal compaction

Rules:
* reconcile must finish before outbox replay (runtime.py:1107-1129); the
  caller passes MemoryCore.recover as recover().
* Every step is isolated: an exception is recorded on that step and the pass
  continues; run_once never raises. The result is completed when no step
  failed, otherwise partial.
* Deletion retry only reruns pending/failed steps (DeletionService.retry
  excludes the plugin step); one deletion failing does not stop the others.
* Journal compaction is off by default and only runs after reconcile drained
  the tail, with caller-supplied snapshot seeds.
* start/stop use one asyncio task and an asyncio.Event per character. No
  thread or process is started, so a temporary root and a fake clock are
  enough for tests.

All writes still go through the B02/B03/B05 services, so WritePermit and the
deletion barrier stay inside them; this module never touches a file directly.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..ports import Clock

#: Step names in execution order (also the key set of MaintenanceResult.steps).
STEP_ORDER = (
    "reconcile",
    "outbox",
    "migrations",
    "synthesize",
    "promote",
    "archive",
    "persona_corrections",
    "growth_reconcile",
    "evolution_trigger",
    # M5: the three working summaries are rebuilt from the records the earlier
    # steps just settled (reflections reconciled, deletions retried), so this
    # runs after them and before the optional compaction.
    "working_summary",
    # M6-A: proactive candidates are derived after the records they rest on are
    # settled (reflections reconciled, deletions retried) -- design section 12.1
    # puts "proactive candidate maintenance" in the background pass, not at the
    # front of a turn. Deriving here only registers candidates; nothing is sent
    # without an explicit dispatch and a pre-send recheck.
    "proactive_candidates",
    "deletion_retry",
    "journal_compaction",
)

#: Step statuses used by MaintenanceResult.steps.
STEP_OK = "ok"
STEP_SKIPPED = "skipped"
STEP_FAILED = "failed"


@dataclass(frozen=True)
class MaintenanceSettings:
    """Loop cadence and per-round switches (no global config in the core)."""

    interval_seconds: float = 60.0
    enable_reflection: bool = True
    enable_archive: bool = True
    enable_growth_reconcile: bool = True
    #: M5: refresh the working-summary audit cache. The summaries themselves are
    #: projections re-derived on every read, so turning this off costs the audit
    #: row only -- never the answer a caller gets.
    enable_working_summary: bool = True
    #: M6-A: derive proactive candidates in the background pass.
    enable_proactive: bool = True
    enable_deletion_retry: bool = True
    enable_compaction: bool = False
    synthesis_limit: int = 1
    outbox_limit: int = 20
    deletion_retry_limit: int = 20

    def __post_init__(self) -> None:
        if isinstance(self.interval_seconds, bool) or not isinstance(
            self.interval_seconds, (int, float),
        ):
            raise ValueError("interval_seconds must be a number")
        if float(self.interval_seconds) <= 0:
            raise ValueError("interval_seconds must be positive")
        for name in (
            "synthesis_limit", "outbox_limit", "deletion_retry_limit",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")


@dataclass(frozen=True)
class MaintenanceResult:
    """Outcome of one pass; steps maps name -> {status, detail?}."""

    character: str
    status: str
    steps: Mapping[str, Mapping[str, Any]]
    processed: int
    pending: int
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in ("completed", "partial"):
            raise ValueError("status must be completed or partial")
        if self.processed < 0 or self.pending < 0:
            raise ValueError("processed/pending must be nonnegative")


class MaintenanceService:
    """Run the ordered maintenance pass once, or loop it in one task.

    Constructor dependencies are duck-typed so composition can pass the B03
    deletion service and B05 reflection/persona services directly. A missing
    optional dependency turns its step into skipped, never a crash.
    """

    def __init__(
        self,
        *,
        dispatcher: Any,
        deletion_service: Any,
        reflection_service: Any,
        persona_service: Any = None,
        journal: Any = None,
        outbox: Any = None,
        clock: Clock | None = None,
        settings: MaintenanceSettings | None = None,
        recover: Callable[..., Any] | None = None,
        migrations: Sequence[Callable[..., Any]] = (),
        compaction_seeds: Callable[..., Any] | None = None,
        scheduler: Any = None,
        working_summary: Any = None,
        proactive: Any = None,
        proactive_subject: str = "private",
    ) -> None:
        self._dispatcher = dispatcher
        self._deletion_service = deletion_service
        self._reflection_service = reflection_service
        self._persona_service = persona_service
        self._journal = journal
        self._outbox = outbox
        self._clock = clock
        self._settings = settings or MaintenanceSettings()
        self._recover = recover
        self._migrations = tuple(migrations or ())
        self._compaction_seeds = compaction_seeds
        self._scheduler = scheduler
        #: M5: (projector, cache store) or None. Refreshing is bookkeeping: the
        #: next read re-derives the summary whether or not this ever ran.
        self._working_summary = working_summary
        #: M6-A: the proactive service. Deriving candidates is the only step that
        #: touches it; sending is never part of a maintenance round.
        self._proactive = proactive
        self._proactive_subject = str(proactive_subject or "private")
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._stops: dict[str, asyncio.Event] = {}
        self._wakes: dict[str, asyncio.Event] = {}
        self._last: dict[str, MaintenanceResult] = {}

    @property
    def settings(self) -> MaintenanceSettings:
        return self._settings

    # -- one pass -------------------------------------------------------

    async def run_once(self, character: Any) -> MaintenanceResult:
        """Run every enabled step once; never raises."""
        name = str(character or "").strip()
        steps: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        processed = 0
        pending = 0
        if not name:
            steps["validate"] = {
                "status": STEP_FAILED, "detail": "character must be nonempty",
            }
            return MaintenanceResult(
                "", "partial", steps, 0, 0, "character must be nonempty",
            )

        def record(step: str, status: str, detail: Any = None) -> None:
            entry: dict[str, Any] = {"status": status}
            if detail not in (None, ""):
                entry["detail"] = str(detail)
            steps[step] = entry

        def failed(step: str, exc: BaseException) -> None:
            text = f"{type(exc).__name__}: {exc}".strip()
            record(step, STEP_FAILED, text)
            errors.append(f"{step}: {text}")

        try:
            # 1. reconcile the journal tail before any replay/write.
            if self._recover is None:
                record("reconcile", STEP_SKIPPED, "recover not wired")
            else:
                try:
                    outcome = await self._call_hook(self._recover, name)
                    paused = bool(getattr(outcome, "paused", False))
                    reason = getattr(outcome, "reason", None)
                    if paused:
                        record(
                            "reconcile", STEP_FAILED,
                            f"paused: {reason or 'reconcile_paused'}",
                        )
                        errors.append(f"reconcile: {reason or 'paused'}")
                    else:
                        applied = getattr(outcome, "applied", None)
                        record(
                            "reconcile", STEP_OK,
                            None if applied is None else f"applied={applied}",
                        )
                except Exception as exc:  # noqa: BLE001
                    failed("reconcile", exc)

            # 2. replay the outbox (must be after reconcile).
            if self._dispatcher is None:
                record("outbox", STEP_SKIPPED, "dispatcher not wired")
            else:
                try:
                    result = await self._call_optional_limit(
                        self._dispatcher.run_pending,
                        name,
                        self._settings.outbox_limit,
                    )
                    payload = dict(result or {})
                    processed += int(payload.get("processed") or 0)
                    pending = int(payload.get("pending") or 0)
                    record(
                        "outbox", STEP_OK,
                        "processed={processed} failed={failed} pending={pending} "
                        "skipped={skipped} dead_letter={dead_letter}".format(
                            processed=payload.get("processed", 0),
                            failed=payload.get("failed", 0),
                            pending=payload.get("pending", 0),
                            skipped=payload.get("skipped", 0),
                            dead_letter=payload.get("dead_letter", 0),
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    failed("outbox", exc)

            # 3. one-shot migrations (idempotent by contract).
            if not self._migrations:
                record("migrations", STEP_SKIPPED, "none configured")
            else:
                migration_errors: list[str] = []
                completed = 0
                for hook in self._migrations:
                    try:
                        await self._call_hook(hook, name)
                        completed += 1
                    except Exception as exc:  # noqa: BLE001
                        migration_errors.append(
                            f"{type(exc).__name__}: {exc}".strip(),
                        )
                if migration_errors:
                    record(
                        "migrations", STEP_FAILED,
                        f"{completed} ok, {len(migration_errors)} failed: "
                        f"{migration_errors[0]}",
                    )
                    errors.append(f"migrations: {migration_errors[0]}")
                else:
                    record("migrations", STEP_OK, f"{completed} hook(s)")

            # 4-6. reflection synthesis, promotion and archive.
            if not self._settings.enable_reflection:
                record("synthesize", STEP_SKIPPED, "disabled")
                record("promote", STEP_SKIPPED, "disabled")
            else:
                if self._settings.synthesis_limit <= 0:
                    record("synthesize", STEP_SKIPPED, "synthesis_limit=0")
                elif self._reflection_service is None:
                    record("synthesize", STEP_SKIPPED, "reflection service not wired")
                else:
                    try:
                        statuses: list[str] = []
                        for _ in range(self._settings.synthesis_limit):
                            # The background pass is the one caller that
                            # honours the normal synthesis interval (RO02):
                            # the service merges new evidence and waits instead
                            # of paying for another call inside the window.
                            outcome = await self._call_optional_flag(
                                self._reflection_service.synthesize, name,
                                respect_interval=True,
                            )
                            status = _outcome_status(outcome)
                            statuses.append(status)
                            if status != "completed":
                                break
                        record("synthesize", STEP_OK, ",".join(statuses))
                    except Exception as exc:  # noqa: BLE001
                        failed("synthesize", exc)
                if self._reflection_service is None:
                    record("promote", STEP_SKIPPED, "reflection service not wired")
                else:
                    try:
                        outcome = await self._call(
                            self._reflection_service.promote, name,
                        )
                        record("promote", STEP_OK, _outcome_status(outcome))
                    except Exception as exc:  # noqa: BLE001
                        failed("promote", exc)

            if not self._settings.enable_archive:
                record("archive", STEP_SKIPPED, "disabled")
            elif self._reflection_service is None:
                record("archive", STEP_SKIPPED, "reflection service not wired")
            else:
                try:
                    result = await self._call(
                        self._reflection_service.archive, name,
                    )
                    record("archive", STEP_OK, _summary(result))
                except Exception as exc:  # noqa: BLE001
                    failed("archive", exc)

            # 7. consume the persona correction queue.
            if self._persona_service is None:
                record("persona_corrections", STEP_SKIPPED, "not wired")
            else:
                try:
                    result = await self._call(
                        self._persona_service.resolve_corrections, name,
                    )
                    record("persona_corrections", STEP_OK, _summary(result))
                except Exception as exc:  # noqa: BLE001
                    failed("persona_corrections", exc)

            # 8. recompute growth support after corrections/deletions.
            if not self._settings.enable_growth_reconcile:
                record("growth_reconcile", STEP_SKIPPED, "disabled")
            elif self._reflection_service is None:
                record(
                    "growth_reconcile", STEP_SKIPPED,
                    "reflection service not wired",
                )
            else:
                try:
                    result = await self._call(
                        self._reflection_service.reconcile_growth_support, name,
                    )
                    record("growth_reconcile", STEP_OK, _summary(result))
                except Exception as exc:  # noqa: BLE001
                    failed("growth_reconcile", exc)

            # 8b. optional background evolution trigger (C03); scheduler=None
            #     keeps the historical step set unchanged.
            if self._scheduler is not None:
                try:
                    result = await self._call(self._scheduler.evaluate, name)
                    outcome = _outcome_status(result)
                    detail = _scheduler_summary(result)
                    if outcome == "failed":
                        record("evolution_trigger", STEP_FAILED, detail)
                        errors.append(f"evolution_trigger: {detail}")
                    elif outcome == "skipped":
                        record("evolution_trigger", STEP_SKIPPED, detail)
                    else:
                        record("evolution_trigger", STEP_OK, detail)
                except Exception as exc:  # noqa: BLE001
                    failed("evolution_trigger", exc)

            # 8c. rebuild the three working summaries (M5). This is the only
            #     writer of the audit row; the summaries a caller receives are
            #     always re-derived from the records, so a failure here degrades
            #     observability and nothing else.
            if not self._settings.enable_working_summary:
                record("working_summary", STEP_SKIPPED, "disabled")
            elif self._working_summary is None:
                record("working_summary", STEP_SKIPPED, "summary projector not wired")
            else:
                try:
                    detail = await self._call_hook(
                        self._refresh_working_summary, name,
                    )
                    record("working_summary", STEP_OK, detail)
                except Exception as exc:  # noqa: BLE001
                    failed("working_summary", exc)

            # 8d. derive proactive candidates (M6-A). Deriving registers a reason
            #     and nothing else: no body is generated here, and a candidate
            #     still has to pass the pre-send recheck before it can be said.
            if not self._settings.enable_proactive:
                record("proactive_candidates", STEP_SKIPPED, "disabled")
            elif self._proactive is None:
                record("proactive_candidates", STEP_SKIPPED, "proactive not wired")
            else:
                try:
                    result = await self._call(
                        self._proactive.derive, name,
                        subject=self._proactive_subject,
                    )
                    payload = dict(result or {})
                    detail = "created={created} duplicates={duplicates} refused={refused}".format(
                        created=len(payload.get("created") or ()),
                        duplicates=len(payload.get("duplicates") or ()),
                        refused=len(payload.get("refused") or ()),
                    )
                    record("proactive_candidates", STEP_OK, detail)
                except Exception as exc:  # noqa: BLE001
                    failed("proactive_candidates", exc)

            # 9. retry pending/failed deletions (plugin step excluded inside
            #    DeletionService.retry); one failure does not stop the rest.
            if not self._settings.enable_deletion_retry:
                record("deletion_retry", STEP_SKIPPED, "disabled")
            elif self._deletion_service is None:
                record("deletion_retry", STEP_SKIPPED, "deletion service not wired")
            else:
                try:
                    listing = await self._call_optional_limit(
                        self._deletion_service.list,
                        name,
                        self._settings.deletion_retry_limit,
                    )
                    items = _deletion_items(listing)
                    targets = [
                        item for item in items
                        if str(item.get("status") or "") != "completed"
                    ]
                    retried = 0
                    retry_errors: list[str] = []
                    for item in targets:
                        deletion_id = str(
                            item.get("deletion_id") or item.get("id") or "",
                        ).strip()
                        if not deletion_id:
                            continue
                        try:
                            receipt = await self._call(
                                self._deletion_service.retry,
                                name, deletion_id,
                            )
                            retried += 1
                            receipt_status = _receipt_status(receipt)
                            if receipt_status == "failed":
                                retry_errors.append(
                                    f"{deletion_id}: status=failed",
                                )
                        except Exception as exc:  # noqa: BLE001
                            retry_errors.append(
                                f"{deletion_id}: {type(exc).__name__}: {exc}",
                            )
                    if retry_errors:
                        record(
                            "deletion_retry", STEP_FAILED,
                            f"retried={retried} failed={len(retry_errors)}: "
                            f"{retry_errors[0]}",
                        )
                        errors.append(f"deletion_retry: {retry_errors[0]}")
                    else:
                        record("deletion_retry", STEP_OK, f"retried={retried}")
                except Exception as exc:  # noqa: BLE001
                    failed("deletion_retry", exc)

            # 10. optional journal compaction after reconcile drained the tail.
            if not self._settings.enable_compaction:
                record("journal_compaction", STEP_SKIPPED, "disabled")
            elif self._journal is None or self._compaction_seeds is None:
                record(
                    "journal_compaction", STEP_SKIPPED,
                    "journal/seeds not wired",
                )
            else:
                try:
                    seeds = await self._call_hook(
                        self._compaction_seeds, name,
                    )
                    seed_rows = list(seeds or ())

                    def seed_events() -> list[Any]:
                        return seed_rows

                    dropped = await asyncio.to_thread(
                        self._journal.compact_if_needed, name, seed_events,
                    )
                    record("journal_compaction", STEP_OK, f"dropped={dropped}")
                except Exception as exc:  # noqa: BLE001
                    failed("journal_compaction", exc)
        except Exception as exc:  # noqa: BLE001 - defensive: never raise
            failed("run_once", exc)

        status = (
            "completed"
            if all(step.get("status") != STEP_FAILED for step in steps.values())
            else "partial"
        )
        return MaintenanceResult(
            character=name,
            status=status,
            steps=steps,
            processed=processed,
            pending=pending,
            error=errors[0] if errors else None,
        )

    # -- loop lifecycle -------------------------------------------------

    async def start(
        self,
        character: Any,
        *,
        interval_seconds: float | None = None,
        max_rounds: int | None = None,
    ) -> asyncio.Task[Any]:
        """Start the single loop task; idempotent while it is still running."""
        name = str(character or "").strip()
        if not name:
            raise ValueError("character must be nonempty")
        interval = (
            self._settings.interval_seconds
            if interval_seconds is None
            else float(interval_seconds)
        )
        if interval <= 0:
            raise ValueError("interval_seconds must be positive")
        if max_rounds is not None:
            if isinstance(max_rounds, bool) or int(max_rounds) < 0:
                raise ValueError("max_rounds must be a nonnegative integer")
            max_rounds = int(max_rounds)
        existing = self._tasks.get(name)
        if existing is not None and not existing.done():
            return existing
        if existing is not None and existing.done():
            self._tasks.pop(name, None)
            self._stops.pop(name, None)
        stop_event = asyncio.Event()
        wake_event = asyncio.Event()
        task = asyncio.create_task(
            self._loop(name, interval, max_rounds, stop_event, wake_event),
            name=f"rin-memory-maintenance:{name}",
        )
        self._tasks[name] = task
        self._stops[name] = stop_event
        self._wakes[name] = wake_event
        return task

    def wake(self, character: str) -> None:
        task = self._tasks.get(character)
        event = self._wakes.get(character)
        if task is not None and not task.done() and event is not None:
            task.get_loop().call_soon_threadsafe(event.set)

    async def _loop(
        self,
        character: str,
        interval: float,
        max_rounds: int | None,
        stop_event: asyncio.Event,
        wake_event: asyncio.Event,
    ) -> None:
        rounds = 0
        try:
            while not stop_event.is_set():
                wake_event.clear()
                if max_rounds is not None and rounds >= max_rounds:
                    break
                try:
                    self._last[character] = await self.run_once(character)
                except Exception as exc:  # noqa: BLE001 - loop must survive
                    self._last[character] = MaintenanceResult(
                        character, "partial", {}, 0, 0,
                        f"{type(exc).__name__}: {exc}",
                    )
                rounds += 1
                if max_rounds is not None and rounds >= max_rounds:
                    break
                try:
                    await asyncio.wait_for(wake_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise

    async def stop(self, character: Any = None) -> None:
        """Signal and await the loop task(s); no task is left behind."""
        if character is None:
            names = list(self._tasks)
        else:
            names = [str(character or "").strip()]
        for name in names:
            if not name:
                continue
            stop_event = self._stops.get(name)
            if stop_event is not None:
                stop_event.set()
                self._wakes[name].set()
            task = self._tasks.get(name)
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - run_once already isolates
                    pass
            self._tasks.pop(name, None)
            self._stops.pop(name, None)
            self._wakes.pop(name, None)

    def running(self, character: Any = None) -> bool:
        if character is None:
            return any(not task.done() for task in self._tasks.values())
        task = self._tasks.get(str(character or "").strip())
        return task is not None and not task.done()

    def task(self, character: Any = None) -> asyncio.Task[Any] | None:
        """The loop task for this character (None when never started/stopped)."""
        if character is None:
            if len(self._tasks) == 1:
                return next(iter(self._tasks.values()))
            return None
        return self._tasks.get(str(character or "").strip())

    def last_result(self, character: Any = None) -> MaintenanceResult | None:
        if character is None:
            if len(self._last) == 1:
                return next(iter(self._last.values()))
            return None
        return self._last.get(str(character or "").strip())

    async def wait(self, character: Any = None, timeout: float | None = None) -> Any:
        """Wait for the loop task(s); does not cancel them on timeout."""
        if character is None:
            tasks = list(self._tasks.values())
            if not tasks:
                return None
            if len(tasks) == 1:
                task = tasks[0]
                if timeout is None:
                    return await task
                return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            return await asyncio.gather(*tasks)
        name = str(character or "").strip()
        task = self._tasks.get(name)
        if task is None:
            return None
        if timeout is None:
            return await task
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    # -- helpers --------------------------------------------------------

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Call a sync or async dependency without blocking the loop."""
        if not callable(fn):
            raise TypeError(f"{fn!r} is not callable")
        if inspect.iscoroutinefunction(fn):
            return await fn(*args, **kwargs)
        value = await asyncio.to_thread(
            functools.partial(fn, *args, **kwargs),
        )
        if inspect.isawaitable(value):
            return await value
        return value

    async def _call_optional_limit(
        self, fn: Any, character: str, limit: int,
    ) -> Any:
        """Pass limit= only when the dependency accepts it (fake-friendly)."""
        try:
            return await self._call(fn, character, limit=limit)
        except TypeError:
            return await self._call(fn, character)

    async def _call_optional_flag(
        self, fn: Any, character: str, *, respect_interval: bool = True,
    ) -> Any:
        """Pass an opt-in flag only when the dependency accepts it."""
        try:
            return await self._call(
                fn, character, respect_interval=respect_interval,
            )
        except TypeError:
            return await self._call(fn, character)

    async def _refresh_working_summary(self, character: str) -> str:
        """Rebuild the audit summary row; returns a one-line detail."""
        pair = self._working_summary
        projector = getattr(pair, "projector", None) or (pair[0] if isinstance(pair, tuple) else pair)
        store = getattr(pair, "store", None) or (pair[1] if isinstance(pair, tuple) else None)
        if projector is None or store is None:
            return "not wired"
        if hasattr(projector, "refresh_cache"):
            payload = await asyncio.to_thread(
                projector.refresh_cache, character, store,
            )
            key = str(payload.get("rebuild_key") or "")
            return f"rebuild_key={key}" if key else "refreshed"
        payload = await asyncio.to_thread(projector.build, character)
        await asyncio.to_thread(
            store.record, character, getattr(payload, "subject", "private"),
            {
                "rebuild_key": getattr(payload, "rebuild_key", ""),
                "built_at": getattr(payload, "built_at", None),
                "sections": {},
                "source_versions": dict(
                    getattr(payload, "source_versions", {}) or {}
                ),
            },
        )
        return "refreshed"

    async def _call_hook(self, fn: Any, character: str) -> Any:
        """Hooks may take the character or no argument at all."""
        if _accepts_character(fn):
            return await self._call(fn, character)
        return await self._call(fn)


def _accepts_character(fn: Any) -> bool:
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
        elif parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
    return positional >= 1


def _outcome_status(outcome: Any) -> str:
    if isinstance(outcome, Mapping):
        status = outcome.get("status")
    else:
        status = getattr(outcome, "status", None)
    return str(status or "completed")


def _receipt_status(receipt: Any) -> str:
    if isinstance(receipt, Mapping):
        status = receipt.get("status")
    else:
        status = getattr(receipt, "status", None)
    return str(status or "")


def _scheduler_summary(result: Any) -> str:
    if isinstance(result, Mapping):
        status = result.get("status")
        reason = result.get("reason")
        run_id = result.get("run_id")
    else:
        status = getattr(result, "status", None)
        reason = getattr(result, "reason", None)
        run_id = getattr(result, "run_id", None)
    parts = [f"status={status or 'unknown'}"]
    if reason:
        parts.append(f"reason={reason}")
    if run_id:
        parts.append(f"run={run_id}")
    return " ".join(parts)


def _summary(result: Any) -> str:
    if isinstance(result, Mapping):
        parts = []
        for key in ("resolved", "dropped", "skipped", "remaining", "changed",
                    "narrowed", "withdrawn", "archived", "incremented"):
            if key in result:
                parts.append(f"{key}={result[key]}")
        return " ".join(parts) or "ok"
    return "ok"


def _deletion_items(listing: Any) -> list[dict[str, Any]]:
    if isinstance(listing, Mapping):
        raw = listing.get("items")
    else:
        raw = listing
    if not isinstance(raw, (list, tuple)):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


__all__ = [
    "MaintenanceResult",
    "MaintenanceService",
    "MaintenanceSettings",
    "STEP_FAILED",
    "STEP_OK",
    "STEP_ORDER",
    "STEP_SKIPPED",
]
