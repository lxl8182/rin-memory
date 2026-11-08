"""B06 recall acceptance: hybrid ranking, filters, budget and continuity.

The batch is judged on replacing the demo context provider with a real,
read-only recall path: BM25/RRF ranking over the B05 stores, optional vectors
with a no-vector fallback, read-side invalidation of corrections/deletions, a
bounded context via the injected token counter, and anaphora/episode
continuation without forcing unrelated memories into a new topic.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.contracts import (
    ContextRequest,
    CorrectionRequest,
    DeletionRequest,
    DeletionTarget,
    Scope,
)
from rin_memory.domain.recall import RecallService, RecallSettings
from rin_memory.errors import NotConfiguredError


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


class FakeEmbedding:
    """Deterministic vector stand-in: one axis per topic."""

    def is_available(self) -> bool:
        return True

    def model_id(self):
        return "fake-embedding-v1"

    async def embed_batch(self, texts):
        out = []
        for text in texts:
            if "跑步" in text:
                out.append([1.0, 0.0, 0.0])
            elif "猫" in text:
                out.append([0.0, 1.0, 0.0])
            else:
                out.append([0.0, 0.0, 1.0])
        return out


class FakeTokenCounter:
    def __init__(self, per_call: int = 10) -> None:
        self.per_call = per_call
        self.seen = 0

    def count(self, text: str) -> int:
        self.seen += 1
        return self.per_call

    async def acount(self, text: str) -> int:
        return self.count(text)

    def truncate(self, text: str, max_tokens: int) -> str:
        return text[:max_tokens]

    def identity(self) -> str:
        return f"fake-tokenizer-{self.per_call}"


def fact(fact_id, text, *, embedding=None, episode_id="", start="2026-10-20T08:00:00+00:00"):
    row = {
        "id": fact_id,
        "text": text,
        "importance": 8,
        "source": "user_observation",
        "schema_version": 2,
        "absorbed": True,
        "created_at": start,
        "event_start_at": start,
        "source_event_keys": [f"host:demo:private:{fact_id}"],
    }
    if episode_id:
        row["episode_id"] = episode_id
    if embedding is not None:
        row["embedding"] = embedding
        row["embedding_model_id"] = "fake-embedding-v1"
    return row


def reflection(reflection_id, text, source_fact_ids, *, embedding=None):
    row = {
        "id": reflection_id,
        "text": text,
        "status": "confirmed",
        "entity": "relationship",
        "source_fact_ids": list(source_fact_ids),
        "reinforcement": 1.2,
        "created_at": "2026-10-21T00:00:00+00:00",
        "source_event_keys": [f"host:demo:private:{item}" for item in source_fact_ids],
    }
    if embedding is not None:
        row["embedding"] = embedding
        row["embedding_model_id"] = "fake-embedding-v1"
    return row


from test_ro03_recall import SelectionModel


class RecallFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, name="memory", **kwargs):
        return build_memory_core(
            memory_root=self.root / name, clock=self.clock, model=SelectionModel(), **kwargs,
        )

    def seed(self, core) -> None:
        core.facts.save("demo", [
            fact("fact_run", "用户周末在公园跑步"),
            fact("fact_cat", "用户养了一只叫团子的猫"),
        ])
        core.reflections.save("demo", [
            reflection("ref_run", "用户喜欢周末户外跑步", ["fact_run"]),
        ])

    async def test_explicit_recall_ranks_and_returns_sources(self):
        core = self.build()
        core.recover("demo")
        self.seed(core)
        result = await core.recall.recall_memory("demo", "团子 猫", purpose="query")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["retrieval_mode"], "bm25")
        self.assertEqual(result["items"][0]["id"], "fact_cat")
        self.assertEqual(
            result["items"][0]["source_keys"], ["host:demo:private:fact_cat"],
        )
        self.assertIn("block", result["corrections_block"])
        self.assertIn("block", result["growth_block"])

        # A second, disjoint query ranks the running memory instead.
        other = await core.recall.recall_memory("demo", "周末 跑步", purpose="query")
        self.assertEqual(other["status"], "matched")
        ids = {item["id"] for item in other["items"]}
        self.assertIn("fact_run", ids)
        self.assertIn("ref_run", ids)

    async def test_hybrid_with_embedding_provider_and_new_topic_miss(self):
        core = self.build(embedding_provider=FakeEmbedding())
        core.recover("demo")
        core.facts.save("demo", [
            fact("fact_run", "用户周末在公园跑步", embedding=[1.0, 0.0, 0.0]),
            fact("fact_cat", "用户养了一只叫团子的猫", embedding=[0.0, 1.0, 0.0]),
        ])
        core.reflections.save("demo", [
            reflection("ref_run", "用户喜欢周末户外跑步", ["fact_run"], embedding=[1.0, 0.0, 0.0]),
        ])
        matched = await core.recall.build_turn_context(
            "demo", current_input="周末跑步",
        )
        self.assertEqual(matched["result"], "matched")
        self.assertEqual(matched["retrieval_mode"], "hybrid")
        self.assertIn("ref_run", matched["memory_ids"])
        self.assertTrue(matched["context"])

        # A new topic must not force an old memory into the prompt.
        miss = await core.recall.build_turn_context(
            "demo", current_input="今天天气怎么样",
        )
        self.assertEqual(miss["result"], "miss")
        self.assertEqual(miss["memory_ids"], [])
        self.assertEqual(miss["context"], "")

        # Without vectors the same explicit query still works (BM25 fallback).
        plain = self.build(name="plain")
        plain.recover("demo")
        self.seed(plain)
        fallback = await plain.recall.recall_memory("demo", "团子 猫", purpose="query")
        self.assertEqual(fallback["status"], "matched")
        self.assertEqual(fallback["retrieval_mode"], "bm25")

    async def test_read_side_invalidation_hides_corrected_and_deleted(self):
        core = self.build()
        core.recover("demo")
        self.seed(core)
        receipt = await core.correction_service.correct(CorrectionRequest(
            scope=Scope("demo"), old_text="在公园跑步", new_text="在河边跑步",
        ))
        self.assertIn("fact_run", receipt.invalidated_fact_ids)
        after_correction = await core.recall.recall_memory("demo", "跑步", purpose="query")
        self.assertEqual(after_correction["status"], "matched")
        self.assertEqual([item["id"] for item in after_correction["items"]], [receipt.replacement_fact_id])
        self.assertEqual(after_correction["items"][0]["text"], "在河边跑步")

        context = await core.recall.build_turn_context("demo", current_input="跑步")
        # The explicit correction stays visible even though the old fact is gone.
        self.assertIn("在河边跑步", context["corrections"]["block"])
        self.assertNotIn("fact_run", context["memory_ids"])

        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), targets=(DeletionTarget("fact", "fact_cat"),),
        ))
        after_delete = await core.recall.recall_memory("demo", "团子 猫", purpose="query")
        self.assertEqual(after_delete["status"], "miss")
        self.assertEqual(after_delete["items"], [])

    async def test_anaphora_continuation_and_episode_background(self):
        core = self.build()
        core.recover("demo")
        core.facts.save("demo", [
            fact(
                "fact_run", "用户周末在公园跑步",
                episode_id="ep:host:e1:2026-10-20",
            ),
            fact("fact_cat", "用户养了一只叫团子的猫"),
        ])
        core.reflections.save("demo", [
            reflection("ref_run", "用户喜欢周末户外跑步", ["fact_run"]),
        ])
        continued = await core.recall.build_turn_context(
            "demo",
            current_input="后来呢？",
            recent_turns=["我们之前聊到周末在公园跑步"],
        )
        self.assertEqual(continued["result"], "matched")
        self.assertIn("fact_run", continued["memory_ids"])
        self.assertTrue(continued["episode"])
        self.assertEqual(
            continued["episode"].get("episode_id"), "ep:host:e1:2026-10-20",
        )

        fresh = await core.recall.build_turn_context(
            "demo", current_input="今天天气怎么样",
        )
        self.assertEqual(fresh["result"], "miss")
        self.assertEqual(fresh["memory_ids"], [])

    async def test_token_budget_and_counter_injection(self):
        counter = FakeTokenCounter(per_call=1000)
        core = self.build(token_counter=counter)
        core.recover("demo")
        self.seed(core)
        await core.correction_service.correct(CorrectionRequest(
            scope=Scope("demo"), old_text="在公园跑步", new_text="在河边跑步",
        ))
        # The injected counter reports a huge block cost, so the block is empty.
        block = core.correction_service.block("demo", language="zh", limit_tokens=200)
        self.assertEqual(block.block, "")
        self.assertGreater(counter.seen, 0)

        plain = self.build(name="plain")
        plain.recover("demo")
        self.seed(plain)
        await plain.correction_service.correct(CorrectionRequest(
            scope=Scope("demo"), old_text="在公园跑步", new_text="在河边跑步",
        ))
        # Without the port the legacy CJK estimate renders the same block.
        legacy = plain.correction_service.block("demo", language="zh", limit_tokens=200)
        self.assertIn("在河边跑步", legacy.block)

        bounded = await core.recall.recall_memory(
            "demo", "周末 跑步", purpose="query", token_budget=1,
        )
        self.assertEqual(bounded["status"], "miss")
        self.assertEqual(bounded["items"], [])

    async def test_growth_block_version_and_no_duplicate_injection(self):
        core = self.build()
        core.recover("demo")
        core.reflections.save("demo", [{
            "id": "ref_grow",
            "text": "用户周末晨跑",
            "status": "confirmed",
            "memory_kind": "habit",
            "claim": "用户周末早晨会晨跑",
            "situation": "周末早晨",
            "support_count": 2,
            "support_event_keys": ["host:demo:private:e1", "host:demo:private:e2"],
            "support_evidence": {
                "actual_result": ["host:demo:private:e1", "host:demo:private:e2"],
                "user_feedback": [], "both_party": [], "real_contact": [],
            },
            "created_at": "2026-10-21T00:00:00+00:00",
        }])
        first = await core.recall.build_turn_context("demo", current_input="随便聊聊")
        self.assertEqual(first["growth"]["version"], 0)
        self.assertEqual(first["working_summary"]["sections"]["growth"]["items"], [])
        usage = await core.reflection_service.record_growth_usage(
            "demo", "ref_grow", "read", source_key="chat:1",
        )
        self.assertEqual(usage.version, 1)
        second = await core.recall.build_turn_context("demo", current_input="周末晨跑")
        self.assertEqual(second["memory_ids"], ["ref_grow"])
        self.assertEqual(len(second["working_summary"]["sections"]["growth"]["items"]), 1)

    async def test_time_recall_and_degraded_service(self):
        core = self.build()
        core.recover("demo")
        self.seed(core)
        inside = await core.recall.recall_by_time(
            "demo",
            time_range=("2026-10-01T00:00:00+00:00", "2026-10-31T00:00:00+00:00"),
        )
        self.assertEqual(inside["status"], "matched")
        self.assertIn("fact_run", {item["id"] for item in inside["items"]})
        outside = await core.recall.recall_by_time(
            "demo",
            time_range=("2026-11-01T00:00:00+00:00", "2026-11-30T00:00:00+00:00"),
        )
        self.assertEqual(outside["status"], "miss")
        invalid = await core.recall.recall_by_time("demo", time_range="not-a-date")
        self.assertEqual(invalid["status"], "miss")
        self.assertEqual(invalid["error_code"], "invalid_time_range")

        bare = RecallService(clock=self.clock, settings=RecallSettings())
        unavailable = await bare.recall_memory("demo", "anything")
        self.assertEqual(unavailable["status"], "unavailable")
        empty_context = await bare.build_turn_context("demo", current_input="anything")
        self.assertEqual(empty_context["result"], "unavailable")

        engine = __import__("rin_memory.api", fromlist=["MemoryEngine"]).MemoryEngine(
            events=None, profiles=None, context=None,
        )
        with self.assertRaises(NotConfiguredError):
            await engine.recall_memory("demo", "anything")
        with self.assertRaises(NotConfiguredError):
            await engine.build_turn_context("demo", current_input="anything")


class EngineContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_context_uses_recall_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FixedClock()
            core = build_memory_core(
                memory_root=root / "memory", clock=clock,
                embedding_provider=FakeEmbedding(),
            )
            core.recover("demo")
            core.facts.save("demo", [
                fact("fact_run", "用户周末在公园跑步", embedding=[1.0, 0.0, 0.0]),
            ])

            class Profiles:
                async def get(self, character):
                    return None

            class UnusedContext:
                async def recall(self, request):
                    raise AssertionError("the demo provider must not be used")

            from rin_memory.api import MemoryEngine
            engine = MemoryEngine(
                events=core.sink, profiles=Profiles(),
                context=UnusedContext(), recall=core.recall,
            )
            bundle = await engine.build_context(ContextRequest(
                scope=Scope("demo"), current_input="周末跑步",
            ))
            self.assertEqual(bundle.recall_status, "ready")
            self.assertTrue(bundle.items)
            self.assertEqual(bundle.items[0].kind, "fact")
            self.assertTrue(bundle.items[0].text)


if __name__ == "__main__":
    unittest.main()
