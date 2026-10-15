# Development

```
pip install -e ".[dev]"      # the package (editable), plus pytest and other development tools
pytest                       # in parallel, a worker per CPU core (pytest-xdist); pytest -n 0 runs them one at a time
```

Use an editable install (`-e`): a plain `pip install .` copies the code once, and later edits
won't show up. Without installing at all: `PYTHONPATH=src python -m sigen …`.

The daily page's Sankey diagram is plain JavaScript (`src/sigen/static/my_sankey.js`, no
dependencies). Its layout is tested with Node's built-in test runner
(`node --test tests/js/my_sankey.test.js`); `pytest` runs that too when Node is installed, and
skips it when it isn't. No npm packages are needed.

## Layout

```
src/sigen/
  registers.py      the register maps (PLANT_REGISTERS for unit 247, INVERTER_REGISTERS), labels,
                    alarm bits; writable=True plus limits is the write allowlist
  modbus.py         a minimal Modbus TCP client (functions 03, 04, 06, 16)
  block_reader.py   plans the fewest reads that cover a unit's registers, and reads them
  control.py        the only write path: allowlist → read → write → read back; journal, undo
  reading.py        derived values, the summary, tables, saved captures
  config.py         finding and validating config.json
  db.py             PostgreSQL: connect, migrations, inserts, status, read-only queries
  migrations/       numbered SQL migrations
  poll.py           the poll loop
  weather.py        Open-Meteo and radar fetching, parsing, storing; the background scheduler
  goes.py           NOAA GOES-West satellite cubes
  alerts.py         `poll --alerts`: the in-memory window, the watcher and the ntfy sender
  battery_watch.py  the battery detectors: pure functions over a window of readings
  ntfy.py           posting to ntfy
  charts.py         the dashboard's queries and figures
  pvmodel.py        the clear-sky model per roof face and string (pvlib)
  dashboard.py      the Flask app; templates/dashboard.html
  netlookup.py      MAC ↔ IP: Windows' SendARP, or the Linux kernel's ARP read with `ip neigh`
  discovery.py      --ip-range parsing, network scan
  data_io.py        the capture format
  fake_server.py    a fake SigenStor for tests and offline work
  cli.py            the `sigen` command
tools/              protocol PDF → CSV extraction (see below)
fake_data/          captures the fake server replays
tests/              pytest
```

## Tests

`pytest` runs everything that doesn't need a database. The database tests (migrations, polling
end to end, the dashboard's numbers, `db query`, alerts) need a PostgreSQL database they can
create schemas in: they use `SIGEN_TEST_DB_URL` if set, otherwise `database.test_url` from the
repository's `config.json`, and are skipped without either. They work in throwaway schemas that
are dropped afterwards, so a permanent test database works fine:

```sql
CREATE ROLE sigen_test LOGIN PASSWORD 'PASSWORD';
CREATE DATABASE sigen_test OWNER sigen_test;
```

Most database tests share one schema (per pytest worker), migrated once at the start and emptied
after each test, so they don't each pay for migrating. A test that changes the schema itself (a
column, a table, `register_meta`) is noticed afterwards, and the schema is built again for the
next one. Tests that need a schema of their own, not yet migrated (tests of migrating, of refusing
an unmigrated database, or that alter the schema on purpose), are marked
`@pytest.mark.fresh_schema`.

The rules the tests follow:
- **Every test starts with an empty config.** An autouse fixture points `SIGEN_CONFIG` at a `{}`
  file, so a test that forgets `--config` or `--ip` can't pick up your `config.json` and reach a
  real inverter. Tests of the config search itself remove the variable.
- **Database tests take the `config` fixture and read the URL from its file**, never a URL as a
  test argument: pytest prints a failing test's arguments, and the URL holds a password.
