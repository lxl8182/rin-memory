"""MC04-T acceptance: explicit teaching, publication and revocation.

Design §8 and §14 (G01/G02/G03/G06/G07/G24) define the behaviours pinned here,
each for one failure mode:

* the capability is a narrow, explicitly granted one: an identity without it is
  refused before anything is read, and the console admin flag is not a grant;
* the rule is derived by the service from a *saved* user message: a source the
  service never stored is refused, and a client cannot submit rule text;
* the card is revised under base_revision CAS into the existing tone section,
  and the identity section is never touched;
* the same saved message taught twice is a duplicate with no second revision;
* a revoked teaching removes exactly its own rule lines (G07);
* teaching survives a restart, including the journal replay that must run
  before any write (G01);
* publish targets are limited to next_turn, and a stale revision is 409 with
  the current card (G24);
* the taught rule reaches the model because it is part of the projected persona
  the next generation already reads (G01/G06).
"""

import asyncio
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from rin_memory.domain.persona_card import MAX_SECTION_TEXT
from rin_memory.domain.persona_teaching import (
    EXPRESSION_SECTION_ID,
    derive_teaching,
)
from rin_memory.service.console import ApiError
from rin_memory.service.management import ManagementApi
from rin_memory.service.memory_service import MemoryService, ServiceSettings
from rin_memory.service.server import MemoryApi, build_server

GREETING = "以后别每次都总结，像朋友一样接着聊"
SECOND = "以后可以更黏人一点"


