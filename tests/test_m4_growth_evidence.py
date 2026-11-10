"""M4 acceptance: growth evidence - independent experiences and real evidence types.

Design basis: companion-growth-design.md sections 3.2, 7.4, 9.3 and G09/G15/G16.

The three defects this batch removes:

1. support was counted per *message* (`support_count = len(source_event_keys)`),
   so one heart-to-heart split into five facts looked like five independent
   experiences and cleared the "two independent experiences" gate at once (G09);
2. every source key was filed under `actual_result` no matter what it rested on,
   so habit/experience passed their own gate by construction. Flags now come
   from the M1 `fact.evidence_kind`;
3. the deletion path recomputed `support_count = len(kept)`, i.e. per message
   again, so a row could be created per episode and narrowed per message. Both
   paths now go through `domain.growth_evidence`, so one experience can never
   be worth two supports in one place and half a support in the other.

Real constraints kept: the deletion barrier semantics (withdrawn / narrowed), the
evidence decay half-lives, `PROMOTION_RULES` and the M3 superseded/withdrawn
split are untouched. Nothing here reads or writes a real runtime root.
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
from rin_memory.domain.evolution import EvolutionSettings
from rin_memory.domain.growth_evidence import (
    EVIDENCE_KIND_FLAGS,
    apply_support_removal_to_row,
    eligibility_of_row,
    evidence_dto,
    evidence_flags_of,
    evidence_kind_of,
    maturity_of_row,
    support_basis_from_facts,
    support_basis_from_row,
    summarize_basis,
)
from rin_memory.domain.reflection import PROMOTION_RULES


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


class ScriptedModel:
    """Returns one scripted synthesis payload and records every prompt."""

    def __init__(self, payload):
        self.calls = []
        self.prompts = []
        self._payload = payload

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls.append({"purpose": purpose, "messages": messages})
        self.prompts.append(messages[0]["content"] if messages else "")
        if purpose not in ("reflection_synthesis", "persona_evolution_proposal"):
            return "{}"
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload, ensure_ascii=False)


def five_over_two_episodes(*, kind="observed_action", source="user_observation",
                           key_prefix="host:demo:private:k"):
    """Five facts of one *conversation pair*: 3 in episode e1, 2 in e2.

    The synthesis threshold is five unabsorbed facts (a real default, not
    lowered for the test), so a two-experience batch has to contain five facts
    spread over two episodes.
    """
    rows = []
    for index in range(5):
        episode = "ep:e1" if index < 3 else "ep:e2"
        rows.append(fact(
            "fact_%d" % index, "用户周末晨跑第%d次" % index,
            episode_id=episode,
            keys=["%s%s" % (key_prefix, "1" if index < 3 else "2")],
            kind=kind, source=source,
        ))
    return rows


def fact(fact_id, text, *, episode_id=None, keys=None, kind="user_assertion",
         source="user_observation", importance=7, scene=None):
    row = {
        "id": fact_id,
        "text": text,
        "importance": importance,
        "source": source,
        "schema_version": 2,
        "absorbed": False,
        "created_at": "2026-10-20T00:00:00+00:00",
        "source_event_keys": list(keys if keys is not None else ["host:demo:private:" + fact_id]),
        "evidence_kind": kind,
        "subject": "private",
    }
    if episode_id:
        row["episode_id"] = episode_id
    if scene:
        row["applies_to"] = scene
    return row


HABIT_OP = {
    "operation": "add",
    "reflection": "用户周末早晨会晨跑",
    "topic": "运动",
    "entity": "master",
    "memory_kind": "habit",
    "claim": "用户周末早晨会晨跑",
    "situation": "周末早晨",
}


class EvidenceKindMappingTests(unittest.TestCase):
    """The mapping table is the contract; pin it down explicitly."""

    def test_each_kind_maps_to_the_documented_flags(self):
        self.assertEqual(EVIDENCE_KIND_FLAGS["user_assertion"], ())
        self.assertEqual(EVIDENCE_KIND_FLAGS["explicit_feedback"], ("user_feedback",))
        self.assertEqual(
            EVIDENCE_KIND_FLAGS["observed_action"], ("actual_result", "real_contact"),
        )
        self.assertEqual(
            EVIDENCE_KIND_FLAGS["tool_result"], ("actual_result", "real_contact"),
        )
        self.assertEqual(EVIDENCE_KIND_FLAGS["agent_expression"], ())
        self.assertEqual(EVIDENCE_KIND_FLAGS["quoted_example"], ())

    def test_flags_are_derived_from_the_fact_not_defaulted(self):
        self.assertEqual(evidence_flags_of(fact("f", "x", kind="user_assertion")), ())
        self.assertEqual(
            evidence_flags_of(fact("f", "x", kind="explicit_feedback")),
            ("user_feedback",),
        )
        self.assertIn(
            "actual_result", evidence_flags_of(fact("f", "x", kind="observed_action")),
        )
        # A shared experience is two-sided by construction.
        self.assertIn(
            "both_party",
            evidence_flags_of(fact("f", "x", kind="observed_action",
                                   source="shared_experience")),
        )

    def test_missing_kind_degrades_to_the_source_and_never_to_actual_result(self):
        legacy = {"id": "f", "text": "x", "source": "user_observation"}
        self.assertEqual(evidence_kind_of(legacy), "user_assertion")
        self.assertEqual(evidence_flags_of(legacy), ())
        self.assertNotIn(
            "actual_result",
            evidence_flags_of({"id": "f", "source": "ai_disclosure"}),
        )

    def test_agent_expression_and_quoted_example_add_no_support(self):
        facts = [
            fact("f1", "她说会陪我", kind="agent_expression", source="ai_disclosure"),
            fact("f2", "别人家的 bot 这样接话", kind="quoted_example"),
        ]
        self.assertEqual(summarize_basis(support_basis_from_facts(facts))["support_count"], 0)


class G09IndependentExperienceTests(unittest.IsolatedAsyncioTestCase):
    """G09: five facts of one episode are one support and stay immature."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = MutableClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, payload=HABIT_OP):
        model = ScriptedModel(payload)
        core = build_memory_core(
            memory_root=self.root / "memory", clock=self.clock, model=model,
        )
        return core, model

    async def test_five_facts_from_one_episode_are_one_support(self):
        core, _model = self.build()
        core.recover("demo")
        facts = [
            fact("fact_%d" % index, "用户周末晨跑第%d次" % index,
                 episode_id="ep:host:e1", keys=["host:demo:private:k1"],
                 kind="observed_action")
            for index in range(5)
        ]
        core.facts.save("demo", facts)
        outcome = await core.reflection_service.synthesize("demo")
        self.assertEqual(outcome.status, "completed")
        rows = core.reflections.load("demo")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Five facts, five keys, ONE conversation.
        self.assertEqual(len(row["source_fact_ids"]), 5)
        self.assertEqual(row["support_count"], 1)
        self.assertEqual(len(row["support_basis"]), 1)
        self.assertEqual(row["status"], "pending")
        # The basis still records that something actually happened.
        self.assertEqual(
            row["support_evidence"]["actual_result"],
            ["host:demo:private:k1"],
        )
        # ... but one experience cannot clear a two-experience gate.
        self.assertFalse(core.reflection_service._growth_promotion_ready(row))
        self.assertEqual(core.reflection_service.growth_items("demo"), [])

    async def test_two_episodes_are_two_supports(self):
        core, _model = self.build()
        core.recover("demo")
        core.facts.save("demo", five_over_two_episodes())
        outcome = await core.reflection_service.synthesize("demo")
        self.assertEqual(outcome.status, "completed")
        row = core.reflections.load("demo")[0]
        self.assertEqual(row["support_count"], 2)
        self.assertTrue(core.reflection_service._growth_promotion_ready(row))
        # The confirmation itself happens on the next promotion pass (the row is
        # written pending by synthesis, as before); what M4 changes is that one
        # experience is no longer enough to reach it.
        promoted = await core.reflection_service.promote("demo")
        self.assertIn(row["id"], promoted.confirmed_ids)
        # The same pass may already materialise it into the persona block; what
        # matters here is that it left "pending" once two experiences existed.
        self.assertIn(
            core.reflections.load("demo")[0]["status"],
            ("confirmed", "promoted"),
        )

    async def test_two_mentions_without_episode_are_two_supports(self):
        """M1 merges sources of independent mentions; without an episode each
        merged key is its own experience (the documented degradation rule)."""
        core, _model = self.build()
        core.recover("demo")
        rows = five_over_two_episodes()
        for row in rows:
            row.pop("episode_id", None)
        core.facts.save("demo", rows)
        await core.reflection_service.synthesize("demo")
        self.assertEqual(core.reflections.load("demo")[0]["support_count"], 2)

    async def test_one_episode_with_merged_keys_counts_once(self):
        """Episode precedence: keys merged in from another mention do not add a
        second experience while the fact still carries its own episode."""
        core, _model = self.build()
        core.recover("demo")
        rows = [
            fact("fact_%d" % index, "用户周末晨跑第%d次" % index,
                 episode_id="ep:host:e1",
                 keys=["host:demo:private:k1", "host:demo:private:k2"],
                 kind="observed_action")
            for index in range(5)
        ]
        core.facts.save("demo", rows)
        await core.reflection_service.synthesize("demo")
        row = core.reflections.load("demo")[0]
        self.assertEqual(row["support_count"], 1)
        self.assertEqual(len(row["support_event_keys"]), 2)

    async def test_bare_assertions_never_open_the_habit_gate(self):
        """The old code filed every key under actual_result; a conversation of
        naked statements could therefore promote a habit."""
        core, _model = self.build()
        core.recover("demo")
        core.facts.save("demo", five_over_two_episodes(kind="user_assertion"))
        await core.reflection_service.synthesize("demo")
        row = core.reflections.load("demo")[0]
        self.assertEqual(row["support_count"], 2)
        self.assertEqual(row["support_evidence"]["actual_result"], [])
        self.assertEqual(row["status"], "pending")
        self.assertFalse(core.reflection_service._growth_promotion_ready(row))

    async def test_feedback_opens_the_habit_gate(self):
        core, _model = self.build()
        core.recover("demo")
        core.facts.save("demo", five_over_two_episodes(kind="explicit_feedback"))
        await core.reflection_service.synthesize("demo")
        row = core.reflections.load("demo")[0]
        self.assertEqual(row["support_count"], 2)
        self.assertEqual(
            row["support_evidence"]["user_feedback"],
            ["host:demo:private:k1", "host:demo:private:k2"],
        )
        self.assertTrue(core.reflection_service._growth_promotion_ready(row))

    async def test_agent_expression_of_one_episode_is_not_support(self):
        core, _model = self.build()
        core.recover("demo")
        core.facts.save("demo", five_over_two_episodes(
            kind="agent_expression", source="ai_disclosure",
        ))
        await core.reflection_service.synthesize("demo")
        row = core.reflections.load("demo")[0]
        self.assertEqual(row["support_count"], 0)
        self.assertFalse(core.reflection_service._growth_promotion_ready(row))


