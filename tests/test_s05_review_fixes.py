"""R01—R07 reproduction and verification, through the real service path.

`docs/standalone-service.md` §8.1 says each fix must be pinned by a behavioural
test that goes through the real assembly and HTTP chain, "not by calling an
internal invalidation method by hand or by testing with a fixed clock only".
That is exactly why this file exists: the previous suites passed while the
defects were live.

Each test names its issue id so a failure points at the reviewed gap.
"""

import asyncio
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.adapters.model_http import ModelCallError, ModelSettings
from rin_memory.contracts import DeletionRequest, DeletionTarget, Scope
from rin_memory.persistence.ownership import acquire_root_lock
from rin_memory.errors import RootLockConflict
from rin_memory.service.memory_service import (
    MemoryService,
    ServiceClosing,
    ServiceSettings,
)
from rin_memory.service.server import ApiError, MemoryApi, build_server

CARD = [
    {"section_id": "identity", "text": "你是一只猫娘。",
     "editable_by": {"user": True, "evolution": False}},
    {"section_id": "tone", "text": "说话温柔简洁。",
     "editable_by": {"user": True, "evolution": True}},
]


def _prepare(root: Path) -> None:
    core = build_memory_core(memory_root=root)
    core.recover("demo")
    core.persona_card_service.import_initial("demo", CARD)


class ScriptedModel:
    """Model stand-in that can block, fail once, then recover."""

    def __init__(self, reply: str = "温柔的回答") -> None:
        self.reply = reply
        self.calls = 0
        self.prompts: list[str] = []
        self.gate = threading.Event()
        self.gate.set()
        self.fail_next: list[Exception] = []
        self.started = threading.Event()

    def available(self, tier: str) -> bool:
        return True

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls += 1
        self.prompts.append(messages[0]["content"] if messages else "")
        self.started.set()
        if not self.gate.is_set():
            await asyncio.to_thread(self.gate.wait, 10)
        if self.fail_next:
            raise self.fail_next.pop(0)
        return self.reply


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "memory"
        self.root.mkdir(parents=True)
        _prepare(self.root)
        self.services: list[MemoryService] = []

    def tearDown(self) -> None:
        for service in self.services:
            try:
                asyncio.run(service.stop(drain_timeout_s=1.0))
            except Exception:  # noqa: BLE001
                pass
        self._tmp.cleanup()

    def start(self, model=None, **overrides) -> MemoryService:
        service = MemoryService.start(ServiceSettings(
            memory_root=self.root, character="demo", **overrides,
        ))
        self.services.append(service)
        if model is not None:
            service.model = model  # type: ignore[assignment]
            service.state.chat_ready = True
            service.state.chat_reason = None
        return service

    def api(self, service) -> MemoryApi:
        return MemoryApi(service)


def _chat_body(request_id: str, text: str = "今天好累", **extra) -> dict:
    body = {
        "request_id": request_id,
        "turn_id": f"turn-{request_id}",
        "source_key": f"host:demo:private:{request_id}",
        "messages": [{"role": "user", "content": text}],
    }
    body.update(extra)
    return body


