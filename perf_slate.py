"""JoySpace Slate block-tree helpers for the offline admission report.

The report is no longer built as Markdown and converted remotely.  The
template page is read as a Slate block tree, measured values are written
straight into that tree, and the very same tree is uploaded and rendered
locally.  The published page and the local preview therefore share the
template's own layout.

Only the template's own block vocabulary is accepted.  An image, a file, an
embed or any unknown block is refused rather than republished, which is what
keeps the "no images" guarantee of this feature.
"""
from __future__ import annotations

import html
import re

HEADER_BG = "#F86560"
NOTE_COLOR = "#F5222D"
HIGH_COLOR = NOTE_COLOR
LOW_COLOR = "#389E0D"
ALT_BG = "rgb(242, 245, 250)"

TYPES = {"p", "list", "table", "table-row", "table-cell"}
LEAF_KEYS = {"text", "bold", "italic", "underline", "strike", "code",
             "fontSize", "fontColor", "bgColor"}
BLOCK_KEYS = {
    "p": {"type", "id", "children", "textIndent", "indent", "align"},
    "list": {"type", "id", "children", "value", "orderedType", "header",
             "textIndent", "indent"},
    "table": {"type", "id", "children", "width", "height"},
    "table-row": {"type", "id", "children", "height"},
    "table-cell": {"type", "id", "children", "rowSpan", "colSpan", "hidden",
                   "bgColor", "align"},
}
CHILD_TYPES = {
    "table": {"table-row"},
    "table-row": {"table-cell"},
    "table-cell": {"p"},
}
LIST_STYLES = {"ordered", "unordered"}

# Section headings the writer needs to locate, and the metric columns of the
# per-process table.
H_TOTAL = "总资源及各个进程占用"
H_OVERALL = "总资源占用"
H_PROCESS = "各个进程占用"
H_CONCLUSION = "准入结论"
METRICS = ["P95%", "P95 K", "峰值%", "峰值K", "内存峰MB", "IO读MB/S", "IO写MB/S"]
GROUP_BACKGROUND = "后台常驻服务"
GROUP_FOREGROUND = "前台top-app"
NAME_COLUMN = 2
FG_NEED_COLUMN = 3
BG_NEED_COLUMN = 4
BACKGROUND_OFFSET = 5
FOREGROUND_OFFSET = 12


class SlateError(ValueError):
    """Carries a message that is safe to show to the user."""


def label(text):
    """Normalise a header/label for comparison (drops spacing and brackets)."""
    text = re.sub(r"<br\s*/?>", "", str(text), flags=re.I)
    return re.sub(r"[\s*`（）()]+", "", text).upper()


def text_of(node):
    if not isinstance(node, dict):
        return ""
    return node.get("text", "") + "".join(text_of(c) for c in node.get("children", []))


def rows_of(table):
    """The table's row blocks."""
    return [r for r in table.get("children", []) if isinstance(r, dict)]


def cells_of(row):
    """The row's cell blocks."""
    return [c for c in row.get("children", []) if isinstance(c, dict)]


def texts_of(row):
    return [text_of(c) for c in cells_of(row)]


def is_blank_row(row):
    return all(not text.strip() for text in texts_of(row))


