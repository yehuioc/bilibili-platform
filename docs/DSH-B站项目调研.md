# DSH与B站工具的机制比较

这是2026-10-22对固定源码的研究，非热度排名或上游当前状态保证。只比较公开工程机制，没有分发账号、采集正文或私人验收记录。当前实现以项目README为准，下列候选未全部集成本项目。

## 十个固定来源

| 项目 | 源码版本 / 最新提交日 | 实际机制与入口 | 可参考之处 | 关键边界 |
|---|---|---|---|---|
| [c1overzz/dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll) | 2.0.1 / 10-22 | DSH设置页配置，宿主起headless会话，Python MCP提供搜索和字幕，结果写日报与逐条记录 | 将逐条读取交给后台任务，按BVID查重 | 主要是关键词发现；README记录部分DSH桌面版本自动运行卡住，不能只凭“有定时器”认定稳定自动化 |
| [Alterssss/dsh-bili-whale](https://github.com/Alterssss/dsh-bili-whale) | 0.4.5 / 10-22 | DSH设置页、调度器、工具链；组合字幕、弹幕、评论、画面，写日记与推荐 | 对重画面视频补充证据，配置和工具链共用文件 | 带人格、互动与随机作息；这些不服务当前收藏整理，不宜整套继承；“看懂”是作者目标，未在本机测准确率 |
| [whiteguo233/OpenBiliClaw](https://github.com/whiteguo233/OpenBiliClaw) | 0.3.231 / 10-22 | 真正的浏览器扩展 + 本地FastAPI/SQLite + Web/移动入口；另有DSH客户端仓库 | 最直接的“插件与本地工作台同库”参考；后台内容发现、反馈与保存状态贯通 | 整体跨平台与画像系统很大，pyproject仍标Pre-Alpha；主要使命是推荐发现，不能直接等同于收藏标签系统 |
| [Debuger-114514/dsh-bili-jar](https://github.com/Debuger-114514/dsh-bili-jar) | 0.2.5 / 10-22 | DSH右下角挂件；兴趣轴、候选池、规则排序，前24条交给模型挑5条 | 便捷挂件、批量调用、理由缓存、可调整兴趣轴 | 核对curator代码，模型主要收到标题、UP、分区、tag和互动数据，不含视频全文；便宜不等于已经理解内容；README明确没有端到端测试 |
| [YOYOLnFu/dsh-bili-pond](https://github.com/YOYOLnFu/dsh-bili-pond) | 1.3.1 / 10-22 | DSH设置页、headless任务、自选模型、进度与失败展示、日报及已看查重 | 减少人工调度，处理中的阶段和失败能直接看到 | README明确基于bili-scroll 0.1.10衍生后重写，配置与接口不兼容；应按同一谱系比较，不算全新发明 |
| [Yuuyuko-uu/dsh-bili-subagent](https://github.com/Yuuyuko-uu/dsh-bili-subagent) | 0.7.5 / 10-22 | 触发指定DSH会话，`bili_scan`调用宿主原生子代理，内部报告交回主会话 | 把内容处理与最终呈现分开，不必让用户在聊天中逐条提交视频 | 明确fork自bili-scroll；依赖DSH的subagents/agents/llm等服务，不能直接复制为Codex后台入口；本版移除了本地转写 |
| [chzmpro/dsh-bilibili-companion](https://github.com/chzmpro/dsh-bilibili-companion) | 最新提交10-19 | Python MCP bridge + SQLite + DSH设置页；文字worker提炼，主Agent拿短候选包并可回查证据 | 原文留本地、短结果进入主上下文的成本分层 | 引入独立豆包模型/密钥；README说明真实读取与真实发布未验收。83.7%是作者架构估算，不能当成实测节省，更不能套到我们的订阅额度 |
| [CZX2244/dsh-bilibili](https://github.com/CZX2244/dsh-bilibili) | 0.7.5 / 10-10 | DSH工具插件，字幕优先；模型识别缺口再指定时间抽帧，提供ASR及视觉引擎选项 | 最贴近重画面、图集及无完整口述内容的按需补证据路线 | 是提取工具，不是完整收藏管理挂件；ASR、视觉模型与可能的媒体下载都有额外资源及数据发送成本 |
| [moxingovo/dsh-bilibili](https://github.com/moxingovo/dsh-bilibili) | 0.2.4 / 09-30 | 搜索、元数据、字幕、播放地址、画面工具；有结构化错误与字幕截断标记 | 小而明确的工具契约、Cookie不随重定向外发、字幕上限与错误分型 | 是读取底层，不负责自动任务或私人标签；不能因工具齐全就认为端到端产品已完成 |
| [pyf2818/dsh-bili-widget](https://github.com/pyf2818/dsh-bili-widget) | 0.1.0 / 08-17 | DSH内浮窗、小窗播放、连播与历史；界面经宿主HTTP路由读写本机JSON | 一眼可用的轻入口，证明浮窗与宿主落盘可分工 | 主要改善观看，缺少内容理解；关注/历史是插件自己的本地记录；Windows/DSH宿主依赖，不能当成任意浏览器扩展 |

## 对本项目的启发

多入口消费同一份后台数据，可以避免扩展与工作台各建权威库再互相合并。长轮询、断线重连和数据持久化仍须实际实现，不能由共库一词代替。

元数据可用于筛选候选，却不能证明模型看过完整内容。可先读可靠字幕，发现画面缺口再按时间补图；原文保留本地，短判断引用可回查证据。压缩单个会话输入不等于模型总成本消失。

本地标签、个人备注、模型建议和站内收藏关系应分开。内容标签不能自动推断用户的收藏用途，无字幕、旧视频或低播放也不等于没有价值。站内变更需要独立预览与读回。

DSH宿主的headless任务或原生子代理调用不能直接复制成其他载体的执行器。当前本项目提供材料与结构化动作，但不以提示词或研究候选冒充已接通的无人值守模型服务。

## 固定提交出处

| 仓库 | 主线提交 | 重点核对文件 |
|---|---|---|
| c1overzz/dsh-bili-scroll | `31e3874f30418735692889305410253b625adef9` | `README.md`、`package.json`、`index.js`、`lib/state.mjs` |
| Alterssss/dsh-bili-whale | `ef3f74171b4104249f5583c4d2df9bebad14934a` | `README.md`、`package.json`、`lib/scheduler.js` |
| whiteguo233/OpenBiliClaw | `008e0a1eb409efd5db3172ba1cc652893c3aed3f` | `pyproject.toml`、`docs/architecture-overview.md`、`docs/modules/saved-sync.md`、`src/openbiliclaw/saved_sync/service.py`、`src/openbiliclaw/web/js/api.js`、`extension/src/shared/backend-endpoint.ts`、`extension/popup/popup-connection-poller.js` |
| Debuger-114514/dsh-bili-jar | `00b25295d517b45674a7814c6559ba949fef49bd` | `README.md`、`package.json`、`index.js`、`lib/curator.mjs`、`lib/reasons.mjs` |
| YOYOLnFu/dsh-bili-pond | `46a377fae4140c77f2850126926192f3f9949555` | `README.md`、`package.json`、`index.js` |
| Yuuyuko-uu/dsh-bili-subagent | `57616b9c121a53ff44d6621b6f9caaf038a047c4` | `README.md`、`package.json`、`index.js` |
| chzmpro/dsh-bilibili-companion | `eb857a0b738c5bb06bc667342d7eb30466b00379` | `README.md`、`core/candidate_digest.py`；根目录无`package.json`，未将此视为JS插件缺失 |
| CZX2244/dsh-bilibili | `2b8e4852f2a36a44bae2148254276697314b018c` | `README.md`、`package.json`、`lib/index.js`、`lib/extractor.js` |
| moxingovo/dsh-bilibili | `42cd1c9e7d975fc46cf62bd86024a134c37501d7` | `README.md`、`package.json`、`src/provider.ts` |
| pyf2818/dsh-bili-widget | `7d564f5f134389a905b5f39485469d602eee2b4a` | `README.md`、`package.json`、`lib/index.js` |

上述判断基于当时README、依赖清单和关键源码，没有运行全部上游测试。作者的兼容与节省声明保留为自述；引用研究不构成代码分发许可，实际吸收前仍须核对各组件许可证。
