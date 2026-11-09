"""B03 deletion acceptance: barrier first, bounded cleanup, no resurrection.

Covers the batch's acceptance criteria through the public path only:
a durable receipt means the barrier is on disk; a retried request applies once;
old journal entries and retried ingests cannot bring deleted content back; an
episode deletion removes the original conversation rows and keeps the receipt
pending until the offline plugin reports; damaged/unknown stores are refused
instead of being treated as "nothing was deleted".
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.contracts import DeletionRequest, DeletionTarget, MemoryEvent, Scope
from rin_memory.errors import (
    BarrierStaleError, CorruptStoreError, DeletionNotFoundError,
    InvalidSchemaError, WriteRejected,
)
from rin_memory.persistence.permits import RejectingWritePermit
from rin_memory.persistence.records import event_snapshot


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


def event(key: str = "host:demo:private:e1", text: str = "今天天气不错") -> MemoryEvent:
    return MemoryEvent(
        scope=Scope("demo"), source_key=key, event_type="user_message",
        text=text, occurred_at=datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc),
        content_class="chat",
    )


class DeletionFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()
        self.core = build_memory_core(memory_root=self.root / "memory", clock=self.clock)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_barrier_is_durable_before_cleanup_and_blocks_reingest(self):
        await self.core.sink.append(event())
        self.core.facts.save("demo", [
            {"id": "f1", "text": "秘密文本A", "source_event_keys": ["host:demo:private:e1"]},
            {"id": "f2", "text": "用户喜欢猫"},
        ])
        receipt = await self.core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"),
            source_keys=("host:demo:private:e1",),
            reason="user_request",
        ))
        self.assertEqual(receipt.status, "completed")
        self.assertTrue(receipt.stopped_using)
        self.assertTrue(receipt.content_cleaned)
        self.assertEqual(receipt.pending, ())
        self.assertEqual(receipt.failed, ())
        self.assertEqual([row["id"] for row in self.core.facts.load("demo")], ["f2"])
        self.assertEqual(self.core.ledger.count("demo"), 0)
        self.assertTrue(self.core.barrier.blocks_target("demo", "fact", "f1"))

        # The store keeps IDs/hashes only: no original text is persisted.
        raw = (self.root / "memory" / "demo" / "memory_deletions.json").read_text(
            encoding="utf-8",
        )
        self.assertNotIn("秘密文本A", raw)
        document = json.loads(raw)
        self.assertEqual(document["schema"], 1)
        self.assertGreaterEqual(document["version"], 1)
        self.assertTrue(any(
            item["type"] == "text" for item in document["tombstones"]
        ))
        event_types = [
            entry.event_type for entry in self.core.journal.read_since("demo", -1)
        ]
        self.assertIn("memory.deletion_registered", event_types)

        # Reopen on the same root: the receipt and the barrier survive.
        reopened = build_memory_core(memory_root=self.root / "memory", clock=self.clock)
        reopened.recover("demo")
        self.assertEqual(
            reopened.deletion_service.get("demo", receipt.deletion_id).status,
            "completed",
        )
        duplicate = await reopened.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), source_keys=("host:demo:private:e1",),
        ))
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.deletion_id, receipt.deletion_id)

        # An old journal entry for the deleted source replays as a no-op and a
        # retried ingest is rejected instead of restoring the ledger hint.
        entry = reopened.journal.append(
            "demo", "core.source_recorded", event_snapshot(event()),
        )
        self.assertGreaterEqual(entry.position, 2)
        result = reopened.journal.reconcile("demo")
        self.assertFalse(result.paused)
        self.assertEqual(reopened.ledger.count("demo"), 0)
        rejected = await reopened.sink.append(event())
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.reason, "deleted_source")
        self.assertTrue(rejected.durable)

    async def test_deleting_an_unconsumed_fact_keeps_its_plugin_source(self):
        source = "plugin:dsh_memory:demo:private:s1:origin"
        await self.core.sink.append(event(source, "原来的对话"))
        self.core.facts.save("demo", [
            {"id": "f1", "text": "原话派生的事实", "source_event_keys": [source]},
        ])
        receipt = await self.core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), targets=(DeletionTarget("fact", "f1"),),
        ))
        self.assertEqual(receipt.status, "completed")
        self.assertEqual(self.core.facts.load("demo"), [])
        self.assertEqual(self.core.ledger.count("demo"), 1)
        self.assertFalse(self.core.deletion_service.pending_plugin_deletions("demo", "dsh_memory"))

    async def test_episode_deletion_cleans_original_rows_and_waits_for_plugin(self):
        index = self.core.timeindex("demo").open()
        index.insert_original({"session_id": "s1", "message": "第一句原文"})
        index.insert_original({"session_id": "s1", "message": "第二句原文"})
        index.insert_original({"session_id": "s2", "message": "别的会话"})
        index.close()
        self.core.outbox.enqueue(
            "demo", "extract_facts", {"episode_id": "ep:plugin:p1:s1"},
        )
        receipt = await self.core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), mode="episode",
            episode_ids=("ep:plugin:p1:s1",), reason="episode",
        ))
        self.assertEqual(receipt.status, "pending")
        self.assertTrue(receipt.content_cleaned)
        self.assertIn("plugin", receipt.pending)
        self.assertEqual(receipt.plugin_receipts, {})
        index = self.core.timeindex("demo").open()
        self.assertEqual(index.count_original(), 1)
        index.close()
        self.assertEqual(self.core.outbox.pending("demo"), ())
        pending = self.core.deletion_service.pending_plugin_deletions("demo", "p1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["episode_ids"], ["ep:plugin:p1:s1"])

        failed = await self.core.deletion_service.record_plugin_receipt(
            "demo", receipt.deletion_id, "p1", "failed", failed=["s1"],
        )
        self.assertEqual(failed.status, "failed")
        self.assertIn("plugin", failed.failed)
        completed = await self.core.deletion_service.record_plugin_receipt(
            "demo", receipt.deletion_id, "p1", "cleaned", cleaned=["s1"],
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.plugin_receipts["p1"]["status"], "cleaned")
        retry = await self.core.deletion_service.retry("demo", receipt.deletion_id)
        self.assertTrue(retry.duplicate)
        self.assertEqual(retry.status, "completed")

    async def test_damaged_or_newer_store_is_refused_and_not_overwritten(self):
        with self.assertRaises(DeletionNotFoundError):
            await self.core.deletion_service.retry("demo", "del_missing")
        self.assertIsNone(self.core.deletion_service.get("demo", "del_missing"))

        path = self.root / "memory" / "demo" / "memory_deletions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        damaged = path.read_bytes()
        with self.assertRaises(CorruptStoreError):
            await self.core.deletion_service.forget(DeletionRequest(
                scope=Scope("demo"), source_keys=("host:demo:private:e1",),
            ))
        self.assertEqual(path.read_bytes(), damaged)

        path.write_text(json.dumps({"schema": 99, "version": 1, "deletions": [], "tombstones": []}), encoding="utf-8")
        newer = path.read_bytes()
        with self.assertRaises(InvalidSchemaError):
            await self.core.deletion_service.forget(DeletionRequest(
                scope=Scope("demo"), source_keys=("host:demo:private:e1",),
            ))
        self.assertEqual(path.read_bytes(), newer)

        fenced_root = self.root / "fenced"
        fenced = build_memory_core(
            memory_root=fenced_root / "memory", clock=self.clock,
            permit=RejectingWritePermit(),
        )
        with self.assertRaises(WriteRejected):
            await fenced.deletion_service.forget(DeletionRequest(
                scope=Scope("demo"), source_keys=("host:demo:private:e1",),
            ))
        self.assertFalse(
            (fenced_root / "memory" / "demo" / "memory_deletions.json").exists()
        )

    async def test_in_flight_snapshot_is_stale_after_a_deletion(self):
        snapshot = self.core.barrier.snapshot("demo")
        await self.core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), source_keys=("host:demo:private:e1",),
        ))
        self.assertTrue(self.core.barrier.is_stale("demo", snapshot))
        with self.assertRaises(BarrierStaleError):
            self.core.barrier.assert_fresh("demo", snapshot)
        self.assertTrue(self.core.barrier.blocks_source_key("demo", "host:demo:private:e1"))


if __name__ == "__main__":
    unittest.main()
