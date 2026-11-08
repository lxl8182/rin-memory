"""FG02: speaker attribution, topic progress and in-flight invalidation."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from test_b04_extraction import extraction_reply

from rin_memory.adapters.composition import build_memory_core
from rin_memory.contracts import CorrectionRequest, DeletionRequest, DeletionTarget, Scope
from rin_memory.domain.extraction import ExtractionSettings
from rin_memory.domain.growth_evidence import support_basis_from_facts


class Clock:
    def __init__(self):
        self.time = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)

    def now(self):
        return self.time

    def monotonic(self):
        return 0.0


class Model:
    def __init__(self):
        self.calls = []
        self.reply = {"operation": "no_change"}
        self.during_call = None

    async def invoke(self, tier, messages, **kwargs):
        self.calls.append((kwargs["purpose"], messages[0]["content"]))
        if self.during_call:
            callback, self.during_call = self.during_call, None
            await callback()
        if kwargs["purpose"] == "memory_fact_extraction":
            return extraction_reply(self.reply, messages[0]["content"])
        return json.dumps(self.reply, ensure_ascii=False)


def message(role, text, **meta):
    return {"type": role, "data": {"content": text, "metadata": meta}}


def fact(key, text, importance=7, **extra):
    return dict(id=key, text=text, subject="private", importance=importance,
                source="user_observation", absorbed=False, schema_version=2,
                source_event_keys=[f"host:demo:private:{key}"], **extra)


class ExperienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, model, name="memory", **kwargs):
        return build_memory_core(memory_root=self.root / name, clock=self.clock, model=model, **kwargs)

    def publish(self, core):
        core.persona_card_service.import_initial("demo", [
            {"section_id": "identity", "text": "名字: 铃芽\n我喜欢留意日常的小细节。"},
            {"section_id": "tone", "text": "我温柔又好奇。"},
            {"section_id": "private_other", "text": "其他主体的秘密", "scope": {"subject": "someone"}},
        ])
        return core.persona_publish.publish("demo", {}).version

    async def test_speakers_bind_identically_in_single_and_batch_and_enter_reflection(self):
        for batch in (False, True):
            with self.subTest(batch=batch):
                model = Model()
                core = self.build(model, str(batch), extraction_settings=ExtractionSettings(inline=not batch))
                version = self.publish(core)
                key = "host:demo:private:walk"
                model.reply = [
                    {"text": "主人告诉我，他今天去散步。", "entity": "master", "source": "user_observation",
                     "sources": [1], "message_refs": [key + ":m0"]},
                    {"text": "我想了解主人散步看见的花。", "entity": "neko", "source": "ai_disclosure",
                     "evidence_kind": "observed_action", "sources": [1], "message_refs": [key + ":m1"]},
                    {"text": "我们散步后分享了路边的花。", "entity": "relationship", "source": "shared_experience",
                     "sources": [1], "message_refs": [key + ":m0"]},
                ]
                await core.extraction.ingest_turn("demo", [message("human", "今天散步见到花了。刚才和你分享了好多花。"),
                                                           message("ai", "我想听听那些花，下次我们也一起留意吧。")],
                                                  source_key=key, occurred_at=self.clock.now())
                if batch:
                    await core.extraction.process_outbox("demo")
                rows = core.facts.load("demo")
                self.assertEqual({row["entity"] for row in rows}, {"master", "relationship"})
                selected = next(row for row in rows if row["entity"] == "relationship")
                # A single substantive experience can produce pending understanding.
                model.reply = {"operation": "add", "reflection": "分享散步中的小发现让我觉得亲近。",
                               "source_fact_ids": [selected["id"]]}
                core.recent.replace("demo", [])  # Force the durable time-index path.
                outcome = await core.reflection_service.synthesize("demo")
                self.assertEqual(outcome.status, "completed")
                self.assertEqual(outcome.absorbed_fact_ids, (selected["id"],))
                prompt = model.calls[-1][1]
                for value in ("名字: 铃芽", "我温柔又好奇", "今天散步见到花了", "我想听听那些花", key + ":m1"):
                    self.assertIn(value, prompt)
                self.assertNotIn("其他主体的秘密", prompt)
                self.assertLessEqual(len(prompt), core.reflection_service.settings.synthesis_input_chars)
                self.assertEqual(core.reflections.load("demo")[0]["persona_version"], version)
                # A false user attribution fails before any fact is committed.
                model.reply = [{"text": "主人喜欢那些花。", "entity": "master", "source": "user_observation",
                                "sources": [1], "message_refs": [key + ":m0"]}]
                result = await core.extraction.extract_facts("demo", [message("ai", "我想听听那些花")], source_keys=[key])
                self.assertEqual(result.reason, "unresolved_sources")

    async def test_topic_results_survive_restart_skip_settled_and_split_exact_sources(self):
        model = Model()
        core = self.build(model)
        core.facts.save("demo", [fact("cat", "我听主人讲蓝猫晒太阳。", 10, topic_key="cat"),
                                  fact("work", "数据库迁移终于完成了。", 8),
                                  fact("game", "我们打败了最终首领。", 6)])
        self.assertEqual((await core.reflection_service.synthesize("demo")).status, "no_change")
        calls = len(model.calls)
        core = self.build(model)
        self.assertEqual((await core.reflection_service.synthesize("demo", respect_interval=True)).reason, "normal_interval")
        model.reply = {"operation": "add", "reflection": "一起完成迁移让我更愿意参与主人的项目。",
                       "topic": "项目协作", "source_fact_ids": ["work"]}
        result = await core.reflection_service.synthesize("demo")
        self.assertEqual(result.absorbed_fact_ids, ("work",))
        self.assertEqual(len(model.calls), calls + 1)
        self.assertNotIn("蓝猫晒太阳", model.calls[-1][1])
        model.reply = {"operation": "add", "reflection": "攻克首领是值得记住的共同体验。", "source_fact_ids": ["game"]}
        self.assertEqual((await core.reflection_service.synthesize("demo")).absorbed_fact_ids, ("game",))
        self.assertFalse(next(row for row in core.facts.load("demo") if row["id"] == "cat")["absorbed"])
        # Adding two related details reopens the cat topic. The model, rather
        # than a lexical assignment, binds each split part to its own detail.
        rows = core.facts.load("demo") + [fact("cat_health", "蓝猫今天看完兽医，恢复了。", 10, topic_key="cat"),
                                         fact("cat_play", "蓝猫喜欢追逗猫棒玩。", 10, topic_key="cat")]
        core.facts.save("demo", rows)
        core.reflections.save("demo", core.reflections.load("demo") + [{
            "id": "old_cat", "text": "我对蓝猫的日常越来越熟悉。", "topic": "蓝猫", "status": "pending",
            "subject": "private", "source_fact_ids": ["cat"],
        }])
        model.reply = {"operation": "split", "target_reflection_id": "old_cat", "topic": "蓝猫",
                       "parts": [{"reflection": "我记挂它恢复后的精神。", "source_fact_ids": ["cat_health"]},
                                 {"reflection": "我想听更多它玩耍的小事。", "source_fact_ids": ["cat_play"]}]}
        result = await core.reflection_service.synthesize("demo")
        self.assertEqual(result.operation, "split")
        self.assertEqual(set(result.absorbed_fact_ids), {"cat_health", "cat_play"})
        rows = {row["id"]: row for row in core.reflections.load("demo")}
        self.assertEqual({tuple(rows[key]["source_fact_ids"]) for key in result.reflection_ids},
                         {("cat_health",), ("cat_play",)})
        self.assertEqual(rows["old_cat"]["status"], "superseded")
        deleted = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), targets=(DeletionTarget("fact", "cat_health"),), request_id="remove-health"))
        self.assertEqual(deleted.status, "completed")
        core = self.build(model)
        self.assertTrue(any(row["text"] == "我想听更多它玩耍的小事。" for row in core.reflections.load("demo")))
        self.assertFalse(any("cat_health" in row.get("source_fact_ids", []) for row in core.reflections.load("demo")))

    async def test_generation_rechecks_sources_persona_and_no_change_before_saving(self):
        for change in ("correction", "deletion", "persona"):
            with self.subTest(change=change):
                model = Model()
                core = self.build(model, change)
                self.publish(core)
                core.facts.save("demo", [fact("one", "主人养的是蓝猫。")])

                async def mutate():
                    if change == "correction":
                        await core.correction_service.correct(CorrectionRequest(
                            scope=Scope("demo"), action="replace", old_text="主人养的是蓝猫", new_text="主人养的是狸花猫"))
                    elif change == "deletion":
                        await core.deletion_service.forget(DeletionRequest(
                            scope=Scope("demo"), targets=(DeletionTarget("fact", "one"),), request_id="remove-one"))
                    else:
                        core.persona_card_service.revise("demo", {"base_revision": 1,
                            "operations": [{"op": "replace", "section_id": "tone", "text": "我认真而直接。"}]})
                        core.persona_publish.publish("demo", {})

                model.during_call = mutate
                model.reply = {"operation": "no_change"}
                stale = await core.reflection_service.synthesize("demo")
                self.assertEqual(stale.status, "stale")
                self.assertFalse(any(entry.get("nc") for entry in core.reflections.load_backoff("demo").values()))
                self.assertEqual(core.reflections.load("demo"), [])
                if change == "persona":
                    model.reply = {"operation": "add", "reflection": "我记得主人养猫的生活。", "source_fact_ids": ["one"]}
                    self.assertEqual((await core.reflection_service.synthesize("demo")).status, "completed")
                    self.assertIn("我认真而直接", model.calls[-1][1])
                else:
                    self.assertEqual((await core.reflection_service.synthesize("demo")).status, "skipped")


if __name__ == "__main__":
    unittest.main()
