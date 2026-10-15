"""Data and figures for the dashboard, straight from the database (usable from your own code too).

Strings: every reading of the inverter's PV strings (voltage, and power = V × I).

Smart loads:
- counters: the first plant reading of each 15-minute window (clock-aligned), the 24 smart-load lifetime
  counters (30098–30144) and the whole house's (30094) at those readings. **Other** = house − all 24 slots.
  Average power per interval = the counter difference ÷ the time between the picked readings. Counters move in
  0.01 kWh steps, so this is only meaningful over ~15 minutes or more.
- energy: last counter minus first over the period, per load, with Other; bars add up to the house total.
- snapshots: every plant reading's instantaneous power per slot (1 W resolution) and Other = total load power
  (30284) − all 24 slots.
Slots are shown if they're named in the config or moved; unnamed ones are "slot N".

Figures use matplotlib's object-oriented API (no pyplot), so they're safe to draw from a web server's threads.
"""
import datetime
import io
import math
import textwrap
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from matplotlib.colors import to_rgb, to_rgba
from matplotlib.dates import AutoDateLocator, DateFormatter, HourLocator, MinuteLocator, date2num
from matplotlib.figure import Figure

from sigen.db import values_table, wide_layout
from sigen.registers import BY_NAME

PLANT, INVERTER = 247, 1
WINDOW = datetime.timedelta(minutes=15)
HOUSE = "plant_accumulated_load_consumption"            # 30094: whole-house lifetime consumption
CHECK = "plant_si_common_load_consumption"              # 30228: same, excluding EV chargers (cross-check)
TOTAL_POWER = "plant_total_load_power"                  # 30284
COUNTERS = [f"plant_smart_load_{n}_total_consumption" for n in range(1, 25)]
POWERS = [f"plant_smart_load_{n}_power" for n in range(1, 25)]
OTHER = "Other (house − smart loads)"
STRINGS = (1, 2, 3, 4)


def _slot(name: str) -> int:
    return int(name.split("_")[3])                      # plant_smart_load_<slot>_…


def slot_label(slot: int, names: Dict[int, str]) -> str:
    return f"{names[slot]} ({slot})" if slot in names else f"slot {slot}"


def _between(start, end, column="started_at"):
    """SQL condition and parameters for an optional [start, end) window."""
    parts, params = [], []
    if start is not None:
        parts.append(f"{column} >= %s")
        params.append(start)
    if end is not None:
        parts.append(f"{column} < %s")
        params.append(end)
    return "".join(f" AND {p}" for p in parts), params


def reading_values(conn, unit: int, names: List[str], start=None, end=None, ids=None):
    """{reading id: (started_at, {register name: value after the gain})} for the successful readings of `unit` in
    the window (or with these ids), for the registers `names`; a register without a value is left out. The one
    place to fetch registers over a time window from.

    Reads the wide table once migration 0004 has switched over, else the long register_values table (until then,
    even if a backfill has already filled the wide tables). For the long table, two steps on purpose: the readings first, then their values by
    (reading, address), the primary key. One join filtered by register name makes the planner guess one register
    and fetch every value those registers ever had (24 h of string data: 16 s instead of well under one)."""
    regs = [BY_NAME[n] for n in names]
    gains = {r.name: float(r.gain) for r in regs}
    if wide_layout(conn):
        from psycopg import sql
        where, params = _between(start, end, "t.started_at")
        if ids is not None:
            where, params = where + " AND t.reading_id = ANY(%s)", [*params, list(ids)]
        query = sql.SQL("SELECT t.reading_id, t.started_at, {} FROM {} t JOIN readings r ON r.id = t.reading_id "
                        "WHERE r.unit_id = %s" + where + " ORDER BY t.reading_id").format(
            sql.SQL(", ").join(sql.Identifier("t", n) for n in names), sql.Identifier(values_table(regs[0].device)))
        return {rid: (t, {n: v / gains[n] for n, v in zip(names, values) if v is not None})
                for rid, t, *values in conn.execute(query, [unit, *params])}
    if ids is not None:
        readings = conn.execute("SELECT id, started_at FROM readings WHERE id = ANY(%s) ORDER BY id",
                                (list(ids),)).fetchall()
    else:
        where, params = _between(start, end)
        readings = conn.execute(f"SELECT id, started_at FROM readings WHERE unit_id = %s AND error IS NULL{where} "
                                "ORDER BY id", [unit, *params]).fetchall()
    by_address = {r.address: r.name for r in regs}
    out = {rid: (t, {}) for rid, t in readings}
    if out:
        # A range of the primary key (reading_id, address) rather than a list of ids: one index range scan.
        # Other units' readings in the range don't have these addresses; failed readings have no values.
        for rid, address, value in conn.execute(
                "SELECT reading_id, address, value FROM register_values "
                "WHERE reading_id BETWEEN %s AND %s AND address = ANY(%s)",
                (readings[0][0], readings[-1][0], list(by_address))):
            if rid in out:
                out[rid][1][by_address[address]] = value / gains[by_address[address]]
    return out


# ---------- strings ----------
@dataclass
class StringData:
    times: List[datetime.datetime] = field(default_factory=list)
    volts: Dict[int, List[float]] = field(default_factory=dict)
    amps: Dict[int, List[float]] = field(default_factory=dict)

    def kw(self, n: int) -> List[float]:
        return [v * a / 1000 for v, a in zip(self.volts[n], self.amps[n])]

    def kwh(self) -> float:
        """Every string's energy over the readings (trapezoids between readings)."""
        total = [sum(kw) for kw in zip(*(self.kw(n) for n in STRINGS))]
        return sum((a + b) / 2 * (t1 - t0).total_seconds() / 3600
                   for a, b, t0, t1 in zip(total, total[1:], self.times, self.times[1:]))


def _hours_per_row(frame) -> float:
    """The spacing of a regular time index, in hours (to turn a sum of kW rows into kWh)."""
    return (frame.index[1] - frame.index[0]).total_seconds() / 3600


def string_readings(conn, start=None, end=None) -> StringData:
    """Every successful inverter reading in the window: voltage and current per string."""
    by_reading = reading_values(conn, INVERTER, [f"inverter_pv{n}_{q}" for n in STRINGS for q in ("voltage", "current")],
                                 start, end)
    data = StringData(volts={n: [] for n in STRINGS}, amps={n: [] for n in STRINGS})
    for t, values in by_reading.values():
        if all(f"inverter_pv{n}_{q}" in values for n in STRINGS for q in ("voltage", "current")):
            data.times.append(t)
            for n in STRINGS:
                data.volts[n].append(values[f"inverter_pv{n}_voltage"])
                data.amps[n].append(values[f"inverter_pv{n}_current"])
    return data


LIVE_VOLTS = 10.0               # a string below this is dark (the panels show voltage well before sunrise)


def live_limits(data: StringData, volts: float = LIVE_VOLTS) -> Optional[Tuple[datetime.datetime, datetime.datetime]]:
    """From the first reading where any string is at `volts` or more to the last one: the window without the dark
    stretches at its ends (nights inside a longer window stay). None if every string stays below it."""
    live = [t for k, t in enumerate(data.times) if any(data.volts[n][k] >= volts for n in STRINGS)]
    return (live[0], live[-1]) if len(live) > 1 else None


