"""Standalone model adapter: one `ModelInvoker` over an OpenAI-compatible API.

`docs/standalone-service.md` §4.1 asks the standalone entry to implement the
existing `ModelInvoker` port (invoke by tier, bounded, cancellable) without
adding a runtime dependency to the core. This module therefore uses only the
standard library:

* requests run in a worker thread through `urllib`, with an explicit socket
  timeout passed to the call and a total deadline enforced by the caller's
  `timeout_s`;
* a semaphore bounds in-flight calls so background extraction cannot starve
  foreground conversation (§4.1: "foreground and background tasks use bounded
  concurrency");
* cancellation is honest: cancelling the await does **not** kill a thread that
  may still write back, so the semaphore is released by the worker itself and
  a cancelled call is logged as abandoned rather than silently dropped. This is
  the caveat the plan cites from the Python docs;
* credentials are read from an environment variable *name* held in config, and
  are never logged, returned, or included in an error message.

Format/transport/auth failures raise `ModelCallError` with a stable `kind` so
the service layer can map them onto 503/504 without parsing message text.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# A short-lived HTTP event loop must be able to return after timing out.
# The worker keeps its slot until urllib actually finishes.
_MODEL_WORKERS = ThreadPoolExecutor(thread_name_prefix="rin-model")


#: Failure kinds the service maps onto HTTP status / capability state.
ERROR_KINDS = frozenset({
    "auth",       # bad or missing credential: pause the capability
    "config",     # tier/model not configured: pause the capability
    "timeout",    # the request exceeded its deadline: retryable
    "rate_limit", # upstream asked us to slow down: retryable
    "transport",  # connection/refused/DNS: retryable
    "format",     # the provider answered with something unusable
})


class ModelCallError(RuntimeError):
    """A model call failed; `kind` is stable and `retryable` is authoritative."""

    def __init__(
        self, message: str, *, kind: str, retryable: bool,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind if kind in ERROR_KINDS else "transport"
        self.retryable = retryable
        self.status = status


#: Thinking-intensity levels the console may request. Empty means "let the
#: provider decide", which is the only value that sends no extra field.
REASONING_EFFORTS = frozenset({"", "low", "medium", "high"})


@dataclass(frozen=True)
class ModelSettings:
    """One provider configuration; tiers name models on it.

    `api_key_env` is the *name* of an environment variable, never the key. An
    empty mapping means "this service has no model configured", which keeps
    `/chat` closed while `/turns` (external generation) still works.

    `api_key` is a *credential saved through the local console* (the plan's
    "本机受控配置"). It is never accepted from the command line or a stack
    config file, never returned by any endpoint and never logged; only
    `api_key_source()` reports where a credential came from. A saved value wins
    over the environment variable because the user typed it for this service.
    """

    base_url: str = ""
    api_key_env: str = ""
    models: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float = 60.0
    max_concurrency: int = 4
    #: Optional explicit proxy; the memory service itself must not use one.
    proxy: str = ""
    #: Credential saved by the local console; empty means "use the env var".
    #: Deliberately *not* named `api_key`: the accessor of that name below is
    #: the resolution order, and a same-named field would shadow it.
    api_key_stored: str = ""
    #: Provider thinking intensity: "" (default) / low / medium / high.
    reasoning_effort: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.models)

    def model_for(self, tier: str) -> str | None:
        return self.models.get(tier)

    def api_key(self) -> str:
        stored = str(self.api_key_stored or "").strip()
        if stored:
            return stored
        if not self.api_key_env:
            return ""
        return os.environ.get(self.api_key_env, "").strip()

    def api_key_source(self) -> str:
        """`console`, `environment` or `none`; never the value itself."""
        if str(self.api_key_stored or "").strip():
            return "console"
        if self.api_key_env and os.environ.get(self.api_key_env, "").strip():
            return "environment"
        return "none"

    def provider_options(self) -> dict[str, Any]:
        """Provider-specific request fields this configuration asks for.

        `reasoning_effort` is the OpenAI-compatible spelling for thinking
        intensity. An empty level sends nothing at all, so a provider that does
        not know the field is unaffected by this setting.
        """
        level = str(self.reasoning_effort or "").strip().lower()
        return {"reasoning_effort": level} if level in REASONING_EFFORTS and level else {}


class HttpModelInvoker:
    """`ModelInvoker` port over one OpenAI-compatible endpoint.

    Concurrency is bounded by a **threading** semaphore, not `asyncio.Semaphore`:
    the service runs each HTTP request on its own short-lived event loop
    (`asyncio.run` in a handler thread), and an asyncio primitive created on one
    loop raises "bound to a different event loop" when awaited on another.

    The slot is acquired *before* the worker thread starts and released *by the
    worker* when the real request finishes. Releasing it in the awaiting
    coroutine would free capacity at the timeout while the underlying `urllib`
    call kept running, so two timed-out calls could leave more threads working
    than the configured limit (R07).
    """

    def __init__(self, settings: ModelSettings) -> None:
        self._settings = settings
        self._slots = threading.BoundedSemaphore(max(1, int(settings.max_concurrency)))
        self._abandoned = 0
        self._in_flight = 0
        self._guard = threading.Lock()

    @property
    def settings(self) -> ModelSettings:
        return self._settings

    def update_settings(self, settings: ModelSettings) -> None:
        """Replace provider settings in place.

        The core's extraction/reflection/evolution services hold *this object*,
        so replacing the invoker instead would leave them on the old provider.

        Capacity is deliberately **not** re-applied: the semaphore counts calls
        that are still running, and swapping it would let the new one admit a
        fresh full batch next to the old batch still in flight -- exactly the
        over-capacity state R07 removed. A concurrency change therefore takes
        effect on restart; the console says so instead of pretending otherwise.
        """
        self._settings = settings

    @property
    def abandoned_calls(self) -> int:
        """Calls a cancelled coroutine left running (observation only)."""
        return self._abandoned

    @property
    def in_flight(self) -> int:
        """Real requests currently occupying a slot (including abandoned ones)."""
        with self._guard:
            return self._in_flight

    @property
    def capacity(self) -> int:
        return max(1, int(self._settings.max_concurrency))

    def available(self, tier: str) -> bool:
        """Whether this tier can be called at all (config, not liveness)."""
        if not self._settings.configured:
            return False
        if self._settings.model_for(tier) is None:
            return False
        return bool(self._settings.api_key())

    async def invoke(
        self,
        tier: str,
        messages: Sequence[Mapping[str, str]],
        *,
        max_output_tokens: int,
        timeout_s: float,
        purpose: str,
    ) -> str:
        model = self._settings.model_for(tier)
        if model is None:
            raise ModelCallError(
                f"tier {tier!r} has no configured model", kind="config",
                retryable=False,
            )
        key = self._settings.api_key()
        if not key:
            raise ModelCallError(
                f"no credential for tier {tier!r} "
                f"(environment variable {self._settings.api_key_env!r})",
                kind="auth", retryable=False,
            )
        payload = {
            "model": model,
            "messages": [dict(item) for item in messages],
            "max_tokens": int(max_output_tokens),
            "stream": False,
        }
        payload.update(self._settings.provider_options())
        budget = float(timeout_s if timeout_s is not None else self._settings.timeout_s)
        deadline = time.monotonic() + budget
        try:
            return await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    _MODEL_WORKERS, self._call_with_slot, payload, key, deadline, purpose,
                ),
                timeout=budget,
            )
        except asyncio.TimeoutError as exc:
            self._abandoned += 1
            raise ModelCallError(
                f"model call exceeded {budget:.2f}s", kind="timeout", retryable=True,
            ) from exc
        except asyncio.CancelledError:
            self._abandoned += 1
            raise

    def _call_with_slot(
        self, payload: Mapping[str, Any], key: str, deadline: float, purpose: str,
    ) -> str:
        """The worker owns the slot throughout queueing, I/O and release."""
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._slots.acquire(timeout=remaining):
            raise ModelCallError("model slot deadline exceeded", kind="timeout", retryable=True)
        with self._guard:
            self._in_flight += 1
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelCallError("model slot deadline exceeded", kind="timeout", retryable=True)
            return self._post(payload, key, remaining, purpose)
        finally:
            with self._guard:
                self._in_flight -= 1
            self._slots.release()

    # -- transport (runs in a worker thread) ----------------------------

    def _post(
        self, payload: Mapping[str, Any], key: str, timeout: float, purpose: str,
    ) -> str:
        url = self._settings.base_url.rstrip("/") + "/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(
                {"http": self._settings.proxy, "https": self._settings.proxy}
                if self._settings.proxy else {}
            ),
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise _map_http_error(exc) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise ModelCallError(
                f"model call timed out ({purpose})", kind="timeout",
                retryable=True,
            ) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise ModelCallError(
                    f"model call timed out ({purpose})", kind="timeout",
                    retryable=True,
                ) from exc
            raise ModelCallError(
                f"model transport failed: {reason}", kind="transport",
                retryable=True,
            ) from exc
        except OSError as exc:
            raise ModelCallError(
                f"model transport failed: {exc}", kind="transport",
                retryable=True,
            ) from exc
        return _extract_text(raw)


def _map_http_error(exc: urllib.error.HTTPError) -> ModelCallError:
    """Translate an HTTP status into a stable kind; never echo the credential."""
    status = int(getattr(exc, "code", 0) or 0)
    if status in (401, 403):
        return ModelCallError(
            f"provider rejected the credential (HTTP {status})",
            kind="auth", retryable=False, status=status,
        )
    if status == 429:
        return ModelCallError(
            "provider rate limit", kind="rate_limit", retryable=True, status=status,
        )
    if status == 404:
        return ModelCallError(
            f"endpoint or model not found (HTTP {status})",
            kind="config", retryable=False, status=status,
        )
    if 500 <= status < 600:
        return ModelCallError(
            f"provider error (HTTP {status})", kind="transport",
            retryable=True, status=status,
        )
    return ModelCallError(
        f"provider rejected the request (HTTP {status})", kind="format",
        retryable=False, status=status,
    )


class ModelText(str):
    """Assistant text with optional provider reasoning kept outside the reply."""

    def __new__(cls, text: str, reasoning: str = ""):
        value = super().__new__(cls, text)
        value.reasoning = reasoning
        return value

    def strip(self, chars=None):
        return ModelText(super().strip(chars), self.reasoning)


def _extract_text(raw: str) -> str:
    """Pull the assistant text out of an OpenAI-shaped reply."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ModelCallError(
            "provider reply is not JSON", kind="format", retryable=False,
        ) from exc
    if not isinstance(data, Mapping):
        raise ModelCallError(
            "provider reply is not an object", kind="format", retryable=False,
        )
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, Mapping):
            message = first.get("message")
            if isinstance(message, Mapping):
                reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
                reasoning = reasoning if isinstance(reasoning, str) else ""
                content = message.get("content")
                if isinstance(content, str):
                    return ModelText(content, reasoning)
                if isinstance(content, list):
                    parts = [
                        part.get("text") for part in content
                        if isinstance(part, Mapping)
                        and isinstance(part.get("text"), str)
                    ]
                    if parts:
                        return ModelText("".join(parts), reasoning)
    raise ModelCallError(
        "provider reply has no assistant content", kind="format", retryable=False,
    )


__all__ = [
    "ERROR_KINDS",
    "HttpModelInvoker",
    "ModelCallError",
    "ModelSettings",
]
