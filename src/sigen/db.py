"""PostgreSQL storage for polled readings, with psycopg (a dependency of the package).

Tables (created by the numbered SQL files in sigen/migrations/):
    readings           one row per unit per poll: IP, unit ID, poll ID, start/end time, tool
                       version, error if it failed
    raw_plant_register_values, raw_inverter_register_values (since 0004)
                       one row per successful reading: reading_id, started_at, then one column per
                       register, named like it, decoded by type but before the gain. Views
                       plant_/inverter_register_values scale them (column <name>_<unit>), and the
                       view register_values keeps the old (reading_id, address, value) shape.
    register_values_v1 the table register_values was until 0004 (kept until a later migration)
    register_meta      address -> name, type, gain, unit, device; kept in sync with sigen.registers
    open_meteo_weather one row per Open-Meteo fetch: the latest period at 60 or 15 minutes (sigen.weather)
    radar_frames       Environment Canada radar at the site, one good row per 6-minute frame
    alert_log          every notification raised (sigen.ntfy): sent or suppressed, and what ntfy answered
    schema_migrations  which migrations have been applied, with checksums

psycopg is imported only inside this module's functions, so the rest of sigen works without it.
"""
import datetime
import hashlib
import re
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from sigen import __version__
from sigen.errors import DatabaseError, SchemaNotReady
from sigen.registers import ALL_REGISTERS, BY_NAME, Register, decode_int

STORED_REGISTERS = [r for r in ALL_REGISTERS if r.stored]     # what register_meta describes
BY_ADDRESS = {r.address: r for r in STORED_REGISTERS}
DEVICES = ("plant", "inverter")
# Column type per register type (u16 doesn't fit SMALLINT; u64 is stored as the same-bits signed number).
COLUMN_TYPES = {"u16": "integer", "s16": "smallint", "u32": "bigint", "s32": "integer", "u64": "bigint"}


# The scaled views' column suffix per register unit, as migration 0004 named them.
VIEW_SUFFIX = {"kW": "kw", "kWh": "kwh", "kvar": "kvar", "kVA": "kva", "V": "v", "A": "a", "%": "pct", "°C": "c",
               "s": "s", "Hz": "hz", "min": "min", "MΩ": "megohm"}


def view_column(reg: Register) -> str:
    """The register's column in plant_/inverter_register_values: 'plant_ess_soc' -> 'plant_ess_soc_pct'; a register
    without a unit keeps its name."""
    return reg.name + (f"_{VIEW_SUFFIX[reg.unit]}" if reg.unit else "")


def values_table(device: str) -> str:
    """The wide table for a device's register values (since migration 0004)."""
    if device not in DEVICES:
        raise ValueError(f"unknown device {device!r}")
    return f"raw_{device}_register_values"

MIGRATION_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER     PRIMARY KEY,
    name       TEXT        NOT NULL,
    checksum   TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)"""

# Held for the whole of `migrate`, per schema, so two migrates can't interleave.
LOCK_SQL = "SELECT pg_advisory_lock(hashtext('sigen-migrate:' || current_schema()))"
UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtext('sigen-migrate:' || current_schema()))"


# ---------- connections and errors ----------
def redact(url: str) -> str:
    """Hide passwords in a postgresql:// URL or a key=value connection string."""
    url = re.sub(r"(://[^:/@?#]*:)[^@/?#]*@", r"\1***@", url)          # user:PASSWORD@host
    return re.sub(r"(password\s*=\s*)('[^']*'|[^&\s]+)", r"\1***", url)  # password=… / ?password=…


def connect(url: str):
    """An autocommit connection: every write below happens inside an explicit transaction."""
    try:
        import psycopg
    except ImportError:
        raise DatabaseError("psycopg isn't installed; run: pip install -e .")
    try:
        return psycopg.connect(url, autocommit=True, connect_timeout=10)
    except psycopg.Error as e:
        raise DatabaseError(f"can't connect to {redact(url)}: {_message(e)}", transient=True)


def _message(e: Exception) -> str:
    return " ".join(str(e).split())


@contextmanager
def errors_as(what: str) -> Iterator[None]:
    """Turn psycopg errors into DatabaseError (exit 10) with some context.
    OperationalError (connection lost, server shutting down) is marked transient."""
    import psycopg
    try:
        yield
    except psycopg.Error as e:
        raise DatabaseError(f"{what}: {_message(e)}", transient=isinstance(e, psycopg.OperationalError))


