"""sigen.battery_watch: each detector on synthetic nights (15-second readings, temperatures in 0.1 °C steps like the
real sensors), with what it must catch and the look-alikes it must ignore."""

import datetime
import math
from zoneinfo import ZoneInfo

import pytest

from sigen import battery_watch as bw
from sigen import db

TZ = ZoneInfo("America/Edmonton")
T0 = datetime.datetime(2026, 11, 20, 20, 0, tzinfo=TZ)
STEP = datetime.timedelta(seconds=15)
PER_HOUR = 240


def window(n, **columns):
    """n readings 15 s apart from T0: no sun, battery idle, the inverter at its 26 W standby, unless a column is
    given. A column is a list, a function of the reading number, or a constant."""
    cols = {bw.PV: 0.0, bw.BATTERY: 0.0, bw.INVERTER_AC: -bw.STANDBY_KW}
    cols.update({bw.__dict__[k] if k.isupper() else k: v for k, v in columns.items()})
    out = {}
    for name, v in cols.items():
        out[name] = list(v) if isinstance(v, (list, tuple)) else [v(k) for k in range(n)] if callable(v) else [v] * n
    return bw.Window([T0 + STEP * k for k in range(n)], out)


def q(x):
    """The sensors' 0.1 °C steps."""
    return round(x, 1)


def cooling(start=12.0, surroundings=lambda h: 5 - h / 2, tau=13.0):
    """Cells cooling toward an unheated space that follows the falling outside air (time constant tau hours)."""
    temps, t = [], start
    for k in range(12 * PER_HOUR):
        temps.append(q(t))
        t += (surroundings(k / PER_HOUR) - t) / (tau * PER_HOUR)
    return temps


def kinds(events):
    return [e.kind for e in events]


# ---------- natural cooling: nothing to report ----------
def test_natural_cooling_is_not_a_heater():
    temps = cooling()
    w = window(len(temps), COOLEST=temps, AIR=lambda k: -k / PER_HOUR / 2, DELIVERABLE=12.34)
    for detect in (bw.detect_plateau, bw.detect_warming, bw.detect_unexplained_power, bw.detect_idle_drain):
        assert detect(w) == [], detect.__name__
    assert set(kinds(bw.detect_all(w))) <= {"cooling", "cold"}


# ---------- plateau ----------
def held(at=5.0, hours=3.0, before=1.0):
    """Cooling to `at`, then held there, flickering between two sensor steps."""
    falling = [q(at + 1 - k / (before * PER_HOUR)) for k in range(int(before * PER_HOUR))]
    return falling + [at if k % 7 else q(at + 0.1) for k in range(int(hours * PER_HOUR))]


def test_thermostat_plateau():
    temps = held()
    events = bw.detect_plateau(window(len(temps), COOLEST=temps, AIR=-10.0))
    assert kinds(events) == ["plateau"]
    e = events[0]
    assert (e.details["low_c"], e.details["high_c"]) == (5.0, 5.1) and e.details["hours"] == pytest.approx(
        3.0, abs=0.15
    )
    assert "outside air -10.0 → -10.0 °C" in e.summary


def test_resting_at_the_surroundings_temperature_is_not_a_plateau():
    temps = held()
    assert bw.detect_plateau(window(len(temps), COOLEST=temps, AIR=0.0)) == []  # 5 °C above steady air
    falling_air = window(len(temps), COOLEST=temps, AIR=lambda k: -k / PER_HOUR)  # air falls ~4 °C meanwhile
    assert kinds(bw.detect_plateau(falling_air)) == ["plateau"]


def test_plateau_without_air_is_reported_and_needs_quiet():
    temps = held()
    events = bw.detect_plateau(window(len(temps), COOLEST=temps))
    assert kinds(events) == ["plateau"] and "outside air unknown" in events[0].summary
    sunny = window(len(temps), COOLEST=temps, AIR=-10.0, PV=1.5)
    assert bw.detect_plateau(sunny) == []
    short = held(hours=1.5)
    assert bw.detect_plateau(window(len(short), COOLEST=short, AIR=-10.0)) == []


