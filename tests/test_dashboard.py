"""sigen.charts and `sigen dashboard`: the window parser offline, the numbers and the pages against the test
database (skipped without one, like test_db.py)."""
import datetime
import json
import math
from zoneinfo import ZoneInfo

import pytest

from sigen import charts, db, dashboard
from sigen.config import Config
from sigen.errors import ExitCode
from sigen.registers import BY_NAME

TZ = ZoneInfo("America/Edmonton")
NAMES = {1: "Garage", 7: "Basement Plugs"}


# ---------- windows (no database) ----------
NOW = datetime.datetime(2026, 10, 11, 14, 30, tzinfo=TZ)


def test_parse_window():
    start, end, live, text = dashboard.parse_window({}, TZ, NOW)
    assert (start, end, live) == (datetime.datetime(2026, 10, 11, tzinfo=TZ), NOW, True) and "so far" in text
    start, end, live, _ = dashboard.parse_window({"day": "2026-10-09"}, TZ, NOW)
    assert (start, end, live) == (datetime.datetime(2026, 10, 9, tzinfo=TZ), datetime.datetime(2026, 10, 10, tzinfo=TZ),
                                  False)
    start, end, live, text = dashboard.parse_window({"hours": "6"}, TZ, NOW)
    assert (end - start, live, text) == (datetime.timedelta(hours=6), True, "last 6 h")
    for bad in ({"day": "10/09/2026"}, {"hours": "x"}, {"hours": "0"}, {"hours": "9999"},
                {"day": "2026-10-09", "hours": "6"}):
        with pytest.raises(dashboard.BadWindow):
            dashboard.parse_window(bad, TZ, NOW)


def test_rolling_mean():
    t = [NOW + datetime.timedelta(seconds=15 * k) for k in range(40)]
    values = [30.0 + (k % 2) for k in range(40)]                         # 30, 31, 30, 31, … every 15 s
    out = charts.rolling_mean(t, values, datetime.timedelta(minutes=5))
    assert all(math.isnan(v) for v in out[:18])                          # less than 4.5 min behind them
    assert out[18] == pytest.approx((10 * 30 + 9 * 31) / 19)             # 4.5 min: 19 readings
    assert out[19:] == [pytest.approx(30.5)] * 21                        # 20 readings: ten of each
    gap = t[:20] + [x + datetime.timedelta(minutes=10) for x in t[20:]]  # polling stopped for 10 minutes
    out = charts.rolling_mean(gap, values, datetime.timedelta(minutes=5))
    assert math.isnan(out[25]) and not math.isnan(out[19])
    one_missed = t[:10] + t[11:]                                         # one 30 s gap is fine
    assert not math.isnan(charts.rolling_mean(one_missed, values[:39], datetime.timedelta(minutes=5))[-1])


def test_battery_power_axis_is_at_least_2_kw():
    t = [NOW + datetime.timedelta(seconds=15 * k) for k in range(40)]
    night = charts.Series(t, {charts.PV_POWER: [0.0] * 40, charts.BATTERY_POWER: [0.001 + (k % 2) / 1000 for k in range(40)]})
    ax = charts.battery_power_figure(night, TZ).axes[0]
    assert ax.get_ylim() == (pytest.approx(-0.999), pytest.approx(1.001))      # around the middle, 2 kW tall
    assert len(ax.get_lines()) == 5 and ax.get_lines()[0].get_alpha() == 0.2   # raw, mean, raw, mean, zero line
    day = charts.Series(t, {charts.PV_POWER: [0.2 + 15.8 * k / 39 for k in range(40)], charts.BATTERY_POWER: [0.0] * 40})
    low, high = charts.battery_power_figure(day, TZ).axes[0].get_ylim()
    assert low < 0 and 16.0 < high < 17.0                                      # matplotlib's own scale


