"""Whole-section teaching edits with paragraph-level source ownership."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping, Sequence

from .persona_card import MAX_SECTION_TEXT
from .prompt_voice import FIRST_PERSON_VOICE


class CompositionError(ValueError):
    pass


def source_plan(section: Any, records: Sequence[Mapping[str, Any]], rules: Sequence[Mapping[str, Any]],
                *, revision: int, exclude: str = "", growth: Sequence[Mapping[str, Any]] = ()) -> dict:
    """Upgrade old line ownership and retain the latest user-authored basis."""
    current = str(getattr(section, "text", "") or "")
    previous = max((row.get("composition") for row in records if isinstance(row.get("composition"), Mapping)),
                   key=lambda row: (int(row.get("applied_revision") or 0), int(row.get("sequence") or 0)), default={})
    active = []
    old_lines = set()
    for record in records:
        for rule in record.get("rules") or ():
            old_lines.add(str(rule.get("line") or rule.get("text") or ""))
            if record.get("status") == "active" and record.get("teaching_id") != exclude and not rule.get("superseded_by"):
                active.append(dict(rule, source_key=record.get("source_key")))
    base = previous.get("base_text") if previous else None
    changed = bool(previous) and current != previous.get("text")
    if base is None or changed and getattr(section, "origin", "") != "evolution":
        # Exact lines establish the old-data mapping. For a semantic user edit,
        # the model receives both versions and returns an updated authored basis.
        base = "\n".join(line for line in current.splitlines() if line.strip() not in old_lines)
    return {"section_id": "tone", "base_revision": revision, "base_text": base,
            "user_revision": revision if changed or not previous else previous.get("user_revision", revision),
            "current_text": current, "previous": dict(previous), "user_edited": changed and getattr(section, "origin", "") == "user_edit",
            "rules": active + [dict(rule) for rule in rules], "growth": list(growth),
            "new_rule_ids": [rule["rule_id"] for rule in rules]}


def deterministic_plan(plan: Mapping[str, Any]) -> dict:
    """Simple known rules converge without needing a model or new dependency."""
    base = str(plan["base_text"])
    paragraphs = [{"text": base, "refs": ["user"]}] if base else []
    for rule in plan["rules"]:
        text = str(rule["text"])
        existing = next((row for row in paragraphs if row["text"] == text), None)
        if existing is None:
            paragraphs.append({"text": text, "refs": [rule["rule_id"]]})
        else:
            existing["refs"].append(rule["rule_id"])
    return {"base_text": base, "paragraphs": paragraphs, "superseded_rule_ids": []}


async def compose(plan: Mapping[str, Any], model: Any, *, tier="summary", timeout_s=60.0, max_output_tokens=2400) -> dict:
    rules = {str(row["rule_id"]): row for row in plan["rules"]}
    growth = {"growth:" + str(row["id"]): row for row in plan["growth"]}
    old = plan.get("previous") or {}
    # A repeated, identical requirement can reuse the complete authored result.
    old_texts = {str(row.get("text")) for row in old.get("rules") or ()}
    if old and not plan["user_edited"] and plan["current_text"] == old.get("text") and rules and all(
            str(row["text"]) in old_texts for row in rules.values()):
        result = {"base_text": old["base_text"], "paragraphs": [dict(row, refs=list(row["refs"])) for row in old["paragraphs"]],
                  "superseded_rule_ids": []}
        old_rules = {str(row["rule_id"]): row for row in old.get("rules") or ()}
        # This path only covers additions; withdrawal must rebuild the affected wording.
        if set(old_rules) <= set(rules):
            for key, rule in rules.items():
                if key in old_rules:
                    continue
                alias = next(ref for ref, value in old_rules.items() if value["text"] == rule["text"])
                for paragraph in result["paragraphs"]:
                    if alias in paragraph["refs"]:
                        paragraph["refs"].append(key)
        else:
            result = None
    else:
        result = None
    if result is None and model is None:
        if plan["user_edited"] and old and all(line not in plan["current_text"] for line in old_texts):
            raise CompositionError("composition_model_required")
        result = deterministic_plan(plan)
    elif result is None:
        system = (FIRST_PERSON_VOICE + "我把当前表达、有效教导和已经采用的成长整合为完整的新段落。"
                  "规则中的 instruction 保留实际教导原话，我据此理解要求的方向、条件和例外。"
                  "我保留用户手写内容与仍有效的认识，把同义要求合并表达；新要求改变旧要求时，以新要求更新相应部分。"
                  "我以第一人称重写自然、连贯的正文，让重复教导收敛。"
                  "我按有效来源重新整理，正文中的每个自然段使用 refs 说明所采用的 user、rule_id 或 growth:id。"
                  "user_edited 为 true 时，我从当前与上版的差异中保留用户的新编辑，base_text 保存最新手写基础。"
                  "user_edited 为 false 时，base_text 逐字沿用输入中的手写基础；自然改写体现在 paragraphs 的正文中。"
                  "撤回后的规则已从 rules 中移除，我依据剩余来源写出当前理解。"
                  "输出 JSON：{\"base_text\":\"手写基础\",\"paragraphs\":[{\"text\":\"新正文\",\"refs\":[\"user或规则id或growth:id\"]}],"
                  "\"superseded_rule_ids\":[\"被新教导替代的旧规则id\"]}。"
                  f"总正文在 {MAX_SECTION_TEXT} 字以内，常规更新优先保持当前篇幅。")
        try:
            raw = await asyncio.wait_for(model.invoke(
                tier, [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(plan, ensure_ascii=False)}],
                timeout_s=timeout_s, max_output_tokens=max_output_tokens, purpose="persona_teaching_composition"), timeout_s)
        except Exception as exc:
            raise CompositionError(f"composition_model:{type(exc).__name__}") from exc
        try:
            result = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
        except (ValueError, TypeError, AttributeError) as exc:
            raise CompositionError("composition_format") from exc
    if not isinstance(result, Mapping) or not isinstance(result.get("base_text"), str):
        raise CompositionError("composition_format")
    if not plan["user_edited"] and result["base_text"].strip() != str(plan["base_text"]).strip():
        raise CompositionError("composition_user_basis")
    paragraphs = result.get("paragraphs")
    superseded = result.get("superseded_rule_ids", [])
    if not isinstance(paragraphs, list) or not isinstance(superseded, list):
        raise CompositionError("composition_format")
    known_rules = set(rules) | {row["rule_id"] for row in old.get("rules") or ()}
    if any(not isinstance(ref, str) or ref not in known_rules or ref in plan["new_rule_ids"] for ref in superseded):
        raise CompositionError("composition_sources")
    superseded = [ref for ref in superseded if ref in rules]
    allowed = {"user", *rules, *growth} - set(superseded)
    adopted = set()
    clean = []
    for row in paragraphs:
        if not isinstance(row, Mapping) or not isinstance(row.get("text"), str) or not isinstance(row.get("refs"), list):
            raise CompositionError("composition_format")
        refs = row["refs"]
        if not refs or any(not isinstance(ref, str) or ref not in allowed for ref in refs):
            raise CompositionError("composition_sources")
        text = row["text"].strip()
        if text:
            clean.append({"text": text, "refs": list(dict.fromkeys(refs))})
            adopted.update(refs)
    if (set(rules) - set(superseded)) - adopted or (result["base_text"].strip() and "user" not in adopted) or set(growth) - adopted:
        raise CompositionError("composition_sources")
    text = "\n".join(row["text"] for row in clean)
    if len(text) > MAX_SECTION_TEXT or len(result["base_text"]) > MAX_SECTION_TEXT:
        raise CompositionError("tone_section_full")
    return {"section_id": "tone", "base_text": result["base_text"].strip(), "user_revision": plan["user_revision"],
            "sequence": int(old.get("sequence") or 0) + 1,
            "paragraphs": clean, "text": text, "rules": [row for key, row in rules.items() if key not in superseded],
            "growth": list(growth.values()), "superseded_rule_ids": superseded}
