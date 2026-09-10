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
```

别名：`/维基`、`/wikisearch`。

页面标题可以直接包含空格；下划线也会被转换为空格。`-d` 强制显示摘要，`-s` 在单个主名字空间页面不存在时继续搜索。

`@机器人 [[页面名]]` 始终有效。配置项“未 @ 机器人时也自动展开”默认关闭；开启后，普通群消息中的 `[[页面名]]` 也会触发查询。

## QQ 链接发送

插件默认直接返回 `https://...` 纯文本链接。若 aiocqhttp/OneBot 被 QQ 风控拦截，可在插件配置中把 `qq_link_prefix` 设置为 `#`，插件会仅对 QQ 链接添加前缀。

多结果会作为一条带空行的普通文本消息发送。可通过 AstrBot 的 `platform_settings.forward_threshold` 让较长的 QQ 回复自动折叠为合并转发。

## 发布前

请修改 `metadata.yaml` 中的 `author` 和 `repo`，并按照 AstrBot 插件市场要求补充仓库地址、许可证和图标。
