"""The dashboard's data, straight from the database (usable from your own code too). The dashboard sends it to the
browser as JSON, where uPlot draws it (static/live_charts.js); nothing here draws.

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

Polls flagged as glitches (readings.glitch, migration 0007; sigen.glitches) are left out everywhere here, both units'
readings, as if never taken: an interval then has its one good poll, or none.
"""
import datetime
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sigen.db import SATELLITE_SHADE_VALUES, has_glitch_column, values_table, wide_layout
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


def _good(conn, alias: str = "readings") -> str:
    """SQL condition for a query over readings: not in a glitched poll. Nothing on a database from before migration
    0007 (no glitch column)."""
    return f" AND {alias}.glitch IS NULL" if has_glitch_column(conn) else ""


def reading_values(conn, unit: int, names: List[str], start=None, end=None, ids=None):
    """{reading id: (started_at, {register name: value after the gain})} for the successful readings of `unit` in
    the window (or with these ids), for the registers `names`; a register without a value is left out. The one
    place to fetch registers over a time window from. Readings of glitched polls are left out.

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
        where += _good(conn, "r")
        query = sql.SQL("SELECT t.reading_id, t.started_at, {} FROM {} t JOIN readings r ON r.id = t.reading_id "
                        "WHERE r.unit_id = %s" + where + " ORDER BY t.started_at").format(
            sql.SQL(", ").join(sql.Identifier("t", n) for n in names), sql.Identifier(values_table(regs[0].device)))
        return {rid: (t, {n: v / gains[n] for n, v in zip(names, values) if v is not None})
                for rid, t, *values in conn.execute(query, [unit, *params])}
    if ids is not None:
        readings = conn.execute("SELECT id, started_at FROM readings WHERE id = ANY(%s) ORDER BY started_at",
                                (list(ids),)).fetchall()
    else:
        where, params = _between(start, end)
        readings = conn.execute(f"SELECT id, started_at FROM readings WHERE unit_id = %s AND error IS NULL{where}"
                                f"{_good(conn)} ORDER BY started_at", [unit, *params]).fetchall()
    by_address = {r.address: r.name for r in regs}
    out = {rid: (t, {}) for rid, t in readings}
    if out:
        # A range of the primary key (reading_id, address) rather than a list of ids: one index range scan.
        # Other units' readings in the range don't have these addresses; failed readings have no values.
        for rid, address, value in conn.execute(
                "SELECT reading_id, address, value FROM register_values "
                "WHERE reading_id BETWEEN %s AND %s AND address = ANY(%s)",
                (min(r[0] for r in readings), max(r[0] for r in readings), list(by_address))):
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


LIVE_VOLTS = 10.0               # a string below this is dark (the panels show voltage well before sunrise): the page
                                # trims the string-voltage chart's time axis to the readings with a string at or above


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


def day_clear_sky_kwh(conn, site, array, start: datetime.datetime, end: datetime.datetime) -> float:
    """The whole array's clear-sky model energy over [start, end), kWh (clear_sky_strings summed; 1-minute steps)."""
    df = clear_sky_strings(conn, site, array, start, end)
    step_hours = (df.index[1] - df.index[0]).total_seconds() / 3600 if len(df.index) > 1 else 1 / 60
    return float(df.sum(axis=1).sum()) * step_hours


# ---------- smart loads ----------
def window_of(t: datetime.datetime) -> datetime.datetime:
    return t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)


