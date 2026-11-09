# 铃芽 RIN 管理台与客户端合同

管理台由独立服务同源托管。本文维护操作入口和 HTTP 字段。完整记忆行为见 [记忆流程](memory-flow.md)，部署及验收状态见 [维护台账](roadmap.md)。

## 1. 启动与初始化

先按 [README](../README.md) 将 wheel 安装到 `.venv`，再运行：

```powershell
.\tools\stack.ps1 init -Config F:\game\projectNEKO\companion-runtime\config\stack.toml
.\tools\stack.ps1 start -Config F:\game\projectNEKO\companion-runtime\config\stack.toml
.\tools\stack.ps1 status -Config F:\game\projectNEKO\companion-runtime\config\stack.toml
.\tools\stack.ps1 stop -Config F:\game\projectNEKO\companion-runtime\config\stack.toml
```

默认打开 [管理台](http://127.0.0.1:48931)。空库先填写显示名与初始人格，再首次发布；身份段默认冻结自动演化，人工编辑仍可进行。稳定 `character_id` 来自栈配置。显示名是人格卡身份段（`identity`）的第一行，固定写成 `名字: <名字>`，后面接昵称、性别、年龄等描述；管理台人格卡编辑器、初始引导和宿主角色卡改名写的都是这一行。编辑身份正文时若漏掉名字行，服务会自动把原名字行补回首行，因此不会因为改正文而丢名字。游戏侧读取人格卡作为角色设定，不再使用 `characters.json` 的资料字段。旧版本留下的 `service_character.json` 只在启动时一次性并入人格卡（卡上已有名字时段落优先），并入成功后删除该文件。重复 init 或创建请求保留已有配置、人格与显示名。

发布页的使用方状态是唯一提示位置：顶部导航不再有发布小圆点，`chat`（管理台自己的对话）显示为“本机对话”，因为它每轮直接读取当前发布版本，不参与采用流程；只有真正接入的外部使用方（如 `neko_chat`）才会显示“待生效”。

`stack.toml` 的路径相对其所在目录解析，`runtime.python` 为绝对解释器，`runtime.package_version` 固定安装版本。memory/host/logs/state 必须互不嵌套且位于源码仓库之外。`models.service` 的 `api_key_env` 填环境变量名；缺模型时管理功能保持可用，状态/设置页列出需要填写的字段。模型与凭据填好后重启。演化从 off 开始，propose/auto 由管理台显式选择并持久保存。

工作区的 N.E.K.O 文字宿主与管理台使用同一个服务。宿主启动、运行根和版本钉住见 [独立服务](standalone-service.md)。宿主作为单独使用方显示其采用与送达状态。

## 2. 服务管理合同

只监听 `127.0.0.1`，校验 Host 与同源 Origin。`GET /v1/health`、`GET /v1/bootstrap` 和静态页面可直接读取，其余 `/v1` 请求均需 `X-Companion-Client` 与 `X-Companion-Token`。bootstrap 提供当前页面的 console 身份；重启后页面遇到 401 会重新建立身份并重试一次，保留草稿。独立客户端使用 §2.3 的配置身份。

服务器管理台可通过 SSH 本地转发访问，例如 `ssh -N -L 127.0.0.1:14931:127.0.0.1:48931 用户@服务器`，浏览器打开 `http://127.0.0.1:14931`。同源校验按请求的 `Host` 与 `Origin` 比较主机和端口，因此本机转发端口可以与服务器监听端口不同；其他站点、主机或端口的来源仍被拒绝。

管理权限绑定本实例配置的角色；管理台查看该角色各范围的数据，显式传入其他 character_id/subject 返回 403。HTTP 接口与 `MemoryApi`、页面业务视图共用服务已经持有的核心，没有第二套领域实现。

提案的“修改前”读取其基础版本的历史原文，“修改后”读取该提案的操作；自动采纳、后续人工编辑或历史归档都不改写这组对比。演化响应中的 `changes` 包含各段的 before/after/before_available/noop，`accepted_revision` 标明实际写入版本；历史原文被清理时明确提示不可用，使用现有人设冒充旧文本的回退已移除。空修改提示仅用于尚待审核、所有操作都没有改变的提案。

反思与提案列表为空时，详情同时清空并隐藏操作，不显示示例。事实的 `confidence` 只保留原记录中有效的 0—1 数值，缺失时返回 `null`，页面显示“未评分”；提炼流程没有生成该评分，不再用默认 0.80 代替。反思的证据强度是衰减后的正向支持减去反证，自动演化另检查独立经历数量、反馈或实际结果、来源有效性与段落权限，没有 0.9 的置信度门槛。

“发布”黄点表示有未发布的人格修改，或已接入的使用方尚未采用新版本/采用失败；悬停显示具体原因。聊天已采用且没有待发布修改时黄点消失。未接入的主动/实时/游戏标为“未接入”，不计入等待数量和采用比例。

| 方法和路径 | 操作 | 幂等与错误 |
|---|---|---|
| GET `/v1/health` | 存储/聊天/维护/管理能力、实例；不含对话正文或凭据 | 只读；Host/Origin 不符为 403 |
| GET `/v1/bootstrap`、`/v1/initialization` | 页面写身份、创建/发布状态与具体缺项 | 只读 |
| POST `/v1/characters` | `character_id/display_name/text` 创建初始人格 | 同一配置角色只导入一次，不覆盖后续编辑；400/403 |
| GET `/v1/meta`、`/v1/snapshot`、`/v1/overview` | 管理快照 | 只读 |
| GET `/v1/timeline`、`/v1/events/{id}`、`/v1/facts`、`/v1/reflections`、`/v1/search` | 时间流、详情、事实、反思/成长、检索 | 查询复用原管理视图；不存在为 404 |
| GET `/v1/persona`、`/v1/evolution`、`/v1/cleanup`、`/v1/state`、`/v1/engine/status`、`/v1/export` | 人格历史/diff、提案/清理/状态、导出 | 只读 |
| POST `/v1/persona/revise` | `base_revision/operations/reason` 人工编辑 | CAS 冲突 409；页面保留草稿和打开时的 base_revision |
| POST `/v1/persona/freeze`、`/restore`、`/publish`、`/rollback` | 冻结/还原/发布/回滚 | 发布当前编辑卡的 revision；拒绝 422；实际采用由使用回执记录，手工 `/adopt` 返回 400 |
| POST `/v1/evolution/mode`、`/settings`、`/evaluate`、`/retry`、`/propose` | 模式与提案操作 | 复用已有冷却/执行状态及回执；无模型不能生成提案 |
| POST `/v1/evolution/{id}/accept`、`/reject`、`/withdraw` | 提案审核 | 复用 proposal_id 与领域幂等 |
| POST `/v1/cleanup/correct`、`/delete` | 更正、删除与请求结果失效 | 删除使用 request_id；正文和派生数据由领域服务处理 |
| POST `/v1/chat/send` | `text`、可选 `request_id`；管理台直接生成并提交一轮 | 同 `request_id` 回放已提交结果；换文本同键为 409；只读根 403；瞬时模型故障 504/可重试 503 不关闭能力 |
| GET `/v1/chat/history` | 对话记录（`recent.json` 已提交的双方原文）、整轮提交账本与能力状态 | 只读；没有配置模型也能读；不含凭据 |
| GET `/v1/model/config` | 接口地址、各档模型名、思考强度、凭据**来源**（console/environment/none） | 只读；不含凭据本身，也不含掩码前缀 |
| POST `/v1/model/config` | `base_url`、`models`、`api_key`、`reasoning_effort`、`clear_api_key` | 立即生效；省略/空 `api_key` 保留已存值；非法值 400 并指明字段；只读根 403 |
| POST `/v1/model/test` | 用当前配置向服务商发一次最小请求 | 返回 `ok` 与失败 `kind`（auth/config/timeout/rate_limit/transport/format）；演示模型拒答 |
| POST `/v1/stack/stop` | 请求有序退出 | 202；需当前写身份 |

统一错误信封包含 `code/message/retryable/request_id`。401 为页面写身份过期/缺失；403 为来源或范围错误；400 为参数错误；409 为编辑冲突；422 为领域拒绝；429 为有界容量占满；503 为服务关闭/能力不可用。修订的 CAS、初始导入、发布和提案幂等均沿用领域实现。

QQ 主动完成接口 `POST /v1/proactive/complete` 可传 `part_sizes`，与 `/v1/outputs` 使用同一分段合同：正整数组成的列表，总和等于规范正文的 Unicode 码点数。分隔换行计入相邻片段，表情字符计一个码点。服务固定分段后，bridge 按原索引发送并以 `/v1/outputs/deliver` 逐段回报；中断使用 `kind: interrupted`，已送达片段保留在近期对话中。

`/v1/chat`、`/v1/turns`、`/v1/context`、`/v1/requests/{id}` 继续复用 S04，补充合同见 §2.3。聊天提交后记录 used，页面显示后记录 delivered；主动/实时/游戏消费者在实际接入前显示未接入。

### 2.1 管理台直连对话（不经过宿主）

导航「对话」页可以直接和服务对话，**不需要先接宿主**。实现是 `service/chat.py` 的 `ChatBridge`：它把一条消息组装成标准 `TurnRequest`，再交给 `MemoryApi.chat`，因此整轮提交、幂等重放、`context_stale`、容量与停机拒绝全部沿用 S02/S04 的既有实现，没有第二套领域逻辑。

- 回复会带人格（当前已发布版本）与本轮 `build_turn_context` 召回的记忆；双方原文经同一提交写入 `recent.json`，下一轮自然可召回。
- 页面历史直接读 `recent.json`，不是页面的本地副本；删除或更正导致的失效会同时反映在提交账本状态上。
- `request_id` 是幂等键：页面在重试时复用它，服务端回放同一结果而不是再生成一轮。演示根未配置模型时使用 `DemoChatModel`，回复明确写着「离线演示模型」。
- 凭据可来自环境变量或管理台保存的本机配置；任何响应都不返回凭据值。对话页显示实际模型名。
- 只读根（未加 `--allow-write`）可以查看历史，但发送会被 403 拒绝并说明原因。
- 对话页支持清空当前网页聊天、编辑双方历史文字、从指定回复重新生成，以及折叠查看模型返回的思考内容。编辑后的文字用于后续对话；重新生成会替换所选回复与后续聊天，失败保留原记录。清空保留长期事实、人格和其他会话。思考内容单独保存，不作为回复正文或后续聊天输入；旧消息没有保存该字段时显示无思考内容。

单独使用开发管理台时配置模型：

```bash
uv run python webui/server.py --memory-root <根> --allow-write \
  --model conversation=<模型名> --model-base-url https://host/v1 \
  --model-api-key-env COMPANION_MODEL_API_KEY
```

服务模式下由 `stack.toml` 的 `[models.service]` 提供同样的配置，页面自动走 `/v1/chat/send`。

### 2.2 在页面里配置模型接口

设置页的「模型接口」面板可以直接填写接口地址、API Key、对话模型、提炼/反思模型与思考强度，**保存后立即生效，不需要改环境变量或重启**。首轮运行没有 `stack.toml` 时，这里是唯一的配置入口。

- 接口地址填到 `/v1` 即可，程序自己接 `/chat/completions`；误填完整路径也会自动去掉后半段。
- 思考强度以 `reasoning_effort` 发送（低/中/高）。选「跟随服务端默认」时**不附加该字段**，因此不识别它的服务商不受影响。
- API Key 输入框留空表示「不修改已保存的值」（页面从不回显，所以无法原样提交回来）；「清除已保存的 Key」需要再点保存才生效。
- `测试连接` 会真的向服务商发一次最小请求，失败时按类别给出可读原因（如「连不上接口地址」「服务商拒绝了这个 Key」），而不是原始异常文本。
- 保存真实配置后，离线演示模型会被移除，后续对话真的走服务商；配置错误时对话返回可重试的 503 而不是退回演示。

凭据存放位置与边界：

| 项 | 约定 |
|---|---|
| 位置 | stack 的 `<paths.state>/memory-service/model_config.json`；开发入口默认使用 memory 根同级 `local_state/model_config.json` |
| 权限 | POSIX 下 `0o600`；Windows 依赖用户目录 ACL |
| 响应 | 任何接口都不返回凭据值，连掩码前缀也不返回（短 Key 的前缀就是 Key） |
| 导出 | 不进入 `/v1/export` |
| 日志 | 适配器从不记录凭据；错误只带失败类别与 HTTP 状态 |
| 优先级 | 管理台录入 > 环境变量（`stack.toml` 的 `api_key_env` 只在没有录入值时使用） |
| 重启 | 启动时读取同一文件，页面配的配置重启后仍在 |

`stack.toml` 继续负责页面不拥有的部分（超时、并发、代理、未被页面提到的档位），页面只覆盖它填过的非空字段。并发上限刻意不支持运行时修改：替换信号量会让新旧两批请求同时在途，正是 R07 消除的过载状态，因此该值在重启后生效。

`live.js` 在新服务页面中使用 `/v1`；开发样稿仍可使用原 `/api`。空库显示创建/发布引导；保存中禁用重复提交，失败保留输入。服务断线显示重连界面，重试不会切回演示数据。设置页展示新配置缺项；冻结只约束自动演化，人工编辑可正常修订。头像继续复用既有设计素材，显示名取自人格卡身份段首行。

### 2.3 非宿主客户端合同

客户端身份在 `stack.toml` 配置，令牌只引用环境变量名；未配置身份不能读取私有数据或写入。console 管理本角色；其他客户端只可调用上下文、整轮、事件和自身消费/清理回执。

`memory_awareness` 由核心按客户端能力、反思配置和 `off/propose/auto` 模式生成，包含 `text/version/capabilities`。版本由有效能力与正文确定，同状态保持稳定。网页对话和 DSH 主会话在系统提示中采用一份；旧服务缺少该字段时，DSH 沿用原装配。

明确教导使用独立的 `persona_teach` 授权。`POST /v1/persona/teach` 从已保存的用户来源生成完整表达段落；`POST /v1/persona/teach/revoke` 按来源重组正文。模型等待期间允许其他编辑，提交时复核版本与来源。回执 `pending` 表示人格已保存、发布仍待续接；再次提交同一来源会继续已完成步骤。段落依据保存在现有人格及教导记录中，覆盖改写后的撤回、删除、历史与恢复。

```toml
[clients.game-client]
token_env = 'COMPANION_GAME_TOKEN'
subjects = ['private']
plugin_id = 'game_plugin'
```

所有数据请求携带 `character_id/subject/session_id/scene`；client_id 来自认证身份，不得冒用。`scene` 使用 `game:<id>`、`plugin:<id>`、`run:<id>` 或空串。source_key 按 `event_sources.py` 构造，必须匹配角色、主体和已绑定插件；同键查询和回放仍校验完整作用域。

| 方法和路径 | 请求与结果 |
|---|---|
| POST `/v1/context` | text 与作用域；返回固定已发布 persona/version/revision、memory_awareness（text/version/capabilities）、必需 corrections、可选 context、recent_turns、随机 context_receipt_id、basis；只读，凭据有效期 300 秒 |
| POST `/v1/context/reserve` | 有效上下文凭据与稳定 request_id；在生成前持久登记角色/客户端/主体/会话/场景及人格版本、删除更正依据，不保存人格正文；只供该轮补交，服务重启后仍复核屏障 |
| POST `/v1/context/validate` | 同作用域与 context_receipt_id；删除、更正、过期、重启或作用域变化返回 409/context_stale、needs_reissue=true；发布新版本不改在途快照 |
| POST `/v1/turns` | 稳定 request_id/turn_id/source_key/occurred_at、完整 messages/reply；声明生成版本时须带 context_receipt_id；同键异正文或 reply 为 409，失效为 410，持久提交失败为可重试 503 |
| POST `/v1/chat` | 同上，服务生成并提交；同会话在途返回 202，同键回放不再生成；新轮返回 context/model/commit/queue 毫秒耗时 |
| GET `/v1/requests/{id}` | 带原 subject/client/session/scene 查询；返回提交结果或失效 410，不向其他作用域返回正文 |
| POST `/v1/events` | source_key、event_id、event_type、text、occurred_at、content_class、scene/applies_to；原始事件与提炼复用既有持久管线，重复不再追加，删除后重放 410 |
| POST、GET `/v1/receipts` | POST 带 request_id/receipt_id/kind，kind 为 used/sent/delivered/interrupted/failed；sent 带 transport 和已接受分段 parts（platform_id/chars），送达/打断带实际 delivered_chars；GET 仅返回本作用域元数据，无回复正文 |
| GET `/v1/memory/entries` | 客户端需显式授予 `memory_tools = true`；仅列其授权 subject 的 fact/reflection，可按 text 筛选 |
| POST `/v1/memory/correct` | 同上；subject、old_text/new_text 和稳定 request_id，复用核心更正规则，写入共享的个人范围 |
| POST `/v1/memory/forget` | 同上；target（fact/reflection 的 type/id）必须属于该 subject；text 多条匹配返回 409/ambiguous_target；source_key 必须属于该角色/主体和客户端绑定插件；episode 删除走管理台 |
| GET `/v1/host/persona` | 需该客户端在 `stack.toml` 中配置 `host_persona = true`；返回当前卡的 `revision/initial_revision/sections`（含 section_id/kind/scope/editable_by/frozen）与 `published`（version/revision/target/published_at/stale）。无授权返回 403 |
| POST `/v1/host/persona` | `base_revision` + `operations`（op=add/replace/merge/delete、section_id、text）；按 CAS 修订。冲突 409 并返回 `current`，拒绝 422 |
| POST `/v1/host/persona/publish` | `revision`（编辑器看到的版本）与 `target`（仅 `next_turn`）；发布后下一轮采用，在途轮次不切换。旧 revision 返回 409，非法 target 返回 400 |
| GET `/v1/cleanup/pending` | 已绑定 plugin_id 查询待清理来源；memory_ids 与 persona_versions 标识受影响的记忆和失效发布版本，供插件清理已登记的上下文副本；重连后继续返回未确认任务 |
| POST `/v1/cleanup/{deletion_id}/ack` | plugin_id、status=cleaned/failed、cleaned/failed 列表；completed 是 cleaned 的别名；不得确认其他插件的任务 |
| GET `/v1/cleanup/receipts/{id}` | console 查询删除状态与各清理步骤 |
| GET `/v1/cleanup?receipts_only=1` | console 刷新清理回执，返回停止召回、本地清理和接入端确认状态 |
| POST `/v1/cleanup/retry`、`/retract-correction` | console 按 deletion_id 重试清理（force=true 可重做已完成步骤），或按 correction_id 撤回更正；页面有对应操作 |
| POST `/v1/maintenance` | console 手动执行一轮维护；设置页可调用；返回每个步骤状态和积压 |

启用记忆的 QQ 私聊可在 `POST /v1/context` 提交 `segment_history`，含 `mode`（`budget` 或 `topic`）与最多 30 条 `exchanges`（`source_key`、`user`、`reply`）。核心按认证主体、插件场景及来源账本过滤，返回 `context.segment_sources`、可选 `segment_anchors` 和 `segment_detail`。预约时在 `consumer_refs.source_ids` 中提交计划带入的键，服务只登记签发范围内的来源。

### 2.4 普通聊天的选择与采用

`context.selection` 包含 `version/status/items`。`version: 1` 的条目带原记录 `id`、稳定 `key`、正文和 `source_keys`。聊天用召回、工作摘要和经历投影共用这份选择，管理台查询仍可查看完整记录。

| 状态 | 含义 |
|---|---|
| `selected` / `empty` | 有入选材料 / 正常无入选 |
| `unavailable` / `timed_out` / `invalid_output` | 筛选不可用、超过预算或返回不合法 |
| `disabled` / `superseded` | 记忆暂停，或上下文在构建时已失效 |

`superseded` 描述核心内部的旧结果。HTTP 层发现依据变化时可直接返回 `409/context_stale`，客户端需重新请求上下文。

预约提交 `consumer_refs.selection_version: 1` 及 `memory_ids/source_ids` 两个数组时，服务器只接受已签发集合的子集。显式空数组保持为空。旧客户端沿用保守登记方式。预约先保留清理依据；DSH 在材料进入会话后随 `POST /v1/turns` 提交 `memory_selection_adopted: true`，服务将对应预约标为已采用。

used 必须对应已提交的生成，delivered 必须在实际显示/播放后上报；读取上下文不建立使用记录。receipt_id 同键同内容幂等，异内容为 409；回执随 journal 恢复，删除后审计标记 invalidated。服务保留最近 2,048 条消费回执，聊天页 sessionStorage 保存待重试请求键与草稿。

sent 表示平台接收。分批接受的 QQ 分段使用各自稳定回执键，重试不重复计数。QQ 轮次提交 `reply_transport: "qq"` 时，发送回执用 `parts[].index` 和 `parts[].text` 逐段补齐对话原话。生成结果保存在轮次记录，回执查询只返回元数据。私人 QQ 聊天提交时明确 `applies_to = 'shared'`，保持 plugin scene 作为来源，个人喜好可跨场景使用。

管理台「人格卡 → QQ 好友私聊人设」通过 `POST /v1/qq/persona` 保存 `text` 和 `base_revision`。已认证客户端可用 `GET /v1/qq/persona` 读取公开人设。携带好友 `subject` 时同时返回该客户端对该主体的记忆授权和服务记忆开关状态。

时间流只将明确的 `game:<game_id>` 场景归为“游戏”。`plugin:dsh_memory` 显示为“DSH 对话”，其它插件场景显示为“插件会话 · <plugin_id>”，均归入“对话”；插件来源本身不作为游戏分类依据。该规则同时用于列表、筛选计数和详情，已有记录按来源直接显示。

N.E.K.O 文字客户端的 system 只包含固定宿主规则与本轮采用的已发布人格。服务召回和更正作为标明来源的背景，在对应用户消息后追加；相同背景在仍有效的历史中只保留一份，工具后续请求使用同一轮快照。近期历史只在会话初始化或删除、更正版本变化时从服务重建，普通轮次沿用原生已提交历史；人设发布换版从下一轮更新 system。

共同游戏经历的来源与适用范围分别保存：有相处意义的经历默认共享给普通聊天按需召回，仍保留游戏、局次、episode 和 source_key；明确的 game/run 范围继续生效。战术、构筑和临时安排仍由游戏插件管理。

按 source_key 删除时同时清理 recent 原文、时间索引和 FTS；按事实条目删除保留无关的原始对话。清理器先去掉已被屏障覆盖的窗口内容，避免另一项待清理来源阻塞本次清理与后续聊天。新轮上下文也经过屏障过滤。

管理台向 `POST /v1/cleanup/delete` 提交 `defer_cleanup: true`。服务持久写入删除屏障后返回 202，随后继续清理。本地数据与接入端副本分别确认，管理台轮询清理回执。省略该字段时沿用同步响应。

按事实 ID 删除时，只有登记过该条记忆的接入端收到副本清理待办。事实所引用的原话保留；按来源或整段经历删除时，才按来源定位聊天副本。

`service.max_pending_events` 默认 1,024；chat/turns/events 共用容量并为在途输入预留位置，满额返回 429，已提交回放继续可用。HTTP、模型并发和超时分别沿用 stack 配置；容量拒绝不进入等待队列，所以 chat 的 queue_ms 为 0。

---

## 3. 前端入口

水彩管理台在 [webui/designs/tidebell](../webui/designs/tidebell/)，页面入口为 `index-watercolor.html`，HTTP 数据与交互由 `live.js` 接入。`webui/server.py` 提供同源页面及只读或演示入口，`webui/testbed` 是独立测试页。

## 4. 运行边界

- 正式栈使用回环地址、客户端认证和 Host/Origin 校验。生产数据由独立服务单写。
- 只读模式可以查记录，写入返回 403。创建、发布、回滚、清理及模型配置使用管理身份。
- 人格、事实和派生数据的变更走服务合同。审计使用稳定 ID 和回执。
- 早期视觉评审和测试页决策见 [整理前原稿](https://github.com/lxl8182/rin-memory/blob/0ee569e21d788076389211e4fada827c8e4631c7/docs/webui-console.md)。
