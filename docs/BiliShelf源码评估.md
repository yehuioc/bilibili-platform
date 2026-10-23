# BiliShelf复用边界

本文是2026-10-22的固定源码评估，当前产品入口以本项目README为准，不代表上游最新版本。

对象：[TLRKFXE/BiliShelf](https://github.com/TLRKFXE/BiliShelf)，主线提交 ac2c56e1bcb69b4c28cfeca35695a77a5322ed35；当时发布标签v1.0.1.1指向6525053ec1ea428b2aa01be052c35462e353e138。以下讨论机制，不分发实际账号或内容样本。

## 可复用的机制

- 视频、收藏夹关联、标签和视频标签关系分别建模，同一视频可有多个关联。
- 自定义标签去重与补全、本地字段检索、JSON导出恢复，可帮助用户在原有收藏关系之外找回内容。
- 同步平台简介与保存人工标注是不同职责，不应把私人备注放进会被刷新覆盖的平台description。
- 本地事务、撤销或回收站不等于站内收藏操作的原子回滚，界面必须区分本地成功和平台写入成功。

## 固定版本的结构与约束

extension使用WXT与扩展运行时，frontend使用Vue、Pinia和路由；主库以IndexedDB中的状态对象维护。前端出现/api路径不意味着有独立可运行后台。其后台文件包含较多业务，整套搬入将同时带入宿主生命周期、平台协议和持久化职责。

该版本独立personalNote字段不明确，description兼有平台简介职责；管理操作以本地库为主，不能将本地复制或移动当作已经更改B站。AI整理的输入和输出范围也需要单独检查，不能因有AI按钮就认定它读过完整字幕或画面。

出处：[数据模型与后台](https://github.com/TLRKFXE/BiliShelf/blob/ac2c56e1bcb69b4c28cfeca35695a77a5322ed35/extension/entrypoints/background.ts)、[前端传输](https://github.com/TLRKFXE/BiliShelf/blob/ac2c56e1bcb69b4c28cfeca35695a77a5322ed35/frontend/src/lib/api.ts)、[扩展配置](https://github.com/TLRKFXE/BiliShelf/blob/ac2c56e1bcb69b4c28cfeca35695a77a5322ed35/extension/wxt.config.ts)。

## 本项目实际采用

仅直接复用MIT标签补全模块，保留原许可证。独立备注、SQLite持久化、本机HTTP、平台SDK及站内收藏执行由本项目维护；没有引入整个Vue前端或background。

源码试验适配器在tools目录，输入必须使用明确合成数据。它们只验证固定上游机制，不证明整个上游扩展在其他宿主或未来浏览器中可靠。上游有模型或WebDAV功能，也不代表本项目已经接入这些功能。
