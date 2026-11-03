"""B06 recall domain: hybrid retrieval, passive turn context, fading, episodes.

Migrated from the host recall chain (see .scratch/b06-recall-spec.md):

* memory/hybrid_recall.py   explicit recall_memory: BM25 + cosine + RRF
* memory/turn_context.py    passive per-turn context: calibrated/fallback
                            keyword+vector selection, 2 injected rows
* memory/recall_render.py   the single recall renderer (400/40/2204 budgets)
* memory/companion_context.py query expansion, scene filter, source background
* memory/temporal.py        time windows, relative labels, event anchors
* memory/episode.py         episode ids, continuation markers, retention class
* memory/fading.py          read-time fade / surface thresholds
* memory/_reflection/selection.py mention window and suppression cooldown

This module is read-only: it never writes a store, never calls a model and
never raises into the caller. Both public entry points return a status
(matched/miss/busy/superseded/error/unavailable) plus empty blocks on failure,
so an unavailable memory service can never turn a reply into a 5xx.

The core has no tokenizer or embedding runtime of its own. Both are injected
through the B06 ports (ports.TokenCounter / ports.EmbeddingProvider). Without
them the service runs BM25-only and reports retrieval_mode="bm25"; token
budgets use the same CJK-aware estimate as the B05 stores.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any, Mapping, Sequence

from ..contracts import MemoryBlock
from ..invalidation import filter_valid_entries, is_friend_subject, normalize_text, subject_key
from ..ports import Clock, EmbeddingProvider, TokenCounter

# -- settings ---------------------------------------------------------------


@dataclass(frozen=True)
class RecallSettings:
    """Frozen B06 defaults (host constants in .scratch/b06-recall-spec.md §2)."""

    # Explicit recall (hybrid_recall.py / config.memory_settings).
    bm25_threshold: float = 0.1
    cosine_threshold: float = 0.3
    budget_each: int = 4
    budget_total: int = 8
    rrf_k: int = 60
    time_budget: int = 8
    bm25_k1: float = 1.5
    bm25_b: float = 0.75

    # Rendering (recall_render.py / RECALL_RENDER_*).
    render_entry_tokens: int = 400
    render_line_overhead: int = 40
    render_total_tokens: int = 2204

    # Passive turn context (turn_context.py / TURN_CONTEXT_*).
    passive_max_items: int = 2
    passive_bm25_top: int = 8
    fallback_bm25_top: int = 2
    fallback_min_query_terms: int = 6
    fallback_min_coverage: float = 0.8
    dual_threshold: float = 0.3
    semantic_threshold: float = 0.3
    lead_threshold: float = 0.02
    rare_term_min_cjk: int = 3
    rare_term_min_latin: int = 4
    rare_term_max_docs: int = 2
    rare_terms_without_vector: int = 2

    # Query expansion and background (companion_context.py).
    query_max_chars: int = 500
    short_message_chars: int = 120
    recent_turns_max: int = 2
    scene_value_max_chars: int = 64
    source_background_items: int = 2
    source_background_tokens: int = 400
    source_background_min_tokens: int = 8

    # Optional blocks.
    correction_tokens: int = 200
    growth_tokens: int = 200
    state_tokens: int = 300
    persona_render_tokens: int = 2000
    episode_background_items: int = 3
    episode_entry_max_chars: int = 600

    # Budget (turn_context_settings.py / memory/turn_context.py).
    budget_seconds: float = 1.0
    budget_ms: int = 1000
    min_budget_ms: int = 50
    max_budget_ms: int = 5000
    min_remaining_seconds: float = 0.3
    http_slack_seconds: float = 0.25
    render_reserve_seconds: float = 0.1
    min_retrieval_seconds: float = 0.05
    max_context_tokens_min: int = 1
    max_context_tokens_max: int = 4000

    # Mention suppression (persona/_shared.py, _reflection/selection.py).
    suppress_mention_limit: int = 2
    suppress_window_hours: int = 5
    suppress_cooldown_hours: int = 5

    def __post_init__(self) -> None:
        for name in (
            "bm25_threshold", "cosine_threshold", "dual_threshold",
            "semantic_threshold", "lead_threshold", "fallback_min_coverage",
        ):
            value = getattr(self, name)
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be within 0..1")
        for name in (
            "budget_each", "budget_total", "time_budget", "render_entry_tokens",
            "render_line_overhead", "render_total_tokens", "passive_max_items",
            "passive_bm25_top", "fallback_bm25_top", "fallback_min_query_terms",
            "source_background_items", "source_background_tokens",
            "episode_background_items", "budget_ms", "min_budget_ms",
            "max_budget_ms",
        ):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if self.min_budget_ms > self.max_budget_ms:
            raise ValueError("min_budget_ms must not exceed max_budget_ms")


# -- text helpers -----------------------------------------------------------

_CJK_RANGES = (
    ("\u4e00", "\u9fff"),
    ("\u3040", "\u30ff"),
    ("\uac00", "\ud7af"),
)

# Host persona/_shared.py _SPLIT_RE: punctuation and whitespace split segments.
_SPLIT_RE = re.compile(
    r"[，。、！？；：\u201c\u201d\u2018\u2019（）()\[\]{}<>《》【】\s,.!?;:\-\u2014\u2026\xb7\u3000]+",
)

_WHITESPACE_RE = re.compile(r"\s+")

# Host companion_context.py _COMMON_TERMS (subset that matters for overlap).
_COMMON_TERMS = frozenset({
    "用户", "今天", "最近", "现在", "然后", "就是", "觉得", "因为", "所以",
    "可以", "自己", "这个", "那个", "什么", "怎么", "已经", "还是", "但是",
    "如果", "我们", "你们", "他们", "她们", "时候", "东西", "事情", "一直",
    "有点", "比较", "真的", "应该", "知道", "告诉", "感觉", "起来", "出来",
    "一下", "一起", "不是", "没有", "一个", "两个", "之后", "之前", "以后",
    "以前", "当时", "后来", "上次", "这次", "下次", "还有", "只是", "不过",
    "而且", "或者", "虽然", "于是", "其实", "好像", "可能", "需要", "这样",
    "那样", "这种", "那种", "里面", "外面", "上面", "下面", "反正", "居然",
    "终于", "确实", "特别", "非常", "更加", "越来越",
    "the", "and", "for", "with", "that", "this", "those", "these", "have",
    "has", "had", "was", "were", "are", "is", "it", "its", "you", "your",
    "i", "me", "my", "we", "they", "them", "he", "she", "his", "her", "not",
    "but", "so", "then", "there", "here", "what", "when", "where", "why",
    "how", "about", "from", "into", "over", "just", "really", "very", "also",
    "still", "been", "being", "will", "would", "could", "should", "can",
    "may", "might", "did", "does", "do", "done", "get", "got", "going", "go",
    "went", "one", "two", "some", "any", "all", "more", "most", "much",
    "many", "lot", "last", "time", "continue", "again", "said", "says",
})


def _is_cjk(char: str) -> bool:
    return any(start <= char <= end for start, end in _CJK_RANGES)


def tokenize(text: Any, stop_names: Sequence[str] = ()) -> list[str]:
    """Host BM25 tokenizer: CJK 2/3-grams (duplicates kept) + Latin segments."""
    raw = normalize_text(text)
    for name in stop_names or ():
        cleaned = normalize_text(name)
        if cleaned:
            raw = raw.replace(cleaned, " ")
    tokens: list[str] = []
    for segment in _SPLIT_RE.split(raw):
        segment = segment.strip()
        if not segment:
            continue
        if segment.isascii():
            if len(segment) >= 2:
                tokens.append(segment)
            continue
        cjk = sum(1 for char in segment if _is_cjk(char))
        if cjk > len(segment) // 2:
            for size in (2, 3):
                for index in range(len(segment) - size + 1):
                    tokens.append(segment[index:index + size])
        elif len(segment) >= 2:
            tokens.append(segment)
    return tokens


def content_terms(text: Any, stop_names: Sequence[str] = ()) -> set[str]:
    """Content terms for overlap checks (common small-talk terms removed)."""
    return {
        token.casefold()
        for token in tokenize(text, stop_names)
        if token and token.casefold() not in _COMMON_TERMS
    }


def _estimate_tokens(text: Any) -> int:
    if not isinstance(text, str) or not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    rest = len(text) - cjk
    return cjk + (rest + 3) // 4


def _truncate_estimate(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    used = 0
    for index, char in enumerate(text):
        cost = 1 if _is_cjk(char) else 1 / 4
        if used + cost > max_tokens:
            return text[:index]
        used += cost
    return text


class _Tokens:
    """TokenCounter adapter with the B05 CJK estimate as the default."""

    def __init__(self, counter: TokenCounter | None = None) -> None:
        self._counter = counter

    def count(self, text: Any) -> int:
        if self._counter is not None:
            try:
                return max(0, int(self._counter.count(str(text or ""))))
            except Exception:  # noqa: BLE001 - counter failure falls back
                pass
        return _estimate_tokens(text)

    def truncate(self, text: str, max_tokens: int) -> str:
        if max_tokens <= 0:
            return ""
        if self._counter is not None:
            try:
                return str(self._counter.truncate(text, max_tokens))
            except Exception:  # noqa: BLE001
                pass
        return _truncate_estimate(text, max_tokens)

    def identity(self) -> str:
        if self._counter is not None:
            try:
                identity = getattr(self._counter, "identity")
                return str(identity() if callable(identity) else identity)
            except Exception:  # noqa: BLE001
                pass
        return "heuristic:v2"


# -- BM25 -------------------------------------------------------------------

@lru_cache(maxsize=8192)
def _document_terms(text: str, stop_names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(tokenize(text, stop_names))


def bm25_scores(
    query: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    stop_names: Sequence[str] = (),
    k1: float = 1.5,
    b: float = 0.75,
) -> list[tuple[dict[str, Any], float, dict[str, int]]]:
    """Okapi BM25 over in-memory rows; returns (row, score, shared tf) DESC.

    Mirrors host hybrid_recall._bm25_scores: RSJ IDF with +0.5 smoothing,
    DF/TF restricted to query terms, zero-score rows dropped.
    """
    if not query or not rows:
        return []
    query_terms = tokenize(query, stop_names)
    if not query_terms:
        return []
    query_unique = set(query_terms)
    doc_terms_list = [_document_terms(str(row.get("text") or ""), tuple(stop_names)) for row in rows]
    total_length = sum(len(terms) for terms in doc_terms_list)
    if total_length == 0:
        return []
    average_length = total_length / len(rows)

    document_frequency: dict[str, int] = dict.fromkeys(query_unique, 0)
    doc_tf_list: list[dict[str, int]] = []
    for terms in doc_terms_list:
        shared = query_unique.intersection(terms)
        if shared:
            counts: dict[str, int] = {}
            for term in terms:
                if term in shared:
                    counts[term] = counts.get(term, 0) + 1
            tf_map = counts
        else:
            tf_map = {}
        doc_tf_list.append(tf_map)
        for term in tf_map:
            document_frequency[term] += 1

    idf_terms: list[tuple[str, float]] = []
    for term in query_unique:
        frequency = document_frequency.get(term, 0)
        if frequency <= 0:
            continue
        idf = math.log(
            (len(rows) - frequency + 0.5) / (frequency + 0.5) + 1.0,
        )
        if idf > 0:
            idf_terms.append((term, idf))

    scored: list[tuple[dict[str, Any], float, dict[str, int]]] = []
    for row, terms, tf_map in zip(rows, doc_terms_list, doc_tf_list):
        if not tf_map:
            continue
        normalization = 1.0 - b + b * len(terms) / average_length
        score = 0.0
        for term, idf in idf_terms:
            frequency = tf_map.get(term, 0)
            if frequency:
                score += idf * (frequency * (k1 + 1)) / (
                    frequency + k1 * normalization
                )
        if score > 0:
            scored.append((dict(row), score, tf_map))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def rrf_fuse(
    bm25_ranking: Sequence[tuple[Mapping[str, Any], float]],
    cosine_ranking: Sequence[tuple[Mapping[str, Any], float]],
    *,
    k: int = 60,
    budget_total: int = 8,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion RRF(d)=sum 1/(k+rank), deduped by row id."""
    by_id: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    for ranking in (bm25_ranking, cosine_ranking):
        for rank, (row, _score) in enumerate(ranking, start=1):
            row_id = str(row.get("id") or "")
            if not row_id:
                continue
            by_id.setdefault(row_id, dict(row))
            scores[row_id] = scores.get(row_id, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores, key=lambda item: scores[item], reverse=True)
    fused: list[dict[str, Any]] = []
    for row_id in ordered[: max(0, int(budget_total))]:
        row = dict(by_id[row_id])
        row["_rrf_score"] = scores[row_id]
        fused.append(row)
    return fused


