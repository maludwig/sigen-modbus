"""sigen.glitches: the verdict on one poll (total load 0 while Running; PV 0 while the strings carry power, sure above
0.5 kW, a suspect above 0.1), poll storing it in readings.glitch and polling again a second later, the loaders and
the alerts leaving flagged polls out, and migration 0007 flagging the history."""
import datetime

import pytest

from sigen import alerts, charts, glitches, poll
from sigen.poll import PollResult
from sigen.registers import BY_NAME

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 15, 13, 40, tzinfo=UTC)
SEC = datetime.timedelta(seconds=1)
A = {name: BY_NAME[name].address for name in (
    "plant_running_state", "plant_total_load_power", "plant_photovoltaic_power", "plant_ess_power",
    "inverter_running_state", "inverter_pv_power", "inverter_pv1_voltage", "inverter_pv1_current",
    "inverter_ess_min_cluster_temperature")}


def plant(load=540, pv=1600, state=1):
    """A plant reading's stored values (before the gain: W, and the running state); None leaves a register out."""
    return [(A[n], v) for n, v in (("plant_running_state", state), ("plant_total_load_power", load),
                                   ("plant_photovoltaic_power", pv), ("plant_ess_power", -596)) if v is not None]


def inverter(pv=1600, volts=300.0, amps=6.0, state=1):
    """An inverter reading's stored values: its PV power (W) and one string, `volts` × `amps`."""
    return [(A["inverter_running_state"], state), (A["inverter_pv_power"], pv),
            (A["inverter_pv1_voltage"], round(volts * 10)), (A["inverter_pv1_current"], round(amps * 100))]


def rule(readings):
    v = glitches.judge(readings)
    return None if v is None else (v.rule, v.sure)


# ---------- the verdict ----------
@pytest.mark.parametrize("readings, expected", [
    ({"plant": plant(load=0)}, (glitches.TOTAL_LOAD_ZERO, True)),
    ({"plant": plant(load=0, state=0)}, None),                         # not Running: anything goes
    ({"plant": plant(load=0, state=2)}, None),
    ({"plant": plant(load=-3)}, None),
    ({"plant": plant(load=None)}, None),
    ({"plant": plant(), "inverter": inverter(pv=0)}, (glitches.PV_ZERO, True)),          # 1.8 kW of strings
    ({"plant": plant(pv=0), "inverter": inverter()}, (glitches.PV_ZERO, True)),          # either unit's PV
    ({"plant": plant(), "inverter": inverter(pv=0, amps=1.0)}, (glitches.PV_ZERO_SUSPECT, False)),   # 0.3 kW: dusk?
    ({"plant": plant(), "inverter": inverter(pv=0, amps=0.3)}, None),                    # 0.09 kW: nothing there
    ({"plant": plant(), "inverter": inverter(pv=0, state=0)}, None),
    ({"plant": plant(pv=0)}, None),                                                       # no strings to judge by
    ({"plant": plant(load=0), "inverter": inverter(pv=0)}, (glitches.TOTAL_LOAD_ZERO, True)),
    ({"plant": plant(), "inverter": inverter()}, None),
])
def test_judge(readings, expected):
    assert rule(readings) == expected


def test_the_verdict_says_why():
    v = glitches.judge({"plant": plant(load=0)})
    assert v.why == "total load 0 while Running (battery -0.60 kW, PV 1.60 kW)"
    v = glitches.judge({"plant": plant(pv=0), "inverter": inverter(pv=0)})
    assert v.why == "PV power 0 (inverter and plant) while the strings make 1.80 kW"


def test_poll_verdict_leaves_failed_units_out():
    units = {247: "plant", 1: "inverter"}
    good, bad_inverter = PollResult(T0, T0, plant()), PollResult(T0, T0, inverter(pv=0))
    assert poll.poll_verdict({247: good, 1: bad_inverter}, units).rule == glitches.PV_ZERO
    assert poll.poll_verdict({247: PollResult(T0, T0, error="timed out"), 1: PollResult(T0, T0, inverter())},
                             units) is None


# ---------- the alerts ----------
class Sender:
    alerts = None

    def __init__(self):
        self.calls = []

    def send(self, kind, notice, message):
        self.calls.append(("sent", kind, message))

    def suppress(self, kind, notice, message, why="cooldown"):
        self.calls.append((why, kind, message))


