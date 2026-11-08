# S05 N.E.K.O 文字接入决策

> 历史摘要 · 2026-10-24，H01–H03。当前状态见 [维护台账](roadmap.md)。[查看完整原稿](https://github.com/lxl8182/rin-memory/blob/0ee569e21d788076389211e4fada827c8e4631c7/docs/s05-neko-host-next-plan.md).

- H01 让宿主编辑同一张人格卡，走版本冲突检查、发布及下一轮采用。
- H02 分开呈现连接、生成、待补交、已存和已显示。宿主在外部生成前预约上下文，原话与结果入队后可补交。
- H03 让文字主动交流使用同一人格、记忆与消费合同。文字、实时语音及游戏采用各有独立证据。
- 现行接入代码为 N.E.K.O 的 `companion_host.py` 与 `main_logic/remote_memory.py`，HTTP 路径见 [客户端合同](webui-console.md#23-非宿主客户端合同)。
