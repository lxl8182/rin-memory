"""M4 growth evidence: independent experiences and real evidence kinds.

Why this module exists (design sections 3.2, 7.4, 9.3):

* support was counted as `len(source_event_keys)`, i.e. the number of messages.
  One heart-to-heart split into five facts therefore looked like five independent
  experiences and cleared a "two independent experiences" promotion gate on the
  first conversation. Support is now counted per *independent experience*: the
  episode (`fact.episode_id`) when the fact has one, and the evidence key when
  it does not (see the degradation rules below);
* the evidence flags were hard-coded: every key was filed under
  `actual_result`, which made `PROMOTION_RULES` (habit/experience need
  `actual_result` *or* `user_feedback`) grant itself. Flags are now derived
  from the M1 `fact.evidence_kind`; a bare assertion carries no flag and the
  character's own words are not evidence about the user;
* deletion has to recompute with the *same* rule as creation, otherwise support
  is counted per episode while a row is built and per message when a source is
  deleted, and eligibility drifts twice. `apply_support_removal_to_row` is that
  one routine, used by the growth reconcile pass *and* by the deletion cleaner;
* automatic evolution may only propose card changes from a mature strategy or
  understanding. A raw fact stays an explanation source and is reported as
  `fact_not_eligible`.

Degradation rules (documented so they cannot drift silently):

* a fact with an `episode_id` contributes exactly one experience, whatever the
  number of facts, keys or statements in that episode. **The episode wins**: if
  M1 merged sources from two different mentions into one fact that still carries
  `episode_id`, those keys are *not* counted a second time. Rows are attributed
  to the episode they were extracted for; a genuinely independent later
  interaction arrives as its own fact with its own episode. (The reverse rule -
  counting merged keys as extra experiences - would let one conversation's
  re-mentions inflate support, which is exactly the bug this layer removes.)
  An `episode_ids` list on the row is accepted as well; each listed episode is
  one experience;
* a fact without any episode degrades to one experience per distinct evidence
  key: five facts of one message (which share the message key) still collapse to
  one support, while a fact merged from two independent mentions legitimately
  carries two;
* a fact with neither an episode nor keys contributes one experience keyed
  `fact:<id>`; it counts toward the experience total but can never claim a flag
  it did not derive from a real `evidence_kind`;
* `agent_expression` and `quoted_example` facts contribute nothing: her own
  words and borrowed material are not evidence about the user or the
  relationship (design section 7.4: the robot repeating itself adds no support).

Pure functions only: no IO, no clock, no model. The policy tables that must not
move (`PROMOTION_RULES`, `GROWTH_TERMINAL_STATUSES`, the promotion lifecycle)
stay in `domain/reflection.py` and are passed in, so the same gate can be used
by `domain/evolution.py` and `domain/evolution_scheduler.py` without a copy.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

#: Growth kinds; the *table* of gates stays in `domain/reflection.py`.
MEMORY_KINDS: tuple[str, ...] = ("habit", "interest", "experience", "relationship")
#: Host evidence flag vocabulary (memory/growth.py EVIDENCE_FLAGS).
EVIDENCE_FLAGS: tuple[str, ...] = (
    "actual_result", "user_feedback", "both_party", "real_contact",
)
#: Caps, matching MAX_SOURCE_KEYS / MAX_SOURCE_FACT_IDS on the host side.
MAX_SUPPORT_EXPERIENCES = 32
MAX_SUPPORT_KEYS = 32

#: `fact.evidence_kind` -> evidence flags. This is the whole point of the M1
#: field: what a fact *rests on* decides which gate it may satisfy.
#:
#: * `user_assertion` - the user said something. A statement is not an outcome,
#:   a feedback signal or a two-sided event, so it carries no flag; it still
#:   counts as one experience (a conversation happened);
#: * `explicit_feedback` - the user evaluated something: user_feedback;
#: * `observed_action` / `tool_result` - something actually happened (both
#:   sides or a tool ran) and there is a real outcome: actual_result, and real
#:   contact with reality rather than a claim;
#: * `agent_expression` / `quoted_example` - no flag (and not counted at all).
EVIDENCE_KIND_FLAGS: dict[str, tuple[str, ...]] = {
    "user_assertion": (),
    "explicit_feedback": ("user_feedback",),
    "observed_action": ("actual_result", "real_contact"),
    "tool_result": ("actual_result", "real_contact"),
    "agent_expression": (),
    "quoted_example": (),
}
#: Kinds that represent an interaction which actually took place.
EVIDENCE_KIND_COUNTS: frozenset[str] = frozenset({
    "user_assertion", "explicit_feedback", "observed_action", "tool_result",
})
#: Fallback kind per fact source, for facts written before M1 added the field.
#: Mirrors `domain.extraction.EVIDENCE_BY_SOURCE` (not imported: the reflection
#: import graph must stay independent of the extraction pipeline).
EVIDENCE_KIND_BY_SOURCE: dict[str, str] = {
    "user_observation": "user_assertion",
    "user_correction": "user_assertion",
    "ai_disclosure": "agent_expression",
    "shared_experience": "observed_action",
}
#: Extra flags implied by the fact source. Only a shared experience is
#: two-sided by construction (relationship gate: both_party).
EVIDENCE_FLAGS_BY_SOURCE: dict[str, tuple[str, ...]] = {
    "shared_experience": ("both_party",),
}
DEFAULT_EVIDENCE_KIND = "user_assertion"

#: Statuses that can never be an evolution candidate.
_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "withdrawn", "superseded", "deleted", "invalidated", "corrected",
    "expired", "archived", "merged", "denied",
})
#: Statuses that already passed the maturity gate.
_MATURE_STATUSES: frozenset[str] = frozenset({"confirmed", "promoted"})

#: Stable eligibility reasons (consumed by evolution receipts and tests).
ELIGIBLE = ""
REASON_FACT_NOT_ELIGIBLE = "fact_not_eligible"
REASON_NOT_MATURE = "not_mature"
REASON_SOURCE_DELETED = "source_deleted"


# -- facts ------------------------------------------------------------------


def _text(value: Any, limit: int = 600) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def evidence_kind_of(fact: Mapping[str, Any]) -> str:
    """Real evidence kind of one fact, deriving from its source when absent.

    Never returns an invented "actual_result": an unknown or missing value falls
    back to the source's *statement* kind, which opens no gate.
    """
    raw = fact.get("evidence_kind")
    if isinstance(raw, str) and raw.strip().lower() in EVIDENCE_KIND_FLAGS:
        return raw.strip().lower()
    source = _text(fact.get("source"), 40)
    return EVIDENCE_KIND_BY_SOURCE.get(source, DEFAULT_EVIDENCE_KIND)


def evidence_flags_of(fact: Mapping[str, Any]) -> tuple[str, ...]:
    """Evidence flags a fact may contribute, in EVIDENCE_FLAGS order."""
    kind = evidence_kind_of(fact)
    flags = list(EVIDENCE_KIND_FLAGS.get(kind, ()))
    source = _text(fact.get("source"), 40)
    for flag in EVIDENCE_FLAGS_BY_SOURCE.get(source, ()):
        bindings = fact.get("source_bindings") or ()
        if flag == "both_party" and bindings:
            roles = {row.get("role") for row in bindings}
            if not {"user", "assistant"} <= roles and kind not in ("observed_action", "tool_result"):
                continue
        if flag not in flags:
            flags.append(flag)
    return tuple(flag for flag in EVIDENCE_FLAGS if flag in flags)


def counts_as_experience(fact: Mapping[str, Any]) -> bool:
    """Whether one fact may add a support slot at all."""
    return evidence_kind_of(fact) in EVIDENCE_KIND_COUNTS


def _fact_id(fact: Mapping[str, Any]) -> str:
    return _text(fact.get("id"), 200)


def _dedupe(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def experiences_of(fact: Mapping[str, Any]) -> list[tuple[str, list[str]]]:
    """(experience id, evidence keys) pairs contributed by one fact.

    The documented degradation rule for facts without `episode_id` lives here so
    that creation and deletion can never disagree about what an experience is.
    """
    episodes: list[str] = []
    raw_episode = fact.get("episode_id")
    if isinstance(raw_episode, str) and raw_episode.strip():
        episodes.append(raw_episode.strip()[:200])
    for item in fact.get("episode_ids") or ():
        if isinstance(item, str) and item.strip() and item.strip()[:200] not in episodes:
            episodes.append(item.strip()[:200])
    keys = _dedupe([
        key.strip() for key in fact.get("source_event_keys") or ()
        if isinstance(key, str) and key.strip()
    ])
    if episodes:
        # Episode precedence: keys merged in by M1 belong to this episode.
        return [("episode:" + episode, keys) for episode in episodes]
    if keys:
        return [("src:" + key, [key]) for key in keys]
    fact_id = _fact_id(fact) or "unknown"
    return [("fact:" + fact_id, [])]


def _support_entry(fact: Mapping[str, Any], key: str) -> dict[str, Any]:
    bindings = [item for item in fact.get("source_bindings") or ()
                if isinstance(item, Mapping) and item.get("source_key") == key]
    if bindings:
        kinds = {item.get("evidence_kind") for item in bindings}
        kind = next((value for value in ("tool_result", "observed_action", "explicit_feedback", "user_assertion",
                                         "agent_expression", "quoted_example") if value in kinds), "user_assertion")
        fact = dict(fact, evidence_kind=kind)
    return {
        "key": key,
        "fact_id": _fact_id(fact),
        "kind": evidence_kind_of(fact),
        "flags": list(evidence_flags_of(fact)),
    }


def support_basis_from_facts(
    facts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build the ordered support basis (one entry per independent experience).

    ```python
    [{"experience": "ep:e1", "facts": ["fact_a"],
      "support": [{"key": "host:...", "fact_id": "fact_a",
                   "kind": "observed_action",
                   "flags": ["actual_result", "real_contact"]}]}]
    ```
    """
    basis: list[dict[str, Any]] = []
    by_experience: dict[str, dict[str, Any]] = {}
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        for experience, keys in experiences_of(fact):
            new_entries = [_support_entry(fact, key) for key in keys or ("",)]
            new_entries = [item for item in new_entries if item["kind"] in EVIDENCE_KIND_COUNTS]
            if not new_entries:
                continue
            entry = by_experience.get(experience)
            if entry is None:
                if len(basis) >= MAX_SUPPORT_EXPERIENCES:
                    continue
                entry = {"experience": experience, "facts": [], "support": []}
                by_experience[experience] = entry
                basis.append(entry)
            fact_id = _fact_id(fact)
            if fact_id and fact_id not in entry["facts"]:
                entry["facts"].append(fact_id)
            for item in new_entries:
                signature = (item["key"], item["fact_id"])
                if any(
                    (existing.get("key"), existing.get("fact_id")) == signature
                    for existing in entry["support"]
                ):
                    continue
                if len(entry["support"]) >= MAX_SUPPORT_KEYS:
                    continue
                entry["support"].append(item)
    return basis


