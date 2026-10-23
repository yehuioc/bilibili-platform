# B站工作台与公共插件底座

本机 Python 后台 + Chrome 侧栏扩展：主动绑定一个 B站标签页，读取页面或控制播放，并在工作台查看真实结果；收藏插件使用单独配置的账号会话创建私密收藏夹、复制和移动收藏视频。另有本地标签、备注、搜索及备份界面。

版本：0.1.0。`git_mode: independent`。本仓库采用 MIT 许可证；复用模块的原作者声明见 [第三方代码](THIRD_PARTY_NOTICES.md)。

## 能做什么

| 能力 | 当前实现与边界 |
|---|---|
| 浏览器连接 | Chrome 扩展与本机后台配对、令牌持久化、后台重启后恢复；扩展侧栏须打开 |
| 浏览器操作 | 只操作用户绑定并允许的B站标签页：页面文字/链接/播放器状态、页面导航、滚动、播放/暂停/跳转；不开放任意脚本 |
| 收藏管理 | 读取账号与收藏夹，创建私密收藏夹；选视频→预览→确认→复制或移动→读回结果 |
| 本地书架 | 多标签、独立私人备注、学习状态、字段搜索、本地归档/撤销及同账号JSON备份恢复；不自动回写站内标签 |
| 视频材料 | 指定BVID/分P取得可核对字幕和带时间抽样画面；原视频不落盘，未独立分析原音 |
| 模型接口 | 可导出证据给会话阅读、导入结构化判断；也有显式启用的兼容API分类入口。未内置无人值守模型执行器 |
| 插件扩展 | 可信本地Python插件共用账号、平台SDK、参数校验、任务状态与取消机制 |

未实现音乐识别、评论图片自动保存、音频转写、自动社交互动或全片连续视听理解。元数据、字幕、静帧和模型判断分别标明覆盖范围。

## 安装与启动

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/getting-started/installation/)，以及 Chrome 116+。后台与静态页面不需要 Node.js 或前端构建工具。

```powershell
git clone https://github.com/yehuioc/bilibili-platform.git
cd bilibili-platform
uv sync --locked --no-dev
uv run --no-dev python -B tools/launch_library.py
```

