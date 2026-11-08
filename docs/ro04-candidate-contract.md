# QQ 主动交流与投递约定

> 历史摘要 · 2026-10-29，RO04-B / T7–T8。当前状态见 [维护台账](roadmap.md)。[查看完整原稿](https://github.com/lxl8182/rin-memory/blob/0ee569e21d788076389211e4fada827c8e4631c7/docs/ro04-candidate-contract.md).

## 职责

DSH 持有约定、未完事项、任务结果与素材候选，按会话持久保存联系时机和来源。QQ bridge 负责已校验绑定与唯一平台投递。RIN 提供上下文、输出记录、版本依据与分段回执。原核心主动接口继续供其他接入方使用。

## 绑定与投递顺序

1. 插件通过 `qqBridgeEvents` 读写合法绑定。启动用 `restoreBindings` 按当前身份配置复核历史记录，再用 `ensureSessions` 建立实际会话。已验证的真实入站绑定优先。
2. 插件调用 `planOutput` 取得规范正文与 `part_sizes`。`POST /v1/outputs` 保存正文，核心固定 `output_id`、总段数和每段索引。
3. `deliverByIndex` 使用合法 `sessionKey`、稳定 `outputId` 和待投递 `parts[{index,text}]`。bridge 按原索引投递并返回每段 `ok/platformId` 和未完成索引。
4. 插件向 `POST /v1/outputs/deliver` 报告平台实际接受的分段。重启用同一输出 ID 处理剩余部分，服务保留可追溯的接受状态。

当前字段以 DSH 的 `proactive.js`、QQ bridge 的 `bridge-events.ts` 及核心 `service/management.py` 为准。旧 T7/T8 切换顺序、恢复拒绝码与验证记录见原稿。
