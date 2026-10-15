"""`sigen dashboard`: a small web page of charts drawn from the database on each request (sigen.charts).

    /                       the page (templates/dashboard.html): links to windows, one image per chart
    /chart/<name>.png       one chart for the window in the query string
    /daily?day=2026-10-11   a day's totals and a Sankey diagram of where its energy went (templates/daily.html,
                            drawn on a canvas by static/my_sankey.js: no libraries, no internet needed)
    /uplot_dashboard        the charts being rebuilt in the browser with uPlot (templates/uplot_dashboard.html), each
                            from /data/<name>.json: the numbers, topped up live with ?since= (placeholders so far for
                            the charts without one)
    /night                  /uplot_dashboard for today as it looks before sunrise (the string charts' night sky)

Windows (query string, local time in the config's site timezone, else this computer's):
    (none)                  today, midnight to now
    ?day=2026-10-11         that local day
    ?hours=6                the last N hours (1 to 744)
A window that reaches now reloads every 5 minutes. Read-only: each request uses its own connection in a read-only
session. Failures are drawn into the chart image (database unreachable, bad window), never a traceback.

Satellite: with a site and a config file, opening the page starts `sigen satellite` for the window (up to
MAX_FETCH long) as a separate process and doesn't wait for it: its output goes to <cube dir>/satellite.log, and it
refuses to run (exit 7) while another run for that cube directory is going. At most one start per window every
FETCH_EVERY, and none while the last one this dashboard started is still running. The charts draw whatever cubes
exist when they're drawn (sigen satellite swaps each one in whole).
"""
import datetime
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, render_template, request, url_for

from sigen import charts, db
from sigen.config import Config
from sigen.errors import DatabaseError

REFRESH_SECONDS = 300
MAX_HOURS = 24 * 31
CHARTS = {                     # name -> (title on the page, what it needs)
    "string_voltage": "String voltage, every reading",
    "string_power": "String power, every reading, with each string's clear-sky model dashed (config array); "
                    "underneath, the radar's rain or snow at the site and NOAA's satellite cloud",
    "load_energy": "Energy by load (smart loads and Other)",
    "load_counters": "Smart-load energy since the window started (the counters: exact energy; slope = power)",
    "load_snapshots": "Smart-load power (5-minute mean; every reading faint behind)",
    "battery_temps": "Battery temperatures (left axis) and the inverter's (red, right axis, 5-minute mean)",
    "battery_power": "Battery charge / discharge and PV power (5-minute means; every reading faint behind), and the "
                     "SOC with its reserve and charge cut-off settings (blue, right axis)",
}
QUICK = [("Today", {}), ("6 h", {"hours": "6"}), ("24 h", {"hours": "24"}), ("3 days", {"hours": "72"})]
FETCH_EVERY = datetime.timedelta(minutes=10)     # NOAA's scan interval
MAX_FETCH = datetime.timedelta(days=3)           # longer windows don't start a fetch (a day is ~1 minute, ~210 MB)
SATELLITE_LOG = "satellite.log"
UPLOT = "https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot"        # .iife.min.js and .min.css (MIT, ~50 KB)
LIVE_EVERY = 15                 # seconds between the uPlot charts' checks for newer readings (a poll every 15 s)
AIR_MARGIN = datetime.timedelta(hours=2)         # a top-up's Open-Meteo rows: stored minutes to an hour after their time
SOC_MARGIN = datetime.timedelta(minutes=1)       # a top-up's plant readings: the same poll's can be a moment later
SKY_MARGIN = datetime.timedelta(hours=1)         # a top-up's clear-sky model: Open-Meteo's weather comes in late
MODEL_AFTER_SUNSET = datetime.timedelta(minutes=30)   # the clear-sky model stops this long after sunset (model_end)
STRIPS_EVERY = 300              # seconds between the page's fetches of the string-power strips (radar 6 min, NOAA 10)


class BadWindow(ValueError):
    pass