def clear_sky_strings(conn, site, array, start: datetime.datetime, end: datetime.datetime):
    """Each string's clear-sky power (kW; a pandas DataFrame, columns = PV input numbers) from `start` to `end`, every
    minute (every 5 minutes for windows over 3 days), with the air temperature and wind from Open-Meteo's
    15-minute rows where the database has them (the model's 12 °C / 2 m/s elsewhere). Needs pvlib. The times are UTC,
    whatever zones `start` and `end` are in (pandas won't make a range between two zones)."""
    import pandas as pd
    from sigen import pvmodel
    start, end = start.astimezone(datetime.timezone.utc), end.astimezone(datetime.timezone.utc)
    step = "1min" if end - start <= datetime.timedelta(days=3) else "5min"
    times = pd.date_range(pd.Timestamp(start).ceil(step), end, freq=step, inclusive="left")
    weather = conn.execute(
        "SELECT DISTINCT ON (valid_at) valid_at, temperature_2m, wind_speed_10m FROM open_meteo_weather "
        "WHERE resolution_minutes = 15 AND error IS NULL AND valid_at >= %s AND valid_at <= %s "
        "ORDER BY valid_at, fetched_at DESC",
        (start - datetime.timedelta(hours=1), end + datetime.timedelta(hours=1))).fetchall()
    temp, wind = pvmodel.TEMP_AIR, pvmodel.WIND
    if weather:
        w = pd.DataFrame(weather, columns=["t", "temp", "wind"]).set_index("t").astype(float)
        w.index = pd.DatetimeIndex(w.index).tz_convert(times.tz)
        bridge = int(datetime.timedelta(hours=2) / pd.Timedelta(step))     # don't draw lines across longer gaps
        hold = int(datetime.timedelta(hours=1) / pd.Timedelta(step))
        both = w.reindex(w.index.union(times)).interpolate(method="time", limit_area="inside", limit=bridge)
        # Hold the newest row for up to an hour (it's up to 15 minutes old: rows are stamped at their period's end),
        # and the oldest back an hour, rather than dropping to the default at the window's live edge (a step).
        both = both.ffill(limit=hold).bfill(limit=hold).reindex(times)
        temp = both["temp"].fillna(pvmodel.TEMP_AIR)
        wind = (both["wind"] / 3.6).fillna(pvmodel.WIND)               # Open-Meteo's km/h, at 10 m
    return pvmodel.Model(site, array).strings_clear_sky(times, temp, wind)


# ---------- smart loads ----------
def window_of(t: datetime.datetime) -> datetime.datetime:
    return t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)


def picked_readings(conn, start=None, end=None) -> List[Tuple[int, datetime.datetime]]:
    """The smallest successful plant reading id of each clock-aligned 15-minute window: [(id, started_at)]."""
    where, params = _between(start, end)
    readings = conn.execute(f"SELECT id, started_at FROM readings WHERE error IS NULL AND unit_id = %s{where} "
                            "ORDER BY id", [PLANT, *params]).fetchall()
    first_in_window = {}
    for reading_id, started_at in readings:
        w = window_of(started_at)
        if w not in first_in_window or reading_id < first_in_window[w][0]:
            first_in_window[w] = (reading_id, started_at)
    return sorted(first_in_window.values())


@dataclass
class LoadCounters:
    times: List[datetime.datetime]
    ids: List[int]
    series: Dict[str, List[float]]          # label -> lifetime kWh at each picked reading; Other last
    idle: List[int]                         # slots that never moved (and aren't named)

    def since_start(self) -> Dict[str, List[float]]:
        """Each series minus its own first value: the energy used since the window's first picked reading (0 there;
        for "today", the kWh used so far today)."""
        return {label: [kwh - values[0] for kwh in values] for label, values in self.series.items()}

    def average_kw(self, label: str) -> List[float]:
        """Average power over each interval between consecutive picked readings (one fewer than times)."""
        kwh, t = self.series[label], self.times
        return [(kwh[k + 1] - kwh[k]) / ((t[k + 1] - t[k]).total_seconds() / 3600) for k in range(len(t) - 1)]


def smart_load_counters(conn, names: Dict[int, str], start=None, end=None) -> Optional[LoadCounters]:
    """The counters at each picked reading, for the slots that are named or moved, plus Other. None if fewer
    than two readings."""
    return _counters_at(conn, names, picked_readings(conn, start, end))


MAX_ENERGY_POINTS = 6000        # a day of 15-second readings; longer windows are thinned to about this many


