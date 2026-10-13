"""Watching the battery in the cold: is it heating itself, is it getting too cold, is the grid down?

Pure functions, no database: each detector takes a Window (readings over a stretch of time, with columns named like
the scaled views' `plant_register_values` / `inverter_register_values`, values after the gain) and returns Events.
`sigen.alerts` builds the Windows from the database and sends the notifications.

Background: a battery in an unheated space (a garage, say) sits between the indoor and outdoor temperatures, and the
inverter stacked on it only warms it under load. What damages LiFePO4 is charging near or below freezing; discharging
in the cold is safe. Whether a SigenStor battery heats itself is undocumented (its datasheet doesn't say, and older
firmware rejects the protocol's preheating registers), and a heater could show up in many shapes, so there's one
detector per signature:

- plateau: the cells held steady for hours while the outside air says they should keep cooling (a thermostat);
- warming / sawtooth: the cells warm up with no sun and the inverter idle (a heater switching on and off);
- unexplained power: the inverter's DC side uses more than its standby (a heater fed from the grid or from the
  battery: the battery stack's only power connection is the inverter's DC bus);
- idle drain: the battery's deliverable energy falls while it reads idle (a heater behind the BMS's meter);
and, whether or not there's a heater:
- cold: the coolest cluster is at a threshold, or heading there (time to insulate it);
- cold charging: the battery charging while the coolest cluster is near freezing (the damage alarm: the BMS should
  prevent it);
- charge limited: the BMS lets the battery charge at only a fraction of its rated power (the cold's real cost);
- below reserve: SOC under the backup reserve, which the grid normally prevents (so: the grid is down, or something
  drains the battery faster than the grid refills it);
- alarm: any bit of the plant's six alarm registers, in plain words (PCS, battery, gateway, DC charger, plant);
  the grid going down is one of them (PCS alarms 1 bit 9, or the plant's on/off-grid status);
- fault: the plant or the inverter reports the running state Fault (e.g. after the rapid-shutdown button).

Unless the system is Running its battery readings aren't real: in Fault (2026-10-12, the rapid-shutdown button) the
deliverable energy reads 0 and minutes later the temperatures read exactly 25.0 °C; powering up (2026-10-13) it
reports Shutdown with every temperature 0.0 °C, then Standby with the deliverable energy still 0, for ~6 minutes. In
normal operation the state is always Running, nights included (6 days of readings). So detect_all() shows every
detector except detect_fault only the readings from a minute after the last one that wasn't Running: none at all
while the system is in Fault, shut down or starting.

Measured on one system on cold nights: with no sun and the battery idle the inverter draws 26 W from the grid
(σ 2 W) and the DC balance (PV − battery − AC) is the same 26 W; discharging at 1–3 kW it's 150–220 W (conversion
losses, ~5–7 %). The cells cooled 0.2–0.4 °C/h at night (time constant ~13 h); temperatures come in 0.1 °C steps.
"""
import datetime
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from sigen import registers

# Columns of the scaled views, and the outside air (Open-Meteo's temperature_2m, interpolated to the reading times).
COOLEST = "inverter_ess_min_cluster_temperature_c"
WARMEST = "inverter_ess_max_cluster_temperature_c"
AVERAGE_CELL = "inverter_ess_average_cell_temperature_c"
PV = "inverter_pv_power_kw"                                     # DC from the panels
BATTERY = "inverter_ess_power_kw"                               # >0 charging, <0 discharging
INVERTER_AC = "inverter_active_power_kw"                        # >0 the inverter supplies the AC side
SOC = "plant_ess_soc_pct"
RESERVE = "plant_backup_soc_pct"
DELIVERABLE = "plant_ess_available_max_discharging_capacity_kwh"    # kWh the battery can still deliver
MAX_CHARGE = "plant_ess_available_max_charging_power_kw"           # what the BMS allows now
RATED_CHARGE = "plant_ess_rated_charging_power_kw"
GRID_STATUS = "plant_on_off_grid_status"                        # 0 on-grid, 1 off-grid (auto), 2 off-grid (manual)
PLANT_STATE = "plant_running_state"                             # 2 = Fault (registers.RUNNING_STATES)
INVERTER_STATE = "inverter_running_state"
AIR = "air_temperature_c"
RUNNING, FAULT = 1, 2

