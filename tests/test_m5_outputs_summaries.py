"""M5 acceptance: assistant outputs, rebuildable summaries, real state, episodes.

The four gaps this batch closes, each with a behavioural test rather than a
source-text assertion:

1. her own words had no record of "what, why, delivered" (POST /v1/outputs);
2. the three working summaries did not exist and could not be rebuilt;
3. build_turn_context returned a constant empty companion_state;
4. an episode had no participants, behaviour, result or terminal state.

The visibility rule is the one that matters most: a body may be persisted
before delivery (so a restart can resume), but only the parts a receipt names
enter the shared conversation or a default recall.
"""

import asyncio
import json
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.contracts import DeletionRequest, DeletionTarget, Scope
from rin_memory.domain.working_summary import SECTION_NAMES


class MutableClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 27, 20, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)


def fact_row(fact_id, text, *, episode_id="", keys=None, evidence_kind="user_assertion",
             importance=8, subject="private", confirmed=None):
    row = {
        "id": fact_id,
        "text": text,
        "importance": importance,
        "source": "user_observation",
        "schema_version": 2,
        "absorbed": True,
        "created_at": confirmed or "2026-10-27T19:00:00+00:00",
        "last_confirmed_at": confirmed or "2026-10-27T19:00:00+00:00",
        "event_start_at": confirmed or "2026-10-27T19:00:00+00:00",
        "source_event_keys": list(keys or [f"host:rin:private:{fact_id}"]),
        "subject": subject,
        "applies_to": {},
        "evidence_kind": evidence_kind,
    }
    if episode_id:
        row["episode_id"] = episode_id
    return row


class M5Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = MutableClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, name="memory", **kwargs):
        return build_memory_core(
            memory_root=self.root / name, clock=self.clock, **kwargs,
        )

    def seed_thread(self, core, *, text="帮主人准备面试", next_step="明天问面试结果",
                    episode_id="ep:host:interview:1027"):
        """One open thread plus the fact that carries its episode."""
        core.facts.save("rin", [fact_row(
            "fact_interview", "用户明天要去面试", episode_id=episode_id,
            keys=["host:rin:private:e_interview"],
        )])
        receipt = core.state_service.commit("rin", {
            "internal": True,
            "base_version": 0,
            "request_id": "state-" + episode_id,
            "ops": [{
                "op": "set_progress",
                "text": text,
                "next_step": next_step,
                "source_event_ids": ["e_interview"],
            }],
        })
        self.assertTrue(receipt.ok, receipt.reason)
        return receipt


