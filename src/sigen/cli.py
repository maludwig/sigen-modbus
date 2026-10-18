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
                      [--[no-]alerts]
    sigen weather     [CONFIG] [--resolution 60 15] [--record-to-db] [--capture DIR]
    sigen radar       [CONFIG] [--record-to-db] [--capture DIR]
    sigen satellite   [CONFIG] [--day DATE | --start TIME --end TIME] [--products ACM COD ACHA] [--km 200]
                      [--daylight MINUTES | --all-day] [--workers 8] [--cube-dir DIR]
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
import datetime
import functools
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

from sigen import __version__, db, sync
from sigen.cloud import CloudClient
from sigen.config import CONFIG_ENV, CONFIG_NAME, Config, get_config
from sigen.control import DEFAULT_JOURNAL, Journal, plan_undo, set_register
from sigen.data_io import build_document, capture_filename, read_json_file, save_document
from sigen.discovery import local_subnet, parse_ip_range, scan
from sigen.errors import ExitCode, NotFound, PlatformNotSupported, SigenError
from sigen.modbus import DEFAULT_PORT, DEFAULT_TIMEOUT, ModbusClient
from sigen.block_reader import BlockReader
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
    print(msg, file=sys.stderr)


# ---------- shared options ----------
def _network_parent() -> argparse.ArgumentParser:
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
    p.add_argument(
        "--journal",
        type=Path,
        default=DEFAULT_JOURNAL,
        help=f"write log, one JSON object per line (default: {DEFAULT_JOURNAL})",
    )


def _config_option(p: argparse.ArgumentParser, what: str) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--config",
        metavar="FILE",
        type=Path,
        help=f"JSON config file: {what} (default: the file named by ${CONFIG_ENV}, else the nearest "
        f"{CONFIG_NAME} in the current directory or above it, e.g. the repo's)",
    )
    g.add_argument("--no-config", action="store_true", help="use no config file, even if one would be found")


def _config(args) -> Config:
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
    ("capture_open_meteo_weather", "--capture-open-meteo-weather", None),
    ("capture_radar", "--capture-radar", None),
]
DASHBOARD_SETTINGS = [("host", "--host", "0.0.0.0"), ("port", "--port", 8050), ("reload", "--reload", False)]


def _settings(args, config: Config, command: str, settings) -> List[tuple]:
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


def _print_settings(config: Config, shown) -> None:
    """One line per setting: the option, its value, and where it came from (to stderr, beside poll's other notes)."""
    err(f"Settings ({f'config: {config.path}' if config.path else config.origin}):")
    width = max(len(option) for option, _, _ in shown)
    vwidth = min(max(len(text) for _, text, _ in shown), 10)  # a long path only pushes its own line's source
    for option, text, source in shown:
        err(f"  {option:<{width}}  {text:<{vwidth}}  {source}")


def _connector(args, unit: Optional[int] = None):
    return functools.partial(ModbusClient, args.ip, args.port, args.unit_id if unit is None else unit, args.timeout)


def _inverter_units(config) -> tuple:
    inverter = config.devices.get("inverter")
    return inverter.units if inverter else ()


def _find(args) -> List[str]:
    ips = args.ip_range or [str(h) for h in local_subnet().hosts()]
    return scan(ips, port=args.port, unit=args.unit_id, timeout=args.timeout, scan_timeout=args.scan_timeout)


# ---------- commands ----------
def _read_device(args, config) -> tuple:
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


def cmd_read(args) -> int:
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


def cmd_capture(args) -> int:
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


def cmd_read_json(args) -> int:
    config = _config(args)
    path = Path(args.data_file)
    try:
        doc = read_json_file(path)
    except OSError as e:
        raise NotFound(f"can't read {path}: {e}")
    except ValueError as e:  # bad JSON, or bytes that aren't UTF-8/UTF-16
        err(f"error: {path} isn't valid JSON: {e}")
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
        print(format_table(readings, config, title=f"Plant (unit {doc.get('unit_id', 247)})" if inverters else ""))
        for unit, unit_readings in sorted(inverters.items()):
            print(format_table(unit_readings, config, title=f"Inverter (unit {unit})"))
    return ExitCode.OK


def cmd_scan(args) -> int:
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


def _report(result, args, journal: Optional[Journal]) -> int:
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


def cmd_set(args) -> int:
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


