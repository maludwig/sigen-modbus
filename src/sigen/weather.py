"""Weather at the site: Open-Meteo (a weather model) and Environment Canada's radar (an observation).

    fetch_open_meteo   one Open-Meteo request at one resolution (60 or 15 minutes) -> the row for
                       the latest period that ended at or before the fetch
    fetch_radar        GeoMet's radar frames at the site: rain rate, snow rate, coverage
    Background         runs jobs on their own schedules in a daemon thread (for `poll --weather`)

Every fetch gives rows even when it fails: a failed row carries the whole HTTP response text,
or what went wrong if nothing answered, so gaps in the tables are explained. The tables are
described in sigen/migrations/0003_weather.sql.

Standard library only (urllib). Both services are free and need no key: Open-Meteo is CC BY 4.0;
GeoMet data is under Environment and Climate Change Canada's Data Server End-use Licence.
"""

import datetime
import json
import re
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import psycopg

from sigen import db
from sigen.config import Site
from sigen.errors import DatabaseError
from sigen.fetch import Response, http_get  # re-exported: weather.Response / weather.http_get (tests patch it)
from sigen.weather_rows import RadarRow, WeatherRow  # re-exported: weather.WeatherRow / weather.RadarRow

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
GEOMET_URL = "https://geo.weather.gc.ca/geomet"

# Column name (= Open-Meteo variable) -> unit, in Open-Meteo's default units. Every one of them
# is available both hourly and every 15 minutes.
OPEN_METEO_VARIABLES: dict[str, str] = {
    "shortwave_radiation": "W/m²",
    "direct_radiation": "W/m²",
    "diffuse_radiation": "W/m²",
    "direct_normal_irradiance": "W/m²",
    "sunshine_duration": "s",
    "cloud_cover": "%",
    "cloud_cover_low": "%",
    "cloud_cover_mid": "%",
    "cloud_cover_high": "%",
    "temperature_2m": "°C",
    "relative_humidity_2m": "%",
    "wind_speed_10m": "km/h",
    "wind_gusts_10m": "km/h",
    "visibility": "m",
    "precipitation": "mm",
    "rain": "mm",
    "showers": "mm",
    "snowfall": "cm",
    "snow_depth": "m",
    "weather_code": "WMO",
    "freezing_level_height": "m",
    "precipitation_probability": "%",
    "is_day": "",
}
RESOLUTIONS = {60: "hourly", 15: "minutely_15"}  # resolution_minutes -> Open-Meteo's block name
PAST_DAYS, FORECAST_DAYS = 1, 3  # what each request returns (all of it is captured)

RADAR_LAYERS = {"rain": "RADAR_1KM_RRAI", "snow": "RADAR_1KM_RSNO"}  # mm/h and cm/h
COVERAGE_LAYER = "RADAR_COVERAGE_RRAI"
RADAR_BOX = 0.01  # degrees: the query box around the site, sampled at its centre


def now_utc() -> datetime.datetime:
    """The current time, aware UTC: the default `now` for the fetches."""
    return datetime.datetime.now(datetime.UTC)


# ---------- HTTP (sigen.fetch) ----------
Getter = Callable[[str, dict[str, object]], Response]


