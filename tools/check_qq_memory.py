"""Replay the QQ question sheet through RIN context and the DSH renderer."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rin_memory.adapters.composition import build_memory_core
from rin_memory.contracts import DeletionRequest, DeletionTarget, Scope
from rin_memory.domain.recall import RecallSettings
from rin_memory.service.memory_service import MemoryService, ServiceSettings
from rin_memory.service.server import MemoryApi


SUBJECTS = {"A": "qq-friend:900001", "B": "qq-friend:900002"}
NOW = datetime(2026, 11, 11, 4, tzinfo=timezone.utc)


class FixedClock:
    def now(self):
        return NOW

    def monotonic(self):
        return time.monotonic()


def table(document, section):
    content = document.split(f"\n## {section}\n", 1)[1].split("\n## ", 1)[0]
    return [[cell.strip() for cell in line.split("|")[1:-1]]
            for line in content.splitlines() if line.startswith("|")][2:]


def render(dsh_root, contexts):
    entry = dsh_root.resolve() / "index.js"
    if not entry.is_file():
        raise SystemExit(f"找不到 DSH RIN 渲染入口 {entry}，请传 --dsh-root。")
    code = f"""
import {{ renderIncrement }} from {json.dumps(entry.as_uri())};
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const cases = JSON.parse(Buffer.concat(chunks).toString('utf8'));
console.log(JSON.stringify(cases.map(({{ context, question }}) =>
  renderIncrement(context, {{}}, {{ friend: true, currentInput: question, memoryTokenBudget: 600 }}).references.ids)));
