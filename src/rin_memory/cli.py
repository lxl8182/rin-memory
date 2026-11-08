"""Standalone entry point for rin-memory (B07/B14).

Commands, all offline and host-independent:

* demo          - the ten-section in-process demonstration from
  rin_memory.__main__ (fixed doubles, no network, temp data roots);
* run           - one explicit data root and character: validate the root, take
  the root ownership lock (S01), build the core, recover the journal tail, then
  run one maintenance round (--once) or the loop (--loop);
* migrate       - detect every store format, back up the root, apply idempotent
  format migrations and verify (--dry-run prints the plan only);
* rollback      - restore a verified backup and, unless --no-preserve-new,
  merge post-backup conversations/corrections/deletions so nothing regresses;
* package-check - uv build a wheel, inspect its contents, install it into an
  isolated temporary venv and run import + demo smoke (reports skipped when uv
  is unavailable; never uses the network).

No host imports, no uvicorn, no network, no default NEKO/Documents path: every
byte written by run/migrate/rollback stays under the explicit roots.

Ownership (docs/standalone-service.md §2.2): `run`, `migrate` and `rollback`
all take the same root lock as the standalone service, the embedded host and
the writable console, so a second writable entry point is refused before it
touches data. The per-character lock below remains as a coarse guard for the
CLI's own maintenance loop; it is no longer the cross-process arbiter.

Exit codes: 0 success, 2 argument/path/format error, 3 single-writer conflict,
1 runtime failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import threading
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import RootLockConflict
from .persistence.ownership import lock_descriptor as _lock_fd_impl
from .persistence.ownership import unlock_descriptor as _unlock_fd_impl

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_LOCKED = 3

#: One writer per character directory. The file holds pid + timestamp + character.
LOCK_FILENAME = ".rin_memory.writer.lock"
#: Path segments that identify real user data / the host checkout.
_FORBIDDEN_SEGMENTS = frozenset({"nekodata", "documents", "n.e.k.o"})
#: Substrings that always identify real user data.
_FORBIDDEN_SUBSTRINGS = ("nekodata", "n.e.k.o")

#: Held per-character locks in this process: normcased path -> open descriptor.
_CHAR_LOCKS: dict[str, int] = {}
_CHAR_LOCK_GUARD = threading.Lock()


class CliError(Exception):
    """Base class for errors that map onto a documented exit code."""

    exit_code = EXIT_ERROR

    def __init__(self, message: str, *, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = str(message)
        self.detail = dict(detail or {})


class UsageError(CliError):
    """Bad argument, unsafe path or unreadable input: exit 2."""

    exit_code = EXIT_USAGE


class LockConflictError(CliError):
    """Another live writer already owns this character directory: exit 3."""

    exit_code = EXIT_LOCKED


# -- path and lock helpers ---------------------------------------------------


def validate_data_root(path: Any, *, label: str) -> Path:
    """Resolve one explicit root and refuse real user-data locations.

    The CLI must never guess or touch Documents / the N.E.K.O checkout / the
    user's nekodata root: those are only reachable through the host adapter
    (B08), which owns write permits and backups.
    """
    raw = str(path or "").strip()
    if not raw:
        raise UsageError(f"{label} must not be empty")
    candidate = Path(raw).expanduser()
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise UsageError(f"{label} cannot be resolved: {exc}") from exc
    lowered = str(resolved).replace("\\", "/").casefold()
    for needle in _FORBIDDEN_SUBSTRINGS:
        if needle in lowered:
            raise UsageError(
                f"{label} {str(resolved)!r} looks like real user data "
                f"({needle!r}); the standalone entry only accepts an explicit "
                f"non-user root",
            )
    for part in resolved.parts:
        if part.casefold() in _FORBIDDEN_SEGMENTS:
            raise UsageError(
                f"{label} {str(resolved)!r} contains a forbidden segment "
                f"{part!r}; use a temporary or dedicated directory",
            )
    if resolved.exists() and not resolved.is_dir():
        raise UsageError(f"{label} {str(resolved)!r} is not a directory")
    return resolved


def _read_lock_pid(lock_path: Path) -> int | None:
    try:
        raw = lock_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, Mapping):
        return None
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return int(pid)


def _lock_fd(fd: int, lock_path: Path, character: str) -> None:
    """Take the per-character OS lock or raise a live-owner conflict."""
    try:
        _lock_fd_impl(fd)
    except OSError as exc:
        raise LockConflictError(
            f"another writer owns {str(lock_path.parent)!r}",
            detail={
                "character": character,
                "lock_path": str(lock_path),
                "owner_pid": _read_lock_pid(lock_path),
            },
        ) from exc


def _unlock_fd(fd: int) -> None:
    _unlock_fd_impl(fd)


def acquire_writer_lock(character_dir: Path, *, character: str) -> Path:
    """Take the per-character guard for this process; return the lock path.

    Cross-process exclusion is an OS file lock, not a pid probe. The previous
    implementation created the file with ``O_CREAT|O_EXCL`` and wrote the pid
    afterwards, then treated any unreadable file as "stale" and deleted it: a
    second process landing in that window removed a lock that was live and both
    then wrote the same character directory. That is exactly the behaviour
    docs/standalone-service.md §2.2 requires fixing, so the pid is now written
    only after the lock is held and is never used to decide ownership.

    The returned path is kept for callers that pass it back to
    `release_writer_lock`; the descriptor that actually holds the lock lives in
    the module-level table below.
    """
    character_dir.mkdir(parents=True, exist_ok=True)
    lock_path = character_dir / LOCK_FILENAME
    key = os.path.normcase(str(lock_path))
    with _CHAR_LOCK_GUARD:
        if key in _CHAR_LOCKS:
            raise LockConflictError(
                f"this process already owns {str(character_dir)!r}",
                detail={"character": character, "lock_path": str(lock_path)},
            )
        try:
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            raise CliError(f"cannot open writer lock {str(lock_path)!r}: {exc}")
        try:
            _lock_fd(fd, lock_path, character)
        except LockConflictError:
            os.close(fd)
            raise
        _CHAR_LOCKS[key] = fd
    payload = json.dumps({
        "pid": os.getpid(),
        "character": character,
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False).encode("utf-8")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        os.ftruncate(fd, len(payload))
        os.fsync(fd)
    except OSError:
        pass
    return lock_path


def release_writer_lock(lock_path: Path | None) -> None:
    """Drop the per-character guard; idempotent, never unlinks the file."""
    if lock_path is None:
        return
    key = os.path.normcase(str(lock_path))
    with _CHAR_LOCK_GUARD:
        fd = _CHAR_LOCKS.pop(key, None)
    if fd is None:
        return
    try:
        _unlock_fd(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def acquire_root_lock(memory_root: Path, *, mode: str = "cli"):
    """Take the one writable-owner lock on the data root (see ownership.py)."""
    from .persistence.ownership import acquire_root_lock as _acquire

    return _acquire(memory_root, mode=mode)


def release_root_lock(lock) -> None:
    if lock is not None:
        lock.release()


# -- summary helpers ---------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return str(value)


def result_to_dict(result: Any) -> dict[str, Any]:
    """Normalise a MaintenanceResult-like object into a JSON summary."""
    if result is None:
        return {}
    if is_dataclass(result) and not isinstance(result, type):
        data = asdict(result)
    elif isinstance(result, Mapping):
        data = dict(result)
    elif hasattr(result, "__dict__"):
        data = dict(vars(result))
    else:
        data = {"value": result}
    return {str(key): _jsonable(value) for key, value in data.items()}


def _print_json(payload: Mapping[str, Any], *, stream: Any = None) -> None:
    print(
        json.dumps(_jsonable(dict(payload)), ensure_ascii=False, indent=2),
        file=stream or sys.stdout,
    )


def _summary(
    *,
    mode: str,
    character: str,
    memory_root: Path,
    local_state_root: Path | None,
    result: Any = None,
    status: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    data = result_to_dict(result)
    summary: dict[str, Any] = {
        "character": character,
        "memory_root": str(memory_root),
        "local_state_root": str(local_state_root) if local_state_root else None,
        "mode": mode,
        "status": status or str(data.get("status") or "completed"),
    }
    for key in ("steps", "processed", "pending", "error"):
        if key in data:
            summary[key] = data[key]
    if extra:
        summary.update(_jsonable(dict(extra)))
    return summary


# -- core wiring -------------------------------------------------------------


def _resolve_services(core: Any) -> tuple[Any, Any]:
    """Return (dispatcher, maintenance), preferring the composition wiring."""
    from .domain.maintenance import MaintenanceService
    from .domain.outbox_dispatch import OutboxDispatcher

    dispatcher = getattr(core, "outbox_dispatcher", None)
    if dispatcher is None:
        dispatcher = OutboxDispatcher(outbox=core.outbox)
        dispatcher.register_bulk(
            "extract_facts",
            lambda character: core.extraction.process_outbox(character),
        )
    maintenance = getattr(core, "maintenance", None)
    if maintenance is None:
        maintenance = MaintenanceService(
            dispatcher=dispatcher,
            deletion_service=core.deletion_service,
            reflection_service=core.reflection_service,
            persona_service=getattr(core, "persona_service", None),
            journal=core.journal,
            outbox=core.outbox,
            clock=core.clock,
            recover=core.recover,
        )
    return dispatcher, maintenance


def _install_stop_handlers(stop_event: asyncio.Event) -> list[int]:
    """Turn SIGINT/SIGTERM into one asyncio event; Windows-safe fallback."""
    loop = asyncio.get_running_loop()
    installed: list[int] = []
    signals = [signal.SIGINT]
    for name in ("SIGTERM", "SIGBREAK"):
        extra = getattr(signal, name, None)
        if extra is not None:
            signals.append(extra)
    for sig in signals:
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                signal.signal(
                    sig,
                    lambda *_args, _sig=sig: loop.call_soon_threadsafe(
                        stop_event.set,
                    ),
                )
            except (ValueError, OSError):
                continue
        installed.append(int(sig))
    return installed


async def _run_async(args: argparse.Namespace) -> int:
    from .adapters.composition import build_memory_core

    character = args.character
    memory_root = Path(args.memory_root)
    local_state_root = (
        Path(args.local_state_root) if args.local_state_root else None
    )
    core = build_memory_core(
        memory_root=memory_root,
        local_state_root=local_state_root,
    )
    recovery = core.recover(character)
    _dispatcher, maintenance = _resolve_services(core)

    if args.loop:
        first = await maintenance.run_once(character)
        await maintenance.start(
            character, interval_seconds=float(args.interval_seconds),
        )
        stop_event = asyncio.Event()
        _install_stop_handlers(stop_event)
        started_at = time.monotonic()
        await stop_event.wait()
        await maintenance.stop()
        elapsed = round(time.monotonic() - started_at, 3)
        # last_result is a method on MaintenanceService (B07 T1); tolerate both
        # an attribute and a callable so the CLI does not depend on internals.
        getter = getattr(maintenance, "last_result", None)
        final = None
        if callable(getter):
            try:
                final = getter(character)
            except TypeError:
                final = None
        elif getter is not None:
            final = getter
        if final is None:
            final = first
        _print_json(_summary(
            mode="loop",
            character=character,
            memory_root=memory_root,
            local_state_root=local_state_root,
            result=final,
            status="stopped",
            extra={
                "loop_seconds": elapsed,
                "interval_seconds": float(args.interval_seconds),
                "recovered": result_to_dict(recovery),
                "initial": result_to_dict(first),
            },
        ))
        return EXIT_OK

    result = await maintenance.run_once(character)
    _print_json(_summary(
        mode="once",
        character=character,
        memory_root=memory_root,
        local_state_root=local_state_root,
        result=result,
        extra={"recovered": result_to_dict(recovery)},
    ))
    return EXIT_OK


# -- commands ----------------------------------------------------------------


def _run_command(args: argparse.Namespace) -> int:
    """Validate inputs, take the root and character locks, then maintain."""
    try:
        memory_root = validate_data_root(args.memory_root, label="memory root")
        local_state_root = (
            validate_data_root(args.local_state_root, label="local state root")
            if args.local_state_root
            else None
        )
        from .persistence.roots import validate_character_name

        character = validate_character_name(args.character)
        interval = float(args.interval_seconds)
        if interval <= 0:
            raise UsageError("interval seconds must be positive")
    except ValueError as exc:
        _print_json({"ok": False, "error": "invalid_argument", "message": str(exc)},
                    stream=sys.stderr)
        return EXIT_USAGE
    except CliError as exc:
        _print_json(
            {"ok": False, "error": "invalid_argument", "message": exc.message,
             **exc.detail},
            stream=sys.stderr,
        )
        return exc.exit_code

    try:
        root_lock = acquire_root_lock(memory_root, mode="cli")
    except RootLockConflict as exc:
        _print_json(
            {"ok": False, "error": "root_lock_conflict",
             "message": str(exc), **exc.detail},
            stream=sys.stderr,
        )
        return EXIT_LOCKED

    character_dir = memory_root / character
    lock_path: Path | None = None
    try:
        lock_path = acquire_writer_lock(character_dir, character=character)
    except CliError as exc:
        release_root_lock(root_lock)
        _print_json(
            {"ok": False, "error": "writer_lock_conflict",
             "message": exc.message, **exc.detail},
            stream=sys.stderr,
        )
        return exc.exit_code

    args.memory_root = str(memory_root)
    args.local_state_root = (
        str(local_state_root) if local_state_root else None
    )
    args.character = character
    try:
        return asyncio.run(_run_async(args))
    except KeyboardInterrupt:  # pragma: no cover - interactive Ctrl+C
        return EXIT_OK
    except CliError as exc:
        _print_json(
            {"ok": False, "error": "cli_error", "message": exc.message,
             **exc.detail},
            stream=sys.stderr,
        )
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - one JSON error, documented exit
        _print_json(
            {"ok": False, "error": "runtime_error",
             "message": f"{type(exc).__name__}: {exc}"},
            stream=sys.stderr,
        )
        return EXIT_ERROR
    finally:
        release_writer_lock(lock_path)
        release_root_lock(root_lock)


def _demo_command(_args: argparse.Namespace) -> int:
    try:
        from .__main__ import run as run_demo

        results = asyncio.run(run_demo())
    except Exception as exc:  # noqa: BLE001
        _print_json(
            {"ok": False, "error": "runtime_error",
             "message": f"{type(exc).__name__}: {exc}"},
            stream=sys.stderr,
        )
        return EXIT_ERROR
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return EXIT_OK


# -- data-format migration / rollback / package (B14) ------------------------

#: Migration error codes that mean "bad input or unsupported format" (exit 2).
_USAGE_ERROR_CODES = frozenset({
    "bad_argument",
    "unknown_schema",
    "backup_exists",
    "backup_invalid",
    "backup_corrupt",
    "invalid_argument",
    "root_lock_conflict",
})


def _command_error(
    error: str, message: str, *, detail: Mapping[str, Any] | None = None,
) -> int:
    payload: dict[str, Any] = {"ok": False, "error": error, "message": message}
    if detail:
        payload.update(_jsonable(dict(detail)))
    _print_json(payload, stream=sys.stderr)
    return EXIT_USAGE if error in _USAGE_ERROR_CODES else EXIT_ERROR


def _migrate_command(args: argparse.Namespace) -> int:
    """Detect, back up, migrate and verify one explicit memory root."""
    try:
        from .migration import MigrationError, migrate
    except Exception as exc:  # noqa: BLE001
        return _command_error(
            "runtime_error", f"{type(exc).__name__}: {exc}",
        )
    try:
        memory_root = validate_data_root(args.memory_root, label="memory root")
        backup_root = (
            validate_data_root(args.backup_root, label="backup root")
            if args.backup_root
            else None
        )
    except CliError as exc:
        return _command_error("invalid_argument", exc.message, detail=exc.detail)
    try:
        root_lock = acquire_root_lock(memory_root, mode="migration")
    except RootLockConflict as exc:
        return _command_error("root_lock_conflict", str(exc), detail=exc.detail)
    try:
        report = migrate(
            memory_root,
            dry_run=bool(args.dry_run),
            backup_root=backup_root,
        )
    except MigrationError as exc:
        return _command_error(exc.code, exc.message, detail=exc.report)
    except Exception as exc:  # noqa: BLE001
        return _command_error("runtime_error", f"{type(exc).__name__}: {exc}")
    finally:
        release_root_lock(root_lock)
    _print_json(report.to_dict())
    return EXIT_OK


def _rollback_command(args: argparse.Namespace) -> int:
    """Restore a verified backup, merging post-backup records by default."""
    try:
        from .migration import MigrationError, rollback
    except Exception as exc:  # noqa: BLE001
        return _command_error(
            "runtime_error", f"{type(exc).__name__}: {exc}",
        )
    try:
        memory_root = validate_data_root(args.memory_root, label="memory root")
        backup_root = validate_data_root(args.backup_root, label="backup root")
    except CliError as exc:
        return _command_error("invalid_argument", exc.message, detail=exc.detail)
    try:
        root_lock = acquire_root_lock(memory_root, mode="migration")
    except RootLockConflict as exc:
        return _command_error("root_lock_conflict", str(exc), detail=exc.detail)
    try:
        report = rollback(
            memory_root,
            backup_root,
            preserve_new=not bool(args.no_preserve_new),
        )
    except MigrationError as exc:
        return _command_error(exc.code, exc.message, detail=exc.report)
    except Exception as exc:  # noqa: BLE001
        return _command_error("runtime_error", f"{type(exc).__name__}: {exc}")
    finally:
        release_root_lock(root_lock)
    _print_json(report.to_dict())
    return EXIT_OK


def _run_package_check() -> dict[str, Any]:
    """Build a wheel, inspect it, install it into a temp venv and smoke it.

    Fully offline: uv runs with UV_OFFLINE=1 and pip install uses --no-index
    semantics through the same environment. A missing uv is reported as
    skipped, not as a failure.
    """
    import shutil
    import subprocess
    import tempfile
    import zipfile

    uv = shutil.which("uv")
    if uv is None:
        return {
            "ok": True,
            "status": "skipped",
            "reason": "uv is not available on PATH; wheel build/install smoke skipped",
        }
    package_root = Path(__file__).resolve().parents[2]
    workdir = Path(tempfile.mkdtemp(prefix="rin-memory-package-"))
    env = dict(os.environ)
    env["UV_OFFLINE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        dist = workdir / "dist"
        dist.mkdir(parents=True, exist_ok=True)
        build = subprocess.run(
            [uv, "build", "--wheel", "--out-dir", str(dist)],
            cwd=str(package_root), capture_output=True, text=True,
            timeout=300, env=env,
        )
        if build.returncode != 0:
            return {
                "ok": False, "status": "failed", "stage": "build",
                "reason": (build.stderr or build.stdout or "").strip()[-800:],
            }
        wheels = sorted(dist.glob("*.whl"))
        if not wheels:
            return {
                "ok": False, "status": "failed", "stage": "build",
                "reason": "uv build produced no wheel",
            }
        wheel = wheels[-1]
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
        forbidden = [
            name for name in names
            if name.startswith(("tests/", "app/", ".scratch/"))
            or "nekodata" in name
            or "__pycache__" in name
            or name.endswith(".pyc")
        ]
        if forbidden:
            return {
                "ok": False, "status": "failed", "stage": "contents",
                "reason": f"wheel contains forbidden paths: {forbidden[:5]}",
            }
        if not any(name.startswith("rin_memory/") for name in names):
            return {
                "ok": False, "status": "failed", "stage": "contents",
                "reason": "wheel does not contain the rin_memory package",
            }
        venv_dir = workdir / "venv"
        venv = subprocess.run(
            [uv, "venv", str(venv_dir), "--python", sys.executable],
            cwd=str(workdir), capture_output=True, text=True,
            timeout=300, env=env,
        )
        if venv.returncode != 0:
            return {
                "ok": False, "status": "failed", "stage": "venv",
                "reason": (venv.stderr or venv.stdout or "").strip()[-800:],
            }
        python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        install = subprocess.run(
            [uv, "pip", "install", "--python", str(python), "--no-deps",
             "--offline", str(wheel)],
            cwd=str(workdir), capture_output=True, text=True,
            timeout=300, env=env,
        )
        if install.returncode != 0:
            return {
                "ok": False, "status": "failed", "stage": "install",
                "reason": (install.stderr or install.stdout or "").strip()[-800:],
            }
        import_smoke = subprocess.run(
            [str(python), "-c",
             "import rin_memory; from rin_memory.cli import build_parser; "
             "print('import-ok', rin_memory.__version__ if hasattr(rin_memory, '__version__') else 'no-version')"],
            cwd=str(workdir), capture_output=True, text=True,
            timeout=120, env=env,
        )
        if import_smoke.returncode != 0:
            return {
                "ok": False, "status": "failed", "stage": "import",
                "reason": (import_smoke.stderr or import_smoke.stdout or "").strip()[-800:],
            }
        demo = subprocess.run(
            [str(python), "-m", "rin_memory"],
            cwd=str(workdir), capture_output=True, text=True,
            timeout=300, env=env,
        )
        demo_output = (demo.stdout or "").strip().splitlines()
        if demo.returncode != 0:
            return {
                "ok": False, "status": "failed", "stage": "demo",
                "reason": (demo.stderr or demo.stdout or "").strip()[-800:],
            }
        return {
            "ok": True,
            "status": "ok",
            "wheel": wheel.name,
            "wheel_bytes": wheel.stat().st_size,
            "wheel_files": len(names),
            "import_smoke": (import_smoke.stdout or "").strip()[-200:],
            "demo_exit": demo.returncode,
            "demo_lines": len(demo_output),
            "demo_last_line": demo_output[-1][:200] if demo_output else "",
            "offline": True,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False, "status": "failed", "stage": "timeout",
            "reason": f"{exc.cmd} timed out",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False, "status": "failed", "stage": "unexpected",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _package_check_command(_args: argparse.Namespace) -> int:
    result = _run_package_check()
    _print_json(result)
    if result.get("status") == "skipped":
        return EXIT_OK
    return EXIT_OK if result.get("ok") else EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rin-memory-run",
        description=(
            "Host-independent companion memory core "
            "(demo / run / migrate / rollback / package-check)"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "demo", help="run the ten-section in-process demonstration",
    )
    run_parser = sub.add_parser(
        "run", help="run one explicit data root and character",
    )
    run_parser.add_argument(
        "--memory-root", required=True,
        help="explicit memory root (one directory per character)",
    )
    run_parser.add_argument(
        "--character", required=True, help="character name (one path segment)",
    )
    run_parser.add_argument(
        "--local-state-root", default=None,
        help="explicit local-state root (defaults to <memory-root>/../local_state)",
    )
    mode = run_parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once", action="store_true",
        help="run one maintenance round and exit (default)",
    )
    mode.add_argument(
        "--loop", action="store_true",
        help="run the maintenance loop until SIGINT/SIGTERM",
    )
    run_parser.add_argument(
        "--interval-seconds", type=float, default=60.0,
        help="maintenance loop interval in seconds (default 60)",
    )
    migrate_parser = sub.add_parser(
        "migrate", help="detect, back up and migrate one explicit data root",
    )
    migrate_parser.add_argument(
        "--memory-root", required=True,
        help="explicit memory root (one directory per character)",
    )
    migrate_parser.add_argument(
        "--backup-root", default=None,
        help="explicit backup directory (defaults to <memory-root>.backup-<utc>)",
    )
    migrate_parser.add_argument(
        "--dry-run", action="store_true",
        help="print the planned steps without writing anything",
    )
    rollback_parser = sub.add_parser(
        "rollback", help="restore a verified backup, preserving post-backup data",
    )
    rollback_parser.add_argument(
        "--memory-root", required=True,
        help="explicit memory root to restore into",
    )
    rollback_parser.add_argument(
        "--backup-root", required=True,
        help="backup directory created by migrate",
    )
    rollback_parser.add_argument(
        "--no-preserve-new", action="store_true",
        help="restore the backup verbatim (do not merge post-backup records)",
    )
    sub.add_parser(
        "package-check",
        help="build a wheel, install it into an isolated venv and run the demo",
    )
    serve_parser = sub.add_parser(
        "serve", help="run the standalone /v1 HTTP service (S04)",
    )
    serve_parser.add_argument(
        "--memory-root", required=True,
        help="explicit memory root (one directory per character)",
    )
    serve_parser.add_argument(
        "--character", required=True, help="character directory to serve",
    )
    serve_parser.add_argument(
        "--local-state-root", default=None,
        help="explicit local-state root (defaults to <memory-root>/../local_state)",
    )
    serve_parser.add_argument(
        "--host", default="127.0.0.1",
        help="bind host (default 127.0.0.1; loopback only)",
    )
    serve_parser.add_argument(
        "--port", type=int, default=8788, help="bind port (default 8788)",
    )
    serve_parser.add_argument(
        "--model", action="append", metavar="TIER=MODEL",
        help="map a model tier (e.g. conversation=...); repeatable",
    )
    serve_parser.add_argument(
        "--model-base-url", default="",
        help="OpenAI-compatible base URL (e.g. https://host/v1)",
    )
    serve_parser.add_argument(
        "--model-api-key-env", default="",
        help="environment variable that holds the API key (never the key itself)",
    )
    serve_parser.add_argument(
        "--model-timeout-s", type=float, default=60.0,
        help="per-call model timeout in seconds (default 60)",
    )
    serve_parser.add_argument(
        "--interval-seconds", type=float, default=60.0,
        help="maintenance loop interval in seconds (default 60)",
    )
    serve_parser.add_argument(
        "--context-budget-ms", type=int, default=9000,
        help="context building budget in milliseconds (default 1000)",
    )
    serve_parser.add_argument(
        "--allow-origin", default="",
        help="single allowed CORS origin (default: none)",
    )
    return parser


def _serve_command(args: argparse.Namespace) -> int:
    """Run the standalone `/v1` service until SIGINT/SIGTERM (S04).

    Startup failures are reported as structured JSON and exit before the socket
    binds, so a misconfigured launch never looks like a healthy service.
    """
    try:
        from .adapters.model_http import ModelSettings
        from .service.memory_service import (
            MemoryService, ServiceSettings, ServiceStartupError,
        )
        from .service.server import MemoryApi, build_server
    except Exception as exc:  # noqa: BLE001 - optional transport/deps
        _print_json(
            {"ok": False, "error": "runtime_error",
             "message": f"{type(exc).__name__}: {exc}"},
            stream=sys.stderr,
        )
        return EXIT_ERROR

    models: dict[str, str] = {}
    for spec in args.model or ():
        if "=" not in spec:
            _print_json(
                {"ok": False, "error": "invalid_argument",
                 "message": f"--model expects tier=model, got {spec!r}"},
                stream=sys.stderr,
            )
            return EXIT_USAGE
        tier, model_name = spec.split("=", 1)
        tier, model_name = tier.strip(), model_name.strip()
        if not tier or not model_name:
            _print_json(
                {"ok": False, "error": "invalid_argument",
                 "message": f"empty tier or model in {spec!r}"},
                stream=sys.stderr,
            )
            return EXIT_USAGE
        models[tier] = model_name

    settings = ServiceSettings(
        memory_root=Path(args.memory_root),
        local_state_root=(
            Path(args.local_state_root) if args.local_state_root else None
        ),
        character=args.character,
        host=args.host,
        port=int(args.port),
        maintenance_interval_s=float(args.interval_seconds),
        context_budget_ms=int(args.context_budget_ms),
        model=ModelSettings(
            base_url=args.model_base_url or "",
            api_key_env=args.model_api_key_env or "",
            models=models,
            timeout_s=float(args.model_timeout_s),
        ),
        allow_origin=args.allow_origin or "",
    )

    async def _serve() -> int:
        try:
            service = MemoryService.start(settings)
        except ServiceStartupError as exc:
            _print_json(
                {"ok": False, "error": exc.code, "message": str(exc)},
                stream=sys.stderr,
            )
            return EXIT_LOCKED if exc.code == "root_lock_conflict" else EXIT_USAGE
        api = MemoryApi(service)
        try:
            server = build_server(
                api, host=settings.host, port=settings.port,
                allow_origin=settings.allow_origin,
            )
        except OSError as exc:
            await service.stop()
            _print_json(
                {"ok": False, "error": "bind_failed", "message": str(exc)},
                stream=sys.stderr,
            )
            return EXIT_ERROR
        try:
            await service.start_background()
            _print_json({
                "ok": True,
                "mode": "serve",
                "host": settings.host,
                "port": server.server_address[1],
                "character": settings.character,
                "memory_root": str(settings.memory_root),
                "capabilities": api.health(),
            })
            stop_event = asyncio.Event()
            _install_stop_handlers(stop_event)
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.2}, daemon=True)
            thread.start()
            await stop_event.wait()
            server.shutdown()
            thread.join(timeout=10)
        finally:
            server.server_close()
            await service.stop()
        return EXIT_OK

    try:
        return asyncio.run(_serve())
    except KeyboardInterrupt:  # pragma: no cover - interactive Ctrl+C
        return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse already printed usage
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    if args.command == "demo":
        return _demo_command(args)
    if args.command == "migrate":
        return _migrate_command(args)
    if args.command == "rollback":
        return _rollback_command(args)
    if args.command == "package-check":
        return _package_check_command(args)
    if args.command == "serve":
        return _serve_command(args)
    return _run_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