def iso_utc(t: datetime.datetime) -> str:
    """ISO 8601 in UTC to the microsecond, with a Z: 2026-10-14T03:01:30.997599Z."""
    return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def model_end(end: datetime.datetime, site, after: datetime.timedelta = None) -> datetime.datetime:
    """Where the clear-sky model can stop: `end`, or if the sun had set by then (on `end`'s local day, or before
    sunrise, the day before), `after` (MODEL_AFTER_SUNSET) past that sunset: after that it's 0 till morning. `end`
    where the sun doesn't set or rise."""
    from sigen.goes import sun_rise_set
    after = MODEL_AFTER_SUNSET if after is None else after
    day = end.astimezone(ZoneInfo(site.timezone)).date()
    today = sun_rise_set(day, site.latitude, site.longitude)
    if today is None:
        return end
    rise, sunset = today
    if end < rise:                                       # small hours: the last sunset was yesterday's
        yesterday = sun_rise_set(day - datetime.timedelta(days=1), site.latitude, site.longitude)
        if yesterday is None:
            return end
        sunset = yesterday[1]
    return min(end, sunset + after)


def satellite_command(config: Config, start: datetime.datetime, end: datetime.datetime, tz) -> List[str]:
    """`sigen satellite` for [start, end) (local times, whole minutes: the end rounded up), with this config."""
    end = end.astimezone(tz)
    whole = end.replace(second=0, microsecond=0)
    end = whole if whole == end else whole + datetime.timedelta(minutes=1)
    return [sys.executable, "-m", "sigen", "satellite", "--start", f"{start.astimezone(tz):%Y-%m-%d %H:%M}",
            "--end", f"{end:%Y-%m-%d %H:%M}", "--config", str(config.path), "--cube-dir", str(config.cube_dir())]


def launch_detached(command: List[str], log: Path):
    """Start `command` in its own process (no console window, not in this one's Ctrl+C group), output to `log`;
    returns the Popen without waiting."""
    log.parent.mkdir(parents=True, exist_ok=True)
    flags = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
             if sys.platform == "win32" else {"start_new_session": True})
    with open(log, "w", encoding="utf-8") as out:          # the child has its own handle
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONUNBUFFERED": "1"}, **flags)


def parse_window(args, tz, now: Optional[datetime.datetime] = None) -> Tuple[datetime.datetime, datetime.datetime,
                                                                            bool, str]:
    """(start, end, reaches now, description) from the query string."""
    now = now or datetime.datetime.now(tz)
    day, hours = args.get("day"), args.get("hours")
    if day and hours:
        raise BadWindow("give day or hours, not both")
    if hours:
        try:
            n = float(hours)
        except ValueError:
            raise BadWindow(f"hours={hours!r} isn't a number")
        if not 0 < n <= MAX_HOURS:
            raise BadWindow(f"hours must be between 0 and {MAX_HOURS}")
        return now - datetime.timedelta(hours=n), now, True, f"last {n:g} h"
    if day:
        try:
            date = datetime.date.fromisoformat(day)
        except ValueError:
            raise BadWindow(f"day={day!r} isn't a date like 2026-10-11")
    else:
        date = now.date()
    start = datetime.datetime(date.year, date.month, date.day, tzinfo=tz)
    end = start + datetime.timedelta(days=1)
    live = start <= now < end
    return start, min(end, now) if live else end, live, f"{date:%A %Y-%m-%d}" + (" (so far)" if live else "")