class OutputVisibilityTests(M5Base):
    """A body is durable before delivery but invisible until it is delivered."""

    async def test_undelivered_body_is_durable_but_not_in_recent(self):
        core = self.build()
        core.recover("rin")
        created = core.output_service.create(
            "rin", text="面试加油，等你消息。明天记得告诉我结果。",
            kind="proactive", subject="private",
            cause={"kind": "open_thread",
                   "source_keys": ["host:rin:private:e_interview"]},
        )
        self.assertTrue(created.ok, created.reason)
        self.assertEqual(created.record.status, "pending")
        self.assertEqual(core.recent.count("rin"), 0,
                         "a generated body must not be a shared experience yet")

        # It is durable: a fresh process can find it and see what is missing.
        reopened = self.build(name="memory")
        reopened.recover("rin")
        pending = reopened.output_service.pending("rin")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["output_id"], created.record.output_id)
        self.assertEqual([part["index"] for part in pending[0]["pending_parts"]],
                         list(range(pending[0]["parts_total"])))

        # Undelivered text is not recallable through a default recall either.
        recall = await reopened.recall.recall_memory(
            "rin", "面试加油 等你消息", purpose="query",
        )
        self.assertEqual(recall["status"], "miss", recall["items"])

    async def test_delivery_is_idempotent_by_part_index(self):
        core = self.build()
        core.recover("rin")
        created = core.output_service.create(
            "rin", text="第一段到了。第二段还没到。", kind="task_feedback",
            subject="private",
        )
        output_id = created.record.output_id
        segments = created.record.row["segments"]
        self.assertGreaterEqual(len(segments), 1)

        first = core.output_service.deliver("rin", output_id, parts=[{"index": 0}])
        self.assertTrue(first.ok)
        self.assertEqual(first.appended_parts, (0,))
        self.assertEqual(first.recent_written, 1)
        self.assertEqual(core.recent.count("rin"), 1)

        # A transport retry of the same part: duplicate, no second line.
        again = core.output_service.deliver("rin", output_id, parts=[{"index": 0}])
        self.assertTrue(again.duplicate)
        self.assertEqual(again.recent_written, 0)
        self.assertEqual(core.recent.count("rin"), 1)

        # Same index with a different length is a conflict, not an overwrite.
        conflict = core.output_service.deliver(
            "rin", output_id, parts=[{"index": 0, "chars": 1}],
        )
        self.assertFalse(conflict.ok)
        self.assertTrue(conflict.conflict)
        self.assertEqual(conflict.reason, "part_chars_mismatch")

        # An index outside the body is invalid.
        out_of_range = core.output_service.deliver(
            "rin", output_id, parts=[{"index": len(segments) + 5}],
        )
        self.assertFalse(out_of_range.ok)
        self.assertEqual(out_of_range.reason, "part_index_out_of_range")

    async def test_partial_delivery_resumes_only_the_missing_parts(self):
        core = self.build()
        core.recover("rin")
        created = core.output_service.create(
            "rin", text="一。" * 400, kind="proactive", subject="private",
        )
        output_id = created.record.output_id
        total = len(created.record.row["segments"])
        self.assertGreaterEqual(total, 2, "the body must split into several parts")

        done = core.output_service.deliver("rin", output_id, parts=[{"index": 0}])
        self.assertEqual(done.record.status, "partial")
        self.assertEqual(done.recent_written, 1)

        pending = core.output_service.pending("rin")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "partial")
        missing = [part["index"] for part in pending[0]["pending_parts"]]
        self.assertNotIn(0, missing)
        self.assertEqual(missing, list(range(1, total)))

        rest = core.output_service.deliver(
            "rin", output_id,
            parts=[{"index": index} for index in missing],
        )
        self.assertEqual(rest.record.status, "delivered")
        self.assertEqual(rest.recent_written, total - 1)
        self.assertEqual(core.recent.count("rin"), total)
        self.assertEqual(core.output_service.pending("rin"), [])

    async def test_interruption_keeps_the_body_and_writes_nothing(self):
        core = self.build()
        core.recover("rin")
        created = core.output_service.create(
            "rin", text="晚饭吃了吗？", kind="proactive", subject="private",
        )
        output_id = created.record.output_id
        receipt = core.output_service.interrupt("rin", output_id, reason="transport")
        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.record.status, "interrupted")
        self.assertEqual(core.recent.count("rin"), 0,
                         "an interrupted delivery was never a shared experience")
        self.assertEqual(core.output_service.pending("rin")[0]["status"], "interrupted")

        # Retryable: the same body still delivers afterwards.
        delivered = core.output_service.deliver("rin", output_id)
        self.assertEqual(delivered.record.status, "delivered")
        self.assertEqual(core.recent.count("rin"), 1)

    async def test_undelivered_output_stays_out_of_the_recent_window(self):
        core = self.build()
        core.recover("rin")
        core.output_service.create(
            "rin", text="这条只生成没送到", kind="notice", subject="private",
        )
        delivered = core.output_service.create(
            "rin", text="这条送到了", kind="notice", subject="private",
        )
        core.output_service.deliver("rin", delivered.record.output_id)
        texts = [
            str((message.get("data") or {}).get("content") or "")
            for message in core.recent.messages("rin")
        ]
        self.assertIn("这条送到了", texts)
        self.assertNotIn("这条只生成没送到", texts)

    async def test_delivered_output_is_tagged_and_never_extracted(self):
        core = self.build()
        core.recover("rin")
        created = core.output_service.create(
            "rin", text="这是我主动说的", kind="proactive", subject="private",
        )
        core.output_service.deliver("rin", created.record.output_id)
        message = core.recent.messages("rin")[0]
        metadata = (message.get("data") or {}).get("metadata") or {}
        self.assertEqual(metadata.get("content_class"), "companion_output")
        self.assertEqual(metadata.get("output_id"), created.record.output_id)
        # companion_output is a plugin-only class: it never feeds extraction.
        from rin_memory.classification import is_extractable
        self.assertFalse(is_extractable("companion_output"))

    async def test_deleted_cause_invalidates_the_output(self):
        core = self.build()
        core.recover("rin")
        core.facts.save("rin", [fact_row(
            "fact_task", "用户在写周报", keys=["host:rin:private:e_task"],
        )])
        created = core.output_service.create(
            "rin", text="周报写完啦，帮你看了一遍。", kind="task_feedback",
            subject="private",
            cause={"kind": "task_result", "source_keys": ["host:rin:private:e_task"]},
        )
        core.output_service.deliver("rin", created.record.output_id)
        self.assertEqual(core.recent.count("rin"), 1)

        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("rin"), targets=(DeletionTarget("fact", "fact_task"),),
        ))
        row = core.outputs.get("rin", created.record.output_id)
        self.assertEqual(row["status"], "invalidated")
        self.assertEqual(row["text"], "", "the body must not survive its cause")
        self.assertTrue(row["invalidated_at"])


