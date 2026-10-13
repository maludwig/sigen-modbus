"""sigen.alerts (`poll --alerts`): the buffer, which events alert (once per event, a cooldown per kind, history
never), the sender thread, and poll itself with alerts against the fake device and the test database."""
import datetime
import json

import pytest

from sigen import alerts, db
from sigen import battery_watch as bw
from sigen.config import Alerts
from sigen.errors import ExitCode
from sigen.poll import PollResult
from sigen.registers import BY_NAME
from test_ntfy import TOPIC, fake_ntfy  # noqa: F401  (pytest fixture)

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
MIN = datetime.timedelta(minutes=1)
SEC = datetime.timedelta(seconds=1)


class FakeSender:
    alerts = Alerts(TOPIC, "https://ntfy.example")

    def __init__(self):
        self.calls = []

    def start(self):
        pass

    def stop(self):
        pass

    def send(self, kind, notice, message):
        self.calls.append(("sent", kind, message))

    def suppress(self, kind, notice, message, why="cooldown"):
        self.calls.append(("suppressed" if why == "cooldown" else why, kind, message))


def watcher_with(events_at):
    """A Watcher whose detectors return events_at(now) (a list of Events), with a FakeSender."""
    sender, state = FakeSender(), {"now": T0}
    w = alerts.Watcher(sender, detect=lambda window: events_at(state["now"]), out=lambda m: None,
                       err=lambda m: state.setdefault("errors", []).append(m))
    return w, sender, state


def checks(w, state, minutes):
    """Run check() once a minute for `minutes` minutes from T0; returns every decision."""
    raised = []
    for k in range(minutes):
        state["now"] = T0 + k * MIN
        raised += [(what, e.kind) for what, e in w.check(state["now"])]
    return raised


# ---------- decisions ----------
def test_an_ongoing_event_alerts_once():
    w, sender, state = watcher_with(lambda now: [bw.Event("cold", now, now, f"coolest 4.9 °C at {now:%H:%M}")])
    assert checks(w, state, 180) == [("sent", "cold")]            # 3 hours of "still cold": one notification
    assert sender.calls[0][:2] == ("sent", "cold")


def test_a_new_event_in_the_cooldown_is_suppressed_once_then_later_ones_send():
    bursts = [T0 + 10 * MIN, T0 + 40 * MIN, T0 + 50 * MIN, T0 + 80 * MIN]          # 3-minute bursts
    w, sender, state = watcher_with(lambda now: [bw.Event("unexplained_power", b, min(now, b + 3 * MIN), "burst")
                                                 for b in bursts if b <= now])
    assert checks(w, state, 120) == [("sent", "unexplained_power"), ("suppressed", "unexplained_power"),
                                     ("suppressed", "unexplained_power"), ("sent", "unexplained_power")]


def test_history_and_unnotified_kinds_dont_alert():
    old = bw.Event("plateau", T0 - 5 * 60 * MIN, T0 - 3 * 60 * MIN, "old")
    limited = lambda now: bw.Event("charge_limited", T0, now, "throttled")      # noqa: E731
    w, sender, state = watcher_with(lambda now: [old, limited(now)])
    assert checks(w, state, 30) == [] and sender.calls == []


def test_kinds_have_their_own_cooldowns_and_restarts_keep_them():
    w, sender, state = watcher_with(lambda now: [bw.Event("cold", now, now, "cold"),
                                                 bw.Event("fault", now, now, "fault")])
    w.last_sent["cold"] = T0 - 30 * MIN                    # sent 30 min before a restart (from alert_log)
    assert sorted(checks(w, state, 5)) == [("sent", "fault"), ("suppressed", "cold")]


def test_each_alarm_bit_has_its_own_cooldown_title_and_priority():
    def alarms(now):
        return [bw.Event("alarm", now, now, "a", key="alarm:gateway.1", title="The gateway is overheating"),
                bw.Event("alarm", now, now, "b", key="alarm:pcs1.9", title="The grid is down", priority="urgent")]
    w, sender, state = watcher_with(alarms)
    w.last_sent["alarm:gateway.1"] = T0 - 10 * MIN         # this one's in its cooldown: the grid isn't
    assert sorted(checks(w, state, 3)) == [("sent", "alarm"), ("suppressed", "alarm")]
    assert sorted(c[:2] for c in sender.calls) == [("sent", "alarm:pcs1.9"), ("suppressed", "alarm:gateway.1")]


def test_messages_end_with_the_events_start_in_local_time():
    from zoneinfo import ZoneInfo
    start = datetime.datetime(2026, 10, 13, 1, 49, 30, 250000, tzinfo=UTC)
    sender = FakeSender()
    w = alerts.Watcher(sender, detect=lambda window: [bw.Event("fault", start, T0, "The SigenStor reports a fault")],
                       tz=ZoneInfo("America/Edmonton"), out=lambda m: None, err=lambda m: None)
    w.check(T0)
    assert sender.calls == [("sent", "fault", "The SigenStor reports a fault\n2026-10-12T19:49:30-06:00")]


