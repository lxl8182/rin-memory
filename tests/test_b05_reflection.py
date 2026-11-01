"""B05 reflection/evidence/promotion/growth acceptance over the public core.

The batch is judged on host-shaped storage plus behaviour: facts form grounded
reflections (deterministic id, no double absorption), model failures back off
and dead-letter instead of writing garbage, evidence decays at read time and
drives the 1.0/2.0 thresholds (with the host's 7/7 day fallback when powerful
memory is off), surfaced feedback moves evidence, growth items need independent
support before promotion, and a deletion/correction withdraws derived persona
content without ever resurrecting it.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.contracts import (
    CorrectionRequest,
    DeletionRequest,
    DeletionTarget,
    Scope,
)
from rin_memory.domain.reflection import (
    ReflectionSettings,
    reflection_id_from_topic,
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


class FakeModel:
    """Deterministic model stand-in; records calls and can fail on purpose."""

    def __init__(self, *, synthesis=None, growth=False, fail_times=0, hook=None):
        self.calls = []
        self._synthesis = synthesis
        self._growth = growth
        self._fail_times = fail_times
        self._hook = hook

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls.append({
            "tier": tier, "purpose": purpose, "max_output_tokens": max_output_tokens,
            "timeout_s": timeout_s,
        })
        if self._hook is not None:
            await self._hook(self, purpose)
        if purpose == "reflection_synthesis":
            if self._fail_times > 0:
                self._fail_times -= 1
                raise RuntimeError("provider down")
            if self._synthesis is not None:
                return self._synthesis
            payload = {
                "operation": "add",
                "reflection": "主人喜欢被直接问",
                "topic": "沟通方式",
                "entity": "relationship",
            }
            if self._growth:
                payload.update({
                    "memory_kind": "habit",
                    "claim": "用户周末早晨会晨跑",
                    "situation": "周末早晨",
                })
            return json.dumps(payload, ensure_ascii=False)
        return "{}"


def facts(count=5, *, importance=7, prefix="fact", evidence_kind=None):
    rows = [
        {
            "id": f"{prefix}_{index}",
            "text": f"用户喜欢第{index}件事",
            "importance": importance,
            "source": "user_observation",
            "schema_version": 2,
            "absorbed": False,
            "created_at": "2026-10-20T00:00:00+00:00",
            "source_event_keys": [f"host:demo:private:e{index}"],
        }
        for index in range(count)
    ]
    if evidence_kind is not None:
        for row in rows:
            row["evidence_kind"] = evidence_kind
    return rows


def rows_id_of(outcome):
    return outcome.reflection_ids[0] if outcome.reflection_ids else ""


class ReflectionFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = MutableClock()
        self.model = FakeModel()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, model=None, **kwargs):
        return build_memory_core(
            memory_root=self.root / "memory", clock=self.clock,
            model=self.model if model is None else model, **kwargs,
        )

    async def test_synthesis_threshold_idempotent_and_absorbed(self):
        core = self.build()
        core.recover("demo")
        skipped = await core.reflection_service.synthesize("demo")
        self.assertEqual(skipped.status, "skipped")
        self.assertEqual(skipped.reason, "insufficient_facts")
        self.assertEqual(self.model.calls, [])

        core.facts.save("demo", facts(5))
        first = await core.reflection_service.synthesize("demo")
        self.assertEqual(first.status, "completed")
        # The id is content-addressed since the reflection-layer rework
        # (topic + scope + text hash); it used to be a hash of the batch's fact
        # ids, which made every new batch a new row.
        expected = reflection_id_from_topic("沟通方式", {}, "主人喜欢被直接问")
        self.assertEqual(first.reflection_ids, (expected,))
        self.assertEqual(rows_id_of(first), expected)
        self.assertEqual(len(self.model.calls), 1)
        call = self.model.calls[0]
        self.assertEqual(call["purpose"], "reflection_synthesis")
        self.assertEqual(call["tier"], "summary")
        self.assertGreater(call["max_output_tokens"], 0)
        self.assertGreater(call["timeout_s"], 0)
        rows = core.reflections.load("demo")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_fact_ids"], [f"fact_{index}" for index in range(5)])
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["schema_version"], 2)
        self.assertTrue(all(row["absorbed"] for row in core.facts.load("demo")))

        again = await core.reflection_service.synthesize("demo")
        self.assertEqual(again.status, "skipped")
        self.assertEqual(len(self.model.calls), 1)

    async def test_model_failure_backoff_and_dead_letter(self):
        self.model = FakeModel(fail_times=5)
        core = self.build()
        core.recover("demo")
        core.facts.save("demo", facts())
        for attempt in range(1, 6):
            outcome = await core.reflection_service.synthesize("demo")
            self.assertEqual(outcome.status, "failed")
            backoff = core.reflections.load_backoff("demo")
            self.assertEqual(len(backoff), 1)
            self.assertEqual(next(iter(backoff.values()))["n"], attempt)
        self.assertEqual(len(self.model.calls), 5)

        dead = await core.reflection_service.synthesize("demo")
        self.assertEqual(dead.status, "skipped")
        self.assertEqual(dead.reason, "dead_letter")
        self.assertEqual(len(self.model.calls), 5)
        self.assertEqual(core.reflections.load("demo"), [])

        # Self-heal window passed: the batch is retried (and now succeeds).
        self.clock.advance(hours=6)
        healed = await core.reflection_service.synthesize("demo")
        self.assertEqual(healed.status, "completed")
        self.assertEqual(len(core.reflections.load("demo")), 1)

    async def test_evidence_decay_and_protected(self):
        core = self.build()
        core.recover("demo")
        now = self.clock.now().isoformat()
        row = {
            "id": "ref_evidence", "text": "证据衰减", "status": "confirmed",
            "reinforcement": 0.8, "rein_last_signal_at": now,
            "disputation": 0.2, "disp_last_signal_at": now,
        }
        core.reflections.save("demo", [row])
        self.assertAlmostEqual(
            core.reflection_service.evidence_score("demo", row), 0.6, places=6,
        )
        self.clock.advance(days=30)
        decayed = core.reflection_service.evidence_score("demo", row)
        self.assertAlmostEqual(decayed, 0.4 - 0.2 * 0.5 ** (30 / 180), places=6)
        protected = dict(row, protected=True)
        self.assertEqual(core.reflection_service.evidence_score("demo", protected), float("inf"))

    async def test_score_promotion_writes_persona_once(self):
        core = self.build()
        core.recover("demo")
        core.facts.save("demo", facts())
        synth = await core.reflection_service.synthesize("demo")
        reflection_id = synth.reflection_ids[0]
        rows = core.reflections.load("demo")
        rows[0]["reinforcement"] = 2.5
        rows[0]["rein_last_signal_at"] = self.clock.now().isoformat()
        core.reflections.save("demo", rows)

        first = await core.reflection_service.promote("demo")
        self.assertIn(reflection_id, first.confirmed_ids)
        second = await core.reflection_service.promote("demo")
        self.assertIn(reflection_id, second.promoted_ids)
        expected_persona_id = "prom_" + reflection_id
        self.assertEqual(second.persona_ids, (expected_persona_id,))
        stored = core.reflections.load("demo")[0]
        self.assertEqual(stored["status"], "promoted")
        self.assertEqual(stored["absorbed_into"], expected_persona_id)
        entries = core.persona_service.entries("demo")
        self.assertEqual([entry["id"] for entry in entries], [expected_persona_id])
        self.assertIn("主人喜欢被直接问", core.persona_service.render_block("demo"))

        third = await core.reflection_service.promote("demo")
        self.assertEqual(third.promoted_ids, ())
        self.assertEqual(len(core.persona_service.entries("demo")), 1)

    async def test_time_driven_promotion_without_powerful_memory(self):
        model = FakeModel()
        core = self.build(
            model=model,
            reflection_settings=ReflectionSettings(powerful_memory=False),
        )
        core.recover("demo")
        core.facts.save("demo", facts())
        synth = await core.reflection_service.synthesize("demo")
        reflection_id = synth.reflection_ids[0]

        self.clock.advance(days=8)
        confirm = await core.reflection_service.promote("demo")
        self.assertIn(reflection_id, confirm.confirmed_ids)
        self.assertEqual(confirm.promoted_ids, ())

        self.clock.advance(days=8)
        promote = await core.reflection_service.promote("demo")
        self.assertIn(reflection_id, promote.promoted_ids)
        self.assertEqual([call["purpose"] for call in model.calls], ["reflection_synthesis"])
        self.assertEqual(len(core.persona_service.entries("demo")), 1)

    async def test_surfaced_cooldown_and_feedback(self):
        core = self.build()
        core.recover("demo")
        core.facts.save("demo", facts())
        synth = await core.reflection_service.synthesize("demo")
        reflection_id = synth.reflection_ids[0]

        surfaced = await core.reflection_service.record_surfaced("demo", reflection_id)
        self.assertEqual(surfaced["status"], "completed")
        self.assertIn(reflection_id, [row["reflection_id"] for row in core.reflections.load_surfaced("demo")])
        row = core.reflections.load("demo")[0]
        self.assertGreaterEqual(
            datetime.fromisoformat(row["next_eligible_at"]), self.clock.now(),
        )
        again = await core.reflection_service.record_surfaced("demo", reflection_id)
        self.assertEqual(again["status"], "completed")
        self.assertEqual(len(core.reflections.load_surfaced("demo")), 1)

        feedback = await core.reflection_service.record_feedback(
            "demo", reflection_id, "confirmed",
        )
        self.assertEqual(feedback["feedback"], "confirmed")
        self.assertEqual(core.reflections.load_surfaced("demo")[0]["feedback"], "confirmed")
        self.assertGreater(core.reflections.load("demo")[0]["reinforcement"], 0.2)
        with self.assertRaises(ValueError):
            await core.reflection_service.record_feedback("demo", reflection_id, "maybe")

        denied = await core.reflection_service.record_feedback("demo", reflection_id, "denied")
        self.assertEqual(denied["feedback"], "denied")
        self.assertEqual(core.reflections.load("demo")[0]["status"], "denied")
        self.assertEqual(core.reflection_service.visible("demo"), [])

    async def test_growth_support_usage_and_retract(self):
        model = FakeModel(growth=True)
        core = self.build(model=model)
        core.recover("demo")
        # M4: a growth candidate needs real evidence. Bare user statements
        # (evidence_kind=user_assertion) no longer open the habit gate, so this
        # fixture records what actually happened in the conversation.
        core.facts.save("demo", facts(evidence_kind="observed_action"))
        synth = await core.reflection_service.synthesize("demo")
        self.assertEqual(len(synth.growth_item_ids), 1)
        item_id = synth.growth_item_ids[0]
        self.assertEqual(core.reflection_service.growth_items("demo"), [])

        promoted = await core.reflection_service.promote("demo")
        self.assertIn(item_id, promoted.promoted_ids)
        claim = "用户周末早晨会晨跑"
        import hashlib
        digest = hashlib.sha1(f"{item_id}|{claim}".encode("utf-8")).hexdigest()[:16]
        expected_persona_id = "prom_growth_" + digest
        self.assertIn(expected_persona_id, promoted.persona_ids)
        self.assertEqual(
            [entry["id"] for entry in core.persona_service.entries("demo")],
            [expected_persona_id],
        )
        block = core.reflection_service.growth_block("demo")
        self.assertIn(claim, block.block)
        self.assertEqual(block.version, 0)

        first_use = await core.reflection_service.record_growth_usage(
            "demo", item_id, "read", source_key="chat:1",
        )
        self.assertEqual(first_use.version, 1)
        self.assertFalse(first_use.duplicate)
        duplicate = await core.reflection_service.record_growth_usage(
            "demo", item_id, "read", source_key="chat:1",
        )
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.version, 1)
        adopted = await core.reflection_service.record_growth_usage(
            "demo", item_id, "adopted", source_key="chat:2",
        )
        self.assertEqual(adopted.version, 2)
        self.assertEqual(core.reflection_service.growth_block("demo").version, 2)

        retracted = await core.reflection_service.retract_growth("demo", item_id)
        self.assertTrue(retracted["ok"])
        self.assertEqual(core.reflection_service.growth_items("demo"), [])
        self.assertEqual(core.reflection_service.growth_block("demo").items, ())
        self.assertEqual(core.persona_service.entries("demo"), [])
        duplicate_retract = await core.reflection_service.retract_growth("demo", item_id)
        self.assertTrue(duplicate_retract["duplicate"])

    async def test_deletion_narrows_and_retracts_derived_persona(self):
        core = self.build()
        core.recover("demo")
        core.facts.save("demo", facts(2))
        now = self.clock.now().isoformat()
        reflection = {
            "id": "ref_multi", "text": "主人喜欢被直接问", "status": "confirmed",
            "entity": "relationship", "source_fact_ids": ["fact_0", "fact_1"],
            "reinforcement": 2.5, "rein_last_signal_at": now, "created_at": now,
        }
        core.reflections.save("demo", [reflection])
        core.persona_service.add_reflection("demo", reflection)

        receipt = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), targets=(DeletionTarget("fact", "fact_0"),),
            reason="partial deletion",
        ))
        self.assertEqual(receipt.status, "completed")
        self.assertIn("reflections", receipt.completed)
        self.assertIn("persona", receipt.completed)
        narrowed = core.reflections.load("demo")
        self.assertEqual(len(narrowed), 1)
        self.assertEqual(narrowed[0]["source_fact_ids"], ["fact_1"])
        self.assertIn("fact_0", narrowed[0]["invalidated_sources"])
        self.assertTrue(narrowed[0]["needs_rebuild"])
        self.assertNotIn("主人喜欢被直接问", core.persona_service.render_block("demo"))

        second = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), targets=(DeletionTarget("fact", "fact_1"),),
            reason="full deletion",
        ))
        self.assertEqual(second.status, "completed")
        self.assertEqual(core.reflections.load("demo"), [])
        self.assertEqual(core.persona_service.entries("demo"), [])

        reopened = build_memory_core(memory_root=self.root / "memory", clock=self.clock, model=self.model)
        reopened.recover("demo")
        self.assertEqual(reopened.reflections.load("demo"), [])

    async def test_correction_invalidates_derived_reflection_and_persona(self):
        core = self.build()
        core.recover("demo")
        core.facts.save("demo", [{
            "id": "fact_tokyo", "text": "用户以前住在东京", "importance": 8,
            "schema_version": 2, "absorbed": False,
        }])
        reflection = {
            "id": "ref_tokyo", "text": "用户以前住在东京", "status": "confirmed",
            "entity": "relationship", "source_fact_ids": ["fact_tokyo"],
            "reinforcement": 1.5, "created_at": self.clock.now().isoformat(),
        }
        core.reflections.save("demo", [reflection])
        core.persona_service.add_reflection("demo", reflection)

        receipt = await core.correction_service.correct(CorrectionRequest(
            scope=Scope("demo"), old_text="以前住在东京", new_text="现在住在大阪",
        ))
        self.assertIn("fact_tokyo", receipt.invalidated_fact_ids)
        self.assertIn("ref_tokyo", receipt.invalidated_reflection_ids)
        self.assertIn("prom_ref_tokyo", receipt.invalidated_persona_ids)
        stored = core.reflections.load("demo")[0]
        self.assertEqual(stored["status"], "withdrawn")
        self.assertEqual(core.persona_service.entries("demo"), [])
        self.assertEqual(core.reflection_service.visible("demo"), [])

        retracted = await core.correction_service.retract("demo", receipt.correction_id)
        self.assertFalse(retracted.applied)
        self.assertEqual(core.reflections.load("demo")[0]["status"], "withdrawn")
        self.assertEqual(core.persona_service.entries("demo"), [])

    async def test_late_synthesis_result_is_discarded_after_deletion(self):
        holder = {}

        async def hook(model, purpose):
            if purpose != "reflection_synthesis":
                return
            core = holder["core"]
            await core.deletion_service.forget(DeletionRequest(
                scope=Scope("demo"),
                targets=(DeletionTarget("fact", "fact_0"),),
                reason="delete while model runs",
            ))

        model = FakeModel(hook=hook)
        core = self.build(model=model)
        holder["core"] = core
        core.recover("demo")
        core.facts.save("demo", facts())
        outcome = await core.reflection_service.synthesize("demo")
        self.assertEqual(outcome.status, "stale")
        self.assertEqual(core.reflections.load("demo"), [])

    async def test_promotion_survives_reopen_and_full_replay(self):
        core = self.build()
        core.recover("demo")
        core.facts.save("demo", facts())
        synth = await core.reflection_service.synthesize("demo")
        reflection_id = synth.reflection_ids[0]
        rows = core.reflections.load("demo")
        rows[0]["reinforcement"] = 2.5
        rows[0]["rein_last_signal_at"] = self.clock.now().isoformat()
        core.reflections.save("demo", rows)
        await core.reflection_service.promote("demo")
        await core.reflection_service.promote("demo")

        reopened = build_memory_core(memory_root=self.root / "memory", clock=self.clock, model=self.model)
        reopened.recover("demo")
        self.assertEqual(reopened.reflections.load("demo")[0]["status"], "promoted")
        self.assertEqual(len(reopened.persona_service.entries("demo")), 1)

        # Force a full replay (sentinel lost) and prove the handlers are idempotent.
        sentinel = reopened.roots.character_dir("demo", create=False) / "events_applied.json"
        sentinel.unlink()
        replayed = build_memory_core(memory_root=self.root / "memory", clock=self.clock, model=self.model)
        replayed.recover("demo")
        self.assertEqual(replayed.reflections.load("demo")[0]["status"], "promoted")
        self.assertEqual(len(replayed.persona_service.entries("demo")), 1)


if __name__ == "__main__":
    unittest.main()
