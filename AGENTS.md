# AGENTS.md — 铃芽 RIN · rin-memory 接手指南

项目根：`F:\game\projectNEKO\rin-memory`。品牌「铃芽 RIN」：核心仓库与发行包 `rin-memory`，
Python 导入名 `rin_memory`，CLI `rin-memory` / `rin-memory-run` / `rin-stack`；
DSH 接入端是旁边的 `dsh-rin-memory`（npm `@local/dsh-rin-memory`）。

- 最后更新：2026-11-03
- 当前版本：`0.1.0.dev14`（`pyproject.toml`）
- 进度只在本仓库 `docs/roadmap.md` 维护；本文件只写**长期有效的结构、边界和做法**。

---

## 1. 目标与边界

把 N.E.K.O 的通用记忆抽成独立项目，加上能修订人格卡的自演化，再无感接回宿主。
情绪分析、Live2D、表情、口型、语音、打断、主动表现、游戏接管**继续走宿主原链路**。

交付形态（2026-10-24 起）：新配置、新数据根、新启动入口；旧流程与旧基线不再作为前置，
收尾后退役旧入口。当前按 `docs/s05-new-system-plan.md` 的 P0 / A—F 推进。

已交付：独立服务 + 同源水彩管理台（48931）、真实模型对话与提炼/召回、更正/删除、
人格卡发布与演化、消费回执、重启恢复、宿主文字端（48911）、DSH/QQ 接入。
详见 `docs/roadmap.md` 的 S / Q / DM / QP / MC / FG 台账。

---

## 2. 先读什么

1. 本文件。
2. `docs/roadmap.md` —— **唯一状态入口**，别再在别处维护完成勾选表。
3. `docs/s05-new-system-plan.md` 的当前工作包。
4. 相关模块与其测试。
5. 需要领域合同时查 `docs/design.md`（§17 是冻结接口清单）；管理台合同查 `docs/webui-console.md` §2.3。

`docs/closeout.md` 与旧启动说明只用于查已有实现，不恢复旧 C06 的前置与验收要求。

---

## 3. 运行与验证

### 3.1 日常入口

```powershell
# 仓库内：服务 + 管理台（首跑自动 init，已在运行则复用，成功后开浏览器）
.\start.cmd
.\stop.cmd
.\start.ps1 -Action status

# 新链路整栈：服务 48931 + 宿主 48911 + 可选 Steam 桌宠壳
F:\game\projectNEKO\rin-memory\start-rin-stack.cmd

# stack CLI
tools\stack.ps1 init|start|stop|status -Config ..\companion-runtime\config\stack.toml
```

Steam 启动选项：`cmd /c "F:\game\projectNEKO\rin-memory\start-rin-stack.cmd" %command%`。
`NEKO_SHELL=none` 只起服务与宿主。

解释器固定用仓库 `.venv`（`-Python` 可覆盖）。运行根是 `F:\game\projectNEKO\companion-runtime`
（`config/stack.toml`、`host/`、`memory/`、`logs/`、`state/`）。

`plugin\nekospire\scripts\start-neko-source-shell.cmd`（旧内嵌链路）占用同样的 48911–48916，
**不能与新链路同时开**；新脚本端口被占时直接报错退出，不强杀进程。

### 3.2 测试

```powershell
uv sync
uv run python -m rin_memory                              # 十一段示例（临时根、替身模型、无网络）
uv run python -m unittest discover -s tests -v           # 63 个测试文件
uv build                                                  # 出 wheel / sdist
```

Windows 管道里需要明确 UTF-8 时用 `python -X utf8 -m rin_memory`。
示例与单测只用临时数据根，**不写运行根**。改动后跑与改动相关的 1–2 个测试文件即可。

---

## 4. 架构边界（必须保持）

1. 核心可以在不安装 N.E.K.O 的环境独立导入与运行；不 import 宿主
   `app/main_logic/main_routers/config/utils` 或插件 SDK。
2. 领域属于核心，宿主适配器属于宿主仓库。新链路只在独立服务内实例化核心，
   宿主与同源管理台都用这个权威实例。
3. **正式数据同一时刻只有一个写入方**（`.rin_memory.root.lock`）。main/tool/plugin 不能各建实例。
4. 存储、模型配置、token 工具、宿主写许可经明确接口注入；不整包复制 `utils`。
5. 角色/主体是身份边界，来源 scene 与 `applies_to` 分开。游戏生活事实可共享，
   战术经验留在插件或原场景；群组/用户习惯不可串到其他主体。
6. 同事件的原文、镜像、摘要、反思只算一份经历。用户明确偏好直接生效；
   AI 复述、时间经过和用户沉默不加支持。
7. 用户情绪、角色持续心情、本次外显表情分别处理。核心不调 Live2D、不发模型参数、不接 TTS。
8. 人格修订只动语义段落，保留宿主工具/语音/表现协议；模型路径、音色、API 凭据不可演化。
9. 引擎是当前人格的唯一真源，旧 `characters.json` 只读它的投影，避免旧卡覆盖新卡。
10. 原 HTTP 路径、WS 事件、turn/request/speech 标识和真实送达回执保持兼容；
    内部 dataclass 不直接替换现有 wire 格式。

**显示名**是人格卡身份段首行 `名字: <名字>`，不是独立文件或独立段落。
`service_character.json` 与旧 `identity_name` 段只作一次性迁移来源，并入后删除。

---

## 5. 代码地图

