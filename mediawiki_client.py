from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

try:
    import aiohttp
except ModuleNotFoundError:  # Allows pure helper tests before plugin deps are installed.
    aiohttp = None  # type: ignore[assignment]


DEFAULT_USER_AGENT = "AstrBot-MediaWiki/1.2"
MAX_TITLES = 5


class MediaWikiError(RuntimeError):
    """Base error raised by the MediaWiki client."""


class MediaWikiHTTPError(MediaWikiError):
    """The MediaWiki endpoint returned a non-success HTTP status."""


class MediaWikiAPIError(MediaWikiError):
    """The MediaWiki Action API returned an error object."""

    def __init__(self, code: str, info: str, payload: dict[str, Any] | None = None):
        super().__init__(f"{code}: {info}" if code else info)
        self.code = code
        self.info = info
        self.payload = payload or {}


@dataclass(slots=True, frozen=True)
class TitleRequest:
    title: str
    anchor: str = ""


@dataclass(slots=True)
class WikiPage:
    title: str
    pageid: int | None = None
    namespace: int = 0
    extract: str = ""
    canonical_url: str = ""
    full_url: str = ""
    edit_url: str = ""
    anchor: str = ""
    missing: bool = False
    invalid: bool = False
    invalid_reason: str = ""
    special: bool = False
    redirect_from: str = ""
    redirect_fragment: str = ""


@dataclass(slots=True)
class InterwikiPage:
    title: str
    url: str
    anchor: str = ""


@dataclass(slots=True)
class WikiQueryResult:
    pages: list[WikiPage] = field(default_factory=list)
    interwiki: list[InterwikiPage] = field(default_factory=list)


@dataclass(slots=True)
class WikiSearchResult:
    total_hits: int
    pages: list[WikiPage] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class CategoryMember:
    pageid: int
    namespace: int
    title: str
    member_type: str


@dataclass(slots=True)
class CategorySnapshot:
    page_ids: set[int] = field(default_factory=set)
    titles: set[str] = field(default_factory=set)
    categories: list[str] = field(default_factory=list)
    truncated: bool = False


@dataclass(slots=True, frozen=True)
class RecentChange:
    rcid: int
    change_type: str
    namespace: int
    title: str
    pageid: int
    revid: int
    old_revid: int
    user: str
    timestamp: str
    comment: str
    old_length: int
    new_length: int
    bot: bool = False
    minor: bool = False

    @property
    def size_delta(self) -> int:
        return self.new_length - self.old_length


def normalize_title(raw: str) -> str:
    """Normalize whitespace like MediaWiki does for display titles."""
    title = re.sub(r"[\s_]+", " ", unescape(raw)).strip()
    if not title:
        return ""
    return title[0].upper() + title[1:]


def split_title(raw: str) -> TitleRequest:
    title, separator, fragment = normalize_title(raw).partition("#")
    anchor = f"#{quote(fragment, safe='/:@-._~!$&\'()*+,;=')}" if separator else ""
    return TitleRequest(title=title, anchor=anchor)


def parse_title_requests(raw: str, limit: int = MAX_TITLES) -> list[TitleRequest]:
    """Parse a pipe-separated title list while preserving section anchors."""
    seen: set[str] = set()
    result: list[TitleRequest] = []
    for part in raw.split("|"):
        request = split_title(part)
        key = request.title.casefold()
        if not request.title or key in seen:
            continue
        seen.add(key)
        result.append(request)
        if len(result) >= limit:
            break
    return result


def extract_wikilink_titles(text: str, limit: int = MAX_TITLES) -> list[TitleRequest]:
    """Extract MediaWiki-style [[target|label]] links from ordinary messages."""
    decoded = unescape(text)
    targets = [match.group(1).split("|", 1)[0] for match in re.finditer(r"\[\[(.+?)\]\]", decoded)]
    return parse_title_requests("|".join(targets), limit=limit)