def smart_load_energy(conn, names: Dict[int, str], start=None, end=None,
                      max_points: int = MAX_ENERGY_POINTS, fewest: int = 2) -> Optional[LoadCounters]:
    """The counters at every successful plant reading in the window (for the dashboard's energy chart), or at every
    k-th one when there are more than `max_points` (always keeping the last). None if fewer than `fewest` readings
    (a page topping up its chart wants even one)."""
    where, params = _between(start, end)
    readings = conn.execute(f"SELECT id, started_at FROM readings WHERE error IS NULL AND unit_id = %s{where} "
                            "ORDER BY id", [PLANT, *params]).fetchall()
    step = -(-len(readings) // max_points) or 1
    picked = readings[::step]
    if readings and picked[-1] != readings[-1]:
        picked.append(readings[-1])
    return _counters_at(conn, names, picked, fewest)


def _counters_at(conn, names: Dict[int, str], picked: List[Tuple[int, datetime.datetime]],
                 fewest: int = 2) -> Optional[LoadCounters]:
    if len(picked) < max(fewest, 1):
        return None
    ids = [i for i, _ in picked]
    counters, house = defaultdict(dict), {}
    for reading_id, (_, values) in reading_values(conn, PLANT, [*COUNTERS, HOUSE], ids=ids).items():
        for name, kwh in values.items():
            if name == HOUSE:
                house[reading_id] = kwh
            else:
                counters[_slot(name)][reading_id] = kwh
    used = [s for s in sorted(counters) if s in names or counters[s][ids[-1]] > counters[s][ids[0]]]
    series = {slot_label(s, names): [counters[s][i] for i in ids] for s in used}
    series[OTHER] = [house[i] - sum(counters[s][i] for s in counters) for i in ids]
    return LoadCounters([t for _, t in picked], ids, series, [s for s in sorted(counters) if s not in used])


@dataclass
class LoadEnergy:
    first: Tuple[int, datetime.datetime]
    last: Tuple[int, datetime.datetime]
    house: float                            # kWh, 30094's difference
    check: float                            # 30228's difference (excludes EV chargers)
    bars: Dict[str, float]                  # label -> kWh, smart loads in slot order, then "Other"

    @property
    def hours(self) -> float:
        return (self.last[1] - self.first[1]).total_seconds() / 3600


def energy_by_load(conn, names: Dict[int, str], start=None, end=None) -> Optional[LoadEnergy]:
    """Latest minus earliest counter over the window's plant readings. None if fewer than two readings."""
    where, params = _between(start, end)
    firsts = conn.execute(f"SELECT min(id), max(id) FROM readings WHERE error IS NULL AND unit_id = %s{where}",
                          [PLANT, *params]).fetchone()
    if firsts[0] is None or firsts[0] == firsts[1]:
        return None
    (first_id, first_t), (last_id, last_t) = conn.execute(
        "SELECT id, started_at FROM readings WHERE id IN (%s, %s) ORDER BY id", firsts).fetchall()
    values = reading_values(conn, PLANT, [*COUNTERS, HOUSE, CHECK], ids=[first_id, last_id])
    start_kwh, end_kwh = values[first_id][1], values[last_id][1]
    delta = {name: round(end_kwh[name] - start_kwh[name], 2) for name in end_kwh}
    house, check = delta.pop(HOUSE), delta.pop(CHECK)
    bars = {}
    for name in sorted(delta, key=_slot):
        slot = _slot(name)
        if slot in names or delta[name] > 0:
            bars[names.get(slot, f"slot {slot}")] = delta[name]
    bars["Other"] = round(house - sum(delta.values()), 2)
    return LoadEnergy((first_id, first_t), (last_id, last_t), house, check, bars)


@dataclass
class LoadSnapshots:
    times: List[datetime.datetime]
    series: Dict[str, List[float]]          # label -> kW at each reading; Other last


def load_power_snapshots(conn, names: Dict[int, str], start=None, end=None) -> LoadSnapshots:
    """Every plant reading's instantaneous power per slot (named or ever above 0), and Other."""
    by_reading = reading_values(conn, PLANT, [*POWERS, TOTAL_POWER], start, end)
    times, slots, total = [], defaultdict(list), []
    for t, values in by_reading.values():
        if TOTAL_POWER not in values:
            continue
        times.append(t)
        total.append(values[TOTAL_POWER])
        for name, kw in values.items():
            if name != TOTAL_POWER:
                slots[_slot(name)].append(kw)
    used = [s for s in sorted(slots) if s in names or max(slots[s], default=0) > 0]
    series = {slot_label(s, names): slots[s] for s in used}
    series[OTHER] = [t - sum(slots[s][k] for s in slots) for k, t in enumerate(total)]
    return LoadSnapshots(times, series)


# ---------- battery ----------
BATTERY_TEMPS = {"inverter_ess_min_cluster_temperature": "coolest cluster",
                 "inverter_ess_max_cluster_temperature": "warmest cluster",
                 "inverter_ess_average_cell_temperature": "average cell"}
INVERTER_TEMP = "inverter_pcs_internal_temperature"
BATTERY_POWER = "inverter_ess_power"                    # >0 charging, <0 discharging
PV_POWER = "inverter_pv_power"
SOC = "plant_ess_soc"
SOC_LIMITS = {"plant_backup_soc": "backup reserve", "plant_charge_cut_off_soc": "charge cut-off"}   # settings, %


@dataclass
class Series:
    times: List[datetime.datetime]
    values: Dict[str, List[float]]                      # register name -> value after the gain, per time


def unit_series(conn, unit: int, names: List[str], start=None, end=None) -> Series:
    """Every reading of Modbus unit `unit` in the window that has all of `names`."""
    data = Series([], {n: [] for n in names})
    for t, values in reading_values(conn, unit, names, start, end).values():
        if all(n in values for n in names):
            data.times.append(t)
            for n in names:
                data.values[n].append(values[n])
    return data


def inverter_series(conn, names: List[str], start=None, end=None) -> Series:
    """Every inverter reading in the window that has all of `names`."""
    return unit_series(conn, INVERTER, names, start, end)


def air_temperatures(conn, start=None, end=None) -> List[Tuple[datetime.datetime, float]]:
    """Open-Meteo's 15-minute temperature_2m in the window (the newest fetch of each period): a model's outside air
    at the site, for context."""
    where, params = _between(start, end, "valid_at")
    return [(t, float(v)) for t, v in conn.execute(
        "SELECT DISTINCT ON (valid_at) valid_at, temperature_2m FROM open_meteo_weather "
        f"WHERE resolution_minutes = 15 AND error IS NULL AND temperature_2m IS NOT NULL{where} "
        "ORDER BY valid_at, fetched_at DESC", params)]


# ---------- precipitation ----------
RADAR_FRAME = datetime.timedelta(minutes=6)      # GeoMet's radar: a frame every 6 minutes
SNOW_CODES = {71, 73, 75, 77, 85, 86}            # WMO weather codes: snow, snow grains, snow showers
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}   # drizzle, rain, freezing, showers, thunder
SNOW_WET_BULB = 1.0          # °C: where Open-Meteo's code says nothing falls, snow at or below this wet-bulb, else rain
PRECIP_LOOKBACK = datetime.timedelta(days=7)     # the darkest colour: the radar's most in the 7 days to the window's end
PRECIP_DEFAULT_TOP = 1.0     # … or this (mm/h or cm/h) after a dry week
PRECIP_EASING_K = 20         # ln(1 + k·f) / ln(1 + k): light echoes show, and the heaviest still stands out
PRECIP_COLOURS = {"rain": "#08306b", "snow": "black"}
PRECIP_UNITS = {"rain": "mm/h", "snow": "cm/h"}
OM_GAP_MARGIN = datetime.timedelta(hours=6)      # how far past the window to look for the rows either side of a gap


def wet_bulb(temp_c: float, rh_pct: float) -> float:
    """Wet-bulb temperature (°C) from air temperature and relative humidity: Stull 2011, within 0.3 °C for RH 5–99 %
    and −20 to 50 °C. Falling snow survives warmer air the drier it is; the wet-bulb captures that in one number."""
    return (temp_c * math.atan(0.151977 * math.sqrt(rh_pct + 8.313659)) + math.atan(temp_c + rh_pct)
            - math.atan(rh_pct - 1.676331) + 0.00391838 * rh_pct ** 1.5 * math.atan(0.023101 * rh_pct) - 4.686035)


def _period_end(t: datetime.datetime) -> datetime.datetime:
    """The end of the 15-minute period (end − 15 min, end] that `t` falls in, in UTC: Open-Meteo stamps a period
    with its end."""
    t = t.astimezone(datetime.timezone.utc)
    start = t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)
    return start if start == t else start + WINDOW


def _lerp(a, b, w):
    return None if a is None or b is None else a + (b - a) * w


def precipitation_kinds(conn, start: datetime.datetime, end: datetime.datetime) -> Dict[datetime.datetime, str]:
    """{period end (UTC): "rain" or "snow"} from Open-Meteo's 15-minute rows (the newest fetch of each): its weather
    code where that says rain or snow, otherwise the wet-bulb temperature from its air temperature and humidity.
    A missing period between two stored ones is filled in: temperature and humidity interpolated linearly in time,
    the code kept if both neighbours agree. A period with neither a code saying rain or snow nor a temperature and
    humidity has no kind."""
    rows = [(t.astimezone(datetime.timezone.utc), code, temp, rh) for t, code, temp, rh in conn.execute(
        "SELECT DISTINCT ON (valid_at) valid_at, weather_code, temperature_2m, relative_humidity_2m "
        "FROM open_meteo_weather WHERE resolution_minutes = 15 AND error IS NULL AND valid_at >= %s AND valid_at <= %s "
        "ORDER BY valid_at, fetched_at DESC", (start - OM_GAP_MARGIN, end + OM_GAP_MARGIN + WINDOW))]
    filled = []
    for (t, code, temp, rh), after in zip(rows, rows[1:] + [None]):
        filled.append((t, code, temp, rh))
        if after is not None:
            t1, code1, temp1, rh1 = after
            steps = round((t1 - t) / WINDOW)
            for s in range(1, steps):
                filled.append((t + s * WINDOW, code if code == code1 else None,
                               _lerp(temp, temp1, s / steps), _lerp(rh, rh1, s / steps)))
    kinds = {}
    for t, code, temp, rh in filled:
        if code in SNOW_CODES or code in RAIN_CODES:
            kinds[t] = "snow" if code in SNOW_CODES else "rain"
        elif temp is not None and rh is not None:
            kinds[t] = "snow" if wet_bulb(temp, rh) <= SNOW_WET_BULB else "rain"
    return kinds


