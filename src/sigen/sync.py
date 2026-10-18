"""Backfill gaps in the local sigen database from a peer's (another machine polling the same system into its own
database). An outage -- a reboot, a deploy, a crashed Docker -- leaves each machine's time series with holes the
other usually has. `sigen sync` pulls a time window of rows from the peer's dashboard (its read-only /sync/*
endpoints) and writes in the ones we're missing. It never touches the peer.

Two paths:
  * STANDALONE tables (weather, radar, heater): independent rows with a natural key and a throwaway id. Pulled and
    inserted one table at a time (export_rows / sync_standalone).
  * the READINGS CLUSTER (readings + raw_plant/raw_inverter_register_values): a connected graph -- raw rows hang off
    a reading, readings are grouped into a poll. The wire form nests that structure (poll -> readings -> raw values),
    so no ids cross the wire at all; the importer regenerates a local poll_id per poll and lets the database assign
    reading ids (export_readings / sync_readings).

Rules, so a sync is always safe to re-run:
  * only GOOD rows are pulled (each table says how, Syncable.good) -- this machine's gap is explained by it being
    down, not by transient errors the peer hit;
  * no ids cross the wire; the local database assigns its own;
  * a row is imported only if its natural key isn't already present locally (readings: unit_id + started_at), so
    re-runs and overlapping windows never duplicate;
  * both databases must be on the same, fully-applied set of migrations, or the sync refuses -- the wide tables'
    columns have to line up.

The window is half-open [start, end), both UTC.
"""

import datetime
import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from collections.abc import Callable

import psycopg

from sigen import db
from sigen.errors import SchemaNotReady, SyncError

TIMEOUT = 60.0
UTC = datetime.UTC

RAW_PLANT = "raw_plant_register_values"
RAW_INVERTER = "raw_inverter_register_values"
READING_COLS = ["ip_addr", "started_at", "ended_at", "version", "error", "unit_id", "glitch"]


# ---------- what can be synced ----------
@dataclass(frozen=True)
class Syncable:
    """A standalone table sigen can copy: the column a window filters on, the self-assigned id (dropped on import, so
    the local database makes its own), the natural key that means "already have this row", and a predicate that
    selects the good rows (the only ones worth copying)."""

    time_column: str
    identity: str
    key: tuple[str, ...]
    good: str = "error IS NULL"


SYNCABLE: dict[str, Syncable] = {
    "open_meteo_weather": Syncable("valid_at", "id", ("resolution_minutes", "valid_at")),
    "radar_frames": Syncable("frame_at", "id", ("frame_at",)),
    "battery_heater": Syncable("polled_at", "id", ("battery", "polled_at")),
    "satellite_shade": Syncable("scan_at", "id", ("scan_at",)),
}

# `--only-sync` kinds. The standalone ones map to one table each; "readings" is the cluster, handled on its own.
STANDALONE_KINDS: dict[str, list[str]] = {
    "weather": ["open_meteo_weather"],
    "radar": ["radar_frames"],
    "heater": ["battery_heater"],
    "satellite": ["satellite_shade"],
}
READINGS_KIND = "readings"
KINDS: list[str] = [*STANDALONE_KINDS, READINGS_KIND]  # every selectable kind, in a stable order


def resolve_kinds(selected: list[str] | None) -> list[str]:
    """The kinds to sync: `selected` (validated, de-duplicated, in the given order) or all of them. SyncError on an
    unknown kind."""
    if not selected:
        return list(KINDS)
    unknown = [k for k in selected if k not in KINDS]
    if unknown:
        raise SyncError(f"unknown sync kind(s) {', '.join(unknown)}; known: {', '.join(KINDS)}")
    out: list[str] = []
    for k in selected:
        if k not in out:
            out.append(k)
    return out


# ---------- times ----------
def parse_iso(text: str) -> datetime.datetime:
    """An ISO 8601 instant (a trailing Z is fine) as an aware UTC datetime; naive input is taken as UTC."""
    try:
        dt = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise SyncError(f"not an ISO 8601 time: {text!r} (e.g. 2026-10-17T16:02:15.010474Z)")
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def iso(dt: datetime.datetime) -> str:
    """UTC ISO 8601 with a Z, never a +00:00 offset -- a '+' in a query string is otherwise decoded as a space."""
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ---------- the peer serves these (read-only; called from the dashboard) ----------
def schema_state(conn: psycopg.Connection) -> dict:
    """What a peer reports for the guard: its applied migrations as {version: checksum}, and whether it is fully
    migrated against the code it runs."""
    applied = db.applied_migrations(conn)
    code = {m.version: m.checksum for m in db.load_migrations()}
    return {"applied": {str(v): c for v, c in applied.items()}, "fully_migrated": applied == code}


