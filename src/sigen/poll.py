"""`sigen poll`: read every register on a schedule and store each poll in the database.

Read-only towards the device. Each poll takes one reading per unit (the plant, and each
inverter in the config), sharing a poll_id. Each reading is all or nothing: either every
register of that unit is stored, or its readings row records the error and has no values.

    connection lost / timeout / device busy   -> error row, keep polling
    any other Modbus exception                 -> error row, then stop (exit 4): probably our bug
    database unreachable                       -> warning, poll not stored, keep polling
    database rejects the data                  -> stop (exit 10): probably our bug

With `rediscover` (the CLI builds it from devices.inverter.mac; Windows and Linux), after
`rediscover_after` connection failures in a row it asks where the device is now, and
carries on at the new IP. readings.ip_addr records the IP each poll actually used.

A poll the device got wrong (sigen.glitches.judge: total load 0 while Running, or PV 0 while the
strings carry power) is stored like any other, with the verdict in readings.glitch, and the next
poll comes GLITCH_RETRY_DELAY later instead of on the schedule, up to GLITCH_RETRIES times in a
row: an ordinary poll in every way (its own poll_id, every unit, counted by --count), then back to
the schedule. A sure glitch is always flagged; a suspect only when a retry follows, which replaces
it (the last attempt is kept as the interval's reading).
"""

import datetime
import sys
import time
from dataclasses import dataclass, field
from collections.abc import Callable

import psycopg

from sigen import db, glitches, weather
from sigen.block_reader import BlockReader
from sigen.errors import ConnectionFailed, DatabaseError, ExitCode, ModbusError, SigenError
from sigen.registers import REGISTERS_BY_DEVICE

TRANSIENT_MODBUS_CODES = {6}  # "device busy": worth another try next interval
GLITCH_RETRIES = 2  # extra polls in a row after a glitched poll
GLITCH_RETRY_DELAY = (
    1.0  # seconds: the glitch lasted under ~12 s (2026-10-15: the Pi's readings 6 s either side were right)
)


def now_utc() -> datetime.datetime:
    """The current time, timezone-aware in UTC: the stamp on every reading."""
    return datetime.datetime.now(datetime.UTC)


@dataclass
class PollResult:
    """One unit's reading in a poll: `values` as [(address, stored value)]
    (empty on failure), or `error`. `fatal`: a Modbus exception that should
    stop polling; `connection_failed`: the connection failed, dropped or timed
    out (counts towards rediscovery)."""

    started_at: datetime.datetime
    ended_at: datetime.datetime
    values: list[tuple[int, int]] = field(default_factory=list)
    error: str | None = None
    fatal: bool = False
    connection_failed: bool = False


def poll_once(reader: BlockReader) -> PollResult:
    """One strict snapshot: every register, or an error. The reader opens a fresh connection."""
    started = now_utc()
    try:
        readings = reader.read_all()
        ended = now_utc()
        return PollResult(started, ended, db.values_for(readings))
    except ConnectionFailed as e:
        return PollResult(started, now_utc(), error=str(e), connection_failed=True)
    except ModbusError as e:
        return PollResult(started, now_utc(), error=str(e), fatal=e.code not in TRANSIENT_MODBUS_CODES)


def poll_verdict(results: dict[int, "PollResult"], units: dict[int, str]) -> glitches.Verdict | None:
    """sigen.glitches' verdict on this poll, from its units' readings (failed ones left out); None if it looks right."""
    readings = {}
    for unit, result in results.items():
        device = units.get(unit)
        if device is not None and result.error is None and device not in readings:
            readings[device] = result.values
    return glitches.judge(readings)


def seconds_to_boundary(interval: float, wall_now: float, offset: float = 0.0) -> float:
    """Seconds until the wall clock next reaches a multiple of `interval` (e.g. the next minute), plus `offset`
    seconds (0 <= offset < interval): interval 15, offset 7 polls at :07, :22, :37, :52."""
    return interval - ((wall_now - offset) % interval)