def _check(node, path):
    if not isinstance(node, dict):
        raise SlateError(f"模板块结构无效（{path}）。")
    keys = set(node)
    kind = node.get("type")
    if kind is None:
        if not keys <= LEAF_KEYS or not isinstance(node.get("text"), str):
            raise SlateError(f"模板文本节点字段不受支持（{path}）。")
        return
    if kind not in TYPES:
        raise SlateError(f"模板包含不支持的块类型 {kind}（{path}）。")
    if not keys <= BLOCK_KEYS[kind]:
        unknown = sorted(keys - BLOCK_KEYS[kind])
        raise SlateError(f"模板块 {kind} 含有不支持的字段 {unknown}（{path}）。")
    if "id" in node and not isinstance(node["id"], str):
        raise SlateError(f"模板块 id 无效（{path}）。")
    if kind == "list":
        if node.get("value") not in LIST_STYLES:
            raise SlateError(f"模板列表样式无效（{path}）。")
        if node.get("header") not in (1, 2, None):
            raise SlateError(f"模板列表标题级别无效（{path}）。")
    if kind == "table":
        width = node.get("width")
        if not isinstance(width, list) or not width \
                or not all(type(v) is int and 0 < v <= 4000 for v in width):
            raise SlateError(f"模板表格列宽无效（{path}）。")
    if kind == "table-cell":
        for span in ("rowSpan", "colSpan"):
            value = node.get(span)
            if span in node and (type(value) is not int or not 1 <= value <= 1000):
                raise SlateError(f"模板单元格合并值无效（{path}）。")
        if "hidden" in node and not isinstance(node["hidden"], bool):
            raise SlateError(f"模板单元格隐藏标记无效（{path}）。")
    children = node.get("children")
    if not isinstance(children, list) or not children:
        raise SlateError(f"模板块缺少子节点（{path}）。")
    allowed = CHILD_TYPES.get(kind)
    for index, child in enumerate(children):
        if allowed is not None and (not isinstance(child, dict) or child.get("type") not in allowed):
            raise SlateError(f"模板块 {kind} 的子节点类型不符（{path}.{index}）。")
        _check(child, f"{path}.{index}")


def validate_tree(blocks):
    """Reject any block the template never produces; guarantees image freedom."""
    if not isinstance(blocks, list) or not blocks:
        raise SlateError("模板内容为空或格式无效。")
    for index, block in enumerate(blocks):
        _check(block, f"blocks[{index}]")
    return blocks


def _headings(blocks):
    found = {}
    for index, block in enumerate(blocks):
        if block.get("type") != "list" or block.get("header") not in (1, 2):
            continue
        key = (block["header"], label(text_of(block)))
        found.setdefault(key, []).append(index)
    return found


def _heading_index(found, level, title):
    hits = found.get((level, label(title)), [])
    if len(hits) != 1:
        raise SlateError(f"模板缺少唯一的「{title}」标题（匹配 {len(hits)} 处）。")
    return hits[0]


def _only_table(blocks, start, stop, title):
    hits = [i for i in range(start, stop) if blocks[i].get("type") == "table"]
    if len(hits) != 1:
        raise SlateError(f"「{title}」小节下应恰有 1 个表格，实际 {len(hits)} 个。")
    return hits[0]


def set_cell_text(cell, text):
    """Replace a cell's text, keeping the cell's and paragraph's formatting."""
    if not isinstance(cell, dict) or cell.get("type") != "table-cell":
        raise SlateError("单元格不可写入。")
    if cell.get("hidden") is True:
        raise SlateError("被合并的单元格不可写入。")
    blocks = cell.get("children", [])
    if len(blocks) != 1 or blocks[0].get("type") != "p":
        raise SlateError("单元格段落结构不符，无法写入。")
    blocks[0]["children"] = [{"text": "" if text is None else str(text)}]


def write_row(row, values, offset=0, colors=None):
    """Write values with optional per-cell font colors; None values skip cells."""
    cells = cells_of(row)
    for index, value in enumerate(values):
        if value is None:
            continue
        column = offset + index
        if column >= len(cells):
            raise SlateError("表格列数不足，无法写入指标。")
        set_cell_text(cells[column], value)
        if colors is not None and colors[index] is not None:
            cells[column]["children"][0]["children"][0]["fontColor"] = colors[index]


def note_blocks(warnings):
    """A bold red 注 paragraph plus one numbered paragraph per warning."""
    if not warnings:
        return []
    blocks = [{"type": "p", "children": [
        {"fontColor": NOTE_COLOR, "bold": True, "text": "注：统计口径与覆盖说明"}]}]
    for index, note in enumerate(warnings, 1):
        blocks.append({"type": "p", "children": [{"text": f"{index}. {note}"}]})
    return blocks


class Layout:
    """Where the writer may put values, resolved from the template itself."""

    def __init__(self, blocks, overall, process, conclusion, names, data_rows):
        self.blocks = blocks
        self.overall = overall
        self.process = process
        self.conclusion = conclusion
        self.names = names
        self.data_rows = data_rows

    def process_row(self, name):
        index = self.names.get(name)
        return None if index is None else rows_of(self.process)[index]


