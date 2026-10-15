"""Against a real PostgreSQL. Skipped unless there's a test database: SIGEN_TEST_DB_URL, e.g.

    SIGEN_TEST_DB_URL=postgresql://postgres:…@localhost:5432/postgres pytest

or else database.test_url in the repo's config.json (docs/DEVELOPMENT.md says how to set one up).
The tests work in throwaway schemas (sigen_test_<random>), dropped afterwards, so nothing else in
that database is touched: one migrated schema shared by most tests and emptied after each, and a new,
empty one for each test marked fresh_schema (see db_url).
"""
import datetime
import json
import socket

import pytest

from conftest import base_database_url, drop_schema, new_schema, schema_url  # noqa: F401  (scratch tools use the first)
from sigen import db
from sigen.errors import ExitCode, SchemaNotReady
from sigen.registers import PLANT_REGISTERS
from sigen.weather import Response

psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def db_url(request, shared_schema):
    """The test's schema. By default the shared one, already migrated, and emptied after the test (rebuilt if the test
    changed its shape: see conftest.SharedSchema), so `db migrate` in a test finds nothing to do. Tests of migrating
    itself, or that need the schema as it was before, are marked @pytest.mark.fresh_schema: they get a new, empty
    schema of their own, dropped afterwards."""
    if shared_schema is None:
        pytest.skip("set SIGEN_TEST_DB_URL, or database.test_url in config.json, to run the database tests")
    if request.node.get_closest_marker("fresh_schema") is None:
        yield shared_schema.url
        shared_schema.reset()
        return
    schema = new_schema(shared_schema.base)
    try:
        yield schema_url(shared_schema.base, schema)
    finally:
        drop_schema(shared_schema.base, schema)


@pytest.fixture
def config(tmp_path, db_url):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"database": {"url": db_url}}), encoding="utf-8")
    return f


@pytest.fixture
def conn(db_url):
    c = db.connect(db_url)
    yield c
    c.close()


def net(server):
    return ["--ip", "127.0.0.1", "--port", server.port]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------- migrate ----------
@pytest.mark.fresh_schema
def test_migrate_creates_everything_and_is_idempotent(run, config, conn):
    code, out, _ = run("db", "migrate", "--config", config)
    assert code == ExitCode.OK and "0001_initial" in out and "0002_units" in out and "0003_weather" in out
    assert "0004_wide_values" in out and "0005_alert_log" in out and "0006_daily_energy" in out
    assert "Applied 6 migration" in out
    assert conn.execute("SELECT count(*) FROM register_meta").fetchone()[0] == len(db.STORED_REGISTERS)
    assert conn.execute("SELECT name, type, gain, unit, device FROM register_meta WHERE address = 30005").fetchone() \
        == ("plant_grid_sensor_active_power", "s32", 1000.0, "kW", "plant")
    assert conn.execute("SELECT device FROM register_meta WHERE address = 31027").fetchone() == ("inverter",)
    code, out, _ = run("db", "migrate", "--config", config)
    assert code == ExitCode.OK and "No migrations pending" in out


