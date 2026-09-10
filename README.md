# AstrBot MediaWiki 页面查询插件

通过 MediaWiki Action API 查询页面摘要与链接。实现参考了 SILI-agent 的 MediaWiki 插件，但使用 AstrBot 当前的 Star 插件接口和异步 HTTP 客户端。

## 功能

- 返回页面规范链接或较短的 `curid` 链接
- 返回页面导言摘要
- 处理重定向、章节锚点、跨 Wiki 链接和不存在页面
- 一次查询最多 5 个页面
- 页面不存在时可搜索相近结果
- 可选自动展开普通消息中的 `[[页面名]]`
- 支持直接发送 `@机器人 [[页面名]]`，并阻止同一消息继续触发 LLM 重复回复
- 订阅某个分类及其子分类中的页面编辑，定时主动推送到当前群聊或私聊
- 推送编辑者、字节变化、编辑时间、差异链接与逐行增删预览
- 首次订阅跳过历史，持久化时间水位、分类成员快照和发送失败队列
- 对未安装 TextExtracts 扩展的 Wiki 自动退化为仅返回页面信息
- 避免 `Special:MyPage`、`Special:MyTalk` 等特殊页暴露请求端身份

## 安装

将插件目录放入 AstrBot 的 `data/plugins/astrbot_plugin_mediawiki`，或推送到 GitHub 后通过 WebUI 安装。插件依赖会根据 `requirements.txt` 安装。

安装后，在插件配置中将 `api_url` 改为目标 Wiki 的 API 地址，例如：

```text
https://zh.wikipedia.org/w/api.php
https://www.mediawiki.org/w/api.php
```

地址必须以 `/api.php` 结尾。部分站点要求使用指定 User-Agent，请同时修改 `user_agent`。

## 指令

```text
/wiki 页面名
/wiki 页面名#章节
/wiki 页面1|页面2|页面3
/wiki -d 页面名
/wiki -s 不存在的页面
/wiki搜索 关键词
@机器人 [[页面名]]
/wiki监控 分类名
/wiki取消监控 分类名
/wiki取消监控 全部
/wiki监控列表
/wiki检查更新
```

别名：`/维基`、`/wikisearch`。

页面标题可以直接包含空格；下划线也会被转换为空格。`-d` 强制显示摘要，`-s` 在单个主名字空间页面不存在时继续搜索。

`@机器人 [[页面名]]` 始终有效。配置项“未 @ 机器人时也自动展开”默认关闭；开启后，普通群消息中的 `[[页面名]]` 也会触发查询。

## 分类变更推送

AstrBot 管理员可以在需要接收通知的群聊或私聊中发送：

```text
/wiki监控 BanG Dream!
```

插件会把它规范化为 `Category:BanG Dream!`，递归读取分类成员，并记录当前会话的 `unified_msg_origin` 作为主动推送目标。首次订阅只建立当前时间水位，不补发已有历史。之后的消息格式类似：

```text
下定决心Hand in Hand
§ LoveLive!学园偶像祭 ～课后活动～（SIFAC）
+95 | Καλλιόπη | 1:57
https://zh.moegirl.org.cn/index.php?diff=8651943&oldid=8651800
✏添加｢==翻唱版本==｣
✏添加｢===[[You and Idol光之美少女]]=== （TBA）｣
✏添加｢{{You_and_Idol光之美少女}}｣
```

一个页面同时属于当前会话订阅的多个分类时只发送一次，并合并显示匹配分类。发送失败的通知不会丢弃，会写入插件数据目录并在后续轮询重试。取消最后一个监控后会清空时间水位和待发送队列。

相关配置：

- `change_push_enabled`：后台轮询总开关，默认开启。
- `change_poll_interval_seconds`：轮询间隔，最小 30 秒，默认 120 秒。
- `change_category_depth`：子分类递归深度，默认 2。
- `change_max_members`：每项监控最多保存的分类成员数，默认 5000。
- `change_diff_lines`：每条通知最多显示的增删行，设为 0 可关闭 compare 请求。
- `change_include_bot_edits`：是否包含机器人编辑。

持久化状态位于 AstrBot 的 `data/plugin_data/astrbot_plugin_mediawiki/change_monitor.json`，插件更新不会覆盖。分类树很大时请降低递归深度或提高检查间隔。

实现参考：

- [MediaWiki RecentChanges、Categorymembers 与 Compare API](https://www.mediawiki.org/wiki/API:RecentChanges)
- [RcGcDw](https://github.com/mwzhx/RcGcDw) 的最近更改与差异预览设计
- [astrbot_plugin_rsshub](https://github.com/AstrBot-Elementary-School/astrbot_plugin_rsshub) 的首次建水位、去重及失败重试思路

SILI-agent 当前仓库没有可直接移植的 MediaWiki 最近更改推送模块，因此这里只延续原页面查询插件，并使用 AstrBot 的主动消息接口独立实现轮询和推送。

## QQ 链接发送

插件默认直接返回 `https://...` 纯文本链接。若 aiocqhttp/OneBot 被 QQ 风控拦截，可在插件配置中把 `qq_link_prefix` 设置为 `#`，插件会仅对 QQ 链接添加前缀。

多结果会作为一条带空行的普通文本消息发送。可通过 AstrBot 的 `platform_settings.forward_threshold` 让较长的 QQ 回复自动折叠为合并转发。

## 发布前

请修改 `metadata.yaml` 中的 `author` 和 `repo`，并按照 AstrBot 插件市场要求补充仓库地址、许可证和图标。