在浏览器打开启动器打印的 [本机连接页](http://127.0.0.1:6861/connection)。Windows 也可双击 `打开拾藏.cmd`。服务仅监听回环地址；保持后台进程运行即可使用，关闭页面不会删除本地标注。首页 `/` 是私人标签书架，默认空库，不附带任何人的收藏或账号资料。

不使用uv时，可自行创建虚拟环境并 `python -m pip install .`，再通过 `bili-platform serve --workspace private/library --port 6861` 启动；该命令的登录参数需显式传入，见下节。

### Chrome扩展

1. 在 Chrome 手动打开 `chrome://extensions`，开启开发者模式，加载本项目的 `extension/` 目录。
2. 点击扩展图标打开侧栏；在连接页生成一次性配对码，侧栏填 `http://127.0.0.1:6861` 与配对码。
3. 在希望操作的B站页面绑定当前标签页，开启“允许工作台操作这个标签页”，保持侧栏打开。
4. 在工作台读取实际页面或控制播放器。停止操作、撤销配对分别控制当前会话与持久凭据。

扩展申请 `activeTab/storage/scripting/sidePanel`，站点访问限本机后台、B站主站与空间。它不读取Cookie。后端收藏账号与Chrome登录账号分别核对，不能把扩展已连接当作后端已有登录。

### 配置自己的B站会话

本项目不提供自动提取浏览器Cookie。将自己合法取得的会话记录保存在忽略目录；[cookies示例](examples/cookies.example.json)仅有合成占位值，须复制到 `private/credentials/` 后编辑，不能直接用于登录。

启动器读取环境变量 `BILIBILI_COOKIE_FILE`，或 `private/library/settings.json` 中的 `cookie_file`。配置示例见 [settings.example.json](examples/settings.example.json)。该字段只保存文件路径，不把凭证复制到源码；凭据只用于B站请求。

```powershell
$env:BILIBILI_COOKIE_FILE = (Resolve-Path private/credentials/session.json).Path
uv run --no-dev python -B tools/launch_library.py
```

直接运行服务时使用 `uv run --no-dev bili-platform serve --workspace private/library --port 6861 --cookie-file private/credentials/session.json`。不配置会话仍可使用本地标注和浏览器连接；平台读取与收藏写入不可用。

工作台读取账号和收藏夹后显示实际账号。复制保留来源；移动先加入目标并核对，再移除来源、再次读回。其他收藏夹关系保留。超时结果未知时不自动重试写请求，部分完成和中断保留检查点；取消不等于撤销，不能保证恢复站内排序。使用真实账号前先核对预览的账号、来源、目标与视频。

## 视频读取与存储

可选安装 [FFmpeg](https://ffmpeg.org/download.html)，在PATH提供 `ffmpeg`，设置 `BILI_FFMPEG`，或在对应私人库 `settings.json` 配置 `ffmpeg_file`。本项目不自动下载工具或模型。没有FFmpeg可用仅字幕动作 `video.text`；画面读取需要FFmpeg。

工作台输入BVID、分P和最多12个画面秒数；默认抽6帧。只从允许的B站CDN按需Range读取，单次累计传输到32MiB停止，原视频不保存；字幕、JPEG和阅读结果的证据目录上限128MiB。磁盘空间不足时停止，不自动删除笔记。

字幕需核对来源、视频与分P身份、时间范围；不透明且不能绑定身份的地址拒用。播放器响应正确或同一字幕重复读取一致，都不足以保证内容没有串稿。身份异常如实保留，不能用错误字幕生成评价；阅读者还应对照实际画面。材料缓存绑定内容摘要和校验版本，旧版本不直接复用。

`video.read` 返回材料包；`video.record` 保存引用本包画面ID的阅读结果。它们不自动启动模型。抽样画面不等于看完全片，可用字幕也不保证覆盖语气、声音与所有视觉信息。

## 模型与分类

默认不发送资料到模型服务。`prepare-session` 导出指定且有可靠字幕的证据包，用户在自己的会话中阅读，再以 `accept-session` 校验精确引文并保存；它不自动创建Codex会话或子代理，也不提供订阅登录令牌转换。

兼容API路径使用自己配置的 `base_url/model/api_key_env`，见 [provider示例](examples/provider.example.json)。先运行 `budget` 展示发送范围和保守预算，再显式提供 `--allow-live --input-cap --output-cap` 才会调用。预算按UTF-8字节加开销保守估计，并非真实token或金额。密钥不要写入可提交文件。

当前分类规则是一个可审阅的示例策略，位于 `classification.py`，不是对所有用户收藏用途的普遍判断。内容判断与人工备注分开；证据不足、抽样和未知分P不能成为自动移走依据。分类结果不会自动执行站内变更；需另建计划并确认。详细CLI见 [能力与可靠性](docs/调研与可靠性.md)。

## 公共底座与插件接口

`bili_platform.BilibiliClient` 统一请求、鉴权、WBI、节流、分页、字幕与收藏操作。业务插件不复制平台协议。`Plugin(name, api_version=1, capabilities, actions)` 的每个 `Action` 声明名称、说明、JSON参数、处理器和 `effect=read|browser|write`。

处理器 `handler(context, args)` 共用 `platform()`、`browser(action,args)`、`check()`、`result(job_id, action)` 与 `checkpoint(value)`。可信本地插件可通过 `bili_platform.plugins` entry point 注册；这不是不可信代码沙箱或插件市场。

```powershell
uv run --no-dev bili-platform control actions
uv run --no-dev bili-platform control call browser.snapshot
uv run --no-dev bili-platform control call favorites.folders
uv run --no-dev bili-platform control call video.read --args-file private/read-args.json
```

请求使用唯一 `request_id`，返回任务ID；相同请求ID不会重复执行。任务失败、取消和后台重启保留真实状态，不自动重放收藏写入。HTTP工作台、扩展与本机CLI分别鉴权；CLI令牌仅从本地文件读取，不开放任意跨域或远程命令。

## 私人数据与备份

运行资料默认位于 `private/`，由Git忽略：库 `library/library.sqlite`、工具内备份 `library/backups/`、材料 `library/readings/`、服务状态/日志、配对与控制令牌。可通过 `--workspace` 使用其他目录，但须自行保证该目录不会被提交。

JSON备份含私人标注和已保存内容，不含Cookie或视频。源码恢复不等于数据恢复；私有资料须自行备份。SQLite复制前停止服务并保留必要的WAL文件。启动器可通过本地配置的 `dataset_file/results_file/annotations_file` 初始化显式指定的资料，不内置或自动寻找私人样本。

公开Git从本版本的干净起点开始，早期本地开发历史不在公开分支或标签中。仓库不分发账号、采集内容、个人笔记、媒体、截图或运行凭证。

## 开发验证

```powershell
uv sync --locked
uv run pytest -q -p no:cacheprovider --basetemp=.tmp/pytest
uv run ruff check src tests
node --test tests/test_extension_protocol.mjs tests/test_browser_site.mjs tests/test_panel_poll.mjs
```

Node仅用于JS测试与可选上游机制试验；建议Node24+。自动测试使用合成数据、模拟网络及隔离临时库，不需要真实B站账号或模型密钥。合成示例中的BVID只验证格式，不能拿去做真实收藏操作。

当前验证覆盖配对/撤销/恢复、浏览器动作返回、收藏预览与写后核对、持久化/备份、字幕边界、画面材料与缓存。已有有界人工平台验证不等于大批量移动、所有视频格式、所有浏览器或未来接口变化都可靠。扩展未在本轮重新操作真实账号，开源发布也不代表服务已经部署到公网。

参考研究：[BiliShelf复用评估](docs/BiliShelf源码评估.md)、[DSH项目机制比较](docs/DSH-B站项目调研.md)。
