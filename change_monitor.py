from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

try:
    from .mediawiki_client import (
        CategorySnapshot,
        MediaWikiClient,
        RecentChange,
        get_index_url,
    )
except ImportError:  # Allows the helpers to be tested outside AstrBot.
    from mediawiki_client import (  # type: ignore[no-redef]
        CategorySnapshot,
        MediaWikiClient,
        RecentChange,
        get_index_url,
    )


LOGGER = logging.getLogger(__name__)
STATE_VERSION = 1


def normalize_category(raw: str) -> str:
    value = re.sub(r"[\s_]+", " ", raw).strip()
    value = re.sub(r"^(?:category|分类)\s*[:：]\s*", "", value, flags=re.I)
    if not value:
        raise ValueError("分类名不能为空")
    return f"Category:{value}"


def display_category(category: str) -> str:
    return re.sub(r"^(?:category|分类)\s*[:：]\s*", "", category, flags=re.I)


@dataclass(slots=True, frozen=True)
class DiffLine:
    action: str
    text: str


class _MediaWikiDiffParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[DiffLine] = []
        self._action = ""
        self._buffer: list[str] = []
        self._cell_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._action:
            if tag == "td":
                self._cell_depth += 1
            return
        if tag != "td":
            return
        classes = dict(attrs).get("class", "") or ""
        class_names = set(classes.split())
        if "diff-addedline" in class_names:
            self._action = "add"
        elif "diff-deletedline" in class_names:
            self._action = "delete"
        if self._action:
            self._buffer = []
            self._cell_depth = 1

    def handle_data(self, data: str) -> None:
        if self._action:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._action or tag != "td":
            return
        self._cell_depth -= 1
        if self._cell_depth > 0:
            return
        text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
        if text:
            self.lines.append(DiffLine(self._action, text))
        self._action = ""
        self._buffer = []


def parse_diff_html(
    html: str,
    *,
    max_lines: int = 5,
    max_chars: int = 180,
) -> list[DiffLine]:
    parser = _MediaWikiDiffParser()
    parser.feed(html or "")
    parser.close()
    max_lines = max(0, min(int(max_lines), 50))
    max_chars = max(20, min(int(max_chars), 1000))
    result: list[DiffLine] = []
    for line in parser.lines[:max_lines]:
        text = line.text
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        result.append(DiffLine(line.action, text))
    return result


def build_diff_url(api_url: str, change: RecentChange) -> str:
    params: dict[str, Any] = {"diff": change.revid}
    if change.old_revid > 0:
        params["oldid"] = change.old_revid
    return get_index_url(api_url, params)


def _local_clock(timestamp: str) -> str:
    try:
        value = timestamp.replace("Z", "+00:00")
        moment = datetime.fromisoformat(value).astimezone()
        return f"{moment.hour}:{moment.minute:02d}"
    except ValueError:
        return timestamp or "时间未知"


def format_change_notification(
    change: RecentChange,
    categories: list[str],
    diff_lines: list[DiffLine],
    *,
    api_url: str,
    link_prefix: str = "",
) -> str:
    category_text = "、".join(display_category(item) for item in categories)
    delta = f"{change.size_delta:+d}"
    flags = ""
    if change.change_type == "new":
        flags += " N"
    if change.minor:
        flags += " m"
    if change.bot:
        flags += " b"
    link = build_diff_url(api_url, change)
    if link_prefix:
        link = f"{link_prefix}{link}"
    lines = [
        change.title,
        f"§ {category_text}",
        f"{delta}{flags} | {change.user} | {_local_clock(change.timestamp)}",
        link,
    ]
    if change.comment:
        lines.append(f"💬{change.comment}")
    for item in diff_lines:
        action = "添加" if item.action == "add" else "删除"
        lines.append(f"✏{action}｢{item.text}｣")
    return "\n".join(lines)


