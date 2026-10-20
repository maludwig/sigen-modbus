"""NOAA GOES-West cloud shade over the site, straight into satellite_shade, with no files: sigen.goes cuts each
scan's box out of NOAA's bucket into memory, and sigen.goes_shade turns the three products' boxes into the strip
values (shade_boxes).

What to fetch comes from the bucket's listing and the table, never from a clock. A scan is READY once all three
products (ACMF, CODF, ACHAF) are listed for its scan start, and NEEDED if it's ready, in daylight, and has no good
row (nor MAX_FAILURES failed ones: a broken file isn't retried for ever). NOAA publishes the three about 10, 11 and
12.5 minutes after the scan starts (CODF now and then up to ~20), so a scan is fetched by the first run that sees all
three, and never with one missing: a row is never revised, so one stored early would lack its optical depth for good.

A run fetches the needed scans one at a time, newest first: the three boxes (~3 MB), the shade, one row, then a
short pause (PAUSE), so a catch-up trickles rather than hogging the network and the CPU. NOAA unreachable ends the
run with one failed row (scan_at NULL); a scan that fails on its own (a missing or broken file) gets a failed row with
its scan_at, and the run goes on. Listings are per UTC day folder (one request per product: a day is 144 keys), and
a day's listing is kept once it can't change any more (Listings).

One run at a time per database: a run holds a PostgreSQL advisory lock (LOCK: on the satellite_shade table, so each
schema has its own) on its connection, so a second one
(`sigen satellite`, a dashboard's, another poll) finds it taken and skips. The lock goes with the connection, so a
run that dies can't leave it stuck.

In `sigen poll` (Filler, with poll.satellite on): a daemon thread of its own runs every RUN_EVERY seconds, RUN_OFFSET
past the boundary, over the last LOOKBACK; each run stops at the next boundary, so a long catch-up re-plans with the
newest scans first.
"""

import collections
import datetime
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO

import aiohttp
import psycopg

from sigen import db, goes, goes_shade
from sigen.config import Site
from sigen.errors import ConnectionFailed, DatabaseError

PRODUCTS = goes.PRODUCTS  # all three, always
LOOKBACK = datetime.timedelta(days=7)  # how far back `sigen poll` catches up
RUN_EVERY = 600  # seconds: NOAA's full-disk scans
RUN_OFFSET = 270  # seconds past each boundary (:04:30, :14:30, …): the scan 14.5 min old usually has all three
PAUSE = 1.0  # seconds between scans
MAX_FAILURES = 3  # a scan that failed this many times on its own isn't tried again
LISTING_FINAL = datetime.timedelta(hours=1)  # a UTC day's listing made this long after the day ends is kept
LOCK = (0x5347_5341, "satellite_shade")  # db.try_lock's (kind, table): one run at a time per database
NETWORK_ERRORS = (ConnectionFailed, aiohttp.ClientError, ConnectionError, TimeoutError)  # NOAA unreachable

Lister = Callable[[str, str], list[tuple[str, int]]]
Opener = Callable[[str], BinaryIO]


def now_utc() -> datetime.datetime:
    """The current time, UTC."""
    return datetime.datetime.now(datetime.UTC)


def utc_days(start: datetime.datetime, end: datetime.datetime) -> list[datetime.date]:
    """The UTC dates [start, end) touches, oldest first."""
    day = start.astimezone(datetime.UTC).date()
    last = (end - datetime.timedelta(microseconds=1)).astimezone(datetime.UTC).date()
    days = []
    while day <= last:
        days.append(day)
        day += datetime.timedelta(days=1)
    return days


class Listings:
    """The scans in NOAA's bucket, per product and UTC day folder. A day's listing made LISTING_FINAL after that day
    ended is kept, since nothing more arrives in it; a younger one is asked for again each time."""

    def __init__(
        self,
        satellite: str = goes.DEFAULT_SATELLITE,
        sector: str = "F",
        lister: Lister | None = None,
        now: Callable[[], datetime.datetime] = now_utc,
    ) -> None:
        self.satellite, self.sector = satellite, sector
        self._lister = lister or goes.list_keys
        self._now = now
        self._kept: dict[tuple[str, datetime.date], dict[datetime.datetime, str]] = {}
        self.requests = 0  # listings asked for

    def scans(self, short: str, day: datetime.date) -> dict[datetime.datetime, str]:
        """{scan start: key} of product `short` ("ACM") in the UTC day folder `day`."""
        kept = self._kept.get((short, day))
        if kept is not None:
            return kept
        asked_at = self._now()
        self.requests += 1
        prefix = f"ABI-L2-{short}{self.sector}/{day:%Y}/{day.timetuple().tm_yday:03d}/"
        found = {goes.scan_start(key): key for key, _ in self._lister(self.satellite, prefix)}
        day_end = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.UTC) + datetime.timedelta(days=1)
        if asked_at >= day_end + LISTING_FINAL:
            self._kept[short, day] = found
        return found

    def ready(self, start: datetime.datetime, end: datetime.datetime) -> dict[datetime.datetime, dict[str, str]]:
        """{scan start: {product: key}} for every scan starting in [start, end) that all three products have."""
        out = {}
        for day in utc_days(start, end):
            per = {short: self.scans(short, day) for short in PRODUCTS}
            for t0 in set.intersection(*(set(scans) for scans in per.values())):
                if start <= t0 < end:
                    out[t0] = {short: per[short][t0] for short in PRODUCTS}
        return out