def cmd_undo(args) -> int:
    journal = Journal(args.journal)
    plan = plan_undo(journal, args.ip, args.port, args.unit_id)
    if plan is None:
        raise NotFound(f"nothing to undo for {args.ip}:{args.port} in {journal.path}")
    entry, reg, restore, expect = plan
    print(
        f"Undoing {entry['id']} ({entry['ts']}): {reg.name} {entry['after']} → {entry['before']}",
        file=sys.stderr if args.json else sys.stdout,
    )
    result = set_register(_connector(args), reg, restore, apply=args.apply, expect_words=expect, undo_of=entry["id"])
    return _report(result, args, journal)


def cmd_db_migrate(args) -> int:
    from sigen import db

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


def cmd_db_status(args) -> int:
    from sigen import db

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
    else:
        print(
            f"Values:        {s['register_values']:,}"
            if s["register_values_exact"]
            else f"Values:        ~{s['register_values']:,} (estimate)"
        )
    last = s["last_reading"]
    if last:
        rid, started, ended, error = last
        print(
            f"Last reading:  {rid} at {started.astimezone(datetime.timezone.utc):%Y-%m-%d %H:%M:%S.%f}Z"
            + (f"  FAILED: {error}" if error else "")
        )
    utc = lambda t: f"{t.astimezone(datetime.timezone.utc):%Y-%m-%d %H:%M}Z"
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


def _cell(value, max_width: int) -> str:
    text = "NULL" if value is None else str(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return text[: max_width - 1] + "…" if max_width and len(text) > max_width else text


def format_query_table(columns: List[str], rows: List[tuple], max_width: int = 0) -> str:
    """psql-style: a header, a rule, one line per row; numbers right-aligned, NULL spelled out,
    line breaks shown as \\n, cells longer than max_width cut with … (0 = never)."""
    import decimal

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


def _site_tz(config):
    """The config's site timezone, else this computer's."""
    from zoneinfo import ZoneInfo

    return ZoneInfo(config.site.timezone) if config.site else datetime.datetime.now().astimezone().tzinfo


def cmd_db_daily(args) -> int:
    from sigen import daily, db

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


def cmd_db_query(args) -> int:
    from sigen import db

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


def cmd_poll(args) -> int:
    import socket
    from sigen import db, netlookup, poll

    config = _config(args)
    _print_settings(config, _settings(args, config, "poll", POLL_SETTINGS))
    url = config.require_database_url()
    inverter = config.devices.get("inverter")
    target = args.ip or (inverter.ip if inverter else None)
    if not target:
        err(f"error: give --ip, or set devices.inverter.ip in the config ({config.origin})")
        return ExitCode.USAGE
    try:
        ip = socket.gethostbyname(target)  # readings.ip_addr is INET: store the address itself
    except OSError as e:
        err(f"error: can't resolve {target}: {e}")
        return ExitCode.USAGE
    rediscover = None
    if inverter and inverter.mac:
        try:
            netlookup.require_supported()  # say so now, not hours later at the first outage
        except PlatformNotSupported as e:
            err(
                f"warning: {e}; polling without following the inverter's MAC (devices.inverter.mac), so if "
                f"its IP changes, polling keeps failing until it's restarted with the new IP"
            )
        else:
            mac = inverter.mac

            def rediscover(current_ip):
                if netlookup.mac_for_ip(current_ip) == mac:
                    return current_ip
                return netlookup.find_ip_for_mac(mac, [str(h) for h in local_subnet().hosts()])

    if not 0 <= args.offset < args.interval:
        err(f"error: --offset must be at least 0 and less than --interval ({args.interval:g})")
        return ExitCode.USAGE
    if args.capture_open_meteo_weather and not args.weather:
        err("error: --capture-open-meteo-weather needs --weather")
        return ExitCode.USAGE
    if args.capture_radar and not args.radar:
        err("error: --capture-radar needs --radar")
        return ExitCode.USAGE
    watcher = alert_store = None
    if args.alerts:
        from sigen import alerts, weather

        alert_config = config.require_alerts()
        from zoneinfo import ZoneInfo

        alert_store = weather.Store(url, err)  # the sender's own connection
        tz = ZoneInfo(config.site.timezone) if config.site else None  # else this computer's
        watcher = alerts.Watcher(
            alerts.Sender(alert_config, alert_store, out=print, err=err), tz=tz, out=print, err=err
        )
    background, store = _background(args, config, url, watcher)
    cloud_heater = None
    if args.cloud and config.cloud is not None:
        from sigen import heater

        cloud_heater = heater.HeaterPoller.from_config(config.cloud, url, out=print, err=err)
    units = {args.unit_id: "plant", **{u: "inverter" for u in _inverter_units(config)}}
    extras = [
        what
        for what, on in (
            ("Open-Meteo hourly and every 15 min", args.weather),
            ("radar every 6 min", args.radar),
            ("alerts" + ("" if args.weather else " (no --weather: outside air unknown)"), args.alerts),
            ("the batteries' cloud heater status", cloud_heater is not None),
        )
        if on
    ]
    print(
        f"Polling {ip}:{args.port} units {', '.join(f'{u} ({d})' for u, d in units.items())} every "
        f"{args.interval:g}s"
        + (f", {args.offset:g}s past the boundary" if args.offset else "")
        + f" into {db.redact(url)} (Ctrl+C to stop)"
        + (f"; following MAC {inverter.mac}" if rediscover else "")
        + (f"; also {' and '.join(extras)}" if extras else ""),
        file=sys.stderr,
    )
    connector_for = lambda address, unit: functools.partial(ModbusClient, address, args.port, unit, args.timeout)
    try:
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
        )
    finally:
        for s in (store, alert_store):
            if s is not None:
                s.close()


