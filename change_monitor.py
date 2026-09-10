from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

try:
    from .mediawiki_client import (
        CategorySnapshot,
        MediaWikiClient,
        RecentChange,
        get_index_url,
        normalize_title,
    )
except ImportError:  # Allows the helpers to be tested outside AstrBot.
    from mediawiki_client import (  # type: ignore[no-redef]
        CategorySnapshot,
        MediaWikiClient,
        RecentChange,
        get_index_url,
        normalize_title,
    )


LOGGER = logging.getLogger(__name__)
STATE_VERSION = 4
SUBSCRIPTION_CATEGORY = "category"
SUBSCRIPTION_PAGE = "page"


def normalize_category(raw: str) -> str:
    value = re.sub(r"[\s_]+", " ", raw).strip()
    value = re.sub(r"^(?:category|分类)\s*[:：]\s*", "", value, flags=re.I)
    if not value:
        raise ValueError("分类名不能为空")
    return f"Category:{value}"


def display_category(category: str) -> str:
    return re.sub(r"^(?:category|分类)\s*[:：]\s*", "", category, flags=re.I)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def overlap_timestamp(timestamp: str, seconds: int) -> str:
    """Move an API cursor backwards to include late RecentChanges rows."""
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return (moment - timedelta(seconds=max(0, int(seconds)))).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
    except (TypeError, ValueError):
        return timestamp


@dataclass(slots=True, frozen=True)
class DiffLine:
    action: str
    text: str
    replacement: str = ""
    count: int = 1
    positions: tuple[tuple[int | None, int | None], ...] = ()
    # Complete pre-cropping identity; never deduplicate rendered/truncated text.
    identity: tuple[str, ...] = ()
    show_position: bool = False
    context: str = ""


@dataclass(slots=True)
class DiffRow:
    cells: list[tuple[str, str, list[str], list[str]]]
    old_line: int | None
    new_line: int | None


class _MediaWikiDiffParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._action = ""
        self._buffer: list[str] = []
        self._cell_depth = 0
        self.blocks: list[list[tuple[str, str, list[str], list[str]]]] = []
        self._row: list[tuple[str, str, list[str], list[str]]] = []
        self._in_row = False
        self._mark_depth = 0
        self._parts: list[str] = []
        self._anchors: list[str] = []
        self.rows: list[DiffRow] = []
        self._old_line: int | None = None
        self._new_line: int | None = None
        self._line_column = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._flush_row()
            self._in_row = True
            self._line_column = 0
        if self._action:
            if tag == "td":
                self._cell_depth += 1
            if tag in {"ins", "del"}:
                if not self._mark_depth:
                    self._parts.append("")
                self._mark_depth += 1
            if tag == "br":
                self.handle_data("\n")
            return
        if tag != "td":
            return
        classes = dict(attrs).get("class", "") or ""
        class_names = set(classes.split())
        if "diff-addedline" in class_names:
            self._action = "add"
        elif "diff-deletedline" in class_names:
            self._action = "delete"
        elif "diff-context" in class_names:
            self._action = ("context-new" if "diff-side-added" in class_names
                            else "context-old" if "diff-side-deleted" in class_names
                            or not self._row else "context-new")
        elif "diff-lineno" in class_names:
            self._action = "lineno"
        if self._action:
            self._buffer = []
            self._cell_depth = 1
            self._mark_depth = 0
            self._parts = []
            self._anchors = [""]

    def handle_data(self, data: str) -> None:
        if self._action:
            self._buffer.append(data)
            if self._mark_depth:
                self._parts[-1] += data
            else:
                self._anchors[-1] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr":
            self._flush_row()
            self._in_row = False
        if self._action and tag in {"ins", "del"} and self._mark_depth:
            self._mark_depth -= 1
            if not self._mark_depth:
                self._anchors.append("")
        if not self._action or tag != "td":
            return
        self._cell_depth -= 1
        if self._cell_depth > 0:
            return
        text = "".join(self._buffer)
        if self._action == "lineno":
            match = re.search(r"\d[\d,]*", text)
            number = int(match.group().replace(',', '')) if match else None
            if self._line_column == 0:
                self._old_line = number
            else:
                self._new_line = number
            self._line_column += 1
        else:
            self._row.append((self._action, text, self._parts, self._anchors))
        if not self._in_row:
            self._flush_row()
        self._action = ""
        self._buffer = []

    def _flush_row(self) -> None:
        if self._row:
            self.rows.append(DiffRow(self._row, self._old_line, self._new_line))
            changed = [cell for cell in self._row if cell[0] in {"add", "delete"}]
            if changed:
                self.blocks.append(changed)
            if any(c[0] in {"delete", "context-old"} for c in self._row) and self._old_line is not None:
                self._old_line += 1
            if any(c[0] in {"add", "context-new"} for c in self._row) and self._new_line is not None:
                self._new_line += 1
            self._row = []

    def close(self) -> None:
        super().close()
        self._flush_row()


def _added_links(old: str, new: str) -> list[DiffLine] | None:
    """Recognize only exact plain-text-to-link conversions, preserving all else."""
    if any(token in old + new for token in ('<', '>', '<!--')):
        return None
    old_pos = new_pos = 0
    result = []
    for match in re.finditer(r'\[\[([^\[\]\n]+)\]\]', new):
        unchanged = new[new_pos:match.start()]
        if not old.startswith(unchanged, old_pos):
            return None
        old_pos += len(unchanged)
        literal = match.group(0)
        if old.startswith(literal, old_pos):
            old_pos += len(literal)
        else:
            parts = match.group(1).split('|')
            if len(parts) > 2:
                return None
            target = parts[0]
            label = parts[-1]
            # Namespaced links may embed files or assign categories.
            if not label or ':' in target or not old.startswith(label, old_pos):
                return None
            old_pos += len(label)
            nearby = ''
            if len(label) <= 2:
                nearby = old[max(0, old_pos - len(label) - 6):old_pos + 6]
                # Do not display half of an adjoining link as natural-language context.
                nearby = re.sub(r'^.*\]\]', '', nearby)
                nearby = re.sub(r'\[\[.*$', '', nearby)
            result.append(DiffLine('link_add', label, target if target != label else '', context=nearby))
        new_pos = match.end()
    if not result or old[old_pos:] != new[new_pos:]:
        return None
    return result


def _compact_pair(line: DiffLine) -> list[DiffLine]:
    if line.action not in {'replace', 'before_after'}:
        return [line]
    left, right = line.text, line.replacement
    if max(len(left), len(right)) <= 120:
        return [line]
    # Bound sequence matching work for exceptionally large revision rows.
    if max(len(left), len(right)) > 2000:
        return [line]
    groups = list(SequenceMatcher(None, left, right, autojunk=False)
                  .get_grouped_opcodes(12))
    if not groups:
        return [line]
    return [replace(line, action='before_after',
        text=('…' if group[0][1] else '') + left[group[0][1]:group[-1][2]]
        + ('…' if group[-1][2] < len(left) else ''),
        replacement=('…' if group[0][3] else '') + right[group[0][3]:group[-1][4]]
        + ('…' if group[-1][4] < len(right) else ''),
    ) for group in groups]