# The plant's merged alarm registers (merged across inverters): column -> (short key, the protocol's name, bits).
ALARM_REGISTERS = {
    "plant_merged_alarm_1": ("pcs1", "PCS alarms 1", registers.PCS_ALARMS_1),
    "plant_merged_alarm_2": ("pcs2", "PCS alarms 2", registers.PCS_ALARMS_2),
    "plant_merged_alarm_3": ("ess", "ESS alarms", registers.ESS_ALARMS),
    "plant_merged_alarm_4": ("gateway", "Gateway alarms", registers.GATEWAY_ALARMS),
    "plant_merged_alarm_5": ("dc_charger", "DC charger alarms", registers.DC_CHARGER_ALARMS),
    "plant_merged_alarm_6": ("plant", "Plant alarms 1", registers.PLANT_ALARMS_1),
}
# Each alarm bit in plain words, for the notification's title. Written from the protocol's names (kept in the message
# for looking things up); a bit missing here falls back to the protocol's name.
ALARM_TEXT = {
    ("pcs1", 0): "Inverter software versions don't match", ("pcs1", 1): "Low insulation resistance on the PV side",
    ("pcs1", 2): "The inverter is overheating", ("pcs1", 3): "Inverter equipment fault",
    ("pcs1", 4): "System grounding fault", ("pcs1", 5): "A PV string's voltage is too high",
    ("pcs1", 6): "A PV string is wired backwards", ("pcs1", 7): "Current is flowing back into a PV string",
    ("pcs1", 8): "Arc fault detected (AFCI)", ("pcs1", 9): "The grid is down",
    ("pcs1", 10): "Grid voltage too high", ("pcs1", 11): "Grid voltage too low",
    ("pcs1", 12): "Grid frequency too high", ("pcs1", 13): "Grid frequency too low",
    ("pcs1", 14): "Grid voltage unbalanced between the legs", ("pcs1", 15): "Too much DC in the inverter's AC output",
    ("pcs2", 0): "Leakage current too high", ("pcs2", 1): "Internal communication fault",
    ("pcs2", 2): "Internal protection tripped", ("pcs2", 3): "The arc-fault detector failed its self-test",
    ("pcs2", 4): "Off-grid protection tripped",
    ("pcs2", 5): "Stopped by hand: the rapid-shutdown button, or a manual stop",
    ("pcs2", 7): "Grid phases in the wrong order", ("pcs2", 8): "Short circuit to ground (PE)",
    ("pcs2", 9): "The inverter failed to soft-start",
    ("ess", 0): "Battery software versions don't match", ("ess", 1): "Battery: low insulation resistance to ground",
    ("ess", 2): "The battery is too hot", ("ess", 3): "Battery equipment fault", ("ess", 4): "The battery is too cold",
    ("ess", 5): "Battery internal protection tripped", ("ess", 6): "BATTERY THERMAL RUNAWAY",
    ("gateway", 0): "Gateway software versions don't match", ("gateway", 1): "The gateway is overheating",
    ("gateway", 2): "Gateway equipment fault", ("gateway", 3): "Too much leakage current on the backup output",
    ("gateway", 4): "Neutral grounding fault", ("gateway", 5): "Grid wiring: phases in the wrong order",
    ("gateway", 6): "Inverter wiring: phases in the wrong order", ("gateway", 7): "A grid phase is missing",
    ("dc_charger", 0): "DC charger software versions don't match",
    ("dc_charger", 1): "DC charger: low insulation resistance to ground", ("dc_charger", 2): "The DC charger is overheating",
    ("dc_charger", 3): "DC charger equipment fault", ("dc_charger", 4): "DC charging fault",
    ("dc_charger", 5): "DC charger protection tripped",
    ("plant", 0): "Lost contact with the gateway", ("plant", 1): "Lost contact with the grid meter",
    ("plant", 2): "Lost contact with the AC power sensor",
    ("plant", 6): "Export limit exceeded: hard protection tripped", ("plant", 8): "The generator failed to start",
    ("plant", 10): "CLS fault",
}
URGENT_ALARMS = {("pcs1", 9), ("ess", 6)}       # the grid down; thermal runaway
GRID_DOWN_KEY = "alarm:pcs1.9"                   # the off-grid status shares it: one grid outage, one alert

COLUMNS = (COOLEST, WARMEST, AVERAGE_CELL, PV, BATTERY, INVERTER_AC, SOC, RESERVE, DELIVERABLE, MAX_CHARGE,
           RATED_CHARGE, GRID_STATUS, PLANT_STATE, INVERTER_STATE, *ALARM_REGISTERS)   # every view column read