# ---------- values ----------
def stored_value(reg: Register, words: List[int]) -> int:
    """The register's words decoded as its type, before the gain. u64 values with the top
    bit set (in practice only "not set", all ones) become the signed number with the same
    bits, so they fit in a BIGINT: all ones -> -1."""
    n = decode_int(words, reg.type)
    if reg.type == "u64" and n >= 1 << 63:
        n -= 1 << 64
    return n


def values_for(readings: Dict[str, dict]) -> List[Tuple[int, int]]:
    """Strict readings (every register read) -> [(address, stored value)], in address order.
    String registers (model, serial, firmware) are display only and aren't stored."""
    out = []
    for name, entry in readings.items():
        if "raw" not in entry:
            raise ValueError(f"{name} has no raw words: {entry.get('error', 'not read')}")
        reg = BY_NAME[name]
        if reg.stored:
            out.append((reg.address, stored_value(reg, entry["raw"])))
    return sorted(out)


# ---------- migrations ----------
@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


def load_migrations() -> List[Migration]:
    """Every sigen/migrations/NNNN_name.sql, in order. Line endings are normalised before
    checksumming, so a git checkout with CRLF doesn't look like an edit."""
    found = []
    for entry in resources.files("sigen.migrations").iterdir():
        if not entry.name.endswith(".sql"):
            continue
        m = MIGRATION_FILE.match(entry.name)
        if m is None:
            raise SchemaNotReady(f"migration file {entry.name} isn't named NNNN_lowercase_name.sql")
        sql = entry.read_text(encoding="utf-8").replace("\r\n", "\n")
        found.append(Migration(int(m.group(1)), m.group(2), sql))
    found.sort(key=lambda mig: mig.version)
    versions = [mig.version for mig in found]
    if versions != list(range(1, len(found) + 1)):
        raise SchemaNotReady(f"migration versions must be 1, 2, 3, … with no gaps; found {versions}")
    return found


def _applied(conn) -> Optional[Dict[int, Tuple[str, str]]]:
    """version -> (name, checksum), or None if migrate has never run in this schema."""
    if conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0] is None:
        return None
    return {v: (n, c) for v, n, c in conn.execute("SELECT version, name, checksum FROM schema_migrations")}


def _check_applied(applied: Dict[int, Tuple[str, str]], migrations: Sequence[Migration]) -> None:
    known = {m.version: m for m in migrations}
    for version, (name, checksum) in sorted(applied.items()):
        mig = known.get(version)
        if mig is None:
            raise SchemaNotReady(f"the database has migration {version:04d}_{name}, which this version "
                                 f"of sigen doesn't know; upgrade sigen")
        if mig.checksum != checksum:
            raise SchemaNotReady(f"migration {mig.label} was edited after it was applied; put the change "
                                 f"in a new migration instead")


