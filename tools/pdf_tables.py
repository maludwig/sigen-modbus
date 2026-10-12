"""Extract the register and appendix tables from a Sigenergy protocol PDF, from its text layer.

    python tools/pdf_tables.py --pdf "ext_doc/pdf/…V2.9.pdf" --out-dir ext_doc/csv/v2.9 [--only 5-3 --only 5-4]

Needs pdfplumber (pip install pdfplumber), which sigen itself doesn't. Unlike marker's OCR,
the text layer has exact numbers, and text_x_tolerance=1.5 keeps the spaces between words.

For each "Table N-…" caption it takes the table that follows plus every headerless
continuation on later pages, merges rows split across pages with extract_table.py's rules
(name/comment text is joined; any other column filled twice is an error), and checks each
row (word count matches type, RO/RW/WO, numbering). "Appendix N …" tables (description +
bit/value) are written too. Problems go to stderr; exit code 1 if there were any.
"""
import argparse
import csv
import re
import sys
from pathlib import Path
from typing import List, Optional

import pdfplumber

from extract_table import MergeConflict, column_names, fit, fix_row, merge, problems_in

SETTINGS = {"text_x_tolerance": 1.5}
# A caption's title starts with a capital: this skips body text that mentions a table
# ("Table 2-1 lists …") and revision-history lines ('Appendix 1 "Running state" added …').
CAPTION = re.compile(r"^(Table (\d+(?:-\d+)+)|Appendix (\d+)) ([A-Z].*)$")


def cell(text: Optional[str]) -> str:
    return " ".join((text or "").split())


def strip_hidden_number(raw: str) -> tuple:
    """Table 5-3's name cells carry a second, invisible row number (clipped in the PDF, but
    present in the text layer), one digit glued to the start of each line: '8Model type',
    '1Machine firmware\\n0version' (= 10). Returns (name without it, the number or None).
    Only fires when the first line starts with a digit glued to a letter or '[', so a real
    name that wraps onto a number ('Alarm\\n1') is left alone."""
    lines = (raw or "").split("\n")
    if not re.match(r"^\d[A-Za-z\[]", lines[0]):
        return raw, None
    digits, kept = [], []
    for i, line in enumerate(lines):
        if (i == 0 or len(digits) == i) and re.match(r"^\d(?=[A-Za-z\[(]|$)", line):
            digits.append(line[0])
            line = line[1:]
        kept.append(line)
    return "\n".join(kept), int("".join(digits))


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


class Logical:
    """One table as printed: its header and rows, possibly spread over several pages."""

    def __init__(self, page: int, header: List[str]):
        self.page, self.header, self.rows = page, header, []      # rows: (page, cells)


def is_header(cells: List[str]) -> bool:
    cells = [cell(c) for c in cells]
    first = cells[0].replace(" ", "").lower() if cells else ""
    return first.startswith("no") or (len(cells) == 2 and cells[1] in ("Bit", "Value"))


def read_document(pdf) -> tuple:
    """Captions (kind, id, title, page) and logical tables, both in document order."""
    captions, tables = [], []
    for n, page in enumerate(pdf.pages, 1):
        for line in (page.extract_text(x_tolerance=1.5) or "").splitlines():
            m = CAPTION.match(line.strip())
            if m and not re.search(r"\.{5,}", line):                # skip table-of-contents lines
                kind = "table" if m.group(2) else "appendix"
                captions.append((kind, m.group(2) or m.group(3), m.group(4).strip(), n))
        for raw in page.extract_tables(table_settings=SETTINGS):
            rows = [[c or "" for c in r] for r in raw]         # raw text, line breaks kept
            if not rows:
                continue
            if is_header(rows[0]):
                tables.append(Logical(n, [cell(c) for c in rows[0]]))
                rows = rows[1:]
            elif not tables or tables[-1].header is None or len(rows[0]) != len(tables[-1].header):
                # Headerless and a different shape: some other table (e.g. chapter 6's frame
                # formats), not a continuation. Block anything from attaching past it.
                tables.append(Logical(n, None))
                continue
            tables[-1].rows += [(n, r) for r in rows]
    return captions, tables