STANDBY_KW = 0.026          # the inverter's DC-side draw with no sun and the battery idle (measured, above)
MAX_GAP = datetime.timedelta(minutes=2)     # readings further apart than this break a run (poll is every 15 s)
NOT_AVAILABLE = 0xFFFF      # an alarm register reading all ones means "not available", not 16 alarms
AFTER_FAULT = datetime.timedelta(minutes=1)  # readings this soon after one not Running aren't trusted either


@dataclass(frozen=True)
class Window:
    """Readings over a stretch of time: `times` ascending and timezone-aware, each column as long as `times`, None
    where a reading has no value. Columns a detector needs but the window lacks count as all None."""
    times: Sequence[datetime.datetime]
    columns: Mapping[str, Sequence[Optional[float]]]

    def __post_init__(self):
        for name, values in self.columns.items():
            if len(values) != len(self.times):
                raise ValueError(f"column {name} has {len(values)} values for {len(self.times)} times")
        if any(b <= a for a, b in zip(self.times, self.times[1:])):
            raise ValueError("times must be strictly ascending")

    def __getitem__(self, name: str) -> Sequence[Optional[float]]:
        return self.columns.get(name) or [None] * len(self.times)

    def __len__(self) -> int:
        return len(self.times)


@dataclass(frozen=True)
class Event:
    kind: str                       # "plateau", "warming", "sawtooth", "unexplained_power", "cold", "alarm", …
    start: datetime.datetime
    end: datetime.datetime
    summary: str                    # one line, for a notification (no clock times: the sender appends the start)
    details: Dict[str, float] = field(default_factory=dict)
    key: Optional[str] = None       # what one event and its cooldown are keyed by, when finer than the kind
    title: Optional[str] = None     # the notification's title, when the kind's fixed one won't do
    priority: Optional[str] = None  # likewise, the ntfy priority

    @property
    def ident(self) -> str:
        return self.key or self.kind


# ---------- helpers ----------
def _hours(a: datetime.datetime, b: datetime.datetime) -> float:
    return (b - a).total_seconds() / 3600



def runs(times: Sequence[datetime.datetime], ok: Sequence[bool],
         max_gap: datetime.timedelta = MAX_GAP) -> List[Tuple[int, int]]:
    """(first, last) index pairs, inclusive, of stretches where `ok` holds without readings further apart than
    `max_gap` (a gap in the polling ends a stretch: nothing is known about the time in between)."""
    out, start = [], None
    for i, good in enumerate(ok):
        if start is not None and (not good or times[i] - times[i - 1] > max_gap):
            out.append((start, i - 1))
            start = None
        if good and start is None:
            start = i
    if start is not None:
        out.append((start, len(ok) - 1))
    return out


def quiet(w: Window, max_pv_kw: float = 0.05, max_battery_kw: float = 0.5, max_ac_kw: float = 0.5) -> List[bool]:
    """Readings with no sun on the panels and the inverter nearly idle: then nothing but the surroundings (and a
    heater) changes the cells' temperature. A small heater still fits under the limits; a missing value is not quiet."""
    return [pv is not None and b is not None and ac is not None
            and pv <= max_pv_kw and abs(b) <= max_battery_kw and abs(ac) <= max_ac_kw
            for pv, b, ac in zip(w[PV], w[BATTERY], w[INVERTER_AC])]


def interpolate(times: Sequence[datetime.datetime], points: Sequence[Tuple[datetime.datetime, float]],
                max_gap: datetime.timedelta = datetime.timedelta(hours=1),
                hold: datetime.timedelta = datetime.timedelta(0)) -> List[Optional[float]]:
    """Values at `times` by straight lines between `points` (time, value), ascending: e.g. Open-Meteo's 15-minute
    air temperatures onto the 15-second readings. None outside the points or between points further apart than
    `max_gap`, except that the last point's value holds for `hold` after it (the newest readings come after the
    newest air temperature)."""
    out, k = [], 0
    for t in times:
        while k + 1 < len(points) and points[k + 1][0] <= t:     # points[k] ≤ t < points[k + 1]
            k += 1
        if points and points[-1][0] < t <= points[-1][0] + hold:
            out.append(points[-1][1])
        elif not points or t < points[0][0] or t > points[-1][0]:
            out.append(None)
        elif t == points[k][0]:
            out.append(points[k][1])
        else:
            (t0, v0), (t1, v1) = points[k], points[k + 1]
            out.append(None if t1 - t0 > max_gap else v0 + (v1 - v0) * (t - t0) / (t1 - t0))
    return out


