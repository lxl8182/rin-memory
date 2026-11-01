# 铃芽 RIN 管理台：新实例启动与接口

更新：2026-10-24。S05 服务侧与非宿主闭环已完成，版本 `0.1.0.dev2`。独立服务持有唯一核心和维护循环，水彩管理台由同一个进程托管；宿主接入继续按 S05 的 A/C/D 推进。

## 1. 启动与初始化

先按 [README](../README.md) 将 wheel 安装到 `.venv`，再运行：

```powershell
.\tools\stack.ps1 init -Config F:\game\projectNEKO\companion-runtime\config\stack.toml
.\tools\stack.ps1 start -Config F:\game\projectNEKO\companion-runtime\config\stack.toml
.\tools\stack.ps1 status -Config F:\game\projectNEKO\companion-runtime\config\stack.toml
.\tools\stack.ps1 stop -Config F:\game\projectNEKO\companion-runtime\config\stack.toml
```

默认打开 [管理台](http://127.0.0.1:48931)。空库先填写显示名与初始人格，再首次发布；身份段默认冻结自动演化，人工编辑仍可进行。稳定 `character_id` 来自新配置。显示名是人格卡身份段（`identity`）的第一行，固定写成 `名字: <名字>`，后面接昵称、性别、年龄等描述；管理台人格卡编辑器、初始引导和宿主角色卡改名写的都是这一行。编辑身份正文时若漏掉名字行，服务会自动把原名字行补回首行，因此不会因为改正文而丢名字。游戏侧读取人格卡作为角色设定，不再使用 `characters.json` 的资料字段。旧版本留下的 `service_character.json` 只在启动时一次性并入人格卡（卡上已有名字时段落优先），并入成功后删除该文件。重复 init 或创建请求保留已有配置、人格与显示名。

发布页的使用方状态是唯一提示位置：顶部导航不再有发布小圆点，`chat`（管理台自己的对话）显示为“本机对话”，因为它每轮直接读取当前发布版本，不参与采用流程；只有真正接入的外部使用方（如 `neko_chat`）才会显示“待生效”。

`stack.toml` 的路径相对其所在目录解析，`runtime.python` 为绝对解释器，`runtime.package_version` 固定安装版本。memory/host/logs/state 必须互不嵌套且位于源码仓库之外。`models.service` 的 `api_key_env` 填环境变量名；缺模型时管理功能保持可用，状态/设置页列出需要填写的字段。模型与凭据填好后重启。演化从 off 开始，propose/auto 由管理台显式选择并持久保存。

N.E.K.O 文字接线已启用。模板默认 `host.enabled=false`；填写 `host.source`（带 `companion_host.py` 的源码目录）、`host.python` 和 `host.client_id`，并配置同名 `clients` 身份后设为 true。新入口先启动记忆服务，再启动宿主；状态显示各自实例，日志分别为 `memory-service.log` / `neko-host.log`。`host.port/memory_port/tool_port/plugin_port` 默认 48911/48912/48915/48916。宿主文字模型默认沿用服务 conversation；填写 `models.host.base_url/conversation` 时改用该组配置和其 `api_key_env`。`presentation.live2d_model` 相对新 host 根的 live2d 目录。当前文字验收关闭 TTS，语音与主动/实时/游戏消费继续推进。

宿主数据、配置、插件状态与启动锁写入新 host 根；不探测旧锚点、不导入旧角色与旧记忆。复用模型素材时只复制所选资源。生成前固定已发布人格与删除/更正依据；生成后完整轮次写入宿主有界队列，先持久保存再上传。服务已落盘但响应丢失时同键重放，不再次生成或播放。N.E.K.O 页面实际收到完整文本后回报显示回执；此回执描述文字显示，语音播放回执待语音接线。使用方 `neko_chat` 与管理台 `chat` 分开显示。宿主的记忆管理页面跳转到同一个铃芽管理台。

服务故障时暂停新的文字生成，页面给出记忆服务不可用提示；已生成轮次由后台定时补交。删除或更正使依据失效时丢弃关联待同步正文，只留请求标识与错误码。人格编辑与发布现在两端同一张卡：铃芽管理台与宿主角色设置都读写服务的当前人格。宿主客户端需在 `stack.toml` 显式配置 `host_persona = true`（默认关闭）才可使用 §2.3 的三个 `/v1/host/persona` 端点；宿主页面按段落编辑、分开保存草稿与发布，冲突保留草稿并显示服务当前 revision，服务不可用时保存失败而不会写入本地替代人格。宿主生成的对白仍以 `neko_chat` 记录采用与显示，主动文字用 `neko_active`，两者互不覆盖。

重复 start 返回同一实例。端口被其他进程占用时直接报 `service.port` 冲突。启动失败查 `logs/memory-service.log`；status 显示配置、根、PID、实例、各能力与积压。停止先关闭 HTTP 入口、排空工作及维护循环，再释放根锁并写 `state/stack.json` 的退出结果。无法联系的实例返回 unreachable，启动器不会凭旧 PID 结束无关进程。

服务从已安装的 wheel 加载 HTML、JS、CSS、图片，子进程工作目录在新 state 根；无需源码目录。`tools/stack.ps1 -Python <解释器>` 和安装命令 `rin-stack` 支持其他明确的安装位置。开发用 `webui/server.py --demo` 继续用于隔离样稿/接口测试，不作为新系统日常入口。

## 2. 服务管理合同

只监听 `127.0.0.1`，校验 Host 与同源 Origin。`GET /v1/health`、`GET /v1/bootstrap` 和静态页面可直接读取，其余 `/v1` 请求均需 `X-Companion-Client` 与 `X-Companion-Token`。bootstrap 提供当前页面的 console 身份；重启后页面遇到 401 会重新建立身份并重试一次，保留草稿。独立客户端使用 §2.3 的配置身份。

服务器管理台可通过 SSH 本地转发访问，例如 `ssh -N -L 127.0.0.1:14931:127.0.0.1:48931 用户@服务器`，浏览器打开 `http://127.0.0.1:14931`。同源校验按请求的 `Host` 与 `Origin` 比较主机和端口，因此本机转发端口可以与服务器监听端口不同；其他站点、主机或端口的来源仍被拒绝。

管理权限绑定本实例配置的角色；管理台查看该角色各范围的数据，显式传入其他 character_id/subject 返回 403。HTTP 接口与 `MemoryApi`、页面业务视图共用服务已经持有的核心，没有第二套领域实现。

提案的“修改前”读取其基础版本的历史原文，“修改后”读取该提案的操作；自动采纳、后续人工编辑或历史归档都不改写这组对比。演化响应中的 `changes` 包含各段的 before/after/before_available/noop，`accepted_revision` 标明实际写入版本；历史原文被清理时明确提示不可用，使用现有人设冒充旧文本的回退已移除。空修改提示仅用于尚待审核、所有操作都没有改变的提案。

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
| GET `/v1/host/persona` | 需该客户端在 `stack.toml` 中配置 `host_persona = true`；返回当前卡的 `revision/initial_revision/sections`（含 section_id/kind/scope/editable_by/frozen）与 `published`（version/revision/target/published_at/stale） | 只读；无授予 403，console 标志不等于该能力 |
| POST `/v1/host/persona` | `base_revision` + `operations`（op=add/replace/merge/delete、section_id、text）；按 CAS 修订 | 冲突 409 且回传 `current`（服务当前卡）；拒绝 422；不代替调用方重读最新 revision |
| POST `/v1/host/persona/publish` | `revision`（编辑器看到的版本）与 `target`（仅 `next_turn`）；发布后下一轮采用，在途轮次不切换 | 旧 revision 409；非 `next_turn` 400 |
| GET `/v1/cleanup/pending` | 已绑定 plugin_id 查询待清理来源；memory_ids 与 persona_versions 标识受影响的记忆和失效发布版本，供插件清理已登记的上下文副本；重连后继续返回未确认任务 |
| POST `/v1/cleanup/{deletion_id}/ack` | plugin_id、status=cleaned/failed、cleaned/failed 列表；completed 是 cleaned 的别名；不得确认其他插件的任务 |
| GET `/v1/cleanup/receipts/{id}` | console 查询删除状态与各清理步骤 |
| POST `/v1/cleanup/retry`、`/retract-correction` | console 按 deletion_id 重试清理（force=true 可重做已完成步骤），或按 correction_id 撤回更正；页面有对应操作 |
| POST `/v1/maintenance` | console 手动执行一轮维护；设置页可调用；返回每个步骤状态和积压 |

used 必须对应已提交的生成，delivered 必须在实际显示/播放后上报；读取上下文不建立使用记录。receipt_id 同键同内容幂等，异内容为 409；回执随 journal 恢复，删除后审计标记 invalidated。服务保留最近 2,048 条消费回执，聊天页 sessionStorage 保存待重试请求键与草稿。

sent 只表示平台接收，不表示用户已读。分批接受的 QQ 分段使用各自稳定回执键，重试不重复计数。私人 QQ 聊天提交时明确 `applies_to = 'shared'`，保持 plugin scene 作为来源，避免把普通个人喜好限定到该插件。

时间流只将明确的 `game:<game_id>` 场景归为“游戏”。`plugin:dsh_memory` 显示为“DSH 对话”，其它插件场景显示为“插件会话 · <plugin_id>”，均归入“对话”；插件来源本身不作为游戏分类依据。该规则同时用于列表、筛选计数和详情，已有记录按来源直接显示。

N.E.K.O 文字客户端的 system 只包含固定宿主规则与本轮采用的已发布人格。服务召回和更正作为标明来源的背景，在对应用户消息后追加；相同背景在仍有效的历史中只保留一份，工具后续请求使用同一轮快照。近期历史只在会话初始化或删除、更正版本变化时从服务重建，普通轮次沿用原生已提交历史；人设发布换版从下一轮更新 system。

共同游戏经历的来源与适用范围分别保存：有相处意义的经历默认共享给普通聊天按需召回，仍保留游戏、局次、episode 和 source_key；明确的 game/run 范围继续生效。战术、构筑和临时安排仍由游戏插件管理。

按 source_key 删除时同时清理 recent 原文、时间索引和 FTS；按事实条目删除保留无关的原始对话。清理器先去掉已被屏障覆盖的窗口内容，避免另一项待清理来源阻塞本次清理与后续聊天。新轮上下文也经过屏障过滤。

`service.max_pending_events` 默认 1,024；chat/turns/events 共用容量并为在途输入预留位置，满额返回 429，已提交回放继续可用。HTTP、模型并发和超时分别沿用 stack 配置；容量拒绝不进入等待队列，所以 chat 的 queue_ms 为 0。

---

## 3. 目录现状

```
webui/
├─ server.py                     # API + 静态服务（stdlib）
├─ seed.py                       # 演示数据种子与 live 核心构造
├─ testbed/                      # C04 独立测试前端（index.html / app.js / style.css）
├─ tests/test_testbed_api.py     # 6 个 HTTP 验收用例
└─ designs/tidebell/
   ├─ index-watercolor.html      # 唯一保留的设计稿
   ├─ live.js                    # 实时层
   ├─ motion.css / motion.js     # 风铃与海面动效（可被 body.calm 关闭）
   └─ assets/
      ├─ rin-logo-refined.svg    # 品牌标（同时用作 favicon）
      ├─ xiaofeiyu-portrait.png  # 概览角色头像（名字由 API 覆盖）
      ├─ sakura-wave.png         # 页脚樱花枝
      └─ motion/                 # 海面、风铃、花瓣、纸纹、页脚水波
```

已删除：其余 9 套设计稿（cinematic / hoshikuzu / lightnovel / magical / manga / moss / neon / tsukishiro / vinyl）、`designs/index.html` 画廊页、旧版 `designs/tidebell/index.html`、`PROMPTS.md`、`webui/index.html` 与 `webui/assets/**`、以及未引用的候选图与花铃素材。

改这些删除的原因：管理台只保留一个前端，避免同一份 API 对应多套页面、也避免 `webui/assets` 与 `designs/**` 两套美术资源同时存在。`/testbed/` 是 C04 的可测试前端且被测试覆盖，保留。

---

## 4. 设计评审

### 4.1 总评

视觉 8.5 / 信息架构 8 / 交互 7.5 / 产品完成度 7。

做得好的：

- **页头是整套设计的灵魂**：海面、樱花、风铃、RIN 三段关键词、品牌句与定位句压在一个紧凑高度里，插画完整，信息也完整。
- **字体有分工**：Bookman 斜体做眉题与数字，楷体做品牌句、解释与落款，宋体做标题，互不抢戏。
- **导航稳定**：两字标签 + 固定左侧「铃芽」+ 吸顶导航与吸顶页头，切换页面时标签不再左右跳。
- **概览的数据表达**：五格统计 + 21 天潮位图，数字大、标签小，今天单独用粉色标出。
- **人格页结构成熟**：左侧语义段落按类别分色，右侧修订历史 + diff，主操作在页头右侧。
- **页脚克制**：低矮落款 + 水波 + 右上角贴边樱花枝。
- **真实 API 已接上**：九个页面都能读真实快照，主要写操作都有真实回执。

### 4.2 P0

1. **移除设计预览顶栏。**（已做）原顶栏 `← 返回方案选择 | 2026.10.23 · 霜降 | 角色 小肥鱼 ▾ | 引擎运行中` 是设计画廊的壳，且 `../index.html` 已不存在。现在只留一行状态：`本地记忆 · 只读/可写 · 角色 小铃`，由 `meta` 驱动；重复的「引擎运行中」已删除。
2. **统一「角色 / 系统」的身份层级。** 页头是「铃芽 RIN」（产品），概览卡是角色名（已由 API 的 `meta.character` 驱动，真实根显示「小铃」），人格正文可能自称别的名字。需要在文案上定死：铃芽 RIN 是记忆系统，角色名是它管理的对象。头像按约定固定使用 `assets/xiaofeiyu-portrait.png`，不随角色名变化。
3. **无变化提案。**（已做）概览卡片只显示「驳回这条空提案」，演化详情隐藏「采纳」并给出说明。剩下的根治建议是数据层直接过滤 `operation.text === 当前段落` 的提案。

### 4.3 P1

4. **2560 尺度。**（已做）新增 `@media (min-width:2200px)`：shell 2040px、页头 300px、插画 672px、RIN 关键词 30px、导航 56px；吸顶偏移改用 `--nav-h` 变量自动跟随。
5. **清理页太长。**（部分已做）已有类型筛选与每页 10 条分页、删除链接改为危险色。仍建议补一个搜索框，并把右栏「已完成」在长列表下做成 sticky 或抽屉。
6. **发布页可读性。**（部分已做）令牌改为前 8 位 + 悬停看全值，措辞改为「使用方」。仍建议给令牌加复制按钮，并在「0/4 已生效」旁补一句等待边界。
7. **设置页语义耦合。**（已做）开关与模式现在互相同步并附带一行说明；剩下的是给「显示」分组标注「仅影响当前页面」。

### 4.4 视觉细节

8. **页头插画左边缘偏硬。**（已做）渐变遮罩从 15% 提到 18%，大屏 18%。
9. **头像风格。** 头像按约定固定为 `xiaofeiyu-portrait.png`，不再替换；若要更贴合水彩，可只加一层柔边/纸张滤镜，不改图。
10. **小字对比度。**（已做）`--ink-3` 由 `#6d6355` 压到 `#655b4e`，`--ink-4` 由 `#8b8172` 压到 `#7d7365`。
11. **红色强调过多。**（已做）导航品牌红点改为中性白点，只保留当前 tab 下划线与预警用的金色圆点。
12. **文案小修。**（已做）「主角色」→「当前角色」、「投影待同步」→「待同步到下一轮对话」、「消费者」→「使用方」。
13. **资源体积。** 首屏图 `assets/motion/seaside-background.png`（约 1.9MB）与风铃素材合计约 9MB。建议转 WebP/AVIF，页头图 `fetchpriority="high"`，页脚与背景图 `loading="lazy"`。
14. **响应式。**（已做）≤1520px 时 RIN 改为压缩排版（关键词 18px、英文 11px），不再整块隐藏。
15. **性能观察。** C05 记录 `snapshot` HTTP p50/p95 ≈ 618/662ms（真实根更大时会更慢）。页面目前每次写操作后整页重拉，若体感变慢，可考虑局部刷新或加短缓存。

### 4.5 本轮已落地的优化

- 页头 RIN 三段删掉后面的解释小字，只留「记忆召回 / 人格身份 / 养成培育」，字号放大到 26px（1920）/ 18px（≤1520），英文 14px；≤1520 不再整块隐藏，改为压缩排版（定位句在该档隐藏）。
- 新增 `@media (min-width:2200px)` 断点：shell 2040px、页头 300px、插画 672px、RIN 关键词 30px、导航 56px，并用 `--nav-h` 变量让吸顶偏移自动跟随。
- 页头插画左缘渐变遮罩从 15% 提到 18%，大屏 18%。
- `--ink-3` / `--ink-4` 各压深一档，小字对比度提高。
- 导航左侧的品牌红点改为中性白点，只保留当前 tab 下划线作为主强调。
- 概览的「主角色 · 投影待同步」改为「当前角色 · 待同步到下一轮对话」。
- 面向用户的「消费者」统一改为「使用方」。
- 版本令牌在表格与 token 行只显示前 8 位，完整值放在 `title` 悬停提示。
- 清理页新增类型筛选（全部 / 反思 / 事实 …）与每页 10 条的「再看 N 条」分页，删除链接改为危险色。
- 无变化提案：概览卡片只给「驳回这条空提案」，演化详情隐藏「采纳」并显示说明文字。
- 设置页的「后台自动演化」开关与「关闭 / 提议 / 自动」chips 联动：关开关同时置 off，选 off 同时关开关，选提议/自动时自动开启。

### 4.6 剩余优先级

1. 数据层过滤无变化提案，而不是只在 UI 隐藏「采纳」。
2. 清理页补搜索框；长列表下右栏改 sticky 或抽屉。
3. 首屏图转 WebP/AVIF 并加 `fetchpriority`；页脚图 `loading="lazy"`。

---

## 5. 已知边界

- 服务只监听回环，没有鉴权与 CSRF 防护；不要用 `--host 0.0.0.0` 暴露到局域网。
- `--memory-root` 只读模式下的所有写按钮都会返回 403，这是保护而非 bug。
- 同一数据根同时只允许一个写者：宿主引擎在跑时不要再加 `--allow-write`。
- 角色自动识别只在根下恰好一个角色目录时生效，多角色根请显式 `--character`。
- 删除与更正走引擎并生成墓碑；删除后同一来源不会因重放复活，这条已有测试覆盖。