| 模块 | 作用 |
|---|---|
| `contracts.py` | 冻结的领域合同（事件、回执、删除/更正、人格块、directive） |
| `ports.py` | 端口接口：EventSink / ProfileReader / ContextProvider / Clock / StorageRoots / AtomicJsonStore / EventJournal / OutboxStore / CursorStore / ModelInvoker / TokenCounter / EmbeddingProvider |
| `api.py` | 组合入口：observe / build_context / build_turn_context / recall / correct / forget / reflect / persona / growth / state / maintain；未装配抛 `NotConfiguredError`，不返回空成功 |
| `invalidation.py`、`event_sources.py`、`classification.py`、`scope.py` | 纯函数：失效判定、来源键、内容分类与窗口、场景适用性 |
| `persistence/` | 原子写、schema 门禁、事件日志与回放、outbox、游标、来源账本、幂等、tombstone、SQLite time_indexed+FTS、recent / facts / reflections / persona / growth 存储 |
| `persistence/ownership.py` | S01 根所有权锁：`canonical_root`、`acquire_root_lock`、`read_root_lock`。CLI / 宿主 / 管理台共用 |
| `persistence/turns.py`、`domain/turns.py` | 整轮提交状态机、`request_id` 业务幂等、`TurnCommitService` |
| `domain/` | 删除屏障、forget / 更正、事实抽取、对话压缩、反思与晋升、人格条目与更正队列、召回（BM25/cosine/RRF/episode/fading）、状态与维护、发布 / 演化 / 审核 |
| `adapters/durable.py` | `DurableEventStore`：journal → 账本 → `durable=true`；回放处理与 reconcile |
| `adapters/composition.py` | `build_memory_core` 显式装配全部存储与域服务 |
| `adapters/model_http.py` | 零依赖模型适配器（urllib + 工作线程、分档、信号量、逐调用超时）；`api_key_env` 是变量**名** |
| `service/memory_service.py`、`service/server.py` | 常驻服务：根锁 → 建核心 → recover → 就绪；`/v1/chat` `/v1/turns` `/v1/context` `/v1/requests/{id}` `/v1/health`；默认仅回环并校验 Host/Origin |
| `service/console.py`、`service/management.py`、`service/chat.py` | 管理台业务视图、空库创建/首次发布、直连对话 `ChatBridge` |
| `service/model_config.py` | 设置页模型配置：原子写 `local_state/model_config.json`，只覆盖非空字段，公开视图不含凭据 |
| `service/listening.py` | 绑定语义唯一实现：Windows 独占、POSIX 允许 reuse（曾因 `SO_REUSEADDR` 出现同端口多实例） |
| `stack_config.py`、`stack.py`、`tools/stack.ps1` | 新配置模板/解析、固定解释器、init/start/stop/status、独立根与服务进程 |
| `cli.py` | `demo` / `run` / `migrate` / `rollback` / `package-check`；单写者锁、路径校验、退出码 0/2/3/1 |
| `migration.py` | 格式检测、迁移步骤、备份 manifest+sha256、`preserve_new` 回滚、verify |
| `webui/` | 管理台：唯一前端 `designs/tidebell/index-watercolor.html`（+ `live.js`），`server.py` 提供 API 与静态资源 |

宿主侧适配器在 `N.E.K.O\app\memory_server\engine_bridge\`（ports / runtime / mappers / middleware）。

---

## 6. 必须保持的行为合同

- 真实删除/更正失效请求结果并清正文（410）；删除走屏障，不直接删记忆文件。
- `stop()` 先拒新请求、排空在途，最后释放根锁。
- HTTP 的 `subject`/`scene` 贯穿上下文与结果查询；重试复用首次 `occurred_at`。
- `/chat` 生成前构建记忆上下文，回复进入整轮历史。
- 只有配置/鉴权错误才关闭聊天能力；模型并发由 `threading.BoundedSemaphore` 在工作线程内释放。
- 内存 append 的 `accepted` 只表示进程内接收，`durable=false`；不能当落盘回执，
  `demo_event` 不能冒充已确认事实。
- 只读入口不取锁；**新增任何可写入口必须先取根锁再装配核心**。
- 互斥只看 OS 锁；pid/mode 仅供排障，不要再引入「pid 不在就清锁」的逻辑。

---

## 7. 数据、部署与回滚

- 可以清空重来：`companion-runtime` 下的 config / memory / logs / 队列 / 回执 / 缓存。
- **人格卡不能清空或重建**：`persona_card.json`、`persona_versions.json`、`persona_admin.json`、
  `persona_card_archive/`，以及宿主侧投影（`host/memory/<角色>/persona.json`、
  `host/config/characters.json` 的身份语义段）。只能走编辑 / 发布 / 回滚 / 演化流程。
- 回滚要保留切换后新增的对话、更正和删除（`preserve_new`），不能把数据倒回去。
- 数据格式升级必须带版本检测与恢复行为；人格历史与恢复副本同样受删除屏障约束。
- 发布前分别构建 Python wheel 与插件 npm 包；只更新源码目录不会替换运行安装版。
- 迁入上游源码保留 Apache-2.0 版权头与 NOTICE，并记录本地修改。

---

## 8. 已知坑

- Windows 上 `SO_REUSEADDR` 会让第二个进程绑上正在监听的端口；监听 socket 统一走
  `service/listening.py`，不要自己调 `allow_reuse_address`。
- 编码护栏在 `tests/test_s03_encoding.py`，已定位正确的存储编码不要重复复查。
- `.cmd` 是 GBK/CP936，`.ps1` 与 `启动说明.txt` 是 UTF-8 BOM；改完保留编码。
- 真实时钟重试与 `R04` 相关，单独处理。
- 不写死模型、不复制密钥；凭据只存本机（POSIX 0o600），不进响应 / 导出 / 日志。
