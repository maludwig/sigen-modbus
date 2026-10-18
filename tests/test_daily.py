"""sigen.daily and `sigen db daily`: each finished day's energy in daily_energy, against the test database (skipped
without one, like test_db.py). The days are test_dashboard's: 2026-10-01 (readings without the energy registers) and
2026-10-02 (a_day), with a reading after 10-02's midnight."""
import argparse
import datetime
import json

import pytest

from sigen import daily
from sigen.config import get_config
from sigen.errors import ExitCode

psycopg = pytest.importorskip("psycopg")
from test_dashboard import TZ, DAY2, a_day, seeded  # noqa: E402,F401  (pytest fixtures)
from test_db import config, conn, db_url, site_config  # noqa: E402,F401  (never request db_url in a test)

OCT1, OCT2 = datetime.date(2026, 10, 1), datetime.date(2026, 10, 2)
MINUTE = 1 / 60


def row(conn, day, *columns):
    return conn.execute(f"SELECT {', '.join(columns)} FROM daily_energy WHERE day = %s", (day,)).fetchone()


def test_finished_days_are_stored_once(a_day, conn):
    assert daily.finished_days(conn, TZ) == [OCT1, OCT2]               # 10-03 has a reading, but isn't over
    assert daily.update(conn, TZ) == [OCT1, OCT2]
    assert row(conn, OCT2, "solar_kwh", "load_kwh", "battery_charged_kwh", "battery_discharged_kwh",
               "grid_imported_kwh", "grid_exported_kwh") == pytest.approx((30, 15, 3.5, 1.25, 8.1, 20))
    # every flow, however small: the daily page would draw none of these
    assert row(conn, OCT2, "solar_to_load_kwh", "solar_to_battery_kwh", "solar_to_grid_kwh", "battery_to_load_kwh",
               "battery_to_grid_kwh", "grid_to_load_kwh", "grid_to_battery_kwh") \
        == pytest.approx((MINUTE, 2 * MINUTE, 2 * MINUTE, MINUTE, 0, 0, 0))
    first, last, hours, method, tz = row(conn, OCT2, "first_reading", "last_reading", "covered_hours", "method",
                                         "timezone")
    assert (first, last) == (DAY2 + datetime.timedelta(seconds=10), DAY2 + datetime.timedelta(hours=23, minutes=59,
                                                                                               seconds=59))
    assert hours == pytest.approx(2 * MINUTE) and method == daily.METHOD and tz == "America/Edmonton"
    assert row(conn, OCT1, "solar_kwh", "grid_imported_kwh", "covered_hours") == (None, None, 0)   # no such registers
    assert daily.update(conn, TZ) == []                                # stored already: nothing to do
    assert conn.execute("SELECT sum(solar_kwh), sum(solar_to_grid_kwh) FROM daily_energy").fetchone() \
        == pytest.approx((30, 2 * MINUTE))                             # the point of it: one SUM


def test_a_range_is_recomputed(a_day, conn):
    daily.update(conn, TZ)
    conn.execute("UPDATE daily_energy SET solar_kwh = 0, method = 0 WHERE day = %s", (OCT2,))
    assert daily.update(conn, TZ) == []                                # without a range: only missing days
    assert daily.update(conn, TZ, first=OCT2) == [OCT2]
    assert row(conn, OCT2, "solar_kwh", "method") == (pytest.approx(30), daily.METHOD)
    assert daily.update(conn, TZ, last=OCT1) == [OCT1]
    assert daily.update(conn, TZ, first=datetime.date(2026, 10, 3)) == []    # not finished


def test_db_daily_and_status(run, a_day, site_config):
    code, out, _ = run("db", "daily", "--config", site_config)
    assert code == ExitCode.OK and "daily energy 2026-10-02: solar 30.00/" in out and "house 15.00" in out
    assert "Stored 2 day(s)" in out
    assert "every finished day is stored already" in run("db", "daily", "--config", site_config)[1]
    code, out, _ = run("db", "daily", "--config", site_config, "--from", "2026-10-02", "--to", "2026-10-02")
    assert code == ExitCode.OK and "Stored 1 day(s)" in out
    assert "Daily energy:  2 day(s) stored, 2026-10-01 to 2026-10-02" in run("db", "status", "--config", site_config)[1]
    with pytest.raises(SystemExit) as refused:                         # argparse's own refusal: exit 2
        run("db", "daily", "--config", site_config, "--from", "yesterday")
    assert refused.value.code == ExitCode.USAGE


def test_poll_stores_finished_days_in_the_background(a_day, site_config, conn):
    from sigen import cli
    url = json.loads(site_config.read_text(encoding="utf-8"))["database"]["url"]
    background, store = cli._background(argparse.Namespace(weather=False, radar=False), get_config(site_config), url)
    assert store is None and [job.name for job in background._jobs] == ["daily energy"]   # even without --weather
    assert background._jobs[0].interval == daily.CHECK_EVERY
    background._jobs[0].run()
    assert [d for d, in conn.execute("SELECT day FROM daily_energy ORDER BY day")] == [OCT1, OCT2]
