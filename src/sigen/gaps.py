"""Gaps: stretches where a table's rows stopped coming for longer than they should (`sigen db gaps`; `sigen sync
--fill-gaps` fills them from a peer).

Each table is one or more SERIES, each with the interval its good rows normally come at: readings (the plant's polls,
every poll.interval), battery_heater (one per pack: the packs take turns, so poll.heater.slow_interval x the number of
packs), open_meteo_weather (one per resolution, 15 and 60 min), radar_frames (6 min) and satellite_shade (10 min). A
gap is a stretch longer than the series' min gap (default MIN_GAP_FACTOR x its interval: that many rows missed in a
row) between two good rows, or from the last good row to the end of the window (open-ended). Failed attempts stored
inside it (rows with an error) are counted, with their commonest error, as its likely explanation.

satellite_shade counts daylight only (NOAA's cloud products are daytime), and is checked only where this computer's
poll stores it (poll.satellite in the config): elsewhere nothing collects it, so it would be one long gap.
daily_energy has a row per finished local day, so there each missing day is the gap (missing_days).
"""

import collections
import datetime
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import psycopg
from psycopg import sql

from sigen import db, heater, weather
from sigen.config import Config, Site
from sigen.goes import sun_rise_set
from sigen.registers import PLANT_UNIT_ID

MIN_GAP_FACTOR = 3  # the default min gap: this many of a series' intervals
DEFAULT_SINCE = datetime.timedelta(days=7)
SATELLITE_INTERVAL = datetime.timedelta(minutes=10)  # NOAA's full-disk scans
TABLES = ("readings", "battery_heater", "open_meteo_weather", "radar_frames", "satellite_shade", "daily_energy")
DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> datetime.timedelta:
    """'45s', '5m', '1.5h', '7d' (or plain seconds, '90') -> a timedelta; ValueError otherwise or for 0."""
    m = DURATION.match(text)
    if not m or float(m.group(1)) <= 0:
        raise ValueError(f"{text!r} isn't a duration like 45s, 5m, 2h or 7d")
    return datetime.timedelta(seconds=float(m.group(1)) * UNIT_SECONDS[m.group(2)])


def parse_min_gaps(specs: list[str]) -> dict[str, datetime.timedelta]:
    """['readings:30s', 'battery_heater:2h'] -> {table: min gap}; ValueError naming a bad one."""
    out = {}
    for spec in specs:
        table, sep, duration = spec.partition(":")
        if not sep or table not in TABLES:
            raise ValueError(f"{spec!r} isn't TABLE:DURATION with TABLE one of {', '.join(TABLES)}")
        out[table] = parse_duration(duration)
    return out