def export_rows(conn: psycopg.Connection, table: str, start: datetime.datetime, end: datetime.datetime) -> dict:
    """Good rows of a standalone `table` whose time column is in [start, end), without the identity column."""
    spec = SYNCABLE.get(table)
    if spec is None:
        raise SyncError(f"{table!r} isn't a syncable table")
    columns = [c for c in _columns(conn, table) if c != spec.identity]
    collist = ", ".join(f'"{c}"' for c in columns)
    with db.errors_as(f"exporting {table}"):
        rows = conn.execute(
            f'SELECT {collist} FROM "{table}" '
            f'WHERE ({spec.good}) AND "{spec.time_column}" >= %s AND "{spec.time_column}" < %s '
            f'ORDER BY "{spec.time_column}"',
            (start, end),
        ).fetchall()
    return {"table": table, "columns": columns, "rows": [[_jsonable(v) for v in row] for row in rows]}


def export_readings(conn: psycopg.Connection, start: datetime.datetime, end: datetime.datetime) -> dict:
    """The readings cluster for [start, end): good readings nested under their poll, each carrying its raw register
    row as a positional array. The raw column order is sent once, at the top, as `columns`. No ids cross the wire."""
    manifest = {table: _register_columns(conn, table) for table in (RAW_PLANT, RAW_INVERTER)}
    with db.errors_as("exporting readings"):
        readings = conn.execute(
            f'SELECT id, poll_id, {", ".join(READING_COLS)} FROM readings '
            f"WHERE error IS NULL AND started_at >= %s AND started_at < %s ORDER BY poll_id, started_at",
            (start, end),
        ).fetchall()
        raw: dict[str, dict[int, list]] = {}
        for table in (RAW_PLANT, RAW_INVERTER):
            collist = ", ".join(f'"{c}"' for c in manifest[table])
            raw[table] = {
                row[0]: list(row[1:])
                for row in conn.execute(
                    f'SELECT reading_id, {collist} FROM "{table}" WHERE started_at >= %s AND started_at < %s',
                    (start, end),
                )
            }
    polls: dict[int, list[dict]] = {}
    for row in readings:
        reading_id, poll_id = row[0], row[1]
        fields = dict(zip(READING_COLS, row[2:]))
        fields["ip_addr"] = str(fields["ip_addr"])
        fields["started_at"] = iso(fields["started_at"])
        fields["ended_at"] = iso(fields["ended_at"])
        for table in (RAW_PLANT, RAW_INVERTER):
            if reading_id in raw[table]:
                fields[table] = [_jsonable(v) for v in raw[table][reading_id]]
                break
        polls.setdefault(poll_id, []).append(fields)
    return {"columns": manifest, "polls": [{"readings": rs} for rs in polls.values()]}


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    """`table`'s column names in the local schema, in table order."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position",
            (table,),
        )
    ]


def _register_columns(conn: psycopg.Connection, table: str) -> list[str]:
    """A raw table's columns minus the ones the importer supplies itself (reading_id, started_at)."""
    return [c for c in _columns(conn, table) if c not in ("reading_id", "started_at")]


def _jsonable(value: Any) -> Any:
    """A database value ready for JSON: datetimes as UTC ISO strings
    (see iso), everything else as is."""
    if isinstance(value, datetime.datetime):
        return iso(value)
    return value


# ---------- the client pulls and imports (called from `sigen sync`) ----------
def check_schema(conn: psycopg.Connection, peer_url: str) -> None:
    """Refuse unless the local database is fully migrated and the peer is on exactly the same migrations -- the wide
    tables' columns must line up before any import."""
    code = {m.version: m.checksum for m in db.load_migrations()}
    if db.applied_migrations(conn) != code:
        raise SchemaNotReady("the local database isn't fully migrated; run `sigen db migrate` first")
    remote = {int(v): c for v, c in _get(peer_url, "/sync/schema").get("applied", {}).items()}
    if remote != code:
        raise SchemaNotReady(
            f"the peer at {peer_url} is on a different set of migrations; bring both to the same "
            f"version with `sigen db migrate` before syncing"
        )