class SupportRemovalRecomputeTests(unittest.IsolatedAsyncioTestCase):
    """Deleting a source recomputes with the *same* per-experience rule."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = MutableClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self):
        core = build_memory_core(
            memory_root=self.root / "memory", clock=self.clock,
            model=ScriptedModel(HABIT_OP),
        )
        return core

    def growth_row(self, *, status="confirmed", episode="ep:host:e1",
                   key="host:demo:private:k1", fact_ids=("fact_a",),
                   kind="observed_action"):
        basis = support_basis_from_facts([
            fact("fact_a", "用户周末晨跑", episode_id=episode, keys=[key],
                 kind=kind),
        ])
        row = {
            "id": "ref_grow",
            "text": "用户周末早晨会晨跑",
            "status": status,
            "memory_kind": "habit",
            "claim": "用户周末早晨会晨跑",
            "situation": "周末早晨",
            "source_fact_ids": list(fact_ids),
            "created_at": "2026-10-20T00:00:00+00:00",
        }
        row.update(summarize_basis(basis))
        return row

    async def test_reconcile_recomputes_per_episode_not_per_message(self):
        core = self.build()
        core.recover("demo")
        basis = support_basis_from_facts([
            fact("fact_a", "用户周末晨跑", episode_id="ep:host:e1",
                 keys=["host:demo:private:k1"], kind="observed_action"),
            fact("fact_b", "用户又晨跑了", episode_id="ep:host:e2",
                 keys=["host:demo:private:k2"], kind="observed_action"),
        ])
        row = {
            "id": "ref_grow", "text": "用户周末早晨会晨跑",
            "status": "confirmed", "memory_kind": "habit",
            "claim": "用户周末早晨会晨跑", "situation": "周末早晨",
            "source_fact_ids": ["fact_a", "fact_b"],
            "created_at": "2026-10-20T00:00:00+00:00",
        }
        row.update(summarize_basis(basis))
        core.reflections.save("demo", [row])
        self.assertEqual(core.reflections.load("demo")[0]["support_count"], 2)

        # One of the two experiences is corrected away.
        core.facts.save("demo", [
            dict(fact("fact_a", "用户周末晨跑", episode_id="ep:host:e1",
                      keys=["host:demo:private:k1"], kind="observed_action"),
                 invalidated_at=self.clock.now().isoformat()),
        ])
        result = await core.reflection_service.reconcile_growth_support("demo")
        self.assertEqual(result["changed"], 1)
        self.assertEqual(result["narrowed"], 1)
        stored = core.reflections.load("demo")[0]
        # Recomputed by experience: 1 left, not 1-per-remaining-key either.
        self.assertEqual(stored["support_count"], 1)
        self.assertEqual(stored["status"], "narrowed")
        self.assertEqual(
            stored["support_event_keys"], ["host:demo:private:k2"],
        )
        self.assertEqual(
            stored["support_evidence"]["actual_result"], ["host:demo:private:k2"],
        )

    async def test_removal_keeps_withdrawn_and_narrowed_semantics(self):
        row = self.growth_row()
        self.assertEqual(row["support_count"], 1)
        self.assertTrue(
            apply_support_removal_to_row(
                row, ["host:demo:private:k1"],
                now_iso="2026-10-27T00:00:00+00:00",
            ),
        )
        self.assertEqual(row["status"], "withdrawn")
        self.assertEqual(row["support_count"], 0)
        self.assertEqual(row["support_basis"], [])
        self.assertEqual(row["support_evidence"]["actual_result"], [])
        self.assertEqual(row["withdrawn_reason"], "sources_deleted")

        two = {
            "id": "ref_two", "text": "用户周末早晨会晨跑", "status": "confirmed",
            "memory_kind": "habit", "claim": "用户周末早晨会晨跑",
            "source_fact_ids": ["fact_a", "fact_b"],
        }
        two.update(summarize_basis(support_basis_from_facts([
            fact("fact_a", "用户周末晨跑", episode_id="ep:e1",
                 keys=["host:demo:private:k1"], kind="observed_action"),
            fact("fact_b", "用户又晨跑", episode_id="ep:e2",
                 keys=["host:demo:private:k2"], kind="observed_action"),
        ])))
        self.assertTrue(apply_support_removal_to_row(
            two, ["host:demo:private:k1"], now_iso="2026-10-27T00:00:00+00:00",
        ))
        self.assertEqual(two["support_count"], 1)
        self.assertEqual(two["status"], "narrowed")
        self.assertEqual(two["narrowed_reason"], "sources_reduced")

    async def test_fact_id_removal_without_key_change_is_counted(self):
        """A fact deleted by id must shed its experience even when its key is
        unchanged (the silent leftover support this rule is here to prevent)."""
        row = {
            "id": "ref_grow", "text": "用户周末早晨会晨跑", "status": "confirmed",
            "memory_kind": "habit", "claim": "用户周末早晨会晨跑",
            "source_fact_ids": ["fact_a", "fact_b"],
        }
        row.update(summarize_basis(support_basis_from_facts([
            fact("fact_a", "用户周末晨跑", episode_id="ep:e1",
                 keys=["host:demo:private:k1"], kind="observed_action"),
            fact("fact_b", "用户又晨跑", episode_id="ep:e2",
                 keys=["host:demo:private:k2"], kind="observed_action"),
        ])))
        changed = apply_support_removal_to_row(
            row, [], invalidated_fact_ids=["fact_a"],
            now_iso="2026-10-27T00:00:00+00:00",
        )
        self.assertTrue(changed)
        self.assertEqual(row["support_count"], 1)
        self.assertEqual(row["source_fact_ids"], ["fact_b"])

    async def test_deletion_cleaner_recomputes_support_the_same_way(self):
        """End to end: delete one source fact of a two-experience strategy."""
        core = self.build()
        core.recover("demo")
        core.facts.save("demo", [
            fact("fact_a", "用户周末晨跑", episode_id="ep:host:e1",
                 keys=["host:demo:private:k1"], kind="observed_action"),
            fact("fact_b", "用户又晨跑", episode_id="ep:host:e2",
                 keys=["host:demo:private:k2"], kind="observed_action"),
        ])
        row = {
            "id": "ref_grow", "text": "用户周末早晨会晨跑",
            "status": "confirmed", "memory_kind": "habit",
            "claim": "用户周末早晨会晨跑", "situation": "周末早晨",
            "source_fact_ids": ["fact_a", "fact_b"],
            "created_at": "2026-10-20T00:00:00+00:00",
        }
        row.update(summarize_basis(support_basis_from_facts(
            core.facts.load("demo"),
        )))
        core.reflections.save("demo", [row])
        core.persona_service.add_growth("demo", row)
        self.assertEqual(len(core.persona_service.entries("demo")), 1)

        receipt = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"),
            targets=(DeletionTarget("fact", "fact_a"),),
            reason="one experience was wrong",
        ))
        self.assertEqual(receipt.status, "completed")
        stored = core.reflections.load("demo")
        self.assertEqual(len(stored), 1)
        narrowed = stored[0]
        self.assertEqual(narrowed["support_count"], 1)
        self.assertEqual(
            narrowed["support_event_keys"], ["host:demo:private:k2"],
        )
        self.assertEqual(
            narrowed["support_evidence"]["actual_result"],
            ["host:demo:private:k2"],
        )
        # Below the two-experience gate: it stops being an adopted strategy and
        # its derived persona entry is retracted.
        self.assertEqual(core.reflection_service.growth_items("demo"), [])
        self.assertEqual(core.persona_service.entries("demo"), [])

    async def test_deleting_the_last_source_withdraws_the_row(self):
        core = self.build()
        core.recover("demo")
        core.facts.save("demo", [
            fact("fact_a", "用户周末晨跑", episode_id="ep:host:e1",
                 keys=["host:demo:private:k1"], kind="observed_action"),
        ])
        row = {
            "id": "ref_grow", "text": "用户周末早晨会晨跑",
            "status": "confirmed", "memory_kind": "habit",
            "claim": "用户周末早晨会晨跑", "situation": "周末早晨",
            "source_fact_ids": ["fact_a"],
            "created_at": "2026-10-20T00:00:00+00:00",
        }
        row.update(summarize_basis(support_basis_from_facts(
            core.facts.load("demo"),
        )))
        core.reflections.save("demo", [row])
        result = await core.reflection_service.reconcile_growth_support("demo")
        self.assertEqual(result["changed"], 0)  # nothing was invalidated yet

        receipt = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"),
            targets=(DeletionTarget("fact", "fact_a"),),
            reason="forget it",
        ))
        self.assertEqual(receipt.status, "completed")
        # The cleaner drops a fully dependent row rather than leaving it as
        # "support 0" evidence; either way it is not an adopted strategy.
        self.assertEqual(core.reflection_service.growth_items("demo"), [])
        self.assertEqual(core.persona_service.entries("demo"), [])


class EvolutionEligibilityTests(unittest.IsolatedAsyncioTestCase):
    """G15 + G16: facts explain, only mature evidence may change the card."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = MutableClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, model):
        return build_memory_core(
            memory_root=self.root / "memory", clock=self.clock, model=model,
            evolution_settings=EvolutionSettings(
                enabled=True, tier="summary", timeout_s=5,
                max_output_tokens=300,
            ),
        )

    def prepare(self, core, *, with_understanding=True):
        core.recover("demo")
        core.persona_card_service.import_initial("demo", [
            {"section_id": "identity", "text": "你是一只猫娘。",
             "editable_by": {"user": True, "evolution": False}},
            {"section_id": "tone", "text": "说话温柔。",
             "editable_by": {"user": True, "evolution": True}},
        ])
        core.facts.save("demo", [
            fact("fact_a", "用户喜欢温柔的语气", keys=["host:demo:private:a"]),
        ])
        if with_understanding:
            core.reflections.save("demo", [{
                "id": "ref_understanding",
                "text": "用户喜欢温柔、具体的语气",
                "status": "confirmed",
                "entity": "relationship",
                "source_fact_ids": ["fact_a", "fact_b"],
                "support_basis": support_basis_from_facts([
                    fact("fact_a", "用户说这样很好", episode_id="ep:e1",
                         keys=["host:demo:private:a"],
                         kind="explicit_feedback"),
                    fact("fact_b", "用户又夸自然", episode_id="ep:e2",
                         keys=["host:demo:private:b"],
                         kind="observed_action"),
                ]),
                "created_at": "2026-10-20T00:00:00+00:00",
            }])
        return core

    async def test_facts_alone_are_rejected_as_explanation_only(self):
        model = ScriptedModel({
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": "用户喜欢温柔的语气。"}],
            "reason": "fact",
        })
        core = self.build(model)
        self.prepare(core, with_understanding=False)
        receipt = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["fact_a"],
        })
        self.assertEqual(receipt.status, "rejected")
        self.assertEqual(receipt.reason, "fact_not_eligible")
        self.assertEqual(model.calls, [])
        self.assertEqual(core.persona_card_service.get("demo").revision, 1)
        refused = [
            check for check in receipt.validation["checks"]
            if check["name"] == "eligibility"
        ]
        self.assertEqual(refused[0]["reason"], "fact_not_eligible")

    async def test_immature_understanding_is_rejected(self):
        model = ScriptedModel({"operations": [], "reason": "no_change"})
        core = self.build(model)
        self.prepare(core, with_understanding=False)
        core.reflections.save("demo", [{
            "id": "ref_thin",
            "text": "用户喜欢温柔",
            "status": "confirmed",
            "entity": "relationship",
            "source_fact_ids": ["fact_a"],
            "created_at": "2026-10-20T00:00:00+00:00",
        }])
        receipt = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["ref_thin"],
        })
        self.assertEqual(receipt.status, "rejected")
        self.assertEqual(receipt.reason, "not_mature")
        self.assertEqual(model.calls, [])

    async def test_confirmed_row_with_empty_basis_is_not_eligible(self):
        """The gap this batch closes: "confirmed" alone is not a maturity proof.

        The growth path and the evidence-score path reach confirmed by different
        routes, so a row with no recorded basis must not become a card-change
        candidate even though its status says confirmed.
        """
        model = ScriptedModel({"operations": [], "reason": "no_change"})
        core = self.build(model)
        self.prepare(core, with_understanding=False)
        core.reflections.save("demo", [{
            "id": "ref_empty",
            "text": "用户喜欢温柔",
            "status": "confirmed",
            "source_fact_ids": ["fact_a"],
        }])
        receipt = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["ref_empty"],
        })
        self.assertEqual(receipt.status, "rejected")
        self.assertEqual(receipt.reason, "not_mature")
        self.assertEqual(model.calls, [])

    async def test_mature_understanding_reaches_the_prompt_with_full_dto(self):
        model = ScriptedModel({
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": "用户喜欢温柔、具体的语气。"}],
            "reason": "mature understanding",
        })
        core = self.build(model)
        self.prepare(core)
        receipt = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["ref_understanding"],
            "reason": "review",
        })
        self.assertEqual(receipt.status, "proposed")

        # G16: the adapter delivered source facts, scope and maturity, both to
        # the model prompt and to the stored proposal.
        prompt = model.calls[0]["messages"][1]["content"]
        payload = json.loads(prompt)
        evidence = {
            item["source_ref"]: item for item in payload["evidence"]
        }
        self.assertTrue(evidence["ref_understanding"]["eligible"])
        self.assertEqual(
            evidence["ref_understanding"]["maturity"]["independent_experiences"],
            2,
        )
        self.assertIn("fact_a", evidence["ref_understanding"]["source_fact_ids"])
        self.assertFalse(evidence["fact_a"]["eligible"])
        self.assertEqual(
            evidence["fact_a"]["eligibility"], "fact_not_eligible",
        )
        stored = core.evolution_service.get("demo", receipt.proposal_id)
        self.assertIn("ref_understanding", stored["evidence"]["reflection_ids"])
        self.assertIn("fact_a", stored["evidence"]["fact_ids"])
        self.assertIn("fact_b", stored["evidence"]["fact_ids"])

        provider_rows = core.evolution_scheduler._evidence_provider("demo")
        by_id = {row["id"]: row for row in provider_rows}
        dto = by_id["ref_understanding"]["evidence"]
        self.assertTrue(dto["eligible"])
        self.assertEqual(dto["eligibility"], "")
        self.assertEqual(dto["maturity"]["independent_experiences"], 2)
        self.assertIn("fact_a", dto["source_fact_ids"])
        # The fact is carried, but as explanation only.
        self.assertFalse(by_id["fact_a"]["evidence"]["eligible"])
        self.assertEqual(by_id["fact_a"]["evidence"]["eligibility"],
                         "fact_not_eligible")

    async def test_scope_and_origin_survive_the_adapter(self):
        """G16: a reflection's scope and origin must reach the proposal, not an
        empty evidence list (the field mismatch the design calls out)."""
        core = self.build(ScriptedModel({"operations": [], "reason": "no_change"}))
        self.prepare(core, with_understanding=False)
        core.reflections.save("demo", [{
            "id": "ref_scoped",
            "text": "用户在游戏里喜欢先稳住",
            "status": "confirmed",
            "entity": "relationship",
            "memory_kind": "experience",
            "claim": "先稳住再推进",
            "situation": "游戏里",
            "applies_to": {"game_id": "sts2"},
            "scope_key": "sc_scoped",
            "source_fact_ids": ["fact_a"],
            "support_basis": support_basis_from_facts([
                fact("fact_a", "一起打了两局", episode_id="ep:e1",
                     keys=["host:demo:private:a"], kind="observed_action",
                     source="shared_experience"),
                fact("fact_b", "第二天又打", episode_id="ep:e2",
                     keys=["host:demo:private:b"], kind="observed_action",
                     source="shared_experience"),
            ]),
            "created_at": "2026-10-20T00:00:00+00:00",
        }])
        rows = core.evolution_scheduler._evidence_provider("demo")
        scoped = [row for row in rows if row["id"] == "ref_scoped"][0]
        dto = scoped["evidence"]
        self.assertEqual(dto["applies_to"], {"game_id": "sts2"})
        self.assertEqual(dto["scope_key"], "sc_scoped")
        self.assertEqual(dto["kind"], "growth")
        self.assertTrue(dto["eligible"])
        self.assertEqual(len(dto["experiences"]), 2)

    async def test_correction_recomputes_support_but_keeps_provenance(self):
        """A correction drops the corrected experience from support; the fact id
        stays as provenance so the persona fan-out can still find the entry."""
        core = self.build(ScriptedModel({"operations": [], "reason": "no_change"}))
        self.prepare(core, with_understanding=False)
        core.facts.save("demo", [
            fact("fact_a", "用户喜欢温柔的语气", keys=["host:demo:private:a"]),
        ])
        core.reflections.save("demo", [{
            "id": "ref_two",
            "text": "用户喜欢温柔、具体的语气",
            "status": "confirmed",
            "entity": "relationship",
            "source_fact_ids": ["fact_a", "fact_b"],
            "support_basis": support_basis_from_facts([
                fact("fact_a", "用户说这样很好", episode_id="ep:e1",
                     keys=["host:demo:private:a"], kind="explicit_feedback"),
                fact("fact_b", "用户又夸自然", episode_id="ep:e2",
                     keys=["host:demo:private:b"], kind="observed_action"),
            ]),
            "created_at": "2026-10-20T00:00:00+00:00",
        }])
        core.persona_service.add_reflection(
            "demo", core.reflections.load("demo")[0],
        )
        receipt = await core.correction_service.correct(
            CorrectionRequest(
                scope=Scope("demo"),
                old_text="用户喜欢温柔的语气",
                new_text="用户喜欢直接一点的语气",
            ),
        )
        self.assertIn("fact_a", receipt.invalidated_fact_ids)
        self.assertIn("ref_two", receipt.invalidated_reflection_ids)
        # Provenance kept, so the derived persona entry is still reached.
        self.assertIn("prom_ref_two", receipt.invalidated_persona_ids)
        stored = [
            row for row in core.reflections.load("demo")
            if row["id"] == "ref_two"
        ][0]
        self.assertEqual(stored["support_count"], 1)
        self.assertEqual(
            stored["support_event_keys"], ["host:demo:private:b"],
        )
        self.assertIn("fact_a", stored["source_fact_ids"])

    async def test_legacy_understanding_still_qualifies_end_to_end(self):
        """The same compatibility check through the real service and adapter."""
        model = ScriptedModel({
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": "用户喜欢温柔、具体的语气。"}],
            "reason": "legacy evidence",
        })
        core = self.build(model)
        self.prepare(core, with_understanding=False)
        # Exactly the shape an installation upgraded from M3 has on disk.
        core.reflections.save("demo", [{
            "id": "ref_legacy",
            "text": "用户喜欢温柔、具体的语气",
            "status": "confirmed",
            "entity": "relationship",
            "source_fact_ids": ["fact_a"],
            "support_event_keys": [
                "host:demo:private:a", "host:demo:private:b",
            ],
            "support_count": 2,
            "support_evidence": {
                "actual_result": ["host:demo:private:a"],
                "user_feedback": ["host:demo:private:b"],
                "both_party": [], "real_contact": [],
            },
            "created_at": "2026-10-20T00:00:00+00:00",
        }])
        receipt = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["ref_legacy"],
        })
        self.assertEqual(receipt.status, "proposed")
        rows = core.evolution_scheduler._evidence_provider("demo")
        dto = [row for row in rows if row["id"] == "ref_legacy"][0]["evidence"]
        self.assertTrue(dto["eligible"])
        self.assertEqual(dto["maturity"]["independent_experiences"], 2)

    async def test_scheduler_reports_fact_not_eligible_instead_of_proposing(self):
        model = ScriptedModel({"operations": [], "reason": "no_change"})
        core = self.build(model)
        self.prepare(core, with_understanding=False)
        core.persona_admin_service.set_mode("demo", "propose")
        result = await core.evolution_scheduler.evaluate("demo")
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.reason, "fact_not_eligible")
        self.assertEqual(model.calls, [])
        self.assertEqual(core.persona_card_service.get("demo").revision, 1)

    async def test_mature_evidence_survives_the_budget_in_both_entry_points(self):
        model = ScriptedModel({
            "operations": [{"op": "replace", "section_id": "tone", "text": "我说话温柔，也会给具体例子。"}],
            "reason": "新的具体做法",
        })
        core = self.build(model)
        self.prepare(core)
        facts = core.facts.load("demo")
        noise = [fact(f"unrelated_{index}", "一条背景事实") for index in range(12)]
        core.facts.save("demo", noise + facts)

        direct = await core.evolution_service.propose("demo", {"base_revision": 1})
        self.assertEqual(direct.status, "proposed")
        direct_evidence = json.loads(model.calls[0]["messages"][1]["content"])["evidence"]
        self.assertEqual([row["source_ref"] for row in direct_evidence[:2]], ["ref_understanding", "fact_a"])

        core.persona_admin_service.set_mode("demo", "propose")
        scheduled = await core.evolution_scheduler.evaluate("demo")
        self.assertEqual(scheduled.status, "proposed")
        self.assertEqual(scheduled.run["evidence"]["ids"][:2], ["ref_understanding", "fact_a"])
        evidence = json.loads(model.calls[1]["messages"][1]["content"])["evidence"]
        self.assertEqual([row["source_ref"] for row in evidence[:2]], ["ref_understanding", "fact_a"])