def _diff_block(cells: list[tuple[str, str, list[str], list[str]]], *, structured: bool = True) -> list[DiffLine]:
    old = [cell for cell in cells if cell[0] == "delete"]
    new = [cell for cell in cells if cell[0] == "add"]
    if len(old) == len(new) == 1:
        before, after = old[0], new[0]
        if before[1] == after[1]:
            return [DiffLine("notice", "该行存在差异标记，但文本相同；请查看差异链接")]
        links = _added_links(before[1], after[1]) if structured else None
        if links:
            return links
        # MediaWiki emits no <del> span for a pure insertion. Verify the full
        # unchanged remainder before turning marked tags into countable events.
        if (structured and after[2] and ''.join(after[3]) == before[1]
                and all(re.fullmatch(r'<br\s*/?>', part, re.I) for part in after[2])):
            return [DiffLine('break_add', part) for part in after[2]]
        # Exact pure insertion/deletion: removing every marked span must recover
        # the entire opposite row. Never infer this from cropped previews.
        for changed, other, action in ((after, before, 'local_add'), (before, after, 'local_delete')):
            if (changed[2] and not other[2] and ''.join(changed[3]) == other[1]
                    and all(part.strip() and not any(c in part for c in '[]{}<>|\n')
                            for part in changed[2])):
                return [DiffLine(action, part,
                                context=re.sub(r"'{2,5}", '', changed[3][i])[-12:] + '▸'
                                + re.sub(r"'{2,5}", '', changed[3][i + 1])[:12])
                        for i, part in enumerate(changed[2])]
        # Only pair marked spans when every unchanged anchor agrees.
        if before[2] and len(before[2]) == len(after[2]) and before[3] == after[3]:
            result = []
            for index, (left, right) in enumerate(zip(before[2], after[2])):
                if left == right:
                    continue
                prefix, suffix = before[3][index], before[3][index + 1]
                short_span = (min(len(left.strip()), len(right.strip())) <= 1
                              or left.strip().isnumeric() or right.strip().isnumeric())
                # A literal source tag is escaped in diff HTML. Count insertions,
                # not all existing tags, and do not infer effects inside markup.
                if (structured and not left and re.fullmatch(r"<br\s*/?>", right, re.I)
                        and not re.search(r"<!--|nowiki|<pre|<source|<syntaxhighlight", before[1], re.I)):
                    result.append(DiffLine("break_add", right))
                    continue
                # Keep syntax context for links, templates and punctuation edits.
                if (any(token in before[1] for token in ('[[', '{{', '|'))
                        or not (left.strip() and right.strip())
                        or min(len(left.strip()), len(right.strip())) <= 1
                        or left.strip().isnumeric() or right.strip().isnumeric()
                        or not any(char.isalnum() for char in left + right)):
                    left = prefix[-24:] + left + suffix[:24]
                    right = prefix[-24:] + right + suffix[:24]
                    if re.sub(r'\s', '', left) == re.sub(r'\s', '', right):
                        left = left.replace(' ', '␠').replace('\t', '⇥')
                        right = right.replace(' ', '␠').replace('\t', '⇥')
                if not left:
                    result.append(DiffLine("add", right))
                elif not right:
                    result.append(DiffLine("delete", left))
                else:
                    result.append(DiffLine("replace", left, right, show_position=short_span))
            if result:
                return result
        # An aligned row is safe to show as before/after, not an inferred edit.
        left, right = before[1], after[1]
        if re.sub(r'\s', '', left) == re.sub(r'\s', '', right):
            left = left.replace(' ', '␠').replace('\t', '⇥')
            right = right.replace(' ', '␠').replace('\t', '⇥')
        return [DiffLine("before_after", left, right)]
    return [DiffLine(action, text) for action, text, _parts, _anchors in cells]


def _visible_diff(text: str, max_chars: int) -> str:
    if not text:
        return "（空行）"
    if not text.strip():
        text = text.replace(" ", "␠").replace("\t", "⇥")
    text = text.replace("\r", "␍").replace("\n", "↵")
    return text if len(text) <= max_chars else text[:max_chars - 1] + "…"


def _source_visible(text: str, limit: int) -> str:
    """Prefer a complete link/template boundary when clipping source."""
    if len(text) <= limit:
        return _visible_diff(text, limit)
    stack: list[str] = []
    safe = 0
    index = 0
    while index < min(len(text), limit - 1):
        opener = next((token for token in ('{{{', '{{', '[[') if text.startswith(token, index)), None)
        if opener:
            stack.append({'{{{': '}}}', '{{': '}}', '[[': ']]'}[opener])
            index += len(opener)
        elif stack and text.startswith(stack[-1], index):
            index += len(stack.pop())
        else:
            index += 1
        if not stack and index < limit:
            safe = index
    if stack:
        if safe >= limit // 2:
            return _visible_diff(text[:safe], limit - 1) + '…'
        marker = '…〔源码截断〕'
        return _visible_diff(text[:limit - len(marker)], limit) + marker
    return _visible_diff(text, limit)


def _category_changes(blocks, *, retain_rows=False):
    """Summarize explicit category-only source lines across the entire diff.

    Ambiguous markup falls back unchanged. This does not infer categories
    generated by templates, or final membership from partial revision text.
    """
    pattern = re.compile(r'\[\[(?:Category|分类|分類)\s*:\s*([^\[\]|{}<>\n]+)(?:\|([^\[\]{}<>\n]*))?\]\]', re.I)
    source = '\n'.join(cell[1] for block in blocks for cell in block)
    if re.search(r'<(?!/?noinclude\s*>)[^>]*>|<!--|-->', source, re.I):
        return [], blocks
    sides = {'delete': {}, 'add': {}}
    orders = {'delete': [], 'add': []}
    cleaned = []
    changed = False
    residuals = {'delete': [], 'add': []}
    for block in blocks:
        new_block = []
        for action, text, parts, anchors in block:
            matches = list(pattern.finditer(text))
            rest = pattern.sub('', text)
            plain = re.sub(r'</?noinclude\s*>', '', rest, flags=re.I).strip()
            # A template closing token immediately before noinclude is common.
            safe = not plain or (plain == '}}' and '<noinclude>' in text.lower())
            if matches and safe:
                for match in matches:
                    title = re.sub(r'[ _]+', ' ', match.group(1)).strip()
                    key = title[:1].upper() + title[1:]
                    value = (title, match.group(2))
                    if key in sides[action] and sides[action][key][1] != value[1]:
                        return [], blocks  # duplicate categories with conflicting keys
                    sides[action][key] = value
                    orders[action].append(key)
                changed = True
                residuals[action].append(rest)
                if rest.strip():
                    new_block.append((action, rest, [], [rest]))
            else:
                residuals[action].append(text)
                new_block.append((action, text, parts, anchors))
        if new_block or retain_rows:
            cleaned.append(new_block)
    if not changed:
        return [], blocks
    old, new = sides['delete'], sides['add']
    lines = []
    def names(keys, values):
        labels = [f'｢{_visible_diff(values[key][0], 60)}｣' for key in keys[:3]]
        return '、'.join(labels) + (f'等 {len(keys)} 项' if len(keys) > 3 else '')
    removed = [key for key in old if key not in new]
    added = [key for key in new if key not in old]
    if removed:
        lines.append('✏移出分类' + names(removed, old))
    if added:
        lines.append('✏加入分类' + names(added, new))
    def sortkey(value):
        if value is None:
            return '（使用默认排序键）'
        if value == '':
            return '（空排序键）'
        return '｢' + _visible_diff(value, 40) + '｣'
    for key in (key for key in old if key in new):
        if old[key][1] != new[key][1]:
            lines.append(f'✏分类｢{_visible_diff(new[key][0], 60)}｣排序键：'
                         f'{sortkey(old[key][1])} → {sortkey(new[key][1])}')
    if not lines:
        if orders['delete'] != orders['add']:
            lines.append('✏调整分类排列或重复项（已识别的分类及排序键未变）')
        else:
            lines.append('✏调整分类源码格式（已识别的分类及排序键未变）')
    left, right = ('\n'.join(residuals[side]) for side in ('delete', 'add'))
    if re.sub(r'\s', '', left) == re.sub(r'\s', '', right):
        cleaned = [[] for _ in blocks] if retain_rows else []
        if left != right:
            lines.append('…另有空白或换行调整')
    # Keep removals and additions together under both preview and message limits.
    return [[DiffLine('category', '\n'.join(lines))]], cleaned