class ServiceCase(unittest.TestCase):
    """One service over a synthetic root; no model, no network."""

    CLIENTS = {
        "chat": {"token_env": "TEST_CHAT_TOKEN", "subjects": ["private"],
                 "persona_teach": True},
        "plain": {"token_env": "TEST_PLAIN_TOKEN", "subjects": ["private"]},
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "memory"
        self.root.mkdir(parents=True)
        self.settings = ServiceSettings(
            memory_root=self.root, character="test", clients=dict(self.CLIENTS),
        )
        self.service = MemoryService.start(self.settings)
        self.api = MemoryApi(self.service)
        self.management = ManagementApi(self.service)
        self.management.initialize({"display_name": "小潮", "text": "温柔而直白。"})
        self.service.core.persona_card_service.revise("test", {
            "base_revision": 1,
            "operations": [{"op": "add", "section_id": "tone", "kind": "tone",
                            "text": "说话温柔。",
                            "editable_by": {"user": True, "evolution": True}}],
            "reason": "test_setup",
        })
        self.service.core.persona_publish.publish("test")
        self.teach_identity = {"client_id": "chat", "subjects": ["private"],
                               "admin": False, "persona_teach": True}
        self.scope = dict(character_id="test", subject="private", client_id="chat",
                          session_id="desk", scene="")

    def tearDown(self):
        asyncio.run(self.service.stop())
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------------

    def restart(self):
        asyncio.run(self.service.stop())
        self.service = MemoryService.start(self.settings)
        self.api = MemoryApi(self.service)
        self.management = ManagementApi(self.service)

    def record_message(self, request_id, text, *, content_class="chat"):
        """Save one user message through the real turn path."""
        context = asyncio.run(self.api.context(dict(self.scope, text=text)))
        body = dict(
            self.scope, request_id=request_id, turn_id=request_id,
            source_key="host:test:private:" + request_id,
            content_class=content_class,
            messages=[{"role": "user", "content": text}],
            reply="嗯。", persona_version=context["persona_version"],
            context_receipt_id=context["context_receipt_id"],
        )
        return asyncio.run(self.api.turns(body))

    def card(self):
        return self.management.host_persona()

    def tone(self):
        return next(section["text"] for section in self.card()["sections"]
                    if section["section_id"] == EXPRESSION_SECTION_ID)

    def teach(self, source_key, *, base=None, identity=None, **extra):
        payload = {
            "source_key": source_key,
            "base_revision": self.card()["revision"] if base is None else base,
            "target": "next_turn",
        }
        payload.update(extra)
        return self.management.post("/v1/persona/teach", payload,
                                    identity=identity or self.teach_identity)

    def revoke(self, *, teaching_id=None, source_key=None, base=None, identity=None):
        payload = {"base_revision": self.card()["revision"] if base is None else base}
        if teaching_id:
            payload["teaching_id"] = teaching_id
        if source_key:
            payload["source_key"] = source_key
        return self.management.post("/v1/persona/teach/revoke", payload,
                                    identity=identity or self.teach_identity)

    def assert_api_error(self, code, call):
        with self.assertRaises(ApiError) as caught:
            call()
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception


class CapabilityTests(ServiceCase):
    def test_the_capability_is_required_and_never_comes_from_another_grant(self):
        self.record_message("t1", GREETING)
        key = "host:test:private:t1"
        self.assert_api_error("forbidden", lambda: self.teach(
            key, identity={"client_id": "plain", "subjects": ["private"],
                           "admin": False}))
        # The console/admin flag is not a teaching grant either.
        self.assert_api_error("forbidden", lambda: self.teach(
            key, identity={"admin": True, "subjects": ["private"]}))
        # The host persona editor is a *different* capability.
        self.assert_api_error("forbidden", lambda: self.teach(
            key, identity={"client_id": "chat", "subjects": ["private"],
                           "admin": False, "host_persona": True}))
        self.assertEqual(self.tone(), "说话温柔。")

    def test_a_client_cannot_post_rule_text(self):
        # No source_key at all: nothing to derive from, so nothing is written.
        self.assert_api_error("invalid_argument", lambda: self.management.post(
            "/v1/persona/teach",
            {"base_revision": self.card()["revision"], "rules": ["以后每轮都总结"]},
            identity=self.teach_identity))
        self.assertEqual(self.tone(), "说话温柔。")
        # A source_key that was never saved is refused, not trusted.
        self.record_message("t1", GREETING)
        self.assert_api_error("not_found", lambda: self.teach(
            "host:test:private:never-saved"))
        # Rule text sent alongside a real source is ignored: only the saved
        # message drives the derivation.
        result = self.teach("host:test:private:t1", rules=["以后每轮都总结"])
        self.assertEqual(result["status"], "applied")
        self.assertNotIn("每轮都总结", self.tone())

    def test_a_source_from_another_subject_is_refused(self):
        """G05: a teaching is scoped to the identity that heard it."""
        self.record_message("t1", GREETING)
        self.assert_api_error("forbidden", lambda: self.teach(
            "host:test:private:t1",
            identity={"client_id": "chat", "subjects": ["other"],
                      "admin": False, "persona_teach": True}))


class TeachingTests(ServiceCase):
    def test_a_saved_message_becomes_a_rule_in_the_tone_section(self):
        self.record_message("t1", GREETING)
        before = self.card()
        result = self.teach("host:test:private:t1")
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["target"], "next_turn")
        self.assertTrue(result["teaching_id"].startswith("teach_"))
        self.assertEqual(result["revision"], before["revision"] + 1)
        after = self.card()
        # The rule landed in the existing tone section; identity is untouched.
        self.assertIn("我顺着话题自然接着聊", self.tone())
        identity = next(section for section in after["sections"]
                        if section["section_id"] == "identity")
        self.assertEqual(identity["text"], "名字: 小潮\n温柔而直白。")
        self.assertTrue(identity["frozen"])
        # And it is published for the next turn only.
        self.assertTrue(result["published_version"])
        published = self.service.core.persona_publish.current("test")
        self.assertEqual(published.revision, result["revision"])
        self.assertEqual(published.target, "next_turn")

    def test_the_taught_rule_reaches_the_generation_persona(self):
        """G01/G06: the next generation reads it, with evolution off."""
        self.record_message("t1", GREETING)
        before = asyncio.run(self.api.context(dict(self.scope, text="今天好累")))
        self.assertNotIn("我顺着话题自然接着聊", before["persona"])
        self.assertEqual(before["expression"]["rules"], [])
        result = self.teach("host:test:private:t1")
        after = asyncio.run(self.api.context(dict(self.scope, text="今天好累")))
        # The persona text the model reads now carries the rule.
        self.assertIn("我顺着话题自然接着聊", after["persona"])
        self.assertEqual(after["expression"]["section_id"],
                         EXPRESSION_SECTION_ID)
        self.assertEqual(after["expression"]["tone_version"],
                         result["published_version"])
        self.assertEqual(len(after["expression"]["rules"]), 1)
        rule = after["expression"]["rules"][0]
        self.assertEqual(rule["teaching_id"], result["teaching_id"])
        self.assertTrue(rule["text"])
        self.assertEqual(after["persona_version"], result["published_version"])
        # Autonomous evolution stays off and is not what produced this (G06).
        self.assertEqual(
            self.service.core.persona_admin_service.auto_evolution("test").mode,
            "off",
        )

    def test_the_same_saved_message_is_only_taught_once(self):
        self.record_message("t1", GREETING)
        first = self.teach("host:test:private:t1")
        revision = first["revision"]
        tone = self.tone()
        second = self.teach("host:test:private:t1", base=revision)
        self.assertEqual(second["status"], "duplicate")
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["teaching_id"], first["teaching_id"])
        # No second revision, no repeated weight, no duplicated line.
        self.assertEqual(self.card()["revision"], revision)
        self.assertEqual(self.tone(), tone)
        self.assertEqual(self.tone().count("我顺着话题自然接着聊"), 1)
        self.assertEqual(len(self.service.persona_teaching.rules("test")), 1)

    def test_a_stale_base_revision_conflicts_and_carries_the_current_card(self):
        self.record_message("t1", GREETING)
        error = self.assert_api_error(
            "revision_conflict",
            lambda: self.teach("host:test:private:t1",
                               base=self.card()["revision"] - 1))
        current = error.extra["current"]
        self.assertEqual(current["revision"], self.card()["revision"])
        self.assertTrue(any(section["section_id"] == "identity"
                            for section in current["sections"]))
        # Nothing was written on the conflict path.
        self.assertEqual(self.tone(), "说话温柔。")

    def test_only_the_next_turn_target_is_accepted(self):
        self.record_message("t1", GREETING)
        self.assert_api_error("rejected", lambda: self.teach(
            "host:test:private:t1", target="realtime"))
        self.assert_api_error("rejected", lambda: self.teach(
            "host:test:private:t1", target="game"))
        self.assertEqual(self.tone(), "说话温柔。")

    def test_a_transient_request_is_never_stored(self):
        """G03: one-off short reply constrains this turn, it is not a rule."""
        self.record_message("t1", "这次简短一点")
        revision = self.card()["revision"]
        result = self.teach("host:test:private:t1")
        self.assertEqual(result["status"], "transient")
        self.assertEqual(result["intent"], "transient")
        self.assertEqual(self.tone(), "说话温柔。")
        self.assertEqual(self.card()["revision"], revision)

    def test_an_unsupported_request_is_refused_with_its_own_code(self):
        self.record_message("t1", "这个 bot 挺好玩")
        error = self.assert_api_error("rejected", lambda: self.teach(
            "host:test:private:t1"))
        self.assertEqual(error.extra["reason"], "not_a_style_teaching")
        self.record_message("t2", "我开玩笑的，刚才那条别学")
        error = self.assert_api_error("rejected", lambda: self.teach(
            "host:test:private:t2"))
        self.assertEqual(error.extra["reason"], "revocation_requested")

    def test_a_frozen_tone_section_is_not_written_by_teaching(self):
        self.service.core.persona_card_service.freeze("test", "tone", True)
        self.record_message("t1", GREETING)
        error = self.assert_api_error("rejected", lambda: self.teach(
            "host:test:private:t1"))
        self.assertEqual(error.extra["reason"], "section_frozen")
        self.assertEqual(self.tone(), "说话温柔。")


