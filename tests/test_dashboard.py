"""sigen.charts and `sigen dashboard`: the window parser offline, the numbers and the pages against the test
database (skipped without one, like test_db.py)."""

import datetime
import json
import math
import re
from zoneinfo import ZoneInfo

import pytest

from sigen import charts, db, dashboard, goes, goes_shade
from sigen.config import Config, Site
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
    assert (start, end, live) == (
        datetime.datetime(2026, 10, 9, tzinfo=TZ),
        datetime.datetime(2026, 10, 10, tzinfo=TZ),
        False,
    )
    start, end, live, text = dashboard.parse_window({"hours": "6"}, TZ, NOW)
    assert (end - start, live, text) == (datetime.timedelta(hours=6), True, "last 6 h")
    start, end, live, text = dashboard.parse_window({"minutes": "10"}, TZ, NOW)
    assert (end - start, live, text) == (datetime.timedelta(minutes=10), True, "last 10 min")
    for bad in (
        {"minutes": "x"},
        {"minutes": "0"},
        {"minutes": "10", "hours": "1"},
        {"minutes": "10", "day": "2026-10-09"},
    ):
        with pytest.raises(dashboard.BadWindow):
            dashboard.parse_window(bad, TZ, NOW)
    denver = ZoneInfo("America/Denver")  # (tzdata 2026 has Edmonton on UTC−6 all year from 2026: no change there)
    after_dst = datetime.datetime(2026, 11, 2, 10, 0, tzinfo=denver)  # daylight saving ended on 11-01, 02:00
    start, end, _, _ = dashboard.parse_window({"hours": "72"}, denver, after_dst)
    utc = datetime.timezone.utc
    assert end.astimezone(utc) - start.astimezone(utc) == datetime.timedelta(hours=72)  # real hours, not wall clock
    assert start == datetime.datetime(2026, 10, 30, 11, 0, tzinfo=denver)  # 11:00 MDT = 10:00 MST + 72 h
    for bad in (
        {"day": "10/09/2026"},
        {"hours": "x"},
        {"hours": "0"},
        {"hours": "9999"},
        {"day": "2026-10-09", "hours": "6"},
    ):
        with pytest.raises(dashboard.BadWindow):
            dashboard.parse_window(bad, TZ, NOW)


def test_satellite_strips():
    t = [NOW + datetime.timedelta(minutes=10 * k) for k in range(3)]
    sky = goes_shade.ShadeSeries(
        t, [5.0, 20.0, 30.0], [150.0] * 3, {"trans_top": [1.0, 0.5, math.nan], "prob_0km": [0.0, 1.0, 0.25]}
    )
    strips, note = charts.satellite_strips(sky)
    assert [s.label for s in strips] == [label for _, label, _, _ in charts.SATELLITE_STRIPS]
    by_label = {s.label: s for s in strips}
    top, overhead, cloud = (by_label[charts.SATELLITE_STRIPS[k][1]] for k in (0, 1, 5))  # trans_top, _0km, prob_0km
    assert top.starts == t and top.length == goes_shade.SCAN
    # 5° sun: too low (pale blue); 0.5 through: mid grey; no value: no bar (the hatching shows)
    assert top.colours == [charts.LOW_SUN_COLOUR, "#808080", None]
    assert cloud.colours == ["#ffffff", "#000000", "#bfbfbf"]  # 1 − prob: 1, 0, 0.75
    assert overhead.colours == [charts.LOW_SUN_COLOUR, None, None]  # a column it doesn't have: only the low sun
    assert "GOES-West" in note


def test_dashboard_needs_a_database_url(run):
    code, _, err = run("dashboard")  # the tests' config is empty
    assert code == ExitCode.USAGE and "no database URL" in err


# ---------- with the test database ----------
psycopg = pytest.importorskip("psycopg")
from test_db import config, conn, db_url  # noqa: E402,F401  (pytest fixtures; never request db_url in a test)

T0 = datetime.datetime(2026, 10, 1, 12, 0, 5, tzinfo=TZ)  # a past day, so its page doesn't reload


def stored(name, value):
    reg = BY_NAME[name]
    return reg.address, round(value * reg.gain)


def plant_values(garage_kwh, plugs_kwh, house_kwh, garage_kw, plugs_kw, total_kw):
    values = [stored(f"plant_smart_load_{n}_total_consumption", 0.0) for n in range(1, 25) if n not in (1, 7)]
    values += [stored(f"plant_smart_load_{n}_power", 0.0) for n in range(1, 25) if n not in (1, 7)]
    values += [
        stored("plant_smart_load_1_total_consumption", garage_kwh),
        stored("plant_smart_load_7_total_consumption", plugs_kwh),
        stored("plant_accumulated_load_consumption", house_kwh),
        stored("plant_si_common_load_consumption", house_kwh),
        stored("plant_smart_load_1_power", garage_kw),
        stored("plant_smart_load_7_power", plugs_kw),
        stored("plant_total_load_power", total_kw),
    ]
    return sorted(values)


def inverter_values(volts, amps, coolest=8.0, battery_kw=0.5):
    """Strings, plus the battery: clusters at coolest / +4 °C, cells +2, inverter 35 °C; battery and PV power."""
    return sorted(
        [stored(f"inverter_pv{n}_voltage", volts) for n in range(1, 5)]
        + [stored(f"inverter_pv{n}_current", amps * n) for n in range(1, 5)]
        + [
            stored("inverter_ess_min_cluster_temperature", coolest),
            stored("inverter_ess_max_cluster_temperature", coolest + 4),
            stored("inverter_ess_average_cell_temperature", coolest + 2),
            stored("inverter_pcs_internal_temperature", 35.0),
            stored("inverter_ess_power", battery_kw),
            stored("inverter_pv_power", 2.0),
        ]
    )


@pytest.fixture
def seeded(run, config, conn):
    """Three polls 15 minutes apart (12:00, 12:15, 12:30 local), plant + inverter, and a failed plant reading."""
    run("db", "migrate", "--config", config)
    plant = [
        (10.0, 5.0, 100.0, 1.44, 0.20, 2.0),
        (10.36, 5.05, 100.75, 1.44, 0.20, 2.5),
        (10.72, 5.10, 101.50, 0.00, 0.30, 0.9),
    ]
    for k, p in enumerate(plant):
        t = T0 + datetime.timedelta(minutes=15 * k)
        poll = db.next_poll_id(conn)
        db.insert_reading(
            conn, ip="127.0.0.1", unit_id=247, poll_id=poll, started_at=t, ended_at=t, values=plant_values(*p)
        )
        db.insert_reading(
            conn,
            ip="127.0.0.1",
            unit_id=1,
            poll_id=poll,
            started_at=t,
            ended_at=t,
            values=inverter_values(300.0 + k, 1.0 + k, coolest=8.0 + k, battery_kw=[0.5, 1.5, -1.0][k]),
        )
    db.insert_reading(
        conn,
        ip="127.0.0.1",
        unit_id=247,
        poll_id=db.next_poll_id(conn),
        started_at=T0 + datetime.timedelta(minutes=40),
        ended_at=T0 + datetime.timedelta(minutes=40),
        error="timed out",
    )
    return config


def test_energy_by_load(seeded, conn):
    e = charts.energy_by_load(conn, NAMES)
    assert e.house == 1.5 and e.check == 1.5
    assert e.bars == {"Garage": 0.72, "Basement Plugs": 0.1, "Other": 0.68}  # named slots, in slot order
    assert e.hours == 0.5