def _check_overall(table):
    rows = rows_of(table)
    if len(rows) != 3 or any(len(cells_of(r)) != 3 for r in rows):
        raise SlateError("「总资源占用」表应为 3 行 3 列。")
    if [label(t) for t in texts_of(rows[0])] != [label(t) for t in ("指标", "P95", "P99")]:
        raise SlateError("「总资源占用」表头应为 指标 / P95 / P99。")
    for row, expected in zip(rows[1:], ("CPU(%)", "内存(GB)")):
        if label(texts_of(row)[0]) != label(expected):
            raise SlateError(f"「总资源占用」表缺少 {expected} 行。")


def _check_process(table):
    rows = rows_of(table)
    if len(rows) < 3:
        raise SlateError("「各个进程占用」表行数不足。")
    columns = FOREGROUND_OFFSET + len(METRICS)
    for index, row in enumerate(rows):
        if len(cells_of(row)) != columns:
            raise SlateError(f"「各个进程占用」表第 {index + 1} 行应为 {columns} 列。")
    head, metrics = rows[0], rows[1]
    groups = texts_of(head)
    for offset, title in ((BACKGROUND_OFFSET, GROUP_BACKGROUND),
                          (FOREGROUND_OFFSET, GROUP_FOREGROUND)):
        if label(groups[offset]) != label(title):
            raise SlateError(f"「各个进程占用」表缺少分组表头「{title}」。")
        if cells_of(head)[offset].get("colSpan") != len(METRICS):
            raise SlateError(f"分组表头「{title}」应横跨 {len(METRICS)} 列。")
        actual = [label(t) for t in texts_of(metrics)[offset:offset + len(METRICS)]]
        if actual != [label(m) for m in METRICS]:
            raise SlateError(f"「{title}」下的指标列与预期不一致。")


def validate_template(blocks, required_names=()):
    """Confirm the template still has the sections and columns we write into."""
    validate_tree(blocks)
    found = _headings(blocks)
    total = _heading_index(found, 1, H_TOTAL)
    overall = _heading_index(found, 2, H_OVERALL)
    process = _heading_index(found, 2, H_PROCESS)
    conclusion = _heading_index(found, 1, H_CONCLUSION)
    if not total < overall < process < conclusion:
        raise SlateError("模板小节顺序与预期不一致，无法定位写入位置。")
    overall_table = blocks[_only_table(blocks, overall, process, H_OVERALL)]
    process_table = blocks[_only_table(blocks, process, conclusion, H_PROCESS)]
    conclusion_table = blocks[_only_table(blocks, conclusion, len(blocks), H_CONCLUSION)]
    _check_overall(overall_table)
    _check_process(process_table)

    names, data_rows = {}, []
    for index, row in enumerate(rows_of(process_table)):
        if index < 2 or is_blank_row(row):
            continue
        name = texts_of(row)[NAME_COLUMN].strip()
        if not name:
            continue
        if name in names:
            raise SlateError(f"模板进程「{name}」重复出现，无法确定写入行。")
        names[name] = index
        data_rows.append(index)
    if not names:
        raise SlateError("「各个进程占用」表未找到任何进程行。")
    missing = [n for n in required_names if n not in names]
    if missing:
        raise SlateError("模板缺少进程行：" + "、".join(missing[:5]))
    return Layout(blocks, overall_table, process_table, conclusion_table,
                  names, data_rows)


def _inline(leaves):
    parts = []
    for leaf in leaves if isinstance(leaves, list) else []:
        if not isinstance(leaf, dict):
            continue
        text = leaf.get("text", "")
        if not text:
            continue
        chunk = html.escape(text, quote=True).replace("\n", "<br>")
        color = leaf.get("fontColor")
        if isinstance(color, str):
            lowered = color.lower()
            if lowered == NOTE_COLOR.lower():
                chunk = f'<span class="note">{chunk}</span>'
            elif lowered == LOW_COLOR.lower():
                chunk = f'<span class="budget-low">{chunk}</span>'
            elif lowered in ("gray", "grey"):
                chunk = f'<span class="muted">{chunk}</span>'
        if leaf.get("bold"):
            chunk = f"<strong>{chunk}</strong>"
        parts.append(chunk)
    return "".join(parts) or "&nbsp;"


