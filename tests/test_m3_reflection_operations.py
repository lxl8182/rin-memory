"""M3: reflection-layer rework - conditional strategies, not per-batch slogans.

Acceptance for the reworked synthesis path (design section 7.2 and section 14
G12/G13/G14):

* G13 an ordinary batch that contains nothing new yields `no_change`: no row
  is written, no dead-letter backoff is consumed, and the model is not called
  again for the same evidence (round 2 of the maintenance loop is free);
* G12 "used to like black tea, now oolong" updates the current view while the
  old understanding stays queryable as history (`superseded`, not deleted);
* G14 new feedback that contradicts an adopted strategy narrows or retracts it
  and leaves the counter-evidence on the record (`disputation`);
* one topic may hold several different understandings, and all of them stay
  recallable - the topic key groups candidates, it never forces uniqueness;
* the synthesis prompt actually receives the old understandings, their
  maturity/scope/counter-evidence and the strategies already adopted.

Every case runs over the public core with a tempfile root; nothing here reads
or writes a real runtime directory.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.domain.maintenance import MaintenanceSettings
from rin_memory.domain.reflection import (
    REFLECTION_OPERATIONS,
    reflection_id_from_topic,
    scope_key,
    topic_key,
)


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


class ScriptedModel:
    """Replays a fixed list of synthesis payloads and records every prompt."""

    def __init__(self, replies=(), default=None):
        self.calls = []
        self.prompts = []
        self._replies = list(replies)
        self._default = default if default is not None else {
            "operation": "no_change", "reflection": None,
        }

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls.append(purpose)
        self.prompts.append(messages[0]["content"] if messages else "")
        if purpose != "reflection_synthesis":
            return "{}"
        reply = self._replies.pop(0) if self._replies else self._default
        if isinstance(reply, str):
            return reply
        return json.dumps(reply, ensure_ascii=False)

    @property
    def synthesis_calls(self):
        return [item for item in self.calls if item == "reflection_synthesis"]


def fact(fact_id, text, *, importance=7, subject="private", scene=None):
    row = {
        "id": fact_id,
        "text": text,
        "importance": importance,
        "source": "user_observation",
        "schema_version": 2,
        "absorbed": False,
        "created_at": "2026-10-20T00:00:00+00:00",
        "source_event_keys": ["host:demo:private:" + fact_id],
        "subject": subject,
    }
    if scene:
        row["applies_to"] = scene
    return row


def batch(prefix, text, count=5, *, start=0, **kwargs):
    return [
        fact("%s_%d" % (prefix, index), text % index, **kwargs)
        for index in range(start, start + count)
    ]


def add_op(text, topic, **extra):
    payload = {
        "operation": "add", "reflection": text, "topic": topic,
        "entity": "master",
    }
    payload.update(extra)
    return payload


class ReflectionOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = MutableClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, model, **kwargs):
        return build_memory_core(
            memory_root=self.root / "memory", clock=self.clock, model=model,
            **kwargs,
        )

    # -- G13: no_change is a real outcome, not a forced conclusion ----------

    async def test_g13_ordinary_facts_report_no_change_without_a_row(self):
        model = ScriptedModel([{"operation": "no_change", "reflection": None}])
        core = self.build(model)
        core.recover("demo")
        core.facts.save("demo", batch("n", "用户今天提了一句普通第%d件事"))
        outcome = await core.reflection_service.synthesize("demo")
        self.assertEqual(outcome.status, "no_change")
        self.assertEqual(outcome.operation, "no_change")
        self.assertEqual(outcome.reflection_ids, ())
        self.assertEqual(core.reflections.load("demo"), [])
        self.assertEqual(core.reflection_service.visible("demo"), [])
        # A no-op batch is a success: it must not consume the failure budget.
        backoff = {key: value for key, value in core.reflections.load_backoff("demo").items()
                   if key.startswith("fg02:")}
        self.assertEqual(len(backoff), 1)
        self.assertEqual(next(iter(backoff.values()))["n"], 0)

    async def test_g13_empty_reflection_is_no_change_not_failure(self):
        # A model that answers with an empty understanding is reporting
        # "nothing new"; the old code called exactly that failed.
        model = ScriptedModel(['{"reflection": "   "}'])
        core = self.build(model)
        core.recover("demo")
        core.facts.save("demo", batch("e", "用户随口说的第%d句话"))
        first = await core.reflection_service.synthesize("demo")
        self.assertEqual(first.status, "no_change")
        self.assertNotEqual(first.status, "failed")
        self.assertEqual(core.reflections.load("demo"), [])

    async def test_same_batch_is_not_resynthesised_without_new_evidence(self):
        model = ScriptedModel()
        core = self.build(model)
        core.recover("demo")
        core.facts.save("demo", batch("s", "用户提到第%d件普通小事"))
        first = await core.reflection_service.synthesize("demo")
        second = await core.reflection_service.synthesize("demo")
        self.assertEqual(first.status, "no_change")
        self.assertEqual(second.status, "no_change")
        self.assertEqual(second.reason, "already_settled")
        self.assertEqual(len(model.synthesis_calls), 1)

    async def test_maintenance_round_two_makes_no_model_call(self):
        # Two consecutive maintenance rounds: the second one must not call the
        # model at all for the same, unchanged evidence.
        model = ScriptedModel()
        core = self.build(
            model,
            maintenance_settings=MaintenanceSettings(
                interval_seconds=60.0, synthesis_limit=3,
            ),
        )
        core.recover("demo")
        core.facts.save("demo", batch("m", "用户提到第%d件普通小事"))
        first = await core.maintenance.run_once("demo")
        self.assertEqual(first.steps["synthesize"]["status"], "ok")
        calls_after_first = len(model.synthesis_calls)
        self.assertEqual(calls_after_first, 1)
        second = await core.maintenance.run_once("demo")
        self.assertEqual(second.steps["synthesize"]["status"], "ok")
        self.assertEqual(len(model.synthesis_calls), calls_after_first)
        self.assertEqual(core.reflections.load("demo"), [])

    async def test_new_evidence_reopens_a_settled_batch(self):
        model = ScriptedModel([
            {"operation": "no_change", "reflection": None},
            add_op("用户喜欢在深夜写代码", "作息"),
        ])
        core = self.build(model)
        core.recover("demo")
        core.facts.save("demo", batch("a", "用户提到第%d件普通小事"))
        self.assertEqual(
            (await core.reflection_service.synthesize("demo")).status, "no_change",
        )
        # New evidence changes the unabsorbed id set, hence the backoff key,
        # hence the settled marker no longer applies.
        core.facts.save(
            "demo",
            batch("a", "用户提到第%d件普通小事")
            + batch("b", "用户凌晨还在写代码第%d次", start=100),
        )
        second = await core.reflection_service.synthesize("demo")
        self.assertEqual(second.status, "completed")
        self.assertEqual(second.operation, "add")
        self.assertEqual(len(core.reflections.load("demo")), 1)
        self.assertEqual(len(model.synthesis_calls), 2)

    # -- G12: an outdated understanding is retired, history stays ----------

    async def test_g12_tea_change_updates_view_and_keeps_history(self):
        model = ScriptedModel([
            add_op("用户以前喜欢喝红茶", "饮品偏好"),
            add_op("用户现在改喝乌龙茶", "饮品偏好", operation="supersede"),
        ])
        core = self.build(model)
        core.recover("demo")
        core.facts.save("demo", batch("t", "用户以前每天喝红茶第%d次"))
        first = await core.reflection_service.synthesize("demo")
        self.assertEqual(first.operation, "add")
        old_id = first.reflection_ids[0]

        core.facts.save(
            "demo",
            batch("t", "用户以前每天喝红茶第%d次")
            + batch("u", "用户现在改喝乌龙茶第%d次", start=100),
        )
        second = await core.reflection_service.synthesize("demo")
        self.assertEqual(second.status, "completed")
        self.assertEqual(second.operation, "supersede")
        new_id = second.reflection_ids[0]
        self.assertNotEqual(new_id, old_id)

        rows = {row["id"]: row for row in core.reflections.load("demo")}
        self.assertEqual(rows[old_id]["status"], "superseded")
        self.assertEqual(rows[old_id]["superseded_by"], new_id)
        self.assertEqual(rows[new_id]["supersedes"], [old_id])
        # The current view carries only the new understanding ...
        visible = [row["text"] for row in core.reflection_service.visible("demo")]
        self.assertEqual(visible, ["用户现在改喝乌龙茶"])
        block = core.reflection_service.block("demo").block
        self.assertIn("乌龙", block)
        self.assertNotIn("红茶", block)
        # ... but the old one is still queryable as history.
        self.assertIn(old_id, rows)
        self.assertEqual(rows[old_id]["text"], "用户以前喜欢喝红茶")

    async def test_superseded_rows_stop_being_recalled(self):
        model = ScriptedModel([
            add_op("用户以前喜欢喝红茶", "饮品偏好"),
            add_op("用户现在改喝乌龙茶", "饮品偏好", operation="supersede"),
        ])
        core = self.build(model)
        core.recover("demo")
        core.facts.save("demo", batch("t", "用户以前每天喝红茶第%d次"))
        await core.reflection_service.synthesize("demo")
        core.facts.save(
            "demo",
            batch("t", "用户以前每天喝红茶第%d次")
            + batch("u", "用户现在改喝乌龙茶第%d次", start=100),
        )
        await core.reflection_service.synthesize("demo")
        result = await core.recall.recall_memory("demo", "红茶 乌龙茶")
        texts = " ".join(
            str(item.get("text") or "") for item in result.get("items") or ()
        )
        self.assertIn("乌龙", texts)
        self.assertNotIn("红茶", texts)

    # -- G14: a contradiction narrows or retracts an adopted strategy ------

    async def test_g14_contradiction_retires_strategy_and_records_disputation(self):
        habit = add_op(
            "用户周末早晨会晨跑", "运动",
            memory_kind="habit", claim="用户周末早晨会晨跑",
            situation="周末早晨",
        )
        contradict = {
            "operation": "contradict",
            "reflection": "用户周末早晨其实不会晨跑，只是偶尔散步",
            "topic": "运动",
            "entity": "master",
        }
        model = ScriptedModel([habit, contradict])
        core = self.build(model)
        core.recover("demo")
        core.facts.save("demo", batch("r", "用户周末早晨晨跑第%d次"))
        first = await core.reflection_service.synthesize("demo")
        strategy_id = first.reflection_ids[0]
        self.assertEqual(first.growth_item_ids, (strategy_id,))

        # Make it an adopted strategy (confirmed + independent support).
        rows = core.reflections.load("demo")
        rows[0]["status"] = "confirmed"
        rows[0]["support_count"] = 3
        rows[0]["support_event_keys"] = ["k1", "k2", "k3"]
        core.reflections.save("demo", rows)
        self.assertEqual(
            [row["id"] for row in core.reflection_service.growth_items("demo")],
            [strategy_id],
        )

        core.facts.save(
            "demo",
            batch("r", "用户周末早晨晨跑第%d次")
            + batch("w", "用户说周末早晨其实没有晨跑第%d次", start=100),
        )
        second = await core.reflection_service.synthesize("demo")
        self.assertEqual(second.status, "completed")
        self.assertEqual(second.operation, "contradict")

        after = {row["id"]: row for row in core.reflections.load("demo")}
        self.assertEqual(after[strategy_id]["status"], "superseded")
        self.assertEqual(
            after[strategy_id]["superseded_reason"],
            "contradicted_by_new_evidence",
        )
        # Counter-evidence stays on the record instead of only a status flip.
        self.assertGreater(after[strategy_id]["disputation"], 0.0)
        # The retired strategy is no longer an adopted strategy ...
        self.assertEqual(core.reflection_service.growth_items("demo"), [])
        self.assertNotIn("晨跑", core.reflection_service.growth_block("demo").block)
        # ... and only the narrowed understanding remains visible.
        visible = [row["text"] for row in core.reflection_service.visible("demo")]
        self.assertEqual(len(visible), 1)
        self.assertIn("散步", visible[0])

    async def test_retired_strategy_is_not_re_adopted_by_promotion(self):
        habit = add_op(
            "用户周末早晨会晨跑", "运动",
            memory_kind="habit", claim="用户周末早晨会晨跑",
            situation="周末早晨",
        )
        contradict = {
            "operation": "contradict", "topic": "运动", "entity": "master",
            "reflection": "用户周末早晨不会晨跑",
        }
        model = ScriptedModel([habit, contradict])
        core = self.build(model)
        core.recover("demo")
        core.facts.save("demo", batch("r", "用户周末早晨晨跑第%d次"))
        await core.reflection_service.synthesize("demo")
        rows = core.reflections.load("demo")
        rows[0]["status"] = "confirmed"
        rows[0]["support_count"] = 3
        rows[0]["support_event_keys"] = ["k1", "k2", "k3"]
        core.reflections.save("demo", rows)

        core.facts.save(
            "demo",
            batch("r", "用户周末早晨晨跑第%d次")
            + batch("w", "用户说周末没有晨跑第%d次", start=100),
        )
        await core.reflection_service.synthesize("demo")
        await core.reflection_service.promote("demo")
        # The retired plan is not silently re-adopted by a later promotion.
        self.assertEqual(core.reflection_service.growth_items("demo"), [])
        self.assertEqual(core.persona_service.entries("demo"), [])

    # -- one topic, several understandings ---------------------------------

    async def test_same_topic_holds_two_understandings_both_recallable(self):
        model = ScriptedModel([
            add_op("用户喜欢拿铁", "咖啡"),
            add_op("用户喝咖啡会胃疼", "咖啡"),
        ])
        core = self.build(model)
        core.recover("demo")
        core.facts.save("demo", batch("c", "用户点了拿铁第%d次"))
        first = await core.reflection_service.synthesize("demo")
        self.assertEqual(first.operation, "add")
        self.assertEqual(len(core.reflections.load("demo")), 1)

        core.facts.save(
            "demo",
            batch("c", "用户点了拿铁第%d次")
            + batch("g", "用户喝完咖啡说胃疼第%d次", start=100),
        )
        second = await core.reflection_service.synthesize("demo")
        # A different understanding in the same topic is an add, never a
        # supersede: the topic key groups candidates, it never forces one row.
        self.assertEqual(second.operation, "add")
        rows = core.reflections.load("demo")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["topic"] for row in rows}, {"咖啡"})
        self.assertEqual({row["status"] for row in rows}, {"pending"})
        self.assertEqual({row["topic_key"] for row in rows}, {topic_key("咖啡")})
        # Both stay visible and both stay recallable.
        visible = {row["text"] for row in core.reflection_service.visible("demo")}
        self.assertEqual(visible, {"用户喜欢拿铁", "用户喝咖啡会胃疼"})
        recalled = await core.recall.recall_memory("demo", "咖啡 拿铁")
        texts = " ".join(
            str(item.get("text") or "") for item in recalled.get("items") or ()
        )
        self.assertIn("拿铁", texts)
        self.assertIn("胃疼", texts)

    async def test_promoted_understanding_is_retired_with_its_persona_entry(self):
        # A promoted reflection was materialised into the persona card. New
        # evidence must retire both, otherwise the outdated understanding keeps
        # shaping replies through the persona block.
        model = ScriptedModel([
            add_op("用户以前喜欢喝红茶", "饮品偏好"),
            add_op("用户现在改喝乌龙茶", "饮品偏好", operation="supersede"),
        ])
        core = self.build(model)
        core.recover("demo")
        core.facts.save("demo", batch("t", "用户以前每天喝红茶第%d次"))
        first = await core.reflection_service.synthesize("demo")
        old_id = first.reflection_ids[0]
        for _ in range(2):
            await core.reflection_service.record_surfaced("demo", old_id)
            await core.reflection_service.record_feedback("demo", old_id, "confirmed")
        await core.reflection_service.promote("demo")
        await core.reflection_service.promote("demo")
        self.assertEqual(
            [entry["id"] for entry in core.persona_service.entries("demo")],
            ["prom_" + old_id],
        )

        core.facts.save("demo", batch("u", "用户现在改喝乌龙茶第%d次"))
        second = await core.reflection_service.synthesize("demo")
        self.assertEqual(second.operation, "supersede")
        self.assertEqual(
            {row["id"]: row["status"] for row in core.reflections.load("demo")}[old_id],
            "superseded",
        )
        self.assertEqual(core.persona_service.entries("demo"), [])

    # -- prompt ------------------------------------------------------------

    async def test_prompt_carries_old_rows_scope_and_strategies(self):
        seed = add_op(
            "用户在倾诉工作挫败时先接住感受", "沟通方式",
            memory_kind="experience", claim="先接住感受再说建议",
            situation="用户倾诉工作挫败时",
        )
        model = ScriptedModel([
            seed, {"operation": "no_change", "reflection": None},
        ])
        core = self.build(model)
        core.recover("demo")
        core.facts.save("demo", batch("p", "用户倾诉工作挫败第%d次"))
        first = await core.reflection_service.synthesize("demo")
        strategy_id = first.reflection_ids[0]
        rows = core.reflections.load("demo")
        rows[0]["status"] = "confirmed"
        rows[0]["disputation"] = 0.4
        rows[0]["support_count"] = 4
        rows[0]["support_event_keys"] = ["k1", "k2", "k3", "k4"]
        core.reflections.save("demo", rows)

        core.facts.save(
            "demo",
            batch("p", "用户倾诉工作挫败第%d次")
            + batch("q", "用户又提到工作上的第%d件事", start=100),
        )
        await core.reflection_service.synthesize("demo")
        prompt = model.prompts[-1]
        self.assertIn("【已有认识】", prompt)
        self.assertIn("【已采用策略】", prompt)
        self.assertIn("【新事实】", prompt)
        # The old understanding, its id and its maturity/counter-evidence.
        self.assertIn(strategy_id, prompt)
        self.assertIn("用户在倾诉工作挫败时先接住感受", prompt)
        self.assertIn("disp=0.40", prompt)
        self.assertIn("support=4", prompt)
        # The adopted strategy block, not just the raw facts.
        self.assertIn("先接住感受再说建议", prompt)
        # Every operation the model may choose must be named.
        for operation in REFLECTION_OPERATIONS:
            self.assertIn(operation, prompt)
        # And the old "always produce an understanding" pressure is gone.
        self.assertNotIn("没有稳定认识时也要给出最保守的一条", prompt)


class ReflectionIdTests(unittest.TestCase):
    """The id is content-addressed now; it is no longer a fact-batch hash."""

    def test_id_is_stable_for_the_same_understanding(self):
        first = reflection_id_from_topic("咖啡", None, "用户喜欢拿铁")
        second = reflection_id_from_topic("咖啡", {}, "用户喜欢拿铁")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("ref_"))

    def test_distinct_understandings_get_distinct_ids(self):
        first = reflection_id_from_topic("咖啡", None, "用户喜欢拿铁")
        second = reflection_id_from_topic("咖啡", None, "用户喝咖啡会胃疼")
        self.assertNotEqual(first, second)

    def test_topic_and_scope_participate_in_the_id(self):
        base = reflection_id_from_topic("咖啡", None, "用户喜欢拿铁")
        other_topic = reflection_id_from_topic("饮品", None, "用户喜欢拿铁")
        other_scope = reflection_id_from_topic(
            "咖啡", {"game_id": "sts2"}, "用户喜欢拿铁",
        )
        self.assertNotEqual(base, other_topic)
        self.assertNotEqual(base, other_scope)

    def test_scope_key_is_empty_for_shared_scope(self):
        self.assertEqual(scope_key(None), "")
        self.assertEqual(scope_key({}), "")
        self.assertTrue(scope_key({"game_id": "sts2"}).startswith("sc_"))


if __name__ == "__main__":
    unittest.main()