def test_counters_and_average_power(seeded, conn):
    c = charts.smart_load_counters(conn, NAMES)
    assert len(c.times) == 3 and list(c.series) == ["Garage (1)", "Basement Plugs (7)", charts.OTHER]
    assert c.series[charts.OTHER] == pytest.approx([85.0, 85.34, 85.68])  # house − all 24 counters
    assert c.average_kw("Garage (1)") == pytest.approx([1.44, 1.44])  # 0.36 kWh per 15 min
    since = c.since_start()  # the dashboard's top panel
    assert since["Garage (1)"] == pytest.approx([0, 0.36, 0.72]) and since[charts.OTHER] == pytest.approx(
        [0, 0.34, 0.68]
    )
    assert charts.smart_load_counters(conn, NAMES, start=T0 + datetime.timedelta(minutes=20)) is None


def test_energy_at_every_reading(seeded, conn):
    c = charts.smart_load_energy(conn, NAMES)
    assert len(c.times) == 3 and c.since_start()["Garage (1)"] == pytest.approx([0, 0.36, 0.72])
    assert c.since_start()[charts.OTHER] == pytest.approx([0, 0.34, 0.68])
    thin = charts.smart_load_energy(conn, NAMES, max_points=2)  # every 2nd reading, and always the last
    assert thin.times == [c.times[0], c.times[-1]]
    assert charts.smart_load_energy(conn, NAMES, start=T0 + datetime.timedelta(minutes=20)) is None  # one reading
    assert len(charts.smart_load_energy(conn, NAMES, start=T0 + datetime.timedelta(minutes=20), fewest=1).times) == 1


def test_snapshots_and_strings(seeded, conn):
    s = charts.load_power_snapshots(conn, NAMES)
    assert s.series["Garage (1)"] == pytest.approx([1.44, 1.44, 0.0])
    assert s.series[charts.OTHER] == pytest.approx([0.36, 0.86, 0.6])  # total − all 24 slots
    strings = charts.string_readings(conn, end=T0 + datetime.timedelta(minutes=20))
    assert len(strings.times) == 2 and strings.kw(2) == pytest.approx([0.6, 1.204])  # 300 V × 2 A, 301 V × 4 A


def test_battery_series(seeded, conn):
    temps = charts.inverter_series(conn, [*charts.BATTERY_TEMPS, charts.INVERTER_TEMP])
    assert len(temps.times) == 3 and temps.values["inverter_ess_min_cluster_temperature"] == [8.0, 9.0, 10.0]
    conn.execute(
        "INSERT INTO open_meteo_weather (fetched_at, resolution_minutes, valid_at, temperature_2m) VALUES "
        "(%s, 15, %s, -3.5)",
        (T0, T0),
    )
    air = charts.air_temperatures(conn, T0 - datetime.timedelta(hours=1), T0 + datetime.timedelta(hours=1))
    assert air == [(T0, -3.5)]
    power = charts.inverter_series(conn, [charts.BATTERY_POWER, charts.PV_POWER])
    assert power.values[charts.BATTERY_POWER] == [0.5, 1.5, -1.0]


def test_string_power_with_clear_sky(seeded, conn, tmp_path):
    from test_pvmodel import ARRAY, SITE, config_with

    c = config_with(tmp_path, site=SITE, array=ARRAY)
    day = datetime.datetime(2026, 10, 1, tzinfo=TZ)
    plain = charts.clear_sky_strings(conn, c.site, c.array, day, day + datetime.timedelta(days=1))
    conn.execute(
        "INSERT INTO open_meteo_weather (fetched_at, resolution_minutes, valid_at, temperature_2m, "
        "wind_speed_10m) SELECT %s, 15, t, -20, 0 FROM generate_series(%s::timestamptz, %s, '15 minutes') t",
        (T0, day, day + datetime.timedelta(days=1)),
    )  # a cold, still day, every 15 min
    cold = charts.clear_sky_strings(conn, c.site, c.array, day, day + datetime.timedelta(days=1))
    assert list(plain.columns) == [1, 2, 3, 4] and len(plain) == 24 * 60
    assert cold.sum().sum() > plain.sum().sum() * 1.05  # −20 °C and still air: cold panels make more
    conn.execute("DELETE FROM open_meteo_weather WHERE valid_at > %s", (day.replace(hour=11),))  # data stops at 11:00
    edge = charts.clear_sky_strings(conn, c.site, c.array, day.replace(hour=10), day.replace(hour=12))[1]
    jumps = edge.diff().abs()
    assert jumps.max() < 0.02  # no step where the weather runs out: the last row holds for an hour
    assert str(plain.index.tz) == "UTC" and plain.index[0] == day  # UTC, whatever zones it was given


def test_wet_bulb():
    assert charts.wet_bulb(20.0, 50.0) == pytest.approx(13.7, abs=0.1)  # Stull 2011's worked example
    assert charts.wet_bulb(0.0, 100.0) == pytest.approx(0.0, abs=0.3)  # saturated air: about the air itself


def test_precipitation(seeded, conn):
    day = datetime.datetime(2026, 10, 1, tzinfo=TZ)
    at = day.replace
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO open_meteo_weather (fetched_at, resolution_minutes, valid_at, weather_code, temperature_2m, "
            "relative_humidity_2m) VALUES (%s, 15, %s, %s, %s, 100)",
            [
                (T0, at(hour=12, minute=0), 3, 0.0),
                (T0, at(hour=12, minute=15), 3, 0.0),  # overcast, wet-bulb ~0: snow
                (T0, at(hour=12, minute=45), 61, 4.0),  # rain by its code; 12:30 is missing
                (T0, at(hour=13, minute=0), 73, 4.0),
            ],
        )  # snow by its code, however warm
        cur.executemany(
            "INSERT INTO radar_frames (fetched_at, frame_at, rain_mm_h, snow_cm_h, covered) VALUES (%s, %s, %s, %s, true)",
            [
                (T0, at(hour=h, minute=m), r, s)
                for h, m, r, s in (
                    (12, 0, 0.0, 0.0),
                    (12, 6, 0.15, 0.15),
                    (12, 12, 0.3, 0.2),
                    (12, 24, 0.5, 0.4),
                    (12, 36, 1.0, 0.6),
                    (12, 48, 0.2, 0.15),
                    (12, 54, 0.1, 0.1),
                    (13, 6, 0.0, 0.0),
                )
            ]
            + [
                (T0, day - datetime.timedelta(days=3), 2.0, 1.5),  # the week's most
                (T0, day - datetime.timedelta(days=8), 9.0, 9.0),
            ],
        )  # more than a week before the window's end
        cur.execute("INSERT INTO radar_frames (fetched_at, error) VALUES (%s, 'HTTP 500')", (T0,))
    p = charts.precipitation(conn, day, day + datetime.timedelta(days=1))
    # 12:00 is the end of its own period; 12:24 falls in the missing 12:30, filled in: the codes either side differ,
    # so the wet-bulb of 2 °C (halfway) decides: rain. 13:06 is past the last row: no kind.
    assert p.kinds == ["snow", "snow", "snow", "rain", "rain", "snow", "snow", None]
    assert p.kind == "snow" and p.top == {"rain": 2.0, "snow": 1.5}
    shares = p.shares()  # snow_cm_h ÷ 1.5, log scale k = 20
    assert shares[0] == 0 and shares[1] == pytest.approx(math.log1p(2) / math.log1p(20))
    assert charts.precipitation(conn, at(hour=12, minute=30), at(hour=12, minute=45)).kind == "rain"
    dry = charts.precipitation(conn, day - datetime.timedelta(days=20), day - datetime.timedelta(days=19))
    assert (dry.times, dry.kind, dry.top) == ([], "rain", {"rain": 1.0, "snow": 1.0})  # a clear day: white "Rain"

    strip, note = charts.precipitation_strip(p)
    assert strip.label == "Snow" and strip.starts == p.times and strip.length == charts.RADAR_FRAME
    grey = round((1 - math.log1p(2) / math.log1p(20)) * 255)  # 0.15 of 1.5 cm/h, lerped white to black
    assert strip.colours[:2] == ["#ffffff", f"#{grey:02x}{grey:02x}{grey:02x}"]  # 0 cm/h: white
    assert "darkest = 1.50 cm/h" in note
    strip, _ = charts.precipitation_strip(
        charts.Precipitation(
            times=[day], rain_mm_h=[2.0], snow_cm_h=[0.0], kinds=["rain"], top={"rain": 2.0, "snow": 1.0}
        )
    )
    assert (strip.label, strip.colours) == ("Rain", [charts.PRECIP_COLOURS["rain"]])  # the top rate: the full colour
    assert charts.precipitation_strip(dry)[0].colours == []  # no frames: no bars, all hatched


