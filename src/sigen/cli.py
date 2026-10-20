"""The `sigen` command.

    sigen read        [--ip IP | --ip-range RANGE] [--json] [CONFIG]
    sigen capture     [--ip IP | --ip-range RANGE] [--out-dir DIR] [--name-prefix PREFIX] [CONFIG]
    sigen read-json   --data-file FILE [--summary-only] [CONFIG]
    sigen scan        [--ip-range RANGE] [--json]
    sigen set         --ip IP (--remote-ems-mode MODE | --backup-soc PCT) [--expect-current V] [--apply] [--json]
    sigen undo        --ip IP [--apply] [--json]
    sigen db migrate  [CONFIG]
    sigen db status   [CONFIG]
    sigen db query    SQL [CONFIG] [--json] [--max-rows N] [--max-width CHARS]
    sigen cloud login   [CONFIG]                         (prompts for your Sigen password; handles the emailed code)
    sigen cloud refresh [CONFIG]                         (mint a fresh access token from the saved refresh token)
    sigen cloud query   [CONFIG] [--path PATH | --sn SN [--device-type N]] [--json]
    sigen poll        [--ip IP] [CONFIG] [--interval SECONDS] [--offset SECONDS] [--count N]
                      [--[no-]weather [--capture-open-meteo-weather DIR]] [--[no-]radar [--capture-radar DIR]]
                      [--[no-]alerts] [--[no-]satellite]
    sigen weather     [CONFIG] [--resolution 60 15] [--record-to-db] [--capture DIR]
    sigen radar       [CONFIG] [--record-to-db] [--capture DIR]
    sigen satellite   [CONFIG] [--day DATE | --start TIME --end TIME] [--km 200] [--daylight MINUTES | --all-day]
                      [--cube-dir DIR [--products ACM COD ACHA] [--workers 8] [--record-to-db]]
    sigen dashboard   [CONFIG] [--host 0.0.0.0] [--port 8050] [--[no-]reload]
    sigen ntfy-test   [CONFIG] [--msg TEXT] [--title TEXT] [--priority LEVEL] [--record-to-db] [--ntfy-server URL]
    sigen mac-lookup  (--ip IP | --mac MAC [--ip-range RANGE])      (Windows and Linux)
    sigen registers   [--writable]
    sigen fake-server [--host H] [--port P] [--fake-data-dir DIR] [--ignore-writes | --reject-writes] [--verbose]

CONFIG is --config FILE or --no-config. Without either, the config is the file named by
$SIGEN_CONFIG, else the nearest config.json in the current directory or above it (see
sigen/config.py). --ip always wins over the config's devices.inverter.ip. set and undo never
use the config: writes go only to an explicit --ip. poll and dashboard take their settings from
the config's "poll" / "dashboard" sections too (POLL_SETTINGS, DASHBOARD_SETTINGS): an option on
the command line wins, then the config, then the built-in default.

Exit codes are listed in sigen/errors.py and in `sigen --help`.
"""

import argparse
import dataclasses
import datetime
import decimal
import functools
import getpass
import json
import logging
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from sigen import (
    __version__,
    alerts,
    config_edit,
    daily,
    dashboard,
    db,
    gaps,
    goes,
    goes_shade,
    heater,
    netlookup,
    ntfy,
    poll,
    satellite,
    sync,
    weather,
)
from sigen.block_reader import BlockReader
from sigen.cloud import CloudClient
from sigen.config import (
    CONFIG_ENV,
    CONFIG_NAME,
    HEATER_SETTINGS,
    NTFY_SERVER,
    Cloud,
    Config,
    config_path,
    get_config,
    parse_config,
    read_config_document,
)
from sigen.control import DEFAULT_JOURNAL, Journal, WriteResult, plan_undo, set_register
from sigen.data_io import build_document, capture_filename, read_json_file, save_document
from sigen.discovery import local_hosts, parse_ip_range, scan
from sigen.errors import ConfigError, ExitCode, NotFound, PlatformNotSupported, SigenError
from sigen.fake_server import FakeSigenServer
from sigen.modbus import DEFAULT_PORT, DEFAULT_TIMEOUT, ModbusClient
from sigen.reading import (
    derive,
    format_inverter,
    format_summary,
    format_table,
    other_unit_readings,
    readings_from_document,
)
from sigen.registers import ALL_REGISTERS, INVERTER_REGISTERS, PLANT_UNIT_ID, WRITABLE

EXIT_CODE_HELP = "exit codes:\n" + "\n".join(f"  {c.value}  {c.name.lower().replace('_', ' ')}" for c in ExitCode)


def err(msg: str) -> None:
    """Print `msg` to stderr: errors, warnings and notes stay out of stdout,
    so piped output (`read --json`, `scan --json`) stays clean."""
    print(msg, file=sys.stderr)


# ---------- shared options ----------
def _network_parent() -> argparse.ArgumentParser:
    """The parent parser the device commands share: --port, --unit-id and
    --timeout, in a "connection" group."""
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("connection")
    g.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Modbus TCP port (default: {DEFAULT_PORT})")
    g.add_argument(
        "--unit-id",
        type=int,
        default=PLANT_UNIT_ID,
        help=f"Modbus unit ID of the Sigen plant (default: {PLANT_UNIT_ID})",
    )
    g.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"seconds to wait for each Modbus reply (default: {DEFAULT_TIMEOUT})",
    )
    return p


def _scan_options(p: argparse.ArgumentParser, with_ip: bool) -> None:
    """Add --ip-range and --scan-timeout to `p`, and with `with_ip` an --ip
    that excludes --ip-range."""
    g = p.add_mutually_exclusive_group()
    if with_ip:
        g.add_argument("--ip", metavar="ADDRESS", help="SigenStor IP address; skips scanning")
    g.add_argument(
        "--ip-range",
        metavar="RANGE",
        type=parse_ip_range,
        help="scan START-END (192.168.1.170-192.168.1.180), short form (192.168.1.170-180) "
        "or CIDR (192.168.1.0/24); default: this computer's /24",
    )
    p.add_argument(
        "--scan-timeout", type=float, default=0.6, help="seconds to wait per address while scanning (default: 0.6)"
    )


def _journal_option(p: argparse.ArgumentParser) -> None:
    """Add --journal, the write log `set` appends to and `undo` reads."""
    p.add_argument(
        "--journal",
        type=Path,
        default=DEFAULT_JOURNAL,
        help=f"write log, one JSON object per line (default: {DEFAULT_JOURNAL})",
    )


def _gap_arguments(p: argparse.ArgumentParser) -> None:
    """Add --since and --min-gap (db gaps, sync --fill-gaps) to `p`."""
    p.add_argument(
        "--since", default="7d", metavar="DURATION", help="look back this far, e.g. 12h or 30d (default: 7d)"
    )
    p.add_argument(
        "--min-gap",
        nargs="+",
        action="extend",
        metavar="TABLE:DURATION",
        help="a table's min gap instead of 3 x its interval, e.g. readings:30s battery_heater:2h",
    )


def _config_option(p: argparse.ArgumentParser, what: str) -> None:
    """Add --config FILE / --no-config to `p`; `what` names, in the help,
    the parts of the config this command uses."""
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--config",
        metavar="FILE",
        type=Path,
        help=f"JSON config file: {what} (default: the file named by ${CONFIG_ENV}, else the nearest "
        f"{CONFIG_NAME} in the current directory or above it, e.g. the repo's)",
    )
    g.add_argument("--no-config", action="store_true", help="use no config file, even if one would be found")


def _config(args: argparse.Namespace) -> Config:
    """The config the command line chose: an empty one for --no-config,
    else --config FILE or the one get_config finds."""
    return Config(origin="--no-config") if args.no_config else get_config(args.config)


# The settings poll and dashboard also take from the config (sigen.config.COMMAND_SETTINGS has their types):
# (setting = the option's dest, the option, its built-in default). The parser leaves them None when not given.
POLL_SETTINGS = [
    ("interval", "--interval", 60.0),
    ("offset", "--offset", 0.0),
    ("weather", "--weather", False),
    ("radar", "--radar", False),
    ("alerts", "--alerts", False),
    ("cloud", "--cloud", True),
    ("satellite", "--satellite", False),
    ("capture_open_meteo_weather", "--capture-open-meteo-weather", None),
    ("capture_radar", "--capture-radar", None),
]
DASHBOARD_SETTINGS = [("host", "--host", "0.0.0.0"), ("port", "--port", 8050), ("reload", "--reload", False)]


def _settings(
    args: argparse.Namespace, config: Config, command: str, settings: list[tuple[str, str, Any]]
) -> list[tuple[str, str, str]]:
    """Fill in each of `settings` the command line left out: from the config's section for `command`, else the
    built-in default. Returns [(option, value as text, where it came from)], one per setting."""
    section = config.commands.get(command, {})
    shown = []
    for name, option, default in settings:
        if getattr(args, name) is not None:
            source = "command line"
        elif name in section:
            setattr(args, name, section[name])
            source = "config"
        else:
            setattr(args, name, default)
            source = "default"
        value = getattr(args, name)
        if isinstance(value, bool):
            text = "on" if value else "off"
        elif isinstance(value, float):
            text = f"{value:g}"
        else:
            text = "(none)" if value is None else str(value)
        shown.append((option, text, source))
    return shown


def _print_settings(config: Config, shown: list[tuple[str, str, str]]) -> None:
    """One line per setting: the option, its value, and where it came from (to stderr, beside poll's other notes)."""
    err(f"Settings ({f'config: {config.path}' if config.path else config.origin}):")
    width = max(len(option) for option, _, _ in shown)
    vwidth = min(max(len(text) for _, text, _ in shown), 10)  # a long path only pushes its own line's source
    for option, text, source in shown:
        err(f"  {option:<{width}}  {text:<{vwidth}}  {source}")


def _connector(args: argparse.Namespace, unit: int | None = None, ip: str | None = None) -> Callable[[], ModbusClient]:
    """A ModbusClient factory for --port and --timeout, talking to `unit`
    (default: --unit-id) at `ip` (default: --ip; poll passes the address it
    follows the device to); BlockReader and set_register call it."""
    return functools.partial(
        ModbusClient, args.ip if ip is None else ip, args.port, args.unit_id if unit is None else unit, args.timeout
    )


def _inverter_units(config: Config) -> tuple[int, ...]:
    """devices.inverter.units: the inverters' Modbus unit IDs; () without
    an inverter entry."""
    inverter = config.devices.get("inverter")
    return inverter.units if inverter else ()


def _find(args: argparse.Namespace) -> list[str]:
    """Scan --ip-range (default: this computer's /24) for SigenStors; the
    IPs that answered."""
    ips = args.ip_range or local_hosts()
    return scan(ips, port=args.port, unit=args.unit_id, timeout=args.timeout, scan_timeout=args.scan_timeout)


# ---------- commands ----------
def _read_device(args: argparse.Namespace, config: Config) -> tuple[dict[str, dict], dict[int, dict[str, dict]]]:
    """Read the plant, and each inverter unit in the config. Without --ip, uses
    devices.inverter.ip from the config, or else scans (sets args.ip).
    Returns (plant readings, {inverter unit: readings})."""
    if not args.ip:
        inverter = config.devices.get("inverter")
        if inverter and inverter.ip and not args.ip_range:
            args.ip = inverter.ip
        else:
            found = _find(args)
            if not found:
                raise NotFound("no SigenStor found; check that Modbus TCP is enabled, or pass --ip")
            args.ip = found[0]
    plant = BlockReader(_connector(args)).read_all()
    inverters = {
        unit: BlockReader(_connector(args, unit), INVERTER_REGISTERS).read_all() for unit in _inverter_units(config)
    }
    return plant, inverters


