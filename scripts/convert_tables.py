"""Convert HTML tables in notebook markdown cells into raw Typst tables.

Used by the `pdf` (Typst) build: notebooks keep HTML tables (readable in Jupyter
and in Quarto HTML output); this script rewrites the markdown cells into ```{=typst}
blocks (relative column widths, cell strokes, styled header row) just for the
Typst compilation. The makefile `restore` target puts the sources back afterwards.

Standard library only (pandoc is not installed in this environment).
"""

import re
import sys
import json
from pathlib import Path
from html import unescape

TABLE_RE = re.compile(r"<table[\s\S]*?</table>", re.IGNORECASE)
ROW_RE = re.compile(r"<tr[^>]*>([\s\S]*?)</tr>", re.IGNORECASE)
CELL_RE = re.compile(r"<(th|td)([^>]*)>([\s\S]*?)</\1>", re.IGNORECASE)
RAW_TYPST_RE = re.compile(r"(```\{=typst\}\n)([\s\S]*?)(```)")
BR_TAG_RE = re.compile(r"(?<!\\)<br\s*/?>", re.IGNORECASE)
HTML_TAG_RE = re.compile(
    r"(?<!\\)</?(small|span|big|sup|sub|u|s|em|i|strong|b|code|mark|font|a|p|div|img)[^>]*>",
    re.IGNORECASE,
)
RAW_TYPST_MATH_RE = re.compile(r"\\\$([^$\n]+?)\\\$")
MATH_ALIAS = {"cdot": "cdot", "infty": "infinity"}


def latex_math_to_typst(s):
    """Convert LaTeX-style math body (no `$`) to Typst math syntax."""
    s = re.sub(r"\\text\s*\{([^{}]*)\}", lambda m: '"%s"' % m.group(1), s)
    s = re.sub(r"\\(leq?)(?![a-zA-Z])", "<=", s)
    s = re.sub(r"\\(geq?)(?![a-zA-Z])", ">=", s)
    s = s.replace("\\%", "%")
    s = re.sub(r"\\([a-zA-Z]+)", lambda m: "\x08" + MATH_ALIAS.get(m.group(1), m.group(1)) + "\x08", s)
    toks = []
    def grab(m):
        toks.append(m.group(0))
        return "\x09%d\x09" % (len(toks) - 1)
    s = re.sub(r"\x08[a-zA-Z]+\x08|\"[^\"]*\"", grab, s)
    s = re.sub(r"[A-Za-z]{2,}", lambda m: " ".join(m.group(0)), s)
    s = re.sub(r"\x09(\d+)\x09", lambda m: toks[int(m.group(1))], s)
    return s.replace("\x08\x08", " ").replace("\x08", "").replace("\\{", "{").replace("\\}", "}")


def fix_legacy_math(m):
    body = m.group(1).replace("\\\\", "\\").replace("\\<", "<").replace("\\>", ">")
    return "$" + latex_math_to_typst(body) + "$"



def fr_columns(widths):
    known = [w for w in widths if w]
    if not known or len(known) != len(widths):
        return ", ".join(["1fr"] * len(widths))
    m = max(known)
    return ", ".join(f"{round(w / m, 3)}fr" for w in known)


def escape_typst(s):
    out = []
    for ch in s:
        if ch in "#@[]|`$\\<>":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def cell_inner(html_frag):
    """HTML fragment -> typst content string (linebreaks as `\\`, lists as `- `)."""
    s = re.sub(r"<br\s*/?>", "\x00", html_frag, flags=re.IGNORECASE)
    s = re.sub(r"<code[^>]*>([\s\S]*?)</code>", "\x03\\1\x03", s, flags=re.IGNORECASE)
    s = re.sub(r"<(strong|b)>([\s\S]*?)</\1>", "\x04\\2\x04", s, flags=re.IGNORECASE)
    # keep list items: each <li> becomes one bullet line
    lis = re.findall(r"<li[^>]*>([\s\S]*?)</li>", s, re.IGNORECASE)
    if lis:
        joined_lists = "\x02".join("\x01" + li for li in lis)
        s = re.sub(r"<ul[^>]*>[\s\S]*?</ul>|<ol[^>]*>[\s\S]*?</ol>", lambda _m: joined_lists, s, flags=re.IGNORECASE)
    s = re.sub(r"</?(strong|b|code|i|em|ul|ol|li|p|div|span|small|big|sup|sub|u|s|mark|a|img|br)[^>]*>", "", s, flags=re.IGNORECASE)
    s = unescape(s)
    math_spans = []

    def protect_math(m):
        math_spans.append(latex_math_to_typst(m.group(1)))
        return "\x07%d\x07" % (len(math_spans) - 1)

    s = re.sub(r"\$([^$\n]+)\$", protect_math, s)
    lines = []
    for chunk in s.split("\x00"):
        chunk = " ".join(chunk.split())
        lines.append(chunk)
    text = "\x06".join(lines)
    if "\x01" in text:
        parts = text.split("\x01")
        text = "\n".join(("- " + p.strip()) if p.strip() else "" for p in parts)
    out = escape_typst(text.strip())
    parts = out.split("\x04")
    out = "".join(
        ('#text(weight: "bold")[' + p + "]") if i % 2 else p for i, p in enumerate(parts)
    )
    out = re.sub(r"\x07(\d+)\x07", lambda m: "$" + math_spans[int(m.group(1))] + "$", out)
    out = out.replace("\x03", "`").replace("\x06", "\\\n")
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", out)


