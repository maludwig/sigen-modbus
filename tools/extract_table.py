"""Turn a register table from a marker-converted protocol doc (Markdown) into CSV.

    python tools/extract_table.py --file "ext_doc/txt/…V2.9/…V2.9.md" --start-line 338 --end-line 374

Fixes the OCR/conversion slips seen in these docs:
- separator rows (|----|) anywhere are ignored, so a row before one isn't taken as a header
- "\\$32" / "$32" in the data type -> "S32"
- a quantity pushed into the next cell ("|   | 2<br>RO |") is moved back
- tick marks read as √ / V / 1 / <b>V</b> -> "Y"
- unit "S" -> "s"
- Greek/Cyrillic lookalike letters in the header (QTΥ, Туре) -> Latin
- <br> -> space, other tags dropped
- the width comes from the header: extra cells that are all empty (a page whose table
  was converted with a phantom empty column) are dropped
- a row with an empty "No" continues the numbered row above. Name and comment text is
  joined; every other column may be filled by at most one of the merged lines, otherwise
  it's an error (exit 2, no CSV), since that means the rows aren't what they look like
- a name that starts with words before a [tag], below a name that is only a [tag], gets
  those words moved up ("[Smart load 1]" / "Power [Smart load 2]" -> "… 1] Power" / "[… 2]")

Every data row is then checked (word count matches the type, permission is RO/RW/WO,
row numbers consecutive). Problems go to stderr and the exit code is 1, but the CSV is
still written so you can look at it. Notes about changes made go to stderr too.
"""

import argparse
import csv
import re
import sys
from pathlib import Path

# Header fragment (after joining a column's pieces) -> CSV column name.
COLUMNS = [
    ("no", "no"),
    ("name", "name"),
    ("add", "address"),
    ("qt", "quantity"),
    ("per", "permission"),
    ("data", "data_type"),
    ("gain", "gain"),
    ("unit", "unit"),
    ("hyb", "hybrid_inv"),
    ("pv", "pv_inv"),
    ("comment", "comment"),
]
LOOKALIKES = str.maketrans("ΥΤΑΒΕΗΚΜΝΟΡΧТАВЕНКМОРСХауеорсх", "YTABEHKMNOPXTABEHKMOPCXayeopcx")
TYPE_WORDS = {"U16": 1, "S16": 1, "U32": 2, "S32": 2, "U64": 4, "S64": 4}
TICKS = {"√", "V", "v", "1", "Y"}
SEPARATOR = re.compile(r"^:?-{2,}:?$")
TEXT_COLUMNS = {"name", "comment"}  # joined when continuation rows merge
LEADING_WORDS = re.compile(r"^([^\[\]]+?)\s+(\[.*)$")
TAG_ONLY = re.compile(r"^\[[^\]]*\]$")


class MergeConflict(ValueError):
    """Two merged lines both have a value in the same non-text column."""


def cells_of(line: str) -> list[str] | None:
    """'| a | b |' -> ['a', 'b'] (raw), or None if the line isn't a table row."""
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return None
    return [c.strip() for c in s[1:-1].split("|")]


def clean(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text)
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)
    return " ".join(text.split())


def column_names(header_rows: list[list[str]], width: int) -> list[str]:
    """Join each column's header fragments ('QT' + 'Υ') and map them to CSV names."""
    names = []
    for i in range(width):
        joined = "".join(row[i] for row in header_rows if i < len(row)).translate(LOOKALIKES)
        key = re.sub(r"[^a-z]", "", joined.lower())
        names.append(next((name for prefix, name in COLUMNS if key.startswith(prefix)), key or f"col{i}"))
    return names


def fix_row(row: dict[str, str]) -> None:
    perm = row.get("permission", "")
    m = re.fullmatch(r"(\d+)\s*(?:<br\s*/?>)?\s*(RO|RW|WO)", perm)
    if not row.get("quantity") and m:
        row["quantity"], row["permission"] = m.group(1), m.group(2)
    for key in row:
        row[key] = clean(row[key])
    dt = re.sub(r"^\\?\$", "S", row.get("data_type", ""))
    row["data_type"] = dt.upper() if re.fullmatch(r"[usUS]\d+", dt) else dt
    if row.get("unit") == "S":
        row["unit"] = "s"
    for key in ("hybrid_inv", "pv_inv"):
        if key in row and row[key] in TICKS:
            row[key] = "Y"


