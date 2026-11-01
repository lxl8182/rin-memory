"""M6-A server-side integration: the plugin gate (B) and receipt closure (C).

Two security-shaped behaviours are pinned here, both over the real HTTP surface:

**B. The plugin source gate.** `_saved_user_message` used to refuse every plugin
source because a plugin event is not this conversation own record. That blanket
rule made the QQ teaching path 100% dead (the DSH source key carries a plugin
segment). It is now four AND-ed conditions -- the identity holds persona_teach,
the source subject is one of this identity subjects, the source character is this
service character, and the plugin id is in this identity trusted list -- while the
host-source path is untouched.

**C. Receipts: adoption vs actual delivery.** The existing used/sent/delivered
kinds keep their exact meaning and their exact fields; the closure is additive
(an optional output_id on a receipt) plus one read view that answers both
questions for one request from the stored rows.
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


class ServiceHarness(unittest.TestCase):
    """One real service on a synthetic root; no model, no network."""

    CLIENTS = {
        # A QQ-like client: teaching granted, trusted to cite its own plugin only.
        "qq": {
            "token_env": "TEST_QQ_TOKEN",
            "subjects": ["private"],
            "plugin_id": "dsh-rin-memory",
            "persona_teach": True,
            "trusted_plugins": ["dsh-rin-memory"],
        },
        # Same capability, but no trusted plugin: every plugin source is refused.
        "qq-untrusted": {
            "token_env": "TEST_QQ_UNTRUSTED_TOKEN",
            "subjects": ["private"],
            "plugin_id": "dsh-rin-memory",
            "persona_teach": True,
        },
        # Trusts the plugin id but has no teaching capability.
        "no-teach": {
            "token_env": "TEST_NO_TEACH_TOKEN",
            "subjects": ["private"],
            "trusted_plugins": ["dsh-rin-memory"],
        },
        # Trusted plugin and capability, but a different subject.
        "other-subject": {
            "token_env": "TEST_OTHER_SUBJECT_TOKEN",
            "subjects": ["friend"],
            "plugin_id": "dsh-rin-memory",
            "persona_teach": True,
            "trusted_plugins": ["dsh-rin-memory"],
        },
        # Trusts the plugin AND is scoped to two subjects, but has no teaching
        # capability. The pairing that a "trusted plugin" must never compensate.
        "trusted-only": {
            "token_env": "TEST_TRUSTED_ONLY_TOKEN",
            "subjects": ["private"],
            "trusted_plugins": ["dsh-rin-memory"],
        },
        # A multi-subject identity: the only shape where "request subject" and
        # "source subject" can legitimately both be inside the identity and still
        # disagree.
        "multi-subject": {
            "token_env": "TEST_MULTI_SUBJECT_TOKEN",
            "subjects": ["private", "friend"],
            "plugin_id": "dsh-rin-memory",
            "persona_teach": True,
            "trusted_plugins": ["dsh-rin-memory"],
        },
        # A plain host client: no plugin binding at all, so it speaks host keys.
        "desk": {
            "token_env": "TEST_DESK_TOKEN",
            "subjects": ["private"],
            "persona_teach": True,
            "memory_outputs": True,
        },
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "memory"
        self.root.mkdir(parents=True)
        self.env = patch.dict(os.environ, {
            "TEST_QQ_TOKEN": "t-qq",
            "TEST_QQ_UNTRUSTED_TOKEN": "t-qq-untrusted",
            "TEST_NO_TEACH_TOKEN": "t-no-teach",
            "TEST_OTHER_SUBJECT_TOKEN": "t-other-subject",
            "TEST_DESK_TOKEN": "t-desk",
            "TEST_TRUSTED_ONLY_TOKEN": "t-trusted-only",
            "TEST_MULTI_SUBJECT_TOKEN": "t-multi-subject",
        })
        self.env.start()
        self.settings = ServiceSettings(
            memory_root=self.root, character="test",
            clients={key: dict(value) for key, value in self.CLIENTS.items()},
            require_published_persona=False,
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
        self.server = build_server(
            self.api, host="127.0.0.1", port=0, management=self.management,
        )
        self.port = self.server.server_address[1]
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

    # -- transport ------------------------------------------------------

    TOKENS = {
        "qq": "t-qq",
        "qq-untrusted": "t-qq-untrusted",
        "no-teach": "t-no-teach",
        "other-subject": "t-other-subject",
        "desk": "t-desk",
        "trusted-only": "t-trusted-only",
        "multi-subject": "t-multi-subject",
    }

    def call(self, method, path, body=None, *, client="qq"):
        headers = {"Content-Type": "application/json",
                   "X-Companion-Client": client,
                   "X-Companion-Token": self.TOKENS[client]}
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

    def card_revision(self):
        return self.management.host_persona()["revision"]

    def tone(self):
        return next(
            section["text"] for section in self.management.host_persona()["sections"]
            if section["section_id"] == "tone"
        )

    def teach(self, source_key, *, client="qq", base=None, target="next_turn"):
        return self.call("POST", "/v1/persona/teach", {
            "source_key": source_key,
            "base_revision": self.card_revision() if base is None else base,
            "target": target,
        }, client=client)


class PluginGateTests(ServiceHarness):
    """B: subjects AND trusted plugin AND character AND capability."""

    PLUGIN_KEY = (
        "plugin:dsh-rin-memory:test:private:qq-session:req-1"
    )

    def save_plugin_message(self, text="以后别每次都总结，像朋友一样接着聊"):
        """Save one user message whose source key is a plugin source."""
        status, payload = self.call("POST", "/v1/turns", {
            "request_id": "req-1",
            "turn_id": "req-1",
            "source_key": self.PLUGIN_KEY,
            "content_class": "chat",
            "messages": [{"role": "user", "content": text}],
            "reply": "嗯。",
        })
        self.assertEqual(status, 200, payload)
        return payload

    def test_a_trusted_plugin_source_is_allowed(self):
        self.save_plugin_message()
        status, payload = self.teach(self.PLUGIN_KEY)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "applied", payload)
        self.assertTrue(payload["published_version"])
        # The derived rule really landed in the tone section.
        self.assertIn("自然接着聊", self.tone())

    def test_the_same_plugin_is_refused_when_not_trusted(self):
        """The easiest case to miss: capability yes, plugin id not in the list."""
        self.save_plugin_message()
        status, payload = self.teach(self.PLUGIN_KEY, client="qq-untrusted")
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["code"], "forbidden")
        self.assertIn("not trusted", payload["message"])
        self.assertEqual(self.tone(), "说话温柔。")

    def test_a_trusted_plugin_source_is_refused_without_the_capability(self):
        self.save_plugin_message()
        status, payload = self.teach(self.PLUGIN_KEY, client="no-teach")
        self.assertEqual(status, 403, payload)
        self.assertEqual(self.tone(), "说话温柔。")

    def test_a_trusted_plugin_source_is_refused_for_another_subject(self):
        self.save_plugin_message()
        status, payload = self.teach(self.PLUGIN_KEY, client="other-subject")
        self.assertEqual(status, 403, payload)
        self.assertEqual(self.tone(), "说话温柔。")

    def test_a_trusted_plugin_source_is_refused_for_another_character(self):
        self.save_plugin_message()
        foreign = "plugin:dsh-rin-memory:someone-else:private:qq-session:req-1"
        status, payload = self.teach(foreign)
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["code"], "scope_mismatch")
        self.assertEqual(self.tone(), "说话温柔。")

    def test_host_sources_keep_their_exact_previous_behaviour(self):
        """The host path must be byte-for-byte what it was before M6-A."""
        host_key = "host:test:private:h1"
        status, payload = self.call("POST", "/v1/turns", {
            "request_id": "h1",
            "turn_id": "h1",
            "source_key": host_key,
            "content_class": "chat",
            "messages": [{"role": "user", "content": "以后别每次都总结"}],
            "reply": "嗯。",
        }, client="desk")
        self.assertEqual(status, 200, payload)
        # A host source teaches with an empty trusted_plugins list: unchanged.
        status, payload = self.teach(host_key, client="desk")
        self.assertEqual(status, 200, payload)
        self.assertIn("自然接着聊", self.tone())
        # ...and a host source outside this subject is still rejected.
        status, payload = self.teach("host:test:friend:h9", client="desk")
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["code"], "scope_mismatch")
        # ...and an unknown host source is still a 404, not a 403.
        status, payload = self.teach("host:test:private:missing", client="desk")
        self.assertEqual(status, 404, payload)

    def test_a_trusted_plugin_never_substitutes_for_the_capability(self):
        """Lead review: persona_teach=false + plugin in the list must still be 403.

        The two switches are independent, so someone may configure
        trusted_plugins after reading only the plugin half of the docs. If a
        trusted id could satisfy the capability check, that configuration would
        silently grant teaching power. The order is: capability first, then the
        allow-list -- this pins it.
        """
        self.save_plugin_message()
        status, payload = self.teach(self.PLUGIN_KEY, client="trusted-only")
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["code"], "forbidden")
        self.assertIn("teaching", payload["message"])
        self.assertEqual(self.tone(), "说话温柔。")
        # The same identity really does hold the trusted id: it is the grant that
        # is missing, not the allow-list.
        identity = self.management.identity("trusted-only", "t-trusted-only")
        self.assertEqual(identity["trusted_plugins"], ["dsh-rin-memory"])
        self.assertFalse(identity.get("persona_teach"))

    def test_a_foreign_request_subject_is_refused_by_the_transport(self):
        """The HTTP layer still narrows a teaching to the configured subject."""
        self.save_plugin_message()
        status, payload = self.call("POST", "/v1/persona/teach", {
            "source_key": self.PLUGIN_KEY,
            "subject": "friend",
            "base_revision": self.card_revision(),
            "target": "next_turn",
        }, client="multi-subject")
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["code"], "scope_mismatch")
        self.assertEqual(self.tone(), "说话温柔。")

    def test_the_request_subject_and_the_source_subject_must_agree(self):
        """Defense in depth below the transport, where the two can differ.

        The transport forces a request subject onto the configured one, so this
        comparison is only reachable through the API surface an embedder or a
        console route calls directly. That is exactly why it is worth pinning: a
        client scoped to two subjects may legitimately ask for either, and the
        cited message may belong to the other one -- every individual gate passes
        and only the comparison refuses it.
        """
        from rin_memory.service.console import ApiError

        self.save_plugin_message()
        identity = {
            "client_id": "multi-subject",
            "subjects": ["private", "friend"],
            "admin": False,
            "persona_teach": True,
            "trusted_plugins": ["dsh-rin-memory"],
        }
        payload = {
            "source_key": self.PLUGIN_KEY,
            "subject": "friend",
            "base_revision": self.card_revision(),
            "target": "next_turn",
        }
        with self.assertRaises(ApiError) as caught:
            self.management.persona_teach(payload, identity)
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertEqual(caught.exception.message, "source is outside this subject")
        self.assertEqual(
            (caught.exception.extra or {}).get("source_subject"), "private",
        )
        self.assertEqual(
            (caught.exception.extra or {}).get("request_subject"), "friend",
        )
        self.assertEqual(self.tone(), "说话温柔。")
        # The aligned case is allowed: same subject on both sides.
        result = self.management.persona_teach(
            dict(payload, subject="private"), identity,
        )
        self.assertEqual(result["status"], "applied", result)

    def test_the_default_configuration_trusts_no_plugin(self):
        """An upgrade alone must not open the gate."""
        from rin_memory.stack_config import initialize

        config_dir = Path(self.tmp.name) / "cfg"
        config = initialize(config_dir / "stack.toml")
        spec = config.service.clients["neko-desktop"]
        self.assertEqual(spec.get("trusted_plugins"), [])
        text = (config_dir / "stack.toml").read_text(encoding="utf-8")
        self.assertIn("trusted_plugins = []", text)

    def test_the_identity_exposes_the_trusted_list(self):
        identity = self.management.identity("qq", "t-qq")
        self.assertEqual(identity["trusted_plugins"], ["dsh-rin-memory"])
        self.assertTrue(identity["persona_teach"])
        untrusted = self.management.identity("qq-untrusted", "t-qq-untrusted")
        self.assertEqual(untrusted["trusted_plugins"], [])


class ReceiptClosureTests(ServiceHarness):
    """C: existing receipt values do not move; the closure is additive."""

    def call(self, method, path, body=None, *, client="desk"):
        # Every call in this class is one host client speaking about its own
        # turns, so the default client is the one that committed them.
        return super().call(method, path, body, client=client)

    def save_turn(self, request_id="r1"):
        status, payload = self.call("POST", "/v1/turns", {
            "request_id": request_id,
            "turn_id": request_id,
            "source_key": "host:test:private:" + request_id,
            "content_class": "chat",
            "messages": [{"role": "user", "content": "今天有点累。"}],
            "reply": "那就早点休息。",
        }, client="desk")
        self.assertEqual(status, 200, payload)
        return payload

    def scope(self, request_id="r1"):
        # A receipt is bound to the same identity and scope the turn was
        # committed under, exactly like the existing contract requires.
        return {"request_id": request_id, "subject": "private",
                "session_id": "", "scene": "", "client_id": "desk"}

    def test_used_sent_and_delivered_keep_their_exact_values(self):
        self.save_turn("r1")
        scope = self.scope("r1")
        status, used = self.call("POST", "/v1/receipts", dict(
            scope, receipt_id="used:r1", kind="used", consumer="chat",
        ))
        self.assertEqual(status, 200, used)
        # The fields the old contract promised are all still there, unchanged.
        self.assertEqual(used["receipt_id"], "used:r1")
        self.assertEqual(used["request_id"], "r1")
        self.assertEqual(used["kind"], "used")
        self.assertEqual(used["consumer"], "chat")
        self.assertEqual(used["source_key"], "host:test:private:r1")
        self.assertEqual(used["scope"]["subject"], "private")
        self.assertEqual(used["durable"], True)
        self.assertEqual(used["duplicate"], False)
        self.assertNotIn("output_id", used)  # additive: absent unless named

        status, sent = self.call("POST", "/v1/receipts", dict(
            scope, receipt_id="sent:r1", kind="sent", consumer="chat",
            transport="qq_official",
            parts=[{"platform_id": "m-1", "chars": 3},
                   {"platform_id": "m-2", "chars": 4}],
        ))
        self.assertEqual(status, 200, sent)
        self.assertEqual(sent["kind"], "sent")
        self.assertEqual(sent["transport"], "qq_official")
        self.assertEqual(sent["sent_chars"], 7)
        self.assertEqual(
            [part["platform_id"] for part in sent["parts"]], ["m-1", "m-2"],
        )
        self.assertEqual([part["index"] for part in sent["parts"]], [0, 1])
        self.assertEqual([part["chars"] for part in sent["parts"]], [3, 4])

        status, delivered = self.call("POST", "/v1/receipts", dict(
            scope, receipt_id="delivered:r1", kind="delivered", consumer="chat",
            delivered_chars=7,
        ))
        self.assertEqual(status, 200, delivered)
        self.assertEqual(delivered["kind"], "delivered")
        self.assertEqual(delivered["delivered_chars"], 7)
        self.assertNotIn("output_id", delivered)

        # The list read still reports exactly the old row shape plus `status`.
        status, listing = self.call("GET", "/v1/receipts?" + urllib.parse.urlencode({
            "subject": "private", "session_id": "", "scene": "", "client_id": "desk",
        }))
        self.assertEqual(status, 200, listing)
        kinds = sorted(row["kind"] for row in listing["items"])
        self.assertEqual(kinds, ["delivered", "sent", "used"])
        for row in listing["items"]:
            self.assertEqual(row["status"], "recorded")
            self.assertEqual(row["request_id"], "r1")

    def test_a_sent_receipt_still_cannot_claim_delivery(self):
        """`sent` is a transport fact; it never becomes a display fact."""
        self.save_turn("r2")
        scope = self.scope("r2")
        status, sent = self.call("POST", "/v1/receipts", dict(
            scope, receipt_id="sent:r2", kind="sent", consumer="chat",
            transport="qq_official", parts=[{"platform_id": "m", "chars": 2}],
        ))
        self.assertEqual(status, 200, sent)
        status, listing = self.call("GET", "/v1/receipts?" + urllib.parse.urlencode({
            "subject": "private", "session_id": "", "scene": "", "client_id": "desk",
        }))
        kinds = {row["kind"] for row in listing["items"]}
        self.assertIn("sent", kinds)
        self.assertNotIn("delivered", kinds)

    def test_a_receipt_may_name_its_output_record(self):
        """The additive link: a receipt can point at the body it is about."""
        self.save_turn("r3")
        status, created = self.call("POST", "/v1/outputs", {
            "text": "面试加油。明天记得告诉我结果。",
            "kind": "proactive",
            "subject": "private",
            "request_id": "r3",
            "cause": {"kind": "open_thread", "request_id": "r3",
                      "source_keys": ["host:test:private:e_i"]},
        })
        self.assertEqual(status, 200, created)
        output_id = created["output_id"]
        self.call("POST", "/v1/outputs/deliver", {
            "output_id": output_id, "parts": [{"index": 0}],
        })
        status, receipt = self.call("POST", "/v1/receipts", dict(
            self.scope("r3"), receipt_id="sent:r3", kind="sent",
            consumer="chat", transport="host", output_id=output_id,
            parts=[{"platform_id": "out-1", "chars": 4}],
        ))
        self.assertEqual(status, 200, receipt)
        self.assertEqual(receipt["output_id"], output_id)
        # A receipt for another subject output is a 404, never a silent link.
        status, refused = self.call("POST", "/v1/receipts", dict(
            self.scope("r3"), receipt_id="sent:r3-bad", kind="sent",
            consumer="chat", transport="host", output_id="out_nope",
            parts=[{"platform_id": "out-1", "chars": 4}],
        ))
        self.assertEqual(status, 404, refused)
        # ...and the summary joins the two records it was told about.
        status, summary = self.call(
            "GET", "/v1/receipts/summary?" + urllib.parse.urlencode({
                "request_id": "r3", "subject": "private",
                "session_id": "", "scene": "", "client_id": "desk",
            }),
        )
        self.assertEqual(status, 200, summary)
        self.assertEqual(
            [row["output_id"] for row in summary["outputs"]], [output_id],
        )
        self.assertGreater(summary["output_delivered_chars"], 0)

    def test_the_summary_answers_adoption_and_delivery_separately(self):
        self.save_turn("r4")
        scope = self.scope("r4")
        self.call("POST", "/v1/receipts", dict(
            scope, receipt_id="used:r4", kind="used", consumer="chat",
        ))
        self.call("POST", "/v1/receipts", dict(
            scope, receipt_id="delivered:r4", kind="delivered", consumer="chat",
            delivered_chars=len("那就早点休息。"),
        ))
        status, summary = self.call(
            "GET", "/v1/receipts/summary?" + urllib.parse.urlencode({
                "request_id": "r4", "subject": "private",
                "session_id": "", "scene": "", "client_id": "desk",
            }),
        )
        self.assertEqual(status, 200, summary)
        self.assertEqual(summary["request_id"], "r4")
        self.assertEqual(summary["status"], "recorded")
        # Adoption is about the persona version, never about the text.
        self.assertIn("persona_version", summary["adopted"])
        # Delivery is about the text, read from the delivered receipt.
        self.assertEqual(summary["delivered"]["text_chars"], len("那就早点休息。"))
        self.assertEqual(
            summary["delivered"]["delivered_chars"], len("那就早点休息。"),
        )
        self.assertTrue(summary["delivered"]["complete"])
        self.assertEqual(summary["receipt_kinds"], ["delivered", "used"])
        self.assertEqual(summary["outputs"], [])

    def test_the_summary_reports_a_partial_transport_honestly(self):
        self.save_turn("r5")
        scope = self.scope("r5")
        self.call("POST", "/v1/receipts", dict(
            scope, receipt_id="sent:r5", kind="sent", consumer="chat",
            transport="qq_official",
            parts=[{"platform_id": "m-1", "chars": 3}],
        ))
        status, summary = self.call(
            "GET", "/v1/receipts/summary?" + urllib.parse.urlencode({
                "request_id": "r5", "subject": "private",
                "session_id": "", "scene": "", "client_id": "desk",
            }),
        )
        self.assertEqual(status, 200, summary)
        # Sent three characters of eight: accepted, not yet displayed.
        self.assertEqual(summary["delivered"]["sent_chars"], 3)
        self.assertEqual(summary["delivered"]["delivered_chars"], 0)
        self.assertFalse(summary["delivered"]["complete"])
        self.assertEqual(
            [part["platform_id"] for part in summary["delivered"]["transport_parts"]],
            ["m-1"],
        )

    def test_an_unknown_request_in_this_scope_is_not_found(self):
        status, payload = self.call(
            "GET", "/v1/receipts/summary?request_id=does-not-exist",
        )
        self.assertEqual(status, 404, payload)


if __name__ == "__main__":
    unittest.main()