def picked_readings(conn, start=None, end=None) -> List[Tuple[int, datetime.datetime]]:
    """The earliest successful plant reading (by time) of each clock-aligned 15-minute window, in time order:
    [(id, started_at)]. Picked and ordered by started_at, not id -- a backfilled reading has the newest id but an
    old time."""
    where, params = _between(start, end)
    readings = conn.execute(f"SELECT id, started_at FROM readings WHERE error IS NULL AND unit_id = %s{where}"
                            f"{_good(conn)} ORDER BY started_at", [PLANT, *params]).fetchall()
    first_in_window = {}
    for reading_id, started_at in readings:
        w = window_of(started_at)
        if w not in first_in_window or started_at < first_in_window[w][1]:
            first_in_window[w] = (reading_id, started_at)
    return sorted(first_in_window.values(), key=lambda r: r[1])


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
    readings = conn.execute(f"SELECT id, started_at FROM readings WHERE error IS NULL AND unit_id = %s{where}"
                            f"{_good(conn)} ORDER BY started_at", [PLANT, *params]).fetchall()
    step =-(-len(readings) // max_points) or 1
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
    ends = (f"SELECT id, started_at FROM readings WHERE error IS NULL AND unit_id = %s{where}{_good(conn)} "
            "ORDER BY started_at")
    first = conn.execute(ends + " LIMIT 1", [PLANT, *params]).fetchone()           # earliest and latest by time,
    last = conn.execute(ends + " DESC LIMIT 1", [PLANT, *params]).fetchone()       # not min / max id (a backfill breaks that)
    if first is None or first[0] == last[0]:
        return None
    (first_id, first_t), (last_id, last_t) = first, last
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


@dataclass
class LoadPower:
    at: datetime.datetime                   # the reading's time
    house: float                            # kW, the total load power (30284)
    bars: Dict[str, float]                  # label -> kW: smart loads in slot order, then "Other"


def latest_load_power(conn, names: Dict[int, str], start=None, end=None) -> Optional[LoadPower]:
    """The newest successful plant reading in the window: each smart load's power (named, or drawing any then; labelled
    as energy_by_load's bars) and Other = total load power − all 24 slots. None if there's no reading. Reads that one
    reading only, so it's cheap to ask for every few seconds."""
    where, params = _between(start, end)
    row = conn.execute(f"SELECT id FROM readings WHERE error IS NULL AND unit_id = %s{where}{_good(conn)} "
                       "ORDER BY started_at DESC LIMIT 1", [PLANT, *params]).fetchone()
    if row is None:
        return None
    t, values = reading_values(conn, PLANT, [*POWERS, TOTAL_POWER], ids=[row[0]]).get(row[0], (None, {}))
    if TOTAL_POWER not in values:
        return None
    bars = {}
    for name in POWERS:                                  # slot order
        slot, kw = _slot(name), values.get(name, 0.0)
        if slot in names or kw > 0:
            bars[names.get(slot, f"slot {slot}")] = kw
    bars["Other"] = values[TOTAL_POWER] - sum(values.get(name, 0.0) for name in POWERS)
    return LoadPower(t, values[TOTAL_POWER], bars)


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
PRECIP_COLOURS = {"rain": "#08306b", "snow": "#000000"}
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
    following = conn.execute("SELECT id FROM readings WHERE unit_id = %s AND error IS NULL AND started_at >= %s"
                             f"{_good(conn)} ORDER BY started_at LIMIT 1", (PLANT, end)).fetchone()
    closing = (next(iter(reading_values(conn, PLANT, list(LIFETIME_COUNTERS.values()), ids=[following[0]]).values()))
               if following else plant[-1])[1]
    opening, totals = plant[0][1], {}
    for key, name in LIFETIME_COUNTERS.items():
        if name in opening and name in closing:
            totals[key] = closing[name] - opening[name]
    flows, covered = {f: 0.0 for f in FLOWS}, 0.0
    for (t0, v), (t1, _) in zip(plant, plant[1:]):
        if datetime.timedelta(0) < t1 - t0 <= MAX_FLOW_GAP and all(n in v for n in FLOW_POWERS):
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


# ---------- the all-time page's day-by-day line ----------
@dataclass
class DailyLine:
    """A day-by-day series for a uPlot line on the all-time page: x = seconds since the epoch (local noon of each day),
    y = the value (kWh); x_min / x_max = the first day's local midnight and the midnight after the last (the x range,
    so each day's strip bar shows whole). Plus the best day, for the caption."""
    t: List[int]
    kwh: List[float]
    peak_day: str
    peak_kwh: float
    x_min: int
    x_max: int


def pv_history(conn, tz, column: str = "solar_kwh") -> Optional[DailyLine]:
    """Each finished day's solar generation (daily_energy.`column`, kWh) as a uPlot series for the all-time page. None
    with fewer than two days. Each day is stamped at local noon, so its date reads the same in any browser's zone."""
    rows = [(d, float(v)) for d, v in conn.execute(
        f"SELECT day, {column} FROM daily_energy WHERE {column} IS NOT NULL ORDER BY day").fetchall()]
    if len(rows) < 2:
        return None
    at = lambda d, hour=0: int(datetime.datetime.combine(d, datetime.time(hour), tz).timestamp())
    peak_day, peak_kwh = max(rows, key=lambda r: r[1])
    return DailyLine([at(d, 12) for d, _ in rows], [v for _, v in rows], peak_day.strftime("%b ") + str(peak_day.day),
                     peak_kwh, at(rows[0][0]), at(rows[-1][0] + datetime.timedelta(days=1)))


def all_time_strips(conn, tz) -> Tuple[List["Strip"], List[str]]:
    """The all-time page's per-day strips, one bar per local day (midnight to midnight) over the daily_energy range,
    painted under its solar line by static/strips.js like the string-power chart's: PV (measured solar / the clear-sky
    model), Rain (the radar at the site), Sun (NOAA's share of sun through the cloud over the house). A strip with no
    data at all is left out; a day without data is hatched. Returns (strips, notes)."""
    days = [d for d, in conn.execute("SELECT day FROM daily_energy ORDER BY day")]
    if len(days) < 2:
        return [], []
    first, last = days[0], days[-1]
    all_days = [first + datetime.timedelta(days=i) for i in range((last - first).days + 1)]
    starts = [datetime.datetime.combine(d, datetime.time(), tz) for d in all_days]
    win_start, win_end = starts[0], datetime.datetime.combine(last + datetime.timedelta(days=1), datetime.time(), tz)

    pv = {d: s / c for d, s, c in conn.execute(
        "SELECT day, solar_kwh, clear_sky_kwh FROM daily_energy WHERE solar_kwh IS NOT NULL AND clear_sky_kwh > 0")}
    rain = {d: float(mm) for d, mm in conn.execute(
        "SELECT (frame_at AT TIME ZONE %s)::date, sum(rain_mm_h) * (%s / 3600.0) FROM radar_frames "
        "WHERE error IS NULL AND frame_at >= %s AND frame_at < %s GROUP BY 1",
        (str(tz), RADAR_FRAME.total_seconds(), win_start, win_end))}
    sun = {d: float(t) for d, t in conn.execute(
        "SELECT (scan_at AT TIME ZONE %s)::date, avg(trans_0km) FROM satellite_shade WHERE error IS NULL "
        "AND sun_elevation_deg > %s AND trans_0km IS NOT NULL AND scan_at >= %s AND scan_at < %s GROUP BY 1",
        (str(tz), SATELLITE_LOW_SUN, win_start, win_end))}

    wettest = max(rain.values(), default=0.0)
    white, blue = (1.0, 1.0, 1.0), _rgb(PRECIP_COLOURS["rain"])
    wet = lambda mm: _hex(tuple(w + (mm / wettest if wettest else 0) * (b - w) for w, b in zip(white, blue)))
    day = datetime.timedelta(days=1)
    strips, notes = [], []
    for label, values, colour, note in (
            ("PV / clear-sky", pv, _ratio_colour,
             "PV / clear-sky: each day's measured solar as a share of the clear-sky model; black = nothing, white = at "
             "the model (a clear day), orange = twice it"),
            ("Rain", rain, wet, f"Rain: the radar at the site, each day's total; white = dry, blue = {wettest:.1f} mm "
                                "(the wettest day here)"),
            ("Sun", sun, lambda t: _hex((t, t, t)),
             f"Sun: NOAA's share of sun through the cloud over the house, each day's mean with the sun over "
             f"{SATELLITE_LOW_SUN}°; white = clear, black = overcast")):
        if values:
            strips.append(Strip(label, starts, day, [colour(values[d]) if d in values else None for d in all_days]))
            notes.append(note)
    return strips, (notes + ["hatched = no data that day"] if strips else [])


# ---------- strips: rows of coloured bars under the string-power chart ----------
@dataclass
class Strip:
    """One strip under a chart: a bar from each start (in time order), `length` long or up to the next start, in its
    colour ("#rrggbb"; None = no bar, so the hatched background shows: no data). The page paints them
    (static/live_charts.js)."""
    label: str
    starts: List[datetime.datetime]
    length: datetime.timedelta
    colours: List[Optional[str]]


def _hex(rgb: Tuple[float, float, float]) -> str:
    """(r, g, b), each 0 to 1, as "#rrggbb"."""
    return "#" + "".join(f"{round(min(max(c, 0.0), 1.0) * 255):02x}" for c in rgb)


def _rgb(colour: str) -> Tuple[float, float, float]:
    """"#rrggbb" as (r, g, b), each 0 to 1."""
    return tuple(int(colour[k:k + 2], 16) / 255 for k in (1, 3, 5))


def precipitation_strip(p: Precipitation) -> Tuple[Strip, str]:
    """`p` as a strip: a bar per radar frame, from white (nothing falling) to the kind's colour at its top rate
    (Precipitation.shares); labelled "Rain" or "Snow". With the note saying how to read it."""
    white, dark = (1.0, 1.0, 1.0), _rgb(PRECIP_COLOURS[p.kind])
    strip = Strip(p.kind.capitalize(), p.times, RADAR_FRAME,
                  [_hex(tuple(w + s * (d - w) for w, d in zip(white, dark))) for s in p.shares()])
    return strip, (f"{p.kind.capitalize()}: the radar at the site, as {p.kind} (Open-Meteo's kind for most frames): "
                   f"white = nothing, darkest = {p.top[p.kind]:.2f} {PRECIP_UNITS[p.kind]} (the most in 7 days), log "
                   f"scale (k = {PRECIP_EASING_K}); hatched = no frame")


def satellite_shade_series(conn, start=None, end=None):
    """The stored GOES shade (satellite_shade) for the window as a goes_shade.ShadeSeries, for satellite_strips: good
    scans in time order, NULL columns as NaN (what ShadeSeries uses for "nothing to measure"). sun_azimuth isn't
    stored (the strips don't use it), so it comes back NaN."""
    from sigen.goes_shade import ShadeSeries
    where, params = _between(start, end, "scan_at")
    rows = conn.execute(f"SELECT scan_at, sun_elevation_deg, {', '.join(SATELLITE_SHADE_VALUES)} "
                        f"FROM satellite_shade WHERE error IS NULL{where} ORDER BY scan_at", params).fetchall()
    nan = float("nan")
    val = lambda v: nan if v is None else v
    return ShadeSeries(times=[r[0] for r in rows], sun_elevation=[val(r[1]) for r in rows],
                       sun_azimuth=[nan] * len(rows),
                       columns={c: [val(r[2 + i]) for r in rows] for i, c in enumerate(SATELLITE_SHADE_VALUES)})


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
                colours.append(_hex((v, v, v)))
        strips.append(Strip(label, s.times, SCAN, colours))
    return strips, ("NOAA: the GOES-West satellite, per 10-minute scan (10-20 min behind): the share of sun through the "
                    "cloud (from its optical depth) and 1 − cloud probability, over the site and where a cloud at the "
                    "measured cloud top or at 2 / 5 / 10 km would shade it; white = clear, black = no sun, pale blue = "
                    f"sun below {SATELLITE_LOW_SUN}°, hatched = no scan")


STRIP_BLOCK = datetime.timedelta(minutes=15)     # the PV-vs-clear-sky strip's bar width
STRIP_LOW_SUN_KW = 1.0                            # a block whose clear-sky model is under this: sun too low to judge
STRIP_OVER_COLOUR = "#ff8c00"                     # ratio 2 and up: measured twice the model (cloud-edge over-irradiance)


def _ratio_colour(ratio: float) -> str:
    """A measured / clear-sky ratio as a colour: black at 0 through white at 1 (the model), then white to
    STRIP_OVER_COLOUR at 2 and above (over the model: cloud edges reflecting extra light onto the sunlit panels)."""
    if ratio <= 1.0:
        grey = max(ratio, 0.0)                                       # black (nothing) to white (at the model)
        return _hex((grey, grey, grey))
    over = min(ratio - 1.0, 1.0)                                     # white to orange at twice the model, held above
    return _hex(tuple(w + over * (o - w) for w, o in zip((1.0, 1.0, 1.0), _rgb(STRIP_OVER_COLOUR))))


def measured_strip(measured: StringData, clear_sky, block: datetime.timedelta = STRIP_BLOCK,
                   low_sun_kw: float = STRIP_LOW_SUN_KW) -> Tuple[Strip, str]:
    """The array's measured PV power as a share of its clear-sky model, per `block`, as a strip: black = nothing,
    white = at the model (a clear-sky day), orange = twice it (cloud edges reflecting extra light onto the sunlit
    panels); pale blue where the model is under `low_sun_kw` (the sun's too low to judge the array), hatched (no bar)
    where there's no reading or no model. Measured is the strings' V × I (string_readings) summed over the array; the
    model is clear_sky_strings (kW per string). Both are on this server's UTC clock, so the blocks are clock-aligned.
    Whole-array, so it's robust to the provisional string-to-face assignment. Needs pandas."""
    import pandas as pd
    rule = pd.Timedelta(block)
    model = clear_sky.sum(axis=1).resample(rule).mean()              # whole-array clear-sky kW per block
    if measured.times:
        total = [sum(kws) for kws in zip(*(measured.kw(n) for n in STRINGS))]
        meas = pd.Series(total, index=pd.DatetimeIndex(measured.times)).resample(rule).mean().reindex(model.index)
    else:
        meas = pd.Series(index=model.index, dtype=float)
    colours: List[Optional[str]] = []
    for m, c in zip(meas.to_numpy(), model.to_numpy()):
        if math.isnan(c):
            colours.append(None)                                    # no model: nothing to compare against
        elif c < low_sun_kw:
            colours.append(LOW_SUN_COLOUR)                          # the sun's too low to judge
        elif math.isnan(m):
            colours.append(None)                                    # a model but no reading there
        else:
            colours.append(_ratio_colour(m / c))                    # black -> white (at the model) -> orange (over it)
    minutes = int(block.total_seconds() // 60)
    return Strip("PV / clear-sky", [t.to_pydatetime() for t in model.index], block, colours), (
        f"PV / clear-sky: the array's measured power (string V × I) as a share of the clear-sky model, per {minutes} "
        f"min; black = nothing, white = at the model (a clear day), orange = twice it (cloud edges reflecting extra "
        f"light onto the sunlit panels); pale blue = model under {low_sun_kw:g} kW (sun too low), hatched = no reading")