def migrate(conn, migrations: Optional[Sequence[Migration]] = None, log=lambda msg: None) -> List[str]:
    """Apply pending migrations (each in its own transaction), then sync register_meta.
    Returns the labels of the migrations applied."""
    migrations = load_migrations() if migrations is None else list(migrations)
    done = []
    with errors_as("migrate failed"):
        conn.execute(LOCK_SQL)
        try:
            conn.execute(BOOTSTRAP_SQL)
            applied = _applied(conn) or {}
            _check_applied(applied, migrations)
            for mig in migrations:
                if mig.version in applied:
                    continue
                log(f"applying {mig.label}")
                with conn.transaction():
                    conn.execute(mig.sql)
                    conn.execute("INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                                 (mig.version, mig.name, mig.checksum))
                done.append(mig.label)
            with conn.transaction():
                sync_register_meta(conn, log)
        finally:
            conn.execute(UNLOCK_SQL)
    return done


# ---------- register_meta ----------
@dataclass
class MetaPlan:
    inserts: List[Register]
    updates: List[Register]
    problems: List[str]        # type changes on addresses that already have values

    @property
    def in_sync(self) -> bool:
        return not (self.inserts or self.updates or self.problems)


def _meta_row(reg: Register) -> tuple:
    return reg.name, reg.type, float(reg.gain), reg.unit, reg.device


def plan_meta_sync(meta: Dict[int, tuple], has_values, registers: Sequence[Register] = STORED_REGISTERS) -> MetaPlan:
    """Compare the code's register map with register_meta rows
    (address -> (name, type, gain, unit, device)).
    `has_values(address)` says whether any stored value depends on that address's type.
    Addresses only in the database are left alone: old readings still refer to them."""
    plan = MetaPlan([], [], [])
    for reg in registers:
        row = meta.get(reg.address)
        if row is None:
            plan.inserts.append(reg)
        elif tuple(row) != _meta_row(reg):
            if row[1] != reg.type and has_values(reg.address):
                plan.problems.append(f"{reg.address} {reg.name}: type is {row[1]} in the database but "
                                     f"{reg.type} in the code, and values are stored; write a migration that "
                                     f"converts them and updates register_meta")
            else:
                plan.updates.append(reg)
    return plan


def wide_layout(conn) -> bool:
    """Whether migration 0004 has switched over: register_values is then a view over the wide tables. (The wide
    tables alone don't say so: the backfill script creates them, and fills them, while register_values is still
    the table poll writes to.)"""
    return conn.execute("SELECT coalesce((SELECT relkind = 'v' FROM pg_class WHERE oid = to_regclass('register_values')), "
                        "false)").fetchone()[0]


_wide = wide_layout


def _columns(conn) -> Dict[str, Dict[str, str]]:
    """device -> {column: data type} of the wide value tables."""
    found = {d: {} for d in DEVICES}
    for table, column, data_type in conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ANY(%s)", ([values_table(d) for d in DEVICES],)):
        found[table.split("_")[1]][column] = data_type
    return found


def _plan(conn) -> MetaPlan:
    from psycopg import sql
    meta = {a: (n, t, g, u, d) for a, n, t, g, u, d in
            conn.execute("SELECT address, name, type, gain, unit, device FROM register_meta")}
    wide = _wide(conn)
    columns = _columns(conn) if wide else {}

    def has_values(address: int) -> bool:
        if not wide:                                   # before 0004: the long table
            return conn.execute("SELECT EXISTS (SELECT 1 FROM register_values WHERE address = %s)",
                                (address,)).fetchone()[0]
        name, device = meta[address][0], meta[address][4]
        if device not in DEVICES or name not in columns[device]:
            return False
        return conn.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {} WHERE {} IS NOT NULL)").format(
            sql.Identifier(values_table(device)), sql.Identifier(name))).fetchone()[0]
    return plan_meta_sync(meta, has_values)


def missing_columns(conn) -> List[str]:
    """Stored registers without a column of the right type in their device's wide table (empty when ready)."""
    columns, problems = _columns(conn), []
    for reg in STORED_REGISTERS:
        have = columns[reg.device].get(reg.name)
        if have is None:
            problems.append(f"{reg.name} ({reg.address}) has no column in {values_table(reg.device)}")
        elif have != COLUMN_TYPES[reg.type]:
            problems.append(f"{values_table(reg.device)}.{reg.name} is {have}, but {reg.type} needs "
                            f"{COLUMN_TYPES[reg.type]}")
    return problems


def sync_register_meta(conn, log=lambda msg: None) -> None:
    """Bring register_meta in line with sigen.registers (call inside a transaction)."""
    plan = _plan(conn)
    if plan.problems:
        raise SchemaNotReady("register_meta can't be synced:\n  " + "\n  ".join(plan.problems))
    with conn.cursor() as cur:          # executemany: one pipelined batch, not a round trip per register
        if plan.inserts:
            cur.executemany("INSERT INTO register_meta (address, name, type, gain, unit, device) "
                            "VALUES (%s, %s, %s, %s, %s, %s)", [(reg.address, *_meta_row(reg)) for reg in plan.inserts])
        if plan.updates:
            cur.executemany("UPDATE register_meta SET name = %s, type = %s, gain = %s, unit = %s, device = %s "
                            "WHERE address = %s", [(*_meta_row(reg), reg.address) for reg in plan.updates])
    if plan.inserts or plan.updates:
        log(f"register_meta: {len(plan.inserts)} added, {len(plan.updates)} updated")