def failed_poll(t):
    return {247: PollResult(t, t, error="couldn't connect: timed out", connection_failed=True),
            1: PollResult(t, t, error="No route to host", connection_failed=True)}


def test_unreachable_once_per_outage_and_the_return_is_only_logged():
    sender = FakeSender()
    w = alerts.Watcher(sender, detect=lambda window: [], out=lambda m: None, err=lambda m: None)
    outcomes = [w.reachability(failed_poll(T0 + k * 15 * SEC), T0 + k * 15 * SEC) for k in range(30)]
    assert outcomes.index("unreachable") == 19 and outcomes.count("unreachable") == 1     # the 20th failed poll
    (what, kind, message), = sender.calls
    assert (what, kind) == ("sent", "unreachable") and message.startswith("No reading from the SigenStor for 5 min")
    assert message.endswith(T0.astimezone(w.tz).isoformat(timespec="seconds"))         # when the outage began
    t = T0 + 30 * 15 * SEC
    assert w.reachability({247: PollResult(t, t, [(30014, 730)])}, t) == "reachable"
    assert sender.calls[-1][:2] == ("log only", "reachable") and w.failed_polls == 0
    short = [w.reachability(failed_poll(t + k * 15 * SEC), t + k * 15 * SEC) for k in range(1, 19)]
    assert set(short) == {None}                              # under 5 min: nothing, not even a log
    w.reachability({247: PollResult(t, t, [(30014, 730)])}, t + 20 * 15 * SEC)
    assert len(sender.calls) == 2


def test_a_failing_detector_warns_once_and_never_raises():
    def broken(window):
        raise ZeroDivisionError("oops")
    errors = []
    w = alerts.Watcher(FakeSender(), detect=broken, out=lambda m: None, err=errors.append)
    for k in range(10):
        w.observe({}, {}, T0 + k * MIN)
    assert errors == ["warning: alert check failed: ZeroDivisionError: oops"]


# ---------- buffer ----------
def test_buffer():
    b = alerts.Buffer(span=datetime.timedelta(hours=1))
    for k in range(90):
        b.add_row(T0 + k * MIN, {bw.COOLEST: 5.0 + k / 100})
    b.add_row(T0, {bw.COOLEST: 99.0})                     # out of order: ignored
    assert len(b) == 61                                   # the last hour, both ends included
    for k in range(0, 91, 15):
        b.add_air(T0 + k * MIN, -k / 15)
    b.add_air(T0 + 90 * MIN, -10.0)                       # a later fetch of the same period wins
    w = b.window()
    assert w.times[0] == T0 + 29 * MIN and w[bw.COOLEST][-1] == pytest.approx(5.89)
    assert w[bw.AIR][0] == pytest.approx(-29 / 15) and w[bw.AIR][-1] == pytest.approx(-5 - 5 * 14 / 15)
    assert w[bw.SOC] == [None] * 61


def test_poll_values():
    def raw(name, value):
        reg = BY_NAME[name]
        return reg.address, round(value * reg.gain)
    t = T0 + MIN
    results = {247: PollResult(t, t, [raw("plant_ess_soc", 83.0), raw("plant_photovoltaic_power", 1.2)]),
               1: PollResult(t + MIN, t + MIN, [raw("inverter_ess_min_cluster_temperature", 7.7)])}
    when, values = alerts.poll_values(results, {247: "plant", 1: "inverter"})
    assert when == t and values == {bw.SOC: 83.0, bw.COOLEST: pytest.approx(7.7)}     # only the detectors' columns
    assert alerts.poll_values({247: PollResult(t, t, error="timed out")}, {247: "plant"}) == (None, {})


# ---------- sender ----------
def test_sender(fake_ntfy, hangs_up):
    out, err = [], []
    s = alerts.Sender(Alerts(TOPIC, fake_ntfy.url), out=out.append, err=err.append)
    s.start()
    notice = alerts.NOTICES["cold"]
    s.send("cold", notice, "coolest cluster 4.9 °C")
    s.suppress("cold", notice, "again")
    s.stop()
    (req,) = fake_ntfy.requests
    assert req["body"] == "coolest cluster 4.9 °C" and req["headers"]["Priority"] == "high"
    assert req["headers"]["Title"] == "Battery is cold: blanket time" and req["headers"]["Tags"] == "snowflake"
    assert out == ["alert sent (cold): Battery is cold: blanket time: coolest cluster 4.9 °C",
                   "alert suppressed (cold, cooldown): Battery is cold: blanket time: again"] and err == []
    s = alerts.Sender(Alerts(TOPIC, f"http://127.0.0.1:{hangs_up}"), out=out.append, err=err.append)
    s.start()
    s.send("cold", notice, "x")
    s.stop()
    assert len(err) == 1 and err[0].startswith("warning: alert NOT sent (cold): couldn't reach ntfy")