def _air_note(air: Sequence[Optional[float]], i: int, j: int) -> Tuple[Optional[float], Optional[float], str]:
    """Outside air at the start and end of [i, j] (the nearest known values inside it), and a phrase for summaries."""
    known = [a for a in air[i:j + 1] if a is not None]
    if not known:
        return None, None, "outside air unknown"
    return known[0], known[-1], f"outside air {known[0]:.1f} → {known[-1]:.1f} °C"


# ---------- heater signatures ----------
def detect_plateau(w: Window, column: str = COOLEST, tolerance: float = 0.1,
                   min_duration: datetime.timedelta = datetime.timedelta(hours=2),
                   min_above_air: float = 8.0, min_air_drop: float = 2.0) -> List[Event]:
    """The cells held within `tolerance` °C (0.1 = one sensor step) for at least `min_duration` while quiet, AND
    the outside air says they should have kept cooling: the cells were at least `min_above_air` °C above it on
    average, or the air fell at least `min_air_drop` °C meanwhile. (An unheated space can run several °C above the
    outside air, ~5 °C in one garage, so cells resting at their surroundings' temperature with steady air are not a
    plateau.) Without air data a plateau is reported anyway, saying so. Overlapping stretches merge into one event,
    whose spread may then reach 2 × `tolerance`."""
    temps, air = w[column], w[AIR]
    ok = [q and v is not None for q, v in zip(quiet(w), temps)]
    found: List[Tuple[int, int]] = []
    tol = tolerance + 1e-9
    for a, b in runs(w.times, ok):
        lo, highs, lows = a, deque(), deque()        # indices of the window's max / min candidates
        for hi in range(a, b + 1):
            v = temps[hi]
            while highs and temps[highs[-1]] <= v:
                highs.pop()
            while lows and temps[lows[-1]] >= v:
                lows.pop()
            highs.append(hi)
            lows.append(hi)
            while temps[highs[0]] - temps[lows[0]] > tol:      # shortest stretch ending at hi that fits
                lo += 1
                if highs[0] < lo:
                    highs.popleft()
                if lows[0] < lo:
                    lows.popleft()
            nxt = temps[hi + 1] if hi < b else None
            last = nxt is None or max(temps[highs[0]], nxt) - min(temps[lows[0]], nxt) > tol
            if last and w.times[hi] - w.times[lo] >= min_duration:
                if found and lo <= found[-1][1]:
                    found[-1] = (found[-1][0], hi)
                else:
                    found.append((lo, hi))
    events = []
    for i, j in found:
        held = temps[i:j + 1]
        mean = sum(held) / len(held)
        a0, a1, note = _air_note(air, i, j)
        if a0 is not None:
            known = [a for a in air[i:j + 1] if a is not None]
            above = mean - sum(known) / len(known)
            if above < min_above_air and a0 - min(known) < min_air_drop:
                continue
        events.append(Event("plateau", w.times[i], w.times[j],
                            f"{column} held at {min(held):.1f}–{max(held):.1f} °C for {_hours(w.times[i], w.times[j]):.1f} h, "
                            f"{note}",
                            {"low_c": min(held), "high_c": max(held), "hours": _hours(w.times[i], w.times[j])}))
    return events