- **A server that's down is the `hangs_up` fixture**, a local port that closes every connection
  at once, never a closed port: Windows retries a refused connection for about 2 s (and
  PostgreSQL's client waits out its whole 10 s connect timeout there).
- **No real waiting.** The `quick_polls` fixture runs `poll`'s polls back to back, and timeouts a
  test means to hit are short (0.2 s).

The device tests run against the fake server, never a real SigenStor.

## How reading works

`block_reader.py` plans the fewest Modbus reads that cover a unit's registers (4 for the plant,
3 for the inverter) and reads them on a fresh connection each time, following the device's rules
for what one read may span (see [SIGENSTOR_NOTES.md](SIGENSTOR_NOTES.md#reading)): blocks start
and end on register boundaries, with no limit on the gaps inside them.

`poll` reads strictly: all or nothing per unit. `read` and `capture` fall back to one register at
a time when a block is rejected, so they name the exact unreadable register.

## How writes work

There is exactly one write path (`control.py`), always **read → write → read back**:

1. **Allowlist.** Only registers marked `writable=True` in `registers.py` can be written, and only
   with their allowed values.
2. **Read** the current value. If `--expect-current` is given and doesn't match, stop (exit 7).
3. If it's already the target, do nothing (exit 0). Without `--apply`, stop (exit 8).
4. **Write**, then **read back** and compare the raw words. If the connection drops mid-write,
   re-read on a fresh connection to find out what happened. A write the device refuses is exit 4;
   a read-back that doesn't match is exit 5.
5. With `--apply`, every attempt goes in the **journal**
   (`~/.local/state/sigen-modbus/journal.jsonl`): time, device, register, before / target / after
   (decoded and raw), outcome. `undo` uses it, and refuses if the register changed since (exit 7).

Never test a write against a real system; use the fake server (`--ignore-writes` makes writes
"succeed" without storing them, for exit 5; `--reject-writes` refuses them, for exit 4).

## The register map

From Sigenergy's Modbus protocol V2.9, cross-checked with
[TypQxQ/Sigenergy-Local-Modbus](https://github.com/TypQxQ/Sigenergy-Local-Modbus) and
[seud0nym/sigenergy2mqtt](https://github.com/seud0nym/sigenergy2mqtt), and **every register read
on its own from a real device before being added** (a block read can't tell an unsupported
register from a real one).

In the database, each register is a column of `raw_plant_register_values` or
`raw_inverter_register_values`, holding the value before its gain (`-2000` for −2.0 kW). Column
types: u16 → `integer`, s16 → `smallint`, s32 → `integer`, u32 and u64 → `bigint` (u64 as the
signed number with the same bits, so "not set" is `-1`). Text registers (model, serial, firmware)
aren't stored. So **adding a register needs a migration** (a new column, and the scaled views
recreated); `poll` refuses to start while a register in the map has no column.

## Migrations

Numbered SQL files in `src/sigen/migrations/`, each applied in its own transaction by
`sigen db migrate`, which then syncs `register_meta` with the register map. **Never edit one that
has been applied**: `migrate` refuses (exit 7) if a checksum changes. Add a new numbered file
instead. Anything that creates tables must run as the role `poll` uses, or `poll` can't write to
them.

## Fake server and data

A capture (`read --json`, `capture`) is also fake data: each register carries its raw words,
which the fake server replays. The top level is the plant; other units go in `other_units`:

```
{"unit_id": 247, "input_registers": {"30014": {"name": "plant_ess_soc", "raw": [880], …}}, …,
 "other_units": [{"unit_id": 1, "input_registers": {"30500": …}, "errors": {}}]}
```

Hand-written data can use typed values instead, e.g. `{"type": "s32", "gain": 1000, "value":
-2.0}` (see `data_io.py`).

Scenarios, one per folder (every `*.json` in a folder is merged, later files win):
- `fake_data/baseline/`: made-up values for a sunny afternoon
- `fake_data/real-2026-10-06/`: an early capture, 41 plant registers (99.9 %, charging)
- `fake_data/real-2026-10-07-midsoc/`: 55.4 %, the same 41 registers
- `fake_data/real-2026-10-07-full/`: all 137 plant registers (88 %, evening); the tests' main
  scenario
- `fake_data/real-2026-10-08-both-units/`: plant and inverter (unit 1), midday

The fake server follows the real device's rules: a read that starts on an address with no data,
or splits a register, gets exception 2; missing addresses inside a read are `0xFFFF`; a unit ID
with no data gets no reply. Writes live in memory only. Against a plant-only scenario, add
`--no-config` to commands, or a config that lists inverter unit 1 makes them wait for it (exit 3).

## Protocol documents

Sigenergy's Modbus protocol PDFs aren't in this repository (they're not ours to redistribute).
Versions V1.7 to V2.9 are collected in
[seud0nym/sigenergy2mqtt's resources](https://github.com/seud0nym/sigenergy2mqtt/tree/main/resources/protocol);
Sigenergy publishes some on its own site. The register map follows **V2.9**.

`tools/` (not part of the package) turns them into CSV, expecting them in a local, gitignored
`ext_doc/`:
- `tools/pdf_tables.py --pdf FILE --out-dir DIR [--only 5-3]`: one CSV per register table and
  appendix, from the PDF's text layer; merges rows split across pages and checks every row. Needs
  `pdfplumber`.
- `tools/extract_table.py --file MD --start-line N --end-line M [--out CSV]`: one table from a
  Markdown conversion of the PDF (e.g. by marker) → CSV, fixing common OCR slips. The PDF route is
  more reliable.