DAY2 = datetime.datetime(2026, 10, 2, tzinfo=TZ)


@pytest.fixture
def a_day(seeded, conn):
    """2026-10-02: plant readings with the six lifetime energy counters and the four powers, and the first reading
    after midnight, which closes the day. Its totals: solar 30, house 15, battery 3.5 in / 1.25 out, grid 8.1 in / 20
    out. Flows only from 00:00:10-00:01:10 (the battery runs the house, 1 kW) and 12:00-12:01 (5 kW of sun: 1 to the
    house, 2 to the battery, 2 exported); the other gaps are over 5 minutes."""

    def plant(at, counters, pv, battery, grid, load):
        t = DAY2 + at
        names = [
            "plant_accumulated_pv_energy",
            "plant_accumulated_load_consumption",
            "plant_accumulated_battery_charge_energy",
            "plant_accumulated_battery_discharge_energy",
            "plant_accumulated_grid_import_energy",
            "plant_accumulated_grid_export_energy",
        ]
        db.insert_reading(
            conn,
            ip="127.0.0.1",
            unit_id=247,
            poll_id=db.next_poll_id(conn),
            started_at=t,
            ended_at=t,
            values=sorted(
                [stored(n, c) for n, c in zip(names, counters)]
                + [
                    stored("plant_photovoltaic_power", pv),
                    stored("plant_ess_power", battery),
                    stored("plant_grid_sensor_active_power", grid),
                    stored("plant_total_load_power", load),
                ]
            ),
        )

    m = lambda h, mi, s=0: datetime.timedelta(hours=h, minutes=mi, seconds=s)
    #     when            solar   house   chg    dis     in      out      pv  batt grid load
    plant(m(0, 0, 10), (100, 200, 50, 40, 100, 50), 0, -1, 0, 1)
    plant(m(0, 1, 10), (100, 200.02, 50, 40.02, 100, 50), 0, -1, 0, 1)
    plant(m(12, 0), (120, 210, 52, 41, 105, 60), 5, 2, -2, 1)
    plant(m(12, 1), (120.08, 211, 52.03, 41, 105.5, 61), 5, 2, -2, 1)
    plant(m(23, 59, 59), (129, 214, 53.4, 41.2, 108, 70), 0, 0, 0, 1)
    plant(m(24, 0, 5), (130, 215, 53.5, 41.25, 108.1, 70), 0, 0, 0, 1)  # the next day's first: it closes this one
    return seeded


def test_day_energy(a_day, conn):
    e = charts.day_energy(conn, DAY2, DAY2 + datetime.timedelta(days=1))
    assert e.totals == pytest.approx(
        {
            "solar": 30,
            "load": 15,
            "battery_charged": 3.5,
            "battery_discharged": 1.25,
            "grid_imported": 8.1,
            "grid_exported": 20,
        }
    )  # midnight to midnight
    minute = 1 / 60
    assert e.flows == pytest.approx(
        {
            ("Battery", "Load"): minute,
            ("Solar", "Load"): minute,
            ("Solar", "Battery"): 2 * minute,
            ("Solar", "Grid"): 2 * minute,
            ("Battery", "Grid"): 0,
            ("Grid", "Load"): 0,
            ("Grid", "Battery"): 0,
        }
    )
    # 10-03 so far: its only reading is the one that closed 10-02 (days share their boundary: they add up exactly);
    # with no reading after it, that's also its closing reading, so nothing yet
    so_far = charts.day_energy(conn, DAY2 + datetime.timedelta(days=1), DAY2 + datetime.timedelta(days=2))
    assert so_far.totals == {key: 0 for key, _ in charts.DAY_TOTALS} and so_far.first == DAY2 + datetime.timedelta(
        days=1, seconds=5
    )
    assert charts.day_energy(conn, DAY2 - datetime.timedelta(days=30), DAY2 - datetime.timedelta(days=29)) is None
    data = charts.sankey(e)  # every flow here is a minute's worth, far under 0.5 % of the day: none drawn
    assert (data["sources"], data["sinks"], data["flows"]) == ([], [], [])
    assert len(data["dropped"]) == 4 and {"from": "Battery", "to": "Load", "kwh": round(minute, 4)} in data["dropped"]


def test_sankey_as_json(a_day):
    client = app_for(a_day)
    d = client.get("/data/sankey.json?day=2026-10-02").get_json()  # /daily's computation, as JSON
    assert d["first"] == "2026-10-02T06:00:10.000000Z" and d["last"] == "2026-10-03T05:59:59.000000Z"  # the day's
    assert set(d["sankey"]) == {"sources", "sinks", "flows", "dropped", "cutoff"} and len(d["sankey"]["dropped"]) == 4
    assert client.get("/data/sankey.json?day=2026-09-01").get_json() == {"first": None, "last": None, "sankey": None}


def test_sankey_leaves_out_flows_under_half_a_percent():
    totals = {
        "solar": 30,
        "battery_discharged": 2,
        "grid_imported": 0.1,  # 32.1 kWh in: the cut-off is 0.1605
        "battery_charged": 5,
        "load": 15,
        "grid_exported": 10,
    }
    flows = {
        ("Solar", "Load"): 13,
        ("Solar", "Battery"): 5,
        ("Solar", "Grid"): 10,
        ("Battery", "Load"): 1.9,
        ("Battery", "Grid"): 0.1,
        ("Grid", "Load"): 0.1,
        ("Grid", "Battery"): 0,
    }
    data = charts.sankey(charts.DayEnergy(totals, flows, T0, T0))
    assert data["cutoff"] == pytest.approx(0.1605)
    assert data["dropped"] == [
        {"from": "Battery", "to": "Grid", "kwh": 0.1},
        {"from": "Grid", "to": "Load", "kwh": 0.1},
    ]
    # Each dropped flow comes off both its ends; the grid source has nothing left to draw: left out
    assert [(n["name"], n["kwh"]) for n in data["sources"]] == [("Solar", 30), ("Battery", 1.9)]
    assert [(n["name"], n["kwh"]) for n in data["sinks"]] == [("Battery", 5), ("Load", 14.9), ("Grid", 9.9)]
    assert {"from": 1, "to": 1, "kwh": 1.9} in data["flows"] and len(data["flows"]) == 4  # re-indexed: battery → load
    sinkless = charts.sankey(charts.DayEnergy(totals, {("Solar", "Load"): 20, ("Solar", "Grid"): 0.1}, T0, T0))
    assert [n["name"] for n in sinkless["sinks"]] == ["Load"]  # a sink whose flows all went: left out too


