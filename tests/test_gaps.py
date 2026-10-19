"""sigen.gaps, `sigen db gaps` and `sigen sync --fill-gaps`: the gap arithmetic offline, then against the test
database (skipped without one, like test_db.py)."""

import datetime
import json

import pytest

from sigen import cli, db, gaps
from sigen.config import Config
from sigen.errors import ExitCode
from sigen.registers import PLANT_UNIT_ID

from test_db import config, conn, db_url  # noqa: F401  (pytest fixtures)

UTC = datetime.UTC
T0 = datetime.datetime(2026, 10, 18, 20, 0, tzinfo=UTC)
SEC = datetime.timedelta(seconds=1)
MIN = datetime.timedelta(minutes=1)


# ---------- offline ----------
def test_parse_duration():
    assert gaps.parse_duration("45s") == 45 * SEC and gaps.parse_duration("90") == 90 * SEC
    assert gaps.parse_duration("1.5h") == 90 * MIN and gaps.parse_duration("7d") == datetime.timedelta(days=7)
    for bad in ("0", "5w", "x", "", "-5m"):
        with pytest.raises(ValueError, match="isn't a duration"):
            gaps.parse_duration(bad)


def test_parse_min_gaps():
    assert gaps.parse_min_gaps(["readings:30s", "battery_heater:2h"]) == {
        "readings": 30 * SEC,
        "battery_heater": 120 * MIN,
    }
    for bad in (["pole:5m"], ["readings"], ["readings:soon"]):
        with pytest.raises(ValueError):
            gaps.parse_min_gaps(bad)


def test_span_text_and_short_error():
    assert [gaps.span_text(s * SEC) for s in (45, 545, 7500, 97200)] == ["45s", "9m05s", "2h05m", "1d03h"]
    assert gaps.span_text(45.4 * SEC) == "46s"  # rounded up: over a 45 s min gap never reads "45s"
    assert gaps.short_error("couldn't connect to 192.168.1.176:502: [Errno 113] No route to host") == (
        "No route to host"
    )
    assert gaps.short_error("couldn't connect to 192.168.1.176:502: timed out") == "timed out"


def test_find():
    times = [T0 + 15 * k * SEC for k in range(5)] + [T0 + 5 * MIN]  # every 15 s, then nothing for 4 minutes
    failed = [(T0 + 2 * MIN, "x: timed out"), (T0 + 3 * MIN, "x: timed out"), (T0 + 4 * MIN, "x: refused")]
    found = gaps.find(times, failed, 45 * SEC, T0 + 5 * MIN + 30 * SEC)
    assert len(found) == 1  # the 30 s at the end is under the min gap
    g = found[0]
    assert (g.start, g.end, g.open_ended, g.failed, g.error) == (T0 + MIN, T0 + 5 * MIN, False, 3, "timed out")
    found = gaps.find(times, [], 45 * SEC, T0 + 10 * MIN)  # nothing since T0 + 5 min: still going
    assert found[-1].open_ended and found[-1].length == 5 * MIN and found[-1].error is None
    assert gaps.find([], failed, 45 * SEC, T0) == []  # no good row to measure from


def test_daylight_between():
    calgary = (51.0, -114.0)
    # In October, Calgary's daylight is about 10-11 h; 06:00-12:00 UTC is local midnight to 6 am: none.
    day = gaps.daylight_between(T0.replace(hour=0), T0.replace(hour=0) + datetime.timedelta(days=1), *calgary)
    assert datetime.timedelta(hours=9) < day < datetime.timedelta(hours=12)
    assert gaps.daylight_between(T0.replace(hour=6), T0.replace(hour=12), *calgary) == datetime.timedelta(0)
    # The night between two scans doesn't make a gap; a missing afternoon does.
    times = [T0.replace(hour=1), T0.replace(hour=13, minute=30)]  # 19:00 MDT (after sunset), 07:30 (before sunrise)
    measure = lambda a, b: gaps.daylight_between(a, b, *calgary)  # noqa: E731
    assert gaps.find(times, [], 30 * MIN, times[-1], measure) == []
    assert len(gaps.find([T0.replace(hour=15), T0.replace(hour=20)], [], 30 * MIN, T0.replace(hour=20), measure))


# ---------- against the database ----------
def add_reading(conn, at, error=None):
    db.insert_reading(
        conn,
        ip="127.0.0.1",
        unit_id=PLANT_UNIT_ID,
        poll_id=db.next_poll_id(conn),
        started_at=at,
        ended_at=at,
        values=[] if error else [(30014, 880)],
        error=error,
    )


def add_day(conn, day):
    zeros = [0.0] * 7
    conn.execute(
        "INSERT INTO daily_energy (day, timezone, solar_to_load_kwh, solar_to_battery_kwh, solar_to_grid_kwh, "
        "battery_to_load_kwh, battery_to_grid_kwh, grid_to_load_kwh, grid_to_battery_kwh, first_reading, "
        "last_reading, covered_hours, method) VALUES (%s, 'UTC', %s, %s, %s, %s, %s, %s, %s, now(), now(), 24, 1)",
        (day, *zeros),
    )


