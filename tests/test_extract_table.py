"""tools/extract_table.py, on the conversion slips found in the marker output of the V2.9 doc."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import pytest  # noqa: E402
from extract_table import MergeConflict, extract  # noqa: E402

HEADER = [
    "| No | Name | Add. | QTY | Perm. | Data Type | Gain | Unit | Hyb | PV | Comment |",
    "|----|------|------|-----|-------|-----------|------|------|-----|----|---------|",
]

# Trimmed from V2.9 lines 340-371: the multi-row header with lookalike letters, "\$32",
# a quantity pushed into the permission cell, and a separator after a data row.
SAMPLE = r"""
| No | Name                          | Add.  | QT | Per | Data | Gain | Unit | Hyb      | PV       | Comment       |
|----|-------------------------------|-------|----|-----|------|------|------|----------|----------|---------------|
|    |                               |       | Υ  | m.  | Туре |      |      | rid      | inv.     |               |
|    |                               |       |    |     |      |      |      | Inv,     |          |               |
| 1  | System time                   | 30000 | 2  | RO  | U32  | 1    | S    | 1        | 1        | Epoch seconds |
| 5  | [Grid sensor]<br>Active power | 30005 | 2  | RO  | \$32 | 1000 | kW   | V        | <b>V</b> | >0 buy        |

Page 15

| 7  | On/Off<br>Grid<br>status      | 30009 | 1 | RO      | U16 | N/A  | N/A  | √ |   | 0:<br>on<br>grid |
|----|-------------------------------|-------|---|---------|-----|------|------|---|---|------------------|
| 14 | Plant<br>phase<br>A<br>reactive<br>power | 30021 |   | 2<br>RO | S32 | 1000 | kVar | √ | √ |   |
""".strip().splitlines()


def test_fixes_and_columns():
    names, rows, problems, _ = extract(SAMPLE, 340)
    assert names == [
        "no",
        "name",
        "address",
        "quantity",
        "permission",
        "data_type",
        "gain",
        "unit",
        "hybrid_inv",
        "pv_inv",
        "comment",
        "source_line",
    ]
    by_no = {r["no"]: r for r in rows}
    assert list(by_no) == ["1", "5", "7", "14"]  # row 7 is data, not a header
    assert by_no["1"]["unit"] == "s" and by_no["1"]["pv_inv"] == "Y"
    assert by_no["5"]["data_type"] == "S32" and by_no["5"]["name"] == "[Grid sensor] Active power"
    assert by_no["7"]["comment"] == "0: on grid" and by_no["7"]["pv_inv"] == ""
    assert (by_no["14"]["quantity"], by_no["14"]["permission"]) == ("2", "RO")
    assert by_no["14"]["source_line"] == "351"  # 12th sample line, from 340
    # Only the gaps in numbering are reported (this sample skips rows on purpose).
    assert all("row number" in p for p in problems) and len(problems) == 3


def test_reports_bad_rows():
    bad = [
        "| No | Name | Add. | QTY | Perm. | Data Type | Gain | Unit |",
        "| 1  | x    | 30000 | 1  | RO    | U32       | 1    | s    |",
        "| 2  | y    | 3000O | 2  | RX    | S32       | 1O   | kW   |",
    ]
    _, _, problems, _ = extract(bad, 1)
    text = "\n".join(problems)
    assert "doesn't match U32" in text and "address '3000O'" in text
    assert "permission 'RX'" in text and "gain '1O'" in text


def test_width_comes_from_the_header():
    # V2.9 line 469-471: one page's table gained a phantom empty column.
    lines = HEADER + [
        "| 74 | [Smartload<br>24]<br>Total<br>consumption | 30144 | 2 | RO | U32 | 100  | kWh | √ | √ |  |  |",
        "|----|----|-------|---|----|-----|------|-----|---|---|--|--|",
        "| 75 | [Smart<br>load<br>1] | 30146 | 2 | RO | S32 | 1000 | kW  | √ | √ |  |  |",
        "| 76 | x | 30148 | 2 | RO | S32 | 1000 | kW | √ | √ |  |",
    ]
    _, rows, problems, _ = extract(lines, 1)
    assert len(rows) == 3 and problems == []
    lines.append("| 77 | y | 30150 | 2 | RO | S32 | 1000 | kW | √ | √ |  | stray |")
    _, _, problems, _ = extract(lines, 1)
    assert len(problems) == 1 and "dropped non-empty ['stray']" in problems[0]


def test_continuation_rows_fill_blanks():
    # V2.9 lines 459-461: row 72's ticks and the rest of its name are on the next two lines.
    lines = HEADER + [
        "| 72 | [Smart<br>load<br>22] | 30140 | 2 | RO | U32 | 100 | kWh |   |   |  |",
        "|    |                       |       |   |    |     |     |     | √ | √ |  |",
        "|    | Total<br>consumption  |       |   |    |     |     |     |   |   |  |",
        "| 73 | [Smart<br>load<br>23] | 30142 | 2 | RO | U32 | 100 | kWh | √ | √ |  |",
    ]
    _, rows, problems, _ = extract(lines, 457)
    assert problems == []
    assert rows[0]["name"] == "[Smart load 22] Total consumption"
    assert (rows[0]["hybrid_inv"], rows[0]["pv_inv"], rows[0]["source_line"]) == ("Y", "Y", "459-461")


def test_continuation_conflict_is_an_error():
    lines = HEADER + [
        "| 72 | [Smart load 22] | 30140 | 2 | RO | U32 | 100 | kWh | √ |   |  |",
        "|    | Total           |       |   |    |     |     |     | √ | √ |  |",
    ]
    with pytest.raises(MergeConflict, match=r"line 4: merging into No 72 \(line 3\) would combine hybrid_inv"):
        extract(lines, 1)


def test_leading_words_move_up():
    # V2.9 lines 471-475: "Power" drifts down a row.
    names = [
        "[Smart<br>load<br>1]",
        "Power<br>[Smart<br>load<br>2]",
        "Power<br>[Smart<br>load<br>3]",
        "Power<br>[Smart<br>load<br>4]<br>Power",
        "[Smart<br>load<br>5]<br>Power",
    ]
    lines = HEADER + [
        f"| {75 + i} | {n} | {30146 + 2 * i} | 2 | RO | S32 | 1000 | kW | √ | √ |  |" for i, n in enumerate(names)
    ]
    _, rows, problems, notes = extract(lines, 1)
    assert [r["name"] for r in rows] == [f"[Smart load {n}] Power" for n in range(1, 6)]
    assert problems == [] and len(notes) == 3 and "moved 'Power'" in notes[0]