def _background(args, config, url, watcher=None):
    """poll's background jobs, one at a time in their own thread: (Background, Store or None).
    Always: storing each finished day in daily_energy (sigen.daily), checked every 10 minutes, with its own
    connection. With --weather / --radar: the fetches, sharing a Store; with a watcher (--alerts), each 15-minute
    Open-Meteo row's air temperature goes to it too."""
    from sigen import daily, db, weather

    tz = _site_tz(config)

    def daily_energy():
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

            def fetch(resolution=resolution):
                row, stored = weather.record_open_meteo(site, resolution, store, args.capture_open_meteo_weather)
                print(
                    weather.summarize_open_meteo(row) + ("" if stored else "  (not stored)"),
                    file=sys.stderr if row.error else sys.stdout,
                )
                if watcher is not None and resolution == 15 and not row.error:
                    watcher.add_air(row.valid_at, row.values.get("temperature_2m"))

            jobs.append(weather.Job(f"Open-Meteo {resolution} min", resolution * 60, fetch))
    if args.radar:

        def radar():
            rows, added = weather.record_radar(site, store, args.capture_radar)
            print(weather.summarize_radar(rows, added), file=sys.stderr if any(r.error for r in rows) else sys.stdout)

        jobs.append(weather.Job("radar", weather.RADAR_INTERVAL, radar))
    return weather.Background(jobs, err), store


def _weather_store(args, config):
    """For `weather` / `radar --record-to-db`: a Store, after checking the schema is ready."""
    from sigen import db, weather

    url = config.require_database_url()
    conn = db.connect(url)
    try:
        db.check_ready(conn)
    finally:
        conn.close()
    return weather.Store(url, err)


def cmd_weather(args) -> int:
    from sigen import weather

    config = _config(args)
    site = config.require_site()
    store = _weather_store(args, config) if args.record_to_db else None
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


def cmd_radar(args) -> int:
    from sigen import weather

    config = _config(args)
    site = config.require_site()
    store = _weather_store(args, config) if args.record_to_db else None
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
    from zoneinfo import ZoneInfo

    try:
        return datetime.datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(timezone))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{what} {text!r} isn't a date/time like 2026-10-10 or 2026-10-10 13:00")