def test_the_watcher_alerts_on_sure_glitches_and_leaves_flagged_polls_out():
    sender, units = Sender(), {247: "plant"}
    w = alerts.Watcher(sender, detect=lambda window: [], tz=UTC, out=lambda m: None, err=lambda m: None)
    minute = datetime.timedelta(minutes=1)

    def observe(at, values, verdict):
        w.observe({247: PollResult(at, at, values)}, units, at, glitch=verdict)
    total = glitches.judge({"plant": plant(load=0)})
    pv = glitches.Verdict(glitches.PV_ZERO, "PV power 0 (plant) while the strings make 1.80 kW", True)
    suspect = glitches.Verdict(glitches.PV_ZERO_SUSPECT, "…", False)
    observe(T0, plant(load=0), total)
    observe(T0 + SEC, plant(), None)
    observe(T0 + 10 * minute, plant(load=0), total)                  # inside the hour: logged as suppressed
    observe(T0 + 11 * minute, plant(pv=0), pv)                       # PV zeros: logged only, never sent
    observe(T0 + 12 * minute, plant(pv=0), suspect)                  # a suspect a retry replaced: nothing at all
    observe(T0 + 61 * minute, plant(load=0), total)
    assert [(what, kind) for what, kind, _ in sender.calls] == [
        ("sent", "glitch:total_load_zero"), ("cooldown", "glitch:total_load_zero"), ("log only", "glitch:pv_zero"),
        ("sent", "glitch:total_load_zero")]
    assert sender.calls[0][2] == ("The SigenStor's reading looks wrong: total load 0 while Running (battery -0.60 kW, "
                                  "PV 1.60 kW). Stored, and left out of the charts and alerts.\n"
                                  "2026-10-15T13:40:00+00:00")
    assert len(w.buffer) == 1                                       # only the unflagged poll


# ---------- with the test database ----------
psycopg = pytest.importorskip("psycopg")
from test_db import config, conn, db_url, url_of  # noqa: E402,F401  (fixtures; never request db_url in a test)
from sigen import db  # noqa: E402


def store_poll(conn, t, plant_values=None, inverter_values=None, glitch=None):
    """One poll (a plant and/or an inverter reading), flagged `glitch`; returns {"plant": id, "inverter": id}."""
    pid, ids = db.next_poll_id(conn), {}
    for unit, device, values in ((247, "plant", plant_values), (1, "inverter", inverter_values)):
        if values is not None:
            ids[device] = db.insert_reading(conn, ip="127.0.0.1", unit_id=unit, poll_id=pid, started_at=t, ended_at=t,
                                            values=values, glitch=glitch)
    return ids


def test_the_loaders_leave_flagged_polls_out_both_units(conn):
    first = store_poll(conn, T0, plant(), inverter())
    store_poll(conn, T0 + 15 * SEC, plant(load=0), inverter(), glitch=glitches.TOTAL_LOAD_ZERO)
    retry = store_poll(conn, T0 + 16 * SEC, plant(load=560), inverter())
    store_poll(conn, T0 + 31 * SEC, plant(pv=0), inverter(pv=0), glitch=glitches.PV_ZERO)   # the newest
    assert list(charts.reading_values(conn, 247, ["plant_total_load_power"])) == [first["plant"], retry["plant"]]
    assert list(charts.reading_values(conn, 1, ["inverter_pv_power"])) == [first["inverter"], retry["inverter"]]
    assert charts.load_power_snapshots(conn, {}).series[charts.OTHER] == [0.54, 0.56]
    assert charts.latest_load_power(conn, {}).house == 0.56
    assert [rid for rid, _ in charts.picked_readings(conn)] == [first["plant"]]


def test_the_alerts_history_leaves_flagged_polls_out(conn):
    for k, (load, glitch) in enumerate([(540, None), (0, glitches.TOTAL_LOAD_ZERO), (560, None),
                                        (580, glitches.PV_ZERO_SUSPECT)]):
        store_poll(conn, T0 + k * SEC, plant(load=load), glitch=glitch)
    w = alerts.Watcher(sender=None, out=lambda m: None)              # preload doesn't use the sender
    w.preload(conn, {247: "plant"}, T0 + datetime.timedelta(minutes=10))
    assert len(w.buffer) == 2


def scripted(monkeypatch, plan):
    """poll_once by `plan`, one letter per poll: g good, x total load 0 (sure), p PV 0 at 1.8 kW (sure), s PV 0 at
    0.3 kW (a suspect)."""
    codes = iter(plan)
    current = {}

    def fake(reader):
        t = poll.now_utc()
        if reader.connector == 247:
            current["code"] = next(codes)
        c = current["code"]
        if reader.connector == 247:
            return PollResult(t, t, plant(load=0 if c == "x" else 540))
        return PollResult(t, t, inverter(pv=0 if c in "ps" else 1600, amps=1.0 if c == "s" else 6.0))
    monkeypatch.setattr(poll, "poll_once", fake)