def run(
    *,
    connector_for: Callable[[str, int], Callable],
    ip: str,
    units: dict[int, str],
    db_url: str,
    interval: float,
    count: int | None = None,
    offset: float = 0.0,
    rediscover: Callable[[str], str | None] | None = None,
    rediscover_after: int = 3,
    background: weather.Background | None = None,
    watcher: "sigen.alerts.Watcher | None" = None,
    cloud_heater: "sigen.heater.HeaterPoller | None" = None,
    satellite: "sigen.satellite.Filler | None" = None,
    glitch_retries: int = GLITCH_RETRIES,
    glitch_retry_delay: float = GLITCH_RETRY_DELAY,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall: Callable[[], float] = time.time,
) -> int:
    """Poll until Ctrl+C, or `count` polls. The first poll is immediate; later ones line up
    with the wall clock (on the minute for --interval 60), `offset` seconds after it: a second poller
    of the same device, offset, doesn't read it at the same instant. Waiting uses the monotonic clock,
    so a system clock adjustment can't make it skip or double up.

    units maps each Modbus unit ID to its device type ("plant" or "inverter"); every poll
    takes one reading per unit, sharing a poll_id. connector_for(ip, unit) gives a
    ModbusClient factory. rediscover(ip) returns the device's current IP (the same one if
    it's still there), or None if it can't be found.

    background (a sigen.weather.Background, for --weather / --radar) is started once the
    schema checks out and stopped as soon as polling ends; its fetches don't count as polls.
    watcher (a sigen.alerts.Watcher, for --alerts) loads its history then too, sees every poll
    after it's stored, and gets a few seconds at the end to send what's queued. cloud_heater (a
    sigen.heater.HeaterPoller, with a "cloud" config section) logs each battery pack's heating
    status from the cloud on its own gated cadence, fed the PV and temperature of each poll
    through observe(). satellite (a sigen.satellite.Filler, for --satellite) stores NOAA's cloud
    shade in a thread of its own. None of these can stop the polling.

    After a glitched poll (poll_verdict), the next poll is `glitch_retry_delay` seconds later
    rather than on the schedule, up to `glitch_retries` in a row, and only if that's before the
    next scheduled poll anyway. Each is a whole poll, counted towards `count`. The verdict is
    stored in readings.glitch when it's sure, or when a retry follows (a suspect the retry
    replaces); the watcher is told, and leaves flagged polls out."""

    def readers_for(address: str) -> dict[int, BlockReader]:
        return {
            unit: BlockReader(connector_for(address, unit), REGISTERS_BY_DEVICE[device], strict=True)
            for unit, device in units.items()
        }

    readers = readers_for(ip)
    conn = db.connect(db_url)
    polls = stored = failed = 0
    connection_failures = 0
    try:
        db.check_ready(conn)
        if watcher is not None:
            watcher.start(conn, units, now_utc())
        if background is not None:
            background.start()
        if cloud_heater is not None:
            cloud_heater.start()
        if satellite is not None:
            satellite.start()
        deadline = monotonic()  # the next scheduled poll
        retry_at: float | None = None  # a glitch retry, sooner than that
        retries = 0  # glitch retries in a row so far
        while count is None or polls < count:
            retrying = retry_at is not None
            delay = (retry_at if retrying else deadline) - monotonic()
            if delay > 0:
                sleep(delay)
            polls += 1
            results = {unit: poll_once(reader) for unit, reader in readers.items()}
            verdict = poll_verdict(results, units)
            # The next scheduled poll, before storing this one: whether a retry fits decides what's stored.
            if polls == 1:
                deadline = monotonic() + seconds_to_boundary(interval, wall(), offset)
            elif not retrying:
                deadline += interval
                skipped = 0
                while deadline <= monotonic():
                    deadline += interval
                    skipped += 1
                if skipped:
                    err(f"warning: a poll took longer than --interval; skipped {skipped}")
            will_retry = (
                verdict is not None and retries < glitch_retries and monotonic() + glitch_retry_delay < deadline
            )
            glitch = verdict.rule if verdict is not None and (verdict.sure or will_retry) else None
            conn, reading_ids = _store(conn, db_url, ip, results, err, glitch)
            stored += len(reading_ids)
            for unit, result in results.items():
                seconds = (result.ended_at - result.started_at).total_seconds()
                stamp = f"{result.started_at:%Y-%m-%d %H:%M:%S.%f}Z"
                where = f"unit {unit}: " + (f"reading {reading_ids[unit]}" if unit in reading_ids else "not stored")
                if result.error is None:
                    out(
                        f"{stamp}  {where}: {len(result.values)} registers in {seconds:.2f}s"
                        + (f" (glitch: {glitch})" if glitch else "")
                    )
                else:
                    failed += 1
                    err(f"{stamp}  {where}: FAILED after {seconds:.2f}s: {result.error}")
            if watcher is not None:
                watcher.observe(results, units, now_utc(), glitch=verdict if glitch else None)
            if cloud_heater is not None:
                cloud_heater.observe(results, units)
            if any(r.fatal for r in results.values()):
                err(
                    "Stopping: the device rejected a register that should exist. That's more likely "
                    "a bug in sigen's register map than a device problem."
                )
                return ExitCode.MODBUS
            all_unreachable = all(r.connection_failed for r in results.values())
            connection_failures = connection_failures + 1 if all_unreachable else 0
            if rediscover is not None and connection_failures >= rediscover_after:
                connection_failures = 0
                new_ip = _rediscover(rediscover, ip, err)
                if new_ip is not None and new_ip != ip:
                    ip = new_ip
                    readers = readers_for(ip)
            retry_at = None
            if will_retry:
                retries += 1
                retry_at = monotonic() + glitch_retry_delay
                err(
                    f"{'warning: the reading looks wrong' if verdict.sure else 'note: a suspect reading'} "
                    f"({verdict.why}); polling again in {glitch_retry_delay:g} s (retry {retries} of {glitch_retries})"
                )
            else:
                if verdict is not None:
                    err(
                        f"warning: the reading looks wrong ({verdict.why}); back to the schedule"
                        if verdict.sure
                        else f"note: a suspect reading ({verdict.why}); keeping it, back to the schedule"
                    )
                retries = 0
    except KeyboardInterrupt:
        out("")
    except DatabaseError as e:
        err(f"error: {e}")
        return ExitCode.DATABASE
    finally:
        if background is not None:
            background.stop()
        if cloud_heater is not None:
            cloud_heater.stop()
        if satellite is not None:
            satellite.stop()
        if watcher is not None:
            watcher.stop()
        if polls:
            out(f"{polls} poll(s): {stored} stored, {failed} failed")
        if conn is not None:
            conn.close()
    return ExitCode.OK


