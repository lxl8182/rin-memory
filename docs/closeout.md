# 首版收尾与兼容验收

> 历史摘要 · 2026-10-22 至 10-24，B / C 批次。当前状态见 [维护台账](roadmap.md)。[查看完整原稿](https://github.com/lxl8182/rin-memory/blob/0ee569e21d788076389211e4fada827c8e4631c7/docs/closeout.md).

- C01–C05 收敛了宿主委托、人格采用、演化管理、WebUI 与隔离验收。关键约束包括单写者、真实采用/送达、删除后的缓存和日志清理。
- C05 保留可回放、可验证的迁移与 `preserve_new` 回滚演练。对应的适配边界及冻结接口保存在 [design.md](design.md) §17。
- 旧 C06 的原根切换、旧数据和旧实机基线后来退出 S05 交付范围。日常独立栈接手看 [独立服务](standalone-service.md)。