def problems_in(row: dict[str, str], expected_no: int | None) -> list[str]:
    found = []
    if not row.get("address", "").isdigit():
        found.append(f"address {row.get('address')!r} isn't a number")
    qty, dt = row.get("quantity", ""), row.get("data_type", "")
    if not qty.isdigit():
        found.append(f"quantity {qty!r} isn't a number")
    elif dt in TYPE_WORDS and int(qty) != TYPE_WORDS[dt]:
        found.append(f"quantity {qty} doesn't match {dt} ({TYPE_WORDS[dt]} words)")
    if "permission" in row and row["permission"] not in ("RO", "RW", "WO"):
        found.append(f"permission {row['permission']!r} isn't RO/RW/WO")
    gain = row.get("gain", "")
    if gain and not re.fullmatch(r"\d+(\.\d+)?|N/?A", gain):
        found.append(f"gain {gain!r} isn't a number or N/A")
    for key in ("hybrid_inv", "pv_inv"):
        if row.get(key) not in ("", "Y", None):
            found.append(f"{key} {row[key]!r} isn't a tick or blank")
    if expected_no is not None and row.get("no") != str(expected_no):
        found.append(f"row number {row.get('no')!r}, expected {expected_no}")
    return found


def fit(cells: list[str], width: int, number: int, problems: list[str]) -> list[str]:
    """Trim or pad a row to the header's width. Extra cells are fine if they're empty."""
    if len(cells) > width:
        extra = cells[width:]
        if any(extra):
            problems.append(f"line {number}: {len(cells)} cells, expected {width}; dropped non-empty {extra}")
        return cells[:width]
    if len(cells) < width:
        problems.append(f"line {number}: {len(cells)} cells, expected {width}")
        return cells + [""] * (width - len(cells))
    return cells


def merge(group: list, number: int, cells: list[str], names: list[str]) -> None:
    """Fold a continuation row (empty "No") into its numbered row: [first line, cells, merged lines]."""
    first, target, merged = group
    for i, c in enumerate(cells):
        if not c:
            continue
        if names[i] in TEXT_COLUMNS:
            target[i] = f"{target[i]} {c}".strip()
        elif not target[i]:
            target[i] = c
        else:
            raise MergeConflict(
                f"line {number}: merging into No {target[0]} (line {first}) would combine "
                f"{names[i]} {target[i]!r} with {c!r}"
            )
    merged.append(number)


def move_leading_words(rows: list[dict[str, str]], notes: list[str]) -> None:
    """'[Smart load 1]' then 'Power [Smart load 2]': the 'Power' belongs to the row above."""
    for prev, row in zip(rows, rows[1:]):
        m = LEADING_WORDS.match(row.get("name", ""))
        if m and TAG_ONLY.match(prev.get("name", "")):
            notes.append(
                f"line {row['source_line']}: moved {m.group(1)!r} from the start of No {row['no']}'s "
                f"name to the end of No {prev['no']}'s"
            )
            prev["name"] = f"{prev['name']} {m.group(1)}"
            row["name"] = m.group(2)


def extract(lines: list[str], first_line: int):
    """lines: the selected file lines; first_line: the file line number of lines[0].
    Returns (column names, rows with 'source_line', problems, notes).
    Raises MergeConflict if continuation rows can't be merged cleanly."""
    header_rows: list[list[str]] = []
    body = []  # (line number, cells), from the first numbered row on
    for offset, line in enumerate(lines):
        cells = cells_of(line)
        if cells is None or all(SEPARATOR.match(c) or not c for c in cells):
            continue
        if not body and not cells[0].isdigit():
            header_rows.append(cells)
        else:
            body.append((first_line + offset, cells))
    width = max((len(r) for r in header_rows), default=max((len(c) for _, c in body), default=0))
    names = column_names(header_rows, width)
    problems: list[str] = []
    notes: list[str] = []
    groups = []  # [first line, cells, [merged line numbers]]
    for number, cells in body:
        cells = fit(cells, width, number, problems)
        if cells[0].isdigit():
            groups.append([number, cells, []])
        else:
            merge(groups[-1], number, cells, names)
    rows = []
    for first, cells, merged in groups:
        row = dict(zip(names, cells))
        fix_row(row)
        row["source_line"] = f"{first}-{merged[-1]}" if merged else str(first)
        rows.append(row)
    move_leading_words(rows, notes)
    expected = None
    for row in rows:
        for p in problems_in(row, expected):
            problems.append(f"line {row['source_line']} (No {row.get('no')}): {p}")
        expected = int(row["no"]) + 1 if row.get("no", "").isdigit() else None
    return names + ["source_line"], rows, problems, notes


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", type=Path, required=True, help="marker-converted Markdown file")
    p.add_argument("--start-line", type=int, required=True, help="first line to read (1-based)")
    p.add_argument("--end-line", type=int, required=True, help="last line to read (inclusive)")
    p.add_argument("--out", type=Path, help="CSV file to write (default: stdout)")
    args = p.parse_args(argv)
    if args.start_line < 1 or args.end_line < args.start_line:
        p.error("need 1 <= --start-line <= --end-line")
    lines = args.file.read_text(encoding="utf-8").splitlines()[args.start_line - 1 : args.end_line]
    try:
        names, rows, problems, notes = extract(lines, args.start_line)
    except MergeConflict as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    out = open(args.out, "w", encoding="utf-8", newline="") if args.out else sys.stdout
    try:
        writer = csv.DictWriter(out, fieldnames=names, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if args.out:
            out.close()
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    print(f"{len(rows)} rows, {len(problems)} problem(s)", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
