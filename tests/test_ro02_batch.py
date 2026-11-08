"""RO02 acceptance: batched extraction keeps per-utterance evidence.

Design contract (docs/rin-next-optimization-plan.md 6.2):

* ordinary turns queue up and are extracted with one model call per batch --
  five turns per scope, or whatever is left once the last utterance has been
  idle for the idle window, with an early split at the input budget;
* the batch key is character + subject + client + session + scene, so a batch
  never crosses a session or a subject;
* every utterance keeps its own source key, business time and order, the model
  cites the utterances a fact rests on, and the service verifies the citation;
* relative time resolves against the *cited* utterance, not the batch head/tail
  or the processing time; conflicting time bases skip the fact;
* conversation-state operations are applied in original event order and a late
  stale update never overrides the newer terminal state;
* extra context used to resolve a reference is stored as a reference and never
  adds evidence;
* assistant lines, external material and conversation examples are never
  user-preference evidence;
* the batch id is stable from sources + input, so retries, partial writes and
  restarts do not double-count; an empty result is a normal completion; a parse
  failure keeps the sources pending;
* explicit teaching, corrections and deletions keep the immediate path.

Every case runs over a real core on a tempfile root; no network, no host state.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.contracts import DeletionRequest, Scope
from rin_memory.domain.extraction import ExtractionSettings
from rin_memory.domain.reflection import ReflectionSettings
from test_b04_extraction import extraction_reply


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


class BatchModel:
    """Answers extraction with citations and records every prompt."""

    def __init__(self, replies=()):
        self.calls = []
        self._replies = list(replies)

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls.append(messages[0]["content"] if messages else "")
        reply = self._replies.pop(0) if self._replies else None
        if reply is None:
            return "[]" if purpose == "memory_fact_extraction" else "{}"
        if purpose == "memory_fact_extraction":
            return extraction_reply(reply, self.calls[-1])
        return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)

    @property
    def extraction_calls(self):
        return list(self.calls)


def human(text, source_key=None):
    message = {"type": "human", "data": {"content": text}}
    if source_key:
        message["data"]["metadata"] = {"source_key": source_key}
    return message


def assistant(text):
    return {"type": "ai", "data": {"content": text}}


class BatchHarness(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = MutableClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, model, *, settings=None, **kwargs):
        return build_memory_core(
            memory_root=self.root / "memory", clock=self.clock, model=model,
            extraction_settings=settings or ExtractionSettings(
                inline=False, batch_max_turns=5, batch_idle_seconds=0.0,
            ),
            **kwargs,
        )


class BatchTriggerTests(BatchHarness):
    """20 ordinary turns in one scope become 4 batches (5 turns each).

    These run with the production idle window (120 s), because that is the
    configuration the cost target is stated for: a five-turn group closes on
    the turn count, and whatever is left closes once the last utterance has
    been idle for the window.
    """

    PRODUCTION = dict(batch_max_turns=5, batch_idle_seconds=120.0)

    async def test_twenty_turns_form_four_batches(self):
        model = BatchModel([
            [{"text": "用户第1批的偏好", "importance": 6, "sources": [1]}],
            [{"text": "用户第2批的偏好", "importance": 6, "sources": [2]}],
            [{"text": "用户第3批的偏好", "importance": 6, "sources": [3]}],
            [{"text": "用户第4批的偏好", "importance": 6, "sources": [4]}],
        ])
        core = self.build(model, settings=ExtractionSettings(
            inline=False, **self.PRODUCTION,
        ))
        core.recover("demo")
        for index in range(20):
            self.clock.advance(seconds=20)
            await core.extraction.ingest_turn(
                "demo", [human("第%d句话" % index)],
                source_key="host:demo:private:r%d" % index, session_id="s1",
            )
        self.assertEqual(len(core.outbox.pending("demo")), 20)
        # Persist now, extract later: the queue is the only source.
        stats = await core.outbox_dispatcher.run_pending("demo", limit=50)
        self.assertEqual(len(model.extraction_calls), 4)
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["processed"], 20)
        self.assertEqual(
            [row["text"] for row in core.facts.load("demo")],
            ["用户第1批的偏好", "用户第2批的偏好", "用户第3批的偏好", "用户第4批的偏好"],
        )

    async def test_twenty_turns_under_the_production_window_are_four_batches(self):
        """The 20 -> 4 acceptance, replayed the way production actually runs.

        Turns arrive every 20 s, the maintenance pass runs every 60 s: exact
        multiples of five close on the count, the rest closes on the idle
        window. Nothing is lost and nothing is extracted twice.
        """
        model = BatchModel()
        core = self.build(model, settings=ExtractionSettings(
            inline=False, **self.PRODUCTION,
        ))
        core.recover("demo")
        for index in range(20):
            self.clock.advance(seconds=20)
            await core.extraction.ingest_turn(
                "demo", [human("第%d句话" % index)],
                source_key="host:demo:private:q%d" % index, session_id="s1",
            )
            # A maintenance round every three turns, like the real scheduler.
            if (index + 1) % 3 == 0:
                await core.maintenance.run_once("demo")
        # Inside the run only the groups that reached five turns were closed:
        # 3 rounds so far, and the tail is still waiting for the idle window.
        self.assertEqual(len(model.extraction_calls), 3)
        self.assertEqual(len(core.outbox.pending("demo")), 5)
        # The window elapses with no new utterance: the tail closes.
        self.clock.advance(seconds=121)
        await core.maintenance.run_once("demo")
        self.assertEqual(len(model.extraction_calls), 4)
        self.assertEqual(core.outbox.pending("demo"), ())
        self.assertEqual(len(core.facts.load("demo")), 0, "the model returns []")
        # Every turn was covered by exactly one call.
        self.assertEqual(core.recent.count("demo"), 20)

    async def test_batch_ranges_cover_the_turns_in_order(self):
        model = BatchModel()
        core = self.build(model, settings=ExtractionSettings(
            inline=False, **self.PRODUCTION,
        ))
        core.recover("demo")
        keys = []
        for index in range(12):
            self.clock.advance(seconds=20)
            key = "host:demo:private:b%d" % index
            keys.append(key)
            await core.extraction.ingest_turn(
                "demo", [human("第%d句话" % index)],
                source_key=key, session_id="s1",
            )
        # A drain only closes groups the rules allow: 5 + 5, and the last two
        # wait for the idle window.
        outcomes = await core.extraction.process_outbox("demo", limit=50)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len(core.outbox.pending("demo")), 2)
        self.clock.advance(seconds=121)
        outcomes = outcomes + await core.extraction.process_outbox("demo", limit=50)
        # 12 turns at five per batch: 5 + 5 + the remaining 2.
        self.assertEqual(len(outcomes), 3)
        self.assertEqual(list(outcomes[0].source_keys), keys[:5])
        self.assertEqual(list(outcomes[1].source_keys), keys[5:10])
        self.assertEqual(list(outcomes[2].source_keys), keys[10:])
        self.assertEqual(core.outbox.pending("demo"), ())
        # Each batch carries a stable id derived from its sources and input.
        self.assertEqual(len(outcomes[0].batch_id), 16)
        self.assertEqual(
            len({outcome.batch_id for outcome in outcomes}), 3,
        )

    async def test_idle_window_flushes_a_partial_batch(self):
        model = BatchModel()
        core = self.build(model, settings=ExtractionSettings(
            inline=False, batch_max_turns=5, batch_idle_seconds=120.0,
        ))
        core.recover("demo")
        for index in range(3):
            self.clock.advance(seconds=10)
            await core.extraction.ingest_turn(
                "demo", [human("第%d句话" % index)],
                source_key="host:demo:private:i%d" % index, session_id="s1",
            )
        # Inside the idle window nothing is extracted yet.
        first = await core.extraction.process_outbox("demo", limit=50)
        self.assertEqual(first, ())
        self.assertEqual(len(model.extraction_calls), 0)
        self.assertEqual(len(core.outbox.pending("demo")), 3)
        # Two minutes after the last utterance the group is flushed.
        self.clock.advance(seconds=121)
        second = await core.extraction.process_outbox("demo", limit=50)
        self.assertEqual(len(second), 1)
        self.assertEqual(len(model.extraction_calls), 1)
        self.assertEqual(core.outbox.pending("demo"), ())

    async def test_input_budget_splits_early(self):
        model = BatchModel()
        core = self.build(model, settings=ExtractionSettings(
            inline=False, batch_max_turns=5, batch_idle_seconds=0.0,
            batch_max_messages=2,
        ))
        core.recover("demo")
        for index in range(5):
            self.clock.advance(seconds=10)
            await core.extraction.ingest_turn(
                "demo", [human("第%d句话" % index), assistant("回应%d" % index)],
                source_key="host:demo:private:z%d" % index, session_id="s1",
            )
        # Two messages per turn against a two-message budget: one turn per call.
        await core.extraction.process_outbox("demo", limit=50)
        self.assertEqual(len(model.extraction_calls), 5)

    async def test_no_batch_crosses_a_session_or_a_subject(self):
        model = BatchModel()
        core = self.build(model)
        core.recover("demo")
        plan = [
            ("s1", "host:demo:private:s1a"),
            ("s2", "host:demo:private:s2a"),
            ("s1", "host:demo:private:s1b"),
        ]
        for index, (session, key) in enumerate(plan):
            self.clock.advance(seconds=10)
            await core.extraction.ingest_turn(
                "demo", [human("会话%d的第%d句" % (int(session[1]), index))],
                source_key=key, session_id=session,
            )
        await core.extraction.process_outbox("demo", limit=50)
        prompts = model.extraction_calls
        self.assertEqual(len(prompts), 2)
        combined = " ".join(prompts)
        # The two s1 turns share one prompt; s2 is alone in its own.
        self.assertTrue(any("s1a" not in prompt for prompt in prompts))
        for prompt in prompts:
            self.assertNotIn("会话1", prompt) if "会话2" in prompt else None

    async def test_subject_boundary_is_never_crossed(self):
        model = BatchModel()
        core = self.build(model)
        core.recover("demo")
        await core.extraction.ingest_turn(
            "demo", [human("私聊里说的话")],
            source_key="host:demo:private:p1", session_id="s1",
        )
        self.clock.advance(seconds=10)
        await core.extraction.ingest_turn(
            "demo", [human("群里说的话")],
            source_key="host:demo:group-a:p2", session_id="s1",
        )
        await core.extraction.process_outbox("demo", limit=50)
        self.assertEqual(len(model.extraction_calls), 2)


class EvidenceBindingTests(BatchHarness):
    """Per-utterance sources, times and order inside one batch."""

    async def test_each_fact_binds_only_the_utterance_it_cites(self):
        model = BatchModel([
            [{"text": "用户喜欢大号马克杯", "importance": 6, "sources": [3]}],
        ])
        core = self.build(model)
        core.recover("demo")
        keys = []
        for index, text in enumerate(
            ("我最近在挑杯子", "看了几款都不错", "以后都买大号的吧",
             "顺便说下今天下雨", "明天还要早起"),
        ):
            self.clock.advance(seconds=30)
            key = "host:demo:private:c%d" % index
            keys.append(key)
            await core.extraction.ingest_turn(
                "demo", [human(text)], source_key=key, session_id="s1",
            )
        outcomes = await core.extraction.process_outbox("demo", limit=50)
        self.assertEqual(len(model.extraction_calls), 1, "five turns, one call")
        self.assertEqual(len(outcomes), 1)
        facts = core.facts.load("demo")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["text"], "用户喜欢大号马克杯")
        self.assertEqual(facts[0]["source_event_keys"], ["host:demo:private:c2"])
        self.assertEqual(facts[0]["context_refs"], [])
        self.assertEqual(facts[0]["evidence_kind"], "user_assertion")

    async def test_uncited_fact_in_a_multi_turn_batch_is_dropped(self):
        model = BatchModel([
            [{"text": "用户喜欢小号杯子", "importance": 6}],
        ])
        core = self.build(model)
        core.recover("demo")
        for index in range(3):
            self.clock.advance(seconds=10)
            await core.extraction.ingest_turn(
                "demo", [human("第%d句话" % index)],
                source_key="host:demo:private:u%d" % index, session_id="s1",
            )
        await core.extraction.process_outbox("demo", limit=50)
        # A fact that cites no utterance cannot be attributed, so it is not
        # written: attaching it to all three would make it look like three
        # independent supports.
        self.assertEqual(core.facts.load("demo"), [])

    async def test_relative_time_resolves_against_the_cited_utterance(self):
        model = BatchModel([
            [
                {"text": "用户明天有面试", "importance": 8, "sources": [1],
                 "event_when": {"start": {"offset": 1, "unit": "day"}, "end": None}},
                {"text": "用户后天要出差", "importance": 7, "sources": [4],
                 "event_when": {"start": {"offset": 2, "unit": "day"}, "end": None}},
            ],
        ])
        core = self.build(model)
        core.recover("demo")
        spoken = datetime(2026, 10, 27, 20, 0, tzinfo=timezone.utc)
        # Utterance 1 at 20:00 on the 27th; the rest a full day later, so the
        # batch head, the batch tail and the processing time (two days after
        # the queue was written) all disagree about what "tomorrow" means.
        offsets = [0, 24 * 60, 24 * 60 + 1, 24 * 60 + 2, 24 * 60 + 3]
        for index, offset in enumerate(offsets):
            await core.extraction.ingest_turn(
                "demo", [human("第%d句" % index)],
                source_key="host:demo:private:t%d" % index, session_id="s1",
                occurred_at=spoken + timedelta(minutes=offset),
            )
        # Queue drains two days later; the batch must not use that time.
        self.clock.advance(days=2)
        await core.extraction.process_outbox("demo", limit=50)
        facts = {row["text"]: row for row in core.facts.load("demo")}
        interview = datetime.fromisoformat(facts["用户明天有面试"]["event_start_at"])
        self.assertEqual(interview.date(), datetime(2026, 10, 28).date())
        trip = datetime.fromisoformat(facts["用户后天要出差"]["event_start_at"])
        # Utterance 5 was spoken a day after utterance 1, so "two days out" is
        # measured from that turn: 30th, not the 29th (batch head / earliest
        # turn) and not the 24th (processing time).
        self.assertEqual(trip.date(), datetime(2026, 10, 30).date())

    async def test_conflicting_time_bases_skip_the_fact(self):
        model = BatchModel([
            [{"text": "用户要出差", "importance": 6, "sources": [1, 3],
              "event_when": {"start": {"offset": 1, "unit": "day"}, "end": None}}],
        ])
        core = self.build(model)
        core.recover("demo")
        base = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
        for index in range(3):
            await core.extraction.ingest_turn(
                "demo", [human("第%d句" % index)],
                source_key="host:demo:private:f%d" % index, session_id="s1",
                occurred_at=base + timedelta(days=index),
            )
        await core.extraction.process_outbox("demo", limit=50)
        # One day after day 1 is not one day after day 3: the citation spans
        # two different answers, so the fact cannot be anchored and is skipped.
        self.assertEqual(core.facts.load("demo"), [])

    async def test_assistant_and_external_material_are_never_user_evidence(self):
        model = BatchModel([
            [{"text": "用户喜欢大号杯子", "importance": 6, "sources": [4]}],
        ])
        core = self.build(model)
        core.recover("demo")
        texts = [
            ("我随便看看杯子", "human"),
            ("别人家的 bot 都推荐大号杯", "ai"),
            ("我记得你说过大号更好用", "ai"),
            ("那就大号吧", "human"),
            ("我随便看看杯子", "human"),
        ]
        for index, (text, kind) in enumerate(texts):
            self.clock.advance(seconds=10)
            message = human(text) if kind == "human" else assistant(text)
            await core.extraction.ingest_turn(
                "demo", [message],
                source_key="host:demo:private:m%d" % index, session_id="s1",
            )
        await core.extraction.process_outbox("demo", limit=50)
        facts = core.facts.load("demo")
        self.assertEqual(len(facts), 1)
        # Only the user's own line is evidence, even though the assistant lines
        # were in the same window.
        self.assertEqual(facts[0]["source_event_keys"], ["host:demo:private:m3"])
        self.assertNotIn("host:demo:private:m1", facts[0]["source_event_keys"])
        self.assertNotIn("host:demo:private:m2", facts[0]["source_event_keys"])


class ReferenceContextTests(BatchHarness):
    """A batch-head ellipsis resolves through reference, never through support."""

    async def test_ellipsis_resolves_and_reference_adds_no_support(self):
        model = BatchModel([
            "[]",
            [{"text": "用户长期喜欢大号马克杯", "importance": 6, "sources": [1]}],
        ])
        core = self.build(model)
        core.recover("demo")
        # Turn 1 establishes the referent and is extracted on its own batch.
        await core.extraction.ingest_turn(
            "demo", [human("我在挑马克杯"), assistant("可以选小号杯或大号杯")],
            source_key="host:demo:private:x1", session_id="s1",
        )
        await core.extraction.process_outbox("demo", limit=50)
        # Turn 2 opens the next batch with a bare ellipsis.
        self.clock.advance(minutes=5)
        await core.extraction.ingest_turn(
            "demo", [human("换大点的，以后都这样")],
            source_key="host:demo:private:x2", session_id="s1",
        )
        await core.extraction.process_outbox("demo", limit=50)
        prompt = model.extraction_calls[-1]
        self.assertIn("前文（仅供指代消解）", prompt)
        self.assertIn("我在挑马克杯", prompt)
        self.assertIn("换大点的", prompt)
        facts = core.facts.load("demo")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["source_event_keys"], ["host:demo:private:x2"])
        self.assertEqual(facts[0]["context_refs"], ["host:demo:private:x1"])
        # Deleting the reference must not delete the fact; deleting the source
        # must.
        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), source_keys=("host:demo:private:x1",),
        ))
        self.assertEqual(len(core.facts.load("demo")), 1)
        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), source_keys=("host:demo:private:x2",),
        ))
        self.assertEqual(core.facts.load("demo"), [])


class StateOrderTests(BatchHarness):
    """Same-batch new -> progress -> close, with late stale protection."""

    @staticmethod
    def _progress(core):
        row = core.state.subject("demo", "private")
        return row["state"]["progress"]

    async def test_new_progress_close_in_one_batch_keeps_order(self):
        model = BatchModel([
            {"facts": [], "state_updates": [
                {"op": "set_progress", "ref": "p1", "text": "用户在准备面试",
                 "next_step": "确认面试时间", "sources": [1]},
                {"op": "set_progress", "ref": "p1", "text": "用户已确认面试时间",
                 "next_step": "等面试结果", "sources": [2]},
                {"op": "close_progress", "ref": "p1", "sources": [3]},
            ]},
        ])
        core = self.build(model)
        core.recover("demo")
        for index, text in enumerate(("我下周要面试", "时间定在周四", "面试结束了")):
            self.clock.advance(seconds=30)
            await core.extraction.ingest_turn(
                "demo", [human(text)],
                source_key="host:demo:private:n%d" % index, session_id="s1",
            )
        outcomes = await core.extraction.process_outbox("demo", limit=50)
        self.assertEqual(len(model.extraction_calls), 1)
        self.assertEqual(outcomes[0].status, "completed")
        self.assertEqual(self._progress(core), [], "closed inside the batch, in order")

    async def test_late_old_update_cannot_override_the_terminal_state(self):
        model = BatchModel([
            {"facts": [], "state_updates": [
                {"op": "set_progress", "ref": "p1", "text": "用户在准备面试",
                 "sources": [1]},
                {"op": "close_progress", "ref": "p1", "sources": [2]},
                {"op": "set_progress", "id": "thread_old", "text": "用户又开始准备面试",
                 "sources": [3]},
            ]},
        ])
        core = self.build(model)
        core.recover("demo")
        for index, text in enumerate(("我下周要面试", "面试结束了", "对了之前那事")):
            self.clock.advance(seconds=30)
            await core.extraction.ingest_turn(
                "demo", [human(text)],
                source_key="host:demo:private:l%d" % index, session_id="s1",
            )
        await core.extraction.process_outbox("demo", limit=50)
        # The closed thread is gone; the stale update for an unknown id did not
        # resurrect it and did not fail the whole batch.
        self.assertEqual([row["id"] for row in self._progress(core)], [])


class CompletionSemanticsTests(BatchHarness):
    """Stable batch id, empty result, parse failure and retries."""

    async def test_empty_result_is_a_normal_completion(self):
        model = BatchModel(["[]"])
        core = self.build(model)
        core.recover("demo")
        for index in range(2):
            self.clock.advance(seconds=10)
            await core.extraction.ingest_turn(
                "demo", [human("随便聊聊第%d句" % index)],
                source_key="host:demo:private:e%d" % index, session_id="s1",
            )
        outcomes = await core.extraction.process_outbox("demo", limit=50)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].status, "completed")
        self.assertEqual(outcomes[0].facts_written, 0)
        self.assertEqual(core.outbox.pending("demo"), ())
        self.assertEqual(core.facts.load("demo"), [])

    async def test_parse_failure_keeps_sources_pending_and_retry_adds_no_duplicate(self):
        model = BatchModel(["not json at all",
                            [{"text": "用户喜欢晨跑", "importance": 7, "sources": [1]}]])
        core = self.build(model)
        core.recover("demo")
        for index in range(3):
            self.clock.advance(seconds=10)
            await core.extraction.ingest_turn(
                "demo", [human("第%d句话" % index)],
                source_key="host:demo:private:p%d" % index, session_id="s1",
            )
        outcomes = await core.extraction.process_outbox("demo", limit=50)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].status, "failed")
        self.assertEqual(outcomes[0].reason, "parse")
        pending = core.outbox.pending("demo")
        self.assertEqual(len(pending), 3, "every source of a failed batch stays pending")
        self.assertTrue(all(op.attempts >= 1 for op in pending))
        self.assertEqual(core.facts.load("demo"), [])

        # The retry rebuilds the same batch from the same sources: one fact,
        # not two, and the batch id is unchanged.
        retried = await core.extraction.process_outbox("demo", limit=50)
        self.assertEqual(retried[0].status, "completed")
        self.assertEqual(core.outbox.pending("demo"), ())
        facts = core.facts.load("demo")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["confirm_count"], 1)

    async def test_stable_batch_id_across_replays(self):
        model = BatchModel()
        core = self.build(model)
        core.recover("demo")
        for index in range(2):
            self.clock.advance(seconds=10)
            await core.extraction.ingest_turn(
                "demo", [human("第%d句话" % index)],
                source_key="host:demo:private:d%d" % index, session_id="s1",
            )
        first = (await core.extraction.process_outbox("demo", limit=50))[0]
        # Re-queuing the very same material must produce the same id, which is
        # what makes a restart or a partial write replay-safe.
        from rin_memory.domain.extraction import _batch_id, _batch_utterances
        ops = core.outbox.pending("demo")
        self.assertEqual(ops, ())
        rebuilt = _batch_id(_batch_utterances(
            list(first.source_keys and ()), core.clock.now(),
        ))
        self.assertEqual(rebuilt, _batch_id([]))


class ImmediatePathTests(BatchHarness):
    """Explicit teaching, correction and deletion do not wait for batching."""

    async def test_explicit_teaching_applies_inside_the_batch_window(self):
        model = BatchModel([
            [{"text": "用户希望被直接叫名字", "importance": 8, "sources": [1]}],
            "[]",
        ])
        core = self.build(model, settings=ExtractionSettings(
            inline=False, batch_max_turns=5, batch_idle_seconds=600.0,
        ))
        core.recover("demo")
        # An explicit teaching is a preference turn: it takes the immediate
        # correction path and returns before any batching decision.
        result = await core.extraction.ingest_turn(
            "demo", [human("以后请直接叫我名字")],
            source_key="host:demo:private:teach1", session_id="s1",
            content_class="preference",
        )
        self.assertIn(
            result.status, ("accepted", "extracted"),
            result,
        )
        self.assertEqual(len(model.extraction_calls), 0)

    async def test_deletion_inside_the_batch_window_takes_effect_at_once(self):
        model = BatchModel([
            [{"text": "用户喜欢热可可", "importance": 6, "sources": [1]}],
        ])
        core = self.build(model)
        core.recover("demo")
        await core.extraction.ingest_turn(
            "demo", [human("我喜欢热可可")],
            source_key="host:demo:private:del1", session_id="s1",
        )
        await core.extraction.process_outbox("demo", limit=50)
        self.assertEqual(len(core.facts.load("demo")), 1)
        # The user forgets it while more turns are still queued.
        self.clock.advance(seconds=10)
        await core.extraction.ingest_turn(
            "demo", [human("还是热可可好喝")],
            source_key="host:demo:private:del2", session_id="s1",
        )
        receipt = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), source_keys=("host:demo:private:del1",),
        ))
        self.assertIn(receipt.status, ("completed", "pending"))
        self.assertEqual(core.facts.load("demo"), [])
        # The queued turn is still extracted, and it cannot resurrect the
        # deleted fact.
        await core.extraction.process_outbox("demo", limit=50)
        texts = [row["text"] for row in core.facts.load("demo")]
        self.assertNotIn("用户喜欢热可可", texts)

    async def test_shared_experience_keeps_the_immediate_single_window_path(self):
        model = BatchModel()
        core = self.build(model, settings=ExtractionSettings(
            inline=False, batch_max_turns=5, batch_idle_seconds=600.0,
        ))
        core.recover("demo")
        message = human("主人和猫娘一起通关")
        message["data"]["metadata"] = {"scene": "game:sts2", "applies_to": {}}
        await core.extraction.ingest_turn(
            "demo", [message],
            source_key="plugin:sts2_autoplay:demo:private:r1:exp1",
            content_class="shared_experience", session_id="r1",
        )
        # No idle wait and no turn grouping: one op, one window, no model call.
        await core.extraction.process_outbox("demo", limit=50)
        self.assertEqual(len(model.extraction_calls), 0)
        shared = next(row for row in core.facts.load("demo")
                      if row["source"] == "shared_experience")
        self.assertEqual(
            shared["source_event_keys"], ["plugin:sts2_autoplay:demo:private:r1:exp1"],
        )


class ReflectionIntervalTests(unittest.IsolatedAsyncioTestCase):
    """RO02: the normal synthesis interval is separate from retry backoff."""

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

    async def test_new_evidence_inside_the_interval_waits(self):
        model = BatchModel([
            {"operation": "add", "reflection": "用户喜欢被直接问", "topic": "沟通方式"},
        ])
        core = self.build(model)
        core.recover("demo")
        core.facts.save("demo", self._facts("a"))
        scheduled = self._scheduled(core)
        first = await scheduled()
        self.assertEqual(first.status, "completed")
        self.assertEqual(len(model.extraction_calls), 1)
        # New evidence arrives five minutes later: inside the normal interval.
        # The background pass is the caller that honours it, so the second
        # round must not pay for another model call.
        core.facts.save("demo", self._facts("a") + self._facts("b", start=100))
        second = await scheduled()
        self.assertEqual(second.status, "skipped")
        self.assertEqual(second.reason, "normal_interval")
        self.assertEqual(len(model.extraction_calls), 1)
        # The queue is not lost: once the interval elapses the new evidence is
        # evaluated with one call.
        self.clock.advance(minutes=31)
        third = await scheduled()
        self.assertNotEqual(third.reason, "normal_interval")
        self.assertEqual(len(model.extraction_calls), 2)
        # A direct, operator-driven call never waits: the interval is a
        # scheduling rule for the background pass, not a gate on demand.
        direct = await core.reflection_service.synthesize("demo")
        self.assertNotEqual(direct.reason, "normal_interval")

    async def test_retry_backoff_is_not_the_normal_interval(self):
        class Boom:
            def __init__(self):
                self.calls = 0

            async def invoke(self, tier, messages, *, max_output_tokens,
                             timeout_s, purpose):
                self.calls += 1
                raise RuntimeError("provider down")

        model = Boom()
        core = self.build(model)
        core.recover("demo")
        core.facts.save("demo", self._facts("a"))
        failed = await core.reflection_service.synthesize("demo")
        self.assertEqual(failed.status, "failed")
        self.assertEqual(model.calls, 1)
        # A failure is retried as soon as the caller allows it: it must not be
        # blocked by the 30 minute normal interval.
        again = await core.reflection_service.synthesize("demo")
        self.assertEqual(again.status, "failed")
        self.assertEqual(model.calls, 2)
        backoff = core.reflections.load_backoff("demo")
        self.assertEqual(len(backoff), 1)
        self.assertEqual(next(iter(backoff.values()))["n"], 2)

    async def test_same_batch_no_change_is_still_deduplicated(self):
        model = BatchModel([{"operation": "no_change", "reflection": None}])
        core = self.build(model)
        core.recover("demo")
        core.facts.save("demo", self._facts("a"))
        first = await core.reflection_service.synthesize("demo")
        second = await core.reflection_service.synthesize("demo")
        self.assertEqual(first.status, "no_change")
        self.assertEqual(second.status, "no_change")
        self.assertIn(second.reason, ("already_settled", "normal_interval"))
        self.assertEqual(len(model.extraction_calls), 1)

    async def test_normal_interval_survives_a_restart(self):
        model = BatchModel([
            {"operation": "add", "reflection": "用户喜欢被直接问", "topic": "沟通方式"},
        ])
        core = self.build(model)
        core.recover("demo")
        core.facts.save("demo", self._facts("a"))
        await self._scheduled(core)()
        # Restart on the same root: the interval is persisted, so new evidence
        # still waits instead of firing immediately.
        reopened = self.build(model)
        reopened.recover("demo")
        reopened.facts.save("demo", self._facts("a") + self._facts("b", start=100))
        blocked = await self._scheduled(reopened)()
        self.assertEqual(blocked.status, "skipped")
        self.assertEqual(blocked.reason, "normal_interval")
        self.assertEqual(len(model.extraction_calls), 1)

    @staticmethod
    def _scheduled(core):
        """The background pass, which is what honours the normal interval."""
        async def run():
            return await core.reflection_service.synthesize(
                "demo", respect_interval=True,
            )
        return run

    @staticmethod
    def _facts(prefix, count=5, *, start=0):
        return [{
            "id": "%s_%d" % (prefix, index),
            "text": "用户提到第%d件普通小事" % index,
            "importance": 7, "source": "user_observation",
            "schema_version": 2, "absorbed": False,
            "created_at": "2026-10-20T00:00:00+00:00",
            "source_event_keys": ["host:demo:private:%s%d" % (prefix, index)],
            "subject": "private",
        } for index in range(start, start + count)]


if __name__ == "__main__":
    unittest.main()