class R01Tests(ServiceCase):
    """A real deletion must invalidate the committed result."""

    def test_real_deletion_invalidates_the_request_result(self):
        service = self.start(ScriptedModel("要被删除的回复"))
        api = self.api(service)
        asyncio.run(api.turns({
            "request_id": "t1", "turn_id": "turn-t1",
            "source_key": "host:demo:private:t1",
            "messages": [{"role": "user", "content": "我平时都喝乌龙茶"}],
            "reply": "好呀",
        }))
        status = api.request_status("t1")
        self.assertEqual(status["status"], "committed")

        # The REAL deletion entry point, not a hand-called invalidator.
        receipt = asyncio.run(service.core.deletion_service.forget(
            DeletionRequest(
                scope=Scope("demo"),
                mode="entry",
                targets=(DeletionTarget(type="fact", id="not-a-real-fact"),),
                source_keys=("host:demo:private:t1",),
                reason="R01 test",
            ),
        ))
        self.assertIn(receipt.status, ("completed", "pending"))

        with self.assertRaises(ApiError) as caught:
            api.request_status("t1")
        self.assertEqual(caught.exception.code, "deleted")
        self.assertEqual(caught.exception.status, 410)

        # The body is gone from the record, not merely hidden by the lookup.
        record = service.core.turn_requests.get("demo", "t1")
        self.assertEqual(record.status, "deleted")
        self.assertIsNone(record.reply)

    def test_deletion_step_is_recorded_as_completed(self):
        service = self.start(ScriptedModel())
        api = self.api(service)
        asyncio.run(api.turns({
            "request_id": "t2", "turn_id": "turn-t2",
            "source_key": "host:demo:private:t2",
            "messages": [{"role": "user", "content": "随便"}],
            "reply": "嗯",
        }))
        receipt = asyncio.run(service.core.deletion_service.forget(
            DeletionRequest(
                scope=Scope("demo"),
                mode="entry",
                targets=(DeletionTarget(type="fact", id="none"),),
                source_keys=("host:demo:private:t2",),
                reason="R01 step",
            ),
        ))
        steps = getattr(receipt, "steps", None) or {}
        self.assertIn("turn_results", steps,
                      "the deletion receipt must report the turn_results step")
        self.assertEqual(steps["turn_results"].get("status"), "completed")


class R02Tests(ServiceCase):
    """The root lock must stay owned until in-flight work finishes."""

    def test_stop_waits_for_in_flight_work_before_releasing_the_lock(self):
        model = ScriptedModel("慢回复")
        model.gate.clear()  # block inside the model call
        service = self.start(model)
        api = self.api(service)

        outcome: dict = {}

        def run_chat():
            try:
                outcome["result"] = asyncio.run(api.chat(_chat_body("r02")))
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = exc

        worker = threading.Thread(target=run_chat, daemon=True)
        worker.start()
        self.assertTrue(model.started.wait(5), "the chat never reached the model")

        stop_done = threading.Event()

        def stop():
            asyncio.run(service.stop(drain_timeout_s=5))
            stop_done.set()

        stopper = threading.Thread(target=stop, daemon=True)
        stopper.start()

        # While the request is still inside the model, another writer must be
        # refused: the lock is released only after the work drains.
        time.sleep(0.4)
        with self.assertRaises(RootLockConflict):
            acquire_root_lock(self.root, mode="cli")
        self.assertFalse(stop_done.is_set(), "stop returned before the drain")

        model.gate.set()
        worker.join(timeout=10)
        self.assertTrue(stop_done.wait(10), "stop never finished")
        self.assertEqual(service.in_flight, 0)
        self.assertTrue(service._last_drain["drained"])

        # After the drain the root is claimable again.
        lock = acquire_root_lock(self.root, mode="cli")
        lock.release()

    def test_request_admitted_before_stop_cannot_commit_after_it(self):
        """The generation check refuses a stale request instead of writing."""
        service = self.start(ScriptedModel())
        generation = service.begin_request()
        asyncio.run(service.stop(drain_timeout_s=0.1))
        with self.assertRaises(ServiceClosing):
            service.assert_current(generation)
        with self.assertRaises(ServiceClosing):
            service.begin_request()
        with self.assertRaises(RootLockConflict):
            acquire_root_lock(self.root, mode="cli")
        service.end_request(generation)
        with acquire_root_lock(self.root, mode="cli"):
            pass


