import unittest
from dataclasses import replace

from change_monitor import DiffLine, parse_diff_html, format_change_notification
from test_change_monitor import sample_change


def row(old, new):
    return (f'<tr><td class="diff-deletedline"><div>{old}</div></td>'
            f'<td class="diff-addedline"><div>{new}</div></td></tr>')


class DiffPreviewTests(unittest.TestCase):
    def test_long_paragraph_link_wrapping_is_short(self):
        middle = 'が約4年ぶりのフル・アルバムを完成させた。' * 20
        old = '{{lj|RAISE A SUILEN' + middle + 'SAVAGEという作品。}}'
        new = '{{lj|[[RAISE A SUILEN]]' + middle + '[[SAVAGE]]という作品。}}'
        lines = parse_diff_html(row(old, new))
        self.assertEqual([(x.action, x.text) for x in lines],
                         [('link_add', 'RAISE A SUILEN'), ('link_add', 'SAVAGE')])
        message = format_change_notification(sample_change(), [], lines,
                                            api_url='https://example.org/api.php')
        self.assertIn('✏为｢RAISE A SUILEN｣添加内链', message)
        self.assertNotIn('修改前', message)

    def test_piped_link_keeps_target(self):
        lines = parse_diff_html(row('曲「Apocalypse」。',
                                    '曲「[[Apocalypse(RAISE A SUILEN)|Apocalypse]]」。'))
        self.assertEqual(lines[0].action, 'link_add')
        self.assertEqual(lines[0].replacement, 'Apocalypse(RAISE A SUILEN)')

    def test_link_summary_does_not_hide_other_changes(self):
        lines = parse_diff_html(row('A和B', '[[A]]和C'))
        self.assertNotIn('link_add', [x.action for x in lines])
        self.assertIn('C', lines[0].replacement)

    def test_category_and_nowiki_do_not_become_link_summary(self):
        for old, new in [('Category:A', '[[Category:A]]'),
                         ('&lt;nowiki&gt;A&lt;/nowiki&gt;',
                          '&lt;nowiki&gt;[[A]]&lt;/nowiki&gt;')]:
            self.assertNotIn('link_add', [x.action for x in parse_diff_html(row(old, new))])

    def test_separated_edits_get_independent_short_previews(self):
        middle = '保持原样的内容。' * 30
        lines = parse_diff_html(row('old' + middle + 'first', 'new' + middle + 'second'))
        self.assertGreaterEqual(len(lines), 2)
        self.assertIn('new', lines[0].replacement)
        self.assertIn('second', lines[-1].replacement)
        for line in lines:
            self.assertLessEqual(len(line.text), 60)
            self.assertLessEqual(len(line.replacement), 60)

    def test_total_message_budget_preserves_pairs_and_link(self):
        lines = [DiffLine('before_after', '旧' * 180, '新' * 180)] * 5
        for budget in (500, 700, 900):
            message = format_change_notification(
                sample_change(), ['Category:测试'], lines,
                api_url='https://example.org/api.php', max_message_chars=budget)
            self.assertLessEqual(len(message), budget)
            self.assertIn('https://example.org/index.php?diff=', message)
            self.assertIn('已达到消息长度上限', message)
            self.assertEqual(message.count('修改前'), message.count('修改后'))

    def test_late_change_is_visible_in_long_paragraph(self):
        prefix, suffix = '共同内容' * 100, '相同结尾' * 100
        lines = parse_diff_html(row(prefix + 'anzu' + suffix,
                                    prefix + '泷泽杏' + suffix), max_chars=80)
        self.assertIn('anzu', lines[0].text)
        self.assertIn('泷泽杏', lines[0].replacement)
        self.assertLessEqual(len(lines[0].text), 80)
        self.assertTrue(lines[0].text.startswith('…'))

    def test_huge_metadata_still_obeys_limit(self):
        change = replace(sample_change(), title='标题' * 500, comment='摘要' * 500)
        message = format_change_notification(change, ['分类' * 500], [],
                    api_url='https://example.org/api.php', max_message_chars=500)
        self.assertLessEqual(len(message), 500)
        self.assertIn('https://example.org/index.php?diff=', message)

    def test_reference_two_replacements(self):
        html = row('<del>anzu</del>、<del>yuzuha</del>',
                   '<ins>泷泽杏</ins>、<ins>皇柚叶</ins>')
        lines = parse_diff_html(html)
        self.assertEqual([(x.action, x.text, x.replacement) for x in lines], [
            ('replace', 'anzu', '泷泽杏'), ('replace', 'yuzuha', '皇柚叶')])
        change = replace(sample_change(), comment='/* 新体制 */ 更新名单')
        text = format_change_notification(change, ['单条目：测试'], lines,
                                          api_url='https://example.org/api.php')
        self.assertIn(' § 新体制\n', text)
        self.assertIn('✏把｢anzu｣改成｢泷泽杏｣', text)
        self.assertIn('💬更新名单', text)
        self.assertNotIn('订阅：', text)

    def test_unrelated_spans_fall_back_to_paired_rows(self):
        lines = parse_diff_html(row('A<del>x</del>B', 'C<ins>y</ins>D'))
        self.assertEqual(lines[0].action, 'before_after')
        self.assertEqual(lines[0].replacement, 'CyD')

    def test_blank_lines_and_whitespace_are_visible(self):
        lines = parse_diff_html(row(' ', '\t'))
        self.assertEqual((lines[0].text, lines[0].replacement), ('␠', '⇥'))
        lines = parse_diff_html('<tr><td class="diff-addedline"><div></div></td></tr>')
        self.assertEqual(lines[0].text, '（空行）')

    def test_html_entities_and_wikitext_are_preserved(self):
        lines = parse_diff_html(row('[[<del>A</del>|名字]]',
                                    '[[<ins>B</ins>|名字]]'))
        self.assertEqual(lines[0].action, 'replace')
        self.assertEqual(lines[0].text, '[[A|名字]]')
        self.assertEqual(lines[0].replacement, '[[B|名字]]')
        lines = parse_diff_html('<td class="diff-addedline">&lt;nowiki&gt;{{T}}&lt;/nowiki&gt;</td>')
        self.assertEqual(lines[0].text, '<nowiki>{{T}}</nowiki>')

    def test_budget_keeps_before_after_atomic_and_covers_later_rows(self):
        html = row('<del>A</del>、<del>B</del>', '<ins>C</ins>、<ins>D</ins>')
        html += row('old', 'new')
        lines = parse_diff_html(html, max_lines=2)
        self.assertEqual([x.action for x in lines], ['replace', 'before_after', 'notice'])
        self.assertIn('1 项', lines[-1].text)
        self.assertEqual(lines[1].replacement, 'new')

    def test_disabled_empty_and_truncated(self):
        self.assertEqual(parse_diff_html('', max_lines=0), [])
        self.assertEqual(parse_diff_html('<table></table>')[0].action, 'notice')
        lines = parse_diff_html(row('a' * 50, 'b' * 50), max_chars=20)
        self.assertEqual(len(lines[0].text), 20)
        self.assertTrue(lines[0].replacement.endswith('…'))

    def test_no_pairing_across_rows(self):
        html = ('<tr><td class="diff-deletedline">old</td></tr>'
                '<tr><td class="diff-addedline">new</td></tr>')
        self.assertEqual([x.action for x in parse_diff_html(html)], ['delete', 'add'])