def cell_arg(attrs, html_frag):
    inner = cell_inner(html_frag)
    props = []
    r = re.search(r"rowspan=\"?(\d+)\"?", attrs)
    c = re.search(r"colspan=\"?(\d+)\"?", attrs)
    if r:
        props.append(f"rowspan: {r.group(1)}")
    if c:
        props.append(f"colspan: {c.group(1)}")
    if "\\\n" in inner or "\n" in inner:
        props.append("align: left + top")
    if not props:
        return "[" + inner + "]"
    multi = "\\\n" in inner or "\n" in inner
    body = "\n    " + inner + "\n  " if multi else inner
    return "table.cell(" + ", ".join(props) + ")[" + body + "]"


def make_typst_table(table_html):
    raw_rows = [CELL_RE.findall(tr) for tr in ROW_RE.findall(table_html)]
    raw_rows = [r for r in raw_rows if r]
    if not raw_rows:
        return None

    def spans(attrs):
        m = re.search(r'colspan="?(\d+)"?', attrs)
        cs = int(m.group(1)) if m else 1
        m = re.search(r'rowspan="?(\d+)"?', attrs)
        rs = int(m.group(1)) if m else 1
        return cs, rs

    grid = []
    pending = {}
    ncols = 0
    maxlen = {}
    for raw in raw_rows:
        active = {c for c, v in pending.items() if v > 0}
        for c in active:
            pending[c] -= 1
        placed = []
        col = 0
        for t, a, frag in raw:
            while col in active:
                col += 1
            cs, rs = spans(a)
            placed.append((t.lower(), a, frag, col, cs))
            if rs > 1:
                for k in range(cs):
                    pending[col + k] = rs - 1
            plain = "".join(unescape(re.sub(r"<[^>]+>", " ", frag)).split())
            for k in range(cs):
                maxlen[col + k] = max(maxlen.get(col + k, 0), len(plain), 6)
            col += cs
            ncols = max(ncols, col)
        for c, v in pending.items():
            ncols = max(ncols, c + 1) if v > 0 else ncols
        grid.append(placed)

    header_grid = grid[0] if all(t == "th" for t, _, _, _, _ in grid[0]) else []
    body = grid[1:] if header_grid else grid

    widths = [None] * ncols
    for _, a, _, col, cs in header_grid:
        m = re.search(r"width:\s*([\d.]+)%", a)
        widths[col] = float(m.group(1)) if m else None
    if not any(widths):
        widths = [(maxlen.get(c, 10) ** 0.5) for c in range(ncols)]
    cols = fr_columns(widths)
    align = ", ".join(["left + horizon"] * ncols)
    head = ""
    if header_grid:
        parts = []
        for t, a, frag, col, cs in header_grid:
            cell = '[#text(weight: "bold", fill: rgb("003247"))[' + cell_inner(frag) + "]]"
            if cs > 1:
                cell = f"table.cell(colspan: {cs})" + cell
            parts.append(cell)
        head = "table.header(" + ", ".join(parts) + "),\n  "

    body_rows = []
    for row in body:
        body_rows.append(",\n  ".join(cell_arg(a, frag) for _, a, frag, _, _ in row))
    body_txt = ",\n  ".join(body_rows)

    return (
        "```{=typst}\n"
        "#table(\n"
        f"  columns: ({cols}),\n"
        f"  align: ({align}),\n"
        '  stroke: 0.5pt + rgb("8197A6"),\n'
        "  inset: (x: 6pt, y: 5pt),\n"
        '  fill: (x, y) => if y == 0 { rgb("CCE5FF") } else if calc.rem(y, 2) == 0 { rgb("F4F8FC") } else { none },\n'
        f"  {head}"
        f"  {body_txt}\n"
        ")\n"
        "```\n"
    )