def detect_warming(w: Window, column: str = COOLEST, min_rise: float = 0.3,
                   max_rise_duration: datetime.timedelta = datetime.timedelta(minutes=45),
                   air_margin: float = 6.0) -> List[Event]:
    """The cells warmed by at least `min_rise` °C within `max_rise_duration` while quiet (no sun, inverter idle),
    with the outside air at least `air_margin` °C below them (so warmer surroundings can't explain it: a warm
    wind can warm an unheated space fast). A zigzag with a `min_rise` threshold finds each rise ("tooth"); the
    0.1 °C steps of the sensor stay below it. One event per quiet stretch: "warming" for one tooth, "sawtooth" for
    several (how many, how far apart). Without air data the rises are reported anyway, saying so."""
    temps, air = w[column], w[AIR]
    ok = [q and v is not None for q, v in zip(quiet(w), temps)]
    events = []
    for a, b in runs(w.times, ok):
        teeth = []
        rising, trough, peak = False, a, a
        for i in range(a, b + 1):
            if not rising:
                if temps[i] < temps[trough]:
                    trough = i
                elif temps[i] - temps[trough] >= min_rise - 1e-9:
                    rising, peak = True, i
            else:
                if temps[i] > temps[peak]:
                    peak = i
                elif temps[peak] - temps[i] >= min_rise - 1e-9:
                    teeth.append((trough, peak))
                    rising, trough = False, i
        if rising:
            teeth.append((trough, peak))
        kept = []
        for t0, t1 in teeth:
            if w.times[t1] - w.times[t0] > max_rise_duration:
                continue
            a_end = next((x for x in reversed(air[t0:t1 + 1]) if x is not None), None)
            if a_end is not None and temps[t1] - a_end < air_margin:
                continue
            kept.append((t0, t1))
        if not kept:
            continue
        rises = [temps[t1] - temps[t0] for t0, t1 in kept]
        first, last = kept[0][0], kept[-1][1]
        _, _, note = _air_note(air, first, last)
        details = {"teeth": len(kept), "mean_rise_c": sum(rises) / len(rises), "max_rise_c": max(rises),
                   "low_c": min(temps[t0] for t0, _ in kept)}
        if len(kept) == 1:
            (t0, t1), = kept
            text = (f"{column} rose {rises[0]:.1f} °C ({temps[t0]:.1f} → {temps[t1]:.1f}) in "
                    f"{(w.times[t1] - w.times[t0]).total_seconds() / 60:.0f} min with no sun and the inverter idle, "
                    f"{note}")
            events.append(Event("warming", w.times[t0], w.times[t1], text, details))
        else:
            period = _hours(w.times[kept[0][0]], w.times[kept[-1][0]]) / (len(kept) - 1) * 60
            details["period_min"] = period
            text = (f"{column} sawtooth: {len(kept)} rises of ~{details['mean_rise_c']:.1f} °C every ~{period:.0f} "
                    f"min over {_hours(w.times[first], w.times[last]):.1f} h, low {details['low_c']:.1f} °C, {note}")
            events.append(Event("sawtooth", w.times[first], w.times[last], text, details))
    return events


def dc_balance(w: Window) -> List[Optional[float]]:
    """What the inverter's DC side uses, kW: PV in, minus what goes into the battery, minus what goes out on the AC
    side. Its own standby and conversion losses, plus anything else hanging off the DC bus (a battery heater)."""
    return [None if None in (pv, b, ac) else pv - b - ac for pv, b, ac in zip(w[PV], w[BATTERY], w[INVERTER_AC])]


def detect_unexplained_power(w: Window, standby_kw: float = STANDBY_KW, min_excess_kw: float = 0.04,
                             loss_fraction: float = 0.08, max_busy_kw: float = 0.2,
                             min_duration: datetime.timedelta = datetime.timedelta(seconds=30)) -> List[Event]:
    """The DC side used more than `standby_kw` plus `loss_fraction` of the power it converts, by at least
    `min_excess_kw`, for at least `min_duration` (≥ 3 readings at 15 s: a single reading can be a load change caught
    mid-way). Only while the inverter isn't converting between the battery and the AC side (one of them within
    `max_busy_kw`, no sun): a heater fed from the grid shows as AC power flowing in, one fed from the battery as the
    battery discharging, and either way the other side stays near 0."""
    balance = dc_balance(w)
    excess, ok = [], []
    for pv, b, ac, d in zip(w[PV], w[BATTERY], w[INVERTER_AC], balance):
        if d is None:
            excess.append(None)
            ok.append(False)
            continue
        e = d - standby_kw - loss_fraction * max(abs(b), abs(ac), pv)
        excess.append(e)
        ok.append(pv <= 0.05 and min(abs(b), abs(ac)) <= max_busy_kw and e >= min_excess_kw)
    events = []
    for i, j in runs(w.times, ok):
        if w.times[j] - w.times[i] < min_duration:
            continue
        used = balance[i:j + 1]
        mean = sum(used) / len(used)
        kwh = sum((max(d - standby_kw, 0)) * (w.times[k + 1] - w.times[k]).total_seconds() / 3600
                  for k, d in zip(range(i, j), used))
        source = "the grid" if -w[INVERTER_AC][i] > abs(w[BATTERY][i]) else "the battery"
        events.append(Event("unexplained_power", w.times[i], w.times[j],
                            f"the inverter's DC side drew {mean * 1000:.0f} W (standby {standby_kw * 1000:.0f} W) for "
                            f"{(w.times[j] - w.times[i]).total_seconds() / 60:.1f} min, from {source}: "
                            f"{kwh * 1000:.0f} Wh",
                            {"mean_kw": mean, "peak_kw": max(used), "excess_kwh": kwh}))
    return events