@dataclass(slots=True)
class ChangeSubscription:
    umo: str
    category: str
    start_timestamp: str
    member_page_ids: list[int] = field(default_factory=list)
    member_titles: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return self.umo, self.category.casefold()


@dataclass(slots=True)
class PendingNotification:
    umo: str
    rcid: int
    message: str

    @property
    def key(self) -> tuple[str, int]:
        return self.umo, self.rcid


@dataclass(slots=True)
class MonitorState:
    version: int = STATE_VERSION
    last_timestamp: str = ""
    seen_rcids_at_timestamp: list[int] = field(default_factory=list)
    subscriptions: list[ChangeSubscription] = field(default_factory=list)
    pending: list[PendingNotification] = field(default_factory=list)


@dataclass(slots=True)
class PollResult:
    fetched: int = 0
    matched: int = 0
    queued: int = 0
    sent: int = 0
    failed: int = 0
    bootstrapped: bool = False


class JsonStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def load(self) -> MonitorState:
        return await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> MonitorState:
        if not self.path.is_file():
            return MonitorState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            subscriptions = [
                ChangeSubscription(
                    umo=str(item.get("umo", "")),
                    category=str(item.get("category", "")),
                    start_timestamp=str(item.get("start_timestamp", "")),
                    member_page_ids=[int(value) for value in item.get("member_page_ids", [])],
                    member_titles=[str(value) for value in item.get("member_titles", [])],
                    truncated=bool(item.get("truncated", False)),
                )
                for item in raw.get("subscriptions", [])
                if isinstance(item, dict) and item.get("umo") and item.get("category")
            ]
            pending = [
                PendingNotification(
                    umo=str(item.get("umo", "")),
                    rcid=int(item.get("rcid", 0)),
                    message=str(item.get("message", "")),
                )
                for item in raw.get("pending", [])
                if isinstance(item, dict)
                and item.get("umo")
                and item.get("message")
            ]
            return MonitorState(
                version=STATE_VERSION,
                last_timestamp=str(raw.get("last_timestamp", "")),
                seen_rcids_at_timestamp=[
                    int(value) for value in raw.get("seen_rcids_at_timestamp", [])
                ],
                subscriptions=subscriptions,
                pending=pending,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            LOGGER.warning("Unable to read MediaWiki change state %s: %s", self.path, exc)
            return MonitorState()

    async def save(self, state: MonitorState) -> None:
        await asyncio.to_thread(self._save_sync, state)

    def _save_sync(self, state: MonitorState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "last_timestamp": state.last_timestamp,
            "seen_rcids_at_timestamp": state.seen_rcids_at_timestamp,
            "subscriptions": [asdict(item) for item in state.subscriptions],
            "pending": [asdict(item) for item in state.pending],
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


class WikiChangeMonitor:
    def __init__(
        self,
        client: MediaWikiClient,
        state_path: Path,
        sender: Callable[[str, str], Awaitable[None]],
        *,
        poll_interval_seconds: int = 120,
        category_depth: int = 2,
        max_members: int = 5000,
        max_changes_per_poll: int = 1000,
        max_diff_lines: int = 5,
        max_diff_line_chars: int = 180,
        max_pending: int = 500,
        include_bot_edits: bool = True,
        link_prefix_for_umo: Callable[[str], str] | None = None,
    ) -> None:
        self.client = client
        self.store = JsonStateStore(state_path)
        self.sender = sender
        self.poll_interval_seconds = max(30, min(int(poll_interval_seconds), 86_400))
        self.category_depth = max(0, min(int(category_depth), 10))
        self.max_members = max(1, min(int(max_members), 100_000))
        self.max_changes_per_poll = max(1, min(int(max_changes_per_poll), 10_000))
        self.max_diff_lines = max(0, min(int(max_diff_lines), 50))
        self.max_diff_line_chars = max(20, min(int(max_diff_line_chars), 1000))
        self.max_pending = max(1, min(int(max_pending), 10_000))
        self.include_bot_edits = include_bot_edits
        self.link_prefix_for_umo = link_prefix_for_umo or (lambda _umo: "")
        self.state = MonitorState()
        self._loaded = False
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def load(self) -> None:
        if not self._loaded:
            self.state = await self.store.load()
            self._loaded = True

    async def start(self) -> None:
        await self.load()
        if self._task and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(
            self._run_loop(), name="astrbot-mediawiki-change-monitor"
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run_loop(self) -> None:
        while not self._stopping:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("MediaWiki change polling failed")
            await asyncio.sleep(self.poll_interval_seconds)

    async def subscribe(self, umo: str, raw_category: str) -> ChangeSubscription:
        await self.load()
        umo = str(umo).strip()
        if not umo:
            raise ValueError("当前消息平台没有提供可用于主动推送的会话标识")
        category = normalize_category(raw_category)
        snapshot = await self.client.category_tree(
            category,
            max_depth=self.category_depth,
            max_members=self.max_members,
        )
        now = await self.client.server_timestamp()
        async with self._lock:
            existing = next(
                (
                    item
                    for item in self.state.subscriptions
                    if item.umo == umo and item.category.casefold() == category.casefold()
                ),
                None,
            )
            if existing:
                existing.member_page_ids = sorted(snapshot.page_ids)
                existing.member_titles = sorted(snapshot.titles)
                existing.truncated = snapshot.truncated
                await self.store.save(self.state)
                return existing
            subscription = ChangeSubscription(
                umo=umo,
                category=category,
                start_timestamp=now,
                member_page_ids=sorted(snapshot.page_ids),
                member_titles=sorted(snapshot.titles),
                truncated=snapshot.truncated,
            )
            self.state.subscriptions.append(subscription)
            if not self.state.last_timestamp:
                self.state.last_timestamp = now
                self.state.seen_rcids_at_timestamp = []
            await self.store.save(self.state)
            return subscription

    async def unsubscribe(self, umo: str, raw_category: str = "") -> int:
        await self.load()
        category = normalize_category(raw_category) if raw_category.strip() else ""
        async with self._lock:
            before = len(self.state.subscriptions)
            self.state.subscriptions = [
                item
                for item in self.state.subscriptions
                if not (
                    item.umo == umo
                    and (not category or item.category.casefold() == category.casefold())
                )
            ]
            removed = before - len(self.state.subscriptions)
            if not self.state.subscriptions:
                self.state.last_timestamp = ""
                self.state.seen_rcids_at_timestamp = []
                self.state.pending = []
            await self.store.save(self.state)
            return removed

    async def subscriptions_for(self, umo: str) -> list[ChangeSubscription]:
        await self.load()
        return [item for item in self.state.subscriptions if item.umo == umo]

    async def poll_once(self) -> PollResult:
        await self.load()
        async with self._lock:
            result = PollResult()
            sent, failed = await self._deliver_pending()
            result.sent += sent
            result.failed += failed
            if not self.state.subscriptions:
                return result

            previous_members = {
                item.key: (set(item.member_page_ids), set(item.member_titles))
                for item in self.state.subscriptions
            }
            await self._refresh_memberships()
            if not self.state.last_timestamp:
                self.state.last_timestamp = await self.client.server_timestamp()
                self.state.seen_rcids_at_timestamp = []
                result.bootstrapped = True
                await self.store.save(self.state)
                return result

            changes = await self.client.recent_changes(
                self.state.last_timestamp,
                limit=self.max_changes_per_poll,
                include_bot_edits=self.include_bot_edits,
            )
            seen = set(self.state.seen_rcids_at_timestamp)
            changes = [
                item
                for item in changes
                if not (
                    item.timestamp == self.state.last_timestamp and item.rcid in seen
                )
            ]
            result.fetched = len(changes)
            pending_keys = {item.key for item in self.state.pending}
            consumed: list[RecentChange] = []

            for change in changes:
                targets = self._matching_targets(change, previous_members)
                new_targets = [
                    (umo, categories)
                    for umo, categories in targets.items()
                    if (umo, change.rcid) not in pending_keys
                ]
                if len(self.state.pending) + len(new_targets) > self.max_pending:
                    LOGGER.warning(
                        "MediaWiki pending queue is full (%s); cursor retained for retry",
                        self.max_pending,
                    )
                    break
                result.matched += len(new_targets)
                diff_lines: list[DiffLine] = []
                if new_targets and self.max_diff_lines:
                    try:
                        diff_html = await self.client.compare_revisions(
                            change.old_revid, change.revid
                        )
                        diff_lines = parse_diff_html(
                            diff_html,
                            max_lines=self.max_diff_lines,
                            max_chars=self.max_diff_line_chars,
                        )
                    except Exception:
                        LOGGER.exception(
                            "Unable to fetch MediaWiki diff for revision %s", change.revid
                        )
                for umo, categories in new_targets:
                    message = format_change_notification(
                        change,
                        categories,
                        diff_lines,
                        api_url=self.client.api_url,
                        link_prefix=self.link_prefix_for_umo(umo),
                    )
                    pending = PendingNotification(umo, change.rcid, message)
                    self.state.pending.append(pending)
                    pending_keys.add(pending.key)
                    result.queued += 1
                consumed.append(change)

            if consumed:
                latest_timestamp = consumed[-1].timestamp
                if latest_timestamp == self.state.last_timestamp:
                    updated_seen = seen | {
                        item.rcid
                        for item in consumed
                        if item.timestamp == latest_timestamp
                    }
                else:
                    updated_seen = {
                        item.rcid
                        for item in consumed
                        if item.timestamp == latest_timestamp
                    }
                self.state.last_timestamp = latest_timestamp
                self.state.seen_rcids_at_timestamp = sorted(updated_seen)

            await self.store.save(self.state)
            sent, failed = await self._deliver_pending()
            result.sent += sent
            result.failed += failed
            return result

    async def _refresh_memberships(self) -> None:
        for subscription in self.state.subscriptions:
            try:
                snapshot: CategorySnapshot = await self.client.category_tree(
                    subscription.category,
                    max_depth=self.category_depth,
                    max_members=self.max_members,
                )
                subscription.member_page_ids = sorted(snapshot.page_ids)
                subscription.member_titles = sorted(snapshot.titles)
                subscription.truncated = snapshot.truncated
            except Exception:
                LOGGER.exception(
                    "Unable to refresh MediaWiki category %s", subscription.category
                )

    def _matching_targets(
        self,
        change: RecentChange,
        previous_members: dict[tuple[str, str], tuple[set[int], set[str]]],
    ) -> dict[str, list[str]]:
        targets: dict[str, list[str]] = {}
        for subscription in self.state.subscriptions:
            previous_ids, previous_titles = previous_members.get(
                subscription.key, (set(), set())
            )
            current_ids = set(subscription.member_page_ids)
            current_titles = set(subscription.member_titles)
            matches = (
                (change.pageid > 0 and change.pageid in (previous_ids | current_ids))
                or change.title.casefold() in (previous_titles | current_titles)
            )
            if not matches or (
                subscription.start_timestamp
                and change.timestamp < subscription.start_timestamp
            ):
                continue
            categories = targets.setdefault(subscription.umo, [])
            if subscription.category not in categories:
                categories.append(subscription.category)
        return targets

    async def _deliver_pending(self) -> tuple[int, int]:
        if not self.state.pending:
            return 0, 0
        sent = 0
        failed = 0
        remaining: list[PendingNotification] = []
        for item in self.state.pending:
            try:
                await self.sender(item.umo, item.message)
                sent += 1
            except Exception:
                failed += 1
                remaining.append(item)
                LOGGER.exception(
                    "Unable to push MediaWiki change rcid=%s to %s", item.rcid, item.umo
                )
        self.state.pending = remaining
        await self.store.save(self.state)
        return sent, failed