class WorkingSummaryTests(M5Base):
    """The three summaries are projections: rebuildable and never a source."""

    async def test_summaries_are_derived_from_the_records(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core)
        core.facts.save("rin", [
            fact_row("fact_coffee", "用户喜欢乌龙茶", keys=["host:rin:private:e_coffee"],
                     importance=9),
            fact_row("fact_other", "别人的事", subject="group"),
        ])
        summary = core.working_summary_service.build("rin", subject="private")
        self.assertEqual(set(summary.sections), set(SECTION_NAMES))
        facts_block = summary.section("important_facts").block
        self.assertIn("用户喜欢乌龙茶", facts_block)
        self.assertNotIn("别人的事", facts_block, "范围过滤必须生效")
        threads = summary.section("open_threads").block
        self.assertIn("帮主人准备面试", threads)
        self.assertIn("明天问面试结果", threads)

    async def test_deleting_a_source_changes_the_summary_immediately(self):
        core = self.build()
        core.recover("rin")
        core.facts.save("rin", [
            fact_row("fact_keep", "用户养了一只猫", keys=["host:rin:private:e_cat"]),
            fact_row("fact_drop", "用户前女友叫小雨", keys=["host:rin:private:e_ex"]),
        ])
        before = core.working_summary_service.build("rin", subject="private")
        self.assertIn("用户前女友叫小雨", before.section("important_facts").block)

        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("rin"), targets=(DeletionTarget("fact", "fact_drop"),),
        ))
        after = core.working_summary_service.build("rin", subject="private")
        self.assertNotIn("用户前女友叫小雨", after.section("important_facts").block,
                         "删除来源后摘要必须同步失效，不能复活")
        self.assertIn("用户养了一只猫", after.section("important_facts").block)
        self.assertNotEqual(before.rebuild_key, after.rebuild_key,
                            "重建键必须反映来源变化")

    async def test_summary_survives_a_missing_cache_file(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core)
        # The cache row is written by maintenance; delete the whole file.
        for path in (self.root / "memory" / "rin").glob("working_summary.json"):
            path.unlink()
        await core.maintenance.run_once("rin")
        first = core.working_summary_service.build("rin", subject="private")

        cache = self.root / "memory" / "rin" / "working_summary.json"
        if cache.exists():
            cache.unlink()
        # Restart from the same root: the summary is rebuilt from the records,
        # not from the (now deleted) cache.
        reopened = self.build(name="memory")
        reopened.recover("rin")
        second = reopened.working_summary_service.build("rin", subject="private")
        self.assertEqual(first.rebuild_key, second.rebuild_key)
        self.assertIn("帮主人准备面试", second.section("open_threads").block)

    async def test_deleting_the_cache_does_not_change_any_answer(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core)
        before = core.working_summary_service.build("rin", subject="private").to_dict()
        for path in (self.root / "memory" / "rin").glob("working_summary.json"):
            path.unlink()
        after = core.working_summary_service.build("rin", subject="private").to_dict()
        self.assertEqual(before["sections"], after["sections"])
        self.assertEqual(before["rebuild_key"], after["rebuild_key"])

    async def test_maintenance_writes_the_audit_row_and_records_the_key(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core)
        result = await core.maintenance.run_once("rin")
        self.assertIn("working_summary", result.steps)
        self.assertEqual(result.steps["working_summary"]["status"], "ok")
        row = core.working_summary.get("rin", "private")
        self.assertIsNotNone(row)
        expected = core.working_summary_service.build("rin", subject="private")
        self.assertEqual(row["rebuild_key"], expected.rebuild_key)

    async def test_deletion_marks_the_audit_row_stale(self):
        core = self.build()
        core.recover("rin")
        core.facts.save("rin", [fact_row(
            "fact_task", "用户在写周报", keys=["host:rin:private:e_task"],
        )])
        await core.maintenance.run_once("rin")
        self.assertIsNone(core.working_summary.get("rin", "private")["invalidated_at"])
        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("rin"), targets=(DeletionTarget("fact", "fact_task"),),
        ))
        row = core.working_summary.get("rin", "private")
        self.assertIsNotNone(row["invalidated_at"],
                             "删除来源后审计缓存必须标失效")