class R03Tests(ServiceCase):
    """HTTP must honour the caller's subject/scene boundaries."""

    def _seed_scoped_facts(self, service) -> None:
        core = service.core
        core.facts.save("demo", [
            {"id": "f_private", "text": "用户喜欢喝乌龙茶", "subject": "private",
             "schema_version": 2, "source": "user_observation",
             "source_event_keys": ["host:demo:private:f1"]},
            {"id": "f_group", "text": "群里说好周五聚餐", "subject": "group:others",
             "schema_version": 2, "source": "user_observation",
             "source_event_keys": ["host:demo:group:others:f2"]},
            {"id": "f_game", "text": "第三层先防后攻", "subject": "private",
             "schema_version": 2, "source": "user_observation",
             "applies_to": {"game_id": "sts2"},
             "source_event_keys": ["host:demo:private:f3"]},
        ])
        core.barrier.refresh("demo")

    def test_context_respects_subject(self):
        """The receipt and the recalled ids are bound to the caller's subject.

        An exact match is visible only to the subject that owns the fact.
        """
        service = self.start(ScriptedModel())
        self._seed_scoped_facts(service)
        api = self.api(service)

        def ids(payload):
            return set(payload["context"].get("memory_ids") or ())

        private = asyncio.run(api.context({"text": "乌龙茶"}))
        group = asyncio.run(api.context({"text": "乌龙茶", "subject": "group:others"}))
        self.assertEqual(service.settings.character, "demo")
        self.assertNotEqual(private["context_receipt_id"],
                            group["context_receipt_id"],
                            "a receipt must be bound to its subject")

        # Ask through explicit recall too: it has no passive threshold, so it
        # proves the subject actually reached the core.
        direct_private = asyncio.run(service.core.recall.recall_memory(
            "demo", "乌龙茶", subject="private",
        ))
        direct_group = asyncio.run(service.core.recall.recall_memory(
            "demo", "乌龙茶", subject="group:others",
        ))
        private_ids = {i["id"] for i in direct_private.get("items") or ()}
        group_ids = {i["id"] for i in direct_group.get("items") or ()}
        self.assertIn("f_private", private_ids)
        self.assertNotIn("f_private", group_ids,
                         "the private fact leaked into another subject")
        self.assertIn("f_private", ids(private))
        self.assertNotIn("f_private", ids(group))

    def test_game_scoped_fact_does_not_leak_into_the_host_scope(self):
        """A game-scoped fact is visible only when the query is in that scene."""
        service = self.start(ScriptedModel())
        self._seed_scoped_facts(service)
        from rin_memory.contracts import Scene

        host = asyncio.run(service.core.recall.recall_memory(
            "demo", "先防后攻", subject="private",
        ))
        game = asyncio.run(service.core.recall.recall_memory(
            "demo", "先防后攻", subject="private", scene=Scene(game_id="sts2"),
        ))
        host_ids = {i["id"] for i in host.get("items") or ()}
        game_ids = {i["id"] for i in game.get("items") or ()}
        self.assertNotIn("f_game", host_ids, "a game tactic leaked into the host scope")
        self.assertIn("f_game", game_ids)

    def test_request_status_refuses_another_subject(self):
        service = self.start(ScriptedModel())
        api = self.api(service)
        asyncio.run(api.turns({
            "request_id": "s1", "turn_id": "turn-s1",
            "source_key": "host:demo:private:s1",
            "messages": [{"role": "user", "content": "私聊内容"}],
            "reply": "私聊回复",
            "subject": "private",
        }))
        with self.assertRaises(ApiError):
            api.request_status("s1", subject="group:others")


class R04Tests(ServiceCase):
    """A retry after a crash must not look like a content conflict."""

    def test_retry_after_a_lost_enqueue_keeps_the_same_business_time(self):
        service = self.start(ScriptedModel())
        api = self.api(service)
        body = {
            "request_id": "q1", "turn_id": "turn-q1",
            "source_key": "host:demo:private:q1",
            "messages": [{"role": "user", "content": "我周末去公园跑步"}],
            "reply": "记住了",
        }
        core = service.core
        real_enqueue = core.outbox.enqueue

        class Boom(RuntimeError):
            pass

        def failing_enqueue(*args, **kwargs):
            raise Boom("disk full between raw save and enqueue")

        core.outbox.enqueue = failing_enqueue  # type: ignore[method-assign]
        # The first attempt fails inside the turn commit; the API reports it as
        # a retryable failure instead of a bare crash.
        with self.assertRaises(ApiError) as failed:
            asyncio.run(api.turns(dict(body)))
        self.assertEqual(failed.exception.status, 503)
        self.assertTrue(failed.exception.retryable)
        self.assertEqual(core.turn_requests.get("demo", "q1").status, "failed")

        # Time moves on (the reviewer's "advance the clock" case), then the
        # caller retries the identical request.
        time.sleep(0.05)
        core.outbox.enqueue = real_enqueue  # type: ignore[method-assign]
        retried = asyncio.run(api.turns(dict(body)))
        self.assertEqual(retried["status"], "committed",
                         "a retry must not fail as a source conflict")
        self.assertEqual(core.recent.count("demo"), 2,
                         "only one turn was stored")

    def test_changing_the_content_still_conflicts(self):
        service = self.start(ScriptedModel())
        api = self.api(service)
        base = {
            "request_id": "q2", "turn_id": "turn-q2",
            "source_key": "host:demo:private:q2",
            "messages": [{"role": "user", "content": "第一版内容"}],
            "reply": "好",
        }
        asyncio.run(api.turns(dict(base)))
        changed = dict(base, messages=[{"role": "user", "content": "完全不同的内容"}])
        with self.assertRaises(ApiError) as caught:
            asyncio.run(api.turns(changed))
        self.assertEqual(caught.exception.code, "request_conflict")