def sync_kind(
    conn: psycopg.Connection,
    peer_url: str,
    kind: str,
    start: datetime.datetime,
    end: datetime.datetime,
    dry_run: bool,
    out: Callable[[str], None] = print,
) -> tuple[int, int]:
    """Sync one kind; returns (imported, already-present)."""
    if kind == READINGS_KIND:
        return sync_readings(conn, peer_url, start, end, dry_run, out)
    imported = present = 0
    for table in STANDALONE_KINDS[kind]:
        got, skip = sync_standalone(conn, peer_url, table, start, end, dry_run, out)
        imported, present = imported + got, present + skip
    return imported, present


def sync_standalone(
    conn: psycopg.Connection,
    peer_url: str,
    table: str,
    start: datetime.datetime,
    end: datetime.datetime,
    dry_run: bool,
    out: Callable[[str], None] = print,
) -> tuple[int, int]:
    """Pull a standalone `table` for [start, end) and insert the rows we don't already have."""
    spec = SYNCABLE[table]
    body = _get(peer_url, "/sync/rows", {"table": table, "start": iso(start), "end": iso(end)})
    columns: list[str] = body["columns"]
    missing = [k for k in spec.key if k not in columns]
    if missing:
        raise SyncError(f"the peer's {table} rows lack the key column(s) {', '.join(missing)}")
    key_at = [columns.index(k) for k in spec.key]
    coerce = _coercers(conn, table, columns)
    have = _present_keys(conn, table, spec, start, end)
    to_insert: list[list] = []
    skipped = 0
    for row in body["rows"]:
        key = tuple(_key(row[i]) for i in key_at)
        if key in have:
            skipped += 1
            continue
        have.add(key)  # don't import the same key twice within one batch
        to_insert.append([fn(v) for fn, v in zip(coerce, row)])
    if to_insert and not dry_run:
        collist = ", ".join(f'"{c}"' for c in columns)
        places = ", ".join(["%s"] * len(columns))
        with db.errors_as(f"importing into {table}"), conn.transaction():
            conn.cursor().executemany(f'INSERT INTO "{table}" ({collist}) VALUES ({places})', to_insert)
    _report(out, table, len(to_insert), skipped, dry_run)
    return len(to_insert), skipped


def sync_readings(
    conn: psycopg.Connection,
    peer_url: str,
    start: datetime.datetime,
    end: datetime.datetime,
    dry_run: bool,
    out: Callable[[str], None] = print,
) -> tuple[int, int]:
    """Pull the readings cluster for [start, end): a fresh local poll_id per poll, the database assigning reading
    ids, raw rows relinked by nesting. Whole window in one transaction."""
    body = _get(peer_url, "/sync/readings", {"start": iso(start), "end": iso(end)})
    manifest = body["columns"]
    _check_manifest(conn, manifest)
    have = _present_reading_keys(conn, start, end)
    counts = [0, 0]  # [imported, already-present]

    def process() -> None:
        for poll in body["polls"]:
            new = [r for r in poll["readings"] if (r["unit_id"], _key(r["started_at"])) not in have]
            counts[1] += len(poll["readings"]) - len(new)
            if not new:
                continue
            poll_id = conn.execute("SELECT nextval('poll_ids')").fetchone()[0] if not dry_run else None
            for reading in new:
                have.add((reading["unit_id"], _key(reading["started_at"])))
                if not dry_run:
                    _insert_reading(conn, reading, poll_id, manifest)
                counts[0] += 1

    if dry_run:
        process()
    else:
        with db.errors_as("importing readings"), conn.transaction():
            process()
    _report(out, "readings", counts[0], counts[1], dry_run)
    return counts[0], counts[1]


def _insert_reading(conn: psycopg.Connection, reading: dict, poll_id: int, manifest: dict[str, list[str]]) -> None:
    """Insert one wire-form reading under local `poll_id`, then its raw
    register row (if any) against the reading id the database assigned.
    `manifest` gives each raw table's column order."""
    started_at = parse_iso(reading["started_at"])
    new_id = conn.execute(
        "INSERT INTO readings (ip_addr, started_at, ended_at, version, error, unit_id, poll_id, glitch) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            ipaddress.ip_address(reading["ip_addr"]),
            started_at,
            parse_iso(reading["ended_at"]),
            reading["version"],
            reading["error"],
            reading["unit_id"],
            poll_id,
            reading["glitch"],
        ),
    ).fetchone()[0]
    for table in (RAW_PLANT, RAW_INVERTER):
        if table in reading:
            cols = manifest[table]
            collist = ", ".join(f'"{c}"' for c in cols)
            places = ", ".join(["%s"] * len(cols))
            conn.execute(
                f'INSERT INTO "{table}" (reading_id, started_at, {collist}) VALUES (%s, %s, {places})',
                [new_id, started_at, *reading[table]],
            )
            break