class FollowupResolutionTests(M5Base):
    """G18 core: a cross-session "那个后来怎么样" continues the open thread."""

    async def test_continuation_question_resolves_the_open_thread(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core)
        context = await core.recall.build_turn_context(
            "rin", current_input="那个后来怎么样了？", subject="private",
        )
        self.assertEqual(context["threads_source"], "companion_state")
        self.assertEqual(context["followup"]["status"], "resolved")
        self.assertIn("准备面试", context["followup"]["thread"]["text"])
        self.assertEqual(context["companion_state_version"], 1)
        self.assertIn("准备面试", context["context"])
        # The continuation actually reaches the right experience, not just the
        # right thread: the episode of the resolved item is the anchor.
        self.assertEqual(context["result"], "matched", context["query_meta"])
        self.assertIn("fact_interview", context["memory_ids"])
        self.assertEqual(context["retrieval_mode"], "episode")

    async def test_cross_session_continuation_after_restart(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core)
        # A brand-new session: no recent turns at all, nothing to lean on but
        # the records (this is the "cross session 那个后来怎么样" case).
        reopened = self.build(name="memory")
        reopened.recover("rin")
        context = await reopened.recall.build_turn_context(
            "rin", current_input="上次说的那件事后来怎么样了？",
            subject="private", recent_turns=[],
        )
        self.assertEqual(context["followup"]["status"], "resolved")
        self.assertIn("准备面试", context["followup"]["thread"]["text"])
        self.assertEqual(context["result"], "matched")
        self.assertIn("fact_interview", context["memory_ids"])

    async def test_several_candidates_give_a_clarification_basis(self):
        core = self.build()
        core.recover("rin")
        core.facts.save("rin", [fact_row(
            "fact_a", "用户在准备面试", episode_id="ep:host:interview",
            keys=["host:rin:private:e_a"],
        ), fact_row(
            "fact_b", "用户在准备答辩", episode_id="ep:host:defense",
            keys=["host:rin:private:e_b"],
        )])
        core.state_service.commit("rin", {
            "internal": True, "base_version": 0, "request_id": "state-two",
            "ops": [
                {"op": "set_progress", "text": "准备面试材料",
                 "next_step": "明天模拟一遍", "source_event_ids": ["e_a"]},
                {"op": "set_progress", "text": "准备答辩幻灯片",
                 "next_step": "周三给导师看", "source_event_ids": ["e_b"]},
            ],
        })
        resolved = await core.recall.build_turn_context(
            "rin", current_input="那个后来怎么样了？", subject="private",
        )
        followup = resolved["followup"]
        self.assertEqual(followup["status"], "ambiguous")
        self.assertGreaterEqual(len(followup["candidates"]), 2)
        basis = followup["clarify_basis"]
        self.assertTrue(basis)
        for item in basis:
            self.assertTrue(item.get("thread_id"))
            self.assertTrue(item.get("distinguisher"),
                            "多候选时必须给出可区分的依据")

    async def test_an_unrelated_topic_is_not_dragged_back(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core)
        fresh = await core.recall.build_turn_context(
            "rin", current_input="今天天气不错", subject="private",
        )
        self.assertEqual(fresh["followup"]["status"], "not_continuation")
        self.assertEqual(fresh["query_meta"].get("used_thread"), False)
        self.assertEqual(fresh["result"], "miss",
                         "新话题不得被拉回旧事项")
        self.assertEqual(fresh["memory_ids"], [])


