"""FG04: effective capabilities and the final model system message."""
import asyncio

from tests.test_s04_service import ServiceTestCase, FakeModel
from rin_memory.contracts import CorrectionRequest, Scope


class AwarenessTests(ServiceTestCase):
    def test_modes_and_permissions_change_the_stable_awareness_version(self):
        service = self.start(clients={"reader": {"subjects": ["private"]},
                                     "writer": {"subjects": ["private"], "memory_tools": True, "persona_teach": True}})
        api = self.api(service)
        reader = asyncio.run(api.context({"client_id": "reader"}))["memory_awareness"]
        self.assertFalse(reader["capabilities"]["correction"])
        self.assertFalse(reader["capabilities"]["teaching"])
        self.assertNotIn("更新记录", reader["text"])
        self.assertEqual(reader, asyncio.run(api.context({"client_id": "reader"}))["memory_awareness"])
        writer = asyncio.run(api.context({"client_id": "writer"}))["memory_awareness"]
        self.assertTrue(writer["capabilities"]["correction"])
        self.assertTrue(writer["capabilities"]["teaching"])
        versions = {writer["version"]}
        for mode, expected in (("propose", "审核并发布"), ("auto", "满足采用条件"), ("off", "沿用已发布版本")):
            service.core.persona_admin_service.set_mode("demo", mode)
            current = asyncio.run(api.context({"client_id": "writer"}))["memory_awareness"]
            self.assertIn(expected, current["text"])
            self.assertEqual(current["capabilities"]["evolution_mode"], mode)
            versions.add(current["version"])
        self.assertEqual(len(versions), 3)

    def test_web_generation_adopts_current_correction_and_one_awareness_section(self):
        service = self.start()
        model = FakeModel("主人，我记得更新后的地点了。")
        api = self.api(service, model)
        service.core.facts.save("demo", [{"id": "city", "text": "主人住在东京", "source_event_keys": ["host:demo:private:city"]}])
        receipt = asyncio.run(service.core.correction_service.correct(CorrectionRequest(
            scope=Scope("demo"), action="replace", old_text="主人住在东京", new_text="主人现在住在大阪")))
        self.assertTrue(receipt.applied)
        for number, mode in enumerate(("off", "auto")):
            service.core.persona_admin_service.set_mode("demo", mode)
            awareness = asyncio.run(api.context({"client_id": "console"}))["memory_awareness"]
            result = asyncio.run(api.chat({"request_id": f"awareness-{number}", "turn_id": f"awareness-turn-{number}",
                "source_key": f"host:demo:private:awareness-{number}", "client_id": "console",
                "messages": [{"role": "user", "content": "我现在住在哪里？"}]}))
            self.assertEqual(result["status"], "committed")
            system = model.calls[-1]["system"]
            self.assertEqual(system.count(awareness["text"]), 1)
            self.assertIn("主人现在住在大阪", system)
            if mode == "auto":
                self.assertNotIn("当前人格沿用已发布版本", system)