def test_daily_page(a_day):
    client = app_for(a_day)
    html = client.get("/daily?day=2026-10-02").get_data(as_text=True)
    assert "Daily totals: Friday 2026-10-02" in html and "30.00 kWh" in html and "Exported to the grid" in html
    assert "/static/my_sankey.js" in html and '"flows": [' in html and "http-equiv" not in html  # a past day
    assert "Not drawn: 4 flows under 0.5 %" in html  # this day's flows are all tiny
    script = client.get("/static/my_sankey.js")
    assert script.status_code == 200 and b"MySankey" in script.data and "javascript" in script.mimetype
    flows = re.findall(r"<tr><td>(\w+)</td><td>(\w+)</td><td class=\"kwh\">([\d.]+)</td></tr>", html)
    assert ("Solar", "Grid", "0.033") in flows and ("Battery", "Load", "0.017") in flows and len(flows) == 7
    assert "29.917" in html  # solar's counter (30) minus its bands (5 kW for 1 minute)
    assert "No readings that day." in client.get("/daily?day=2026-09-01").get_data(as_text=True)
    assert "Bad day" in client.get("/daily?day=yesterday").get_data(as_text=True)
    assert 'href="/daily?day=2026-10-01"' in client.get("/?day=2026-10-01").get_data(as_text=True)  # linked


def app_for(config_path):
    """The dashboard for the seeded test schema. The URL is read here, never passed as a test argument: pytest
    prints a failing test's arguments, and the URL holds the test database's password."""
    url = json.loads(config_path.read_text(encoding="utf-8"))["database"]["url"]
    return dashboard.create_app(Config(database_url=url, origin="test")).test_client()


def page_env(client, path):
    """The settings a page hands static/live_charts.js (LiveCharts.start)."""
    page = client.get(path).get_data(as_text=True)
    return json.loads(re.search(r"LiveCharts\.start\((.*)\);", page).group(1))


def test_dashboard_css_is_served():
    client = dashboard.create_app(
        Config(database_url="postgresql://u:x@127.0.0.1:1/x", origin="t"), launcher=None
    ).test_client()
    css = client.get("/static/dashboard.css")
    assert css.status_code == 200 and "css" in css.mimetype
    body = css.get_data(as_text=True)
    assert "position: sticky" in body  # the window nav follows you down
    assert ".u-legend .u-series:first-child" in body  # the legend's "--" hidden on phones


def test_the_page(hangs_up):
    client = dashboard.create_app(
        Config(database_url=f"postgresql://u:x@127.0.0.1:{hangs_up}/x", origin="t"), launcher=None
    ).test_client()
    html = client.get("/?day=2026-10-01").get_data(as_text=True)  # no database needed: the data comes by fetch
    assert "Thursday 2026-10-01" in html and "http-equiv" not in html  # the page never reloads itself
    assert all(
        f'id="plot-{name}"' in html
        and f'id="note-{name}" hidden' in html  # notes behind each title's ⓘ
        and f'aria-controls="note-{name}"' in html
        for name in dashboard.CHARTS
    )
    assert "Battery temperatures (left axis)" in html and f"{dashboard.UPLOT}.iife.min.js" in html
    assert f"{dashboard.UPLOT}.min.css" in html and "/static/live_charts.js" in html
    assert 'href="/?day=2026-09-30"' in html and 'href="/?day=2026-10-02"' in html  # the days either side
    assert '<option value="day" selected>Custom day…</option>' in html and 'value="2026-10-01"' in html
    assert re.search(r'<span id="day-picker"\s*>', html)  # a custom day: its picker shown
    today_html = client.get("/").get_data(as_text=True)
    assert '<option value="/" selected>Today</option>' in today_html and re.search(
        r'id="day-picker"\s+hidden', today_html
    )
    assert '<option value="/?hours=168" >Last 7 days</option>' in today_html
    odd = client.get("/?hours=4").get_data(as_text=True)  # not in the menu: an entry of its own
    assert '<option value="" selected>last 4 h</option>' in odd
    assert 'href="/daily?day=2026-10-01"' in html
    past = page_env(client, "/?day=2026-10-01")
    assert past["data"]["string_voltage"] == "/data/string_voltage.json?day=2026-10-01"
    assert set(past["data"]) == set(dashboard.CHARTS) | {"string_power_strips", "sankey"}  # and the strips, Sankey
    assert past["data"]["sankey"] == "/data/sankey.json?day=2026-10-01" and "/static/my_sankey.js" in html
    assert html.index('id="sankey"') < html.index('id="plot-string_voltage"')  # at the top
    assert "/static/dashboard.css" in html and '<span class="mouse-only">Drag across' in html  # the hint: not on phones
    assert (past["live"], past["keep"], past["today"]) == (False, None, False)  # a past day: never topped up
    six = page_env(client, "/?hours=6")
    assert (six["live"], six["keep"], six["today"]) == (True, 21600.0, False)  # a sliding window
    ten = page_env(client, "/?minutes=10")
    assert (ten["keep"], ten["data"]["load_snapshots"]) == (600.0, "/data/load_snapshots.json?minutes=10")
    assert '<option value="/?minutes=10" selected>Last 10 min</option>' in client.get("/?minutes=10").get_data(
        as_text=True
    )
    today = page_env(client, "/")
    assert today["today"] is True and today["every"] == dashboard.LIVE_EVERY
    assert "Bad window" in client.get("/?day=yesterday").get_data(as_text=True)
    old = client.get("/uplot_dashboard?hours=6")  # where it was while it was being built
    assert old.status_code == 302 and old.headers["Location"].endswith("/?hours=6")
    for gone in ("/chart/string_power.png", "/uplot", "/data/other.json"):
        assert client.get(gone).status_code == 404, gone


class FakeProcess:
    def __init__(self):
        self.running = True

    def poll(self):
        return None if self.running else 0


def test_the_page_starts_a_satellite_fetch(seeded, tmp_path):
    url = json.loads(seeded.read_text(encoding="utf-8"))["database"]["url"]
    started = []

    def launcher(command, log):
        started.append((command, log, FakeProcess()))
        return started[-1][2]

    config = Config(
        database_url=url,
        site=Site(51.0443, -114.0631, "America/Edmonton"),
        path=tmp_path / "config.json",
        satellite_cube_dir=tmp_path / "cubes",
        origin="test",
    )
    client = dashboard.create_app(config, launcher=launcher).test_client()
    assert client.get("/?day=2026-10-01").status_code == 200
    ((command, log, process),) = started
    assert command[1:4] == ["-m", "sigen", "satellite"]
    assert command[4:] == [
        "--start",
        "2026-10-01 00:00",
        "--end",
        "2026-10-02 00:00",
        "--config",
        str(tmp_path / "config.json"),
        "--cube-dir",
        str(tmp_path / "cubes"),
        "--record-to-db",
    ]
    assert log == tmp_path / "cubes" / dashboard.SATELLITE_LOG
    client.get("/?day=2026-09-30")
    assert len(started) == 1  # ours is still running: none started
    process.running = False
    client.get("/?day=2026-09-30")
    assert len(started) == 2 and started[1][0][5] == "2026-09-30 00:00"
    started[1][2].running = False
    client.get("/?day=2026-09-30")
    assert len(started) == 2  # that window had one in the last 10 minutes
    client.get("/?hours=744")
    assert len(started) == 2  # longer than MAX_FETCH: never from the page
    no_site = dashboard.create_app(Config(database_url=url, origin="t"), launcher=launcher).test_client()
    no_site.get("/?day=2026-09-29")
    assert len(started) == 2  # no site: nothing to fetch