def validate_api_url(api_url: str) -> str:
    value = api_url.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("MediaWiki API 地址必须是有效的 http/https URL")
    if parsed.username or parsed.password:
        raise ValueError("MediaWiki API 地址不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("MediaWiki API 地址不能包含查询参数或锚点")
    if not parsed.path.endswith("/api.php"):
        raise ValueError("MediaWiki API 地址必须以 /api.php 结尾")
    return urlunsplit(parsed)


def get_index_url(api_url: str, params: dict[str, Any] | None = None) -> str:
    parsed = urlsplit(validate_api_url(api_url))
    path = parsed.path[: -len("api.php")] + "index.php"
    query = urlencode(params or {}, doseq=True)
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [item for item in value.values() if isinstance(item, dict)]
    return []


class MediaWikiClient:
    def __init__(
        self,
        api_url: str,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 12,
        summary_chars: int = 200,
    ) -> None:
        self.api_url = validate_api_url(api_url)
        self.index_url = get_index_url(self.api_url)
        self.user_agent = user_agent.strip() or DEFAULT_USER_AGENT
        self.timeout_seconds = max(3.0, min(float(timeout_seconds), 60.0))
        self.summary_chars = max(50, min(int(summary_chars), 1200))
        self._session: aiohttp.ClientSession | None = None
        self._extracts_supported = True

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if aiohttp is None:
            raise MediaWikiHTTPError(
                "缺少 aiohttp；请让 AstrBot 根据 requirements.txt 安装插件依赖"
            )
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": self.user_agent},
                trust_env=True,
            )
        return self._session

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        session = await self._get_session()
        try:
            async with session.get(self.api_url, params=params) as response:
                body = await response.text()
                if response.status < 200 or response.status >= 300:
                    excerpt = re.sub(r"\s+", " ", body).strip()[:300]
                    raise MediaWikiHTTPError(
                        f"HTTP {response.status}: {excerpt or response.reason}"
                    )
        except aiohttp.ClientError as exc:
            raise MediaWikiHTTPError(str(exc)) from exc

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise MediaWikiHTTPError("Wiki API 返回的不是 JSON") from exc

        if not isinstance(data, dict):
            raise MediaWikiHTTPError("Wiki API 返回了无法识别的数据")
        if error := data.get("error"):
            if not isinstance(error, dict):
                raise MediaWikiAPIError("unknown", str(error))
            raise MediaWikiAPIError(
                str(error.get("code", "unknown")),
                str(error.get("info", "Wiki API 请求失败")),
                error,
            )
        if errors := data.get("errors"):
            raise MediaWikiAPIError("errors", json.dumps(errors, ensure_ascii=False))
        return data

    @staticmethod
    def _without_extracts(params: dict[str, Any]) -> dict[str, Any]:
        fallback = dict(params)
        props = [
            item
            for item in str(fallback.get("prop", "")).split("|")
            if item and item != "extracts"
        ]
        fallback["prop"] = "|".join(props)
        for key in list(fallback):
            if key.startswith("ex"):
                fallback.pop(key)
        return fallback

    async def _request_with_extract_fallback(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        if not self._extracts_supported:
            return await self._request(self._without_extracts(params))
        try:
            return await self._request(params)
        except MediaWikiAPIError as exc:
            error_text = f"{exc.code} {exc.info} {exc.payload}".lower()
            if "extract" not in error_text:
                raise
            self._extracts_supported = False
            return await self._request(self._without_extracts(params))

    def _base_query_params(self) -> dict[str, Any]:
        return {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "errorformat": "plaintext",
            "utf8": "1",
            "maxlag": "5",
        }

    async def query_pages(self, requests: list[TitleRequest]) -> WikiQueryResult:
        requests = requests[:MAX_TITLES]
        if not requests:
            return WikiQueryResult()

        params = self._base_query_params()
        params.update(
            {
                "prop": "extracts|info",
                "meta": "siteinfo",
                "siprop": "specialpagealiases|namespaces",
                "titles": "|".join(item.title for item in requests),
                "redirects": "1",
                "converttitles": "1",
                "iwurl": "1",
                "exchars": str(self.summary_chars),
                "exlimit": "max",
                "exintro": "1",
                "explaintext": "1",
                "exsectionformat": "plain",
                "inprop": "url|displaytitle",
            }
        )
        data = await self._request_with_extract_fallback(params)
        query = data.get("query")
        if not isinstance(query, dict):
            raise MediaWikiHTTPError("Wiki API 响应缺少 query 字段")

        anchors = {item.title.casefold(): item.anchor for item in requests}
        for mapping_name in ("normalized", "converted"):
            for mapping in _as_list(query.get(mapping_name)):
                source = str(mapping.get("from", "")).casefold()
                target = str(mapping.get("to", "")).casefold()
                if source in anchors and target:
                    anchors[target] = anchors[source]

        redirects = _as_list(query.get("redirects"))
        for redirect in redirects:
            source = str(redirect.get("from", "")).casefold()
            target = str(redirect.get("to", "")).casefold()
            if source in anchors and target:
                anchors[target] = anchors[source]

        danger_aliases, special_namespace = self._dangerous_special_pages(query)
        result = WikiQueryResult()
        for raw_page in _as_list(query.get("pages")):
            title = str(raw_page.get("title", ""))
            redirect = next(
                (
                    item
                    for item in redirects
                    if str(item.get("to", "")).casefold() == title.casefold()
                ),
                None,
            )
            if redirect and self._is_dangerous_redirect(
                str(redirect.get("from", "")), danger_aliases, special_namespace
            ):
                # Do not reveal the identity generated for Special:MyPage/MyTalk.
                original = str(redirect.get("from", title))
                result.pages.append(
                    WikiPage(
                        title=original,
                        namespace=-1,
                        special=True,
                        anchor=anchors.get(original.casefold(), ""),
                    )
                )
                continue

            result.pages.append(
                WikiPage(
                    title=title,
                    pageid=_optional_int(raw_page.get("pageid")),
                    namespace=int(raw_page.get("ns", 0)),
                    extract=str(raw_page.get("extract", "")).strip(),
                    canonical_url=str(raw_page.get("canonicalurl", "")),
                    full_url=str(raw_page.get("fullurl", "")),
                    edit_url=str(raw_page.get("editurl", "")),
                    anchor=anchors.get(title.casefold(), ""),
                    missing="missing" in raw_page,
                    invalid="invalid" in raw_page,
                    invalid_reason=str(raw_page.get("invalidreason", "")),
                    special=bool(raw_page.get("special", False)),
                    redirect_from=str(redirect.get("from", "")) if redirect else "",
                    redirect_fragment=str(redirect.get("tofragment", "")) if redirect else "",
                )
            )

        for item in _as_list(query.get("interwiki")):
            title = str(item.get("title", ""))
            result.interwiki.append(
                InterwikiPage(
                    title=title,
                    url=str(item.get("url", "")),
                    anchor=anchors.get(title.casefold(), ""),
                )
            )
        return result

    async def search(self, keywords: str, limit: int = 5) -> WikiSearchResult:
        keywords = keywords.strip()
        if not keywords:
            return WikiSearchResult(total_hits=0)
        limit = max(1, min(int(limit), MAX_TITLES))

        params = self._base_query_params()
        params.update(
            {
                "prop": "extracts|info",
                "list": "search",
                "generator": "search",
                "srsearch": keywords,
                "srnamespace": "0",
                "srlimit": str(limit),
                "srinfo": "totalhits",
                "srprop": "",
                "gsrsearch": keywords,
                "gsrnamespace": "0",
                "gsrlimit": str(limit),
                "exchars": str(self.summary_chars),
                "exlimit": "max",
                "exintro": "1",
                "explaintext": "1",
                "exsectionformat": "plain",
                "inprop": "url|displaytitle",
            }
        )
        data = await self._request_with_extract_fallback(params)
        query = data.get("query")
        if not isinstance(query, dict):
            return WikiSearchResult(total_hits=0)

        search_info = query.get("searchinfo", {})
        total_hits = (
            int(search_info.get("totalhits", 0))
            if isinstance(search_info, dict)
            else 0
        )
        raw_pages = sorted(
            _as_list(query.get("pages")),
            key=lambda page: int(page.get("index", 10_000)),
        )
        pages = [
            WikiPage(
                title=str(page.get("title", "")),
                pageid=_optional_int(page.get("pageid")),
                namespace=int(page.get("ns", 0)),
                extract=str(page.get("extract", "")).strip(),
                canonical_url=str(page.get("canonicalurl", "")),
                full_url=str(page.get("fullurl", "")),
                edit_url=str(page.get("editurl", "")),
            )
            for page in raw_pages
        ]
        return WikiSearchResult(total_hits=total_hits, pages=pages)

    async def server_timestamp(self) -> str:
        """Return the wiki server clock in MediaWiki timestamp format."""
        params = self._base_query_params()
        params["curtimestamp"] = "1"
        data = await self._request(params)
        timestamp = str(data.get("curtimestamp", "")).strip()
        if not timestamp:
            raise MediaWikiHTTPError("Wiki API 响应缺少 curtimestamp 字段")
        return timestamp

    async def category_tree(
        self,
        root_category: str,
        *,
        max_depth: int = 2,
        max_members: int = 5000,
    ) -> CategorySnapshot:
        """Recursively enumerate a category tree using categorymembers."""
        max_depth = max(0, min(int(max_depth), 10))
        max_members = max(1, min(int(max_members), 100_000))
        queue: list[tuple[str, int]] = [(root_category, 0)]
        visited: set[str] = set()
        snapshot = CategorySnapshot()

        while queue and len(snapshot.page_ids) < max_members:
            category, depth = queue.pop(0)
            key = category.casefold()
            if key in visited:
                continue
            visited.add(key)
            snapshot.categories.append(category)
            continuation = ""

            while len(snapshot.page_ids) < max_members:
                params = self._base_query_params()
                params.update(
                    {
                        "list": "categorymembers",
                        "cmtitle": category,
                        "cmprop": "ids|title|type",
                        "cmtype": "page|subcat|file",
                        "cmlimit": "max",
                    }
                )
                if continuation:
                    params["cmcontinue"] = continuation
                data = await self._request(params)
                query = data.get("query", {})
                raw_members = (
                    query.get("categorymembers", [])
                    if isinstance(query, dict)
                    else []
                )
                for raw in _as_list(raw_members):
                    member = CategoryMember(
                        pageid=int(raw.get("pageid", 0) or 0),
                        namespace=int(raw.get("ns", 0) or 0),
                        title=str(raw.get("title", "")).strip(),
                        member_type=str(raw.get("type", "page")),
                    )
                    if member.pageid > 0:
                        snapshot.page_ids.add(member.pageid)
                    if member.title:
                        snapshot.titles.add(member.title.casefold())
                    if (
                        member.member_type == "subcat"
                        and member.title
                        and depth < max_depth
                    ):
                        queue.append((member.title, depth + 1))
                    if len(snapshot.page_ids) >= max_members:
                        snapshot.truncated = True
                        break

                raw_continue = data.get("continue", {})
                continuation = (
                    str(raw_continue.get("cmcontinue", ""))
                    if isinstance(raw_continue, dict)
                    else ""
                )
                if not continuation or snapshot.truncated:
                    break

        if queue:
            snapshot.truncated = True
        return snapshot

    async def recent_changes(
        self,
        since: str,
        *,
        limit: int = 1000,
        include_bot_edits: bool = True,
    ) -> list[RecentChange]:
        """Fetch edit/new entries from oldest to newest since a timestamp."""
        limit = max(1, min(int(limit), 10_000))
        changes: list[RecentChange] = []
        continuation = ""
        while len(changes) < limit:
            params = self._base_query_params()
            params.update(
                {
                    "list": "recentchanges",
                    "rcstart": since,
                    "rcdir": "newer",
                    "rctype": "edit|new",
                    "rcprop": "title|ids|sizes|flags|user|timestamp|comment|tags",
                    "rclimit": "max",
                }
            )
            if not include_bot_edits:
                params["rcshow"] = "!bot"
            if continuation:
                params["rccontinue"] = continuation
            data = await self._request(params)
            query = data.get("query", {})
            raw_changes = (
                query.get("recentchanges", []) if isinstance(query, dict) else []
            )
            for raw in _as_list(raw_changes):
                revid = int(raw.get("revid", 0) or 0)
                if revid <= 0:
                    continue
                changes.append(
                    RecentChange(
                        rcid=int(raw.get("rcid", 0) or 0),
                        change_type=str(raw.get("type", "edit")),
                        namespace=int(raw.get("ns", 0) or 0),
                        title=str(raw.get("title", "")).strip(),
                        pageid=int(raw.get("pageid", 0) or 0),
                        revid=revid,
                        old_revid=int(raw.get("old_revid", 0) or 0),
                        user=str(raw.get("user", "（用户名已隐藏）")),
                        timestamp=str(raw.get("timestamp", "")),
                        comment=str(raw.get("comment", "")).strip(),
                        old_length=int(raw.get("oldlen", 0) or 0),
                        new_length=int(raw.get("newlen", 0) or 0),
                        bot="bot" in raw,
                        minor="minor" in raw,
                    )
                )
                if len(changes) >= limit:
                    break

            raw_continue = data.get("continue", {})
            continuation = (
                str(raw_continue.get("rccontinue", ""))
                if isinstance(raw_continue, dict)
                else ""
            )
            if not continuation or len(changes) >= limit:
                break
        return changes

    async def compare_revisions(self, old_revid: int, revid: int) -> str:
        """Return the HTML table rows generated by action=compare."""
        params = {
            "action": "compare",
            "format": "json",
            "formatversion": "2",
            "errorformat": "plaintext",
            "utf8": "1",
            "maxlag": "5",
            "torev": str(int(revid)),
            "prop": "diff",
            "difftype": "table",
        }
        if old_revid > 0:
            params["fromrev"] = str(int(old_revid))
        else:
            params["fromslots"] = "main"
            params["fromtext-main"] = ""
            params["fromcontentmodel-main"] = "wikitext"
        data = await self._request(params)
        compare = data.get("compare", {})
        if not isinstance(compare, dict):
            return ""
        for key in ("body", "*", "diff"):
            value = compare.get(key)
            if isinstance(value, str):
                return value
        return ""

    @staticmethod
    def _dangerous_special_pages(query: dict[str, Any]) -> tuple[set[str], str]:
        aliases: set[str] = {"mypage", "mytalk"}
        for item in _as_list(query.get("specialpagealiases")):
            if str(item.get("realname", "")).casefold() not in {"mypage", "mytalk"}:
                continue
            aliases.update(str(alias).casefold() for alias in item.get("aliases", []))

        special_namespace = "special"
        namespaces = query.get("namespaces", {})
        if isinstance(namespaces, dict):
            special = namespaces.get("-1", {})
            if isinstance(special, dict):
                special_namespace = str(
                    special.get("name") or special.get("*") or special_namespace
                ).casefold()
        return aliases, special_namespace

    @staticmethod
    def _is_dangerous_redirect(
        source: str, aliases: set[str], special_namespace: str
    ) -> bool:
        namespace, separator, page_name = source.partition(":")
        if not separator or namespace.casefold() != special_namespace:
            return False
        return page_name.split("/", 1)[0].casefold() in aliases

    def page_url(self, page: WikiPage) -> str:
        if page.special:
            return get_index_url(self.api_url, {"title": page.title}) + page.anchor
        if page.missing:
            return page.edit_url or get_index_url(
                self.api_url, {"title": page.title, "action": "edit"}
            )
        short_url = (
            get_index_url(self.api_url, {"curid": page.pageid})
            if page.pageid is not None
            else ""
        )
        candidates = [url for url in (short_url, page.canonical_url, page.full_url) if url]
        base = min(candidates, key=len) if candidates else get_index_url(
            self.api_url, {"title": page.title}
        )
        return base + page.anchor


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