def _redundant_category_comment(comment: str, previews: list[DiffLine]) -> bool:
    if len(previews) != 1 or previews[0].action != 'category':
        return False
    match = re.fullmatch(r'(添加|移除|删除)分类[:：]\s*(.+?)(?:——|--|—)\s*HotCat', comment.strip())
    if not match:
        return False
    verb = '加入' if match.group(1) == '添加' else '移出'
    return previews[0].text == f'✏{verb}分类｢{match.group(2).strip()}｣'


def _focus_pair(left: str, right: str, max_chars: int) -> tuple[str, str]:
    """Discard shared distant context before truncating a long aligned pair."""
    if max(len(left), len(right)) <= max_chars or left == right:
        return left, right
    prefix = 0
    while prefix < min(len(left), len(right)) and left[prefix] == right[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < min(len(left), len(right)) - prefix
           and left[len(left) - suffix - 1] == right[len(right) - suffix - 1]):
        suffix += 1
    start = max(0, prefix - 20)
    trim = max(0, suffix - 20)
    return tuple(
        ('…' if start else '') + value[start:len(value) - trim]
        + ('…' if trim else '') for value in (left, right)
    )


def _table_cells(text: str) -> list[str] | None:
    """Split only balanced, top-level || delimiters. Never interpret columns.

    HTML, attributes, multiline cells and unusual syntax deliberately fall back
    to an atomic source preview. Template parameters are not table delimiters.
    """
    if len(text) > 10000 or not text.startswith('|') or text.startswith(('|-', '|}')):
        return None
    if any(c in text for c in ('\n', '<', '>', '"', "'")):
        return None
    stack: list[str] = []
    result: list[str] = []
    start, index = 1, 1
    while index < len(text):
        token = next((x for x in ('{{{', '{{', '[[', '}}}', '}}', ']]')
                      if text.startswith(x, index)), '')
        if token in ('{{{', '{{', '[['):
            stack.append({'{{{': '}}}', '{{': '}}', '[[': ']]'}[token])
            index += len(token)
        elif token:
            # A sequence of four closing braces closes two nested templates.
            if not stack or not text.startswith(stack[-1], index):
                return None
            index += len(stack.pop())
        elif text[index] in '[]':
            return None  # external links and unmatched brackets
        elif not stack and text.startswith('||', index):
            result.append(text[start:index].strip())
            index += 2
            start = index
        elif not stack and text[index] in '|={}':
            return None  # attributes/invalid syntax, not an ordinary cell value
        else:
            index += 1
    if stack:
        return None
    result.append(text[start:].strip())
    return result if len(result) > 1 else None


def _event_blocks(rows: list[DiffRow], *, structured: bool) -> list[list[DiffLine]]:
    blocks: list[list[DiffLine]] = []
    index = 0
    section = ''
    # This is a conservative visible-context guard, not a full Wikitext parser.
    template_depth = {'old': 0, 'new': 0}
    while index < len(rows):
        row = rows[index]
        inside_template = any(template_depth.values())
        for action, text, _parts, _anchors in row.cells:
            if re.match(r'^={1,6}[^=].*?={1,6}\s*$', text):
                section = text
            side = 'old' if action in {'delete', 'context-old'} else 'new'
            for token in re.findall(r'\{\{|\}\}', text):
                template_depth[side] = max(0, template_depth[side] + (1 if token == '{{' else -1))
        cells = [c for c in row.cells if c[0] in {'add', 'delete'}]
        if not cells:
            index += 1
            continue
        positions = ((row.old_line, row.new_line),)
        if structured and not inside_template and len(cells) == 1:
            action, text, _, _ = cells[0]
            heading = re.fullmatch(r'={2,6}\s*(.+?)\s*={2,6}', text)
            if heading:
                path = heading.group(1)
                end = index + 1
                while end < len(rows) and len(rows[end].cells) == 1 and rows[end].cells[0][0] == action:
                    body = rows[end].cells[0][1]
                    if not body.strip():
                        end += 1
                        continue
                    year = re.fullmatch(r"'{2,3}(\d{4}年)'{2,3}", body.strip())
                    if year and end == index + 1:
                        path += ' / ' + year.group(1)
                        end += 1
                        continue
                    if not body.startswith(('=', '|', '!', '{{', '}}')):
                        blocks.append([DiffLine('section_' + action, body, path,
                                                positions=positions, identity=(action, text, body, path))])
                        index = end + 1
                    break
                if index > end:
                    continue
        # Only coalesce contiguous one-sided source rows beginning with a real
        # table separator. Unchanged context and opposite-side edits are barriers.
        if structured and not inside_template and len(cells) == 1 and cells[0][1].strip() == '|-':
            action = cells[0][0]
            source = []
            end = index + 1
            while end < len(rows):
                following = rows[end]
                if len(following.cells) != 1 or following.cells[0][0] != action:
                    break
                text = following.cells[0][1]
                if not text.startswith(('|', '!')) or text.startswith(('|-', '|}')):
                    break
                source.append(text)
                positions += ((following.old_line, following.new_line),)
                end += 1
            if source:
                raw = '\n'.join(['|-', *source])
                if (raw.count('{{') != raw.count('}}') or raw.count('[[') != raw.count(']]')):
                    # A following separator could still be part of an unfinished
                    # template. Fall back without consuming subsequent rows.
                    source = []
            if source:
                values = _table_cells(source[0]) if len(source) == 1 else None
                # No template expansion or inferred labels: expose intact cells.
                preview = '｜'.join(value or '（空）' for value in values) if values else '\n'.join(source)
                blocks.append([DiffLine('table_' + action, preview,
                                        positions=positions, identity=(action, raw))])
                index = end
                continue
        events = []
        for event in _diff_block(cells, structured=structured):
            identity = (event.action, event.text, event.replacement, event.context)
            for fragment in _compact_pair(event):
                # Cropped fragments keep their parent identity as well as their
                # own text, so distinct long edits cannot collapse accidentally.
                events.append(replace(fragment, positions=positions,
                                      identity=(section,) + identity + (fragment.text, fragment.replacement)))
        blocks.append(events)
        index += 1
    return blocks