def check_ready(conn, migrations: Optional[Sequence[Migration]] = None) -> None:
    """Raise SchemaNotReady unless every migration is applied, unedited, and register_meta
    matches the code. `poll` calls this before reading anything."""
    migrations = load_migrations() if migrations is None else list(migrations)
    with errors_as("checking the schema failed"):
        applied = _applied(conn)
        if applied is None:
            raise SchemaNotReady("the database has no sigen tables yet; run `sigen db migrate`")
        _check_applied(applied, migrations)
        pending = [m.label for m in migrations if m.version not in applied]
        if pending:
            raise SchemaNotReady(f"migrations pending ({', '.join(pending)}); run `sigen db migrate`")
        plan = _plan(conn)
        missing = missing_columns(conn)
    if not plan.in_sync:
        detail = "\n  ".join(plan.problems) if plan.problems else \
            f"{len(plan.inserts)} register(s) missing, {len(plan.updates)} changed; run `sigen db migrate`"
        raise SchemaNotReady(f"register_meta doesn't match the code:\n  {detail}")
    if missing:
        raise SchemaNotReady("the register map has registers the database can't store; add them with a migration "
                             "(ALTER TABLE … ADD COLUMN, and recreate the views):\n  " + "\n  ".join(missing))


# ---------- status ----------
def status(conn, migrations: Optional[Sequence[Migration]] = None) -> dict:
    migrations = load_migrations() if migrations is None else list(migrations)
    with errors_as("reading status failed"):
        applied = _applied(conn)
        out = {"migrations_applied": sorted(applied) if applied else [],
               "migrations_pending": [m.label for m in migrations if not applied or m.version not in applied]}
        if not applied:
            return out
        try:
            check_ready(conn, migrations)
            out["ready"], out["problem"] = True, None
        except SchemaNotReady as e:
            out["ready"], out["problem"] = False, str(e)
        total, failed = conn.execute("SELECT count(*), count(error) FROM readings").fetchone()
        out["readings"], out["failed_readings"] = total, failed
        if _wide(conn):                       # since 0004: one row per successful reading, per device
            out["value_rows"] = {d: conn.execute(f"SELECT count(*) FROM {values_table(d)}").fetchone()[0]
                                 for d in DEVICES}
        else:
            # An exact count(*) on a large table takes a while, so past a million rows use the
            # planner's estimate (which is 0 until the table has been analyzed, hence not always).
            estimate = conn.execute("SELECT greatest(reltuples, 0)::bigint FROM pg_class "
                                    "WHERE oid = 'register_values'::regclass").fetchone()[0]
            if estimate < 1_000_000:
                out["register_values"] = conn.execute("SELECT count(*) FROM register_values").fetchone()[0]
                out["register_values_exact"] = True
            else:
                out["register_values"], out["register_values_exact"] = estimate, False
        out["last_reading"] = conn.execute(
            "SELECT id, started_at, ended_at, error FROM readings ORDER BY id DESC LIMIT 1").fetchone()
        if out["ready"]:                       # unit_id only exists once 0002 is applied
            out["readings_by_unit"] = conn.execute(
                "SELECT unit_id, count(*), count(error) FROM readings GROUP BY unit_id ORDER BY unit_id").fetchall()
        if conn.execute("SELECT to_regclass('open_meteo_weather')").fetchone()[0] is not None:   # 0003
            out["open_meteo"] = conn.execute(
                "SELECT count(*), count(error), count(*) FILTER (WHERE resolution_minutes = 60 AND error IS NULL), "
                "count(*) FILTER (WHERE resolution_minutes = 15 AND error IS NULL), max(valid_at), max(fetched_at) "
                "FROM open_meteo_weather").fetchone()
            out["radar"] = conn.execute(
                "SELECT count(*) FILTER (WHERE error IS NULL), count(error), max(frame_at) FILTER (WHERE error IS NULL), "
                "max(fetched_at) FROM radar_frames").fetchone()
        if conn.execute("SELECT to_regclass('alert_log')").fetchone()[0] is not None:            # 0005
            out["alerts"] = conn.execute(
                "SELECT count(*) FILTER (WHERE NOT was_suppressed AND error IS NULL), count(error), "
                "count(*) FILTER (WHERE was_suppressed), max(alerted_at) FROM alert_log").fetchone()
        if conn.execute("SELECT to_regclass('daily_energy')").fetchone()[0] is not None:         # 0006
            out["daily_energy"] = conn.execute("SELECT count(*), min(day), max(day) FROM daily_energy").fetchone()
        out["tables"] = table_sizes(conn)
    return out