def span_text(span: datetime.timedelta) -> str:
    """A length as people say it, rounded up (a gap listed as over 45s never reads '45s'): '46s', '9m05s', '2h05m',
    '1d03h'."""
    s = math.ceil(span.total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 86400}d{s % 86400 // 3600:02d}h"


def short_error(error: str) -> str:
    """The reason in a stored error, for a summary: its first line's last part, without errno.
    "couldn't connect to 192.168.1.176:502: [Errno 113] No route to host" -> 'No route to host'."""
    lines = error.strip().splitlines()
    text = lines[0].rsplit(": ", 1)[-1] if lines else error
    return re.sub(r"^\[Errno -?\d+\]\s*", "", text)[:80]


@dataclass(frozen=True)
class Series:
    """One time series in a table: its good rows' and failed rows' time columns, the condition picking its rows
    (on %(key)s), and the interval its good rows normally come at."""

    table: str
    label: str  # what tells it apart within the table: "battery 1", "15 min"; "" if it's the only one
    interval: datetime.timedelta
    time: str  # the good rows' time column
    failed_time: str  # the failed rows' (their `time` can be NULL)
    where: str = "TRUE"  # trusted SQL: this series' rows, using %(key)s
    key: object = None
    daylight_only: bool = False

    @property
    def name(self) -> str:
        """'readings, plant (unit 247)', 'radar_frames'."""
        return f"{self.table}, {self.label}" if self.label else self.table


@dataclass
class Gap:
    """A stretch with no good rows: from the last good row before it to the next one (or the window's end, then
    open-ended), with the failed attempts stored inside it."""

    start: datetime.datetime
    end: datetime.datetime
    open_ended: bool = False
    failed: int = 0
    error: str | None = None  # the commonest short_error of those attempts

    @property
    def length(self) -> datetime.timedelta:
        """How long it lasted (so far, if open-ended)."""
        return self.end - self.start


@dataclass
class SeriesGaps:
    """A series' gaps in a window, the min gap they're over, and how many good rows the window had."""

    series: Series
    min_gap: datetime.timedelta
    rows: int
    gaps: list[Gap] = field(default_factory=list)


def find(
    times: list[datetime.datetime],
    failed: list[tuple[datetime.datetime, str]],
    min_gap: datetime.timedelta,
    end: datetime.datetime,
    measure: Callable[[datetime.datetime, datetime.datetime], datetime.timedelta] | None = None,
) -> list[Gap]:
    """The gaps in a series: `times` are its good rows' times, sorted (the latest before the window first, if any),
    `failed` its failed attempts (time, error), `end` the window's end. measure(a, b) says how much of [a, b] counts
    (default all of it; daylight_between for a daylight-only series). Nothing to measure from without good rows."""
    gaps: list[Gap] = []
    if not times:
        return gaps
    spans = [(a, b, False) for a, b in zip(times, times[1:])] + [(times[-1], end, True)]
    for a, b, open_ended in spans:
        length = measure(a, b) if measure else b - a
        if length > min_gap:
            errors = [short_error(e) for t, e in failed if a < t < b]
            common = collections.Counter(errors).most_common(1)
            gaps.append(Gap(a, b, open_ended, len(errors), common[0][0] if common else None))
    return gaps


def daylight_between(a: datetime.datetime, b: datetime.datetime, lat: float, lon: float) -> datetime.timedelta:
    """How much of [a, b] is between sunrise and sunset at (lat, lon) (all of a day where the sun doesn't set)."""
    total = datetime.timedelta(0)
    day = a.astimezone(datetime.UTC).date() - datetime.timedelta(days=1)
    while day <= b.astimezone(datetime.UTC).date():
        times = sun_rise_set(day, lat, lon)
        midnight = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.UTC)
        rise, sunset = times if times else (midnight, midnight + datetime.timedelta(days=1))
        overlap = min(b, sunset) - max(a, rise)
        if overlap > datetime.timedelta(0):
            total += overlap
        day += datetime.timedelta(days=1)
    return total


def default_series(conn: psycopg.Connection, config: Config, poll_interval: float) -> list[Series]:
    """The series to check, for the tables this database has: readings at `poll_interval` seconds, a heater series
    per pack (the config's cloud.batteries, else those in the table), Open-Meteo per resolution, radar, and the
    satellite if the config has a site and poll.satellite is on."""
    have = {t for t in TABLES if conn.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0] is not None}
    series = []
    if "readings" in have:
        interval = datetime.timedelta(seconds=poll_interval)
        label = f"plant (unit {PLANT_UNIT_ID})"
        series.append(
            Series("readings", label, interval, "started_at", "started_at", "unit_id = %(key)s", PLANT_UNIT_ID)
        )
    if "battery_heater" in have:
        packs = [i + 1 for i in range(len(config.cloud.batteries))] if config.cloud and config.cloud.batteries else []
        packs = packs or [b for (b,) in conn.execute("SELECT DISTINCT battery FROM battery_heater ORDER BY 1")]
        slow = config.commands.get("poll", {}).get("heater", {}).get("slow_interval", heater.BASE_INTERVAL)
        every = datetime.timedelta(seconds=slow * max(len(packs), 1))  # the packs take turns
        series += [
            Series("battery_heater", f"battery {b}", every, "polled_at", "polled_at", "battery = %(key)s", b)
            for b in packs
        ]
    if "open_meteo_weather" in have:
        series += [
            Series(
                "open_meteo_weather",
                f"{r} min",
                datetime.timedelta(minutes=r),
                "valid_at",
                "fetched_at",
                "resolution_minutes = %(key)s",
                r,
            )
            for r in sorted(weather.RESOLUTIONS)
        ]
    if "radar_frames" in have:
        series.append(
            Series("radar_frames", "", datetime.timedelta(seconds=weather.RADAR_INTERVAL), "frame_at", "fetched_at")
        )
    if "satellite_shade" in have and config.site is not None and config.commands.get("poll", {}).get("satellite"):
        series.append(Series("satellite_shade", "", SATELLITE_INTERVAL, "scan_at", "fetched_at", daylight_only=True))
    return series