def create_app(config: Config, launcher: Optional[Callable] = launch_detached) -> Flask:
    """The dashboard. `launcher(command, log path)` starts the satellite fetch (None: never; tests pass a fake)."""
    url = config.require_database_url()
    tz = ZoneInfo(config.site.timezone) if config.site else datetime.datetime.now().astimezone().tzinfo
    names = config.smart_load_names
    app = Flask(__name__)
    # Each chart's size in inches: what it's drawn at (errors too), and the box the page reserves for it.
    sizes = {"string_voltage": charts.strings_size(),
             "string_power": charts.strings_size(precip=True, satellite=config.site is not None),
             "load_energy": charts.ENERGY_SIZE, "load_counters": charts.ENERGY_SINCE_START_SIZE,
             "load_snapshots": charts.SNAPSHOTS_SIZE, "battery_temps": charts.BATTERY_TEMPS_SIZE,
             "battery_power": charts.BATTERY_POWER_SIZE}
    fetch_lock = threading.Lock()
    fetches: Dict[str, object] = {"process": None}       # the last process started
    last_start: Dict[Tuple[datetime.date, datetime.date], float] = {}   # window's local days -> time.monotonic()

    def fetch_satellite(start, end):
        """Start `sigen satellite` for the window, unless one of ours is running or this window had one lately."""
        if launcher is None or config.site is None or config.path is None or end - start > MAX_FETCH:
            return
        key = (start.astimezone(tz).date(), end.astimezone(tz).date())
        with fetch_lock:
            process = fetches["process"]
            if process is not None and process.poll() is None:
                return
            now = time.monotonic()
            if key in last_start and now - last_start[key] < FETCH_EVERY.total_seconds():
                return
            last_start[key] = now
            try:
                fetches["process"] = launcher(satellite_command(config, start, end, tz),
                                              config.cube_dir() / SATELLITE_LOG)
            except OSError as e:
                print(f"warning: couldn't start sigen satellite: {e}", file=sys.stderr)

    def satellite_series(start, end):
        """The satellite strips' data for the window: whatever cubes exist now (none: every strip hatched)."""
        if config.site is None:
            return None
        from sigen import goes_shade
        try:
            return goes_shade.shade_between(config.cube_dir(), config.site.latitude, config.site.longitude,
                                            config.site.timezone, start, end)
        except (OSError, KeyError, ValueError) as e:      # a damaged cube: draw the strips empty, say why
            print(f"warning: reading the satellite cubes: {e}", file=sys.stderr)
            return goes_shade.ShadeSeries()

    def draw(name, start, end):
        conn = db.connect(url)
        try:
            with db.errors_as(f"drawing {name}"):
                conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                if name == "string_voltage":
                    strings = charts.string_readings(conn, start, end)
                    return charts.strings_figure(strings, tz, "voltage", xlim=charts.live_limits(strings))
                if name == "string_power":
                    strings = charts.string_readings(conn, start, end)
                    sky = faces = None
                    if config.array is not None and config.site is not None:
                        sky = charts.clear_sky_strings(conn, config.site, config.array, start, end)
                        faces = {n: s.face for n, s in config.array.strings.items()}
                    return charts.strings_figure(strings, tz, "power", sky, faces, xlim=charts.live_limits(strings),
                                                 precip=charts.precipitation(conn, start, end),
                                                 satellite=satellite_series(start, end))
                if name == "load_energy":
                    return charts.energy_figure(charts.energy_by_load(conn, names, start, end), tz)
                if name == "load_counters":
                    return charts.energy_since_start_figure(charts.smart_load_energy(conn, names, start, end), tz)
                if name == "battery_temps":
                    series = charts.inverter_series(conn, [*charts.BATTERY_TEMPS, charts.INVERTER_TEMP], start, end)
                    return charts.battery_temps_figure(series, charts.air_temperatures(conn, start, end), tz)
                if name == "battery_power":
                    series = charts.inverter_series(conn, [charts.BATTERY_POWER, charts.PV_POWER], start, end)
                    soc = charts.unit_series(conn, charts.PLANT, [charts.SOC, *charts.SOC_LIMITS], start, end)
                    return charts.battery_power_figure(series, tz, soc)
                return charts.snapshots_figure(charts.load_power_snapshots(conn, names, start, end), tz)
        finally:
            conn.close()

    @app.get("/")
    def page():
        query = {k: v for k, v in request.args.items() if k in ("day", "hours")}
        try:
            start, end, live, description = parse_window(request.args, tz)
            error = None
            fetch_satellite(start, end)                  # in the background: this page doesn't wait for it
        except BadWindow as e:
            start = end = None
            live, description, error = False, "", str(e)
        nav = day_nav(start, live) if start is not None and "hours" not in query else []
        daily_query = {"day": f"{start.date()}"} if start is not None and "hours" not in query and not live else {}
        return render_template("dashboard.html", charts=CHARTS, query=query, quick=QUICK, nav=nav, live=live,
                               pixels={name: charts.pixels(size) for name, size in sizes.items()},
                               refresh=REFRESH_SECONDS, description=description, error=error, tz=str(tz),
                               drawn=datetime.datetime.now(tz), daily_query=daily_query)

    def day_nav(start, live):
        """The previous / next day links (none after today)."""
        before, after = start.date() - datetime.timedelta(days=1), start.date() + datetime.timedelta(days=1)
        return [(f"◀ {before:%m-%d}", {"day": f"{before}"})] + ([] if live else [(f"{after:%m-%d} ▶", {"day": f"{after}"})])

    @app.get("/daily")
    def daily():
        """A day's totals (the device's counters) and a Sankey diagram of where the energy went (static/my_sankey.js)."""
        query = {k: v for k, v in request.args.items() if k == "day"}
        energy = sankey = None
        try:
            start, end, live, description = parse_window(query, tz)
            error = None
        except BadWindow as e:
            start, live, description, error = None, False, "", f"Bad day: {e}"
        if error is None:
            conn = None
            try:
                conn = db.connect(url)
                with db.errors_as("reading the day's energy"):
                    conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                    energy = charts.day_energy(conn, start, end)
            except DatabaseError as e:
                error = f"database: {e}"
            finally:
                if conn is not None:
                    conn.close()
            if energy is not None:
                sankey = charts.sankey(energy)
        return render_template("daily.html", query=query, nav=day_nav(start, live) if start is not None else [],
                               live=live, refresh=REFRESH_SECONDS, description=description, error=error, tz=str(tz),
                               energy=energy, totals=charts.DAY_TOTALS, sankey=sankey,
                               span=(f"{energy.first.astimezone(tz):%H:%M} to {energy.last.astimezone(tz):%H:%M}"
                                     if energy is not None else ""),
                               drawn=datetime.datetime.now(tz))

    @app.get("/uplot")
    def uplot_trial():
        """A trial of uPlot (charts drawn in the browser from numbers): Other's power over the last hour, from
        /data/other.json, kept in the page and topped up with the newer readings every few seconds."""
        return render_template("uplot.html", uplot=UPLOT, every=5)

    # ---------- the data endpoints: numbers for charts drawn in the browser ----------
    # Each answers JSON {"t": [ISO 8601 UTC, to the microsecond: "2026-10-14T03:01:30.997599Z"], …the values…} for
    # the window: ?day= / ?hours= as for the charts (else its default), or ?since=<a "t" it returned> for only the
    # readings after it (what a page that already has the earlier ones asks for). The browser shows the times in its
    # own timezone. A page must send back the exact string it got: parsed into a JavaScript Date it loses its
    # microseconds, and that reading would come again. {"error": …} with 400 for a bad window, 503 if the database
    # fails.

    def data_window(default) -> Tuple[datetime.datetime, datetime.datetime, Optional[datetime.datetime]]:
        """(start, end, since or None), all UTC, from a data endpoint's query string; BadWindow if it's no good. (A
        ?day= is the site's local day; after that, everything is UTC until the browser shows it in its own zone.)"""
        since = request.args.get("since")
        utc = datetime.timezone.utc
        if since is None:
            start, end, _, _ = parse_window(request.args if request.args else default, tz)
            return start.astimezone(utc), end.astimezone(utc), None
        try:
            start = datetime.datetime.fromisoformat(since[:-1] + "+00:00" if since.endswith("Z") else since)
        except ValueError:                # (Python 3.9's fromisoformat doesn't take "Z" itself)
            raise BadWindow(f"since={since!r} isn't an ISO 8601 time like 2026-10-14T03:01:30.997599Z")
        if start.tzinfo is None:
            raise BadWindow(f"since={since!r} needs its timezone, e.g. Z for UTC")
        start, end = start.astimezone(utc), datetime.datetime.now(utc)
        if not datetime.timedelta(0) <= end - start <= datetime.timedelta(hours=MAX_HOURS):
            raise BadWindow(f"since must be within the last {MAX_HOURS} hours")
        return start, end, start

    def data_response(what: str, read: Callable, default):
        """`read(conn, start, end, since)` → the JSON body, in a read-only session; errors as JSON too."""
        try:
            start, end, since = data_window(default)
        except BadWindow as e:
            return {"error": f"bad window: {e}"}, 400
        conn = None
        try:
            conn = db.connect(url)
            with db.errors_as(what):
                conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                body = read(conn, start, end, since)
        except DatabaseError as e:
            return {"error": f"database: {e}"}, 503
        finally:
            if conn is not None:
                conn.close()
        return Response(json.dumps(body), mimetype="application/json", headers={"Cache-Control": "no-store"})

    def after(times, since) -> List[int]:
        """The indexes of the readings to send: all, or with ?since= only those strictly after it (none twice)."""
        return [k for k, t in enumerate(times) if since is None or t > since]

    def columns(times, series: Dict[str, List[float]], since, digits: int):
        """{"t": [ISO…], label: […]}-style pieces for the readings after `since`: (the times, {label: values})."""
        keep = after(times, since)
        return ([iso_utc(times[k]) for k in keep],
                {label: [round(values[k], digits) for k in keep] for label, values in series.items()})

    def string_voltage_data(conn, start, end, since):
        """{"t", "volts": {"1": [V, …], … "4": […]}}: each string's voltage at every inverter reading."""
        s = charts.string_readings(conn, start, end)
        t, volts = columns(s.times, {str(n): s.volts[n] for n in charts.STRINGS}, since, 1)
        return {"t": t, "volts": volts}

    def string_power_data(conn, start, end, since):
        """{"t", "kw": {"1"…"4": [kW, …]}, "faces": {"1": "house SE", …}, "sky": {"t", "kw": {"1"…"4"}}}: each
        string's power (V × I) at every inverter reading, and with the config's array and site, its clear-sky model
        every minute up to now, or once the sun has set, MODEL_AFTER_SUNSET after it (model_end) ("sky" null without
        them). A top-up sends the last SKY_MARGIN of the model again: its weather comes in late (the page keeps the
        minutes it doesn't have)."""
        s = charts.string_readings(conn, start, end)
        t, kw = columns(s.times, {str(n): s.kw(n) for n in charts.STRINGS}, since, 3)
        body = {"t": t, "kw": kw, "faces": {}, "sky": None}
        if config.array is not None and config.site is not None:
            body["faces"] = {str(n): string.face for n, string in config.array.strings.items()}
            sky_start, sky_end = start - SKY_MARGIN if since else start, model_end(end, config.site)
            body["sky"] = {"t": [], "kw": {}}
            if sky_end > sky_start:
                sky = charts.clear_sky_strings(conn, config.site, config.array, sky_start, sky_end)
                body["sky"] = {"t": [iso_utc(x.to_pydatetime()) for x in sky.index],
                               "kw": {str(n): [None if math.isnan(v) else round(float(v), 3) for v in sky[n]]
                                      for n in sky.columns}}
        return body

    def string_power_strips_data(conn, start, end, since):
        """{"strips": [{"label", "starts": [ISO…], "length" (seconds), "colours": ["#rrggbb", or null: no data,
        hatched]}], "notes": [how to read them]}: the strips under the string-power chart, as on the PNG one: the
        radar (charts.precipitation_strip) and with a site the satellite (satellite_strips), for the whole window
        every time. They change every few minutes, so the page fetches them again rather than topping them up. Each
        fetch also starts `sigen satellite` for the window (fetch_satellite's limits apply), so a page left open keeps
        getting new scans: the PNG page does that by reloading, the uPlot page never reloads."""
        from matplotlib.colors import to_hex
        fetch_satellite(start, end)                      # in the background: these are the cubes there are now
        strip, note = charts.precipitation_strip(charts.precipitation(conn, start, end))
        strips, notes = [strip], [note]
        satellite = satellite_series(start, end)
        if satellite is not None:
            more, note = charts.satellite_strips(satellite)
            strips, notes = strips + more, notes + [note]
        return {"strips": [{"label": s.label, "starts": [iso_utc(x) for x in s.starts],
                            "length": s.length.total_seconds(),
                            "colours": [None if c is None else to_hex(c) for c in s.colours]} for s in strips],
                "notes": notes}

    def load_energy_data(conn, start, end, since):
        """{"first", "last" (the window's first and last plant readings), "house" (kWh), "bars": {load: kWh}}: the
        whole window every time (two readings' counters: nothing to top up). Empty with fewer than two readings."""
        e = charts.energy_by_load(conn, names, start, end)
        if e is None:
            return {"first": None, "last": None, "house": None, "bars": {}}
        return {"first": iso_utc(e.first[1]), "last": iso_utc(e.last[1]), "house": e.house, "bars": e.bars}

    def load_counters_data(conn, start, end, since):
        """{"t", "kwh": {load: [lifetime kWh, …]}}: the counters at every plant reading (thinned past
        MAX_ENERGY_POINTS), for the slots that are named or moved, and Other. The page takes each one's first value
        off. A top-up leaves out a slot that didn't move: its counter is where it was."""
        c = charts.smart_load_energy(conn, names, start, end, fewest=1)
        if c is None:
            return {"t": [], "kwh": {}}
        t, kwh = columns(c.times, c.series, since, 2)
        return {"t": t, "kwh": kwh}

    def load_snapshots_data(conn, start, end, since):
        """{"t", "kw": {load: [kW, …]}}: each plant reading's power per slot (named or ever above 0) and Other. A
        top-up leaves out a slot that stayed at 0."""
        s = charts.load_power_snapshots(conn, names, start, end)
        t, kw = columns(s.times, s.series, since, 3)
        return {"t": t, "kw": kw}

    def battery_temps_data(conn, start, end, since):
        """{"t", "c": {"coolest cluster", "warmest cluster", "average cell", "inverter internal": [°C, …]},
        "air": {"t", "c"}}: every inverter reading, and Open-Meteo's 15-minute air temperature. A row of those is
        stored a few minutes after its time, so a top-up sends the last AIR_MARGIN of them again (the page keeps the
        ones it doesn't have)."""
        s = charts.inverter_series(conn, [*charts.BATTERY_TEMPS, charts.INVERTER_TEMP], start, end)
        t, c = columns(s.times, {**{label: s.values[name] for name, label in charts.BATTERY_TEMPS.items()},
                                 "inverter internal": s.values[charts.INVERTER_TEMP]}, since, 1)
        air = charts.air_temperatures(conn, start - AIR_MARGIN if since else start, end)
        return {"t": t, "c": c, "air": {"t": [iso_utc(at) for at, _ in air], "c": [round(v, 1) for _, v in air]}}

    def battery_power_data(conn, start, end, since):
        """{"t", "kw": {"battery" (>0 charging), "pv": [kW, …]}, "soc": {"t", "pct": {"SOC", "backup reserve",
        "charge cut-off": [%…]}}}: every inverter reading, and every plant reading's SOC and settings (a few
        milliseconds off the inverter's, so a top-up sends the last SOC_MARGIN of them again)."""
        s = charts.inverter_series(conn, [charts.BATTERY_POWER, charts.PV_POWER], start, end)
        t, kw = columns(s.times, {"battery": s.values[charts.BATTERY_POWER], "pv": s.values[charts.PV_POWER]},
                        since, 3)
        soc = charts.unit_series(conn, charts.PLANT, [charts.SOC, *charts.SOC_LIMITS],
                                 start - SOC_MARGIN if since else start, end)
        soc_t, pct = columns(soc.times, {"SOC": soc.values[charts.SOC],
                                         **{label: soc.values[name] for name, label in charts.SOC_LIMITS.items()}},
                             None, 1)
        return {"t": t, "kw": kw, "soc": {"t": soc_t, "pct": pct}}

    DATA = {                                                    # chart name -> (what it's doing, read)
        "string_voltage": ("reading the string voltages", string_voltage_data),
        "string_power": ("reading the string power", string_power_data),
        "string_power_strips": ("reading the radar and satellite strips", string_power_strips_data),
        "load_energy": ("reading the energy by load", load_energy_data),
        "load_counters": ("reading the smart-load counters", load_counters_data),
        "load_snapshots": ("reading the smart-load power", load_snapshots_data),
        "battery_temps": ("reading the battery temperatures", battery_temps_data),
        "battery_power": ("reading the battery and PV power", battery_power_data),
    }

    @app.get("/uplot_dashboard")
    def uplot_dashboard():
        """Where the dashboard is being rebuilt in uPlot: each chart's title, over its chart where it has a data
        endpoint (DATA), else a placeholder sin(x). Same windows as the dashboard; a window that reaches now is topped
        up every LIVE_EVERY seconds."""
        return uplot_page(request.args)

    @app.get("/night")
    def night():
        """The uPlot dashboard for today as it looks before sunrise, whatever the time: its sunrise is tomorrow's, so
        the string charts show their night sky (to see it by day)."""
        return uplot_page({}, sunrise_in_days=1)

    def uplot_page(args, sunrise_in_days: int = 0):
        query = {k: v for k, v in args.items() if k in ("day", "hours")}
        try:
            start, end, live, description = parse_window(args, tz)
            error = None
            fetch_satellite(start, end)                  # for the strips, as the dashboard's page does
        except BadWindow as e:
            live, description, error = False, "", str(e)
        live = live and error is None
        sunrise = None                                  # "today" before sunrise: the string charts show the night
        if live and not query and config.site is not None:
            from sigen.goes import sun_rise_set
            day = datetime.datetime.now(tz).date() + datetime.timedelta(days=sunrise_in_days)
            times = sun_rise_set(day, config.site.latitude, config.site.longitude)
            sunrise = times[0].timestamp() if times else None
        if sunrise is None and sunrise_in_days:         # /night with no site (or no sunrise there): any time ahead
            sunrise = time.time() + 6 * 3600
        env = {                                         # for static/live_charts.js
            "data": {name: url_for("chart_data", name=name, **query) for name in DATA},
            "live": live,                               # top the charts up every `every` seconds
            "every": LIVE_EVERY,
            "strips_every": STRIPS_EVERY,
            "keep": float(query["hours"]) * 3600 if "hours" in query and live else None,   # a sliding window (s)
            "today": live and not query,                # load again after midnight, for the new day
            "sunrise": sunrise,                         # today's, unix seconds (null: not "today", or no site)
            "live_volts": charts.LIVE_VOLTS,
        }
        return render_template("uplot_dashboard.html", uplot=UPLOT, charts=CHARTS, query=query, quick=QUICK,
                               description=description, error=error, live=live, every=LIVE_EVERY, env=env)

    @app.get("/data/<name>.json")
    def chart_data(name):
        """A chart's numbers (DATA), for today by default."""
        if name not in DATA:
            abort(404)
        what, read = DATA[name]
        return data_response(what, read, {})

    @app.get("/data/other.json")
    def other_json():
        """Other (the house's power minus all 24 smart loads, as on the smart-load chart) at every plant reading, for
        the last hour by default: {"t", "kw": […]}."""
        def read(conn, start, end, since):
            s = charts.load_power_snapshots(conn, names, start, end)
            kw = s.series.get(charts.OTHER, [])
            keep = after(s.times[:len(kw)], since)
            return {"t": [iso_utc(s.times[k]) for k in keep], "kw": [round(kw[k], 3) for k in keep]}
        return data_response("reading Other", read, {"hours": "1"})

    @app.get("/chart/<name>.png")
    def chart(name):
        if name not in CHARTS:
            abort(404)
        try:
            start, end, _, _ = parse_window(request.args, tz)
            fig = draw(name, start, end)
            status = 200
        except BadWindow as e:
            fig, status = charts.message_figure(f"bad window: {e}", *sizes[name]), 400
        except DatabaseError as e:
            fig, status = charts.message_figure(f"database: {e}", *sizes[name]), 503
        return Response(charts.png(fig), status=status, mimetype="image/png",
                        headers={"Cache-Control": "no-store"})

    return app