class R05Tests(ServiceCase):
    """/chat must use memory context and store the assistant reply."""

    def test_generation_sees_recalled_memories(self):
        service = self.start(ScriptedModel("好的"))
        core = service.core
        core.facts.save("demo", [
            {"id": "m1", "text": "用户喜欢喝乌龙茶", "subject": "private",
             "schema_version": 2, "source": "user_observation",
             "source_event_keys": ["host:demo:private:m1"]},
            {"id": "m2", "text": "用户周末去公园跑步", "subject": "private",
             "schema_version": 2, "source": "user_observation",
             "source_event_keys": ["host:demo:private:m2"]},
            {"id": "m3", "text": "用户养了一只猫叫团子", "subject": "private",
             "schema_version": 2, "source": "user_observation",
             "source_event_keys": ["host:demo:private:m3"]},
        ])
        core.barrier.refresh("demo")
        api = self.api(service)
        # This input clears the passive calibration threshold (§6.2): BM25 must
        # cover the query terms, and "点/热的" dilution makes the longer variant
        # miss by design.
        asyncio.run(api.chat(_chat_body("c1", "我今天想喝乌龙茶")))
        self.assertTrue(service.model.prompts, "the model was never called")
        self.assertIn("乌龙茶", service.model.prompts[0],
                      "the prompt must carry the recalled memory")

    def test_assistant_reply_is_stored_in_the_turn_history(self):
        service = self.start(ScriptedModel("这是生成好的回复"))
        api = self.api(service)
        asyncio.run(api.chat(_chat_body("c2", "你好")))
        window = service.core.recent.load("demo")
        roles = [row.get("type") for row in window]
        self.assertEqual(roles, ["human", "ai"],
                         "the reply must be part of the stored turn")
        self.assertEqual(window[1]["data"]["content"], "这是生成好的回复")

    def test_external_turn_does_not_get_a_duplicate_reply(self):
        service = self.start(ScriptedModel())
        api = self.api(service)
        asyncio.run(api.turns({
            "request_id": "c3", "turn_id": "turn-c3",
            "source_key": "host:demo:private:c3",
            "messages": [
                {"role": "user", "content": "用户说的话"},
                {"role": "assistant", "content": "客户端自己生成的回复"},
            ],
            "reply": "客户端自己生成的回复",
        }))
        window = service.core.recent.load("demo")
        self.assertEqual([row.get("type") for row in window], ["human", "ai"],
                         "an external turn must not append a second reply")


