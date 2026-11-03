"""Non-host service acceptance over real HTTP and isolated roots."""
import asyncio
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from rin_memory.adapters.model_http import ModelCallError
from rin_memory.contracts import DeletionRequest, DeletionTarget, Scene, Scope
from rin_memory.service.management import ManagementApi
from rin_memory.service.memory_service import MemoryService, ServiceSettings
from rin_memory.service.server import MemoryApi, build_server


class Model:
    def __init__(self):
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()
        self.started = threading.Event()
        self.fail = False

    def available(self, tier):
        return True

    async def invoke(self, tier, messages, **kwargs):
        purpose = kwargs["purpose"]
        self.calls.append((purpose, messages))
        if purpose == "memory_fact_extraction":
            return json.dumps([{"text": "用户喜欢乌龙茶", "importance": 7, "entity": "master"}], ensure_ascii=False)
        if purpose == "persona_evolution_proposal":
            count = sum(call[0] == purpose for call in self.calls)
            return json.dumps({"operations": [{"op": "replace", "section_id": "tone", "text": "先给结论，再给具体理由。" + "嗯。" * count}],
                               "reason": "用户明确偏好具体表达"}, ensure_ascii=False)
        if purpose != "chat_reply":
            return "[]"
        self.started.set()
        await asyncio.to_thread(self.gate.wait, 5)
        if self.fail:
            self.fail = False
            raise ModelCallError("test timeout", kind="timeout", retryable=True)
        return "记得你喜欢乌龙茶。"


class ServiceCloseoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "memory"
        self.root.mkdir()
        self.model = Model()
        self.env = patch.dict(os.environ, {"TEST_PLUGIN_TOKEN": "test-plugin"})
        self.env.start()
        self.start()

    def start(self):
        self.service = MemoryService.start(ServiceSettings(
            memory_root=self.root, character="test", require_published_persona=True,
            clients={"plugin": {"token_env": "TEST_PLUGIN_TOKEN", "subjects": ["private"], "plugin_id": "game_test"}},
        ))
        self.service.model = self.model
        for name in ("extraction", "compression", "evolution_service", "reflection_service"):
            getattr(self.service.core, name)._model = self.model
        self.management = ManagementApi(self.service)
        self.api = MemoryApi(self.service)
        self.server = build_server(self.api, port=0, management=self.management)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.token = self.management.token

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        asyncio.run(self.service.stop())

    def tearDown(self):
        self.close()
        self.env.stop()
        self.tmp.cleanup()

    def request(self, path, body=None, *, client="console", token=None, headers=None):
        headers = {"Content-Type": "application/json", "X-Companion-Client": client,
                   "X-Companion-Token": self.token if token is None else token,
                   **(headers or {})}
        req = urllib.request.Request(self.url + path, headers=headers,
            data=None if body is None else json.dumps(body, ensure_ascii=False).encode())
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=8) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def ok(self, path, body=None, **kwargs):
        code, data = self.request(path, body, **kwargs)
        self.assertEqual(code, 200, (path, data))
        return data

    def initialize(self):
        self.ok("/v1/characters", {"display_name": "测试伙伴", "text": "你是温柔简洁的伙伴。"})
        self.ok("/v1/persona/publish", {"revision": 1})
        self.service._refresh_chat_capability()

    def body(self, request_id="one", **kwargs):
        return {"request_id": request_id, "turn_id": "turn-" + request_id,
                "source_key": "host:test:private:" + request_id,
                "messages": [{"role": "user", "content": "我喜欢乌龙茶"}],
                "session_id": "session", **kwargs}

    def test_friend_memory_is_scoped_factual_and_stays_deleted_after_restart(self):
        self.initialize()
        friend = {"subject": "qq-friend:42", "session_id": "qq-friend-old", "scene": "plugin:game_test"}
        plugin = {"client": "plugin", "token": "test-plugin"}
        self.assertEqual(self.request("/v1/context", friend, **plugin)[0], 403)
        self.service.settings.clients["plugin"]["subjects"].append("qq-friend:42")
        self.assertEqual(self.request("/v1/context", dict(friend, subject="qq-friend:43"), **plugin)[0], 403)
        context = self.ok("/v1/context", dict(friend, text="红茶"), **plugin)
        self.assertEqual(context["persona"], "")
        self.assertEqual(context["persona_version"], "")
        self.assertEqual(context["memory_awareness"], {})
        self.assertEqual(context["projection"], {})
        self.assertTrue(self.ok("/v1/context", {"text": "红茶"})["persona"])

        calls = []
        async def extract(tier, messages, **kwargs):
            calls.append(messages[0]["content"])
            return json.dumps({"facts": [{"text": "用户喜欢红茶", "importance": 8,
                "utterance_indices": [1]}], "state_updates": [{"kind": "thread_open", "utterance_indices": [99]}],
                "signals": [{"topic": "红茶", "utterance_indices": [1]}]}, ensure_ascii=False)
        core = self.service.core
        core.extraction._model.invoke = extract
        core.extraction._settings = replace(core.extraction._settings, batch_max_turns=1, batch_idle_seconds=0)
        self.ok("/v1/context/reserve", dict(friend, request_id="friend-one",
            context_receipt_id=context["context_receipt_id"]), **plugin)
        body = self.body("friend-one", **friend,
            source_key="plugin:game_test:test:qq-friend_42:old:one",
            context_receipt_id=context["context_receipt_id"],
            messages=[{"role": "user", "content": "我喜欢红茶"}], reply="记住啦。")
        self.ok("/v1/turns", body, **plugin)
        self.ok("/v1/receipts", dict(friend, request_id="friend-one", receipt_id="friend-used", kind="used"), **plugin)
        outcomes = asyncio.run(core.extraction.process_outbox("test"))
        self.assertTrue(outcomes)
        self.assertTrue(all(row.status == "completed" for row in outcomes), outcomes)
        facts = core.facts.load("test")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["subject"], "qq-friend:42")
        self.assertNotIn("state_updates", calls[0])
        self.assertNotIn("兴趣信号", calls[0])
        fact_id = facts[0]["id"]

        async def recalled(subject):
            result = await core.recall.recall_memory("test", "用户喜欢红茶", subject=subject,
                scene=Scene(plugin_id="game_test"))
            return {row["id"] for row in result.get("items") or ()}
        self.assertIn(fact_id, asyncio.run(recalled("qq-friend:42")))
        self.assertNotIn(fact_id, asyncio.run(recalled("qq-friend:43")))
        self.assertNotIn(fact_id, asyncio.run(recalled("private")))
        before = len(calls)
        asyncio.run(core.reflection_service.synthesize("test"))
        self.assertEqual(len(calls), before, "friend facts never enter global reflection")
        self.assertEqual(core.reflections.load("test"), [])
        new = dict(friend, session_id="qq-friend-new")
        self.ok("/v1/context", dict(new, text="红茶"), **plugin)
        # Deletion uses the friend's subject; the friend keeps a zero-tool session.
        asyncio.run(core.deletion_service.forget(DeletionRequest(
            scope=Scope("test", "qq-friend:42"), targets=(DeletionTarget("fact", fact_id),),
            reason="user_request",
        )))
        self.assertEqual(asyncio.run(recalled("qq-friend:42")), set())
        self.close()
        self.start()
        self.service.settings.clients["plugin"]["subjects"].append("qq-friend:42")
        after = self.ok("/v1/context", dict(new, text="红茶"), **plugin)
        self.assertNotIn(fact_id, after["context"].get("memory_ids") or [])
        code, error = self.request("/v1/turns", body, **plugin)
        self.assertEqual(code, 410)
        self.assertEqual(error["code"], "deleted")

    def test_tunnel_origin_can_delete_a_fact(self):
        self.service.core.facts.save("test", [{
            "id": "tunnel-fact", "text": "隧道删除验收事实", "subject": "private",
            "schema_version": 2, "source": "user_observation",
            "source_event_keys": ["host:test:private:tunnel-fact"],
        }])
        headers = {"Host": "127.0.0.1:14931", "Origin": "http://127.0.0.1:14931"}
        self.ok("/v1/bootstrap", headers=headers)
        receipt = self.ok("/v1/cleanup/delete", {
            "type": "fact", "id": "tunnel-fact", "request_id": "tunnel-delete",
        }, headers=headers)
        self.assertEqual(receipt["status"], "completed")
        self.assertFalse(any(row["id"] == "tunnel-fact"
                             for row in self.service.core.facts.load("test")))

    def test_cross_origin_and_untrusted_host_requests_stay_blocked(self):
        for host, origin in (
            ("127.0.0.1:14931", "http://evil.test"),
            ("127.0.0.1:14931", "http://127.0.0.1:3080"),
            ("127.0.0.1:14931", self.url),
            ("127.0.0.1:14931", "http://localhost:14931"),
            ("127.0.0.1:14931", "https://127.0.0.1:14931"),
            ("127.0.0.1:14931", "null"),
            ("127.0.0.1:14931", "http://127.0.0.1:bad"),
            ("evil.test:14931", "http://evil.test:14931"),
        ):
            with self.subTest(host=host, origin=origin):
                code, error = self.request("/v1/cleanup/delete", {
                    "type": "fact", "id": "any-fact",
                }, headers={"Host": host, "Origin": origin})
                self.assertEqual(code, 403, error)
                self.assertEqual(error["code"], "forbidden")

    def test_loopback_authorities_and_explicit_origin_remain_supported(self):
        for host, origin in (
            ("127.0.0.1", "http://127.0.0.1"),
            ("localhost:14931", "http://localhost:14931"),
            ("[::1]:14931", "http://[::1]:14931"),
        ):
            with self.subTest(host=host):
                self.ok("/v1/bootstrap", headers={"Host": host, "Origin": origin})
        self.server.allow_origin = "https://console.example"
        self.ok("/v1/bootstrap", headers={"Origin": self.server.allow_origin})

    def test_context_event_delivery_cleanup_and_restart(self):
        self.initialize()
        ctx = self.ok("/v1/context", {"text": "乌龙茶", "session_id": "session"})
        self.assertEqual(self.service.core.turn_requests.count("test"), 0)
        self.assertFalse(self.ok("/v1/receipts?session_id=session")["items"])
        self.assertEqual(self.request("/v1/context", {"subject": "group:foreign"})[0], 403)
        self.ok("/v1/context/validate", {"session_id": "session", "context_receipt_id": ctx["context_receipt_id"]})
        turn = self.ok("/v1/turns", self.body(context_receipt_id=ctx["context_receipt_id"], reply="好的"))
        self.assertTrue(turn["durable"])
        self.assertEqual(self.request("/v1/requests/one")[0], 404)
        self.ok("/v1/requests/one?session_id=session")
        receipt = {"request_id": "one", "receipt_id": "used-one", "session_id": "session", "kind": "used"}
        self.ok("/v1/receipts", receipt)
        self.assertTrue(self.ok("/v1/receipts", receipt)["duplicate"])
        self.ok("/v1/receipts", dict(receipt, receipt_id="shown-one", kind="delivered", delivered_chars=2))
        self.assertEqual(self.request("/v1/receipts", dict(receipt, kind="failed"))[0], 409)
        stale = self.ok("/v1/context", {"text": "乌龙茶", "session_id": "session"})
        correction = self.ok("/v1/cleanup/correct", {"old_text": "喜欢乌龙茶", "new_text": "改成喜欢红茶"})
        code, error = self.request("/v1/turns", self.body("stale", reply="旧内容", context_receipt_id=stale["context_receipt_id"]))
        self.assertEqual(code, 409, error)
        self.assertTrue(error["needs_reissue"])
        self.ok("/v1/cleanup/retract-correction", {"correction_id": correction["correction_id"]})
        event = {"source_key": "plugin:game_test:test:private:run:event", "event_id": "event", "plugin_id": "game_test",
                 "event_type": "scene_experience", "text": "在游戏里偏好保守战术", "scene": "game:example",
                 "occurred_at": datetime.now(timezone.utc).isoformat(), "content_class": "shared_experience"}
        self.ok("/v1/events", event, client="plugin", token="test-plugin")
        self.service.settings = replace(self.service.settings, max_pending_events=1)
        self.assertEqual(self.request("/v1/events", dict(event, source_key=event["source_key"] + "-new"), client="plugin", token="test-plugin")[0], 429)
        self.assertEqual(self.ok("/v1/events", event, client="plugin", token="test-plugin")["status"], "duplicate")
        self.ok("/v1/maintenance", {})
        self.assertTrue(any(row.get("applies_to", {}).get("game_id") == "example" for row in self.service.core.facts.load("test")))
        deletion = self.ok("/v1/cleanup/delete", {"source_key": event["source_key"], "request_id": "delete-event"})
        self.assertEqual(len(self.service.core.recent.messages("test")), 2)
        index = self.service.core.extraction._timeindex_factory("test")
        try:
            index.open()
            self.assertEqual(index.count_original(), 2)
        finally:
            index.close()
        self.ok("/v1/chat", self.body("after-delete"))
        pending = self.ok("/v1/cleanup/pending", client="plugin", token="test-plugin")
        self.assertTrue(pending["items"])
        self.assertEqual(self.request("/v1/cleanup/" + deletion["deletion_id"] + "/ack",
            {"plugin_id": "foreign", "status": "completed"}, client="plugin", token="test-plugin")[0], 403)
        done = self.ok("/v1/cleanup/" + deletion["deletion_id"] + "/ack",
            {"status": "completed", "cleaned": [event["source_key"]]}, client="plugin", token="test-plugin")
        self.assertEqual(done["status"], "completed")
        self.ok("/v1/cleanup/retry", {"deletion_id": deletion["deletion_id"], "force": True})
        self.assertEqual(self.request("/v1/events", event, client="plugin", token="test-plugin")[0], 410)
        fresh = self.ok("/v1/context", {"session_id": "session"})
        self.close()
        self.start()
        self.assertEqual(self.request("/v1/turns", self.body("after-restart", reply="旧内容", context_receipt_id=fresh["context_receipt_id"]))[0], 409)
        self.assertTrue(self.ok("/v1/receipts?session_id=session")["items"])

    def test_generation_boundary_session_capacity_and_transient_failure(self):
        self.initialize()
        first_version = self.service.core.persona_publish.current("test").version
        self.model.gate.clear()
        result = {}
        worker = threading.Thread(target=lambda: result.update(value=self.request("/v1/chat", self.body())))
        worker.start()
        self.assertTrue(self.model.started.wait(3))
        self.assertEqual(self.request("/v1/chat", self.body("competing"))[0], 202)
        self.ok("/v1/persona/revise", {"base_revision": 1, "operations": [{"op": "replace", "section_id": "identity", "text": "你是新版伙伴。"}]})
        new_version = self.ok("/v1/persona/publish", {"revision": 2})["version"]
        self.model.gate.set()
        worker.join(5)
        self.assertEqual(result["value"][0], 200, result)
        self.assertEqual(result["value"][1]["persona_version"], first_version)
        self.assertEqual(self.ok("/v1/chat", self.body("second"))["persona_version"], new_version)
        self.model.fail = True
        self.assertEqual(self.request("/v1/chat", self.body("retry"))[0], 504)
        self.ok("/v1/chat", self.body("retry"))
        self.service.state.chat_ready = False
        replay = self.ok("/v1/chat", self.body("retry"))
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.request("/v1/chat", self.body("retry", messages=[{"role":"user","content":"变更正文"}]))[0], 409)

    def test_persona_management_and_scoped_extraction(self):
        self.initialize()
        self.ok("/v1/persona/revise", {"base_revision": 1, "operations": [{"op": "add", "section_id": "tone", "text": "说话温柔。", "editable_by": {"user": True, "evolution": True}}]})
        self.ok("/v1/persona/publish", {"revision": 2})
        self.ok("/v1/chat", self.body())
        self.ok("/v1/maintenance", {})
        rows = self.service.core.facts.load("test")
        self.assertTrue(rows)
        self.assertTrue(all(row.get("subject") == "private" for row in rows))
        for number, scene in enumerate(("game:example", "run:trial")):
            self.ok("/v1/turns", self.body("scoped-" + str(number), scene=scene, reply="收到"))
        self.ok("/v1/maintenance", {})
        rows = self.service.core.facts.load("test")
        self.assertEqual(len(rows), 3)
        unrestricted = self.ok("/v1/context", {"text": "用户喜欢乌龙茶"})["context"]["memory_ids"]
        self.assertTrue(unrestricted)
        scoped_ids = {row["id"] for row in rows if row.get("applies_to")}
        self.assertFalse(scoped_ids.intersection(unrestricted))
        run = self.ok("/v1/context", {"text": "用户喜欢乌龙茶", "scene": "run:trial"})["context"]["memory_ids"]
        self.assertFalse({row["id"] for row in rows if row.get("applies_to", {}).get("game_id")}.intersection(run))
        self.assertEqual(self.ok("/v1/evolution/evaluate", {})["reason"], "off")
        for mode in ("off", "propose", "auto"):
            status = self.ok("/v1/evolution/mode", {"mode": mode})
            self.assertEqual(status["mode"], mode)
        self.ok("/v1/evolution/mode", {"mode": "propose"})
        # M4 (design 9.3): a raw fact is explanation only and can never qualify a
        # card change on its own. Seed the mature understanding a real
        # conversation would have produced, resting on two independent
        # experiences, and let the fact ride along as the explanation source.
        from rin_memory.domain.growth_evidence import support_basis_from_facts
        understanding = {
            "id": "ref_understanding",
            "text": "用户喜欢温柔、具体的表达",
            "status": "confirmed",
            "entity": "relationship",
            "source_fact_ids": [rows[0]["id"]],
            "support_basis": support_basis_from_facts([
                {"id": rows[0]["id"], "text": "用户喜欢温柔", "episode_id": "ep:test:e1",
                 "source_event_keys": list(rows[0].get("source_event_keys") or ()),
                 "evidence_kind": "explicit_feedback"},
                {"id": "fact_second", "text": "用户又夸具体",
                 "episode_id": "ep:test:e2",
                 "source_event_keys": ["host:test:private:second"],
                 "evidence_kind": "observed_action"},
            ]),
            "created_at": "2026-10-20T00:00:00+00:00",
        }
        core_rows = self.service.core.reflections.load("test")
        self.service.core.reflections.save("test", core_rows + [understanding])
        proposal = self.ok("/v1/evolution/propose", {
            "base_revision": 2,
            "source_refs": ["ref_understanding", rows[0]["id"]],
            "focus_sections": ["tone"],
        })
        self.assertEqual(proposal["status"], "proposed", proposal)
        self.ok("/v1/evolution/" + proposal["proposal_id"] + "/accept", {"reason": "合成证据验收"})
        card = self.ok("/v1/persona")["card"]
        self.assertEqual(card["sections"][0]["text"], "名字: 测试伙伴\n你是温柔简洁的伙伴。")
        self.assertTrue(card["sections"][0]["frozen"])
        self.assertGreater(card["revision"], 2)
        self.ok("/v1/evolution/mode", {"mode": "auto"})
        auto = self.ok("/v1/evolution/evaluate", {})
        self.assertEqual(auto["status"], "published", auto)
        next_turn = self.ok("/v1/chat", self.body("after-auto"))
        self.assertEqual(next_turn["persona_version"], auto["published_version"])


if __name__ == "__main__":
    unittest.main()