def cmd_read(args: argparse.Namespace) -> int:
    """`sigen read`: read the plant and inverters once and print the
    summary, or with --json every register as a dump document."""
    config = _config(args)
    readings, inverters = _read_device(args, config)
    if args.json:
        doc = build_document(
            readings, ip=args.ip, port=args.port, unit=args.unit_id, derived=derive(readings), other_units=inverters
        )
        print(json.dumps(doc, indent=2, ensure_ascii=False))
    else:
        print(format_summary(args.ip, readings, config=config, inverters=inverters))
    return ExitCode.OK


def cmd_capture(args: argparse.Namespace) -> int:
    """`sigen capture`: read like `read --json`, save it to a new
    timestamped file in --out-dir, and print the summary. ERROR if the file
    exists already or can't be written."""
    config = _config(args)
    readings, inverters = _read_device(args, config)
    doc = build_document(
        readings, ip=args.ip, port=args.port, unit=args.unit_id, derived=derive(readings), other_units=inverters
    )
    out_dir = Path(args.out_dir)
    path = out_dir / capture_filename(doc, args.name_prefix)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        save_document(doc, path, overwrite=False)
    except FileExistsError:
        err(f"error: {path} already exists; not overwriting it")
        return ExitCode.ERROR
    except OSError as e:
        err(f"error: can't write {path}: {e}")
        return ExitCode.ERROR
    print(format_summary(args.ip, readings, config=config, inverters=inverters))
    print(f"\nSaved {path}")
    return ExitCode.OK


def cmd_read_json(args: argparse.Namespace) -> int:
    """`sigen read-json`: print a saved dump's summary (in local time) and,
    unless --summary-only, a table per unit. NotFound if the file can't be
    read, ERROR if it isn't JSON."""
    config = _config(args)
    path = Path(args.data_file)
    try:
        doc = read_json_file(path)
    except OSError as e:
        raise NotFound(f"can't read {path}: {e}") from e
    except ValueError as e:  # bad JSON, or bytes that aren't UTF-8/UTF-16
        err(f"error: {path} isn't valid JSON: {e}")
        return ExitCode.ERROR
    if not isinstance(doc, dict):
        err(f"error: {path} isn't a sigen dump: expected a JSON object, got a {type(doc).__name__}")
        return ExitCode.ERROR
    readings = readings_from_document(doc)
    source = doc.get("source", {})
    read_at = source.get("read_at")
    when = None
    if read_at:
        try:
            when = datetime.datetime.fromisoformat(read_at)
        except ValueError:
            pass
    ip = source.get("ip", "?")
    title = f"📄 {path.name}: SigenStor at {ip}"
    if when:
        local = when.astimezone()
        title += f", read {local:%Y-%m-%d %H:%M:%S %Z} ({read_at})"
    print(format_summary(ip, readings, now=when, title=title, config=config))
    d = derive(readings, when)
    if "device_clock_local" in d:
        print(
            f"  Device clock:      {d['device_clock_local']} (local; {d['device_clock_skew_hours_vs_utc']:+g} h vs UTC)"
        )
    inverters = other_unit_readings(doc)
    for unit, unit_readings in sorted(inverters.items()):
        print("\n" + "\n".join(format_inverter(unit, unit_readings)))
    if not args.summary_only:
        print(
            format_table(
                readings, config, title=f"Plant (unit {doc.get('unit_id', PLANT_UNIT_ID)})" if inverters else ""
            )
        )
        for unit, unit_readings in sorted(inverters.items()):
            print(format_table(unit_readings, config, title=f"Inverter (unit {unit})"))
    return ExitCode.OK


def cmd_scan(args: argparse.Namespace) -> int:
    """`sigen scan`: print the IP of each SigenStor found; NOT_FOUND if
    none answered."""
    found = _find(args)
    if args.json:
        print(json.dumps({"found": found, "port": args.port, "unit_id": args.unit_id}))
    else:
        for ip in found:
            print(ip)
    if not found:
        err("No SigenStor found.")
        return ExitCode.NOT_FOUND
    return ExitCode.OK


SYMBOL = {"written": "✓", "unchanged": "✓", "dry_run": "…"}


def _report(result: WriteResult, args: argparse.Namespace, journal: Journal | None) -> int:
    """Finish `set` / `undo`: journal the result (with --apply; a journal
    that can't be written is only a warning), print it as JSON or as
    before/target/after lines, and return its exit code."""
    context = {"ip": args.ip, "port": args.port, "unit": args.unit_id, "command": args.command}
    entry = result.to_dict(**context)
    if journal is not None and args.apply:
        try:
            journal.append(entry)
        except OSError as e:
            err(f"WARNING: couldn't write the journal at {journal.path}: {e}")
    if args.json:
        print(json.dumps(entry, indent=2, ensure_ascii=False))
    else:
        r = result.register
        print(f"{r.name} ({r.table} {r.address})")
        for key in ("before", "target", "after"):
            if entry[key] is not None:
                print(f"  {key + ':':8} {entry[key]}")
        print(f"  {SYMBOL.get(result.outcome, '✗')} {result.message}")
    return int(result.exit_code)


def cmd_set(args: argparse.Namespace) -> int:
    """`sigen set`: write the one register option given, through
    control.set_register (a dry run without --apply). REFUSED if the value
    or --expect-current doesn't parse."""
    chosen = [(reg, getattr(args, reg.name)) for reg in WRITABLE if getattr(args, reg.name) is not None]
    reg, text = chosen[0]
    try:
        value = reg.parse_value(text)
        expect = reg.encode_unchecked(reg.parse_value(args.expect_current)) if args.expect_current is not None else None
    except ValueError as e:
        err(str(e))
        return ExitCode.REFUSED
    result = set_register(_connector(args), reg, value, apply=args.apply, expect_words=expect)
    return _report(result, args, Journal(args.journal))


def cmd_undo(args: argparse.Namespace) -> int:
    """`sigen undo`: restore the value from before the last journaled write
    to this --ip/--port/--unit-id, expecting the value that write left.
    NotFound if there's nothing to undo."""
    journal = Journal(args.journal)
    plan = plan_undo(journal, args.ip, args.port, args.unit_id)
    if plan is None:
        raise NotFound(f"nothing to undo for {args.ip}:{args.port} in {journal.path}")
    entry, reg, restore_words, expect = plan
    print(
        f"Undoing {entry['id']} ({entry['ts']}): {reg.name} {entry['after']} → {entry['before']}",
        file=sys.stderr if args.json else sys.stdout,
    )
    result = set_register(
        _connector(args), reg, words=restore_words, apply=args.apply, expect_words=expect, undo_of=entry["id"]
    )
    return _report(result, args, journal)


def cmd_db_migrate(args: argparse.Namespace) -> int:
    """`sigen db migrate`: apply the pending migrations, printing each."""
    url = _config(args).require_database_url()
    conn = db.connect(url)
    try:
        applied = db.migrate(conn, log=lambda msg: print(f"  {msg}"))
    finally:
        conn.close()
    print(
        f"Applied {len(applied)} migration(s); schema is up to date."
        if applied
        else "No migrations pending; schema is up to date."
    )
    return ExitCode.OK


def cmd_db_status(args: argparse.Namespace) -> int:
    """`sigen db status`: the schema version, row counts, the latest of
    each kind of row, and each table's size and growth. PRECONDITION if
    migrations are pending or the schema isn't ready (`poll` would refuse)."""
    config = _config(args)
    url = config.require_database_url()
    conn = db.connect(url)
    try:
        s = db.status(conn)
    finally:
        conn.close()
    print(f"Config:        {config.origin}")
    print(f"Database:      {db.redact(url)}")
    applied = s["migrations_applied"]
    print(f"Migrations:    {len(applied)} applied" + (f" (latest {applied[-1]:04d})" if applied else ""))
    if s["migrations_pending"]:
        print(f"Pending:       {', '.join(s['migrations_pending'])}  → run `sigen db migrate`")
        return ExitCode.PRECONDITION
    if not s["ready"]:
        print(f"Not ready:     {s['problem']}")
        return ExitCode.PRECONDITION
    glitched = s.get("glitched_readings") or {}
    print(
        f"Readings:      {s['readings']:,} ({s['failed_readings']:,} failed"
        + (
            f", {sum(glitched.values()):,} glitched: " + ", ".join(f"{n:,} {rule}" for rule, n in glitched.items())
            if glitched
            else ""
        )
        + ")"
    )
    if "value_rows" in s:
        print(
            "Value rows:    "
            + ", ".join(f"{n:,} {device}" for device, n in s["value_rows"].items())
            + " (one per successful reading)"
        )
    last = s["last_reading"]
    if last:
        rid, started, ended, error = last
        print(
            f"Last reading:  {rid} at {started.astimezone(datetime.UTC):%Y-%m-%d %H:%M:%S.%f}Z"
            + (f"  FAILED: {error}" if error else "")
        )
    utc = lambda t: f"{t.astimezone(datetime.UTC):%Y-%m-%d %H:%M}Z"
    if "open_meteo" in s:
        total, failed, hourly, quarter, last_valid, last_fetch = s["open_meteo"]
        print(
            f"Open-Meteo:    {total:,} rows ({failed:,} failed): {hourly:,} hourly, {quarter:,} every 15 min"
            + (f"; latest period {utc(last_valid)}, last fetch {utc(last_fetch)}" if last_fetch else "")
        )
    if "radar" in s:
        good, failed, last_frame, last_fetch = s["radar"]
        print(
            f"Radar frames:  {good:,} ({failed:,} failed fetches)"
            + (f"; latest frame {utc(last_frame)}" if last_frame else "")
            + (f", last fetch {utc(last_fetch)}" if last_fetch else "")
        )
    if "alerts" in s:
        sent, failed, suppressed, last_alert = s["alerts"]
        print(
            f"Alerts:        {sent:,} sent, {failed:,} failed, {suppressed:,} suppressed"
            + (f"; last {utc(last_alert)}" if last_alert else "")
        )
    if "daily_energy" in s:
        days, first_day, last_day = s["daily_energy"]
        print(
            f"Daily energy:  {days:,} day(s) stored"
            + (f", {first_day} to {last_day}" if days else "")
            + " (`sigen db daily` adds finished days; `sigen poll` does too)"
        )
    tables = s["tables"]
    print(
        "Table sizes:   data + indexes + TOAST; growth = average row size × rows from the last 24 h; "
        "a year = 365 × a day"
    )
    width = max([len(t.name) for t in tables] + [len("all tables")])
    for t in tables:
        if t.bytes_per_day is None:
            growth = f"no time column ({'' if t.rows_exact else '~'}{t.rows:,} rows)"
        else:
            growth = (
                f"{_growth(t.bytes_per_day)}  ({t.rows_last_day:,} of {t.rows:,} rows "
                f"in the last 24 h, by {t.time_column})"
            )
        print(f"  {t.name:<{width}}  {_size(t.total_bytes):>9}  {growth}")
    per_day = sum(t.bytes_per_day for t in tables if t.bytes_per_day is not None)
    # The tables' sum, not pg_database_size: that checks every file in the database's directory, slow where file
    # checks are (Docker on Windows: ~0.6 ms each), and slower still for a while after tables are dropped.
    print(f"  {'all tables':<{width}}  {_size(sum(t.total_bytes for t in tables)):>9}  {_growth(per_day)}")
    return ExitCode.OK


def _growth(per_day: float) -> str:
    """Per day, and per year as simply 365 days of it."""
    return f"{'+' + _size(per_day) + '/day':>14}  {'+' + _size(per_day * 365) + '/year':>15}"


def _size(n: float) -> str:
    """Decimal units: 1 MB = 10⁶ bytes."""
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= scale:
            return f"{n / scale:.2f} {unit}" if unit == "GB" else f"{n / scale:.1f} {unit}"
    return f"{n:.0f} B"


