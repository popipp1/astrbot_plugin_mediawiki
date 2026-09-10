from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import quote

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

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
    "MediaWiki 页面摘要与链接查询",
    "1.1.0",
)
class MediaWikiPlugin(Star):
    """Query page summaries and links through the MediaWiki Action API."""

    COMMAND_NAMES = ("wiki", "维基")
    SEARCH_COMMAND_NAMES = ("wiki搜索", "wikisearch")

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.show_summary = bool(config.get("show_summary", True))
        self.search_if_missing = bool(config.get("search_if_missing", False))
        self.auto_expand_brackets = bool(config.get("auto_expand_brackets", False))
        self.qq_link_prefix = str(config.get("qq_link_prefix", "")).strip()

        self.client = MediaWikiClient(
            str(config.get("api_url", "https://zh.wikipedia.org/w/api.php")),
            user_agent=str(config.get("user_agent", "AstrBot-MediaWiki/1.0")),
            timeout_seconds=float(config.get("timeout_seconds", 12)),
            summary_chars=int(config.get("summary_chars", 200)),
        )

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

    async def terminate(self):
        await self.client.close()

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
