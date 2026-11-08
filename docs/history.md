# 历史决策与原稿索引

当前阅读顺序见 [记忆流程](memory-flow.md)、[独立服务](standalone-service.md)、[管理台与客户端合同](webui-console.md) 和 [维护台账](roadmap.md)。下表按决策定位历史，原稿固定在整理前的 Git 版本，可在本地用 `git show 0ee569e:docs/<文件名>` 查看。

| 主题 | 保留的决策摘要 | 完整原稿 |
|---|---|---|
| B / C 首版与宿主兼容 | [冻结合同与设计](design.md)、[首版收尾](closeout.md) | [设计原稿](https://github.com/lxl8182/rin-memory/blob/0ee569e21d788076389211e4fada827c8e4631c7/docs/design.md) |
| S05 独立服务与新栈 | [新系统范围](s05-new-system-plan.md)、[N.E.K.O 接入](s05-neko-host-next-plan.md) | [新系统原稿](https://github.com/lxl8182/rin-memory/blob/0ee569e21d788076389211e4fada827c8e4631c7/docs/s05-new-system-plan.md) |
| Q / DM / QP | [QQ 接入](server-qq-dsh-plan.md)、[远程宿主](remote-memory-host-plan.md) | [QQ 计划原稿](https://github.com/lxl8182/rin-memory/blob/0ee569e21d788076389211e4fada827c8e4631c7/docs/server-qq-dsh-plan.md) |
| MC / RO 连续性与主动交流 | [连续性](companion-continuity-plan.md)、[继续优化](rin-next-optimization-plan.md)、[RO04 投递约定](ro04-candidate-contract.md) | [连续性原稿](https://github.com/lxl8182/rin-memory/blob/0ee569e21d788076389211e4fada827c8e4631c7/docs/companion-continuity-plan.md) |
| 养成设计与第一人称 | [养成决策](companion-growth-design.md)、[FG01–FG05](first-person-growth-plan.md) | [第一人称原稿](https://github.com/lxl8182/rin-memory/blob/0ee569e21d788076389211e4fada827c8e4631c7/docs/first-person-growth-plan.md) |
| 旧批次台账、交付证据和未来提案 | 当前事项汇入 [维护台账](roadmap.md)，未采用的扩展仍按提案阅读 | [整理前台账](https://github.com/lxl8182/rin-memory/blob/0ee569e21d788076389211e4fada827c8e4631c7/docs/roadmap.md) |

工作区根的《N.E.K.O-记忆成长与遗忘方案》《独立记忆与人格演化引擎方案》《记忆质量优化实施方案》保留各自原稿。前两份记录抽离前的设计，后一份记录 11-06 的实施任务和验收口径。

## 已记录的服务器里程碑

| 日期 | 版本与事件 |
|---|---|
| 2026-11-02 | 核心 `0.1.0.dev14`（`e24ce23`），插件 `0.3.3`（`1597164`），QQ bridge `1.0.6-neko.11`。验收了人格发布与采用、管理台空状态、原始置信度显示和自动演化模式 |
| 2026-11-04 | 核心 `0.1.0.dev15`、插件 `0.3.4`、QQ bridge `1.0.6-neko.13`。主人和授权好友的主体隔离通过，发布人格为第 51 版 |
| 2026-11-04 | 安装核心 `0.1.0.dev16`，设置 `service.memory_enabled = false`。已记录的检查显示主人和好友召回为零，主人已发布人格可读取，已有事实和人格数据保持，RIN / DSH 服务正常 |

日期对应各次交付记录。当前源码功能及已知的环境差异见 [维护台账](roadmap.md)。