def test_battery_power_with_soc():
    t = [NOW + datetime.timedelta(seconds=15 * k) for k in range(40)]
    power = charts.Series(t, {charts.PV_POWER: [1.0] * 40, charts.BATTERY_POWER: [0.5] * 40})
    soc = charts.Series(t[::4], {charts.SOC: [80.0 + k / 5 for k in range(10)],
                                 "plant_backup_soc": [73.0] * 10, "plant_charge_cut_off_soc": [86.0] * 10})
    fig = charts.battery_power_figure(power, TZ, soc)
    left, right = fig.axes
    assert right.get_ylabel() == "SOC (%)" and right.get_ylim() == (71.0, 88.0)    # the settings' range, padded
    assert [l.get_label() for l in right.get_legend().get_lines()] == [
        "PV (5-min mean)", "battery, >0 charging, <0 discharging (5-min mean)", "SOC",
        "backup reserve (setting)", "charge cut-off (setting)"]                     # faint readings left out
    assert "SOC 80.0 → 81.8 %" in left.get_title()
    flat = charts.Series(t[:1], {charts.SOC: [73.0], "plant_backup_soc": [73.0], "plant_charge_cut_off_soc": [73.0]})
    assert charts.battery_power_figure(power, TZ, flat).axes[1].get_ylim() == (68.0, 78.0)   # MIN_SOC_SPAN
    assert len(charts.battery_power_figure(power, TZ, charts.Series([], {})).axes) == 1       # no plant readings


def test_dashboard_needs_a_database_url(run):
    code, _, err = run("dashboard")                       # the tests' config is empty
    assert code == ExitCode.USAGE and "no database URL" in err


# ---------- with the test database ----------
psycopg = pytest.importorskip("psycopg")
from test_db import config, conn, db_url  # noqa: E402,F401  (pytest fixtures; never request db_url in a test)

T0 = datetime.datetime(2026, 10, 1, 12, 0, 5, tzinfo=TZ)        # a past day, so its page doesn't reload


def stored(name, value):
    reg = BY_NAME[name]
    return reg.address, round(value * reg.gain)


def plant_values(garage_kwh, plugs_kwh, house_kwh, garage_kw, plugs_kw, total_kw):
    values = [stored(f"plant_smart_load_{n}_total_consumption", 0.0) for n in range(1, 25) if n not in (1, 7)]
    values += [stored(f"plant_smart_load_{n}_power", 0.0) for n in range(1, 25) if n not in (1, 7)]
    values += [stored("plant_smart_load_1_total_consumption", garage_kwh),
               stored("plant_smart_load_7_total_consumption", plugs_kwh),
               stored("plant_accumulated_load_consumption", house_kwh),
               stored("plant_si_common_load_consumption", house_kwh),
               stored("plant_smart_load_1_power", garage_kw), stored("plant_smart_load_7_power", plugs_kw),
               stored("plant_total_load_power", total_kw)]
    return sorted(values)


def inverter_values(volts, amps, coolest=8.0, battery_kw=0.5):
    """Strings, plus the battery: clusters at coolest / +4 °C, cells +2, inverter 35 °C; battery and PV power."""
    return sorted([stored(f"inverter_pv{n}_voltage", volts) for n in range(1, 5)]
                  + [stored(f"inverter_pv{n}_current", amps * n) for n in range(1, 5)]
                  + [stored("inverter_ess_min_cluster_temperature", coolest),
                     stored("inverter_ess_max_cluster_temperature", coolest + 4),
                     stored("inverter_ess_average_cell_temperature", coolest + 2),
                     stored("inverter_pcs_internal_temperature", 35.0),
                     stored("inverter_ess_power", battery_kw), stored("inverter_pv_power", 2.0)])


@pytest.fixture
def seeded(run, config, conn):
    """Three polls 15 minutes apart (12:00, 12:15, 12:30 local), plant + inverter, and a failed plant reading."""
    run("db", "migrate", "--config", config)
    plant = [(10.0, 5.0, 100.0, 1.44, 0.20, 2.0), (10.36, 5.05, 100.75, 1.44, 0.20, 2.5),
             (10.72, 5.10, 101.50, 0.00, 0.30, 0.9)]
    for k, p in enumerate(plant):
        t = T0 + datetime.timedelta(minutes=15 * k)
        poll = db.next_poll_id(conn)
        db.insert_reading(conn, ip="127.0.0.1", unit_id=247, poll_id=poll, started_at=t, ended_at=t,
                          values=plant_values(*p))
        db.insert_reading(conn, ip="127.0.0.1", unit_id=1, poll_id=poll, started_at=t, ended_at=t,
                          values=inverter_values(300.0 + k, 1.0 + k, coolest=8.0 + k, battery_kw=[0.5, 1.5, -1.0][k]))
    db.insert_reading(conn, ip="127.0.0.1", unit_id=247, poll_id=db.next_poll_id(conn),
                      started_at=T0 + datetime.timedelta(minutes=40), ended_at=T0 + datetime.timedelta(minutes=40),
                      error="timed out")
    return config


