# AstrBot MediaWiki 页面查询与变更推送插件

通过 MediaWiki Action API 查询页面摘要与链接。实现参考了 SILI-agent 的 MediaWiki 插件，但使用 AstrBot 当前的 Star 插件接口和异步 HTTP 客户端。

当前版本：`1.4.1`。新增整条监控消息字符预算，默认 900 字符；长段落优先截取实际改动附近的上下文。

可在插件配置中设置 `change_message_max_chars=700`、`change_diff_line_chars=100`、`change_diff_lines=3`，得到更紧凑的消息。整条上限包含标题、摘要、订阅、链接、差异和省略提示，范围 500–5000；差异项目完整保留或省略，不会拆开修改前后。单行字符限制分别作用于修改前后两侧。长度按 Python 字符数计算，不是 UTF-8 字节数。

升级并重新加载后，新生成的通知使用新限制；此前已持久化的失败通知仍为旧文本。标题、摘要和订阅过长时会缩短；异常超长的自定义 API URL 也可能被总长度限制截断。

### 1.4.0 差异预览

- 在同一差异行、未变化上下文一致时，将局部修改展示为 `✏把｢anzu｣改成｢泷泽杏｣`；支持一行多处替换。
- 对应关系不明确时展示修改前后，纯新增和删除继续分别展示，不跨行强行配对。
- 保留链接、模板等 Wikitext 的附近上下文；空白变化用 `␠`、`⇥`、`↵` 等可见符号呈现。
- 预览预算以差异项目为单位，修改前后不会被拆开；优先覆盖不同差异行，超出时提示省略项目数。长片段以省略号截断。
- 标题后的 `§` 表示编辑摘要提供的章节提示，不代表已验证本次只修改该章节；分类匹配范围单独显示为 `订阅：`，单条目订阅不重复标题。
- 差异关闭、获取失败或没有可展示内容时给出提示，保留完整差异链接。

本版覆盖文本差异，不包含模板参数语义解析、段落移动识别或删除/恢复等日志监控。已有待发送通知保存的是旧版文本，升级后重试时仍按原格式发送；新抓取通知使用新版格式。

## 功能

- 返回页面规范链接或较短的 `curid` 链接
- 返回页面导言摘要
- 处理重定向、章节锚点、跨 Wiki 链接和不存在页面
- 一次查询最多 5 个页面
- 页面不存在时可搜索相近结果
- 可选自动展开普通消息中的 `[[页面名]]`
- 支持直接发送 `@机器人 [[页面名]]`，并阻止同一消息继续触发 LLM 重复回复
- 可按单一条目（pageid）或分类树订阅编辑，定时主动推送到当前群聊或私聊
- 推送编辑者、字节变化、编辑时间、差异链接与逐行增删预览
- 单条目移动或重命名后会按 pageid 继续追踪，并自动更新显示标题
- RecentChanges 使用重叠时间窗口和持久化 RCID 去重，降低延迟写入造成的漏报风险
- 相同分类的多个订阅共享分类树刷新结果，并与变更轮询分开限频
- 首次订阅跳过历史，持久化时间水位、去重记录、分类成员快照和发送失败队列
- 提供统一监控列表、运行状态和主动推送连通性测试
- 支持使用 MediaWiki Bot Password 登录受限 API，并在登录失效后自动重登一次
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

### 萌娘百科等禁止匿名 API 的站点

若查询返回 `action-notallowed: Unauthorized API call`，需要在该 Wiki 登录后打开
`Special:BotPasswords` 创建一个专用于本插件的 Bot Password，然后在 AstrBot 插件配置中填写：

```text
api_url=https://zh.moegirl.org.cn/api.php
api_username=你的账号名@机器人名
api_bot_password=Special:BotPasswords 生成的密码
```

请填写 Bot Password，不要填写账号主密码。插件通过同一个 HTTP 会话先获取登录令牌，
再用 POST 登录；查询时不会把密码放入 URL 或日志。AstrBot 会将插件配置保存在本机
`data/config`，请限制该目录的访问权限。保存配置后重新加载插件，再用 `/wiki 页面名`
验证。若站点 WAF 对主域名不稳定，也可以尝试把 `api_url` 改为
`https://mzh.moegirl.org.cn/api.php`，但登录仍是更可靠的方案。

## 指令

