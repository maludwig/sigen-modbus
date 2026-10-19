"""sigen.sync: the peer backfill. DB-backed pieces run on the shared test schema; the HTTP client is tested by
faking sync._get (no network), and the dashboard endpoints via Flask's test client."""

import datetime

import pytest

from sigen import dashboard, db, sync
from sigen.config import Config
from sigen.errors import SchemaNotReady, SyncError
from sigen.registers import ALL_REGISTERS, PLANT_UNIT_ID

from test_db import conn, db_url  # noqa: F401  (pytest fixtures)

UTC = datetime.UTC


def _t(minute, second=0, micro=0):
    return datetime.datetime(2026, 10, 17, 16, minute, second, micro, tzinfo=UTC)


def _weather(conn, valid_at, resolution=15, temp=5.0, error=None):
    conn.execute(
        "INSERT INTO open_meteo_weather (fetched_at, resolution_minutes, valid_at, temperature_2m, error) "
        "VALUES (%s, %s, %s, %s, %s)",
        (_t(0), resolution, valid_at, None if error else temp, error),
    )


# ---------- kinds ----------
def test_resolve_kinds():
    assert sync.resolve_kinds(None) == sync.KINDS  # None -> all
    assert sync.resolve_kinds(["weather", "readings"]) == ["weather", "readings"]
    assert sync.resolve_kinds(["weather", "weather"]) == ["weather"]  # de-duplicated
    with pytest.raises(SyncError, match="unknown sync kind"):
        sync.resolve_kinds(["nope"])


# ---------- times ----------
def test_parse_iso_accepts_z_and_offset():
    assert sync.parse_iso("2026-10-17T16:02:15.010474Z") == _t(2, 15, 10474)
    assert sync.parse_iso("2026-10-17T10:02:15-06:00") == _t(2, 15)  # offset normalised to UTC
    with pytest.raises(SyncError):
        sync.parse_iso("not-a-time")


# ---------- export (peer side) ----------
def test_export_rows_good_in_window_only(conn):
    _weather(conn, _t(5))  # good, in window
    _weather(conn, _t(9))  # good, in window
    _weather(conn, _t(30))  # good, out of window
    _weather(conn, None, error="HTTP 500")  # error row (valid_at NULL): excluded
    body = sync.export_rows(conn, "open_meteo_weather", _t(0), _t(15))
    assert "id" not in body["columns"]  # identity column dropped
    valid_at = [row[body["columns"].index("valid_at")] for row in body["rows"]]
    assert valid_at == [sync.iso(_t(5)), sync.iso(_t(9))]  # only good + in window, ordered, times as ISO


def test_export_unknown_table(conn):
    with pytest.raises(SyncError, match="syncable"):
        sync.export_rows(conn, "readings", _t(0), _t(15))


# ---------- schema state / guard ----------
def test_schema_state_fully_migrated(conn):
    state = sync.schema_state(conn)
    assert state["fully_migrated"] is True
    assert {int(v) for v in state["applied"]} == {m.version for m in db.load_migrations()}


def test_check_schema_rejects_a_different_peer(conn, monkeypatch):
    monkeypatch.setattr(sync, "_get", lambda url, path, params=None: {"applied": {"1": "deadbeef"}})
    with pytest.raises(SchemaNotReady, match="different set of migrations"):
        sync.check_schema(conn, "http://peer")


def test_check_schema_ok_when_matched(conn, monkeypatch):
    matched = {str(m.version): m.checksum for m in db.load_migrations()}
    monkeypatch.setattr(sync, "_get", lambda url, path, params=None: {"applied": matched})
    sync.check_schema(conn, "http://peer")  # no raise


# ---------- sync_table (client side), peer faked ----------
WEATHER_COLS = ["fetched_at", "resolution_minutes", "valid_at", "temperature_2m", "error"]


def _peer_body(valids):
    return {
        "table": "open_meteo_weather",
        "columns": WEATHER_COLS,
        "rows": [[sync.iso(_t(0)), 15, sync.iso(v), 5.0, None] for v in valids],
    }


def test_sync_table_dry_run_then_import_then_idempotent(conn, monkeypatch):
    monkeypatch.setattr(sync, "_get", lambda url, path, params=None: _peer_body([_t(5), _t(9)]))
    present = lambda: conn.execute(
        "SELECT count(*) FROM open_meteo_weather WHERE valid_at >= %s AND valid_at < %s", (_t(0), _t(15))
    ).fetchone()[0]

    assert sync.sync_standalone(conn, "http://peer", "open_meteo_weather", _t(0), _t(15), True, out=lambda m: None) == (
        2,
        0,
    )
    assert present() == 0  # dry run wrote nothing

    assert sync.sync_standalone(
        conn, "http://peer", "open_meteo_weather", _t(0), _t(15), False, out=lambda m: None
    ) == (2, 0)
    assert present() == 2  # imported

    assert sync.sync_standalone(
        conn, "http://peer", "open_meteo_weather", _t(0), _t(15), False, out=lambda m: None
    ) == (0, 2)
    assert present() == 2  # re-run is a no-op: both already present