def output_table_html(cell):
    """Cleaned HTML table found in a code cell's outputs (index column dropped), or None."""
    blob = ""
    for out in cell.get("outputs", []):
        data = out.get("data", {}) if isinstance(out, dict) else {}
        blob = "".join(data.get("text/html", [])) or blob
    tables = TABLE_RE.findall(blob)
    if not tables:
        return None
    table = tables[-1]
    row_ms = list(ROW_RE.finditer(table))
    if not row_ms:
        return table
    first_cells = CELL_RE.findall(row_ms[0].group(1))
    drop_index = len(first_cells) > 1 and not "".join(first_cells[0][2].split())
    out_parts = []
    last = 0
    for idx, rm in enumerate(row_ms):
        row = rm.group(0)
        if drop_index and idx >= 1:
            cm = next(CELL_RE.finditer(row), None)
            if cm:
                row = row[:cm.start()] + row[cm.end():]
        elif drop_index and idx == 0:
            cm = next(CELL_RE.finditer(row), None)
            if cm:
                row = row[:cm.start()] + row[cm.end():]
        out_parts.append(table[last:rm.start()] + row)
        last = rm.end()
    out_parts.append(table[last:])
    return "".join(out_parts) if drop_index else table


def strip_display_line(cell):
    """Drop trailing bare-dataframe display expressions from a code cell's source."""
    lines = list(cell.get("source", []))
    while lines and re.fullmatch(r"\s*(df_\w+|\w+_df|\w+_data(frame)?|display\(\s*(df_\w+|\w+_df)\s*\))\s*\n?", lines[-1], re.IGNORECASE):
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def process_notebook(nb_path: Path, hide_code: bool = False):
    with open(nb_path, "r", encoding="utf-8") as f:
        nb_data = json.load(f)

    changed = False
    cells = nb_data.get("cells", [])
    keep = []
    for cell in cells:
        if cell.get("cell_type") == "code":
            table_html = output_table_html(cell)
            if table_html:
                moved = dict(cell)
                moved["outputs"] = []
                moved["source"] = [] if hide_code else strip_display_line(cell)
                changed = True
                if not hide_code or moved["source"]:
                    keep.append(moved)
                block = make_typst_table(table_html) or table_html
                md = {
                    "cell_type": "markdown",
                    "metadata": {},
                    "source": [ln + "\n" for ln in block.splitlines()],
                }
                if md["source"]:
                    md["source"][-1] = md["source"][-1].rstrip("\n")
                keep.append(md)
                continue
            if hide_code and cell.get("source"):
                cell = dict(cell)
                cell["source"] = []
                changed = True
            if hide_code and not cell.get("outputs") and not cell.get("source"):
                changed = True
                continue
            keep.append(cell)
            continue
        source_text = "".join(cell.get("source", []))

        def repl(m):
            block = make_typst_table(m.group(0))
            return block if block else m.group(0)

        new_text = TABLE_RE.sub(repl, source_text)

        def sanitize(m):
            inner = BR_TAG_RE.sub(" \\\n", m.group(2))
            inner = HTML_TAG_RE.sub("", inner)
            inner = RAW_TYPST_MATH_RE.sub(fix_legacy_math, inner)
            return m.group(1) + inner + m.group(3)
        new_text = RAW_TYPST_RE.sub(sanitize, new_text)
        if new_text != source_text:
            lines = new_text.splitlines()
            cell["source"] = [line + "\n" for line in lines]
            if cell["source"]:
                cell["source"][-1] = cell["source"][-1].rstrip("\n")
            changed = True
        keep.append(cell)

    if changed:
        nb_data["cells"] = keep
        with open(nb_path, "w", encoding="utf-8") as f:
            json.dump(nb_data, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    hide_code = "--hide-code" in sys.argv[1:]
    for arg in sys.argv[1:]:
        if arg != "--hide-code":
            process_notebook(Path(arg), hide_code=hide_code)