def test_the_night_sky_and_the_strips_satellite_fetches(seeded, tmp_path):
    url = json.loads(seeded.read_text(encoding="utf-8"))["database"]["url"]
    started = []

    def launcher(command, log):
        started.append(FakeProcess())
        return started[-1]

    config = Config(
        database_url=url,
        site=Site(51.0443, -114.0631, "America/Edmonton"),
        path=tmp_path / "config.json",
        satellite_cube_dir=tmp_path / "cubes",
        origin="test",
    )
    client = dashboard.create_app(config, launcher=launcher).test_client()
    assert page_env(client, "/?day=2026-10-01")["sunrise"] is None  # not "today": no night sky
    assert len(started) == 1  # opening the page
    rise, _ = goes.sun_rise_set(datetime.datetime.now(TZ).date(), 51.0443, -114.0631)
    assert page_env(client, "/")["sunrise"] == rise.timestamp()  # today's, for the string charts' night sky
    tomorrow, _ = goes.sun_rise_set(datetime.datetime.now(TZ).date() + datetime.timedelta(days=1), 51.0443, -114.0631)
    assert page_env(client, "/night")["sunrise"] == tomorrow.timestamp()  # today before sunrise, whatever the time
    started[-1].running = False
    n = len(started)  # (today's page started one too)
    client.get("/data/string_power_strips.json?day=2026-09-30")
    assert len(started) == n + 1  # the page's strips, fetched again every few minutes
    started[-1].running = False
    client.get("/data/string_power_strips.json?day=2026-09-30")
    assert len(started) == n + 1  # that window had one in the last 10 minutes


def test_the_clear_sky_model_ends_now_or_half_an_hour_after_sunset():
    site = Site(51.0443, -114.0631, "America/Edmonton")
    rise, sunset = goes.sun_rise_set(datetime.date(2026, 10, 11), site.latitude, site.longitude)
    _, yesterday = goes.sun_rise_set(datetime.date(2026, 10, 10), site.latitude, site.longitude)
    half = datetime.timedelta(minutes=30)
    assert dashboard.model_end(NOW, site) == NOW  # 14:30: the sun's up
    late = datetime.datetime(2026, 10, 11, 22, 0, tzinfo=TZ)
    assert dashboard.model_end(late, site) == sunset + half
    small_hours = datetime.datetime(2026, 10, 11, 3, 0, tzinfo=TZ)
    assert dashboard.model_end(small_hours, site) == yesterday + half  # the last sunset was yesterday's
    assert dashboard.model_end(sunset + datetime.timedelta(minutes=10), site) == sunset + datetime.timedelta(minutes=10)
    north = Site(80.0, 0.0, "UTC")  # the midnight sun: no sunset
    assert dashboard.model_end(datetime.datetime(2026, 6, 21, 23, 0, tzinfo=datetime.timezone.utc), north).hour == 23


def test_satellite_command_rounds_the_end_up():
    config = Config(site=Site(51.0, -114.0, "America/Edmonton"), path=None, origin="t")
    start, end = NOW - datetime.timedelta(hours=6), NOW + datetime.timedelta(seconds=1)
    command = dashboard.satellite_command(config, start, end, TZ)
    assert command[4:8] == ["--start", "2026-10-11 08:30", "--end", "2026-10-11 14:31"]


def test_data_when_the_database_is_down(hangs_up):
    client = dashboard.create_app(
        Config(database_url=f"postgresql://u:s3cret@127.0.0.1:{hangs_up}/x", origin="t"), launcher=None
    ).test_client()
    r = client.get("/data/load_energy.json")
    assert r.status_code == 503 and r.get_json()["error"].startswith("database:")  # the page shows it under the chart
    assert "s3cret" not in r.get_data(as_text=True)


def test_string_voltage_as_json(seeded, monkeypatch):
    client = app_for(seeded)
    r = client.get("/data/string_voltage.json?day=2026-10-01")
    d = r.get_json()
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert d["t"] == ["2026-10-01T18:00:05.000000Z", "2026-10-01T18:15:05.000000Z", "2026-10-01T18:30:05.000000Z"]
    assert d["volts"] == {n: [300.0, 301.0, 302.0] for n in ("1", "2", "3", "4")}
    later = T0 + datetime.timedelta(hours=1)
    monkeypatch.setattr(
        dashboard.datetime,
        "datetime",
        type("Frozen", (datetime.datetime,), {"now": classmethod(lambda cls, tz=None: later.astimezone(tz))}),
    )
    d = client.get(f"/data/string_voltage.json?since={d['t'][0]}").get_json()
    assert d["t"] == ["2026-10-01T18:15:05.000000Z", "2026-10-01T18:30:05.000000Z"]  # strictly after
    assert d["volts"]["4"] == [301.0, 302.0]
    assert client.get("/data/string_voltage.json?since=soon").status_code == 400
    assert client.get("/data/nonsense.json").status_code == 404


def test_string_power_and_its_strips_as_json(seeded):
    client = app_for(seeded)  # no site or array: no clear-sky model, no satellite
    d = client.get("/data/string_power.json?day=2026-10-01").get_json()
    assert d["t"] == SEEDED_T and d["sky"] is None and d["faces"] == {}
    assert d["kw"]["1"] == [0.3, 0.602, 0.906] and d["kw"]["4"] == [1.2, 2.408, 3.624]  # V × I: 300 V × 1 A × n …
    d = client.get("/data/string_power_strips.json?day=2026-10-01").get_json()
    (radar,) = d["strips"]  # the radar's only: no site, no satellite strips
    assert radar["label"] == "Rain" and radar["starts"] == [] and radar["length"] == 360.0
    assert len(d["notes"]) == 1 and d["notes"][0].startswith("Rain:")


def test_string_power_with_the_clear_sky_model(seeded, tmp_path, monkeypatch):
    from test_pvmodel import ARRAY, SITE, config_with

    url = json.loads(seeded.read_text(encoding="utf-8"))["database"]["url"]
    c = config_with(tmp_path, site=SITE, array=ARRAY, database={"url": url})  # cubes: none under tmp_path
    client = dashboard.create_app(c, launcher=None).test_client()
    d = client.get("/data/string_power.json?day=2026-10-01").get_json()
    sunset = goes.sun_rise_set(datetime.date(2026, 10, 1), SITE["latitude"], SITE["longitude"])[1]
    stop = sunset + dashboard.MODEL_AFTER_SUNSET  # every minute from midnight to 30 min after sunset
    assert d["sky"]["t"][0] == "2026-10-01T06:00:00.000000Z" and set(d["sky"]["kw"]) == {"1", "2", "3", "4"}
    assert dashboard.iso_utc(stop - datetime.timedelta(minutes=1)) <= d["sky"]["t"][-1] < dashboard.iso_utc(stop)
    assert max(d["sky"]["kw"]["1"]) > 0 and d["sky"]["kw"]["1"][-1] == 0
    assert d["faces"] == {"1": "south", "2": "east", "3": "west", "4": "north"}
    strips = client.get("/data/string_power_strips.json?day=2026-10-01").get_json()["strips"]
    assert strips[0]["label"] == "PV / clear-sky" and strips[1]["label"] == "Rain"  # the PV strip is nearest the chart
    assert [s["label"] for s in strips][2:] == [label for _, label, _, _ in charts.SATELLITE_STRIPS]
    assert all(colour is None for s in strips[2:] for colour in s["colours"])  # no cubes: every scan hatched
    frozen_an_hour_later(monkeypatch)  # a top-up: since is UTC, "now" the site's time
    r = client.get(f"/data/string_power.json?since={SEEDED_T[-1]}")
    assert r.status_code == 200 and r.get_json()["t"] == []
    sky = r.get_json()["sky"]["t"]  # the last SKY_MARGIN of the model again, to now
    assert (sky[0], sky[-1], len(sky)) == ("2026-10-01T17:31:00.000000Z", "2026-10-01T19:00:00.000000Z", 90)


def frozen_an_hour_later(monkeypatch):
    """ "Now", for ?since= (which must be within MAX_HOURS of it): an hour after the seeded readings."""
    later = T0 + datetime.timedelta(hours=1)
    monkeypatch.setattr(
        dashboard.datetime,
        "datetime",
        type("Frozen", (datetime.datetime,), {"now": classmethod(lambda cls, tz=None: later.astimezone(tz))}),
    )


SEEDED_T = ["2026-10-01T18:00:05.000000Z", "2026-10-01T18:15:05.000000Z", "2026-10-01T18:30:05.000000Z"]