def register_rows(table: Logical):
    width = len(table.header)
    names = column_names([table.header], width)
    name_col = names.index("name") if "name" in names else None
    problems, groups, hidden = [], [], []                         # groups: [page, cells, merged pages]
    for page, raw in table.rows:
        raw = list(raw)
        if name_col is not None and name_col < len(raw):
            raw[name_col], number = strip_hidden_number(raw[name_col])
            if number is not None:
                hidden.append(number)
        cells = [cell(c) for c in raw]
        if not any(cells):
            continue
        cells = fit(cells, width, page, problems)
        if cells[0].isdigit():
            groups.append([page, cells, []])
        elif groups:
            merge(groups[-1], page, cells, names)
        else:
            problems.append(f"page {page}: continuation row before any numbered row: {cells}")
    rows, expected = [], None
    for page, cells, merged in groups:
        row = dict(zip(names, cells))
        fix_row(row)
        if "unit" in row:
            row["unit"] = row["unit"].replace("℃", "°C")
        last = merged[-1] if merged else page
        row["source_page"] = str(page) if last == page else f"{page}-{last}"
        checked = row
        span = re.fullmatch(r"(\d{5})-(\d{5})", row.get("address", "").replace(" ", ""))
        if span:                     # a "Reserved 40069-40156" range row: check its first address
            row["address"] = f"{span.group(1)}-{span.group(2)}"
            row["quantity"] = row.get("quantity", "").replace(" ", "")
            checked = {**row, "address": span.group(1)}
        problems += [f"page {page} (No {row.get('no')}): {p}" for p in problems_in(checked, expected)]
        expected = int(row["no"]) + 1 if row.get("no", "").isdigit() else None
        rows.append(row)
    if hidden:
        print(f"  note: removed hidden row numbers from {len(hidden)} name(s): {hidden[0]}…{hidden[-1]}",
              file=sys.stderr)
    return names + ["source_page"], rows, problems


def appendix_rows(table: Logical):
    rows = []
    for page, raw in table.rows:
        cells = [cell(c) for c in raw]
        if len(cells) < 2 or not cells[0] or cells[1].lower().replace(" ", "") == "notdefined":
            continue
        if rows and not cells[1]:                                  # description wrapped onto a new row
            rows[-1]["description"] += " " + cells[0]
            continue
        rows.append({"value": cells[1], "description": cells[0], "source_page": str(page)})
    return ["value", "description", "source_page"], rows, []


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pdf", type=Path, required=True, help="protocol PDF")
    p.add_argument("--out-dir", type=Path, required=True, help="directory for the CSV files")
    p.add_argument("--only", action="append", metavar="ID",
                   help="table id (e.g. 5-3) or appendix number to write; repeatable (default: all)")
    args = p.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with pdfplumber.open(args.pdf) as pdf:
        captions, tables = read_document(pdf)
    used, total_problems = -1, 0
    for kind, ident, title, page in captions:
        if kind == "table" and not ident.startswith("5-"):
            continue                                              # chapters 2-4: prose tables, not registers
        # The table starts on the caption's page or the next; a caption with no table there
        # (e.g. a stray mention in the revision history) claims nothing.
        index = next((i for i, t in enumerate(tables)
                      if i > used and t.header is not None and page <= t.page <= page + 1), None)
        if index is None:
            print(f"warning: no table right after {kind} {ident} {title!r} (page {page}); skipped",
                  file=sys.stderr)
            continue
        used = index
        if args.only and ident not in args.only:
            continue
        table = tables[index]
        try:
            names, rows, problems = (register_rows if kind == "table" else appendix_rows)(table)
        except MergeConflict as e:
            print(f"error: {kind} {ident}: {e}", file=sys.stderr)
            total_problems += 1
            continue
        stem = f"table_{ident}_{slug(title)}" if kind == "table" else f"appendix_{int(ident):02d}_{slug(title)}"
        with open(args.out_dir / f"{stem}.csv", "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=names, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        print(f"{stem}.csv: {len(rows)} rows from page {table.page}, {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems:
            print(f"  problem: {problem}", file=sys.stderr)
        total_problems += len(problems)
    return 1 if total_problems else 0


if __name__ == "__main__":
    sys.exit(main())