def test_energy_by_load(seeded, conn):
    e = charts.energy_by_load(conn, NAMES)
    assert e.house == 1.5 and e.check == 1.5
    assert e.bars == {"Garage": 0.72, "Basement Plugs": 0.1, "Other": 0.68}       # named slots, in slot order
    assert e.hours == 0.5


def test_counters_and_average_power(seeded, conn):
    c = charts.smart_load_counters(conn, NAMES)
    assert len(c.times) == 3 and list(c.series) == ["Garage (1)", "Basement Plugs (7)", charts.OTHER]
    assert c.series[charts.OTHER] == pytest.approx([85.0, 85.34, 85.68])          # house − all 24 counters
    assert c.average_kw("Garage (1)") == pytest.approx([1.44, 1.44])               # 0.36 kWh per 15 min
    since = c.since_start()                                                        # the dashboard's top panel
    assert since["Garage (1)"] == pytest.approx([0, 0.36, 0.72]) and since[charts.OTHER] == pytest.approx([0, 0.34, 0.68])
    assert charts.smart_load_counters(conn, NAMES, start=T0 + datetime.timedelta(minutes=20)) is None


def test_energy_at_every_reading(seeded, conn):
    c = charts.smart_load_energy(conn, NAMES)
    assert len(c.times) == 3 and c.since_start()["Garage (1)"] == pytest.approx([0, 0.36, 0.72])
    assert c.since_start()[charts.OTHER] == pytest.approx([0, 0.34, 0.68])
    thin = charts.smart_load_energy(conn, NAMES, max_points=2)              # every 2nd reading, and always the last
    assert thin.times == [c.times[0], c.times[-1]]
    ax, = charts.energy_since_start_figure(c, TZ).axes                       # one panel: no average-power panel
    assert "a reading every ~15.0 min" in ax.get_title() and len(ax.get_lines()) == 3
    s = charts.load_power_snapshots(conn, NAMES)
    ax, = charts.snapshots_figure(s, TZ).axes
    assert len(ax.get_lines()) == 2 * len(s.series) and ax.get_lines()[0].get_alpha() == 0.2   # faint, then mean
    assert "5-min mean" in ax.get_title()


def test_snapshots_and_strings(seeded, conn):
    s = charts.load_power_snapshots(conn, NAMES)
    assert s.series["Garage (1)"] == pytest.approx([1.44, 1.44, 0.0])
    assert s.series[charts.OTHER] == pytest.approx([0.36, 0.86, 0.6])             # total − all 24 slots
    strings = charts.string_readings(conn, end=T0 + datetime.timedelta(minutes=20))
    assert len(strings.times) == 2 and strings.kw(2) == pytest.approx([0.6, 1.204])  # 300 V × 2 A, 301 V × 4 A


def test_battery_series_and_figures(seeded, conn):
    temps = charts.inverter_series(conn, [*charts.BATTERY_TEMPS, charts.INVERTER_TEMP])
    assert len(temps.times) == 3 and temps.values["inverter_ess_min_cluster_temperature"] == [8.0, 9.0, 10.0]
    conn.execute("INSERT INTO open_meteo_weather (fetched_at, resolution_minutes, valid_at, temperature_2m) VALUES "
                 "(%s, 15, %s, -3.5)", (T0, T0))
    air = charts.air_temperatures(conn, T0 - datetime.timedelta(hours=1), T0 + datetime.timedelta(hours=1))
    assert air == [(T0, -3.5)]
    fig = charts.battery_temps_figure(temps, air, TZ)
    left, right = fig.axes                                          # the inverter on its own axis, on the right
    assert [l.get_label() for l in right.get_lines()] == ["inverter internal (5-min mean)"]
    assert right.get_ylabel() == "Inverter internal (°C)" and "now 10.0 °C" in left.get_title()
    assert len(right.get_legend().get_lines()) == 5                 # 3 battery, the air, the inverter: on top
    power = charts.inverter_series(conn, [charts.BATTERY_POWER, charts.PV_POWER])
    title = charts.battery_power_figure(power, TZ).axes[0].get_title()
    assert "0.50 kWh in, 0.00 kWh out" in title                     # 0.5 then 1.5 kW for 15 min each; -1.0 is the end