"""
    result = subprocess.run(["node", "--input-type=module", "-e", code], check=True,
                            input=json.dumps(contexts, ensure_ascii=False), encoding="utf-8", stdout=subprocess.PIPE)
    return json.loads(result.stdout)


def score(case, injected):
    needed = case["expected"]
    missing = needed.copy()
    extra = []
    for entry in injected:
        group = next((options for options in missing if entry in options), None)
        if group is None:
            extra.append(entry)
        else:
            missing.remove(group)
    return {**case, "injected": injected, "extra": extra, "missing": missing}


async def replay(args):
    document = args.sheet.read_text(encoding="utf-8")
    questions = table(document, "召回题")
    question_ids = {row[0] for row in questions}
    unknown = (set(args.case or ()) | set(args.allow_missing or ())) - question_ids
    if unknown:
        raise SystemExit(f"找不到题号 {', '.join(sorted(unknown))}。")
    if len(question_ids) != len(questions):
        raise ValueError("题单含有重复题号")
    facts = []
    for key, who, day, text, *metadata in table(document, "测试数据"):
        timestamp = day + "T03:00:00+00:00"
        subject = SUBJECTS[who]
        fact = {"id": key, "text": text, "subject": subject, "source": "user_observation",
                "importance": 6, "schema_version": 2, "created_at": timestamp, "event_start_at": timestamp,
                "source_event_keys": [f"plugin:qq:check:{subject.replace(':', '_')}:seed:{key}"],
                "episode_id": f"ep:qq-check:{key}", "retention_class": "normal"}
        if metadata and metadata[0]:
            fact["search_aliases"] = [name.strip() for name in metadata[0].split("/")]
        facts.append(fact)
    fact_subjects = {fact["id"]: fact["subject"] for fact in facts}
    if len(fact_subjects) != len(facts):
        raise ValueError("题单含有重复事实编号")
    with tempfile.TemporaryDirectory(prefix="rin-qq-check-") as directory:
        root = Path(directory)
        settings = ServiceSettings(memory_root=root / "memory", local_state_root=root / "state",
                                   character="check", context_budget_ms=1000)
        core = build_memory_core(memory_root=settings.memory_root, local_state_root=settings.local_state_root,
                                 clock=FixedClock(), recall_settings=RecallSettings(fast_chat=True))
        service = MemoryService(settings, core=core, model=None, lock=None)
        try:
            core.recover("check")
            service.state.storage_ready = True
            core.facts.save("check", facts)
            api = MemoryApi(service)
            cases, contexts = [], []
            deleted = set()
            for key, who, question, expected, _, delete, *labels in questions:
                if args.case and key not in args.case:
                    continue
                category, prior = (labels + ["", ""])[:2]
                needed = [] if expected == "空" else [group.split("/") for group in expected.split(",")]
                if any(entry not in fact_subjects for group in needed for entry in group):
                    raise ValueError(f"{key} 引用了不存在的预期事实")
                subject = SUBJECTS[who]
                if delete and delete not in deleted:
                    if delete not in fact_subjects:
                        raise ValueError(f"{key} 引用了不存在的待删事实 {delete}")
                    result = await core.deletion_service.forget(DeletionRequest(
                        scope=Scope("check", fact_subjects[delete]), mode="entry",
                        targets=(DeletionTarget(type="fact", id=delete),),
                        reason="QQ fixture delete test"))
                    if result.status != "completed":
                        raise RuntimeError(f"{key} 的删除未完成 {result.status}")
                    deleted.add(delete)
                payload = {"subject": subject, "session_id": key, "scene": "plugin:qq",
                           "client_id": "check", "text": question}
                if prior:
                    payload["recent_turns"] = [{"role": "user", "text": prior}]
                context = await api.context(payload)
                selection = context["context"]["selection"]
                cases.append({"id": key, "category": category or "快速题", "question": question,
                              "expected": needed, "status": selection["status"],
                              "selected": [item["id"] for item in selection["items"]]})
                contexts.append({"context": context, "question": question})
            if not cases:
                raise SystemExit("没有找到匹配的题号。")
            return [score(case, ids) for case, ids in zip(cases, render(args.dsh_root, contexts), strict=True)]
        finally:
            await service.stop()


def summarize(cases):
    positive = [row for row in cases if row["expected"]]
    empty = [row for row in cases if not row["expected"]]
    return {"questions": len(cases), "injected": sum(len(row["injected"]) for row in cases),
            "extra": sum(len(row["extra"]) for row in cases),
            "questions_with_extra": sum(bool(row["extra"]) for row in cases),
            "empty_questions": len(empty), "empty_with_injection": sum(bool(row["injected"]) for row in empty),
            "positive_questions": len(positive), "positive_complete": sum(not row["missing"] for row in positive)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet", type=Path, default=ROOT / "docs" / "QQ记忆快速测试.md")
    parser.add_argument("--dsh-root", type=Path, default=ROOT.parent / "dsh-rin-memory")
    parser.add_argument("--case", action="append", help="只运行指定题号，可重复传入")
    parser.add_argument("--json", action="store_true", help="输出逐题和汇总 JSON")
    parser.add_argument("--check", action="store_true", help="有误召回、意外漏召回或异常状态时退出 1")
    parser.add_argument("--allow-missing", action="append", help="检查时允许指定题号漏召回，可重复传入")
    args = parser.parse_args()
    cases = asyncio.run(replay(args))
    summary = summarize(cases)
    groups = {}
    for row in cases:
        groups.setdefault(row["category"], []).append(row)
    by_category = {name: summarize(rows) for name, rows in groups.items()}
    if args.json:
        print(json.dumps({"summary": summary, "by_category": by_category, "cases": cases}, ensure_ascii=False, indent=2))
    else:
        for row in cases:
            print(f"{row['id']}  带入 {row['injected']}  无用 {row['extra']}  漏掉 {row['missing']}")
        for name, stats in ([*by_category.items(), ("总计", summary)] if len(groups) > 1 else [("总计", summary)]):
            print(f"{name}  无用事实 {stats['extra']}/{stats['injected']}，"
                  f"应空误注入 {stats['empty_with_injection']}/{stats['empty_questions']}，"
                  f"正例完整命中 {stats['positive_complete']}/{stats['positive_questions']}")
    allowed = set(args.allow_missing or ())
    failed = any(row["extra"] or (row["missing"] and row["id"] not in allowed)
                 or row["status"] not in {"selected", "empty"} for row in cases)
    return int(args.check and failed)


if __name__ == "__main__":
    raise SystemExit(main())