@dataclass
class Precipitation:
    """The radar at the site over a window (GeoMet's two conversions of the same echo), each frame's kind from
    Open-Meteo (precipitation_kinds), and each kind's darkest-colour rate. Drawn as one kind: the one most frames
    have."""
    times: List[datetime.datetime] = field(default_factory=list)
    rain_mm_h: List[float] = field(default_factory=list)
    snow_cm_h: List[float] = field(default_factory=list)
    kinds: List[Optional[str]] = field(default_factory=list)
    top: Dict[str, float] = field(default_factory=lambda: {k: PRECIP_DEFAULT_TOP for k in PRECIP_COLOURS})

    @property
    def kind(self) -> str:
        """"snow" if more frames are snow than rain, else "rain" (also with no frames: a clear summer day)."""
        return "snow" if self.kinds.count("snow") > self.kinds.count("rain") else "rain"

    def shares(self) -> List[float]:
        """Per frame, 0 (white) to 1 (the darkest colour): its rate in the strip's kind ÷ that kind's top, eased
        with ln(1 + k·f) / ln(1 + k), k = PRECIP_EASING_K."""
        values, top = (self.snow_cm_h if self.kind == "snow" else self.rain_mm_h), self.top[self.kind]
        return [math.log1p(PRECIP_EASING_K * min(max(v / top, 0.0), 1.0)) / math.log1p(PRECIP_EASING_K)
                for v in values]


def precipitation(conn, start: datetime.datetime, end: datetime.datetime) -> Precipitation:
    """Every good radar frame in [start, end), each with its Open-Meteo kind; the tops are the radar's most in the 7
    days to `end` (or the whole window, if longer), PRECIP_DEFAULT_TOP if that's 0 or there are no frames."""
    data = Precipitation()
    kinds = precipitation_kinds(conn, start, end)
    for t, rain, snow in conn.execute(
            "SELECT frame_at, rain_mm_h, snow_cm_h FROM radar_frames WHERE error IS NULL AND rain_mm_h IS NOT NULL "
            "AND snow_cm_h IS NOT NULL AND frame_at >= %s AND frame_at < %s ORDER BY frame_at", (start, end)):
        data.times.append(t)
        data.rain_mm_h.append(float(rain))
        data.snow_cm_h.append(float(snow))
        data.kinds.append(kinds.get(_period_end(t)))
    rain_top, snow_top = conn.execute(
        "SELECT max(rain_mm_h), max(snow_cm_h) FROM radar_frames WHERE error IS NULL AND frame_at >= %s "
        "AND frame_at < %s", (min(start, end - PRECIP_LOOKBACK), end)).fetchone()
    data.top = {"rain": float(rain_top or 0) or PRECIP_DEFAULT_TOP, "snow": float(snow_top or 0) or PRECIP_DEFAULT_TOP}
    return data


# ---------- a day's energy ----------
DAY_TOTALS = [("solar", "Solar produced"), ("load", "Used by the house"), ("battery_charged", "Battery charged"),
              ("battery_discharged", "Battery discharged"), ("grid_imported", "Imported from the grid"),
              ("grid_exported", "Exported to the grid")]
LIFETIME_COUNTERS = {"solar": "plant_accumulated_pv_energy",                        # 30088
                     "load": "plant_accumulated_load_consumption",                  # 30094
                     "battery_charged": "plant_accumulated_battery_charge_energy",  # 30200
                     "battery_discharged": "plant_accumulated_battery_discharge_energy",   # 30204
                     "grid_imported": "plant_accumulated_grid_import_energy",       # 30216
                     "grid_exported": "plant_accumulated_grid_export_energy"}       # 30220
FLOW_POWERS = ["plant_photovoltaic_power", "plant_ess_power", "plant_grid_sensor_active_power", TOTAL_POWER]
MAX_FLOW_GAP = datetime.timedelta(minutes=5)     # a longer gap between readings (polling stopped) isn't counted
FLOWS = [("Solar", "Load"), ("Solar", "Battery"), ("Solar", "Grid"), ("Battery", "Load"), ("Battery", "Grid"),
         ("Grid", "Load"), ("Grid", "Battery")]


@dataclass
class DayEnergy:
    """One local day's energy, kWh. `totals` (keys of DAY_TOTALS; one is missing if its registers weren't read that
    day) are the counters; `flows` ((source, sink) -> kWh, FLOWS) split them up, from the power readings."""
    totals: Dict[str, float]
    flows: Dict[Tuple[str, str], float]
    first: datetime.datetime                     # the day's first and last plant readings
    last: datetime.datetime
    covered_hours: float = 0.0                   # the time the flows cover (gaps over MAX_FLOW_GAP left out)


def _split(flows: Dict[Tuple[str, str], float], v: Dict[str, float], hours: float) -> None:
    """Add one interval's energy to `flows`, from the powers at its start: solar serves the load first, then the
    battery, then the grid; the battery serves the load, then the grid; the grid serves the load, then the battery.
    What's left over (the inverter's losses, the sensors' disagreements) goes nowhere."""
    pv = max(v["plant_photovoltaic_power"], 0.0)
    charge, discharge = max(v["plant_ess_power"], 0.0), max(-v["plant_ess_power"], 0.0)
    imported, exported = max(v["plant_grid_sensor_active_power"], 0.0), max(-v["plant_grid_sensor_active_power"], 0.0)
    load = max(v[TOTAL_POWER], 0.0)
    for source, sink in FLOWS:
        supply = {"Solar": pv, "Battery": discharge, "Grid": imported}[source]
        demand = {"Load": load, "Battery": charge, "Grid": exported}[sink]
        kw = min(supply, demand)
        if source == "Solar":
            pv -= kw
        elif source == "Battery":
            discharge -= kw
        else:
            imported -= kw
        if sink == "Load":
            load -= kw
        elif sink == "Battery":
            charge -= kw
        else:
            exported -= kw
        flows[source, sink] += kw * hours


def day_energy(conn, start: datetime.datetime, end: datetime.datetime) -> Optional[DayEnergy]:
    """The energy of the local day [start, end) (end = now, for today so far); None without plant readings.

    Every total is a lifetime counter's difference between this server's readings, by this server's clock: its value
    at the first plant reading at or after the next midnight (today: at the day's last reading), minus its value at
    the day's first. Not the device's daily counters: it resets those by its own clock, which keeps no daylight
    saving, and a few seconds late. Consecutive days share their boundary reading, so days add up exactly; a gap in
    polling moves energy to the day before the gap, never loses it.
    Known and accepted: the lifetime counters roll back after a power-off (2026-10-13: back to their values ~30
    minutes before it, as if saved every half hour), so a day containing a power-off loses that stretch. One
    difference per day keeps this cheap; summing only the increases would avoid it, and isn't worth it here."""
    plant = list(reading_values(conn, PLANT, [*LIFETIME_COUNTERS.values(), *FLOW_POWERS], start, end).values())
    if not plant:
        return None
    following = conn.execute("SELECT id FROM readings WHERE unit_id = %s AND error IS NULL AND started_at >= %s "
                             "ORDER BY started_at LIMIT 1", (PLANT, end)).fetchone()
    closing = (next(iter(reading_values(conn, PLANT, list(LIFETIME_COUNTERS.values()), ids=[following[0]]).values()))
               if following else plant[-1])[1]
    opening, totals = plant[0][1], {}
    for key, name in LIFETIME_COUNTERS.items():
        if name in opening and name in closing:
            totals[key] = closing[name] - opening[name]
    flows, covered = {f: 0.0 for f in FLOWS}, 0.0
    for (t0, v), (t1, _) in zip(plant, plant[1:]):
        if t1 - t0 <= MAX_FLOW_GAP and all(n in v for n in FLOW_POWERS):
            hours = (t1 - t0) / datetime.timedelta(hours=1)
            _split(flows, v, hours)
            covered += hours
    return DayEnergy(totals, flows, plant[0][0], plant[-1][0], covered)