def outage(conn, start):
    """Polls every 15 s for 5 minutes from `start`, 3 minutes of failures, 2 minutes of polls again, then silence."""
    for k in range(20):
        add_reading(conn, start + 15 * k * SEC)
    for k in range(12):
        add_reading(conn, start + 5 * MIN + 15 * k * SEC, "couldn't connect to 192.168.1.176:502: timed out")
    for k in range(8):
        add_reading(conn, start + 8 * MIN + 15 * k * SEC)


def test_readings_and_heater_gaps(conn):
    outage(conn, T0)
    for k in range(10):  # battery 1 every 20 minutes, then 90 minutes with one failed check
        if not 3 <= k <= 6:
            db.insert_battery_heater(conn, polled_at=T0 + 20 * k * MIN, battery=1, heater_on=False)
    db.insert_battery_heater(conn, polled_at=T0 + 90 * MIN, battery=1, heater_on=None, error="cloud: unreachable")
    found = {
        sg.series.name: sg
        for sg in gaps.find_gaps(conn, Config(), T0 - MIN, T0 + 200 * MIN, 15, ["readings", "battery_heater"])
    }
    readings = found[f"readings, plant (unit {PLANT_UNIT_ID})"]
    assert readings.min_gap == 45 * SEC and readings.rows == 28
    middle, end = readings.gaps
    assert (middle.start, middle.end) == (T0 + 4 * MIN + 45 * SEC, T0 + 8 * MIN)
    assert (middle.failed, middle.error, middle.open_ended) == (12, "timed out", False)
    assert end.open_ended and end.start == T0 + 9 * MIN + 45 * SEC
    heater = found["battery_heater, battery 1"]
    assert heater.min_gap == 30 * MIN  # one pack: every 10 min, x3
    (hole,) = [g for g in heater.gaps if not g.open_ended]
    assert (hole.start, hole.end, hole.failed) == (T0 + 40 * MIN, T0 + 140 * MIN, 1)


def test_missing_days(conn):
    tz = datetime.UTC
    today = datetime.datetime.now(tz).date()
    for back in (5, 4, 2):
        add_day(conn, today - datetime.timedelta(days=back))
    now = datetime.datetime.now(tz)
    one = datetime.timedelta(days=1)
    assert gaps.missing_days(conn, now - datetime.timedelta(days=30), now, tz) == [
        (today - 3 * one, today - 3 * one),
        (today - one, today - one),
    ]  # not the days before the first row, nor today


def test_cli_db_gaps(run, config, conn):
    now = datetime.datetime.now(UTC).replace(microsecond=0)
    outage(conn, now - 30 * MIN)
    code, out, _ = run("db", "gaps", "--config", config, "--table", "readings", "--min-gap", "readings:45s")
    assert code == ExitCode.OK and "readings, plant (unit 247): every 1m00s, gaps over 45s:" in out
    assert "12 failed: timed out" in out and "(now)" in out and "battery_heater" not in out
    code, out, _ = run("db", "gaps", "--config", config, "--json", "--table", "readings")
    (series,) = json.loads(out)["series"]
    assert series["min_gap_s"] == 180 and series["rows"] == 28  # 3 x poll's default 60 s
    assert json.loads(out)["daily_energy_missing"] is None
    code, _, err = run("db", "gaps", "--config", config, "--min-gap", "pole:5m")
    assert code == ExitCode.USAGE and "TABLE:DURATION" in err


def test_sync_fill_gaps_syncs_each_gap(run, config, conn, monkeypatch):
    now = datetime.datetime.now(UTC).replace(microsecond=0)
    outage(conn, now - 30 * MIN)
    windows, recomputed = [], []
    monkeypatch.setattr(cli.sync, "check_schema", lambda c, url: None)
    monkeypatch.setattr(
        cli.sync, "sync_kind", lambda c, url, kind, s, e, dry, out: windows.append((kind, s, e)) or (3, 0)
    )
    monkeypatch.setattr(cli.daily, "update", lambda *a, **kw: recomputed.append(a[2:4]) or [])
    fill = ["sync", "--url", "http://peer", "--fill-gaps", "--config", config]
    code, out, _ = run(*fill, "--only-sync", "readings", "--min-gap", "readings:45s")
    assert code == ExitCode.OK and "Filling 2 gap(s)" in out
    assert [(k, s) for k, s, _ in windows] == [
        ("readings", now - 30 * MIN + 4 * MIN + 45 * SEC),
        ("readings", now - 30 * MIN + 9 * MIN + 45 * SEC),
    ]
    assert len(recomputed) == 1  # readings came in: the days' totals recomputed once
    code, _, err = run(*fill, "--start-at", "2026-10-18T00:00:00Z")
    assert code == ExitCode.USAGE and "--fill-gaps finds its own windows" in err
    code, _, err = run("sync", "--url", "http://peer", "--config", config)
    assert code == ExitCode.USAGE and "or --fill-gaps" in err
