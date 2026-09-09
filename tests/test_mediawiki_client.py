import unittest
from unittest.mock import AsyncMock

from mediawiki_client import (
    MediaWikiAPIError,
    MediaWikiClient,
    RecentChange,
    TitleRequest,
    WikiPage,
    extract_wikilink_titles,
    get_index_url,
    normalize_title,
    parse_title_requests,
    validate_api_url,
)


class TitleParsingTests(unittest.TestCase):
    def test_normalize_title(self):
        self.assertEqual(normalize_title("  hello___world  "), "Hello world")

    def test_parse_titles_deduplicates_and_limits(self):
        requests = parse_title_requests("foo#A|Foo#B|bar|baz|qux|last")
        self.assertEqual([item.title for item in requests], ["Foo", "Bar", "Baz", "Qux", "Last"])
        self.assertEqual(requests[0].anchor, "#A")

    def test_extract_wikilinks_uses_target_not_label(self):
        requests = extract_wikilink_titles(
            "@机器人 参见 [[Foo bar|显示名]] 和 &#91;&#91;Baz#章节&#93;&#93;"
        )
        self.assertEqual([item.title for item in requests], ["Foo bar", "Baz"])
        self.assertEqual(requests[1].anchor, "#%E7%AB%A0%E8%8A%82")


