# 第三方代码与许可证

## BiliShelf 标签补全模块

本项目的 src/bili_platform/web_assets/vendor/custom-tag-suggestions.js 复用自 [TLRKFXE/BiliShelf](https://github.com/TLRKFXE/BiliShelf/blob/ac2c56e1bcb69b4c28cfeca35695a77a5322ed35/extension/utils/custom-tag-suggestions.js)，固定提交 ac2c56e1bcb69b4c28cfeca35695a77a5322ed35。

Copyright (c) 2026 TLRK。原MIT许可证完整保留在同目录 BiliShelf-LICENSE.txt；本项目的MIT许可证不替代原作者声明。

本项目参考标签与实体关系分离、字段检索和预览交互。Python后台、SQLite、本地备注、扩展桥接与公共SDK是本项目维护的适配实现；没有把整套BiliShelf扩展声明成原创。

## 可选上游验证工具

 tools/bilishelf_trial.mjs 与 tools/bilishelf_sync_trial.mjs 是针对上述固定源码提交的验证适配器。用户自行准备上游源码路径，使用 examples/synthetic-dataset.json 作为离线合成输入；这些工具不附带上游工程或私人样本，也不是产品运行依赖。构建出的上游切片继续受原许可证约束，不应将生成目录提交到本仓库。

## 运行依赖与可选工具

Python依赖及来源记录在pyproject.toml与uv.lock中，由安装器获取，不在仓库中复制依赖树。FFmpeg由用户自行安装并配置，本仓库不分发FFmpeg二进制。B站内容、账号会话和运行资料不属于本仓库的许可分发内容。
