"""B07 acceptance: companion state, outbox dispatch, maintenance and CLI.

The batch closes D1: a standalone core can commit/read the host-shaped
companion state with lease/version/idempotency rules, drain the outbox through
registered handlers, run one ordered maintenance pass (including pending
deletion retries) without leaving background tasks behind, and be driven from
an explicit data directory through the CLI with a single-writer lock.
"""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from test_b04_extraction import extraction_reply

from rin_memory.adapters.composition import build_memory_core
from rin_memory.cli import acquire_writer_lock, main as cli_main, release_writer_lock
from rin_memory.contracts import DeletionRequest, Scope
from rin_memory.domain.extraction import ExtractionSettings
from rin_memory.errors import NotConfiguredError


class MutableClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
        self._monotonic = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, **kwargs) -> None:
        delta = timedelta(**kwargs)
        self._now += delta
        self._monotonic += delta.total_seconds()


class FakeModel:
    """Deterministic extraction model stand-in (no network)."""

    def __init__(self) -> None:
        self.calls = []

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls.append(purpose)
        if purpose == "memory_fact_extraction":
            return extraction_reply([{
                "text": "用户喜欢晨跑", "importance": 7,
                "source": "user_observation",
            }], messages[0]["content"])
        return "[]"


class StateFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = MutableClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, name="memory", **kwargs):
        return build_memory_core(
            memory_root=self.root / name, clock=self.clock, **kwargs,
        )

    def _commit(self, core, request_id, ops, *, base_version=0, lease_id=None,
                internal=False):
        return core.state_service.commit("demo", {
            "ops": list(ops),
            "producer": "host",
            "instance": "inst1",
            "lease_id": lease_id,
            "request_id": request_id,
            "base_version": base_version,
            "internal": internal,
        })

    async def test_commit_read_version_and_mood_decay(self):
        core = self.build()
        core.recover("demo")
        lease = core.state_service.claim_lease(
            "demo", instance="inst1", lease_id="lease-1", ttl_seconds=90,
        )
        self.assertTrue(lease["ok"])
        lease_id = lease["lease"]["lease_id"]
        first = self._commit(core, "req-1", [
            {"op": "set_thought", "text": "用户在准备长跑",
             "source_event_ids": ["e1"]},
            {"op": "set_mood", "label": "开心", "intensity": 1.0,
             "cause": "约好周末跑步", "source_event_ids": ["e1"]},
        ], lease_id=lease_id)
        self.assertTrue(first.ok)
        self.assertEqual(first.version, 1)
        self.assertIn("用户在准备长跑", first.block)
        self.assertIn("开心", first.block)

        snapshot = core.state_service.read("demo")
        self.assertEqual(snapshot.version, 1)
        self.assertEqual(snapshot.subject, "private")
        self.assertIn("开心", snapshot.block)

        # Mood decays with wall-clock time but a read never writes a version.
        self.clock.advance(hours=6)
        decayed = core.state_service.read("demo")
        self.assertEqual(decayed.version, 1)
        self.assertIn("强度 0.50", decayed.block)

        # Restart (same clock) must not refresh the mood back to 1.0.
        reopened = self.build(name="memory")
        reopened.recover("demo")
        after_restart = reopened.state_service.read("demo")
        self.assertEqual(after_restart.version, 1)
        self.assertIn("强度 0.50", after_restart.block)

    async def test_request_idempotency_and_base_version_conflict(self):
        core = self.build()
        core.recover("demo")
        ops = [{"op": "set_thought", "text": "在准备长跑",
                "source_event_ids": ["e1"]}]
        first = self._commit(core, "req-1", ops, internal=True)
        self.assertTrue(first.ok)
        self.assertEqual(first.version, 1)
        duplicate = self._commit(core, "req-1", ops, internal=True)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.version, 1)
        self.assertFalse(duplicate.applied)

        conflict = self._commit(core, "req-2", [
            {"op": "set_thought", "text": "另一条", "source_event_ids": ["e2"]},
        ], base_version=0, internal=True)
        self.assertTrue(conflict.conflict)
        self.assertEqual(conflict.reason, "version_conflict")
        self.assertEqual(conflict.version, 1)
        self.assertEqual(conflict.current.version, 1)

        invalid = core.state_service.commit("demo", {
            "ops": [{"op": "not_an_op"}], "internal": True,
        })
        self.assertFalse(invalid.ok)
        self.assertEqual(invalid.reason, "invalid_request")

    async def test_lease_rules_and_internal_bypass(self):
        core = self.build()
        core.recover("demo")
        claimed = core.state_service.claim_lease(
            "demo", instance="inst1", lease_id="lease-1", ttl_seconds=90,
        )
        self.assertTrue(claimed["ok"])
        other = core.state_service.claim_lease(
            "demo", instance="inst2", lease_id="lease-2", ttl_seconds=90,
        )
        self.assertFalse(other["ok"])
        self.assertTrue(other["conflict"])

        # An external commit without a valid lease is rejected.
        stale = self._commit(core, "req-1", [
            {"op": "set_thought", "text": "x", "source_event_ids": ["e1"]},
        ], lease_id="wrong-lease")
        self.assertTrue(stale.conflict)
        self.assertEqual(stale.reason, "lease_stale")
        # Internal host writes are not blocked by the lease.
        internal = self._commit(core, "req-2", [
            {"op": "set_thought", "text": "host write", "source_event_ids": ["e1"]},
        ], internal=True)
        self.assertTrue(internal.ok)
        released = core.state_service.release_lease(
            "demo", lease_id="lease-1", instance="inst1",
        )
        self.assertTrue(released["released"])
        again = core.state_service.release_lease("demo", lease_id="lease-1")
        self.assertFalse(again["released"])

    async def test_outbox_dispatch_handles_extraction_and_unknown_ops(self):
        model = FakeModel()
        core = self.build(
            model=model, extraction_settings=ExtractionSettings(inline=False),
        )
        core.recover("demo")
        await core.extraction.ingest_turn(
            "demo",
            [{"type": "human", "data": {"content": "我最近开始晨跑"}}],
            source_key="host:demo:private:e1",
            session_id="s1",
        )
        self.assertEqual(len(core.outbox.pending("demo")), 1)
        stats = await core.outbox_dispatcher.run_pending("demo")
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(stats["pending"], 0)
        self.assertEqual([row["text"] for row in core.facts.load("demo")], ["用户喜欢晨跑"])

        # Unknown op types stay pending (legacy wire op has no core handler yet).
        core.outbox.enqueue("demo", "persist_prompt_locale", {"locale": "zh"})
        skipped = await core.outbox_dispatcher.run_pending("demo")
        self.assertEqual(skipped["skipped"], 1)
        self.assertEqual(skipped["pending"], 1)

    async def test_maintenance_run_once_and_loop_lifecycle(self):
        model = FakeModel()
        core = self.build(model=model)
        core.recover("demo")
        core.facts.save("demo", [{
            "id": "fact_run", "text": "用户周末在公园跑步", "importance": 8,
            "schema_version": 2, "absorbed": False,
            "source_event_keys": ["host:demo:private:e1"],
            "created_at": "2026-10-20T00:00:00+00:00",
        } for _ in range(1)] + [{
            "id": f"fact_{index}", "text": f"用户第{index}次聊到跑步",
            "importance": 7, "schema_version": 2, "absorbed": False,
            "source_event_keys": [f"host:demo:private:e{index}"],
            "created_at": "2026-10-20T00:00:00+00:00",
        } for index in range(1, 5)])
        # A plugin-scoped deletion stays pending until the plugin reports.
        deletion = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"),
            source_keys=("plugin:nekospire:demo:private:s1:e1",),
            reason="plugin cleanup",
        ))
        self.assertEqual(deletion.status, "pending")

        result = await core.maintenance.run_once("demo")
        self.assertIn(result.status, ("completed", "partial"))
        for step in (
            "reconcile", "outbox", "synthesize", "promote", "archive",
            "deletion_retry",
        ):
            self.assertIn(step, result.steps)
        self.assertEqual(result.steps["outbox"]["status"], "ok")
        self.assertEqual(result.steps["deletion_retry"]["status"], "ok")

        # The loop is one task per character, idempotent and fully stopped.
        await core.maintenance.start("demo", interval_seconds=0.01, max_rounds=2)
        self.assertTrue(core.maintenance.running("demo"))
        await core.maintenance.wait("demo", timeout=5)
        self.assertFalse(core.maintenance.running("demo"))
        await core.maintenance.stop("demo")
        self.assertFalse(core.maintenance.running("demo"))

    async def test_engine_state_and_maintenance_api(self):
        from rin_memory.api import MemoryEngine

        core = self.build()
        core.recover("demo")

        class Profiles:
            async def get(self, character):
                return None

        class UnusedContext:
            async def recall(self, request):
                return ()

        engine = MemoryEngine(
            events=core.sink, profiles=Profiles(), context=UnusedContext(),
            state=core.state_service, maintenance=core.maintenance,
        )
        receipt = await engine.commit_state("demo", {
            "ops": [{"op": "set_thought", "text": "状态入口",
                     "source_event_ids": ["e1"]}],
            "internal": True,
        })
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["version"], 1)
        snapshot = await engine.read_state("demo")
        self.assertEqual(snapshot["version"], 1)
        self.assertIn("状态入口", snapshot["block"])
        maintained = await engine.maintain("demo")
        self.assertIn("steps", maintained)

        bare = MemoryEngine(events=None, profiles=None, context=None)
        with self.assertRaises(NotConfiguredError):
            await bare.read_state("demo")
        with self.assertRaises(NotConfiguredError):
            await bare.maintain("demo")


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli_main(list(argv))
        return code, out.getvalue()

    def test_run_once_and_single_writer_lock(self):
        memory_root = self.root / "memory"
        code, output = self.run_cli([
            "run", "--memory-root", str(memory_root),
            "--character", "demo", "--once",
        ])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["character"], "demo")
        self.assertIn("steps", payload)
        self.assertTrue((memory_root / "demo" / "companion_state.json").parent.exists())

        character_dir = memory_root / "demo"
        lock = acquire_writer_lock(character_dir, character="demo")
        try:
            blocked, _ = self.run_cli([
                "run", "--memory-root", str(memory_root),
                "--character", "demo", "--once",
            ])
            self.assertEqual(blocked, 3)
        finally:
            release_writer_lock(lock)

        code_again, _ = self.run_cli([
            "run", "--memory-root", str(memory_root),
            "--character", "demo", "--once",
        ])
        self.assertEqual(code_again, 0)

    def test_cli_rejects_real_user_data_roots(self):
        for label in ("nekodata", "Documents"):
            target = self.root / label / "memory"
            code, _ = self.run_cli([
                "run", "--memory-root", str(target),
                "--character", "demo", "--once",
            ])
            self.assertEqual(code, 2, label)


if __name__ == "__main__":
    unittest.main()
