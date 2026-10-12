"""`sigen dashboard`: a small web page of charts drawn from the database on each request (sigen.charts).

    /                       the page (templates/dashboard.html): links to windows, one image per chart
    /chart/<name>.png       one chart for the window in the query string

Windows (query string, local time in the config's site timezone, else this computer's):
    (none)                  today, midnight to now
    ?day=2026-10-11         that local day
    ?hours=6                the last N hours (1 to 744)
A window that reaches now reloads every 5 minutes. Read-only: each request uses its own connection in a read-only
session. Failures are drawn into the chart image (database unreachable, bad window), never a traceback.
"""
import datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, render_template, request

from sigen import charts, db
from sigen.config import Config
from sigen.errors import DatabaseError

REFRESH_SECONDS = 300
MAX_HOURS = 24 * 31
CHARTS = {                     # name -> (title on the page, what it needs)
    "string_voltage": "String voltage, every reading",
    "string_power": "String power, every reading, with each string's clear-sky model dashed (config array)",
    "load_energy": "Energy by load (smart loads and Other)",
    "load_counters": "Smart-load energy since the window started (the counters: exact energy; slope = power)",
    "load_snapshots": "Smart-load power (5-minute mean; every reading faint behind)",
    "battery_temps": "Battery temperatures (left axis) and the inverter's (red, right axis, 5-minute mean)",
    "battery_power": "Battery charge / discharge and PV power (5-minute means; every reading faint behind), and the "
                     "SOC with its reserve and charge cut-off settings (blue, right axis)",
}
QUICK = [("Today", {}), ("6 h", {"hours": "6"}), ("24 h", {"hours": "24"}), ("3 days", {"hours": "72"})]


class BadWindow(ValueError):
    pass


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


def create_app(config: Config) -> Flask:
    url = config.require_database_url()
    tz = ZoneInfo(config.site.timezone) if config.site else datetime.datetime.now().astimezone().tzinfo
    names = config.smart_load_names
    app = Flask(__name__)

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
                    return charts.strings_figure(strings, tz, "power", sky, faces, xlim=charts.live_limits(strings))
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
        except BadWindow as e:
            start = end = None
            live, description, error = False, "", str(e)
        nav = []                                 # previous / next day, when the window is a day
        if start is not None and "hours" not in query:
            before, after = start.date() - datetime.timedelta(days=1), start.date() + datetime.timedelta(days=1)
            nav.append((f"◀ {before:%m-%d}", {"day": f"{before}"}))
            if not live:
                nav.append((f"{after:%m-%d} ▶", {"day": f"{after}"}))
        return render_template("dashboard.html", charts=CHARTS, query=query, quick=QUICK, nav=nav, live=live,
                               refresh=REFRESH_SECONDS, description=description, error=error, tz=str(tz),
                               drawn=datetime.datetime.now(tz))

    @app.get("/chart/<name>.png")
    def chart(name):
        if name not in CHARTS:
            abort(404)
        try:
            start, end, _, _ = parse_window(request.args, tz)
            fig = draw(name, start, end)
            status = 200
        except BadWindow as e:
            fig, status = charts.message_figure(f"bad window: {e}"), 400
        except DatabaseError as e:
            fig, status = charts.message_figure(f"database: {e}"), 503
        return Response(charts.png(fig), status=status, mimetype="image/png",
                        headers={"Cache-Control": "no-store"})

    return app