def _cell(value: Any, max_width: int) -> str:
    """One `db query` table cell: NULL for None, CR/LF/tab escaped, cut
    with … past max_width (0 = never)."""
    text = "NULL" if value is None else str(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return text[: max_width - 1] + "…" if max_width and len(text) > max_width else text


def format_query_table(columns: list[str], rows: list[tuple], max_width: int = 0) -> str:
    """psql-style: a header, a rule, one line per row; numbers right-aligned, NULL spelled out,
    line breaks shown as \\n, cells longer than max_width cut with … (0 = never)."""
    cells = [[_cell(v, max_width) for v in row] for row in rows]
    numeric = [
        all(
            isinstance(row[i], (int, float, decimal.Decimal)) and not isinstance(row[i], bool)
            for row in rows
            if row[i] is not None
        )
        and any(row[i] is not None for row in rows)
        for i in range(len(columns))
    ]
    widths = [max([len(name)] + [len(r[i]) for r in cells]) for i, name in enumerate(columns)]
    lines = [
        " | ".join(name.ljust(w) for name, w in zip(columns, widths)).rstrip(),
        "-+-".join("-" * w for w in widths),
    ]
    for r in cells:
        lines.append(" | ".join(c.rjust(w) if num else c.ljust(w) for c, w, num in zip(r, widths, numeric)).rstrip())
    return "\n".join(lines)


def _site_tz(config: Config) -> datetime.tzinfo:
    """The config's site timezone, else this computer's."""
    return ZoneInfo(config.site.timezone) if config.site else datetime.datetime.now().astimezone().tzinfo


def _poll_interval(config: Config) -> float:
    """The seconds between this machine's polls: the config's poll.interval, else poll's default."""
    default = next(d for name, _, d in POLL_SETTINGS if name == "interval")
    return float(config.commands.get("poll", {}).get("interval", default))


def _gap_options(args: argparse.Namespace) -> tuple[datetime.timedelta, dict[str, datetime.timedelta]]:
    """--since and --min-gap, read. ValueError (with the reason) for a bad one."""
    return gaps.parse_duration(args.since), gaps.parse_min_gaps(args.min_gap or [])


def cmd_db_gaps(args: argparse.Namespace) -> int:
    """`sigen db gaps`: each table's stretches without good rows over the last --since, each over its min gap
    (default 3 x its interval), with the failed attempts inside them; --json for a list. Read-only."""
    config = _config(args)
    url = config.require_database_url()
    try:
        since, min_gaps = _gap_options(args)
    except ValueError as e:
        err(f"error: {e}")
        return ExitCode.USAGE
    tz = _site_tz(config)
    end = datetime.datetime.now(datetime.UTC)
    start = end - since
    tables = args.table or None
    with db.read_only(url) as conn:
        found = gaps.find_gaps(conn, config, start, end, _poll_interval(config), tables, min_gaps)
        days = gaps.missing_days(conn, start, end, tz) if tables is None or "daily_energy" in tables else None
    if args.json:
        print(json.dumps(_gaps_json(found, days, start, end), indent=1))
        return ExitCode.OK

    def local(t: datetime.datetime) -> str:
        """A time as the listing shows it, in the site's zone: '10-18 20:01:45'."""
        return f"{t.astimezone(tz):%m-%d %H:%M:%S}"

    print(f"Gaps from {start.astimezone(tz):%Y-%m-%d %H:%M} to {end.astimezone(tz):%Y-%m-%d %H:%M} ({tz}):")
    for sg in found:
        s = sg.series
        head = f"{s.name}: every {gaps.span_text(s.interval)}, gaps over {gaps.span_text(sg.min_gap)}"
        head += ", daylight only" if s.daylight_only else ""
        if not sg.gaps:
            print(f"{head}: {'none' if sg.rows else 'no rows in the window'}")
            continue
        print(f"{head}:")
        for g in sg.gaps:
            to = f"{local(g.end)} (now)" if g.open_ended else local(g.end)
            why = f"{g.failed} failed: {g.error}" if g.failed else "no attempts stored"
            print(f"  {local(g.start)} -> {to}  {gaps.span_text(g.length):>7}  {why}")
    if days is not None:
        missing = ", ".join(f"{a:%m-%d}" if a == b else f"{a:%m-%d} to {b:%m-%d}" for a, b in days)
        print(f"daily_energy: a row per finished local day; missing: {missing or 'none'}")
    return ExitCode.OK


def _gaps_json(
    found: list[gaps.SeriesGaps],
    days: list[tuple[datetime.date, datetime.date]] | None,
    start: datetime.datetime,
    end: datetime.datetime,
) -> dict:
    """`db gaps --json`: the window, each series with its gaps (UTC ISO times), and daily_energy's missing days."""
    return {
        "start": sync.iso(start),
        "end": sync.iso(end),
        "series": [
            {
                "table": sg.series.table,
                "series": sg.series.name,
                "interval_s": sg.series.interval.total_seconds(),
                "min_gap_s": sg.min_gap.total_seconds(),
                "daylight_only": sg.series.daylight_only,
                "rows": sg.rows,
                "gaps": [
                    {
                        "start": sync.iso(g.start),
                        "end": sync.iso(g.end),
                        "seconds": g.length.total_seconds(),
                        "open_ended": g.open_ended,
                        "failed": g.failed,
                        "error": g.error,
                    }
                    for g in sg.gaps
                ],
            }
            for sg in found
        ],
        "daily_energy_missing": None if days is None else [[a.isoformat(), b.isoformat()] for a, b in days],
    }


def cmd_db_daily(args: argparse.Namespace) -> int:
    """`sigen db daily`: store each finished local day's energy in
    daily_energy; with --from / --to, recompute every day in that range."""
    config = _config(args)
    url = config.require_database_url()
    conn = db.connect(url)
    try:
        db.check_ready(conn)
        days = daily.update(
            conn, _site_tz(config), args.first, args.last, site=config.site, array=config.array, log=print
        )
    finally:
        conn.close()
    if days:
        print(f"Stored {len(days)} day(s) in daily_energy.")
    else:
        print(
            "Nothing to store: "
            + ("no finished day in that range." if args.first or args.last else "every finished day is stored already.")
        )
    return ExitCode.OK


def cmd_db_query(args: argparse.Namespace) -> int:
    """`sigen db query`: run SQL read-only and print each statement's rows
    as a table, or all of them as one JSON list; truncation noted on
    stderr."""
    url = _config(args).require_database_url()
    conn = db.connect(url)
    try:
        results = db.query(conn, args.sql, max_rows=args.max_rows)
    finally:
        conn.close()
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "status": r.status,
                        "columns": r.columns,
                        "rows": [list(row) for row in r.rows],
                        "truncated": r.truncated,
                    }
                    for r in results
                ],
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
    for n, r in enumerate(results):
        if not args.json:
            if n:
                print()
            if r.columns is None:
                print(r.status)
            else:
                print(format_query_table(r.columns, r.rows, args.max_width))
                print(f"({len(r.rows)} row{'' if len(r.rows) == 1 else 's'})")
        if r.truncated:
            err(f"note: stopped after {args.max_rows} rows; --max-rows 0 prints them all")
    return ExitCode.OK


def _poll_arg_error(args: argparse.Namespace) -> str | None:
    """The first impossible combination of poll options, or None."""
    if not 0 <= args.offset < args.interval:
        return f"--offset must be at least 0 and less than --interval ({args.interval:g})"
    if args.capture_open_meteo_weather and not args.weather:
        return "--capture-open-meteo-weather needs --weather"
    if args.capture_radar and not args.radar:
        return "--capture-radar needs --radar"
    return None


def _poll_ip(args: argparse.Namespace, config: Config) -> str | None:
    """The inverter's IP, from --ip or the config, resolved (readings.ip_addr
    is INET, so store the address itself); None, after saying why, if
    there's no address or it won't resolve."""
    inverter = config.devices.get("inverter")
    target = args.ip or (inverter.ip if inverter else None)
    if not target:
        err(f"error: give --ip, or set devices.inverter.ip in the config ({config.origin})")
        return None
    try:
        return socket.gethostbyname(target)
    except OSError as e:
        err(f"error: can't resolve {target}: {e}")
        return None


def _mac_follower(mac: str | None) -> Callable[[str], str | None] | None:
    """poll.run's rediscover callback: keep the IP while the inverter's MAC
    still answers there, else sweep the LAN for it. None without a MAC, or
    where netlookup can't work, which it warns about now, not hours later
    at the first outage."""
    if not mac:
        return None
    try:
        netlookup.require_supported()
    except PlatformNotSupported as e:
        err(
            f"warning: {e}; polling without following the inverter's MAC (devices.inverter.mac), so if "
            f"its IP changes, polling keeps failing until it's restarted with the new IP"
        )
        return None

    def rediscover(current_ip: str) -> str | None:
        if netlookup.mac_for_ip(current_ip) == mac:
            return current_ip
        return netlookup.find_ip_for_mac(mac, local_hosts())

    return rediscover


def _alert_watcher(
    args: argparse.Namespace, config: Config, url: str
) -> tuple[alerts.Watcher | None, weather.Store | None]:
    """With --alerts, the watcher and the sender's own store connection,
    which the caller closes; else (None, None)."""
    if not args.alerts:
        return None, None
    alert_config = config.require_alerts()
    store = weather.Store(url, err)
    tz = ZoneInfo(config.site.timezone) if config.site else None  # else this computer's
    sender = alerts.Sender(alert_config, store, out=print, err=err)
    return alerts.Watcher(sender, tz=tz, out=print, err=err), store


def _poll_banner(
    args: argparse.Namespace, ip: str, units: dict[int, str], url: str, mac: str | None, cloud_heater: bool
) -> str:
    """The one-line "Polling ..." summary: what, how often, into where, and
    the extras that are on. mac is the MAC being followed, if any."""
    extras = [
        what
        for what, on in (
            ("Open-Meteo hourly and every 15 min", args.weather),
            ("radar every 6 min", args.radar),
            ("alerts" + ("" if args.weather else " (no --weather: outside air unknown)"), args.alerts),
            ("the batteries' cloud heater status", cloud_heater),
            ("NOAA satellite cloud shade every 10 min", args.satellite),
        )
        if on
    ]
    return (
        f"Polling {ip}:{args.port} units {', '.join(f'{u} ({d})' for u, d in units.items())} every "
        f"{args.interval:g}s"
        + (f", {args.offset:g}s past the boundary" if args.offset else "")
        + f" into {db.redact(url)} (Ctrl+C to stop)"
        + (f"; following MAC {mac}" if mac else "")
        + (f"; also {' and '.join(extras)}" if extras else "")
    )


