"""FG03: whole-section ownership, generation conflicts and durable retries."""
import json
import tempfile
import unittest
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.service.memory_service import MemoryService
from rin_memory.contracts import CorrectionRequest, DeletionRequest, Scope


class ComposerModel:
    def __init__(self):
        self.text = "我温柔地说重点，按需要补充。"
        self.base = "我说话温柔。"
        self.calls = []
        self.callback = None

    async def invoke(self, tier, messages, **kwargs):
        plan = json.loads(messages[-1]["content"])
        self.calls.append(plan)
        if self.callback:
            callback, self.callback = self.callback, None
            callback()
        return json.dumps({"base_text": self.base,
                           "paragraphs": [{"text": self.text, "refs": ["user", *[rule["rule_id"] for rule in plan["rules"]]]}],
                           "superseded_rule_ids": []}, ensure_ascii=False)


class RevisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.model = ComposerModel()
        self.core = build_memory_core(memory_root=self.root, model=self.model)
        self.teacher = MemoryService._build_persona_teaching(self.core, self.model)
        self.core.recover("demo")
        self.core.persona_card_service.import_initial("demo", [{"section_id": "identity", "text": "我叫铃芽。"},
                                                               {"section_id": "tone", "text": self.model.base}])
        self.core.persona_publish.publish("demo")

    def tearDown(self):
        self.tmp.cleanup()

    def req(self, number, text):
        return {"source_key": f"host:demo:private:{number}", "source_text": text,
                "base_revision": self.core.persona_card_service.get("demo").revision}

    def tone(self):
        return self.core.persona_card_service.render_projection("demo")["tone"]

    async def test_rewritten_rules_converge_and_revoke_preserves_user_edit_and_other_rule(self):
        first = await self.teacher.ateach("demo", self.req(1, "以后简短一点"))
        self.assertEqual(first.status, "applied")
        self.assertEqual(self.tone(), self.model.text)
        self.model.text = "我温柔又亲近地聊天，先说重点，再按需要补充。"
        second = await self.teacher.ateach("demo", self.req(2, "以后亲近一点"))
        self.assertEqual(second.status, "applied")
        self.assertEqual(len(self.teacher.rules("demo")), 2)
        before = self.core.persona_card_service.get("demo").revision
        repeated = await self.teacher.ateach("demo", self.req(3, "以后回答精简一点"))
        self.assertEqual(repeated.status, "applied")
        self.assertEqual(self.core.persona_card_service.get("demo").revision, before + 1)
        self.assertEqual(len(self.model.calls), 2)
        await self.teacher.arevoke("demo", {"teaching_id": repeated.teaching_id,
            "base_revision": self.core.persona_card_service.get("demo").revision})
        before = self.core.persona_card_service.get("demo").revision
        self.core.persona_card_service.revise("demo", {"base_revision": before,
            "operations": [{"op": "replace", "section_id": "tone", "text": "我温柔又俏皮，亲近地陪聊，回应简练。"}]})
        self.model.base = "我说话温柔又俏皮。"
        self.model.text = "我温柔又俏皮，亲近地陪主人聊开心的事。"
        result = await self.teacher.arevoke("demo", {"teaching_id": first.teaching_id,
            "base_revision": self.core.persona_card_service.get("demo").revision})
        self.assertEqual(result.status, "revoked")
        self.assertEqual(self.tone(), self.model.text)
        self.assertEqual(self.model.calls[-1]["user_edited"], True)
        self.assertNotIn(first.teaching_id, json.dumps(self.model.calls[-1]["rules"]))
        self.assertIn(second.teaching_id, json.dumps(self.model.calls[-1]["rules"]))
        self.assertEqual([rule["teaching_id"] for rule in self.teacher.rules("demo")], [second.teaching_id])
        await self.core.deletion_service.forget(DeletionRequest(scope=Scope("demo"),
            source_keys=("host:demo:private:2",), request_id="delete-rewritten-teaching"))
        self.assertIn("俏皮", self.tone())
        self.assertNotIn("亲近", self.tone())
        self.core = build_memory_core(memory_root=self.root, model=self.model)
        self.teacher = MemoryService._build_persona_teaching(self.core, self.model)
        self.core.recover("demo")
        self.assertNotIn("亲近", self.tone())
        section = next(row for row in self.core.persona_card_service.get("demo").sections if row.section_id == "tone")
        growth_text = self.tone() + "我喜欢探索烘焙。"
        basis = dict(section.content_basis, text=growth_text,
                     growth=[{"id": "shared_observation", "text": "我喜欢探索烘焙。", "source_fact_ids": ["growth_fact"]}],
                     paragraphs=[{"text": growth_text, "refs": ["user", "growth:shared_observation"]}])
        self.core.facts.save("demo", [{"id": "growth_fact", "text": "这次烘焙形成长期兴趣", "subject": "private"}])
        self.core.persona_card_service.revise("demo", {
            "base_revision": self.core.persona_card_service.get("demo").revision, "origin": "evolution",
            "operations": [{"op": "replace", "section_id": "tone", "text": growth_text,
                            "content_basis": basis, "source_refs": ["growth_fact"]}]})
        self.core.persona_publish.publish("demo")
        corrected = await self.core.correction_service.correct(CorrectionRequest(scope=Scope("demo"), action="replace",
            target_fact_ids=("growth_fact",), new_text="这次是临时尝试，之后没有继续。"))
        self.assertTrue(corrected.applied)
        self.assertIn("俏皮", self.tone())
        self.assertNotIn("烘焙", self.tone())
        self.assertTrue(self.core.persona_publish.current("demo").available)

    async def test_user_edit_during_model_wait_returns_conflict_and_keeps_published_persona(self):
        published = self.core.persona_publish.current("demo").version
        self.model.callback = lambda: self.core.persona_card_service.revise("demo", {"base_revision": 1,
            "operations": [{"op": "replace", "section_id": "tone", "text": "我保留用户的新编辑。"}]})
        result = await self.teacher.ateach("demo", self.req(1, "以后简短一点"))
        self.assertEqual(result.status, "conflict")
        self.assertEqual(self.tone(), "我保留用户的新编辑。")
        self.assertEqual(self.core.persona_publish.current("demo").version, published)
        self.assertEqual(self.teacher.teachings("demo"), [])

    async def test_card_save_and_publish_interruptions_resume_without_a_second_edit(self):
        req = self.req(1, "以后简短一点")
        original = self.teacher._store.record
        def fail_after_card(character, payload, **kwargs):
            if payload["teaching"].get("phase") == "applied":
                raise OSError("injected log interruption")
            return original(character, payload, **kwargs)
        self.teacher._store.record = fail_after_card
        with self.assertRaises(OSError):
            await self.teacher.ateach("demo", req)
        revision = self.core.persona_card_service.get("demo").revision
        self.core = build_memory_core(memory_root=self.root, model=self.model)
        self.teacher = MemoryService._build_persona_teaching(self.core, self.model)
        publish = self.teacher._publish
        self.teacher._publish = lambda *args: ""
        self.core.recover("demo")
        pending = await self.teacher.ateach("demo", req)
        self.assertEqual(pending.status, "pending")
        self.teacher._publish = publish
        await self.core.maintenance.run_once("demo")
        done = await self.teacher.ateach("demo", req)
        self.assertEqual(done.status, "duplicate")
        self.assertEqual(self.core.persona_card_service.get("demo").revision, revision)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.core.persona_publish.current("demo").version, done.published_version)
