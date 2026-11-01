"""B04 conversation compression: summarize old turns into a memo message.

The host keeps a single summary SystemMessage at the head of recent.json and
removes the summarized prefix; the original rows stay in the time index
(design §17.15.5, B04 recent spec §4). The LLM call happens outside the store
lock; the commit re-checks the anchor (snapshot must still be the current
prefix) and, additionally, the deletion barrier — the host only had the anchor
check, so a deletion during the LLM call could otherwise summarize deleted
content back into the prompt.

Failure never writes: an empty summary, a moved anchor, a stale barrier or a
write error all leave recent.json untouched (the caller may retry later).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..domain.barrier import DeletionBarrier, message_text
from ..ports import Clock, ModelInvoker
from ..persistence.recent import RecentStore, is_summary_message
from .prompt_voice import FIRST_PERSON_VOICE

#: Host defaults (config/memory_settings.py): 20 items trigger, keep last 10.
DEFAULT_THRESHOLD_ITEMS = 20
DEFAULT_KEEP_LAST = 10
DEFAULT_MAX_OUTPUT_TOKENS = 1000
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_TIER = "summary"

_MEMO_TEMPLATES = {
    "zh": "先前对话的备忘录: {summary}",
    "zh-TW": "先前對話的備忘錄: {summary}",
    "en": "Memo of the earlier conversation: {summary}",
    "ja": "以前の会話のメモ: {summary}",
    "ko": "이전 대화 메모: {summary}",
}


@dataclass(frozen=True)
class CompressionSettings:
    """B04 compression policy; thresholds keep the host defaults."""

    threshold_items: int = DEFAULT_THRESHOLD_ITEMS
    keep_last: int = DEFAULT_KEEP_LAST
    tier: str = DEFAULT_TIER
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    timeout_s: float = DEFAULT_TIMEOUT_S
    language: str = "zh"


@dataclass(frozen=True)
class CompressionOutcome:
    """One compression attempt: completed / skipped / moot / failed."""

    status: str
    summarized: int = 0
    reason: str | None = None


def _memo_text(summary: str, language: str) -> str:
    template = _MEMO_TEMPLATES.get(str(language or "zh"), _MEMO_TEMPLATES["en"])
    return template.format(summary=summary)


class CompressionService:
    """Summarize old recent messages when the threshold is crossed."""

    def __init__(
        self,
        *,
        recent: RecentStore,
        barrier: DeletionBarrier,
        clock: Clock,
        model: ModelInvoker | None = None,
        settings: CompressionSettings | None = None,
    ) -> None:
        self._recent = recent
        self._barrier = barrier
        self._clock = clock
        self._model = model
        self._settings = settings or CompressionSettings()

    async def compress(
        self, character: str, *, language: str | None = None,
    ) -> CompressionOutcome:
        """Compress the current recent history into one head memo message."""
        settings = self._settings
        messages = self._recent.load(character)
        normal = [
            message for message in messages
            if not is_summary_message(message)
        ]
        if len(normal) <= settings.threshold_items:
            return CompressionOutcome("skipped", reason="below_threshold")
        if self._model is None:
            return CompressionOutcome("failed", reason="model_unavailable")
        keep_last = max(0, int(settings.keep_last))
        if len(messages) <= keep_last:
            return CompressionOutcome("skipped", reason="nothing_to_summarize")
        snapshot = messages[: len(messages) - keep_last]
        if not snapshot:
            return CompressionOutcome("skipped", reason="nothing_to_summarize")
        # A deleted message in the snapshot must never be summarized back.
        for message in snapshot:
            if self._barrier.blocks_message(character, message):
                return CompressionOutcome("moot", reason="deleted_content")
        version = self._barrier.snapshot(character)
        prompt = self._build_prompt(snapshot, language or settings.language)
        try:
            summary = await self._model.invoke(
                settings.tier,
                [{"role": "system", "content": prompt}],
                max_output_tokens=settings.max_output_tokens,
                timeout_s=settings.timeout_s,
                purpose="recent_compression",
            )
        except Exception as exc:  # noqa: BLE001 - failure never writes
            return CompressionOutcome("failed", reason=f"model_error:{type(exc).__name__}")
        summary = str(summary or "").strip()
        if not summary:
            return CompressionOutcome("failed", reason="empty_summary")
        # Late result: the deletion barrier moved or the snapshot now holds
        # deleted content -> drop the memo instead of resurrecting it.
        if self._barrier.is_stale(character, version):
            return CompressionOutcome("moot", reason="barrier_stale")
        for message in snapshot:
            if self._barrier.blocks_message(character, message):
                return CompressionOutcome("moot", reason="deleted_content")
        memo = {
            "type": "system",
            "data": {"content": _memo_text(summary, language or settings.language)},
        }
        if not self._recent.apply_compression(
            character, snapshot=snapshot, summary=memo,
        ):
            return CompressionOutcome("moot", reason="anchor_moved")
        return CompressionOutcome("completed", summarized=len(snapshot))

    @staticmethod
    def _build_prompt(messages: Any, language: str) -> str:
        lines: list[str] = []
        for message in messages:
            text = message_text(message).strip()
            if not text:
                continue
            if is_summary_message(message):
                lines.append(f"memo | {text}")
                continue
            role = "user"
            if isinstance(message, Mapping):
                kind = str(message.get("type") or "").lower()
                if kind in ("ai", "assistant"):
                    role = "assistant"
            lines.append(f"{role} | {text}")
        body = "\n".join(lines)
        return (
            FIRST_PERSON_VOICE
            + "我把下面的对话整理成一段 1000 token 以内的中文回忆便条，"
            "保留对方的稳定事实、偏好、我们的约定和待完成的计划，"
            "以及这段交流里的重要回应与进展。内容依据实际对话，延续我的第一人称视角。\n"
            "本次输出为备忘录正文。\n\n"
            f"对话：\n{body}"
        )


__all__ = [
    "CompressionOutcome",
    "CompressionService",
    "CompressionSettings",
]