class RealStateTests(M5Base):
    """companion_state is the real snapshot, read once, same version."""

    async def test_turn_context_carries_the_real_state_version(self):
        core = self.build()
        core.recover("rin")
        receipt = self.seed_thread(core)
        self.assertEqual(receipt.version, 1)
        context = await core.recall.build_turn_context(
            "rin", current_input="随便聊聊", subject="private",
        )
        self.assertEqual(context["companion_state_version"], 1)
        state = context["companion_state"]
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["items"], [])
        self.assertEqual(state["block"], "")

    async def test_state_block_and_version_come_from_one_read(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core)
        context = await core.recall.build_turn_context(
            "rin", current_input="随便聊聊", subject="private",
        )
        version = context["companion_state_version"]
        live = core.state_service.read("rin", subject="private")
        self.assertEqual(version, live.version)
        self.assertIn("准备面试", live.block)
        self.assertEqual(context["companion_state"]["block"], "")

    async def test_no_state_degrades_to_the_documented_empty_shape(self):
        core = self.build()
        core.recover("rin")
        context = await core.recall.build_turn_context(
            "rin", current_input="随便聊聊", subject="private",
        )
        self.assertEqual(context["companion_state_version"], 0)
        self.assertEqual(context["companion_state"]["block"], "")
        self.assertEqual(context["companion_state"]["items"], [])


class EpisodeViewTests(M5Base):
    """An episode is derived with participants, behaviour, result and state."""

    async def test_episode_has_both_sides_result_and_terminal_state(self):
        core = self.build()
        core.recover("rin")
        core.facts.save("rin", [
            fact_row("fact_ask", "用户说明天要面试", episode_id="ep:host:interview",
                     keys=["host:rin:private:e1"], evidence_kind="user_assertion"),
            fact_row("fact_rehearse", "用户演练了一遍自我介绍",
                     episode_id="ep:host:interview",
                     keys=["host:rin:private:e2"],
                     evidence_kind="observed_action"),
            fact_row("fact_reply", "我说今天可以先歇会儿",
                     episode_id="ep:host:interview",
                     keys=["host:rin:private:e3"],
                     evidence_kind="agent_expression"),
        ])
        core.state_service.commit("rin", {
            "internal": True, "base_version": 0, "request_id": "st",
            "ops": [{"op": "set_progress", "text": "准备面试",
                     "next_step": "等面试结果", "source_event_ids": ["e2"]}],
        })
        summary = core.working_summary_service.build("rin", subject="private")
        episode = summary.episode
        self.assertIsNotNone(episode)
        self.assertEqual(episode["episode_id"], "ep:host:interview")
        self.assertIn("master", episode["participants"])
        self.assertIn("assistant", episode["participants"])
        self.assertTrue(episode["user_behaviour"])
        self.assertTrue(episode["assistant_behaviour"])
        self.assertEqual(episode["result"], "用户演练了一遍自我介绍")
        self.assertEqual(episode["state"], "waiting")
        self.assertEqual(episode["followup_at"], None)
        self.assertTrue(episode["last_activity_at"])

    async def test_episode_reports_a_mention_restriction(self):
        core = self.build()
        core.recover("rin")
        core.facts.save("rin", [fact_row(
            "fact_ex", "用户不想提前女友的事", episode_id="ep:host:ex",
            keys=["host:rin:private:e_ex"],
        )])
        core.directives.record(
            "rin", term="前女友", kind="ban_topic", locale="zh",
        )
        summary = core.working_summary_service.build("rin", subject="private")
        episode = summary.episode
        self.assertIsNotNone(episode)
        self.assertTrue(episode["mention_restricted"])
        self.assertIn("别提", summary.section("episode").block)

    async def test_episode_without_id_is_not_invented(self):
        core = self.build()
        core.recover("rin")
        core.facts.save("rin", [fact_row("fact_plain", "用户喜欢乌龙茶")])
        summary = core.working_summary_service.build("rin", subject="private")
        self.assertIsNone(summary.episode)
        self.assertEqual(summary.section("episode").block, "")



