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
"""
import datetime
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from sigen import db
from sigen.block_reader import BlockReader
from sigen.errors import ConnectionFailed, DatabaseError, ExitCode, ModbusError
from sigen.registers import REGISTERS_BY_DEVICE

TRANSIENT_MODBUS_CODES = {6}     # "device busy": worth another try next interval


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass
class PollResult:
    started_at: datetime.datetime
    ended_at: datetime.datetime
    values: List[Tuple[int, int]] = field(default_factory=list)
    error: Optional[str] = None
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


def seconds_to_boundary(interval: float, wall_now: float) -> float:
    """Seconds until the wall clock next reaches a multiple of `interval` (e.g. the next minute)."""
    return interval - (wall_now % interval)


def run(*, connector_for: Callable[[str, int], Callable], ip: str, units: Dict[int, str], db_url: str,
        interval: float, count: Optional[int] = None,
        rediscover: Optional[Callable[[str], Optional[str]]] = None, rediscover_after: int = 3,
        background=None, watcher=None,
        out=print, err=lambda msg: print(msg, file=sys.stderr),
        monotonic=time.monotonic, sleep=time.sleep, wall=time.time) -> int:
    """Poll until Ctrl+C, or `count` polls. The first poll is immediate; later ones line up
    with the wall clock (on the minute for --interval 60). Waiting uses the monotonic clock,
    so a system clock adjustment can't make it skip or double up.

    units maps each Modbus unit ID to its device type ("plant" or "inverter"); every poll
    takes one reading per unit, sharing a poll_id. connector_for(ip, unit) gives a
    ModbusClient factory. rediscover(ip) returns the device's current IP (the same one if
    it's still there), or None if it can't be found.

    background (a sigen.weather.Background, for --weather / --radar) is started once the
    schema checks out and stopped as soon as polling ends; its fetches don't count as polls.
    watcher (a sigen.alerts.Watcher, for --alerts) loads its history then too, sees every poll
    after it's stored, and gets a few seconds at the end to send what's queued. Neither can
    stop the polling."""
    def readers_for(address):
        return {unit: BlockReader(connector_for(address, unit), REGISTERS_BY_DEVICE[device], strict=True)
                for unit, device in units.items()}

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
        deadline = monotonic()
        while count is None or polls < count:
            delay = deadline - monotonic()
            if delay > 0:
                sleep(delay)
            polls += 1
            results = {unit: poll_once(reader) for unit, reader in readers.items()}
            conn, reading_ids = _store(conn, db_url, ip, results, err)
            stored += len(reading_ids)
            for unit, result in results.items():
                seconds = (result.ended_at - result.started_at).total_seconds()
                stamp = f"{result.started_at:%Y-%m-%d %H:%M:%S.%f}Z"
                where = f"unit {unit}: " + (f"reading {reading_ids[unit]}" if unit in reading_ids else "not stored")
                if result.error is None:
                    out(f"{stamp}  {where}: {len(result.values)} registers in {seconds:.2f}s")
                else:
                    failed += 1
                    err(f"{stamp}  {where}: FAILED after {seconds:.2f}s: {result.error}")
            if watcher is not None:
                watcher.observe(results, units, now_utc())
            if any(r.fatal for r in results.values()):
                err("Stopping: the device rejected a register that should exist. That's more likely "
                    "a bug in sigen's register map than a device problem.")
                return ExitCode.MODBUS
            all_unreachable = all(r.connection_failed for r in results.values())
            connection_failures = connection_failures + 1 if all_unreachable else 0
            if rediscover is not None and connection_failures >= rediscover_after:
                connection_failures = 0
                new_ip = _rediscover(rediscover, ip, err)
                if new_ip is not None and new_ip != ip:
                    ip = new_ip
                    readers = readers_for(ip)
            if polls == 1:
                deadline = monotonic() + seconds_to_boundary(interval, wall())
            else:
                deadline += interval
                skipped = 0
                while deadline <= monotonic():
                    deadline += interval
                    skipped += 1
                if skipped:
                    err(f"warning: a poll took longer than --interval; skipped {skipped}")
    except KeyboardInterrupt:
        out("")
    except DatabaseError as e:
        err(f"error: {e}")
        return ExitCode.DATABASE
    finally:
        if background is not None:
            background.stop()
        if watcher is not None:
            watcher.stop()
        if polls:
            out(f"{polls} poll(s): {stored} stored, {failed} failed")
        if conn is not None:
            conn.close()
    return ExitCode.OK


def _rediscover(rediscover, ip: str, err) -> Optional[str]:
    err(f"{ip} keeps failing to connect; looking the device up by its MAC…")
    new_ip = rediscover(ip)
    if new_ip is None:
        err("  not found on the network; will keep trying the same IP")
    elif new_ip == ip:
        err(f"  it's still at {ip} (the MAC answers there), but Modbus isn't; will keep trying")
    else:
        err(f"  found it at {new_ip}; polling there from now on (update devices.inverter.ip in the config)")
    return new_ip


def _store(conn, db_url: str, ip: str, results: Dict[int, PollResult], err):
    """Store one poll: a reading per unit, sharing a new poll_id. Returns (connection to use
    next time, {unit: reading id} for the readings stored).
    An unreachable database is survivable; a rejected insert is re-raised (a bug)."""
    if conn is None or conn.closed:
        try:
            conn = db.connect(db_url)
        except DatabaseError as e:
            err(f"warning: database unavailable, this poll isn't stored: {e}")
            return None, {}
    reading_ids: Dict[int, int] = {}
    try:
        poll_id = db.next_poll_id(conn)
        for unit, result in results.items():
            reading_ids[unit] = db.insert_reading(
                conn, ip=ip, unit_id=unit, poll_id=poll_id, started_at=result.started_at,
                ended_at=result.ended_at, values=result.values, error=result.error)
    except DatabaseError as e:
        if not e.transient:
            raise
        err(f"warning: database unavailable, this poll isn't (fully) stored: {e}")
        conn.close()
        return None, reading_ids
    return conn, reading_ids