@dataclass
class Plan:
    """A run's work: how many daylight scans are ready, how many of those are stored or given up on, and the rest
    to fetch, newest first."""

    ready: int
    stored: int
    given_up: int
    todo: list[tuple[datetime.datetime, dict[str, str]]]  # (scan start, {product: key})


def plan(
    ready: dict[datetime.datetime, dict[str, str]],
    done: set[datetime.datetime],
    failures: collections.Counter[datetime.datetime],
    site: Site,
    daylight_minutes: float | None,
) -> Plan:
    """The Plan for the `ready` scans, given the scan_at of the good rows (`done`) and the failed rows per scan_at:
    only daylight ones (within `daylight_minutes` of sunrise..sunset; None: all)."""
    scans = {
        t0: keys
        for t0, keys in ready.items()
        if daylight_minutes is None
        or goes.in_daylight(t0, site.latitude, site.longitude, site.timezone, daylight_minutes)
    }
    stored = {t0 for t0 in scans if t0 + goes_shade.SAMPLE_DELAY in done}
    given_up = {t0 for t0 in scans if t0 not in stored and failures[t0 + goes_shade.SAMPLE_DELAY] >= MAX_FAILURES}
    todo = sorted(
        ((t0, keys) for t0, keys in scans.items() if t0 not in stored and t0 not in given_up),
        key=lambda scan: scan[0],
        reverse=True,
    )
    return Plan(len(scans), len(stored), len(given_up), todo)


def shade_scan(
    t0: datetime.datetime,
    keys: dict[str, str],
    site: Site,
    km: float = goes.DEFAULT_KM,
    satellite: str = goes.DEFAULT_SATELLITE,
    opener: Opener | None = None,
) -> tuple[goes_shade.ShadeSeries, int]:
    """(the one-row series of the scan that started at `t0`, bytes fetched), from its products' `keys`, each box
    fetched into memory."""
    boxes, fetched = {}, 0
    for short, key in keys.items():
        url = f"{goes.bucket_url(satellite)}/{key}"
        boxes[short], n_bytes = goes.fetch_box(url, site.latitude, site.longitude, km, opener=opener)
        fetched += n_bytes
    return goes_shade.shade_boxes(boxes, t0, site.latitude, site.longitude), fetched


@dataclass
class RunResult:
    """What fill() did."""

    plan: Plan | None = None  # None: it didn't get that far (the lock was taken, or the listing failed)
    locked_out: bool = False
    tried: int = 0  # scans of the plan it got to
    stored: int = 0
    failed: int = 0  # scans that failed on their own, each stored as a failed row
    fetched_bytes: int = 0
    newest: datetime.datetime | None = None  # the scan_at of the newest scan stored
    error: str | None = None  # NOAA unreachable: what ended the run (stored as a failed row)
    stopped: bool = False  # stop() said so before the plan was done

    @property
    def left(self) -> int:
        """Scans of the plan still to fetch."""
        return 0 if self.plan is None else len(self.plan.todo) - self.tried


def _sleep(seconds: float) -> bool:
    """fill()'s default pause: time.sleep, never asking to stop."""
    time.sleep(seconds)
    return False


def fill(
    conn: psycopg.Connection,
    listings: Listings,
    site: Site,
    start: datetime.datetime,
    end: datetime.datetime,
    *,
    km: float = goes.DEFAULT_KM,
    daylight_minutes: float | None = goes.DEFAULT_DAYLIGHT_MINUTES,
    opener: Opener | None = None,
    stop: Callable[[], bool] = lambda: False,
    wait: Callable[[float], bool] = _sleep,
    pause: float | None = None,
    log: Callable[[str], None] = lambda msg: None,
    now: Callable[[], datetime.datetime] = now_utc,
) -> RunResult:
    """Store every needed scan starting in [start, end), newest first, holding the advisory lock (locked_out if
    another run has it). stop() is asked before each scan; wait(pause) runs between them (default PAUSE), and True
    from it stops."""
    pause = PAUSE if pause is None else pause
    result = RunResult()
    if not db.try_lock(conn, *LOCK):
        result.locked_out = True
        return result
    try:
        try:
            ready = listings.ready(start, end)
        except NETWORK_ERRORS as e:
            return _failed_run(conn, result, f"listing NOAA's scans failed: {e}", now())
        done, failures = db.stored_satellite_scans(conn, start)
        result.plan = plan(ready, done, failures, site, daylight_minutes)
        for n, (t0, keys) in enumerate(result.plan.todo):
            if stop() or (n and wait(pause)):
                result.stopped = True
                break
            scan_at = t0 + goes_shade.SAMPLE_DELAY
            try:
                series, fetched = shade_scan(t0, keys, site, km, listings.satellite, opener)
            except NETWORK_ERRORS as e:
                return _failed_run(conn, result, f"reading the {scan_at:%Y-%m-%d %H:%M}Z scan failed: {e}", now())
            except Exception as e:  # this scan's own problem (a missing or broken file): the others may be fine
                result.tried += 1
                result.failed += 1
                error = f"the {scan_at:%Y-%m-%d %H:%M}Z scan: {type(e).__name__}: {e}"
                db.insert_satellite_shade(conn, now(), None, error=error, scan_at=scan_at)
                log(f"{scan_at:%Y-%m-%d %H:%M}Z: FAILED: {type(e).__name__}: {e}")
                continue
            result.tried += 1
            result.stored += db.insert_satellite_shade(conn, now(), series)
            result.fetched_bytes += fetched
            result.newest = max(result.newest or scan_at, scan_at)
            log(f"{scan_at:%Y-%m-%d %H:%M}Z: stored ({fetched / 1e6:.1f} MB fetched)")
        return result
    finally:
        try:
            db.unlock(conn, *LOCK)
        except DatabaseError:
            pass  # the connection is gone, and the lock with it