def cmd_poll(args: argparse.Namespace) -> int:
    """`sigen poll`: settle the settings (command line, config, default),
    check them, set up MAC following, alerts, the background jobs and the
    cloud heater poller, then hand over to sigen.poll.run. USAGE for a bad
    combination or an address that won't resolve."""
    config = _config(args)
    shown = _settings(args, config, "poll", POLL_SETTINGS)
    heater_settings = config.commands.get("poll", {}).get("heater", {})
    if args.cloud and config.cloud is not None:  # the heater job's cadence, config only (poll.heater)
        shown += [
            (
                f"poll.heater.{name}",
                f"{heater_settings.get(name, default):g}",
                "config" if name in heater_settings else "default",
            )
            for name, (default, _, _) in HEATER_SETTINGS.items()
        ]
    _print_settings(config, shown)
    if problem := _poll_arg_error(args):
        err(f"error: {problem}")
        return ExitCode.USAGE
    url = config.require_database_url()
    ip = _poll_ip(args, config)
    if ip is None:
        return ExitCode.USAGE
    inverter = config.devices.get("inverter")
    mac = inverter.mac if inverter else None
    rediscover = _mac_follower(mac)
    units = {args.unit_id: "plant", **{u: "inverter" for u in _inverter_units(config)}}
    watcher, alert_store = _alert_watcher(args, config, url)
    store = None
    try:
        background, store = _background(args, config, url, watcher)
        cloud_heater = None
        if args.cloud and config.cloud is not None:
            cloud_heater = heater.HeaterPoller.from_config(config.cloud, url, heater_settings, out=print, err=err)
        shade = satellite.Filler(config.require_site(), url, out=print, err=err) if args.satellite else None
        following = mac if rediscover else None
        print(_poll_banner(args, ip, units, url, following, cloud_heater is not None), file=sys.stderr)
        connector_for = lambda address, unit: _connector(args, unit, ip=address)
        return poll.run(
            connector_for=connector_for,
            ip=ip,
            units=units,
            db_url=url,
            interval=args.interval,
            offset=args.offset,
            count=args.count,
            rediscover=rediscover,
            background=background,
            watcher=watcher,
            cloud_heater=cloud_heater,
            satellite=shade,
        )
    finally:
        for s in (store, alert_store):
            if s is not None:
                s.close()


def _background(
    args: argparse.Namespace, config: Config, url: str, watcher: alerts.Watcher | None = None
) -> tuple[weather.Background, weather.Store | None]:
    """poll's background jobs, one at a time in their own thread: (Background, Store or None).
    Always: storing each finished day in daily_energy (sigen.daily), checked every 10 minutes, with its own
    connection. With --weather / --radar: the fetches, sharing a Store; with a watcher (--alerts), each 15-minute
    Open-Meteo row's air temperature goes to it too."""
    tz = _site_tz(config)

    def daily_energy() -> None:
        conn = db.connect(url)
        try:
            daily.update(conn, tz, site=config.site, array=config.array, log=print)
        finally:
            conn.close()

    jobs = [weather.Job("daily energy", daily.CHECK_EVERY, daily_energy)]
    if not (args.weather or args.radar):
        return weather.Background(jobs, err), None
    site = config.require_site()
    store = weather.Store(url, err)
    if args.weather:
        for resolution in weather.RESOLUTIONS:

            def fetch(resolution: int = resolution) -> None:
                row, stored = weather.record_open_meteo(site, resolution, store, args.capture_open_meteo_weather)
                print(
                    weather.summarize_open_meteo(row) + ("" if stored else "  (not stored)"),
                    file=sys.stderr if row.error else sys.stdout,
                )
                if watcher is not None and resolution == 15 and not row.error:
                    watcher.add_air(row.valid_at, row.values.get("temperature_2m"))

            jobs.append(weather.Job(f"Open-Meteo {resolution} min", resolution * 60, fetch))
    if args.radar:

        def radar() -> None:
            rows, added = weather.record_radar(site, store, args.capture_radar)
            print(weather.summarize_radar(rows, added), file=sys.stderr if any(r.error for r in rows) else sys.stdout)

        jobs.append(weather.Job("radar", weather.RADAR_INTERVAL, radar))
    return weather.Background(jobs, err), store


def _weather_store(config: Config) -> weather.Store:
    """For `weather` / `radar --record-to-db`: a Store, after checking the schema is ready."""
    url = config.require_database_url()
    conn = db.connect(url)
    try:
        db.check_ready(conn)
    finally:
        conn.close()
    return weather.Store(url, err)


def cmd_weather(args: argparse.Namespace) -> int:
    """`sigen weather`: fetch each --resolution from Open-Meteo once and
    print it, storing it with --record-to-db. DATABASE if a row wasn't
    stored, else CONNECTION if a fetch failed."""
    config = _config(args)
    site = config.require_site()
    store = _weather_store(config) if args.record_to_db else None
    code = ExitCode.OK
    try:
        for resolution in args.resolution:
            row, stored = weather.record_open_meteo(site, resolution, store, args.capture)
            print(weather.format_open_meteo(row))
            if store is not None:
                print("  stored" if stored else "  NOT stored")
                if not stored:
                    code = ExitCode.DATABASE
            if row.error and code == ExitCode.OK:
                code = ExitCode.CONNECTION
    finally:
        if store is not None:
            store.close()
    return code


def cmd_radar(args: argparse.Namespace) -> int:
    """`sigen radar`: fetch the radar at the site once and print it; with
    --record-to-db, store every frame not stored yet. DATABASE if nothing
    could be stored, else CONNECTION if a fetch failed."""
    config = _config(args)
    site = config.require_site()
    store = _weather_store(config) if args.record_to_db else None
    try:
        rows, added = weather.record_radar(site, store, args.capture)
    finally:
        if store is not None:
            store.close()
    for row in rows:
        print(weather.format_radar(row))
    if not rows:
        print("no new radar frames")
    if store is not None:
        print("NOT stored" if added is None else f"{added} frame(s) stored")
        if added is None:
            return ExitCode.DATABASE
    return ExitCode.CONNECTION if any(r.error for r in rows) else ExitCode.OK


def _local_time(text: str, timezone: str, what: str) -> datetime.datetime:
    """`text` (ISO date or date and time) as wall-clock time in `timezone`,
    an IANA name; a time written with its own offset keeps that instant.
    ArgumentTypeError naming the option `what` if it won't parse."""
    try:
        when = datetime.datetime.fromisoformat(text)
        if when.tzinfo is not None:
            return when.astimezone(ZoneInfo(timezone))
        return when.replace(tzinfo=ZoneInfo(timezone))
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"{what} {text!r} isn't a date/time like 2026-10-10 or 2026-10-10 13:00"
        ) from e


def cmd_satellite(args: argparse.Namespace) -> int:
    """`sigen satellite`: store the shade of every GOES scan of a local day (or --start to --end) that the
    satellite_shade table is missing, fetched into memory (sigen.satellite; PRECONDITION if another run has the
    database, CONNECTION if NOAA couldn't be reached). With --cube-dir instead: add the scans to per-day cubes there,
    and with --record-to-db also store the shade from them."""
    config = _config(args)
    site = config.require_site()
    if args.cube_dir is None and (args.products is not None or args.workers is not None):
        err("error: --products and --workers go with --cube-dir (without it, each scan takes all three products)")
        return ExitCode.USAGE
    try:
        if args.start or args.end:
            if not (args.start and args.end) or args.day:
                err("error: give --start and --end together, or --day")
                return ExitCode.USAGE
            start = _local_time(args.start, site.timezone, "--start")
            end = _local_time(args.end, site.timezone, "--end")
        else:
            day = (
                _local_time(args.day, site.timezone, "--day")
                if args.day
                else datetime.datetime.now(ZoneInfo(site.timezone))
            ).replace(hour=0, minute=0, second=0, microsecond=0)
            start, end = day, day + datetime.timedelta(days=1)
    except argparse.ArgumentTypeError as e:
        err(f"error: {e}")
        return ExitCode.USAGE
    if end <= start:
        err("error: --end must be after --start")
        return ExitCode.USAGE
    daylight = None if args.all_day else args.daylight
    if args.cube_dir is None:
        return _fill_satellite(args, config, start, end, daylight)
    return _satellite_cubes(args, config, start, end, daylight)


def _fill_satellite(
    args: argparse.Namespace,
    config: Config,
    start: datetime.datetime,
    end: datetime.datetime,
    daylight: float | None,
) -> int:
    """`sigen satellite` without --cube-dir: the scans satellite_shade is missing, into it (sigen.satellite.fill)."""
    site = config.require_site()
    err(
        f"GOES-{args.satellite} shade ±{args.km:g} km around {site.latitude}, {site.longitude}, "
        f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} {site.timezone}"
        + ("" if daylight is None else f", within {daylight:g} min of daylight")
        + ", the scans the database is missing"
    )
    conn = db.connect(config.require_database_url())
    try:
        db.check_ready(conn)
        result = satellite.fill(
            conn,
            satellite.Listings(args.satellite, args.sector),
            site,
            start,
            end,
            km=args.km,
            daylight_minutes=daylight,
            log=print,
        )
    finally:
        conn.close()
    line = satellite.summarize(result)
    if result.locked_out:
        err(f"error: {line}: another `sigen satellite` or `sigen poll` is filling this database's satellite_shade")
        return ExitCode.PRECONDITION
    print(line, file=sys.stderr if result.error or result.failed else sys.stdout)
    return ExitCode.CONNECTION if result.error else ExitCode.OK


def _satellite_cubes(
    args: argparse.Namespace,
    config: Config,
    start: datetime.datetime,
    end: datetime.datetime,
    daylight: float | None,
) -> int:
    """`sigen satellite --cube-dir DIR`: the scans into per-day cubes there (sigen.goes.build_cubes); with
    --record-to-db, also each scan's shade from the cubes, or the run's failure (then CONNECTION)."""
    site = config.require_site()
    cube_dir = config.cube_dir() if args.cube_dir is True else args.cube_dir  # --cube-dir alone: the config's
    products = args.products or list(goes.PRODUCTS)
    err(
        f"GOES-{args.satellite} {', '.join(products)} ±{args.km:g} km around {site.latitude}, {site.longitude}, "
        f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} {site.timezone}"
        + ("" if daylight is None else f", within {daylight:g} min of daylight")
        + f", into {cube_dir}"
    )
    conn = db.connect(config.require_database_url()) if args.record_to_db else None
    try:
        if conn is not None:
            db.check_ready(conn)  # fail early if the schema isn't ready
        pid_file = goes.claim_pid_file(cube_dir)  # another run still going: exit 7
        error = None
        try:
            goes.build_cubes(
                lat=site.latitude,
                lon=site.longitude,
                timezone=site.timezone,
                start=start,
                end=end,
                cube_dir=cube_dir,
                products=products,
                km=args.km,
                satellite=args.satellite,
                sector=args.sector,
                workers=args.workers or goes.DEFAULT_WORKERS,
                daylight_minutes=daylight,
                log=print,
            )
            series = (
                goes_shade.shade_between(
                    cube_dir, site.latitude, site.longitude, site.timezone, start, end, args.satellite
                )
                if conn is not None
                else None
            )
        except Exception as e:  # a build / shade failure: record it (if storing), then report
            if conn is None:
                raise
            error, series = str(e), None
        finally:
            goes.release_pid_file(pid_file)
        if conn is not None:
            added = db.insert_satellite_shade(conn, datetime.datetime.now(datetime.UTC), series, error=error)
            if error is not None:
                err(f"error: satellite run failed: {error}")
                return ExitCode.CONNECTION
            print(f"{added} scan(s) stored")
        return ExitCode.OK
    finally:
        if conn is not None:
            conn.close()


def _ntfy_server(text: str) -> str:
    """argparse type for --ntfy-server: an http(s) base URL, returned
    without its trailing slash."""
    if not NTFY_SERVER.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r} isn't a server URL like https://ntfy.sh or http://localhost:8051")
    return text.rstrip("/")


def cmd_ntfy_test(args: argparse.Namespace) -> int:
    """`sigen ntfy-test`: post one message to the config's ntfy topic,
    storing the attempt in alert_log with --record-to-db. CONNECTION if it
    didn't get through."""
    config = _config(args)
    alerts = config.require_alerts()
    if args.ntfy_server:
        alerts = dataclasses.replace(alerts, ntfy_server=args.ntfy_server)
    conn = None
    if args.record_to_db:  # check the database before sending, not after
        conn = db.connect(config.require_database_url())
    try:
        if conn is not None:
            db.check_ready(conn)
        attempt = ntfy.post(alerts, args.msg, title=args.title, priority=args.priority, kind="test")
        if attempt.ok:
            print(f"sent to {attempt.where}" + (f" (id {attempt.ntfy_id})" if attempt.ntfy_id else ""))
        else:
            err(f"error: {attempt.problem()}")
        if conn is not None:
            print(f"stored as alert_log {db.insert_alert(conn, attempt)}")
    finally:
        if conn is not None:
            conn.close()
    return ExitCode.OK if attempt.ok else ExitCode.CONNECTION


