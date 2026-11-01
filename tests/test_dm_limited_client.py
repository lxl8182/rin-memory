"""Limited chat-client memory tools and transport receipts (DM03/DM04).

These pin the two service-side contracts the DSH memory plugin depends on:

* a chat client may list, correct and forget **its own subject's** facts, and
  nothing else — the capability is a separate grant, a group subject sees
  nothing of the private one, and an ambiguous "forget that" is answered with
  candidates instead of a guess;
* a QQ bridge can record that the platform accepted the text (`sent`) without
  claiming the user read it (`delivered`), and a rejected segment of a split
  reply is visible as its own part.
"""
import asyncio
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from rin_memory.service.management import ManagementApi
from rin_memory.service.memory_service import MemoryService, ServiceSettings
from rin_memory.service.server import MemoryApi, build_server


class FakeModel:
    """Deterministic extractor: one fact per remembered sentence."""

    def __init__(self):
        self.calls = []

    def is_available(self):
        return True

    @property
    def model_id(self):
        return "test-extractor"

    async def invoke(self, tier, messages, **kwargs):
        purpose = kwargs["purpose"]
        self.calls.append((purpose, messages))
        if purpose == "memory_fact_extraction":
            prompt = "\n".join(
                str(message.get("content") or "") for message in messages
                if isinstance(message, dict)
            )
            lines = [line.split("| user |", 1)[-1].split("| message_ref=", 1)[0].strip()
                     for line in prompt.splitlines() if "| user |" in line]
            text = (lines[-1] if lines else "").strip().rstrip("。")
            if not text:
                return "[]"
            return json.dumps([{"text": text, "importance": 7, "entity": "master"}],
                              ensure_ascii=False)
        return "[]"


class LimitedMemoryClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "memory").mkdir()
        self.env = patch.dict(os.environ, {"TEST_DSH_TOKEN": "t-dsh",
                                           "TEST_PLAIN_TOKEN": "t-plain"})
        self.env.start()
        self.settings = ServiceSettings(
            memory_root=self.root / "memory", character="test",
            clients={
                "dsh-desktop": {"token_env": "TEST_DSH_TOKEN", "subjects": ["private", "group-7"],
                                "plugin_id": "dsh_memory", "memory_tools": True},
                # Same token family, no grant: the capability is not implied by
                # being a known client.
                "dsh-plain": {"token_env": "TEST_PLAIN_TOKEN", "subjects": ["private"],
                              "plugin_id": "dsh_memory"},
            },
            require_published_persona=False,
        )
        self.service = MemoryService.start(self.settings)
        self.model = FakeModel()
        for name in ("extraction", "compression"):
            getattr(self.service.core, name)._model = self.model
        self.api = MemoryApi(self.service)
        self.management = ManagementApi(self.service)
        self.management.initialize({"display_name": "小潮", "text": "温柔而直白。"})
        self.service.core.persona_publish.publish("test")
        self.server = build_server(self.api, host="127.0.0.1", port=0, management=self.management)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        asyncio.run(self.service.core.maintenance.run_once("test"))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        asyncio.run(self.service.stop())
        self.tmp.cleanup()
        self.env.stop()

    # -- helpers ---------------------------------------------------------

    def call(self, method, path, body=None, *, client="dsh-desktop"):
        name = "TEST_DSH_TOKEN" if client == "dsh-desktop" else "TEST_PLAIN_TOKEN"
        headers = {"Content-Type": "application/json",
                   "X-Companion-Client": client,
                   "X-Companion-Token": os.environ.get(name, "")}
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def remember(self, text, *, subject="private", session="qq-session", scene=""):
        """Commit one turn so the extractor has a real fact to work with."""
        request_id = "req-" + str(abs(hash((text, subject, session))))
        body = {
            "request_id": request_id, "turn_id": request_id,
            "source_key": f"plugin:dsh_memory:test:{subject}:{session}:{request_id}",
            "subject": subject, "session_id": session, "scene": scene, "applies_to": "shared",
            "occurred_at": "2026-10-25T12:00:00+00:00",
            "messages": [{"role": "user", "content": text}],
            "reply": "好，记住了。",
        }
        status, payload = self.call("POST", "/v1/turns", body)
        self.assertEqual(status, 200, payload)
        return request_id

    def console_call(self, method, path, body=None):
        """The management console identity (the only one that may delete)."""
        headers = {"Content-Type": "application/json",
                   "X-Companion-Client": "console",
                   "X-Companion-Token": self.management.token}
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def entries(self, *, client="dsh-desktop", text="", subject="private"):
        query = f"?text={urllib.parse.quote(text)}&subject={subject}"
        return self.call("GET", "/v1/memory/entries" + query, client=client)

    # -- tests -----------------------------------------------------------

    def test_capability_is_required_and_entries_never_cross_subjects(self):
        status, payload = self.entries(client="dsh-plain")
        self.assertEqual(status, 403, payload)

        self.remember("只属于群七的事实。", subject="group-7", session="group-session")
        asyncio.run(self.service.core.maintenance.run_once("test"))
        status, listed = self.entries(subject="group-7")
        foreign = next(item for item in listed["items"] if "群七" in item.get("text", ""))
        self.settings.clients["dsh-desktop"]["subjects"] = ["private"]
        status, denied = self.call("POST", "/v1/memory/forget", {
            "subject": "private", "target": {"type": "fact", "id": foreign["id"]},
        })
        self.assertEqual(status, 403, denied)
        self.assertTrue(any(row.get("id") == foreign["id"] for row in self.service.core.facts.load("test")))
        for body in ({"source_key": "plugin:dsh_memory:test:group-7:s:e"},
                     {"source_key": "plugin:other:test:private:s:e"},
                     {"episode_id": "foreign-episode"}):
            status, denied = self.call("POST", "/v1/memory/forget", dict(body, subject="private"))
            self.assertEqual(status, 403, denied)
        self.assertEqual(payload["code"], "forbidden")

        status, payload = self.entries(client="dsh-desktop")
        self.assertEqual(status, 200, payload)

        status, payload = self.call("POST", "/v1/memory/correct", {
            "old_text": "红茶", "new_text": "乌龙茶",
        }, client="dsh-plain")
        self.assertEqual(status, 403, payload)

    def test_correct_from_a_conversation_updates_the_shared_fact_and_is_idempotent(self):
        with patch.dict(os.environ, {"TEST_DSH_TOKEN": "t-dsh", "TEST_PLAIN_TOKEN": "t-plain"}):
            self.remember("我最喜欢的饮料是红茶。", scene="plugin:dsh_memory")
            asyncio.run(self.service.core.maintenance.run_once("test"))
            shared = next(row for row in self.service.core.facts.load("test") if "红茶" in row["text"])
            self.assertEqual(shared["applies_to"], {}, "QQ origin must not restrict the personal fact to that plugin")

            status, corrected = self.call("POST", "/v1/memory/correct", {
                "old_text": "红茶", "new_text": "乌龙茶", "request_id": "corr-1",
            })
            self.assertEqual(status, 200, corrected)
            self.assertTrue(corrected["applied"], corrected)

            # Same request id replays instead of adding a second correction.
            status, again = self.call("POST", "/v1/memory/correct", {
                "old_text": "红茶", "new_text": "乌龙茶", "request_id": "corr-1",
            })
            self.assertEqual(status, 200, again)
            self.assertTrue(again["applied"])
            self.assertEqual(again["correction_id"], corrected["correction_id"])

            # The correction is what the next context block carries, so the
            # QQ turn and the web conversation read the same statement.
            context = asyncio.run(self.api.context({"text": "我喜欢喝什么？", "subject": "private"}))
            self.assertIn("乌龙茶", context["corrections"]["block"])

    def test_forget_deletes_only_the_named_row_and_reports_ambiguity(self):
        with patch.dict(os.environ, {"TEST_DSH_TOKEN": "t-dsh", "TEST_PLAIN_TOKEN": "t-plain"}):
            self.remember("我养了一只叫小满的猫。")
            asyncio.run(self.service.core.maintenance.run_once("test"))
            self.remember("我养了一只叫阿吉的狗。")
            asyncio.run(self.service.core.maintenance.run_once("test"))

            status, listed = self.entries(text="养了一只")
            self.assertEqual(status, 200, listed)
            self.assertGreaterEqual(len(listed["items"]), 2, listed)
            wanted = next(item for item in listed["items"]
                          if "小满" in str(item.get("text") or ""))
            survivor = next(item for item in listed["items"]
                            if "阿吉" in str(item.get("text") or ""))

            # A partial phrase matching several rows asks instead of guessing.
            status, ambiguous = self.call("POST", "/v1/memory/forget", {"text": "养了一只"})
            self.assertEqual(status, 409, ambiguous)
            self.assertEqual(ambiguous["code"], "ambiguous_target")
            self.assertGreaterEqual(len(ambiguous["candidates"]), 2)

            status, deleted = self.call("POST", "/v1/memory/forget", {
                "target": {"type": wanted["type"], "id": wanted["id"]},
            })
            self.assertEqual(status, 200, deleted)
            self.assertEqual(deleted["subject"], "private")
            status, pending = self.call("GET", "/v1/cleanup/pending?plugin_id=dsh_memory")
            self.assertEqual(status, 200, pending)
            self.assertTrue(any(wanted["id"] in row.get("memory_ids", []) for row in pending["items"]),
                            "entry deletion must notify the plugin that owns a copy")

            status, after = self.entries(text="养了一只")
            self.assertEqual(status, 200, after)
            ids = {item["id"] for item in after["items"]}
            self.assertNotIn(wanted["id"], ids)
            self.assertIn(survivor["id"], ids)

    def test_web_fact_consumed_via_reserve_is_cleaned_after_deletion(self):
        """QP05 over the real HTTP shape a plugin client uses.

        The fact is created on the web (no plugin segment in its source key).
        The plugin registers it as consumed through `/v1/context/reserve`, the
        console deletes it, and the plugin must then see the cleanup task.
        """
        with patch.dict(os.environ, {"TEST_DSH_TOKEN": "t-dsh"}):
            web_fact = {"id": "f_web", "subject": "private",
                        "text": "用户只喝乌龙茶", "source_event_keys": ["host:test:private:web-1"]}
            self.service.core.facts.save("test", [web_fact])
            scope = {"subject": "private", "session_id": "qq-session"}
            status, context = self.call("POST", "/v1/context", dict(scope, text="用户只喝乌龙茶"))
            self.assertEqual(status, 200, context)
            self.assertIn("f_web", context["context"]["memory_ids"])

            # A client that does not track consumption gets the old response
            # byte for byte: no new key, and no consumer row either.
            status, plain = self.call("POST", "/v1/context/reserve", dict(
                scope, request_id="turn-plain",
                context_receipt_id=context["context_receipt_id"],
            ))
            self.assertEqual(status, 200, plain)
            self.assertEqual(sorted(plain), ["durable", "request_id", "status"])
            self.assertEqual(self.service.core.consumer_refs.refs("test"), [])

            status, reserved = self.call("POST", "/v1/context/reserve", dict(
                scope, request_id="turn-web", plugin_id="dsh_memory",
                context_receipt_id=context["context_receipt_id"],
                consumer_refs={"session_id": "qq-session",
                               "memory_ids": ["f_web"], "context_generation": "v1:1"},
            ))
            self.assertEqual(status, 200, reserved)
            self.assertEqual(reserved["consumer_refs"], "recorded")
            self.assertEqual([row["memory_ids"] for row in
                              self.service.core.consumer_refs.refs("test")], [["f_web"]])

            status, deleted = self.console_call("POST", "/v1/cleanup/delete",
                                                {"type": "fact", "id": "f_web"})
            self.assertEqual(status, 200, deleted)
            status, pending = self.call("GET", "/v1/cleanup/pending?plugin_id=dsh_memory")
            self.assertEqual(status, 200, pending)
            self.assertIn(deleted["deletion_id"],
                          [row["deletion_id"] for row in pending["items"]])

            status, done = self.call(
                "POST", "/v1/cleanup/" + deleted["deletion_id"] + "/ack",
                {"plugin_id": "dsh_memory", "status": "completed", "cleaned": ["f_web"]},
            )
            self.assertEqual(status, 200, done)
            status, empty = self.call("GET", "/v1/cleanup/pending?plugin_id=dsh_memory")
            self.assertEqual(status, 200, empty)
            self.assertFalse(any(row["deletion_id"] == deleted["deletion_id"]
                                 for row in empty["items"]))
            self.assertEqual(self.service.core.consumer_refs.refs("test"), [])

    def test_sent_receipt_records_platform_parts_without_claiming_delivery(self):
        with patch.dict(os.environ, {"TEST_DSH_TOKEN": "t-dsh"}):
            request_id = self.remember("今天有点累。")
            scope = {"request_id": request_id, "subject": "private",
                     "session_id": "qq-session", "scene": ""}

            status, receipt = self.call("POST", "/v1/receipts", dict(
                scope, receipt_id="sent:" + request_id, kind="sent",
                consumer="dsh_chat", transport="qq_official",
                parts=[{"platform_id": "msg-1", "chars": 3},
                       {"platform_id": "msg-2", "chars": 2}],
            ))
            self.assertEqual(status, 200, receipt)
            self.assertEqual(receipt["sent_chars"], 5)
            self.assertEqual([part["platform_id"] for part in receipt["parts"]],
                             ["msg-1", "msg-2"])

            # `sent` is not `delivered`: no receipt of the display kind exists.
            status, receipts = self.call("GET", "/v1/receipts?" + urllib.parse.urlencode(
                {k: v for k, v in scope.items() if k != "request_id"}))
            self.assertEqual(status, 200, receipts)
            kinds = {row["kind"] for row in receipts["items"]}
            self.assertIn("sent", kinds)
            self.assertNotIn("delivered", kinds)

            # An unknown transport, empty parts and over-long char counts are
            # refused rather than recorded as evidence.
            status, refused = self.call("POST", "/v1/receipts", dict(
                scope, receipt_id="sent:bad", kind="sent",
                consumer="dsh_chat", transport="qq_official", parts=[]))
            self.assertEqual(status, 400, refused)
            status, refused = self.call("POST", "/v1/receipts", dict(
                scope, receipt_id="sent:bad2", kind="sent",
                consumer="dsh_chat", transport="carrier_pigeon",
                parts=[{"platform_id": "x", "chars": 1}]))
            self.assertEqual(status, 400, refused)
            status, refused = self.call("POST", "/v1/receipts", dict(
                scope, receipt_id="sent:bad3", kind="sent",
                consumer="dsh_chat", transport="qq_official",
                parts=[{"platform_id": "x", "chars": 99}]))
            self.assertEqual(status, 400, refused)


if __name__ == "__main__":
    unittest.main()