def detect_idle_drain(w: Window, min_kwh: float = 0.05, max_battery_kw: float = 0.02, max_soc: float = 95.0,
                      min_duration: datetime.timedelta = datetime.timedelta(minutes=30)) -> List[Event]:
    """The battery's deliverable energy fell by at least `min_kwh` (its steps are 0.01 kWh) over a stretch of at
    least `min_duration` in which it read idle (|power| ≤ `max_battery_kw`) below `max_soc`: energy leaving that the
    battery's power reading doesn't show, e.g. a heater behind the BMS's meter. Not near full: right after a full
    charge the BMS's estimate settles by ~0.25 kWh within the hour (seen twice, idle at ~100 %). Caveat: the
    BMS may also lower its estimate as the cells get cold; the summary gives the temperatures."""
    energy, temps = w[DELIVERABLE], w[COOLEST]
    ok = [None not in (b, e, s) and abs(b) <= max_battery_kw and s < max_soc
          for b, e, s in zip(w[BATTERY], energy, w[SOC])]
    events = []
    for i, j in runs(w.times, ok):
        drop = energy[i] - energy[j]
        if w.times[j] - w.times[i] < min_duration or drop < min_kwh - 1e-9:
            continue
        hours = _hours(w.times[i], w.times[j])
        t0, t1 = temps[i], temps[j]
        cells = f", coolest cluster {t0:.1f} → {t1:.1f} °C" if None not in (t0, t1) else ""
        events.append(Event("idle_drain", w.times[i], w.times[j],
                            f"deliverable energy fell {drop:.2f} kWh ({energy[i]:.2f} → {energy[j]:.2f}) in {hours:.1f} h "
                            f"while the battery read idle (~{drop / hours * 1000:.0f} W){cells}",
                            {"kwh": drop, "mean_w": drop / hours * 1000}))
    return events


# ---------- cold, charging, grid ----------
def detect_cold(w: Window, column: str = COOLEST, threshold: float = 5.0,
                horizon: datetime.timedelta = datetime.timedelta(hours=6),
                trend: datetime.timedelta = datetime.timedelta(hours=1)) -> List[Event]:
    """"cold" if the latest value is at or below `threshold`; otherwise "cooling" if the straight-line trend over
    the last `trend` reaches it within `horizon`. The cells move slowly (time constant ~13 h), so a straight line
    over a few hours ahead is a fair guess; it overestimates the fall as they near their surroundings' temperature."""
    pts = [(t, v) for t, v in zip(w.times, w[column]) if v is not None]
    if not pts:
        return []
    now, latest = pts[-1]
    if latest <= threshold:
        return [Event("cold", now, now, f"{column} is {latest:.1f} °C (≤ {threshold:g} °C): blanket time",
                      {"value_c": latest})]
    recent = [(t, v) for t, v in pts if now - t <= trend]
    if len(recent) < 2 or now - recent[0][0] < trend / 2:
        return []
    xs = [_hours(recent[0][0], t) for t, _ in recent]
    ys = [v for _, v in recent]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0   # °C per hour
    if slope >= 0:
        return []
    eta = datetime.timedelta(hours=(latest - threshold) / -slope)
    if eta > horizon:
        return []
    return [Event("cooling", now, now + eta,
                  f"{column} is {latest:.1f} °C, falling {-slope:.2f} °C/h: {threshold:g} °C in "
                  f"~{_hours(now, now + eta):.1f} h",
                  {"value_c": latest, "slope_c_per_h": slope, "hours_to_threshold": _hours(now, now + eta)})]