SANKEY_COLOURS = {"Solar": "#e8c547", "Battery": "#45b8ac", "Grid": "#6f7bd9", "Load": "#9b5de5"}


SANKEY_SOURCES = [("Solar", "solar"), ("Battery", "battery_discharged"), ("Grid", "grid_imported")]
SANKEY_SINKS = [("Battery", "battery_charged"), ("Load", "load"), ("Grid", "grid_exported")]
SANKEY_MIN_SHARE = 0.005        # a flow under this share of the day's energy in isn't drawn: at true scale it's a hairline


def sankey(e: DayEnergy) -> dict:
    """What the daily page's Sankey diagram (static/my_sankey.js) draws: sources (solar, the battery discharging, the
    grid importing) and sinks (the battery charging, the house, the grid exporting), each with its counter's kWh (0
    without one: the drawing then sizes it by its bands) and colour, and the flows between them (kWh, `from` / `to`
    indexing the sources and sinks listed).

    A flow under SANKEY_MIN_SHARE of the day's energy in (the sources' counters added up) isn't drawn: it's listed in
    `dropped` instead (for the note under the diagram) and taken off the totals of both its ends, and a source or sink
    left with no flow to draw is left out. `cutoff` is that threshold in kWh."""
    source_kwh = {name: e.totals.get(key, 0.0) for name, key in SANKEY_SOURCES}
    sink_kwh = {name: e.totals.get(key, 0.0) for name, key in SANKEY_SINKS}
    cutoff = SANKEY_MIN_SHARE * sum(source_kwh.values())
    kept, dropped = [], []
    for (s, d), kwh in e.flows.items():
        if kwh <= 0:
            continue
        if kwh < cutoff:
            dropped.append({"from": s, "to": d, "kwh": round(kwh, 4)})
            source_kwh[s] -= kwh
            sink_kwh[d] -= kwh
        else:
            kept.append((s, d, kwh))
    sources = [name for name, _ in SANKEY_SOURCES if any(s == name for s, _, _ in kept)]
    sinks = [name for name, _ in SANKEY_SINKS if any(d == name for _, d, _ in kept)]

    def nodes(names, kwh):
        return [{"name": n, "kwh": round(max(kwh[n], 0.0), 4), "colour": SANKEY_COLOURS[n]} for n in names]
    return {"sources": nodes(sources, source_kwh), "sinks": nodes(sinks, sink_kwh),
            "flows": [{"from": sources.index(s), "to": sinks.index(d), "kwh": round(kwh, 4)} for s, d, kwh in kept],
            "dropped": dropped, "cutoff": round(cutoff, 4)}


# ---------- figures ----------
def _colors(labels) -> Dict[str, str]:
    return {name: ("grey" if name in (OTHER, "Other") else f"C{n}") for n, name in enumerate(labels)}


def _time_axis(ax, tz, times):
    span = (times[-1] - times[0]) if len(times) > 1 else datetime.timedelta(0)
    ax.xaxis.set_major_locator(AutoDateLocator(tz=tz))
    ax.xaxis.set_major_formatter(DateFormatter("%H:%M" if span <= datetime.timedelta(hours=24) else "%m-%d %H:%M",
                                               tz=tz))


def _minor_time_grid(ax, tz, times):
    """Unlabelled minor ticks with fainter grid lines between the labelled ones: every 15 minutes up to a day,
    hourly up to three days, none beyond (they'd be a grey wash)."""
    span = (times[-1] - times[0]) if len(times) > 1 else datetime.timedelta(0)
    if span <= datetime.timedelta(hours=24):
        ax.xaxis.set_minor_locator(MinuteLocator(byminute=[0, 15, 30, 45], tz=tz))
    elif span <= datetime.timedelta(hours=72):
        ax.xaxis.set_minor_locator(HourLocator(tz=tz))
    else:
        return
    ax.grid(True, axis="x", which="minor", alpha=0.12, lw=0.6)


def _local(t, tz):
    return t.astimezone(tz)


def _legend(ax):
    """A legend whose lines are thick enough to read the colours of thin data lines."""
    for line in ax.legend(loc="upper left", fontsize=9).get_lines():
        line.set_linewidth(2.5)


# Each chart's size in inches; at PNG_DPI, its size in pixels. The dashboard page gives each <img> the same width and
# height, so the browser reserves the box before the image arrives and nothing moves while the charts load; a chart's
# "no data" message is drawn at the chart's own size for the same reason.
PNG_DPI = 100
ENERGY_SIZE = (10, 5)
ENERGY_SINCE_START_SIZE = (11, 4.8)
SNAPSHOTS_SIZE = (11, 4.5)
BATTERY_TEMPS_SIZE = (11, 4.5)
BATTERY_POWER_SIZE = (11, 3.5)
STRINGS_HEIGHT = 4.2


def strings_size(precip: bool = False, satellite: bool = False) -> Tuple[float, float]:
    """strings_figure's size: taller by each strip underneath (the radar's, and the satellite's nine)."""
    strips = STRIP_HEIGHT * (precip + len(SATELLITE_STRIPS) * satellite)
    return 11, round(STRINGS_HEIGHT + 0.25 * strips + 0.35 * (strips > 0), 2)


def pixels(size: Tuple[float, float]) -> Tuple[int, int]:
    """A size in inches as the PNG's width and height in pixels."""
    return round(size[0] * PNG_DPI), round(size[1] * PNG_DPI)


def message_figure(text: str, width=11, height=2.5) -> Figure:
    fig = Figure(figsize=(width, height))
    fig.text(0.5, 0.5, text, ha="center", va="center", fontsize=12, wrap=True)
    return fig


@dataclass
class Strip:
    """One strip under a chart: a bar from each start (in time order), `length` long or up to the next start, in its
    colour (anything matplotlib takes as a colour; None = no bar, so the hatched background shows: no data)."""
    label: str
    starts: List[datetime.datetime]
    length: datetime.timedelta
    colours: list


STRIP_HEIGHT = 0.8                      # each strip's share of the chart, against the main plot's 12
STRIP_LABEL_SIZE = 8
STRIP_COLUMNS = 1100                    # the strips' image: about one column per pixel of the chart's width
STRIP_ROWS = 4                          # image rows per strip


def draw_strips(ax, strips: List[Strip], x0: float, x1: float, columns: int = STRIP_COLUMNS) -> None:
    """All `strips` on `ax` from x0 to x1 (matplotlib date numbers) as ONE image: a band of rows per strip, a white
    line between bands, each label as a y tick. Transparent where a strip has no bar, so the hatched background shows.
    One artist however many bars: a Rectangle per bar and an axes per strip made a day's chart ~4× slower."""
    centres = x0 + (np.arange(columns) + 0.5) * (x1 - x0) / columns
    bands, edges = [], [0]
    for strip in strips:
        row = np.zeros((columns, 4))                      # RGBA, transparent
        if strip.starts:
            starts = date2num(strip.starts)
            stops = np.minimum(starts + strip.length / datetime.timedelta(days=1), np.r_[starts[1:], np.inf])
            rgba = np.array([to_rgba(c) if c is not None else (0.0, 0.0, 0.0, 0.0) for c in strip.colours])
            bar = np.searchsorted(starts, centres, side="right") - 1          # the last bar starting at or before
            inside = (bar >= 0) & (centres < stops[np.maximum(bar, 0)])
            row[inside] = rgba[bar[inside]]
        bands.append(np.broadcast_to(row, (STRIP_ROWS, columns, 4)))
        edges.append(edges[-1] + STRIP_ROWS)
    ax.patch.set_hatch("////")
    ax.patch.set_edgecolor("#c8c8c8")
    ax.imshow(np.concatenate(bands), aspect="auto", interpolation="nearest", extent=(x0, x1, edges[-1], 0))
    ax.hlines(edges[1:-1], x0, x1, color="white", lw=2)
    ax.set_yticks([(a + b) / 2 for a, b in zip(edges, edges[1:])], [s.label for s in strips], fontsize=STRIP_LABEL_SIZE)
    ax.tick_params(axis="y", length=0)
    ax.set_ylim(edges[-1], 0)
    ax.set_xlim(x0, x1)