# Each table's row time: when its rows were written, or the nearest thing it has. A table not
# listed here has no such column (register_meta, register_values_v1).
ROW_TIMES = {"readings": "started_at", "raw_plant_register_values": "started_at",
             "raw_inverter_register_values": "started_at", "open_meteo_weather": "fetched_at",
             "radar_frames": "fetched_at", "alert_log": "alerted_at", "schema_migrations": "applied_at",
             "daily_energy": "computed_at"}


@dataclass
class TableSize:
    name: str
    total_bytes: int                    # data + indexes + TOAST
    rows: int                           # exact, unless rows_exact is False (the planner's estimate)
    rows_exact: bool
    time_column: Optional[str]
    rows_last_day: Optional[int]        # rows whose time is in the last 24 h; None without a time column

    @property
    def bytes_per_day(self) -> Optional[float]:
        """Growth over the last 24 h, estimated as the table's average bytes per row times the
        rows added in that time (indexes and TOAST included)."""
        if self.rows_last_day is None:
            return None
        return self.total_bytes * self.rows_last_day / self.rows if self.rows else 0.0


def table_sizes(conn) -> List[TableSize]:
    """Every table in the current schema, biggest first."""
    tables = conn.execute(
        "SELECT c.relname, pg_total_relation_size(c.oid), greatest(c.reltuples, 0)::bigint "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p') AND n.nspname = current_schema() "
        "ORDER BY pg_total_relation_size(c.oid) DESC, c.relname").fetchall()
    out = []
    for name, total, estimate in tables:
        column = ROW_TIMES.get(name)
        if column is None:              # past a million rows the estimate, as above (count(*) takes seconds)
            if estimate >= 1_000_000:
                out.append(TableSize(name, total, estimate, False, None, None))
            else:
                out.append(TableSize(name, total, conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0],
                                     True, None, None))
            continue
        rows, last_day = conn.execute(
            f"SELECT count(*), count(*) FILTER (WHERE {column} > now() - interval '24 hours') FROM {name}").fetchone()
        out.append(TableSize(name, total, rows, True, column, last_day))
    return out


# ---------- ad-hoc queries ----------
@dataclass
class QueryResult:
    status: str                         # PostgreSQL's command tag, e.g. "SELECT 10"
    columns: Optional[List[str]]        # None: the statement returns no rows
    rows: List[tuple]
    truncated: bool                     # it returned more than max_rows rows


def query(conn, sql_text: str, max_rows: Optional[int] = None) -> List[QueryResult]:
    """Run SQL typed by a person, read-only; one result per statement.

    The session defaults to read-only transactions and the text runs inside one that is
    rolled back afterwards, so writes and schema changes fail (exit 10), even after a COMMIT
    in the text. A guard against accidents, not a security boundary: an explicit
    `SET default_transaction_read_only = off` gets round it."""
    results = []
    with errors_as("query failed"):
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        with conn.transaction(force_rollback=True):
            cur = conn.execute(sql_text)
            while True:
                if cur.description is None:
                    results.append(QueryResult(cur.statusmessage or "", None, [], False))
                else:
                    rows = cur.fetchall() if not max_rows else cur.fetchmany(max_rows + 1)
                    truncated = bool(max_rows) and len(rows) > max_rows
                    results.append(QueryResult(cur.statusmessage or "", [c.name for c in cur.description],
                                               rows[:max_rows] if truncated else rows, truncated))
                if not cur.nextset():
                    break
    return results


# ---------- storing readings ----------
def next_poll_id(conn) -> int:
    """A new id shared by the readings (one per unit) that one poll takes."""
    with errors_as("allocating a poll id failed"):
        return conn.execute("SELECT nextval('poll_ids')").fetchone()[0]


