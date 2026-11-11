"""S06 recall baseline as a regression gate.

The measurement itself lives in `tools/check_s06_recall.py` (it needs a report
file and timings). This suite pins the part that must never regress:

* every hard contract still holds (isolation, deletion, unrelated injection);
* Recall@K never drops below the recorded baseline;
* the known synonym gap is still *recorded* rather than accidentally "passing"
  because a case stopped expecting anything.

That split matters: a quality gap is allowed to exist and be listed, but a
dataset that silently stops testing what it claims is not.
"""

import sys
import unittest
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.contracts import DeletionRequest, DeletionTarget, Scene, Scope
from rin_memory.domain.recall import RecallSettings
from rin_memory.persistence.facts_view import FactsView

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from s06_samples import (  # noqa: E402  (tools/s06_samples.py)
    FIXED_NOW,
    build_cases,
)


class FixedClock:
    def now(self):
        return FIXED_NOW

    def monotonic(self) -> float:
        import time

        return time.monotonic()


async def _run(case, root: Path) -> list[str]:
    core = build_memory_core(memory_root=root, clock=FixedClock())
    core.recover("demo")
    core.facts.save("demo", [
        {
            "id": fact.id,
            "text": fact.text,
            "subject": fact.subject,
            "schema_version": 2,
            "source": "user_observation",
            "importance": 6,
            "created_at": FIXED_NOW.isoformat(),
            "episode_id": f"ep:{case.name}:{fact.id}",
            "source_event_keys": [f"host:demo:{fact.subject}:{fact.id}"],
            **(dict(applies_to=dict(fact.scene)) if fact.scene else {}),
        }
        for fact in case.facts
    ])
    if case.deleted_ids:
        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"),
            mode="entry",
            targets=tuple(
                DeletionTarget(type="fact", id=entry_id)
                for entry_id in case.deleted_ids
            ),
            reason="s06 regression",
        ))
        core.barrier.refresh("demo")

    scene = Scene(**dict(case.scene)) if case.scene else None
    if case.path == "passive":
        result = await core.recall.build_turn_context(
            "demo",
            current_input=case.query,
            recent_turns=list(case.recent_turns) or None,
            subject=case.subject or None,
            scene=scene,
        )
        return [str(item) for item in result.get("memory_ids", [])]
    result = await core.recall.recall_memory(
        "demo", case.query, subject=case.subject or None, scene=scene,
    )
    return [
        str(item.get("id")) for item in result.get("items", [])
        if isinstance(item, dict)
    ]


class RecallSampleContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "memory"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_fast_recall_of_a_personal_list_prefers_the_latest_confirmed_list(self):
        core = build_memory_core(memory_root=self.root, clock=FixedClock(),
                                 recall_settings=RecallSettings(fast_chat=True))
        core.recover("demo")
        core.facts.save("demo", [
            {"id": key, "text": text, "subject": "qq-friend:42", "source": "user_observation",
             "importance": 6, "created_at": f"2026-10-05T{hour}:00:00+00:00", "schema_version": 2}
            for key, text, hour in (
                ("older", "我记得对方会用普洱、白牡丹招待客人。", "09"),
                ("newer", "我记得对方说普洱、白牡丹和碧螺春都适合招待客人。", "10"),
                ("placement", "我记得对方把招待客人的茶叶放在橱柜上层。", "11"),
            )
        ])
        for question, expected in (
            ("我平时用哪些茶叶招待？", ["newer"]),
            ("你记得我之前把招待客人的茶叶放在哪里吗？", ["placement"]),
            ("我用哪些材料做相框？", []),
        ):
            with self.subTest(question=question):
                result = await core.recall.build_turn_context("demo", current_input=question, subject="qq-friend:42")
                self.assertEqual(result["memory_ids"], expected)

    def test_corpus_is_well_formed(self):
        cases = build_cases()
        self.assertGreaterEqual(len(cases), 8)
        names = [case.name for case in cases]
        self.assertEqual(len(names), len(set(names)), "case names must be unique")
        for case in cases:
            ids = {fact.id for fact in case.facts}
            self.assertFalse(
                set(case.expect_recalled) - ids,
                f"{case.name}: expects an id outside its own corpus",
            )
            self.assertFalse(
                set(case.expect_excluded) - ids,
                f"{case.name}: excludes an id outside its own corpus",
            )
            self.assertFalse(
                set(case.expect_recalled) & set(case.expect_excluded),
                f"{case.name}: an id cannot be both expected and excluded",
            )
            self.assertFalse(
                set(case.deleted_ids) & set(case.expect_recalled),
                f"{case.name}: a deleted id cannot be expected to recall",
            )
            self.assertIn(case.path, ("explicit", "passive"))

    def test_no_case_allows_unrelated_injection(self):
        for case in build_cases():
            recalled = self._run_case(case)
            injected = set(recalled) & set(case.expect_excluded)
            self.assertFalse(
                injected,
                f"{case.name}: injected unrelated memories {sorted(injected)}",
            )

    def test_deleted_content_never_leaks(self):
        for case in build_cases():
            if not case.deleted_ids:
                continue
            recalled = self._run_case(case)
            leaked = set(recalled) & set(case.deleted_ids)
            self.assertFalse(
                leaked, f"{case.name}: deleted ids came back {sorted(leaked)}",
            )

    def test_recall_at_k_meets_the_recorded_baseline(self):
        """The one declared gap is `synonym_paraphrase`; everything else hits."""
        gaps: list[str] = []
        for case in build_cases():
            if not case.expect_recalled:
                continue
            recalled = self._run_case(case)
            hit = set(recalled) & set(case.expect_recalled)
            if not hit:
                gaps.append(case.name)
        self.assertEqual(
            gaps, ["synonym_paraphrase"],
            "recall regressed beyond the documented synonym gap",
        )

    def test_every_declared_gap_is_still_declared(self):
        """A gap must stay measurable, not be quietly emptied out."""
        synonym = [c for c in build_cases() if c.failure_type == "synonym"]
        self.assertTrue(synonym, "the synonym case disappeared from the corpus")
        for case in synonym:
            self.assertTrue(
                case.expect_recalled,
                "a synonym case with no expectation stops measuring the gap",
            )

    def _run_case(self, case) -> list[str]:
        import asyncio

        return asyncio.run(_run(case, self.root / case.name))


class RecallSampleDeterminismTests(unittest.IsolatedAsyncioTestCase):
    def test_same_case_twice_returns_the_same_ids(self):
        """A baseline that drifts run to run cannot be regressed against."""
        import asyncio
        import tempfile

        case = next(c for c in build_cases() if c.name == "cjk_entity_drink")
        with tempfile.TemporaryDirectory() as tmp:
            first = asyncio.run(_run(case, Path(tmp) / "a" / "memory"))
        with tempfile.TemporaryDirectory() as tmp:
            second = asyncio.run(_run(case, Path(tmp) / "b" / "memory"))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