def envelope(times: List[datetime.datetime], values, columns: int):
    """A line thinned to what `columns` pixel columns can show: in each of `columns` equal slices of its time span,
    its lowest and highest point, in time order; that's exactly what a line through every point covers, single-reading
    spikes included (a mean would flatten them). Unchanged with no more than 2 × columns points."""
    if len(times) <= 2 * columns:
        return times, values
    t, v = date2num(times), np.asarray(values, dtype=float)
    slot = np.minimum(((t - t[0]) / (t[-1] - t[0]) * columns).astype(int), columns - 1)
    starts = np.flatnonzero(np.r_[True, slot[1:] != slot[:-1]])
    keep = []
    for a, b in zip(starts, np.r_[starts[1:], len(t)]):
        low, high = a + int(v[a:b].argmin()), a + int(v[a:b].argmax())
        keep += [low] if low == high else sorted((low, high))
    return [times[i] for i in keep], [float(v[i]) for i in keep]


def precipitation_strip(p: Precipitation) -> Tuple[Strip, str]:
    """`p` as a strip: a bar per radar frame, from white (nothing falling) to the kind's colour at its top rate
    (Precipitation.shares); labelled "Rain" or "Snow". With the note saying how to read it."""
    white, dark = (1.0, 1.0, 1.0), to_rgb(PRECIP_COLOURS[p.kind])
    strip = Strip(p.kind.capitalize(), p.times, RADAR_FRAME,
                  [tuple(w + s * (d - w) for w, d in zip(white, dark)) for s in p.shares()])
    return strip, (f"{p.kind.capitalize()}: the radar at the site, as {p.kind} (Open-Meteo's kind for most frames): "
                   f"white = nothing, darkest = {p.top[p.kind]:.2f} {PRECIP_UNITS[p.kind]} (the most in 7 days), log "
                   f"scale (k = {PRECIP_EASING_K}); hatched = no frame")


# The satellite strips (sigen.goes_shade): (column, label, low-sun mask?, value -> share of sun: 1 white, 0 black)
SATELLITE_STRIPS = (
    [(f"trans_{tag}", f"NOAA sun through, {where}", True, lambda v: v) for tag, where in (
        ("top", "cloud top"), ("0km", "overhead"), ("2km", "2 km"), ("5km", "5 km"), ("10km", "10 km"))]
    + [(f"prob_{tag}", f"NOAA cloud, {where}", False, lambda v: 1 - v) for tag, where in (
        ("0km", "overhead"), ("2km", "2 km"), ("5km", "5 km"), ("10km", "10 km"))])
SATELLITE_LOW_SUN = 10           # degrees: below this the sun-through-cloud estimate is unreliable
LOW_SUN_COLOUR = "#cfe2f3"


def satellite_strips(s) -> Tuple[List[Strip], str]:
    """One strip per SATELLITE_STRIPS from a goes_shade.ShadeSeries: a bar per scan, white = clear, black = no sun
    (grey in between), pale blue = sun too low for the estimate, none (hatched) = no scan or no value. With the note
    saying how to read them."""
    from sigen.goes_shade import SCAN
    low = [e < SATELLITE_LOW_SUN for e in s.sun_elevation]
    strips = []
    for column, label, masked, clear in SATELLITE_STRIPS:
        colours = []
        for value, is_low in zip(s.column(column), low):
            if masked and is_low:
                colours.append(LOW_SUN_COLOUR)
            elif math.isnan(value):
                colours.append(None)
            else:
                v = min(max(clear(value), 0.0), 1.0)
                colours.append((v, v, v))
        strips.append(Strip(label, s.times, SCAN, colours))
    return strips, ("NOAA: the GOES-West satellite, per 10-minute scan (10-20 min behind): the share of sun through the "
                    "cloud (from its optical depth) and 1 − cloud probability, over the site and where a cloud at the "
                    "measured cloud top or at 2 / 5 / 10 km would shade it; white = clear, black = no sun, pale blue = "
                    f"sun below {SATELLITE_LOW_SUN}°, hatched = no scan")


def strings_figure(data: StringData, tz, quantity: str, clear_sky=None, faces: Optional[Dict[int, str]] = None,
                   xlim: Optional[Tuple[datetime.datetime, datetime.datetime]] = None,
                   precip: Optional[Precipitation] = None, satellite=None) -> Figure:
    """quantity "voltage" (each string's voltage) or "power" (V × I, kW), every reading. With `clear_sky` (power
    only: clear_sky_strings' DataFrame), each string's clear-sky model dashed and faint behind it in its colour;
    `faces` names each string's roof face in the legend; `xlim` (live_limits) trims the time axis. Strips underneath,
    in one panel (draw_strips): `precip`, the radar (precipitation_strip); `satellite`, a goes_shade.ShadeSeries
    (satellite_strips). Each string's line is thinned to the chart's pixel columns (envelope)."""
    size = strings_size(precip is not None, satellite is not None)
    if len(data.times) < 2:
        return message_figure("no string readings in this window", *size)
    strips, notes = [], []
    if precip is not None:
        strip, note = precipitation_strip(precip)
        strips, notes = strips + [strip], notes + [note]
    if satellite is not None:
        more, note = satellite_strips(satellite)
        strips, notes = strips + more, notes + [note]
    fig = Figure(figsize=size)
    if strips:
        ax, strip_ax = fig.subplots(2, 1, sharex=True,
                                    gridspec_kw={"height_ratios": [12, STRIP_HEIGHT * len(strips)]})
    else:
        ax = fig.subplots()
    for i, n in enumerate(STRINGS):
        if clear_sky is not None and n in clear_sky:
            ax.plot(clear_sky.index, clear_sky[n], lw=1.3, ls="--", color=f"C{i}", alpha=0.55)
        values = data.volts[n] if quantity == "voltage" else data.kw(n)
        face = f" ({faces[n]})" if faces and n in faces else ""
        ax.plot(*envelope(data.times, values, pixels(size)[0]), lw=0.9, label=f"String {n}{face}", color=f"C{i}")
    if clear_sky is not None:
        ax.plot([], [], lw=1.3, ls="--", color="grey", label="clear-sky model (dashed)")
    ax.set_ylabel("String voltage (V)" if quantity == "voltage" else "Power (kW) = string voltage × current")
    ax.set_title(f"PV string {quantity}, every reading, {_local(data.times[0], tz):%m-%d %H:%M} to "
                 f"{_local(data.times[-1], tz):%m-%d %H:%M}"
                 + (f" (clear sky: {clear_sky.sum().sum() * _hours_per_row(clear_sky):.1f} kWh, "
                    f"measured {data.kwh():.1f} kWh)" if clear_sky is not None and len(clear_sky) > 1 else ""))
    ax.set_ylim(bottom=0)
    _time_axis(ax, tz, data.times)
    if xlim is not None:
        ax.set_xlim(*xlim)
    ax.grid(True, alpha=0.3)
    _legend(ax)
    if strips:
        x0, x1 = ax.get_xlim()                   # the strings' span: strip data outside it doesn't widen the axis
        draw_strips(strip_ax, strips, x0, x1)
        ax.set_xlim(x0, x1)
        strip_ax.set_xlabel("\n".join(textwrap.fill(n, 140) for n in notes), fontsize=8)
    fig.tight_layout(h_pad=0.2)
    return fig


