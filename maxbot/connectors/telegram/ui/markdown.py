"""Markdown → Telegram MarkdownV2 conversion with table rendering and codeblocks."""

from __future__ import annotations

import re


def escape_mdv2(text: str) -> str:
    return re.sub(r'([_\[\]()~>#+\-=|{}.!\\])', r'\\\1', text)


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith('|') and stripped.endswith('|') and stripped.count('|') >= 2


def _is_separator_line(line: str) -> bool:
    stripped = line.strip()
    return bool(re.match(r'^\|[\s\-:|]+\|$', stripped))


def _render_table(table_lines: list[str]) -> str:
    rows = []
    for line in table_lines:
        if _is_separator_line(line):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        rows.append(cells)
    if not rows:
        return ''
    num_cols = max(len(r) for r in rows)
    col_widths = [0] * num_cols
    for row in rows:
        for i, cell in enumerate(row):
            if i < num_cols:
                col_widths[i] = max(col_widths[i], len(cell))
    out = []
    for idx, row in enumerate(rows):
        parts = []
        for i in range(num_cols):
            cell = row[i] if i < len(row) else ''
            parts.append(cell.ljust(col_widths[i]))
        out.append('  '.join(parts))
        if idx == 0:
            out.append('  '.join(['─' * w for w in col_widths]))
    content = '\n'.join(out)
    content = content.replace('\\', '\\\\').replace('`', '\\`')
    return f'```\n{content}\n```'


def md_to_telegram(text: str) -> str:
    """Convert generic Markdown to Telegram-MarkdownV2 with safe escaping."""
    lines = text.split('\n')
    result = []
    in_code_block = False
    code_block_lines: list[str] = []
    code_lang = ""
    table_lines: list[str] = []

    for line in lines:
        if line.strip().startswith('```') and not in_code_block and not _is_table_line(line):
            if table_lines:
                result.append(_render_table(table_lines))
                table_lines = []
            in_code_block = True
            code_lang = line.strip()[3:].strip()
            code_block_lines = []
            continue
        if line.strip().startswith('```') and in_code_block:
            in_code_block = False
            code_content = '\n'.join(code_block_lines)
            code_content = code_content.replace('\\', '\\\\').replace('`', '\\`')
            result.append(f'```{code_lang}\n{code_content}\n```')
            continue
        if in_code_block:
            code_block_lines.append(line)
            continue
        if _is_table_line(line):
            table_lines.append(line)
            continue
        if table_lines:
            result.append(_render_table(table_lines))
            table_lines = []
        result.append(_process_inline(line))

    if in_code_block:
        code_content = '\n'.join(code_block_lines)
        code_content = code_content.replace('\\', '\\\\').replace('`', '\\`')
        result.append(f'```{code_lang}\n{code_content}\n```')
    if table_lines:
        result.append(_render_table(table_lines))
    return '\n'.join(result)


def _process_inline(line: str) -> str:
    header_match = re.match(r'^(#{1,6})\s+(.*)', line)
    if header_match:
        content = _process_inline(header_match.group(2))
        return f'*{content}*'
    parts: list[str] = []
    i = 0
    while i < len(line):
        if line[i] == '`':
            end = line.find('`', i + 1)
            if end != -1:
                code = line[i+1:end].replace('\\', '\\\\').replace('`', '\\`')
                parts.append(f'`{code}`')
                i = end + 1
                continue
        if line[i:i+2] == '**':
            end = line.find('**', i + 2)
            if end != -1:
                inner = escape_mdv2(line[i+2:end])
                parts.append(f'*{inner}*')
                i = end + 2
                continue
        link_match = re.match(r'\[([^\]]+)\]\(([^)]+)\)', line[i:])
        if link_match:
            link_text = escape_mdv2(link_match.group(1))
            link_url = link_match.group(2).replace(')', '\\)')
            parts.append(f'[{link_text}]({link_url})')
            i += link_match.end()
            continue
        if i == 0 and re.match(r'^(\s*[-*])\s', line):
            bullet_match = re.match(r'^(\s*)[-*]\s(.*)', line)
            if bullet_match:
                indent = bullet_match.group(1)
                content = _process_inline(bullet_match.group(2))
                parts.append(f'{escape_mdv2(indent)}• {content}')
                i = len(line)
                continue
        if line[i] in r'_[]()~>#+\-=|{}.!\\':
            parts.append(f'\\{line[i]}')
        else:
            parts.append(line[i])
        i += 1
    return ''.join(parts)


def split_at_safe_point(text: str, max_len: int) -> int:
    """Find a good break point (paragraph / sentence / word) near max_len."""
    if len(text) <= max_len:
        return len(text)
    window_start = max(0, max_len - max_len // 4)
    for sep in ["\n\n", "\n", ". ", " "]:
        idx = text.rfind(sep, window_start, max_len)
        if idx > 0:
            return idx + len(sep)
    return max_len


def smart_split(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    for sep in ["\n", ". ", " "]:
        idx = text.rfind(sep, 0, max_len)
        if idx > max_len // 4:
            return text[:idx + len(sep)]
    return text[:max_len]