def cmd_satellite(args) -> int:
    from zoneinfo import ZoneInfo
    from sigen import db, goes, goes_shade

    config = _config(args)
    site = config.require_site()
    cube_dir = args.cube_dir if args.cube_dir is not None else config.cube_dir()
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
    err(
        f"GOES-{args.satellite} {', '.join(args.products)} ±{args.km:g} km around {site.latitude}, {site.longitude}, "
        f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} {site.timezone}"
        + ("" if daylight is None else f", within {daylight:g} min of daylight")
        + f", into {cube_dir}"
    )
    conn = db.connect(config.require_database_url()) if args.record_to_db else None
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
            products=args.products,
            km=args.km,
            satellite=args.satellite,
            sector=args.sector,
            workers=args.workers,
            daylight_minutes=daylight,
            log=print,
        )
        series = (
            goes_shade.shade_between(cube_dir, site.latitude, site.longitude, site.timezone, start, end, args.satellite)
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
        added = db.insert_satellite_shade(conn, datetime.datetime.now(datetime.timezone.utc), series, error=error)
        conn.close()
        if error is not None:
            err(f"error: satellite run failed: {error}")
            return ExitCode.CONNECTION
        print(f"{added} scan(s) stored")
    return ExitCode.OK


def _ntfy_server(text: str) -> str:
    from sigen.config import NTFY_SERVER

    if not NTFY_SERVER.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r} isn't a server URL like https://ntfy.sh or http://localhost:8051")
    return text.rstrip("/")


def cmd_ntfy_test(args) -> int:
    import dataclasses
    from sigen import db, ntfy

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