def series_gaps(
    conn: psycopg.Connection,
    series: Series,
    start: datetime.datetime,
    end: datetime.datetime,
    min_gap: datetime.timedelta,
    site: Site | None = None,
) -> SeriesGaps:
    """One series' gaps in [start, end): its good rows there, plus the latest before `start` to measure from."""
    names = {
        "t": sql.Identifier(series.time),
        "ft": sql.Identifier(series.failed_time),
        "table": sql.Identifier(series.table),
        "where": sql.SQL(series.where),
    }
    good = sql.SQL(
        "SELECT {t} FROM {table} WHERE error IS NULL AND {where} AND {t} >= %(start)s AND {t} < %(end)s "
        "UNION ALL SELECT max({t}) FROM {table} WHERE error IS NULL AND {where} AND {t} < %(start)s"
    ).format(**names)
    failed = sql.SQL(
        "SELECT {ft}, error FROM {table} WHERE error IS NOT NULL AND {where} AND {ft} >= %(from)s AND {ft} < %(end)s"
    ).format(**names)
    params = {"start": start, "end": end, "key": series.key}
    with db.errors_as(f"finding gaps in {series.name}"):
        times = sorted(t for (t,) in conn.execute(good, params) if t is not None)
        params["from"] = times[0] if times else start
        attempts = [(t, e) for t, e in conn.execute(failed, params)]
    rows = sum(1 for t in times if t >= start)

    def daylight(a: datetime.datetime, b: datetime.datetime) -> datetime.timedelta:
        """The daylight part of [a, b] at the site."""
        return daylight_between(a, b, site.latitude, site.longitude)

    measure = daylight if series.daylight_only and site is not None else None
    return SeriesGaps(series, min_gap, rows, find(times, attempts, min_gap, end, measure))


def find_gaps(
    conn: psycopg.Connection,
    config: Config,
    start: datetime.datetime,
    end: datetime.datetime,
    poll_interval: float,
    tables: list[str] | None = None,
    min_gaps: dict[str, datetime.timedelta] | None = None,
) -> list[SeriesGaps]:
    """Every series' gaps in [start, end), for `tables` (default all but daily_energy, which missing_days covers),
    each over its min gap: min_gaps[table] if given, else MIN_GAP_FACTOR x its interval."""
    min_gaps = min_gaps or {}
    out = []
    for series in default_series(conn, config, poll_interval):
        if tables is not None and series.table not in tables:
            continue
        min_gap = min_gaps.get(series.table, MIN_GAP_FACTOR * series.interval)
        out.append(series_gaps(conn, series, start, end, min_gap, config.site))
    return out


def missing_days(
    conn: psycopg.Connection, start: datetime.datetime, end: datetime.datetime, tz: datetime.tzinfo
) -> list[tuple[datetime.date, datetime.date]]:
    """The finished local days in [start, end) with no daily_energy row, as (first, last) runs. Days before the
    table's first row don't count (nothing was logged yet), nor does today (not finished)."""
    if conn.execute("SELECT to_regclass('daily_energy')").fetchone()[0] is None:
        return []
    with db.errors_as("finding missing days"):
        stored = {d for (d,) in conn.execute("SELECT day FROM daily_energy")}
    if not stored:
        return []
    first = max(min(stored), start.astimezone(tz).date())
    last = min(end.astimezone(tz).date(), datetime.datetime.now(tz).date()) - datetime.timedelta(days=1)
    runs: list[tuple[datetime.date, datetime.date]] = []
    day = first
    while day <= last:
        if day not in stored:
            if runs and runs[-1][1] == day - datetime.timedelta(days=1):
                runs[-1] = (runs[-1][0], day)
            else:
                runs.append((day, day))
        day += datetime.timedelta(days=1)
    return runs
