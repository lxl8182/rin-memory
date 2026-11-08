<p align="center">
  <img src="webui/designs/tidebell/assets/rin-logo-refined.svg" width="112" alt="铃芽 RIN">
</p>

<h1 align="center">铃芽 RIN</h1>

<p align="center"><strong>可编辑人格、可追溯记忆、可持续成长的陪伴系统。</strong></p>
<p align="center"><em>风起时铃响，记忆落土发芽。</em></p>

RIN 由 Recall（记忆召回）、Identity（人格身份）和 Nurture（持续养成）组成。独立服务持有记忆与人格，N.E.K.O 和 DSH 使用同一套已发布人格与有范围的记忆。水彩管理台与服务同源，可以查看原话、事实、经历、成长及回执，也可以编辑、发布、回滚人格或更正与删除记忆。

## 从哪里开始

| 想做什么 | 入口 |
|---|---|
| 启动服务与管理台 | [构建与启动](#构建与启动) |
| 了解记忆怎么保存、使用和删除 | [完整记忆流程](docs/memory-flow.md) |
| 在 DSH / QQ 里使用 | [dsh-rin-memory](https://github.com/lxl8182/dsh-rin-memory) 与 [dsh-qq-bridge](https://github.com/lxl8182/dsh-qq-bridge) |
| 接入自己的客户端或宿主 | [独立服务](docs/standalone-service.md) 与 [客户端合同](docs/webui-console.md#23-非宿主客户端合同) |
| 接手开发或查询交付状态 | [AGENTS.md](AGENTS.md)、[维护台账](docs/roadmap.md) 与 [历史索引](docs/history.md) |

## 构建与启动

需要 Python 3.11+。核心运行依赖为零。克隆项目后，在仓库目录创建并激活虚拟环境。

```bash
python -m venv .venv
```

Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1`，Linux / macOS 使用 `source .venv/bin/activate`。

```bash
python -m pip install .
rin-stack init --config ./runtime/config/stack.toml
rin-stack start --config ./runtime/config/stack.toml
rin-stack status --config ./runtime/config/stack.toml
rin-stack stop --config ./runtime/config/stack.toml
```

首次打开 [管理台](http://127.0.0.1:48931) 后创建角色、填写初始人格并发布。模型档位与凭据环境变量可写入 `stack.toml`，也可在管理台设置模型连接。没有配置模型时可以先管理已保存数据。

工作区的 `start.cmd` 启动服务与管理台，`start-rin-stack.cmd` 启动独立服务和可选 N.E.K.O 宿主。数据根、版本钉住和端口见 [独立服务](docs/standalone-service.md) 与 [开发运行说明](AGENTS.md#3-运行与验证)。

## 日常记忆与人格

普通对话先存原话，再按证据和后续用途提取事实或更新事项。下次聊天按当前话题选择可用记忆。明确历史查询可找到仍有效的旧材料。更正和遗忘会更新核心记录并通知有副本的接入方。

明确教导使用已保存的用户原话形成可撤销的表达规则。后台反思和人格演化按证据、模式与段落权限运行；接入方按发布版本采用。

暂停长期记忆时，在 `stack.toml` 的 `[service]` 设置 `memory_enabled = false` 并重启。原话存档和普通聊天继续，已有事实和待处理任务保留。暂停期间的轮次不新增提取任务。

## 构建版本包

用 `uv build` 构建 wheel 与 sdist。服务安装新的 wheel 后需同步 `runtime.package_version`，DSH 插件另行打包与安装。当前发布、安装和验证状态见 [维护台账](docs/roadmap.md)。

## 许可

[Apache-2.0](LICENSE)。项目保留迁入实现的版权头、适用 NOTICE 和修改说明。
