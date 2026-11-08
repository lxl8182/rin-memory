"""S03 §5.2 regression: one Chinese sample stays byte-exact across every layer.

The service plan asks to find the earliest hop where the text stops matching.
This suite pins that comparison so a future change cannot silently reintroduce
mojibake in any of them:

    input dict -> message_text -> recent.json bytes -> time index -> model
    prompt -> model reply -> facts.json

Byte-level assertions matter here: `ensure_ascii=True` anywhere in the chain
would keep the decoded text correct while turning every stored Chinese string
into `\\uXXXX` escapes, which is how the original report looked.

The last layer is the interesting one: a Windows console or pipe that cannot
encode the sample corrupts only what it *prints*. That case is asserted
separately so a console problem is never mistaken for a storage problem.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from test_b04_extraction import extraction_reply
from rin_memory.domain.barrier import message_text
from rin_memory.domain.extraction import ExtractionSettings

#: Chinese + full-width punctuation + an em dash pair + emoji.
SAMPLE = "她喜欢乌龙茶🥤，说“下次再聊”——记得提醒我！"


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return 0.0


class CapturingModel:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.prompts.append(messages[0]["content"] if messages else "")
        return extraction_reply([{
            "text": "用户喜欢乌龙茶",
            "importance": 7,
            "source": "user_observation",
            "entity": "master",
        }], messages[0]["content"])


class ChineseChainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "memory"
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, model):
        return build_memory_core(
            memory_root=self.root, clock=self.clock, model=model,
            extraction_settings=ExtractionSettings(inline=True),
        )

    async def test_sample_survives_every_layer_byte_exactly(self):
        model = CapturingModel()
        core = self.build(model)
        core.recover("demo")
        message = {"type": "human", "data": {"content": SAMPLE}}
        self.assertEqual(message_text(message), SAMPLE)

        result = await core.extraction.ingest_turn(
            "demo", [message], source_key="host:demo:private:u1",
            content_class="chat", session_id="s1",
        )
        self.assertEqual(result.status, "extracted")

        # recent.json: correct text AND no \u escaping on disk.
        recent_bytes = (self.root / "demo" / "recent.json").read_bytes()
        self.assertNotIn(b"\\u", recent_bytes, "stored Chinese must not be escaped")
        recent = json.loads(recent_bytes.decode("utf-8"))
        self.assertIn(SAMPLE, [row["data"]["content"] for row in recent])

        # Time index: the raw conversation row keeps the same bytes.
        index = core.timeindex("demo").open()
        try:
            row = index.get_original(1)
        finally:
            index.close()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row["message"])["data"]["content"], SAMPLE)

        # The exact string handed to the model.
        self.assertEqual(len(model.prompts), 1)
        self.assertIn(SAMPLE, model.prompts[0])
        self.assertNotIn("\ufffd", model.prompts[0])

        # Facts written back from the model reply.
        facts = core.facts.load("demo")
        self.assertEqual(facts[0]["text"], "用户喜欢乌龙茶")
        self.assertNotIn(b"\\u", (self.root / "demo" / "facts.json").read_bytes())

    async def test_reopened_root_reads_the_same_text(self):
        core = self.build(CapturingModel())
        core.recover("demo")
        await core.extraction.ingest_turn(
            "demo", [{"type": "human", "data": {"content": SAMPLE}}],
            source_key="host:demo:private:u2", content_class="chat",
        )
        reopened = self.build(CapturingModel())
        reopened.recover("demo")
        rows = reopened.recent.load("demo")
        self.assertEqual(rows[0]["data"]["content"], SAMPLE)

    def test_core_survives_a_console_that_cannot_encode_the_sample(self):
        """A CP1252 stdout must not change what the core stores.

        This is the separation the plan asks for: a display-layer failure is
        not a storage failure, and must not be "fixed" by touching storage.
        """
        script = (
            "import json, sys, tempfile, asyncio\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "sys.stdout.reconfigure(errors='strict')\n"
            "from rin_memory.adapters.composition import build_memory_core\n"
            "root = Path(tempfile.mkdtemp()) / 'memory'\n"
            "core = build_memory_core(memory_root=root)\n"
            "core.recent.append('demo', [{'type': 'human', 'data': {'content': sys.argv[2]}}])\n"
            "stored = core.recent.load('demo')[0]['data']['content']\n"
            "print(json.dumps({'equal': stored == sys.argv[2], 'bytes_ok': '\\\\u' not in (root/'demo'/'recent.json').read_text('utf-8')}))\n"
        )
        src = str(Path(__file__).resolve().parents[1] / "src")
        import os

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "cp1252"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", script, src, SAMPLE],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
            env=env,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["equal"], "the stored text must be unaffected")
        self.assertTrue(payload["bytes_ok"])


if __name__ == "__main__":
    unittest.main()