class R06Tests(ServiceCase):
    """A transient model failure must not close chat permanently."""

    def test_transient_timeout_recovers_without_a_restart(self):
        model = ScriptedModel("恢复后的回复")
        model.fail_next.append(
            ModelCallError("timeout", kind="timeout", retryable=True),
        )
        service = self.start(model)
        api = self.api(service)

        with self.assertRaises(ApiError):
            asyncio.run(api.chat(_chat_body("e1")))

        # No restart, no manual capability reset: the next call must work.
        second = asyncio.run(api.chat(_chat_body("e2")))
        self.assertEqual(second["status"], "committed")
        self.assertEqual(second["reply"], "恢复后的回复")
        self.assertEqual(model.calls, 2)

    def test_auth_failure_does_close_the_capability(self):
        model = ScriptedModel()
        model.fail_next.append(
            ModelCallError("bad key", kind="auth", retryable=False),
        )
        service = self.start(model)
        api = self.api(service)
        with self.assertRaises(ApiError):
            asyncio.run(api.chat(_chat_body("e3")))
        self.assertFalse(service.state.chat_ready)
        with self.assertRaises(ApiError) as caught:
            asyncio.run(api.chat(_chat_body("e4")))
        self.assertEqual(caught.exception.code, "chat_unavailable")
        self.assertEqual(model.calls, 1, "a closed capability must not call out")

    def test_committed_results_replay_while_the_model_is_down(self):
        model = ScriptedModel("先成功")
        service = self.start(model)
        api = self.api(service)
        body = _chat_body("e5")
        asyncio.run(api.chat(dict(body)))

        model.fail_next.append(
            ModelCallError("down", kind="transport", retryable=True),
        )
        replayed = asyncio.run(api.chat(dict(body)))
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["reply"], "先成功")


class R07Tests(ServiceCase):
    """Concurrency must be bounded across loops and survive timeouts."""

    def test_no_cross_loop_semaphore_error_under_batches(self):
        """Every request runs on its own loop; the adapter must not care."""
        service = self.start()
        from rin_memory.adapters.model_http import HttpModelInvoker

        invoker = HttpModelInvoker(ModelSettings(
            base_url="http://127.0.0.1:1/v1",
            api_key_env="COMPANION_MEMORY_TEST_KEY",
            models={"conversation": "m"},
            max_concurrency=2,
            timeout_s=0.2,
        ))
        import os

        os.environ["COMPANION_MEMORY_TEST_KEY"] = "test-key"
        try:
            errors: list[str] = []

            def worker() -> None:
                try:
                    asyncio.run(invoker.invoke(
                        "conversation", [{"role": "user", "content": "hi"}],
                        max_output_tokens=8, timeout_s=0.2, purpose="r07",
                    ))
                except ModelCallError as exc:
                    if "different event loop" in str(exc):
                        errors.append(str(exc))
                except Exception as exc:  # noqa: BLE001
                    if "different event loop" in str(exc):
                        errors.append(str(exc))

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
            self.assertEqual(errors, [], f"cross-loop failure: {errors}")
        finally:
            os.environ.pop("COMPANION_MEMORY_TEST_KEY", None)
        self.assertIsNotNone(service)

    def test_timeouts_never_exceed_the_configured_capacity(self):
        """A timed-out call keeps its slot until its worker actually ends."""
        from rin_memory.adapters import model_http
        import os

        release = threading.Event()
        entered = threading.Semaphore(0)
        peak = {"value": 0}
        lock = threading.Lock()

        class SlowInvoker(model_http.HttpModelInvoker):
            def _post(self, payload, key, timeout, purpose):
                with lock:
                    peak["value"] = max(peak["value"], self.in_flight)
                entered.release()
                release.wait(10)
                return "ok"

        os.environ["COMPANION_MEMORY_TEST_KEY"] = "test-key"
        invoker = SlowInvoker(ModelSettings(
            base_url="http://127.0.0.1:1/v1",
            api_key_env="COMPANION_MEMORY_TEST_KEY",
            models={"conversation": "m"},
            max_concurrency=1,
            timeout_s=0.2,
        ))
        try:
            def worker() -> None:
                try:
                    asyncio.run(invoker.invoke(
                        "conversation", [{"role": "user", "content": "hi"}],
                        max_output_tokens=8, timeout_s=0.2, purpose="r07b",
                    ))
                except ModelCallError:
                    pass

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            # Both coroutines give up at ~0.2s; the workers are still blocked.
            time.sleep(0.8)
            self.assertEqual(
                peak["value"], 1,
                "more workers ran at once than the configured capacity",
            )
            release.set()
            for thread in threads:
                thread.join(timeout=20)
        finally:
            release.set()
            os.environ.pop("COMPANION_MEMORY_TEST_KEY", None)


if __name__ == "__main__":
    unittest.main()