def _rediscover(rediscover: Callable[[str], str | None], ip: str, err: Callable[[str], None]) -> str | None:
    """Ask `rediscover` where the device is now, after repeated connection
    failures at `ip`, and say what it found. Returns its answer: the new IP,
    the same one, or None if it's not on the network or the lookup failed (it
    runs during an outage, when the network itself may be down)."""
    err(f"{ip} keeps failing to connect; looking the device up by its MAC…")
    try:
        new_ip = rediscover(ip)
    except (SigenError, OSError) as e:
        err(f"  the lookup failed ({e}); will keep trying the same IP")
        return None
    if new_ip is None:
        err("  not found on the network; will keep trying the same IP")
    elif new_ip == ip:
        err(f"  it's still at {ip} (the MAC answers there), but Modbus isn't; will keep trying")
    else:
        err(f"  found it at {new_ip}; polling there from now on (update devices.inverter.ip in the config)")
    return new_ip


def _store(
    conn: psycopg.Connection | None,
    db_url: str,
    ip: str,
    results: dict[int, PollResult],
    err: Callable[[str], None],
    glitch: str | None = None,
) -> tuple[psycopg.Connection | None, dict[int, int]]:
    """Store one poll: a reading per unit, sharing a new poll_id (and `glitch`, if any). Returns (connection to use
    next time, {unit: reading id} for the readings stored).
    An unreachable database is survivable; a rejected insert is re-raised (a bug)."""
    if conn is None or conn.closed:
        try:
            conn = db.connect(db_url)
        except DatabaseError as e:
            err(f"warning: database unavailable, this poll isn't stored: {e}")
            return None, {}
    reading_ids: dict[int, int] = {}
    try:
        poll_id = db.next_poll_id(conn)
        for unit, result in results.items():
            reading_ids[unit] = db.insert_reading(
                conn,
                ip=ip,
                unit_id=unit,
                poll_id=poll_id,
                started_at=result.started_at,
                ended_at=result.ended_at,
                values=result.values,
                error=result.error,
                glitch=glitch,
            )
    except DatabaseError as e:
        if not e.transient:
            raise
        err(f"warning: database unavailable, this poll isn't (fully) stored: {e}")
        conn.close()
        return None, reading_ids
    return conn, reading_ids
