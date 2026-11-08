"""B04 extraction acceptance: raw storage, dedupe, self-statements, retries, fence.

Covers the batch criteria through the public path: a chat turn reaches
recent.json + time_indexed.db and its extraction op; facts are written once per
exact text with host field names; assistant lines never become user facts and
user corroboration upgrades a self-disclosure monotonically; a model failure
keeps the original text and the op retryable; a deletion that lands while the
model is running cannot be resurrected by the late result.
"""

import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.contracts import DeletionRequest, Scope, Scene
from rin_memory.domain.extraction import ExtractionSettings


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


def extraction_reply(response, prompt):
    """Adapt readable fact fixtures to the numbered-message model contract."""
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return response
    if isinstance(response, list):
        facts = response
    elif isinstance(response, dict):
        facts = response.get("facts", [])
    else:
        return json.dumps(response)
    evidence = re.findall(r"^\[(\d+)\].*?\| (user|assistant|tool) \| .*?message_ref=([^\s]+)",
                          prompt, flags=re.MULTILINE)
    indices = sorted({int(index) for index, _, _ in evidence})
    for row in facts:
        row.setdefault("operation", "add")
        row.setdefault("retention_class", "normal")
        row.setdefault("retention_reason", "用于后续交流")
        if len(indices) == 1:
            row.setdefault("sources", indices)
        role = "assistant" if row.get("source") == "ai_disclosure" else "user"
        if isinstance(row.get("sources"), list):
            row.setdefault("message_refs", [ref for index, speaker, ref in evidence
                                            if int(index) in row["sources"] and speaker == role])
    return json.dumps(response, ensure_ascii=False)


