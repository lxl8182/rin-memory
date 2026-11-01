"""Historical proposal comparisons and actionable publication indicators."""
import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from rin_memory.service.management import ManagementApi
from rin_memory.service.memory_service import MemoryService, ServiceSettings
from rin_memory.service.console import _fact_view
if __package__:
    from .test_b12_evolution import FakeModel, INITIAL, PROPOSAL
else:
    from test_b12_evolution import FakeModel, INITIAL, PROPOSAL


class EvolutionDisplayTests(unittest.TestCase):
    def test_fact_confidence_requires_an_explicit_valid_score(self):
        for score in (None, "invalid", True, -0.1, 1.1, float("nan"), float("inf")):
            self.assertIsNone(_fact_view({"confidence": score})["confidence"])
        self.assertIsNone(_fact_view({})["confidence"])
        self.assertEqual(_fact_view({"confidence": 0})["confidence"], 0)
        self.assertEqual(_fact_view({"confidence": 0.86})["confidence"], 0.86)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = MemoryService.start(ServiceSettings(memory_root=self.root, character="demo"))
        self.core = self.service.core
        self.core.persona_card_service.import_initial("demo", INITIAL)
        self.core.facts.save("demo", [{"id": "fact_a", "text": "用户喜欢具体自然的表达", "importance": 7,
            "schema_version": 2, "source_event_keys": ["host:demo:private:a"]}])
        self.core.reflections.save("demo", [{
            "id": "ref_display", "text": "用户认可温柔、具体的回应",
            "status": "confirmed", "entity": "relationship",
            "source_fact_ids": ["fact_a"],
            "support_basis": [
                {"experience": "episode:display-a", "facts": ["fact_a"],
                 "support": [{"key": "host:demo:private:a", "fact_id": "fact_a",
                              "kind": "explicit_feedback", "flags": ["user_feedback"]}]},
                {"experience": "episode:display-b", "facts": ["fact_a"],
                 "support": [{"key": "host:demo:private:b", "fact_id": "fact_a",
                              "kind": "explicit_feedback", "flags": ["user_feedback"]}]},
            ],
        }])
        self.core.evolution_service._model = FakeModel(PROPOSAL)
        self.app = ManagementApi(self.service).console

    def tearDown(self):
        asyncio.run(self.service.stop())
        self.tmp.cleanup()

    def test_accepted_proposal_keeps_its_base_text_after_later_edits_and_archival(self):
        self.core.persona_card_service.revise("demo", {"base_revision": 1,
            "operations": [{"op": "replace", "section_id": "tone", "text": "原来先给建议。"}]})
        receipt = asyncio.run(self.core.evolution_service.propose("demo", {"base_revision": 2, "source_refs": ["ref_display"]}))
        self.assertEqual(receipt.status, "proposed")
        self.core.persona_admin_service.accept_proposal("demo", receipt.proposal_id)
        self.core.persona_publish.publish("demo")
        for number in range(21):
            card = self.core.persona_card_service.get("demo")
            self.core.persona_card_service.revise("demo", {"base_revision": card.revision,
                "operations": [{"op": "replace", "section_id": "tone", "text": "后来人工修改 " + str(number)}]})
        self.assertNotIn(2, [revision.revision for revision in self.core.persona_card_service.get("demo").revisions])
        proposal = self.app.evolution()["proposals"][0]
        self.assertEqual(proposal["status"], "accepted")
        self.assertEqual(proposal["accepted_revision"], 3)
        self.assertEqual(proposal["changes"][0]["before"], "原来先给建议。")
        self.assertNotEqual(proposal["changes"][0]["before"], proposal["changes"][0]["after"])
        self.assertFalse(proposal["changes"][0]["noop"])
        load = self.app._load()
        load["proposals"] = [dict(load["proposals"][0], base_revision=999)]
        missing = self.app._evolution(load)["proposals"][0]["changes"][0]
        self.assertIsNone(missing["before"])
        self.assertFalse(missing["before_available"])
        self.assertFalse(missing["noop"])

    def test_only_connected_consumers_wait_for_a_published_version(self):
        published = self.core.persona_publish.publish("demo")
        persona = self.app.persona()
        # The console's own conversation reads the current published card on
        # every send, so it never has an adoption step to wait for; a pending
        # marker there was an alert the user could not clear.
        self.assertEqual(persona["pending"], [])
        self.assertEqual(persona["consumers"]["chat"]["status"], "inline")
        self.assertTrue(all(persona["consumers"][name]["status"] == "not_connected" for name in ("active", "realtime", "game")))
        self.core.persona_publish.consume("demo", "chat", expected_version=published.version)
        self.assertEqual(self.app.persona()["consumers"]["chat"]["status"], "adopted")
        self.core.persona_card_service.revise("demo", {"base_revision": 1,
            "operations": [{"op": "replace", "section_id": "tone", "text": "新版语气。"}]})
        self.assertTrue(self.app.persona()["stale"])
        self.core.persona_publish.publish("demo")
        pending = self.app.persona()
        self.assertEqual(pending["pending"], [])
        self.assertEqual(pending["consumers"]["chat"]["status"], "inline")
        self.assertEqual(pending["consumers"]["chat"]["version"], published.version)

    def test_a_connected_service_consumer_still_reports_pending_until_it_adopts(self):
        published = self.core.persona_publish.publish("demo")
        self.core.persona_publish.consume("demo", "chat", expected_version=published.version)
        # A real consumer that read an older version stays pending until it
        # adopts, which is exactly the state the alert is for.
        self.core.persona_publish.consume("demo", "neko_chat", expected_version=published.version)
        self.core.persona_card_service.revise("demo", {"base_revision": 1,
            "operations": [{"op": "replace", "section_id": "tone", "text": "再改一次。"}]})
        self.core.persona_publish.publish("demo")
        persona = self.app.persona()
        self.assertEqual(persona["pending"], ["neko_chat"])
        self.core.persona_publish.consume("demo", "neko_chat")
        self.assertEqual(self.app.persona()["pending"], [])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the existing UI script")
    def test_client_shows_historical_changes_and_only_actionable_notices(self):
        script = r'''
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const nodes = new Map(["#propNoop", "#propHint", "#propTitle", "#propBefore", "#propAfter", "#propEv", "#propImpact",
  "#reflHint", "#reflTitle", "#reflReason", "#reflEv", "#reflSuggest", "#reflFoot",
  '[data-page="reflections"] .detail-act', '[data-page="evolution"] .detail-act'].map(key => [key, {hidden: false}]));
const window = {__RIN_SERVICE__: true};
const context = {window, document: {querySelector: key => nodes.get(key) || null,
  querySelectorAll: () => [], addEventListener: () => {}},
  location: {origin: "http://test"}, sessionStorage: {getItem: () => null},
  fetch: () => new Promise(() => {}), setInterval: () => 0};
const source = fs.readFileSync(process.argv[1], "utf8").replace(/\}\)\(\);\s*$/,
  "window.testViews = {proposalDiff, publicationState, selectLiveProposal, renderReflections, renderEvolution};})();");
vm.runInNewContext(source, context);
const {proposalDiff, publicationState, selectLiveProposal, renderReflections, renderEvolution} = window.testViews;
const accepted = {proposal_id: "accepted", base_revision: 6, status: "accepted", operations: [],
  changes: [{section_id: "tone", before_available: true, before: "原文", after: "新文", noop: false}]};
window.RIN_LIVE.snapshot = {persona: {card: {sections: [{section_id: "tone", text: "新文"}]}}, facts: {facts: []}, reflections: {reflections: []}};
window.RIN_LIVE.proposals = [accepted];
assert.equal(proposalDiff(accepted).before, "原文");
assert.equal(proposalDiff(accepted).after, "新文");
selectLiveProposal("accepted");
assert.equal(nodes.get("#propNoop").hidden, true);
window.RIN_LIVE.proposals = [{...accepted, changes: [{...accepted.changes[0], before: "新文", noop: true}]}];
selectLiveProposal("accepted");
assert.equal(nodes.get("#propNoop").hidden, true);
window.RIN_LIVE.proposals[0].status = "proposed";
selectLiveProposal("accepted");
assert.equal(nodes.get("#propNoop").hidden, false);
const multi = {...accepted, changes: [{...accepted.changes[0], before: "新文", noop: true},
  {section_id: "relationship", before_available: true, before: "疏远", after: "亲近", noop: false}]};
assert.equal(proposalDiff(multi).noop, false);
assert.match(proposalDiff(multi).before, /疏远/);
assert.match(proposalDiff({...accepted, changes: [{section_id: "tone", before_available: false, after: "新文"}]}).before, /第 6 版原文已不可用/);
const persona = {stale: false, versions: {current: {version: "v7"}}, consumers: {
  chat: {status: "adopted", version: "v7"}, game: {status: "not_connected", version: ""}}};
assert.equal(publicationState(persona).notice, "");
assert.equal(publicationState(persona).names.length, 1);
assert.match(publicationState({...persona, stale: true}).notice, /尚未发布/);
assert.match(publicationState({...persona, consumers: {...persona.consumers, chat: {status: "pending", version: "v6"}}}).notice, /等待采用/);
assert.match(publicationState({...persona, consumers: {...persona.consumers, chat: {status: "failed", version: "v7"}}}).notice, /采用失败/);
window.RIN_LIVE.snapshot.reflections.reflections = [{id: "real", text: "真实反思", evidence: []}];
renderReflections();
assert.equal(nodes.get("#reflTitle").textContent, "真实反思");
assert.match(nodes.get('[data-page="reflections"] .detail-act').innerHTML, /查看演化/);
window.RIN_LIVE.snapshot.reflections.reflections = [];
renderReflections();
assert.equal(nodes.get("#reflTitle").textContent, "暂无反思详情");
assert.equal(nodes.get('[data-page="reflections"] .detail-act').innerHTML, "");
window.RIN_LIVE.snapshot.evolution = {mode: "auto", proposals: [accepted]};
renderEvolution();
assert.equal(window.RIN_LIVE.selectedProposal.proposal_id, "accepted");
window.RIN_LIVE.snapshot.evolution.proposals = [];
renderEvolution();
assert.equal(window.RIN_LIVE.selectedProposal, null);
assert.equal(nodes.get("#propTitle").textContent, "暂无提案详情");
assert.equal(nodes.get("#propBefore").textContent, "—");
assert.equal(nodes.get('[data-page="evolution"] .detail-act').innerHTML, "");
'''
        source = Path(__file__).resolve().parents[1] / "webui/designs/tidebell/live.js"
        subprocess.run([shutil.which("node"), "-e", script, str(source)], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