def test_load_energy_as_json(seeded):
    client = app_for(seeded)
    d = client.get("/data/load_energy.json?day=2026-10-01").get_json()
    assert d == {
        "first": SEEDED_T[0],
        "last": SEEDED_T[-1],
        "house": 1.5,
        "bars": {"slot 1": 0.72, "slot 7": 0.1, "Other": 0.68},
    }  # as test_energy_by_load
    assert client.get("/data/load_energy.json?day=2026-09-30").get_json()["bars"] == {}


def test_power_by_load_now(seeded, conn):
    p = charts.latest_load_power(conn, NAMES)  # 12:30, the newest good reading (12:40's failed)
    assert p.at == T0 + datetime.timedelta(minutes=30) and p.house == pytest.approx(0.9)
    assert p.bars == pytest.approx({"Garage": 0.0, "Basement Plugs": 0.3, "Other": 0.6})  # named: shown at 0 too
    assert charts.latest_load_power(conn, NAMES, end=T0 + datetime.timedelta(minutes=20)).bars["Garage"] == 1.44
    assert charts.latest_load_power(conn, NAMES, end=T0) is None
    d = app_for(seeded).get("/data/load_power_now.json?day=2026-10-01").get_json()
    assert d == {"at": SEEDED_T[-1], "house": 0.9, "bars": {"slot 7": 0.3, "Other": 0.6}}  # unnamed slot 1: off


def test_load_counters_and_snapshots_as_json(seeded, monkeypatch):
    client = app_for(seeded)
    d = client.get("/data/load_counters.json?day=2026-10-01").get_json()
    assert d["t"] == SEEDED_T
    assert d["kwh"] == {"slot 1": [10.0, 10.36, 10.72], "slot 7": [5.0, 5.05, 5.1], charts.OTHER: [85.0, 85.34, 85.68]}
    d = client.get("/data/load_snapshots.json?day=2026-10-01").get_json()
    assert d["t"] == SEEDED_T and list(d["kw"]) == ["slot 1", "slot 7", charts.OTHER]
    assert d["kw"]["slot 1"] == [1.44, 1.44, 0.0] and d["kw"][charts.OTHER] == pytest.approx([0.36, 0.86, 0.6])
    frozen_an_hour_later(monkeypatch)
    d = client.get(f"/data/load_counters.json?since={SEEDED_T[0]}").get_json()
    assert d["t"] == SEEDED_T[1:] and d["kwh"]["slot 1"] == [10.36, 10.72]  # even one new reading
    d = client.get(f"/data/load_counters.json?since={SEEDED_T[-1]}").get_json()
    assert d == {"t": [], "kwh": {charts.OTHER: []}}  # nothing new; the slots that didn't move are left out
    d = client.get(f"/data/load_snapshots.json?since={SEEDED_T[1]}").get_json()
    assert d["t"] == SEEDED_T[2:] and d["kw"]["slot 7"] == [0.3]


def test_battery_charts_as_json(seeded, conn, monkeypatch):
    conn.execute(
        "INSERT INTO open_meteo_weather (fetched_at, resolution_minutes, valid_at, temperature_2m) VALUES "
        "(%s, 15, %s, -3.5)",
        (T0, T0 - datetime.timedelta(seconds=5)),
    )  # 12:00:00
    soc_at = T0 + datetime.timedelta(minutes=30, seconds=1)  # a plant reading a moment after the last poll's
    db.insert_reading(
        conn,
        ip="127.0.0.1",
        unit_id=247,
        poll_id=db.next_poll_id(conn),
        started_at=soc_at,
        ended_at=soc_at,
        values=sorted(
            [stored("plant_ess_soc", 80.0), stored("plant_backup_soc", 20.0), stored("plant_charge_cut_off_soc", 100.0)]
        ),
    )
    client = app_for(seeded)
    d = client.get("/data/battery_temps.json?day=2026-10-01").get_json()
    assert d["t"] == SEEDED_T
    assert d["c"] == {
        "coolest cluster": [8.0, 9.0, 10.0],
        "warmest cluster": [12.0, 13.0, 14.0],
        "average cell": [10.0, 11.0, 12.0],
        "inverter internal": [35.0, 35.0, 35.0],
    }
    assert d["air"] == {"t": ["2026-10-01T18:00:00.000000Z"], "c": [-3.5]}
    d = client.get("/data/battery_power.json?day=2026-10-01").get_json()
    assert d["kw"] == {"battery": [0.5, 1.5, -1.0], "pv": [2.0, 2.0, 2.0]}
    soc = {
        "t": ["2026-10-01T18:30:06.000000Z"],
        "pct": {"SOC": [80.0], "backup reserve": [20.0], "charge cut-off": [100.0]},
    }
    assert d["soc"] == soc  # (the seeded plant readings have no SOC)
    frozen_an_hour_later(monkeypatch)
    d = client.get(f"/data/battery_temps.json?since={SEEDED_T[-1]}").get_json()
    assert d["t"] == [] and d["air"]["c"] == [-3.5]  # the air of the last AIR_MARGIN again: the page dedupes
    d = client.get(f"/data/battery_power.json?since=2026-10-01T18:30:30Z").get_json()
    assert d["t"] == [] and d["soc"] == soc  # the plant's of the last SOC_MARGIN again


def test_the_window_and_since(seeded, conn, monkeypatch):
    """What every data endpoint does with ?day= / ?hours= / ?since= (data_window), through load_snapshots' Other."""
    client = app_for(seeded)
    other = lambda d: d["kw"].get(charts.OTHER, [])  # noqa: E731
    d = client.get("/data/load_snapshots.json?day=2026-10-01").get_json()
    assert other(d) == pytest.approx([0.36, 0.86, 0.6])  # total − all 24 slots
    assert set(d) == {"t", "kw"}  # no timezone: the browser shows its own
    assert client.get("/data/load_snapshots.json?hours=1").get_json()["t"] == []  # the last hour: nothing then
    bad = client.get("/data/load_snapshots.json?hours=x")
    assert bad.status_code == 400 and "bad window" in bad.get_json()["error"]
    # one more reading, with microseconds, as real ones have: 12:45:05.997599 MDT
    odd = T0 + datetime.timedelta(minutes=45, microseconds=997599)
    db.insert_reading(
        conn,
        ip="127.0.0.1",
        unit_id=247,
        poll_id=db.next_poll_id(conn),
        started_at=odd,
        ended_at=odd,
        values=plant_values(11, 5.2, 102, 0.5, 0.3, 1.2),
    )
    first, *rest = client.get("/data/load_snapshots.json?day=2026-10-01").get_json()["t"]
    assert rest[-1] == "2026-10-01T18:45:05.997599Z"
    frozen_an_hour_later(monkeypatch)  # since must be within MAX_HOURS of "now"
    d = client.get(f"/data/load_snapshots.json?since={first}").get_json()
    assert d["t"] == rest and other(d) == pytest.approx([0.86, 0.6, 0.4])  # strictly after: the first isn't again
    # The exact string back: nothing new. Cut to milliseconds (what a JavaScript Date keeps), the last comes again.
    assert client.get(f"/data/load_snapshots.json?since={rest[-1]}").get_json()["t"] == []
    assert client.get("/data/load_snapshots.json?since=2026-10-01T18:45:05.997Z").get_json()["t"] == [rest[-1]]
    assert client.get("/data/load_snapshots.json?since=2026-10-01T12:45:05.997599-06:00").get_json()["t"] == []
    for bad in ("soon", "2026-10-01T18:00:05", "2026-10-01T19:01:00Z", "2026-08-01T00:00:00Z"):
        r = client.get(f"/data/load_snapshots.json?since={bad}")  # not a time; no timezone; the future; too long ago
        assert r.status_code == 400 and "since" in r.get_json()["error"], bad