def _merge_repeated(blocks: list[list[DiffLine]]) -> list[list[DiffLine]]:
    seen: dict[tuple[str, ...], tuple[int, int]] = {}
    result: list[list[DiffLine]] = []
    for block in blocks:
        result.append([])
        for event in block:
            # Notices and table rows must not lose their separate structural role.
            if event.action not in {'add', 'delete', 'replace', 'link_add', 'break_add', 'local_add', 'local_delete'}:
                result[-1].append(event)
                continue
            key = event.identity or (event.action, event.text, event.replacement)
            if key in seen:
                i, j = seen[key]
                old = result[i][j]
                result[i][j] = replace(old, count=old.count + event.count,
                                       positions=old.positions + event.positions)
            else:
                seen[key] = (len(result) - 1, len(result[-1]))
                result[-1].append(event)
    return [block for block in result if block]


def parse_diff_html(
    html: str,
    *,
    max_lines: int = 5,
    max_chars: int = 180,
    content_model: str = "wikitext",
) -> list[DiffLine]:
    parser = _MediaWikiDiffParser()
    parser.feed(html or "")
    parser.close()
    max_lines = max(0, min(int(max_lines), 50))
    max_chars = max(20, min(int(max_chars), 1000))
    if not max_lines:
        return []
    source = '\n'.join(cell[1] for row in parser.rows for cell in row.cells)
    structured = content_model == 'wikitext'
    # Partial compare context may be inside a template or protected region.
    # If visible context suggests that ambiguity, keep literal source previews.
    protected = bool(re.search(r'<!--|-->|</?(?:nowiki|pre|source|syntaxhighlight)\b', source, re.I))
    category_blocks, text_blocks = ([], parser.blocks)
    if structured and not protected and '{{' not in source:
        category_blocks, text_blocks = _category_changes(parser.blocks, retain_rows=True)
    rows = parser.rows
    if category_blocks:
        # Preserve context and emptied category rows as structural barriers.
        # Residual blocks align with changed rows, including empty blocks.
        rows = []
        residuals = iter(text_blocks)
        for original in parser.rows:
            if any(c[0] in {'add', 'delete'} for c in original.cells):
                block = next(residuals)
                context = [c for c in original.cells if c[0].startswith('context-')]
                rows.append(DiffRow(context + block, original.old_line, original.new_line))
            else:
                rows.append(original)
    blocks = _merge_repeated(category_blocks + _event_blocks(rows, structured=structured and not protected))
    has_content = any(line.text.strip() for block in blocks for line in block)
    whitespace = False
    if has_content:
        filtered = []
        for block in blocks:
            kept = []
            for line in block:
                if line.action in {'add', 'delete'} and not line.text.strip():
                    whitespace = True
                else:
                    kept.append(line)
            if kept:
                filtered.append(kept)
        blocks = filtered
    # Standalone headings/year labels remain visible if there is room, but must
    # not displace substantive changes. Paired/structured events stay atomic.
    blocks.sort(key=lambda block: int(all(line.action in {'add', 'delete'} and
                re.fullmatch(r"\s*(?:={2,6}.+={2,6}|'{2,3}\d{4}年'{2,3})\s*", line.text)
                for line in block)))
    # Reserve one preview for each row before spending the budget on details.
    selected: dict[int, list[DiffLine]] = {}
    remaining = max_lines
    for index, block in enumerate(blocks):
        if block and remaining:
            selected[index] = [block[0]]
            remaining -= 1
    for index in selected:
        extra = blocks[index][1:1 + remaining]
        selected[index].extend(extra)
        remaining -= len(extra)
    result = []
    for block in selected.values():
        for line in block:
            if line.action == 'category':
                result.append(line)
                continue
            left, right = line.text, line.replacement
            if line.action in {"replace", "before_after"}:
                pair_chars = min(max_chars, 60)
                left, right = _focus_pair(left, right, pair_chars)
                right = _source_visible(right, pair_chars)
                left = _source_visible(left, pair_chars)
            else:
                left = _source_visible(left, min(max_chars, 60))
                if right:
                    right = _source_visible(right, min(max_chars, 60))
            result.append(replace(line, text=left, replacement=right,
                                  context=_visible_diff(line.context, 40) if line.context else ''))
    omitted = sum(len(block) for block in blocks) - len(result)
    if omitted:
        result.append(DiffLine("notice", f"另有 {omitted} 项差异未展示，详见差异链接"))
    if whitespace:
        result.append(DiffLine('notice', '另有空行调整'))
    if not result:
        result.append(DiffLine("notice", "未获得可展示的文本差异，请查看差异链接"))
    return result


def build_diff_url(api_url: str, change: RecentChange) -> str:
    params: dict[str, Any] = {"diff": change.revid}
    if change.old_revid > 0:
        params["oldid"] = change.old_revid
    return get_index_url(api_url, params)


def redirect_preview(change: RecentChange, targets: dict[int, str]) -> list[DiffLine]:
    """Only summarize states verified from the exact revisions."""
    if change.revid not in targets:
        return []
    new = targets[change.revid]
    if change.change_type == 'new':
        return [DiffLine('redirect', f'↪新建重定向 → ｢{_visible_diff(new, 120)}｣')] if new else []
    if change.old_revid not in targets:
        return []
    old = targets[change.old_revid]
    if new == old:
        return []
    if old and new:
        text = f'↪重定向目标：｢{_visible_diff(old, 100)}｣ → ｢{_visible_diff(new, 100)}｣'
    elif new:
        text = f'↪改为重定向 → ｢{_visible_diff(new, 120)}｣'
    else:
        text = f'↩取消重定向（原目标：｢{_visible_diff(old, 120)}｣）'
    return [DiffLine('redirect', text)]


def _local_clock(timestamp: str) -> str:
    try:
        value = timestamp.replace("Z", "+00:00")
        moment = datetime.fromisoformat(value).astimezone()
        return f"{moment.hour}:{moment.minute:02d}"
    except ValueError:
        return timestamp or "时间未知"


