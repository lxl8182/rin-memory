**完整项目分批开发计划与后续扩展路线**

更新：2026-11-01。适用项目：`F:\game\projectNEKO\rin-memory`。

本文件负责“按什么顺序开发、做到哪里算完成、下一批做什么”。架构与行为合同以 [design.md](F:/game/projectNEKO/rin-memory/docs/design.md) 为准，开发边界与源码地图见 [AGENTS.md](F:/game/projectNEKO/rin-memory/AGENTS.md)。

**2026-11-01｜第一人称记忆与人格成长优化**

方案见 [第一人称记忆与人格成长优化方案](first-person-growth-plan.md)。实施按 FG01 → FG02 → FG03 → FG04 → FG05 推进。

| 工作包 | 交付重点 | 状态 |
|---|---|---|
| FG01 统一视角与提示词 | 核心生成第一人称，采用正向任务表述，覆盖提取、反思、摘要、教导与人格 | 已完成（本机交付） |
| FG02 经历输入与主题组织 | 完整互动、相关人格和旧认识进入反思，围绕具体事情处理变化 | 已完成（本机验收） |
| FG03 人格整段修订 | 演化与教导重写相关段落，篇幅收敛，来源撤回与历史继续有效 | 已完成（本机验收） |
| FG04 自我更正与跨端采用 | 核心共享认知、真实能力与回执、DSH 和网页采用、旧条目衔接 | 已完成（本机验收） |
| FG05 效果验收与部署准备 | 固定样本、多轮行为、人格差异、定向回归与版本包 | 已完成（本机版本包） |

FG01（2026-11-01）：新增核心共用第一人称提示段，接入单轮/合批提取、反思、压缩、人格演化与纠错复核；事项、兴趣信号和教导规则同步采用正向任务表述。DSH 主会话魔法说明、内心口吻及记忆说明统一为第一人称。调整了演化提示的段落修订引导。核心 100 项、DSH 47 项定向检查通过，静态提示词检查通过。改动保存在本机工作树。

FG02（2026-11-01）：双方消息采用稳定 `message_ref`，事实保留消息位置、说话身份、原始时间和逐条来源绑定；单轮与合批采用相同语义，用户陈述、角色表达和共同经历分别归属。提取与反思读取当前已发布人格，表达示例保持其示例用途；反思从时间索引读取完整交换和相关旧经历。逐主体、适用场景与主题处理单次有效经历，模型用 `source_fact_ids` 选择实际支持，split 各部分分别绑定。主题结果、正常间隔与失败重试沿 synth_backoff 持久化；提交前复查删除、更正、来源内容和人格，角色重复表达维持独立支持数量。

FG03（2026-11-01）：演化生成器采用 `replace/add/delete` 和全卡结构，旧 `merge` 模型结果返回 `full_section_required`；卡片原有 API 的 merge 合同保留。教导生成完整正文，段落保留手写基础、规则与已采用成长的来源映射；同义来源更新支持信息，正文保持收敛。模型等待位于写锁之外，提交复核版本与来源；prepared/applied/completed 阶段经正常恢复与维护续接发布。改写后的教导可撤回，删除与更正同时处理正文、版本、教导副本、历史与归档，保留其余有效来源。三项行为测试覆盖归属与撤回、生成中用户编辑、保存/发布中断恢复。

FG04（2026-11-01）：`/v1/context` 增加核心共用 `memory_awareness`（text/version/capabilities），按客户端授权、反思配置和 off/propose/auto 生成稳定版本。网页 system 与 DSH `shapeAssembly()` 每轮采用一份；新会话携带材料改用第一人称并保留原说话者和先前时间。旧服务缺少字段时沿原装配；好友/群轻量模式继续使用自己的会话人格。DSH 按 pending 回执说明“已保存、待发布”。能力与模式切换、真实网页模型输入及 DSH 最终 assembly 检查通过。

FG05（2026-11-01）：按用户要求各阶段先做小测试，全部实现后统一回归：核心 `unittest discover -s tests` **556 项，555 通过、1 原有跳过**；DSH `npm test` **112 通过**，`npm run check` 与两库 diff 检查通过。当前配置 `deepseek-v4.1-flash` 在隔离根完成明确标注的 A—G 模拟互动，以及同一输入的 HEAD 提取提示基线对比；最终验收使用 30 份调用记录，调试阶段共 54 份实际模型请求留档。观察到具体生活记忆、角色兴趣延续、三主题分别吸收、更正后认识更新、同义教导收敛与来源撤回；两份独立共同完成经历通过成熟门槛后，真实模型生成 replace 提案并完成审核发布。输入、输出、模型参数和人格差异见 `.scratch/fg05/acceptance.json` 与 `call-*.json`，真实网页回复及 DSH 最终请求分别见同目录记录。

本机交付核心 **0.1.0.dev13**、DSH 插件 **0.3.3**，包位于 `.scratch/fg-release/`；wheel 独立环境的导入、教导、发布与撤回通过，包清单检查通过。版本包尚未安装到日常实例或服务器；本轮未向真实 QQ 投递测试消息。

**1. 如何按这份计划推进**

原开发顺序为 **B01 → B02 → … → B15**，随后由 C 系列补齐宿主接线。2026-10-24 用户将当前目标调整为全新独立系统，按 [S05 新系统开发与验收计划](s05-new-system-plan.md) 推进；旧 C06 实机基线、旧数据迁移和双模式切换退出执行范围。已有核心和可用代码继续复用。

每批开工先核对前置批次的实际代码与检查结果。完成时更新本文件的状态台账，在该批末尾补一行简短交付记录：实际改动、验证命令与结果、提交号或工作树路径、尚待处理的具体事项。复用同一文件，不为每批另建过程报告。

本文件的台账是批次状态的唯一维护位置。README 和 AGENTS 只保留入口和阶段说明；`design.md` 的 D0—D5 是大阶段定义，不另维护一份完成勾选表。

状态使用“未开始 / 进行中 / 已完成 / 待条件 / 已取消”。“已取消”用于用户明确替代或取消的旧交付，不计为完成。按当前执行卡的完整交付判断状态；核心接口完成、宿主接线、隔离集成、正式环境验收分别记录。前置未闭环时，可交付不依赖它的模块并记录局部结果，整批仍按剩余任务判断。外部条件只阻塞实际依赖它的步骤，隔离开发继续推进。

用户要求“继续下一批”时，按 S05 新计划的 P0、A—F 顺序领取首个可执行工作包，先核对当前已实现内容；不再返回旧 C06 清单。正常实施与必要修正一起完成，运行操作按当次已有授权执行。

**2. 批次总览与当前台账**

| 批次 | 交付主题 | 阶段 / 前置 | 当前状态 | 已交付范围与剩余项 |
|---|---|---|---|---|
| B00 | 可安装框架、内存示例与接手文档 | D1 框架 / 无 | 已完成 | 框架、示例与包验证 |
| B01 | 冻结兼容合同与迁移清单 | D0 / B00 | 已完成 | 合同、归属与测试入口 |
| B02 | 原格式存储、来源账本与可恢复提交 | D1 / B01 | 已完成 | 核心存储与恢复 |
| B03 | 更正、删除屏障与防复活底座 | D1 / B02 | 已完成 | 核心更正与删除闭环 |
| B04 | 最近对话、事实抽取与内容闸门 | D1 / B03 | 已完成 | 核心抽取与压缩 |
| B05 | 反思、现有人格与成长能力迁入 | D1 / B04 | 已完成 | 核心反思、成长与失效联动 |
| B06 | 相关召回、经历关联与淡出 | D1 / B05 | 已完成 | 核心召回与预算 |
| B07 | 当前状态、后台维护与独立闭环 | D1 / B06 | 已完成 | 独立核心闭环 |
| B08 | N.E.K.O 记忆接口与生命周期适配 | D2 / B07 | 已完成 | 完整委托/写入者归属由 C01 补齐 |
| B09 | 对话、游戏、语音和 Live2D 兼容验收 | D2 / B08 | 已完成 | 隔离集成由 C05 补齐；实机语音/Live2D/游戏/浏览器归 C06 |
| B10 | 当前人设卡、版本与人工编辑 | D3 / B09 | 已完成 | 核心卡 + 宿主投影/编辑由 C02 补齐 |
| B11 | 人格发布、实时会话与游戏版本一致 | D3 / B10 | 已完成 | 发布/消费 + 聊天/主动/游戏采用由 C02 补齐；实时 ack 待 C05 |
| B12 | 有依据的自动人设修订 | D4 / B11 | 已完成 | 提案/校验 + 后台触发/自动采用由 C03 补齐 |
| B13 | 演化管理与撤回/删除完整闭环 | D4 / B12 | 已完成 | 核心审核/撤回/模式 + 宿主演化管理由 C03 补齐；管理台待 C04 |
| B14 | 发布包、迁移及回滚演练 | D5 准备 / B13 | 已完成 | 版本固定/迁移钩子/一致性副本回滚由 C05 补齐 |
| B15 | 原内嵌方案正式切换与实机验收 | D5 交付 / B14 | 已取消 | 2026-10-24 用户改为新系统交付，由 S05 的 N01—N15 替代；保留历史切换记录 |

2026-10-22 状态校正：B08—B14 按原执行卡恢复为进行中，保留全部已有成果和验证记录。下方 B 交付记录按当时范围保存，其中“下一批”和环境缺项是历史现场信息；当前任务与现场条件以 C 台账和执行前核对为准。

**2.1 收尾执行台账**

历史范围、验收和 WebUI 合同见 [closeout.md](closeout.md)。C01—C05 已有成果继续复用；旧 C06 于 2026-10-24 取消，当前开发与验收入口为 S05 新计划。

| 任务 | 工作 | 前置 / 可并行部分 | 当前状态 | 完成后回填 |
|---|---|---|---|---|
| C01 | 完整记忆委托、单写者与生命周期 | 现有核心/薄适配器 | 已完成 | B08 |
| C02 | 当前卡投影、编辑与各消费者实际采用 | C01；核心端可先准备 | 已完成 | B10/B11 接线 |
| C03 | 自动演化触发、审核采用、发布与恢复 | C02；调度端可先准备 | 已完成 | B12/B13 后台流程 |
| C04 | WebUI 页面、真实 API 与管理闭环 | 页面可先做；联调依赖 C01—C03 | 已完成 | B13 管理与删除联动 |
| C05 | 新引擎启用集成、性能、固定版本与副本演练 | C01—C04 | 已完成 | B09 隔离验收、B14，复核 B10—B13 |
| C06 | 原内嵌方案正式切换、当前角色实机与交付 | C05 | 已取消 | 2026-10-24 用户取消旧流程/基线/数据前置；新根实机验收改由 S05 承担 |

**2.2 独立服务化台账（S01—S07）**

范围与接口合同见 [standalone-service.md](standalone-service.md)，当前执行计划见 [s05-new-system-plan.md](s05-new-system-plan.md)。S 系列让 rin-memory 脱离宿主独立运行，并给 N.E.K.O/WebUI 提供统一权威实例；复用 C01—C05 成果，交付新配置、新根和新启动入口。

前置关系：S01 是其余全部项目的前置（写者所有权必须先统一）；S03 与 S02 可同期；S06/S07 属召回增强，不阻塞首版服务化交付。