class SectionShapeTests(ServiceCase):
    def test_a_card_without_a_tone_section_gets_one(self):
        """A role whose card never had a tone section still gets the rule."""
        self.service.core.persona_card_service.revise("test", {
            "base_revision": self.card()["revision"],
            "operations": [{"op": "delete", "section_id": "tone"}],
            "reason": "test_setup",
        })
        self.record_message("t1", GREETING)
        result = self.teach("host:test:private:t1")
        self.assertEqual(result["status"], "applied")
        section = next(section for section in self.card()["sections"]
                       if section["section_id"] == EXPRESSION_SECTION_ID)
        self.assertEqual(section["kind"], EXPRESSION_SECTION_ID)
        self.assertIn("我顺着话题自然接着聊", section["text"])
        self.assertFalse(section["frozen"])
        # The identity section is still the one the card started with.
        identity = next(section for section in self.card()["sections"]
                        if section["section_id"] == "identity")
        self.assertTrue(identity["frozen"])

    def _fill_tone(self, *, base: str, base_revision: int, keep_room: int) -> str:
        """Replace the tone section with authored prose sized to leave room.

        A section is capped at MAX_SECTION_TEXT; this helper works out how many
        characters of prose can be added while leaving exactly `keep_room` free,
        so a later rule either fits exactly or does not fit at all.
        """
        available = MAX_SECTION_TEXT - len(base) - 1 - keep_room
        self.assertGreater(available, 0, "the test needs a section to fill")
        prose = ("手写补充。" * (available // 5 + 1))[:available]
        receipt = self.service.core.persona_card_service.revise("test", {
            "base_revision": base_revision,
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": base + "\n" + prose}],
            "reason": "hand_edit",
        })
        self.assertEqual(receipt.status, "completed", receipt.reason)
        return prose

    def test_a_full_tone_section_drops_the_oldest_taught_rule_first(self):
        """Room is made from teaching-owned lines, never from hand-written prose."""
        self.record_message("t1", GREETING)
        first = self.teach("host:test:private:t1")
        base = self.tone()
        # Leave 20 characters: less than a new rule line needs, more than the
        # old taught line frees up.
        self._fill_tone(base=base, base_revision=first["revision"], keep_room=20)
        self.record_message("t2", "以后简短一点")
        result = self.teach("host:test:private:t2")
        self.assertEqual(result["status"], "applied")
        tone = self.tone()
        # The old taught line made way; the hand-written prose is intact.
        self.assertNotIn("我顺着话题自然接着聊", tone)
        self.assertIn("手写补充", tone)
        self.assertIn("回答更短", tone)
        superseded = self.service.persona_teaching.teaching(
            "test", first["teaching_id"])
        self.assertEqual(superseded["status"], "superseded")
        self.assertTrue(superseded["superseded_at"])

    def test_a_tone_section_with_no_room_for_the_rule_is_refused(self):
        """The refusal names the reason instead of deleting authored text."""
        base = self.tone()
        # One free character, and no taught rule to drop: the only way to fit
        # would be to delete prose the user wrote, which is refused instead.
        self._fill_tone(base=base, base_revision=self.card()["revision"],
                        keep_room=1)
        before = self.tone()
        self.record_message("t1", GREETING)
        error = self.assert_api_error("rejected", lambda: self.teach(
            "host:test:private:t1"))
        self.assertEqual(error.extra["reason"], "tone_section_full")
        self.assertEqual(self.tone(), before)
        self.assertEqual(self.card()["revision"], 3)


class AuthorisedExampleTests(ServiceCase):
    def test_an_authorised_example_is_referenced_but_never_replayed(self):
        self.record_message("t1", "你看这个 bot 的接话，我希望你学这种感觉")
        self.record_message("t2", "以后别每次都总结")
        result = self.teach("host:test:private:t2", quoted_example={
            "source_key": "host:test:private:t1",
        })
        self.assertEqual(result["status"], "applied")
        record = self.service.persona_teaching.teaching(
            "test", result["teaching_id"])
        self.assertEqual(record["quoted_example"]["source_key"],
                         "host:test:private:t1")
        self.assertIn("这个 bot 的接话", record["quoted_example"]["text"])
        # The rule cites the example; the card and the prompt never contain it.
        self.assertNotIn("这个 bot 的接话", self.tone())
        context = asyncio.run(self.api.context(dict(self.scope, text="随便聊聊")))
        self.assertNotIn("这个 bot 的接话", context["persona"])
        self.assertNotIn("这个 bot 的接话", json.dumps(context["expression"]))
        # An example must itself be a saved message.
        self.record_message("t3", "以后简短一点")
        self.assert_api_error("not_found", lambda: self.teach(
            "host:test:private:t3",
            quoted_example={"source_key": "host:test:private:nope"}))


class RevocationTests(ServiceCase):
    def test_revoking_one_teaching_leaves_the_other_rules_in_place(self):
        """G07: take back that one, without touching any other rule."""
        self.record_message("t1", GREETING)
        self.record_message("t2", SECOND)
        first = self.teach("host:test:private:t1")
        second = self.teach("host:test:private:t2")
        self.assertNotEqual(first["teaching_id"], second["teaching_id"])
        tone = self.tone()
        self.assertIn("我顺着话题自然接着聊", tone)
        self.assertIn("更亲近", tone)
        revoked = self.revoke(teaching_id=first["teaching_id"])
        self.assertEqual(revoked["status"], "revoked")
        tone_after = self.tone()
        self.assertNotIn("我顺着话题自然接着聊", tone_after)
        self.assertIn("更亲近", tone_after)
        # The hand-written tone text survives, and the other rule is still live.
        self.assertIn("说话温柔", tone_after)
        remaining = [rule["teaching_id"]
                     for rule in self.service.persona_teaching.rules("test")]
        self.assertEqual(remaining, [second["teaching_id"]])
        # Revoking again is a duplicate, not an error and not a new revision.
        again = self.revoke(teaching_id=first["teaching_id"],
                            base=revoked["revision"])
        self.assertEqual(again["status"], "duplicate")
        self.assertTrue(again["duplicate"])

    def test_revocation_by_source_key_and_a_conflict_carries_the_card(self):
        self.record_message("t1", GREETING)
        result = self.teach("host:test:private:t1")
        error = self.assert_api_error(
            "revision_conflict",
            lambda: self.revoke(teaching_id=result["teaching_id"],
                                base=result["revision"] - 1))
        self.assertEqual(error.extra["current"]["revision"], result["revision"])
        self.assertIn("我顺着话题自然接着聊", self.tone())
        revoked = self.revoke(source_key="host:test:private:t1")
        self.assertEqual(revoked["status"], "revoked")
        self.assertNotIn("我顺着话题自然接着聊", self.tone())

    def test_revoking_an_unknown_teaching_is_refused(self):
        self.assert_api_error("rejected", lambda: self.revoke(
            teaching_id="teach_nope"))

    def test_a_revoked_teaching_cannot_be_taught_again_from_the_same_source(self):
        self.record_message("t1", GREETING)
        result = self.teach("host:test:private:t1")
        self.revoke(teaching_id=result["teaching_id"])
        tone = self.tone()
        again = self.teach("host:test:private:t1")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(self.tone(), tone, "a revoked teaching stays revoked")


class RestartTests(ServiceCase):
    def test_rules_survive_a_restart_and_replay_stays_healthy(self):
        """G01: the rule is still there after a real stop/start cycle."""
        self.record_message("t1", GREETING)
        result = self.teach("host:test:private:t1")
        tone = self.tone()
        self.restart()
        # The journal replay ran (that is what recover() does) and did not pause
        # on the teaching event: its handler is registered before it.
        recovery = self.service.state.recovery
        self.assertFalse(recovery.get("paused"), recovery)
        self.assertEqual(self.tone(), tone)
        rules = self.service.persona_teaching.rules("test")
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["teaching_id"], result["teaching_id"])
        context = asyncio.run(self.api.context(dict(self.scope, text="今天好累")))
        self.assertIn("我顺着话题自然接着聊", context["persona"])
        self.assertEqual(context["projection"][EXPRESSION_SECTION_ID], tone)
        # And the teaching log itself survived, not just the card text.
        stored = self.service.persona_teaching.teaching(
            "test", result["teaching_id"])
        self.assertEqual(stored["status"], "active")
        self.assertEqual(stored["source_key"], "host:test:private:t1")

    def test_the_log_is_rebuilt_from_the_journal_on_startup(self):
        """The replay handler must exist before recover(), or the round pauses.

        The crash window is real: the journal entry is durable and the derived
        file is not. Removing both the log and its replay marker makes the next
        startup replay the whole journal, which is exactly where an unregistered
        event type would pause recovery and lose the teaching.
        """
        self.record_message("t1", GREETING)
        result = self.teach("host:test:private:t1")
        tone = self.tone()
        character_dir = self.root / "test"
        stored = character_dir / "persona_teachings.json"
        self.assertTrue(stored.exists())
        stored.unlink()
        # Forget how far the replayer got, so the teaching event is replayed.
        applied = character_dir / "events_applied.json"
        if applied.exists():
            applied.unlink()
        self.restart()
        recovery = self.service.state.recovery
        self.assertFalse(recovery.get("paused"), recovery)
        rebuilt = self.service.persona_teaching.teaching(
            "test", result["teaching_id"])
        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt["status"], "active")
        self.assertEqual(rebuilt["source_key"], "host:test:private:t1")
        # The card was already authoritative; it is not re-derived from the log.
        self.assertEqual(self.tone(), tone)
        self.assertEqual(len(self.service.persona_teaching.rules("test")), 1)

    def test_a_rule_removed_by_hand_is_not_reported_as_active(self):
        self.record_message("t1", GREETING)
        result = self.teach("host:test:private:t1")
        # The user edits the section and keeps only their own prose.
        self.service.core.persona_card_service.revise("test", {
            "base_revision": result["revision"],
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": "说话温柔。"}],
            "reason": "hand_edit",
        })
        self.assertEqual(self.service.persona_teaching.rules("test"), ())