def detect_cold_charging(w: Window, threshold: float = 2.0, min_kw: float = 0.1,
                         min_duration: datetime.timedelta = datetime.timedelta(minutes=1)) -> List[Event]:
    """The battery charging at `min_kw` or more for at least `min_duration` while the coolest cluster is at or below
    `threshold` °C. The damage alarm: charging cold LiFePO4 plates lithium (permanent), and the BMS is supposed to
    prevent it (it already cuts the charge rate from ~16 °C down), so this says the protection isn't holding."""
    temps, power = w[COOLEST], w[BATTERY]
    ok = [None not in (t, p) and t <= threshold and p >= min_kw for t, p in zip(temps, power)]
    events = []
    for i, j in runs(w.times, ok):
        if w.times[j] - w.times[i] < min_duration:
            continue
        charging = power[i:j + 1]
        events.append(Event("cold_charging", w.times[i], w.times[j],
                            f"the battery charged at up to {max(charging):.2f} kW with the coolest cluster at "
                            f"{min(temps[i:j + 1]):.1f} °C for {(w.times[j] - w.times[i]).total_seconds() / 60:.0f} min",
                            {"max_kw": max(charging), "coolest_c": min(temps[i:j + 1])}))
    return events


def detect_charge_limited(w: Window, min_fraction: float = 0.5, rated_kw: Optional[float] = None,
                          max_soc: float = 90.0) -> List[Event]:
    """The BMS allowed charging at less than `min_fraction` of the rated power (`rated_kw`, else the window's rated
    charging power column) while the SOC was below `max_soc` (near full it tapers anyway: ~3.1–3.6 kW at 95 %).
    What the cold costs. Measured on one system below 90 % SOC: the full 7.3 kW with the coolest cluster at 16 °C
    or more, then less as it cools (on average ~6.2 kW at 13 °C, 3.6 kW at 10 °C, 2.3 kW at 7 °C)."""
    rated = w[RATED_CHARGE]
    allowed, socs, temps = w[MAX_CHARGE], w[SOC], w[COOLEST]
    ok = []
    for k, (m, s) in enumerate(zip(allowed, socs)):
        r = rated_kw if rated_kw is not None else rated[k]
        ok.append(None not in (m, s, r) and s < max_soc and m < min_fraction * r)
    events = []
    for i, j in runs(w.times, ok):
        lowest = min(allowed[i:j + 1])
        known = [t for t in temps[i:j + 1] if t is not None]
        cells = f", coolest cluster {min(known):.1f} °C" if known else ""
        r = rated_kw if rated_kw is not None else rated[i]
        events.append(Event("charge_limited", w.times[i], w.times[j],
                            f"the battery may charge at only {lowest:.2f} kW of {r:g} kW (SOC {socs[i]:.0f} %){cells}",
                            {"min_kw": lowest}))
    return events


def detect_below_reserve(w: Window, margin: float = 2.0,
                         min_duration: datetime.timedelta = datetime.timedelta(minutes=1)) -> List[Event]:
    """The SOC at least `margin` points below the backup reserve for at least `min_duration`. With the grid up the
    battery charges from it at full power up to the reserve, so this means the grid is down (the battery is backing
    up the house), or something drains it faster than the grid refills it."""
    socs, reserves, status = w[SOC], w[RESERVE], w[GRID_STATUS]
    ok = [None not in (s, r) and s < r - margin for s, r in zip(socs, reserves)]
    events = []
    for i, j in runs(w.times, ok):
        if w.times[j] - w.times[i] < min_duration:
            continue
        off = any(s not in (None, 0) for s in status[i:j + 1])
        why = "the grid is down (off-grid status)" if off else "the grid status says on-grid"
        events.append(Event("below_reserve", w.times[i], w.times[j],
                            f"SOC {socs[j]:.1f} % is below the {reserves[j]:g} % backup reserve for "
                            f"{(w.times[j] - w.times[i]).total_seconds() / 60:.0f} min: {why}",
                            {"soc_pct": socs[j], "reserve_pct": reserves[j], "off_grid": float(off)}))
    return events


# ---------- alarms and faults ----------
def alarm_bits(value) -> List[int]:
    """The bits set in an alarm register's value; none for "not available" (all ones) or no value."""
    if value is None or value == NOT_AVAILABLE:
        return []
    return [bit for bit in range(16) if int(value) >> bit & 1]


def alarm_text(short: str, name: str, flags: Mapping[int, str], bit: int) -> Tuple[str, str]:
    """(plain title, the protocol's own words) for one alarm bit."""
    protocol = f"{name} bit {bit}: {flags.get(bit, 'not defined by the protocol')}"
    return ALARM_TEXT.get((short, bit), flags.get(bit, f"Undefined alarm ({name} bit {bit})")), protocol