2026-10-24 最新范围：P0 核验 S02/S04 的 R01—R07，新配置和初始化工作可先准备；S05 从新根直接开发、验收，不再等待旧 C06、旧基线或旧数据。完成后退役旧配置读取、旧启动与内嵌运行路径，不保留双模式切换。具体问题见 [服务化复核](standalone-service.md#service-review-20261024)，工作包与 N01—N15 验收见 [新系统计划](s05-new-system-plan.md)。

| 任务 | 工作 | 前置 | 当前状态 | 完成后回填 |
|---|---|---|---|---|
| S01 | 运行所有权：根锁协议下沉到共享入口 | 现有核心/CLI/宿主 | 已完成 | §2.1/§2.2 合同、C 系列写者归属 |
| S02 | 整轮提交、恢复与结果回放（含入队断点） | S01 | 已完成 | §3.2—§3.4；复核修复 R01/R04/R05 已验证 |
| S03 | 分类兼容与中文链路定位 | 已知输入路径，可与 S02 同期 | 已完成 | §5.1/§5.2 |
| S04 | 模型适配器、常驻服务与独立安装 | S01—S03 | 已完成 | §4.1—§4.4；复核修复 R01—R03/R05—R07 已验证 |
| S05 | 新配置/启动、N.E.K.O / WebUI 接入、新系统验收与旧入口退役 | 新服务 P0 核验；新配置与资源 | 进行中 | dev2 服务侧/非宿主收尾完成：B 全部、C 管理台聊天、D 服务消费合同、E 非宿主验收；A/C 文字宿主已接；剩语音打断/播放、D 主动/实时/游戏及宿主编辑、E 完整整栈与 F 退役 |
| S06 | 召回失败样本与基线 | 最小服务可用 | 已完成 | §6.2 |
| S07 | 按样本选择分词/扩展/向量 | S06 | 未开始 | §6.3—§6.5 |

S05 宿主下一批按 [详细接手计划](s05-neko-host-next-plan.md) 执行。以下为待开发工作包，编写计划不计入实现完成：

| 工作包 | 前置 | 状态 | 完成标准 |
|---|---|---|---|
| S05-H01 人格设置统一 | 已交付的文字宿主链路 | 已交付（隔离+日常只读/草稿验证） | 宿主读改发布同一张服务卡；管理权限、草稿冲突和资源边界通过验收 |
| S05-H02 连接与保存状态 | H01 | 已交付（隔离+真实队列验证） | 真实服务状态、逐轮提交结果、显示回执、断线及刷新恢复在共享聊天 UI 可见 |
| S05-H03 文字主动交流 | H01/H02 | 已交付（服务快照与放弃语义已验证） | 独立消费者、固定人格/记忆、正确来源分类、用户抢占和持久回写闭环 |
| S05-G01 尖塔最小记忆接入 | 已有服务与统一同伴会话 | 已接入（定向、真实模型、日常 HTTP 通过；实际游戏场景待验收） | 接管时读取已发布人设并在正常游玩期间固定；重载/续局可采用新版；后台提炼共享生活事实与通用相处偏好，关键共同经历保留游戏来源；打法和临时要求仍归插件 |

2026-10-26 G01 范围按用户最新约定收窄：宿主 `main_logic/game_memory.py` 负责已发布人设读取、独立持久事件队列及原 ID/时间/正文补交；`sts2_companion_router` 在接管/新局时固定人设，玩家原话在输入入口入队；插件使用该正文，不再拼接旧宿主档案或局内注入通用召回/成长。`shared_experience` 沿用已有结构化事件，适用范围固定为尖塔；生活事实与通用相处偏好交服务后台从真实用户原话提炼，游戏打法/数值/临时安排从提炼目标排除。TTS/表情/动作链路复用，语音播放完成回执改造不在本批。既有删除待办/插件清理回执接到服务，补交先处理删除范围。

G01 验证与部署：新增 3 项行为检查，分别覆盖宿主固定人设及持久事件恢复、插件人设稳定/重载换版及导出范围、服务提炼目标与事实范围；相关插件组合 47 passed，宿主组合 30 项通过，核心提炼 5 项通过。使用真实 summary 模型的独立数据根验证：混合发言只产出工作安排和通用交流偏好，纯战术发言为零事实，共同通关经历保留 game 范围，outbox 清空；日常人格卡未改动。已构建并重装当前 wheel、重启新 stack、将插件部署到新 host 根；日常 `/api/sts2-companion/poll` 返回 `standalone_game`，重复轮询人设/版本稳定、待上传为零。当前未运行游戏，真实战斗中的 TTS/表情体验与跨局观察仍待实际游玩。

G01 启动收尾：新 host 根已启用插件自动启动偏好。插件沿用宿主既有的延迟恢复机制，在桌宠/聊天窗口首次连接并发送会话就绪信号后加载；冷启动尚无客户端时不提前扫描插件。真实 `sts2_get_status` 工具调用成功。

2026-10-27 缓存与共同经历调整：N.E.K.O 文字客户端保留宿主规则与已发布人格组成的 system，原生已提交历史按轮追加；服务召回/更正作为带来源标识的 resident 背景追加到对应用户消息之后，同一背景在有效历史中去重，工具轮次使用原快照。初始化及删除/更正版本变化时才从服务重建已过滤历史，人设换版从下一轮更新 system。共同经历取消统一 game 限制：有相处意义的 `shared_experience` 默认 shared，仍保留游戏、局次、episode 与来源，明确的 game/run 范围继续生效；战术与临时安排仍留在插件。提炼入口合并场景字段时保留来源中的插件和局次。新增 2 项原生流式链路测试，更新既有范围测试；宿主 27、插件 17、核心 8 项通过，共 52 项。已重装 wheel、定点部署插件并重启日常 stack；宿主 connected、pending=0，真实 `sts2_get_status` 成功。当前库没有需要调整范围的旧游戏经历。

2026-10-25 文档口径调整（仅文档，未改代码）：用户明确本轮是测试开发、过去数据不必保留、进程可随意操作，因此删除 A 类数据/测试隔离规则与 B 类运行开关规则。现有 `companion-runtime`、`nekodata`、锚点根可直接读写清空重来，不再强制临时目录/合成根；TTS、麦克风、屏幕观察、摄像头、主动交流开关不再默认静音或禁用；自动演化模式（off/propose/auto）可自由切换；测试数量不再设上限。**唯一保留的例外是人格卡不能清空或重建**（`persona_card.json`、`persona_versions.json`、`persona_admin.json`、`persona_card_archive/` 及宿主侧投影），只能经服务的编辑/发布/回滚/演化修改。受影响文档：根 `AGENTS.md`、本文件、`design.md`、`s05-new-system-plan.md`、`s05-neko-host-next-plan.md`、`standalone-service.md`、`server-qq-dsh-plan.md`、`closeout.md`、工作区 `../AGENTS.md`。代码侧 `cli.py` 的 `validate_data_root`（拒绝 nekodata/Documents 路径）保持不变，本轮按要求未改代码；需要直连真实根时走服务/宿主/管理台。

2026-10-24 管理台显示修复：提案对比改为读取基础 revision 的历史原文，自动采纳或后续编辑后仍保留正确的修改前后；归档历史可读取，已清理历史明确标注不可用，多段操作全部参与对比，已采纳提案不再显示驳回空修改的提示。发布黄点改为真实的待发布/待采用/采用失败提醒并附悬停说明；主动、实时、游戏尚未接入时明确标为“未接入”，不计入采用比例或等待数。新增 3 个针对历史对比、使用方状态和实际前端脚本的行为检查，与已有服务 HTTP 验收合计 6 项通过。

S05 H01—H03 交付（2026-10-25，工作树 `F:\game\projectNEKO\rin-memory` + `N.E.K.O`，版本 `0.1.0.dev2`）：服务新增受限宿主人格能力，端点 `GET /v1/host/persona`、`POST /v1/host/persona`（按 `base_revision` CAS 修订）、`POST /v1/host/persona/publish`（只接受 `next_turn`，必须提交编辑器看到的 revision）。能力由 `stack.toml` 的 `[clients.<id>] host_persona = true` 显式授予，默认关闭；`ManagementApi.require_host_persona` 与传输层共同校验，console 管理标志不等于该能力，也不经过模型工具。冲突返回 409 并附带服务当前卡，草稿始终留在调用方；错误信封新增可选 `extra`（`ApiError(extra=...)`）。宿主侧 `main_logic/remote_memory.py` 增加 `persona/revise_persona/publish_persona`，`main_routers/remote_memory_router.py` 提供同源 `/api/rin-memory/persona*`；`characters_router/persona.py` 与 `cards.py` 在 `NEKO_COMPANION_CONFIG` 存在时拒绝旧人格写入并改走服务，服务失败返回 5xx，不再回落本地保存。前端新增 `static/js/character_card_manager/persona-service-editor.js`：按段落呈现服务的 id/kind/frozen/可编辑性、草稿 revision 与已发布版本，分开“保存草稿”和“发布到下一轮”，409 时保留输入并提供放弃/基于最新版本两个明确选择。H02：宿主队列新增有界、无正文的逐轮状态（`generating/pending/saved/interrupted/failed`，失效由删除回执体现），`GET /api/rin-memory/turns` 与状态接口提供 `turns`/`pending_turns`/`failures` 摘要；`status` 改为真实探测（区分未配置、连不上、被拒、存储不可用、可用），`/v1/health` 的 `chat` 仅描述服务自身模型，宿主生成+服务存储不依赖它。新增 `static/app/app-companion-turns.js`：以 host turn id 精确归属气泡，只追加状态块不替换原文，显示回执按码点计数（emoji 计 1），刷新后从页面缓存与宿主状态恢复，迟到回调只改自己的轮次。H03：新增 `main_logic/proactive_chat/remote_memory.py`，主动轮次在 Phase2 生成前取一次固定快照（人格+更正+召回），使用消费者 `neko_active`、分类 `companion_output`（不进入事实提炼，不增加用户偏好支持），只在实际送达后提交；未送达、被过滤、异常或取消一律 `interrupted` 且不写正文；不持有整轮生成锁，用户在生成中发言由既有抢占路径取消。服务侧 `/v1/health`、管理台与水彩页面未改版，纯新增端点。

验证：核心 `tests/test_s05_host_persona_state.py` 8 项（能力授予/拒绝、CAS 冲突携带当前卡、草稿不影响在途发布、发布旧 revision 拒绝、非 `next_turn` 拒绝、`companion_output` 不产生偏好、放弃轮无正文、HTTP 403/409 信封）通过；既有 `test_s05_neko_host`/`test_s05_stack`/`test_s04_service` 34 项通过；全量 discover 269 项通过，唯一 error 是既有 `test_s05_evolution_display` 的 `from tests.test_b12_evolution` 在无 `tests/__init__.py` 时无法解析（本轮未改动该文件）。宿主新增 `tests/unit/test_host_persona_and_turn_state.py` 7 项（服务不可用不落本地、只转发编辑过的文本段、保存路径返回 5xx、逐轮状态无正文且重启保持、generating 重启后读作 interrupted、主动轮使用自己的消费者与分类、被过滤候选不写文本）+ 既有 personas/proactive/delivery/state 定向 124 项通过。真实页面：`/character_card_manager` 显示 `人格设定（铃芽服务）`、`草稿 rN · 已发布 rN (hash)`、四段含冻结标记；制造并发修订后保存返回 409，提示 `服务当前为 rN` 并保留草稿；放弃草稿后回到服务最新文本。日常实例已用 `uv build` 重建 wheel 并安装到配置解释器，整栈 stop/start 两次；`/api/rin-memory/status` 返回 `connected`、`character_matches=true`、`turns={}`、`pending=0`，人格 r21 已发布且 `stale=false`，四段原文与验收前一致（208/57/134/273 字）。端到端脚本经真实服务验证：丢失一次提交响应后状态为 `pending`，后台同键重放后变 `saved`，服务只多一轮且模型只调用一次；主动轮次取到 678 字固定快照、放弃后状态 `interrupted` 且 `reply_chars=0`。所有合成验收轮次已通过管理 API 按 `source_key` 删除（正文清空、`turn_requests` 置 deleted、`recent.json` 无残留），未写入事实。未做语音、实时、游戏或桌宠壳完整验收。

S05 N.E.K.O 文字接线（2026-10-24）：新增宿主 `companion_host.py`、`main_logic/remote_memory.py` 和状态/显示回执路由，在现有 `OmniOfflineClient.stream_text` 生成边界读取固定发布人格、必需更正与召回，复用原流式输出/工具流程。宿主旧历史上传与被动旧记忆读取在新入口停用；显式 recall_memory 也调用独立服务。服务新增 `/v1/context/reserve`，只持久登记请求、身份与版本依据，使已生成轮次在服务重启后仍可补交；删除/更正屏障始终复核。宿主队列有界、定时重试、重启续传，响应丢失同键重放，失效正文清除。网页显示回执与使用回执分开，`neko_chat` 独立显示。新 stack 启动宿主并注入新数据/配置/插件根、端口与启动状态目录；修复原启动器清除显式目录的路径，跳过旧数据探测和云存档导入。隔离实例真实中文多轮、红茶续接、新人格发布后的下一轮采用和实际显示回执已通过。语音/主动/实时/游戏、宿主语义编辑与完整桌宠验收仍未完成。验证：新增 3 项覆盖登记重启/作用域与屏障/原生生成边界及丢响应恢复；配套启动、服务和前端 12 项检查通过，宿主上下文 16 + 17 项通过，合计 45 项定向检查。日常 48911 已连接 48931。

S05 非宿主收尾（2026-10-24，版本 **0.1.0.dev2**）：按用户要求先完成服务侧与管理台对话。新增 `service/protocol.py` 和持久消费回执存储，共用唯一核心与 journal：300 秒随机上下文凭据绑定角色/客户端/主体/会话/场景及删除、更正依据；校验与外部提交在失效、过期和重启后返回 409/needs_reissue。已发布文本及 scope 固定在生成开始时，在途生成保持原版本，后续轮次采用新版本。提交后记录 used，实际显示后记录 delivered，interrupted 按实际字符数记录；上下文读取不算使用。客户端按 TOML 身份、令牌环境变量与主体/插件绑定访问，私有读取与请求回放同样校验范围。新增事件上传、插件待清理/确认、清理重试、更正撤回和手动维护合同；chat/turns/events 共用有界积压与在途预留，满额 429，已提交回放不受模型离线影响。提炼确定性保留主体、game/run 范围和来源，同文事实按 scope 去重。

管理台：稳定请求键与草稿存于 sessionStorage，响应丢失/重连可同键重试，服务重启后的 401 自动重建身份；显示实际模型名、提交/显示状态；新增可演化段落、清理重试和更正撤回入口。修复单段编辑丢失打开时 revision、编辑后发布旧 revision、发布页静态假令牌、未发布修订误标生效的问题。演化服务在装配时开放提案能力，持久模式默认 off；off/propose/auto 三种模式通过服务 HTTP 验证，冻结身份不变，auto 发布后下一轮采用。

验证：**131 项定向回归 + 3 项已安装包/启动检查通过**，JS 语法检查通过；dev2 从 sdist 构建 wheel，仓库外启动，水彩 HTML/JS/CSS/图片与 API 可用。新合成根 `artifacts/s05-nonhost-20261024` 使用用户指定 `deepseek-v4.1-flash`（conversation/summary/correction）及 `https://d1api.xin/v1/chat/completions` 完成中文三轮：青禾花园/茉莉与乌龙茶信息→提炼 2 条事实并召回→更正红茶→真实回复采用新偏好；管理台真实编辑发布、重启历史/模型/版本保持。凭据写在新运行 state 根的本机配置，导出/公开健康/日志均不含凭据。服务实际使用根为 `F:\game\projectNEKO\companion-runtime`，管理台 `http://127.0.0.1:48931`，host.enabled=false。

N14：Windows 11、i5-12400F（6 核/12 线程）、Python 3.11.15；新根 1,000 条事实，预热 8 次，并发 4、200 次真实 HTTP context。首次 p95 539.64 ms，剖析定位为逐条屏障查询与重复分词；批次共享屏障快照、按不可变文本/停用词缓存分词后 p50 **97.82 ms**、p95 **109.14 ms**、最大 **125.52 ms**，0 失败，仍在请求结束复查删除/更正依据。chat 返回 context/model/commit/queue 分段耗时，容量拒绝直接返回、queue_ms=0。

最后的删除续聊复核修复了按来源删除只清摘要、原文仍留在 recent 的缺口：显式来源删除现在同时清 recent、SQLite 原文与 FTS，清理前过滤已失效窗口，避免后续写入被旧正文阻塞；force 清理重试支持修复已完成回执对应的残留。新增行为纳入同一组 3 个 HTTP 验收测试，没有增加测试基础设施。更新后在已安装包上跑 **18 项删除/原文/人格管理定向检查**通过；真实来源删除后旧请求 410、旧上下文 409、继续聊天成功，服务重启后同键回放不新增轮次，8 条消费回执仍可读取。一次真实模型样本：context 16.079 ms、模型 2864.970 ms、提交 84.883 ms、排队 0 ms，总请求 2972.684 ms。

当前结论：**非宿主流程已完成，S05 整体进行中。** N02/N09/N10/N14 通过；N01/N03/N04/N05/N06/N07/N08/N11 的服务/管理台条件通过，宿主相关条件保留。剩余按 A 宿主早期目录/资源注入 → C 远程客户端和有界持久待同步队列 → D 主动/实时/游戏与 TTS/Live2D/打断 → E 整栈验收 → F 旧入口退役推进。完整合同与矩阵见 `webui-console.md` §2.3 和 `s05-new-system-plan.md`。

交付时运行状态（2026-10-24 更新）：用户确认没有其他窗口操作并要求按既定配置恢复后，已通过管理 API 恢复 48931 的端点、本机凭据及 conversation/summary/correction 三档 `deepseek-v4.1-flash`。完整 stop/start 后凭据和模型配置保持、原有人格 revision/version 不变，真实对话返回 committed/durable，按原会话查询结果成功；日常实例的凭据缺项已解决。48932 隔离验收实例继续可用。源码、wheel/sdist 的真实凭据扫描为 0 命中。

S05 首批交付（历史记录，2026-10-24，工作树 `F:\game\projectNEKO\rin-memory`，版本 `0.1.0.dev1`）：完成计划 §5 指定的“新入口启动服务和管理台，新建人格后重启仍在”。新增 `stack_config.py`（显式 TOML、相对路径/独立根/解释器/版本校验、可编辑模板与缺项）、`stack.py` + UTF-8 BOM `tools/stack.ps1`（init/start/stop/status、隐藏后台进程、实例身份、重复启动、端口冲突、失败日志与有序退出）。宿主尚未接入，模板保持 `host.enabled=false`；开启时明确拒绝，避免拉起旧流程。将既有 `RinApp` 业务视图移入 `service/console.py`，旧开发传输复用；`service/management.py` 与 `/v1` 传输使用服务同一个核心。加入空库创建/首次发布、显示名独立保存、管理写身份、Host/Origin 防护、409 保留草稿、断线重连与配置缺项显示。HTTP 工作线程和模型工作均有界。wheel/sdist 包含实际水彩资源，安装后从新 state 目录启动。具体已实现方法/作用域/幂等与错误码见 `docs/webui-console.md`。

P0 本批核验通过；另修复 R02 两处生命周期遗漏：结束旧代次请求也扣减在途计数；排空超时仍保留根锁，待后台与最后一个处理器结束后释放。加强原 R02 行为测试验证正常排空及超时后拒绝第二写者。验证：安装 wheel 后 `python tests/test_s05_stack.py -q` **3/3**（配置/不覆盖、同核心 HTTP 创建/编辑冲突/发布/重启与资源、子进程启动/重启/端口冲突/停止）；`PYTHONPATH=src python -m unittest tests.test_s04_service tests.test_s05_review_fixes tests.test_s01_console webui.tests.test_testbed_api -q` **46/46**，合计 **49 项定向检查**。`uv build` 从 sdist 构建 wheel 成功（101 文件，16 项 UI 资源），JS 语法和 PS1 BOM 通过。Browser Use 在独立合成根 `artifacts/s05-acceptance` 实际完成创建→发布→重启→读取、人工编辑冲突后草稿保留；修正了 service 模式角色名与修订数量的静态样稿残留。测试实例已停止。

管理台直连对话（2026-10-24，C 工作包的先行部分）：新增 `service/chat.py`（`ChatBridge`）与管理台「对话」页，使 WebUI 在宿主接入（C）完成前即可直接对话，**不经过宿主**。实现要点：`ChatBridge.send()` 组装标准 `TurnRequest` 后调用 `MemoryApi.chat`，因此复用 S02/S04 的整轮提交（reserve → 生成 → basis 复核 → journal 提交 → 入队提炼）、幂等重放、`context_stale` 与 `service_closing` 拒绝，未新增任何领域规则；`history()` 只读 `recent.json`（提交实际写入的位置）并附整轮提交账本，不另存一份对话；`status()` 只报告模型是否已配置与凭据是否存在，**从不返回凭据本身**（`api_key_env` 始终是变量名）。装配：`RinApp` 新增可选 `chat`；`ManagementApi` 用一个 `ChatBridge` 复用服务的同一核心与模型（console 与服务不会变成两个写者）；独立 `webui/server.py` 新增 `--model TIER=MODEL --model-base-url --model-api-key-env --model-timeout-s`，demo 根用带标注的 `DemoChatModel`（回复明确写「离线演示模型」，避免被当成真实生成）。失败映射：可重试的 timeout 报 504、rate_limit/transport 报可重试 503，且**不关闭对话能力**（R06）；只有 config/auth 关闭能力。只读根仍能读历史并明确拒绝发送（403）。前端在导航新增「对话」页（`data-page="chat"`），气泡按 `recent.json` 渲染、Enter 发送 / Shift+Enter 换行、发送中占位、请求标识随本轮复用（重试回放同一结果）、右侧显示人格版本/修订/模型/存储与整轮提交状态。验证：新增 `tests/test_s05_console_chat.py` **11/11**（HTTP 全链路发送/回放/409/空白与 405、记忆上下文与人格进入 system prompt、瞬时故障 504 后可重试、只读根读历史+拒发、bridge 共用核心、凭据不泄露、离线演示模型自述）；全量 **224/224 通过**（`test_s05_stack.py` 2 项因当前 `.venv` 安装的是不含 UI 资源的旧构建而失败，头部已注明需用已安装 wheel 的解释器运行，与本次改动无关）；`node -e new Function(live.js)` 语法通过。Browser Use 在 `artifacts/chat-accept` 真实点击发送与回车发送各完成一轮，页面气泡、右侧「已提交」计数与 `/api/chat/history` 完全一致，第三轮起回复已带召回记忆。未做：流式回复、宿主侧 C 接线、语音/表现。

管理台模型配置（2026-10-24）：设置页新增「模型接口」面板，可在页面里填接口地址、API Key、对话模型、提炼/反思模型与思考强度，**保存后立即生效、无需重启**，解决首轮运行既没有 `stack.toml` 也无法在浏览器里改环境变量的空白。新增 `service/model_config.py`：`save_model_config`（校验 + 规范化 + 原子写 `local_state/model_config.json`）、`load_model_config`（损坏时 fail closed 并给出 `problem`，不猜默认值）、`apply_model_config`（只覆盖页面填过的非空字段，不动 `stack.toml` 的 timeout/并发/未被提到的档位）、`public_model_config`（不含凭据）。适配器侧 `ModelSettings` 增加 `api_key_stored` 与 `reasoning_effort`：`api_key()` 的解析顺序是「管理台录入 > 环境变量」并由 `api_key_source()` 只报来源；思考强度以 `reasoning_effort` 发送，选「跟随服务端默认」时不附加该字段（不识别它的服务商不受影响）。`HttpModelInvoker.update_settings()` **原地替换**设置而不换实例——核心的提炼/反思/演化服务持有同一对象，换实例会让后台停留在旧服务商；并发上限刻意不在运行时改（换信号量会让新旧两批同时在途，正是 R07 消除的状态），控制台据此说明重启生效。`ChatBridge.reconfigure()` 负责热切换与「真实配置替换掉演示模型」，并在启动路径（`MemoryService.start`）读取同一文件，使页面配的配置**重启后仍在**。凭据边界：只存本机数据根下 `local_state`、POSIX `0o600`、不进 `/v1/export`、不进任何响应（连掩码前缀都不给，短 Key 的前缀就是 Key）、不进日志；只读根能读不能写（403）。路由：`GET/POST /api/model/config`、`POST /api/model/test`，服务模式经 `/v1/model/*` 转发并复用同一写身份校验。前端：设置页表单（URL/Key/两个模型名/强度下拉、保存中禁用、失败保留输入、`测试连接` 按 `kind` 给可读原因如「连不上接口地址」）。验证：新增 `tests/test_s05_model_config.py` **19/19**（Key 优先级与来源、只发送已知强度、invoker 原地更新且不换信号量、规范化与逐字段校验、损坏文件 fail closed、叠加不丢未触碰档位、公开视图无凭据、HTTP 存取与省略 Key 保留 / 显式清除、连接测试返回失败归类且演示模型拒答、真实配置替换演示模型并重绑核心、导出不含凭据、配置跨服务重启保持、只读 403、服务模式同合同与校验）；全量 **243/243 通过**（同上排除 `test_s05_stack.py` 的 2 项 wheel 资源用例）。Browser Use 在隔离 demo 根实际填表保存：页面显示「已配置」「已保存（由管理台录入）」、Key 输入框清空、思考强度为高；`model_config.json` 落盘而 `GET /api/model/config` 与对话状态均无密钥；`测试连接` 对假地址返回 transport 并可读原因，配置后发送对话返回可重试 503（不再退回演示模型）。未做：多套配置切换、并发上限热改、密钥加密存储（当前依赖本机文件权限）。

Windows 一键启动与独占端口（2026-10-24）：仓库根新增 `start.cmd` / `stop.cmd` / `start.ps1` / `启动说明.txt`，双击即可启动或停止；首跑自动 `stack init` 生成配置，已在运行则复用同一实例，成功后打开管理台，缺项按字段逐条列出并说明可在页面「设置 → 模型接口」补。`start.ps1` 负责解释器探测（默认仓库 `.venv`，可 `-Python` 覆盖）、前置检查、状态渲染（storage/chat/background 分项）与失败原因提示。**同时修掉本仓库长期存在的重复实例根因**：`http.server.HTTPServer` 默认 `allow_reuse_address = 1`，而 Windows 上 `SO_REUSEADDR` 允许第二个进程绑定**正在监听**的端口——两者都报成功、连接随机分流，这正是同端口出现多个进程的原因（实测：默认态与 `SO_EXCLUSIVEADDRUSE` 第二次 bind 均报 WSAEADDRINUSE 10048，`SO_REUSEADDR` 则成功）。新增 `service/listening.py` 作为绑定语义唯一实现（Windows 独占 / POSIX 保留 reuse），`webui/server.py` 的 `RinHTTPServer` 与 `service/server.py` 的 `CapacityServer` 统一改用 `ExclusiveAddrMixin`。另修两个同源缺陷：(1) `stack.stop()` 原先只等 `_alive(pid)`，而 Windows 的 venv `python.exe` 是 launcher，`Popen` 记录的是父 shim，它退出后子进程仍占端口，于是 `stop` 报成功却端口未释放、下次启动撞「幽灵冲突」——改为以**端口是否真正释放**为判据（`_port_held`），超时则 `taskkill /T` 收拢整棵进程树；(2) `runtime.package_version` 与日志路径的误报/误显——版本钉在源码树按 editable 解释器放行（仅安装包保留严格校验），日志路径改从状态里的实际 logs 根取。此外 `_ui_resource()` 让管理台静态资源在「已安装 wheel」与「源码树 `webui/designs/tidebell`」两处都能解析，`stack.py` 启动前的资源检查随之复用，使一键启动在开发树可用而不放宽对真实安装的打包校验。验证：新增 `tests/test_s05_port_exclusive.py` **8/8**（平台策略、两个传输层均拒绝第二次 bind、关闭后可重新绑定、`_port_held` 正反例、`_kill_tree` 容错）；全量 **254/254 连续三次通过**（排除 `test_s05_stack.py` 的 2 项 wheel 资源用例，其头部已注明需用已安装 wheel 的解释器运行）。端到端实测：默认路径启动 → 首跑生成配置 → 服务就绪 → `status` 显示运行中与分项能力 → 停止后端口释放且无孤儿进程；服务配置正确读取 `stack.toml`（`https://d1api.xin/v1` 与三档模型名），Key 来源如实报告为环境变量未设置。未做：Linux/systemd 单元与安装脚本（当前只为 POSIX 保留安全的 reuse 语义）。

验收回填：**N02 已通过**（安装包 API/HTML/JS/CSS/图片，仓库外工作目录）；**N01 服务/管理台部分、N03 服务部分、N10 创建/人工编辑冲突/发布部分通过**。其余按工作包继续，未计入通过。新运行入口已在 `F:\game\projectNEKO\companion-runtime\config\stack.toml` 初始化，服务 `http://127.0.0.1:48931` 运行、根内人格为空，等待用户填写；模型端点、summary/correction 档位和凭据变量在该新配置中待填写。下一批先完成 **A 宿主早期目录注入 + C 远程客户端/有界持久待同步队列**，同时补齐 B 的事件/上下文凭据/送达与清理客户端合同，再进入 D—F；真实模型/语音/游戏验收按 N04/N12/N13 执行，旧入口退役仍在全链路验收之后。

S02/S04 复核修复记录（2026-10-24）：七项 P1 已全部修复并有行为测试（`tests/test_s05_review_fixes.py` 17 项，走真实服务装配与 HTTP 调用链）。R01 删除/更正联动：`domain/turn_results.py` 的 `TurnResultCleaner` 接入删除步骤（新增 `turn_results` 步骤，计入 CONTENT_STEPS），`CorrectionTurnInvalidator` 接入更正派生失效；实测 `forget()` 后 `/v1/requests/{id}` 返回 410 且记录正文已清空。R02 退出顺序：`stop()` 先递增代次拒绝新请求、等 `_drain` 排空在途（默认 30s 上限）、再停后台、最后释放根锁；`begin_request` 清 `_drained` 事件（原实现事件初始为 set，导致排空瞬间返回——由测试发现）。R03 范围贯穿：`subject/scene/session_id/client_id` 进入上下文构建、回执摘要与结果查询；`TurnRecord` 新增 `subject`，跨主体查同一 request_id 返回“未知”。R04 稳定业务时间：`ingest_turn(occurred_at=...)` 与记录持久化，重试复用首次值，不再变成 `source_key_reused_with_different_payload`；内容真变仍冲突。R05 上下文与整轮：`/chat` 生成前构建并注入记忆上下文，assistant 回复以 `ai` 消息进入 recent/原文，外部轮次不重复追加。R06 故障恢复：可恢复错误（timeout/rate_limit/transport）只记 `last_model_error` 不关闭能力，配置/鉴权错误才降级；已提交结果回放不受模型可用性影响。R07 并发与循环：适配器改用 `threading.BoundedSemaphore` 并在工作线程内释放，跨事件循环不再报错；超时后底层线程仍占槽位，实测 peak 并发严格等于上限。验证：核心 **213/213**、宿主 bridge **59 passed**、WebUI 6/6、demo 与打包通过（wheel 81 文件）。原复核记录：已有主体实现保留，批次恢复为进行中。临时根与替身模型复现七项 P1：R01 实际删除/更正未完整联动请求结果与上下文失效；R02 在途请求未结束即释放根锁；R03 HTTP 上下文丢失主体/场景边界；R04 入队失败后重试因 `occurred_at` 变化触发来源冲突；R05 `/chat` 未调用记忆上下文且生成回复未进入整轮历史；R06 瞬时模型错误后聊天能力持续关闭；R07 每请求事件循环与共享信号量冲突、超时后底层线程突破并发上限。现有 `tests.test_s02_turns` + `tests.test_s04_service` **38/38 通过**，新增复现场景及修复验收条件见 [standalone-service.md §8.1](standalone-service.md#service-review-20261024)。该复核的代码修复及对应回归已完成；下列历史交付记录按原范围保留。S01、S03 分类/编码、S06 基线状态保持不变。

S01 交付记录（2026-10-23）：新增 `persistence/ownership.py`（`canonical_root`/`acquire_root_lock`/`read_root_lock`，根目录 `.rin_memory.root.lock`），并接入全部可写入口——CLI `run`/`migrate`/`rollback`（`cli.py`，冲突沿用退出码 3 / 错误码 `root_lock_conflict`）、WebUI 可写在线模式（`webui/seed.py` 的 `build_live_core` 取 `mode="console"`，`RinApp.close()` 经 `release_live_lock` 释放，只读模式不取锁）、宿主 `engine_bridge/runtime.py`（`build_core` 先取 `mode="host"`，`reload`/`shutdown`/`set_core_factory`/`set_port_overrides`/`set_config_manager` 释放）。同时修掉方案点名的两处缺陷：角色锁改为 OS 文件锁并停止「内容不可读即清理旧锁」（原实现 `O_CREAT|O_EXCL` 建文件后才写 pid，第二个进程读到空文件即删除在用的锁）；锁记录移入锁定字节之前，使冲突时仍能读到持有者信息。互斥完全由 OS 锁承担，pid/mode 仅作说明。验证：核心 **149/149**（新增 `tests/test_s01_ownership.py` 9 项、`tests/test_s01_console.py` 3 项），其中含真实子进程争抢、异常退出后可重启、大小写/`.`/`..`/尾分隔符别名共用一把锁、空/损坏/他人 pid 锁不可夺取、被拒方不创建角色数据；宿主适配器 **59 passed**（新增 2 项宿主锁生命周期），默认关闭基线行为不变。未做：S01 未覆盖「服务＋服务」中的独立 HTTP 服务端（S04 才有该进程）；锁文件不入迁移备份（`migration.py` 已跳过点文件与 `LOCK_FILENAME`）。

S02 交付记录（2026-10-23）：新增 `persistence/turns.py`（`turn_requests.json`、`TurnRequestStore`、`TurnRecord`/`TurnReservation`、`turn_fingerprint`）与 `domain/turns.py`（`TurnCommitService`、`TurnRequest`、`TurnOutcome`）。实现 §3.2 的 `request_id` 业务幂等键（同键同指纹回放、同键异指纹 `conflict`、删除后 `deleted` 且不回放正文）、§3.3 的提交顺序（reserve→生成→依据复查→入队→journal 提交，`context_stale` 时不提交任何内容）、§3.4 的状态机（pending/committed/failed/interrupted/deleted）与 **journal 权威提交**：整轮记录经 `record_and_save` 写入，新增事件类型 `core.turn_committed` 并在装配时注册回放处理器，崩溃后由 `recover()` 重建记录；重启时 `recover()` 把仍为 pending 的记录标为 `interrupted`（可重试）。删除/更正经 `invalidate_sources()` 清空正文只留标识。镜像去重：同 `turn_id` 的第二客户端请求复用原记录并返回 `replayed`，不产生第二轮。迁移/回滚已登记 `turn_requests.json`（格式检测 + preserve_new 合并 + 信封重建）。验证：新增 `tests/test_s02_turns.py` 18 项——响应丢失回放不重新生成、同键异内容冲突、并发同键 `in_progress`、生成失败可重试且只提交一次、入队拒绝不留正文、依据失效不提交、删除后回放/重试均无正文、journal 回放修复未落视图的提交、回放不复活已删正文、镜像不新增轮次、重启中断可重试、损坏/未知 schema fail-closed、记录有界；**核心 167/167**；B14 迁移演练 **44 checks** 全过，实测 migrate→备份后新增提交→rollback(preserve_new=True) 后 r1/r2 均在、正文完好。未做：`/v1/chat` 与 `/v1/turns` HTTP 端点（属 S04 服务层）、流式回复、公网部署；`TurnCommitService` 的生成器与依据检查仍需 S04 注入。

S03 分类交付记录（2026-10-23）：`classification.py` 新增 `CHAT` 常量与 `classify_message(default=...)`；`domain/extraction.py` 的 `ingest_turn` 把普通聊天入口默认标注为 `chat`，并把 `_recent.append`/`_store_raw` 移到类别闸门之前，使 plugin-only 类别（游戏流水、工具结果、系统通知）也保存整轮原文而只跳过提炼（`kept_in_plugin` 现带 `reason="stored_without_extraction"`）。附带修掉 §3.4 点名的入队断点：`OutboxStore.find_by_source_key` + `_repair_missing_extraction_op`，令「原文已保存、入队前崩溃」的轮次在重试命中 `duplicate` 时补回缺失的提炼任务，且不会重复入队。验证：新增 `tests/test_s03_ingest.py` 9 项（原文去向、不触发模型、删除屏障仍生效、chat 不受旧窗口屏障影响、故障注入后重试恢复、重复不双入队、不可提炼类别不补队列）；核心 **149/149**。

S06 交付记录（2026-10-24）：新增 `tools/s06_samples.py`（8 个脱敏合成样本，覆盖 §6.2 全部失败类型：中文实体与短词、同义表达、指代续接、话题切换/无关抑制、跨主体隔离、跨场景隔离、已删除内容）与 `tools/check_s06_recall.py`（真实核心跑标准召回与被动上下文，输出 `tools/s06-recall-baseline.json`）。**基线结果**（Python 3.11.15 / Windows 10.0.22631 / 12 核 / 顺序执行 / 无向量后端 / 8 案 17 条事实）：Recall@K 均值 **0.857**，硬失败 **0**，已知缺口 **1**（同义表达：BM25 无法把“失眠”匹配到“熬夜”，需 §6.3 扩展或 §6.4 向量才可能改善），**无关注入 0**、**删除泄漏 0**、重复提及 0；专项召回 p50/p95/max = **0.69/2.00/2.08 ms**，被动上下文 = **0.81/1.26/1.31 ms**。新增 `tests/test_s06_recall_samples.py` 6 项把契约固化：语料自洽（期望 id 必须在本案语料内、不得兼任召回与排除、已删不得期望召回）、无关注入为零、删除不复活、Recall@K 不低于已记录基线（唯一允许的缺口必须是 `synonym_paraphrase`）、已声明缺口不得被清空、同案两次结果一致。**定位到两条链路的行为差异并据此建模**：指代补全只存在于被动 `build_turn_context`，显式 `recall_memory` 不做展开（B06 规格 §0.2 的“两条独立召回链路”），因此 `reference_continuation` 明确标注 `path="passive"`，避免用错链路误判。**过程中修正的三处样本缺陷**（都不是实现问题）：把场景限制写成不透明标签而非 `applies_to` 字段导致隔离用例空转；两条事实共用 `episode_id` 使单条删除误测成整段删除；同义用例把 `expect_recalled` 留空，使缺口被当成通过。验证：核心 **196/196**。未做：S07（按样本选择分词/扩展/向量）——是否启动取决于这 1 个同义缺口是否值得付出模型或索引成本。

S04 交付记录（2026-10-24）：新增三个模块——`adapters/model_http.py`（零依赖 `ModelInvoker`：`HttpModelInvoker` 用 `urllib` 在工作线程调用 OpenAI 兼容端点，分档映射、信号量限制并发、逐调用超时、凭据只从环境变量**名**读取且永不记录；失败归类为 auth/config/timeout/rate_limit/transport/format，并标注是否可重试）、`service/memory_service.py`（`MemoryService.start()` 按 §4.2 顺序装配：取 S01 根锁 → 建核心 → `recover()` → 建立就绪状态；`storage/chat/background` 三项能力分开报告；`stop()` 先停后台再释放锁）、`service/server.py`（`MemoryApi` 无传输实现 §3.1 四个端点，`MemoryRequestHandler` 为标准库 HTTP 传输；统一错误信封 `code/message/retryable/request_id`，按 §3.2 映射 400/401/403/409/410/202/429/503/504）。CLI 新增 `serve` 子命令（`--memory-root --character --port --model TIER=MODEL --model-base-url --model-api-key-env` 等）。关键行为：`/context` 只读且不记录提及或送达（测试断言前后 turns/recent/facts 不变）；`/chat` 在能力未就绪时**先拒绝再登记**，不留 pending 记录；`/turns` 在无模型时仍可用（外部生成模式）；同 `request_id` 重试回放已提交回复且不再调用模型；`/v1/requests/{id}` 对已失效结果返回 410 且不带正文；Host/Origin 校验默认只允许回环。验证：新增 `tests/test_s04_service.py` 20 项（能力状态三态、chat 拒绝无残留、外部轮次、后台状态、生成一次并回放、persona 进入 system prompt、模型失败可重试、409 冲突、context 只读、receipt 随人格版本变化、单写者与释放、启动错误码、stop 幂等、HTTP 全端点、错误信封形状、Host 拒绝、适配器解析与错误映射、未配置/缺凭据）；**核心 190/190**。独立安装验收：`.scratch` 内 wheel → 隔离 venv 安装 → 在仓库外目录用 `python -m rin_memory.cli serve` 启动，`/v1/health` 200（storage ready、background running）、`/v1/context` 200、`/v1/turns` 200 committed/durable、`/v1/requests/p1` 200 回放正文，进程正常退出。未做：S05 的 N.E.K.O/WebUI 接入与模式切换；流式回复、公网鉴权/TLS、向量召回（S06/S07）。

S03 中文链路交付记录（2026-10-24）：按 §5.2 的四步顺序完成定位，**结论是核心链路逐层字节保真，损坏不在核心存储层**。样本统一为 `她喜欢乌龙茶🥤，说“下次再聊”——记得提醒我！`（中文＋全角标点＋破折号＋emoji）。逐层实测（`.scratch/check_s03_encoding.py` 9 层全 PASS）：输入 dict → `message_text` → `ingest_turn` → `recent.json` 字节（142 字节，**无 `\u` 转义**）→ `time_indexed_original` 行 → 送入模型的 prompt 原文 → 模型回写的事实文本 → `facts.json` 字节，全部逐字符一致；宿主转接层 `engine_bridge.middleware._to_core_message` 的四种输入形态（langchain `{type,data}`、cross_server `{role,content}`、多模态 list、纯字符串 list）经 `message_text` 后也全部保真。**排除结论**：任何一层出现 `ensure_ascii=True` 才会产生「解码正确但磁盘是 `\uXXXX`」的现象，全仓库存储路径均为 `ensure_ascii=False`，已核查到 `atomic.py`/`journal.py`/`recent.py`/`outbox.py`/`records.py`/`state.py`/`turns.py`。**剩余可疑面**（本轮未复现，需实机触发）：Windows 控制台/管道的显示代码页——用 `PYTHONIOENCODING=cp1252` 起子进程验证过，控制台编码不足只影响打印，**不影响落盘内容**。验证：新增 `tests/test_s03_encoding.py` 3 项（逐层字节一致＋无转义、重开根目录仍一致、CP1252 控制台不影响存储）；核心 **170/170**。未做：如果用户报告的乱码来自真实会话，需要按其现场样本（那段具体文字、出现的界面）再定一次；本轮只能证明核心链路不可疑。

C04 启动记录（2026-10-22）：用户正在开发 `webui/`；已存在主页面、原生 HTML/CSS/JS 及月白/星屑/霓虹设计预览。真实接口联调和完整操作验收按 C04 完成条件记录。本次文档更新保留这些文件的当前工作状态。

本次文档收尾：更新入口与状态口径，新增 C01—C06 范围、自动演化三种模式、管理台 API/状态合同及月白风格建议；实现与验收数字沿用历史批次记录。

C01 交付记录（2026-10-23）：宿主 `app/memory_server/engine_bridge/ROUTE_MATRIX.md` 覆盖 routes.py 全部 64 端点与后台任务（委托核心 15 个 + 管理状态 1 个；宿主保留约 40 个并注明 C02/C05 迁移；明确停用 10 个旧循环与启动期 reconcile/outbox/迁移/post-turn 写任务）。middleware 扩展 corrections POST/retract、无显式 id 删除（deletable_entries 文本解析）、get_persona、companion_events 子集；runtime.py 在 engine 模式跳过旧写循环并接 reload/release generation；generation 守卫改为执行前检查（旧任务不会先落盘）；middleware 首次成功请求后启动每角色维护循环，release 停止；宿主编辑入口/投影回写随 C02。验证：宿主适配器 **47 passed**（B08 25 + C01 5 + C02 5 + flow 5 + C03 7），默认关闭基线 **38 passed**，宿主 `git status` **116**（110 + 6 个新增文件/目录），无 `__pycache__`、未读 nekodata、未启端口。复核缺口（.scratch/c01-c03-review.md）中 generation 前置与 start_maintenance 已修复。

C02 交付记录（2026-10-23）：核心新增 `persistence/persona_projection.py` + `domain/persona_projection.py`（宿主卡一次性导入、卡/发布同步、四消费者 saved/pending/published/adopted/failed+retry、宿主编辑 base_revision 冲突保留 draft、删除/更正失效、权威投影解析、回放），composition/api 接线；宿主新增 `engine_bridge/host_projection.py`（characters.json 语义字段读写回，资源/语音/凭据保留）、`engine_bridge/runtime.py` projection/status/edit/adopt/planner 接口、`main_logic/engine_persona.py` 编辑入口；`memory/persona/persistence.py` engine 模式停止旧 characters.json 反向覆盖；get_settings/new_dialog 用引擎投影 + 宿主工具/语音协议；turn_context 带 persona_version/revision 并在使用后 adopt(chat/active)，sts2 poll 带版本 + planner_pair（mismatch→reissue），realtime ack 后 adopt 留 C05。验证：核心 C02 7 tests、e2e 脚本四消费者 adopted；宿主 C02 5 + flow 5 passed；默认关闭 38 passed。

C03 交付记录（2026-10-23）：`persona_admin.json` 增加单源 mode=off|propose|auto（旧 bool 迁移 false→off/true→propose，auto 只能显式设置）；新增 `persistence/evolution_runs.py` + `domain/evolution_scheduler.py`（证据触发、trigger_key 幂等、duplicate_evidence 去重、冷却/预算、propose、auto accept→revise→publish、失败/重试/重启续跑、冻结/关闭拦截、已接受发布重试）；MaintenanceService 增加 evolution_trigger 步骤；composition 装配 scheduler 并传入 maintenance；宿主新增 `engine_bridge/evolution_admin.py`（status/mode/evaluate/retry）与维护循环自动启动。验证：核心 C03 12 tests + self-check 50 checks；e2e 脚本 off→propose→auto→发布→四消费者 adopted→重启保持（final revision 4/version 8b8b63faf5650f99d7f47dd3）；宿主 C03 7 passed；全量核心 **119/119**；`uv build` 成功；默认关闭基线 38 passed。剩余 C05：实时 ack 实机、主服务↔memory_server 编辑端到端、迁移钩子、性能/副本演练。

C04 交付记录（2026-10-23）：新增独立测试前端 `webui/testbed/`（原生 HTML/CSS/JS，无构建/外部依赖，同源 fetch）与 `webui/tests/test_testbed_api.py`（6 tests）、`.scratch/check_c04_webui.py`；现有 `webui/index.html`、`webui/assets/**`、`webui/designs/**` 未改动（美术继续由用户推进）。`webui/server.py` 仅加法 API：engine/status、evolution mode（off/propose/auto，旧 enabled 映射保留）、persona consumers/pending/failed/stale + adopt/sync、cleanup/delete、evolution evaluate/retry、/testbed 静态路由与 405 表。测试前端覆盖：状态头/刷新/导出/只读/离线、概览、时间流+搜索、事实/反思、人格卡编辑/冻结/恢复/历史/投影、发布 target/rollback/consumers/adopt、演化模式/提案/接受/拒绝/撤回/evaluate/retry、清理更正/按来源删除/删除列表、原始 JSON；写操作生成 request_id 并显示 pending/failed/重试与错误 code/message。验证：`webui.tests.test_testbed_api` 6/6；`.scratch/check_c04_webui.py` exit 0（看来源 67 events/13 facts→改卡 r7→发布→adopt→提案 accept/withdraw→evaluate/retry→删除 fact_coffee→search/export→重启 revision 8、deletions 3 保持）；核心全量 121/121。未做：浏览器自动化/E2E、宿主鉴权/CSRF（服务仍 loopback），实机 UI 与美术完善待 C06。

C05 交付记录（2026-10-23）：核心 `build_memory_core(migration_hooks=())` 接入 MaintenanceService（默认空不改变行为）；宿主 engine_bridge/runtime.py 在 `COMPANION_MEMORY_MIGRATE_ON_START=1` 时注入 `make_migration_hook(memory_root, backup_root=<root>/../migration-backups)`，默认关闭，新增宿主 test_engine_bridge_c05.py（2 passed，默认不迁移/开启后一次且幂等）。`.scratch/check_c05_rehearsal.py` 15 checks PASS：backup sha256→migrate→verify→写切换后事件/事实/对话/删除/tombstone/更正→rollback(preserve_new=True)→verify，新数据保留、删除不复活、重启两次树哈希一致；报告 `.scratch/c05-rehearsal.json`。`.scratch/check_c05_integration.py` exit 0：临时根 webui HTTP 11 读端点 200、revise→publish→sync→四消费者 match、propose→accept、delete completed、只读 live 写 403；`.scratch/check_c05_perf.py`：turn_context p50/p95/max=25.3/27.1/37.8ms、recall=22.1/25.5/28.3ms、0 失败、前台模型调用 0；webui snapshot HTTP p50/p95/max=617.8/662.2/678.8ms（管理台刷新风险记 C06 观察项）。`.scratch/check_c05_versions.py` 固定版本 0.1.0.dev0、wheel 374,956B sha256 4184e300…、sdist 523,272B（pyproject 增加 sdist exclude，去掉 .scratch/webui 设计资源后从 33MB 降到 0.5MB）、宿主 bridge/core/webui 分组哈希；`.scratch/c05-a01-a16.md` 逐项映射。验证：核心 121/121；宿主适配器 49 passed（B08 25+C01 5+C02 5+flow 5+C03 7+C05 2），默认关闭基线 38 passed；宿主 git 117；未读 nekodata、未启长期端口。剩余实机项（语音/首字首音/Live2D/游戏/浏览器/真实模型）归 C06。

C06 链路修复记录（2026-10-23 19:30，用户授权在真实服务上排查）：切换后引擎虽 enabled，但真实对话**完全没进引擎**。逐个定位并修通五处缺陷：

1. **`/cache` 从未被委托**（宿主 `engine_bridge/middleware.py`）。宿主每回合结束写记忆走 `POST /cache/{name}`，该路由从未进委托表（`ROUTE_MATRIX.md:37` 登记为 C05 待办但未实现），导致对话只写旧链路、`events.ndjson` 里 `core.source_recorded`=0、WebUI 时间流永远空。修复：新增 `cache_turn` 委托到 `core.extraction.ingest_turn`，`inline=False` 让抽取 op 只入 outbox 由维护循环后台执行；来源键优先用宿主 `mention_event_id`，否则用批次 sha256（重试=duplicate）；theater 小剧场与 visit 日记通过 `_is_special_cache_batch` 回退旧链路（它们有各自的幂等/撤回/归档语义）。

2. **委托请求固定多花 ~750 ms**（核心 `persistence/deletions.py`、`corrections.py`、`roots.py`）。宿主日志两次记录 `[turn-context] memory provider missed the 1.00s budget`，超时导致 `persona_version` 传不到宿主、adopt 永不触发。cProfile（5 次 projection.status 共 2.17 s）定位：单次 status 触发 950 次 `version()`，每次全量归一化 28 KB 删除库（87,400 次 `_normalize_tombstone`）占 1.12 s；`roots._is_within` 每次 `path.resolve()`（NTFS `_getfinalpathname`）3,860 次占 0.62 s。修复：新增 `persistence/version_cache.py`（`VersionReader` + `file_signature`），按 `(st_dev, st_ino, st_mtime_ns, st_size)` 缓存；未命中仍走原严格 `_normalize_doc`，损坏/未知 schema 照常抛错且不缓存，stat 失败或读取期变动不缓存；roots 缓存 resolve 结果与按角色名的 character_dir。实测 turn_context **1250 ms → 85 ms**、projection.status 190→11 ms、recover#2 180→1.4 ms、state.read 25→1.9 ms；核心 unittest 121→**128/128**，全量耗时 29.9 s→11.5 s。

3. **宿主消息格式未转换**（宿主 `middleware.py`）。`cross_server` 发 `{"role":"user","content":[...]}`，核心 recent.json 合同严格要求 `{"type":"human","data":{"content":...}}`；`extraction._is_human_message()` 只认 type，于是用户文本取到空串、抽取窗口过滤为空（`ExtractionOutcome("empty", reason="no_user_messages")`）。实测 `core.source_recorded` 的 text_len 为 0。修复：新增 `_to_core_message()` 规范化（已是 langchain 形状原样透传含 metadata，未知 role 映射 human）。验证：role 格式 POST 后 text_len=16。

4. **模型调用传参类型错误**（宿主 `engine_bridge/ports.py`）。只有一条消息时传 `host_messages[0]`，而宿主 `_normalize_messages` 直接迭代入参 → `TypeError: 'SystemMessage' object is not iterable`，所有引擎模型调用（事实抽取、反思合成）以 `transport:TypeError` 失败。修复：始终传 list。

5. **summary 档位指向被服务端拒绝的免费路由**（宿主配置，非引擎回归）。修完 4 后错误变 `transport:BadRequestError`：`400 {"error":"Invalid request: you are not using Lanlan. STOP ABUSE THE API."}`。档位实测：`conversation`/`game_main` = deepseek-v4.1-flash + 51 位真实 key → OK；`summary`/`correction` = free-model + 11 位占位 `free-access` → 400。根因 `summaryModelProvider="follow_assist"` → `utils/config_manager/core_config.py:1555` 解析到 `OPENROUTER_API_KEY` → `config/api_profiles.py:111` free profile 给的是占位符。**宿主旧链路同样全线走 summary 档**（`memory/facts.py:6849`、`memory/fact_dedup.py:1086`、`memory/reflection/synthesis.py:322`、`memory/recall.py:666`），所以旧链路的事实抽取/反思/召回增强一直是坏的（facts 停在 2026-10-22）。修复：`summary`/`correction` 改为 custom 指向 conversation 档已验证 provider；备份 `nekodata/config/core_config.json.bak-summary`。

端到端验证（真实服务）：重启后 `POST /cache/小铃` → `{"status":"cached","count":2,"engine":{"status":"accepted","source_key":"host:小铃:private:tier-probe-004"}}`；等 100 s（维护循环 60 s）→ facts.json **1 条 → 3 条**（新增「喜欢喝乌龙茶」「晚上常熬夜」）；`chat` 消费者 **adopted**（`receipt=turn:chat`, 19:22:50）；`core.source_recorded` 10 条；WebUI `timeline` 0→10、`facts` 1→3。验证：核心 **128/128**、宿主适配器 **57 passed**（含新增 cache 委托/格式转换/theater 回退 6 个测试）、宿主 `git status` 117 未变。记录见 `.scratch/c06-switch.md` §8—§11。

剩余 C06：A01—A16 实机验收（文字/语音/打断、Live2D、游戏、主动、手改/演化、删除后重启、性能与回滚），当前状态见 [closeout §6](closeout.md)。`active`/`game`/`realtime` 三消费者仍 `saved`，需各自真实链路触发；自动演化仍 off。

C06 切换记录（2026-10-23，用户授权）：宿主 .venv 安装固定 wheel；停服单写者窗口后以 `COMPANION_MEMORY_ENGINE=1` 启动；首个委托请求自动导入真实 characters.json（小铃，19 段，card_revision=1，version=card_8d654ff831d8c007）；`turn_context` 返回 persona_version/revision，`get_settings` 返回 1087 字符引擎投影文本；维护循环 running/writeback running，last_status=completed，evolution_trigger=skipped(off)，legacy_writers_allowed=false；服务 48911/48912/48915 健康。迁移钩子首轮失败：DataFormatManifest 检测到 `outbox.ndjson` 第 2 行缺 op_id/type（corrupt），按设计拒绝迁移且未写 manifest/备份；已关闭钩子（migrations skipped），数据未修改。切换中发现并修复宿主缺口：生产从未触发 persona_import（已改为首个委托请求自动导入、幂等）、真实 v2 中文 profile + `_reserved` 结构此前不识别（已支持导入/写回并保留资源字段）、engine/status 增加维护可观测字段。验证：宿主适配器 51 passed、默认关闭基线 38 passed、宿主 git 117；记录见 `.scratch/c06-switch.md`。回滚：停服后去掉引擎环境变量重启即回旧链路。剩余：A01—A16 实机验收（语音/打断、Live2D、游戏、主动、手改/演化、性能）与 WebUI 正式页面联调，手动清单见 closeout §6，等用户 webui 完成后一起验收；自动演化当前 off。

B00 交付记录：独立本地仓库、零运行依赖的 Python 框架、内存示例、3 个 unittest；sdist/wheel 构建及隔离安装示例通过。没有真实持久化、模型调用、自演化或宿主接入。当前文件保存在项目工作树，未创建初始提交。

B01 交付记录：在 design.md 新增 §17（D0 兼容合同冻结与迁移清单）：宿主工作树基线（HEAD 4c510926 + 106 项未提交改动）、持久数据清单（谁写/谁读/恢复/删除/幂等字段）、事件日志 17 类型/7 处理器与 outbox/cursors/启动恢复、路由与响应合同、20 项后台任务、角色/数据根/维护模式/云存档许可生命周期、来源/主体/场景/版本映射、9 个核心操作与 9 个端口冻结、逐模块迁移分类、测试与合成样本映射、最小验证与基线方法、14 项待确认与 B02 输入。只读调研，未改宿主/插件、未写正式数据、未新增核心代码。验证：rin-memory `uv run python -m unittest discover -s tests -v` 3/3 通过、`uv run python -m rin_memory` 正常；宿主侧只读核对 38 个纯单测与 71 个 Node 前端用例通过（未改宿主，git status 保持 106 项）。工作树：F:\game\projectNEKO\rin-memory（无初始提交）。下一批：B02，按 §17.13 的 B02 输入执行。

B02 交付记录：新增 `persistence/` 存储层与 `adapters/durable.py`。基础层：原子 JSON/文本写（临时文件+fsync+replace）、schema 门禁、存储根与角色名校验、写许可、时钟、事件快照/指纹。持久存储：位置哨兵事件日志与回放、outbox、cursors、来源账本、请求幂等、按 subject 的 scoped tombstone、facts/archive 裸数组原始视图、SQLite time_indexed 原始/压缩表+FTS5+schema_meta。`DurableEventStore` 构造时注册 `core.source_recorded` 回放处理器，首次写入前 reconcile，journal 落盘且账本提交后才返回 `durable=true`。修复 §17.4 的“哨兵无条件前进”限制：哨兵改为 position，仅在与新条目连续时前进，未应用尾部不再被静默跳过；torn 尾行读侧忽略、写侧原子自愈。损坏/未知 schema 抛 `CorruptStoreError`/`InvalidSchemaError`（带 path/code）且不覆盖原文件；所有写入经注入的 `WritePermit`。验证：`uv run python -m unittest discover -s tests -v` 15/15 通过（新增 12 个用例：journal 3、ledger 3、views 2、端到端 4；超过默认 3 个上限，原因见 design.md §17.14.6）；`uv run python -m rin_memory` 输出内存与临时数据根两段，后者 `durable=true`、重试与重开均 `duplicate`；核心包无宿主导入，未读写 `nekodata`、未改宿主/插件。工作树：F:\game\projectNEKO\rin-memory（无初始提交）。下一批：B03，按 §17.14.5 的 B03 输入执行。
B03 交付记录：新增 `invalidation.py`、`event_sources.py`、`persistence/deletions.py`、`persistence/corrections.py`、`persistence/directives.py`、`domain/barrier.py`、`domain/deletions.py`、`domain/corrections.py`、`adapters/composition.py`，并扩展 `contracts.py`、`adapters/durable.py`、`api.py`、`__main__.py`。实现 `memory_deletions.json`/`memory_corrections.json`/`user_directives.json` 宿主原格式的严格读写（损坏/未知 schema fail closed，不覆盖）；删除先登记屏障（journal `memory.deletion_registered` → 视图 → 缓存刷新）再执行 `resolve/facts/reflections/persona/recent/cache/plugin` 步骤，entry 不删原文、episode 删 timeindex 会话行，插件离线保持 pending；更正先记录版本再失效 facts/归档并支持派生失效器注册，读侧隐藏被删内容；屏障统一拦截事件写入/回放、账本重建、排队 outbox op 与在途结果（BarrierSnapshot/assert_fresh）。决策：更正读侧参与删除、提及限制不参与删除、三个存储坏读 fail closed、retract 保持宿主语义（不恢复旧事实）。验证：unittest 29/29（B03 新增 14：deletion 4、correction 4、barrier 3、directives 3）、三段示例（第三段更正→删除→重放被拒→重开仍生效）、uv build 成功、核心无宿主导入；数据只在系统临时目录，未读写 nekodata、未改宿主/插件。工作树：F:\game\projectNEKO\rin-memory（无初始提交）。下一批：B04，按 §17.15.5 的 B04 输入执行。
B04 交付记录：新增 `persistence/recent.py`、`classification.py`、`persistence/gates.py`、`domain/extraction.py`、`domain/compression.py`、`domain/recent.py`，并扩展 `ports.py`（ModelInvoker）、`adapters/composition.py`、`__init__.py`、`__main__.py`。实现 recent.json 宿主原格式（坏读 fail closed、压缩锚点+屏障、episode/entry 两种删除范围）；内容分类按实测 9 类与优先级路由，plugin-only 类不抽取；两代内容闸门与旧窗口屏障只读解析；ingest 先 durable 事件再写 recent/timeindex 原文，outbox `extract_facts` 记录处理位置，OFF 模式内联抽取；模型调用经注入端口（tier/timeout/max tokens），失败 `None` 可重试、`[]` 为成功空窗口，失败不丢原文；事实按 `sha256(text)[:16]` 去重、自述 `ai_disclosure` 可被用户印证单调升级、窗口来源键删除则丢弃晚到结果；压缩在锁外调模型、提交前重查 anchor 与屏障。验证：unittest 39/39（B04 新增 10：recent 3、classification 3、extraction 4）、四段示例（第四段 ingest→facts→压缩闭环）、`uv build` 成功、核心无宿主导入；数据只在系统临时目录，未读写 nekodata、未改宿主/插件。工作树：F:\game\projectNEKO\rin-memory（无初始提交）。下一批：B05，按 §17.16.5 的 B05 输入执行。
B05 交付记录：新增 `persistence/shards.py`、`persistence/reflections.py`、`persistence/persona.py`、`domain/reflection.py`、`domain/persona.py`，扩展 `domain/deletions.py`（删除前解析并持久化派生目标的全删/收窄两档）、`adapters/composition.py`、`api.py`、`__init__.py`、`__main__.py`。实现 reflections.json/surfaced.json/synth_backoff.json/reflection_archive/、persona.json/persona_corrections.json/persona_archive/、growth_uses.json 宿主原格式（坏读 fail closed 不覆盖、未知字段原样往返、permit+屏障 write_guard）；反思合成（确定性 id `ref_<sha256(sorted source ids)>`、全量未吸收集合退避键、失败 backoff/死信不写、模型前后 BarrierSnapshot 检查与 stale 丢弃）、证据读时衰减（30/180 天半衰期、protected=inf）、三条晋升路径（分数 1.0/2.0、强力记忆关闭 7/7 天、growth min_support=2+证据旗标）、surfaced 送达记录/冷却与 feedback 证据增量、归档先写分片后移出主视图、growth 候选按 growth_key 合并/usage 幂等/撤回联动派生人格、删除清理器（整行删除/部分收窄+needs_rebuild/surfaced 与归档副本/派生人格撤回）与更正失效器（withdrawn、invalidated_sources、persona invalidated+suppress+version_history）、reflection.*/persona.* 回放处理器（构造时注册先于写入、失败抛错暂停哨兵）。验证：`uv run python -m unittest discover -s tests` **60/60 通过**（B05 新增 21：存储 6、反思域 11、人格域 4；超过默认 3 个上限的原因见 design.md §17.17.6）；`uv run python -X utf8 -m rin_memory` 五段示例（第五段：事实→反思→证据→晋升→成长→删除收窄→growth 撤回→重开保持）；`uv build` 成功；核心无宿主导入；数据只在系统临时目录创建，未读写 nekodata、未改宿主/插件。工作树：F:\game\projectNEKO\rin-memory（无初始提交）。下一批：B06，按 design.md §17.17.5 的 B06 输入执行。
B06 交付记录：新增 `domain/recall.py`（RecallSettings + RecallService + BM25/CJK 分词/RRF/渲染/时间窗/淡出/mention/episode 纯函数）与 `ports.py` 的 `TokenCounter`/`EmbeddingProvider` 端口；扩展 `persistence/corrections.py`（render_block 接注入 counter）、`domain/corrections.py`（token_counter）、`adapters/composition.py`（装配 RecallService 并注入 token_counter）、`api.py`（build_context 改用真实召回；新增 build_turn_context/recall_memory/recall_by_time）、`__init__.py`、`__main__.py`（第六段召回示例）。实现候选池 facts+reflections+archive+persona-left-out、BM25+可选 cosine+RRF 融合、无向量降级、scene→subject→失效→屏障过滤、被动 turn_context 校准阈值与预算（50..5000ms、render reserve、max_context_tokens 1..4000 行裁剪）、指代续接与 episode 背景、fading/mention 常量、结果 status 化不抛异常、读取期间删除/更正版本前进标 superseded。验证：unittest **68/68 通过**（B06 新增 8：排序/融合 2、读侧失效 1、指代/episode 1、预算与端口 1、成长块 1、时间召回/降级 1、引擎 build_context 1；超过默认 3 个上限的原因见 design §17.18.6）；`uv run python -X utf8 -m rin_memory` 六段示例（第六段显式召回 hybrid+source_keys、被动上下文命中 2 条、新话题 miss、指代续接带 episode、更正后读侧过滤但更正块可见、时间窗召回）；`uv build` 成功；核心无宿主导入；数据只在系统临时目录创建，未读写 nekodata、未改宿主/插件。工作树：F:\game\projectNEKO\rin-memory（无初始提交）。下一批：B07，按 design §17.18.5 的 B07 输入与 `.scratch/b07-state-spec.md` 执行。
B07 交付记录：新增 `persistence/state.py`、`domain/state.py`、`domain/outbox_dispatch.py`、`domain/maintenance.py`、`cli.py`，扩展 `adapters/composition.py`、`api.py`、`__init__.py`、`__main__.py`、`pyproject.toml`。实现 companion_state.json 宿主原格式（schema/version/state{thoughts,moods,concerns,progress}/processed_events/requests/lease，8 ops、有界字段、坏读 fail closed、journal 回放）；StateService（租约→request_id 幂等→base_version→版本+1→事件+视图，冲突以回执返回；internal 写入不受租约阻塞；mood 6h 衰减读时计算）；OutboxDispatcher（bulk/per-op 注册、extract_facts 批量、未知 op 保留 pending、失败 attempts/死信）；MaintenanceService（固定 10 步 reconcile→outbox→migrations→synthesize→promote→archive→persona corrections→growth reconcile→删除 pending/failed retry→可选 compaction，单步异常隔离、start/stop 单任务无残留）；CLI 独立入口（demo/run --memory-root --character --once|--loop，单写者锁+路径校验，退出码 0/2/3/1）。验证：unittest **76/76 通过**（B07 新增 8：状态 3、outbox 1、维护循环 1、引擎 API 1、CLI 2；超过默认 3 个上限的原因见 design §17.19.6）；七段示例（第七段：租约+状态提交 version=1、6h 后情绪强度 0.50 且版本不变、outbox processed=1/pending=0、维护 10 步 ok/skipped、删除后 retry ok、重开保持）；CLI once 退出码 0、持锁并发退出码 3、nekodata/Documents 拒绝退出码 2；`uv build` 成功；核心无宿主导入；数据只在系统临时目录创建，未读写 nekodata、未改宿主/插件。工作树：F:\game\projectNEKO\rin-memory（无初始提交）。下一批：B08，按 design §17.19.5 的 B08 输入与 `.scratch/b08-host-adapter-spec.md` 执行（至此 D1 完成）。
B08 交付记录：宿主仓库新增 `app/memory_server/engine_bridge/`（`ports.py` 实现 HostStorageRoots/HostClock/HostWritePermit/HostModelInvoker/HostTokenCounter/HostEmbeddingProvider；`runtime.py` EngineRuntime 单例按角色 build/recover/release/reload、generation 守卫、每角色 maintenance start/stop、`COMPANION_MEMORY_ENGINE=1` 开关与 `COMPANION_MEMORY_ENGINE_ROOT` 隔离根；`mappers.py` 核心回执↔宿主 wire；`middleware.py` 仅 enabled 时拦截约定路径并委托、其余原样放行、异常结构化降级），并在 `app/memory_server/runtime.py` 的 FastAPI app 创建后加 5 行安装钩子（唯一既有文件改动）。委托端点：turn_context、companion_state GET/POST+lease、companion_events 子集、corrections GET、deletions list/typed create/retry/receipt、query_memory；其余路径 fallback 旧链路。默认关闭，既有宿主行为不变。验证：`pytest tests/unit/test_engine_bridge_*.py -q` **25 passed**（enabled/disabled、turn_context+state 块、state/lease 409、events 子集、corrections、deletions、query_memory、异常降级无 5xx、release/reload generation）；宿主既有基线 38 passed；宿主 `git status --porcelain` 110（原 106 + engine_bridge 目录 + 3 个测试文件），未读 nekodata、未启动服务、未切换正式目录。遗留（B09）：POST /corrections 文本解析语义、无显式 id 删除、get_persona/get_settings/new_dialog、events 其余类型仍走旧链路；宿主 venv 需安装 rin-memory 包（B14 打包/安装演练处理）。工作树：F:\game\projectNEKO\rin-memory 与 F:\game\projectNEKO\N.E.K.O（宿主改动仅新增文件+1 处钩子）。下一批：B09，隔离集成验收与宿主既有回归（见 .scratch/b09-baseline.md）。

B09 交付记录：隔离环境兼容验收，不修改宿主、不读 nekodata、不启端口。宿主纯单测必跑 38 passed；26 个定向回归文件 25 个全绿 506 passed，唯一失败 `test_outbox_wiring::test_concurrent_post_turn_tasks_keep_their_recorded_language`（29 passed/1 failed，基线即失败，非本次引入）；B08 适配器 engine enabled 集成 25 passed（TestClient+临时根+假 core/模型）；前端 `node --test tests/frontend/live2d_audio_ownership.test.cjs` 71 pass；隔离构造 `.venv`+`PYTHONDONTWRITEBYTECODE=1`+`-p no:cacheprovider`+仓库外 basetemp+conftest 真实根重定向。只读核对：宿主 `git status --porcelain` 前后 110、`__pycache__`/`.pyc` 0、48912 监听 0、`COMPANION_MEMORY_ENGINE` unset。覆盖映射与未验证项见 `.scratch/b09-baseline.md`、`.scratch/b09-integration.md` 与 design §17.21。未验证：无运行中宿主/游戏/音频/浏览器，实时语音、Live2D 实机、游戏 poll 实机与 turn_context p95 未测；宿主 venv 未装 rin-memory（B14）。下一批：B10 当前人设卡与人工编辑。

B10 交付记录：新增 `persistence/persona_card.py`（`persona_card.json` schema 1、initial/active revision、sections+history、坏读 fail-closed、permit+删除屏障、`persona_card_archive/`、投影）与 `domain/persona_card.py`（`PersonaCardService`：一次性导入、base_revision CAS 局部 add/replace/merge/delete、冻结只约束自动修订、restore_initial 保留历史、删除清理器/更正失效器、`core.persona_card_revised` 回放）；`adapters/composition.py` 装配卡片并把清理器/失效器并入删除与更正链；`api.py` 的 `get_profile/build_context` 改读卡片并新增 persona card 操作。新增 `tests/test_b10_persona_card.py` 6 测试；B10 当批 82/82（B10 新增 6；B12 后累计 94/94、B13 后 100/100）；八段 demo 第 8 段（当前已扩为十一段）显示导入/修订/冻结拒绝自动修订/来源删除移除投影/重开保持；`uv build` 成功；核心无宿主导入，数据只在临时目录。宿主 characters.json 尚未切换读投影（B11/B13 与 B08 适配器接线）。下一批：B11 人格发布与版本一致性。

B11 交付记录：新增 `persistence/persona_versions.py`（`persona_versions.json`、`version_token=sha1('<revision>:<text>')[:24]`、current/published/consumers/history、permit+屏障失效、历史≤32+归档）与 `domain/persona_publish.py`（`PersonaPublishService`：publish target=next_turn/realtime/game、current、consume match/mismatch/superseded/unavailable、pair_planner matched/reissue、invalidate_for_deletion、rollback allow_consumed、`persona.version_published` 回放）；composition 装配并接入删除/更正失效链；api 增 publish/current/consume/pair_planner/rollback。新增 `tests/test_b11_publish.py` 6 测试；B11 当批 88/88（B12 后累计 94/94、B13 后 100/100）；九段 demo 第 9 段（当前已扩为十一段）显示三种 target 发布、realtime 旧消费者 superseded、planner matched/reissue、rollback 后重开保持；全量 94/94；核心无宿主导入。宿主实时 session.update/instructions ack、游戏 poll 接线与实机验证待 B15 环境。下一批：B12 有依据的自动修订。

B12 交付记录：新增 `persistence/evolution.py`（`evolution_proposals.json`、提案≤64、permit+屏障、终态不复活、坏读 fail-closed）与 `domain/evolution.py`（`EvolutionService` 默认关闭；证据收集 facts/reflections→选开放段落→ModelInvoker purpose=persona_evolution_proposal 预算/timeout→严格 JSON→确定性校验→只写提案不写卡；request_id 幂等；get/list/withdraw/expire_stale；`core.persona_evolution_proposed` 回放）；composition 装配默认证据提供者与 `evolution_settings`；api 增 evolve_persona（async）/list/get/withdraw/expire。新增 `tests/test_b12_evolution.py` 6 测试；十段 demo 第 10 段显示模型仅 2 次调用、提案写入但卡片 revision 不变、request_id duplicate、冻结段落拒绝；全量 94/94；核心无宿主导入。审核/接受/自动开关/管理接口属 B13。下一批：B13 演化管理与撤回/删除闭环。

B13 交付记录：新增 `persistence/persona_admin.py`（`persona_admin.json` schema 1、auto_evolution 开关、decisions≤64、permit+屏障、坏读 fail-closed）与 `domain/persona_admin.py`（`PersonaAdminService`：提案审核 list/get/accept/reject/withdraw、accept 经 `PersonaCardService.revise` 做 base_revision/冻结/来源校验、revisions/diff/rollback_to_revision 保留历史、auto_evolution 开关持久化默认关闭、删除清理器/更正失效器、`core.persona_admin_decision` 回放）；composition 装配并接入删除/更正链；api 增管理面板方法。新增 `tests/test_b13_admin.py` 6 测试；全量 100/100；十一段 demo 第 11 段显示 accept 后 revision 1→2、reject 提案、diff 仅 tone、rollback 恢复且 revision 3、开关重开保持；核心无宿主导入。宿主管理面板/删除引擎覆盖与发布/游戏接线待 B14/B15 与适配器扩展。下一批：B14 发布包/迁移/回滚演练。

B14 交付记录：新增 `src/rin_memory/migration.py`（24 类存储格式清单与版本检测，未知/更高版本拒绝并带恢复提示；MigrationPlan 步骤 detect→preflight→backup→apply→verify，失败自动回滚；backup 写 sha256 manifest；rollback(preserve_new=True) 合并切换后删除/更正/反思/人格/对话/事实/事件并报冲突；verify；make_migration_hook）与 `cli.py` 扩展 `migrate --memory-root [--backup-root] [--dry-run]`、`rollback ... [--no-preserve-new]`、`package-check`（保留 demo/run 与退出码 0/2/3/1）。自检 `.scratch/check_b14_migration.py` 44 PASS；Lead 复核临时根 migrate dry-run/migrate/rollback 均 exit 0 且 backup_verified=true；`package-check` ok（wheel 347,469 B/68 文件，隔离 venv 安装后 import+demo 退出 0，离线无网络）；全量 100/100、十一段 demo、`uv build` 成功、核心无宿主导入；宿主 git 110 未改、未读 nekodata、未启服务。未执行：正式用户数据切换、真实副本停写/单写者切换、宿主侧迁移钩子接线（需部署授权）。

B15 交付记录（待条件）：未执行正式切换与完整实机验收；缺失项：用户部署授权/维护窗口、运行中宿主/游戏/音频/浏览器、宿主 venv 未安装 rin-memory、实机首字/首音与真实模型条件性能、真实数据副本回滚证据。C05 已提供隔离集成、核心 p95 测量、迁移钩子与副本回滚演练；C06 按 closeout 在授权后执行。已具备证据：核心 100/100、十一段 demo、uv build、B14 迁移/回滚演练、B08 适配器 25 passed + 宿主默认关闭 506 passed + 前端 71 pass + git 110。验收映射与回滚草案见 `.scratch/b15-switch-plan.md`；恢复原则见 design §17.27.3。



四个阶段的完成条件（当前进度见上表）：

- **B07：独立核心可用。** 不启动 N.E.K.O，也能完成真实记忆流程及更正/删除。
- **B09：N.E.K.O 兼容接入完成。** 在隔离集成环境中保持现有角色、聊天、语音、表情、Live2D 和游戏合同，自动改卡仍关闭。
- **B13：自演化功能完整。** 改卡、发布、查看、冻结、撤回和来源删除能够协同工作。
- **B15：首版项目交付。** 正式环境完成切换与验收，并具备保留新数据的回滚方式。

**2.3 QQ/DSH 独立接入台账（Q01—Q06）**

2026-10-25 用户确认将原 AstrBot 路线替换为 **现成 QQ bridge → DSH 陪伴角色 → 原生后台委派执行会话**，随后指定先在 Windows 本机开发。详细交付与验收见 [QQ 接入计划](server-qq-dsh-plan.md)。以 TomoyoNatsume/dsh-qq-bridge 为基座，复用当前记忆服务与文字接入成果；本批独立于 N.E.K.O 宿主工作包。当前已开始桌面版适配与记忆插件开发，实施状态如下。

| 工作包 | 交付 | 状态 | 完成条件 |
|---|---|---|---|
| Q01 | 基座、QQ 通道与兼容版本验证 | 进行中 | Windows 桌面版 `0.2.1-rc.1` + bridge `1.0.6-neko.1` 已加载，用户已确认官方 QQ 私聊正常回复；恢复与委派继续验收 |
| Q02 | DSH 记忆插件 | 进行中 | 已有上下文、轮次提交、持久补交与清理屏障原型，3 项行为检查及真实核心 HTTP 合同通过；后续按 §2.4 的 DM01—DM06 完成 rc.3 共存、配置、人格、更正删除与真实 QQ 联调 |
| Q03 | 陪伴与执行角色分工 | 未开始 | 原生后台委派、持久任务索引、查询/取消/补充/继续，执行不占住 QQ 聊天 |
| Q04 | 可靠性、发送回执与历史清理 | 未开始 | 聊天保存/发送恢复与历史清理由 §2.4 专项先行实施；任务调度断点核对与通知可靠性在 Q03 后推进 |
| Q05 | Linux 安装与部署配套 | 未开始 | 新根、固定包与配置、systemd、权限、启停和恢复可用 |
| Q06 | 真实 QQ 端到端验收 | 未开始 | 文字聊天、记忆演化、真实执行、通知、故障恢复及范围校验形成闭环 |

2026-10-25 本机开发：用户改为 Windows 优先，记忆插件创建于 `F:\game\projectNEKO\dsh-rin-memory`，bridge 副本位于 `E:\dsh\plugins\dsh-qq-bridge`。适配实际桌面版 `0.2.1-rc.1`：更新已存在的 Host/Client peer，移除两个不应作为运行依赖声明的 Client 纯包，设置迁至 volatile Config/configForms，预设改为 bundle 原生声明，取消启动时修改旧 web profile/旧 preset 目录。bridge 构建、消息形态/客户端包/设置生命周期 6 项和已有配对 1 项通过；用户已通过桌面插件管理安装本地包，页面显示 bridge 与 preset 两个组件运行。新增 `npm run pair:official` 只获取主人 OpenID，不修改 profile；账号与 QQ 配置由用户自行操作。记忆插件 3 项隔离行为检查通过，并在临时核心实例完成 context/reserve/turns/used 全部 HTTP 200、仅保存 1 轮与队列排空；未启用日常桌面记忆插件、未修改日常角色数据。Q03/Q04 的完整角色分工、DSH 历史清理和 QQ 传输回执继续按计划推进，Linux 部署后置。

QQ 无响应修复（2026-10-25）：用户已配置官方私聊。只读校验 QQ 凭据和网关发现均为 HTTP 200，定位到兼容层误用普通 Schemastery：元数据可编辑，但 Host 实际得到普通对象，保存后未更新运行中的配置。改用 `@deepseek-ai/schemastery@3.18.5` 的 `.volatile()`，补强已有设置测试断言真实、冻结的引用；构建与设置/客户端包 2 项通过，使用桌面版冻结运行时的隔离探针验证配置引用为函数、关闭→启用实际启动连接。用户按其偏好自行重启桌面版并验收 QQ 回复；未代发 QQ 消息、未放宽白名单或改写其凭据。

QQ 预设必填项修复（2026-10-25）：重启后用户已收到 DSH 返回的 `agent error`，说明官方消息已进入桥接并回发错误。补齐原生 QQ 预设 `tool-fs-search` 的 `sampleOverCapGlobResults: false`；使用桌面实际 `0.2.1-rc.1` 模块导出的 Config 逐项验证 Windows 启用的 persona/tool-fs/tool-fs-search/tool-pwsh，4 项均接受配置。用户再次重启后明确确认“已经正常回复”，官方 QQ→DSH→QQ 的真实私聊链路通过。日常记忆插件仍未启用，后续接入与 Q03/Q04 保持独立状态。

**2.4 DSH 记忆插件共存专项（DM01—DM06）**

2026-10-25 用户确定：统一人格、记忆、自动保存、更正删除和恢复全部落在 `F:\game\projectNEKO\dsh-rin-memory`，与现有 QQ bridge 共存，DSH 继续负责回复和工具。直接交接入口为 [插件目录 PLAN.md](../../dsh-rin-memory/PLAN.md)。用户正在升级 DSH rc.3，专项以 `0.2.1-rc.3` 为目标，首步核对实际版本及两插件依赖；原 rc.1 检查作为既有基线。先完成 Windows 本机记忆闭环，不等待后台任务或 Linux 部署。

| 工作包 | 交付 | 状态 | 完成条件 |
|---|---|---|---|
| DM01 | rc.3 兼容、原生配置、身份与共存 | 进行中 | 核对升级后的接口与依赖；配置一次后启用可连接，不重复收发/提交，不影响普通会话 |
| DM02 | 唯一人格与每轮固定上下文 | 进行中 | 完善竞争人格处理、召回/近期对话去重和实际模型输入；新发布下一轮采用 |
| DM03 | 最终对外对话保存、来源和使用回执 | 进行中 | 对齐真实 QQ 输入与最终文字；重投/工具步骤不重复入库，工具正文不冒充用户证据 |
| DM04 | QQ 更正遗忘、服务同步和 DSH 历史清理 | 进行中 | 受限操作实际更新服务，按范围清理、会话交接和回执完成，重启后内容不复活 |
| DM05 | 持久恢复、QQ 平台确认和状态 | 进行中 | 保存与发送分别核对，错误分类恢复，重试不重新执行工具 |
| DM06 | rc.3 桌面与真实 QQ 闭环验收 | 未开始 | PLAN.md §7 场景通过，用户完成实际桌面启用与 QQ 操作，交付记录回填本表 |

2026-10-25 交付（代码与隔离验收，工作树 `dsh-rin-memory` + `plugin/dsh-qq-bridge` + `rin-memory`，核心版本 `0.1.0.dev3`）。DM01—DM05 的实现与隔离检查已完成，DM06 仍需用户完成桌面启用与真实 QQ 操作，故五包均按完整条件保留“进行中”。

**版本核对**：桌面实际为 `0.2.1-rc.3`（`desktop/resources/runtime/primary-runtime/runtime.json`）；`dsh-agent`、`dsh-system-prompt`、`dsh-agent-loop` 三个核心模块与 rc.1 **逐字节相同**（仅换行差异），因此原型用的 `agent/created`、`agent/pre-step`、`session/event`、`systemPrompt.section` 时序在 rc.3 仍成立；`dsh-settings`、`dsh-plugin-manager`、`dsh-api-remotes` 同样无实现差异。真正会拒绝加载的是 rc.3 的兼容预检：它读 `engines.dsh` 或 `@deepseek-ai/dsh-*` peers，原插件写死 `0.2.1-rc.1`。两插件改为 `>=0.2.1-rc.1 <0.2.2`（用 rc.3 自带 semver 7.8.5 实测：rc.1/rc.2/rc.3 通过，0.2.2 排除）。

**核心（rin-memory，dev3）**：新增受限客户端合同。`stack.toml` 的 `[clients.*]` 新增独立授权 `memory_tools`，与 `host_persona` 互不蕴含；端点 `GET /v1/memory/entries`、`POST /v1/memory/correct`、`POST /v1/memory/forget` 在传输层先于 console 身份闸放行、再由 `ManagementApi.require_memory_tools` 复核，主体只能取自身身份列表内，`forget` 的 `target` 仅接受 fact/reflection，文本匹配命中多条返回 409 `ambiguous_target` 并附候选而不猜。事实列表新增 `subject` 过滤（`domain/deletions.py`）。回执新增 `kind=sent`，带 `transport`（qq_official/qq_napchat/console/host）与逐段 `platform_id/chars`，与 `delivered`（显示/播放）严格分开：bridge 只能证明平台接收，不能冒充用户已读。验证：新增 `tests/test_dm_limited_client.py` 4 项（授权必需与主体隔离、更正幂等并进入下一轮上下文块、遗忘只删点名条目且歧义回候选、`sent` 只记已接受分段与非平台传输/空分段/超长字符被拒）；连同 `test_s04_service`/`test_s05_host_persona_state`/`test_s05_service_closeout`/`test_s02_turns` 共 **35 项通过**。`uv build` 出 dev3 wheel/sdist，安装到新运行根解释器并把 `stack.toml` 的 `package_version` 同步为 `0.1.0.dev3`，日常 48931 已重启到该构建。

**bridge（`plugin/dsh-qq-bridge`）**：只补四类接入点，记忆逻辑仍在本插件。`AccessGate.isOwner()` 以配置的 admin 身份为唯一依据（open 模式与白名单成员都不算主人）；`HandlerContext` 新增 `owner`/`messageId`/`send()`（带平台回执）；`AgentRpcHandler` 新增 `observeTurn`，报告真实输入、**解析控制块后的最终可见文字**、每段发送结果、DSH 会话与用户消息 id，失败轮次报 `failed` 而非伪造回复；`QqBridgeEvents`（服务名 `qqBridgeEvents`）作为只读出口，监听器抛错互相隔离；`DshAgentExecutor.extractLastUserMessage`/`lastTurnIdentity`/`currentSessionId` 提供轮次身份与会话关联，会话销毁时一并清除；`readPlatformMessageId` 兼容官方 SDK 与 OneBot 的 id 形状，取不到不合成。构建通过；新增 `test/bridge-events.test.ts` 6 项通过（主人判定、open 模式陌生人非主人、最终文字不含控制块且分段回执带 platformId、失败轮次如实报告、订阅隔离与释放、id 提取正反例）；`dsh-executor`/`router`/`control`/`web-activity`/`interactions`/`client-bundle` 合计 22 项通过。全量 187 passed / 8 failed，8 项均为 Windows 既有平台差异（`/opt`、`/tmp` 路径与 PATH/PowerShell 假设），与本次改动无关；基线（暂存本次 src/test 改动后）为 184 passed / 11 failed，本次改动净增 3 项通过、无新增失败。

**插件（`dsh-rin-memory` 0.2.0，零依赖，10 个发布文件）**：`config.js` 收敛配置并分离“缺凭据/地址非法”与“连不上”；`memory-client.js` 负责请求、失败五分类（retry/stale/gone/config/conflict）、原子 JSON 队列、清理待办、更正遗忘与状态；`session-store.js` 按 DSH 布局定位并删除会话日志（`session.v<N>.jsonl[.zstd]`，按魔数解压，删后复核目录消失）；`index.js` 管生命周期——作用域订阅、每轮一次快照（工具步骤复用、新发布下一轮采用）、只保存真实用户输入与最终文字、`source_key` 与清理归属、受限记忆工具（`companion_forget`/`companion_correct`/`companion_recall`）、桥接事实订阅、清理闭环与状态。默认 `enabled: false`。验证：`npm test` **20 项通过**（每轮单快照与下一轮采用、失败重启同正文重放一次、清理待办阻断补交、410 终止并退役会话、队列有界不丢轮、缺凭据/关闭时零注册、重复启停无重复监听与工具、非匹配会话完全不动、非主人与群聊不记、`sent` 只记已接受分段且与保存同一轮次、保存前到达的回执落盘后随该轮提交、工具双侧授权、歧义遗忘回候选、更正幂等、错误分类、快照四段材料分离、退役会话重启不复活、卸载释放订阅、会话日志真删且不虚构），`npm run check` 通过，`npm pack --dry-run` 10 个文件。

**隔离与真实服务校验**：真实核心合同脚本 18/18（health、context 带发布版本与凭据、reserve 持久、turns 一次提交与同键回放、used 回执、`sent` 记录 transport 与分段、entries 主体、更正进入下一轮更正块、按 source_key 遗忘、清理待办与确认、删除后重放 410）；清理闭环 7/7（合成日志 → 待办 → 本地目录真删 → 服务确认 → 退役落盘 → 重启不复活）；跨插件服务用桌面实际 rc.3 的 `cordis` 验证 5/5（`ctx.provide` 可见、`inject` 同一实例、事件到达、卸载释放、桥接卸载撤销服务）；日常实例只读检查 12/12（配置门、连接、未知客户端 401 且归类 config、状态不含凭据、entries、无匹配遗忘）。校验中产生的合成更正在日常根上已按 correction_id 撤回，未留真实事实。

**尚未完成（DM06 及完整条件）**：① 用户经 DSH 插件管理安装并启用本插件、把 `COMPANION_DSH_TOKEN` 配进 DSH 进程环境或用 `tokenFile`；② 真实 QQ 私聊逐条验收 PLAN §7 十个场景（含一轮内多次工具调用的版本固定、网页↔QQ 回忆互通、更正/遗忘双向、单条与整段删除、重启与断线恢复、发送失败只重试失败分段）；③ 实际模型下的“压缩后重建”与工具结果不变成偏好需真实对话样本；④ bridge 需重启 DSH 加载新的 dist 与放宽后的 peer 范围。本批未改 QQ 账号/白名单/工具设置，未启用 TTS 或麦克风，未代发 QQ 消息。

2026-10-25 复核修复：记忆插件 `0.2.1`、bridge `1.0.6-neko.2`、核心 `0.1.0.dev4`。

- 修正原生 volatile 配置读取与热启停；补齐 profileContext 依赖及可选服务的原生读取，避免设置启用却提前退出。
- 删除入口检查事实/反思的主体归属，限制来源键到本角色/主体/绑定插件，整段 episode 删除保留管理入口；记忆工具仅在已校验的主人私聊 Agent 作用域注册，执行边界继续核对调用者。
- 生成前完成身份和必需人格/更正检查；服务不可用的输入由 bridge 持久等待恢复，不让模型静默切回旧上下文。保存等待 bridge 最终可见文字，剔除用户消息的风格包装与输出控制块；个人聊天明确 shared 适用范围，并适配真实召回响应的 context 字段。
- 统一原生会话与服务会话映射，登记记忆/发布版本引用；清理先停止旧 Agent、清除对应 bridge 正文和补交副本，再移除 DSH 日志并确认，新输入切入新会话。补修核心在按事实解析后使用旧记录、漏发 plugin 清理待办的问题；清理待办增加 memory_ids/persona_versions 元数据，无关会话与队列保留。
- bridge 按账号持久保存输入和发送分段；已生成回复只补失败分段，成功回执保留并按稳定键去重，避免再次执行工具。崩溃中的生成和不明发送保留 unknown，停止时不再发新消息。

检查：插件语法与 **24 项**行为检查通过，bridge 构建与 **26 项**定向检查通过，核心客户端/服务/宿主人格/收尾/整轮/删除 **57 项**通过，共 **107 项**。新增插件 3 项覆盖原生配置、最终文字边界、必需上下文阻断；新增 bridge 3 项分别覆盖输入恢复、分段补发、未知状态防重放，权限与清理风险补强原有用例。rc.3 桌面原生 Cordis/工具注册表验证配置激活、主人作用域工具和卸载释放；临时真实核心 + bridge + 记忆插件完成发布上下文、纯用户输入、提交、发送失败、删除确认、会话交接与重试闭环，使用合成数据和替身发送器。

核心 dev4 wheel 已安装到本机运行解释器，48931 记忆服务已恢复；只重启记忆服务，48911 N.E.K.O 宿主保持原进程。只读核对 desktop profile：两个包均以 link 安装，记忆插件已配置 enabled=true 及令牌文件。DSH 桌面重启加载新代码、真实 QQ 操作仍由用户执行；本轮保持静音、账号与工具设置未改。DM06 继续按真实 QQ 场景验收，DM01—DM05 保留进行中直到对应完整条件满足。

<a id="qq-main-session-roadmap"></a>

**2.5 QQ 主会话精简与缓存专项（QP01—QP08）**

2026-10-25 用户确认精简 QQ 主会话的系统提示与工具，移除每条消息的渠道/临时风格包装，把完整人格移到系统段；正常上下文保持稳定前缀与原历史，相关记忆按条目只追加必要增量，更正/遗忘时优先清理。详细交接入口为 [插件 PLAN.md §9](../../dsh-rin-memory/PLAN.md#qq-main-session-plan)，主会话之外只落实统一任务入口所需的最小独立执行闭环。本专项细化 Q02/Q03/Q04 的相关工作，Linux 和完整任务调度扩展仍按原计划推进。

| 工作包 | 交付 | 状态 | 完成条件 |
|---|---|---|---|
| QP01 | 原生请求与作用域核对 | 实现完成 | 找到 rc.3 实际预设、提示来源与最终模型请求，建立普通桌面对照 |
| QP02 | 主预设精简与原文输入 | 实现+隔离完成 | 原文不带风格包装，团队/编码说明不进 QQ 主请求，其他会话保留原能力 |
| QP03 | 系统人格与每轮依据 | 实现+隔离完成 | 当前完整人格一份，每轮必需校验、固定版本和生成登记保持 |
| QP04 | 追加式背景与缓存 | 实现+隔离完成 | 稳定消息/工具前缀，按条目去重；中断、重启、压缩可恢复 |
| QP05 | 更正遗忘与消费副本 | 实现完成 | 网页来源事实的 QQ 副本、历史、摘要、队列与相关工作引用清理闭环 |
| QP06 | 统一任务入口与独立执行 | 实现完成 | start/status/cancel、后台结果回传、幂等及重启核对；QQ 可继续聊天 |
| QP07 | 保存、发送与恢复回归 | 实现完成 | 真实输入/最终输出保存，重投和发送重试不重做任务，身份权限保持 |
| QP08 | 安装与真实模型/QQ 验收 | 待桌面/模型/QQ | 原生加载、实际请求、路由返回的缓存指标和用户 QQ 场景分别验收 |

按 PLAN §9 要求,本节分五类记录:**实现**(代码与定向验证)、**隔离**(真实 rc.3 模块上的作用域验证)、**桌面**(DSH 重启后原生加载)、**模型**(真实模型最终请求)、**QQ**(用户真实私聊操作)。后三类需用户重启 DSH 后完成。

2026-10-26 当前合同以 [SPELL-PLAN.md](../../dsh-rin-memory/SPELL-PLAN.md) 为准：**主会话完整人格 + 魔法段、0 tools；执行与 followup 回填归插件，QQ 标签剥离归 bridge**。网页/桌面直接选择 QQ 预设即可使用通用咒语，无需 bridge 服务和 QQ 身份绑定。下列 2026-10-25 的“四工具”记录保留为历史；当前实现、隔离与验收要求见各节新增记录。

### 2.5.1 实现（2026-10-25，插件 0.2.2 / bridge 1.0.6-neko.2 / 核心源码 0.1.0.dev4 + QP05 未发版改动）

**2026-10-26 咒语链（插件 0.2.3 / bridge 1.0.6-neko.3）**：新增 `spell-runner.js`、`spell-library.js`。插件从已提交 `assistant/message` 解析咒语，session/seq 去重、串行运行；读取直接读盘，生成/执行用 `ctx.get('agents').create({sessionId,agentOptions,meta})` 创建无 preset 的标准 agent，沿用来源会话模型和工作目录，默认 120 秒超时并释放句柄。以带唯一 id 和 `source.kind=companion-spell` 的 `followup()` 回填真实结果，主会话销毁或超过 5 分钟停止回填。worker 的 `{ok,text}` 结果协议区分任务成功与正常结束但执行失败。

`attachSpells()` 与 QQ 个人记忆接入分开：网页 QQ 预设按原生 `agentPreset` 当前投影接入，切换预设/销毁/卸载释放；普通工作会话和 worker 排除。无 bridge 时仍能读取服务人格、注入魔法段、执行与回填；原始 assistant 标签保留在网页。通用咒语不调用 QQ 身份检查，个人记忆三咒语保持原授权。既有 `companion_task` 台账和原生子会话路径保留。

模板库位于插件状态目录 `spells.json`：最多 8 条、名字 2–8 个中文字符，via 限读取/生成/执行。`<学咒语>` 保存前真实试跑，失败不落盘；`{body}` 按字面替换、只展开一层；无占位符时说明固定命令；`<忘掉咒语>` 持久删除并确认。魔法段动态列名字与说明，不列模板。

bridge 删除旧的咒语执行服务查找及调用，只对 QQ 出站剥离内置/元咒语/学习后的闭合中文标签，保存/重试使用最终可见文字，未闭合标签保留；咒语 worker 不触发普通工作会话管理员提醒。没有新增 bridge 等待回填或执行调度路径。

**QP01 原生请求与作用域核对**：从本机真实 QQ 会话日志（`~/.dsh/sessions/--C-Users-22712--/qq-1ueypo-mvnl7jew-r4id0i-0/session.v4.jsonl.zstd`，按 zstd 拼接帧解码）读取 `request/header`/`system/message`/`user/message`，采集到基线：26 个工具、7399 字符系统段、用户消息含 `/qq-session-reply-style` + 来源横幅 + `User QQ Message:` 包装、每轮追加整张人格卡快照。证据与机制清单在 `dsh-rin-memory/.scratch/qp01-baseline.md`。作用域实测为 bundle patch 的 `preset-dsh-qq-bridge`（`@deepseek-ai/dsh-agent-preset`），旧 `.agent-presets` 目录式预设在 rc.3 **已不被读取**（核心文档原文 "Nothing reads that directory any more"），故无需重装即可生效。

**QP02 主预设精简与原文输入**：bridge 删除 `formatQqReplyStyleSkillPrompt`/`nextQqStyleSkillInjection`/`qqStyleSkillTurnCounts`/自动 `/qq-session-reply-style`，payload 严格等于 `ctx.payload`；`qqReplyStyleSkill` 仅保留解析兼容，全仓 grep 无第二条 skill 注入路径。验证：`npm run build` exit 0，定向 39/41（2 个失败为既有 Windows `/tmp` 路径差异，干净 HEAD 同样失败），全量 189/197（8 个失败均用 HEAD 干净克隆逐项对照确认为既有平台/未提交改动问题）。

**QP03 系统人格与每轮依据 / QP04 追加式背景与缓存 / QP02 主会话隔离**：插件新增 `main-session.js`，在 agent 作用域的 `system-prompt/assemble` 上按名字重建段与工具：只留 `harness:identity` + 完整人格（`deployment:persona-prefix`，核心提供的已发布全文，不压缩不改写）+ 短规则段（179 中文字符，无版本号/时间戳/请求号），删去 AgentTeam `team:policy`、AgentTeamNext `agents-team-next:policy`、`app:web-surface`、`harness:source` 与全部工具 guidance 段；工具只输出 `companion_recall/correct/forget` + `companion_task`，并加 `tools.guard()` 拒绝其余名字。人格 provider 读 `this.personas`，新发布在 `ensureTurn`（reserve 之后）与 `prepareBridgeInput`（首个装配之前）两处 set，下一轮生效。背景改为纯函数 `renderIncrement(context, seen, {scopeId})`，三类材料分开按稳定指纹去重，**无新增时完全不注入任何消息**；指纹只在真正交给模型后登记，持久化在 `outbox.json.injected`；`compaction/end`、`compaction/prune` 开新代际允许重新送达。隔离机制选择依据（为什么不是 `tools.restrict()` 也不是空段遮蔽）见插件 README「主会话隔离机制」，均由真实 rc.3 模块实测：`tools.restrict()` 只过滤继承工具、对同 agent 作用域注册的工具报 `unknown global tool`；同层空段遮蔽报 `already registered in this scope`。

**QP05 更正遗忘与消费副本**：核心新增 `persistence/consumer_refs.py`（`memory_consumer_refs.json`，有界 2048，只存 plugin_id/session/request/memory_ids/source_ids/人格版本/代际等指纹元数据，**零正文**；去重键 plugin_id+session_id+request_id），`/v1/context/reserve` 可选落盘消费关联（认证身份的 plugin_id 优先，memory_ids 以已签发 context 的实际 served 为准、客户端声明只作过滤，交集为空回落实际值——宁可多通知不可漏），`pending_plugin_deletions` 在消费关联 memory_ids 与 deletion 目标有交集时也把该插件计入待办（原 source_key/episode 路径与顺序不变，同一 deletion 对同一 plugin 只出一次），acks `cleaned` 时在同一把 `_lock(character)` 内清关联（`failed` 不清，避免"待办没了但正文还在"）。补上了"网页来源事实被 QQ 召回注入后、网页删除时 QQ 插件收不到待办"的闭环缺口。向后兼容：不传 `consumer_refs` 时响应键与行为与改动前逐字节一致。插件侧已在 reserve 里登记 `consumer_refs`（只报 id 与代际，不报正文）。

**QP06 统一任务入口与独立执行**：新增 `task-store.js`（原子写台账，`recover()` 把 running 收敛为 interrupted/unknown 且**绝不重派发**）与 `task-tools.js`（`companion_task` 的 start/status/cancel）。派发用原生 `subagents.startActivation`（provider `spawn`），`subagent/end` 写回终态并排队；主会话在 `pre-step` 以来源标记 `companion-task` 的外部事件投递，投递成功才 `markNotified`。幂等键 = owner|会话|源轮次|调用 id 的哈希，重放返回原任务且不重复派发；status/cancel 在工具执行边界复核 owner，不存在与不属于自己都回同一个 not_found。执行端不可用时返回明确错误，聊天不受影响。

**QP07 保存、发送与恢复回归**：原有 `context → requirePersona → reserve → 生成 → turns/used/sent` 链路、`MEMORY_REQUIRED` 等待、工具步骤复用同轮依据、原文与最终可见文字保存、断线补交、`isTrusted` 身份边界均未改动；四个工具都在 `execution.agent !== agent` 时拒绝。

### 2.5.2 隔离验收（真实 rc.3 模块，2026-10-25）

**2026-10-26 当前验收**：V1–V4 通过，使用桌面 `app.asar` 内真实 rc.3 Cordis、Agent、AgentLoop、Session、Tools、SystemPrompt，模型与记忆服务使用替身。确认创建 API 需要 sessionId，followup 需要 identified message，事件读取为 `session.snapshotEvents()`，whenIdle 无内建超时。

2026-10-26 工作目录上下文修复：先前只在 `.scratch/probe-runtime-context-shield.mjs` 验证的覆盖注册现已写入正式 `main-session.js` 的 `install()`，由本会话 disposer 管理。原生 `suppressRuntimeContext()` 无法抑制 required 工作目录，现用会话内同名空文本 provider 覆盖；不改真实 cwd。探针改为直接验证正式隔离类，使用真实 rc.3 WorkingDirectory、SystemPrompt、AgentLoop：新会话最终模型请求和 `user/message` 轨迹没有 runtime-context，标准 worker 仍有目录，卸载恢复原 provider。桌面已安装包是指向开发目录的 junction；当前运行进程需完全退出并重启，之后新建 QQ 会话采用修复。

`.scratch/verify-integration.mjs` 在**不提供 qqBridgeEvents 服务、不建立 QQ 绑定**的环境中创建网页 QQ 预设会话，验证 0 tools、人格+魔法、网页原始标签保留、独立标准 worker 有原生工具、来源回填后继续生成；学习/失败试跑/调用/遗忘全链路通过。另验既有绑定 QQ 的记忆 reserve 仅记真实输入、卸载后原提示与工具恢复。`npm test` **46/46**，`npm run check` 通过；bridge `delivery/bridge-events/reply-notifier` 定向 **17/17**，`npm run build` 通过。

`.scratch/verify-integration.mjs` 用 `C:\Users\22712\.dsh\profiles\node_modules` 的真实 `@deepseek-ai/dsh-system-prompt`/`dsh-tools`/`dsh-scope` + 真实 bridge `dist/bridge-events.js` + 本地替身核心服务，复现真实部署形态（全局 host 段与 13 个工具 + 同一 agent 作用域的预设 persona 与 team 工具）：

- 隔离前：9 个段（含 `team:policy`/`agents-team-next:policy`/`app:web-surface`/`harness:source`/`tool:read`/`tool:pwsh`）、13 个继承工具。
- 隔离后（按用户最终要求收紧）：系统提示**只剩 1 个段** `deployment:persona-prefix`（正文 === 服务返回的完整人格），宿主身份句 `harness:identity` 与常驻规则段都不再进入；工具恰为 **4 个**（`companion_recall`/`companion_correct`/`companion_forget`/`companion_task`）；`tools.guard` 拒绝 `read`、放行 `companion_task`。
- 运行上下文：用 rc.3 原生 `systemPrompt.suppressRuntimeContext()` 关闭。探针复现了真实部署的 6 条（`working-directory:current`/`sandbox:policy`/`approval:policy`/`harness:source`/`web-surface`/`team:identity`）：组装后 contexts 为空，发请求前 `refreshContext()` 只补回 **2 条 `required: true` 的**（`working-directory:current`、`team:identity`），其余 4 条彻底消失。这 2 条是宿主的硬约束（`refreshContext` 对 `required` 无条件恢复），插件侧无法过滤；用户已确认"teammate 可以自己关、工作区那条麻烦就算了"，故不再为它们改宿主或让第三方插件注册失败。
- 卸载后：段与工具完全恢复（9 段、13 工具、guard 消失），无残留。

QP04 增量与压缩验证（`.scratch/verify-qp04.mjs`）：首次渲染三类材料并给出 3 个指纹；登记后同一 context 返回空串（不重贴）；清空 seen（等同压缩开新代际）后重新送达；人格正文不出现在注入消息里；同一记忆只出现一次。

QP05 核心验证：新增 `tests/test_qp05_consumer_refs.py` 3 断言组（网页事实→QQ 消费→删除后待办可见、重建整个 core 后仍可查、ack 后消失且重建后仍为空；无关插件不产生待办；已有 source-key 插件优先且顺序为 `["p_source","dsh_memory"]` 无重复）+ `test_dm_limited_client.py` 1 个真实 HTTP 用例（老客户端不传 `consumer_refs` 时响应键恰为 `{durable,request_id,status}` 且不落行；带 `consumer_refs` 时 complete 链路 reserve→删除→pending→ack→消失）。实测：`tests.test_qp05_consumer_refs` 3/3 OK；定向 50/50 OK；核心全量 **285/285 OK**（仅排除 `test_s05_evolution_display`——它在 `-s tests` 发现模式下有既有的 `tests.test_b12_evolution` 导入怪癖，单独跑 4/4 OK，与本批无关）。同时核对 `memory_consumer_refs.json` 内不含任何正文。

插件自测：`npm run check` exit 0（7 个文件语法）；`npm test` **28/28 pass**（含新增：连续两轮原文各只出现一次、完整人格不在 user 消息、系统段含当前人格、无新增记忆时不产生背景消息、任务幂等重放、recover 不重派发、非本人 owner 被拒；系统段数量断言已改为 1，新增"不存在常驻规则段"断言）。`npm pack --dry-run` 14 个文件（新增 `main-session.js`/`task-store.js`/`task-tools.js`），零运行依赖（`dependencies` 为空）。

bridge 验证：`npm run build` exit 0；定向 `bridge-events/prompt/router/delivery` 41 tests、39 passed（2 个失败为既有 Windows `/tmp` 路径差异，干净 HEAD 克隆同样失败）；全量 197 tests、189 passed（8 个失败已用 HEAD 干净克隆逐项对照确认为既有平台/未提交改动问题，非本批引入）；`src/` 与 `dist/` 中 `本条用户消息来自|User QQ Message|QQ Session Temporary Reply Style|formatQqReplyStyleSkillPrompt|nextQqStyleSkillInjection` 匹配数均为 0。

**版本与兼容边界（重要）**：当前 48931 服务进程启动于 16:31，加载的是 QP05 改动之前的代码；核心源码已含 QP05，但**要等核心服务重启后 `consumer_refs` 记录才会真正生效**。两侧都是可选字段：老核心只在 `reserve()` 里读 `request_id`/`context_receipt_id`/`scope`，多出来的 `consumer_refs` 被忽略，插件照常工作；新核心用 `if "consumer_refs" in body` 守卫，老插件（不传）响应逐字节不变。所以插件与核心的升级顺序不影响可用性，只影响"网页来源事实的删除能否通知到 QQ 插件"这一能力的生效时间。核心打包（`uv build`）与版本号提升未在本批执行——按用户要求"核心仅补确需的兼容合同/清理关联"，避免无关重发版。

### 2.5.3 桌面验收（待用户重启 DSH）

当前交付为插件 `0.2.3` 与 bridge `1.0.6-neko.3` 的开发目录和构建产物。需要用户通过 DSH 插件管理更新并重启；网页场景直接新建 QQ 预设会话，检查标签保留且执行、回填正常。日常 profile、账号和服务进程保持现状。

### 2.5.4 模型验收（待真实请求）

重启后的真实模型验收：主会话请求不含工具 schema，系统段为当前完整人格与魔法；念咒语后等待 `companion-spell` 反馈再回答。标准 worker 单独使用原生工具并返回 `{ok,text}`；试跑失败不保存模板。学习/忘掉之外的普通轮次保持系统前缀稳定；缓存只登记路由实际返回的字段。

### 2.5.5 QQ 验收（待用户操作）

需要用户真实私聊确认：内置、学咒语/忘掉咒语及学习后的标签不发到 QQ，bridge 不重复执行或发送旧终端反馈，记忆更正/遗忘仍按主人权限生效。慢任务最终回复的 QQ 平台回发与实际分段接收在真实 QQ 场景验收；插件的执行与 followup 已在无 bridge 环境验收。

**2.6 记忆与陪伴连续性专项（MC00—MC07）**

2026-10-27 用户要求形成完整方案，并明确开发使用本机代码。执行规格见 [记忆与陪伴连续性改造方案](companion-continuity-plan.md)，涵盖完整经历、时间、召回、事项/状态、反馈、成长、主动交流及 Git 与版本包发布。新增 [养成感与活人感设计](companion-growth-design.md)，整合 Kindroid、Hindsight、Mem0、Zep、Letta、Honcho 对照及四条主链，明确单次教导快速采用、自主成长按独立经历成熟。MC02 承接已有 S06/S07，沿用当前样本与基线。现有源码已有独立 Git 基线；以下工作包仍待按各项完成标准实施验收，不改动其它专项的既有完成状态。

| 工作包 | 前置 | 状态 | 完成标准 |
|---|---|---|---|
| MC00 源码与发布基线 | 当前工作树核对 | 已完成（2026-10-27） | 独立仓库 9725445、origin 齐平、wheel 构建链与包白名单可核对；三个文档已入工作树 |
| MC01 时间与助手后续输出 | MC00 | **已完成（M1 + M5，2026-10-27）** | 原始时间锚点（G11）、证据与参考分离、重复来源合并（G08）、evidence_kind；助手正文与输出回执（`POST /v1/outputs`，按片段序号幂等）、待投递正文可持久补发、只有送达片段进入已发生对话、删除来源清正文留 id |
| MC02 召回增强 | MC00 | 未开始（S06 基线已存在） | A 短句/指代/背景通过原样本与新增场景；B 语义配置具备后完成向量与降级验收，分别记录状态 |
| MC03 事项与共同经历 | MC01、MC02-A | 未开始 | 同主体跨会话续接，进展、终态、禁提及来源删除生效 |
| MC04 状态、事实更新与反馈 | MC03；教导子项 MC04-T 可在 MC01 必要基础后先行 | 进行中（**MC04-T 已交付 2026-10-27**） | **已交付**：明确持久教导单次形成 tone 局部规则、下一轮采用、重启保持、自动演化关闭仍可用、可撤销。**剩余**：固定快照注入相关状态/事项/经历/成长，更正更新与实际反馈按来源生效 |
| MC05 反思与相处成长 | MC04 | **已完成（M3+M4，2026-10-27）** | 反思允许 no_change、七种局部操作、superseded/withdrawn 分离；独立经历计数、证据类型从 evidence_kind 推导、演化资格门槛、删除与更正同口径重算、旧格式兼容回退 |
| MC06 主动交流 | MC01、MC03、MC04 | 进行中（M6 候选核心与 DSH 渲染已完成） | 五类真实缘由候选、待聊意图与四操作、发送前重检、时机与回执核心合同已通过合成验收；剩 DSH 取候选/唤醒/发送/outputs 回执实际接线，纳入 MC07 渠道收口 |
| MC07 多端与整体发布验收 | 已交付目标批次 | 进行中（本机包与独立安装检查已完成；渠道验收待做） | 多端范围、版本安装与回退；接通 DSH 主动轮拉取/唤醒/生成/发送/保存，任务反馈正文进入 outputs；真实 QQ 教导、主动消息和重启恢复验收 |

推荐先核对 MC00 的现有基线与打包条件，再推进 MC01 → MC04-T → MC02-A / MC03。MC04-T 直接教导独立于自动演化开关，完整依赖与验收见新设计 §8、§13。源码与测试、修复和构建都在本机进行；服务器接收精确版本 wheel/tgz 并运行渠道验收。Git 管理版本，SSH 传输构建包；正常发布不覆盖安装目录单文件，也不重启无关的 SnowLuma/QQ。语义后端、新付费服务或账号的配置条件按方案 MC02-B 单独处理。

**M1 事实语义交付记录（2026-10-27，工作树 `F:\game\projectNEKO\rin-memory`）**

范围：只动事实层与删除层，不碰人格卡、不碰教导链路。交付四件事，全部有行为测试。

1. **证据与参考分离（设计 §6.2）**。`domain/extraction.py` 原先把用于消解指代的前文来源键并进每条事实的 `source_event_keys`，使每条事实都携带整个会话，下游成长门槛把会话长度当成独立经历数。改为 `evidence_keys`（本轮真实来源）与 `context_keys`（仅前文消歧）分开；事实行新增 `context_refs` 字段，`source_event_keys` 只装证据。
2. **相对时间锚点（G11）**。`_fact_row` 原先用 `self._clock.now()`（队列处理时刻）解析 `event_when`；`ingest_turn` 的 `occurred_at` 只进了来源账本。改为把业务时间写入 outbox payload（`occurred_at`），经 `process_outbox` → `extract_facts` → `_persist_candidates` → `_fact_row` 一路透传；老 payload 缺该字段时回退 `clock.now()`，不使窗口失败。
3. **重复事实合并来源（G08）**。`_persist_candidates` 命中既有哈希时原先直接 `continue`（只有一条 ai_disclosure 升级特例），第二次真实提及完全丢弃，删掉首次来源后事实随之消失。改为合并 `source_event_keys`、记录 `context_refs`、推进 `last_confirmed_at` 与 `confirm_count`；重试与镜像携带同一键因而不增加支持。
4. **`evidence_kind` 字段**。事实行新增 `evidence_kind`（user_assertion / explicit_feedback / observed_action / tool_result / agent_expression / quoted_example），模型给了合法值就采用，非法或缺失按来源兜底；为 M3/M4 的成长门槛提供真实依据类型，取代「一律按 actual_result 计」。

配套删除层改动（`domain/deletions.py`）：`_delete_facts` 区分**硬目标**（明确点名的事实 id、精确文本哈希、被删集所属的 episode）与**偶然命中**（仅靠 source_key 命中）。硬目标直接删除；偶然命中若仍有其它来源，只移除被删的 key 并保留事实。同时补上「支持减少但行仍在」也提交落盘的判定（原实现仅在行消失时 commit，support 收缩被静默丢弃）。

验证：新增 `tests/test_m1_fact_semantics.py` **6 项**（重复合并与单源删除存活、重试不加支持、隔日队列仍按原话解析「明天」、老 payload 兼容、context_refs 不计支持且删除参考不动事实、evidence_kind 白名单兜底）。既有 `test_b04_extraction` 的参考语义断言按新合同改为 6 项断言（证据=ctx3、参考=ctx1、删参考保留事实、删证据删除事实）。全量 `unittest discover` **299 项，2 项已知问题**：`test_s05_evolution_display` 的 `from tests.test_b12_evolution` 导入失败（无 `tests/__init__.py`，既有）；1 skipped。定向 `test_m1 + test_b04 + test_b03_deletion + test_b03_barrier + test_b03_correction` 全绿。

前后对比实测（同一句话，队列延迟两天处理）：队列时间 2026-10-29、原话说于 2026-10-27「明天面试」——修改前 `event_start_at=2026-10-30`（错两天），修改后 `2026-10-28`。两次独立提及「喜欢乌龙茶」：修改后 1 行事实、2 个来源、`confirm_count=2`；删除首源后事实仍在（修改前删除即消失）。

未做（留给后续批次）：助手后续正文与输出回执（MC01 后半）、主题反思与 no_change（M3）、独立经历计数与成长资格（M4）。本批未改真实运行根 `companion-runtime`，全部验证在临时根完成。

**M2 明确教导（MC04-T）交付记录（2026-10-27，工作树 `F:\game\projectNEKO\rin-memory`）**

范围：用户单轮明确指令即可形成持久表达规则、下一次生成采用、重启后仍在、自动演化 off 时仍可用。Lead 与 teammate 并行：Lead 做 M1（事实层），teammate 做 M2，文件不重叠。

交付：
1. **新端点** `POST /v1/persona/teach`、`POST /v1/persona/teach/revoke`、`GET /v1/persona/teachings`。能力键 `persona_teach` 独立于 `host_persona` 与 `memory_tools`，默认 `false`，在 `stack_config.py` 校验与模板注释同步。
2. **安全边界：客户端不能送规则文本**。请求只给 `source_key`；服务端从 journal（`core.source_recorded`）与 `recent.json` 读取已保存的用户原话，`_saved_user_message` 校验角色/主体/插件来源/内容分类，找不到 404。插件事件与其他主体的消息一律 403。
3. **规则落进既有 `tone` 段**，不新建表达段（设计 §5：派生行不得成为第二事实源）。实测现网卡为 identity/interests/relationship/tone 四段，tone 即既有表达段。identity 保持冻结；无 tone 段时才 add 一段。
4. **确定性派生，无模型调用**。`derive_teaching` 用规则表 + 意图判定：持久标记（以后/每次/总是）与临时标记（这次/这一轮）分流；身份请求（改名/自称）拒绝；撤销语（别学/开玩笑/当我没说）拒绝；无持久标记也无改法请求的评价（「这个 bot 挺好玩」）拒绝。实测 10 例全部符合 §8.1。
5. **幂等与并发**：`teaching_id` 由（角色，主体，source_key）派生，重复提交返回 duplicate，不产生第二次 revision；CAS 失败 409 并附当前卡。
6. **撤销**：按 `teaching_id` 或 `source_key` 撤回对应规则行，其它规则与用户手写的 tone 正文都保留。
7. **发布**：只 `next_turn`；非该 target 显式 422 `invalid_target`（此前域层会静默改写 target，已补）。
8. **tone 段溢出策略**：`MAX_SECTION_TEXT=4000`。超限时先淘汰最旧的、教学派生的行（status=superseded，正文与日志同时记）；淘汰后仍放不下就拒绝本次教导，返回 422 `tone_section_full`，**绝不删除用户手写的 tone 正文**。
9. **`/v1/context` 增补 additive 字段 `expression`**：`{section_id, rules[], tone_revision, tone_version}`，只报告当前投影里仍存在的规则行，用户手改掉的规则不会被谎报为生效。
10. **DSH 侧零改动**（修正了原设计）。规则进 tone 段后，tone 本就是 `render_projection` 的一部分，服务 `protocol.py` 的 `context.persona` 因此自动包含规则。`dsh-rin-memory/index.js` 的 `renderIncrement` 一行未改。原计划的「版本变化第一轮才渲染 [表达方式]」被否决：增量段的语义是本次新增材料，常驻规则塞进去会让 seen 指纹去重语义变味，且改动后的第二轮失去补救。
11. **重启安全**：教学服务在 `MemoryService` 构造时（`core.recover()` 之前）建立并注册 journal replay handler，避免旧进程写入的 teach 事件在启动时变成未知事件类型而让整个 replay 暂停。

验证：新增 `tests/test_mc04_teaching.py` **24 项**，覆盖能力授予/拒绝、CAS 冲突携带当前卡、幂等 duplicate、非 next_turn 拒绝、撤销只影响对应规则、重启后规则仍在、tone 溢出淘汰与拒绝、客户端不能送规则文本、跨主体/插件来源拒绝。定向回归 `test_b10_persona_card` 6/6、`test_b11_publish` 6/6、`test_s05_host_persona_state` 8/8。全量 discover **323 项，1 个既有 error**（`test_s05_evolution_display` 的导入问题，本轮未改动该文件），1 skipped。

端到端实测（临时根）：导入卡（identity 冻结 + tone）→ 教导「以后别每次都总结，像朋友一样接着聊」→ 派生 1 条规则 → tone 追加规则行 → revision 1→2、发布 `33bc5b6c0a98` → 重建核心重启：`recover` paused=False、tone 与规则完全保持、published version 仍可用 → 撤销后 revision 3、tone 回到原样。

**验收口径（2026-10-27 修订）**：M2 记为「核心接口、发布撤销、表达渲染完成」。**QQ 单轮教导体验不在本批验收**——DSH 侧没有教导入口（主会话 0 tools），且服务端 plugin 来源门禁未放开，两者都归 M6 接通后验收。

未做：G04（学另一个 bot 的四段拆解，留 MC05）、G05 群聊权限矩阵（现有主体/scope 拦截已足够）。本批未改真实运行根 `companion-runtime`，全部验证在临时根完成。

**QQ/DSH 教导链路：M2 交付时的接线缺口（历史，当前收尾见下文）**

M2 交付时用户决定先完善记忆系统，再接 QQ。该阶段 QQ 尚无教导入口，plugin 来源也被拒绝；M6 已补咒语与服务权限合同，当前剩实际运行授权、安装及 QQ 渠道验收。

已定位的两个阻塞（接通前必须解决，勿当成配置问题）：
1. **插件来源被 403 拒**。`service/management.py` 的 `_saved_user_message` 写死 `if parsed.get("plugin_id") is not None: 403`，而 DSH 插件的 source_key 形如 `plugin:dsh-rin-memory:<char>:<subject>:<session>:<request>`（`memory-client.js` 的 `buildSourceKey`），解析后 `plugin_id` 非空 → 所有 QQ 消息 100% 被拒。M2 的一刀切门禁对「防别的插件冒充」是对的，但 QQ 是主人私聊，需要改为按身份 `subjects` + 受信插件白名单判定，而不是按 `plugin_id` 是否存在。
2. **没有任何地方判断「哪条消息要学」**。teach 需要 `source_key`，而意图判断来自模型；DSH 侧没有对应工具。需要新增 `companion_teach` 工具（只传 `source_key`、绝不传规则文本），并在 `MEMORY_RULES` 补一句触发条件说明。

接通时的权限决策待用户拍板（三选一或组合）：直接生效 / 先返回 pending 由下一轮确认 / 只对 `persistent` 且命中规则表的自动生效、兜底类走确认。建议 1+3 组合。

另需注意：`/v1/turns` 的 `content_class` 缺省即 `chat`，落在 `TEACHABLE_CLASSES` 内，因此第一道阻塞解除后 QQ 消息在分类上就可用，无需额外改动。


**M3—M6 工作包与前提（2026-10-27 立案，状态只在任务板与本台账维护）**

M1（事实层）与 M2（MC04-T 明确教导）已交付验收。以下四张卡按依赖链串行：**M3 → M4 → M5 → M6**，共享任务板 task-2/3/4/5 已建并设 blocked_by。

| 卡 | 主题 | 前置 | 核心交付 | 关键验收 |
|---|---|---|---|---|
| M3 ✅ 已交付 | 反思改造 | M2 | no_change 真正可用**且记录「这批已处理」**（复用 synth_backoff，无需新文件/回放）；合成输入带旧认识/反证/范围/已采用策略；七种局部操作；去重键换掉「事实 id 哈希」，主题键只归组、同主题允许多条并存；新增 `superseded` 与 `withdrawn` 严格分离 | G12/G13/G14 全部通过；连跑两轮第二轮模型调用 0；同主题两种认识并存且都可召回 |
| M4 ✅ 已交付 | 成长证据改造 | **仅 M3（串行，不等 M5）** | 独立经历计数（按 episode 去重）；证据类型从 evidence_kind 推导；演化资格门槛（事实不得直接改卡）；统一 evidence DTO；删除/更正与新增同口径重算；旧格式兼容回退 | G09/G15/G16 全部通过；旧行升级不掉成熟度；全量 374 仅既有 1 error |
| M5 ✅ 已交付 | 助手输出 + 三个工作摘要 + 未完话题 + 真实状态 | M4（串行） | `POST /v1/outputs`；三个**可重建**摘要；`companion_state` 接真实 StateService 快照（现为 `_EMPTY_STATE` 空占位）；episode 补参与者/结果/终态。**输出可见性**：待投递正文可持久用于补发，**只有送达部分**进已发生对话与默认召回 | 摘要删掉能重建、删来源同步失效；未送达不进 recent/召回；**G20/G21 不在本卡** |
| M6 已交付 A/B 核心与消费代码；渠道待 MC07 | 主动交流（含待聊意图）+ DSH 消费端 + QQ 教导 | M4 + M5 | M6-A：候选、待聊意图、时机和回执核心；M6-B：五段渲染与 `<学到>` 咒语，主会话仍 0 tools | 合成核心、HTTP 门禁和 DSH 测试通过；QQ 实机教导与主动消息的运行链验收归 MC07 |

**串行，无循环**：M4 不再等 M5。M4 先独占 `adapters/composition.py` 定好证据合同，M5 后续装配**必须保留**该合同，不得重写或绕过。

**M6 的两个关键修正**：(1) 主动候选范围从「事项跟进」扩到「待聊意图」，否则只有任务感没有活人感；(2) QQ 教导**不注册新工具**——`README.md:93` 明确主会话输出空工具集、`tools.guard()` 拒绝工具调用，注册了模型也用不到，还会破坏隔离。改为在 `magic.js` 的 `SPELLS` 加一条内置记忆咒语（与翻本子/改写/撕掉同族），服务端仍只接受 source_key、规则由 `derive_teaching` 确定性派生。授权学习其他 bot 的表达方式走同一条快速路径，不等自主成长成熟。

**验收归属**：M2 记为「核心接口、发布撤销、表达渲染完成」；**QQ 单轮教导体验等 M6 接通后验收**。G20（实际模型输入含片段）与 G21（真实 QQ 闭环）归 M6，M5 只保证服务侧数据与合同到位。

**QQ 教导权限（2026-10-27 用户拍板）**：**直接生效**——明确教导经咒语提交后立即落 `tone` 段并按 `next_turn` 发布，不设 pending 确认。安全底线不变（客户端只传 `source_key`、规则由 `derive_teaching` 确定性派生、identity 冻结、tone 溢出拒绝）。`transient`「这次简短一点」仍不落盘。若日后改回确认制，改动点只在 `PersonaTeachingService.teach` 的发布步骤，不影响合同形状。

**不在 456 范围内**：向量召回（MC02-B）。S06 基线 Recall@K 0.857、硬失败 0、唯一缺口 1 个同义表达，收益不足，等真实使用暴露问题再评估。

**M5 之后仍需单独处理**：`/v1/receipts` 的输出分类扩展（采用 vs 实际送达），与 M5 的 outputs 合同一起收口更省事；多端范围与版本安装验收（MC07）留到最后。


**M3 反思层改造交付记录（2026-10-27，工作树 `F:\game\projectNEKO\rin-memory`）**

设计依据 §7 全节。三条硬缺陷全部修掉，另修了自己实现里的三个真 bug。

1. **no_change 真正可用**。`_build_synthesis_prompt` 原结尾「没有稳定认识时也要给出最保守的一条」删除，改为显式列出七种操作并声明 no_change 是正常结果；`synthesize()` 的空结果不再判 `failed`。**已处理标记复用 `synth_backoff` 条目**（`nc`/`nc_at`，`n` 恒 0）：key 仍是未吸收事实全集哈希，新证据使 id 集合变化 → key 变 → 标记自然失效；来源删光 → key 不再命中。因此**不需要新状态文件、不需要新回放处理器、不需要删除失效逻辑**。这是本批最省的做法，理由写在 `_no_change_settled` 的 docstring 里。
2. **合成输入扩展**。prompt 增加【已有认识】（id/status/support/reinforcement/disputation/scope/topic）与【已采用策略】两段；旧 prompt 只有事实列表，所以六种维护操作结构上不可能发生。
3. **去重键换掉**。id 改为 `ref_<sha1(topic_key|scope_key|text_hash)[:16]>`；`topic` 只做**候选分组**，同主题允许多条不同认识并存（实测「喜欢拿铁」「喝咖啡胃疼」同 topic_key 并存且都被召回）；唯一性由 topic + scope + 受范围约束的语义匹配判定。`reflection_id_from_facts` 保留，只作 backoff key 与 split 清理。
4. **新增 `superseded` 状态**（进 `REFLECTION_TERMINAL_STATUSES` 与 `REFLECTION_STATUSES`），与 `withdrawn` **严格分开**：`withdrawn` 只由删除/更正屏障写（语义未改），`superseded` 只由新证据写，带 `superseded_by` + `superseded_reason`（`superseded_by_new_evidence` / `contradicted_by_new_evidence`）。
5. **split 每条 part 独立分配 `source_fact_ids`**，按 term overlap 把事实分给最匹配的 part，无可分事实的 part 直接跳过，不共用一份支持（否则 M4 按 episode 计数会算重）。

新增字段（`refresh` 行）：`topic` / `topic_key` / `scope_key` / `applies_to` / `operation` / `supersedes` / `superseded_by` / `superseded_at` / `superseded_reason` / `previous_texts`。`SynthesisOutcome` 新增 `operation`（additive，默认 None，不参与调度）。新增 `reflection_id_from_topic` / `topic_key` / `scope_key` / `operation_of` / `REFLECTION_OPERATIONS` / `OPERATION_ALIASES`。未新增事件类型、未改 `journal` 白名单；`state_changed` 回放仅多传两个 payload 字段。

**实现过程中修掉自己引入的三个真 bug**：(a) `_apply_new` 先退役再 `store.save(rows)` 会把退役写回滚掉；(b) 目标匹配拿**新措辞**跟旧行比相似度，而 supersede 本就是换说法，实测相似度 0.0 永远匹配不上 → 改为同 topic 只有一条时直接锁定、多条时按新证据文本排序、无 topic 命中才回落语义阈值 0.45；(c) 已 promoted 的认识不在可维护目标里，退役不了且人格卡派生条目残留 → 加 `_TARGET_STATUSES=(pending,confirmed,promoted)` 与 `_retract_persona_for_reflection`。

验证：`tests/test_b05_reflection` **11 项全绿**（只调整断言口径，用例一个未删）；新增 `tests/test_m3_reflection_operations` **16 项全绿**（含「维护两轮第二轮 model_calls=0」「同主题两条并存且都可召回」）；全量 discover **342 项 = 基线 326 + 16**，唯一 error 仍是既有 `test_s05_evolution_display` 导入问题，1 skipped；十一段 demo 正常。

Lead 独立复验（不依赖其自述）：G13 普通事实连跑两轮 → round1 `no_change/nothing_new`（调用 1 次）、round2 `already_settled`（累计仍 1 次，**delta 0**）、未写任何行；G12 红茶→乌龙 → 旧行 `superseded`、新行 `pending`、`supersedes` 指向旧 id，退役链路完整。

前后对比（实测）：G13 before 是 `failed/empty_reflection` 且两轮都调模型、backoff n 涨到 2；G12 before 两行并存都 pending 都可见、旧认识**永不退役**；G14 before 两条并存、growth 为空、**反证零影响**。

残余风险：① topic 由模型给出，模型频繁换主题词时归组会退化为语义阈值匹配（有兜底但不稳）；② 模型不输出 `operation` 时按 add 向后兼容处理，不会退役旧行；③ 已 promoted 行退役后人格条目是 invalidated 而非物理删除（符合既有 persona 语义）。

未做（按分工，属 M4）：`_apply_support_removal` 仍是 `support_count = len(kept)` 按消息数重算，需在 M4 与新的按 episode 计数口径统一。

**M4 成长证据改造交付记录（2026-10-27）**

设计依据 §7.4 / §9.3。三个假象全部修掉，过程中由全量测试逼出三个真缺陷（都在本批新增代码里）。

1. **独立经历计数**。新增 `domain/growth_evidence.py`（纯函数、无 IO/无时钟）。`support_count` = 独立经历数而非 `source_event_keys` 数量。退化规则文档化：有 `episode_id` 时一个 episode 恒算一份（M1 合并的跨 episode 键不额外计数）；无 episode 时按 key 退化（两次独立提及算两份）；都无时按 fact id 兜底；`agent_expression` / `quoted_example` 完全不计数（对齐 §7.4）。
2. **证据类型从真实来源推导**。`EVIDENCE_KIND_FLAGS`：user_assertion→无旗标、explicit_feedback→user_feedback、observed_action/tool_result→actual_result+real_contact、agent_expression/quoted_example→无。删掉写死 `actual_result` 那段，`PROMOTION_RULES` 不再自我绕过。
3. **删除重算与新增同口径**（用户点名项）。`recompute_support_after_removal` / `apply_support_removal_to_row` 一处实现，growth reconcile 与 deletion cleaner 共用；withdrawn/narrowed 语义保留。`drop_fact_ids` 区分两路径：删除移除 `source_fact_ids`，更正**只重算支持与旗标、保留 provenance**。
4. **演化资格门槛**。`eligibility_of_row` 两条路都走 basis 校验：growth item 需 `promotion_ready`（经历数≥门槛且有对应旗标）；裸反思需 `reflection_ready`（basis 非空且经历数≥`evidence_min_support`）。事实一律 `fact_not_eligible`，只作解释依据。
5. **统一 evidence DTO**，`composition._evolution_evidence` 重写为同一形状；修复证据过滤器在 `wanted` 命中时丢掉调用方点名事实的问题。

**本批由全量测试逼出的三个真缺陷**（都不是断言口径问题）：
a. **旧库兼容事故**：`support_basis_from_row` 对无 `support_basis` 的历史行返回空 → 升级后所有既有认识会静默掉成 not_mature。修法：旧格式回退（`support_basis` → `support_event_keys`/`source_event_keys` → `source_fact_ids` → 0），旗标只继承该行已声明的，绝不凭空补 actual_result；新建行必写 `support_basis`。
b. **seed 未适配**：`webui/seed.py` 演示数据无 `support_basis` → 提案被拦 → `build_app` 崩溃，连带 22 个 console/model-config 测试 setUp 失败。修法：补 `support_basis` 并加一条两经历的成熟依据。
c. **更正撤回失败**：`ReflectionInvalidator` 先删 `source_fact_ids`，而 persona invalidator 靠该字段做映射 → 派生人格条目没被撤回（事实更正了但人格卡还留着）。修法：更正路径 `drop_fact_ids=False`。

测试适配（均为新行为正确期望）：b05 growth fixture 补 `evidence_kind=observed_action`（裸 user_observation 本就不该开 habit 门）；b12/c03/b13/s05_service_closeout 的 prepare 补两经历成熟认识（事实池现被 `fact_not_eligible` 拦，正是新行为）。

验证：新增 `tests/test_m4_growth_evidence.py` **32 项**；全量 discover **374 项**（基线 342 + 32），errors=1 为既有 `test_s05_evolution_display`，1 skipped。

Lead 独立复验：eligibility 矩阵（confirmed+空 basis→not_mature、+1 key→not_mature、+2 keys→eligible、pending+2→not_mature、fact→fact_not_eligible）；旧行兼容（重建 2 份 basis 且不凭空加旗标）；`drop_fact_ids` 两路径（删除→facts `['f2']`，更正→仍 `['f1','f2']`）。

前后对比：改造前一次交心 5 句话即达「两份独立经历」并开晋升门（因所有来源一律 actual_result）；改造后同 episode 恒 1 份支持、裸陈述不带旗标，必须真有结果或反馈才成熟。

**M4 遗留项（需后续决策，勿静默处理）**

1. **旧行旗标不可信但被保留**：M4 不回填、不回溯修正旧行的 `evidence_kind`/旗标，只在读时忠实继承行内**已声明**的值。后果是：M1 之前由 `ai_disclosure` 却写死成 `actual_result` 的旧行，仍可能通过 habit 门。彻底干净需要一次性迁移重算旧行 `evidence_kind`——那是**改写历史证据**，应单独决策、单独备份、显式执行，不塞进任何批次静默做。记为独立后续项。

2. **裸反思资格已定案并实施（2026-10-27 收尾）**：confirmed / promoted、有至少两份独立经历，且至少有 user_feedback / actual_result / both_party / real_contact 中一类真实依据。纯陈述反思继续用于理解与召回，不自动改卡；真实用户反馈无需工具结果即可满足条件。明确教导仍单次生效。删除最后一份交互依据后，即使还剩两份陈述经历也失去资格。

3. **`evidence_min_support` 默认 2**：与 `PROMOTION_RULES` 每类 min_support 一致（host 写死 2），是「可试用」的最低线；可配 1..8，设为 1 等于退回「一次就成熟」。它和 reflection 的 `growth_min_support` 是两个独立设置但**默认值必须同步**，composition 里已让 provider 与 gate 读同一个值避免漂移。

4. **`_apply_eligibility` 优先信任 provider 的 DTO**：注入自定义 `evidence_provider` 的调用方必须给出可信 DTO，否则资格判定会信任它。目前只有 composition 一个 provider，风险可控，已写进 docstring 作为隐式合同。


**M5 助手输出、三个工作摘要、未完话题与真实状态交付记录（2026-10-27，工作树 `F:/game/projectNEKO/rin-memory`）**

范围：补齐设计 §5 的「助手表达」对象、§10.1 的「当前工作摘要」层、§11.1 的经历投影与 §11.2 的未完话题；`companion_state` 从空占位换成真实快照。四条链共用一条原则：**能从权威记录重建的才落成对象，抽不出来的一律做投影**。

1. **`POST /v1/outputs`（正文 + 因果来源 + 送达回执）**。新增 `persistence/outputs.py`（`outputs.json` 版本化表）与 `domain/outputs.py`（`OutputService`），journal 事件 `core.output_recorded`。分类固定 `companion_output`，属 plugin-only 类，不进事实提炼、不增加用户偏好支持。可见性语义按设计 §12.3 落实：
   - **正文在生成时即落盘**（status=pending），使进程重启后能补发；`GET /v1/outputs?pending=1` 返回仍未送达的片段（含序号与文本），「只补未送达部分」有服务端依据。
   - **只有送达片段进入已发生对话**。`POST /v1/outputs/deliver` 按片段序号回执，写入 `recent.json` 的只有被回执点名的片段；未送达正文不进 recent、不参与默认召回。
   - **回执按片段序号幂等**：同序号重复上报是 duplicate（不写第二行、不重复计字符）；同序号不同长度是 409 `part_chars_mismatch`；越界序号拒绝。这就是「只重试失败片段」能安全重放的原因。
   - **`interrupted` 与 M2 同义**：保留正文、记录可重试、**recent 增量为 0**——中断的生成从未送达，不是双方共同经历。
   - 删除来源时 `_OutputCleaner` 清正文留 id（status=invalidated），与 M1 的删除屏障同口径。
   - 新增第 4 个客户端能力 `memory_outputs`（默认 false，独立于 host_persona / memory_tools / persona_teach）。

2. **三个可重建工作摘要**。新增 `domain/working_summary.py`（`WorkingSummaryService`，纯投影）与 `persistence/working_summary.py`（审计缓存）。
   - 成长与关系：已发布教导规则（且必须在当前投影里仍存在）+ growth/reflection 行 + 关系节点。
   - 重要事实：当前有效事实投影 + 主体/场景过滤 + 重要度排序。
   - 未完话题：`companion_state` 的 open/waiting 事项，带下一步、等待时长与提问禁制标记。
   - **读路径永远实时投影，不读缓存**：删除来源后摘要当轮就消失，不存在「删了还复活」。落盘的 `working_summary.json` 只作审计（`rebuild_key` + `invalidated_at`），由 maintenance 的 `working_summary` 步骤刷新、由删除 fan-out 的 cache invalidator 置失效；测试证明删掉该文件后 sections 与 rebuild_key 完全一致。
   - `rebuild_key` 覆盖全部输入版本：删除版本、更正版本、事实数量+最新确认时间、反思数量+更新时间、state 版本+更新时间、规则数+card revision、persona 条目、directives 数量。

3. **接真实 state**。`build_turn_context` 拆为外层包装 + `_turn_context_core`：一次 `StateService.read` 得到快照，**同一版本**既渲染 block 又回填 `companion_state`（避免两次读取之间提交造成 payload 说谎）；`open_threads` 未由调用方传入时从该快照的 open/waiting 事项派生（此前该参数没人传）。

4. **未完话题续接（G18 核心）**。新增 `followup` 解析：只在**指代/续接标记**出现时解析，解析到唯一事项才算 `resolved`；多候选同等合理时返回 `ambiguous` + `clarify_basis`（每条候选带 `thread_id` 与区分依据）。续接真正落地还要求命中对应经历，因此 recall 增加 `retrieval_mode="episode"` 分支：用解析出的经历的 episode 作为锚点选行。**新话题不继承旧事项**（「今天天气不错」仍为 miss），这是设计 §10.4 的明确要求。

5. **episode 投影补全**。参与者（master/assistant，由证据类型推出）、双方行为、实际结果（`observed_action` / `tool_result` / `explicit_feedback`）、终态（由 state 事项经证据键派生）、后续时间、禁提设置（来自该主体的生效 directives）。**不改 `extraction.py`**：这些字段本就是随后续轮次逐步形成的，抽取时刻的快照只能记录当时看到的，写法上必然成为第二事实源。

6. **`/v1/context` 扩层（additive）**。新增 `working_summary` / `state` / `state_version` / `episode` / `threads_source`，既有字段（`context` / `memory_ids` / `persona_*` / `corrections` / `recent_turns` / `basis` 等）形状完全不变，`context` 子块仍是 B06 那份。

验证：新增 `tests/test_m5_outputs_summaries.py` **29 项**（输出可见性 7：未送达不落 recent/可恢复补发/分段幂等/部分送达只补缺失/中断不写/已送达才可见/删除源失效；工作摘要 6：权威派生/删除同步失效/删缓存重建/删缓存不改答案/维护写审计行/删除标失效；G18 续接 4：解析/多候选澄清依据/新话题不误接/跨 session 重启续接；真实 state 3；episode 3；`/v1/context` 扩层 1；HTTP 层 5：能力门禁/记录与送达/未知输出 404/中断/摘要路由）。定向 `test_b06_recall + test_b07_state + test_b05_reflection + test_m3_reflection_operations + test_m4_growth_evidence + test_m5` 共 **104 项全绿**。全量 `unittest discover` **403 项 = 基线 374 + 29**，唯一 error 仍是既有 `test_s05_evolution_display` 的 `tests.test_b12_evolution` 导入问题（未触碰该文件），1 skipped。

**前后对比（同一段对话：事实「用户明天要去面试」（episode 1027）+「用户喜欢乌龙茶」+ 一条 habit 反思 + 一条 state 进展「帮主人准备面试」，用户说「那个后来怎么样了？」）**

| | 改造前（M4 基线） | 改造后（M5） |
|---|---|---|
| `result` | `miss` | `matched` |
| `retrieval_mode` | `bm25` | `episode` |
| `memory_ids` | `[]` | `["fact_interview"]` |
| `context` | `""` | `1. [事实/-] 用户明天要去面试 (2026-10-28, 当下)` |
| `query_meta.used_thread` | `false` | `true`（`thread_from_followup`） |
| `companion_state` | `{"block":"","version":0,"items":[]}` 恒为空占位 | `version=1`、`block="进展：帮主人准备面试"`、items 含 source_event_ids |
| `threads_source` | 不存在（参数靠调用方，没人传） | `companion_state` |
| `followup` | 不存在 | `resolved` → 「帮主人准备面试」 |
| `working_summary` | 不存在 | 相处与成长 / 重要事实 2 条 / 未完话题 1 条（附 rebuild_key） |
| `episode_summary` | 不存在 | `episode_id=ep:host:interview:1027`、`participants=[master]`、`state=waiting` |

新话题对照：同一状态下说「今天天气不错」→ `result=miss`、`memory_ids=[]`、`followup=not_continuation`，旧事项不被拉回。

**M5 遗留项（需后续决策，勿静默处理）**

1. **`/v1/receipts` 的输出分类扩展未做**：设计 §12.2 列了「输出采用 vs 实际送达」的区分。本批把送达事实放进 outputs 自己的回执（按片段序号），既有 `/v1/receipts` 的 `used/sent/delivered` 语义一行未动，两套口径并存。合并它们要改 DSH/QQ 侧消费，故留给 M6 或独立批次。
2. **多片段正文的片段边界由服务端决定**：客户端必须引用服务端返回的序号与字符数；若某渠道自己的分段边界不同，需在 create 时用 `part_sizes` 显式声明，否则按 400 字符并在句末回退切分。这是合同选择，不是缺陷，但 M6 接入真实渠道时要核对。
3. **episode 的 `assistant_behaviour` 依赖 `agent_expression` 证据**：目前只从事实行的 `evidence_kind` 推导。她的输出正文本身已单独存进 outputs，但**尚未**作为证据回流到 episode（那需要先决定「她的自述是否算一次经历」——按 §7.4 自己不应给自己加支持，所以本批刻意不接）。记为独立后续项。
4. **摘要的多语言文案**只覆盖 zh/en 两套标签；其它语言回退 zh。语料扩展留待需要时再补。
5. **审计缓存的保留策略**：`working_summary.json` 按主体保留一行，删除来源只标失效不删行（便于排障）。每角色每主体恒定一行，不随时间增长；若将来主体数量成为问题再补保留策略。


**M5 助手输出 + 三个工作摘要 + 未完话题 + 真实状态交付记录（2026-10-27）**

设计依据 §5 / §10.1 / §11.1–11.2 / §12.2。核心侧完成，渠道验收（G20/G21、DSH 渲染、真实 QQ）归 M6。

1. **`POST /v1/outputs`**（新 `persistence/outputs.py` + `domain/outputs.py`）。助手正文独立存档，`kind` ∈ proactive / task_feedback / reply / notice，`content_class=companion_output`（不进事实提炼、不增用户偏好支持）。
   **可见性语义（用户特别强调，已精确实现）**：`create` 落全文 `status=pending`、**不写 recent**；`deliver` 按**片段序号**上报，只有实际送达片段进 recent，幂等（同 index 重报 → duplicate 不重复写、不重复计 chars；同 index 不同 chars → 409 `part_chars_mismatch`；越界 → 拒绝）；`interrupt`/`failed` 保留正文可补发但**不写 recent**。送达写走 `journal.record_and_save`，replay 幂等补 recent（覆盖崩溃窗口）。
2. **三个可重建工作摘要**（新 `domain/working_summary.py` + `persistence/working_summary.py`）：成长与关系（已发布 teaching 规则 + growth/reflection + 关系节点）、重要事实（当前有效事实投影 + 范围过滤）、未完话题（state 的 open/waiting）。
   **关键取舍**：召回/上下文路径**永远从权威记录实时投影、不读缓存**；落盘的 `working_summary.json` 只作审计缓存（`rebuild_key` + `invalidated_at`），由 maintenance 刷新、由删除/更正 invalidator 置失效。这样「删掉缓存功能不变」有实证，且摘要永不成为第二事实源。`rebuild_key` 输入含 deletion/correction 版本、facts 数量+时间戳、reflections、state 版本、规则数+card revision、persona、directives。
3. **接真实 state**。`build_turn_context` 拆为 wrapper + `_turn_context_core`，一次性读 StateService 快照，**同一版本**既渲染 block 又回 `companion_state`（替掉恒空的 `_EMPTY_STATE`）；`open_threads` 未由调用方传入时从 state 的 open/waiting 派生（G18 续接来源）。新增 `followup` 解析（多候选给澄清依据）。
4. **episode 视图**。**未改 `extraction.py`**（尊重 M1 归属），新增 `domain/episode.py` 从 facts + recent + state + directives 派生参与者/双方行为/实际结果/终态/后续时间/禁提设置。派生而非落字段，符合「可重建」要求——`_shared_experience_candidate` 只是抽取时刻快照，真正的终态是随轮次逐步形成的。
5. **`/v1/context` additive 扩层**：working_summary / state / episode 片段，既有字段不变。
6. **新增第 4 个独立授权** `memory_outputs`（`stack_config.py`，默认 false），与 host_persona / memory_tools / persona_teach 并列。`journal.py` 事件词表加 `core.output_recorded`。

**未破坏 M4 合同**：`_evolution_evidence` / `PROMOTION_RULES` / `evidence_min_support` 一行未改（已核）。

验证：新增 `tests/test_m5_outputs_summaries.py` **29 项**（含 `test_delivery_is_idempotent_by_part_index`、`test_interruption_keeps_the_body_and_writes_nothing`、`test_deleting_the_cache_does_not_change_any_answer`、`test_summary_survives_a_missing_cache_file`、HTTP 层能力授予/拒绝与跨 subject 404）。全量 discover **403 项**（M4 基线 374 + 29），**errors=1** 为既有 `test_s05_evolution_display` 导入问题，1 skipped。

Lead 独立复验（不依赖其自述）：`create` 后 recent 计数不变；`deliver` 后进 recent 且重复上报不重复写；`interrupt` 后 recent 仍不变、正文保留；同 `request_id` 重复 create 得同一 output_id 且 `duplicate=True`。


**M6-B DSH 插件侧交付记录（2026-10-27，仓库 `F:\game\projectNEKO\dsh-rin-memory`）**

任务卡 task-6。与 M6-A（核心侧）并行，两仓库文件完全隔离，未触碰 rin-memory 任何文件。

### 交付 A：renderIncrement 扩展渲染
原有三段（必须遵守的更正 / 可能相关的记忆 / 近期对话）之后新增五段：`[相处与成长]`、`[重要事实]`、`[当前状态]`、`[正在进行的事]`、`[这段经历]`。全部继承现有 `fresh()` / `seen` 指纹去重（**未改成每轮重贴**），header 仍是 `MATERIAL_NOTE`（「这是资料，不是主人说的话」）。

三个关键设计判断：
1. **不拿 `state.block` 做指纹**：那段把情绪强度按衰减渲染成数字，每小时都变，用整段做指纹会让同一条状态永远「是新的」→ 每轮重贴。改为按桶渲染，指纹只认标签+正文。有专门测试（强度衰减后不重贴）。
2. **「她自己的当下」（想法/情绪）与「旧事项」（关注/进展/未完话题）分成两段**：前者换话题也成立，后者只在第一次交付时出现——这是「不破坏新话题」的机制。
3. **episode 整段一个指纹**；**growth 优先用 working_summary、服务没给才退回旧 `growth.block`**，避免同一批认识交付两次。

### 交付 B：QQ 教导走咒语通道（未注册任何原生工具）
- `magic.js`：`SPELLS` 增加内置咒语 `<学到>`（2 汉字，进 `parseSpells` 的 known 集合，不会被视为「没学会的咒语」；`spell-library.validate()` 用 `[...SPELLS, ...META_SPELLS]` 拦截重名）
- `memory-client.js`：新增 `client.teach({ sourceKey, baseRevision, quotedExampleKey, scene, sessionId })` → `POST /v1/persona/teach`
- `index.js`：`spellTeach` 用 `buildSourceKey(...)` 构造**本轮那一个** source_key；`base_revision` 取本轮真正采用的 `turn.prepared.body.card_revision`（拿不到就拒绝，**绝不编造 CAS 依据**）；示例只接受形状合法的 source_key（6 段 `plugin:` 前缀），自然语言描述安全丢弃；失败按 403/404/409/410/422/transient 分档给可读原因（transient 明说「没写盘」）
- `main-session.js`（**经 Lead 批准**）：`BASE_MAGIC_RULES` 补 `6. 学到`，原 6/7 顺延为 `7. 学咒语` / `8. 忘掉咒语`，说明文字原样保留。**只改魔法段正文**——组装钩子、`tools: []`、`suppressRuntimeContext`、working-directory 覆盖、`THINKING_VOICE` 位置一个字未碰
- `MEMORY_RULES` 补触发条件（现 237 字，仍在 `RULES_CHAR_LIMIT=300` 内）

**`main-session.js` 这处改动是必须的**：主会话 0 tools、系统提示只有人格段+魔法段，不写进魔法清单则模型根本不知道有这个咒语——解析器认得但没人会念，功能实际不可达。魔法段长度 442 → 495 字符（+12%），已确认该段无字符上限约束。

### 验证
- `npm test` **80/80**（基线 57 + 交付 A 10 + 交付 B 13）；`npm run check` 11 个源文件全过
- **G20 走真实 `runtime.preStep`**，断言**最终 messages** 里那条 `source.kind=rin-memory` 的正文真含 `[相处与成长]`/`[当前状态]`/`[这段经历]` 片段，而非只断言 renderIncrement 输出；另一项断言无新增时 messages 长度不增加、不留占位句
- **主会话 0 tools 用真实 `MainSessionScope.shapeAssembly` 复核**：`tools reported = []`、`contexts: []`、sections 仅 `deployment:persona-prefix` + `rin-memory:magic`、magic 含「学到」
- **请求体断言用真 `MemoryClient` 配假 fetch**（断的是真会发出去的字节流）：`Object.keys(body).sort()` 恰为 6 个键，序列化后不含用户原话、不含规则文本、无 `rules` 字段

顺带修的质量问题：召回结果与摘要投影天然重叠（「用户喜欢红茶」两处各贴一次），已加互相包含判重，摘要段让位给召回调（召回才是本轮针对性结果）。

**遗留风险**：① 端到端未验——M6-A 的 plugin 门禁未落地，当前传 `plugin:` 前缀会被服务端 403 拒；② `base_revision` 依赖人格卡已建立（revision>0），新建根首次教导需先在管理台建卡；③ 示例来源只认完整 source_key 形状，模型写自然语言会被安全丢弃。
**M6-A 核心侧交付记录（2026-10-27）**

任务卡 task-5。与 M6-B（DSH 侧）并行，两仓库文件隔离。

### A. 主动交流（含待聊意图队列）
新增 `persistence/proactive.py` + `domain/proactive.py`（ProactiveService / ProactiveSettings），journal 事件 `core.proactive_candidate`；composition 增 `proactive` / `proactive_store` 字段、`proactive_settings` 参数、`_ProactiveCleaner`。

1. **无来源即拒**：`enqueue` 无 `source_keys`/`fact_ids`/`reflection_ids`/`thread_id`/`episode_id` → `no_source`。
2. **五类缘由**：thread_due / agreement / task_result / interest_news / shared_finding。其中 **agreement 与 task_result 只能由运行时声明**（服务不从文本猜「约好了」），其余三类从 state / reflection / growth 派生，同源重复派生为 duplicate。
3. **成熟度按来源分流**：explicit_agreement / task_result 直接成候选；autonomous_inference 走 M4 的 `eligibility_of_row`（裸事实 `fact_not_eligible`、单来源反思 `not_mature`、成熟反思放行）。
4. **四操作**：postpone / rewrite / merge / cancel（merge 让存活者继承两边来源，删任一边都能命中实际发出的那条）。
5. **发送前重检**：`recheck()` 与 `dispatch()` 共用判定，`send()` 默认自己再判一次（静默时间绕不过去）。四类结果：同题被谈 → absorbed（G25）；换题 → defer `topic_changed`；来源删除/话题暂停/禁提/事项关闭 → refused。
6. **双重时间冷却**（Lead 追加要求）：除「刚发言 30 分钟」外，新增 `topic_cooldown_minutes=360`——用户上一轮提过该题、这一轮沉默也不会重复发。360 分钟短于 `thread_followup_min_hours=12`，所以不会挡住合法跟进。为此 `ProactiveStore` 增有界 utterance 记录（32 条，只存文本/时间/subject/request_id 用于匹配，不复读为正文）。明确约定豁免（用户提过恰是约好的理由）。
7. **时机**：每日 2 次 / 间隔 90 分钟 / 本地 00:00-09:00 静默，约定提醒豁免。跨日边界显式测了默认窗口与 wrap 窗口（23:00-07:00）共 8 个时间点。
8. **发送-保存-回执**：`send` 只创建 M5 output（pending，不写 recent），`deliver` 委托 OutputService。G22 实测：3 段长文只报 index 0 → `partial`、`resumable()` 返回缺失段、补发只报缺失 → `delivered`、重复上报 duplicate 且 recent 计数不变。
9. **G26 三件分离**：「先不聊」= 话题暂停（到期自动失效，不产生删除记录）；「别再提」= 真实 directive（recall 共用同一份）；「忘掉」= 真删除，清理器把候选置 invalidated 并清正文。

### B. 服务端 plugin 门禁改造（QQ 教导前提）
`_saved_user_message` 拆为：角色校验（在分流之前，两条路都覆盖）→ host 来源走原分支（subject 判定与 200/403/404 三态**一字未改**）→ plugin 来源走新增 `_require_trusted_plugin_source`。
四条 **AND**：`persona_teach` 能力 → subject ∈ 该身份 subjects → character == 服务角色 → `plugin_id` ∈ 该身份 `trusted_plugins`。
**`trusted_plugins` 挂在 client 上、默认空**（模板 `trusted_plugins = []`），升级本身不放开权限。新增 `scope_subject` 参数用于校验 payload.subject 与 source_key.subject 一致（只在 plugin 分支，避免破坏 host 语义）。

### C. outputs / receipts 收口
`/v1/receipts` 加 additive `output_id` 参数与只读 `GET /v1/receipts/summary?request_id=`，把「采用（persona_version + adoption_status）」与「实际送达（sent_chars / delivered_chars / complete / transport_parts）」分开回答，并给出该 request 的 output 片段进度。既有 `used/sent/delivered` 语义**逐字段断言未变**（含 sent 的 transport/sent_chars/parts、delivered 的 delivered_chars），未命名 output 时回复不含 `output_id` 键。

### 过程中修掉的三个真缺陷
1. **派生候选的来源不可删**：state 事项只记本地 id（`e_interview`），删除记录用全键（`host:rin:private:e_interview`）→ 派生候选带着删不掉的来源。修法：通过 facts 建立 local-id → 全键/归属 episode 索引，派生时写全键（顺带补上 `episode_id`）。与 M4 的「更正撤回失败」是同一族问题：同实体跨层 id 表示不一致导致静默失效。
2. **M5 cause 归一化丢字段**：`persistence/outputs.py` 的 cause 白名单丢弃未列出键，M6 加 `candidate_id` / `thread_id` 时暴露。按 additive 加入，既有 5 键语义与顺序不变。
3. **subject 一致性检查放错位置**：最初放在 host/plugin 共用位置，`test_mc04_teaching` 的 host 用例立刻挂（错误码变 `scope_mismatch`）。修正为只在 plugin 分支。

验证：新增 `tests/test_m6_proactive.py` **40 项** + `tests/test_m6_server_gate.py` **17 项**。全量 discover **460 项**（M5 基线 403 + 57），**errors=1** 为既有 `test_s05_evolution_display` 导入问题，1 skipped。

**Lead 独立复验**：
- 门禁四态矩阵实测：`teach=yes trusted=yes → ALLOWED`；`teach=yes trusted=no → forbidden(not trusted)`；**`teach=no trusted=yes → forbidden(no capability)`**（配了白名单但没能力位仍拒绝，无越权）；`teach=yes wrong subject → scope_mismatch`。
- **真实端到端打通**：用 `plugin:dsh-rin-memory:companion:private:dsh-abc:req-001` 作为 source_key，经 `ingest_turn` 存档 → `derive_teaching` 派生 1 条规则 → `teach` 返回 `applied`、revision 1→2、tone 段正确追加规则行。**这是 QQ 教导链路首次真实打通**（M2 时因门禁被 403 全程阻断）。



**M6 遗留项（需后续决策，勿静默处理）**

1. **跟进间隔定案：保留 `thread_followup_min_hours=12`**。这是普通事项产生候选的起点；实际打扰还取决于候选质量、近期交流、事项终态和提及限制。保持现有时机规则，不新增发送门禁。出现具体打扰样本后先核对来源与内容，再决定是否调为 24—48 小时。
2. **每日 2 次 / 90 分钟在真实使用中几乎碰不到**：一天攒够 2 条有真实缘由的候选本身就少见，真正会打扰的是一次低质量候选而不是十次高质量候选。**不建议动这两个参数。** 90 分钟防的是两条同时到期连发；若真遇到两条都急，应改「同类合并优先」而不是缩短间隔。约定提醒有豁免，不会漏掉重要提醒。
3. **`agreement` / `task_result` 依赖调用方诚实**（信任边界，非缺陷）：服务只校验「有真实来源引用」+「来源未删」，无法验证「两人确实约好了」。QQ/DSH 必须只在真实发生时调用。
4. **时区只有固定偏移**：本机无 tzdata，`user_utc_offset_minutes` 无法表达 DST，DST 地区换季前后静默窗偏一小时。docstring 已标为**当前限制**而非设计选择。
5. **`dispatch` 一次只处理一条**：多条同时到期无批量合并策略，等真实投递数据再定。
6. **同题冷却用最长公共子串 ≥2**：不是向量也不是关键词表，误判成本不对称（误合=多一句自然衔接，误分=重复问），故偏保守。
7. **冷却/日上限是全局单组参数**：多用户需下沉到 (角色, 主体) 维度。
8. **QQ 教导的日常实例仍需显式授权**：`stack.toml` 同时配置 `persona_teach = true` 与匹配来源标识的 `trusted_plugins`。插件默认 `pluginId=dsh_memory`，因此默认配置应为 `trusted_plugins = ["dsh_memory"]`；自定义时以 source_key 第二段为准。两项授权缺一即 403，升级包本身不改变运行授权。

**M4 遗留修复：`python -m rin_memory` 第 11 段 demo 崩溃（Lead 收口）**

M6-A 收尾时发现仓库自带十一段 demo 在第 11 段 `persona_admin_demo` 崩溃（`evolution_service.get()` 返回 None）。它用四条证据排除自身责任：① 纯 HEAD 源码（git archive）同样崩；② 崩点前 `second.status` 为 `rejected/base_revision_conflict`，根因是 `p1` 被判 `rejected/fact_not_eligible`、`accepted.revision_after=0`；③ 直接调 `eligibility_of_row`，demo 的 `fact_tone` 是裸陈述（`user_assertion`），M4 按 §9.3 判 `(False, not_mature)`——**这正是 M4 的预期行为**；④ 临时副本里只把 `_apply_eligibility` 改成直通，demo 立刻跑通。

结论：**M4 让 demo 的裸事实不再具备演化资格，但 demo 没跟着适配**，且 460 项测试无一覆盖这段 demo（都直接调 service），所以整条 demo 链路的崩溃没人发现。Lead 直接收口：
- `__main__.py` 新增 `_demo_understanding(fact_id)` helper，产出一条两独立经历（`support_basis` 带两个 episode、`explicit_feedback` 旗标）、`status=confirmed` 的成熟认识 `ref_tone`
- `persona_evolution_demo` 与 `persona_admin_demo` 的种子数据补该认识，`source_refs` 从 `["fact_tone"]` 改为 `["ref_tone", "fact_tone"]`——**认识作资格依据、事实仍作解释来源**，正是 G15 的语义

修复后：十一段 demo 全部正常输出；全量 discover **460 项 / errors=1（既有导入问题）/ 1 skipped**，与修复前一致（demo 未被任何测试覆盖，此修复不改变测试数字）。

**教训**：demo 与测试是两条独立链路，测试全绿不代表 demo 能跑。M4 那轮跑了四次全量都没发现，是因为全量只跑 `tests/`。**后续改动 `__main__.py` 所依赖的语义时，要额外跑一次 `python -m rin_memory`。**

**本机交付收尾与后续顺序（2026-10-27）**

两项决策已落实：普通事项跟进间隔保持 12 小时，保留已有时机规则、不追加发送门禁；裸反思改卡需至少两次独立经历与一类真实交互依据，用户反馈可满足，无需强求工具结果。明确教导仍单次采用。

本次补 3 项独立风险回归：纯陈述反思拒绝改卡、反馈可在无 actual_result 时取得资格、删除最后一份交互依据后资格撤销。修复 `test_s05_evolution_display` 在 discover / 包模块两种加载方式下的导入，并让其历史对比样本使用合格反思。最终核心 **466 项，0 失败、0 错误、1 skipped**；DSH **80/80**，JS 语法检查通过。新增回归 3 项，原来无法加载的显示测试恢复 4 项，替代旧错误占位后总数为 466。

本机构建产物：核心 `0.1.0.dev5` wheel/sdist，DSH `0.2.4` tgz。wheel 在仓库外临时 venv 独立安装，确认导入来自 site-packages，11 段 demo 全部运行通过；包文件检查无运行根、备份与开发依赖。正式人格卡、运行根与部署进程保持原状态。

**MC07 本机接线（2026-10-28）**：已补齐两条链路。普通对话复用一次提炼调用返回 facts + state_updates，由程序绑定主体、场景、原始时间与证据键，支持事项建立/更新/结案与明确回访时间。主动交流新增受 `memory_outputs` 限制的 poll/use/observe/complete/check/release 接口，租约绑定身份与会话；DSH 定时消费、同人格固定快照生成、插话撤回、QQ 稳定 output ID 分段投递与重启补发已接通，送达回执才写近期。主请求继续完全省略 tools。

本次新增核心 3 项、DSH 2 项、bridge 1 项测试，分别覆盖事项生命周期/范围、租约与插话、HTTP 权限与丢响应重放、部分回执与恢复、生成中撤回、桥接持久分段与发送前复查。核心相关组合 **110/110**、DSH **82/82**、bridge 投递 **8/8**；bridge 构建通过。使用 DSH 保存的官方 Key、`deepseek-flash` 和桌面包内真实 rc.3 SDK，在临时数据根跑通面试约定→提炼与事项→到期候选→0 tools/开启思考的真实生成→模拟 QQ 首次失败/客户端重开/同正文补发→回执入近期→用户报喜结案。QQ 传输使用模拟发送器；本轮在本机源码工作，服务器与在线 profile 未更新。任务反馈正文独立 outputs 接入和 QQ 跨天使用观察仍按 MC07 接续。

**后续体验阶段：本轮不实施，先完成 MC07 与跨天实际使用。**

**目录与导入名统一（2026-10-28）**：按用户要求，本机目录正式改为 `F:\game\projectNEKO\rin-memory` 与 `F:\game\projectNEKO\dsh-rin-memory`，源码包为 `src/rin_memory`，不再保留旧的源码目录与 CLI 别名。当前核心版本 `0.1.0.dev8`、DSH 插件 `0.2.7`；构建入口、资源打包、测试、启动脚本、运行配置与 N.E.K.O 导入引用同步。核心全量 469 项通过（保留 1 skipped），DSH 82 项通过，宿主导入相关 15 项通过。当前推送不会触发自动部署，发布使用固定提交与手动部署流程。

**铃芽 RIN 更名（2026-10-28）**：GitHub 核心仓库与 Python 发行包改为 `rin-memory`（`0.1.0.dev7`），DSH 仓库与 npm 包改为 `dsh-rin-memory` / `@local/dsh-rin-memory`（`0.2.6`）。两份 README 按 Recall / Identity / Nurture 重写，统一铃芽标识、品牌释义与项目简介；管理台关于页和插件展示信息同步。新增 `rin-memory`、`rin-memory-run`、`rin-stack` CLI，旧命令、Python 导入名、运行根和记忆来源标识继续兼容。包安装与 stack 相关 7 项检查通过（保留 1 skipped），DSH 82 项通过；本机旧发行包已替换并核对命令入口。

**服务器部署（2026-10-28）**：三个仓库已推送 origin/main，代码提交为核心 `54993c9`、DSH 插件 `3e4f2be`、QQ bridge `b455749`。服务器在 `/opt/companion-src/{rin-memory,dsh-rin-memory,dsh-qq-bridge}` 保存固定提交的 Git 工作树；核心 wheel 与 Git 中全部 Python 源文件逐一核对一致，插件从服务器工作树打包后通过 `dsh plugin --profile web add` 安装。运行版本为核心 `0.1.0.dev6`、DSH 插件 `0.2.5`、桥接 `1.0.6-neko.4`。既有 `dsh-server` 身份增加 `persona_teach=true`、`memory_outputs=true`、`trusted_plugins=['dsh_memory']`；人格卡与当前模型配置沿用服务器现状。

部署验收：`rin-memory` 与 `dsh-web` 均 active/running、NRestarts=0；记忆存储/聊天/后台 ready、outbox=0；客户端 context 返回当前发布人格，主动候选接口授权访问 200、匿名访问 401；插件状态 connected、bridge subscribed、tools=0、无待补交；OneBot get_status 返回 online=true/good=true，SnowLuma 保持运行。重启后当前没有已绑定的 live QQ 主会话，收到下一条主人 QQ 消息后建立新会话并开始消费主动候选。本轮未向真实 QQ 注入模拟对话或测试消息。本机发布检查发现 M7 新测试的 discover 导入方式问题，已修正；原有 466 项组合与 M7 独立 discover 的 3 项通过，保留既有 1 skipped。

| 方向 | 复用入口与完成重点 | 状态 |
|---|---|---|
| 1. 教导理解与规则整理 | O02；复杂条件、授权示例、规则替换与收窄，减少重复和冲突；服务端仍验证来源和改卡范围 | 未开始 |
| 2. 持续兴趣与小目标 | O02 / X05；保存自选兴趣、项目进度、实际成果和放弃原因，从持续选择形成偏好 | 未开始 |
| 3. 当前相处行为选择 | O01 / O03；按具体情境选择倾听、建议、短回复或接梗，沿用现有生成前流程 | 未开始 |
| 4. 可见养成与纠错 | O05；管理台展示已学规则、共同经历、变化依据与主动原因，并支持纠正和撤回 | 未开始 |
| 自主发展与服务器活动 | X05；专属服务器执行自选项目、产出作品与技能，实际经历经核心回写、值得分享的成果进入待聊队列；框架继续本机开发与版本包发布 | 未开始 |

**2026-10-29 本机交付与联合验收（RO01—RO04）**：实施方案见 [铃芽 RIN 继续优化方案](rin-next-optimization-plan.md)。QQ 主动搭话的取材、候选、时机与生成改由 DSH 插件负责；RIN 提供已发布人格、结构化兴趣依据、相关记忆、依据校验和输出/投递存档；用户画像先放在人格卡“关于主人”段落。支持零记忆探索，保留主请求零工具、来源删除和送达回执闭环。补齐新会话最小认识与本轮相关事实选择、CQ 正文清理、语义气泡、事件机会与开口空窗、任意人格段落和对话示例；推荐先交付 RO04-A，再推进 RO01 → RO02 → RO03 → RO04-B。分群记忆与自主服务器项目继续暂缓。

| 工作包 | 交付重点 | 状态 |
|---|---|---|
| RO04-A QQ 收发表达 | CQ 清理、图片块保留、语义气泡、可见消息边界、插话与分段回执 | 本机验收完成，待服务器同步 |
| RO01 人格卡自定义 | 任意段落、对话示例、已有段权限与关于主人，复用修订/发布/回滚 | 本机验收完成，待服务器同步 |
| RO02 调用与上下文优化 | 普通提取合批、逐条来源与时间、反思间隔、128K干净分段 | 本机验收完成，待服务器同步 |
| RO03 关于主人与注入 | 兴趣信号、段落取材依据、相关召回、最小背景与第一人称去重注入 | 本机验收完成，待服务器同步 |
| RO04-B DSH 主动搭话 | 统一候选、单轮正文与联系元数据、持久计划、一次性唤醒、通用投递及恢复 | 本机验收完成，待服务器同步 |

本批按用户要求取消独立画像页面、画像专用服务和用量看板；画像复用 `about_owner` 可演化段落。本机交付核心 `0.1.0.dev11`、DSH 插件 `0.3.0`、QQ bridge `1.0.6-neko.7`，尚未推送或同步服务器。本机核心 venv 已由 dev9 更新为 dev11；发行包位于 `.scratch/ro03-ro04-release/`。

统一验收：核心 548 项（547 通过、1 跳过），插件 109 通过；bridge 251 通过、5 条既有 Windows 环境失败，已与基线逐项复现一致，构建通过。官方模型共 5 次调用：识图、招呼、相关召回、主动元数据各一次，另一次完成 5 轮合批，3 条事实均有原话来源，四类兴趣信号齐全。真实 rc.3 SDK 的预算/空闲换段检查通过，旧日志保留，空闲后不回放旧原话；QQ 主会话的原生压力压缩与超限压缩重试均被隔离，工作会话策略保留。只使用模拟 QQ 投递，未向真实 QQ 发测试消息。

RO04-A 已完成本机代码与构建，交付核心 `0.1.0.dev10`、DSH 插件 `0.2.9`、QQ bridge `1.0.6-neko.6`。入站媒体规范化和准入结果先于记忆准备；普通回复、主动消息和咒语后续统一使用语义气泡，结构块保护、Unicode 码点、稳定索引、失败停止与重启恢复已验证。按用户最新要求，所有轮次完整说完再接新消息，不因插话取消；主动轮次从生成到首次投递结束也会先完成，后到输入持久排队。已移除逐增量回发设置，推理与后台过程不转发；第一人称所有格漏转一并补齐。

验证：核心 M7/M5 定向 33 项、DSH 插件 83 项、桥接相关 88 项通过；另有 2 条既有 Windows `/tmp` 路径断言在原 HEAD 复现，未修改。真实官方 API 通过本机 DSH rc.3 原生 Session/LLM 适配器完成识图并生成三个模拟 QQ 气泡，实际请求保留图片、没有 CQ 码或 `tools`，回执索引为 0/1/2。没有向真实 QQ 发送测试消息，服务器版本保持原状。本条保留 RO04-A 当批验收记录，当前 RO01—RO04-B 状态见上表。

**3. 各批执行卡**

**B01｜冻结兼容合同与迁移清单**

目标：把现有有效实现映射到独立核心，明确入口和所有权，避免后续一边搬文件一边猜合同。

工作范围：定向读取当前工作树，列出持久文件/表、来源字段、主体与场景、幂等/删除版本、路由、后台任务、角色与数据根生命周期；记录哪些代码迁入核心、哪些留在宿主、哪些使用兼容包装。确认模型和 token 工具的必要接口，并核对现有测试与固定样本。

交付：在 `design.md` 增补具体迁移表及需补充的接口字段；为可用样本记录来源和用法，新增样本优先使用合成数据。真实数据副本须有明确范围，不把用户原文提交进仓库。

验收：能明确回答“每类数据由谁写、怎样恢复、怎样删除、哪些调用者依赖它”；找到原行为的最小验证入口；所有已知写入路径都有归属。B01 不提前搬迁业务逻辑。

**B02｜原格式存储、来源账本与可恢复提交**

目标：在独立项目中读写原存储格式，形成可靠的持久接收与恢复基础。

工作范围：迁入必要的 JSON/SQLite 读写、原子替换、角色锁、来源账本、请求幂等、事件日志/outbox/cursors 及生命周期；通过配置注入数据根和写入许可。准备原事实、反思、人格等视图的读写适配，不在本批重新实现领域规则。

交付：隔离临时目录可持久保存、退出重开、识别重复来源；`durable=true` 只在约定的持久提交完成后返回。每种已开放事件注册恢复处理器，尚未支持的 schema 明确拒绝。

验收：重开后 ID、主体、时间与版本保持；提交后回执丢失的重试只生效一次；一次中断提交能恢复到一致状态；损坏或未知格式不会被覆盖成空存储。写入仅限测试目录，生产持久入口等 B03 后一起评估。

**B03｜更正、删除屏障与防复活底座**

目标：为后续领域写入提供统一的失效和删除合同。

工作范围：迁入更正记录、提及限制、删除屏障、forget epoch、来源依赖撤回、条目/经历删除与回执；接通 B02 视图、缓存失效及恢复排序。多来源条目保留独立内容；插件清理保持待回执语义。

交付：独立 `correct/forget` 入口、范围明确的回执、旧任务写入拒绝和恢复时屏障优先生效。后续每新增一种存储或派生物，都在该批接入这套删除流程。

验收：修改或删除后目标内容停止进入读取；旧事件重放、在途写入和恢复副本不能复活已删内容；离线插件的清理回执保持待处理。条目删除与整段经历删除继续遵循旧方案各自的数据范围。

**B04｜最近对话、事实抽取与内容闸门**

目标：把输入从“原始演示事件”接成已有的正常对话记忆路径。

工作范围：迁入 recent、原始时间索引、压缩与事实抽取，保留 user/assistant、原事件/镜像、chat/life_fact/preference/shared_experience/game_stream 等内容分类；保留旧 schema 闸门与当前配置开关语义。模型调用通过注入接口，带输入/输出预算、timeout 和可恢复处理位置。

交付：普通对话可进入相应存储，游戏生活聊天可按内容保存，游戏流水/临时要求/战术留在原范围；独立演示能够展示真实抽取链路。自动测试使用固定模型替身，必要实连只使用明确配置的模型。

验收：重复镜像不重复抽取；角色自述不变成用户事实；一次抽取失败可以重试且不丢原文；压缩、晚到结果和重试遵守 B03 的更正/删除屏障。

**B05｜反思、现有人格与成长能力迁入**

目标：完整继承旧系统已经具备的长期理解和成长行为。

工作范围：迁入反思合成、证据、既有人格条目、归档、成长分类与晋升、read/adopted/result/feedback 记录；保留来源、版本、主体与适用范围。原人格快照经接口输入，宿主配置对象留在外部。

交付：事实可以形成有依据的反思和成长，相关条目可以晋升、收窄、被取代或撤回。此次仍为已有成长语义；当前人设卡自动修订放在 B12。

验收：重复自述、镜像、时间和沉默不增加独立支持；shared/game/run 隔离；来源删除能撤回派生人格；旧证据门槛和关闭强力记忆时的行为保持一致。

**B06｜相关召回、经历关联与淡出**

目标：替换 `DemoEventContext`，形成可实际用于对话的上下文提供方。

工作范围：迁入混合检索、当前话题与指代补全、来源背景、episode、淡出、提及冷却和预算；兼容无向量时已有降级方式。区分“记忆可信”“当前适用”“现在值得提起”。

交付：真实 `ContextProvider`，返回有来源的相关条目、修正和成长块；通过注入的 token 工具控制体积；主动候选与普通召回共用必要的相关性规则。

验收：相同输入与固定候选集的行为对齐；“后来/上次那个”能带回相关经历，新话题不硬接旧事；超时取消可选回忆，明确修正和删除仍生效；同一成长不会无条件重复注入。

**B07｜当前状态、后台维护与独立闭环**

目标：完成真正能单独运行的第一版记忆核心。

工作范围：迁入心情、想法、关注、进展和版本提交，明确租约/控制权凭据由宿主提供；统一后台抽取、反思、归档、恢复与维护任务的启动、停止和处理位置。提供显式数据目录与模型配置的独立入口。

交付：在独立临时数据根中完成“记录→抽取→反思/成长→召回→更正/删除→重启恢复”；保持 `demo` 模式可用，但真实流程有明确不同入口和回执。

验收：无 N.E.K.O 模块依赖；旧状态版本与过期租约不能覆盖当前状态；心情衰减不被重启刷新；关闭/重载不留下重复后台任务或越代写入。至此 D1 才标为完成。

**B08｜N.E.K.O 记忆接口与生命周期适配**

目标：让旧调用者通过现有入口访问新引擎。

工作范围：在宿主仓库实现适配器，现有 memory routes、通用 companion 接口、导入路径和后台装配改为委托；接通已有模型配置、存储许可、角色创建/切换/重命名/删除和数据根维护流程。

交付：新引擎只由现有记忆运行时实例化；旧 route/status/error/字段继续兼容；旧后台循环委托或退场，主进程、工具和插件不各自写一份状态。

验收：普通读取、管理写入、导入/导出、配置开关和错误响应的合同通过；角色切换后旧任务无法写回；隔离集成环境中数据只有一个写入者。本批不切换用户正式目录。

**B09｜对话、游戏、语音和 Live2D 兼容验收**

目标：在启用新人设机制之前，把“接入后和现在差不多”验收到具体链路。

工作范围：接通普通文字、语音、主动交流、游戏 context/poll/接管/退出和真实送达记录；检查 TTS、实时音频、打断、气泡、消息反应、Live2D 自动/手动表情动作及口型。只修复接入引起的差异。

交付：D2 的完整接入与兼容结果，记录当前启用的角色及模型模式；自动改卡保持关闭，原角色与资源配置保持原值。

验收：覆盖 design 的 A02—A07、A09、A12 和 A16 中与兼容接入有关的部分；暂停动作仍可聊天，接管输出不重复入库，分类延迟时气泡仍有内容，首字/首音不增加前台模型调用。使用隔离配置/副本运行集成验证，不占用正在使用的正式会话。

**B10｜当前人设卡、版本与人工编辑**

目标：建立可被修订的权威当前卡，并与已有设定编辑方式衔接。

工作范围：一次性导入初始卡、当前卡、段落及主体/场景范围；实现 revision、局部修改、冻结、来源和历史；接通原人设编辑入口；将 `characters.json` 中对应内容改为有版本的人设投影，处理直接改文件产生的用户修改。

交付：可人工编辑和读取的当前卡；初次导入保留原设定和顺序，未明确划分的自由文本继续由用户控制。新卡、历史版本和投影同时接入删除/更正流程。

验收：旧配置不覆盖新卡；手动编辑与基版本冲突正确；人设段落不吞掉宿主工具、语音与表现协议；新版本重启后保持；冻结只约束自动修订。自动改卡仍关闭。

**B11｜人格发布、实时会话与游戏版本一致**

目标：卡片变更实际影响后续生成，同时维持交互连续。

工作范围：区分已提交/已渲染/已采用版本；普通请求更新完整人格快照，实时客户端按其能力更新指令或准备新连接；游戏 poll 的人格文本与版本成对发布，主动生成共用相同来源。

交付：下一安全生成边界采用新版本的发布机制、必要的生效状态和重试；原输入队列、历史、音频和控制权交接继续使用宿主实现。

验收：手改人设后普通聊天、当前启用实时模式、主动生成和游戏下一轮请求内容发生对应变化；更新时继续说话或打断不丢输入、不重复出声；失败时不把未采用版本标成已生效。

**B12｜有依据的自动人设修订**

目标：将已成立的成长转成可审核、可发布的局部卡片修订。

工作范围：新增变化触发、受影响段落选择、结构化提案、语义审核、字段/范围/来源/长度校验、基版本提交与预算；支持替换/合并/删除，不无限追加。沿用 B05 的依据，已吸收成长不重复作为第二个人格块。

交付：可在独立测试环境自动完成一条“经历→成长→局部修订→发布”的流程；明确无变化、待依据、预算耗尽、拒绝及可重试状态；默认不开启正式角色自动演化。

验收：真实独立依据可修订开放段落，重复自述/镜像无效；用户冻结及手改优先；游戏战术不会变成通用性格；无效模型输出保持原卡；演化调用只在后台并遵守预算。

**B13｜演化管理与撤回/删除完整闭环**

目标：用户能理解和控制变化，演化新增的副本全部纳入纠正与遗忘。

工作范围：在宿主相关页面提供当前卡、差异、来源、自动开关、冻结、人工修订和撤回；覆盖修订提案、历史卡、初始导入副本、投影、缓存和待发布版本的来源清理；处理用户修正与演化提交并发。

交付：关闭演化保留当前卡，恢复初始卡是单独操作；撤回变化保留真实经历，删除经历撤回对应派生影响。多来源段落根据剩余依据重建，后台回执可查看。

验收：完成 A10、A11、A13、A14 中涉及演化的部分；删除后回滚历史卡、重启、恢复或插件重连均不复活；一次变化能够查到来源和生效版本。至此 D4 功能闭环完成。

**B14｜发布包、迁移及回滚演练**

目标：把开发成果整理成可安装、可切换、可回退的版本组合。

工作范围：固定核心与宿主适配器版本，构建 wheel，完成独立安装检查；在一致性副本演练停写、备份、恢复屏障、迁移、单写者切换；演练保留新增对话/修正/删除的回滚；补齐运行、导入/导出和故障恢复说明。

交付：发布包、版本对应关系、迁移/回滚命令与数据格式兼容检查；将全部验收场景映射到已有测试或实机步骤，确认必要前置配置。

验收：副本切换与重启恢复通过，回滚后新数据和最新删除仍在；包中不含用户数据或宿主重依赖；未知 schema 有可处理的错误，正式部署步骤可重复执行。本批完成后再进入正式切换。

**B15｜正式切换与完整实机验收**

目标：交付用户日常可用的独立记忆与自演化系统。

前置：C01—C05 收尾完成，B14 版本组合与演练通过；按已有授权核对当次维护窗口、目标配置/数据根/运行进程。

工作范围：按照已演练步骤停写并切换唯一写入者，恢复既有启动方式；先以自动改卡关闭的模式检查原体验，再开启用户选择的演化范围；实际验证文字、语音与打断、Live2D 表情动作、游戏进入退出、卡片修改、一次有来源演化与删除后重启。

交付：对应版本、实际数据位置、演化开关状态、A01—A16 验收结果和可用回滚入口；同步工作区 AGENTS 和运行文档。

验收：所有首版必需场景通过；故障时按演练回滚并保留新数据。未触发的实际演化或游戏场景保留具体待验收项，不能用包安装成功替代其完成状态。完成后进入正常使用与反馈优化。

**4. 每批统一的完成规则**

- 关键路径可用，相关的入口、状态、失败和撤回行为一起完成；独立模块测试通过后，还要在所属阶段完成实际接线。
- 检查范围按本批影响选择，优先复用原测试；默认最多新增 3 个独立风险用例，确需更多时说明原因。文档批次不运行无关业务全量测试。
- 状态标“已完成”时写实际证据，不用“预期可用”代替结果。未提交也能交接，但须说明工作树位置及相关文件。
- 不为测试关闭来源、范围、删除和版本校验。没有必要配置时，继续完成独立部分，留下具体待处理项。
- 新存储、新缓存、新人格副本和新接入方必须在本批加入更正/删除/恢复范围。
- 有关文档同步到最终实现；不保留两套互相矛盾的接口说明。

可复用的单批任务描述：

```text
请先读 rin-memory/AGENTS.md 和 docs/roadmap.md。
当前收尾执行 Cxx（见 docs/closeout.md），先核对前置代码与交付记录，再完成本项及必要配套。
遵守 docs/design.md 的宿主/核心边界；使用隔离测试数据。
完成后运行相关检查，更新 roadmap 台账及该批交付记录。
更新 C 台账及对应 B 批次；发现影响本项的缺口应修复或明确记录。
```

B15 按已有授权的服务范围和维护时机执行，不重复询问已经明确的事项；如果现场状态要求扩大停止范围或处理未保存的游戏进度，先说明具体影响并确定处理方式。

**5. 上线后的优化路线：先改具体体验，再调整算法**

以下 O 系列以 B15 完成为通常前置。若某项问题已经影响首版必需验收，应回到对应 B 批次修复，不将其推迟为优化。O 系列各自可按实际反馈领取，不要求全部完成后才扩展。

| 优化 | 触发条件 | 推荐优先级 | 目标 |
|---|---|---|---|
| O01 对话连续与召回精度 | 记错对象、接不上经历、话题硬转或反复提起 | 优先 | 想起正确、适时且足够的背景 |
| O02 演化质量与人格连续性 | 改卡过快、没有实际变化、风格漂移或自述空转 | 优先 | 变化有来由、影响行为并保留身份连续 |
| O03 心情与主动关注 | 心情跳变、恢复后失真、主动内容无来由 | 次优先 | 稳定而灵活的状态与主动表达 |
| O04 延迟、成本与规模 | 数据增长后变慢、后台调用过多或内存膨胀 | 按指标 | 在相同语义下提高效率 |
| O05 可解释管理与评估 | 用户难以找到错误来源或判断改动效果 | 按使用需求 | 看得懂、改得动、能复现实例 |

**O01｜对话连续与召回精度**

把实际失败对话整理成最小、脱敏或合成的固定回放集，分别检查实体/主体识别、指代补全、时间关系、来源扩展、当前话题和排序。先调整已定位的环节，再考虑跨事件关系检索。

可逐步增加“同一人物/事物在不同时间的变化”“共同目标的多个进展节点”“事件前因与结果”的关联，并保留来源。先利用现有字段与轻量索引；只有关系查询的实际需求超出当前实现时再评估图存储。

完成标准：指定失败样例修复；无关召回、重复提及和跨主体泄漏没有增加；成本/延迟与原结果一起比较。每次优化都能说明改变了哪个可观察行为。

**O02｜演化质量与人格连续性**

从“经历足不足”“理解对不对”“卡片是否真改变行为”三个环节定位问题。优化提案合并、矛盾处理、变化幅度、适用情境和新旧段落取代，避免仅优化文案流畅度。

更大的方向是建立带时间的自我认识：过去怎样、哪些经历改变了看法、现在何时仍保留原倾向。角色可以探索新兴趣，也可以放弃不再适用的习惯；探索意图和已发生结果分开。

完成标准：后续独立情境能观察到相关行为差异；误推断能回撤；固定身份与用户冻结段落稳定；自我总结不形成循环自证。自动调整演化频率只能依据实际质量/成本记录，并受用户设置约束。

**O03｜心情与主动关注**

优化状态之间的联系：事件理解、期待、实际结果、问题解决和兴趣持续性；让不同情境下的情绪有具体来由。关注事项支持继续条件、机会、完成/暂停和结束，不按聊天次数涨好感度。

需要更丰富的外显情绪时，通过宿主适配映射到现有表现能力，仍由 N.E.K.O 管理模型动作及语音。先评估文本、声音、表情之间的一致性，再逐项扩充语义状态。

完成标准：跨天/重启状态合理衰减；不因用户离线积累惩罚；主动表达遵守当前活动与打断规则；延续关注不反复催促用户。

**O04｜延迟、成本与规模**

测量真实瓶颈后再选择缓存、增量索引、批量模型任务、归档分层或存储迁移。缓存键包含主体、场景及相关修正/删除/人格版本，过期结果不会重新注入。

需要更大数据量时，先比较现有 JSON/SQLite 的实际读写、索引与锁开销，再决定是否把特定视图迁到数据库。向量模型或后端替换独立评估，保留无向量检索和可重建派生索引。

完成标准：同一输入与数据规模下性能改善、召回质量保持；迁移可重放/可回退；删除及时生效。只有测量显示需要时才新增服务或依赖。

**O05｜可解释管理与持续评估**

在首版小面板上逐步增加经历时间线、人格变化对比、来源依赖视图、纠正定位和任务状态。把“为什么想起”“为什么改卡”“为什么现在不再使用”展示为简短可追溯说明。

建立可复用的回放与对比命令，记录召回相关性、重复提及、修正生效、人格范围、删除恢复和延迟；需要用户体验评价的部分用具体对话样例评审，避免只依赖模型自评打分。

完成标准：能够从错误回答追到相关来源并纠正，能比较前后行为；日志和分享结果默认脱敏；评估操作不会修改正式记忆。

**6. 更大的扩展路线：按实际用途选择**

| 扩展 | 启动条件与前置 | 首个可交付版本 | 新增影响 |
|---|---|---|---|
| X01 多宿主共享与 SDK | B15 后，出现第二个实际接入应用 | 薄 HTTP 服务＋第二宿主适配示例 | 新部署方式、身份与访问控制 |
| X02 多模态经历记忆 | 有明确的图片/音频/文档记忆需求 | 一类媒体引用及可追溯摘要 | 媒体存储、模型处理与删除范围 |
| X03 跨设备同步与离线 | 明确需要两个设备延续同一角色 | 配对设备同步及离线事件补交 | 同步服务、冲突、加密和设备管理 |
| X04 多角色与共同经历 | 多角色使用已超出当前主体隔离能力 | 独立人格＋显式共享的经历空间 | 新共享权限及引用删除规则 |
| X05 经验到技能与主动学习 | 现有能力有真实重复任务和结果回执 | 一个场景的条件化技能候选与试用 | 执行器适配、授权范围及结果评价 |
| X06 可复用发行与接入生态 | 已有第二宿主验证通用性 | 稳定接口版本、迁移工具与参考适配器 | 发布维护和兼容承诺 |

这些方向是规划，不是当前默认实施范围。引入新付费服务、账号授权、对外网络访问或部署条件时，先把方案、成本及运行方式具体化，再由用户选择。

**X01｜多宿主共享与 SDK**

第一步让现有核心既能嵌入运行，也能由一个服务进程独占数据目录；二者复用同一份领域实现。第二步提供身份/主体/场景明确的事件与上下文 API，完成 N.E.K.O 与一个实际第二宿主的共同角色接入。

第二宿主可选择现有 DSH 桥接应用，先覆盖工作观察和普通交流，不同时重写其委托系统。分别验证来源去重、前台控制权、并发更正、离线补交和跨宿主人格更新。

完成标准：两边共享适用记忆但不串动作权限；数据只有一个权威写入方；一个宿主离线不阻塞另一个；删除可追踪到所有已接入副本。SDK 和服务适配不引入第二套记忆算法。

**X02｜多模态经历记忆**

先选一种有价值的媒体，例如用户主动提供的图片。事件记录媒体引用、摘要、时间、来源和内容范围；原媒体与文字理解分开存储，便于更新解释和删除。

随后按需要扩展音频、文档或片段视频；“实际看过的内容”“模型推测”和“用户补充”保持不同来源。不把桌面持续截图直接变成默认长期记忆。

完成标准：能从召回摘要追溯对应片段；错误理解可修正；媒体、缩略图、转写、embedding 和衍生人格均能按范围删除；处理成本有明确预算。

**X03｜跨设备同步与离线**

先确定设备身份、同步权限、加密与密钥管理，再设计事件交换、稳定来源 ID、删除 tombstone、版本冲突和离线恢复。分清可重放事件、可重建视图和必须仲裁的当前人格/控制权。

第一版优先单一权威端加离线提交队列；只有确需多端同时写当前状态时再扩展仲裁。离线设备重连先接收删除和有效版本，再评估本地未提交事件。

完成标准：设备丢失可撤销；离线补交不覆盖新卡；删除跨设备可追踪；旧备份和断网设备不复活内容。同步元数据和账号凭据不进入人格提示词。

**X04｜多角色与共同经历**

保留每个角色独立的人设、私有记忆和关系认识，新增可显式共享的共同事件；共享事实与各自感受分别记录。角色切换继续使用独立生效版本，不用简单合并记忆池实现群体人格。

先做一个由用户指定的共享经历空间，再考虑角色间转述与共同任务。转述保留原作者和可信来源，不能由某个角色的推测直接改写另一个角色的自我认识。

完成标准：私有信息不被共享检索带出；共享事件删除能够通知引用方；一个角色退出/删除不会误删其他独立经历；关系条目仍按主体隔离。

**X05｜经验到技能与主动学习**

从现有某类重复任务出发，将已验证经验整理为“适用条件、步骤、结果检查、失败条件”的技能候选，由宿主能力执行；先提供建议或在已授权范围试用，再依据真实回执决定保留。

角色可以提出想继续了解或尝试的事情，记录探索目标与收获。知识学习、相处习惯、工具技能和代码修改是不同能力；自改人设不授予修改代码、扩大工具权限或自行部署的能力。

完成标准：技能能在相似情境改善结果，条件不匹配时不会强行执行；失败可撤回；来源删除能清理对应候选；执行成本和权限由宿主控制。

**X06｜可复用发行与接入生态**

在至少两个真实宿主验证通用合同后，稳定公开接口、schema 版本、升级策略和错误语义。提供参考适配器、接入检查、可重放样例和清晰的贡献边界。

按实际使用需求发布 Python 包和可选服务镜像，后续再评估其他语言客户端。每次版本升级都有兼容范围和数据迁移说明，不靠用户同时升级全部宿主保持工作。

完成标准：新接入方按文档能完成事件提交、上下文读取、人格更新及删除回执；许可与来源齐全；发行包无凭据和用户数据；旧版本升级失败有明确恢复路径。

**7. 推荐的首版后顺序**

默认先正常使用，按反馈选择 O01/O02，出现性能或管理问题再处理 O04/O05；O03 根据陪伴体验推进。建议把第一项大型扩展放在 X01，用第二宿主验证核心的通用性。

X02、X04、X05 按实际场景独立选择；X03 涉及同步与密钥管理，放在本地单写者和删除恢复长期稳定之后。X06 在第二宿主接入成熟后整理，不提前建设完整插件市场。

每个 O/X 项目开工时复用本文件的批次格式：补充目标场景、当前问题、前置、最小交付、验证和新增部署影响。需要多批时使用 `X01.1`、`X01.2` 细分；保留 B 编号不重排，便于追踪旧交付。