def test_sync_table_skips_rows_already_local(conn, monkeypatch):
    _weather(conn, _t(5))  # already have this period locally
    monkeypatch.setattr(sync, "_get", lambda url, path, params=None: _peer_body([_t(5), _t(9)]))
    got, skipped = sync.sync_standalone(
        conn, "http://peer", "open_meteo_weather", _t(0), _t(15), False, out=lambda m: None
    )
    assert (got, skipped) == (1, 1)  # only the new period imported


# ---------- the dashboard endpoints ----------
def test_sync_endpoints(conn, db_url):
    _weather(conn, _t(5))
    _weather(conn, _t(9))
    client = dashboard.create_app(Config(database_url=db_url, origin="t"), launcher=None).test_client()

    schema = client.get("/sync/schema")
    assert schema.status_code == 200 and schema.get_json()["fully_migrated"] is True

    rows = client.get(
        "/sync/rows", query_string={"table": "open_meteo_weather", "start": sync.iso(_t(0)), "end": sync.iso(_t(15))}
    )
    assert rows.status_code == 200
    body = rows.get_json()
    assert body["table"] == "open_meteo_weather" and len(body["rows"]) == 2 and "id" not in body["columns"]

    assert (
        client.get("/sync/rows", query_string={"table": "readings", "start": "x", "end": "y"}).status_code == 404
    )  # not a syncable table
    assert (
        client.get("/sync/rows", query_string={"table": "open_meteo_weather", "start": "bad", "end": "bad"}).status_code
        == 400
    )


# ---------- standalone: the battery_heater entry ----------
def test_export_battery_heater_good_only(conn):
    db.insert_battery_heater(conn, polled_at=_t(5), battery=1, heater_on=False)
    db.insert_battery_heater(conn, polled_at=_t(9), battery=2, heater_on=None, error="cloud down")  # error: excluded
    body = sync.export_rows(conn, "battery_heater", _t(0), _t(15))
    assert "id" not in body["columns"]
    polled = [row[body["columns"].index("polled_at")] for row in body["rows"]]
    assert polled == [sync.iso(_t(5))]  # only the good row


# ---------- standalone: the satellite_shade entry ----------
def test_export_satellite_shade_good_only(conn):
    from sigen.goes_shade import ShadeSeries

    nan = float("nan")
    series = ShadeSeries(
        times=[_t(5)], sun_elevation=[30.0], sun_azimuth=[nan], columns={c: [0.5] for c in db.SATELLITE_SHADE_VALUES}
    )
    db.insert_satellite_shade(conn, _t(6), series)
    db.insert_satellite_shade(conn, _t(9), None, error="NOAA down")  # error row: excluded
    body = sync.export_rows(conn, "satellite_shade", _t(0), _t(15))
    assert "id" not in body["columns"]
    scans = [row[body["columns"].index("scan_at")] for row in body["rows"]]
    assert scans == [sync.iso(_t(5))]  # only the good scan


# ---------- the readings cluster ----------
def _insert_poll(conn, at=None):
    at = at or _t(5)
    plant = next(r for r in ALL_REGISTERS if r.device == "plant" and r.stored)
    inverter = next(r for r in ALL_REGISTERS if r.device == "inverter" and r.stored)
    db.insert_reading(
        conn,
        ip="192.168.1.50",
        unit_id=PLANT_UNIT_ID,
        poll_id=1,
        started_at=at,
        ended_at=at,
        values=[(plant.address, 123)],
    )
    db.insert_reading(
        conn,
        ip="192.168.1.50",
        unit_id=1,
        poll_id=1,
        started_at=at + datetime.timedelta(microseconds=100),
        ended_at=at,
        values=[(inverter.address, 456)],
    )
    return plant, inverter


def test_export_readings_nests_by_poll_without_ids(conn):
    _insert_poll(conn)
    body = sync.export_readings(conn, _t(0), _t(15))
    assert set(body["columns"]) == {sync.RAW_PLANT, sync.RAW_INVERTER}  # the raw column manifest
    assert len(body["polls"]) == 1
    readings = body["polls"][0]["readings"]
    assert len(readings) == 2
    for reading in readings:
        assert "id" not in reading and "poll_id" not in reading  # no ids cross the wire
        assert (sync.RAW_PLANT in reading) ^ (sync.RAW_INVERTER in reading)  # exactly one raw array


def test_sync_readings_dry_then_import_then_idempotent(conn, monkeypatch):
    plant, _inverter = _insert_poll(conn)
    body = sync.export_readings(conn, _t(0), _t(15))
    conn.execute("DELETE FROM raw_plant_register_values")
    conn.execute("DELETE FROM raw_inverter_register_values")
    conn.execute("DELETE FROM readings")
    monkeypatch.setattr(sync, "_get", lambda url, path, params=None: body)
    count = lambda: conn.execute("SELECT count(*) FROM readings").fetchone()[0]

    assert sync.sync_readings(conn, "http://peer", _t(0), _t(15), True, out=lambda m: None) == (2, 0)
    assert count() == 0  # dry run wrote nothing

    assert sync.sync_readings(conn, "http://peer", _t(0), _t(15), False, out=lambda m: None) == (2, 0)
    assert count() == 2
    assert conn.execute("SELECT count(DISTINCT poll_id) FROM readings").fetchone()[0] == 1  # one fresh poll_id
    assert conn.execute(f'SELECT "{plant.name}" FROM raw_plant_register_values').fetchone()[0] == 123  # value kept

    assert sync.sync_readings(conn, "http://peer", _t(0), _t(15), False, out=lambda m: None) == (0, 2)
    assert count() == 2  # re-run imports nothing