def test_a_polling_gap_splits_a_plateau():
    temps = held(hours=3.0)
    times = [
        T0 + STEP * k + (datetime.timedelta(hours=1) if k >= 2.5 * PER_HOUR else datetime.timedelta(0))
        for k in range(len(temps))
    ]
    w = bw.Window(
        times,
        {
            bw.COOLEST: temps,
            bw.AIR: [-10.0] * len(temps),
            bw.PV: [0.0] * len(temps),
            bw.BATTERY: [0.0] * len(temps),
            bw.INVERTER_AC: [-0.026] * len(temps),
        },
    )
    assert bw.detect_plateau(w) == []  # ~1.5 h + 1.5 h, not 3
    halves = bw.detect_plateau(w, min_duration=datetime.timedelta(hours=1))
    assert len(halves) == 2 and all(e.details["hours"] < 1.7 for e in halves)


# ---------- warming / sawtooth ----------
def heated(start=3.0, on_below=2.0, burst_min=5, rise=0.6, hours=8, fall_per_h=0.3):
    """Cooling at fall_per_h; a heater adds `rise` over `burst_min` minutes whenever the cells reach `on_below`."""
    temps, t, burst = [], start, 0
    for _ in range(hours * PER_HOUR):
        if burst == 0 and t <= on_below:
            burst = burst_min * 4
        if burst:
            t += rise / (burst_min * 4)
            burst -= 1
        t -= fall_per_h / PER_HOUR
        temps.append(q(t))
    return temps


def test_sawtooth():
    temps = heated()
    events = bw.detect_warming(window(len(temps), COOLEST=temps, AIR=-12.0))
    assert kinds(events) == ["sawtooth"]
    d = events[0].details
    assert (
        d["teeth"] >= 3
        and d["mean_rise_c"] == pytest.approx(0.6, abs=0.15)
        and d["low_c"] == pytest.approx(2.0, abs=0.1)
    )
    assert d["period_min"] == pytest.approx(0.6 / 0.3 * 60, rel=0.15)  # it takes 2 h to cool back down


def test_one_burst_is_warming():
    temps = heated(start=2.3, hours=3)  # one burst at ~1 h, then 2 h of cooling: no second one
    events = bw.detect_warming(window(len(temps), COOLEST=temps, AIR=-12.0))
    assert kinds(events) == ["warming"] and events[0].details["max_rise_c"] == pytest.approx(0.6, abs=0.15)
    assert "with no sun and the inverter idle, outside air -12.0 → -12.0 °C" in events[0].summary