def format_change_notification(
    change: RecentChange,
    scopes: list[str],
    diff_lines: list[DiffLine],
    *,
    api_url: str,
    link_prefix: str = "",
    max_message_chars: int = 700,
) -> str:
    max_message_chars = max(500, min(int(max_message_chars), 5000))
    scope_text = "、".join(
        display_category(item) if item.casefold().startswith("category:") else item
        for item in scopes if not item.startswith("单条目：")
    )
    delta = f"{change.size_delta:+d}"
    flags = ""
    is_new_page = change.change_type == "new"
    is_move = change.change_type == 'move'
    if is_move:
        delta = '移动'
    if is_move:
        link = get_index_url(api_url, {'title': 'Special:Log', 'logid': change.logid}) if change.logid else get_index_url(api_url, {'title': change.move_target})
        detail = ('保留旧标题重定向' if change.suppress_redirect is False else
                  '未保留旧标题重定向' if change.suppress_redirect is True else '重定向保留状态未知')
        diff_lines = [DiffLine('move', f'📦移动至｢{_visible_diff(change.move_target, 140)}｣\n{detail}')]
    elif is_new_page:
        flags += " N"
    if change.minor:
        flags += " m"
    if change.bot:
        flags += " b"
    if is_new_page:
        params = {"curid": change.pageid} if change.pageid > 0 else {"title": change.title}
        link = get_index_url(api_url, params)
        # New pages link to the article itself, never to a comparison or preview.
        diff_lines = [line for line in diff_lines if line.action == 'redirect']
    elif not is_move:
        link = build_diff_url(api_url, change)
    if link_prefix:
        link = f"{link_prefix}{link}"
    comment = change.comment
    section = re.match(r"^\s*/\*\s*(.*?)\s*\*/\s*", comment, re.S)
    title = change.title
    if section:
        title += f" § {section.group(1)[:100]}"
        comment = comment[section.end():]
    lines = [
        _visible_diff(title, 140),
        f"{delta}{flags} | {_visible_diff(change.user, 60)} | {_local_clock(change.timestamp)}",
    ]
    if scope_text:
        lines.append(f"订阅：{_visible_diff(scope_text, 80)}")
    lines.append(link)
    if comment:
        lines.append(f"💬{_visible_diff(comment, 100)}")
    header_count = len(lines)
    last_position = None
    for item in diff_lines:
        if item.action != 'replace':
            last_position = None
        count = f"（{item.count}处）" if item.count > 1 else ""
        if item.action in {'move', 'redirect'}:
            lines.append(item.text)
            continue
        if item.action in {'local_add', 'local_delete'}:
            action = '添加' if item.action == 'local_add' else '删除'
            lines.append(f'✏{action}｢{item.text}｣（附近：{item.context}）{count}')
            continue
        if item.action in {'section_add', 'section_delete'}:
            action = '添加' if item.action == 'section_add' else '删除'
            lines.append(f'✏在｢{item.replacement}｣下{action}｢{item.text}｣')
            continue
        if item.action == 'break_add':
            lines.append(f"✏添加换行标签{count}")
            continue
        if item.action in {'table_add', 'table_delete'}:
            action = '新增' if item.action == 'table_add' else '删除'
            lines.append(f"✏{action}表格行：{item.text}")
            continue
        if item.action == 'category':
            lines.append(item.text)
            continue
        if item.action == "link_add":
            target = f"（目标：{item.replacement}）" if item.replacement else ""
            nearby = f'（附近：{item.context}）' if item.context else ''
            lines.append(f"✏为｢{item.text}｣添加内链{target}{nearby}{count}")
            continue
        if item.action == "notice":
            lines.append(f"…{item.text}")
            continue
        if item.action == "replace":
            position = ''
            if item.show_position and item.count == 1 and item.positions:
                old_line, new_line = item.positions[0]
                if new_line is not None:
                    position = f'第{new_line}行：'
                elif old_line is not None:
                    position = f'原第{old_line}行：'
            if position and position == last_position:
                position = '同一行：'
            elif position:
                last_position = position
            else:
                last_position = None
            lines.append(f"✏{position}把｢{item.text}｣改成｢{item.replacement}｣{count}")
            continue
        if item.action == "before_after":
            lines.append(f"✏修改前｢{item.text}｣\n  修改后｢{item.replacement}｣")
            continue
        action = "添加" if item.action == "add" else "删除"
        lines.append(f"✏{action}｢{item.text}｣{count}")
    message = "\n".join(lines)
    if len(message) <= max_message_chars:
        if _redundant_category_comment(comment, diff_lines):
            lines = [value for value in lines if value != f"💬{_visible_diff(comment, 100)}"]
            message = '\n'.join(lines)
        return message
    footer = ("…已达到消息长度上限，其余内容请查看日志链接" if is_move else
              "…已达到消息长度上限，其余内容请查看条目链接" if is_new_page
              else "…已达到消息长度上限，其余内容请查看差异链接")
    header = lines[:header_count]
    # Preserve the URL and metadata; summaries and subscription labels are optional.
    while len('\n'.join(header)) + len(footer) + 1 > max_message_chars and len(header) > 3:
        optional = next((i for i, value in enumerate(header)
                         if value.startswith(('订阅：', '💬'))), None)
        if optional is None:
            break
        header.pop(optional)
    # Extremely long custom API URLs still obey the configured hard cap.
    base = '\n'.join(header)
    if len(base) + len(footer) + 1 > max_message_chars:
        return base[:max_message_chars - len(footer) - 2] + '…\n' + footer
    accepted = header[:]
    for entry in lines[header_count:]:
        if len('\n'.join([*accepted, entry, footer])) <= max_message_chars:
            accepted.append(entry)
        else:
            break
    return '\n'.join([*accepted, footer])


@dataclass(slots=True)
class ChangeSubscription:
    umo: str
    kind: str
    target: str
    start_timestamp: str
    page_id: int = 0
    member_page_ids: list[int] = field(default_factory=list)
    member_titles: list[str] = field(default_factory=list)
    member_categories: list[str] = field(default_factory=list)
    member_category_depths: dict[str, int] = field(default_factory=dict)
    truncated: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return self.umo, self.kind, self.target.casefold()

    @property
    def label(self) -> str:
        if self.kind == SUBSCRIPTION_PAGE:
            return f"单条目：{self.target}"
        return self.target


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
    recent_rcids: list[int] = field(default_factory=list)
    subscriptions: list[ChangeSubscription] = field(default_factory=list)
    pending: list[PendingNotification] = field(default_factory=list)
    move_redirects: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class PollResult:
    fetched: int = 0
    matched: int = 0
    queued: int = 0
    sent: int = 0
    failed: int = 0
    bootstrapped: bool = False