# ---------- real HTTP round-trip (client <-> a live dashboard) ----------
@pytest.fixture
def live_url(db_url):
    import threading

    from werkzeug.serving import make_server

    app = dashboard.create_app(Config(database_url=db_url, origin="t"), launcher=None)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(2)


def test_roundtrip_over_http(conn, live_url):
    _weather(conn, _t(5))
    _weather(conn, _t(9))
    _insert_poll(conn)
    sync.check_schema(conn, live_url)  # the guard, over real HTTP
    rows = sync._get(
        live_url, "/sync/rows", {"table": "open_meteo_weather", "start": sync.iso(_t(0)), "end": sync.iso(_t(15))}
    )
    assert len(rows["rows"]) == 2  # Z-form times survive URL-encoding
    readings = sync._get(live_url, "/sync/readings", {"start": sync.iso(_t(0)), "end": sync.iso(_t(15))})
    assert len(readings["polls"]) == 1 and len(readings["polls"][0]["readings"]) == 2


# ---------- `sigen sync` recomputes daily totals after a readings import ----------
def test_sync_recomputes_daily_after_readings(db_url, tmp_path, monkeypatch):
    import json
    import types

    from sigen import cli, daily

    cfg = tmp_path / "config.json"
    site = {"latitude": 51.0, "longitude": -114.0, "timezone": "America/Edmonton"}
    cfg.write_text(json.dumps({"database": {"url": db_url}, "site": site}), encoding="utf-8")
    recomputed = []
    monkeypatch.setattr(cli.sync, "check_schema", lambda c, url: None)

    def update(c, tz, first, last, site=None, array=None, log=None):
        recomputed.append((first, last))
        assert site is not None and site.latitude == 51.0  # the clear-sky model's inputs go along
        return []

    monkeypatch.setattr(daily, "update", update)
    args = types.SimpleNamespace(
        config=cfg,
        no_config=False,
        url="http://peer",
        start_at="2026-10-17T16:00:00Z",
        end_at="2026-10-17T17:00:00Z",
        only_sync=["readings"],
        dry_run=False,
        fill_gaps=False,
        since="7d",
        min_gap=None,
    )

    monkeypatch.setattr(cli.sync, "sync_kind", lambda c, url, kind, s, e, d, out: (2, 0))  # readings imported
    assert cli.cmd_sync(args) == cli.ExitCode.OK
    assert len(recomputed) == 1  # fresh readings -> daily recompute fired

    recomputed.clear()
    monkeypatch.setattr(cli.sync, "sync_kind", lambda c, url, kind, s, e, d, out: (0, 5))  # nothing new
    assert cli.cmd_sync(args) == cli.ExitCode.OK
    assert recomputed == []  # nothing imported -> no recompute

    recomputed.clear()
    args.dry_run = True
    monkeypatch.setattr(cli.sync, "sync_kind", lambda c, url, kind, s, e, d, out: (2, 0))
    assert cli.cmd_sync(args) == cli.ExitCode.OK
    assert recomputed == []  # dry run -> no recompute


# ---------- a peer's malformed answers are SyncError ----------
def test_a_peer_answer_that_isnt_an_object_is_sync_error(monkeypatch):
    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"[1, 2]"

    monkeypatch.setattr(sync.urllib.request, "urlopen", lambda req, timeout: Reply())
    with pytest.raises(SyncError, match="JSON list, not an object"):
        sync._get("http://peer", "/sync/schema")


def test_a_good_peer_row_replaces_a_local_error_row(conn, monkeypatch):
    t = _t(5)
    conn.execute("INSERT INTO battery_heater (polled_at, battery, error) VALUES (%s, 1, 'timed out')", (t,))
    body = {"columns": ["polled_at", "battery", "heater_on", "error"], "rows": [[sync.iso(t), 1, True, None]]}
    monkeypatch.setattr(sync, "_get", lambda url, path, params=None: body)
    got = sync.sync_standalone(conn, "http://peer", "battery_heater", _t(0), _t(15), dry_run=False, out=lambda m: None)
    assert got == (1, 0)  # imported, not skipped as "already have it"
    assert conn.execute("SELECT battery, heater_on, error FROM battery_heater").fetchall() == [(1, True, None)]


def test_peer_rows_without_a_key_column_are_sync_error(monkeypatch):
    monkeypatch.setattr(sync, "_get", lambda url, path, params=None: {"columns": ["temperature_2m"], "rows": []})
    with pytest.raises(SyncError, match="lack the key column"):
        sync.sync_standalone(None, "http://peer", "open_meteo_weather", _t(0), _t(15), dry_run=True)