def _lan_address() -> str | None:
    """This computer's address on the local network (the route a packet would take; nothing is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed anywhere real
            return s.getsockname()[0]
    except OSError:
        return None


def cmd_dashboard(args: argparse.Namespace) -> int:
    """`sigen dashboard`: serve the Flask dashboard until Ctrl+C, settings
    from the command line, else the config's dashboard section, else the
    defaults."""
    config = _config(args)
    _settings(args, config, "dashboard", DASHBOARD_SETTINGS)
    app = dashboard.create_app(config)
    # The reloader restarts on .py changes only, and Flask caches templates unless debugging: with --reload, re-read
    # a template when it changes, so editing one shows on the next page load.
    app.config["TEMPLATES_AUTO_RELOAD"] = args.reload
    where = [f"http://127.0.0.1:{args.port}/"]
    if args.host == "0.0.0.0" and (lan := _lan_address()):
        where.append(f"http://{lan}:{args.port}/  (other devices on this network)")
    err(f"Dashboard from {config.origin}: " + "  ".join(where) + "  (Ctrl+C to stop)")
    # Never debug=True: Werkzeug's debugger runs any code typed into an error page, and this listens on the LAN.
    app.run(host=args.host, port=args.port, debug=False, use_debugger=False, use_reloader=args.reload, threaded=True)
    return ExitCode.OK


def cmd_mac_lookup(args: argparse.Namespace) -> int:
    """`sigen mac-lookup`: print --ip's MAC, or the address in --ip-range
    that has --mac. NotFound if nothing answered."""
    if args.ip:
        mac = netlookup.mac_for_ip(socket_ip(args.ip))
        if mac is None:
            raise NotFound(f"nothing at {args.ip} answered ARP")
        print(mac)
        return ExitCode.OK
    try:
        mac = netlookup.normalize_mac(args.mac)
    except ValueError as e:
        err(f"error: {e}")
        return ExitCode.USAGE
    ips = args.ip_range or local_hosts()
    err(f"Asking {len(ips)} address(es) for {mac}…")
    found = netlookup.find_ip_for_mac(mac, ips)
    if found is None:
        raise NotFound(f"no device with MAC {mac} answered")
    print(found)
    return ExitCode.OK


def socket_ip(name: str) -> str:
    """`name` (a host name or IPv4 address) resolved to an IPv4 address;
    NotFound if it won't resolve."""
    try:
        return socket.gethostbyname(name)
    except OSError as e:
        raise NotFound(f"can't resolve {name}: {e}") from e


def _positive(kind: Callable[[str], float], minimum: float) -> Callable[[str], float]:
    """An argparse type: `kind` (int or float) applied to the text,
    refused below `minimum`."""

    def parse(text: str) -> float:
        try:
            value = kind(text)
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"'{text}' isn't a number") from e
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}")
        return value

    return parse


def cmd_registers(args: argparse.Namespace) -> int:
    """`sigen registers`: list the register map (or only the writable
    ones), one line each; writable ones show their option and range."""
    regs = WRITABLE if args.writable else ALL_REGISTERS
    for r in regs:
        flag = "W" if r.writable else " "
        unit = r.unit or ""
        extra = ""
        if r.writable:
            extra = f"  {r.cli_option} " + ("|".join(r.choices()) if r.labels else f"{r.min:g}-{r.max:g}")
        print(f"{flag} {r.device:8} {r.table:7} {r.address}  {r.type:6}  {unit:4}  {r.name}{extra}")
    return ExitCode.OK


def cmd_fake_server(args: argparse.Namespace) -> int:
    """`sigen fake-server`: serve the registers in --fake-data-dir's JSON
    files as a fake SigenStor until Ctrl+C."""
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not Path(args.fake_data_dir).is_dir():
        err(f"--fake-data-dir {args.fake_data_dir} is not a directory")
        return ExitCode.USAGE
    try:
        server = FakeSigenServer(args.host, args.port, args.fake_data_dir, args.ignore_writes, args.reject_writes)
    except OSError as e:
        err(f"can't listen on {args.host}:{args.port}: {e}")
        return ExitCode.ERROR
    if not server.bank.units():
        logging.warning("No registers loaded; every request will fail until you add *.json files.")
    mode = " — IGNORING WRITES" if args.ignore_writes else " — REJECTING WRITES" if args.reject_writes else ""
    logging.info("Fake SigenStor listening on %s:%d%s (Ctrl+C to stop)", args.host, server.port, mode)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Stopping.")
    finally:
        server.server_close()
    return ExitCode.OK


def _cloud_client(args: argparse.Namespace) -> tuple[CloudClient, Cloud]:
    """A CloudClient for the config's cloud section, and the section;
    ConfigError if the config has none."""
    cloud = _config(args).require_cloud()
    return CloudClient.from_config(cloud), cloud


def _config_file(args: argparse.Namespace) -> Path:
    """The file `sigen config` works on: --config, else $SIGEN_CONFIG, else the nearest config.json. Not loaded
    first, so a file with a bad value can still be fixed. NotFound (exit 9) with none."""
    path = config_path(args.config)
    if path is None:
        raise NotFound(f"no config file: give --config FILE, set ${CONFIG_ENV}, or run where a {CONFIG_NAME} is")
    return path


def cmd_config_show(args: argparse.Namespace) -> int:
    """`sigen config show`: the whole file as JSON with its secrets masked, then (on stderr) whether it passes
    sigen's checks. USAGE if it doesn't."""
    path = _config_file(args)
    doc = read_config_document(path)
    print(json.dumps(config_edit.mask("", doc), indent=2, ensure_ascii=False))
    try:
        parse_config(doc, path)
    except ConfigError as e:
        err(f"{path}: has a problem: {e}")
        return ExitCode.USAGE
    err(f"{path}: OK")
    return ExitCode.OK


def cmd_config_get(args: argparse.Namespace) -> int:
    """`sigen config get KEY`: one value (a string bare, else JSON; a section as JSON), a secret masked unless
    --reveal. NOT_FOUND if the key isn't set."""
    path = _config_file(args)
    value = config_edit.get(read_config_document(path), args.key)
    if value is config_edit.MISSING:
        err(f"error: {args.key} isn't set in {path}")
        return ExitCode.NOT_FOUND
    print(config_edit.shown(value if args.reveal else config_edit.mask(args.key, value)))
    return ExitCode.OK


def cmd_config_set(args: argparse.Namespace) -> int:
    """`sigen config set KEY VALUE` (or --stdin): set one value; see _change for the checks and the write."""
    if args.stdin == (args.value is not None):
        err("error: give VALUE, or --stdin to read it from standard input (one, not both)")
        return ExitCode.USAGE
    text = sys.stdin.readline().rstrip("\r\n") if args.stdin else args.value
    if args.key in config_edit.SECRETS and not args.stdin:
        err("note: a secret on the command line stays in your shell history; --stdin keeps it out")
    path = _config_file(args)
    doc = read_config_document(path)
    return _change(args, path, doc, config_edit.set_value(doc, args.key, config_edit.parse_value(text, args.string)))


def cmd_config_unset(args: argparse.Namespace) -> int:
    """`sigen config unset KEY`: remove one value or section; see _change."""
    path = _config_file(args)
    doc = read_config_document(path)
    return _change(args, path, doc, config_edit.unset(doc, args.key))


def _change(args: argparse.Namespace, path: Path, doc: dict, new: dict) -> int:
    """Check the changed document `new` with sigen's own rules (ConfigError, exit 2, and nothing written if it
    fails), then write it (backup first), saying what changed with secrets masked. DRY_RUN with --dry-run."""
    before, after = (config_edit.shown(config_edit.mask(args.key, config_edit.get(d, args.key))) for d in (doc, new))
    if new == doc:
        print(f"{args.key} is already {after}; nothing to write")
        return ExitCode.OK
    try:
        parse_config(new, path)
    except ConfigError as e:
        raise ConfigError(f"{e}; nothing written") from e
    change = f"{args.key}: {before} -> {after}"
    if args.dry_run:
        print(f"{change} (checked; dry run, not written)")
        return ExitCode.DRY_RUN
    backup = config_edit.write_document(path, new)
    print(f"{change}\nWritten to {path} (the old file is {backup.name}).")
    return ExitCode.OK


def cmd_cloud_login(args: argparse.Namespace) -> int:
    """`sigen cloud login`: prompt for the username, password and any
    emailed code, log in once (no retries), and save the tokens to
    cloud.token_file."""
    client, cloud = _cloud_client(args)
    username = input("Sigen username (email): ").strip()
    password = getpass.getpass("Sigen password (not shown): ")
    if not username or not password:
        err("Need both a username and a password; nothing sent.")
        return ExitCode.USAGE

    def mfa(body: dict) -> str:
        print(f"The cloud wants a security code (code {body.get('code')}: {body.get('msg')}).")
        return input("Security code from the email: ").strip()

    tokens = client.login(username, password, mfa)
    print(
        f"Logged in. Saved {cloud.token_file} (keep it secret). "
        f"Access token good for ~{tokens.seconds_left() / 3600:.1f} h."
    )
    return ExitCode.OK


def cmd_cloud_refresh(args: argparse.Namespace) -> int:
    """`sigen cloud refresh`: trade the saved single-use refresh token for
    fresh tokens (no password)."""
    client, cloud = _cloud_client(args)
    tokens = client.refresh()
    print(f"Refreshed {cloud.token_file}. Access token good for ~{tokens.seconds_left() / 3600:.1f} h.")
    return ExitCode.OK


def cmd_cloud_query(args: argparse.Namespace) -> int:
    """`sigen cloud query`: GET --path and print the JSON, or each
    battery's (or --sn's) real-time info as key/value lines or JSON. USAGE
    without a station ID or a battery to ask about."""
    client, cloud = _cloud_client(args)
    if args.path:
        print(json.dumps(client.get(args.path), indent=1))
        return ExitCode.OK
    if not cloud.station_id:
        err("this query needs cloud.station_id in the config (or use --path for a raw API path).")
        return ExitCode.USAGE
    sns = [args.sn] if args.sn else list(cloud.batteries)
    if not sns:
        err("no battery to query: add cloud.batteries to the config, or pass --sn, or use --path.")
        return ExitCode.USAGE
    out = {}
    for sn in sns:
        data = client.device_dynamic_info(cloud.station_id, sn, args.device_type)
        if args.json:
            out[sn] = data
        else:
            print(f"\n== {sn}")
            for row in data.get("realTimeInfo", []) if isinstance(data, dict) else []:
                print(f"  {row.get('paramKey', ''):42} {row.get('paramValue', '')}")
    if args.json:
        print(json.dumps(out, indent=1))
    return ExitCode.OK


