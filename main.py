from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except ImportError:  # Compatibility with AstrBot releases before this helper.
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    def get_astrbot_plugin_data_path() -> str:
        return str(Path(get_astrbot_data_path()) / "plugin_data")

from .change_monitor import WikiChangeMonitor, display_category

from .mediawiki_client import (
    InterwikiPage,
    MediaWikiAPIError,
    MediaWikiClient,
    MediaWikiError,
    WikiPage,
    WikiQueryResult,
    WikiSearchResult,
    extract_wikilink_titles,
    parse_title_requests,
)


@register(
    "astrbot_plugin_mediawiki",
    "Lukec",
    "MediaWiki 页面查询与分类变更推送",
    "1.2.0",
)
class MediaWikiPlugin(Star):
    """Query page summaries and links through the MediaWiki Action API."""

    COMMAND_NAMES = ("wiki", "维基")
    SEARCH_COMMAND_NAMES = ("wiki搜索", "wikisearch")
    WATCH_COMMAND_NAMES = ("wiki监控", "wikiwatch")
    UNWATCH_COMMAND_NAMES = ("wiki取消监控", "wikiunwatch")

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.show_summary = bool(config.get("show_summary", True))
        self.search_if_missing = bool(config.get("search_if_missing", False))
        self.auto_expand_brackets = bool(config.get("auto_expand_brackets", False))
        self.qq_link_prefix = str(config.get("qq_link_prefix", "")).strip()
        self.change_push_enabled = bool(config.get("change_push_enabled", True))

        self.client = MediaWikiClient(
            str(config.get("api_url", "https://zh.wikipedia.org/w/api.php")),
            user_agent=str(config.get("user_agent", "AstrBot-MediaWiki/1.2")),
            timeout_seconds=float(config.get("timeout_seconds", 12)),
            summary_chars=int(config.get("summary_chars", 200)),
        )
        state_path = (
            Path(get_astrbot_plugin_data_path())
            / "astrbot_plugin_mediawiki"
            / "change_monitor.json"
        )
        self.change_monitor = WikiChangeMonitor(
            self.client,
            state_path,
            self._send_change_message,
            poll_interval_seconds=int(config.get("change_poll_interval_seconds", 120)),
            category_depth=int(config.get("change_category_depth", 2)),
            max_members=int(config.get("change_max_members", 5000)),
            max_changes_per_poll=int(
                config.get("change_max_changes_per_poll", 1000)
            ),
            max_diff_lines=int(config.get("change_diff_lines", 5)),
            max_diff_line_chars=int(config.get("change_diff_line_chars", 180)),
            max_pending=int(config.get("change_pending_limit", 500)),
            include_bot_edits=bool(config.get("change_include_bot_edits", True)),
            link_prefix_for_umo=self._change_link_prefix,
        )

    async def initialize(self):
        if self.change_push_enabled:
            await self.change_monitor.start()
            logger.info("MediaWiki 分类变更监控已启动")

    @filter.command("wiki", alias={"维基"})
    async def wiki(self, event: AstrMessageEvent):
        """查询 Wiki 页面；支持 /wiki -d -s 页面1|页面2。"""
        raw = self._command_tail(event, self.COMMAND_NAMES)
        details, search_missing, title_text = self._parse_query_options(raw)
        if not title_text:
            yield event.plain_result(self.client.index_url)
            return

        requests = parse_title_requests(title_text)
        if not requests:
            yield event.plain_result("请输入有效页面名，例如：/wiki 初音未来")
            return

        try:
            result = await self.client.query_pages(requests)
            messages = self._format_query_result(result, details=details)
            if not messages:
                yield event.plain_result("Wiki API 没有返回页面信息。")
                return

            missing_page = self._single_missing_main_page(result)
            if (search_missing or self.search_if_missing) and missing_page:
                search_result = await self.client.search(missing_page.title)
                messages.extend(
                    self._format_search_result(missing_page.title, search_result)
                )

            yield event.plain_result(
                self._prepare_for_platform(event, "\n\n".join(messages))
            )
        except MediaWikiError as exc:
            logger.warning("MediaWiki query failed: %s", exc)
            yield event.plain_result(f"⚠️ Wiki 查询失败：{self._friendly_error(exc)}")
        except Exception as exc:
            logger.exception("Unexpected MediaWiki query error")
            yield event.plain_result(f"⚠️ Wiki 查询失败：{exc}")

    @filter.command("wiki搜索", alias={"wikisearch"})
    async def wiki_search(self, event: AstrMessageEvent):
        """搜索当前配置的 Wiki，例如 /wiki搜索 初音未来。"""
        keywords = self._command_tail(event, self.SEARCH_COMMAND_NAMES).strip()
        if not keywords:
            yield event.plain_result("请输入搜索关键词，例如：/wiki搜索 初音未来")
            return
        try:
            result = await self.client.search(keywords)
            messages = self._format_search_result(keywords, result)
            yield event.plain_result(
                self._prepare_for_platform(event, "\n\n".join(messages))
            )
        except MediaWikiError as exc:
            logger.warning("MediaWiki search failed: %s", exc)
            yield event.plain_result(f"⚠️ Wiki 搜索失败：{self._friendly_error(exc)}")
        except Exception as exc:
            logger.exception("Unexpected MediaWiki search error")
            yield event.plain_result(f"⚠️ Wiki 搜索失败：{exc}")

    @filter.regex(r"\[\[.+?\]\]")
    async def expand_wikilinks(self, event: AstrMessageEvent):
        """Expand @bot [[page]] and optionally expand links without a mention."""
        is_directed_at_bot = bool(
            getattr(event, "is_at_or_wake_command", False)
        )
        if not is_directed_at_bot and not self.auto_expand_brackets:
            return
        if self._looks_like_plugin_command(event):
            return
        requests = extract_wikilink_titles(event.get_message_str())
        if not requests:
            return
        try:
            result = await self.client.query_pages(requests)
            messages = self._format_query_result(result, details=False)
            if messages:
                yield event.plain_result(
                    self._prepare_for_platform(event, "\n\n".join(messages))
                )
                if is_directed_at_bot:
                    # The Wiki result is the complete response to an @ mention;
                    # prevent an additional LLM reply to the same message.
                    event.stop_event()
        except MediaWikiError as exc:
            logger.warning("Automatic MediaWiki expansion failed: %s", exc)
            if is_directed_at_bot:
                yield event.plain_result(
                    f"⚠️ Wiki 查询失败：{self._friendly_error(exc)}"
                )
                event.stop_event()
        except Exception as exc:
            logger.exception("Unexpected automatic MediaWiki expansion error")
            if is_directed_at_bot:
                yield event.plain_result(f"⚠️ Wiki 查询失败：{exc}")
                event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("wiki监控", alias={"wikiwatch"})
    async def watch_category(self, event: AstrMessageEvent):
        """订阅当前会话的 Wiki 分类变更：/wiki监控 分类名。"""
        raw = self._command_tail(event, self.WATCH_COMMAND_NAMES)
        if not raw:
            yield event.plain_result("用法：/wiki监控 分类名")
            return
        try:
            subscription = await self.change_monitor.subscribe(
                event.unified_msg_origin, raw
            )
            suffix = "（成员数量达到上限，当前为截断监控）" if subscription.truncated else ""
            status = "后台轮询已启用" if self.change_push_enabled else "已保存，但插件配置中的变更推送目前关闭"
            yield event.plain_result(
                f"✅ 已监控分类：{display_category(subscription.category)}\n"
                f"当前收录 {len(subscription.member_page_ids)} 个页面，{status}{suffix}。\n"
                "首次订阅只建立水位，不补发历史变更。"
            )
        except MediaWikiError as exc:
            logger.warning("MediaWiki category subscription failed: %s", exc)
            yield event.plain_result(
                f"⚠️ 分类监控创建失败：{self._friendly_error(exc)}"
            )
        except Exception as exc:
            logger.exception("Unexpected MediaWiki category subscription error")
            yield event.plain_result(f"⚠️ 分类监控创建失败：{exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("wiki取消监控", alias={"wikiunwatch"})
    async def unwatch_category(self, event: AstrMessageEvent):
        """取消当前会话的 Wiki 分类监控：/wiki取消监控 分类名|全部。"""
        raw = self._command_tail(event, self.UNWATCH_COMMAND_NAMES).strip()
        if not raw:
            yield event.plain_result("用法：/wiki取消监控 分类名；取消全部请填写“全部”。")
            return
        remove_all = raw.casefold() in {"all", "全部"}
        removed = await self.change_monitor.unsubscribe(
            event.unified_msg_origin, "" if remove_all else raw
        )
        if removed:
            yield event.plain_result(f"✅ 已取消 {removed} 项分类监控。")
        else:
            yield event.plain_result("当前会话没有匹配的分类监控。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("wiki监控列表", alias={"wikiwatchlist"})
    async def list_watched_categories(self, event: AstrMessageEvent):
        """列出当前会话的 Wiki 分类监控。"""
        subscriptions = await self.change_monitor.subscriptions_for(
            event.unified_msg_origin
        )
        if not subscriptions:
            yield event.plain_result("当前会话尚未监控任何 Wiki 分类。")
            return
        lines = ["当前会话的 Wiki 分类监控："]
        for index, subscription in enumerate(subscriptions, start=1):
            suffix = "，已截断" if subscription.truncated else ""
            lines.append(
                f"{index}. {display_category(subscription.category)}"
                f"（{len(subscription.member_page_ids)} 个页面{suffix}）"
            )
        lines.append(
            f"轮询状态：{'已启用' if self.change_push_enabled else '已关闭'}；"
            f"间隔 {self.change_monitor.poll_interval_seconds} 秒。"
        )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("wiki检查更新", alias={"wikicheck"})
    async def check_wiki_changes(self, event: AstrMessageEvent):
        """立即执行一次分类变更检查。"""
        try:
            result = await self.change_monitor.poll_once()
            yield event.plain_result(
                "Wiki 变更检查完成："
                f"抓取 {result.fetched}，匹配 {result.matched}，"
                f"入队 {result.queued}，发送 {result.sent}，失败 {result.failed}。"
            )
        except MediaWikiError as exc:
            logger.warning("Manual MediaWiki change poll failed: %s", exc)
            yield event.plain_result(
                f"⚠️ Wiki 变更检查失败：{self._friendly_error(exc)}"
            )
        except Exception as exc:
            logger.exception("Unexpected manual MediaWiki change poll error")
            yield event.plain_result(f"⚠️ Wiki 变更检查失败：{exc}")

    async def terminate(self):
        await self.change_monitor.stop()
        await self.client.close()

    async def _send_change_message(self, umo: str, text: str) -> None:
        await self.context.send_message(umo, MessageChain().message(text))

    def _change_link_prefix(self, umo: str) -> str:
        platform = umo.casefold()
        if "aiocqhttp" in platform or "onebot" in platform:
            return self.qq_link_prefix
        return ""

    @staticmethod
    def _parse_query_options(raw: str) -> tuple[bool, bool, str]:
        details = False
        search = False
        remaining = raw.strip()
        while remaining.startswith("-"):
            match = re.match(r"^(--details|--search|-d|-s)(?:\s+|$)", remaining)
            if not match:
                break
            flag = match.group(1)
            details = details or flag in {"-d", "--details"}
            search = search or flag in {"-s", "--search"}
            remaining = remaining[match.end() :].lstrip()
        return details, search, remaining

    def _format_query_result(
        self, result: WikiQueryResult, *, details: bool
    ) -> list[str]:
        messages = [self._format_page(page, details=details) for page in result.pages]
        messages.extend(self._format_interwiki(item) for item in result.interwiki)
        return [message for message in messages if message]

    def _format_page(self, page: WikiPage, *, details: bool) -> str:
        lines = [f"您要的“{page.title}”："]
        if page.redirect_from:
            target = page.title
            if page.redirect_fragment:
                target += f"#{page.redirect_fragment}"
                page.anchor = f"#{quote(page.redirect_fragment, safe='/:@-._~')}"
            lines.append(f"重定向：[{page.redirect_from}] → [{target}]")

        if page.invalid:
            reason = page.invalid_reason or "原因未知"
            lines.append(f"😟 页面名称不合法：{reason}")
            return "\n".join(lines)

        lines.append(self.client.page_url(page))
        if page.special:
            lines.append("特殊页面")
        elif page.missing:
            lines.append("💔 页面不存在")
        elif (details or self.show_summary) and page.extract:
            lines.extend(("", page.extract))
        return "\n".join(lines)

    @staticmethod
    def _format_interwiki(item: InterwikiPage) -> str:
        return f"跨 Wiki 页面“{item.title}”：\n{item.url}{item.anchor}"

    def _format_search_result(
        self, keywords: str, result: WikiSearchResult
    ) -> list[str]:
        if not result.pages:
            return [f"💔 找不到与“{keywords}”匹配的结果。"]
        messages = [
            f"🔍 关键词“{keywords}”共匹配到 {result.total_hits} 个结果，以下是前 {len(result.pages)} 个："
        ]
        for index, page in enumerate(result.pages, start=1):
            lines = [f"({index}) {page.title}"]
            if page.extract:
                lines.append(page.extract)
            lines.append(self.client.page_url(page))
            messages.append("\n".join(lines))
        return messages

    @staticmethod
    def _single_missing_main_page(result: WikiQueryResult) -> WikiPage | None:
        if len(result.pages) != 1 or result.interwiki:
            return None
        page = result.pages[0]
        if page.namespace == 0 and page.missing and not page.invalid:
            return page
        return None

    @classmethod
    def _command_tail(
        cls, event: AstrMessageEvent, command_names: Iterable[str]
    ) -> str:
        # AstrBot removes the wake prefix before command filters run. Keeping a
        # no-parameter handler lets us retain titles containing ordinary spaces.
        message = re.sub(r"\s+", " ", event.get_message_str().strip())
        lowered = message.casefold()
        for command in sorted(command_names, key=len, reverse=True):
            key = command.casefold()
            if lowered == key:
                return ""
            if lowered.startswith(key + " "):
                return message[len(command) :].strip()
        return message

    @classmethod
    def _looks_like_plugin_command(cls, event: AstrMessageEvent) -> bool:
        message = re.sub(r"\s+", " ", event.get_message_str().strip()).casefold()
        names = (*cls.COMMAND_NAMES, *cls.SEARCH_COMMAND_NAMES)
        return any(
            message == name.casefold()
            or message.startswith(name.casefold() + " ")
            for name in names
        )

    def _prepare_for_platform(self, event: AstrMessageEvent, text: str) -> str:
        if not self.qq_link_prefix:
            return text
        platform_name = ""
        try:
            platform_name = str(event.get_platform_name()).casefold()
        except (AttributeError, TypeError):
            platform_name = str(getattr(event, "platform_meta", "")).casefold()
        if "aiocqhttp" not in platform_name and "onebot" not in platform_name:
            return text
        return re.sub(
            r"(?<!\S)(https?://\S+)", rf"{self.qq_link_prefix}\1", text
        )

    @staticmethod
    def _friendly_error(exc: MediaWikiError) -> str:
        if isinstance(exc, MediaWikiAPIError) and exc.code == "maxlag":
            return "Wiki 服务器繁忙，请稍后重试。"
        return str(exc)
