"""RO03 acceptance: interest signals are user evidence, not a profile.

The contract under test (docs/rin-next-optimization-plan.md, RO03):

* a signal names the kind of preference the user expressed and the utterances
  that express it -- `like` / `curious` / `dislike` / `paused`;
* a signal with no resolvable user citation is **discarded**, never stored
  source-less: an unattributed preference cannot be corrected or deleted and
  would be recited as a lasting trait;
* "最近想试胶片" is a near-term curiosity and must not become `about_owner`
  section material, while "我一直喜欢胶片摄影" may;
* deleting or correcting a source retires the signal and removes it from the
  section material;
* the same words stated twice (a mirror turn, a drain retry) do not become two
  supports, and one (subject, topic) keeps one row;
* a signal is scoped: another subject never reads this subject's preferences.

Every case runs over a real core on a tempfile root; the model is a fixed
stand-in, so nothing reaches the network.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from test_b04_extraction import extraction_reply
from rin_memory.contracts import (
    CorrectionRequest,
    DeletionRequest,
    Scope,
)
from rin_memory.domain.extraction import ExtractionSettings

CHARACTER = "demo"


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


class SignalModel:
    """Answers extraction with the facts/signals shape and records prompts."""

    def __init__(self, *replies):
        self.calls = []
        self.replies = list(replies)

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls.append(messages[0]["content"] if messages else "")
        reply = self.replies.pop(0) if self.replies else {"facts": []}
        if purpose == "memory_fact_extraction":
            return extraction_reply(reply, messages[0]["content"])
        return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)


def human(text):
    return {"type": "human", "data": {"content": text}}


class SignalHarness(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = MutableClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, model, **kwargs):
        return build_memory_core(
            memory_root=self.root / "memory", clock=self.clock, model=model,
            extraction_settings=ExtractionSettings(
                inline=False, batch_max_turns=5, batch_idle_seconds=0.0,
            ),
            **kwargs,
        )

    async def say(self, core, text, source_key):
        """Queue one turn and drain it (never inline, so batching applies)."""
        self.clock.advance(seconds=1)
        await core.extraction.ingest_turn(
            CHARACTER, [human(text)], source_key=source_key, session_id="qq",
        )
        return await core.extraction.process_outbox(CHARACTER)


class SignalKindsAreNotMixed(SignalHarness):
    """A near-term curiosity and a lasting liking must not become one thing."""

    async def test_curious_and_like_stay_separate(self):
        # Two separate drains, so each answer cites the turn it was extracted
        # from: a citation that cannot be resolved is discarded (see
        # NoCitationNoSignal), and that is exactly what keeps these apart.
        model = SignalModel(
            {"facts": [], "interest_signals": [
                {"topic": "胶片", "keywords": ["胶片"], "signal": "curious",
                 "evidence_refs": [1]},
            ]},
            {"facts": [], "interest_signals": [
                {"topic": "胶片摄影", "keywords": ["胶片摄影"], "signal": "like",
                 "evidence_refs": [1]},
            ]},
        )
        core = self.build(model)
        await self.say(core, "最近想试胶片", "host:demo:private:a")
        await self.say(core, "我一直喜欢胶片摄影", "host:demo:private:b")

        snapshot = core.interests.snapshot(CHARACTER)
        self.assertEqual(
            [row["topic"] for row in snapshot["recent_interests"]], ["胶片"],
            "近期好奇只出现在 recent_interests",
        )
        self.assertEqual(
            [row["topic"] for row in snapshot["stable_interests"]], ["胶片摄影"],
            "稳定喜好只出现在 stable_interests",
        )
        material = core.interests.material(CHARACTER)
        self.assertEqual(
            [row["topic"] for row in material["items"]], ["胶片摄影"],
            "只有稳定喜好能作为 about_owner 段落的取材依据",
        )
        self.assertEqual(material["excluded"]["curious"], 1)

    async def test_curiosity_expires_but_a_liking_does_not(self):
        model = SignalModel({"facts": [], "interest_signals": [
            {"topic": "天文摄影", "signal": "curious", "evidence_refs": [1]},
            {"topic": "红茶", "signal": "like", "evidence_refs": [1]},
        ]})
        core = self.build(model)
        await self.say(core, "最近想试天文摄影，也一直喜欢红茶", "host:demo:private:e1")
        await core.extraction.process_outbox(CHARACTER)
        self.assertEqual(len(core.interests.snapshot(CHARACTER)["recent_interests"]), 1)

        self.clock.advance(days=40)
        snapshot = core.interests.snapshot(CHARACTER)
        self.assertEqual(
            snapshot["recent_interests"], [],
            "超过窗口的近期好奇不再算当前兴趣",
        )
        self.assertEqual(
            [row["topic"] for row in snapshot["stable_interests"]], ["红茶"],
            "稳定喜好没有时效窗口",
        )


class NoCitationNoSignal(SignalHarness):
    """A preference that cannot be attributed is worse than no preference."""

    async def test_uncited_and_invented_references_are_discarded(self):
        model = SignalModel({"facts": [], "interest_signals": [
            {"topic": "钢琴", "signal": "like"},
            {"topic": "小提琴", "signal": "like", "evidence_refs": [9]},
            {"topic": "大提琴", "signal": "like", "evidence_refs": []},
            {"topic": "竖琴", "signal": "like", "evidence_refs": [1]},
        ]})
        core = self.build(model)
        await self.say(core, "我喜欢竖琴", "host:demo:private:ok")
        await core.extraction.process_outbox(CHARACTER)
        items = core.interests.snapshot(CHARACTER)["items"]
        self.assertEqual(
            [row["topic"] for row in items], ["竖琴"],
            "没有可解析原话引用的信号必须丢弃，不能降级成无来源信号",
        )
        for row in items:
            self.assertTrue(row["source_event_keys"])

    async def test_an_unknown_signal_kind_is_not_stored(self):
        model = SignalModel({"facts": [], "interest_signals": [
            {"topic": "露营", "signal": "maybe", "evidence_refs": [1]},
        ]})
        core = self.build(model)
        await self.say(core, "露营也许可以试试", "host:demo:private:maybe")
        await core.extraction.process_outbox(CHARACTER)
        self.assertEqual(core.interests.snapshot(CHARACTER)["items"], [])


class SignalsFollowTheirSources(SignalHarness):
    """Deleting or correcting a source retires the signal and its material."""

    async def test_deleting_the_source_removes_signal_and_material(self):
        model = SignalModel({"facts": [{"text": "用户一直喜欢胶片摄影"}],
                             "interest_signals": [
            {"topic": "胶片摄影", "signal": "like", "evidence_refs": [1]},
        ]})
        core = self.build(model)
        await self.say(core, "我一直喜欢胶片摄影", "host:demo:private:photo")
        await core.extraction.process_outbox(CHARACTER)
        self.assertEqual(len(core.interests.material(CHARACTER)["items"]), 1)

        await core.deletion_service.forget(DeletionRequest(
            scope=Scope(CHARACTER), source_keys=("host:demo:private:photo",),
        ))
        self.assertEqual(
            core.interests.material(CHARACTER)["items"], [],
            "来源被删除后，信号不能再作为段落依据",
        )
        self.assertEqual(core.interests.snapshot(CHARACTER)["items"], [])
        stored = core.interest_signals.load(CHARACTER)["items"]
        self.assertTrue(stored)
        self.assertTrue(
            all(row["status"] == "invalidated" for row in stored.values()),
            "删除后信号必须在文件里失效，而不能只被读路径临时藏起来",
        )

    async def test_correcting_the_fact_retires_the_signal(self):
        model = SignalModel({"facts": [{"text": "用户喜欢看科幻小说"}],
                             "interest_signals": [
            {"topic": "科幻小说", "signal": "like", "evidence_refs": [1]},
        ]})
        core = self.build(model)
        await self.say(core, "我喜欢看科幻小说", "host:demo:private:book")
        await core.extraction.process_outbox(CHARACTER)
        self.assertEqual(len(core.interests.material(CHARACTER)["items"]), 1)

        await core.correction_service.correct(CorrectionRequest(
            scope=Scope(CHARACTER), old_text="科幻小说", new_text="推理小说",
        ))
        self.assertEqual(
            core.interests.material(CHARACTER)["items"], [],
            "用户更正后，旧信号不能再作为段落依据",
        )


class OneVoiceOneSignal(SignalHarness):
    """A mirror, summary or drain retry of the same words is not support."""

    async def test_repeated_words_keep_one_row(self):
        payload = {"facts": [], "interest_signals": [
            {"topic": "手冲咖啡", "signal": "like", "evidence_refs": [1]},
        ]}
        model = SignalModel(payload, payload)
        core = self.build(model)
        await self.say(core, "我喜欢手冲咖啡", "host:demo:private:one")
        await self.say(core, "我喜欢手冲咖啡", "host:demo:private:two")
        await core.extraction.process_outbox(CHARACTER)
        items = core.interests.snapshot(CHARACTER)["items"]
        self.assertEqual(len(items), 1, "同一主题只保留一个信号行")
        # Two distinct utterances are two sources: the rule is one row, not one
        # key, so the second statement still counts as its own voice.
        self.assertEqual(len(items[0]["source_event_keys"]), 2)

    async def test_a_redrain_does_not_add_support_or_bump_the_version(self):
        payload = {"facts": [], "interest_signals": [
            {"topic": "手冲咖啡", "signal": "like", "evidence_refs": [1]},
        ]}
        model = SignalModel(payload)
        core = self.build(model)
        await self.say(core, "我喜欢手冲咖啡", "host:demo:private:one")
        await core.extraction.process_outbox(CHARACTER)
        first = core.interests.snapshot(CHARACTER)

        model.replies = [payload]
        await core.extraction.process_outbox(CHARACTER)
        second = core.interests.snapshot(CHARACTER)
        self.assertEqual(len(second["items"][0]["source_event_keys"]), 1)
        self.assertEqual(second["version"], first["version"])

    async def test_signals_are_scoped_to_their_subject(self):
        model = SignalModel({"facts": [], "interest_signals": [
            {"topic": "胶片摄影", "signal": "like", "evidence_refs": [1]},
        ]})
        core = self.build(model)
        await self.say(core, "我喜欢胶片摄影", "host:demo:private:e1")
        await core.extraction.process_outbox(CHARACTER)
        self.assertEqual(len(core.interests.snapshot(CHARACTER)["items"]), 1)
        self.assertEqual(
            core.interests.snapshot(CHARACTER, subject="group")["items"], [],
            "别的范围不能读到这个范围的喜好",
        )


class StorageContract(SignalHarness):
    """The store owns a version, survives a restart and replays cleanly."""

    async def test_version_and_rows_survive_a_restart(self):
        model = SignalModel({"facts": [], "interest_signals": [
            {"topic": "胶片摄影", "signal": "like", "evidence_refs": [1]},
        ]})
        core = self.build(model)
        await self.say(core, "我喜欢胶片摄影", "host:demo:private:e1")
        await core.extraction.process_outbox(CHARACTER)
        before = core.interests.snapshot(CHARACTER)
        self.assertGreaterEqual(before["version"], 1)

        reopened = self.build(SignalModel())
        reopened.recover(CHARACTER)
        self.assertEqual(reopened.interests.snapshot(CHARACTER), before)
        # Replaying the journal tail is idempotent.
        reopened.recover(CHARACTER)
        self.assertEqual(reopened.interests.snapshot(CHARACTER), before)

    async def test_inline_extraction_writes_signals_too(self):
        model = SignalModel({"facts": [], "interest_signals": [
            {"topic": "胶片摄影", "signal": "like", "evidence_refs": [1]},
        ]})
        core = build_memory_core(
            memory_root=self.root / "inline", clock=self.clock, model=model,
            extraction_settings=ExtractionSettings(inline=True),
        )
        await core.extraction.ingest_turn(
            CHARACTER, [human("我喜欢胶片摄影")],
            source_key="host:demo:private:e1", session_id="qq",
        )
        self.assertEqual(len(core.interests.snapshot(CHARACTER)["items"]), 1)

    async def test_a_paused_topic_is_reported_as_a_constraint(self):
        model = SignalModel({"facts": [], "interest_signals": [
            {"topic": "前任", "signal": "paused", "evidence_refs": [1]},
        ]})
        core = self.build(model)
        await self.say(core, "别再提前任的事了", "host:demo:private:e1")
        await core.extraction.process_outbox(CHARACTER)
        self.assertEqual(core.interests.paused_topics(CHARACTER), ["前任"])
        self.assertEqual(
            core.interests.material(CHARACTER)["items"], [],
            "暂停提及是约束，不是段落依据",
        )

    async def test_the_extraction_prompt_asks_for_signals_only_when_wired(self):
        model = SignalModel({"facts": []})
        core = self.build(model)
        await self.say(core, "随便聊聊", "host:demo:private:e1")
        await core.extraction.process_outbox(CHARACTER)
        self.assertIn('"interest_signals"', model.calls[0])

        # An embedder without the layer must not pay for the extra prompt: the
        # guard is asserted on the service, since a built core always wires it.
        unwired_model = SignalModel({"facts": []})
        plain = build_memory_core(
            memory_root=self.root / "plain", clock=self.clock,
            model=unwired_model, extraction_settings=ExtractionSettings(inline=True),
        )
        plain.extraction._interests = None
        await plain.extraction.ingest_turn(
            CHARACTER, [human("随便聊聊")],
            source_key="host:demo:private:p1", session_id="qq",
        )
        self.assertNotIn('"interest_signals"', unwired_model.calls[0])


if __name__ == "__main__":
    unittest.main()