def _failed_run(conn: psycopg.Connection, result: RunResult, error: str, at: datetime.datetime) -> RunResult:
    """Store the run's failure (scan_at NULL) and note it in `result`, which it returns."""
    db.insert_satellite_shade(conn, at, None, error=error)
    result.error = error
    return result


def summarize(result: RunResult) -> str:
    """One line on a run, for poll's log and `sigen satellite`."""
    if result.locked_out:
        return "satellite: skipped, another run has the database's satellite lock"
    text = f"satellite: {result.stored} scan(s) stored"
    if result.newest is not None:
        text += f" (newest {result.newest:%Y-%m-%d %H:%M}Z, {result.fetched_bytes / 1e6:.0f} MB fetched)"
    if result.plan is not None:
        p = result.plan
        text += f"; {p.stored + result.stored} of {p.ready} daylight scans stored, {result.left} still to fetch"
        if p.given_up:
            text += f", {p.given_up} given up on (failed {MAX_FAILURES} times)"
    if result.failed:
        text += f"; {result.failed} FAILED"
    if result.error:
        text += f"; FAILED: {result.error}"
    return text


class Filler:
    """`sigen poll`'s satellite job, in a daemon thread of its own (a catch-up can take an hour; neither the polling
    nor the weather jobs wait for it). Its first run is at once; then one every RUN_EVERY seconds, RUN_OFFSET past
    the boundary, each over the last LOOKBACK and stopping at the next boundary. It never stops the polling: a failed
    run is a warning."""

    def __init__(
        self,
        site: Site,
        db_url: str,
        *,
        km: float = goes.DEFAULT_KM,
        listings: Listings | None = None,
        opener: Opener | None = None,
        out: Callable[[str], None] = print,
        err: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr),
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime.datetime] = now_utc,
    ) -> None:
        self._site, self._url, self._km = site, db_url, km
        self._listings = listings or Listings(now=now)
        self._opener = opener
        self._out, self._err = out, err
        self._wall, self._monotonic, self._now = wall, monotonic, now
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="sigen-satellite", daemon=True)

    def start(self) -> None:
        """Start the thread; the first run is at once."""
        self._thread.start()

    def stop(self, wait: float = 1.0) -> None:
        """Ask the thread to stop (between scans) and give it up to `wait` seconds; it's a daemon after that."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(wait)

    def first_deadline(self) -> float:
        """The monotonic time of the next boundary (RUN_OFFSET past a multiple of RUN_EVERY on the wall clock)."""
        return self._monotonic() + ((RUN_OFFSET - self._wall()) % RUN_EVERY or RUN_EVERY)

    def run_once(self, deadline: float) -> RunResult:
        """One run over the last LOOKBACK, stopping at `deadline` (monotonic) or at stop()."""
        conn = db.connect(self._url)
        try:
            end = self._now()
            return fill(
                conn,
                self._listings,
                self._site,
                end - LOOKBACK,
                end,
                km=self._km,
                opener=self._opener,
                stop=lambda: self._stop.is_set() or self._monotonic() >= deadline,
                wait=self._stop.wait,
                now=self._now,
            )
        finally:
            conn.close()

    def _loop(self) -> None:
        """The thread: run, log a line, wait for the next boundary (none if the run took up to it)."""
        deadline = self.first_deadline()
        while not self._stop.is_set():
            try:
                result = self.run_once(deadline)
                (self._err if result.error or result.failed else self._out)(summarize(result))
            except Exception as e:  # never let the satellite stop the polling
                self._err(f"warning: satellite failed: {type(e).__name__}: {e}")
            delay = deadline - self._monotonic()
            if delay > 0 and self._stop.wait(delay):
                return
            while deadline <= self._monotonic():
                deadline += RUN_EVERY