class FakeModel:
    """Deterministic model stand-in; records every call for assertions."""

    def __init__(self, responses=None, hook=None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self.hook = hook

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls.append({
            "tier": tier, "messages": messages,
            "max_output_tokens": max_output_tokens,
            "timeout_s": timeout_s, "purpose": purpose,
        })
        if self.hook is not None:
            await self.hook(self, messages)
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return extraction_reply(item, messages[0]["content"]) if purpose == "memory_fact_extraction" else item
        return "[]"


def human(text: str, source_key: str | None = None, episode_id: str | None = None):
    metadata: dict = {}
    if source_key:
        metadata["companion_event"] = {"source_key": source_key}
    if episode_id:
        metadata["episode_id"] = episode_id
    message = {"type": "human", "data": {"content": text}}
    if metadata:
        message["data"]["metadata"] = metadata
    return message


def assistant(text: str):
    return {"type": "ai", "data": {"content": text}}


def candidates(*items) -> str:
    return json.dumps(list(items), ensure_ascii=False)


class ExtractionFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, model):
        return build_memory_core(
            memory_root=self.root / "memory", clock=self.clock, model=model,
        )

    async def test_reference_context_is_scoped_and_time_is_optional(self):
        model = FakeModel(['[]', '[]', candidates({"text": "用户长期喜欢大号马克杯"})])
        core = self.build(model)
        await core.extraction.ingest_turn(
            "demo", [human("我在挑马克杯"), assistant("可以选小号杯或大号杯")],
            source_key="host:demo:private:ctx1", session_id="qq-one",
        )
        await core.extraction.ingest_turn(
            "demo", [human("别的会话秘密"), assistant("秘密建议")],
            source_key="host:demo:private:ctx2", session_id="qq-two",
        )
        await core.extraction.ingest_turn(
            "demo", [human("我一直喜欢大点的，以后都选大号"), assistant("本轮未确认的推测")],
            source_key="host:demo:private:ctx3", session_id="qq-one",
        )
        prompt = model.calls[-1]["messages"][0]["content"]
        self.assertIn("user | 我在挑马克杯", prompt)
        self.assertIn("assistant | 可以选小号杯或大号杯", prompt)
        self.assertNotIn("别的会话秘密", prompt)
        self.assertNotIn("秘密建议", prompt)
        self.assertIn("assistant | 本轮未确认的推测", prompt)
        self.assertIn("只针对当前回复", prompt)
        self.assertIn("可填写 event_when", prompt)
        self.assertNotIn('"offset": -1', prompt)
        fact = core.facts.load("demo")[0]
        self.assertIsNone(fact["event_start_at"])
        self.assertIsNone(fact["event_when_raw"])
        # Support vs. reference (growth design section 6.2): ctx1 only
        # disambiguated the reference, it is not what the fact rests on.
        self.assertEqual(fact["source_event_keys"], ["host:demo:private:ctx3"])
        self.assertEqual(fact["context_refs"], ["host:demo:private:ctx1"])
        self.assertEqual(fact["evidence_kind"], "user_assertion")
        # Deleting the earlier reference must not delete the fact.
        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), source_keys=("host:demo:private:ctx1",),
        ))
        self.assertEqual(len(core.facts.load("demo")), 1)
        # Deleting the real source must.
        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), source_keys=("host:demo:private:ctx3",),
        ))
        self.assertEqual(core.facts.load("demo"), [])

    async def test_game_dialogue_extracts_shared_life_facts_and_keeps_experience_scope(self):
        model = FakeModel([candidates({"text": "用户明天出差", "importance": 6})])
        core = self.build(model)
        message = human("这局保留药水，明天我出差")
        message["data"]["metadata"] = {"scene": "game:sts2", "applies_to": {}}
        await core.extraction.ingest_turn(
            "demo", [message], source_key="plugin:sts2_autoplay:demo:private:r1:msg1",
            content_class="life_fact", session_id="r1",
        )
        self.assertIn("提取游戏之外的生活事实和通用相处偏好", model.calls[0]["messages"][0]["content"])
        life = core.facts.load("demo")[0]
        self.assertEqual(life["text"], "用户明天出差")
        self.assertFalse(life.get("applies_to"))
        experience = human("主人和猫娘一起通关")
        experience["data"]["metadata"] = {"scene": "game:sts2", "applies_to": {}}
        await core.extraction.ingest_turn(
            "demo", [experience], source_key="plugin:sts2_autoplay:demo:private:r1:exp1",
            content_class="shared_experience", session_id="r1",
        )
        shared = next(row for row in core.facts.load("demo") if row["source"] == "shared_experience")
        self.assertEqual(shared["applies_to"], {})
        self.assertEqual(shared["retention_class"], "normal")
        self.assertNotIn("last_confirmed_at", shared)
        self.assertEqual(shared["source_event_keys"], ["plugin:sts2_autoplay:demo:private:r1:exp1"])
        for number, applies_to in enumerate(({"game_id": "sts2"}, {"game_id": "sts2", "session_id": "r1"}), 2):
            scoped = human("主人和猫娘一起通关")
            scoped["data"]["metadata"] = {"scene": "game:sts2", "applies_to": applies_to}
            await core.extraction.ingest_turn(
                "demo", [scoped], source_key=f"plugin:sts2_autoplay:demo:private:r1:exp{number}",
                content_class="shared_experience", session_id="r1",
            )
        ordinary = await core.recall.recall_memory("demo", "主人和猫娘一起通关", subject="private")
        self.assertEqual({row["id"] for row in ordinary["items"]}, {shared["id"]})
        game = await core.recall.recall_memory("demo", "主人和猫娘一起通关", subject="private", scene=Scene(game_id="sts2"))
        self.assertEqual(len(game["items"]), 2)
        run = await core.recall.recall_memory("demo", "主人和猫娘一起通关", subject="private", scene=Scene(game_id="sts2", session_id="r1"))
        self.assertEqual(len(run["items"]), 3)
        source = next(entry.payload for entry in core.journal.read_since("demo", -1)
                      if entry.event_type == "core.source_recorded" and entry.payload.get("source_key") == shared["source_event_keys"][0])
        self.assertEqual(source["scene"]["game_id"], "sts2")
        self.assertEqual(source["scene"]["session_id"], "r1")
        self.assertEqual(len(model.calls), 1)

    async def test_turn_reaches_raw_stores_and_facts_once(self):
        model = FakeModel([candidates(
            {"text": "用户喜欢在睡前喝热牛奶", "importance": 7,
             "event_when": {"start": {"offset": -1, "unit": "day"}, "end": None}},
            {"text": "用户养了一只叫团子的猫", "importance": 5},
        )])
        core = self.build(model)
        result = await core.extraction.ingest_turn(
            "demo",
            [human("我最近睡前都喝热牛奶，家里还有只猫叫团子", "host:demo:private:e1", "ep:host:e1:2026-10-22"),
             assistant("团子听起来很可爱")],
            source_key="host:demo:private:e1",
            episode_id="ep:host:e1:2026-10-22",
            session_id="s1",
        )
        self.assertEqual(result.status, "extracted")
        self.assertEqual(result.facts_written, 2)
        facts = core.facts.load("demo")
        self.assertEqual(len(facts), 2)
        by_text = {row["text"]: row for row in facts}
        milk = by_text["用户喜欢在睡前喝热牛奶"]
        self.assertTrue(milk["id"].startswith("fact_"))
        self.assertEqual(len(milk["hash"]), 16)
        self.assertEqual(milk["schema_version"], 2)
        self.assertEqual(milk["source"], "user_observation")
        self.assertFalse(milk["signal_processed"])
        self.assertEqual(milk["source_event_keys"], ["host:demo:private:e1"])
        self.assertEqual(milk["episode_id"], "ep:host:e1:2026-10-22")
        self.assertIsNotNone(milk["event_start_at"])
        self.assertEqual(core.recent.count("demo"), 2)
        index = core.timeindex("demo").open()
        try:
            self.assertEqual(index.count_original(), 2)
        finally:
            index.close()
        self.assertEqual(core.outbox.pending("demo"), ())
        self.assertEqual(model.calls[0]["tier"], "summary")
        self.assertEqual(model.calls[0]["max_output_tokens"], 4096)
        self.assertEqual(model.calls[0]["purpose"], "memory_fact_extraction")

        duplicate = await core.extraction.ingest_turn(
            "demo",
            [human("我最近睡前都喝热牛奶，家里还有只猫叫团子", "host:demo:private:e1", "ep:host:e1:2026-10-22"),
             assistant("团子听起来很可爱")],
            source_key="host:demo:private:e1",
            episode_id="ep:host:e1:2026-10-22",
            session_id="s1",
        )
        self.assertEqual(duplicate.status, "duplicate")
        self.assertEqual(len(core.facts.load("demo")), 2)

        reopened = build_memory_core(memory_root=self.root / "memory", clock=self.clock)
        self.assertEqual(len(reopened.facts.load("demo")), 2)

    async def test_assistant_lines_do_not_become_or_refresh_user_facts(self):
        model = FakeModel([
            candidates({"text": "用户喜欢雨天"}),
            candidates({"text": "用户喜欢雨天", "message_refs": ["host:demo:private:e3:m1"]}),
            candidates({"text": "角色喜欢雨天", "entity": "neko", "source": "ai_disclosure"}),
        ])
        core = self.build(model)
        first = await core.extraction.ingest_turn(
            "demo", [human("我一直喜欢雨天")], source_key="host:demo:private:e2",
        )
        self.assertEqual(first.status, "extracted")
        [original] = core.facts.load("demo")
        second = await core.extraction.ingest_turn(
            "demo", [human("今天下雨了"), assistant("我记得你喜欢雨天")],
            source_key="host:demo:private:e3", occurred_at=self.clock.now() + timedelta(days=3),
        )
        self.assertEqual(second.reason, "unresolved_sources")
        third = await core.extraction.ingest_turn(
            "demo", [human("今天下雨了"), assistant("我其实喜欢雨天")],
            source_key="host:demo:private:e4", occurred_at=self.clock.now() + timedelta(days=4),
        )
        self.assertEqual(third.status, "extracted")
        self.assertEqual(core.facts.load("demo"), [original])

    async def test_confirm_and_correct_keep_one_fact_and_use_spoken_time(self):
        model = FakeModel()
        core = self.build(model)
        now = self.clock.now()

        async def extract(key, spoken, user, **answer):
            model.responses.append(candidates(answer))
            return await core.extraction.extract_facts(
                "demo", [human(user)], source_keys=[f"host:demo:private:{key}"], occurred_at=spoken,
            )

        await extract("first", now, "我喜欢乌龙茶", text="我记得主人喜欢乌龙茶",
                      retention_class="normal")
        [row] = core.facts.load("demo")
        target = row["id"]
        for key, days in (("new", 3), ("new", 3), ("late", 1)):
            outcome = await extract(key, now + timedelta(days=days), "乌龙茶是我最爱喝的",
                                    text="对方偏爱乌龙茶", operation="confirm", target_id=target,
                                    retention_class="durable")
            self.assertEqual(outcome.status, "completed")
        [confirmed] = core.facts.load("demo")
        self.assertEqual(confirmed["text"], row["text"])
        self.assertEqual(confirmed["retention_class"], "normal")
        self.assertEqual(confirmed["confirm_count"], 3)
        self.assertEqual(confirmed["last_confirmed_at"], (now + timedelta(days=3)).isoformat())
        self.assertEqual(confirmed["confirmation_basis"], "user")
        invalid = await extract("bad", now, "乌龙茶是我最爱喝的", text="喜欢乌龙茶",
                                operation="confirm", target_id="not-offered")
        self.assertEqual(invalid.reason, "invalid_fact_target")
        result = await extract("corrected", now, "你记错了，我喜欢乌龙茶以外的白茶",
                               text="我记得主人喜欢白茶", operation="correct", target_id=target)
        self.assertEqual(result.status, "completed")
        old, replacement = core.facts.load("demo")
        self.assertTrue(old["invalidated_at"])
        self.assertEqual(replacement["text"], "我记得主人喜欢白茶")
        self.assertEqual(replacement["source_event_keys"], ["host:demo:private:corrected"])
        self.assertIn("白茶", core.correction_service.block("demo").block)
        await extract("corrected", now, "你记错了，我喜欢乌龙茶以外的白茶",
                      text="对方偏爱白茶", operation="add")
        self.assertEqual(len(core.facts.load("demo")), 2)
        self.assertEqual(len(core.correction_service.active("demo")["items"]), 1)

    async def test_failed_extraction_keeps_raw_text_and_retries(self):
        model = FakeModel([RuntimeError("provider down"), candidates(
            {"text": "用户周末常去公园跑步"},
        )])
        core = self.build(model)
        result = await core.extraction.ingest_turn(
            "demo", [human("我周末一般去公园跑步", "host:demo:private:e4")],
            source_key="host:demo:private:e4",
        )
        self.assertEqual(result.status, "extraction_failed")
        self.assertEqual(core.facts.load("demo"), [])
        pending = core.outbox.pending("demo")
        self.assertEqual(len(pending), 1)
        self.assertGreaterEqual(pending[0].attempts, 1)
        self.assertEqual(core.recent.count("demo"), 1)
        index = core.timeindex("demo").open()
        try:
            self.assertEqual(index.count_original(), 1)
        finally:
            index.close()

        outcomes = await core.extraction.process_outbox("demo")
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].status, "completed")
        self.assertEqual(core.outbox.pending("demo"), ())
        facts = core.facts.load("demo")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["text"], "用户周末常去公园跑步")

    async def test_late_result_cannot_resurrect_a_deleted_window(self):
        core = self.build(None)

        async def delete_while_model_runs(model, messages):
            await core.deletion_service.forget(DeletionRequest(
                scope=Scope("demo"), source_keys=("host:demo:private:e5",),
            ))

        model = FakeModel([candidates({"text": "用户喜欢热可可"})], hook=delete_while_model_runs)
        core.extraction._model = model
        outcome = await core.extraction.extract_facts(
            "demo", [human("我喜欢热可可", "host:demo:private:e5")],
            source_keys=["host:demo:private:e5"],
        )
        self.assertEqual(outcome.status, "stale")
        self.assertEqual(core.facts.load("demo"), [])

        # Even a rewritten candidate (no source key of its own) must be dropped
        # when the window's source key is already deleted.
        rewritten = FakeModel([candidates({"text": "用户爱喝热巧克力"})])
        core.extraction._model = rewritten
        outcome = await core.extraction.extract_facts(
            "demo", [{"type": "human", "data": {"content": "我喜欢热可可"}}],
            source_keys=["host:demo:private:e5"],
        )
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.facts_written, 0)
        self.assertEqual(core.facts.load("demo"), [])


if __name__ == "__main__":
    unittest.main()

