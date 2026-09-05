import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from change_monitor import (
    SUBSCRIPTION_CATEGORY,
    SUBSCRIPTION_PAGE,
    JsonStateStore,
    WikiChangeMonitor,
    format_change_notification,
    normalize_category,
    overlap_timestamp,
    parse_diff_html,
)
from mediawiki_client import CategorySnapshot, RecentChange, WikiPage


def sample_change() -> RecentChange:
    return RecentChange(
        rcid=101,
        change_type="edit",
        namespace=0,
        title="下定决心Hand in Hand",
        pageid=42,
        revid=8651943,
        old_revid=8651800,
        user="Καλλιόπη",
        timestamp="2026-09-04T01:57:00Z",
        comment="补充翻唱版本",
        old_length=1000,
        new_length=1095,
    )


class FormattingTests(unittest.TestCase):
    def test_normalize_category(self):
        self.assertEqual(normalize_category(" 分类：测试分类 "), "Category:测试分类")
        self.assertEqual(normalize_category("Category:Foo_bar"), "Category:Foo bar")

    def test_overlap_timestamp(self):
        self.assertEqual(
            overlap_timestamp("2026-09-04T01:00:00Z", 60),
            "2026-09-04T00:59:00Z",
        )

    def test_parse_diff_html_extracts_added_and_deleted_rows(self):
        html = """
        <tr>
          <td class="diff-deletedline"><div>旧的<del>内容</del></div></td>
          <td class="diff-addedline"><div>新的<ins>内容</ins></div></td>
        </tr>
        <tr><td class="diff-addedline"><div>{{Template}}</div></td></tr>
        """
        lines = parse_diff_html(html, max_lines=3, max_chars=100)
        self.assertEqual(
            [(item.action, item.text) for item in lines],
            [
                ("before_after", "旧的内容"),
                ("add", "{{Template}}"),
            ],
        )

    def test_format_change_notification(self):
        change = sample_change()
        lines = parse_diff_html(
            '<td class="diff-addedline">==翻唱版本==</td>', max_lines=5
        )
        message = format_change_notification(
            change,
            ["Category:SIFAC"],
            lines,
            api_url="https://zh.moegirl.org.cn/api.php",
        )
        self.assertIn("下定决心Hand in Hand", message)
        self.assertIn("订阅：SIFAC", message)
        self.assertIn("+95 | Καλλιόπη", message)
        self.assertIn("diff=8651943", message)
        self.assertIn("oldid=8651800", message)
        self.assertIn("✏添加｢==翻唱版本==｣", message)


class FakeClient:
    api_url = "https://zh.moegirl.org.cn/api.php"

    def __init__(self):
        self.changes = [sample_change()]
        self.category_tree_calls = 0
        self.page_categories_calls = 0
        self.page_category_map = {}
        self.page_categories_error = None
        self.category_tree_categories = ["Category:SIFAC"]
        self.recent_since = ""

    async def server_timestamp(self):
        return "2026-09-04T01:00:00Z"

    async def category_tree(self, _category, **_kwargs):
        self.category_tree_calls += 1
        return CategorySnapshot(
            page_ids={42},
            titles={"下定决心hand in hand"},
            categories=list(self.category_tree_categories),
        )

    async def page_categories(self, page_ids):
        self.page_categories_calls += 1
        if self.page_categories_error:
            raise self.page_categories_error
        return {page_id: set(self.page_category_map.get(page_id, set())) for page_id in page_ids}

    async def resolve_page(self, title):
        return WikiPage(title=title, pageid=42)

    async def recent_changes(self, since, **kwargs):
        self.recent_since = since
        excluded = kwargs.get("exclude_rcids", set())
        return [item for item in self.changes if item.rcid not in excluded]

    async def compare_revisions(self, _old_revid, _revid):
        return '<td class="diff-addedline">==翻唱版本==</td>'


class MonitorWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_category_lookup_failure_does_not_consume_new_page(self):
        client = FakeClient()
        client.changes = [
            replace(
                sample_change(), rcid=199, change_type="new", pageid=96,
                old_revid=0, title="稍后重试的页面",
                timestamp=datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ).replace("+00:00", "Z"),
            )
        ]
        client.page_categories_error = RuntimeError("temporary category error")

        async def sender(_umo, _message):
            return None

        with tempfile.TemporaryDirectory() as directory:
            monitor = WikiChangeMonitor(client, Path(directory) / "state.json", sender)
            await monitor.subscribe_category("aiocqhttp:group:123", "SIFAC")
            with self.assertRaisesRegex(RuntimeError, "temporary category error"):
                await monitor.poll_once()
            self.assertNotIn(199, monitor.state.recent_rcids)

    async def test_unmatched_new_page_is_retried_during_category_grace_window(self):
        client = FakeClient()
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
        client.changes = [
            replace(
                sample_change(), rcid=201, change_type="new", pageid=98,
                old_revid=0, title="分类索引稍后出现的页面", timestamp=timestamp,
            )
        ]
        client.page_category_map = {98: set()}
        messages = []

        async def sender(_umo, message):
            messages.append(message)

        with tempfile.TemporaryDirectory() as directory:
            monitor = WikiChangeMonitor(client, Path(directory) / "state.json", sender)
            await monitor.subscribe_category("aiocqhttp:group:123", "SIFAC")

            first = await monitor.poll_once()
            self.assertEqual(first.queued, 0)
            self.assertNotIn(201, monitor.state.recent_rcids)

            client.page_category_map[98] = {"Category:SIFAC"}
            second = await monitor.poll_once()
            self.assertEqual(second.queued, 1)
            self.assertIn(201, monitor.state.recent_rcids)
            self.assertIn("分类索引稍后出现的页面", messages[0])

    async def test_unrelated_old_new_page_does_not_hold_cursor_forever(self):
        client = FakeClient()
        client.changes = [
            replace(
                sample_change(), rcid=200, change_type="new", pageid=97,
                old_revid=0, title="无关页面", timestamp="2020-01-01T00:00:00Z",
            )
        ]
        client.page_category_map = {97: {"Category:Other"}}

        async def sender(_umo, _message):
            return None

        with tempfile.TemporaryDirectory() as directory:
            monitor = WikiChangeMonitor(client, Path(directory) / "state.json", sender)
            await monitor.subscribe_category("aiocqhttp:group:123", "SIFAC")
            result = await monitor.poll_once()
            self.assertEqual(result.queued, 0)
            self.assertIn(200, monitor.state.recent_rcids)

    async def test_new_page_in_watched_category_is_sent_before_tree_refresh(self):
        client = FakeClient()
        client.changes = [
            replace(
                sample_change(),
                rcid=202,
                change_type="new",
                pageid=99,
                revid=9001,
                old_revid=0,
                title="刚创建的页面",
                timestamp="2026-09-04T01:00:01Z",
            )
        ]
        client.page_category_map = {99: {"Category:SIFAC"}}
        messages = []

        async def sender(_umo, message):
            messages.append(message)

        with tempfile.TemporaryDirectory() as directory:
            monitor = WikiChangeMonitor(client, Path(directory) / "state.json", sender)
            await monitor.subscribe_category("aiocqhttp:group:123", "SIFAC")
            result = await monitor.poll_once()

            self.assertEqual(result.queued, 1)
            self.assertEqual(client.category_tree_calls, 1)
            self.assertEqual(client.page_categories_calls, 1)
            self.assertIn("刚创建的页面", messages[0])
            self.assertIn(" N |", messages[0])
            self.assertIn(99, monitor.state.subscriptions[0].member_page_ids)

    async def test_new_page_in_known_subcategory_is_sent(self):
        client = FakeClient()
        client.category_tree_categories = ["Category:Root", "Category:Child"]
        client.changes = [
            replace(
                sample_change(), rcid=203, change_type="new", pageid=100,
                old_revid=0, title="子分类新页面",
                timestamp="2026-09-04T01:00:01Z",
            )
        ]
        client.page_category_map = {100: {"Category:Child"}}

        async def sender(_umo, _message):
            return None

        with tempfile.TemporaryDirectory() as directory:
            monitor = WikiChangeMonitor(client, Path(directory) / "state.json", sender)
            await monitor.subscribe_category("aiocqhttp:group:123", "Root")
            result = await monitor.poll_once()
            self.assertEqual(result.queued, 1)

    async def test_new_subcategory_connects_new_page_in_same_batch(self):
        client = FakeClient()
        client.category_tree_categories = ["Category:Root"]
        client.changes = [
            replace(
                sample_change(), rcid=204, change_type="new", namespace=0,
                pageid=101, old_revid=0, title="新子分类中的页面",
                timestamp="2026-09-04T01:00:01Z",
            ),
            replace(
                sample_change(), rcid=205, change_type="new", namespace=14,
                pageid=102, old_revid=0, title="Category:New Child",
                timestamp="2026-09-04T01:00:02Z",
            ),
        ]
        client.page_category_map = {
            101: {"Category:New Child"},
            102: {"Category:Root"},
        }

        async def sender(_umo, _message):
            return None

        with tempfile.TemporaryDirectory() as directory:
            monitor = WikiChangeMonitor(client, Path(directory) / "state.json", sender)
            await monitor.subscribe_category("aiocqhttp:group:123", "Root")
            result = await monitor.poll_once()
            self.assertEqual(result.queued, 2)
            self.assertIn("category:new child", monitor.state.subscriptions[0].member_categories)

    async def test_new_subcategory_respects_configured_category_depth(self):
        client = FakeClient()
        client.category_tree_categories = ["Category:Root"]
        client.changes = [
            replace(
                sample_change(), rcid=206, change_type="new", namespace=14,
                pageid=103, old_revid=0, title="Category:New Child",
                timestamp="2026-09-04T01:00:01Z",
            ),
            replace(
                sample_change(), rcid=207, change_type="new", namespace=0,
                pageid=104, old_revid=0, title="深度之外的页面",
                timestamp="2026-09-04T01:00:02Z",
            ),
        ]
        client.page_category_map = {
            103: {"Category:Root"},
            104: {"Category:New Child"},
        }
        messages = []

        async def sender(_umo, message):
            messages.append(message)

        with tempfile.TemporaryDirectory() as directory:
            monitor = WikiChangeMonitor(
                client, Path(directory) / "state.json", sender, category_depth=0
            )
            await monitor.subscribe_category("aiocqhttp:group:123", "Root")
            result = await monitor.poll_once()
            self.assertEqual(result.queued, 1)
            self.assertIn("Category:New Child", messages[0])
            self.assertNotIn(
                "category:new child",
                monitor.state.subscriptions[0].member_categories,
            )

    async def test_single_page_subscription_matches_by_pageid_and_tracks_rename(self):
        client = FakeClient()
        messages = []

        async def sender(_umo, message):
            messages.append(message)

        with tempfile.TemporaryDirectory() as directory:
            monitor = WikiChangeMonitor(
                client, Path(directory) / "state.json", sender
            )
            subscription = await monitor.subscribe_page(
                "aiocqhttp:group:123", "原条目名"
            )
            self.assertEqual(subscription.kind, SUBSCRIPTION_PAGE)
            self.assertEqual(subscription.page_id, 42)

            result = await monitor.poll_once()

            self.assertEqual(result.queued, 1)
            self.assertTrue(messages[0].startswith("下定决心Hand in Hand\n"))
            self.assertNotIn("单条目：", messages[0])
            self.assertEqual(subscription.target, "下定决心Hand in Hand")

    async def test_recent_changes_uses_overlap_and_durable_rcid_deduplication(self):
        client = FakeClient()

        async def sender(_umo, _message):
            return None

        with tempfile.TemporaryDirectory() as directory:
            monitor = WikiChangeMonitor(
                client,
                Path(directory) / "state.json",
                sender,
                overlap_seconds=60,
            )
            await monitor.subscribe_page("aiocqhttp:group:123", "条目")
            await monitor.poll_once()
            self.assertEqual(client.recent_since, "2026-09-04T00:59:00Z")
            self.assertEqual(monitor.state.recent_rcids, [101])

            reloaded = WikiChangeMonitor(
                client,
                Path(directory) / "state.json",
                sender,
                overlap_seconds=60,
            )
            result = await reloaded.poll_once()
            self.assertEqual(result.queued, 0)

    async def test_category_refresh_is_shared_for_duplicate_subscriptions(self):
        client = FakeClient()

        async def sender(_umo, _message):
            return None

        with tempfile.TemporaryDirectory() as directory:
            monitor = WikiChangeMonitor(
                client, Path(directory) / "state.json", sender
            )
            await monitor.subscribe("aiocqhttp:group:1", "SIFAC")
            await monitor.subscribe("aiocqhttp:group:2", "SIFAC")
            client.category_tree_calls = 0
            monitor._category_refresh_at.clear()

            await monitor.poll_once()

            self.assertEqual(client.category_tree_calls, 1)

    async def test_v1_state_is_migrated_to_category_subscription(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "last_timestamp": "2026-09-04T01:00:00Z",
                        "seen_rcids_at_timestamp": [99],
                        "subscriptions": [
                            {
                                "umo": "aiocqhttp:group:123",
                                "category": "Category:SIFAC",
                                "start_timestamp": "2026-09-04T00:00:00Z",
                                "member_page_ids": [42],
                                "member_titles": ["page"],
                            }
                        ],
                        "pending": [],
                    }
                ),
                encoding="utf-8",
            )
            state = await JsonStateStore(state_path).load()

            self.assertEqual(state.version, 3)
            self.assertEqual(state.recent_rcids, [99])
            self.assertEqual(state.subscriptions[0].kind, SUBSCRIPTION_CATEGORY)
            self.assertEqual(state.subscriptions[0].target, "Category:SIFAC")

    async def test_failed_send_is_persisted_and_retried_without_duplicate(self):
        client = FakeClient()
        attempts = []
        fail = True

        async def sender(umo, message):
            attempts.append((umo, message))
            if fail:
                raise RuntimeError("temporary failure")

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            monitor = WikiChangeMonitor(client, state_path, sender)
            await monitor.subscribe("aiocqhttp:group:123", "SIFAC")
            with self.assertLogs("change_monitor", level="ERROR"):
                first = await monitor.poll_once()
            self.assertEqual(first.queued, 1)
            self.assertEqual(first.failed, 1)
            self.assertEqual(len(monitor.state.pending), 1)
            self.assertTrue(state_path.is_file())

            fail = False
            reloaded = WikiChangeMonitor(client, state_path, sender)
            await reloaded.load()
            self.assertEqual(len(reloaded.state.pending), 1)
            second = await reloaded.poll_once()
            self.assertEqual(second.sent, 1)
            self.assertEqual(second.queued, 0)
            self.assertEqual(len(reloaded.state.pending), 0)
            self.assertEqual(len(attempts), 2)

    async def test_same_session_combines_matching_categories(self):
        client = FakeClient()
        messages = []

        async def sender(_umo, message):
            messages.append(message)

        with tempfile.TemporaryDirectory() as directory:
            monitor = WikiChangeMonitor(
                client, Path(directory) / "state.json", sender
            )
            await monitor.subscribe("aiocqhttp:group:123", "SIFAC")
            await monitor.subscribe("aiocqhttp:group:123", "LoveLive")
            result = await monitor.poll_once()
            self.assertEqual(result.queued, 1)
            self.assertEqual(len(messages), 1)
            self.assertIn("订阅：SIFAC、LoveLive", messages[0])


if __name__ == "__main__":
    unittest.main()