# ---------- id order is not time order: sigen sync backfills a gap now, so an old-timestamped reading gets the
# newest id. Every reader must order and bound by started_at, never by id. ----------
def _insert_energy(conn, t, counters, pv, battery, grid, load):
    """One plant reading at `t` with the six lifetime energy counters and the four flow powers (like a_day.plant)."""
    names = [
        "plant_accumulated_pv_energy",
        "plant_accumulated_load_consumption",
        "plant_accumulated_battery_charge_energy",
        "plant_accumulated_battery_discharge_energy",
        "plant_accumulated_grid_import_energy",
        "plant_accumulated_grid_export_energy",
    ]
    db.insert_reading(
        conn,
        ip="127.0.0.1",
        unit_id=247,
        poll_id=db.next_poll_id(conn),
        started_at=t,
        ended_at=t,
        values=sorted(
            [stored(n, c) for n, c in zip(names, counters)]
            + [
                stored("plant_photovoltaic_power", pv),
                stored("plant_ess_power", battery),
                stored("plant_grid_sensor_active_power", grid),
                stored("plant_total_load_power", load),
            ]
        ),
    )


def _insert_load(conn, t, house_kw, house_kwh=100.0):
    db.insert_reading(
        conn,
        ip="127.0.0.1",
        unit_id=247,
        poll_id=db.next_poll_id(conn),
        started_at=t,
        ended_at=t,
        values=plant_values(10.0, 5.0, house_kwh, 1.0, 0.2, house_kw),
    )


def test_day_energy_splits_flows_in_time_order_not_id_order(conn):
    """A backfilled reading (newest id, earlier time) must not make day_energy step backwards in time: a backward
    step would be counted with negative hours, giving negative flows (the thin-solar-bar bug)."""
    noon = datetime.datetime(2026, 10, 5, 12, 0, tzinfo=TZ)
    c = (120, 210, 52, 41, 105, 60)
    _insert_energy(conn, noon + datetime.timedelta(seconds=30), c, pv=5, battery=2, grid=-2, load=1)
    _insert_energy(conn, noon + datetime.timedelta(minutes=1), c, pv=5, battery=2, grid=-2, load=1)
    _insert_energy(conn, noon, c, pv=5, battery=2, grid=-2, load=1)  # backfilled: newest id, earliest time
    e = charts.day_energy(conn, datetime.datetime(2026, 10, 5, tzinfo=TZ), datetime.datetime(2026, 10, 6, tzinfo=TZ))
    assert all(v >= 0 for v in e.flows.values()), e.flows
    minute = 1 / 60
    assert e.flows[("Solar", "Load")] == pytest.approx(minute)  # 2 × 30 s of: 1 kW to load,
    assert e.flows[("Solar", "Battery")] == pytest.approx(2 * minute)  # 2 kW to the battery,
    assert e.flows[("Solar", "Grid")] == pytest.approx(2 * minute)  # 2 kW exported


def test_energy_by_load_bounds_the_window_by_time_not_id(conn):
    """The window's first/last readings are the earliest/latest by time, not min/max id."""
    t0 = datetime.datetime(2026, 10, 6, 12, 0, tzinfo=TZ)
    _insert_load(conn, t0, 2.0, house_kwh=100.0)
    _insert_load(conn, t0 + datetime.timedelta(minutes=30), 2.0, house_kwh=101.5)
    _insert_load(conn, t0 + datetime.timedelta(minutes=15), 2.0, house_kwh=100.7)  # backfilled: newest id, middle time
    e = charts.energy_by_load(conn, NAMES)
    assert e.house == 1.5 and e.hours == 0.5  # 101.5 − 100.0 over 12:00→12:30, not 100.7 − 100.0 over 15 min


def test_latest_load_power_is_the_newest_by_time(conn):
    """The live 'current power' tile is the latest reading by time, not by id."""
    newer = datetime.datetime(2026, 10, 7, 12, 30, tzinfo=TZ)
    _insert_load(conn, newer, 3.0)
    _insert_load(conn, datetime.datetime(2026, 10, 7, 12, 0, tzinfo=TZ), 5.0)  # backfilled: newest id, earlier time
    p = charts.latest_load_power(conn, NAMES)
    assert p.at == newer and p.house == 3.0  # 12:30's 3 kW, not the backfilled 12:00's 5 kW


def test_energy_readings_stay_time_ordered_after_backfill(conn):
    """smart_load_energy and picked_readings order by time, so a backfilled row doesn't scramble the chart's x-axis."""
    base = datetime.datetime(2026, 10, 8, 12, 0, tzinfo=TZ)
    _insert_load(conn, base, 2.0, house_kwh=100.0)
    _insert_load(conn, base + datetime.timedelta(minutes=30), 2.0, house_kwh=101.0)
    _insert_load(
        conn, base + datetime.timedelta(minutes=15), 2.0, house_kwh=100.5
    )  # backfilled: newest id, middle time
    assert charts.smart_load_energy(conn, NAMES).times == [
        base,
        base + datetime.timedelta(minutes=15),
        base + datetime.timedelta(minutes=30),
    ]
    assert [t for _, t in charts.picked_readings(conn)] == [
        base,
        base + datetime.timedelta(minutes=15),
        base + datetime.timedelta(minutes=30),
    ]


# ---------- the all-time Sankey: every finished day in daily_energy added up ----------
def _flows(**kw):
    """All seven flow keys (NOT NULL in daily_energy), zero except those named, e.g. solar_load=5.0."""
    from sigen import daily

    names = {
        ("Solar", "Load"): "solar_load",
        ("Solar", "Battery"): "solar_battery",
        ("Solar", "Grid"): "solar_grid",
        ("Battery", "Load"): "battery_load",
        ("Battery", "Grid"): "battery_grid",
        ("Grid", "Load"): "grid_load",
        ("Grid", "Battery"): "grid_battery",
    }
    return {flow: kw.get(names[flow], 0.0) for flow in daily.FLOWS}


def _insert_daily_energy(conn, day, totals, flows, covered=24.0):
    from sigen import daily

    cols = {
        "day": day,
        "timezone": "America/Edmonton",
        "first_reading": T0,
        "last_reading": T0,
        "covered_hours": covered,
        "method": daily.METHOD,
    }
    cols.update({daily.TOTALS[k]: v for k, v in totals.items()})
    cols.update({daily.FLOWS[k]: v for k, v in flows.items()})
    names = list(cols)
    conn.execute(
        f"INSERT INTO daily_energy ({', '.join(names)}) VALUES ({', '.join('%(' + n + ')s' for n in names)})", cols
    )


def test_all_time_energy_sums_the_daily_energy_table(conn):
    from sigen import daily

    _insert_daily_energy(
        conn,
        datetime.date(2026, 10, 1),
        {"solar": 10.0, "load": 7.0, "grid_imported": 2.0},
        _flows(solar_load=5.0, grid_load=2.0),
        covered=20.0,
    )
    _insert_daily_energy(
        conn,
        datetime.date(2026, 10, 2),
        {"solar": 12.0, "load": 8.0, "grid_imported": 1.0},
        _flows(solar_load=7.0, solar_battery=3.0, grid_load=1.0),
        covered=22.0,
    )
    e = daily.all_time_energy(conn)
    assert e.totals["solar"] == 22.0 and e.totals["load"] == 15.0 and e.totals["grid_imported"] == 3.0
    assert e.totals["grid_exported"] == 0.0  # never set: summed NULL -> 0.0
    assert (
        e.flows[("Solar", "Load")] == 12.0 and e.flows[("Solar", "Battery")] == 3.0 and e.flows[("Grid", "Load")] == 3.0
    )
    assert e.covered_hours == 42.0
    assert [n["name"] for n in charts.sankey(e)["sources"]] == ["Solar", "Grid"]  # a drawable Sankey


