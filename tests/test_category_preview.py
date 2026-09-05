import unittest
from html import escape
from dataclasses import replace
from change_monitor import parse_diff_html, format_change_notification
from test_change_monitor import sample_change


def diff(old, new):
    rows = []
    for index in range(max(len(old), len(new))):
        cells = ''
        for values, side in ((old, 'deleted'), (new, 'added')):
            if index < len(values):
                cells += f'<td class="diff-{side}line">{escape(values[index])}</td>'
        rows.append('<tr>' + cells + '</tr>')
    return ''.join(rows)


class CategoryPreviewTests(unittest.TestCase):
    def test_hotcat_cross_line_example(self):
        html = diff(['}}<noinclude>[[Category:Userboxes|Human]]</noinclude>'],
                    ['}}<noinclude> ', '[[Category:Human life user templates|Human]]', '</noinclude>'])
        previews = parse_diff_html(html, max_lines=1)
        self.assertEqual(len(previews), 1)
        self.assertIn('✏移出分类｢Userboxes｣', previews[0].text)
        self.assertIn('✏加入分类｢Human life user templates｣', previews[0].text)
        self.assertNotIn('noinclude', previews[0].text)
        self.assertNotIn('排序键', previews[0].text)

    def test_sort_key_is_not_a_membership_change(self):
        previews = parse_diff_html(diff(['[[分类:歌曲|A]]'], ['[[Category:歌曲|B]]']))
        self.assertIn('排序键：｢A｣ → ｢B｣', previews[0].text)
        self.assertNotIn('加入', previews[0].text)

    def test_empty_key_differs_from_default(self):
        previews = parse_diff_html(diff(['[[Category:A]]'], ['[[Category:A|]]']))
        self.assertIn('（使用默认排序键） → （空排序键）', previews[0].text)

    def test_order_change_does_not_report_add_remove(self):
        previews = parse_diff_html(diff(['[[Category:A]]', '[[Category:B]]'],
                                       ['[[Category:B]]', '[[Category:A]]']))
        self.assertIn('排列', previews[0].text)
        self.assertNotIn('加入', previews[0].text)
        self.assertNotIn('移出', previews[0].text)

    def test_non_category_markup_and_mixed_text_preserved(self):
        for text in ('[[:Category:A]]', '<nowiki>[[Category:A]]</nowiki>',
                     '<!-- [[Category:A]] -->', '{{T|[[Category:A]]}}'):
            with self.subTest(text=text):
                self.assertNotIn('category', [x.action for x in parse_diff_html(diff([], [text]))])
        previews = parse_diff_html(diff(['旧正文', '[[Category:A]]'],
                                       ['新正文', '[[Category:B]]']))
        self.assertEqual(previews[0].action, 'category')
        self.assertTrue(any('新正文' in x.replacement for x in previews[1:]))

    def test_only_exact_repeated_hotcat_summary_is_hidden(self):
        previews = parse_diff_html(diff([], ['[[分类:歌曲]]']))
        for comment, keep in [('添加分类:歌曲——HotCat', False),
                              ('添加分类:歌曲——HotCat，因为这是角色歌', True),
                              ('添加分类:其他——HotCat', True)]:
            message = format_change_notification(replace(sample_change(), comment=comment),
                [], previews, api_url='https://example.org/api.php')
            self.assertEqual('💬' in message, keep)

    def test_many_categories_show_count_and_keep_pair_atomic(self):
        previews = parse_diff_html(diff(['[[Category:Old]]'],
                                       [f'[[Category:New{i}]]' for i in range(8)]), max_lines=1)
        self.assertEqual(len(previews), 1)
        self.assertIn('移出分类', previews[0].text)
        self.assertIn('等 8 项', previews[0].text)

    def test_message_limit_never_splits_category_bundle(self):
        previews = parse_diff_html(diff(['[[Category:' + '旧' * 80 + ']]'],
                                       ['[[Category:' + '新' * 80 + ']]']))
        message = format_change_notification(
            replace(sample_change(), title='很长的标题' * 40, comment='编辑原因' * 40),
            ['Category:' + '分类' * 60], previews,
            api_url='https://example.org/api.php', max_message_chars=500)
        self.assertLessEqual(len(message), 500)
        self.assertEqual('✏移出分类' in message, '✏加入分类' in message)

    def test_noinclude_scope_changes_are_not_discarded(self):
        previews = parse_diff_html(diff(['<noinclude>[[Category:A]]</noinclude>'],
                                       ['[[Category:A]]']))
        self.assertTrue(any('noinclude' in (item.text + item.replacement)
                            for item in previews))