def run_poll(config, count, interval=60):
    sleeps, messages = [], []
    code = poll.run(connector_for=lambda ip, unit: unit, ip="127.0.0.1", units={247: "plant", 1: "inverter"},
                    db_url=url_of(config), interval=interval, count=count, out=messages.append,
                    err=messages.append, sleep=sleeps.append, wall=lambda: 0.0)
    assert code == 0
    return [round(s) for s in sleeps], messages


def glitch_per_poll(conn):
    rows = conn.execute("SELECT poll_id, array_agg(DISTINCT coalesce(glitch, '-')) FROM readings GROUP BY poll_id "
                        "ORDER BY poll_id").fetchall()
    assert all(len(values) == 1 for _, values in rows)              # both units' readings agree
    return [None if values[0] == "-" else values[0] for _, values in rows]


def test_a_glitch_brings_the_next_poll_forward_and_is_stored(config, conn, monkeypatch):
    """Each poll is a whole poll (its own poll_id, both units, counted by --count). A sure glitch is flagged
    whatever happens next; a suspect only when a retry replaces it, so the last attempt is the interval's reading."""
    scripted(monkeypatch, "xgsssg")
    sleeps, messages = run_poll(config, 6)
    assert sleeps == [1, 60, 1, 1, 120]
    assert glitch_per_poll(conn) == [glitches.TOTAL_LOAD_ZERO, None, glitches.PV_ZERO_SUSPECT,
                                     glitches.PV_ZERO_SUSPECT, None, None]
    notes = [m for m in messages if m.startswith(("warning", "note"))]
    assert notes == [
        "warning: the reading looks wrong (total load 0 while Running (battery -0.60 kW, PV 1.60 kW)); polling again "
        "in 1 s (retry 1 of 2)",
        "note: a suspect reading (PV power 0 (inverter) while the strings make 0.30 kW (perhaps dusk)); polling again "
        "in 1 s (retry 1 of 2)",
        "note: a suspect reading (PV power 0 (inverter) while the strings make 0.30 kW (perhaps dusk)); polling again "
        "in 1 s (retry 2 of 2)",
        "note: a suspect reading (PV power 0 (inverter) while the strings make 0.30 kW (perhaps dusk)); keeping it, "
        "back to the schedule"]
    assert sum("(glitch: " in m for m in messages) == 2 * 3          # both units' lines, three flagged polls
    assert "6 poll(s): 12 stored, 0 failed" in messages


def test_a_sure_glitch_stays_flagged_when_the_retries_run_out(config, conn, monkeypatch):
    scripted(monkeypatch, "pppg")
    sleeps, messages = run_poll(config, 4)
    assert sleeps == [1, 1, 60]
    assert glitch_per_poll(conn) == [glitches.PV_ZERO] * 3 + [None]
    assert any(m.endswith("back to the schedule") and m.startswith("warning") for m in messages)


def test_no_retry_when_the_next_poll_comes_sooner(config, conn, monkeypatch):
    scripted(monkeypatch, "sx")
    sleeps, messages = run_poll(config, 2, interval=1)
    assert sleeps == [1] and glitch_per_poll(conn) == [None, glitches.TOTAL_LOAD_ZERO]   # the suspect is kept


@pytest.mark.fresh_schema
def test_0007_flags_the_history_as_judge_would(conn):
    db.migrate(conn, db.load_migrations()[:6])
    polls = {"good": (plant(), inverter()), "total": (plant(load=0), inverter()),
             "pv inverter": (plant(), inverter(pv=0)), "pv plant": (plant(pv=0), inverter()),
             "suspect": (plant(), inverter(pv=0, amps=1.0)), "stopped": (plant(load=0, state=0), inverter(state=0)),
             "plant only": (plant(load=0), None)}
    ids = {name: store_poll(conn, T0 + k * 15 * SEC, p, i) for k, (name, (p, i)) in enumerate(polls.items())}
    assert db.migrate(conn) == ["0007_glitch", "0008_battery_heater", "0009_timestamp_brin", "0010_satellite_shade",
                                "0011_daily_clear_sky"]
    flags = {rid: g for rid, g in conn.execute("SELECT id, glitch FROM readings")}
    for name, (p, i) in polls.items():
        verdict = glitches.judge({d: v for d, v in (("plant", p), ("inverter", i)) if v is not None})
        expected = verdict.rule if verdict is not None and verdict.sure else None    # history had no retries
        assert {flags[rid] for rid in ids[name].values()} == {expected}, name