class HttpOutputTests(unittest.TestCase):
    """The /v1/outputs transport: capability, scoping and the visibility rule."""

    def setUp(self):
        import os
        from unittest.mock import patch

        from rin_memory.service.management import ManagementApi
        from rin_memory.service.memory_service import MemoryService, ServiceSettings
        from rin_memory.service.server import MemoryApi, build_server

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "memory").mkdir()
        self.env = patch.dict(os.environ, {
            "TEST_OUT_TOKEN": "t-out", "TEST_PLAIN_TOKEN": "t-plain-out",
        })
        self.env.start()
        self.settings = ServiceSettings(
            memory_root=self.root / "memory", character="test",
            clients={
                "dsh-desktop": {
                    "token_env": "TEST_OUT_TOKEN",
                    "subjects": ["private"],
                    "plugin_id": "dsh_memory",
                    "memory_outputs": True,
                },
                "dsh-plain": {
                    "token_env": "TEST_PLAIN_TOKEN",
                    "subjects": ["private"],
                    "plugin_id": "dsh_memory",
                },
            },
            require_published_persona=False,
        )
        self.service = MemoryService.start(self.settings)
        self.api = MemoryApi(self.service)
        self.management = ManagementApi(self.service)
        self.management.initialize({"display_name": "小潮", "text": "温柔而直白。"})
        self.service.core.persona_publish.publish("test")
        self.server = build_server(
            self.api, host="127.0.0.1", port=0, management=self.management,
        )
        self.port = self.server.server_address[1]
        import threading
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05}, daemon=True,
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        asyncio.run(self.service.stop())
        self.tmp.cleanup()
        self.env.stop()

    def call(self, method, path, body=None, *, client="dsh-desktop"):
        import urllib.error
        import urllib.request

        token = "t-out" if client == "dsh-desktop" else "t-plain-out"
        headers = {
            "Content-Type": "application/json",
            "X-Companion-Client": client,
            "X-Companion-Token": token,
        }
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

    def test_capability_is_required(self):
        status, payload = self.call(
            "POST", "/v1/outputs", {"text": "我没有这个能力"},
            client="dsh-plain",
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["code"], "forbidden")

    def test_record_deliver_and_visibility_over_http(self):
        status, created = self.call("POST", "/v1/outputs", {
            "text": "面试加油。明天记得告诉我结果。",
            "kind": "proactive",
            "subject": "private",
            "cause": {"kind": "open_thread",
                      "source_keys": ["host:test:private:e_i"]},
        })
        self.assertEqual(status, 200, created)
        output_id = created["output_id"]
        self.assertEqual(created["status"], "pending")
        self.assertEqual(self.service.core.recent.count("test"), 0)

        status, listed = self.call("GET", "/v1/outputs?pending=1")
        self.assertEqual(status, 200, listed)
        self.assertEqual([item["output_id"] for item in listed["items"]], [output_id])

        status, delivered = self.call("POST", "/v1/outputs/deliver", {
            "output_id": output_id, "parts": [{"index": 0}],
        })
        self.assertEqual(status, 200, delivered)
        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual(self.service.core.recent.count("test"), 1)

        # A retry: duplicate, still one line.
        status, again = self.call("POST", "/v1/outputs/deliver", {
            "output_id": output_id, "parts": [{"index": 0}],
        })
        self.assertEqual(status, 200, again)
        self.assertTrue(again["duplicate"])
        self.assertEqual(self.service.core.recent.count("test"), 1)

    def test_unknown_or_foreign_output_is_not_found(self):
        status, payload = self.call("POST", "/v1/outputs/deliver", {
            "output_id": "out_does_not_exist", "parts": [{"index": 0}],
        })
        self.assertEqual(status, 404, payload)

    def test_interrupted_delivery_writes_nothing(self):
        status, created = self.call("POST", "/v1/outputs", {
            "text": "这条会中断", "kind": "notice", "subject": "private",
        })
        self.assertEqual(status, 200, created)
        status, current = self.call("POST", "/v1/outputs/deliver", {
            "output_id": created["output_id"], "kind": "interrupted",
            "reason": "transport",
        })
        self.assertEqual(status, 200, current)
        self.assertEqual(current["status"], "interrupted")
        self.assertEqual(self.service.core.recent.count("test"), 0)
        status, pending = self.call("GET", "/v1/outputs?pending=1")
        self.assertEqual(status, 200, pending)
        self.assertEqual(len(pending["items"]), 1)

    def test_working_summary_and_followup_routes(self):
        core = self.service.core
        core.facts.save("test", [fact_row(
            "fact_interview", "用户明天要去面试",
            episode_id="ep:host:interview",
            keys=["host:test:private:e_i"],
        )])
        core.state_service.commit("test", {
            "internal": True, "base_version": 0, "request_id": "http-state",
            "ops": [{"op": "set_progress", "text": "帮主人准备面试",
                     "next_step": "明天问结果", "source_event_ids": ["e_i"]}],
        })
        status, summary = self.call("GET", "/v1/working-summary?subject=private")
        self.assertEqual(status, 200, summary)
        self.assertIn("important_facts", summary["sections"])
        self.assertIn("帮主人准备面试",
                      summary["sections"]["open_threads"]["block"])

        status, followup = self.call(
            "GET", "/v1/followup?subject=private&text="
            + urllib.parse.quote("那个后来怎么样了？"),
        )
        self.assertEqual(status, 200, followup)
        self.assertEqual(followup["status"], "resolved")
        self.assertIn("准备面试", followup["thread"]["text"])