def save_capture(directory: Path, name: str, when: datetime.datetime, text: str) -> Path:
    """DIR/NAME_YYYY-MM-DDTHHMMSSZ.json (UTC); never overwrites (adds _2, _3, …)."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{name}_{when.astimezone(datetime.UTC):%Y-%m-%dT%H%M%S}Z"
    for n in range(1, 1000):
        path = directory / (f"{stem}.json" if n == 1 else f"{stem}_{n}.json")
        try:
            with path.open("x", encoding="utf-8", newline="\n") as f:
                f.write(text)
            return path
        except FileExistsError:
            continue
    raise FileExistsError(f"{directory / stem}.json and 998 numbered copies already exist")


# ---------- Open-Meteo (WeatherRow: sigen.weather_rows) ----------


def open_meteo_params(site: Site, resolution: int) -> dict[str, object]:
    """Open-Meteo query for the site: every variable at `resolution` minutes
    (60 or 15), past and forecast days, times as UTC unix seconds."""
    return {
        "latitude": site.latitude,
        "longitude": site.longitude,
        RESOLUTIONS[resolution]: ",".join(OPEN_METEO_VARIABLES),
        "past_days": PAST_DAYS,
        "forecast_days": FORECAST_DAYS,
        "timezone": "GMT",
        "timeformat": "unixtime",
    }


def _convert(name: str, value: Any) -> int | bool | float | None:
    """An Open-Meteo JSON value as its column's type: weather_code an int,
    is_day a bool, the rest floats; None (no data) stays None."""
    if value is None:
        return None
    if name == "weather_code":
        return int(value)
    if name == "is_day":
        return bool(value)
    return float(value)


def parse_open_meteo(response: Response, resolution: int, fetched_at: datetime.datetime) -> WeatherRow:
    """The row for the latest period ending at or before fetched_at, or an error row."""
    row = WeatherRow(fetched_at, resolution)
    if not response.ok:
        row.error = response.describe()
        return row
    try:
        block = json.loads(response.text)[RESOLUTIONS[resolution]]
        times = [int(t) for t in block["time"]]
        cutoff = fetched_at.timestamp()
        index = max((i for i, t in enumerate(times) if t <= cutoff), default=None)
        if index is None:
            row.error = (
                f"no {resolution}-minute period ends at or before {fetched_at:%Y-%m-%d %H:%M:%S}Z\n{response.text}"
            )
            return row
        row.values = {
            name: _convert(name, block[name][index]) if name in block else None for name in OPEN_METEO_VARIABLES
        }
    except (ValueError, KeyError, TypeError, IndexError) as e:
        row.values = {}
        row.error = f"unexpected response ({type(e).__name__}: {e})\n{response.text}"
        return row
    row.valid_at = datetime.datetime.fromtimestamp(times[index], datetime.UTC)
    return row


def fetch_open_meteo(
    site: Site, resolution: int, get: Getter | None = None, now: Callable[[], datetime.datetime] = now_utc
) -> tuple[WeatherRow, Response]:
    """One Open-Meteo request: (the parsed row, the raw response to capture).
    `get` and `now` stand in for http_get and the clock in tests."""
    fetched_at = now()
    response = (get or http_get)(OPEN_METEO_URL, open_meteo_params(site, resolution))
    return parse_open_meteo(response, resolution, fetched_at), response


def format_open_meteo(row: WeatherRow) -> str:
    """For `sigen weather`: the period, then every variable with its unit on a
    line of its own; or one FAILED line."""
    head = f"Open-Meteo {row.resolution_minutes} min, fetched {row.fetched_at:%Y-%m-%d %H:%M:%S}Z"
    if row.error:
        return f"{head}: FAILED: {_first_line(row.error)}"
    lines = [f"{head}: period ending {row.valid_at:%Y-%m-%d %H:%M}Z"]
    lines += [
        f"  {name:<26} {'-' if value is None else value} {OPEN_METEO_VARIABLES[name]}".rstrip()
        for name, value in row.values.items()
    ]
    return "\n".join(lines)


def _first_line(text: str, limit: int = 200) -> str:
    """An error text's first non-blank line (two when the first is "HTTP nnn",
    so the body's first line shows too), cut to `limit` characters with "…"."""
    lines = [line for line in text.splitlines() if line.strip()]
    first = " ".join(lines[:2]) if lines and lines[0].startswith("HTTP ") else (lines[0] if lines else "")
    return first[:limit] + ("…" if len(first) > limit else "")


# ---------- radar (RadarRow: sigen.weather_rows) ----------

TIME_DIMENSION = re.compile(r'<Dimension name="time"[^>]*>\s*([^/<\s]+)/([^/<\s]+)/PT(\d+)M\s*</Dimension>')


def parse_frames(response: Response) -> list[datetime.datetime]:
    """Frame times from a one-layer GetCapabilities ("start/end/PT6M"), oldest first."""
    if not response.ok:
        raise ValueError(response.describe())
    match = TIME_DIMENSION.search(response.text)
    if match is None:
        raise ValueError(f"no time dimension in the capabilities\n{response.text}")
    start, end = (
        datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC) for t in match.group(1, 2)
    )
    step = datetime.timedelta(minutes=int(match.group(3)))
    frames = []
    while start <= end:
        frames.append(start)
        start += step
    return frames


def feature_info_params(site: Site, layer: str, frame: datetime.datetime) -> dict[str, object]:
    """WMS GetFeatureInfo query for one layer at one frame: a 3x3 image of the
    box around the site (lat,lon, as WMS 1.3.0 wants), read at its centre."""
    lat, lon = site.latitude, site.longitude
    box = f"{lat - RADAR_BOX},{lon - RADAR_BOX},{lat + RADAR_BOX},{lon + RADAR_BOX}"
    return {
        "service": "WMS",
        "version": "1.3.0",
        "request": "GetFeatureInfo",
        "layers": layer,
        "query_layers": layer,
        "crs": "EPSG:4326",
        "bbox": box,
        "width": 3,
        "height": 3,
        "i": 1,
        "j": 1,
        "info_format": "application/json",
        "time": f"{frame:%Y-%m-%dT%H:%M:%SZ}",
    }