class DerivationTests(unittest.TestCase):
    """The deterministic derivation table (no service, no IO)."""

    def test_persistent_requests_produce_rules(self):
        self.assertEqual(derive_teaching(GREETING).intent, "persistent")
        self.assertTrue(derive_teaching(GREETING).rules)
        self.assertTrue(derive_teaching("你说话太人机了，要改").rules)

    def test_transient_and_opinion_and_identity_are_all_kept_apart(self):
        transient = derive_teaching("这次简短一点")
        self.assertEqual(transient.intent, "transient")
        self.assertEqual(transient.rules, ())
        opinion = derive_teaching("这个 bot 挺好玩")
        self.assertEqual(opinion.reject_reason, "not_a_style_teaching")
        identity = derive_teaching("你以后叫小蓝")
        self.assertEqual(identity.reject_reason, "not_a_style_teaching")
        revoke = derive_teaching("我开玩笑的，刚才那条别学")
        self.assertEqual(revoke.reject_reason, "revocation_requested")

    def test_one_message_yields_at_most_three_short_lines(self):
        derived = derive_teaching(
            "以后别每次都总结，也别一直追问，不要那么啰嗦，说话温柔一点")
        self.assertLessEqual(len(derived.rules), 3)
        for line in derived.rules:
            self.assertLessEqual(len(line), 160)