@dataclass(slots=True, frozen=True)
class MonitorStatus:
    running: bool
    category_subscriptions: int
    page_subscriptions: int
    current_session_subscriptions: int
    pending: int
    last_timestamp: str
    last_poll_at: str
    last_success_at: str
    last_error: str


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
            subscriptions: list[ChangeSubscription] = []
            for item in raw.get("subscriptions", []):
                if not isinstance(item, dict) or not item.get("umo"):
                    continue
                # State v1 only had category subscriptions. Reading the old
                # field here makes upgrades transparent and the next save
                # writes the v2 representation.
                kind = str(item.get("kind") or SUBSCRIPTION_CATEGORY)
                target = str(item.get("target") or item.get("category") or "")
                if kind not in {SUBSCRIPTION_CATEGORY, SUBSCRIPTION_PAGE} or not target:
                    continue
                subscriptions.append(
                    ChangeSubscription(
                        umo=str(item.get("umo", "")),
                        kind=kind,
                        target=target,
                        start_timestamp=str(item.get("start_timestamp", "")),
                        page_id=int(item.get("page_id", 0) or 0),
                        member_page_ids=[
                            int(value) for value in item.get("member_page_ids", [])
                        ],
                        member_titles=[
                            str(value) for value in item.get("member_titles", [])
                        ],
                        member_categories=[
                            str(value) for value in item.get("member_categories", [])
                        ],
                        member_category_depths={
                            str(key): int(value)
                            for key, value in item.get(
                                "member_category_depths", {}
                            ).items()
                        },
                        truncated=bool(item.get("truncated", False)),
                    )
                )
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
                recent_rcids=[
                    int(value)
                    for value in (
                        raw.get("recent_rcids")
                        or raw.get("seen_rcids_at_timestamp", [])
                    )
                ],
                subscriptions=subscriptions,
                pending=pending,
                move_redirects=[item for item in raw.get('move_redirects', [])
                                if isinstance(item, dict) and isinstance(item.get('umos'), list)],
            )
        except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            LOGGER.warning("Unable to read MediaWiki change state %s: %s", self.path, exc)
            return MonitorState()

    async def save(self, state: MonitorState) -> None:
        await asyncio.to_thread(self._save_sync, state)

    def _save_sync(self, state: MonitorState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "last_timestamp": state.last_timestamp,
            "recent_rcids": state.recent_rcids,
            "subscriptions": [asdict(item) for item in state.subscriptions],
            "pending": [asdict(item) for item in state.pending],
            "move_redirects": state.move_redirects,
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
        max_message_chars: int = 700,
        max_pending: int = 500,
        include_bot_edits: bool = True,
        include_moves: bool = True,
        detect_redirects: bool = True,
        overlap_seconds: int = 60,
        recent_rcid_limit: int = 20_000,
        category_refresh_interval_seconds: int = 900,
        new_page_grace_seconds: int = 900,
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
        self.max_message_chars = max(500, min(int(max_message_chars), 5000))
        self.max_pending = max(1, min(int(max_pending), 10_000))
        self.include_bot_edits = include_bot_edits
        self.include_moves = include_moves
        self.detect_redirects = detect_redirects
        self.overlap_seconds = max(0, min(int(overlap_seconds), 600))
        self.recent_rcid_limit = max(1000, min(int(recent_rcid_limit), 100_000))
        self.category_refresh_interval_seconds = max(
            60, min(int(category_refresh_interval_seconds), 86_400)
        )
        self.new_page_grace_seconds = max(
            0, min(int(new_page_grace_seconds), 3600)
        )
        self.link_prefix_for_umo = link_prefix_for_umo or (lambda _umo: "")
        self.state = MonitorState()
        self._loaded = False
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._category_refresh_at: dict[str, float] = {}
        self.last_poll_at = ""
        self.last_success_at = ""
        self.last_error = ""

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

    @staticmethod
    def _validated_umo(umo: str) -> str:
        value = str(umo).strip()
        if not value:
            raise ValueError("当前消息平台没有提供可用于主动推送的会话标识")
        return value

    def _reset_cursor_if_empty(self) -> None:
        if self.state.subscriptions:
            return
        self.state.last_timestamp = ""
        self.state.recent_rcids = []
        self.state.pending = []

    async def subscribe_category(
        self, umo: str, raw_category: str
    ) -> ChangeSubscription:
        await self.load()
        umo = self._validated_umo(umo)
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
                    if item.umo == umo
                    and item.kind == SUBSCRIPTION_CATEGORY
                    and item.target.casefold() == category.casefold()
                ),
                None,
            )
            if existing:
                existing.member_page_ids = sorted(snapshot.page_ids)
                existing.member_titles = sorted(snapshot.titles)
                existing.member_categories = sorted(snapshot.categories)
                existing.member_category_depths = dict(snapshot.category_depths)
                existing.truncated = snapshot.truncated
                await self.store.save(self.state)
                self._category_refresh_at[category.casefold()] = time.monotonic()
                return existing
            subscription = ChangeSubscription(
                umo=umo,
                kind=SUBSCRIPTION_CATEGORY,
                target=category,
                start_timestamp=now,
                member_page_ids=sorted(snapshot.page_ids),
                member_titles=sorted(snapshot.titles),
                member_categories=sorted(snapshot.categories),
                member_category_depths=dict(snapshot.category_depths),
                truncated=snapshot.truncated,
            )
            self.state.subscriptions.append(subscription)
            if not self.state.last_timestamp:
                self.state.last_timestamp = now
                self.state.recent_rcids = []
            await self.store.save(self.state)
            self._category_refresh_at[category.casefold()] = time.monotonic()
            return subscription

    async def subscribe_page(self, umo: str, raw_title: str) -> ChangeSubscription:
        await self.load()
        umo = self._validated_umo(umo)
        page = await self.client.resolve_page(raw_title)
        now = await self.client.server_timestamp()
        page_id = int(page.pageid or 0)
        async with self._lock:
            existing = next(
                (
                    item
                    for item in self.state.subscriptions
                    if item.umo == umo
                    and item.kind == SUBSCRIPTION_PAGE
                    and (
                        (page_id > 0 and item.page_id == page_id)
                        or item.target.casefold() == page.title.casefold()
                    )
                ),
                None,
            )
            if existing:
                existing.target = page.title
                existing.page_id = page_id
                await self.store.save(self.state)
                return existing
            subscription = ChangeSubscription(
                umo=umo,
                kind=SUBSCRIPTION_PAGE,
                target=page.title,
                page_id=page_id,
                start_timestamp=now,
            )
            self.state.subscriptions.append(subscription)
            if not self.state.last_timestamp:
                self.state.last_timestamp = now
                self.state.recent_rcids = []
            await self.store.save(self.state)
            return subscription

    async def subscribe(self, umo: str, raw_category: str) -> ChangeSubscription:
        """Backward-compatible alias for category subscriptions."""
        return await self.subscribe_category(umo, raw_category)

    async def unsubscribe_category(self, umo: str, raw_category: str = "") -> int:
        await self.load()
        category = normalize_category(raw_category) if raw_category.strip() else ""
        async with self._lock:
            before = len(self.state.subscriptions)
            self.state.subscriptions = [
                item
                for item in self.state.subscriptions
                if not (
                    item.umo == umo
                    and item.kind == SUBSCRIPTION_CATEGORY
                    and (
                        not category
                        or item.target.casefold() == category.casefold()
                    )
                )
            ]
            removed = before - len(self.state.subscriptions)
            self._reset_cursor_if_empty()
            await self.store.save(self.state)
            return removed

    async def unsubscribe_page(self, umo: str, raw_title: str = "") -> int:
        await self.load()
        title = normalize_title(raw_title) if raw_title.strip() else ""
        async with self._lock:
            before = len(self.state.subscriptions)
            self.state.subscriptions = [
                item
                for item in self.state.subscriptions
                if not (
                    item.umo == umo
                    and item.kind == SUBSCRIPTION_PAGE
                    and (not title or item.target.casefold() == title.casefold())
                )
            ]
            removed = before - len(self.state.subscriptions)
            self._reset_cursor_if_empty()
            await self.store.save(self.state)
            return removed

    async def unsubscribe_all(self, umo: str) -> int:
        await self.load()
        async with self._lock:
            before = len(self.state.subscriptions)
            self.state.subscriptions = [
                item for item in self.state.subscriptions if item.umo != umo
            ]
            removed = before - len(self.state.subscriptions)
            self._reset_cursor_if_empty()
            await self.store.save(self.state)
            return removed

    async def unsubscribe(self, umo: str, raw_category: str = "") -> int:
        """Backward-compatible alias for category unsubscription."""
        return await self.unsubscribe_category(umo, raw_category)

    async def subscriptions_for(self, umo: str) -> list[ChangeSubscription]:
        await self.load()
        return [item for item in self.state.subscriptions if item.umo == umo]

    async def poll_once(self) -> PollResult:
        self.last_poll_at = _utc_now()
        try:
            result = await self._poll_once()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:500]
            raise
        self.last_success_at = _utc_now()
        self.last_error = ""
        return result

    async def _poll_once(self) -> PollResult:
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
                self.state.recent_rcids = []
                result.bootstrapped = True
                await self.store.save(self.state)
                return result

            seen = set(self.state.recent_rcids)
            changes = await self.client.recent_changes(
                overlap_timestamp(self.state.last_timestamp, self.overlap_seconds),
                limit=self.max_changes_per_poll,
                include_bot_edits=self.include_bot_edits,
                exclude_rcids=seen,
                include_moves=self.include_moves,
            )
            changes = [item for item in changes if item.rcid not in seen
                       and (self.include_moves or item.change_type != 'move')]
            # A move's revid belongs to the moved page, NOT the new redirect.
            # Only reorder simultaneous source-title creations by the same user.
            for move in [item for item in changes if item.change_type == 'move']:
                first = next((i for i, item in enumerate(changes)
                              if item.change_type == 'new' and item.title == move.title
                              and item.timestamp == move.timestamp and item.user == move.user), None)
                if first is not None and changes.index(move) > first:
                    changes.remove(move)
                    changes.insert(first, move)
            await self._learn_new_category_memberships(changes)
            result.fetched = len(changes)
            pending_keys = {item.key for item in self.state.pending}
            models: dict[int, str] = {}
            model_ids = list(dict.fromkeys(
                revid for change in changes
                if self.max_diff_lines and change.change_type == 'edit'
                and any((umo, change.rcid) not in pending_keys
                        for umo in self._matching_targets(change, previous_members, update_titles=False))
                for revid in (change.old_revid, change.revid) if revid > 0
            ))
            if model_ids:
                try:
                    models = await self.client.revision_content_models(model_ids)
                except Exception:
                    LOGGER.warning("Revision content models unavailable; using literal diff previews", exc_info=True)
            redirect_targets: dict[int, str] = {}
            if self.detect_redirects:
                redirect_ids = list(dict.fromkeys(
                    revid for change in changes if change.change_type in {'edit', 'new'}
                    and any((umo, change.rcid) not in pending_keys
                            for umo in self._matching_targets(change, previous_members, update_titles=False))
                    for revid in (change.old_revid, change.revid) if revid > 0))
                if redirect_ids:
                    try:
                        redirect_targets = await self.client.revision_redirects(redirect_ids)
                    except Exception:
                        LOGGER.warning('Historical redirect states unavailable; retaining ordinary notifications')
            consumed: list[RecentChange] = []
            deferred_timestamps: list[str] = []

            for change in changes:
                targets = self._matching_targets(change, previous_members)
                if not targets and self._should_defer_new_page(change):
                    if change.timestamp:
                        deferred_timestamps.append(change.timestamp)
                    continue
                new_targets = [
                    (umo, scopes)
                    for umo, scopes in targets.items()
                    if (umo, change.rcid) not in pending_keys
                ]
                if change.change_type == 'new' and redirect_targets.get(change.revid):
                    covered = {umo for move in self.state.move_redirects
                               if move.get('title') == change.title
                               and move.get('timestamp') == change.timestamp and move.get('user') == change.user
                               and bool(change.timestamp and change.user)
                               and change.pageid > 0 and move.get('pageid', 0) > 0
                               and change.pageid != move['pageid']
                               and move.get('target') == redirect_targets[change.revid]
                               for umo in move['umos']}
                    new_targets = [(umo, scopes) for umo, scopes in new_targets if umo not in covered]
                if len(self.state.pending) + len(new_targets) > self.max_pending:
                    LOGGER.warning(
                        "MediaWiki pending queue is full (%s); cursor retained for retry",
                        self.max_pending,
                    )
                    break
                result.matched += len(new_targets)
                diff_lines: list[DiffLine] = []
                if new_targets and self.max_diff_lines and change.change_type == 'edit':
                    try:
                        diff_html = await self.client.compare_revisions(
                            change.old_revid, change.revid
                        )
                        diff_lines = await asyncio.to_thread(
                            parse_diff_html,
                            diff_html,
                            max_lines=self.max_diff_lines,
                            max_chars=self.max_diff_line_chars,
                            content_model=('wikitext' if models.get(change.revid) == 'wikitext'
                                           and models.get(change.old_revid) == 'wikitext' else 'unknown'),
                        )
                    except Exception:
                        diff_lines = [DiffLine("notice", "差异预览获取失败，请查看差异链接")]
                        LOGGER.exception(
                            "Unable to fetch MediaWiki diff for revision %s", change.revid
                        )
                if new_targets and self.detect_redirects and change.change_type in {'new', 'edit'}:
                    diff_lines = redirect_preview(change, redirect_targets) + diff_lines
                for umo, scopes in new_targets:
                    message = format_change_notification(
                        change,
                        scopes,
                        diff_lines,
                        api_url=self.client.api_url,
                        link_prefix=self.link_prefix_for_umo(umo),
                        max_message_chars=self.max_message_chars,
                    )
                    pending = PendingNotification(umo, change.rcid, message)
                    self.state.pending.append(pending)
                    pending_keys.add(pending.key)
                    result.queued += 1
                if change.change_type == 'move' and change.suppress_redirect is False and new_targets:
                    self.state.move_redirects.append(dict(pageid=change.pageid, title=change.title,
                                                          target=change.move_target, timestamp=change.timestamp,
                                                          user=change.user, umos=[umo for umo, _ in new_targets]))
                    self.state.move_redirects = self.state.move_redirects[-self.recent_rcid_limit:]
                consumed.append(change)

            if consumed or deferred_timestamps:
                timestamps = [item.timestamp for item in consumed if item.timestamp]
                next_timestamp = max([self.state.last_timestamp, *timestamps])
                if deferred_timestamps:
                    next_timestamp = min(next_timestamp, min(deferred_timestamps))
                self.state.last_timestamp = next_timestamp
                deduplicated = list(
                    dict.fromkeys(
                        [*self.state.recent_rcids, *(item.rcid for item in consumed)]
                    )
                )
                self.state.recent_rcids = deduplicated[-self.recent_rcid_limit :]

            await self.store.save(self.state)
            sent, failed = await self._deliver_pending()
            result.sent += sent
            result.failed += failed
            return result

    def _should_defer_new_page(self, change: RecentChange) -> bool:
        if change.change_type != "new" or not self.new_page_grace_seconds:
            return False
        eligible = any(
            item.kind == SUBSCRIPTION_CATEGORY
            and (
                not item.start_timestamp
                or not change.timestamp
                or change.timestamp >= item.start_timestamp
            )
            for item in self.state.subscriptions
        )
        if not eligible or not change.timestamp:
            return False
        try:
            moment = datetime.fromisoformat(change.timestamp.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - moment.astimezone(timezone.utc)).total_seconds()
            return age < self.new_page_grace_seconds
        except ValueError:
            return False

    async def _learn_new_category_memberships(
        self, changes: list[RecentChange]
    ) -> None:
        subscriptions = [
            item
            for item in self.state.subscriptions
            if item.kind == SUBSCRIPTION_CATEGORY
        ]
        new_changes = [
            item for item in changes if item.change_type == "new" and item.pageid > 0
        ]
        if not subscriptions or not new_changes:
            return

        categories_by_page = await self.client.page_categories(
            {item.pageid for item in new_changes}
        )
        for subscription in subscriptions:
            watched_depths = {
                str(key).casefold(): int(value)
                for key, value in subscription.member_category_depths.items()
            }
            for item in subscription.member_categories:
                watched_depths.setdefault(item.casefold(), self.category_depth)
            watched_depths[subscription.target.casefold()] = 0

            # Discover a newly-created subcategory before checking new pages
            # from the same RecentChanges batch, regardless of event ordering.
            changed = True
            while changed:
                changed = False
                for change in new_changes:
                    if change.namespace != 14 or not change.title:
                        continue
                    direct = {
                        value.casefold()
                        for value in categories_by_page.get(change.pageid, set())
                    }
                    parents = direct & set(watched_depths)
                    if not parents:
                        continue
                    category_key = change.title.casefold()
                    depth = min(watched_depths[parent] + 1 for parent in parents)
                    if depth <= self.category_depth and (
                        category_key not in watched_depths
                        or depth < watched_depths[category_key]
                    ):
                        watched_depths[category_key] = depth
                        changed = True

            for change in new_changes:
                direct = {
                    value.casefold()
                    for value in categories_by_page.get(change.pageid, set())
                }
                if not direct & set(watched_depths):
                    continue
                if change.pageid not in subscription.member_page_ids:
                    subscription.member_page_ids.append(change.pageid)
                    subscription.member_page_ids.sort()
                    if len(subscription.member_page_ids) > self.max_members:
                        subscription.truncated = True
                title_key = change.title.casefold()
                if title_key and title_key not in subscription.member_titles:
                    subscription.member_titles.append(title_key)
                    subscription.member_titles.sort()
            subscription.member_categories = sorted(watched_depths)
            subscription.member_category_depths = dict(sorted(watched_depths.items()))

    async def _refresh_memberships(self) -> None:
        grouped: dict[str, list[ChangeSubscription]] = {}
        for subscription in self.state.subscriptions:
            if subscription.kind != SUBSCRIPTION_CATEGORY:
                continue
            grouped.setdefault(subscription.target.casefold(), []).append(subscription)

        now = time.monotonic()
        for key, subscriptions in grouped.items():
            refreshed_at = self._category_refresh_at.get(key, 0.0)
            if now - refreshed_at < self.category_refresh_interval_seconds:
                continue
            category = subscriptions[0].target
            try:
                snapshot: CategorySnapshot = await self.client.category_tree(
                    category,
                    max_depth=self.category_depth,
                    max_members=self.max_members,
                )
                for subscription in subscriptions:
                    subscription.member_page_ids = sorted(snapshot.page_ids)
                    subscription.member_titles = sorted(snapshot.titles)
                    subscription.member_categories = sorted(snapshot.categories)
                    subscription.member_category_depths = dict(
                        snapshot.category_depths
                    )
                    subscription.truncated = snapshot.truncated
                self._category_refresh_at[key] = now
            except Exception:
                LOGGER.exception(
                    "Unable to refresh MediaWiki category %s", category
                )

    def _matching_targets(
        self,
        change: RecentChange,
        previous_members: dict[
            tuple[str, str, str], tuple[set[int], set[str]]
        ],
        *,
        update_titles: bool = True,
    ) -> dict[str, list[str]]:
        targets: dict[str, list[str]] = {}
        for subscription in self.state.subscriptions:
            if subscription.kind == SUBSCRIPTION_PAGE:
                matches = (
                    change.pageid > 0
                    and subscription.page_id > 0
                    and change.pageid == subscription.page_id
                ) or ((change.pageid <= 0 or subscription.page_id <= 0)
                      and subscription.target.casefold() in {change.title.casefold(), change.move_target.casefold()})
                if update_titles and matches and (change.pageid == subscription.page_id or change.change_type == 'move') and change.title:
                    subscription.target = change.move_target if change.change_type == 'move' else change.title
            else:
                previous_ids, previous_titles = previous_members.get(
                    subscription.key, (set(), set())
                )
                current_ids = set(subscription.member_page_ids)
                current_titles = set(subscription.member_titles)
                matches = (
                    change.pageid > 0
                    and change.pageid in (previous_ids | current_ids)
                ) or bool({change.title.casefold(), change.move_target.casefold()} & (previous_titles | current_titles))
            if not matches or (
                subscription.start_timestamp
                and change.timestamp < subscription.start_timestamp
            ):
                continue
            scopes = targets.setdefault(subscription.umo, [])
            if subscription.label not in scopes:
                scopes.append(subscription.label)
        return targets

    async def status_for(self, umo: str) -> MonitorStatus:
        await self.load()
        current = [item for item in self.state.subscriptions if item.umo == umo]
        return MonitorStatus(
            running=bool(self._task and not self._task.done()),
            category_subscriptions=sum(
                item.kind == SUBSCRIPTION_CATEGORY for item in self.state.subscriptions
            ),
            page_subscriptions=sum(
                item.kind == SUBSCRIPTION_PAGE for item in self.state.subscriptions
            ),
            current_session_subscriptions=len(current),
            pending=len(self.state.pending),
            last_timestamp=self.state.last_timestamp,
            last_poll_at=self.last_poll_at,
            last_success_at=self.last_success_at,
            last_error=self.last_error,
        )

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