def parse_feature(response: Response, layer: str) -> tuple[float | None, str | None] | bool:
    """(value, class) for a radar layer; True/False for the coverage layer (a vector layer:
    a polygon comes back only where the radars reach)."""
    if not response.ok:
        raise ValueError(response.describe())
    try:
        features = json.loads(response.text)["features"]
        if layer == COVERAGE_LAYER:
            return bool(features)
        if features:
            properties = features[0]["properties"]
            value = properties.get("value")
            return (None if value is None else float(value)), properties.get("class")
    except (ValueError, KeyError, TypeError, IndexError) as e:
        raise ValueError(f"unexpected response ({type(e).__name__}: {e})\n{response.text}") from e
    raise ValueError("no feature at the site")  # a well-formed answer that doesn't cover the site


def fetch_radar(
    site: Site,
    skip: Iterable[datetime.datetime] = (),
    latest_only: bool = False,
    get: Getter | None = None,
    now: Callable[[], datetime.datetime] = now_utc,
    capture: Path | None = None,
) -> list[RadarRow]:
    """Rows for GeoMet's frames, newest first: just the newest with latest_only, otherwise
    every frame not in `skip`. The first failure ends the round with an error row (the rest
    are tried next time), so an outage gives one row per round, not one per frame."""
    get = get or http_get
    fetched_at = now()
    params = {"service": "WMS", "version": "1.3.0", "request": "GetCapabilities", "layer": RADAR_LAYERS["rain"]}
    try:
        frames = parse_frames(get(GEOMET_URL, params))
    except ValueError as e:
        return [RadarRow(fetched_at, error=f"listing the radar frames failed: {e}")]
    skip = set(skip)
    todo = [f for f in reversed(frames) if f not in skip]
    rows = []
    for frame in todo[:1] if latest_only else todo:
        row = RadarRow(fetched_at, frame)
        try:
            for kind, layer in RADAR_LAYERS.items():
                response = get(GEOMET_URL, feature_info_params(site, layer, frame))
                if capture is not None:
                    save_capture(capture, f"radar_{kind}_{frame:%Y-%m-%dT%H%M}Z", fetched_at, response.text)
                value, cls = parse_feature(response, layer)
                setattr(row, f"{kind}_{'mm_h' if kind == 'rain' else 'cm_h'}", value)
                setattr(row, f"{kind}_class", cls)
            response = get(GEOMET_URL, feature_info_params(site, COVERAGE_LAYER, frame))
            if capture is not None:
                save_capture(capture, f"radar_coverage_{frame:%Y-%m-%dT%H%M}Z", fetched_at, response.text)
            row.covered = parse_feature(response, COVERAGE_LAYER)
        except ValueError as e:
            rows.append(RadarRow(fetched_at, frame, error=str(e)))
            break
        rows.append(row)
    return rows


def format_radar(row: RadarRow) -> str:
    """One line for `sigen radar` (and summarize_radar): the frame's rain and
    snow rates with their classes, flagged if radar doesn't cover the site."""
    head = f"radar frame {row.frame_at:%Y-%m-%d %H:%M}Z" if row.frame_at else "radar"
    if row.error:
        return f"{head}: FAILED: {_first_line(row.error)}"
    coverage = "" if row.covered else "  (site NOT covered by radar)"
    rain, snow = ("-" if v is None else f"{v:g}" for v in (row.rain_mm_h, row.snow_cm_h))
    return f"{head}: rain {rain} mm/h ({row.rain_class}), snow {snow} cm/h ({row.snow_class}){coverage}"


def summarize_open_meteo(row: WeatherRow) -> str:
    """One line, for poll's log."""
    head = f"Open-Meteo {row.resolution_minutes} min"
    if row.error:
        return f"{head}: FAILED: {_first_line(row.error)}"
    v = row.values
    return (
        f"{head}: period ending {row.valid_at:%Y-%m-%d %H:%M}Z: GHI {v['shortwave_radiation']} W/m², "
        f"cloud {v['cloud_cover']} %, {v['temperature_2m']} °C, precipitation {v['precipitation']} mm, "
        f"snowfall {v['snowfall']} cm, code {v['weather_code']}"
    )


# ---------- storing ----------
T = TypeVar("T")