def _row_vector(row: Mapping[str, Any]) -> list[float] | None:
    """Decoded candidate vector, when the row carries a plain float list."""
    value = row.get("embedding")
    if isinstance(value, Mapping):
        value = value.get("vector") or value.get("values")
    if not isinstance(value, (list, tuple)) or not value:
        return None
    vector: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        vector.append(float(item))
    return vector


def _cosine_rank(
    query_vector: Sequence[float],
    rows: Sequence[Mapping[str, Any]],
) -> list[tuple[dict[str, Any], float]]:
    query = [float(item) for item in query_vector]
    query_norm = math.sqrt(sum(item * item for item in query))
    if query_norm <= 0:
        return []
    scored: list[tuple[dict[str, Any], float]] = []
    for row in rows:
        vector = _row_vector(row)
        if vector is None or len(vector) != len(query):
            continue
        norm = math.sqrt(sum(item * item for item in vector))
        if norm <= 0:
            continue
        dot = sum(left * right for left, right in zip(query, vector))
        scored.append((dict(row), dot / (query_norm * norm)))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


# -- scene / subject --------------------------------------------------------

_SCENE_KEYS = ("plugin_id", "game_id", "session_id")
_SHARED_SCOPES = frozenset({"shared", "global", "all"})


def normalize_scene(scene: Any, *, max_chars: int = 64) -> dict[str, str]:
    """Normalize a Scene-like value onto plugin_id/game_id/session_id."""
    if scene is None:
        return {}
    if not isinstance(scene, Mapping):
        scene = getattr(scene, "__dict__", None)
        if not isinstance(scene, Mapping):
            return {}
    normalized: dict[str, str] = {}
    for key in _SCENE_KEYS:
        value = scene.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            return {}
        value = value.strip()
        if value:
            normalized[key] = value[:max_chars]
    return normalized


def entry_applies_to_scene(entry: Any, scene: Any, *, max_chars: int = 64) -> bool:
    """Host scene rule: no restriction applies everywhere; otherwise every
    named field must match and a missing scene keeps only unrestricted rows."""
    if not isinstance(entry, Mapping):
        return False
    restriction = entry.get("applies_to")
    if not isinstance(restriction, Mapping):
        restriction = entry.get("source_scene")
    if not isinstance(restriction, Mapping) or not restriction:
        return True
    scope = restriction.get("scope")
    if isinstance(scope, str) and scope.strip().lower() in _SHARED_SCOPES:
        return True
    entry_scope = entry.get("scope")
    if isinstance(entry_scope, str) and entry_scope.strip().lower() in _SHARED_SCOPES:
        return True
    normalized = normalize_scene(scene, max_chars=max_chars)
    for key in _SCENE_KEYS:
        wanted = restriction.get(key)
        if wanted is None:
            continue
        if not isinstance(wanted, str):
            return False
        wanted = wanted.strip()
        if not wanted:
            continue
        if normalized.get(key) != wanted[:max_chars]:
            return False
    return True


def filter_entries_for_scene(
    entries: Sequence[Mapping[str, Any]], scene: Any, *, max_chars: int = 64,
) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for entry in entries or ():
        if isinstance(entry, Mapping) and entry_applies_to_scene(
            entry, scene, max_chars=max_chars,
        ):
            kept.append(dict(entry))
    return kept


def _row_subject_key(row: Mapping[str, Any]) -> str:
    if row.get("subject") is not None:
        return subject_key(row.get("subject"))
    kind = row.get("subject_kind")
    subject_id = row.get("subject_id") or row.get("subject_key")
    if isinstance(subject_id, str) and subject_id.strip():
        if isinstance(kind, str) and kind.strip():
            return f"{kind.strip()}:{subject_id.strip()}"
        return subject_id.strip()
    return ""


