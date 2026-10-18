"""sigen.heater: the gated cloud heater poll. The cloud client is faked (no network); the DB tests use the shared
test schema's battery_heater table."""

import datetime
from pathlib import Path

import pytest

from sigen import db, heater
from sigen.config import Cloud
from sigen.errors import CloudError
from sigen.heater import HeaterPoller, gate_values, heating_from
from sigen.registers import ALL_REGISTERS

from test_db import conn, db_url  # noqa: F401  (pytest fixtures)

STATION = "1234567890123456"
BAT1, BAT2 = "110AXX1234000A", "110AXX1234000B"
NOW = datetime.datetime(2026, 10, 17, 9, 0, tzinfo=datetime.timezone.utc)


def a_poll_result(values):
    from sigen.poll import PollResult

    return PollResult(NOW, NOW, values=values)


def reg(name):
    return next(r for r in ALL_REGISTERS if r.name == name)


class FakeClient:
    """Stands in for sigen.cloud.CloudClient: heating is {sn: 'On'/'Off' or an Exception to raise}."""

    def __init__(self):
        self.heating = {}
        self.refresh_calls = self.ensure_calls = 0
        self.refresh_error = None

    def refresh(self):
        self.refresh_calls += 1
        if self.refresh_error:
            raise self.refresh_error

    def ensure_fresh(self):
        self.ensure_calls += 1

    def device_dynamic_info(self, station, sn, device_type):
        v = self.heating[sn]
        if isinstance(v, Exception):
            raise v
        return {
            "realTimeInfo": [
                {"paramKey": "Battery SOC", "paramValue": "52 %"},
                {"paramKey": "Heating status", "paramValue": v},
            ]
        }


def poller(client, **kw):
    return HeaterPoller(
        client,
        STATION,
        [BAT1, BAT2],
        "db-url-unused",
        out=lambda m: None,
        err=kw.pop("err", lambda m: None),
        now=lambda: NOW,
        **kw,
    )


# ---------- helpers ----------
def test_heating_from():
    assert heating_from({"realTimeInfo": [{"paramKey": "Heating status", "paramValue": "On"}]}) is True
    assert heating_from({"realTimeInfo": [{"paramKey": "Heating status", "paramValue": "Off"}]}) is False
    assert heating_from({"realTimeInfo": [{"paramKey": "Battery SOC", "paramValue": "52 %"}]}) is None
    assert heating_from({}) is None and heating_from(None) is None


def test_gate_values():
    pv, cool = reg("inverter_pv_power"), reg("inverter_ess_min_cluster_temperature")
    results = {
        1: a_poll_result([(pv.address, int(1.5 * pv.gain)), (cool.address, int(8 * cool.gain))]),
        247: a_poll_result([]),
    }
    assert gate_values(results, {1: "inverter", 247: "plant"}) == (1.5, 8.0)
    bad = {1: __import__("sigen.poll", fromlist=["PollResult"]).PollResult(NOW, NOW, error="x")}
    assert gate_values(bad, {1: "inverter"}) == (None, None)


# ---------- the gate ----------
def test_active_gate():
    pv, cool = reg("inverter_pv_power"), reg("inverter_ess_min_cluster_temperature")
    hp = poller(FakeClient())
    assert hp._active() is False  # default: PV 0, coolest +inf
    hp.observe(
        {1: a_poll_result([(pv.address, int(0.5 * pv.gain)), (cool.address, int(10 * cool.gain))])}, {1: "inverter"}
    )
    assert hp._active() is True  # PV 0.5 > 0.2 and coolest 10 < 15
    hp.observe(
        {1: a_poll_result([(pv.address, int(0.5 * pv.gain)), (cool.address, int(20 * cool.gain))])}, {1: "inverter"}
    )
    assert hp._active() is False  # warm now


def test_active_while_heater_on():
    hp = poller(FakeClient())
    hp._state[0] = True  # a heater known to be on -> active regardless of gate
    assert hp._active() is True


