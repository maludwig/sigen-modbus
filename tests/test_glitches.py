"""sigen.glitches: the rule for plant readings the device got wrong (total load 0 while Running), in Python and in SQL
(the two must agree), poll's extra polls after one, and the loaders and the alerts leaving them out."""
import datetime

import pytest

from sigen import alerts, charts, glitches, poll
from sigen.poll import PollResult
from sigen.registers import BY_NAME

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 15, 13, 40, tzinfo=UTC)
SEC = datetime.timedelta(seconds=1)
LOAD, STATE = glitches.TOTAL_LOAD.address, glitches.RUNNING_STATE.address
# (total load power before the gain, running state) -> a glitch? None = the register wasn't read.
CASES = [((0, 1), True), ((0, 0), False), ((0, 2), False), ((540, 1), False), ((-3, 1), False),
         ((None, 1), False), ((0, None), False), ((None, None), False)]


def values(load, state=1):
    """A plant reading's stored values with these two registers (left out where None)."""
    return [(a, v) for a, v in ((STATE, state), (LOAD, load)) if v is not None]


@pytest.mark.parametrize("pair, expected", CASES)
def test_plant_glitch(pair, expected):
    assert glitches.plant_glitch(values(*pair)) == (glitches.TOTAL_LOAD_ZERO if expected else None)


def test_polls_plant_glitch_looks_at_the_plants_good_reading_only():
    units = {247: "plant", 1: "inverter"}
    good, bad = PollResult(T0, T0, values(540)), PollResult(T0, T0, values(0))
    assert poll.plant_glitch({247: bad, 1: good}, units) == glitches.TOTAL_LOAD_ZERO
    assert poll.plant_glitch({247: good, 1: bad}, units) is None              # the rule is about the plant
    assert poll.plant_glitch({247: PollResult(T0, T0, error="timed out")}, units) is None


def test_poll_values_leaves_out_a_glitched_poll():
    inverter = PollResult(T0, T0, [(BY_NAME["inverter_ess_min_cluster_temperature"].address, 77)])
    units = {247: "plant", 1: "inverter"}
    assert alerts.poll_values({247: PollResult(T0, T0, values(0)), 1: inverter}, units) == (None, {})
    when, _ = alerts.poll_values({247: PollResult(T0, T0, values(540)), 1: inverter}, units)
    assert when == T0


# ---------- with the test database ----------
psycopg = pytest.importorskip("psycopg")
from test_db import config, conn, db_url, url_of  # noqa: E402,F401  (fixtures; never request db_url in a test)
from sigen import db  # noqa: E402


def store(conn, unit, vals, t, error=None):
    return db.insert_reading(conn, ip="127.0.0.1", unit_id=unit, poll_id=db.next_poll_id(conn), started_at=t,
                             ended_at=t, values=vals, error=error)


def test_the_sql_agrees_with_python(conn):
    expected = {store(conn, 247, values(*pair), T0 + k * SEC): (glitch, any(v is not None for v in pair))
                for k, (pair, glitch) in enumerate(CASES)}
    inverter = store(conn, 1, [(BY_NAME["inverter_pv1_voltage"].address, 2941)], T0 + 20 * SEC)
    failed = store(conn, 247, (), T0 + 21 * SEC, error="timed out")
    good = {rid for rid, in conn.execute(f"SELECT id FROM readings WHERE {glitches.good_reading_sql('readings.id')}")}
    not_glitch = {rid for rid, in conn.execute(
        f"SELECT reading_id FROM raw_plant_register_values t WHERE {glitches.not_glitch_sql('t')}")}
    fine = {rid for rid, (glitch, _) in expected.items() if not glitch}
    with_a_row = {rid for rid, (_, has_values) in expected.items() if has_values}      # no values, no plant row
    assert not_glitch == fine & with_a_row and good == fine | {inverter, failed}


def test_the_loaders_leave_glitches_out(conn):
    first = store(conn, 247, values(540), T0)
    store(conn, 247, values(0), T0 + 15 * SEC)                     # glitched, then the poll a second later
    retry = store(conn, 247, values(560), T0 + 16 * SEC)
    store(conn, 247, values(0), T0 + 31 * SEC)                     # the newest reading is a glitch
    assert list(charts.reading_values(conn, 247, ["plant_total_load_power"])) == [first, retry]
    assert charts.load_power_snapshots(conn, {}).series[charts.OTHER] == [0.54, 0.56]
    assert charts.latest_load_power(conn, {}).house == 0.56
    assert [rid for rid, _ in charts.picked_readings(conn)] == [first]


def test_the_alerts_history_leaves_glitches_out(conn):
    for k, load in enumerate([540, 0, 560, 0]):
        store(conn, 247, values(load), T0 + k * SEC)
    w = alerts.Watcher(sender=None, out=lambda m: None)            # preload doesn't use the sender
    w.preload(conn, {247: "plant"}, T0 + datetime.timedelta(minutes=10))
    assert len(w.buffer) == 2


def test_a_glitch_brings_the_next_poll_forward(config, conn, monkeypatch):
    """Each poll is a whole poll (its own poll_id, counted by --count); after a glitch the next one is a second later,
    up to two in a row; then the schedule again, where the next glitch starts over."""
    plan = iter("xxxxg")
    monkeypatch.setattr(poll, "poll_once", lambda reader: PollResult(poll.now_utc(), poll.now_utc(),
                                                                       values(0 if next(plan) == "x" else 540)))
    sleeps, messages = [], []
    code = poll.run(connector_for=lambda ip, unit: None, ip="127.0.0.1", units={247: "plant"}, db_url=url_of(config),
                    interval=60, count=5, out=messages.append, err=messages.append, sleep=sleeps.append,
                    wall=lambda: 0.0)
    assert code == 0 and [round(s) for s in sleeps] == [1, 1, 60, 1]
    assert [m for m in messages if m.startswith("warning")] == [
        "warning: the plant's reading looks wrong (total load 0 while Running); polling again in 1 s (retry 1 of 2)",
        "warning: the plant's reading looks wrong (total load 0 while Running); polling again in 1 s (retry 2 of 2)",
        "warning: the plant's reading looks wrong (total load 0 while Running); back to the schedule",
        "warning: the plant's reading looks wrong (total load 0 while Running); polling again in 1 s (retry 1 of 2)"]
    assert sum("(glitch: total load 0 while Running)" in m for m in messages) == 4
    assert "5 poll(s): 5 stored, 0 failed" in messages
    rows = conn.execute("SELECT poll_id FROM readings ORDER BY id").fetchall()
    assert len(rows) == 5 and len(set(rows)) == 5


def test_no_glitch_retry_when_the_next_poll_comes_sooner(config, monkeypatch):
    plan = iter("xg")
    monkeypatch.setattr(poll, "poll_once", lambda reader: PollResult(poll.now_utc(), poll.now_utc(),
                                                                       values(0 if next(plan) == "x" else 540)))
    sleeps, messages = [], []
    poll.run(connector_for=lambda ip, unit: None, ip="127.0.0.1", units={247: "plant"}, db_url=url_of(config),
             interval=1, count=2, out=messages.append, err=messages.append, sleep=sleeps.append, wall=lambda: 0.0)
    assert [round(s) for s in sleeps] == [1] and any(m.endswith("back to the schedule") for m in messages)
