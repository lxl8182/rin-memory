# QQ 与 DSH 接入决策

> 历史摘要 · 2026-10-25，Q / DM / QP。当前状态见 [维护台账](roadmap.md)。[查看完整原稿](https://github.com/lxl8182/rin-memory/blob/0ee569e21d788076389211e4fada827c8e4631c7/docs/server-qq-dsh-plan.md).

- 接入方案从早期 AstrBot 转为 DSH 与现有 QQ bridge 共存。QQ bridge 校验平台身份和消息收发，DSH 生成与执行，RIN 提供权威人格及记忆。
- QQ 主会话采用已发布人格与记忆，任务执行使用独立标准 agent，最终文本与平台分段回执分别保存。
- DSH 插件承担上下文预约、持久补交和副本清理，随后加入按主体隔离的好友事实。实际入口见 [DSH 接入](https://github.com/lxl8182/dsh-rin-memory)。
- 历史上的桌面实测、Linux 安装及真实 QQ 验收分开记载。部署里程碑见 [历史索引](history.md#已记录的服务器里程碑)。
