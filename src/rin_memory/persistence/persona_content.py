"""Filter learned paragraph ownership through the existing deletion fence."""
from __future__ import annotations

import copy


def filter_owned_content(section, blocked):
    result = copy.deepcopy(dict(section))
    basis = result.get("content_basis")
    if not isinstance(basis, dict):
        return result
    rules = basis.get("rules") or []
    growth = basis.get("growth") or []
    removed = {row["rule_id"] for row in rules if blocked(row.get("source_key", ""))}
    removed.update("growth:" + row["id"] for row in growth
                   if blocked(row["id"]) or any(blocked(key) for key in
                       [*(row.get("source_keys") or ()), *(row.get("source_fact_ids") or ())]))
    if not removed:
        return result
    basis["rules"] = [row for row in rules if row["rule_id"] not in removed]
    basis["growth"] = [row for row in growth if "growth:" + row["id"] not in removed]
    units = {"user": basis.get("base_text", ""), **{row["rule_id"]: row["text"] for row in basis["rules"]},
             **{"growth:" + row["id"]: row["text"] for row in basis["growth"]}}
    paragraphs = []
    for paragraph in basis.get("paragraphs") or []:
        refs = [ref for ref in paragraph["refs"] if ref not in removed]
        if not refs:
            continue
        text = paragraph["text"]
        if set(paragraph["refs"]) & removed:
            text = "\n".join(dict.fromkeys(units[ref] for ref in refs if units.get(ref)))
        if text:
            paragraphs.append({"text": text, "refs": refs})
    basis["paragraphs"] = paragraphs
    basis["text"] = "\n".join(dict.fromkeys(row["text"] for row in paragraphs))
    result["text"] = basis["text"]
    removed_growth = {ref.removeprefix("growth:") for ref in removed if ref.startswith("growth:")}
    if "source_refs" in result:
        result["source_refs"] = [ref for ref in result["source_refs"] if not blocked(ref) and ref not in removed_growth]
    return result


def filter_owned_tree(value, blocked):
    if isinstance(value, list):
        return [filter_owned_tree(row, blocked) for row in value]
    if isinstance(value, dict):
        result = filter_owned_content(value, blocked) if "content_basis" in value else value
        return {key: filter_owned_tree(row, blocked) for key, row in result.items()}
    return value