class HttpTests(ServiceCase):
    """The real transport: capability headers, status codes and bodies."""

    def setUp(self):
        super().setUp()
        self.server = build_server(self.api, host="127.0.0.1", port=0,
                                   management=self.management)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        super().tearDown()

    def call(self, method, path, body=None, token="granted"):
        headers = {"Content-Type": "application/json",
                   "X-Companion-Client": "chat" if token == "granted" else "plain",
                   "X-Companion-Token": os.environ.get(
                       "TEST_CHAT_TOKEN" if token == "granted" else "TEST_PLAIN_TOKEN",
                       "")}
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_teach_over_http_grants_duplicates_and_conflicts(self):
        env = {"TEST_CHAT_TOKEN": "t-chat", "TEST_PLAIN_TOKEN": "t-plain"}
        with patch.dict(os.environ, env):
            self.record_message("t1", GREETING)
            revision = self.card()["revision"]
            status, body = self.call("POST", "/v1/persona/teach", {
                "source_key": "host:test:private:t1",
                "base_revision": revision, "target": "next_turn",
            })
            self.assertEqual(status, 200, body)
            self.assertEqual(body["status"], "applied")
            self.assertEqual(body["target"], "next_turn")
            self.assertTrue(body["published_version"])
            # A second identical submission is a duplicate, not a revision.
            status, body = self.call("POST", "/v1/persona/teach", {
                "source_key": "host:test:private:t1",
                "base_revision": revision + 1,
            })
            self.assertEqual(status, 200, body)
            self.assertEqual(body["status"], "duplicate")
            # A stale revision is 409 and carries the current card.
            status, body = self.call("POST", "/v1/persona/teach", {
                "source_key": "host:test:private:t1",
                "base_revision": revision - 1,
            })
            self.assertEqual(status, 409)
            self.assertEqual(body["code"], "revision_conflict")
            self.assertIn("sections", body["current"])
            # A client without the grant never reaches the handler.
            status, body = self.call("POST", "/v1/persona/teach", {
                "source_key": "host:test:private:t1",
                "base_revision": revision + 1,
            }, token="plain")
            self.assertEqual(status, 403)
            self.assertNotIn("sections", body)
            # The list route reports what is active.
            status, body = self.call("GET", "/v1/persona/teachings")
            self.assertEqual(status, 200, body)
            self.assertEqual(len(body["items"]), 1)
            self.assertEqual(body["items"][0]["status"], "active")
            self.assertEqual(len(body["active_rule_ids"]), 1)

    def test_revoke_over_http_removes_only_that_rule(self):
        env = {"TEST_CHAT_TOKEN": "t-chat", "TEST_PLAIN_TOKEN": "t-plain"}
        with patch.dict(os.environ, env):
            self.record_message("t1", GREETING)
            revision = self.card()["revision"]
            status, applied = self.call("POST", "/v1/persona/teach", {
                "source_key": "host:test:private:t1", "base_revision": revision,
            })
            self.assertEqual(status, 200, applied)
            status, body = self.call("POST", "/v1/persona/teach/revoke", {
                "teaching_id": applied["teaching_id"],
                "base_revision": applied["revision"],
            })
            self.assertEqual(status, 200, body)
            self.assertEqual(body["status"], "revoked")
            card = self.card()
            tone = next(section["text"] for section in card["sections"]
                        if section["section_id"] == EXPRESSION_SECTION_ID)
            self.assertEqual(tone, "说话温柔。")


if __name__ == "__main__":
    unittest.main()