def counters_figure(c: Optional[LoadCounters], tz, since_start: bool = False) -> Figure:
    """The counters, and the energy between consecutive picked readings as average kW.
    since_start: the top panel shows each counter minus its first value (energy used since the window started)
    instead of the lifetime counter, which flattens out as the lifetime grows."""
    if c is None:
        return message_figure("fewer than two 15-minute windows of plant readings in this window")
    colors = _colors(c.series)
    fig = Figure(figsize=(11, 8))
    top, bottom = fig.subplots(2, 1, sharex=True)
    shown = c.since_start() if since_start else c.series
    for name, kwh in shown.items():
        top.plot(c.times, kwh, marker=".", linewidth=1.5, label=name, color=colors[name])
        bottom.step(c.times[:-1], c.average_kw(name), where="post", linewidth=1.5, label=name, color=colors[name])
    top.set_ylabel(f"Energy since {_local(c.times[0], tz):%m-%d %H:%M} (kWh)" if since_start
                   else "Lifetime counter (kWh)")
    top.set_title(f"Smart loads and Other, first reading of each 15-min window "
                  f"({_local(c.times[0], tz):%m-%d %H:%M} to {_local(c.times[-1], tz):%m-%d %H:%M} local)")
    bottom.set_ylabel("Average power per interval (kW)")
    bottom.xaxis.set_major_formatter(DateFormatter("%m-%d %H:%M", tz=tz))
    bottom.set_xlabel(f"Local time ({datetime.datetime.now(tz):%Z})")
    for ax in (top, bottom):
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    return fig


def energy_since_start_figure(c: Optional[LoadCounters], tz) -> Figure:
    """Each smart load's and Other's energy since the window's first reading, from the lifetime counters (exact
    energy, in 0.01 kWh steps), at every reading. The slope is the average power over any stretch you look at."""
    if c is None:
        return message_figure("fewer than two plant readings in this window", *ENERGY_SINCE_START_SIZE)
    colors = _colors(c.series)
    fig = Figure(figsize=ENERGY_SINCE_START_SIZE)
    ax = fig.subplots()
    for name, kwh in c.since_start().items():
        ax.plot(c.times, kwh, linewidth=1.5, label=name, color=colors[name])
    spacing = (c.times[-1] - c.times[0]) / (len(c.times) - 1)
    every = "every reading" if spacing < datetime.timedelta(seconds=20) else \
        f"a reading every ~{spacing.total_seconds() / 60:.1f} min" if spacing >= datetime.timedelta(minutes=1) else \
        f"a reading every ~{spacing.total_seconds():.0f} s"
    ax.set_ylabel(f"Energy since {_local(c.times[0], tz):%m-%d %H:%M} (kWh)")
    ax.set_title(f"Smart loads and Other, energy since the window started ({every}, "
                 f"{_local(c.times[0], tz):%m-%d %H:%M} to {_local(c.times[-1], tz):%m-%d %H:%M})")
    _time_axis(ax, tz, c.times)
    ax.grid(True, alpha=0.3)
    _legend(ax)
    fig.tight_layout()
    return fig


def energy_figure(e: Optional[LoadEnergy], tz) -> Figure:
    """A bar chart: energy per load over the period, with each bar's share of the house total."""
    if e is None:
        return message_figure("fewer than two plant readings in this window", *ENERGY_SIZE)
    fig = Figure(figsize=ENERGY_SIZE)
    ax = fig.subplots()
    colors = [f"C{i}" for i in range(len(e.bars) - 1)] + ["grey"]
    rects = ax.bar(list(e.bars), list(e.bars.values()), color=colors)
    for rect, kwh in zip(rects, e.bars.values()):
        share = f"\n{100 * kwh / e.house:.1f}%" if e.house else ""
        ax.annotate(f"{kwh:.2f} kWh{share}", (rect.get_x() + rect.get_width() / 2, rect.get_height()),
                    xytext=(0, 4), textcoords="offset points", ha="center", va="bottom", fontsize=10)
    ax.set_ylabel("Energy (kWh)")
    ax.set_title(f"Energy by load, {_local(e.first[1], tz):%m-%d %H:%M} to {_local(e.last[1], tz):%m-%d %H:%M} "
                 f"local ({e.hours:.1f} h, {e.house:.2f} kWh total)")
    ax.set_ylim(0, max(max(e.bars.values()), 0.01) * 1.2)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    return fig


LOAD_POWER_SMOOTHING = datetime.timedelta(minutes=5)


def snapshots_figure(s: LoadSnapshots, tz, smoothing: Optional[datetime.timedelta] = LOAD_POWER_SMOOTHING) -> Figure:
    """Instantaneous power per load and Other: a trailing mean over `smoothing` (rolling_mean), with every reading
    faint behind it in the same colour; smoothing=None draws just the readings. (Samples every 15 s: a short burst
    between two can be missed, which the energy chart's counters never do.)"""
    if len(s.times) < 2:
        return message_figure("no plant readings in this window", *SNAPSHOTS_SIZE)
    colors = _colors(s.series)
    fig = Figure(figsize=SNAPSHOTS_SIZE)
    ax = fig.subplots()
    for name, kw in s.series.items():
        if smoothing is None:
            ax.plot(s.times, kw, lw=0.8, label=name, color=colors[name])
        else:
            ax.plot(s.times, kw, lw=0.6, color=colors[name], alpha=0.2)
            ax.plot(s.times, rolling_mean(s.times, kw, smoothing), lw=1.3, label=name, color=colors[name])
    how = "every reading" if smoothing is None else \
        f"{smoothing.total_seconds() / 60:g}-min mean (every reading faint)"
    ax.set_ylabel("Power (kW), instantaneous")
    ax.set_title(f"Smart loads and Other, {how}, {_local(s.times[0], tz):%m-%d %H:%M} to "
                 f"{_local(s.times[-1], tz):%m-%d %H:%M}")
    _time_axis(ax, tz, s.times)
    ax.grid(True, alpha=0.3)
    _legend(ax)
    fig.tight_layout()
    return fig


INVERTER_SMOOTHING = datetime.timedelta(minutes=5)


def rolling_mean(times: List[datetime.datetime], values: List[float], window: datetime.timedelta,
                 coverage: float = 0.9, max_gap: datetime.timedelta = datetime.timedelta(minutes=2)) -> List[float]:
    """The trailing mean over each reading's last `window` (readings in (t − window, t]). NaN (a gap in the plot)
    unless those readings span at least `coverage` × window with no gap between them over `max_gap`: at the start,
    and after a stop in the polling, rather than a mean of a few readings."""
    out, total, lo, last_gap = [], 0.0, 0, None          # last_gap: index after the latest gap over max_gap
    for hi, (t, v) in enumerate(zip(times, values)):
        total += v
        if hi and t - times[hi - 1] > max_gap:
            last_gap = hi
        while times[lo] <= t - window:
            total -= values[lo]
            lo += 1
        full = t - times[lo] >= window * coverage and (last_gap is None or last_gap <= lo)
        out.append(total / (hi - lo + 1) if full else float("nan"))
    return out