class EvidenceDtoTests(unittest.TestCase):
    """The DTO is the contract all three layers read."""

    ROW = {
        "id": "ref_dto",
        "text": "用户倾诉时先接住感受",
        "status": "confirmed",
        "topic_key": "tp_1",
        "scope_key": "sc_game",
        "applies_to": {"game_id": "sts2"},
        "source_fact_ids": ["fact_a"],
    }

    def dto(self, row):
        return evidence_dto(
            row, kind="reflection", promotion_rules=PROMOTION_RULES,
        )

    def test_dto_carries_origin_scope_and_maturity(self):
        row = dict(self.ROW)
        row.update(summarize_basis(support_basis_from_facts([
            fact("fact_a", "用户倾诉", episode_id="ep:e1",
                 keys=["host:demo:private:k1"], kind="explicit_feedback"),
            fact("fact_b", "用户又说", episode_id="ep:e2",
                 keys=["host:demo:private:k2"], kind="explicit_feedback"),
        ])))
        dto = self.dto(row)
        self.assertEqual(dto["source_fact_ids"], ["fact_a", "fact_b"])
        self.assertEqual(dto["source_keys"],
                         ["host:demo:private:k1", "host:demo:private:k2"])
        self.assertEqual(dto["applies_to"], {"game_id": "sts2"})
        self.assertEqual(dto["scope_key"], "sc_game")
        self.assertEqual(dto["maturity"]["independent_experiences"], 2)
        self.assertTrue(dto["eligible"])

    def test_plain_assertions_do_not_authorize_persona_evolution(self):
        row = dict(self.ROW)
        row.update(summarize_basis(support_basis_from_facts([
            fact("a", "用户喜欢茶", episode_id="e1"),
            fact("b", "用户再次谈到茶", episode_id="e2"),
        ])))
        dto = self.dto(row)
        self.assertEqual(dto["maturity"]["independent_experiences"], 2)
        self.assertFalse(dto["maturity"]["has_interaction_evidence"])
        self.assertFalse(dto["eligible"])
        self.assertEqual(dto["eligibility"], "not_mature")

    def test_feedback_can_authorize_a_reflection_without_actual_result(self):
        row = dict(self.ROW)
        row.update(summarize_basis(support_basis_from_facts([
            fact("a", "用户谈到自己的偏好", episode_id="e1"),
            fact("b", "用户认可这次回应", episode_id="e2", kind="explicit_feedback"),
        ])))
        dto = self.dto(row)
        self.assertEqual(row["support_evidence"]["actual_result"], [])
        self.assertIn("user_feedback", dto["maturity"]["flags"])
        self.assertTrue(dto["eligible"])

    def test_losing_last_feedback_removes_eligibility_even_with_two_experiences(self):
        row = dict(self.ROW)
        row.update(summarize_basis(support_basis_from_facts([
            fact("a", "用户谈到偏好", episode_id="e1"),
            fact("b", "用户补充偏好", episode_id="e2"),
            fact("c", "用户认可互动", episode_id="e3", kind="explicit_feedback"),
        ])))
        self.assertTrue(self.dto(row)["eligible"])
        self.assertTrue(apply_support_removal_to_row(
            row, {"host:demo:private:c"}, now_iso="2026-10-27T12:00:00+00:00",
        ))
        self.assertEqual(row["support_count"], 2)
        self.assertFalse(self.dto(row)["eligible"])

    def test_legacy_row_keeps_its_maturity_after_the_upgrade(self):
        """Data compatibility: every row written before M4 has no support_basis.

        Such a row must keep the support it recorded (one experience per key,
        under the pre-M4 counting rule) instead of silently dropping to zero -
        otherwise an upgrade would turn every existing understanding
        `not_mature` and withdraw every adopted strategy overnight.
        """
        legacy = {
            "id": "ref_legacy",
            "text": "用户喜欢温柔的语气",
            "status": "confirmed",
            "entity": "relationship",
            "source_fact_ids": ["fact_a"],
            "support_event_keys": ["host:demo:private:a", "host:demo:private:b"],
            "support_count": 2,
            "support_evidence": {
                "actual_result": ["host:demo:private:a"],
                "user_feedback": ["host:demo:private:b"],
                "both_party": [], "real_contact": [],
            },
            "created_at": "2026-10-20T00:00:00+00:00",
        }
        self.assertNotIn("support_basis", legacy)
        basis = support_basis_from_row(legacy)
        self.assertEqual(len(basis), 2)
        maturity = maturity_of_row(legacy, promotion_rules=PROMOTION_RULES)
        self.assertEqual(maturity["independent_experiences"], 2)
        self.assertTrue(maturity["basis_present"])
        self.assertTrue(maturity["reflection_ready"])
        eligible, reason = eligibility_of_row(
            legacy, promotion_rules=PROMOTION_RULES,
        )
        self.assertTrue(eligible, reason)

    def test_legacy_fact_id_only_row_counts_without_inventing_flags(self):
        legacy = {
            "id": "ref_facts_only",
            "status": "confirmed",
            "source_fact_ids": ["fact_a", "fact_b"],
        }
        basis = support_basis_from_row(legacy)
        self.assertEqual(len(basis), 2)
        summary = summarize_basis(basis)
        self.assertEqual(summary["support_count"], 2)
        for flag in ("actual_result", "user_feedback", "both_party", "real_contact"):
            self.assertEqual(summary["support_evidence"][flag], [])

    def test_legacy_row_without_basis_reconstructs_its_recorded_flags(self):
        """A pre-M4 row keeps exactly the flags it declared: it can neither
        regain actual_result nor lose a flag it really had."""
        row = {
            "id": "ref_legacy", "text": "旧认识", "status": "confirmed",
            "support_event_keys": ["k1", "k2"],
            "support_evidence": {
                "actual_result": ["k1"], "user_feedback": ["k2"],
                "both_party": [], "real_contact": [],
            },
            "support_count": 2,
        }
        basis = support_basis_from_row(row)
        self.assertEqual(len(basis), 2)
        summary = summarize_basis(basis)
        self.assertEqual(summary["support_count"], 2)
        self.assertEqual(summary["support_evidence"]["actual_result"], ["k1"])
        self.assertEqual(summary["support_evidence"]["user_feedback"], ["k2"])

    def test_row_with_inflated_count_but_one_episode_is_recomputed(self):
        row = {"id": "r", "status": "confirmed", "support_count": 5,
               "support_event_keys": ["k1", "k2", "k3", "k4", "k5"]}
        row.update(summarize_basis(support_basis_from_facts([
            fact("f%d" % index, "t", episode_id="ep:e1",
                 keys=["k%d" % (index + 1)], kind="observed_action")
            for index in range(5)
        ])))
        self.assertEqual(row["support_count"], 1)
        eligible, reason = eligibility_of_row(
            row, promotion_rules=PROMOTION_RULES,
        )
        self.assertFalse(eligible)
        self.assertEqual(reason, "not_mature")

    def test_fact_rows_are_never_eligible(self):
        dto = evidence_dto(
            {"id": "fact_a", "kind": "fact", "text": "x"},
            kind="fact", promotion_rules=PROMOTION_RULES,
        )
        self.assertFalse(dto["eligible"])
        self.assertEqual(dto["eligibility"], "fact_not_eligible")