def insert_reading(conn, *, ip: str, unit_id: int, poll_id: int, started_at: datetime.datetime,
                   ended_at: datetime.datetime, values: Sequence[Tuple[int, int]] = (),
                   error: Optional[str] = None) -> int:
    """One unit's snapshot, in one transaction: a readings row plus, if it has values, one row in its device's
    wide table (raw_plant_register_values or raw_inverter_register_values), one column per register."""
    from psycopg import sql
    columns = []
    if values:
        regs = [BY_ADDRESS.get(address) for address, _ in values]
        unknown = [address for (address, _), reg in zip(values, regs) if reg is None]
        if unknown:
            raise ValueError(f"addresses not in the stored register map: {unknown}")
        devices = {reg.device for reg in regs}
        if len(devices) != 1:
            raise ValueError(f"one reading holds one device's registers, got {sorted(devices)}")
        columns = [reg.name for reg in regs]
    with errors_as("storing the reading failed"):
        with conn.transaction():
            reading_id = conn.execute(
                "INSERT INTO readings (ip_addr, unit_id, poll_id, started_at, ended_at, version, error) "
                "VALUES (%s::inet, %s, %s, %s, %s, %s, %s) RETURNING id",
                (ip, unit_id, poll_id, started_at, ended_at, __version__, error)).fetchone()[0]
            if values:
                query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(values_table(devices.pop())),
                    sql.SQL(", ").join(map(sql.Identifier, ["reading_id", "started_at", *columns])),
                    sql.SQL(", ").join([sql.Placeholder()] * (len(columns) + 2)))
                conn.execute(query, [reading_id, started_at, *(value for _, value in values)])
    return reading_id


# ---------- storing weather (rows from sigen.weather) ----------
COLUMN_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def insert_open_meteo(conn, row) -> int:
    """One sigen.weather.WeatherRow -> its open_meteo_weather id."""
    from psycopg import sql
    columns = ["fetched_at", "resolution_minutes", "valid_at", *row.values, "error"]
    bad = [c for c in columns if not COLUMN_NAME.match(c)]
    if bad:
        raise ValueError(f"not column names: {bad}")
    query = sql.SQL("INSERT INTO open_meteo_weather ({}) VALUES ({}) RETURNING id").format(
        sql.SQL(", ").join(map(sql.Identifier, columns)), sql.SQL(", ").join([sql.Placeholder()] * len(columns)))
    with errors_as("storing the Open-Meteo row failed"):
        return conn.execute(query, [row.fetched_at, row.resolution_minutes, row.valid_at,
                                    *row.values.values(), row.error]).fetchone()[0]


def stored_radar_frames(conn, since: datetime.datetime) -> set:
    """Frame times from `since` on that already have a good row."""
    with errors_as("reading the stored radar frames failed"):
        return {t for t, in conn.execute(
            "SELECT frame_at FROM radar_frames WHERE error IS NULL AND frame_at >= %s", (since,))}


def insert_alert(conn, attempt) -> int:
    """One sigen.ntfy.Attempt (sent or suppressed) -> its alert_log id."""
    a = attempt
    with errors_as("storing the alert failed"):
        return conn.execute(
            "INSERT INTO alert_log (alerted_at, kind, was_suppressed, title, message, priority, tags, click, "
            "http_code, ntfy_id, error) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (a.alerted_at, a.kind, a.was_suppressed, a.title, a.message, a.priority, list(a.tags), a.click,
             a.http_code, a.ntfy_id, a.error)).fetchone()[0]


def last_alerts_sent(conn) -> Dict[Optional[str], datetime.datetime]:
    """{kind: when the last alert of that kind was sent} (suppressed ones don't count; failed sends do: they were
    tried). For keeping the per-kind cooldown across a restart of `poll`."""
    with errors_as("reading the alert log failed"):
        return dict(conn.execute("SELECT kind, max(alerted_at) FROM alert_log WHERE NOT was_suppressed "
                                 "GROUP BY kind").fetchall())


def insert_radar_rows(conn, rows) -> int:
    """sigen.weather.RadarRow objects, in one transaction. A frame that already has a good row
    is skipped; error rows are always kept. Returns how many good rows were added."""
    added = 0
    with errors_as("storing radar frames failed"):
        with conn.transaction():
            for r in rows:
                cur = conn.execute(
                    "INSERT INTO radar_frames (fetched_at, frame_at, rain_mm_h, rain_class, snow_cm_h, snow_class, "
                    "covered, error) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (frame_at) WHERE error IS NULL DO NOTHING",
                    (r.fetched_at, r.frame_at, r.rain_mm_h, r.rain_class, r.snow_cm_h, r.snow_class, r.covered, r.error))
                if r.error is None:
                    added += cur.rowcount
    return added