def _normalize_subjects(value: Any) -> tuple[str, ...] | None:
    """None keeps legacy-private; a list (even empty) is an explicit boundary."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        key = subject_key(value)
        return (key,) if key else ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    cleaned: list[str] = []
    for item in value:
        key = subject_key(item) if isinstance(item, Mapping) else str(item or "").strip()
        if key:
            cleaned.append(key)
    return tuple(cleaned)


def _row_in_subjects(row: Mapping[str, Any], subjects: tuple[str, ...] | None) -> bool:
    stored = _row_subject_key(row)
    if subjects is None:
        return stored in ("", "private")
    if not subjects:
        return False
    if stored in ("", "private"):
        return "private" in subjects
    return stored in subjects


# -- time -------------------------------------------------------------------

def _parse_iso_safe(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except (TypeError, ValueError):
        return None


def _to_naive_local(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is not None:
        try:
            return value.astimezone().replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return value.replace(tzinfo=None)
    return value


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _to_naive_local(value)
    return _to_naive_local(_parse_iso_safe(value))


def _now_naive(now: Any) -> datetime | None:
    return _coerce_datetime(now) if now is not None else None


_TIME_LABELS = {
    "zh": {"now": "当下", "day": "{n} 天前", "week": "{n} 周前", "month": "{n} 月前"},
    "zh-TW": {"now": "當下", "day": "{n} 天前", "week": "{n} 週前", "month": "{n} 個月前"},
    "en": {"now": "now", "day": "{n}d ago", "week": "{n}w ago", "month": "{n}mo ago"},
    "ja": {"now": "今", "day": "{n} 日前", "week": "{n} 週間前", "month": "{n} ヶ月前"},
    "ko": {"now": "지금", "day": "{n}일 전", "week": "{n}주 전", "month": "{n}개월 전"},
    "ru": {
        "now": "сейчас", "day": "{n} дн назад",
        "week": "{n} нед назад", "month": "{n} мес назад",
    },
    "es": {"now": "ahora", "day": "hace {n}d", "week": "hace {n}sem", "month": "hace {n}mes"},
    "pt": {"now": "agora", "day": "há {n}d", "week": "há {n}sem", "month": "há {n}mês"},
}


def _normalize_language(language: Any) -> str:
    raw = str(language or "").strip().replace("_", "-")
    lowered = raw.lower()
    if lowered.startswith("zh-tw") or lowered.startswith("zh-hant"):
        return "zh-TW"
    if lowered.startswith("zh"):
        return "zh"
    base = lowered.split("-")[0]
    return base if base in _TIME_LABELS else "en"


def days_since(anchor_iso: Any, now: Any = None) -> int | None:
    anchor = _coerce_datetime(anchor_iso)
    if anchor is None:
        return None
    current = _coerce_datetime(now) if now is not None else datetime.now()
    if current is None:
        return None
    return max(0, int((current - anchor).total_seconds() // 86400))


def time_since_label(
    anchor_iso: Any, *, now: Any = None, lang: str = "zh",
) -> str:
    days = days_since(anchor_iso, now=now)
    if days is None:
        return ""
    table = _TIME_LABELS.get(_normalize_language(lang), _TIME_LABELS["en"])
    if days == 0:
        return table["now"]
    if days < 7:
        return table["day"].format(n=days)
    if days < 30:
        return table["week"].format(n=days // 7)
    return table["month"].format(n=days // 30)


def _token_window(token: str) -> tuple[datetime, datetime] | None:
    token = (token or "").strip()
    if not token:
        return None

    def next_month(value: datetime) -> datetime:
        if value.month == 12:
            return value.replace(year=value.year + 1, month=1)
        return value.replace(month=value.month + 1)

    for pattern, end_fn in (
        ("%Y-%m-%d", lambda value: value + timedelta(days=1)),
        ("%Y-%m-%dT%H", lambda value: value + timedelta(hours=1)),
        ("%Y-%m-%d %H", lambda value: value + timedelta(hours=1)),
        ("%Y-%m", next_month),
        ("%Y", lambda value: value.replace(year=value.year + 1)),
    ):
        try:
            start = datetime.strptime(token, pattern)
        except ValueError:
            continue
        try:
            return start, end_fn(start)
        except (ValueError, OverflowError):
            return None
    parsed = _to_naive_local(_parse_iso_safe(token))
    if parsed is None:
        return None
    hour = parsed.replace(minute=0, second=0, microsecond=0)
    try:
        return hour, hour + timedelta(hours=1)
    except (ValueError, OverflowError):
        return None


def parse_time_range(spec: Any) -> tuple[datetime, datetime] | None:
    """Parse a recall time argument into a naive-local [start, end) window."""
    if spec is None:
        return None
    if isinstance(spec, (tuple, list)) and len(spec) == 2:
        left = _coerce_datetime(spec[0])
        right = _coerce_datetime(spec[1])
        if left is None or right is None:
            return None
        return (min(left, right), max(left, right))
    if not isinstance(spec, str):
        return None
    text = spec.strip()
    if not text:
        return None
    separator = "/" if "/" in text else (".." if ".." in text else None)
    if separator:
        left, _, right = text.partition(separator)
        left_window = _token_window(left)
        right_window = _token_window(right)
        if left_window is None or right_window is None:
            return None
        return (
            min(left_window[0], right_window[0]),
            max(left_window[1], right_window[1]),
        )
    return _token_window(text)


def entry_event_window(entry: Mapping[str, Any]) -> tuple[datetime, datetime] | None:
    """Event interval with anchor priority end -> start -> created (host rule)."""
    start = _coerce_datetime(entry.get("event_start_at"))
    end = _coerce_datetime(entry.get("event_end_at"))
    created = _coerce_datetime(entry.get("created_at"))
    anchor = end or start or created
    if anchor is None:
        return None
    first = start or anchor
    last = end or anchor
    if last < first:
        first, last = last, first
    return first, last


def _overlaps_window(
    entry: Mapping[str, Any], window: tuple[datetime, datetime],
) -> bool:
    event = entry_event_window(entry)
    if event is None:
        return False
    start, end = event
    win_start, win_end = window
    return start < win_end and end >= win_start


# -- fading / retention (host memory/fading.py + episode.retention_class) ---

RETENTION_DURABLE = "durable"
RETENTION_SHARED_IMPORTANT = "shared_important"
RETENTION_NORMAL = "normal"
RETENTION_DAILY = "daily"
RETENTION_TRANSIENT = "transient"
RETENTION_CLASSES = (
    RETENTION_DURABLE, RETENTION_SHARED_IMPORTANT, RETENTION_NORMAL,
    RETENTION_DAILY, RETENTION_TRANSIENT,
)
DEFAULT_RETENTION_CLASS = RETENTION_NORMAL

HALF_LIFE_DAYS = {
    RETENTION_NORMAL: 90.0,
    RETENTION_DAILY: 7.0,
    RETENTION_TRANSIENT: 1.5,
    RETENTION_SHARED_IMPORTANT: 365.0,
}
FLOOR_BY_CLASS = {
    RETENTION_DURABLE: 0.95,
    RETENTION_SHARED_IMPORTANT: 0.6,
    RETENTION_NORMAL: 0.3,
    RETENTION_DAILY: 0.05,
    RETENTION_TRANSIENT: 0.0,
}
DEFAULT_IMPORTANCE = 5
IMPORTANCE_HALF_LIFE_STEP = 0.15
MIN_IMPORTANCE_MULTIPLIER = 0.5
MAX_IMPORTANCE_MULTIPLIER = 2.0

QUERY_PURPOSES = frozenset({"query", "explicit", "explicit_query", "recall", "lookup"})
# Host persona/_shared.py + _reflection/selection.py mention constants.
SUPPRESS_MENTION_LIMIT = 2
SUPPRESS_WINDOW_HOURS = 5
SUPPRESS_COOLDOWN_HOURS = 5
SURFACE_THRESHOLDS = {
    "proactive": 0.5,
    "proactive_share": 0.5,
    "proactive_generate": 0.5,
    "share": 0.45,
    "decision": 0.3,
    "inject": 0.25,
    "context": 0.25,
    "turn_context": 0.25,
}
DEFAULT_SURFACE_THRESHOLD = 0.35
HIDDEN_STATUSES = frozenset({
    "promoted", "denied", "archived", "merged", "promote_blocked",
    "withdrawn", "invalidated", "superseded", "expired",
    "completed", "closed", "ended", "resolved", "cancelled", "canceled",
    "done", "finished",
})
_DURABLE_SOURCES = frozenset({
    "character_card", "settings", "identity", "manual", "reflection",
    "user_correction",
})
_SHARED_SOURCES = frozenset({"shared_experience"})
_OBSERVATION_SOURCES = frozenset({"user_observation", "ai_disclosure"})
_GROWTH_KINDS = frozenset({"habit", "interest", "experience", "relationship"})
_GROWTH_STABLE_STATUSES = frozenset({"confirmed", "promoted"})
_GROWTH_TERMINAL_STATUSES = frozenset({"withdrawn", "invalidated", "superseded"})
_ENDED_FLAGS = ("ended", "is_closed", "closed", "completed", "expired")


def _coerce_importance(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        try:
            number = int(float(value))
        except (TypeError, ValueError, OverflowError):
            return None
    return max(1, min(10, number))


def _importance_multiplier(entry: Any) -> float:
    mapping = entry if isinstance(entry, Mapping) else {}
    importance = _coerce_importance(mapping.get("importance")) or DEFAULT_IMPORTANCE
    multiplier = 1.0 + (importance - DEFAULT_IMPORTANCE) * IMPORTANCE_HALF_LIFE_STEP
    return max(MIN_IMPORTANCE_MULTIPLIER, min(MAX_IMPORTANCE_MULTIPLIER, multiplier))


def retention_class(entry: Any) -> str:
    """Host retention class inference (read-only; no extra activity score)."""
    if not isinstance(entry, Mapping):
        return DEFAULT_RETENTION_CLASS
    explicit = entry.get("retention_class") or entry.get("fading_class")
    if isinstance(explicit, str):
        normalized = explicit.strip().lower()
        if normalized in RETENTION_CLASSES:
            return normalized
    if entry.get("protected") is True:
        return RETENTION_DURABLE
    target = str(entry.get("target_type") or entry.get("_tier") or "").strip().lower()
    if target == "persona":
        return RETENTION_DURABLE
    source = str(entry.get("source") or entry.get("source_type") or "").strip().lower()
    if source in _DURABLE_SOURCES:
        return RETENTION_DURABLE
    status = str(entry.get("status") or "").strip().lower()
    kind = str(entry.get("memory_kind") or "").strip().lower()
    if kind in _GROWTH_KINDS:
        if status in _GROWTH_TERMINAL_STATUSES:
            return RETENTION_TRANSIENT
        if status in _GROWTH_STABLE_STATUSES:
            return RETENTION_SHARED_IMPORTANT
        return RETENTION_NORMAL
    if status in HIDDEN_STATUSES:
        return RETENTION_TRANSIENT
    if any(entry.get(flag) is True for flag in _ENDED_FLAGS):
        return RETENTION_TRANSIENT
    if source in _SHARED_SOURCES:
        return RETENTION_SHARED_IMPORTANT
    temporal = str(entry.get("temporal_scope") or "").strip().lower()
    if temporal == "state":
        return RETENTION_TRANSIENT
    if temporal in ("episode", "past"):
        return RETENTION_DAILY
    if target == "fact_archive":
        return RETENTION_DAILY
    importance = _coerce_importance(entry.get("importance"))
    if importance is not None:
        if importance <= 3:
            return RETENTION_DAILY
        if source in _OBSERVATION_SOURCES and importance <= 4:
            return RETENTION_DAILY
    return RETENTION_NORMAL


def _entry_anchor(entry: Any, last_mentioned_at: Any) -> datetime | None:
    mapping = entry if isinstance(entry, Mapping) else {}
    candidates: list[datetime] = []
    mentioned = _coerce_datetime(last_mentioned_at)
    if mentioned is not None:
        candidates.append(mentioned)
    for key in (
        "last_mentioned_at", "updated_at", "created_at", "added_at", "timestamp",
    ):
        parsed = _coerce_datetime(mapping.get(key))
        if parsed is not None:
            candidates.append(parsed)
    mentions = mapping.get("recent_mentions")
    if isinstance(mentions, (list, tuple)):
        for value in mentions:
            parsed = _coerce_datetime(value)
            if parsed is not None:
                candidates.append(parsed)
    if not candidates:
        return None
    return max(candidates)


def fading_factor(
    entry: Any, *, now: Any, last_mentioned_at: Any = None,
) -> float:
    """Read-time injection priority in 0..1 (host memory/fading.py)."""
    current = _coerce_datetime(now)
    if current is None:
        return 1.0
    level = retention_class(entry)
    if level == RETENTION_DURABLE:
        return 1.0
    anchor = _entry_anchor(entry, last_mentioned_at)
    if anchor is None:
        return 1.0
    elapsed = (current - anchor).total_seconds()
    if elapsed <= 0:
        return 1.0
    half_life = HALF_LIFE_DAYS.get(level, HALF_LIFE_DAYS[RETENTION_NORMAL])
    half_life *= _importance_multiplier(entry)
    factor = 0.5 ** ((elapsed / 86400.0) / half_life)
    floor = FLOOR_BY_CLASS.get(level, FLOOR_BY_CLASS[RETENTION_NORMAL])
    return round(max(floor, min(1.0, factor)), 4)


def fading_score(
    entry: Any, now: Any = None, *, last_mentioned_at: Any = None,
) -> float:
    """Alias used by B06/主动候选: fade factor at the supplied (or current) time."""
    return fading_factor(
        entry, now=now if now is not None else datetime.now(),
        last_mentioned_at=last_mentioned_at,
    )


def _normalize_purpose(purpose: Any) -> str:
    return (
        str(purpose or "").strip().lower().replace("-", "_").replace(":", "_")
    )


def _surface_threshold(purpose: str) -> float:
    if purpose in SURFACE_THRESHOLDS:
        return SURFACE_THRESHOLDS[purpose]
    if purpose.startswith("proactive"):
        return SURFACE_THRESHOLDS["proactive"]
    return DEFAULT_SURFACE_THRESHOLD


def should_surface(
    entry: Any, now: Any = None, *, purpose: Any = "chat",
    last_mentioned_at: Any = None,
) -> bool:
    """Whether the entry may be volunteered / injected for this purpose."""
    purpose_key = _normalize_purpose(purpose)
    if purpose_key in QUERY_PURPOSES:
        return True
    if not isinstance(entry, Mapping):
        return False
    if entry.get("suppress") is True:
        return False
    if entry.get("invalidated_at"):
        return False
    status = str(entry.get("status") or "").strip().lower()
    if status in HIDDEN_STATUSES:
        return False
    factor = fading_factor(entry, now=now, last_mentioned_at=last_mentioned_at)
    return factor >= _surface_threshold(purpose_key)


# -- mention window / suppression (host _reflection/selection.py) -----------

def record_mention(
    entry: Any,
    *,
    now: Any,
    window_hours: int = SUPPRESS_WINDOW_HOURS,
    mention_limit: int = SUPPRESS_MENTION_LIMIT,
) -> bool:
    """Record one delivered mention on a reflection row; returns changed.

    Mutates the row (the caller owns the loaded view): appends now to
    recent_mentions, prunes the window, and sets suppress once the count
    exceeds the limit. Host only tracks confirmed reflections.
    """
    if not isinstance(entry, dict) or entry.get("status") != "confirmed":
        return False
    current = _coerce_datetime(now) or datetime.now()
    cutoff = current - timedelta(hours=max(0, int(window_hours)))
    mentions = [
        value for value in entry.get("recent_mentions") or ()
        if isinstance(value, str) and value
    ]
    cleaned: list[str] = []
    for value in mentions:
        parsed = _coerce_datetime(value)
        if parsed is not None and parsed >= cutoff:
            cleaned.append(value)
    mentions = cleaned
    mentions.append(current.isoformat())
    entry["recent_mentions"] = mentions
    changed = True
    if not entry.get("suppress") and len(mentions) > int(mention_limit):
        entry["suppress"] = True
        entry["suppressed_at"] = current.isoformat()
    return changed


def update_suppression(
    entry: Any,
    *,
    now: Any,
    window_hours: int = SUPPRESS_WINDOW_HOURS,
    cooldown_hours: int = SUPPRESS_COOLDOWN_HOURS,
) -> bool:
    """Prune the mention window and lift expired suppression; returns changed."""
    if not isinstance(entry, dict):
        return False
    current = _coerce_datetime(now) or datetime.now()
    cutoff = current - timedelta(hours=max(0, int(window_hours)))
    changed = False
    mentions = entry.get("recent_mentions")
    if isinstance(mentions, list):
        cleaned = [
            value for value in mentions
            if isinstance(value, str)
            and (_coerce_datetime(value) or cutoff) >= cutoff
        ]
        if cleaned != mentions:
            entry["recent_mentions"] = cleaned
            changed = True
    if not entry.get("suppress"):
        return changed
    suppressed_at = _coerce_datetime(entry.get("suppressed_at"))
    if suppressed_at is None:
        return changed
    if (current - suppressed_at).total_seconds() / 3600 >= int(cooldown_hours):
        entry["suppress"] = False
        entry["suppressed_at"] = None
        entry["recent_mentions"] = []
        changed = True
    return changed


# -- episode ----------------------------------------------------------------

_ZH_CONTINUATION_MARKERS = (
    "后来", "上次", "上回", "那次", "接着说", "接着聊", "接着讲", "接着上次",
    "接着刚才", "继续上次", "继续之前", "继续刚才", "继续这个话题",
    "回到刚才", "说回刚才", "刚才说到", "刚刚说到", "之前说到", "上次说到",
    "说到哪", "讲到哪", "后续呢", "后来呢", "然后呢", "我们刚才", "刚才聊",
    "之前聊", "从那次以后", "那次之后",
    "後來", "後續呢", "接著說", "接著聊", "接著講", "接著上次", "接著剛才",
    "繼續上次", "繼續之前", "繼續剛才", "繼續這個話題", "回到剛才", "說回剛才",
    "剛才說到", "剛剛說到", "之前說到", "上次說到", "說到哪", "講到哪",
    "然後呢", "我們剛才", "剛才聊", "之前聊", "從那次以後", "那次之後",
)
_EN_CONTINUATION_RE = re.compile(
    r"\b(?:later|last\s+time|back\s+then|continue|continuing|what\s+happened|"
    r"where\s+we\s+left\s+off|that\s+time|after\s+that)\b",
    re.IGNORECASE,
)


def continuation_markers(text: Any) -> bool:
    """Whether the message explicitly continues an earlier experience."""
    raw = str(text or "")
    if not raw.strip():
        return False
    folded = raw.casefold()
    if any(marker in folded for marker in _ZH_CONTINUATION_MARKERS):
        return True
    return bool(_EN_CONTINUATION_RE.search(raw))


def episode_id_of(entry: Any) -> str:
    """Read episode_id from the row top level or metadata/companion_event."""
    if not isinstance(entry, Mapping):
        return ""
    value = entry.get("episode_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    metadata = entry.get("metadata")
    if isinstance(metadata, Mapping):
        value = metadata.get("episode_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
        companion = metadata.get("companion_event")
        if isinstance(companion, Mapping):
            value = companion.get("episode_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def link_followup_episode(query: Any, recalled_entries: Any) -> str | None:
    """The unique episode id when the query explicitly continues it."""
    if not continuation_markers(query):
        return None
    try:
        entries = list(recalled_entries or ())
    except TypeError:
        return None
    episode_ids = {
        episode_id
        for episode_id in (episode_id_of(entry) for entry in entries)
        if episode_id
    }
    if len(episode_ids) != 1:
        return None
    return next(iter(episode_ids))


def episode_background(
    query: Any,
    picked: Any,
    pool: Any,
    *,
    max_items: int = 3,
    max_chars: int = 600,
) -> dict[str, Any] | None:
    """Fill in same-episode rows not picked, when the query continues it."""
    episode_id = link_followup_episode(query, picked)
    if not episode_id:
        return None
    picked_ids = {
        str(row.get("id") or "") for row in picked or () if isinstance(row, Mapping)
    }
    entries: list[dict[str, Any]] = []
    for row in pool or ():
        if not isinstance(row, Mapping) or episode_id_of(row) != episode_id:
            continue
        row_id = str(row.get("id") or "")
        if row_id and row_id in picked_ids:
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        entries.append({
            "id": row_id,
            "kind": str(row.get("_tier") or row.get("kind") or ""),
            "text": text[:max_chars],
            "created_at": row.get("created_at"),
        })
        if len(entries) >= max(0, int(max_items)):
            break
    return {"episode_id": episode_id, "entries": entries}


# -- query expansion (host memory/companion_context.py) ---------------------

_ZH_ANAPHORA_MARKERS = (
    "那个", "这个", "上次", "后来", "然后", "继续", "结果", "怎么样了",
    "它", "那件事", "呢", "后来呢", "还说", "之前说的",
)
_EN_ANAPHORA_RE = re.compile(
    r"\b(?:that|those|these|later|last\s+time|continue|what\s+happened|"
    r"how\s+did|what\s+about|it|they|them|the\s+one|back\s+then)\b",
    re.IGNORECASE,
)


def has_anaphora(text: Any) -> bool:
    raw = str(text or "")
    if not raw:
        return False
    if any(marker in raw for marker in _ZH_ANAPHORA_MARKERS):
        return True
    return bool(_EN_ANAPHORA_RE.search(raw))


def _turn_texts(recent_turns: Any) -> list[str]:
    texts: list[str] = []
    for item in recent_turns or ():
        role: Any = None
        text: Any = None
        if isinstance(item, str):
            text = item
        elif isinstance(item, Mapping):
            role = item.get("role", item.get("speaker"))
            text = item.get("text", item.get("content", item.get("message")))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            role, text = item
        else:
            continue
        if role is not None:
            role_text = str(role).strip().lower()
            if role_text not in {"user", "human", "master", "玩家", "主人"}:
                continue
        piece = str(text or "").strip()
        if piece:
            texts.append(piece)
    return texts


def _thread_texts(open_threads: Any) -> list[str]:
    texts: list[str] = []
    for item in open_threads or ():
        if isinstance(item, str):
            text = item
        elif isinstance(item, Mapping):
            text = item.get("text", item.get("summary", item.get("title")))
        else:
            continue
        piece = str(text or "").strip()
        if piece:
            texts.append(piece)
    return texts


def build_recall_query(
    text: Any,
    *,
    recent_turns: Any = None,
    open_threads: Any = None,
    max_chars: int = 500,
    short_message_chars: int = 120,
    recent_turns_max: int = 2,
    stop_names: Sequence[str] = (),
) -> tuple[str, dict[str, Any]]:
    """Expand the current message with relevant recent turns (host 4.1 rules)."""
    current = str(text or "").strip()
    meta: dict[str, Any] = {"used_turns": 0, "anaphora": False, "used_thread": False}
    if not current:
        return "", meta
    limit = int(max_chars) if max_chars is not None else 500
    if limit <= 0:
        return "", meta
    current = current[:limit]
    anaphora = has_anaphora(current)
    meta["anaphora"] = anaphora

    selected: list[str] = []
    previous = _turn_texts(recent_turns)
    if previous and (len(current) < int(short_message_chars) or anaphora):
        merged = current
        for distance, turn in enumerate(reversed(previous)):
            if len(selected) >= int(recent_turns_max):
                break
            adjacent = distance == 0 and anaphora
            if not (
                adjacent
                or bool(content_terms(turn, stop_names) & content_terms(merged, stop_names))
            ):
                continue
            selected.append(turn)
            merged = f"{merged} {turn}"

    thread_text = ""
    if anaphora:
        current_terms = content_terms(current, stop_names)
        best_overlap = 0
        for thread in _thread_texts(open_threads):
            overlap = len(content_terms(thread, stop_names) & current_terms)
            if overlap > best_overlap:
                thread_text = thread
                best_overlap = overlap
        meta["used_thread"] = bool(thread_text)

    parts = [current]
    remaining = limit - len(current)
    used_turns = 0
    for turn in reversed(selected):
        if remaining <= 1:
            break
        piece = turn[:remaining]
        if not piece:
            break
        parts.append(piece)
        remaining -= len(piece) + 1
        used_turns += 1
    meta["used_turns"] = used_turns
    if thread_text and remaining > 1:
        piece = thread_text[:remaining]
        if piece:
            parts.append(piece)
    return " ".join(parts).strip()[:limit], meta


# -- rendering (host memory/recall_render.py) ------------------------------

@dataclass(frozen=True)
class RenderedRecallBlock:
    """Rendered recall text plus the kept/dropped entry counts."""

    text: str
    kept: int
    dropped: int


_TIER_LABELS = {
    "zh": {"fact": "事实", "reflection": "反思", "persona": "人格", "fact_archive": "旧记忆"},
    "zh-TW": {"fact": "事實", "reflection": "反思", "persona": "人格", "fact_archive": "舊記憶"},
    "en": {
        "fact": "fact", "reflection": "reflection",
        "persona": "persona", "fact_archive": "memory",
    },
}
_ENTITY_LABELS = {
    "zh": {"master": "主人", "neko": "我", "relationship": "关系"},
    "zh-TW": {"master": "主人", "neko": "我", "relationship": "關係"},
    "en": {"master": "about user", "neko": "self", "relationship": "relationship"},
}


def render_recall_entry_tag(kind: Any, entity: Any, language: str = "zh") -> str:
    lang = _normalize_language(language)
    tier_key = str(kind or "").strip()
    entity_key = str(entity or "").strip()
    tier_table = _TIER_LABELS.get(lang) or _TIER_LABELS["en"]
    entity_table = _ENTITY_LABELS.get(lang) or _ENTITY_LABELS["en"]
    tier_text = tier_table.get(tier_key, tier_key or "?")
    entity_text = entity_table.get(entity_key, entity_key or "-")
    return f"[{tier_text}/{entity_text}]"


def _time_suffix(entry: Mapping[str, Any], language: str) -> str:
    anchor = None
    for candidate in (
        entry.get("event_end_at"),
        entry.get("event_start_at"),
        entry.get("created_at"),
    ):
        anchor = _coerce_datetime(candidate)
        if anchor is not None:
            break
    if anchor is None:
        return ""
    date_part = anchor.strftime("%Y-%m-%d")
    label = time_since_label(anchor.isoformat(), lang=language)
    return f"  ({date_part}, {label})" if label else f"  ({date_part})"


def _take_lines_within_budget(
    lines: Sequence[str], max_tokens: int, tokens: _Tokens,
) -> tuple[list[str], int]:
    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        cost = tokens.count(line) + (tokens.count("\n") if kept else 0)
        if kept and used + cost > max_tokens:
            return kept, len(lines) - index
        kept.append(line)
        used += cost
    return kept, 0


def render_recall_block(
    items: Sequence[Mapping[str, Any]] | None,
    language: str = "zh",
    *,
    header_template: str | None = None,
    settings: RecallSettings | None = None,
    token_counter: TokenCounter | None = None,
) -> RenderedRecallBlock:
    """The single recall renderer (numbered lines, three token budgets)."""
    config = settings or RecallSettings()
    tokens = _Tokens(token_counter)
    entries = [dict(item) for item in (items or ()) if isinstance(item, Mapping)]
    lines: list[str] = []
    for item in entries:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        text = tokens.truncate(text, config.render_entry_tokens)
        tag = render_recall_entry_tag(
            item.get("kind") or item.get("tier"), item.get("entity"), language,
        )
        lines.append(f"{len(lines) + 1}. {tag} {text}{_time_suffix(item, language)}")
    lines = [
        tokens.truncate(
            line, config.render_entry_tokens + config.render_line_overhead,
        )
        for line in lines
    ]
    budget = config.render_total_tokens
    if header_template is not None:
        budget -= tokens.count(header_template.format(n=len(lines)))
        budget -= tokens.count("\n")
    kept, dropped = _take_lines_within_budget(lines, max(0, budget), tokens)
    block = list(kept)
    if header_template is not None:
        block.insert(0, header_template.format(n=len(kept)))
    return RenderedRecallBlock(
        text="\n".join(block), kept=len(kept), dropped=dropped,
    )


# -- source background (host memory/companion_context.py) ------------------

_BACKGROUND_LABEL = {
    "zh": "背景", "zh-TW": "背景", "en": "background", "ja": "背景",
    "ko": "배경", "ru": "контекст", "es": "contexto", "pt": "contexto",
}


def _source_rows(
    picked: Sequence[Mapping[str, Any]],
    pool: Sequence[Mapping[str, Any]],
) -> list[tuple[dict[str, Any], str, int]]:
    picked_ids = {
        str(row.get("id") or "") for row in picked if isinstance(row, Mapping)
    }
    wanted: set[str] = set()
    for row in picked:
        source_ids = row.get("source_fact_ids")
        if not isinstance(source_ids, (list, tuple)):
            continue
        for raw in source_ids:
            source_id = str(raw or "").strip()
            if source_id and source_id not in picked_ids:
                wanted.add(source_id)
    if not wanted:
        return []
    by_id: dict[str, dict[str, Any]] = {}
    for row in pool:
        row_id = str(row.get("id") or "")
        if row_id not in wanted:
            continue
        current = by_id.get(row_id)
        if current is None or (
            str(current.get("_tier") or current.get("kind") or "") == "fact_archive"
            and str(row.get("_tier") or row.get("kind") or "") != "fact_archive"
        ):
            by_id[row_id] = dict(row)
    seen: set[str] = set()
    candidates: list[tuple[dict[str, Any], str, int]] = []
    for picked_row in picked:
        source_ids = picked_row.get("source_fact_ids")
        if not isinstance(source_ids, (list, tuple)) or not source_ids:
            continue
        picked_id = str(picked_row.get("id") or "")
        picked_terms = content_terms(picked_row.get("text"))
        for raw in source_ids:
            source_id = str(raw or "").strip()
            if not source_id or source_id in seen or source_id in picked_ids:
                continue
            source = by_id.get(source_id)
            if source is None:
                continue
            seen.add(source_id)
            source_terms = content_terms(source.get("text"))
            candidates.append((source, picked_id, len(source_terms & picked_terms)))
    return candidates


def expand_source_background(
    picked: Sequence[Mapping[str, Any]],
    pool: Sequence[Mapping[str, Any]],
    *,
    query: str = "",
    stop_names: Sequence[str] = (),
    max_items: int = 2,
    max_tokens: int = 400,
    min_tokens: int = 8,
    token_counter: TokenCounter | None = None,
) -> list[dict[str, Any]]:
    """Follow picked reflections to a few source facts (optional background)."""
    if max_items <= 0 or max_tokens <= 0:
        return []
    tokens = _Tokens(token_counter)
    query_terms = content_terms(query, stop_names)
    anaphora = has_anaphora(query)
    candidates = _source_rows(picked, pool)
    per_picked: dict[str, int] = {}
    for _source, source_of, _overlap in candidates:
        per_picked[source_of] = per_picked.get(source_of, 0) + 1
    filtered: list[tuple[dict[str, Any], str]] = []
    for source, source_of, overlap in candidates:
        if overlap:
            filtered.append((source, source_of))
            continue
        source_terms = content_terms(source.get("text"), stop_names)
        if source_terms & query_terms:
            filtered.append((source, source_of))
            continue
        if anaphora and per_picked.get(source_of, 0) == 1:
            filtered.append((source, source_of))
    rows: list[dict[str, Any]] = []
    used = 0
    for source, source_of in filtered:
        if len(rows) >= max_items:
            break
        text = str(source.get("text") or "").strip()
        if not text:
            continue
        remaining = max_tokens - used
        if remaining <= 0 or (remaining < min_tokens and rows):
            break
        cost = tokens.count(text)
        if cost > remaining:
            text = tokens.truncate(text, remaining)
            if not text:
                break
            cost = tokens.count(text)
        rows.append({
            "id": str(source.get("id") or ""),
            "text": text,
            "kind": str(source.get("_tier") or source.get("kind") or "fact"),
            "entity": source.get("entity"),
            "created_at": source.get("created_at"),
            "event_start_at": source.get("event_start_at"),
            "event_end_at": source.get("event_end_at"),
            "source_of": source_of,
        })
        used += cost
        if used >= max_tokens:
            break
    return rows


def _render_background_lines(
    rows: Sequence[Mapping[str, Any]],
    language: str,
    *,
    start_index: int = 0,
    token_counter: TokenCounter | None = None,
) -> list[str]:
    tokens = _Tokens(token_counter)
    lang = _normalize_language(language)
    label = _BACKGROUND_LABEL.get(lang, _BACKGROUND_LABEL["en"])
    lines: list[str] = []
    for row in rows:
        text = tokens.truncate(str(row.get("text") or "").strip(), 400).strip()
        if not text:
            continue
        lines.append(
            f"{start_index + len(lines) + 1}. [{label}] {text}{_time_suffix(row, language)}"
        )
    return lines


# -- evidence score (reflection rows carry reinforcement/disputation) ------

_REIN_HALF_LIFE_DAYS = 30.0
_DISP_HALF_LIFE_DAYS = 180.0


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _age_days(value: Any, now: datetime) -> float:
    parsed = _coerce_datetime(value)
    if parsed is None:
        return 0.0
    delta = (now - parsed).total_seconds()
    return 0.0 if delta <= 0 else delta / 86400.0


def evidence_score(entry: Mapping[str, Any], now: datetime) -> float:
    """Net decayed evidence (+rein -disp); protected rows score +inf."""
    if entry.get("protected"):
        return float("inf")
    reinforcement = _as_float(entry.get("reinforcement"))
    disputation = _as_float(entry.get("disputation"))
    if reinforcement:
        reinforcement *= 0.5 ** (
            _age_days(entry.get("rein_last_signal_at"), now) / _REIN_HALF_LIFE_DAYS
        )
    if disputation:
        disputation *= 0.5 ** (
            _age_days(entry.get("disp_last_signal_at"), now) / _DISP_HALF_LIFE_DAYS
        )
    return reinforcement - disputation


# -- service ----------------------------------------------------------------

_EMPTY_CORRECTIONS = {"block": "", "items": [], "version": 0}
_EMPTY_GROWTH = {"block": "", "items": [], "version": 0}
_EMPTY_STATE = {"block": "", "version": 0, "updated_at": None, "items": []}

#: State kinds whose items are unfinished things a follow-up can continue.
_OPEN_THREAD_KINDS = ("progress", "concerns", "thoughts")
#: Kinds that are explicitly "waiting on something" rather than merely open.
_WAITING_THREAD_KINDS = frozenset({"progress", "concerns"})


def _threads_from_state(state: Any) -> list[dict[str, Any]] | None:
    """Open/waiting items of a state snapshot as continuation candidates.

    Returns None when the snapshot has no usable item, so the caller can tell
    "no state at all" apart from "state with nothing open".
    """
    if not isinstance(state, Mapping):
        return None
    threads: list[dict[str, Any]] = []
    for kind in _OPEN_THREAD_KINDS:
        for item in state.get(kind) or ():
            if not isinstance(item, Mapping):
                continue
            if str(item.get("state") or "").lower() in ("done", "cancelled", "closed"):
                continue
            text = str(item.get("text") or item.get("label") or "").strip()
            if not text:
                continue
            threads.append({
                "text": text,
                "thread_id": str(item.get("id") or ""),
                "kind": kind,
                "state": "waiting" if kind in _WAITING_THREAD_KINDS else "open",
                "next_step": str(item.get("next_step") or ""),
            })
    return threads or None


def _block_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, MemoryBlock):
        return {
            "block": block.block,
            "items": [dict(item) for item in block.items],
            "version": int(block.version),
        }
    if isinstance(block, Mapping):
        return {
            "block": str(block.get("block") or ""),
            "items": [dict(item) for item in block.get("items") or ()
                      if isinstance(item, Mapping)],
            "version": int(block.get("version") or 0),
        }
    return dict(_EMPTY_CORRECTIONS)


class _FallbackClock:
    """Clock fallback so the service is usable in isolated tests."""

    def now(self) -> datetime:
        return datetime.now().astimezone()

    def monotonic(self) -> float:
        return time.monotonic()


class RecallService:
    """Read-only recall over the B05 stores plus optional vectors/tokens.

    Constructor is keyword-only and duck-typed so composition can pass the B05
    services directly:

    * facts: FactsView (load/load_archive)
    * reflections: ReflectionService (load/evidence_score/growth_block)
    * persona: PersonaService (entries)
    * corrections: CorrectionService (block), optional
    * barrier: DeletionBarrier (blocks_row/snapshot/is_stale/state), optional
    * clock, token_counter, embedding: injected ports, optional

    All public methods are async, never write, and never raise: failures are
    reported as status=unavailable/error with empty blocks.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        facts: Any = None,
        reflections: Any = None,
        persona: Any = None,
        corrections: Any = None,
        barrier: Any = None,
        token_counter: TokenCounter | None = None,
        embedding: EmbeddingProvider | None = None,
        settings: RecallSettings | None = None,
        stop_names: Sequence[str] = (),
        roots: Any = None,
        state: Any = None,
        working_summary: Any = None,
        interests: Any = None,
    ) -> None:
        self._clock: Clock = clock if clock is not None else _FallbackClock()
        self._facts = facts
        self._reflections = reflections
        self._persona = persona
        self._corrections = corrections
        self._barrier = barrier
        self._tokens = _Tokens(token_counter)
        self._embedding = embedding
        self._settings = settings or RecallSettings()
        self._stop_names = tuple(
            str(name).strip() for name in stop_names or () if str(name or "").strip()
        )
        self._roots = roots  # kept for composition symmetry; recall never writes
        # M5: the real companion state and the summary projector. Both optional
        # so a B06-only core keeps returning the same empty state it always did
        # rather than a fabricated one.
        self._state = state
        self._working_summary = working_summary
        self._interests = interests

    @property
    def settings(self) -> RecallSettings:
        return self._settings

    @staticmethod
    def _coerce_recall_args(
        character: Any,
        query: Any,
        subject: Any,
        scene: Any,
        purpose: Any,
        budget_ms: Any,
        max_items: Any,
    ) -> tuple[Any, Any, Any, Any, Any, Any, Any]:
        """Accept a ContextRequest-like object as the first argument."""
        if isinstance(character, str) or character is None:
            return character, query, subject, scene, purpose, budget_ms, max_items
        request = character
        scope = getattr(request, "scope", None)
        if scope is not None:
            character = getattr(scope, "character", None)
            if subject is None:
                subject = getattr(scope, "subject", None)
        if not query:
            query = getattr(request, "current_input", "")
        if scene is None:
            scene = getattr(request, "scene", None)
        if purpose in (None, "", "query"):
            purpose = getattr(request, "purpose", purpose) or purpose
        if budget_ms is None:
            budget_ms = getattr(request, "budget_ms", None)
        if max_items is None:
            max_items = getattr(request, "max_items", None)
        return character, query, subject, scene, purpose, budget_ms, max_items

    # -- public API -----------------------------------------------------

    async def recall_memory(
        self,
        character: Any,
        query: Any = "",
        *,
        subject: Any = None,
        scene: Any = None,
        purpose: str = "query",
        budget_ms: int | None = None,
        max_items: int | None = None,
        token_budget: int | None = None,
        time_range: Any = None,
        language: str = "zh",
        stop_names: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Explicit recall (host recall_memory tool / /query_memory)."""
        start = self._clock.monotonic()
        try:
            character, query, subject, scene, purpose, budget_ms, max_items = (
                self._coerce_recall_args(
                    character, query, subject, scene, purpose,
                    budget_ms, max_items,
                )
            )
            name = self._character(character)
            if self._facts is None or self._reflections is None:
                return self._unavailable(start, "recall components not wired")
            query_text = str(query or "").strip()
            window = parse_time_range(time_range)
            if time_range is not None and window is None:
                return self._result(
                    "miss", start, error_code="invalid_time_range",
                )
            if not query_text and window is None:
                return self._result("miss", start)
            if not query_text and window is not None:
                timed = await self.recall_by_time(
                    name,
                    time_range=window,
                    subject=subject,
                    scene=scene,
                    limit=max_items,
                    language=language,
                    stop_names=stop_names,
                )
                return self._result(
                    "matched" if timed.get("items") else "miss",
                    start,
                    items=timed.get("items") or (),
                    mode="time",
                    scene_filtered=int(timed.get("scene_filtered") or 0),
                    corrections_block=self._corrections_block(
                        name, subject, scene, language,
                    ),
                    growth_block=self._growth_block(name, scene, language, subject),
                    query_meta=timed.get("query_meta") or {},
                    candidates_total=int(timed.get("candidates_total") or 0),
                    error_code=timed.get("error_code"),
                )
            deadline = self._deadline(start, budget_ms)
            subjects = _normalize_subjects(subject)
            pool, scene_filtered = self._collect_pool(
                name, subjects=subjects, scene=scene,
                stop_names=tuple(stop_names) or self._stop_names,
            )
            if window is not None:
                for tier, rows in pool.items():
                    pool[tier] = [
                        row for row in rows if _overlaps_window(row, window)
                    ]
            bm25_pool = self._candidate_rows(pool)
            embedding_pool = self._embedding_rows(pool)
            if not bm25_pool:
                return self._result(
                    "miss", start, query_meta={}, scene_filtered=scene_filtered,
                )
            tokens = _Tokens(self._token_counter)
            names = tuple(stop_names) or self._stop_names
            bm25_scored = bm25_scores(
                query_text, bm25_pool, stop_names=names,
                k1=self._settings.bm25_k1, b=self._settings.bm25_b,
            ) if query_text else []
            cosine_scored: list[tuple[dict[str, Any], float]] = []
            mode = "bm25"
            query_vector = await self._query_vector(
                query_text, deadline, tokens,
            )
            if query_vector is not None and embedding_pool:
                cosine_scored = _cosine_rank(query_vector, embedding_pool)
                if cosine_scored:
                    mode = "hybrid"
            bm25_top = [
                (row, score) for row, score, _tf in bm25_scored
                if score >= self._settings.bm25_threshold
            ][: self._settings.budget_each]
            cosine_top = [
                (row, score) for row, score in cosine_scored
                if score >= self._settings.cosine_threshold
            ][: self._settings.budget_each]
            fused = rrf_fuse(
                bm25_top, cosine_top,
                k=self._settings.rrf_k,
                budget_total=self._settings.budget_total,
            )
            limit = (
                self._settings.budget_total if max_items is None
                else max(0, int(max_items))
            )
            items = self._items_from_rows(fused[:limit], mode)
            if token_budget is not None and token_budget > 0:
                kept: list[dict[str, Any]] = []
                used = 0
                for item in items:
                    cost = tokens.count(item.get("text") or "")
                    if used + cost > int(token_budget):
                        break
                    kept.append(item)
                    used += cost
                items = kept
            corrections_block = self._corrections_block(name, subject, scene, language)
            growth_block = self._growth_block(name, scene, language, subject)
            return self._result(
                "matched" if items else "miss",
                start,
                items=items,
                mode=mode,
                scene_filtered=scene_filtered,
                corrections_block=corrections_block,
                growth_block=growth_block,
                query_meta={
                    "query": query_text,
                    "time_range": self._window_meta(window),
                },
                candidates_total=len(bm25_pool),
            )
        except Exception as exc:  # noqa: BLE001 - recall never raises
            return self._error(start, exc)

    async def recall_by_time(
        self,
        character: Any,
        *,
        time_range: Any,
        subject: Any = None,
        subjects: Any = None,
        scene: Any = None,
        limit: int | None = None,
        language: str = "zh",
        stop_names: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Rows closest to a time window (host recall_by_time)."""
        start = self._clock.monotonic()
        try:
            name = self._character(character)
            if self._facts is None or self._reflections is None:
                return self._unavailable(start, "recall components not wired")
            window = parse_time_range(time_range)
            if window is None:
                return self._result(
                    "miss", start, error_code="invalid_time_range",
                )
            subjects_value = subjects if subjects is not None else subject
            pool, scene_filtered = self._collect_pool(
                name, subjects=_normalize_subjects(subjects_value), scene=scene,
                stop_names=tuple(stop_names) or self._stop_names,
            )
            candidates = [
                row for row in self._candidate_rows(
                    pool, include_persona=False,
                )
                if _overlaps_window(row, window)
            ]
            win_start, win_end = window
            scored: list[tuple[int, float, datetime, dict[str, Any]]] = []
            for row in candidates:
                event = entry_event_window(row)
                if event is None:
                    continue
                first, last = event
                in_window = first < win_end and last >= win_start
                if in_window:
                    distance = 0.0
                elif last < win_start:
                    distance = (win_start - last).total_seconds()
                else:
                    distance = (first - win_end).total_seconds()
                scored.append((0 if in_window else 1, distance, first, row))
            scored.sort(key=lambda item: (item[0], item[1], win_start - item[2]))
            cap = self._settings.time_budget if limit is None else max(0, int(limit))
            picked = [row for _flag, _dist, _start, row in scored[:cap]]
            items = self._items_from_rows(picked, "time")
            for item in items:
                item["score"] = None
            return self._result(
                "matched" if items else "miss",
                start,
                items=items,
                mode="time",
                scene_filtered=scene_filtered,
                query_meta={"time_range": self._window_meta(window)},
                candidates_total=len(candidates),
            )
        except Exception as exc:  # noqa: BLE001
            return self._error(start, exc)

    async def build_turn_context(
        self,
        character: Any,
        *,
        current_input: Any = "",
        recent_turns: Any = None,
        open_threads: Any = None,
        scene: Any = None,
        purpose: str = "chat",
        language: str = "zh",
        budget_ms: int | None = None,
        max_context_tokens: int | None = None,
        request_id: str | None = None,
        stop_names: Sequence[str] = (),
        subject: Any = None,
    ) -> dict[str, Any]:
        """Passive turn context (host-shaped response subset), plus M5 layers.

        M5 makes two additative changes without touching the existing keys:

        * 'companion_state' is now a real StateService snapshot instead of the
          empty placeholder. It is read **once** and both rendered as a block
          and returned verbatim, so the version the model saw and the version
          the caller can audit are the same read -- a commit landing between
          the two would otherwise make the payload lie;
        * 'working_summary' and 'episode_summary' carry the rebuildable
          projections (growth / important facts / open threads / one episode).

        The open threads a continuation question resolves against are derived
        from that same state snapshot when the caller does not supply them:
        before M5 the parameter existed but nobody passed it, so "那个后来怎么
        样" had no thread to continue.
        """
        start = self._clock.monotonic()
        # RO03: the turn's own words must reach the summary projector, or
        # `important_facts` can only ever be "the most important rows" and a
        # plain picture turn inherits facts that have nothing to do with it.
        # One build per call is the snapshot: this read is never cached across
        # turns, so the next turn sees the next version.
        extras = self._turn_extras(
            character, subject=subject, scene=scene, language=language,
            current_input=str(current_input or ""),
        )
        threads = open_threads if open_threads is not None else extras["threads"]
        followup = self._resolve_followup(
            character, current_input, subject=subject, scene=scene,
            language=language,
        )
        result = await self._turn_context_core(
            character,
            current_input=current_input,
            recent_turns=recent_turns,
            open_threads=threads,
            scene=scene,
            purpose=purpose,
            language=language,
            budget_ms=budget_ms,
            max_context_tokens=max_context_tokens,
            request_id=request_id,
            stop_names=stop_names,
            subject=subject,
            start=start,
            followup_thread=(
                str(followup["thread"].get("text") or "")
                if followup.get("status") == "resolved" and followup.get("thread")
                else ""
            ),
            # A brand new topic must never inherit the open thread's episode:
            # the fallback only applies once the resolver decided the message
            # *is* a continuation (design section 10.4 -- "an explicit new topic
            # stops inheriting the old one").
            followup_episode=(
                str(followup["thread"].get("episode_id") or "")
                if followup.get("status") == "resolved" and followup.get("thread")
                else (
                    extras["thread_episode"]
                    if followup.get("status") != "not_continuation"
                    else ""
                )
            ),
        )
        result["companion_state"] = extras["state"]
        result["companion_state_version"] = extras["state_version"]
        result["working_summary"] = extras["summary"]
        result["threads_source"] = (
            "caller" if open_threads is not None else "companion_state"
        )
        result["episode_summary"] = extras["episode"]
        result["followup"] = followup
        # RO03: interest evidence is reported, never injected as prose. The
        # section it feeds (`about_owner`) is written by the user or by
        # evolution; this is the material that decision is made from, so a
        # caller can show it without the core writing the persona.
        result["interest_signals"] = self._interest_snapshot(
            character, subject=subject, scene=scene,
        )
        return result

    def _interest_snapshot(
        self, character: Any, *, subject: Any, scene: Any,
    ) -> dict[str, Any]:
        """Interest evidence for this subject/scene; unwired layers are empty."""
        empty = {
            "version": 0, "items": [], "stable_interests": [],
            "recent_interests": [], "avoid_topics": [],
        }
        service = self._interests
        if service is None or is_friend_subject(subject):
            return empty
        name = str(character or "").strip()
        if not name:
            return empty
        return dict(service.snapshot(
            name, subject=str(subject or "private"), scene=scene,
        ))

    def _resolve_followup(
        self,
        character: Any,
        query: Any,
        *,
        subject: Any,
        scene: Any,
        language: str,
    ) -> dict[str, Any]:
        """Which open thread a continuation question is about (M5 / G18).

        "后来怎么样" has to continue the most recent open thread; when two items
        are equally plausible the answer is *which ones*, so the caller can ask
        a natural clarifying question instead of guessing (design section 10.4).
        Only a continuation question is resolved: an ordinary new topic must not
        be dragged back to an old item.
        """
        empty = {"status": "not_continuation", "thread": None,
                 "candidates": [], "clarify_basis": []}
        if self._working_summary is None or is_friend_subject(subject):
            return empty
        text = str(query or "")
        if not text.strip():
            return empty
        if not (has_anaphora(text) or continuation_markers(text)):
            return empty
        try:
            return self._working_summary.resolve_followup(
                character, text, subject=subject, scene=scene, language=language,
            )
        except Exception:  # noqa: BLE001 - resolution is an optional layer
            return {"status": "miss", "thread": None, "candidates": [],
                    "clarify_basis": []}

    def _turn_extras(
        self,
        character: Any,
        *,
        subject: Any,
        scene: Any,
        language: str,
        current_input: str = "",
    ) -> dict[str, Any]:
        """Read the state snapshot and the summary projections once (never raises)."""
        extras: dict[str, Any] = {
            "state": dict(_EMPTY_STATE),
            "state_version": 0,
            "summary": None,
            "episode": None,
            "threads": None,
            "thread_episode": "",
        }
        name = str(character or "").strip()
        if not name or is_friend_subject(subject):
            return extras
        subject_key = str(subject or "private").strip() or "private"
        snapshot = None
        if self._state is not None:
            try:
                snapshot = self._state.read(name, subject=subject_key)
            except Exception:  # noqa: BLE001 - state is an optional layer
                snapshot = None
        if snapshot is not None:
            extras["state"] = self._state_payload(snapshot)
            extras["state_version"] = int(getattr(snapshot, "version", 0) or 0)
            extras["threads"] = _threads_from_state(getattr(snapshot, "state", None))
        if self._working_summary is not None:
            try:
                summary = self._working_summary.build(
                    name, subject=subject_key, scene=scene, language=language,
                    query=current_input,
                )
            except Exception:  # noqa: BLE001 - a summary never fails a turn
                summary = None
            if summary is not None:
                extras["summary"] = summary.to_dict()
                extras["episode"] = (
                    dict(summary.episode) if summary.episode else None
                )
                if extras["threads"] is None:
                    items = summary.section("open_threads").items
                    extras["threads"] = [
                        {
                            "text": str(item.get("text") or ""),
                            "thread_id": item.get("thread_id"),
                            "next_step": item.get("next_step"),
                        }
                        for item in items
                        if str(item.get("text") or "").strip()
                    ] or None
                extras["thread_episode"] = self._thread_episode(
                    summary, extras["threads"],
                )
        return extras

    @staticmethod
    def _thread_episode(
        summary: Any, threads: Any,
    ) -> str:
        """The episode id the continuation target belongs to ("" when unknown)."""
        if not threads:
            return ""
        lead = str((threads[0] or {}).get("text") or "").strip()
        for item in summary.section("open_threads").items:
            if str(item.get("text") or "").strip() == lead:
                return str(item.get("episode_id") or "")
        return ""

    @staticmethod
    def _state_payload(snapshot: Any) -> dict[str, Any]:
        """The companion_state block in its frozen wire shape."""
        payload = {
            "block": str(getattr(snapshot, "block", "") or ""),
            "version": int(getattr(snapshot, "version", 0) or 0),
            "updated_at": getattr(snapshot, "updated_at", None),
            "items": [
                dict(item) for item in (getattr(snapshot, "items", ()) or ())
                if isinstance(item, Mapping)
            ],
        }
        state = getattr(snapshot, "state", None)
        if isinstance(state, Mapping):
            payload["state"] = {
                str(kind): [dict(item) for item in rows if isinstance(item, Mapping)]
                for kind, rows in state.items()
            }
        lease = getattr(snapshot, "lease", None)
        payload["lease"] = dict(lease) if isinstance(lease, Mapping) else None
        return payload

    async def _turn_context_core(
        self,
        character: Any,
        *,
        current_input: Any = "",
        recent_turns: Any = None,
        open_threads: Any = None,
        scene: Any = None,
        purpose: str = "chat",
        language: str = "zh",
        budget_ms: int | None = None,
        max_context_tokens: int | None = None,
        request_id: str | None = None,
        stop_names: Sequence[str] = (),
        subject: Any = None,
        start: float | None = None,
        followup_thread: str = "",
        followup_episode: str = "",
    ) -> dict[str, Any]:
        """The B06 passive-context body; M5 layers are attached by the caller."""
        if start is None:
            start = self._clock.monotonic()
        try:
            name = self._character(character)
            if self._facts is None or self._reflections is None:
                return self._turn_result(
                    "unavailable", start, request_id=request_id,
                    reason="recall components not wired",
                )
            settings = self._settings
            tokens = _Tokens(self._token_counter)
            names = tuple(stop_names) or self._stop_names
            deadline = self._deadline(start, budget_ms)
            snapshot = self._barrier_snapshot(name)
            versions = self._barrier_versions(name)
            # M5: when the caller passed no threads but the follow-up resolver
            # identified the open item a continuation question is about, that
            # item's text is what lets the query reach the right memories.
            threads_for_query = open_threads
            if not threads_for_query and followup_thread:
                threads_for_query = [{"text": followup_thread}]
            query, query_meta = build_recall_query(
                current_input,
                recent_turns=recent_turns,
                open_threads=threads_for_query,
                max_chars=settings.query_max_chars,
                short_message_chars=settings.short_message_chars,
                recent_turns_max=settings.recent_turns_max,
                stop_names=names,
            )
            # M5: a resolved follow-up is the strongest signal there is about
            # what "that" means. The thread's own text is appended to the query
            # even when the word-overlap heuristic declined it, otherwise
            # "那个后来怎么样" stays a miss and the continuation retrieves
            # nothing -- which is exactly the pre-M5 behaviour this fixes.
            if followup_thread and not query_meta.get("used_thread"):
                merged = f"{query} {followup_thread}".strip()
                query = merged[: settings.query_max_chars]
                query_meta = dict(query_meta, used_thread=True,
                                  thread_from_followup=True)
            if not query:
                return self._turn_result(
                    "miss", start, query_meta=query_meta,
                    request_id=request_id, versions=versions,
                )
            subjects = _normalize_subjects(subject)
            pool, scene_filtered = self._collect_pool(
                name, subjects=subjects, scene=scene, stop_names=names,
            )
            corrections_block = self._corrections_block(
                name, subject, scene, language,
            )
            growth_block = self._growth_block(name, scene, language, subject)
            bm25_pool = self._candidate_rows(pool)
            embedding_pool = self._embedding_rows(pool)
            if not bm25_pool:
                return self._turn_result(
                    "miss", start, query_meta=query_meta,
                    scene_filtered=scene_filtered,
                    corrections=corrections_block, growth=growth_block,
                    request_id=request_id, versions=versions,
                )
            bm25_scored = bm25_scores(
                query, bm25_pool, stop_names=names,
                k1=settings.bm25_k1, b=settings.bm25_b,
            )
            query_vector = await self._query_vector(query, deadline, tokens)
            cosine_scored: list[tuple[dict[str, Any], float]] = []
            if query_vector is not None and embedding_pool:
                cosine_scored = _cosine_rank(query_vector, embedding_pool)
            picked, mode = self._passive_pick(
                query, bm25_scored, cosine_scored, names, purpose,
            )
            if not picked and followup_episode:
                # M5: a resolved continuation ("后来怎么样") continues the
                # episode of the thread it resolved to. Counting words alone
                # would miss it -- the current message is pure anaphora, so its
                # only useful term is "面试" buried in a stop-word cloud. The
                # episode membership *is* the anchor, and it is derived, so a
                # deleted episode simply stops being selected.
                picked = [
                    row for row in bm25_pool
                    if episode_id_of(row) == followup_episode
                ][: settings.passive_max_items]
                if picked:
                    mode = "episode"
            if not picked:
                return self._turn_result(
                    "miss", start, query_meta=query_meta, mode=mode,
                    scene_filtered=scene_filtered,
                    corrections=corrections_block, growth=growth_block,
                    request_id=request_id, versions=versions,
                )
            rendered = render_recall_block(
                [self._render_item(row) for row in picked],
                language,
                settings=settings,
                token_counter=self._token_counter,
            )
            context = rendered.text.strip()
            if not context:
                return self._turn_result(
                    "miss", start, query_meta=query_meta, mode=mode,
                    scene_filtered=scene_filtered,
                    corrections=corrections_block, growth=growth_block,
                    request_id=request_id, versions=versions,
                )
            main_line_count = len(context.split("\n"))
            source_rows = expand_source_background(
                [self._render_item(row) for row in picked],
                [self._render_item(row) for row in bm25_pool],
                query=query,
                stop_names=names,
                max_items=settings.source_background_items,
                max_tokens=settings.source_background_tokens,
                min_tokens=settings.source_background_min_tokens,
                token_counter=self._token_counter,
            )
            source_ids = [str(row.get("id") or "") for row in source_rows if row.get("id")]
            if source_rows:
                background_lines = _render_background_lines(
                    source_rows, language, start_index=main_line_count,
                    token_counter=self._token_counter,
                )
                if background_lines:
                    context = context + "\n" + "\n".join(background_lines)
            limit = self._context_limit(max_context_tokens)
            if limit is not None and limit <= 0:
                return self._turn_result(
                    "miss", start, query_meta=query_meta, mode=mode,
                    scene_filtered=scene_filtered,
                    corrections=corrections_block, growth=growth_block,
                    request_id=request_id, versions=versions,
                )
            if limit is not None:
                lines, _dropped = _take_lines_within_budget(
                    context.split("\n"), limit, tokens,
                )
                context = "\n".join(lines).strip()
                if not context:
                    return self._turn_result(
                        "miss", start, query_meta=query_meta, mode=mode,
                        scene_filtered=scene_filtered,
                        corrections=corrections_block, growth=growth_block,
                        request_id=request_id, versions=versions,
                    )
                kept_background = max(0, len(lines) - main_line_count)
                source_ids = source_ids[:kept_background]
            context_tokens = tokens.count(context)
            corrections_block = self._trim_block(
                corrections_block, context_tokens, limit, tokens,
            )
            growth_block = self._trim_block(
                growth_block, context_tokens, limit, tokens,
            )
            episode = episode_background(
                query,
                picked,
                bm25_pool,
                max_items=settings.episode_background_items,
                max_chars=settings.episode_entry_max_chars,
            )
            result = self._turn_result(
                "matched", start,
                context=context,
                memory_ids=[str(row.get("id") or "") for row in picked],
                tokens=context_tokens,
                mode=mode,
                query_meta=query_meta,
                scene_filtered=scene_filtered,
                source_ids=source_ids,
                corrections=corrections_block,
                growth=growth_block,
                episode=episode,
                request_id=request_id,
                versions=versions,
            )
            if self._is_stale(name, snapshot):
                result["result"] = "superseded"
            return result
        except Exception as exc:  # noqa: BLE001
            return self._turn_result(
                "error", start, request_id=request_id,
                reason=type(exc).__name__,
            )

    # -- internals: basic -------------------------------------------------

    @property
    def _token_counter(self) -> TokenCounter | None:
        return self._tokens._counter

    @staticmethod
    def _character(character: Any) -> str:
        value = str(character or "").strip()
        if not value:
            raise ValueError("character must be nonempty")
        return value

    def _deadline(self, start: float, budget_ms: int | None) -> float:
        settings = self._settings
        raw = settings.budget_ms if budget_ms is None else int(budget_ms)
        raw = max(settings.min_budget_ms, min(raw, settings.max_budget_ms))
        return start + raw / 1000.0

    def _remaining(self, deadline: float) -> float:
        return deadline - self._clock.monotonic()

    def _context_limit(self, max_context_tokens: int | None) -> int | None:
        if max_context_tokens is None:
            return None
        settings = self._settings
        value = int(max_context_tokens)
        if value < settings.max_context_tokens_min:
            return 0
        return min(value, settings.max_context_tokens_max)

    def _barrier_snapshot(self, character: str) -> Any:
        if self._barrier is None:
            return None
        try:
            return self._barrier.snapshot(character)
        except Exception:  # noqa: BLE001 - barrier absence is not a recall failure
            return None

    def _barrier_versions(self, character: str) -> dict[str, int]:
        versions = {"deletion_version": 0, "correction_version": 0}
        if self._barrier is None:
            return versions
        try:
            state = self._barrier.state(character)
            versions["deletion_version"] = int(
                getattr(state, "deletion_version", 0) or 0
            )
            versions["correction_version"] = int(
                getattr(state, "correction_version", 0) or 0
            )
        except Exception:  # noqa: BLE001
            pass
        return versions

    def _is_stale(self, character: str, snapshot: Any) -> bool:
        if self._barrier is None or snapshot is None:
            return False
        try:
            return bool(self._barrier.is_stale(character, snapshot))
        except Exception:  # noqa: BLE001
            return False

    def _window_meta(
        self, window: tuple[datetime, datetime] | None,
    ) -> list[str] | None:
        if window is None:
            return None
        return [window[0].isoformat(), window[1].isoformat()]

    # -- internals: pool --------------------------------------------------

    def _tag(self, rows: Sequence[Mapping[str, Any]], tier: str) -> list[dict[str, Any]]:
        now = self._clock.now()
        tagged: list[dict[str, Any]] = []
        for row in rows or ():
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            item["_tier"] = tier
            if tier == "reflection":
                item.setdefault("target_type", "reflection")
                if _as_float(item.get("disputation")) > 0.0:
                    item["score"] = evidence_score(item, now)
            tagged.append(item)
        return tagged

    def _collect_pool(
        self,
        character: str,
        *,
        subjects: tuple[str, ...] | None,
        scene: Any,
        stop_names: Sequence[str] = (),
    ) -> tuple[dict[str, list[dict[str, Any]]], int]:
        from .reflection import REFLECTION_TERMINAL_STATUSES

        scene_filtered = 0
        tiers: dict[str, list[dict[str, Any]]] = {
            "fact": [], "reflection": [], "fact_archive": [], "persona": [],
        }
        facts: list[dict[str, Any]] = []
        if self._facts is not None:
            facts = list(self._facts.load(character) or ())
            archive = list(self._facts.load_archive(character) or ())
        else:
            archive = []
        active_ids = {
            str(row.get("id") or "") for row in facts
            if isinstance(row, Mapping) and row.get("id")
        }
        archive = [
            row for row in archive
            if isinstance(row, Mapping)
            and str(row.get("id") or "") not in active_ids
            and not row.get("subject_archived_at")
            and not row.get("arbitration_archived_at")
        ]
        reflections: list[dict[str, Any]] = []
        facts_only = bool(subjects) and all(is_friend_subject(subject) for subject in subjects)
        if self._reflections is not None and not facts_only:
            reflections = [
                row for row in (self._reflections.load(character) or ())
                if isinstance(row, Mapping)
                and str(row.get("status") or "") not in REFLECTION_TERMINAL_STATUSES
            ]
        persona_rows: list[dict[str, Any]] = []
        if self._persona is not None and not facts_only:
            persona_rows = self._persona_left_out(
                character, subjects=subjects, scene=scene,
            )

        tiers["fact"] = self._tag(facts, "fact")
        tiers["reflection"] = self._tag(reflections, "reflection")
        tiers["fact_archive"] = self._tag(archive, "fact_archive")
        tiers["persona"] = self._tag(persona_rows, "persona")

        for tier, rows in tiers.items():
            kept: list[dict[str, Any]] = []
            for row in rows:
                if not _row_in_subjects(row, subjects):
                    continue
                if not entry_applies_to_scene(
                    row, scene, max_chars=self._settings.scene_value_max_chars,
                ):
                    scene_filtered += 1
                    continue
                kept.append(row)
            tiers[tier] = kept

        for tier in list(tiers):
            tiers[tier] = filter_valid_entries(tiers[tier])
            tiers[tier] = self._barrier_filter(
                character, tiers[tier], subjects,
            )
            tiers[tier] = self._hard_filter(tiers[tier])
        return tiers, scene_filtered

    def _persona_left_out(
        self,
        character: str,
        *,
        subjects: tuple[str, ...] | None,
        scene: Any,
    ) -> list[dict[str, Any]]:
        try:
            entries = self._persona.entries(character)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - optional pool
            return []
        now = self._clock.now()
        candidates: list[dict[str, Any]] = []
        for entry in entries or ():
            if not isinstance(entry, Mapping):
                continue
            if not str(entry.get("text") or "").strip():
                continue
            if entry.get("suppress") or entry.get("protected"):
                continue  # rendered in their own persona section
            if not _row_in_subjects(entry, subjects):
                continue
            if not entry_applies_to_scene(
                entry, scene, max_chars=self._settings.scene_value_max_chars,
            ):
                continue
            candidates.append(dict(entry))
        candidates.sort(
            key=lambda item: evidence_score(item, now), reverse=True,
        )
        budget = self._settings.persona_render_tokens
        used = 0
        left_out: list[dict[str, Any]] = []
        for entry in candidates:
            cost = self._tokens.count(str(entry.get("text") or "")) + 1
            if used + cost <= budget:
                used += cost
                continue
            left_out.append(entry)
        return left_out

    def _barrier_filter(
        self,
        character: str,
        rows: Sequence[dict[str, Any]],
        subjects: tuple[str, ...] | None,
    ) -> list[dict[str, Any]]:
        if self._barrier is None:
            return list(rows)
        barrier_subject = "private" if subjects is None else (
            subjects[0] if len(subjects) == 1 else None
        )
        if hasattr(self._barrier, "filter_rows"):
            return self._barrier.filter_rows(character, rows, subject=barrier_subject)
        kept: list[dict[str, Any]] = []
        for row in rows:
            try:
                try:
                    blocked = self._barrier.blocks_row(
                        character, row, subject=barrier_subject,
                    )
                except TypeError:
                    blocked = self._barrier.blocks_row(character, row)
            except Exception:  # noqa: BLE001 - barrier failure keeps the row
                blocked = False
            if not blocked:
                kept.append(row)
        return kept

    def _hard_filter(self, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            score = row.get("score")
            try:
                if score is not None and float(score) < 0:
                    continue
            except (TypeError, ValueError):
                pass
            if row.get("suppress") or row.get("suppressed"):
                continue
            if row.get("protected"):
                continue
            text = row.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            kept.append(row)
        return kept

    def _candidate_rows(
        self, pool: Mapping[str, list[dict[str, Any]]], *,
        include_persona: bool = True,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        tiers = ("fact", "reflection", "fact_archive")
        if include_persona:
            tiers = (*tiers, "persona")
        for tier in tiers:
            for row in pool.get(tier, ()):
                row_id = str(row.get("id") or "")
                if row_id and row_id in seen:
                    continue
                if row_id:
                    seen.add(row_id)
                rows.append(row)
        return rows

    @staticmethod
    def _embedding_rows(
        pool: Mapping[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        return (
            list(pool.get("fact", ()))
            + list(pool.get("reflection", ()))
            + list(pool.get("persona", ()))
        )

    # -- internals: scoring ----------------------------------------------

    async def _query_vector(
        self,
        query: str,
        deadline: float,
        tokens: _Tokens,
    ) -> list[float] | None:
        provider = self._embedding
        if provider is None or not query:
            return None
        try:
            if not provider.is_available():
                return None
            model_id = getattr(provider, "model_id", None)
            if callable(model_id):
                model_id = model_id()
            if model_id is None:
                return None
        except Exception:  # noqa: BLE001 - provider failure degrades
            return None
        remaining = self._remaining(deadline) - self._settings.render_reserve_seconds
        if remaining <= 0:
            return None
        try:
            result = provider.embed_batch([query])
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=remaining)
            vectors = list(result or ())
            if not vectors or vectors[0] is None:
                return None
            return [float(item) for item in vectors[0]]
        except (asyncio.TimeoutError, TimeoutError):
            return None
        except Exception:  # noqa: BLE001
            return None

    def _passive_pick(
        self,
        query: str,
        bm25_scored: Sequence[tuple[dict[str, Any], float, dict[str, int]]],
        cosine_scored: Sequence[tuple[dict[str, Any], float]],
        stop_names: Sequence[str],
        purpose: Any,
    ) -> tuple[list[dict[str, Any]], str]:
        settings = self._settings
        query_terms = set(tokenize(query, stop_names))

        def key(row: Mapping[str, Any]) -> str:
            row_id = str(row.get("id") or "")
            return row_id or f"obj:{id(row)}"

        doc_terms = {key(row): set(tf) for row, _score, tf in bm25_scored}
        document_frequency: dict[str, int] = {}
        for terms in doc_terms.values():
            for term in terms:
                document_frequency[term] = document_frequency.get(term, 0) + 1
        rare_terms = {
            term for term, count in document_frequency.items()
            if count <= settings.rare_term_max_docs
            and self._is_rare_term(term)
            and term not in _COMMON_TERMS
        }
        chosen: dict[str, tuple[int, float, dict[str, Any]]] = {}

        def offer(row: dict[str, Any], strength: int, score: float) -> None:
            row_key = key(row)
            current = chosen.get(row_key)
            if current is None or (strength, score) > current[:2]:
                chosen[row_key] = (strength, score, row)

        cosine_by_doc = {key(row): score for row, score in cosine_scored}
        rare_docs = [
            (row, score, set(tf) & rare_terms)
            for row, score, tf in bm25_scored
            if set(tf) & rare_terms
        ]
        for row, score, shared in rare_docs:
            cosine = cosine_by_doc.get(key(row))
            if cosine is not None:
                if cosine >= settings.dual_threshold:
                    offer(row, 3, score)
            elif len(shared) >= settings.rare_terms_without_vector:
                offer(row, 3, score)

        bm25_top = [
            (row, score) for row, score, _tf in bm25_scored
            if score >= settings.bm25_threshold
        ][: settings.passive_bm25_top]
        mode = "hybrid" if cosine_scored else "bm25"
        if cosine_scored:
            top_ids = {key(row) for row, _score in bm25_top}
            for row, score in cosine_scored:
                if key(row) in top_ids and score >= settings.dual_threshold:
                    offer(row, 2, score)
            take = self._leading_take(
                [score for _row, score in cosine_scored],
                threshold=settings.semantic_threshold,
                lead=settings.lead_threshold,
                max_take=settings.passive_max_items,
            )
            for row, score in cosine_scored[:take]:
                offer(row, 1, score)
        else:
            if len(query_terms) >= settings.fallback_min_query_terms:
                for row, score in bm25_top[: settings.fallback_bm25_top]:
                    coverage = (
                        len(doc_terms.get(key(row), set())) / len(query_terms)
                        if query_terms else 0.0
                    )
                    if coverage >= settings.fallback_min_coverage:
                        offer(row, 2, score)
        if not chosen:
            return [], mode
        now = self._clock.now()
        apply_fading = _normalize_purpose(purpose) not in QUERY_PURPOSES
        ranked = sorted(
            chosen.values(),
            key=lambda item: (
                -item[0],
                -(
                    item[1] * (
                        fading_factor(item[2], now=now) if apply_fading else 1.0
                    )
                ),
            ),
        )
        return [row for _strength, _score, row in ranked[: settings.passive_max_items]], mode

    def _is_rare_term(self, term: str) -> bool:
        cjk = sum(1 for char in term if _is_cjk(char))
        if cjk:
            return cjk >= self._settings.rare_term_min_cjk
        return sum(1 for char in term if char.isalpha()) >= (
            self._settings.rare_term_min_latin
        )

    @staticmethod
    def _leading_take(
        scores: Sequence[float], *, threshold: float, lead: float, max_take: int,
    ) -> int:
        ordered = list(scores)
        take = min(max(int(max_take), 0), len(ordered))
        while take > 0 and ordered[take - 1] < threshold:
            take -= 1
        while take > 0:
            following = ordered[take] if take < len(ordered) else None
            if following is None or ordered[take - 1] - following >= lead:
                return take
            take -= 1
        return 0

    # -- internals: blocks -----------------------------------------------

    def _corrections_block(
        self,
        character: str,
        subject: Any,
        scene: Any,
        language: str,
    ) -> dict[str, Any]:
        if self._corrections is None:
            return dict(_EMPTY_CORRECTIONS)
        try:
            block = self._corrections.block(
                character,
                subject=subject,
                scene=scene,
                language=language,
                limit_tokens=self._settings.correction_tokens,
            )
            return _block_dict(block)
        except Exception:  # noqa: BLE001 - optional block
            return dict(_EMPTY_CORRECTIONS)

    def _growth_block(
        self, character: str, scene: Any, language: str, subject: Any = None,
    ) -> dict[str, Any]:
        if self._reflections is None or is_friend_subject(subject):
            return dict(_EMPTY_GROWTH)
        render = getattr(self._reflections, "growth_block", None)
        if not callable(render):
            return dict(_EMPTY_GROWTH)
        try:
            block = render(
                character,
                scene=scene,
                language=language,
                limit_tokens=self._settings.growth_tokens,
            )
            return _block_dict(block)
        except Exception:  # noqa: BLE001 - optional block
            return dict(_EMPTY_GROWTH)

    def _trim_block(
        self,
        block: Mapping[str, Any],
        context_tokens: int,
        max_context_tokens: int | None,
        tokens: _Tokens,
    ) -> dict[str, Any]:
        payload = {
            "block": str(block.get("block") or ""),
            "items": [dict(item) for item in block.get("items") or ()
                      if isinstance(item, Mapping)],
            "version": int(block.get("version") or 0),
        }
        if not max_context_tokens or not payload["block"]:
            return payload
        total = int(context_tokens or 0) + tokens.count(payload["block"])
        if total > int(max_context_tokens):
            payload["block"] = ""
        return payload

    # -- internals: items / results --------------------------------------

    @staticmethod
    def _render_item(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": str(row.get("id") or ""),
            "text": str(row.get("text") or ""),
            "kind": str(row.get("_tier") or row.get("kind") or ""),
            "entity": row.get("entity"),
            "created_at": row.get("created_at"),
            "event_start_at": row.get("event_start_at"),
            "event_end_at": row.get("event_end_at"),
            "source_fact_ids": row.get("source_fact_ids"),
        }

    def _items_from_rows(
        self, rows: Sequence[Mapping[str, Any]], mode: str,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row in rows:
            score = row.get("_rrf_score")
            if score is None:
                score = row.get("score")
            items.append({
                "kind": str(row.get("_tier") or row.get("kind") or ""),
                "id": str(row.get("id") or ""),
                "text": str(row.get("text") or ""),
                "score": None if score is None else round(float(score), 6),
                "source_keys": self._source_keys(row),
                "episode_id": episode_id_of(row),
                "applies_to": self._applies_to(row),
            })
        return items

    @staticmethod
    def _source_keys(row: Mapping[str, Any]) -> list[str]:
        keys: list[str] = []
        for field in ("source_event_keys", "source_keys", "source_key"):
            value = row.get(field)
            if isinstance(value, str):
                if value.strip():
                    keys.append(value.strip())
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        keys.append(item.strip())
        return list(dict.fromkeys(keys))

    @staticmethod
    def _applies_to(row: Mapping[str, Any]) -> dict[str, str]:
        value = row.get("applies_to")
        if not isinstance(value, Mapping):
            value = row.get("source_scene")
        return normalize_scene(value) if isinstance(value, Mapping) else {}

    def _result(
        self,
        status: str,
        start: float,
        *,
        items: Sequence[Mapping[str, Any]] = (),
        mode: str = "bm25",
        scene_filtered: int = 0,
        corrections_block: Mapping[str, Any] | None = None,
        growth_block: Mapping[str, Any] | None = None,
        episode: Mapping[str, Any] | None = None,
        query_meta: Mapping[str, Any] | None = None,
        candidates_total: int = 0,
        error_code: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "items": [dict(item) for item in items],
            "corrections_block": dict(corrections_block or _EMPTY_CORRECTIONS),
            "growth_block": dict(growth_block or _EMPTY_GROWTH),
            "episode": dict(episode) if episode else None,
            "query_meta": dict(query_meta or {}),
            "elapsed_ms": self._elapsed(start),
            "tokens": sum(self._tokens.count(item.get("text") or "") for item in items),
            "retrieval_mode": mode,
            "scene_filtered": int(scene_filtered),
            "candidates_total": int(candidates_total),
            "error_code": error_code,
            "reason": reason,
        }

    def _turn_result(
        self,
        result: str,
        start: float,
        *,
        context: str = "",
        memory_ids: Sequence[str] = (),
        tokens: int = 0,
        mode: str = "bm25",
        query_meta: Mapping[str, Any] | None = None,
        scene_filtered: int = 0,
        source_ids: Sequence[str] = (),
        corrections: Mapping[str, Any] | None = None,
        growth: Mapping[str, Any] | None = None,
        episode: Mapping[str, Any] | None = None,
        versions: Mapping[str, int] | None = None,
        request_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        version_map = dict(versions or {})
        return {
            "result": result,
            "context": context,
            "memory_ids": [str(item) for item in memory_ids],
            "tokens": int(tokens),
            "retrieval_mode": mode,
            "elapsed_ms": self._elapsed(start),
            "query_meta": dict(query_meta or {}),
            "scene_filtered": int(scene_filtered),
            "source_ids": [str(item) for item in source_ids],
            "corrections": dict(corrections or _EMPTY_CORRECTIONS),
            "growth": dict(growth or _EMPTY_GROWTH),
            "deletion_version": int(version_map.get("deletion_version", 0)),
            "episode": dict(episode) if episode else {},
            "companion_state": dict(_EMPTY_STATE),
            # M5 additive layers; build_turn_context overwrites these with the
            # real snapshot and projections, so every early-return path still
            # carries the documented empty shapes rather than omitting the keys.
            "companion_state_version": 0,
            "working_summary": None,
            "episode_summary": None,
            "threads_source": "none",
        }

    def _unavailable(self, start: float, reason: str) -> dict[str, Any]:
        return self._result("unavailable", start, reason=reason)

    def _error(self, start: float, exc: BaseException) -> dict[str, Any]:
        return self._result(
            "error", start,
            error_code=type(exc).__name__,
            reason=str(exc)[:200],
        )

    def _elapsed(self, start: float) -> int:
        return int(max(0.0, self._clock.monotonic() - start) * 1000)


__all__ = [
    "DEFAULT_RETENTION_CLASS",
    "FLOOR_BY_CLASS",
    "HALF_LIFE_DAYS",
    "QUERY_PURPOSES",
    "RETENTION_CLASSES",
    "RETENTION_DAILY",
    "RETENTION_DURABLE",
    "RETENTION_NORMAL",
    "RETENTION_SHARED_IMPORTANT",
    "RETENTION_TRANSIENT",
    "RecallService",
    "RecallSettings",
    "RenderedRecallBlock",
    "SUPPRESS_COOLDOWN_HOURS",
    "SUPPRESS_MENTION_LIMIT",
    "SUPPRESS_WINDOW_HOURS",
    "SURFACE_THRESHOLDS",
    "bm25_scores",
    "build_recall_query",
    "content_terms",
    "continuation_markers",
    "days_since",
    "entry_applies_to_scene",
    "entry_event_window",
    "episode_background",
    "episode_id_of",
    "evidence_score",
    "expand_source_background",
    "fading_factor",
    "fading_score",
    "filter_entries_for_scene",
    "has_anaphora",
    "link_followup_episode",
    "normalize_scene",
    "parse_time_range",
    "record_mention",
    "render_recall_block",
    "render_recall_entry_tag",
    "retention_class",
    "rrf_fuse",
    "should_surface",
    "time_since_label",
    "tokenize",
    "update_suppression",
]