def _cell_html(cell):
    if cell.get("hidden") is True:
        return ""
    background = cell.get("bgColor")
    classes = []
    if isinstance(background, str):
        if background.upper() == HEADER_BG:
            classes.append("head")
        else:
            classes.append("alt")
    blocks = cell.get("children", [])
    if blocks and blocks[0].get("align") == "center":
        classes.append("c")
    attrs = ""
    for key, name in (("colSpan", "colspan"), ("rowSpan", "rowspan")):
        span = cell.get(key)
        if type(span) is int and 1 < span <= 1000:
            attrs += f' {name}="{span}"'
    if classes:
        attrs += ' class="' + " ".join(classes) + '"'
    tag = "th" if "head" in classes else "td"
    body = "".join(_inline(b.get("children", [])) for b in blocks)
    return f"<{tag}{attrs}>{body}</{tag}>"


def _table_html(table):
    rows = rows_of(table)
    parts = ['<div class="scroll"><table class="tbl">']
    width = table.get("width")
    if isinstance(width, list):
        parts.append("<colgroup>"
                     + "".join(f'<col style="width:{w}px">' for w in width)
                     + "</colgroup>")
    for row in rows:
        parts.append("<tr>" + "".join(_cell_html(c) for c in cells_of(row)) + "</tr>")
    parts.append("</table></div>")
    return "".join(parts)


def to_html(blocks):
    """Render the block tree the way JoySpace shows it, for a faithful preview."""
    parts, counters = [], [0, 0]
    for block in blocks:
        kind = block.get("type")
        if kind == "table":
            parts.append(_table_html(block))
            continue
        if kind == "list" and block.get("header") in (1, 2):
            level = block["header"]
            counters[level - 1] += 1
            if level == 1:
                counters[1] = 0
            number = ".".join(str(v) for v in counters[:level])
            tag = f"h{level + 1}"
            parts.append(f'<{tag}><span class="num">{number}.</span>'
                         + _inline(block.get("children", [])) + f"</{tag}>")
            continue
        body = _inline(block.get("children", []))
        if body == "&nbsp;":
            continue
        parts.append("<p>" + body + "</p>")
    return "\n".join(parts)


PREVIEW_CSS = (
    "body{font:14px/1.6 -apple-system,BlinkMacSystemFont,'PingFang SC',"
    "'Microsoft YaHei',sans-serif;margin:20px;color:#1a2233}"
    "h2{font-size:20px;margin:24px 0 10px}"
    "h3{font-size:16px;margin:18px 0 8px}"
    ".num{margin-right:6px;color:#8c8c8c}"
    "p{margin:8px 0}"
    ".note{color:#F5222D}"
    ".budget-low{color:#389E0D}"
    ".muted{color:#8c8c8c}"
    ".scroll{overflow-x:auto;margin:12px 0}"
    "table.tbl{border-collapse:collapse;table-layout:fixed;font-size:13px}"
    "table.tbl th,table.tbl td{border:1px solid #e8b4ae;padding:6px 8px;"
    "vertical-align:middle;word-break:break-word;overflow-wrap:anywhere}"
    "table.tbl th{background:#F86560;color:#fff;font-weight:600;text-align:center}"
    "table.tbl td.alt{background:rgb(242,245,250)}"
    "table.tbl td.c,table.tbl th.c{text-align:center}"
)

_PREVIEW_SHELL = (
    "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none';"
    " script-src 'none'; img-src 'none'; style-src 'unsafe-inline';"
    " base-uri 'none'; form-action 'none'\">"
    "<title>性能准入报告预览</title><style>{css}</style></head><body>{body}</body></html>"
)


def wrap_preview(body):
    """Wrap rendered HTML in a self-contained, network-free preview document."""
    return _PREVIEW_SHELL.format(css=PREVIEW_CSS, body=body)