```text
/wiki 页面名
/wiki 页面名#章节
/wiki 页面1|页面2|页面3
/wiki -d 页面名
/wiki -s 不存在的页面
/wiki搜索 关键词
@机器人 [[页面名]]
/wiki监控条目 页面名
/wiki取消监控条目 页面名
/wiki取消监控条目 全部
/wiki监控 分类名
/wiki取消监控 分类名
/wiki取消监控 全部
/wiki监控列表
/wiki监控状态
/wiki测试推送
/wiki检查更新
```

常用别名：`/维基`、`/wikisearch`、`/wikiwatchpage`、`/wikiwatchlist`、`/wikiwatchstatus`。

页面标题可以直接包含空格；下划线也会被转换为空格。`-d` 强制显示摘要，`-s` 在单个主名字空间页面不存在时继续搜索。

`@机器人 [[页面名]]` 始终有效。配置项“未 @ 机器人时也自动展开”默认关闭；开启后，普通群消息中的 `[[页面名]]` 也会触发查询。

## 条目与分类变更推送

AstrBot 管理员可以在需要接收通知的群聊或私聊中订阅一个条目：

```text
/wiki监控条目 下定决心Hand in Hand
```

插件会先解析规范标题和 pageid。即使页面后来移动或重命名，后续变更仍能依靠 pageid 匹配，并把订阅标题更新为新标题。

也可以订阅一个分类及其指定深度内的子分类成员：

```text
/wiki监控 BanG Dream!
```

插件会把它规范化为 `Category:BanG Dream!`，递归读取分类成员，并记录当前会话的 `unified_msg_origin` 作为主动推送目标。首次订阅只建立当前时间水位，不补发已有历史。相同分类被多个会话订阅时只刷新一次分类树。之后的消息格式类似：

```text
下定决心Hand in Hand
+95 | Καλλιόπη | 1:57
订阅：LoveLive!学园偶像祭 ～课后活动～（SIFAC）
https://zh.moegirl.org.cn/index.php?diff=8651943&oldid=8651800
✏添加｢==翻唱版本==｣
✏添加｢===[[You and Idol光之美少女]]=== （TBA）｣
✏添加｢{{You_and_Idol光之美少女}}｣
```

一个页面同时命中当前会话的单条目和分类订阅时只发送一次，并合并显示匹配范围。发送失败的通知不会丢弃，会写入插件数据目录并在后续轮询重试。

管理命令说明：

- `/wiki监控列表`：统一列出当前会话的单条目和分类订阅。
- `/wiki监控状态`：显示后台任务、认证、订阅数、待发送队列、变更水位和最近错误。
- `/wiki测试推送`：绕过轮询，立即向当前会话发送一条主动消息，用来排查平台是否允许主动推送。
- `/wiki取消监控条目 全部`：只删除当前会话的全部单条目订阅。
- `/wiki取消监控 全部`：删除当前会话的全部单条目和分类订阅。

取消全局最后一个订阅后，插件会清空时间水位、RCID 去重记录和待发送队列。

相关配置：

- `change_push_enabled`：后台轮询总开关，默认开启。
- `change_poll_interval_seconds`：轮询间隔，最小 30 秒，默认 120 秒。
- `change_category_depth`：子分类递归深度，默认 2。
- `change_category_refresh_interval_seconds`：分类树刷新间隔，默认 900 秒；相同分类共享一次刷新。
- `change_max_members`：每项监控最多保存的分类成员数，默认 5000。
- `change_overlap_seconds`：每轮从水位前方回退的秒数，默认 60 秒。
- `change_recent_rcid_limit`：持久化的已处理 RCID 数量，默认 20000。
- `change_diff_lines`：每条通知最多显示的差异项目，默认 5；设为 0 可关闭 compare 请求。
- `change_include_bot_edits`：是否包含机器人编辑。

持久化状态位于 AstrBot 的 `data/plugin_data/astrbot_plugin_mediawiki/change_monitor.json`，插件更新不会覆盖。1.2.x 的状态文件会在读取时自动迁移到 v2，无需重新订阅。分类树很大时请降低递归深度、降低成员上限或提高分类刷新间隔。

RecentChanges 可能出现时间戳略早、但稍后才可见的记录，因此 1.3 系列不再只读取严格晚于最后时间戳的更改，而是回退一小段时间并按 RCID 去重。重启后去重记录仍然有效。

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