# ---------- the launch refresh ----------
def test_launch_refresh_failure_disables(monkeypatch):
    client = FakeClient()
    client.refresh_error = CloudError("dead refresh token")
    errs = []
    hp = poller(client, err=errs.append)
    hp._loop()  # returns at once; never connects to the DB
    assert client.refresh_calls == 1
    assert any("off this session" in e for e in errs)


# ---------- from_config ----------
def test_from_config_needs_station_and_batteries():
    errs = []
    assert HeaterPoller.from_config(Cloud(token_file=Path("tok.json")), "db", err=errs.append) is None
    assert any("station_id and batteries" in e for e in errs)


# ---------- querying and storing (needs the DB) ----------
def test_query_stores_and_detects_change(conn):
    client = FakeClient()
    client.heating = {BAT1: "Off"}
    hp = poller(client)
    assert hp._query(conn, 0) is False  # first reading: no prior state, no "change"
    row = conn.execute("SELECT battery, heater_on, error FROM battery_heater ORDER BY id DESC LIMIT 1").fetchone()
    assert row == (1, False, None) and client.ensure_calls == 1
    client.heating[BAT1] = "On"
    assert hp._query(conn, 0) is True  # off -> on


def test_query_error_row(conn):
    client = FakeClient()
    client.heating = {BAT1: CloudError("unreachable")}
    errs = []
    hp = poller(client, err=errs.append)
    assert hp._query(conn, 0) is False
    row = conn.execute("SELECT battery, heater_on, error FROM battery_heater ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == 1 and row[1] is None and "unreachable" in row[2]
    assert any("battery 1" in e for e in errs)


def test_poll_round_flip_triggers_other(conn):
    client = FakeClient()
    client.heating = {BAT1: "Off", BAT2: "Off"}
    hp = poller(client)
    hp._poll_round(conn)  # rr 0 -> battery 1 only
    hp._poll_round(conn)  # rr 1 -> battery 2 only
    n = conn.execute("SELECT count(*) FROM battery_heater").fetchone()[0]
    assert n == 2 and hp._state == {0: False, 1: False}
    client.heating[BAT1] = "On"
    hp._poll_round(conn)  # rr 2 -> battery 1 changed -> also queries battery 2
    rows = conn.execute("SELECT battery FROM battery_heater ORDER BY id DESC LIMIT 2").fetchall()
    assert sorted(b for (b,) in rows) == [1, 2]
    assert conn.execute("SELECT count(*) FROM battery_heater").fetchone()[0] == 4


# ---------- logging ----------
def test_logs_each_query_in_the_wanted_format(conn):
    import re

    client = FakeClient()
    client.heating = {BAT1: "On"}
    out = []
    hp = HeaterPoller(client, STATION, [BAT1, BAT2], "x", out=out.append, err=lambda m: None, now=lambda: NOW)
    hp._query(conn, 0)
    assert any(line.endswith(f"Cloud: Battery {BAT1} querying") for line in out)
    assert any(f"Cloud: Battery {BAT1} heater On, written successfully" in line for line in out)
    # "2026-10-17 09:00:00.000000Z  Cloud: ..." -- UTC, microseconds, Z, two spaces, "Cloud:"
    assert all(re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{6}Z  Cloud: ", line) for line in out)


# ---------- a database outage must not stop the thread ----------
def test_write_failure_is_survivable_and_logged():
    class DeadConn:
        def execute(self, *a, **k):
            raise RuntimeError("server closed the connection unexpectedly")

    client = FakeClient()
    client.heating = {BAT1: "Off"}
    out, errs = [], []
    hp = HeaterPoller(client, STATION, [BAT1, BAT2], "x", out=out.append, err=errs.append, now=lambda: NOW)
    assert hp._query(DeadConn(), 0) is False  # no exception escapes
    assert hp._db_broken is True  # flagged so _loop reopens the connection
    assert any("heater Off, database unwritten" in line for line in out)
    assert any("can't write to the database" in e for e in errs)


def test_write_with_no_connection_is_unwritten():
    hp = poller(FakeClient())
    assert hp._write(None, NOW, 1, False, None) is False