def _normalize_basis_entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    experience = _text(value.get("experience"), 300)
    if not experience:
        return None
    support: list[dict[str, Any]] = []
    raw_support = value.get("support")
    if isinstance(raw_support, (list, tuple)):
        for item in raw_support:
            if not isinstance(item, Mapping):
                continue
            flags = [
                flag for flag in EVIDENCE_FLAGS
                if isinstance(item.get("flags"), (list, tuple))
                and flag in item.get("flags")
            ]
            support.append({
                "key": _text(item.get("key"), 600),
                "fact_id": _text(item.get("fact_id"), 200),
                "kind": _text(item.get("kind"), 40),
                "flags": flags,
            })
    elif isinstance(raw_support, Mapping):
        # Tolerate a hand-written single entry.
        support.append({
            "key": _text(raw_support.get("key"), 600),
            "fact_id": _text(raw_support.get("fact_id"), 200),
            "kind": _text(raw_support.get("kind"), 40),
            "flags": [flag for flag in EVIDENCE_FLAGS
                      if flag in (raw_support.get("flags") or ())],
        })
    facts = [
        _text(item, 200) for item in value.get("facts") or ()
        if isinstance(item, str) and item.strip()
    ]
    return {"experience": experience, "facts": facts, "support": support}


def support_basis_from_row(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Basis stored on the row, or a faithful reconstruction for legacy rows.

    **New rows always carry `support_basis`** (written by
    `support_basis_from_facts` / `merge_basis` / `invalidate_basis`), and that
    stored basis is authoritative: it is the only path that can say "these five
    facts are one experience".

    Rows written before M4 have no `support_basis` at all - every existing
    installation is full of them. They must not silently collapse to zero
    experiences, or an upgrade would turn every stored understanding into
    `not_mature` (and drop every adopted strategy) overnight. So the fallbacks
    below reconstruct from what the row already recorded, in this order:

    1. `support_basis` present -> used as is (new data, strict rule);
    2. `support_event_keys` / `source_event_keys` -> one experience per key,
       carrying exactly the flags the row already declared for that key. This is
       the pre-M4 counting rule used **only for reading old data**: nothing is
       invented, so a legacy row can neither regain an `actual_result` it never
       had nor lose a flag it really recorded;
    3. only `source_fact_ids` -> one experience per fact, with no flags (it
       counts as an experience but can satisfy no evidence gate);
    4. nothing -> no basis (genuinely empty evidence, e.g. a hand-written row).
    """
    raw = row.get("support_basis")
    if isinstance(raw, (list, tuple)) and raw:
        basis = [
            entry for entry in (_normalize_basis_entry(item) for item in raw)
            if entry is not None
        ]
        if basis:
            return basis
    keys: list[str] = []
    for field_name in ("support_event_keys", "source_event_keys"):
        value = row.get(field_name)
        if isinstance(value, (list, tuple)):
            for item in value:
                key = _text(item, 600)
                if key and key not in keys:
                    keys.append(key)
    if keys:
        evidence = row.get("support_evidence")
        buckets: dict[str, list[str]] = {}
        if isinstance(evidence, Mapping):
            for flag in EVIDENCE_FLAGS:
                bucket = evidence.get(flag)
                if isinstance(bucket, (list, tuple)):
                    buckets[flag] = [str(item) for item in bucket]
        basis = []
        for key in keys[:MAX_SUPPORT_EXPERIENCES]:
            flags = [flag for flag in EVIDENCE_FLAGS if key in buckets.get(flag, ())]
            basis.append({
                "experience": "src:" + key,
                "facts": [],
                "support": [{
                    "key": key, "fact_id": "", "kind": "", "flags": flags,
                }],
            })
        return basis
    # Last resort: a row that only remembers which facts it came from. Each
    # fact is one experience with no evidence kind recorded, so it counts as an
    # experience but answers no flag.
    fact_ids = [
        _text(item, 200) for item in row.get("source_fact_ids") or ()
        if isinstance(item, str) and item.strip()
    ]
    return [
        {
            "experience": "fact:" + fact_id,
            "facts": [fact_id],
            "support": [{
                "key": "", "fact_id": fact_id, "kind": "", "flags": [],
            }],
        }
        for fact_id in fact_ids[:MAX_SUPPORT_EXPERIENCES]
    ]


def merge_basis(
    existing: Sequence[Mapping[str, Any]],
    incoming: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Union two bases by experience id; facts/keys inside an experience merge.

    Re-synthesizing the same evidence is idempotent: an experience already on the
    row contributes nothing new, which is what keeps a retry from inflating
    support (design section 7.4).
    """
    out: list[dict[str, Any]] = []
    by_experience: dict[str, dict[str, Any]] = {}
    for source in (existing, incoming):
        for entry in source:
            if not isinstance(entry, Mapping):
                continue
            experience = _text(entry.get("experience"), 300)
            if not experience:
                continue
            target = by_experience.get(experience)
            if target is None:
                if len(out) >= MAX_SUPPORT_EXPERIENCES:
                    continue
                target = {"experience": experience, "facts": [], "support": []}
                by_experience[experience] = target
                out.append(target)
            for fact_id in entry.get("facts") or ():
                value = _text(fact_id, 200)
                if value and value not in target["facts"]:
                    target["facts"].append(value)
            for item in entry.get("support") or ():
                if not isinstance(item, Mapping):
                    continue
                key = _text(item.get("key"), 600)
                fact_id = _text(item.get("fact_id"), 200)
                if any(
                    (existing_item.get("key"), existing_item.get("fact_id"))
                    == (key, fact_id)
                    for existing_item in target["support"]
                ):
                    continue
                if len(target["support"]) >= MAX_SUPPORT_KEYS:
                    continue
                target["support"].append({
                    "key": key,
                    "fact_id": fact_id,
                    "kind": _text(item.get("kind"), 40),
                    "flags": [
                        flag for flag in EVIDENCE_FLAGS
                        if flag in (item.get("flags") or ())
                    ],
                })
    return out


def invalidate_basis(
    basis: Sequence[Mapping[str, Any]],
    *,
    keys: Sequence[str] = (),
    fact_ids: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Drop deleted keys/facts from the basis; an emptied experience disappears.

    The same recompute is applied to creation, growth reconcile and the deletion
    cleaner, so support can never be counted per episode in one path and per
    message in another.
    """
    removed_keys = {str(item) for item in keys if isinstance(item, str) and item}
    removed_facts = {str(item) for item in fact_ids if isinstance(item, str) and item}
    if not removed_keys and not removed_facts:
        return [dict(entry) for entry in basis]
    out: list[dict[str, Any]] = []
    for entry in basis:
        if not isinstance(entry, Mapping):
            continue
        kept_support: list[dict[str, Any]] = []
        for item in entry.get("support") or ():
            if not isinstance(item, Mapping):
                continue
            key = _text(item.get("key"), 600)
            fact_id = _text(item.get("fact_id"), 200)
            if key and key in removed_keys:
                continue
            if fact_id and fact_id in removed_facts:
                continue
            kept_support.append({
                "key": key,
                "fact_id": fact_id,
                "kind": _text(item.get("kind"), 40),
                "flags": [
                    flag for flag in EVIDENCE_FLAGS
                    if flag in (item.get("flags") or ())
                ],
            })
        if not kept_support:
            continue
        facts = [
            _text(item, 200) for item in entry.get("facts") or ()
            if isinstance(item, str) and item.strip()
            and item not in removed_facts
        ]
        out.append({
            "experience": _text(entry.get("experience"), 300),
            "facts": facts,
            "support": kept_support,
        })
    return out


def _as_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def summarize_basis(basis: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Row fields derived from a basis: count, keys, flag buckets, the basis."""
    experiences = [entry for entry in basis if isinstance(entry, Mapping)]
    keys: list[str] = []
    buckets: dict[str, list[str]] = {flag: [] for flag in EVIDENCE_FLAGS}
    for entry in experiences:
        experience = _text(entry.get("experience"), 300)
        for item in entry.get("support") or ():
            if not isinstance(item, Mapping):
                continue
            key = _text(item.get("key"), 600)
            reference = key or experience
            if key and key not in keys and len(keys) < MAX_SUPPORT_KEYS:
                keys.append(key)
            for flag in EVIDENCE_FLAGS:
                if flag in (item.get("flags") or ()) and reference:
                    if reference not in buckets[flag]:
                        buckets[flag].append(reference)
    return {
        "support_count": min(len(experiences), MAX_SUPPORT_EXPERIENCES),
        "support_event_keys": keys,
        "support_evidence": buckets,
        "support_basis": [dict(entry) for entry in experiences],
    }


def support_quality(basis: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Which flags the basis can still claim (independent of any row field)."""
    summary = summarize_basis(basis)
    evidence = summary["support_evidence"]
    return {
        "support_count": summary["support_count"],
        "flags": [flag for flag in EVIDENCE_FLAGS if evidence.get(flag)],
        "evidence": evidence,
    }


def support_after_removal(
    row: Mapping[str, Any],
    *,
    keys: Sequence[str] = (),
    fact_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """What support is left once these keys/facts are gone (no mutation)."""
    basis = invalidate_basis(
        support_basis_from_row(row),
        keys=sorted(str(item) for item in keys if item),
        fact_ids=sorted(str(item) for item in fact_ids if item),
    )
    return summarize_basis(basis)


def recompute_support_after_removal(
    row: dict[str, Any],
    invalidated_keys: Sequence[str] | set[str],
    *,
    invalidated_fact_ids: Sequence[str] = (),
    now_iso: str = "",
    drop_fact_ids: bool = True,
) -> bool:
    """Rewrite count/keys/flag buckets/basis from the surviving support.

    Only the support fields move: this is the shared recompute used by the
    deletion cleaner (which owns `needs_rebuild` and row removal) and by
    :func:`apply_support_removal_to_row` (which owns withdrawn/narrowed). Both
    therefore read the same numbers; neither can fall back to "one support per
    message".

    `drop_fact_ids=False` keeps `source_fact_ids` intact. A *correction* needs
    that: the id is durable provenance, and the persona invalidator that runs
    right after the reflection one maps persona entries back to their
    reflection through exactly those ids. Only the support that can still
    justify the row is recomputed there. Deletions do narrow
    `source_fact_ids`, because the fact itself is gone.
    """
    keys = {str(item) for item in invalidated_keys if isinstance(item, str) and item}
    fact_ids = {
        str(item) for item in invalidated_fact_ids
        if isinstance(item, str) and item
    }
    if not keys and not fact_ids:
        return False
    current = support_basis_from_row(row)
    basis = invalidate_basis(current, keys=sorted(keys), fact_ids=sorted(fact_ids))
    if basis == current:
        return False
    row.update(summarize_basis(basis))
    if drop_fact_ids:
        row_fact_ids = [
            str(item) for item in row.get("source_fact_ids") or ()
            if isinstance(item, str) and item
        ]
        if row_fact_ids:
            row["source_fact_ids"] = [
                item for item in row_fact_ids if item not in fact_ids
            ]
    row["source_event_keys"] = [
        str(item) for item in row.get("source_event_keys") or ()
        if isinstance(item, str) and item not in keys
    ]
    if now_iso:
        row["updated_at"] = now_iso
    return True


def apply_support_removal_to_row(
    row: dict[str, Any],
    invalidated_keys: Sequence[str] | set[str],
    *,
    invalidated_fact_ids: Sequence[str] = (),
    now_iso: str = "",
    min_support: int = 2,
) -> bool:
    """Recompute a row after sources disappeared; return whether it changed.

    Keeps the deletion-barrier semantics unchanged (no support -> `withdrawn`;
    below the growth gate -> `narrowed`) while the count, the evidence flags and
    the maturity all come from the *same* basis routine that created the row.
    """
    if not recompute_support_after_removal(
        row,
        invalidated_keys,
        invalidated_fact_ids=invalidated_fact_ids,
        now_iso=now_iso,
    ):
        return False
    support = _as_count(row.get("support_count"))
    status = str(row.get("status") or "pending")
    if support <= 0:
        row["status"] = "withdrawn"
        if now_iso:
            row["withdrawn_at"] = now_iso
        row["withdrawn_reason"] = "sources_deleted"
    elif support < int(min_support) and status in ("confirmed", "promoted", "narrowed"):
        row["status"] = "narrowed"
        if now_iso:
            row["narrowed_at"] = now_iso
        row["narrowed_reason"] = "sources_reduced"
    return True


# -- maturity and eligibility ----------------------------------------------


def maturity_of_row(
    row: Mapping[str, Any],
    *,
    promotion_rules: Mapping[str, Sequence[str]],
    min_support: int = 2,
) -> dict[str, Any]:
    """Maturity computed from the support basis, never from a stale count."""
    kind = _text(row.get("memory_kind"), 40)
    required = tuple(promotion_rules.get(kind) or ())
    basis = support_basis_from_row(row)
    quality = support_quality(basis)
    evidence = quality["evidence"]
    has_required = any(bool(evidence.get(flag)) for flag in required) if required else False
    experiences = int(quality["support_count"])
    basis_present = bool(basis)
    enough_experiences = experiences >= int(min_support)
    has_interaction_evidence = bool(quality["flags"])
    return {
        "memory_kind": kind,
        "independent_experiences": experiences,
        "support_count": experiences,
        "basis_present": basis_present,
        "enough_experiences": enough_experiences,
        "flags": list(quality["flags"]),
        "required_flags": list(required),
        "has_required_flag": bool(has_required),
        "has_interaction_evidence": has_interaction_evidence,
        "min_support": int(min_support),
        "promotion_ready": bool(required) and enough_experiences
        and bool(has_required),
        # A plain understanding needs independent experiences and at least one
        # real feedback/action signal. User feedback qualifies without a tool
        # result; repeated assertions alone remain recallable understanding,
        # rather than permission to revise the character's persona.
        "reflection_ready": basis_present and enough_experiences
        and has_interaction_evidence,
    }


def eligibility_of_row(
    row: Mapping[str, Any],
    *,
    promotion_rules: Mapping[str, Sequence[str]],
    min_support: int = 2,
) -> tuple[bool, str]:
    """Whether one evidence row may drive an automatic card change.

    A raw fact never may: it stays an explanation source (`fact_not_eligible`).
    A behavior strategy needs a mature status and its kind-specific basis;
    a plain understanding needs independent experiences and real interaction
    evidence, even if its reinforcement score already confirmed it.
    """
    if not isinstance(row, Mapping):
        return False, REASON_NOT_MATURE
    kind = _text(row.get("kind") or row.get("evidence_type"), 40).lower()
    if kind == "fact":
        return False, REASON_FACT_NOT_ELIGIBLE
    status = _text(row.get("status"), 40).lower()
    if (
        status in _TERMINAL_STATUSES
        or row.get("deleted")
        or row.get("invalidated_at")
        or row.get("corrected")
        or row.get("needs_rebuild")
    ):
        return False, REASON_SOURCE_DELETED
    if status not in _MATURE_STATUSES:
        return False, REASON_NOT_MATURE
    maturity = maturity_of_row(
        row, promotion_rules=promotion_rules, min_support=min_support,
    )
    memory_kind = _text(row.get("memory_kind"), 40)
    if memory_kind in MEMORY_KINDS:
        return (
            (True, ELIGIBLE) if maturity["promotion_ready"]
            else (False, REASON_NOT_MATURE)
        )
    # Plain understandings use the shared interaction-evidence gate instead of
    # a growth-kind-specific flag table. Confirmation alone is insufficient.
    return (
        (True, ELIGIBLE) if maturity["reflection_ready"]
        else (False, REASON_NOT_MATURE)
    )


def evidence_dto(
    row: Mapping[str, Any],
    *,
    kind: str,
    promotion_rules: Mapping[str, Sequence[str]],
    min_support: int = 2,
) -> dict[str, Any]:
    """Unified evidence DTO for reflection -> evidence -> evolution.

    The three consumers used to read different fields, which is how the
    evolution prompt ended up with empty sources and no scope. Every row now
    carries the same shape: identity, origin (fact ids / event keys /
    experiences), scope, maturity and eligibility.
    """
    eligible, reason = eligibility_of_row(
        row, promotion_rules=promotion_rules, min_support=min_support,
    )
    maturity = maturity_of_row(
        row, promotion_rules=promotion_rules, min_support=min_support,
    )
    basis = support_basis_from_row(row)
    fact_ids = [
        _text(item, 200) for item in row.get("source_fact_ids") or ()
        if isinstance(item, str) and item.strip()
    ]
    for entry in basis:
        for fact_id in entry.get("facts") or ():
            if fact_id not in fact_ids:
                fact_ids.append(str(fact_id))
    event_keys: list[str] = []
    for field_name in ("support_event_keys", "source_event_keys"):
        for item in row.get(field_name) or ():
            if isinstance(item, str) and item.strip() and item not in event_keys:
                event_keys.append(item)
    for entry in basis:
        for item in entry.get("support") or ():
            key = item.get("key") if isinstance(item, Mapping) else ""
            if isinstance(key, str) and key and key not in event_keys:
                event_keys.append(key)
    applies_to = row.get("applies_to")
    scene = dict(applies_to) if isinstance(applies_to, Mapping) else {}
    text = ""
    for key in ("text", "claim", "summary", "statement"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break
    return {
        "id": _text(row.get("id"), 200),
        "kind": kind,
        "source": _text(row.get("source"), 60) or kind,
        "text": text,
        "status": _text(row.get("status"), 40),
        "memory_kind": _text(row.get("memory_kind"), 40),
        "source_fact_ids": fact_ids,
        "source_keys": event_keys,
        "experiences": [
            _text(entry.get("experience"), 300) for entry in basis
        ],
        "applies_to": scene,
        "scope_key": _text(row.get("scope_key"), 80),
        "topic_key": _text(row.get("topic_key"), 80),
        "maturity": maturity,
        "eligible": bool(eligible),
        "eligibility": reason,
    }


__all__ = [
    "DEFAULT_EVIDENCE_KIND",
    "ELIGIBLE",
    "EVIDENCE_FLAGS",
    "EVIDENCE_FLAGS_BY_SOURCE",
    "EVIDENCE_KIND_BY_SOURCE",
    "EVIDENCE_KIND_COUNTS",
    "EVIDENCE_KIND_FLAGS",
    "MAX_SUPPORT_EXPERIENCES",
    "MAX_SUPPORT_KEYS",
    "MEMORY_KINDS",
    "REASON_FACT_NOT_ELIGIBLE",
    "REASON_NOT_MATURE",
    "REASON_SOURCE_DELETED",
    "apply_support_removal_to_row",
    "counts_as_experience",
    "eligibility_of_row",
    "evidence_dto",
    "evidence_flags_of",
    "evidence_kind_of",
    "experiences_of",
    "invalidate_basis",
    "merge_basis",
    "maturity_of_row",
    "recompute_support_after_removal",
    "summarize_basis",
    "support_after_removal",
    "support_basis_from_facts",
    "support_basis_from_row",
    "support_quality",
]