class ContextLayersTests(M5Base):
    """/v1/context gains the new layers additively; old keys keep their shape."""

    async def test_context_payload_adds_layers_without_changing_existing_keys(self):
        from rin_memory.service.management import ManagementApi
        from rin_memory.service.memory_service import MemoryService, ServiceSettings
        from rin_memory.service.server import MemoryApi

        root = self.root / "svc"
        root.mkdir(parents=True, exist_ok=True)
        service = MemoryService.start(
            ServiceSettings(memory_root=root, character="rin"),
        )
        try:
            management = ManagementApi(service)
            management.initialize({"display_name": "小铃", "text": "温柔而直白。"})
            core = service.core
            core.recover("rin")
            core.persona_publish.publish("rin")
            core.facts.save("rin", [fact_row(
                "fact_interview", "用户明天要去面试",
                episode_id="ep:host:interview",
                keys=["host:rin:private:e_i"],
            )])
            core.state_service.commit("rin", {
                "internal": True, "base_version": 0, "request_id": "svc-state",
                "ops": [{"op": "set_progress", "text": "帮主人准备面试",
                         "next_step": "明天问结果",
                         "source_event_ids": ["e_i"]}],
            })
            payload = await MemoryApi(service).context({
                "text": "那个后来怎么样了？", "subject": "private",
                "session_id": "s1",
            })
        finally:
            await service.stop(drain_timeout_s=5)

        for key in ("character_id", "persona_version", "card_revision",
                    "projection", "persona", "expression", "context",
                    "corrections", "recent_turns", "context_receipt_id",
                    "expires_in_s", "basis"):
            self.assertIn(key, payload, f"既有字段 {key} 不得消失")
        # Additive layers.
        self.assertIsNotNone(payload["working_summary"])
        self.assertIn("sections", payload["working_summary"])
        self.assertEqual(payload["state_version"], 1)
        self.assertEqual(payload["state"]["block"], "")
        self.assertIn("准备面试", payload["context"]["context"])
        self.assertEqual(payload["threads_source"], "companion_state")
        self.assertEqual(payload["episode"]["episode_id"], "ep:host:interview")
        self.assertEqual(payload["working_summary"]["episode"]["episode_id"],
                         "ep:host:interview")
        # The B06 'context' sub-payload keeps its own shape untouched.
        self.assertIn("result", payload["context"])
        self.assertIn("memory_ids", payload["context"])


if __name__ == "__main__":
    unittest.main()