class Store:
    """The database connection weather rows go through, reconnecting after it drops. An
    unreachable database costs the rows of that fetch (with a warning); a rejected insert is
    raised, since it's a bug."""

    def __init__(self, db_url: str, err: Callable[[str], None]) -> None:
        self._url = db_url
        self._err = err
        self._conn: psycopg.Connection | None = None

    def use(self, what: str, fn: Callable[[psycopg.Connection], T]) -> tuple[bool, T | None]:
        """(True, fn(conn)), or (False, None) if the database is unavailable."""
        if self._conn is None or self._conn.closed:
            try:
                self._conn = db.connect(self._url)
            except DatabaseError as e:
                self._err(f"warning: database unavailable, {what}: {e}")
                return False, None
        try:
            return True, fn(self._conn)
        except DatabaseError as e:
            if not e.transient:
                raise
            self._err(f"warning: database unavailable, {what}: {e}")
            self._conn.close()
            self._conn = None
            return False, None

    def close(self) -> None:
        """Close the connection, if open; a later use() reconnects."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def record_open_meteo(
    site: Site,
    resolution: int,
    store: Store | None = None,
    capture: Path | None = None,
    get: Getter | None = None,
) -> tuple[WeatherRow, bool]:
    """Fetch one resolution; save the whole response under `capture`; store the row.
    Returns (row, whether it was stored)."""
    row, response = fetch_open_meteo(site, resolution, get)
    if capture is not None:
        save_capture(capture, f"open-meteo_{resolution}min", row.fetched_at, response.text)
    if store is None:
        return row, False
    stored, _ = store.use("this Open-Meteo row isn't stored", lambda conn: db.insert_open_meteo(conn, row))
    return row, stored


RADAR_INTERVAL = 360  # seconds: GeoMet makes a frame every 6 minutes
RADAR_LOOKBACK = datetime.timedelta(hours=4)  # longer than GeoMet keeps frames


def record_radar(
    site: Site, store: Store | None = None, capture: Path | None = None, get: Getter | None = None
) -> tuple[list[RadarRow], int | None]:
    """Without a store: the newest frame. With one: every frame GeoMet has that isn't stored
    yet, stored. Returns (rows, how many good rows were newly stored: None if the database
    was unavailable, 0 without a store)."""
    if store is None:
        return fetch_radar(site, latest_only=True, get=get, capture=capture), 0
    ok, stored = store.use(
        "radar frames aren't stored", lambda conn: db.stored_radar_frames(conn, now_utc() - RADAR_LOOKBACK)
    )
    if not ok:
        return fetch_radar(site, latest_only=True, get=get, capture=capture), None
    rows = fetch_radar(site, skip=stored, get=get, capture=capture)
    ok, added = store.use("radar frames aren't stored", lambda conn: db.insert_radar_rows(conn, rows))
    return rows, added if ok else None


def summarize_radar(rows: Sequence[RadarRow], added: int | None) -> str:
    """One line, for poll's log."""
    good = [r for r in rows if not r.error]
    failed = [r for r in rows if r.error]
    text = "radar: not stored" if added is None else f"radar: {added} frame(s) stored"
    if good:
        text += f"; newest {format_radar(good[0])[len('radar '):]}"
    if failed:
        text += f"; FAILED: {_first_line(failed[0].error)}"
    return text


# ---------- background jobs ----------
@dataclass
class Job:
    """A Background job: run() every `interval` seconds, named in warnings."""

    name: str
    interval: float  # seconds; the first run is immediate
    run: Callable[[], None]


class Background:
    """Runs jobs on their own schedules, one at a time, in a daemon thread. stop() wakes it at
    once; a job in the middle of a request is abandoned (the thread is a daemon), so stopping
    never waits for the next scheduled run."""

    def __init__(
        self, jobs: Sequence[Job], err: Callable[[str], None], monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._jobs = list(jobs)
        self._err = err
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="sigen-weather", daemon=True)

    def start(self) -> None:
        """Start the thread; every job's first run is immediate."""
        self._thread.start()

    def stop(self, wait: float = 1.0) -> None:
        """Wake the thread and give a running job up to `wait` seconds to
        finish; a started job that's still running is left to the daemon."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(wait)

    def _loop(self) -> None:
        """The thread: run whichever job is due first, one at a time, logging
        its exceptions. A run that overran skips the missed slots instead of
        catching up."""
        due = {id(job): self._monotonic() for job in self._jobs}
        while self._jobs and not self._stop.is_set():
            job = min(self._jobs, key=lambda j: due[id(j)])
            delay = due[id(job)] - self._monotonic()
            if delay > 0 and self._stop.wait(delay):
                return
            try:
                job.run()
            except Exception as e:  # never let the weather stop the polling
                self._err(f"warning: {job.name} failed: {type(e).__name__}: {e}")
            due[id(job)] += job.interval
            while due[id(job)] <= self._monotonic():
                due[id(job)] += job.interval
