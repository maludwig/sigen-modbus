"""The daily_energy table (migration 0006): each finished local day's energy,
stored once, so a month's or a year's is a single SUM. The numbers are the
dashboard's daily page's (sigen.charts.day_energy): the totals and every
flow, before the page leaves the small ones out of its drawing.

A day is finished once a plant reading after its local midnight is stored:
its totals run to the first reading after it (the lifetime counters'
increase, by this server's clock: sigen.charts.day_energy). update() stores
the finished days not stored yet, or recomputes a range of them; `sigen db
daily` runs it, and so does `sigen poll` every CHECK_EVERY seconds (a
background job), which stores each day soon after it ends and catches up on
any that ended while it wasn't running. Recomputing a day replaces its row.
"""

import datetime
from collections.abc import Callable

import psycopg

from sigen import charts, db
from sigen.config import Array, Site

# How a day is computed (sigen.charts.day_energy): bump it when that changes.
# 1: the device's daily counters for solar, house, battery; 2: lifetime counters only
METHOD = 2
CHECK_EVERY = 600  # seconds between poll's checks for a newly finished day
TOTALS = {
    "solar": "solar_kwh",
    "load": "load_kwh",
    "battery_charged": "battery_charged_kwh",
    "battery_discharged": "battery_discharged_kwh",
    "grid_imported": "grid_imported_kwh",
    "grid_exported": "grid_exported_kwh",
}
FLOWS = {
    ("Solar", "Load"): "solar_to_load_kwh",
    ("Solar", "Battery"): "solar_to_battery_kwh",
    ("Solar", "Grid"): "solar_to_grid_kwh",
    ("Battery", "Load"): "battery_to_load_kwh",
    ("Battery", "Grid"): "battery_to_grid_kwh",
    ("Grid", "Load"): "grid_to_load_kwh",
    ("Grid", "Battery"): "grid_to_battery_kwh",
}
PLANT = 247


def finished_days(conn: psycopg.Connection, tz: datetime.tzinfo) -> list[datetime.date]:
    """Every local day from the first successful plant reading's to the one before the last's: each has a reading
    after its midnight."""
    first, last = conn.execute(
        "SELECT min(started_at), max(started_at) FROM readings WHERE unit_id = %s AND error IS NULL", (PLANT,)
    ).fetchone()
    if first is None:
        return []
    day, last_day = first.astimezone(tz).date(), last.astimezone(tz).date()
    return [day + datetime.timedelta(days=k) for k in range((last_day - day).days)]


def store_day(
    conn: psycopg.Connection,
    day: datetime.date,
    tz: datetime.tzinfo,
    site: Site | None = None,
    array: Array | None = None,
) -> dict | None:
    """Compute `day` and store it (replacing its row, if any). Returns the row stored, or None if the day has no
    plant readings. With `site` and `array`, also store the day's clear-sky model energy (clear_sky_kwh)."""
    start = datetime.datetime.combine(day, datetime.time(), tz)
    end = start + datetime.timedelta(days=1)  # wall-clock arithmetic: the next local midnight, DST or not
    with db.errors_as(f"computing the energy of {day}"):
        e = charts.day_energy(conn, start, end)
    if e is None:
        return None
    clear_sky = None
    if site is not None and array is not None:
        with db.errors_as(f"computing the clear-sky model of {day}"):
            clear_sky = charts.day_clear_sky_kwh(conn, site, array, start, end)
    row = {
        "day": day,
        "timezone": str(tz),
        **{column: e.totals.get(key) for key, column in TOTALS.items()},
        **{column: e.flows[flow] for flow, column in FLOWS.items()},
        "first_reading": e.first,
        "last_reading": e.last,
        "covered_hours": e.covered_hours,
        "clear_sky_kwh": clear_sky,
        "method": METHOD,
    }
    columns = list(row)
    with db.errors_as(f"storing the energy of {day}"):
        conn.execute(
            f"INSERT INTO daily_energy ({', '.join(columns)}) "
            f"VALUES ({', '.join(f'%({c})s' for c in columns)}) ON CONFLICT (day) DO UPDATE SET "
            + ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c != "day")
            + ", computed_at = now()",
            row,
        )
    return row


def describe(row: dict) -> str:
    """One line for the log about a row store_day() returned: kWh with 2 decimals, "-" for a counter not read."""
    kwh = lambda column: "-" if row[column] is None else f"{row[column]:.2f}"
    return (
        f"{row['day']}: solar {kwh('solar_kwh')}/{kwh('clear_sky_kwh')} clear-sky, "
        f"house {kwh('load_kwh')}, grid in {kwh('grid_imported_kwh')} / "
        f"out {kwh('grid_exported_kwh')}, battery in {kwh('battery_charged_kwh')} / out "
        f"{kwh('battery_discharged_kwh')} kWh ({row['covered_hours']:.1f} h of readings)"
    )


def update(
    conn: psycopg.Connection,
    tz: datetime.tzinfo,
    first: datetime.date | None = None,
    last: datetime.date | None = None,
    site: Site | None = None,
    array: Array | None = None,
    log: Callable[[str], None] = lambda msg: None,
) -> list[datetime.date]:
    """Store finished days. Without `first` and `last`: those not stored yet. With either: every finished day in
    [first, last], recomputed. With `site` and `array`, each day's clear-sky model energy too. Returns the days
    stored; logs one line each."""
    with db.errors_as("finding the days to store"):
        days = finished_days(conn, tz)
        if first is None and last is None:
            stored = {d for d, in conn.execute("SELECT day FROM daily_energy")}
            days = [d for d in days if d not in stored]
        else:
            days = [d for d in days if (first is None or d >= first) and (last is None or d <= last)]
    done = []
    for day in days:
        row = store_day(conn, day, tz, site, array)
        if row is not None:
            done.append(day)
            log(f"daily energy {describe(row)}")
    return done


def all_time_energy(conn: psycopg.Connection) -> "charts.DayEnergy | None":
    """Every finished day in daily_energy added up, as one charts.DayEnergy: the totals and the flows summed, the
    first and last readings spanned, covered_hours summed -- for an all-time Sankey. None if the table is empty.
    (The table stores each day once so this is a single SUM; see the module docstring.) A total summed to NULL
    because no stored day read that counter becomes 0.0."""
    sums = ", ".join(f"SUM({c})" for c in (*TOTALS.values(), *FLOWS.values()))
    count, first, last, covered, *values = conn.execute(
        "SELECT count(*), min(first_reading), max(last_reading), COALESCE(SUM(covered_hours), 0), "
        f"{sums} FROM daily_energy"
    ).fetchone()
    if not count:
        return None
    n = len(TOTALS)
    totals = {key: float(v or 0.0) for key, v in zip(TOTALS, values[:n])}
    flows = {flow: float(v or 0.0) for flow, v in zip(FLOWS, values[n:])}
    return charts.DayEnergy(totals, flows, first, last, float(covered))
