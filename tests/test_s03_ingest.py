"""S03 acceptance: saving a turn and extracting from it are separate decisions.

docs/standalone-service.md §5.1 fixes two defects in one place:

* plugin-only classes returned from `ingest_turn` *before* recent.json and the
  time index were written, so a game-stream or tool-result turn disappeared
  from the conversation entirely;
* an ordinary chat entry point left its turns 'unclassified', which is both the
  legacy-barrier bucket and the value the gate could not attribute to anyone.

The assertions here are about where the text ends up, not about status codes
alone: raw storage, extraction enqueue and the classes each path produces.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from test_b04_extraction import extraction_reply
from rin_memory.classification import CHAT, UNCLASSIFIED, classify_message
from rin_memory.contracts import DeletionRequest, Scope
from rin_memory.domain.extraction import ExtractionSettings


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 23, 9, 30, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


class FakeModel:
    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls.append({"messages": messages, "purpose": purpose})
        reply = self.responses.pop(0) if self.responses else "[]"
        return extraction_reply(reply, messages[0]["content"]) if purpose == "memory_fact_extraction" else reply


def human(text: str) -> dict:
    return {"type": "human", "data": {"content": text}}


def assistant(text: str) -> dict:
    return {"type": "ai", "data": {"content": text}}


class IngestStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, model=None, **kwargs):
        return build_memory_core(
            memory_root=self.root / "memory", clock=self.clock,
            model=model, **kwargs,
        )

    # -- §5.1: a non-extractable class is still stored -------------------

    async def test_plugin_only_turn_is_saved_but_not_extracted(self):
        model = FakeModel()
        core = self.build(model)
        result = await core.extraction.ingest_turn(
            "demo",
            [human("第三层打掉了 12 点血"), assistant("这一刀有点贪")],
            source_key="host:demo:private:g1",
            content_class="game_stream",
            session_id="s1",
        )
        self.assertEqual(result.status, "kept_in_plugin")
        # Saved for conversation continuity...
        self.assertEqual(core.recent.count("demo"), 2)
        index = core.timeindex("demo").open()
        try:
            self.assertEqual(index.count_original(), 2)
        finally:
            index.close()
        # ...but no extraction work was queued and no fact appeared.
        self.assertEqual(core.outbox.pending("demo"), ())
        self.assertEqual(core.facts.load("demo"), [])
        self.assertEqual(model.calls, [])
        self.assertEqual(result.reason, "stored_without_extraction")

    async def test_tool_result_class_is_saved_without_prompting_the_model(self):
        model = FakeModel()
        core = self.build(model)
        result = await core.extraction.ingest_turn(
            "demo", [human("查询返回了 3 条记录")],
            source_key="host:demo:private:p1",
            content_class="plugin_notice",
        )
        self.assertEqual(result.status, "kept_in_plugin")
        self.assertEqual(core.recent.count("demo"), 1)
        self.assertEqual(model.calls, [])

    async def test_stored_turn_survives_a_deletion_of_another_source(self):
        """Saving earlier must not bypass the barrier that was already applied."""
        core = self.build(FakeModel())
        await core.extraction.ingest_turn(
            "demo", [human("游戏流水一")],
            source_key="host:demo:private:g2", content_class="game_stream",
        )
        self.assertEqual(core.recent.count("demo"), 1)
        # A later deletion of a different source keeps the stored turn.
        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"),
            source_keys=("host:demo:private:other",),
        ))
        self.assertEqual(core.recent.count("demo"), 1)

    # -- §5.1: ordinary chat is labelled, not left ambiguous -------------

    async def test_ordinary_chat_turn_is_classified_as_chat(self):
        model = FakeModel([json.dumps([{
            "text": "用户喜欢乌龙茶", "importance": 6,
        }], ensure_ascii=False)])
        core = self.build(model)
        result = await core.extraction.ingest_turn(
            "demo", [human("我平时都喝乌龙茶")],
            source_key="host:demo:private:c1",
        )
        self.assertEqual(result.content_class, CHAT)
        self.assertEqual(result.status, "extracted")
        self.assertEqual(core.facts.load("demo")[0]["text"], "用户喜欢乌龙茶")
        # The ledger records the same class the caller can audit.
        entry = core.ledger.lookup(Scope("demo"), "host:demo:private:c1")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.content_class, CHAT)

    async def test_classify_message_default_is_configurable(self):
        plain = {"type": "human", "data": {"content": "随便聊聊"}}
        self.assertEqual(classify_message(plain), UNCLASSIFIED)
        self.assertEqual(classify_message(plain, default=CHAT), CHAT)
        # An explicit class still wins over the default.
        labelled = {
            "type": "human",
            "data": {
                "content": "第三层",
                "metadata": {"companion_event": {"content_class": "game_stream"}},
            },
        }
        self.assertEqual(classify_message(labelled, default=CHAT), "game_stream")

    async def test_chat_class_is_not_subject_to_the_legacy_barrier(self):
        """'chat' is a host class, so an old timestamp no longer drops it."""
        model = FakeModel()
        core = self.build(model)
        old = {
            "type": "human",
            "data": {
                "content": "很久以前的一句话",
                "timestamp": "2020-01-01T00:00:00+00:00",
            },
        }
        result = await core.extraction.ingest_turn(
            "demo", [old], source_key="host:demo:private:c2",
        )
        self.assertEqual(result.content_class, CHAT)
        self.assertEqual(core.recent.count("demo"), 1)


class IngestRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """§3.4: a crash between saving the turn and enqueueing must be repairable.

    The turn is durable (recent.json + time index) before the outbox op exists,
    so the failure window is real. A retry used to hit `duplicate` and return,
    leaving a saved turn that could never be extracted.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, model=None, **kwargs):
        return build_memory_core(
            memory_root=self.root / "memory", clock=self.clock,
            model=model, **kwargs,
        )

    async def test_duplicate_retry_restores_a_lost_extraction_op(self):
        model = FakeModel([json.dumps([{"text": "用户喜欢乌龙茶"}], ensure_ascii=False)])
        core = self.build(model, extraction_settings=ExtractionSettings(inline=False))
        turn = [human("我平时都喝乌龙茶")]

        class Boom(RuntimeError):
            pass

        real_enqueue = core.outbox.enqueue

        def failing_enqueue(*args, **kwargs):
            raise Boom("disk full between raw save and enqueue")

        core.extraction._outbox.enqueue = failing_enqueue  # type: ignore[union-attr]
        with self.assertRaises(Boom):
            await core.extraction.ingest_turn(
                "demo", turn, source_key="host:demo:private:r1", session_id="s1",
            )
        # The turn is already durable, the op is not: this is the broken state.
        self.assertEqual(core.recent.count("demo"), 1)
        self.assertEqual(core.outbox.pending("demo"), ())

        core.extraction._outbox.enqueue = real_enqueue  # type: ignore[union-attr]
        retried = await core.extraction.ingest_turn(
            "demo", turn, source_key="host:demo:private:r1", session_id="s1",
        )
        self.assertEqual(retried.status, "duplicate")
        pending = core.outbox.pending("demo")
        self.assertEqual(len(pending), 1, "the retry must restore the extraction op")
        self.assertEqual(pending[0].payload["source_keys"], ["host:demo:private:r1"])

        await core.maintenance.run_once("demo")
        self.assertEqual(core.facts.load("demo")[0]["text"], "用户喜欢乌龙茶")

    async def test_duplicate_does_not_queue_a_second_op(self):
        core = self.build(FakeModel(), extraction_settings=ExtractionSettings(inline=False))
        turn = [human("今天去公园跑步")]
        first = await core.extraction.ingest_turn(
            "demo", turn, source_key="host:demo:private:r2",
        )
        self.assertEqual(first.status, "accepted")
        self.assertEqual(len(core.outbox.pending("demo")), 1)

        second = await core.extraction.ingest_turn(
            "demo", turn, source_key="host:demo:private:r2",
        )
        self.assertEqual(second.status, "duplicate")
        self.assertIsNone(second.op_id, "an existing op must not be duplicated")
        self.assertEqual(len(core.outbox.pending("demo")), 1)

    async def test_non_extractable_duplicate_stays_unextracted(self):
        core = self.build(FakeModel(), extraction_settings=ExtractionSettings(inline=False))
        turn = [human("第三层打掉了 12 点血")]
        await core.extraction.ingest_turn(
            "demo", turn, source_key="host:demo:private:r3",
            content_class="game_stream",
        )
        again = await core.extraction.ingest_turn(
            "demo", turn, source_key="host:demo:private:r3",
            content_class="game_stream",
        )
        self.assertEqual(again.status, "duplicate")
        self.assertIsNone(again.op_id)
        self.assertEqual(core.outbox.pending("demo"), ())


if __name__ == "__main__":
    unittest.main()