def _check_manifest(conn: psycopg.Connection, manifest: dict[str, list[str]]) -> None:
    """SyncError unless the peer's raw-table column lists match ours
    exactly: the positional raw arrays depend on it."""
    for table in (RAW_PLANT, RAW_INVERTER):
        if manifest.get(table) != _register_columns(conn, table):
            raise SyncError(
                f"the peer's {table} columns don't match this database's -- both must be on the same "
                f"migrations (they passed the schema check, so this shouldn't happen)"
            )


def _present_keys(
    conn: psycopg.Connection, table: str, spec: Syncable, start: datetime.datetime, end: datetime.datetime
) -> set[tuple[Any, ...]]:
    """The natural keys (canonicalised by _key) of the rows `table` already
    holds in [start, end), good or not."""
    cols = ", ".join(f'"{c}"' for c in spec.key)
    with db.errors_as(f"reading existing {table}"):
        rows = conn.execute(
            f'SELECT {cols} FROM "{table}" WHERE "{spec.time_column}" >= %s AND "{spec.time_column}" < %s',
            (start, end),
        ).fetchall()
    return {tuple(_key(v) for v in row) for row in rows}


def _present_reading_keys(
    conn: psycopg.Connection, start: datetime.datetime, end: datetime.datetime
) -> set[tuple[int, str]]:
    """(unit_id, started_at as UTC ISO) of every local reading in
    [start, end), errored ones included."""
    with db.errors_as("reading existing readings"):
        rows = conn.execute(
            "SELECT unit_id, started_at FROM readings WHERE started_at >= %s AND started_at < %s", (start, end)
        ).fetchall()
    return {(unit_id, _key(started_at)) for unit_id, started_at in rows}


def _coercers(conn: psycopg.Connection, table: str, columns: list[str]) -> list[Callable[[Any], Any]]:
    """One coercer per column: JSON hands timestamps back as ISO strings and inet as plain strings, so turn those
    into the types the local schema stores; every other type comes back ready to insert."""
    types = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        )
    }

    def to_ts(value: str | None) -> datetime.datetime | None:
        return None if value is None else parse_iso(value)

    def to_inet(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        return None if value is None else ipaddress.ip_address(value)

    def coercer(column: str) -> Callable[[Any], Any]:
        kind = types.get(column, "")
        if kind.startswith("timestamp"):
            return to_ts
        if kind == "inet":
            return to_inet
        return _identity

    return [coercer(c) for c in columns]


def _identity(value: Any) -> Any:
    """The coercer for columns that need none."""
    return value


def _key(value: Any) -> Any:
    """Canonicalise a key value so the peer's JSON (timestamps as strings) compares equal to the local database's
    (timestamps as datetimes)."""
    if isinstance(value, datetime.datetime):
        return iso(value)
    if isinstance(value, str):
        try:
            return iso(parse_iso(value))
        except SyncError:
            return value
    return value


def _report(out: Callable[[str], None], what: str, imported: int, skipped: int, dry_run: bool) -> None:
    """Print one table's summary line through `out`."""
    out(
        f"  {what}: {imported} to import, {skipped} already present"
        + (" (dry run, nothing written)" if dry_run else "")
    )


def _get(peer_url: str, path: str, params: dict[str, str] | None = None) -> dict:
    """GET a peer /sync/* endpoint as JSON (TIMEOUT seconds). SyncError on
    an HTTP error, no connection, bad JSON, or an {"error": ...} body.
    Tests monkeypatch this to fake the peer."""
    full = peer_url.rstrip("/") + path
    if params:
        full += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"accept": "application/json", "user-agent": "sigen-modbus sync"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SyncError(f"the peer at {full} returned HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SyncError(f"couldn't reach the peer at {full}: {getattr(e, 'reason', e)}")
    if not isinstance(body, dict):
        raise SyncError(f"the peer at {full} sent a JSON {type(body).__name__}, not an object")
    if body.get("error"):
        raise SyncError(f"the peer at {full} reported: {body['error']}")
    return body