def battery_temps_figure(s: Series, air: List[Tuple[datetime.datetime, float]], tz) -> Figure:
    """The battery's cluster and cell temperatures and the outside air on the left axis; the inverter's internal
    temperature (red, a trailing 5-minute mean: its readings jump around by ±0.5 °C) on its own right axis, so both
    ranges fill the plot. The legend belongs to the right axis, which matplotlib draws on top."""
    if len(s.times) < 2:
        return message_figure("no battery temperatures in this window", *BATTERY_TEMPS_SIZE)
    fig = Figure(figsize=BATTERY_TEMPS_SIZE)
    ax = fig.subplots()
    for (name, label), color in zip(BATTERY_TEMPS.items(), ("tab:blue", "tab:orange", "tab:purple")):
        ax.plot(s.times, s.values[name], lw=1.8 if "min" in name else 1.0, color=color, label=label)
    if air:
        ax.plot([t for t, _ in air], [v for _, v in air], lw=1.2, ls="--", color="grey", label="outside air (Open-Meteo)")
    ax.axhline(0, color="black", lw=0.6)
    ax.set_ylabel("Battery (°C)")
    right = ax.twinx()
    right.plot(s.times, rolling_mean(s.times, s.values[INVERTER_TEMP], INVERTER_SMOOTHING), lw=1.2, color="tab:red",
               label=f"inverter internal ({INVERTER_SMOOTHING.total_seconds() / 60:g}-min mean)")
    right.set_ylabel("Inverter internal (°C)", color="tab:red")
    right.tick_params(axis="y", colors="tab:red")
    coolest = s.values["inverter_ess_min_cluster_temperature"]
    ax.set_title(f"Battery temperatures, {_local(s.times[0], tz):%m-%d %H:%M} to {_local(s.times[-1], tz):%m-%d %H:%M}"
                 f" (coolest cluster: low {min(coolest):.1f} °C, now {coolest[-1]:.1f} °C)")
    _time_axis(ax, tz, s.times)
    ax.grid(True, alpha=0.3)
    _minor_time_grid(ax, tz, s.times)
    lines = ax.get_lines()[:len(BATTERY_TEMPS) + bool(air)] + right.get_lines()
    legend = right.legend(lines, [l.get_label() for l in lines], loc="upper left", fontsize=9, framealpha=0.9)
    for line in legend.get_lines():
        line.set_linewidth(2.5)
    fig.subplots_adjust(**BATTERY_MARGINS)
    return fig


# Fixed margins (fractions of the figure) for the two battery charts, so their time axes line up on the page: the
# temperatures need room on the right for the inverter's axis.
BATTERY_MARGINS = dict(left=0.07, right=0.93, top=0.91, bottom=0.12)


POWER_SMOOTHING = datetime.timedelta(minutes=5)
MIN_POWER_SPAN = 2.0            # kW: a quiet night's ±10 W noise shouldn't fill the plot
MIN_SOC_SPAN = 10.0             # %: likewise for a battery sitting at its reserve


def _soc_axis(ax, soc: Series):
    """The SOC (blue) on a right-hand axis, with the backup reserve and the charge cut-off settings as dashed /
    dotted steps; the axis spans at least MIN_SOC_SPAN points, within 0–100 %."""
    right = ax.twinx()
    right.plot(soc.times, soc.values[SOC], lw=1.4, color="tab:blue", label="SOC")
    for (name, label), ls in zip(SOC_LIMITS.items(), ("--", ":")):
        right.step(soc.times, soc.values[name], where="post", lw=1.1, ls=ls, color="tab:blue", alpha=0.6,
                   label=f"{label} (setting)")
    everything = [v for name in (SOC, *SOC_LIMITS) for v in soc.values[name]]
    lo, hi = min(everything) - 2, max(everything) + 2
    if hi - lo < MIN_SOC_SPAN:
        middle = (lo + hi) / 2
        lo, hi = middle - MIN_SOC_SPAN / 2, middle + MIN_SOC_SPAN / 2
    right.set_ylim(max(lo, 0), min(hi, 100))
    right.set_ylabel("SOC (%)", color="tab:blue")
    right.tick_params(axis="y", colors="tab:blue")
    return right


def battery_power_figure(s: Series, tz, soc: Optional[Series] = None) -> Figure:
    """Battery power (>0 charging, <0 discharging) and PV power: a trailing 5-minute mean, with every reading in
    the same colour, faint, behind it. The y axis spans at least MIN_POWER_SPAN kW (around the data's middle), so a
    night of near-zero readings is a flat line, not noise blown up to fill the plot. With `soc` (plant readings of SOC
    and SOC_LIMITS), the SOC and those settings on a right-hand axis; the legend then belongs to that axis, which
    matplotlib draws on top."""
    if len(s.times) < 2:
        return message_figure("no battery or PV readings in this window", *BATTERY_POWER_SIZE)
    fig = Figure(figsize=BATTERY_POWER_SIZE)
    ax = fig.subplots()
    smooth = f"{POWER_SMOOTHING.total_seconds() / 60:g}-min mean"
    for name, color, label in ((PV_POWER, "goldenrod", f"PV ({smooth})"),
                               (BATTERY_POWER, "tab:green", f"battery, >0 charging, <0 discharging ({smooth})")):
        ax.plot(s.times, s.values[name], lw=0.6, color=color, alpha=0.2)
        ax.plot(s.times, rolling_mean(s.times, s.values[name], POWER_SMOOTHING), lw=1.2, color=color, label=label)
    ax.axhline(0, color="black", lw=0.6)
    everything = s.values[PV_POWER] + s.values[BATTERY_POWER]
    lo, hi = min(everything), max(everything)
    if hi - lo < MIN_POWER_SPAN:
        middle = (lo + hi) / 2
        ax.set_ylim(middle - MIN_POWER_SPAN / 2, middle + MIN_POWER_SPAN / 2)
    ax.set_ylabel("Power (kW)")
    charged = sum(max(kw, 0) * (b - a).total_seconds() for kw, a, b in
                  zip(s.values[BATTERY_POWER], s.times, s.times[1:])) / 3600
    discharged = sum(max(-kw, 0) * (b - a).total_seconds() for kw, a, b in
                     zip(s.values[BATTERY_POWER], s.times, s.times[1:])) / 3600
    has_soc = soc is not None and len(soc.times) > 0
    soc_change = f", SOC {soc.values[SOC][0]:.1f} → {soc.values[SOC][-1]:.1f} %" if has_soc else ""
    ax.set_title(f"Battery and PV power ({smooth}s), {_local(s.times[0], tz):%m-%d %H:%M} to "
                 f"{_local(s.times[-1], tz):%m-%d %H:%M}: battery {charged:.2f} kWh in, {discharged:.2f} kWh out"
                 + soc_change)
    _time_axis(ax, tz, s.times)
    ax.grid(True, alpha=0.3)
    _minor_time_grid(ax, tz, s.times)
    if has_soc:
        right = _soc_axis(ax, soc)
        lines = [l for l in ax.get_lines() + right.get_lines() if not l.get_label().startswith("_")]
        legend = right.legend(lines, [l.get_label() for l in lines], loc="upper left", fontsize=9, framealpha=0.9)
        for line in legend.get_lines():
            line.set_linewidth(2.5)
    else:
        _legend(ax)
    fig.subplots_adjust(**{**BATTERY_MARGINS, "bottom": 0.14})     # a shorter figure: a bit more room for labels
    return fig


def png(fig: Figure, dpi: int = PNG_DPI) -> bytes:
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=dpi)
    return buffer.getvalue()