def test_string_power_with_clear_sky(seeded, conn, tmp_path):
    from test_pvmodel import ARRAY, SITE, config_with
    c = config_with(tmp_path, site=SITE, array=ARRAY)
    day = datetime.datetime(2026, 10, 1, tzinfo=TZ)
    plain = charts.clear_sky_strings(conn, c.site, c.array, day, day + datetime.timedelta(days=1))
    conn.execute("INSERT INTO open_meteo_weather (fetched_at, resolution_minutes, valid_at, temperature_2m, "
                 "wind_speed_10m) SELECT %s, 15, t, -20, 0 FROM generate_series(%s::timestamptz, %s, '15 minutes') t",
                 (T0, day, day + datetime.timedelta(days=1)))                    # a cold, still day, every 15 min
    cold = charts.clear_sky_strings(conn, c.site, c.array, day, day + datetime.timedelta(days=1))
    assert list(plain.columns) == [1, 2, 3, 4] and len(plain) == 24 * 60
    assert cold.sum().sum() > plain.sum().sum() * 1.05                # −20 °C and still air: cold panels make more
    conn.execute("DELETE FROM open_meteo_weather WHERE valid_at > %s", (day.replace(hour=11),))   # data stops at 11:00
    edge = charts.clear_sky_strings(conn, c.site, c.array, day.replace(hour=10), day.replace(hour=12))[1]
    jumps = edge.diff().abs()
    assert jumps.max() < 0.02                     # no step where the weather runs out: the last row holds for an hour
    fig = charts.strings_figure(charts.string_readings(conn), TZ, "power", plain, {n: s.face for n, s in c.array.strings.items()})
    ax, = fig.axes
    assert len(ax.get_lines()) == 9 and "String 1 (south)" in [l.get_label() for l in ax.get_lines()]
    assert "clear sky:" in ax.get_title()


def test_live_limits():
    t = [NOW + datetime.timedelta(minutes=k) for k in range(10)]
    dark, lit = [0.0, 3.0, 9.9], [12.0, 250.0, 0.0, 300.0, 15.0]                 # a dark gap in the middle stays
    volts = {n: dark + lit + [2.0, 0.0] for n in charts.STRINGS}
    volts[2] = [0.0] * 2 + [11.0] + [0.0] * 7                                    # one string lit earlier is enough
    data = charts.StringData(t, volts, {n: [0.0] * 10 for n in charts.STRINGS})
    assert charts.live_limits(data) == (t[2], t[7])
    night = charts.StringData(t, {n: [5.0] * 10 for n in charts.STRINGS}, {n: [0.0] * 10 for n in charts.STRINGS})
    assert charts.live_limits(night) is None


def app_for(config_path):
    """The dashboard for the seeded test schema. The URL is read here, never passed as a test argument: pytest
    prints a failing test's arguments, and the URL holds the test database's password."""
    url = json.loads(config_path.read_text(encoding="utf-8"))["database"]["url"]
    return dashboard.create_app(Config(database_url=url, origin="test")).test_client()


def test_pages(seeded):
    client = app_for(seeded)
    page = client.get("/?day=2026-10-01")
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and "Thursday 2026-10-01" in html and "http-equiv" not in html  # a past day
    for name in dashboard.CHARTS:
        assert f"/chart/{name}.png?day=2026-10-01" in html
        r = client.get(f"/chart/{name}.png?day=2026-10-01")
        assert r.status_code == 200 and r.mimetype == "image/png" and r.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert 'http-equiv="refresh" content="300"' in client.get("/").get_data(as_text=True)        # today: live
    assert client.get("/chart/nope.png").status_code == 404
    bad = client.get("/chart/string_power.png?hours=x")
    assert bad.status_code == 400 and bad.data[:4] == b"\x89PNG"
    assert "Bad window" in client.get("/?day=yesterday").get_data(as_text=True)


def test_database_down_is_drawn_not_raised():
    client = dashboard.create_app(Config(database_url="postgresql://u:s3cret@127.0.0.1:1/x", origin="t")).test_client()
    r = client.get("/chart/load_energy.png")
    assert r.status_code == 503 and r.data[:4] == b"\x89PNG"


def test_cli_starts_the_server(run, seeded, monkeypatch):
    calls = []
    monkeypatch.setattr("flask.Flask.run", lambda self, **kw: calls.append(kw))
    code, _, err = run("dashboard", "--config", seeded, "--port", 8123, "--reload")
    assert code == ExitCode.OK and "http://127.0.0.1:8123/" in err
    assert calls == [{"host": "0.0.0.0", "port": 8123, "debug": False, "use_debugger": False, "use_reloader": True,
                      "threaded": True}]
    password = psycopg.conninfo.conninfo_to_dict(json.loads(seeded.read_text())["database"]["url"]).get("password")
    assert not password or password not in err
