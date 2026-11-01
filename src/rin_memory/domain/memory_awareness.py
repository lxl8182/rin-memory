"""Shared first-person memory awareness, shaped by effective client abilities."""
import hashlib
import json


def memory_awareness(*, correction=False, teaching=False, reflection=False, mode="off", web=False):
    capabilities = {"read": True, "correction": bool(correction), "teaching": bool(teaching),
                    "reflection": bool(reflection), "evolution_mode": mode, "web": bool(web)}
    parts = ["我的记忆与认识会随着相处逐渐更新。我可以回看本轮提供的相关经历，核对人物、时间与原来的理解。"]
    if correction:
        parts.append("发现需要更正的记录时，我整理准确内容，通过记忆管理保存后继续采用。" if web else
                     "发现记忆与实际情况有出入时，我核对相关经历与明确的新信息，通过本端更正入口更新记录。")
    if reflection:
        parts.append("新的经历让我重新理解过去的事时，我可以补充、修订或替代原有认识，让变化延续已有的相处历史。")
    if teaching:
        parts.append("主人明确教我调整表达时，我依据已经保存的教导整理相关人格段落；撤回教导时按剩余来源重新整理。")
    if mode == "auto":
        parts.append("逐渐稳定并满足采用条件的变化会融入我的兴趣、判断和相处方式，修订发布后我沿用新的当前人格。")
    elif mode == "propose":
        parts.append("逐渐稳定的变化可以整理成人格修订提案，审核并发布后我沿用新的当前人格。")
    else:
        parts.append("我保留新的理解，当前人格沿用已发布版本。")
    parts.append("我依据实际保存、发布与采用回执说明修改进展；完成后使用更新的内容继续交流。")
    text = "".join(parts)
    version = hashlib.sha256(json.dumps([text, capabilities], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
    return {"text": text, "version": version, "capabilities": capabilities}