def test_view_columns_match_the_views(conn):
    db.migrate(conn)
    for device in db.DEVICES:
        columns = {c for c, in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = %s", (f"{device}_register_values",))}
        expected = {db.view_column(r) for r in db.STORED_REGISTERS if r.device == device}
        assert columns == expected | {"reading_id", "started_at"}, device


@pytest.mark.fresh_schema
def test_0002_upgrades_existing_readings(conn):
    # Build a database exactly as 0001 left it, with a reading in it.
    # (db.migrate can't stop at 0001: its register_meta sync is written for today's schema.)
    first = db.load_migrations()[0]
    conn.execute(db.BOOTSTRAP_SQL)
    conn.execute(first.sql)
    conn.execute("INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                 (first.version, first.name, first.checksum))
    conn.execute("INSERT INTO register_meta (address, name, type, gain, unit) "
                 "VALUES (30014, 'plant_ess_soc', 'u16', 10, '%')")
    rid = conn.execute("INSERT INTO readings (ip_addr, started_at, ended_at, version) "
                       "VALUES ('192.168.1.100', now(), now(), '0.1.0') RETURNING id").fetchone()[0]
    conn.execute("INSERT INTO register_values VALUES (%s, 30014, 880)", (rid,))
    assert db.migrate(conn) == ["0002_units", "0003_weather", "0004_wide_values", "0005_alert_log",
                                "0006_daily_energy"]
    assert conn.execute("SELECT unit_id, poll_id FROM readings WHERE id = %s", (rid,)).fetchone() == (247, rid)
    assert conn.execute("SELECT plant_ess_soc FROM raw_plant_register_values WHERE reading_id = %s",
                        (rid,)).fetchone() == (880,)
    assert conn.execute("SELECT device FROM register_meta WHERE address = 30014").fetchone() == ("plant",)
    assert db.next_poll_id(conn) > rid                       # new polls never reuse an old poll id


def test_migrate_refuses_edited_migration(conn):
    db.migrate(conn)
    edited = [db.Migration(m.version, m.name, m.sql + "\n-- tweak\n") for m in db.load_migrations()]
    with pytest.raises(SchemaNotReady, match="edited after it was applied"):
        db.migrate(conn, edited)


@pytest.mark.fresh_schema
def test_migrate_refuses_unknown_applied_migration(conn):
    db.migrate(conn)
    conn.execute("INSERT INTO schema_migrations (version, name, checksum) VALUES (99, 'future', 'x')")
    with pytest.raises(SchemaNotReady, match="doesn't know"):
        db.migrate(conn)


def test_failed_migration_leaves_nothing_behind(conn):
    good = db.load_migrations()
    bad = good + [db.Migration(len(good) + 1, "broken", "CREATE TABLE half_done (x int);\nSELECT * FROM nope;\n")]
    with pytest.raises(Exception):
        db.migrate(conn, bad)
    assert conn.execute("SELECT to_regclass('half_done')").fetchone()[0] is None
    assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == len(good)


# ---------- 0004: wide value tables ----------
def at_0003(conn):
    """A database exactly as migrations 0001-0003 left it (register_meta synced)."""
    db.migrate(conn, db.load_migrations()[:3])


def old_reading(conn, unit_id, values, when):
    """A reading stored the pre-0004 way: one register_values row per value."""
    rid = conn.execute("INSERT INTO readings (ip_addr, unit_id, poll_id, started_at, ended_at, version) "
                       "VALUES ('127.0.0.1', %s, nextval('poll_ids'), %s, %s, 'test') RETURNING id",
                       (unit_id, when, when)).fetchone()[0]
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO register_values VALUES (%s, %s, %s)", [(rid, a, v) for a, v in values])
    return rid


def sample_values(device, k):
    """A value for every stored register of `device`, varying with k, with each type's extremes in it."""
    extremes = {"u16": 65535, "s16": -32768, "u32": 4294967295, "s32": -2147483648, "u64": -1}
    return [(r.address, extremes[r.type] if (r.address + k) % 7 == 0 else (r.address + 31 * k) % 1000 - 500 * (k % 2)
             * (r.type[0] == "s")) for r in db.STORED_REGISTERS if r.device == device]


@pytest.mark.fresh_schema
def test_0004_moves_every_value(conn):
    at_0003(conn)
    when = datetime.datetime(2026, 10, 9, 12, tzinfo=datetime.timezone.utc)
    plant = [old_reading(conn, 247, sample_values("plant", k), when + datetime.timedelta(seconds=15 * k)) for k in range(3)]
    inverter = [old_reading(conn, 1, sample_values("inverter", k), when + datetime.timedelta(seconds=15 * k))
                for k in range(3)]
    old_reading(conn, 247, [(30014, 880)], when + datetime.timedelta(minutes=5))        # an early, partial reading
    before = sorted(conn.execute("SELECT reading_id, address, value FROM register_values").fetchall())

    assert db.migrate(conn) == ["0004_wide_values", "0005_alert_log", "0006_daily_energy"]
    assert sorted(conn.execute("SELECT reading_id, address, value FROM register_values").fetchall()) == before
    assert sorted(conn.execute("SELECT reading_id, address, value FROM register_values_v1").fetchall()) == before
    assert conn.execute("SELECT count(*) FROM raw_plant_register_values").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM raw_inverter_register_values").fetchone()[0] == 3
    rid, k = plant[1], 1
    row = dict(zip(["started_at", "plant_ess_soc", "plant_photovoltaic_power"], conn.execute(
        "SELECT started_at, plant_ess_soc, plant_photovoltaic_power FROM raw_plant_register_values WHERE reading_id = %s",
        (rid,)).fetchone()))
    assert row["started_at"] == when + datetime.timedelta(seconds=15)
    assert row["plant_ess_soc"] == dict(sample_values("plant", k))[30014]
    # The scaled views: <name>_<unit>, value / gain; a register without a unit keeps its name.
    soc, pv, mode = conn.execute("SELECT plant_ess_soc_pct, plant_photovoltaic_power_kw, plant_ems_work_mode "
                                 "FROM plant_register_values WHERE reading_id = %s", (rid,)).fetchone()
    assert float(soc) == row["plant_ess_soc"] / 10 and float(pv) == row["plant_photovoltaic_power"] / 1000
    assert mode == dict(sample_values("plant", k))[30003]
    assert conn.execute("SELECT inverter_pv1_voltage_v FROM inverter_register_values WHERE reading_id = %s",
                        (inverter[0],)).fetchone()[0] is not None
    # The partial reading: one value, every other column NULL (not 0).
    partial = conn.execute("SELECT plant_ess_soc, plant_photovoltaic_power FROM raw_plant_register_values "
                           "ORDER BY reading_id DESC LIMIT 1").fetchone()
    assert partial == (880, None)
    db.check_ready(conn)                                                   # poll could run


@pytest.mark.fresh_schema
def test_0004_refuses_a_value_it_cannot_place(conn):
    at_0003(conn)
    conn.execute("INSERT INTO register_meta (address, name, type, gain, unit, device) "
                 "VALUES (39990, 'removed_from_the_map', 'u16', 1, NULL, 'plant')")
    old_reading(conn, 247, [(30014, 880), (39990, 5)], datetime.datetime(2026, 10, 9, tzinfo=datetime.timezone.utc))
    with pytest.raises(Exception, match="2 values before, 1 after"):
        db.migrate(conn)
    # One transaction: nothing changed.
    assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 3
    assert conn.execute("SELECT to_regclass('raw_plant_register_values')").fetchone()[0] is None
    assert conn.execute("SELECT count(*) FROM register_values").fetchone()[0] == 2           # still the table


def backfill(conn):
    from importlib import resources
    conn.execute((resources.files("sigen.migrations") / "backfill" / "0004_wide_values.sql").read_text(encoding="utf-8"))


@pytest.mark.fresh_schema
def test_backfill_runs_while_poll_writes_then_migrate_copies_the_rest(conn):
    at_0003(conn)
    t = datetime.datetime(2026, 10, 9, 12, tzinfo=datetime.timezone.utc)
    step = datetime.timedelta(seconds=15)
    for k in range(3):                                                     # history
        old_reading(conn, 247, sample_values("plant", k), t + 2 * k * step)
        old_reading(conn, 1, sample_values("inverter", k), t + (2 * k + 1) * step)
    backfill(conn)
    rows = lambda: conn.execute("SELECT (SELECT count(*) FROM raw_plant_register_values), "
                                "(SELECT count(*) FROM raw_inverter_register_values)").fetchone()
    assert rows() == (3, 3)
    assert not db.wide_layout(conn)                    # still the table poll writes to: charts keep reading it
    assert conn.execute("SELECT to_regclass('register_values_v1')").fetchone()[0] is None
    old_reading(conn, 247, sample_values("plant", 7), t + 10 * step)      # poll kept writing
    backfill(conn)                                                         # again: only what's new
    assert rows() == (4, 3)
    old_reading(conn, 1, sample_values("inverter", 8), t + 11 * step)     # the last minutes before the switch
    conn.execute("INSERT INTO readings (ip_addr, unit_id, poll_id, started_at, ended_at, version, error) "
                 "VALUES ('127.0.0.1', 247, nextval('poll_ids'), now(), now(), 'test', 'timed out')")   # a failed one
    before = sorted(conn.execute("SELECT reading_id, address, value FROM register_values").fetchall())

    assert db.migrate(conn) == ["0004_wide_values", "0005_alert_log", "0006_daily_energy"]
    assert rows() == (4, 4) and db.wide_layout(conn)
    assert sorted(conn.execute("SELECT reading_id, address, value FROM register_values").fetchall()) == before
    assert sorted(conn.execute("SELECT reading_id, address, value FROM register_values_v1").fetchall()) == before
    db.check_ready(conn)


@pytest.mark.fresh_schema
def test_backfill_refuses_a_value_it_cannot_place(conn):
    at_0003(conn)
    conn.execute("INSERT INTO register_meta (address, name, type, gain, unit, device) "
                 "VALUES (39990, 'removed_from_the_map', 'u16', 1, NULL, 'plant')")
    old_reading(conn, 247, [(30014, 880), (39990, 5)], datetime.datetime(2026, 10, 9, tzinfo=datetime.timezone.utc))
    with pytest.raises(Exception, match="2 values before, 1 after"):
        backfill(conn)
    conn.execute("ROLLBACK")                           # the script's own transaction is left aborted
    assert conn.execute("SELECT to_regclass('raw_plant_register_values')").fetchone()[0] is None


@pytest.mark.fresh_schema
def test_poll_refuses_a_register_without_a_column(conn):
    db.migrate(conn)
    conn.execute("ALTER TABLE raw_plant_register_values DROP COLUMN plant_ess_soc CASCADE")   # takes the views too
    with pytest.raises(SchemaNotReady, match=r"plant_ess_soc \(30014\) has no column in raw_plant_register_values"):
        db.check_ready(conn)


def test_insert_reading_writes_one_wide_row(conn):
    db.migrate(conn)
    t = datetime.datetime(2026, 10, 9, 12, tzinfo=datetime.timezone.utc)
    rid = db.insert_reading(conn, ip="127.0.0.1", unit_id=1, poll_id=db.next_poll_id(conn), started_at=t, ended_at=t,
                            values=[(31027, 2941), (31028, 1234)])
    assert conn.execute("SELECT started_at, inverter_pv1_voltage, inverter_pv1_current FROM "
                        "raw_inverter_register_values WHERE reading_id = %s", (rid,)).fetchone() == (t, 2941, 1234)
    with pytest.raises(ValueError, match="one device"):
        db.insert_reading(conn, ip="127.0.0.1", unit_id=1, poll_id=1, started_at=t, ended_at=t,
                          values=[(31027, 1), (30014, 2)])
    with pytest.raises(ValueError, match="not in the stored register map"):
        db.insert_reading(conn, ip="127.0.0.1", unit_id=1, poll_id=1, started_at=t, ended_at=t, values=[(39990, 1)])


# ---------- status ----------
@pytest.mark.fresh_schema
def test_status_before_and_after_migrate(run, config):
    db_url = url_of(config)
    code, out, _ = run("db", "status", "--config", config)
    assert code == ExitCode.PRECONDITION and "0001_initial" in out
    run("db", "migrate", "--config", config)
    code, out, _ = run("db", "status", "--config", config)
    assert code == ExitCode.OK and "Readings:      0" in out and f"Config:        {config}\n" in out
    assert "Open-Meteo:    0 rows (0 failed): 0 hourly, 0 every 15 min\n" in out
    assert "Radar frames:  0 (0 failed fetches)\n" in out
    lines = {line.split()[0]: line for line in out.split("Table sizes:")[1].splitlines()[1:]}
    assert "no time column" in lines["register_meta"]
    assert "(0 of 0 rows in the last 24 h, by started_at)" in lines["readings"]
    assert "(0 of 0 rows in the last 24 h, by alerted_at)" in lines["alert_log"]
    assert "/day" in lines["all"]
    password = psycopg.conninfo.conninfo_to_dict(db_url).get("password")
    if password:
        assert password not in out


def test_table_sizes_count_the_last_day(conn):
    db.migrate(conn)
    add_reading(conn)                                                   # now
    old = add_reading(conn)
    conn.execute("UPDATE readings SET started_at = now() - interval '3 days' WHERE id = %s", (old,))
    sizes = {t.name: t for t in db.table_sizes(conn)}
    readings = sizes["readings"]
    assert (readings.rows, readings.rows_last_day, readings.time_column) == (2, 1, "started_at")
    assert readings.bytes_per_day == readings.total_bytes / 2
    meta = sizes["register_meta"]
    assert meta.bytes_per_day is None and meta.time_column is None
    assert meta.rows_exact and meta.rows > 100          # counted: a fresh table's estimate is 0
    assert db.TableSize("t", 1000, 0, True, "x", 0).bytes_per_day == 0.0


# ---------- query ----------
def add_reading(conn, error=None):
    return conn.execute("INSERT INTO readings (ip_addr, unit_id, poll_id, started_at, ended_at, version, error) "
                        "VALUES ('192.168.1.100', 247, nextval('poll_ids'), now(), now(), 'test', %s) "
                        "RETURNING id", (error,)).fetchone()[0]


def test_query_prints_a_table(run, config, conn):
    run("db", "migrate", "--config", config)
    rid = add_reading(conn, error="timed out\nafter 3 s")
    code, out, _ = run("db", "query", "SELECT id, unit_id, host(ip_addr) AS ip, error FROM readings", "--config", config)
    assert code == ExitCode.OK
    header, rule, row, count = out.splitlines()
    assert [c.strip() for c in header.split("|")] == ["id", "unit_id", "ip", "error"]
    assert [c.strip() for c in row.split("|")] == [str(rid), "247", "192.168.1.100", "timed out\\nafter 3 s"]
    assert count == "(1 row)"


def test_query_refuses_writes(run, config, conn):
    run("db", "migrate", "--config", config)
    add_reading(conn)
    for sql in ("DELETE FROM readings",
                "COMMIT; DELETE FROM readings",                 # a COMMIT in the text doesn't escape
                "CREATE TABLE sneaky (x int)"):
        code, _, err = run("db", "query", sql, "--config", config)
        assert code == ExitCode.DATABASE and "read-only transaction" in err
    assert conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 1
    assert conn.execute("SELECT to_regclass('sneaky')").fetchone()[0] is None


def test_query_json_and_several_statements(run, config, conn):
    run("db", "migrate", "--config", config)
    code, out, _ = run("db", "query", "SELECT 1 AS a, NULL AS b, now() AS t; SET search_path TO DEFAULT; "
                       "SELECT 2 AS a", "--config", config, "--json")
    assert code == ExitCode.OK
    first, second, third = json.loads(out)
    assert first["columns"] == ["a", "b", "t"] and first["rows"][0][:2] == [1, None]
    assert isinstance(first["rows"][0][2], str) and first["status"] == "SELECT 1"
    assert second == {"status": "SET", "columns": None, "rows": [], "truncated": False}
    assert third["rows"] == [[2]]


def test_query_max_rows(run, config):
    run("db", "migrate", "--config", config)
    code, out, err = run("db", "query", "SELECT generate_series(1, 5) AS n", "--config", config, "--max-rows", 3)
    assert code == ExitCode.OK and out.splitlines()[-1] == "(3 rows)" and "stopped after 3 rows" in err
    code, out, err = run("db", "query", "SELECT generate_series(1, 5) AS n", "--config", config, "--max-rows", 0)
    assert out.splitlines()[-1] == "(5 rows)" and err == ""


def test_query_finds_config_json_by_itself(run, config, monkeypatch):
    run("db", "migrate", "--config", config)
    below = config.parent / "somewhere" / "deeper"
    below.mkdir(parents=True)
    monkeypatch.chdir(below)
    monkeypatch.delenv("SIGEN_CONFIG")
    code, out, _ = run("db", "query", "SELECT count(*) AS n FROM readings")
    assert code == ExitCode.OK and out.splitlines()[2].strip() == "0"


def test_query_error_is_exit_10(run, config):
    code, _, err = run("db", "query", "SELECT * FROM no_such_table", "--config", config)
    assert code == ExitCode.DATABASE and "no_such_table" in err


# ---------- poll ----------
@pytest.mark.fresh_schema
def test_poll_refuses_before_migrate(run, config, full_server):
    code, _, err = run("poll", *net(full_server), "--config", config, "--count", 1)
    assert code == ExitCode.PRECONDITION and "sigen db migrate" in err


def test_poll_stores_every_register(run, config, conn, full_server, quick_polls):
    run("db", "migrate", "--config", config)
    code, out, _ = run("poll", *net(full_server), "--config", config, "--count", 2, "--interval", 1)
    assert code == ExitCode.OK and "2 stored, 0 failed" in out
    code, out, _ = run("db", "status", "--config", config)
    assert code == ExitCode.OK and "Value rows:    2 plant, 0 inverter (one per successful reading)\n" in out
    assert conn.execute("SELECT count(*) FROM register_values").fetchone()[0] == 2 * len(PLANT_REGISTERS)  # the view
    rows = conn.execute("SELECT id, host(ip_addr), started_at, ended_at, version, error FROM readings ORDER BY id").fetchall()
    assert len(rows) == 2
    for rid, ip, started, ended, version, error in rows:
        assert ip == "127.0.0.1" and error is None and ended >= started
        n = conn.execute("SELECT count(*) FROM register_values WHERE reading_id = %s", (rid,)).fetchone()[0]
        assert n == len(PLANT_REGISTERS)
    battery = conn.execute("SELECT value FROM register_values WHERE reading_id = %s AND address = 30037",
                           (rows[0][0],)).fetchone()[0]
    assert battery == -1859                # decoded s32, before the gain


def test_poll_records_connection_failures_and_continues(run, config, conn):
    run("db", "migrate", "--config", config)
    code, _, err = run("poll", "--ip", "127.0.0.1", "--port", free_port(), "--timeout", 0.2,
                       "--config", config, "--count", 1)
    assert code == ExitCode.OK and "FAILED" in err
    (rid, error), = conn.execute("SELECT id, error FROM readings").fetchall()
    assert "couldn't connect" in error
    assert conn.execute("SELECT count(*) FROM register_values").fetchone()[0] == 0


def test_poll_stops_on_rejected_register(run, config, conn, server):
    # The 10-06 fake data predates most of the map, so blocks starting on registers it lacks
    # are rejected: exactly what a register-map bug looks like. Nothing partial is stored.
    run("db", "migrate", "--config", config)
    code, _, err = run("poll", *net(server), "--config", config, "--count", 5)
    assert code == ExitCode.MODBUS and "Stopping" in err
    (error,), = conn.execute("SELECT error FROM readings").fetchall()
    assert "Modbus exception 2" in error
    assert conn.execute("SELECT count(*) FROM register_values").fetchone()[0] == 0


def url_of(config):
    """The test schema's URL, read from the config file. Tests take `config`, never `db_url`: pytest prints a
    failing test's arguments, and the URL holds the test database's password."""
    return json.loads(config.read_text(encoding="utf-8"))["database"]["url"]


def test_poll_follows_the_device_to_a_new_ip(run, config, conn, full_server):
    # Two documentation addresses stand in for the old and new IP: the old one maps to a
    # port nobody listens on, the new one to the fake server. The stand-in rediscovery
    # plays the part of the MAC lookup.
    import functools
    from sigen import poll
    from sigen.modbus import ModbusClient
    db_url = url_of(config)
    run("db", "migrate", "--config", config)
    ports = {"192.0.2.1": free_port(), "192.0.2.2": full_server.port}
    asked = []

    def rediscover(ip):
        asked.append(ip)
        return "192.0.2.2"
    messages = []
    timeouts = {"192.0.2.1": 0.2, "192.0.2.2": 1.0}
    code = poll.run(connector_for=lambda ip, unit: functools.partial(ModbusClient, "127.0.0.1", ports[ip], unit,
                                                                     timeouts[ip]),
                    ip="192.0.2.1", units={247: "plant"}, db_url=db_url, interval=0.05, count=4,
                    rediscover=rediscover, rediscover_after=2, out=messages.append, err=messages.append)
    assert code == ExitCode.OK and asked == ["192.0.2.1"]
    rows = conn.execute("SELECT host(ip_addr), error IS NULL FROM readings ORDER BY id").fetchall()
    assert rows == [("192.0.2.1", False), ("192.0.2.1", False), ("192.0.2.2", True), ("192.0.2.2", True)]
    assert any("found it at 192.0.2.2" in m for m in messages)


def test_poll_takes_a_reading_per_unit(run, config, conn, tmp_path, both_server, quick_polls):
    f = tmp_path / "both.json"
    f.write_text(json.dumps({"database": {"url": url_of(config)}, "devices": {"inverter": {"units": [1]}}}),
                 encoding="utf-8")
    run("db", "migrate", "--config", f)
    code, out, err = run("poll", *net(both_server), "--config", f, "--count", 2, "--interval", 1)
    assert code == ExitCode.OK and "4 stored, 0 failed" in out and "units 247 (plant), 1 (inverter)" in err
    rows = conn.execute("""SELECT r.poll_id, r.unit_id, count(v.address) FROM readings r
                           LEFT JOIN register_values v ON v.reading_id = r.id
                           GROUP BY r.id ORDER BY r.id""").fetchall()
    stored = {device: sum(1 for r in db.STORED_REGISTERS if r.device == device) for device in ("plant", "inverter")}
    assert [(u, n) for _, u, n in rows] == [(247, stored["plant"]), (1, stored["inverter"])] * 2
    assert rows[0][0] == rows[1][0] != rows[2][0] == rows[3][0]          # one poll_id per tick
    pv1 = conn.execute("""SELECT v.value FROM register_values v JOIN readings r ON r.id = v.reading_id
                          WHERE r.unit_id = 1 AND v.address = 31027 LIMIT 1""").fetchone()[0]
    assert pv1 == 2941                                                    # PV1 294.1 V, before the gain


@pytest.mark.fresh_schema
def test_type_change_with_stored_values_needs_a_migration(run, config, conn, full_server):
    run("db", "migrate", "--config", config)
    run("poll", *net(full_server), "--config", config, "--count", 1)
    conn.execute("UPDATE register_meta SET type = 'u32' WHERE address = 30005")
    code, _, err = run("poll", *net(full_server), "--config", config, "--count", 1)
    assert code == ExitCode.PRECONDITION and "write a migration" in err
    code, _, err = run("db", "migrate", "--config", config)
    assert code == ExitCode.PRECONDITION and "write a migration" in err


@pytest.fixture
def site_config(tmp_path, db_url):
    f = tmp_path / "site.json"
    f.write_text(json.dumps({"database": {"url": db_url}, "site": {
        "latitude": 51.0443, "longitude": -114.0631, "timezone": "America/Edmonton"}}), encoding="utf-8")
    return f


@pytest.mark.fresh_schema
def test_weather_record_to_db(run, site_config, conn, monkeypatch):
    from sigen import weather
    from test_weather import FakeWeb
    code, _, err = run("weather", "--config", site_config, "--record-to-db")
    assert code == ExitCode.PRECONDITION and "sigen db migrate" in err          # tables first
    run("db", "migrate", "--config", site_config)
    monkeypatch.setattr(weather, "http_get", FakeWeb.around_now())
    code, out, _ = run("weather", "--config", site_config, "--record-to-db")
    assert code == ExitCode.OK and out.count("  stored") == 2
    rows = conn.execute("SELECT resolution_minutes, valid_at <= fetched_at, fetched_at - valid_at, temperature_2m, "
                        "weather_code, is_day, error FROM open_meteo_weather ORDER BY id").fetchall()
    assert [(r[0], r[1], r[6]) for r in rows] == [(60, True, None), (15, True, None)]
    assert rows[0][2].total_seconds() < 3600 and rows[1][2].total_seconds() < 900   # the latest period ended
    assert isinstance(rows[0][4], int) and isinstance(rows[0][5], bool)

    monkeypatch.setattr(weather, "http_get", FakeWeb(fail=lambda u, p: Response(429, '{"reason":"limit"}')))
    code, out, _ = run("weather", "--config", site_config, "--record-to-db", "--resolution", 60)
    assert code == ExitCode.CONNECTION and "FAILED" in out and "  stored" in out      # failures are stored too
    valid_at, temperature, error = conn.execute(
        "SELECT valid_at, temperature_2m, error FROM open_meteo_weather ORDER BY id DESC LIMIT 1").fetchone()
    assert valid_at is None and temperature is None and error == 'HTTP 429\n{"reason":"limit"}'


def test_radar_record_to_db_stores_each_frame_once(run, site_config, conn, monkeypatch):
    from sigen import weather
    from test_weather import FakeWeb
    run("db", "migrate", "--config", site_config)
    monkeypatch.setattr(weather, "now_utc", lambda: datetime.datetime(2026, 10, 10, 14, 20, tzinfo=datetime.timezone.utc))
    monkeypatch.setattr(weather, "http_get", FakeWeb())
    code, out, _ = run("radar", "--config", site_config, "--record-to-db")
    assert code == ExitCode.OK and "3 frame(s) stored" in out
    code, out, _ = run("radar", "--config", site_config, "--record-to-db")
    assert code == ExitCode.OK and "no new radar frames" in out and "0 frame(s) stored" in out
    rows = conn.execute("SELECT to_char(frame_at AT TIME ZONE 'UTC', 'HH24:MI'), rain_mm_h, rain_class, covered "
                        "FROM radar_frames ORDER BY frame_at").fetchall()
    assert rows == [("14:06", 0.0, "Undetected", True), ("14:12", 0.0, "Undetected", True),
                    ("14:18", 0.0, "Undetected", True)]
    monkeypatch.setattr(weather, "http_get", FakeWeb(fail=lambda u, p: Response(None, "no response: refused")))
    code, out, _ = run("radar", "--config", site_config, "--record-to-db")
    assert code == ExitCode.CONNECTION and "0 frame(s) stored" in out
    assert conn.execute("SELECT frame_at, error FROM radar_frames WHERE error IS NOT NULL").fetchall() == \
        [(None, "listing the radar frames failed: no response: refused")]


def test_poll_with_weather_and_radar(run, site_config, conn, monkeypatch, tmp_path, full_server):
    import time
    from sigen import weather
    from test_weather import FakeWeb
    run("db", "migrate", "--config", site_config)
    monkeypatch.setattr(weather, "http_get", FakeWeb.around_now())
    started = time.monotonic()
    code, out, err = run("poll", *net(full_server), "--config", site_config, "--count", 2, "--interval", 1,
                         "--weather", "--radar", "--capture-open-meteo-weather", tmp_path / "cap",
                         "--capture-radar", tmp_path / "radar")
    assert time.monotonic() - started < 5          # didn't wait for the hourly fetch to come round again
    assert code == ExitCode.OK and "2 poll(s): 2 stored, 0 failed" in out          # weather isn't a poll
    assert "also Open-Meteo hourly and every 15 min and radar every 6 min" in err
    assert "Open-Meteo 60 min: period ending" in out and "radar: 3 frame(s) stored" in out
    assert conn.execute("SELECT resolution_minutes FROM open_meteo_weather ORDER BY resolution_minutes").fetchall() \
        == [(15,), (60,)]
    assert conn.execute("SELECT count(*) FROM radar_frames WHERE error IS NULL").fetchone()[0] == 3
    assert len(list((tmp_path / "cap").iterdir())) == 2
    assert len(list((tmp_path / "radar").iterdir())) == 9          # 3 frames × rain, snow, coverage
    code, out, _ = run("db", "status", "--config", site_config)
    assert "Open-Meteo:    2 rows (0 failed): 1 hourly, 1 every 15 min; latest period" in out
    assert "Radar frames:  3 (0 failed fetches); latest frame 2026-10-10 14:18Z, last fetch" in out


def test_type_change_without_values_is_just_synced(run, config, conn):
    run("db", "migrate", "--config", config)
    conn.execute("UPDATE register_meta SET type = 'u32', name = 'old' WHERE address = 30005")
    assert run("db", "migrate", "--config", config)[0] == ExitCode.OK
    assert conn.execute("SELECT name, type FROM register_meta WHERE address = 30005").fetchone() \
        == ("plant_grid_sensor_active_power", "s32")


# ---------- the tests' shared schema ----------
def test_the_shared_schema_is_emptied_after_a_test_and_rebuilt_if_it_changed(shared_schema, conn):
    # On the shared schema itself (conn is connected to it): whatever this leaves behind, the reset after it repairs.
    from conftest import fingerprint
    t = datetime.datetime(2026, 10, 9, 12, tzinfo=datetime.timezone.utc)
    db.insert_reading(conn, ip="127.0.0.1", unit_id=247, poll_id=db.next_poll_id(conn), started_at=t, ended_at=t,
                      values=[(30014, 880)])                            # a wide row, referring to its reading
    add_reading(conn, error="timed out")
    shared_schema.reset()
    assert conn.execute("SELECT (SELECT count(*) FROM readings), (SELECT count(*) FROM raw_plant_register_values)"
                        ).fetchone() == (0, 0)
    assert db.next_poll_id(conn) == 1 and add_reading(conn) == 1               # the sequences start again
    assert conn.execute("SELECT count(*) FROM register_meta").fetchone()[0] == len(db.STORED_REGISTERS)
    for change, undo in (("CREATE TABLE stray (x int)", "DROP TABLE stray"),                     # the shape …
                         ("UPDATE register_meta SET unit = 'x' WHERE address = 30005",           # … or a table it keeps
                          "UPDATE register_meta SET unit = 'kW' WHERE address = 30005")):
        conn.execute(change)
        assert fingerprint(conn) != shared_schema.shape, change
        conn.execute(undo)
        assert fingerprint(conn) == shared_schema.shape, undo
    rebuilds, schema = shared_schema.rebuilds, shared_schema.schema
    conn.execute("CREATE TABLE stray (x int)")
    shared_schema.reset()
    assert shared_schema.rebuilds == rebuilds + 1 and shared_schema.schema != schema      # built again …
    with db.connect(shared_schema.url) as c:
        db.check_ready(c)                                                                 # … as migrate leaves it
        assert c.execute("SELECT to_regclass('stray')").fetchone()[0] is None