def _gap_windows(
    conn: psycopg.Connection,
    config: Config,
    kinds: list[str],
    since: datetime.timedelta,
    min_gaps: dict[str, datetime.timedelta],
    tz: datetime.tzinfo,
) -> list[tuple[str, datetime.datetime, datetime.datetime, str]]:
    """(sync kind, start, end, label) for each gap over the last `since` in the tables `kinds` sync (see
    gaps.find_gaps), overlapping gaps of one kind merged (the heater's packs share a table)."""
    table_kinds = {t: k for k, tables in sync.STANDALONE_KINDS.items() for t in tables}
    table_kinds["readings"] = sync.READINGS_KIND
    tables = [t for t, k in table_kinds.items() if k in kinds]
    end = datetime.datetime.now(datetime.UTC)
    found = gaps.find_gaps(conn, config, end - since, end, _poll_interval(config), tables, min_gaps)
    spans: dict[str, list[list]] = {}
    for sg in found:
        for g in sg.gaps:
            spans.setdefault(sg.series.table, []).append([g.start, g.end])
    windows = []
    for table, found_spans in spans.items():
        merged: list[list] = []
        for start, stop in sorted(found_spans):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], stop)
            else:
                merged.append([start, stop])
        for start, stop in merged:
            label = (
                f"{table}: {start.astimezone(tz):%m-%d %H:%M:%S} -> {stop.astimezone(tz):%m-%d %H:%M:%S}"
                f" ({gaps.span_text(stop - start)})"
            )
            windows.append((table_kinds[table], start, stop, label))
    return windows


def cmd_sync(args: argparse.Namespace) -> int:
    """`sigen sync`: copy the rows in [--start-at, --end-at) that are missing here from the peer dashboard at
    --url, or with --fill-gaps the rows in each of this database's gaps (`db gaps`) over the last --since; then
    recompute the affected days' totals if readings came in."""
    config = _config(args)
    local_url = config.require_database_url()
    kinds = sync.resolve_kinds(args.only_sync)
    tz = _site_tz(config)
    if args.fill_gaps:
        if args.start_at or args.end_at:
            err("error: --fill-gaps finds its own windows (over --since): leave out --start-at and --end-at")
            return ExitCode.USAGE
        try:
            since, min_gaps = _gap_options(args)
        except ValueError as e:
            err(f"error: {e}")
            return ExitCode.USAGE
    else:
        if not (args.start_at and args.end_at):
            err("error: give --start-at and --end-at, or --fill-gaps")
            return ExitCode.USAGE
        start, end = sync.parse_iso(args.start_at), sync.parse_iso(args.end_at)
        if end <= start:
            err("--end-at must be after --start-at")
            return ExitCode.USAGE
    dry = " -- dry run" if args.dry_run else ""
    conn = db.connect(local_url)
    try:
        sync.check_schema(conn, args.url)
        if args.fill_gaps:
            windows = _gap_windows(conn, config, kinds, since, min_gaps, tz)
            print(f"Filling {len(windows)} gap(s) from the last {args.since} from {args.url}{dry}")
        else:
            windows = [(kind, start, end, None) for kind in kinds]
            print(f"Syncing {', '.join(kinds)} from {args.url} for [{sync.iso(start)}, {sync.iso(end)}){dry}")
        imported = present = 0
        readings_windows = []  # where readings came in: those days' totals are stale
        for kind, w_start, w_end, label in windows:
            if label:
                print(label)
            got, skip = sync.sync_kind(conn, args.url, kind, w_start, w_end, args.dry_run, out=print)
            imported, present = imported + got, present + skip
            if kind == sync.READINGS_KIND and got:
                readings_windows.append((w_start, w_end))
        if readings_windows and not args.dry_run:
            print("Recomputing daily totals for the imported days...")
            daily.update(
                conn,
                tz,
                min(s for s, _ in readings_windows).astimezone(tz).date(),
                max(e for _, e in readings_windows).astimezone(tz).date(),
                site=config.site,
                array=config.array,
                log=print,
            )
    finally:
        conn.close()
    print(
        f"Done: {imported} imported, {present} already present"
        + (" (dry run, nothing written)" if args.dry_run else "")
    )
    return ExitCode.OK


