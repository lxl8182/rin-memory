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

from test_b04_extraction import extraction_reply

from rin_memory.adapters.model_http import ModelCallError
from rin_memory.contracts import CorrectionRequest, DeletionRequest, DeletionTarget, Scene, Scope
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
            return extraction_reply([{"text": "用户喜欢乌龙茶", "importance": 7, "entity": "master", "sources": [1]}],
                                    messages[0]["content"])
        if purpose == "memory_recall_selection":
            data = json.loads(messages[-1]["content"])
            return json.dumps({"selected": [{"key": item["key"], "use": "偏好"} for item in data["candidates"][:3]]})
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

    def start(self, *, memory_enabled=True, context_budget_ms=9000):
        self.service = MemoryService.start(ServiceSettings(
            memory_root=self.root, character="test", require_published_persona=True,
            memory_enabled=memory_enabled,
            context_budget_ms=context_budget_ms,
            clients={"plugin": {"token_env": "TEST_PLUGIN_TOKEN", "subjects": ["private"], "plugin_id": "game_test"}},
        ))
        self.service.model = self.model
        for name in ("extraction", "compression", "evolution_service", "reflection_service", "recall"):
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

    def test_qq_history_accumulates_only_confirmed_bubbles(self):
        self.initialize()
        scope = {"scene": "plugin:game_test", "session_id": "old"}
        plugin = {"client": "plugin", "token": "test-plugin"}
        first, last = "第一条：潮桥路线。", "第二条：银杉站。"
        turn = self.body("qq-reply", **scope, source_key="plugin:game_test:test:private:old:qq-reply",
                         reply=first + last, reply_transport="qq")
        self.ok("/v1/turns", turn, **plugin)

        def recent():
            context = self.ok("/v1/context", scope, **plugin)
            return [row["data"]["content"] for row in context["recent_turns"] if row["type"] == "ai"]

        def accept(index, text):
            body = {**scope, "request_id": "qq-reply", "receipt_id": f"qq-{index}", "kind": "sent",
                    "transport": "qq_napcat", "parts": [{"index": index, "chars": len(text), "text": text}]}
            self.assertNotIn("text", self.ok("/v1/receipts", body, **plugin)["parts"][0])

        def historical(text):
            return asyncio.run(self.service.core.recall.recall_memory(
                "test", text, scene=Scene(plugin_id="game_test"),
            ))["items"]

        self.assertEqual(recent(), [])
        self.assertEqual(historical("银杉站"), [])
        accept(0, first)
        self.assertEqual(recent(), [first])
        self.assertEqual(historical("银杉站"), [])
        accept(1, last)
        accept(1, last)
        self.assertEqual(recent(), [first + last])
        self.assertIn(last, historical("银杉站")[0]["text"])
        self.assertEqual(self.ok("/v1/turns", turn, **plugin)["reply"], first + last)

    def test_public_qq_persona_is_separate_and_console_editable(self):
        self.initialize()
        plugin = {"client": "plugin", "token": "test-plugin"}
        self.assertEqual(self.ok("/v1/qq/persona", **plugin), {"text": "", "revision": 0})
        edit = {"text": "我是铃芽，和朋友用短句聊天。", "base_revision": 0}
        self.assertEqual(self.request("/v1/qq/persona", edit, **plugin)[0], 403)
        self.assertEqual(self.ok("/v1/qq/persona", edit)["revision"], 1)
        self.assertEqual(self.request("/v1/qq/persona", edit)[0], 409)
        self.assertEqual(self.ok("/v1/qq/persona", **plugin)["text"], edit["text"])
        self.assertNotIn(edit["text"], self.ok("/v1/context", {})["persona"])

    def test_friend_short_followup_at_one_second_reuses_only_its_own_exchange(self):
        self.close()
        self.start(context_budget_ms=1000)
        self.initialize()
        self.service.settings.clients["plugin"]["subjects"].extend(("qq-friend:42", "qq-friend:43"))
        plugin = {"client": "plugin", "token": "test-plugin"}
        friend = {"subject": "qq-friend:42", "scene": "plugin:game_test", "session_id": "old"}
        source = "plugin:game_test:test:qq-friend_42:old:tent"
        old_exchange = {"source_key": source, "user": "露营要带帐篷", "reply": "帐篷我已经列进清单了。"}
        self.ok("/v1/turns", self.body("tent", **friend, source_key=source,
            messages=[{"role": "user", "content": old_exchange["user"]}], reply=old_exchange["reply"]), **plugin)
        history = {"mode": "topic", "exchanges": [old_exchange]}

        def sources(text, subject="qq-friend:42"):
            context = self.ok("/v1/context", dict(friend, subject=subject, session_id="new", text=text,
                segment_history=history), **plugin)
            return context["context"].get("segment_sources", [])

        for text in ("早安", "我们聊聊新游戏", "上次那个呢？"):
            self.assertEqual(sources(text), [])
        self.assertEqual(sources("帐篷呢？"), [source])
        self.assertEqual(sources("帐篷呢？", "qq-friend:43"), [])
        self.assertEqual(self.model.calls, [])

    def test_qq_segment_replay_keeps_the_topic_and_honors_the_source_fence(self):
        self.initialize()
        plugin = {"client": "plugin", "token": "test-plugin"}
        old = {"session_id": "old", "scene": "plugin:game_test"}
        new = {**old, "session_id": "new"}
        rows = []
        for i in range(16):
            key = f"plugin:game_test:test:private:old:exchange-{i}"
            user = f"海边露营装备清单第{i}项" if i < 14 else f"番茄炒蛋第{i}步"
            reply = f"海边露营装备清单已记第{i}项。" if i < 14 else "先切番茄。"
            if i == 15:
                user = "讲一下 Go 运行时"
                reply = "这里是前文。" * 100 + "最后聊到 GMP 调度。"
            receipt = self.ok("/v1/context", old, **plugin)["context_receipt_id"]
            self.ok("/v1/turns", self.body(f"exchange-{i}", **old,
                source_key=key, context_receipt_id=receipt,
                messages=[{"role": "user", "content": user}], reply=reply), **plugin)
            rows.append({"source_key": key, "user": user, "reply": reply})
        forged = dict(rows[0], source_key="plugin:game_test:test:private:old:never-saved")

        calls = []

        async def select(tier, messages, **kwargs):
            self.assertEqual(kwargs["purpose"], "memory_recall_selection")
            material = json.loads(messages[-1]["content"])
            calls.append(material)
            return json.dumps({"selected": [], "continuation": [
                row["key"] for row in material.get("previous", [])
                if "露营" in material["current"] and "露营" in row["user"]
            ]})

        self.service.core.recall._model.invoke = select

        def replay(text, mode="topic", **scope):
            request = {**new, **scope, "text": text,
                       "segment_history": {"mode": mode, "exchanges": [*rows, forged]}}
            return self.ok("/v1/context", request, **plugin)

        self.assertFalse(replay("早安呀")["context"].get("segment_sources"))
        self.assertFalse(calls)
        self.assertFalse(replay("帮我查明天的班车")["context"].get("segment_sources"))
        self.assertEqual(len(calls), 1)
        same_topic = replay("继续聊海边露营装备清单")["context"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(calls[-1]["previous"]), 6)
        self.assertEqual(same_topic["segment_sources"], [row["source_key"] for row in rows[:14]])
        self.assertFalse(same_topic["segment_detail"])
        self.assertTrue(replay("昨天露营装备清单具体有哪些？")["context"]["segment_detail"])

        async def timeout(*args, **kwargs):
            raise TimeoutError()

        self.service.core.recall._model.invoke = timeout
        self.assertEqual(replay("昨天露营装备清单具体有哪些？")["context"]["segment_sources"],
                         [row["source_key"] for row in rows[:14]])
        self.assertEqual(replay("GMP 调度呢？")["context"]["segment_sources"], [rows[15]["source_key"]])
        self.assertFalse(replay("上次那个呢？")["context"].get("segment_sources"))

        all_sources = [row["source_key"] for row in rows]
        warm = replay("接着来", mode="budget")
        self.assertEqual(warm["context"]["segment_sources"], all_sources)
        self.assertFalse(replay("接着来", mode="budget", scene="plugin:another")["context"].get("segment_sources"))
        self.ok("/v1/context/reserve", dict(new, request_id="next",
            context_receipt_id=warm["context_receipt_id"], consumer_refs={
                "selection_version": 1, "memory_ids": [], "source_ids": [all_sources[0], forged["source_key"]],
                "plugin_id": "game_test",
            }), **plugin)
        refs = self.service.core.consumer_refs.refs("test", plugin_id="game_test")
        self.assertEqual(refs[-1]["source_ids"], all_sources[:1])
        asyncio.run(self.service.core.correction_service.correct(CorrectionRequest(
            scope=Scope("test", "private"), target_source_keys=(all_sources[1],),
            old_text=rows[1]["user"], new_text="露营装备清单第 1 项已更换。",
        )))
        self.ok("/v1/cleanup/delete", {"source_key": all_sources[0], "request_id": "drop-exchange"})
        self.assertEqual(replay("接着来", mode="budget")["context"]["segment_sources"], all_sources[2:])

    def test_memory_pause_keeps_persona_chat_and_data_without_extracting_or_recalling(self):
        self.initialize()
        fact = {"id": "keep-fact", "text": "用户喜欢乌龙茶", "subject": "private",
            "source_event_keys": ["host:test:private:old"], "importance": 8}
        self.service.core.facts.save("test", [fact])
        self.ok("/v1/turns", self.body("queued-before-pause", reply="好。"))
        pending = len(self.service.core.outbox.pending("test"))
        self.assertGreater(pending, 0)
        self.close()
        self.start(memory_enabled=False)
        core = self.service.core
        context = self.ok("/v1/context", {"text": "乌龙茶", "session_id": "session"})
        self.assertFalse(context["memory_enabled"])
        self.assertTrue(context["persona"])
        self.assertTrue(context["persona_version"])
        self.assertEqual(context["context"]["reason"], "memory_paused")
        self.assertEqual(context["context"]["memory_ids"], [])
        self.assertIsNone(context["working_summary"])
        self.assertEqual(context["recent_turns"], [])
        self.assertEqual(context["corrections"]["block"], "")
        self.ok("/v1/turns", self.body("during-pause", context_receipt_id=context["context_receipt_id"], reply="正常聊天。"))
        calls = len(self.model.calls)
        self.assertEqual(asyncio.run(core.extraction.process_outbox("test")), ())
        self.assertEqual(len(self.model.calls), calls)
        self.assertEqual(len(core.outbox.pending("test")), pending)
        self.assertEqual(core.facts.load("test"), [fact])
        self.assertEqual(asyncio.run(core.recall.recall_memory("test", "乌龙茶"))["items"], [])
        self.service.settings.clients["plugin"]["memory_tools"] = True
        entries = self.ok("/v1/memory/entries?subject=private", client="plugin", token="test-plugin")
        self.assertEqual(entries["items"], [])
        self.assertEqual(entries["reason"], "memory_paused")
        asyncio.run(self.service.start_background())
        self.assertIsNone(self.service._maintenance_task)
        self.close()
        self.start()
        self.assertTrue(self.ok("/v1/context", {"text": "乌龙茶"})["memory_enabled"])
        self.assertEqual(self.service.core.facts.load("test"), [fact])

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
            if kwargs["purpose"] == "memory_recall_selection":
                offered = json.loads(messages[-1]["content"])["candidates"]
                return json.dumps({"selected": [{"key": item["key"], "use": "茶饮偏好"} for item in offered[:3]]})
            return extraction_reply({"facts": [{"text": "用户喜欢红茶", "importance": 8, "sources": [1]}],
                "state_updates": [{"kind": "thread_open", "utterance_indices": [99]}],
                "signals": [{"topic": "红茶", "utterance_indices": [1]}]}, messages[0]["content"])
        core = self.service.core
        core.extraction._model.invoke = extract
        core.extraction._settings = replace(core.extraction._settings, batch_max_turns=1, batch_idle_seconds=0)
        self.ok("/v1/context/reserve", dict(friend, request_id="friend-one",
            context_receipt_id=context["context_receipt_id"]), **plugin)
        body = self.body("friend-one", **friend,
            source_key="plugin:game_test:test:qq-friend_42:qq-friend-old:one",
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
        self.assertIn("新事项仅收录这位朋友明确愿意以后继续的话题", calls[0])
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
        served = self.ok("/v1/context", dict(new, text="红茶"), **plugin)
        self.assertEqual(served["context"]["memory_ids"], [fact_id])
        history = {"mode": "budget", "exchanges": [
            {"source_key": body["source_key"], "user": "我喜欢红茶", "reply": "记住啦。"},
        ]}
        for subject, expected in (("qq-friend:42", [body["source_key"]]), ("qq-friend:43", []), ("private", [])):
            if subject == "qq-friend:43":
                self.service.settings.clients["plugin"]["subjects"].append(subject)
            carried = self.ok("/v1/context", dict(new, subject=subject, text="那红茶呢", segment_history=history), **plugin)
            self.assertEqual(carried["context"].get("segment_sources", []), expected)
            self.assertEqual(bool(carried["context"]["query_meta"]["used_turns"]), bool(expected))
        for index, (ids, sources) in enumerate((
            ([], []), ([fact_id, "unknown"], facts[0]["source_event_keys"]),
        )):
            self.ok("/v1/context/reserve", dict(new, request_id=f"ref-{index}",
                context_receipt_id=served["context_receipt_id"], consumer_refs={
                    "selection_version": 1, "memory_ids": ids, "source_ids": sources,
                    "plugin_id": "game_test",
                }), **plugin)
        first, second = core.consumer_refs.refs("test", plugin_id="game_test")
        self.assertEqual(first["memory_ids"], [])
        self.assertEqual(first["source_ids"], [])
        self.assertEqual(first["phase"], "reserved")
        self.assertEqual(second["memory_ids"], [fact_id])
        self.assertEqual(second["source_ids"], facts[0]["source_event_keys"])
        self.ok("/v1/turns", self.body("ref-1", **new,
            source_key="plugin:game_test:test:qq-friend_42:qq-friend-new:ref-1",
            context_receipt_id=served["context_receipt_id"], memory_selection_adopted=True,
            messages=[{"role": "user", "content": "谢谢"}], reply="不客气。"), **plugin)
        adopted = core.consumer_refs.refs("test", plugin_id="game_test")
        self.assertEqual({row["request_id"]: row["phase"] for row in adopted},
                         {"ref-0": "reserved", "ref-1": "adopted"})
        selector_scope = {**new, "client_id": "plugin"}
        self.assertEqual(self.api.protocol.recent_for_selection(selector_scope),
                         [{"user": "谢谢", "assistant": "不客气。"}])
        self.assertFalse(self.api.protocol.recent_for_selection({**selector_scope, "session_id": "missing"}))
        # Deletion uses the friend's subject; the friend keeps a zero-tool session.
        asyncio.run(core.deletion_service.forget(DeletionRequest(
            scope=Scope("test", "qq-friend:42"), targets=(DeletionTarget("fact", fact_id),),
            reason="user_request",
        )))
        self.assertEqual(asyncio.run(recalled("qq-friend:42")), set())
        self.close()
        self.start()
        self.service.settings.clients["plugin"]["subjects"].append("qq-friend:42")
        after = self.ok("/v1/context", dict(new, text="红茶", segment_history=history), **plugin)
        self.assertNotIn(fact_id, after["context"].get("memory_ids") or [])
        code, error = self.request("/v1/turns", body, **plugin)
        self.assertEqual(code, 410)
        self.assertEqual(error["code"], "deleted")
        asyncio.run(self.service.core.deletion_service.forget(DeletionRequest(
            scope=Scope("test", "qq-friend:42"), source_keys=(body["source_key"],), reason="user_request",
        )))
        after_source_delete = self.ok("/v1/context", dict(new, text="红茶", segment_history=history), **plugin)
        self.assertFalse(after_source_delete["context"].get("segment_sources"))

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

    def test_console_deletion_reports_the_barrier_before_cleanup_and_tracks_plugin_receipts(self):
        core = self.service.core
        core.facts.save("test", [{"id": "async-fact", "text": "等待接入端的临时事实"}])
        self.assertTrue(core.consumer_refs.record("test", {
            "plugin_id": "game_test", "session_id": "chat-session",
            "request_id": "used-fact", "memory_ids": ["async-fact"],
        }))
        release = threading.Event()
        finished = threading.Event()
        run_steps = core.deletion_service._run_steps

        def wait_for_cleanup(*args, **kwargs):
            try:
                if not release.wait(5):
                    raise TimeoutError("test did not release cleanup")
                return run_steps(*args, **kwargs)
            finally:
                finished.set()

        with patch.object(core.deletion_service, "_run_steps", side_effect=wait_for_cleanup):
            try:
                code, receipt = self.request("/v1/cleanup/delete", {
                    "type": "fact", "id": "async-fact", "request_id": "async-delete", "defer_cleanup": True,
                })
                self.assertEqual(code, 202)
                self.assertTrue(receipt["stopped_using"])
                self.assertFalse(receipt["content_cleaned"])
                deletion_id = receipt["deletion_id"]
                rows = self.ok("/v1/cleanup?receipts_only=1")
                self.assertEqual(set(rows), {"deletions"})
                self.assertFalse(rows["deletions"][0]["content_cleaned"])
                self.assertFalse(self.ok("/v1/cleanup/pending", client="plugin", token="test-plugin")["items"])
            finally:
                release.set()
            self.assertTrue(finished.wait(5))

        path = "/v1/cleanup/" + deletion_id + "/ack"
        rows = self.ok("/v1/cleanup?receipts_only=1")["deletions"]
        self.assertTrue(rows[0]["content_cleaned"])
        self.assertEqual(rows[0]["status"], "pending")
        pending = self.ok("/v1/cleanup/pending", client="plugin", token="test-plugin")["items"]
        self.assertEqual(pending[0]["consumer_sessions"], ["chat-session"])
        self.assertEqual(pending[0]["source_keys"], [])
        failed = self.ok(path, {"status": "failed", "failed": [{"reason": "session lock"}]},
                         client="plugin", token="test-plugin")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["plugin_receipts"]["game_test"]["failed"], ["session lock"])
        retried = self.ok("/v1/cleanup/retry", {"deletion_id": deletion_id})
        self.assertEqual(retried["status"], "failed")
        self.assertEqual(self.ok(path, {"status": "cleaned"}, client="plugin", token="test-plugin")["status"], "completed")

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