def _lan_address() -> Optional[str]:
    """This computer's address on the local network (the route a packet would take; nothing is sent)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed anywhere real
            return s.getsockname()[0]
    except OSError:
        return None


def cmd_dashboard(args) -> int:
    from sigen import dashboard

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


def cmd_mac_lookup(args) -> int:
    from sigen import netlookup

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
    ips = args.ip_range or [str(h) for h in local_subnet().hosts()]
    err(f"Asking {len(ips)} address(es) for {mac}…")
    found = netlookup.find_ip_for_mac(mac, ips)
    if found is None:
        raise NotFound(f"no device with MAC {mac} answered")
    print(found)
    return ExitCode.OK


def socket_ip(name: str) -> str:
    import socket

    try:
        return socket.gethostbyname(name)
    except OSError as e:
        raise NotFound(f"can't resolve {name}: {e}")


def _positive(kind, minimum):
    def parse(text):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"'{text}' isn't a number")
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}")
        return value

    return parse


def cmd_registers(args) -> int:
    regs = WRITABLE if args.writable else ALL_REGISTERS
    for r in regs:
        flag = "W" if r.writable else " "
        unit = r.unit or ""
        extra = ""
        if r.writable:
            extra = f"  {r.cli_option} " + ("|".join(r.choices()) if r.labels else f"{r.min:g}-{r.max:g}")
        print(f"{flag} {r.device:8} {r.table:7} {r.address}  {r.type:6}  {unit:4}  {r.name}{extra}")
    return ExitCode.OK


def cmd_fake_server(args) -> int:
    from sigen.fake_server import FakeSigenServer

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


def _cloud_client(args) -> tuple:
    cloud = _config(args).require_cloud()
    return (
        CloudClient(
            cloud.token_file,
            cloud.base_url,
            app_base_url=cloud.app_base_url,
            obfuscation_key=cloud.obfuscation_key,
            client_basic=cloud.client_basic,
        ),
        cloud,
    )


def cmd_cloud_login(args) -> int:
    import getpass

    client, cloud = _cloud_client(args)
    username = input("Sigen username (email): ").strip()
    password = getpass.getpass("Sigen password (not shown): ")
    if not username or not password:
        err("Need both a username and a password; nothing sent.")
        return ExitCode.USAGE

    def mfa(body):
        print(f"The cloud wants a security code (code {body.get('code')}: {body.get('msg')}).")
        return input("Security code from the email: ").strip()

    tokens = client.login(username, password, mfa)
    print(
        f"Logged in. Saved {cloud.token_file} (keep it secret). "
        f"Access token good for ~{tokens.seconds_left() / 3600:.1f} h."
    )
    return ExitCode.OK


def cmd_cloud_refresh(args) -> int:
    client, cloud = _cloud_client(args)
    tokens = client.refresh()
    print(f"Refreshed {cloud.token_file}. Access token good for ~{tokens.seconds_left() / 3600:.1f} h.")
    return ExitCode.OK


def cmd_cloud_query(args) -> int:
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


def cmd_sync(args) -> int:
    from sigen import daily

    config = _config(args)
    local_url = config.require_database_url()
    start, end = sync.parse_iso(args.start_at), sync.parse_iso(args.end_at)
    if end <= start:
        err("--end-at must be after --start-at")
        return ExitCode.USAGE
    kinds = sync.resolve_kinds(args.only_sync)
    conn = db.connect(local_url)
    try:
        sync.check_schema(conn, args.url)
        print(
            f"Syncing {', '.join(kinds)} from {args.url} for [{sync.iso(start)}, {sync.iso(end)})"
            + (" -- dry run" if args.dry_run else "")
        )
        imported = present = readings_imported = 0
        for kind in kinds:
            got, skip = sync.sync_kind(conn, args.url, kind, start, end, args.dry_run, out=print)
            imported, present = imported + got, present + skip
            if kind == sync.READINGS_KIND:
                readings_imported = got
        if readings_imported and not args.dry_run:  # fresh readings -> the affected days' totals are stale
            tz = _site_tz(config)
            print("Recomputing daily totals for the imported days...")
            daily.update(conn, tz, start.astimezone(tz).date(), end.astimezone(tz).date(), log=print)
    finally:
        conn.close()
    print(
        f"Done: {imported} imported, {present} already present"
        + (" (dry run, nothing written)" if args.dry_run else "")
    )
    return ExitCode.OK


# ---------- parser ----------
def build_parser() -> argparse.ArgumentParser:
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

    sy = sub.add_parser(
        "sync",
        formatter_class=fmt,
        epilog=EXIT_CODE_HELP,
        help="backfill gaps in this database from a peer machine's",
        description="Pull a time window of rows from a peer's dashboard (its read-only /sync/* "
        "endpoints) and insert the ones missing locally -- for filling gaps left by an "
        "outage. Only good rows, never duplicates (safe to re-run), and identity ids are "
        "reassigned locally. Both databases must be on the same migrations, or it "
        "refuses. Window is [start, end), UTC.",
    )
    sy.add_argument(
        "--url", required=True, metavar="URL", help="the peer's dashboard base URL, e.g. http://othermachine:8050"
    )
    sy.add_argument(
        "--start-at",
        required=True,
        metavar="ISO",
        help="window start, UTC ISO 8601, inclusive (e.g. 2026-10-17T16:02:15Z)",
    )
    sy.add_argument("--end-at", required=True, metavar="ISO", help="window end, UTC ISO 8601, exclusive")
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
        "database.url, devices.inverter (ip, mac, units), site for --weather / --radar, and the "
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
        help="store NOAA GOES-West cloud data around the site (per-day cubes)",
        description="Cut a box around the config's site out of every NOAA GOES-West scan (one "
        "every 10 minutes: clear-sky mask, cloud optical depth, cloud-top height) and "
        "add it to per-day HDF5 files ('cubes') in --cube-dir. Reads only "
        "the box from NOAA's public AWS bucket (HTTP Range requests, ~2.5 MB a scan for "
        "all three products instead of ~33 MB), several scans at once. Scans already in "
        "a cube are skipped, so re-running only fetches what's new. Each cube is updated "
        "by appending to a copy and swapping it in, so readers never see half a scan. "
        "One run at a time per --cube-dir (satellite.pid there): exit 7 if another is "
        "still running. Exit 3 if NOAA couldn't be reached.",
    )
    _config_option(
        sa, "site (where the box is centred, and its timezone for the dates; database.url for --record-to-db)"
    )
    sa.add_argument("--day", metavar="DATE", help="local date, e.g. 2026-10-10 (default: today)")
    sa.add_argument(
        "--start", metavar="TIME", help='local start, e.g. "2026-10-10 06:00" (with --end, instead of --day)'
    )
    sa.add_argument("--end", metavar="TIME", help="local end (exclusive)")
    sa.add_argument(
        "--products",
        nargs="+",
        default=["ACM", "COD", "ACHA"],
        choices=["ACM", "COD", "ACHA"],
        help="clear-sky mask, cloud optical depth, cloud-top height (default: all three)",
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
    sa.add_argument("--workers", type=_positive(int, 1), default=8, help="scans fetched at once (default: 8)")
    sa.add_argument(
        "--cube-dir",
        metavar="DIR",
        type=Path,
        help="where the cubes go (default: the config's satellite.cube_dir, else "
        "captures/satellite/cubes next to the config file)",
    )
    sa.add_argument("--satellite", default="18", help="GOES satellite number (default: 18, GOES-West)")
    sa.add_argument(
        "--record-to-db",
        action="store_true",
        help="also compute and store each scan's cloud shade over the site (the strip values) in the "
        "satellite_shade table",
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


def main(argv: Optional[List[str]] = None) -> int:
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
