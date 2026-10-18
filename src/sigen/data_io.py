"""The shared JSON format: `sigen read --json` dumps it, the fake server serves it.

A document looks like:

    {
      "unit_id": 247,
      "input_registers":   {"30014": {"name": "plant_ess_soc", "type": "u16", "gain": 10, "value": 62.3, "raw": [623]}},
      "holding_registers": {"40046": {"name": "plant_backup_soc", "type": "u16", "gain": 10, "value": 20.0}}
    }

When loading, "raw" (16-bit words, as read from the wire) wins over "value".
Hand-written fake data can use typed values instead:
    {"type": "s32", "gain": 1000, "value": -2.0}         -> encoded for you
    {"type": "string", "count": 15, "value": "SigenStor"}
Other top-level keys ("source", "derived", "errors", "_comment") are ignored on load.

The top level is the plant (unit 247). Other units read at the same time (the inverter,
unit 1 here) go in "other_units", each with the same unit_id / input_registers /
holding_registers / errors layout:

    {"unit_id": 247, "input_registers": {…}, …,
     "other_units": [{"unit_id": 1, "input_registers": {"30500": …}, "errors": {}}]}
"""

import datetime
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sigen import __version__
from sigen.registers import PLANT_UNIT_ID, TYPE_WORDS, encode_int, encode_string

TABLE_KEYS = {"input": "input_registers", "holding": "holding_registers"}

# (unit_id, table) -> {address: word}
RegisterImage = Dict[Tuple[int, str], Dict[int, int]]


def spec_to_words(spec: dict, where: str) -> List[int]:
    """One register spec from a JSON file -> list of 16-bit words."""
    if "raw" in spec:
        words = [int(w) & 0xFFFF for w in spec["raw"]]
        if not words:
            raise ValueError(f"{where}: 'raw' is empty")
        return words
    rtype = spec.get("type", "u16")
    value = spec.get("value")
    if value is None:
        raise ValueError(f"{where}: needs 'raw', or a non-null 'value'")
    if rtype == "string":
        return encode_string(str(value), int(spec.get("count", (len(str(value)) + 1) // 2)))
    if rtype not in TYPE_WORDS:
        raise ValueError(f"{where}: unknown type '{rtype}'")
    n = round(float(value) * float(spec.get("gain", 1)))
    try:
        return encode_int(n, rtype)
    except ValueError as e:
        raise ValueError(f"{where}: {e}")


def read_json_file(path: Path):
    """Parse a JSON file written as UTF-8 (with or without a BOM), or as UTF-16 with a BOM,
    which is what Windows PowerShell 5's `>` produces. Raises OSError or ValueError."""
    data = Path(path).read_bytes()
    encoding = "utf-16" if data[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    return json.loads(data.decode(encoding))


def unit_sections(doc: dict) -> List[dict]:
    """The document's per-unit sections: the top level (the plant), then each of other_units."""
    return [doc] + list(doc.get("other_units", []))


def load_document(path: Path) -> RegisterImage:
    doc = read_json_file(path)
    image: RegisterImage = {}
    for section in unit_sections(doc):
        unit = int(section.get("unit_id", PLANT_UNIT_ID))
        for table, key in TABLE_KEYS.items():
            regs = image.setdefault((unit, table), {})
            for addr_s, spec in section.get(key, {}).items():
                where = f"{Path(path).name} unit {unit} {key}[{addr_s}]"
                for i, word in enumerate(spec_to_words(spec, where)):
                    regs[int(addr_s) + i] = word
    return image


def load_dir(data_dir: Path) -> Tuple[RegisterImage, List[Path]]:
    """Merge every *.json in a directory (name order; later files win)."""
    files = sorted(Path(data_dir).glob("*.json"))
    merged: RegisterImage = {}
    for path in files:
        for key, regs in load_document(path).items():
            merged.setdefault(key, {}).update(regs)
    return merged, files


def _section(readings: dict, unit: int) -> dict:
    section = {"unit_id": unit, "input_registers": {}, "holding_registers": {}, "errors": {}}
    for name, r in readings.items():
        if "error" in r:
            section["errors"][name] = {"table": r["table"], "address": r["address"], "error": r["error"]}
            continue
        spec = {"name": name}
        spec.update({k: r[k] for k in ("type", "gain", "unit", "value", "label", "raw") if k in r})
        section[TABLE_KEYS[r["table"]]][str(r["address"])] = spec
    return section


def build_document(
    readings: dict, *, ip: str, port: int, unit: int, derived: dict, other_units: Optional[Dict[int, dict]] = None
) -> dict:
    """Readings (from BlockReader.read_all) -> a dump document. `readings` is the plant's;
    other_units maps further unit IDs (inverters) to their readings."""
    plant = _section(readings, unit)
    doc = {
        "_comment": "Written by sigen-modbus. Also loadable as fake data by `sigen fake-server`.",
        "source": {
            "ip": ip,
            "port": port,
            "tool": f"sigen-modbus {__version__}",
            "read_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        },
        "unit_id": unit,
        "derived": derived,
        "input_registers": plant["input_registers"],
        "holding_registers": plant["holding_registers"],
        "errors": plant["errors"],
    }
    if other_units:
        doc["other_units"] = [_section(r, u) for u, r in sorted(other_units.items())]
    return doc


def capture_filename(doc: dict, prefix: str) -> str:
    """'my_system_2026-10-07_17-13-34Z.json', from the document's own UTC read time."""
    read_at = datetime.datetime.fromisoformat(doc["source"]["read_at"]).astimezone(datetime.timezone.utc)
    return f"{prefix}_{read_at:%Y-%m-%d_%H-%M-%S}Z.json"


def save_document(doc: dict, path: Path, *, overwrite: bool = True) -> None:
    """Write a dump as UTF-8 with LF line endings. With overwrite=False, raises
    FileExistsError instead of replacing an existing file."""
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    with open(path, "w" if overwrite else "x", encoding="utf-8", newline="\n") as f:
        f.write(text)