class PromotionRulesUnchangedTests(unittest.TestCase):
    """M4 must not move the gate itself, only what feeds it."""

    def test_promotion_rules_definition_is_untouched(self):
        self.assertEqual(
            sorted(PROMOTION_RULES),
            ["experience", "habit", "interest", "relationship"],
        )
        self.assertEqual(PROMOTION_RULES["habit"],
                         ("actual_result", "user_feedback"))
        self.assertEqual(PROMOTION_RULES["experience"],
                         ("actual_result", "user_feedback"))
        self.assertEqual(PROMOTION_RULES["interest"], ("real_contact",))
        self.assertEqual(PROMOTION_RULES["relationship"], ("both_party",))

    def test_interest_needs_real_contact(self):
        row = {"id": "g", "status": "confirmed", "memory_kind": "interest",
               "claim": "用户喜欢摄影"}
        row.update(summarize_basis(support_basis_from_facts([
            fact("f", "说喜欢摄影", episode_id="ep:e1", keys=["k1"],
                 kind="user_assertion"),
            fact("g", "又说喜欢", episode_id="ep:e2", keys=["k2"],
                 kind="user_assertion"),
        ])))
        self.assertFalse(maturity_of_row(
            row, promotion_rules=PROMOTION_RULES,
        )["promotion_ready"])

        real = {"id": "g", "status": "confirmed", "memory_kind": "interest",
                "claim": "用户喜欢摄影"}
        real.update(summarize_basis(support_basis_from_facts([
            fact("f", "一起看了展", episode_id="ep:e1", keys=["k1"],
                 kind="observed_action", source="shared_experience"),
            fact("g", "又去了第二次", episode_id="ep:e2", keys=["k2"],
                 kind="observed_action", source="shared_experience"),
        ])))
        self.assertTrue(maturity_of_row(
            real, promotion_rules=PROMOTION_RULES,
        )["promotion_ready"])


if __name__ == "__main__":
    unittest.main()