def test_all_time_energy_is_none_without_finished_days(conn):
    from sigen import daily

    assert daily.all_time_energy(conn) is None


def test_all_time_page(config, conn):
    _insert_daily_energy(
        conn,
        datetime.date(2026, 10, 1),
        {"solar": 10.0, "load": 7.0, "grid_imported": 2.0},
        _flows(solar_load=5.0, grid_load=2.0),
    )
    _insert_daily_energy(
        conn,
        datetime.date(2026, 10, 2),
        {"solar": 12.0, "load": 8.0, "grid_imported": 1.0},
        _flows(solar_load=6.0, solar_battery=4.0, grid_load=2.0),
    )
    html = app_for(config).get("/all-time").get_data(as_text=True)
    assert "Where all your energy has gone" in html and "/static/my_sankey.js" in html
    assert '"name": "Solar"' in html and '"flows": [' in html and '"flows": []' not in html  # a real Sankey
    assert ">All-time<" in html and 'class="on"' in html  # the nav, this page on
    assert "/static/strips.js" in html and "Strips.layout" in html  # the solar line, with strips.js's renderer


def test_all_time_page_without_days(config):
    assert "No finished days yet" in app_for(config).get("/all-time").get_data(as_text=True)


def test_pv_history(conn):
    _insert_daily_energy(conn, datetime.date(2026, 10, 6), {"solar": 12.0, "load": 8.0}, _flows())
    _insert_daily_energy(conn, datetime.date(2026, 10, 7), {"solar": 30.0, "load": 9.0}, _flows())
    _insert_daily_energy(conn, datetime.date(2026, 10, 8), {"solar": 18.0, "load": 7.0}, _flows())
    line = charts.pv_history(conn, TZ)
    assert line.kwh == [12.0, 30.0, 18.0] and len(line.t) == 3
    assert line.peak_kwh == 30.0 and line.peak_day == "Oct 7"
    assert line.t[0] == int(datetime.datetime(2026, 10, 6, 12, tzinfo=TZ).timestamp())  # stamped at local noon
    assert (line.x_min, line.x_max) == (
        int(datetime.datetime(2026, 10, 6, tzinfo=TZ).timestamp()),
        int(datetime.datetime(2026, 10, 9, tzinfo=TZ).timestamp()),
    )  # whole days
    conn.execute("DELETE FROM daily_energy WHERE day > %s", (datetime.date(2026, 10, 6),))
    assert charts.pv_history(conn, TZ) is None  # fewer than two days


def test_all_time_strips(conn):
    for day, solar in [(datetime.date(2026, 10, 6), 10.0), (datetime.date(2026, 10, 7), 40.0)]:
        _insert_daily_energy(conn, day, {"solar": solar, "load": 8.0}, _flows())
    conn.execute("UPDATE daily_energy SET clear_sky_kwh = 50")
    strips, notes = charts.all_time_strips(conn, TZ)
    assert [s.label for s in strips] == ["PV / clear-sky"]  # only PV has data (no radar / satellite inserted)
    pv = strips[0]
    assert pv.starts == [datetime.datetime(2026, 10, 6, tzinfo=TZ), datetime.datetime(2026, 10, 7, tzinfo=TZ)]
    assert pv.length == datetime.timedelta(days=1)  # a bar per local day, midnight to midnight
    assert pv.colours == [charts._ratio_colour(0.2), charts._ratio_colour(0.8)]  # 10/50 and 40/50
    assert notes[-1] == "hatched = no data that day"
    conn.execute("DELETE FROM daily_energy WHERE day > %s", (datetime.date(2026, 10, 6),))
    assert charts.all_time_strips(conn, TZ) == ([], [])  # fewer than two days


def test_measured_strip():
    """PV / clear-sky per 15-min block: measured half the model (grey), the model too low (pale blue), the model but
    no reading (hatched), 1.5× (white→orange), and 3× (clamped to full orange)."""
    import pandas as pd

    base = datetime.datetime(2026, 10, 5, 12, 0, tzinfo=datetime.timezone.utc)
    m = lambda mins: base + datetime.timedelta(minutes=mins)
    idx = pd.date_range(base, periods=75, freq="1min", tz="UTC")  # five 15-min blocks
    cols = list(charts.STRINGS)
    totals = [0.5 if idx[15] <= t < idx[30] else 4.0 for t in idx]  # block 2 under the 1 kW low-sun floor
    clear = pd.DataFrame({n: [tot / len(cols) for tot in totals] for n in cols}, index=idx)
    first = cols[0]
    # two readings in blocks 1, 4 and 5 (none in 2 and 3); kW on string 1 = 0.4 × amps -> 2, 6 and 12 kW (model 4)
    times = [m(5), m(10), m(50), m(55), m(65), m(70)]
    amps = [5.0, 5.0, 15.0, 15.0, 30.0, 30.0]
    sd = charts.StringData(
        times=times,
        volts={n: ([400.0] * 6 if n == first else [0.0] * 6) for n in cols},
        amps={n: (amps if n == first else [0.0] * 6) for n in cols},
    )
    strip, note = charts.measured_strip(sd, clear)
    assert strip.label == "PV / clear-sky" and strip.length == charts.STRIP_BLOCK
    assert strip.starts == [m(0), m(15), m(30), m(45), m(60)]
    assert strip.colours == ["#808080", charts.LOW_SUN_COLOUR, None, "#ffc680", charts.STRIP_OVER_COLOUR]
    assert "orange = twice it" in note and "per 15 min" in note


def test_satellite_shade_round_trip(conn):
    """A ShadeSeries stored and read back: dedup (one good row per scan), NaN <-> NULL, an error row isn't a scan,
    and the result drives satellite_strips."""
    nan = float("nan")
    t0 = datetime.datetime(2026, 10, 9, 20, 0, tzinfo=datetime.timezone.utc)
    t1 = t0 + datetime.timedelta(minutes=10)
    series = goes_shade.ShadeSeries(
        times=[t0, t1],
        sun_elevation=[30.0, 28.0],
        sun_azimuth=[nan, nan],
        columns={c: [0.5, nan] for c in db.SATELLITE_SHADE_VALUES},
    )
    assert db.insert_satellite_shade(conn, t1, series) == 2
    assert db.insert_satellite_shade(conn, t1, series) == 0  # one good row per scan: dedup
    assert db.insert_satellite_shade(conn, t1, None, error="NOAA down") == 0  # a failed run: an error row, not a scan
    back = charts.satellite_shade_series(conn)
    assert back.times == [t0, t1] and back.sun_elevation == [30.0, 28.0]
    assert back.column("trans_top")[0] == 0.5 and math.isnan(back.column("trans_top")[1])  # NULL -> NaN
    assert [s.label for s in charts.satellite_strips(back)[0]] == [label for _, label, _, _ in charts.SATELLITE_STRIPS]


def test_cli_starts_the_server(run, seeded, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "flask.Flask.run",
        lambda self, **kw: calls.append({**kw, "templates_auto_reload": self.config["TEMPLATES_AUTO_RELOAD"]}),
    )
    code, _, err = run("dashboard", "--config", seeded, "--port", 8123, "--reload")
    assert code == ExitCode.OK and "http://127.0.0.1:8123/" in err
    assert calls == [
        {
            "host": "0.0.0.0",
            "port": 8123,
            "debug": False,
            "use_debugger": False,
            "use_reloader": True,
            "threaded": True,
            "templates_auto_reload": True,
        }
    ]  # a changed template shows without a restart
    run("dashboard", "--config", seeded, "--port", 8123)
    assert calls[1]["use_reloader"] is False and calls[1]["templates_auto_reload"] is False
    password = psycopg.conninfo.conninfo_to_dict(json.loads(seeded.read_text())["database"]["url"]).get("password")
    assert not password or password not in err