# ---------- poll --alerts, with the test database ----------
psycopg = pytest.importorskip("psycopg")
from test_db import config, conn, db_url, net, url_of  # noqa: E402,F401  (fixtures; never request db_url in a test)


@pytest.fixture
def alert_config(tmp_path, config, fake_ntfy):
    f = tmp_path / "alerts.json"
    f.write_text(json.dumps({"database": {"url": url_of(config)}, "devices": {"inverter": {"units": [1]}},
                             "alerts": {"ntfy_topic": TOPIC, "ntfy_server": fake_ntfy.url}}), encoding="utf-8")
    return f


def test_poll_with_alerts(run, alert_config, conn, both_server, fake_ntfy, monkeypatch, quick_polls):
    run("db", "migrate", "--config", alert_config)
    code, _, _ = run("poll", *net(both_server), "--config", alert_config, "--count", 2, "--interval", 1)
    assert code == ExitCode.OK                                                     # history, without alerts
    seen = []

    def cold(window):
        seen.append(window)
        return [bw.Event("cold", window.times[-1], window.times[-1], "coolest cluster 4.9 °C")]
    monkeypatch.setattr(bw, "detect_all", cold)
    code, out, err = run("poll", *net(both_server), "--config", alert_config, "--count", 2, "--interval", 1, "--alerts")
    assert code == ExitCode.OK and "4 stored, 0 failed" in out and "also alerts (no --weather" in err
    assert "alerts: 2 poll(s) of history loaded" in out and "alert sent (cold)" in out
    assert len(seen) == 2 and len(seen[-1]) == 4 and seen[-1][bw.COOLEST][-1] is not None    # history + 2 new
    assert len(fake_ntfy.requests) == 1
    # A restart within the hour: the same "cold" is a new event to the new process, so it's logged as suppressed.
    code, out, _ = run("poll", *net(both_server), "--config", alert_config, "--count", 1, "--alerts")
    assert code == ExitCode.OK and "alert suppressed (cold, cooldown)" in out and len(fake_ntfy.requests) == 1
    rows = conn.execute("SELECT kind, was_suppressed, http_code, error FROM alert_log ORDER BY id").fetchall()
    assert rows == [("cold", False, 200, None), ("cold", True, None, None)]


def test_alert_failures_dont_stop_polling(run, alert_config, both_server, monkeypatch, quick_polls):
    run("db", "migrate", "--config", alert_config)

    def broken(window):
        raise RuntimeError("detector bug")
    monkeypatch.setattr(bw, "detect_all", broken)
    code, out, err = run("poll", *net(both_server), "--config", alert_config, "--count", 3, "--interval", 1, "--alerts")
    assert code == ExitCode.OK and "6 stored, 0 failed" in out
    assert err.count("warning: alert check failed: RuntimeError: detector bug") == 1


def test_a_restart_in_an_outage_carries_on_counting_and_doesnt_alert_twice(conn):
    db.migrate(conn)
    from sigen import ntfy

    def poll(t, error=None):
        pid = db.next_poll_id(conn)
        for unit in (247, 1):
            db.insert_reading(conn, ip="127.0.0.1", unit_id=unit, poll_id=pid, started_at=t, ended_at=t,
                              values=[] if error else ([(30014, 730)] if unit == 247 else [(30601, 730)]),
                              error=error)
    poll(T0)
    for k in range(1, 23):                                  # 22 failed polls: alerted at the 20th
        poll(T0 + k * 15 * SEC, error="timed out")
    db.insert_alert(conn, ntfy.Attempt(T0 + 20 * 15 * SEC, "No reading", kind="unreachable", http_code=200))
    sender = FakeSender()
    w = alerts.Watcher(sender, detect=lambda window: [], out=lambda m: None, err=lambda m: None)
    w.last_sent.update(db.last_alerts_sent(conn))
    w.preload_outage(conn)
    assert (w.failed_polls, w.outage_start, w.outage_alerted) == (22, T0 + 15 * SEC, True)
    t = T0 + 23 * 15 * SEC
    assert w.reachability(failed_poll(t), t) is None and sender.calls == []          # not again
    conn.execute("DELETE FROM alert_log")                   # a restart before the 20th poll alerted …
    w = alerts.Watcher(sender, detect=lambda window: [], out=lambda m: None, err=lambda m: None)
    w.preload_outage(conn)
    assert w.outage_alerted is False and w.reachability(failed_poll(t), t) == "unreachable"    # … alerts at once


def test_poll_alerts_needs_the_alerts_section(run, config, both_server):
    run("db", "migrate", "--config", config)
    code, _, err = run("poll", *net(both_server), "--config", config, "--count", 1, "--alerts")
    assert code == ExitCode.USAGE and 'add "alerts"' in err