# ---------- parser ----------
def build_parser() -> argparse.ArgumentParser:
    """The `sigen` parser: every command and its options, with its cmd_*
    function as the `func` default."""
    net = _network_parent()
    fmt = argparse.RawDescriptionHelpFormatter
    p = argparse.ArgumentParser(
        prog="sigen",
        formatter_class=fmt,
        description="Read and safely control a Sigenergy SigenStor over local Modbus TCP.",
        epilog=EXIT_CODE_HELP,
    )
    p.add_argument("--version", action="version", version=f"sigen-modbus {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    r = sub.add_parser(
        "read",
        parents=[net],
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="read live status (read-only)",
        description="Read every known register. Read-only. Without --ip, scans for the device.",
    )
    _scan_options(r, with_ip=True)
    r.add_argument(
        "--json",
        action="store_true",
        help="print every register (value, units, raw words) as JSON; the output is "
        "also valid fake data for `sigen fake-server`",
    )
    _config_option(r, "smart-load names, and devices.inverter (ip, units) to read the inverter too")
    r.set_defaults(func=cmd_read)

    c = sub.add_parser(
        "capture",
        parents=[net],
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="read everything and save it to a timestamped JSON file (read-only)",
        description="Same as `read --json`, but written straight to "
        "DIR/PREFIX_YYYY-MM-DD_HH-MM-SSZ.json (UTC) as UTF-8, so shell "
        "redirection can't change the encoding. Never overwrites a file. "
        "Prints the summary and the saved path.",
    )
    _scan_options(c, with_ip=True)
    c.add_argument(
        "--out-dir",
        metavar="DIR",
        type=Path,
        default=Path("captures"),
        help="directory to save into; created if missing (default: captures)",
    )
    c.add_argument(
        "--name-prefix", metavar="PREFIX", default="my_system", help="start of the file name (default: my_system)"
    )
    _config_option(c, "smart-load names, and devices.inverter (ip, units) to read the inverter too")
    c.set_defaults(func=cmd_capture)

    rj = sub.add_parser(
        "read-json",
        aliases=["read_json"],
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="pretty-print a saved `read --json` dump (no device needed)",
        description="Show a saved dump as a summary plus a table of every register. "
        "Raw words are re-decoded with the current register map, so dumps "
        "from older versions display correctly.",
    )
    rj.add_argument(
        "--data-file",
        metavar="FILE",
        required=True,
        type=Path,
        help="a JSON file written by `sigen read --json` or `sigen capture`",
    )
    rj.add_argument("--summary-only", action="store_true", help="skip the full register table")
    _config_option(rj, "smart-load names")
    rj.set_defaults(func=cmd_read_json)

    s = sub.add_parser(
        "scan",
        parents=[net],
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="find SigenStors on the network",
        description="Scan for devices answering like a SigenStor and print their IPs.",
    )
    _scan_options(s, with_ip=False)
    s.add_argument("--json", action="store_true", help="print the result as JSON")
    s.set_defaults(func=cmd_scan)

    w = sub.add_parser(
        "set",
        parents=[net],
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="change one setting (read → write → read back)",
        description="Change one allowlisted setting. Always reads first, writes only with "
        "--apply, then reads back to verify. Without --apply this is a dry run.",
    )
    w.add_argument(
        "--ip", metavar="ADDRESS", required=True, help="SigenStor IP address (required: writes never use scanning)"
    )
    which = w.add_mutually_exclusive_group(required=True)
    for reg in WRITABLE:
        if reg.labels:
            help_text = f"{reg.description} Choices: {', '.join(reg.choices())}"
        else:
            help_text = f"{reg.description} ({reg.min:g}-{reg.max:g} {reg.unit or ''})".strip()
        which.add_argument(reg.cli_option, dest=reg.name, metavar="VALUE", help=help_text)
    w.add_argument(
        "--expect-current", metavar="VALUE", help="only write if the current value is this (exit 7 otherwise)"
    )
    w.add_argument("--apply", action="store_true", help="actually write (default: dry run, exit 8)")
    w.add_argument("--json", action="store_true", help="print the result as JSON")
    _journal_option(w)
    w.set_defaults(func=cmd_set)

    u = sub.add_parser(
        "undo",
        parents=[net],
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="revert the last journaled write",
        description="Restore the value from before the last successful write to this "
        "device, using the same read → write → read-back path. Refuses if "
        "the register has changed since (exit 7).",
    )
    u.add_argument("--ip", metavar="ADDRESS", required=True, help="SigenStor IP address")
    u.add_argument("--apply", action="store_true", help="actually write (default: dry run, exit 8)")
    u.add_argument("--json", action="store_true", help="print the result as JSON")
    _journal_option(u)
    u.set_defaults(func=cmd_undo)

    d = sub.add_parser(
        "db",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="set up, inspect and query the PostgreSQL database",
        description="Manage the database that `sigen poll` writes to. The connection URL "
        "comes from the config file's database.url.",
    )
    dsub = d.add_subparsers(dest="db_command", required=True, metavar="DB_COMMAND")
    dm = dsub.add_parser(
        "migrate",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="create or upgrade the tables (safe to re-run)",
        description="Apply pending migrations in order, each in its own transaction, then "
        "sync register_meta with the register map. Refuses (exit 7) if an applied "
        "migration was edited, or a register's type changed while values exist.",
    )
    _config_option(dm, "database.url")
    dm.set_defaults(func=cmd_db_migrate)
    ds = dsub.add_parser(
        "status",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="show schema version, counts and the last reading",
        description="Read-only. Exit 0 if `sigen poll` could run against this database, "
        "7 if migrations are pending or register_meta is out of date.",
    )
    _config_option(ds, "database.url")
    ds.set_defaults(func=cmd_db_status)
    dg = dsub.add_parser(
        "gaps",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="list each table's gaps: stretches without good rows (read-only)",
        description="Read-only. For each table's time series (the plant's readings, each battery pack's heater "
        "checks, Open-Meteo at each resolution, radar, the satellite), the stretches over the last --since with no "
        "good row for longer than its min gap: by default 3 x the interval its rows normally come at (readings: "
        "poll.interval), so a gap is at least three missed in a row. Each shows the failed attempts stored inside "
        "it, with their commonest error. The satellite counts daylight only, and is checked only with "
        "poll.satellite on in the config. daily_energy lists its missing days. "
        "`sigen sync --fill-gaps` fills them from a peer.",
    )
    _gap_arguments(dg)
    dg.add_argument(
        "--table",
        nargs="+",
        action="extend",
        choices=gaps.TABLES,
        metavar="TABLE",
        help=f"only these tables (default: all): {', '.join(gaps.TABLES)}",
    )
    dg.add_argument("--json", action="store_true", help="print the gaps as JSON (times in UTC)")
    _config_option(dg, "database.url, poll.interval, cloud.batteries and site (the zone times are shown in)")
    dg.set_defaults(func=cmd_db_gaps)
    dd = dsub.add_parser(
        "daily",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="store each finished day's energy in daily_energy",
        description="Compute each finished local day's energy (the dashboard's daily page: solar, "
        "the house, the battery, the grid, and every flow between them) and store it in "
        "daily_energy, so a month or a year is one SUM. A day is finished once a reading "
        "after its midnight is stored. Without --from / --to: the days not stored yet. "
        "With them: every finished day in that range, recomputed. `sigen poll` does the "
        "former every 10 minutes by itself. Exit 7 if the schema isn't ready.",
    )
    _config_option(dd, "database.url, and site.timezone for where days start (else this computer's)")
    dd.add_argument(
        "--from",
        dest="first",
        metavar="DATE",
        type=datetime.date.fromisoformat,
        help="the first day to (re)compute, e.g. 2026-10-07",
    )
    dd.add_argument(
        "--to", dest="last", metavar="DATE", type=datetime.date.fromisoformat, help="the last day to (re)compute"
    )
    dd.set_defaults(func=cmd_db_daily)
    dq = dsub.add_parser(
        "query",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="run SQL read-only and print the rows",
        description="Run SQL against the database and print each statement's rows as a "
        "table (or JSON). Read-only: it runs in a read-only transaction that is "
        "rolled back afterwards, so writes and schema changes are refused (exit "
        "10). That guards against accidents; it isn't a security boundary. "
        "The plant_/inverter_register_values views are scaled to real units; the "
        "raw_… tables hold values before the gain. Exit 10 if the query fails.",
    )
    dq.add_argument("sql", metavar="SQL", help='the query, e.g. "SELECT * FROM readings ORDER BY id DESC LIMIT 10"')
    _config_option(dq, "database.url")
    dq.add_argument(
        "--json", action="store_true", help="print a JSON list, one {status, columns, rows, truncated} per statement"
    )
    dq.add_argument(
        "--max-rows",
        metavar="N",
        type=_positive(int, 0),
        default=1000,
        help="print at most N rows per statement (default: 1000; 0 = all)",
    )
    dq.add_argument(
        "--max-width",
        metavar="CHARS",
        type=_positive(int, 0),
        default=80,
        help="cut table cells longer than CHARS with … (default: 80; 0 = never; --json never cuts)",
    )
    dq.set_defaults(func=cmd_db_query)

    cl = sub.add_parser(
        "cloud",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="talk to the Sigen cloud (login, refresh, query)",
        description="Read-only access to the mySigen cloud API, for what Modbus can't give us (the "
        "LoadHub/gateway's grid and contactor data, each battery pack's heating status). "
        'Needs the config\'s "cloud" section: token_file (gitignored; holds the rotating '
        "tokens), station_id / batteries for device queries, and the app's static "
        "constants obfuscation_key (login) and client_basic (login + refresh), which are "
        "kept in the config rather than in this code. The tokens are NOT in the config.",
    )
    clsub = cl.add_subparsers(dest="cloud_command", required=True, metavar="CLOUD_COMMAND")
    cli_ = clsub.add_parser(
        "login",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="log in with your password (prompts; handles the emailed code), save the tokens",
        description="Prompt for your Sigen username and password (never stored), log in, and "
        "save the access + refresh tokens to cloud.token_file. If the cloud asks for "
        "an emailed security code, it prompts for that too. One attempt: the account "
        "locks after a few failures, so nothing is retried.",
    )
    _config_option(cli_, "cloud.token_file (where to save) and cloud.base_url")
    cli_.set_defaults(func=cmd_cloud_login)
    clr = clsub.add_parser(
        "refresh",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="mint a fresh access token from the saved refresh token (no password)",
        description="Exchange the saved (single-use) refresh token for a fresh access token and a "
        "new refresh token, saved atomically. No password needed. Don't run this while "
        "`sigen poll` is refreshing (it keeps the token fresh itself).",
    )
    _config_option(clr, "cloud.token_file")
    clr.set_defaults(func=cmd_cloud_refresh)
    clq = clsub.add_parser(
        "query",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="GET a device's real-time info (default: the batteries' heating status)",
        description="Read-only GET using the saved access token (it does NOT refresh; run "
        "`sigen cloud refresh` if it's expired). Default: the heating status, SOC and "
        "voltage of each cloud.batteries pack. --path gets any raw API path instead.",
    )
    _config_option(clq, "cloud.token_file, cloud.station_id, cloud.batteries")
    clq.add_argument("--path", help="GET this raw API path and print the JSON, e.g. /device/gateway/<stationId>")
    clq.add_argument("--sn", help="one device SN to query instead of cloud.batteries")
    clq.add_argument("--device-type", type=int, default=4, help="2 AIO, 3 inverter, 4 battery, 8 gateway (default: 4)")
    clq.add_argument("--json", action="store_true", help="print the raw JSON")
    clq.set_defaults(func=cmd_cloud_query)

    cf = sub.add_parser(
        "config",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="show, read and change the config file (secrets masked, every change checked)",
        description="Read and change the config file without hand-editing its JSON. Keys are dotted paths "
        "(poll.interval, devices.inverter.ip, smart_loads.0.name). A VALUE is JSON (15, true, "
        '\'{"ip": "192.168.1.100"}\'); anything that isn\'t JSON is a string, so `site.timezone America/Edmonton` '
        "needs no quotes. A change is checked with the same rules sigen loads the file by, and refused (exit 2) "
        "before anything is written; then the file is replaced atomically, after a copy of the old one to "
        "<name>.bak, keeping its owner and mode. Secrets (database passwords, the ntfy topic, the cloud's keys) "
        "are masked in everything printed.",
    )
    cfsub = cf.add_subparsers(dest="config_command", required=True, metavar="CONFIG_COMMAND")
    config_file_help = (
        f"the config file (default: the file named by ${CONFIG_ENV}, else the nearest {CONFIG_NAME} "
        "in the current directory or above it)"
    )
    cfs = cfsub.add_parser(
        "show",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="print the whole file, secrets masked, and whether it passes the checks",
        description="Print the config file as JSON with its secrets masked; then, on stderr, whether it passes "
        "sigen's checks (exit 2 and the problem if it doesn't).",
    )
    cfg = cfsub.add_parser(
        "get",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="print one value or section",
        description="Print one value (a string bare, anything else as JSON) or a whole section. A secret is "
        "masked unless --reveal. Exit 9 if the key isn't set.",
    )
    cfg.add_argument("key", metavar="KEY", help="e.g. poll.interval, or a section like poll")
    cfg.add_argument("--reveal", action="store_true", help="print secrets as they are")
    cfst = cfsub.add_parser(
        "set",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="set one value (checked before it's written)",
        description="Set KEY to VALUE, making any missing sections on the way. Refused (exit 2), with nothing "
        "written, if the result wouldn't load. Prints the old and the new value (secrets masked).",
    )
    cfst.add_argument("key", metavar="KEY", help="e.g. poll.interval")
    cfst.add_argument("value", metavar="VALUE", nargs="?", help="JSON, else a string; leave out with --stdin")
    cfst.add_argument(
        "--stdin", action="store_true", help="read VALUE from the first line of standard input (for secrets)"
    )
    cfst.add_argument("--string", action="store_true", help="take VALUE as a string even if it looks like JSON")
    cfu = cfsub.add_parser(
        "unset",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="remove one value or section (checked before it's written)",
        description="Remove KEY (a value, a list item or a whole section). Refused (exit 2), with nothing "
        "written, if the result wouldn't load.",
    )
    cfu.add_argument("key", metavar="KEY", help="e.g. poll.radar")
    for cp, func in ((cfs, cmd_config_show), (cfg, cmd_config_get), (cfst, cmd_config_set), (cfu, cmd_config_unset)):
        cp.add_argument("--config", metavar="FILE", type=Path, help=config_file_help)
        if cp in (cfst, cfu):
            cp.add_argument("--dry-run", action="store_true", help="check and show the change, write nothing (exit 8)")
        cp.set_defaults(func=func)

    sy = sub.add_parser(
        "sync",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="backfill gaps in this database from a peer machine's",
        description="Pull a time window of rows from a peer's dashboard (its read-only /sync/* "
        "endpoints) and insert the ones missing locally -- for filling gaps left by an "
        "outage. Only good rows, never duplicates (safe to re-run), and identity ids are "
        "reassigned locally. Both databases must be on the same migrations, or it "
        "refuses. Window is [start, end), UTC. Or --fill-gaps: a window per gap that `sigen db gaps` "
        "finds here over the last --since (a gap that stays empty was the device's, not this machine's).",
    )
    sy.add_argument(
        "--url", required=True, metavar="URL", help="the peer's dashboard base URL, e.g. http://othermachine:8050"
    )
    sy.add_argument(
        "--start-at",
        metavar="ISO",
        help="window start, UTC ISO 8601, inclusive (e.g. 2026-10-17T16:02:15Z); not with --fill-gaps",
    )
    sy.add_argument("--end-at", metavar="ISO", help="window end, UTC ISO 8601, exclusive; not with --fill-gaps")
    sy.add_argument(
        "--fill-gaps",
        action="store_true",
        help="sync each of this database's gaps over --since instead of one window (see `sigen db gaps`)",
    )
    _gap_arguments(sy)
    sy.add_argument(
        "--only-sync",
        nargs="+",
        metavar="KIND",
        help=f"which kinds to sync (default: all); choices: {', '.join(sorted(sync.KINDS))}",
    )
    sy.add_argument("--dry-run", action="store_true", help="report what would be imported; write nothing")
    _config_option(
        sy,
        "the LOCAL database URL to write into, and site.timezone (to recompute daily totals after a readings sync)",
    )
    sy.set_defaults(func=cmd_sync)

    pl = sub.add_parser(
        "poll",
        parents=[net],
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="read everything on a schedule and store it in the database (read-only)",
        description="Read every register each interval and store the poll in the database "
        "from the config: all registers or none. The first poll is immediate; "
        "later ones line up with the clock. Connection problems are recorded "
        "and polling continues; a register the device rejects stops it (exit 4). "
        "Refuses to start (exit 7) until `sigen db migrate` is up to date. "
        "With devices.inverter.mac in the config (Windows and Linux), it finds the "
        "inverter by MAC after 3 connection failures in a row and follows it "
        "to its new IP. Every 10 minutes it also stores any finished day's energy "
        "in daily_energy (as `sigen db daily` does).",
    )
    pl.add_argument(
        "--ip",
        metavar="ADDRESS",
        help="SigenStor IP address (default: devices.inverter.ip from the config; polling never uses scanning)",
    )
    _config_option(
        pl,
        "database.url, devices.inverter (ip, mac, units), site for --weather / --radar / --satellite, and the "
        "poll section's settings",
    )
    pl.add_argument(
        "--interval",
        metavar="SECONDS",
        type=_positive(float, 1.0),
        help="seconds between polls (default: poll.interval in the config, else 60; minimum 1)",
    )
    pl.add_argument(
        "--offset",
        metavar="SECONDS",
        type=float,
        help="poll this long after each --interval boundary of the clock (default: poll.offset in the "
        "config, else 0, on it; e.g. --interval 15 --offset 7 polls at :07, :22, :37, :52). For a "
        "second poller of the same device, so the two don't read it at the same instant",
    )
    pl.add_argument(
        "--count",
        metavar="N",
        type=_positive(int, 1),
        help="stop after N polls (default: run until Ctrl+C; weather fetches don't count)",
    )
    on_off = argparse.BooleanOptionalAction
    pl.add_argument(
        "--weather",
        action=on_off,
        help="also store Open-Meteo for the config's site: at start, then hourly (60 min row) "
        "and every 15 min (15 min row), in a background thread (default: poll.weather in the "
        "config, else off)",
    )
    pl.add_argument(
        "--radar",
        action=on_off,
        help="also store Environment Canada radar at the site: at start (backfilling the "
        "3 hours GeoMet keeps), then every 6 min, in a background thread (default: poll.radar in "
        "the config, else off)",
    )
    pl.add_argument(
        "--alerts",
        action=on_off,
        help="after every poll, check the battery (cold, heater signatures, grid down, below the "
        "reserve, alarms) over the last 6 hours and push new problems to the config's ntfy topic, "
        "each kind at most once an hour; every alert is logged in alert_log. Use with --weather "
        "for the outside air (default: poll.alerts in the config, else off)",
    )
    pl.add_argument(
        "--satellite",
        action=on_off,
        help="also store NOAA GOES-West cloud shade over the config's site (satellite_shade, as `sigen satellite` "
        "does): each daylight scan once all three of its products are published, every 10 min (at :04:30, "
        ":14:30, …), catching up on the last 7 days one scan at a time, in a thread of its own (default: "
        "poll.satellite in the config, else off)",
    )
    pl.add_argument(
        "--cloud",
        action=on_off,
        help="also log each battery pack's self-heating status to battery_heater via the Sigen cloud, "
        'on a gated cadence (needs the config\'s "cloud" section). This holds a cloud session; '
        "turn it off to free one (default: poll.cloud in the config, else on when a cloud section "
        "is present)",
    )
    pl.add_argument(
        "--capture-open-meteo-weather",
        metavar="DIR",
        type=Path,
        help="with --weather: also save every whole Open-Meteo response (past day and "
        "3-day forecast) to DIR, one timestamped JSON file per fetch (default: "
        "poll.capture_open_meteo_weather in the config, else none)",
    )
    pl.add_argument(
        "--capture-radar",
        metavar="DIR",
        type=Path,
        help="with --radar: also save every whole GeoMet response (rain, snow and coverage per "
        "frame; the coverage one is ~40 kB) to DIR (default: poll.capture_radar in the config, "
        "else none)",
    )
    pl.set_defaults(func=cmd_poll)

    we = sub.add_parser(
        "weather",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="fetch Open-Meteo for the site once (optionally store it)",
        description="Ask Open-Meteo (a weather model; free, no key) about the config's site and "
        "print the latest period that has ended, hourly and/or 15-minute. With "
        "--record-to-db, store each as an open_meteo_weather row (a failed fetch is "
        "stored too, with the response). Exit 3 if a fetch failed, 10 if a row "
        "couldn't be stored.",
    )
    _config_option(we, "site (and database.url for --record-to-db)")
    we.add_argument(
        "--resolution",
        metavar="MINUTES",
        type=int,
        nargs="+",
        choices=[60, 15],
        default=[60, 15],
        help="60 (hourly) and/or 15 (default: both)",
    )
    we.add_argument("--record-to-db", action="store_true", help="store the rows in the database")
    we.add_argument(
        "--capture", metavar="DIR", type=Path, help="also save each whole response (past day and 3-day forecast) to DIR"
    )
    we.set_defaults(func=cmd_weather)

    ra = sub.add_parser(
        "radar",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="fetch Environment Canada radar at the site once (optionally store it)",
        description="Ask MSC GeoMet (free, no key) for the 1 km radar composite at the config's "
        "site: precipitation rate as rain (mm/h) and as snow (cm/h) - the same echo, "
        "two formulas, so not the type - and whether the radars covered it. Prints the "
        "newest frame; with --record-to-db, stores every frame from the last 3 hours "
        "not stored yet. Exit 3 if a fetch failed, 10 if nothing could be stored.",
    )
    _config_option(ra, "site (and database.url for --record-to-db)")
    ra.add_argument("--record-to-db", action="store_true", help="store the frames in the database")
    ra.add_argument("--capture", metavar="DIR", type=Path, help="also save each whole GeoMet response to DIR")
    ra.set_defaults(func=cmd_radar)

    sa = sub.add_parser(
        "satellite",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="store NOAA GOES-West cloud shade over the site (the satellite_shade table)",
        description="For every NOAA GOES-West scan in the window (one every 10 minutes) that the "
        "satellite_shade table doesn't have yet, cut a box around the config's site out of its "
        "three products (clear-sky mask, cloud optical depth, cloud-top height), compute the "
        "cloud shade over the site and store it. Reads only the boxes from NOAA's public AWS "
        "bucket (HTTP Range requests, ~3 MB a scan instead of ~33 MB), into memory: no files. A "
        "scan is fetched only once all three products are published (~10 to 20 minutes after "
        "it starts), newest first, one at a time. One run at a time per database (sigen poll's "
        "satellite job too): exit 7 if another has it. Exit 3 if NOAA couldn't be reached. "
        "With --cube-dir instead: add the boxes to per-day HDF5 files ('cubes') there, several "
        "scans at once, skipping those already in a cube (one run at a time per --cube-dir: "
        "satellite.pid there); --record-to-db also stores the shade from them.",
    )
    _config_option(sa, "site (where the box is centred, and its timezone for the dates) and database.url")
    sa.add_argument("--day", metavar="DATE", help="local date, e.g. 2026-10-10 (default: today)")
    sa.add_argument(
        "--start", metavar="TIME", help='local start, e.g. "2026-10-10 06:00" (with --end, instead of --day)'
    )
    sa.add_argument("--end", metavar="TIME", help="local end (exclusive)")
    sa.add_argument(
        "--products",
        nargs="+",
        choices=["ACM", "COD", "ACHA"],
        help="with --cube-dir: clear-sky mask, cloud optical depth, cloud-top height (default: all three)",
    )
    sa.add_argument(
        "--km",
        type=_positive(float, 1),
        default=200,
        help="box half-width in km (default: 200; a 10 km cloud's shade at 10° sun is ~57 km away)",
    )
    daylight = sa.add_mutually_exclusive_group()
    daylight.add_argument(
        "--daylight",
        metavar="MINUTES",
        type=_positive(float, 0),
        default=60,
        help="only scans from MINUTES before sunrise to MINUTES after sunset (default: 60)",
    )
    daylight.add_argument("--all-day", action="store_true", help="every scan, night included")
    sa.add_argument(
        "--workers",
        type=_positive(int, 1),
        help=f"with --cube-dir: scans fetched at once (default: {goes.DEFAULT_WORKERS})",
    )
    sa.add_argument(
        "--cube-dir",
        metavar="DIR",
        type=Path,
        nargs="?",
        const=True,
        help="keep the boxes in per-day cubes in DIR instead, for looking at the pixels yourself (no DIR: the "
        "config's satellite.cube_dir, else captures/satellite/cubes next to the config file). NOAA keeps every "
        "scan since 2022-05-11, so they can always be fetched again",
    )
    sa.add_argument("--satellite", default="18", help="GOES satellite number (default: 18, GOES-West)")
    sa.add_argument(
        "--record-to-db",
        action="store_true",
        help="with --cube-dir: also compute and store each scan's cloud shade from the cubes in the satellite_shade "
        "table (without --cube-dir, that's what it always does)",
    )
    sa.add_argument("--sector", default="F", choices=["F", "C"], help="F = full disk (default), C = CONUS")
    sa.set_defaults(func=cmd_satellite)

    da = sub.add_parser(
        "dashboard",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="a web page of charts from the database (strings, smart loads, battery)",
        description="Serve a page of charts of the database, drawn in the browser (uPlot): string "
        "voltage and power (every reading; with the clear-sky model, and the radar and "
        "satellite strips underneath), energy by load, each smart load's energy since the "
        "window started, smart-load power (5-minute means), the battery's temperatures "
        "with the inverter's on a right axis, and battery and PV power with the SOC; "
        "smart loads include Other = house minus all smart loads. Today by default; links "
        "for the last 6 h / 24 h / 3 days and other days, and a daily-totals page. A "
        "window that reaches now is topped up every 15 s with only the newer readings. "
        "Read-only. Listens on all interfaces by default so a phone on the same network "
        "can open it (Windows Firewall asks the first time).",
    )
    _config_option(
        da, "database.url, smart-load names, site.timezone for local times, and the dashboard section's settings"
    )
    da.add_argument(
        "--host",
        help="address to listen on (default: dashboard.host in the config, else 0.0.0.0, every "
        "interface; 127.0.0.1 = this computer only)",
    )
    da.add_argument("--port", type=_positive(int, 1), help="port (default: dashboard.port in the config, else 8050)")
    da.add_argument(
        "--reload",
        action=argparse.BooleanOptionalAction,
        help="restart when sigen's code changes (Flask's reloader; its debugger stays off) "
        "(default: dashboard.reload in the config, else off)",
    )
    da.set_defaults(func=cmd_dashboard)

    nt = sub.add_parser(
        "ntfy-test",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="send a test push notification to the config's ntfy topic",
        description="Post a message to the ntfy topic in the config's alerts section, to check "
        "that notifications reach your phone (subscribe to the topic in the ntfy app). "
        "Prints the topic masked. With --record-to-db, the attempt is also stored in "
        "alert_log (kind 'test'), whether or not it got through. Exit 3 if ntfy couldn't "
        "be reached or refused it, 10 if it couldn't be stored.",
    )
    _config_option(nt, "alerts.ntfy_topic (and alerts.ntfy_server; database.url for --record-to-db)")
    nt.add_argument("--record-to-db", action="store_true", help="also store the attempt in the alert_log table")
    nt.add_argument(
        "--ntfy-server",
        metavar="URL",
        type=_ntfy_server,
        help="send to this ntfy server instead of the config's alerts.ntfy_server (same topic), "
        "e.g. http://localhost:8051",
    )
    nt.add_argument("--msg", default="Hello world", help='the message (default: "Hello world")')
    nt.add_argument("--title", help="a title above the message")
    nt.add_argument(
        "--priority",
        choices=["min", "low", "default", "high", "urgent"],
        help="ntfy priority (urgent can break through Do Not Disturb; default: the server's, 'default')",
    )
    nt.set_defaults(func=cmd_ntfy_test)

    ml = sub.add_parser(
        "mac-lookup",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="IP -> MAC, or find a MAC's IP on the local network (Windows and Linux)",
        description="No admin rights needed: Windows' SendARP, or on Linux the kernel's own "
        "ARP read back with `ip neigh` (iproute2). --ip prints that address's MAC; "
        "--mac asks every address in --ip-range (default: this computer's /24) and "
        "prints the IP that has it (a /24: ~15 s on Windows, ~3-8 s on Linux). "
        "On other systems, or Linux without `ip`: exit 2.",
    )
    which_way = ml.add_mutually_exclusive_group(required=True)
    which_way.add_argument("--ip", metavar="ADDRESS", help="print the MAC of this address")
    which_way.add_argument("--mac", metavar="MAC", help="find the IP of this MAC, e.g. 02:ab:cd:ef:00:01")
    ml.add_argument(
        "--ip-range",
        metavar="RANGE",
        type=parse_ip_range,
        help="with --mac: addresses to ask (default: this computer's /24)",
    )
    ml.set_defaults(func=cmd_mac_lookup)

    g = sub.add_parser(
        "registers",
        help="list the register map",
        description="List known registers: plant ones (unit 247) and inverter ones "
        "(the inverter's own unit ID). W = writable (on the allowlist).",
    )
    g.add_argument("--writable", action="store_true", help="only list writable registers")
    g.set_defaults(func=cmd_registers)

    f = sub.add_parser(
        "fake-server",
        help="run a fake SigenStor for testing",
        description="Serve register values from JSON files (e.g. `sigen read --json` dumps).",
    )
    f.add_argument(
        "--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1; 0.0.0.0 for all interfaces)"
    )
    f.add_argument("--port", type=int, default=10502, help="TCP port (default: 10502)")
    f.add_argument(
        "--fake-data-dir",
        type=Path,
        default=Path("fake_data/baseline"),
        help="directory of *.json register files (default: fake_data/baseline)",
    )
    writes = f.add_mutually_exclusive_group()
    writes.add_argument(
        "--ignore-writes",
        action="store_true",
        help="acknowledge writes but don't store them (tests the verify-failed path)",
    )
    writes.add_argument(
        "--reject-writes",
        action="store_true",
        help="answer every write with exception 04 (tests the write-rejected path)",
    )
    f.add_argument("--verbose", action="store_true", help="log every read and connection")
    f.set_defaults(func=cmd_fake_server)
    return p


def _utf8_output() -> None:
    """Make stdout/stderr UTF-8 regardless of the Windows locale (cp1252), so piped or
    redirected output (`read --json | …`) is UTF-8 like every file this tool writes."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass  # not a reconfigurable text stream (e.g. replaced by a test harness)


def main(argv: list[str] | None = None) -> int:
    """The `sigen` entry point: run the command and return its exit code. A
    SigenError prints `error: …` (with --json, also a JSON object on
    stdout) and returns its code; Ctrl+C returns 130."""
    _utf8_output()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except SigenError as e:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(e), "exit_code": int(e.exit_code)}))
        err(f"error: {e}")
        return int(e.exit_code)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
