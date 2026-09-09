"""Minimal source excerpts and adversarial cases for preview boundaries.

Observed sources (2026-09-07), Moegirlpedia:
8648986/8631126: added table separator + data row.
8646519/8646514: three literal br insertions in separate title cells.
8659712/8631124: second field of row 188 changes 1 -> 2.
8391921/8347481: inline highlights cross adjacent category boundaries.
Only the structural excerpts needed for these regressions are reproduced.
"""
import unittest
from html import escape

from change_monitor import format_change_notification, parse_diff_html, _MediaWikiDiffParser
from test_change_monitor import sample_change


def added(text):
    return '<tr><td class="diff-addedline"><div>' + escape(text) + '</div></td></tr>'


def paired(old, new):
    return ('<tr><td class="diff-deletedline">' + old + '</td>'
            '<td class="diff-addedline">' + new + '</td></tr>')


def context(text):
    return ('<tr><td class="diff-context diff-side-deleted">' + escape(text)
            + '</td><td class="diff-context diff-side-added">' + escape(text) + '</td></tr>')


def message(lines, budget=700):
    return format_change_notification(sample_change(), [], lines,
        api_url='https://zh.moegirl.org.cn/api.php', max_message_chars=budget)


class StructuredPreviewTests(unittest.TestCase):
    def test_real_added_table_row_with_one_slot(self):
        source = '| 33 || 2026年{{0}}8月28日 || {{bililink|BV1yBtN6fEs5}} ||'
        lines = parse_diff_html(added('|-') + added(source), max_lines=1)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].action, 'table_add')
        self.assertIn('BV1yBtN6fEs5', lines[0].text)
        self.assertIn('（空）', lines[0].text)
        self.assertNotIn('添加｢|-｣', message(lines))

    def test_nested_templates_and_links_not_split(self):
        source = '| 33 || {{T|{{U|a|b}}}} || [[Page|Label]] ||'
        lines = parse_diff_html(added('|-') + added(source))
        self.assertIn('{{T|{{U|a|b}}}}', lines[0].text)
        self.assertIn('[[Page|Label]]', lines[0].text)

    def test_multiline_and_attributes_use_atomic_source(self):
        for cells in [ ['| rowspan="2" | A || B'], ['| A', '| {{T|x}}', '| B'] ]:
            with self.subTest(cells=cells):
                lines = parse_diff_html(added('|-') + ''.join(map(added, cells)), max_lines=1)
                self.assertEqual(len(lines), 1)
                self.assertEqual(lines[0].action, 'table_add')
                self.assertIn('A', lines[0].text)
                self.assertIn('B', lines[0].text)
                self.assertNotIn('期数', lines[0].text)

    def test_context_breaks_table_grouping(self):
        lines = parse_diff_html(added('|-') + context('unchanged') + added('| A || B'))
        self.assertNotIn('table_add', [line.action for line in lines])

    def test_opposite_sides_not_joined(self):
        lines = parse_diff_html(added('|-') + '<tr><td class="diff-deletedline">| A || B</td></tr>')
        self.assertNotIn('table_add', [line.action for line in lines])

    def test_category_summary_preserves_context_barriers(self):
        for barrier in (context('unchanged'), added('[[Category:B]]')):
            lines = parse_diff_html(added('[[Category:A]]') + added('|-')
                                    + barrier + added('| A || B'))
            self.assertIn('category', [line.action for line in lines])
            self.assertNotIn('table_add', [line.action for line in lines])

    def test_category_summary_preserves_sections_and_positions(self):
        html = '<tr><td class="diff-lineno">Line 10:</td><td class="diff-lineno">Line 10:</td></tr>'
        html += added('[[Category:A]]') + context('== One ==') + added('same')
        html += context('== Two ==') + added('same')
        lines = parse_diff_html(html)
        additions = [line for line in lines if line.action == 'add']
        self.assertEqual(len(additions), 2)
        self.assertEqual([line.positions[0][1] for line in additions], [12, 14])

    def test_unfinished_template_does_not_consume_following_table_row(self):
        lines = parse_diff_html(added('|-') + added('| {{T') + added('|-') + added('| A || B'))
        self.assertNotIn('table_add', [line.action for line in lines])

    def test_template_context_not_inferred_as_table(self):
        lines = parse_diff_html(context('{{T') + added('|-') + added('| A || B') + context('}}'))
        self.assertNotIn('table_add', [line.action for line in lines])

    def test_removed_table_row(self):
        html = (added('|-') + added('| A || B')).replace('diff-addedline', 'diff-deletedline')
        lines = parse_diff_html(html, max_lines=1)
        self.assertEqual(lines[0].action, 'table_delete')
        self.assertIn('删除表格行', message(lines))

    def test_two_table_rows_remain_separate(self):
        lines = parse_diff_html((added('|-') + added('| A || B')) * 2)
        self.assertEqual(len(lines), 2)

    def test_three_br_insertions_not_existing_br(self):
        html = ''.join(paired('标题' + str(i) + '&lt;br /&gt;结尾',
                              '标题' + str(i) + '<ins>&lt;br /&gt;</ins>&lt;br /&gt;结尾')
                       for i in range(3))
        lines = parse_diff_html(html, max_lines=1)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].count, 3)
        self.assertEqual(len(lines[0].positions), 3)
        self.assertIn('添加换行标签（3处）', message(lines))

    def test_repeat_with_other_edits_preserves_other_edits(self):
        html = paired('A', 'A<ins>&lt;br /&gt;</ins>')
        html += paired('B<del>old</del>', 'B<ins>new</ins><ins>&lt;br /&gt;</ins>')
        # Unequal anchors deliberately trigger full before/after fallback.
        lines = parse_diff_html(html)
        self.assertIn('new', message(lines))

    def test_full_text_dedup_before_budget(self):
        lines = parse_diff_html(added('same') * 3 + added('different'), max_lines=2)
        self.assertEqual([x.count for x in lines], [3, 1])
        self.assertIn('different', message(lines))

    def test_truncated_prefix_collision_not_merged(self):
        lines = parse_diff_html(added('A' * 100 + 'one') + added('A' * 100 + 'two'), max_chars=20)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(x.count == 1 for x in lines))

    def test_known_sections_do_not_merge(self):
        lines = parse_diff_html(context('== One ==') + added('same')
                                + context('== Two ==') + added('same'))
        self.assertEqual(len(lines), 2)

    def test_short_number_keeps_row_key_and_location(self):
        html = '<tr><td class="diff-lineno">第72行：</td><td class="diff-lineno">第72行：</td></tr>'
        html += context('| 187 || 1 || date') + context('|-')
        html += paired('| 188 || <del>1</del> || date', '| 188 || <ins>2</ins> || date')
        lines = parse_diff_html(html)
        self.assertIn('188', lines[0].text)
        self.assertEqual(lines[0].positions, ((74, 74),))
        self.assertIn('第74行', message(lines))
        self.assertNotIn('总回数：1', message(lines))

    def test_context_and_line_numbers_retained(self):
        parser = _MediaWikiDiffParser()
        parser.feed('<tr><td class="diff-lineno">Line 10:</td><td class="diff-lineno">Line 20:</td></tr>'
                    + context('line') + added('new') + paired('old', 'new'))
        parser.close()
        self.assertEqual([(r.old_line, r.new_line) for r in parser.rows], [(10,20),(11,21),(11,22)])

    def test_adjacent_category_highlight_not_a_node(self):
        html = paired('[[分类:百合<del>]][[分类:偶像</del>]][[分类:歌姬]]',
                      '[[分类:百合]][[分类:歌姬]]')
        self.assertIn('移出分类｢偶像｣', message(parse_diff_html(html)))
        self.assertNotIn('移出分类｢百合｣', message(parse_diff_html(html)))

    def test_non_wikitext_models_disable_semantics(self):
        for model in ('css', 'javascript', 'Scribunto', 'json', 'unknown'):
            with self.subTest(model=model):
                lines = parse_diff_html(added('[[Category:A]]') + added('|-') + added('| A || B'), content_model=model)
                self.assertTrue(all(x.action == 'add' for x in lines))
                self.assertNotIn('link_add', [x.action for x in parse_diff_html(paired('A', '[[A]]'), content_model=model)])

    def test_protected_context_disables_category_and_break_summary(self):
        for opener, closer in [('<!--', '-->'), ('<nowiki>', '</nowiki>'), ('{{T', '}}')]:
            with self.subTest(opener=opener):
                lines = parse_diff_html(context(opener) + added('[[Category:A]]') + context(closer))
                self.assertNotIn('category', [x.action for x in lines])
        lines = parse_diff_html(context('<nowiki>') + paired('A','A<ins>&lt;br /&gt;</ins>') + context('</nowiki>'))
        self.assertNotIn('break_add', [x.action for x in lines])

    def test_css_comment_preserved_without_membership_claim(self):
        lines = parse_diff_html(added('/* [[Category:A]] */'), content_model='css')
        self.assertIn('/* [[Category:A]] */', message(lines))
        self.assertNotIn('加入分类', message(lines))

    def test_all_previews_respect_700_char_limit(self):
        html = ''.join(added('|-') + added('| ' + str(i) + ' || ' + '数据' * 80) for i in range(20))
        lines = parse_diff_html(html, max_lines=20)
        text = message(lines)
        self.assertLessEqual(len(text), 700)
        self.assertIn('已达到消息长度上限', text)
        self.assertIn('diff=8651943&oldid=8651800', text)


if __name__ == '__main__':
    unittest.main()