def active_alarms(w: Window, k: int) -> List[str]:
    """Every alarm set in reading k, in plain words with the protocol's words after them."""
    out = []
    for column, (short, name, flags) in ALARM_REGISTERS.items():
        for bit in alarm_bits(w[column][k]):
            title, protocol = alarm_text(short, name, flags, bit)
            out.append(f"{title} ({protocol})")
    return out


def detect_alarms(w: Window) -> List[Event]:
    """Stretches where an alarm bit is set, one event per bit, titled in plain words; and stretches where the plant
    says it's off-grid (30009 ≠ 0), keyed like the "grid down" alarm bit, so one outage is one alert."""
    events = []
    for column, (short, name, flags) in ALARM_REGISTERS.items():
        values = w[column]
        for bit in sorted({b for v in values for b in alarm_bits(v)}):
            ok = [bit in alarm_bits(v) for v in values]
            title, protocol = alarm_text(short, name, flags, bit)
            for i, j in runs(w.times, ok):
                events.append(Event("alarm", w.times[i], w.times[j], f"{title}. {protocol}", {"bit": bit},
                                    key=f"alarm:{short}.{bit}", title=title,
                                    priority="urgent" if (short, bit) in URGENT_ALARMS else None))
    status = w[GRID_STATUS]
    for i, j in runs(w.times, [s is not None and s != 0 for s in status]):
        label = registers.GRID_STATUS.get(int(status[j]), f"status {status[j]:g}")
        events.append(Event("alarm", w.times[i], w.times[j],
                            f"The grid is down. The plant's on/off-grid status: {label}", {"status": status[j]},
                            key=GRID_DOWN_KEY, title="The grid is down", priority="urgent"))
    return events


def in_fault(w: Window) -> List[bool]:
    return [FAULT in (p, i) for p, i in zip(w[PLANT_STATE], w[INVERTER_STATE])]


def not_running(w: Window) -> List[bool]:
    """Readings where the plant or the inverter reports a state other than Running (Fault, Shutdown, Standby, …). A
    missing state (a plant-only setup, or before the register was logged) doesn't count."""
    return [any(s is not None and s != RUNNING for s in (p, i)) for p, i in zip(w[PLANT_STATE], w[INVERTER_STATE])]


def detect_fault(w: Window) -> List[Event]:
    """Stretches where the plant or the inverter reports the running state Fault. The summary lists the alarms set
    at the stretch's last reading (a fault's alarms say what it is: e.g. the rapid-shutdown button)."""
    events = []
    for i, j in runs(w.times, in_fault(w)):
        alarms = active_alarms(w, j)
        why = "; ".join(alarms) if alarms else "no alarm bit set"
        events.append(Event("fault", w.times[i], w.times[j],
                            f"The SigenStor reports a fault, for {_hours(w.times[i], w.times[j]) * 60:.0f} min so "
                            f"far. Alarms: {why}", {"minutes": _hours(w.times[i], w.times[j]) * 60}))
    return events


def after_faults(w: Window) -> Window:
    """The readings the other detectors may trust: those from AFTER_FAULT past the last reading that wasn't Running
    on (always Running: all of them; not Running now: none). Not Running, the battery's values aren't real (deliverable
    energy 0; 25.0 °C in a fault, 0.0 °C while starting), and the first readings after may not be either."""
    stopped = [t for t, f in zip(w.times, not_running(w)) if f]
    if not stopped:
        return w
    cutoff = stopped[-1] + AFTER_FAULT
    keep = [k for k, t in enumerate(w.times) if t > cutoff]
    return Window([w.times[k] for k in keep], {c: [v[k] for k in keep] for c, v in w.columns.items()})


DETECTORS = (detect_plateau, detect_warming, detect_unexplained_power, detect_idle_drain, detect_cold,
             detect_cold_charging, detect_charge_limited, detect_below_reserve, detect_alarms)


def detect_all(w: Window) -> List[Event]:
    """detect_fault on every reading; every other detector, with its defaults, only on after_faults(w). In time
    order."""
    trusted = after_faults(w)
    found = detect_fault(w) + [e for d in DETECTORS for e in d(trusted)]
    return sorted(found, key=lambda e: (e.start, e.kind))
