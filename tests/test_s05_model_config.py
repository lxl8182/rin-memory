"""Console model configuration: URL, credential, model names, thinking effort.

The page's settings form is the only place a first run can configure a provider
(it has no `stack.toml` yet and cannot set environment variables), so this file
is the whole contract for that: what is accepted, where it is stored, what is
applied immediately, what survives a restart, and -- most importantly -- what is
never returned.

Credential rules are the reason most of these tests exist. Workspace policy is
"凭据由环境或本机受控配置提供，不进入命令行参数、前端响应、普通日志和导出", so
every path here is asserted to keep the value out of responses and out of the
export snapshot.
"""

import argparse
import asyncio
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import sys

REPO = Path(__file__).resolve().parents[1]
for extra in (REPO, REPO / "webui", REPO / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import server as webui_server  # noqa: E402

from rin_memory.adapters.model_http import (  # noqa: E402
    HttpModelInvoker,
    ModelSettings,
)
from rin_memory.service.memory_service import (  # noqa: E402
    MemoryService,
    ServiceSettings,
)
from rin_memory.service.model_config import (  # noqa: E402
    ModelConfigError,
    apply_model_config,
    load_model_config,
    public_model_config,
    save_model_config,
)


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        memory_root="", demo_root="", allow_write=False, character="",
        model=[], model_base_url="", model_api_key_env="", model_timeout_s=60.0,
        demo=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class ModelSettingsTests(unittest.TestCase):
    """The adapter: resolution order, source reporting and request payload."""

    def test_stored_key_wins_over_the_environment(self):
        settings = ModelSettings(
            base_url="https://x/v1", models={"conversation": "m"},
            api_key_env="CM_TEST_KEY", api_key_stored="sk-stored",
        )
        os.environ["CM_TEST_KEY"] = "sk-env"
        try:
            self.assertEqual(settings.api_key(), "sk-stored")
            self.assertEqual(settings.api_key_source(), "console")
        finally:
            os.environ.pop("CM_TEST_KEY", None)

    def test_environment_is_used_when_nothing_is_stored(self):
        settings = ModelSettings(
            base_url="https://x/v1", models={"conversation": "m"},
            api_key_env="CM_TEST_KEY2",
        )
        os.environ["CM_TEST_KEY2"] = "sk-env"
        try:
            self.assertEqual(settings.api_key(), "sk-env")
            self.assertEqual(settings.api_key_source(), "environment")
        finally:
            os.environ.pop("CM_TEST_KEY2", None)
        self.assertEqual(settings.api_key_source(), "none")

    def test_reasoning_effort_only_sends_a_known_level(self):
        base = ModelSettings(base_url="https://x/v1", models={"conversation": "m"})
        self.assertEqual(base.provider_options(), {})
        for level in ("low", "medium", "high"):
            settings = ModelSettings(
                base_url="https://x/v1", models={"conversation": "m"},
                reasoning_effort=level,
            )
            self.assertEqual(settings.provider_options(), {"reasoning_effort": level})
        # An unknown level must send nothing rather than an invalid field.
        odd = ModelSettings(
            base_url="https://x/v1", models={"conversation": "m"},
            reasoning_effort="extreme",
        )
        self.assertEqual(odd.provider_options(), {})

    def test_update_settings_keeps_the_shared_invoker_identity(self):
        first = ModelSettings(
            base_url="https://a/v1", models={"conversation": "a"},
            max_concurrency=2,
        )
        invoker = HttpModelInvoker(first)
        slots = invoker._slots
        second = ModelSettings(
            base_url="https://b/v1", models={"conversation": "b"},
            max_concurrency=9, reasoning_effort="high",
        )
        invoker.update_settings(second)
        self.assertIs(invoker.settings, second)
        # Capacity is not swapped: a new semaphore would admit a fresh batch
        # while the old batch is still running (the over-capacity state R07
        # removed). It takes effect on restart instead.
        self.assertIs(invoker._slots, slots)


class StorageTests(unittest.TestCase):
    """The on-disk file: validation, normalisation and fail-closed reads."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="companion cfg ")
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "local_state"

    def test_save_normalises_and_round_trips(self):
        saved = save_model_config(self.state, {
            "base_url": "https://api.example.com/v1/chat/completions",
            "models": {"conversation": " gpt-a ", "summary": "", "correction": "c"},
            "api_key": "  sk-abc  ",
            "reasoning_effort": "HIGH",
        })
        self.assertEqual(saved["base_url"], "https://api.example.com/v1")
        self.assertEqual(saved["models"], {"conversation": "gpt-a", "correction": "c"})
        self.assertEqual(saved["api_key"], "sk-abc")
        self.assertEqual(saved["reasoning_effort"], "high")
        loaded = load_model_config(self.state)
        self.assertEqual(loaded["base_url"], "https://api.example.com/v1")
        self.assertEqual(loaded["api_key"], "sk-abc")
        self.assertNotIn("problem", loaded)

    def test_validation_rejects_bad_values_naming_the_field(self):
        for values, field in [
            ({"base_url": "ftp://x"}, "base_url"),
            ({"reasoning_effort": "extreme"}, "reasoning_effort"),
            ({"models": {"bogus": "m"}}, "models"),
            ({"api_key": "a" * 5000}, "api_key"),
            ({"api_key": "line1\nline2"}, "api_key"),
        ]:
            with self.assertRaises(ModelConfigError) as caught:
                save_model_config(self.state, values)
            self.assertEqual(caught.exception.field, field, values)

    def test_a_malformed_file_reads_as_unconfigured_with_a_reason(self):
        self.state.mkdir(parents=True)
        (self.state / "model_config.json").write_text("{not json", encoding="utf-8")
        loaded = load_model_config(self.state)
        self.assertEqual(loaded["base_url"], "")
        self.assertIn("problem", loaded)

    def test_apply_overlays_without_dropping_untouched_tiers(self):
        configured = ModelSettings(
            base_url="https://stack/v1",
            models={"conversation": "old", "summary": "keep-me"},
            api_key_env="CM_ENV", timeout_s=12.0, max_concurrency=2,
        )
        applied = apply_model_config(configured, {
            "base_url": "", "api_key": "", "models": {"conversation": "new"},
            "reasoning_effort": "low",
        })
        # An empty stored URL keeps the configured one; an untouched tier stays.
        self.assertEqual(applied.base_url, "https://stack/v1")
        self.assertEqual(applied.models, {"conversation": "new", "summary": "keep-me"})
        self.assertEqual(applied.timeout_s, 12.0)
        self.assertEqual(applied.max_concurrency, 2)
        self.assertEqual(applied.reasoning_effort, "low")

    def test_public_view_never_contains_the_credential(self):
        settings = ModelSettings(
            base_url="https://x/v1", models={"conversation": "m"},
            api_key_stored="sk-very-secret", reasoning_effort="medium",
        )
        payload = json.dumps(public_model_config(settings, {}), ensure_ascii=False)
        self.assertNotIn("sk-very-secret", payload)
        self.assertNotIn("very-secret", payload)
        self.assertIn('"source": "console"', payload)


class ConsoleConfigTests(unittest.TestCase):
    """The page's transport: save, apply, restart, and what it may not reveal."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="companion cfg http ")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "memory"
        self.root.mkdir()
        # A demo root is the state a first run is actually in: no model, and
        # the labelled offline stand-in answering until a provider is saved.
        self.app = webui_server.build_app(_args(demo_root=str(self.root)))
        self.addCleanup(self.app.close)
        self.server = webui_server.RinHTTPServer(("127.0.0.1", 0), webui_server.RinHandler)
        self.server.app = self.app  # type: ignore[attr-defined]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_config_round_trip_through_http_never_echoes_the_key(self):
        code, saved = self.call("POST", "/api/model/config", {
            "base_url": "https://api.example.com/v1",
            "models": {"conversation": "gpt-a", "summary": "gpt-b"},
            "api_key": "sk-http-secret-42",
            "reasoning_effort": "medium",
        })
        self.assertEqual(code, 200, saved)
        self.assertEqual(saved["base_url"], "https://api.example.com/v1")
        self.assertTrue(saved["api_key"]["stored"])
        self.assertEqual(saved["api_key"]["source"], "console")
        self.assertEqual(saved["reasoning_effort"], "medium")
        self.assertNotIn("sk-http-secret", json.dumps(saved))

        code, current = self.call("GET", "/api/model/config")
        self.assertEqual(code, 200)
        self.assertNotIn("sk-http-secret", json.dumps(current))

        code, chat = self.call("GET", "/api/chat/history")
        self.assertEqual(code, 200)
        self.assertEqual(chat["status"]["model"]["reasoning_effort"], "medium")
        self.assertEqual(chat["status"]["model"]["api_key_source"], "console")

    def test_saving_without_a_key_keeps_the_stored_one_and_clear_removes_it(self):
        self.call("POST", "/api/model/config", {
            "base_url": "https://a/v1", "models": {"conversation": "m"},
            "api_key": "sk-keep-me",
        })
        code, saved = self.call("POST", "/api/model/config", {
            "base_url": "https://b/v1", "models": {"conversation": "m2"},
        })
        self.assertEqual(code, 200)
        self.assertTrue(saved["api_key"]["stored"], "an omitted key must not clear it")
        self.assertEqual(saved["base_url"], "https://b/v1")

        code, cleared = self.call("POST", "/api/model/config", {"clear_api_key": True})
        self.assertEqual(code, 200)
        self.assertFalse(cleared["api_key"]["stored"])
        self.assertEqual(cleared["api_key"]["source"], "none")

    def test_validation_errors_are_four_hundreds_with_an_actionable_message(self):
        code, payload = self.call("POST", "/api/model/config", {"base_url": "ftp://x"})
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_argument")
        self.assertIn("http", payload["error"]["message"])
        code, payload = self.call(
            "POST", "/api/model/config", {"reasoning_effort": "extreme"},
        )
        self.assertEqual(code, 400)
        self.assertEqual(self.call("GET", "/api/model/test")[0], 405)

    def test_connection_test_reports_the_failure_kind(self):
        # Demo model: the test must refuse rather than claim the provider works.
        code, result = self.call("POST", "/api/model/test", {})
        self.assertEqual(code, 200)
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "config")
        self.assertIn("离线演示", result["message"])

        self.call("POST", "/api/model/config", {
            "base_url": "http://127.0.0.1:1/v1",
            "models": {"conversation": "m"}, "api_key": "sk-x",
        })
        code, result = self.call("POST", "/api/model/test", {})
        self.assertEqual(code, 200)
        self.assertFalse(result["ok"])
        self.assertIn(result["kind"], ("transport", "timeout"))

    def test_saving_a_real_provider_replaces_the_demo_stand_in(self):
        self.assertTrue(self.app.chat_status()["model"]["demo"])
        self.call("POST", "/api/model/config", {
            "base_url": "https://real.example/v1",
            "models": {"conversation": "m"}, "api_key": "sk-x",
        })
        status = self.app.chat_status()
        self.assertFalse(status["model"]["demo"])
        # The core's own consumers were rebound to the real invoker, so the
        # setting does not apply to chat while background stays on the stub.
        from rin_memory.adapters.model_http import HttpModelInvoker

        self.assertIsInstance(self.app.core.extraction._model, HttpModelInvoker)
        self.assertIs(
            self.app.core.extraction._model, self.app.chat._service.model,
        )
        self.assertIs(self.app.core.recall._model, self.app.chat._service.model)

    def test_config_file_is_not_part_of_the_export(self):
        self.call("POST", "/api/model/config", {
            "base_url": "https://x/v1", "models": {"conversation": "m"},
            "api_key": "sk-export-secret",
        })
        payload, _filename = self.app.export()
        self.assertNotIn("sk-export-secret", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("api_key", json.dumps(payload, ensure_ascii=False))

    def test_saved_config_survives_a_service_restart(self):
        self.call("POST", "/api/model/config", {
            "base_url": "https://persisted.example/v1",
            "models": {"conversation": "persisted-model", "summary": "summary-model"},
            "api_key": "sk-persisted",
            "reasoning_effort": "high",
        })
        self.app.close()
        settings = ServiceSettings(
            memory_root=self.root, character=self.app.character,
            local_state_root=self.root / "local_state",
        )
        service = MemoryService.start(settings)
        try:
            model = service.settings.model
            self.assertEqual(model.base_url, "https://persisted.example/v1")
            self.assertEqual(model.models["conversation"], "persisted-model")
            self.assertEqual(model.models["summary"], "summary-model")
            self.assertEqual(model.reasoning_effort, "high")
            self.assertEqual(model.api_key_source(), "console")
            self.assertIsInstance(service.model, HttpModelInvoker)
        finally:
            asyncio.run(service.stop())

    def test_a_read_only_console_can_read_but_not_save(self):
        self.app.close()
        read_only = webui_server.build_app(_args(
            memory_root=str(self.root), allow_write=False,
        ))
        try:
            config = read_only.model_config()
            self.assertTrue(config["available"])
            with self.assertRaises(webui_server.ApiError) as caught:
                read_only.save_model_config({"base_url": "https://x/v1"})
            self.assertEqual(caught.exception.status, 403)
        finally:
            read_only.close()


class ServiceModeTests(unittest.TestCase):
    """The same form through the service transport (`/v1/model/...`)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="companion cfg svc ")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "memory"
        self.root.mkdir()
        self.service = MemoryService.start(ServiceSettings(
            memory_root=self.root, character="companion",
            local_state_root=self.root / "local_state",
        ))
        from rin_memory.service.management import ManagementApi

        self.management = ManagementApi(self.service, display_name="小潮")
        self.service.core.persona_card_service.import_initial("companion", [
            {"section_id": "identity", "text": "你是小潮。"},
        ])

    def tearDown(self) -> None:
        asyncio.run(self.service.stop())

    def test_service_transport_exposes_the_same_contract(self):
        config = self.management.get("/v1/model/config", {})
        self.assertTrue(config["available"])
        self.assertEqual(
            set(config["tiers"]), {"conversation", "summary", "correction"},
        )
        saved = self.management.post("/v1/model/config", {
            "base_url": "https://svc.example/v1",
            "models": {"conversation": "svc-model"},
            "api_key": "sk-service-secret",
            "reasoning_effort": "low",
        })
        self.assertEqual(saved["api_key"]["source"], "console")
        self.assertNotIn("sk-service-secret", json.dumps(saved))
        self.assertEqual(
            self.service.settings.model.base_url, "https://svc.example/v1",
        )
        self.assertIsInstance(self.service.model, HttpModelInvoker)
        # The service's own invoker instance is the one the core now uses.
        self.assertIs(self.service.core.extraction._model, self.service.model)

    def test_service_transport_rejects_a_bad_value(self):
        from rin_memory.service.console import ApiError as ConsoleError

        with self.assertRaises(ConsoleError) as caught:
            self.management.post("/v1/model/config", {"reasoning_effort": "nope"})
        self.assertEqual(caught.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