def test_warming_explained_by_the_weather_or_the_sun_is_ignored():
    warm_spell = [q(3 + k / PER_HOUR) for k in range(3 * PER_HOUR)]  # +1 °C/h for 3 h
    warm_air = window(len(warm_spell), COOLEST=warm_spell, AIR=lambda k: -10 + 8 * k / PER_HOUR)
    assert bw.detect_warming(warm_air, max_rise_duration=datetime.timedelta(hours=4)) == []
    temps = heated(start=2.3, hours=3)
    sunny = window(len(temps), COOLEST=temps, AIR=-12.0, PV=lambda k: 0.5 if k > PER_HOUR // 2 else 0.0)
    assert bw.detect_warming(sunny) == []
    slow = [q(3 + 0.5 * k / PER_HOUR) for k in range(2 * PER_HOUR)]  # +0.5 °C/h: too slow for a heater
    assert bw.detect_warming(window(len(slow), COOLEST=slow, AIR=-20.0)) == []


def test_sensor_flicker_is_not_warming():
    temps = [5.0 if k % 3 else 5.1 for k in range(2 * PER_HOUR)]
    assert bw.detect_warming(window(len(temps), COOLEST=temps, AIR=-20.0)) == []


# ---------- unexplained power ----------
def test_heater_fed_from_the_grid():
    burst = range(PER_HOUR // 2, PER_HOUR // 2 + 20)  # 5 minutes
    w = window(PER_HOUR, INVERTER_AC=lambda k: -0.326 if k in burst else -0.026)
    events = bw.detect_unexplained_power(w)
    assert kinds(events) == ["unexplained_power"]
    e = events[0]
    assert e.details["mean_kw"] == pytest.approx(0.326) and "from the grid" in e.summary
    assert e.details["excess_kwh"] == pytest.approx(0.3 * 19 / 240, rel=0.01)  # 19 intervals of 15 s


def test_heater_fed_from_the_battery():
    burst = range(100, 140)
    w = window(PER_HOUR, BATTERY=lambda k: -0.3 if k in burst else 0.0)
    events = bw.detect_unexplained_power(w)
    assert kinds(events) == ["unexplained_power"] and "from the battery" in events[0].summary
    assert events[0].details["mean_kw"] == pytest.approx(0.326)


def test_normal_operation_is_explained():
    discharging = window(PER_HOUR, BATTERY=-3.0, INVERTER_AC=2.8)  # 200 W of losses at 3 kW
    small = window(PER_HOUR, BATTERY=-0.17, INVERTER_AC=0.13)  # covering a small load
    charging = window(PER_HOUR, BATTERY=7.3, INVERTER_AC=-7.6)  # from the grid, up to the reserve
    solar = window(PER_HOUR, PV=5.0, BATTERY=2.0, INVERTER_AC=2.8)
    for w in (discharging, small, charging, solar):
        assert bw.detect_unexplained_power(w) == []
    blip = window(PER_HOUR, INVERTER_AC=lambda k: -0.128 if k in (50, 51) else -0.026)  # 2 readings: 15 s
    assert bw.detect_unexplained_power(blip) == []


# ---------- idle drain ----------
def test_idle_drain():
    draining = lambda k: round(10.0 - 0.1 * k / PER_HOUR, 2)  # noqa: E731
    w = window(PER_HOUR + 1, DELIVERABLE=draining, COOLEST=2.0, SOC=83.0)
    events = bw.detect_idle_drain(w)
    assert kinds(events) == ["idle_drain"]
    assert events[0].details["kwh"] == pytest.approx(0.1) and events[0].details["mean_w"] == pytest.approx(100)
    assert bw.detect_idle_drain(window(PER_HOUR, DELIVERABLE=10.0, SOC=83.0)) == []
    discharging = window(PER_HOUR + 1, DELIVERABLE=lambda k: 10.0 - 0.5 * k / PER_HOUR, BATTERY=-0.5, SOC=83.0)
    assert bw.detect_idle_drain(discharging) == []
    just_full = window(PER_HOUR + 1, DELIVERABLE=draining, SOC=99.0)  # the BMS's estimate settling
    assert bw.detect_idle_drain(just_full) == []


# ---------- cold ----------
def test_cold_and_cooling():
    assert kinds(bw.detect_cold(window(10, COOLEST=4.9))) == ["cold"]
    falling = window(PER_HOUR, COOLEST=lambda k: q(7.0 - 0.5 * k / PER_HOUR))  # 6.5 now, −0.5 °C/h
    events = bw.detect_cold(falling)
    assert kinds(events) == ["cooling"]
    assert events[0].details["hours_to_threshold"] == pytest.approx(3.0, abs=0.2)
    assert bw.detect_cold(falling, horizon=datetime.timedelta(hours=2)) == []
    assert bw.detect_cold(window(PER_HOUR, COOLEST=lambda k: q(7.0 + k / PER_HOUR))) == []
    assert bw.detect_cold(window(10, COOLEST=None)) == []


# ---------- charging, reserve, grid, alarms ----------
def test_charge_limited():
    w = window(20, MAX_CHARGE=1.0, RATED_CHARGE=7.3, SOC=50.0, COOLEST=1.5)
    events = bw.detect_charge_limited(w)
    assert kinds(events) == ["charge_limited"] and "1.00 kW of 7.3 kW" in events[0].summary
    assert bw.detect_charge_limited(window(20, MAX_CHARGE=3.1, RATED_CHARGE=7.3, SOC=95.0)) == []  # tapering
    assert bw.detect_charge_limited(window(20, MAX_CHARGE=7.3, RATED_CHARGE=7.3, SOC=50.0)) == []
    assert kinds(bw.detect_charge_limited(window(20, MAX_CHARGE=1.0, SOC=50.0), rated_kw=7.3)) == ["charge_limited"]


PCS1, PCS2, ESS = "plant_merged_alarm_1", "plant_merged_alarm_2", "plant_merged_alarm_3"


def test_below_reserve():
    assert bw.detect_below_reserve(window(PER_HOUR, SOC=82.5, RESERVE=83.0)) == []
    outage = window(PER_HOUR, SOC=lambda k: 83.0 - 10 * k / PER_HOUR, RESERVE=83.0, GRID_STATUS=1)
    events = bw.detect_below_reserve(outage)
    assert kinds(events) == ["below_reserve"] and "the grid is down" in events[0].summary
    drained = window(PER_HOUR, SOC=lambda k: 83.0 - 10 * k / PER_HOUR, RESERVE=83.0, GRID_STATUS=0)
    assert "says on-grid" in bw.detect_below_reserve(drained)[0].summary


def test_every_alarm_bit_in_plain_words():
    w = window(40, **{PCS2: lambda k: 1 << 5 if 10 <= k < 30 else 0, ESS: lambda k: (1 << 4) | (1 << 6)})
    events = sorted(bw.detect_alarms(w), key=lambda e: e.ident)
    assert [e.ident for e in events] == ["alarm:ess.4", "alarm:ess.6", "alarm:pcs2.5"]
    rsd = events[2]
    assert rsd.title == "Stopped by hand: the rapid-shutdown button, or a manual stop" and rsd.priority is None
    assert rsd.summary.endswith("PCS alarms 2 bit 5: Manual operation protection")  # the protocol's words too
    assert (rsd.start, rsd.end) == (T0 + 10 * STEP, T0 + 29 * STEP)
    assert events[0].title == "The battery is too cold" and events[1].priority == "urgent"  # thermal runaway
    assert bw.detect_alarms(window(40, **{PCS1: 0xFFFF})) == []  # "not available"
    (undefined,) = bw.detect_alarms(window(5, **{PCS2: 1 << 6}))  # a gap in the protocol
    assert undefined.title == "Undefined alarm (PCS alarms 2 bit 6)"


def test_the_grid_down_is_one_alarm_whichever_way_it_shows():
    status = bw.detect_alarms(window(40, GRID_STATUS=1))
    bit = bw.detect_alarms(window(40, **{PCS1: 1 << 9}))
    assert [e.ident for e in status] == [e.ident for e in bit] == [bw.GRID_DOWN_KEY]
    assert status[0].title == bit[0].title == "The grid is down" and status[0].priority == "urgent"
    assert "Off-grid (auto)" in status[0].summary


# ---------- faults: the rapid-shutdown button, as it happened on 2026-10-12 ----------
def rsd_night(fault_from, fault_until=None, n=4 * PER_HOUR, air=-10.0):
    """A quiet night; from reading fault_from the plant reports Fault (PCS alarms 2 bit 5): the deliverable energy
    reads 0 at once, and 20 readings (5 min) later the three temperatures read exactly 25.0. Recovered (or not) at
    fault_until."""

    def faulty(k):
        return fault_from <= k and (fault_until is None or k < fault_until)

    def temp(real):
        return lambda k: 25.0 if faulty(k) and k >= fault_from + 20 else real

    return window(
        n,
        PLANT_STATE=lambda k: 2 if faulty(k) else 1,
        INVERTER_STATE=lambda k: 2 if faulty(k) else 1,
        **{PCS2: lambda k: 1 << 5 if faulty(k) else 0},
        DELIVERABLE=lambda k: 0.0 if faulty(k) else 11.44,
        SOC=73.0,
        RESERVE=73.0,
        COOLEST=temp(14.8),
        WARMEST=temp(21.3),
        AVERAGE_CELL=temp(18.0),
        AIR=air,
    )


def test_during_a_fault_only_the_fault_alerts():
    w = rsd_night(fault_from=PER_HOUR)  # the fault started 3 h ago and is still going on
    assert kinds(bw.detect_idle_drain(w)) == ["idle_drain"]  # what fired on 2026-10-12 …
    assert kinds(bw.detect_plateau(w)) == ["plateau"]  # … and what would have next
    events = bw.detect_all(w)
    assert kinds(events) == ["fault"]
    assert "Stopped by hand: the rapid-shutdown button" in events[0].summary and events[0].start == T0 + PER_HOUR * STEP
    assert len(bw.after_faults(w)) == 0


def test_powering_up_is_hidden_too():
    """As on 2026-10-13: Shutdown with every temperature 0.0 °C, then Standby
    with real temperatures but the deliverable energy still 0, then Running.
    The 0.0 °C sent a "cold" alert; nothing should until a minute into
    Running."""
    state = lambda k: 3 if k < 2 else 0 if k < 24 else 1  # noqa: E731
    temps = lambda k: 0.0 if k < 3 else 5.8  # noqa: E731
    w = window(
        40,
        PLANT_STATE=state,
        INVERTER_STATE=state,
        COOLEST=temps,
        WARMEST=temps,
        AVERAGE_CELL=temps,
        DELIVERABLE=lambda k: 0.0 if k < 24 else 11.55,
        SOC=73.0,
        RESERVE=73.0,
    )
    for n in range(1, 29):  # up to a minute into Running: nothing at all
        part = bw.Window(w.times[:n], {c: v[:n] for c, v in w.columns.items()})
        assert bw.detect_all(part) == [], n
        assert bool(bw.detect_cold(part)) == (n <= 3)  # without the guard: "cold" on the 0.0 °C readings
    assert kinds(bw.detect_all(w)) == []  # 5.8 °C once trusted: above the 5 °C threshold
    assert kinds(bw.detect_all(window(5, PLANT_STATE=1, INVERTER_STATE=1, COOLEST=4.9))) == ["cold"]


def test_after_a_fault_its_readings_stay_hidden():
    w = rsd_night(fault_from=PER_HOUR, fault_until=3 * PER_HOUR)  # 2 h of fault, then an hour of normal
    trusted = bw.after_faults(w)
    assert trusted.times[0] > w.times[3 * PER_HOUR - 1] + bw.AFTER_FAULT - STEP
    assert kinds(bw.detect_all(w)) == ["fault"]  # the fault itself (history now); nothing from its readings
    assert bw.detect_all(window(10)) == [] and bw.after_faults(window(10)).times == window(10).times


# ---------- helpers ----------
def test_runs_and_interpolate():
    times = [T0 + STEP * k for k in range(6)] + [T0 + datetime.timedelta(hours=1)]
    assert bw.runs(times, [True, True, False, True, True, True, True]) == [(0, 1), (3, 5), (6, 6)]
    points = [(T0, 0.0), (T0 + datetime.timedelta(minutes=15), 3.0), (T0 + datetime.timedelta(hours=3), 9.0)]
    at = [
        T0 - STEP,
        T0,
        T0 + datetime.timedelta(minutes=5),
        T0 + datetime.timedelta(minutes=15),
        T0 + datetime.timedelta(hours=1),
        T0 + datetime.timedelta(hours=3, seconds=1),
    ]
    assert bw.interpolate(at, points) == [None, 0.0, pytest.approx(1.0), 3.0, None, None]


def test_window_checks():
    with pytest.raises(ValueError, match="2 values for 3 times"):
        bw.Window([T0, T0 + STEP, T0 + 2 * STEP], {bw.SOC: [1.0, 2.0]})
    with pytest.raises(ValueError, match="ascending"):
        bw.Window([T0, T0], {})
    assert bw.Window([T0], {})[bw.SOC] == [None]


@pytest.mark.parametrize("column", bw.COLUMNS)
def test_columns_are_view_columns(column):
    """Every column constant is a stored register's column in the scaled views (test_db checks the views)."""
    assert column in {db.view_column(r) for r in db.STORED_REGISTERS}


def test_cold_charging():
    w = window(PER_HOUR, COOLEST=1.5, BATTERY=lambda k: 0.8 if 100 <= k < 140 else 0.0)
    events = bw.detect_cold_charging(w)
    assert kinds(events) == ["cold_charging"] and events[0].details["max_kw"] == 0.8
    assert bw.detect_cold_charging(window(PER_HOUR, COOLEST=6.0, BATTERY=2.0)) == []  # warm enough
    assert bw.detect_cold_charging(window(PER_HOUR, COOLEST=1.5, BATTERY=-0.5)) == []  # discharging is fine
    assert bw.detect_cold_charging(window(PER_HOUR, COOLEST=1.5, BATTERY=lambda k: 0.8 if k < 3 else 0)) == []


def test_interpolate_holds_the_last_value():
    points = [(T0, 0.0), (T0 + datetime.timedelta(minutes=15), 3.0)]
    at = [T0 + datetime.timedelta(minutes=20), T0 + datetime.timedelta(minutes=50)]
    assert bw.interpolate(at, points) == [None, None]
    assert bw.interpolate(at, points, hold=datetime.timedelta(minutes=30)) == [3.0, None]


def test_cooling_fixture_sanity():
    temps = cooling()
    assert temps[0] == 12.0 and 2 < temps[-1] < 9 and all(b <= a + 1e-9 for a, b in zip(temps, temps[1:]))
    assert math.isclose(heated()[0], 3.0, abs_tol=0.05)