class UrlTests(unittest.TestCase):
    def test_validate_api_url(self):
        self.assertEqual(
            validate_api_url("https://example.org/w/api.php"),
            "https://example.org/w/api.php",
        )
        for invalid in (
            "ftp://example.org/w/api.php",
            "https://example.org/wiki/Main_Page",
            "https://user:pass@example.org/w/api.php",
            "https://example.org/w/api.php?x=1",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_api_url(invalid)

    def test_get_index_url(self):
        self.assertEqual(
            get_index_url("https://example.org/w/api.php", {"title": "Foo bar"}),
            "https://example.org/w/index.php?title=Foo+bar",
        )

    def test_page_url_prefers_shortest(self):
        client = MediaWikiClient("https://example.org/w/api.php")
        page = WikiPage(
            title="A very long title",
            pageid=123,
            canonical_url="https://example.org/wiki/A_very_long_title",
            anchor="#Section",
        )
        self.assertEqual(
            client.page_url(page),
            "https://example.org/w/index.php?curid=123#Section",
        )


class ResponseParsingTests(unittest.IsolatedAsyncioTestCase):
    def test_multi_error_response_preserves_action_notallowed_code(self):
        with self.assertRaises(MediaWikiAPIError) as raised:
            MediaWikiClient._raise_for_api_errors(
                {
                    "errors": [
                        {
                            "code": "action-notallowed",
                            "text": "Unauthorized API call",
                            "module": "main",
                        }
                    ]
                }
            )
        self.assertEqual(raised.exception.code, "action-notallowed")
        self.assertEqual(raised.exception.info, "Unauthorized API call")

    async def test_bot_password_login_and_authenticated_query(self):
        client = MediaWikiClient(
            "https://example.org/w/api.php",
            username="Example@AstrBot",
            bot_password="not-a-main-password",
        )
        client._http_json = AsyncMock(
            side_effect=[
                {"query": {"tokens": {"logintoken": "TOKEN+\\"}}},
                {"login": {"result": "Success"}},
                {"query": {"pages": []}},
            ]
        )

        result = await client._request({"action": "query", "format": "json"})

        self.assertEqual(result, {"query": {"pages": []}})
        self.assertEqual(client._http_json.await_count, 3)
        login_call = client._http_json.await_args_list[1]
        self.assertTrue(login_call.kwargs["post"])
        self.assertEqual(login_call.args[0]["lgname"], "Example@AstrBot")
        self.assertEqual(
            login_call.args[0]["lgpassword"], "not-a-main-password"
        )
        query_call = client._http_json.await_args_list[2]
        self.assertEqual(query_call.args[0]["assert"], "user")

    async def test_incomplete_bot_password_configuration_fails_clearly(self):
        client = MediaWikiClient(
            "https://example.org/w/api.php", username="Example@AstrBot"
        )
        with self.assertRaises(MediaWikiAPIError) as raised:
            await client.ensure_authenticated()
        self.assertEqual(raised.exception.code, "credentials-incomplete")

    async def test_query_parses_redirect_summary_and_anchor(self):
        client = MediaWikiClient("https://example.org/w/api.php")

        async def fake_request(_params):
            return {
                "query": {
                    "redirects": [
                        {"from": "Old page", "to": "New page", "tofragment": "Part"}
                    ],
                    "pages": [
                        {
                            "pageid": 42,
                            "ns": 0,
                            "title": "New page",
                            "extract": "Summary",
                            "canonicalurl": "https://example.org/wiki/New_page",
                        }
                    ],
                }
            }

        client._request_with_extract_fallback = fake_request
        result = await client.query_pages([TitleRequest("Old page", "#Input")])
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(result.pages[0].redirect_from, "Old page")
        self.assertEqual(result.pages[0].extract, "Summary")
        self.assertEqual(result.pages[0].anchor, "#Input")

    async def test_query_hides_dangerous_special_redirect(self):
        client = MediaWikiClient("https://example.org/w/api.php")

        async def fake_request(_params):
            return {
                "query": {
                    "specialpagealiases": [
                        {"realname": "Mypage", "aliases": ["MyPage", "我的用户页"]}
                    ],
                    "namespaces": {"-1": {"name": "Special"}},
                    "redirects": [
                        {"from": "Special:MyPage", "to": "User:192.0.2.1"}
                    ],
                    "pages": [
                        {"pageid": 7, "ns": 2, "title": "User:192.0.2.1"}
                    ],
                }
            }

        client._request_with_extract_fallback = fake_request
        result = await client.query_pages([TitleRequest("Special:MyPage")])
        self.assertEqual(result.pages[0].title, "Special:MyPage")
        self.assertTrue(result.pages[0].special)
        self.assertNotIn("192.0.2.1", client.page_url(result.pages[0]))

    async def test_category_tree_follows_subcategories(self):
        client = MediaWikiClient("https://example.org/w/api.php")
        requests = []

        async def fake_request(params):
            requests.append(dict(params))
            if params["cmtitle"] == "Category:Root":
                return {
                    "query": {
                        "categorymembers": [
                            {
                                "pageid": 1,
                                "ns": 0,
                                "title": "Page A",
                                "type": "page",
                            },
                            {
                                "pageid": 2,
                                "ns": 14,
                                "title": "Category:Child",
                                "type": "subcat",
                            },
                        ]
                    }
                }
            return {
                "query": {
                    "categorymembers": [
                        {
                            "pageid": 3,
                            "ns": 0,
                            "title": "Page B",
                            "type": "page",
                        }
                    ]
                }
            }

        client._request = fake_request
        snapshot = await client.category_tree(
            "Category:Root", max_depth=1, max_members=10
        )
        self.assertEqual(snapshot.page_ids, {1, 2, 3})
        self.assertEqual(
            snapshot.categories, ["Category:Root", "Category:Child"]
        )
        self.assertEqual(
            snapshot.category_depths,
            {"category:root": 0, "category:child": 1},
        )
        self.assertEqual([item["cmtitle"] for item in requests], [
            "Category:Root",
            "Category:Child",
        ])

    async def test_recent_changes_parses_fields_and_bot_filter(self):
        client = MediaWikiClient("https://example.org/w/api.php")
        captured = {}

        async def fake_request(params):
            captured.update(params)
            return {
                "query": {
                    "recentchanges": [
                        {
                            "rcid": 4,
                            "type": "edit",
                            "ns": 0,
                            "title": "Page",
                            "pageid": 7,
                            "revid": 11,
                            "old_revid": 10,
                            "user": "Editor",
                            "timestamp": "2026-09-04T00:00:01Z",
                            "comment": "Summary",
                            "oldlen": 20,
                            "newlen": 25,
                            "bot": False,
                            "minor": True,
                        }
                    ]
                }
            }

        client._request = fake_request
        changes = await client.recent_changes(
            "2026-09-04T00:00:00Z", include_bot_edits=False
        )
        self.assertEqual(len(changes), 1)
        self.assertIsInstance(changes[0], RecentChange)
        self.assertEqual(changes[0].size_delta, 5)
        self.assertTrue(changes[0].minor)
        self.assertFalse(changes[0].bot)
        self.assertEqual(captured["rcdir"], "newer")
        self.assertEqual(captured["rcshow"], "!bot")

    async def test_page_categories_collects_continued_memberships(self):
        client = MediaWikiClient("https://example.org/w/api.php")
        requests = []

        async def fake_request(params):
            requests.append(dict(params))
            if "clcontinue" not in params:
                return {
                    "continue": {"clcontinue": "7|next", "continue": "||"},
                    "query": {
                        "pages": [
                            {
                                "pageid": 7,
                                "categories": [{"title": "Category:One"}],
                            }
                        ]
                    },
                }
            return {
                "query": {
                    "pages": [
                        {
                            "pageid": 7,
                            "categories": [{"title": "Category:Two"}],
                        },
                        {"pageid": 8},
                    ]
                }
            }

        client._request = fake_request
        result = await client.page_categories([7, 8, 7])

        self.assertEqual(result[7], {"Category:One", "Category:Two"})
        self.assertEqual(result[8], set())
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["clcontinue"], "7|next")

    async def test_recent_changes_skips_known_rcids_across_api_pages(self):
        client = MediaWikiClient("https://example.org/w/api.php")
        requests = []

        async def fake_request(params):
            requests.append(dict(params))
            if "rccontinue" not in params:
                return {
                    "continue": {"rccontinue": "next"},
                    "query": {
                        "recentchanges": [
                            {
                                "rcid": 1,
                                "type": "edit",
                                "ns": 0,
                                "title": "Known",
                                "pageid": 10,
                                "revid": 100,
                                "old_revid": 99,
                                "timestamp": "2026-09-04T00:00:00Z",
                            }
                        ]
                    },
                }
            return {
                "query": {
                    "recentchanges": [
                        {
                            "rcid": 2,
                            "type": "edit",
                            "ns": 0,
                            "title": "New",
                            "pageid": 11,
                            "revid": 101,
                            "old_revid": 100,
                            "timestamp": "2026-09-04T00:00:01Z",
                        }
                    ]
                }
            }

        client._request = fake_request
        changes = await client.recent_changes(
            "2026-09-04T00:00:00Z", limit=1, exclude_rcids={1}
        )

        self.assertEqual([item.rcid for item in changes], [2])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["rccontinue"], "next")

    async def test_recent_changes_false_flags_are_not_treated_as_present_flags(self):
        client = MediaWikiClient("https://example.org/w/api.php")

        async def fake_request(_params):
            return {
                "query": {
                    "recentchanges": [
                        {
                            "rcid": 9,
                            "type": "edit",
                            "ns": 0,
                            "title": "Human edit",
                            "pageid": 8,
                            "revid": 12,
                            "old_revid": 11,
                            "timestamp": "2026-09-04T00:00:02Z",
                            "bot": False,
                            "minor": False,
                        }
                    ]
                }
            }

        client._request = fake_request
        changes = await client.recent_changes("2026-09-04T00:00:00Z")

        self.assertFalse(changes[0].bot)
        self.assertFalse(changes[0].minor)

    async def test_resolve_page_rejects_missing_page(self):
        client = MediaWikiClient("https://example.org/w/api.php")

        async def fake_query(_requests):
            from mediawiki_client import WikiQueryResult

            return WikiQueryResult(pages=[WikiPage(title="Missing", missing=True)])

        client.query_pages = fake_query
        with self.assertRaises(MediaWikiAPIError) as raised:
            await client.resolve_page("Missing")
        self.assertEqual(raised.exception.code, "page-not-found")

    async def test_compare_revisions_accepts_formatversion_two_body(self):
        client = MediaWikiClient("https://example.org/w/api.php")

        async def fake_request(_params):
            return {"compare": {"body": '<td class="diff-addedline">New</td>'}}

        client._request = fake_request
        diff = await client.compare_revisions(10, 11)
        self.assertIn("diff-addedline", diff)

    async def test_revision_models_are_batched_and_revision_specific(self):
        client = MediaWikiClient("https://example.org/w/api.php")
        calls = []

        async def fake_request(params):
            calls.append(params)
            return {'query': {'pages': [{'contentmodel': 'javascript', 'revisions': [
                {'revid': int(rid), 'slots': {'main': {'contentmodel': 'wikitext'}}}
                for rid in params['revids'].split('|')]}]}}

        client._request = fake_request
        result = await client.revision_content_models([*range(1, 53), 1, 0])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(result), 52)
        self.assertEqual(result[1], 'wikitext')
        self.assertEqual(calls[0]['rvprop'], 'ids|contentmodel')
        self.assertEqual(calls[0]['rvslots'], 'main')
        self.assertNotIn('titles', calls[0])

    async def test_revision_models_unknown_and_legacy(self):
        client = MediaWikiClient("https://example.org/w/api.php")
        client._request = AsyncMock(return_value={'query': {'pages': [
            {'revisions': [{'revid': 1, 'contentmodel': 'css'}, {'revid': 2}]}
        ]}})
        self.assertEqual(await client.revision_content_models([1, 2]), {1: 'css'})
        client._request.reset_mock()
        self.assertEqual(await client.revision_content_models([]), {})
        client._request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
