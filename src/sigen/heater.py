"""Log each battery pack's self-heating status from the Sigen cloud while `sigen poll` runs. The heater isn't in any
Modbus register (the LoadHub has no Modbus), so the cloud is the only window on it.

A daemon thread, like sigen.weather.Background, that never affects the Modbus polling and never stops itself on a
cloud or database problem: it logs each query (UTC-timestamped "Cloud: ..." lines on its out stream), stores an
error row for a cloud problem, and keeps polling the cloud right through a database outage (reopening the connection
when the database returns). The one thing that disables it for the session is being unable to refresh the token at
launch.

Cadence (Mitchell's design; the config's poll.heater can set each number, sigen.config.HEATER_SETTINGS): every
`base_interval` (slow_interval, 10 min) at rest; every `active_interval` (fast_interval, 1 min) while the heater
might run -- PV over `pv_threshold` (fast_pv_kw) and the coolest cluster under `cold_threshold` (fast_below_c); and
every `on_interval` (1 min unless set) while a pack's heater is on or was seen on in the last ON_HOLD, to catch the
next of its short bursts. Each tick queries ONE pack (round-robin); if its state changed, the other pack(s) are
queried at once, to catch the ~10-minute stagger between them. Every poll is stored (no dedup), one row per query,
with the rest of the pack's answer: its own SOC, charge/discharge power and voltage (readings_from), which the Modbus
registers only give for the whole battery.

Back-off: after a failed query (the cloud unreachable or answering with an error, a token refresh that failed), the
wait doubles with each failure in a row, is at least what a Retry-After header asked for, and never more than
`base_interval`; the first success brings the normal cadence back.

The main poll feeds the gate (PV, coolest temperature) through observe() after each Modbus poll; the cloud token is
refreshed once at launch and kept fresh (>1 h of life) by the thread, so queries never have to refresh.
"""

import datetime
import threading
from collections.abc import Callable, Mapping
from typing import Any

import psycopg

from sigen import db
from sigen.cloud import DEVICE_BATTERY, CloudClient
from sigen.config import HEATER_SETTINGS, Cloud
from sigen.errors import CloudError
from sigen.poll import PollResult

BASE_INTERVAL = HEATER_SETTINGS["slow_interval"][0]  # seconds between polls at rest (10 min)
ACTIVE_INTERVAL = HEATER_SETTINGS["fast_interval"][0]  # seconds between polls while the heater might run (1 min)
ON_INTERVAL = HEATER_SETTINGS["on_interval"][0]  # seconds between polls while a heater is on or was lately (1 min)
PV_THRESHOLD_KW = HEATER_SETTINGS["fast_pv_kw"][0]  # PV above this (with a cold pack) -> it might turn on soon
COLD_THRESHOLD_C = HEATER_SETTINGS["fast_below_c"][0]  # coolest cluster below this -> it might run (off at ~15 °C)
ON_HOLD = datetime.timedelta(minutes=10)  # on_interval this long after a heater was last seen on: its bursts repeat
MAX_DOUBLINGS = 16  # back-off: past this many failures in a row the wait stops doubling (it's capped anyway)
# poll.heater's names -> the constructor's
SETTING_ARGS = {
    "slow_interval": "base_interval",
    "fast_interval": "active_interval",
    "on_interval": "on_interval",
    "fast_pv_kw": "pv_threshold",
    "fast_below_c": "cold_threshold",
}
PV_REGISTER = "inverter_pv_power"
COOLEST_REGISTER = "inverter_ess_min_cluster_temperature"
# The answer's other rows, stored with each poll: paramKey -> (battery_heater column, {unit: factor to its unit}).
PACK_READINGS = {
    "Battery SOC": ("soc_pct", {"%": 1.0}),
    "Charging & Discharging Power": ("power_kw", {"kW": 1.0, "W": 0.001}),
    "Battery Pack Voltage": ("voltage_v", {"V": 1.0, "mV": 0.001}),
}


def heating_from(data: Any) -> bool | None:
    """The pack's "Heating status" (True/False) from a device dynamic-info response, or None if it isn't there."""
    rows = data.get("realTimeInfo", []) if isinstance(data, dict) else []
    for row in rows:
        if isinstance(row, dict) and row.get("paramKey") == "Heating status":
            value = str(row.get("paramValue", "")).strip().lower()
            if value in ("on", "off"):
                return value == "on"
    return None


def readings_from(data: Any) -> dict[str, float]:
    """The pack's SOC, power and voltage from a dynamic-info response, as {column: value} (PACK_READINGS); a row
    that's missing, isn't a number or has a unit we don't know is left out (so its column stays NULL)."""
    rows = data.get("realTimeInfo", []) if isinstance(data, dict) else []
    out = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("paramKey") not in PACK_READINGS:
            continue
        column, units = PACK_READINGS[row["paramKey"]]
        factor = units.get(str(row.get("paramValueUnit", "")).strip())
        try:
            value = float(str(row.get("paramValueText", "")).strip())
        except ValueError:
            continue
        if factor is not None:
            out[column] = value * factor
    return out


def gate_values(results: dict[int, PollResult], units: dict[int, str]) -> tuple[float | None, float | None]:
    """(PV power kW, coolest cluster °C) from a poll's inverter reading, or (None, None) where unavailable."""
    pv_kw = coolest_c = None
    for unit, result in results.items():
        if units.get(unit) != "inverter" or result.error is not None:
            continue
        for address, raw in result.values:
            reg = db.BY_ADDRESS.get(address)
            if reg is None:
                continue
            if reg.name == PV_REGISTER:
                pv_kw = raw / reg.gain
            elif reg.name == COOLEST_REGISTER:
                coolest_c = raw / reg.gain
    return pv_kw, coolest_c


class HeaterPoller:
    """Polls the cloud for the packs' heating status on the gated cadence, in its own daemon thread."""

    def __init__(
        self,
        client: CloudClient,
        station_id: str,
        batteries: list[str],
        db_url: str,
        *,
        out: Callable[[str], None] = print,
        err: Callable[[str], None] = print,
        base_interval: float = BASE_INTERVAL,
        active_interval: float = ACTIVE_INTERVAL,
        on_interval: float = ON_INTERVAL,
        pv_threshold: float = PV_THRESHOLD_KW,
        cold_threshold: float = COLD_THRESHOLD_C,
        now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC),
    ) -> None:
        self._client = client
        self._station = station_id
        self._batteries = list(batteries)
        self._db_url = db_url
        self._out, self._err = out, err
        self._base, self._active_iv, self._on_iv = base_interval, active_interval, on_interval
        self._pv_threshold, self._cold_threshold = pv_threshold, cold_threshold
        self._now = now
        self._pv_kw, self._coolest_c = 0.0, float("inf")  # gate; not active until the first poll says cold
        self._state: dict[int, bool] = {}  # battery index -> last heating (True/False)
        self._last_on: datetime.datetime | None = None  # when any pack's heater was last seen on
        self._failures = 0  # failed queries in a row (back-off)
        self._retry_after: float | None = None  # what the last failure's Retry-After asked for
        self._rr = 0  # round-robin cursor
        self._last_error: str | None = None  # to avoid repeating the same cloud warning each minute
        self._last_db_error: str | None = None  # the same, for database trouble
        self._db_broken = False  # a write failed -> reopen the connection next round
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="sigen-heater", daemon=True)

    @classmethod
    def from_config(
        cls,
        cloud: Cloud,
        db_url: str,
        settings: Mapping[str, float] | None = None,
        *,
        out: Callable[[str], None] = print,
        err: Callable[[str], None] = print,
    ) -> "HeaterPoller | None":
        """Build one from the config's cloud section and poll.heater `settings` (those given; the rest default), or
        warn and return None if it can't poll (no station / packs)."""
        if not cloud.station_id or not cloud.batteries:
            err("warning: cloud heater polling off: the config's cloud section needs station_id and batteries")
            return None
        cadence = {SETTING_ARGS[name]: value for name, value in (settings or {}).items()}
        client = CloudClient.from_config(cloud)
        return cls(client, cloud.station_id, list(cloud.batteries), db_url, out=out, err=err, **cadence)

    # ---------- the main poll feeds the gate ----------
    def observe(self, results: dict[int, PollResult], units: dict[int, str]) -> None:
        """Feed the gate from one Modbus poll (sigen.poll calls it after each): a failed or missing inverter reading
        leaves the last PV / coolest values in place."""
        pv_kw, coolest_c = gate_values(results, units)
        with self._lock:
            if pv_kw is not None:
                self._pv_kw = pv_kw
            if coolest_c is not None:
                self._coolest_c = coolest_c

    def _active(self) -> bool:
        """True for the fast cadence: PV over the threshold with the coolest cluster under the cold threshold, or
        any pack's heater last seen on."""
        with self._lock:
            might_run = self._pv_kw > self._pv_threshold and self._coolest_c < self._cold_threshold
        return might_run or any(self._state.values())

    def _heating_lately(self) -> bool:
        """True for the on cadence: a pack's heater last seen on, or seen on within ON_HOLD."""
        recent = self._last_on is not None and self._now() - self._last_on < ON_HOLD
        return recent or any(self._state.values())

    def interval(self) -> float:
        """Seconds to the next tick by the cadence alone: on_interval, active_interval or base_interval."""
        if self._heating_lately():
            return self._on_iv
        return self._active_iv if self._active() else self._base

    def next_wait(self) -> float:
        """Seconds to the next tick: interval(), or after failures in a row, doubled per failure (and at least the
        last Retry-After), never more than base_interval."""
        wait = self.interval()
        if not self._failures:
            return wait
        wait = max(wait * 2 ** min(self._failures, MAX_DOUBLINGS), self._retry_after or 0.0)
        return min(wait, self._base)

    # ---------- the thread ----------
    def start(self) -> None:
        """Start the daemon thread; once only (sigen.poll calls it as the polling begins)."""
        self._thread.start()

    def stop(self, wait: float = 2.0) -> None:
        """Ask the thread to stop and wait up to `wait` seconds for it; one stuck in a cloud query may outlive that,
        which is harmless as it's a daemon."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(wait)

    def _log(self, message: str) -> None:
        """One UTC-timestamped activity line, e.g. `2026-10-17 15:21:38.225991Z  Cloud: Battery … heater Off, …`."""
        self._out(f"{self._now().strftime('%Y-%m-%d %H:%M:%S.%f')}Z  Cloud: {message}")

    def _open(self) -> psycopg.Connection | None:
        """A database connection, or None (warned once) if the database is down -- the cloud is still polled."""
        try:
            return db.connect(self._db_url)
        except Exception as e:
            self._warn_db("can't reach the database", e)
            return None

    def _warn_db(self, trouble: str, e: Exception) -> None:
        """Warn about a database problem once: the same error again (every poll, through an outage) stays quiet."""
        msg = f"{type(e).__name__}: {e}"
        if msg != self._last_db_error:
            self._err(f"warning: cloud heater {trouble} ({msg}); will keep polling the cloud")
            self._last_db_error = msg

    @staticmethod
    def _close(conn: psycopg.Connection | None) -> None:
        """Close the connection if there is one, ignoring any error (it's often the broken one being dropped)."""
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _loop(self) -> None:
        """The thread: refresh the token at launch (giving up for the session if that fails), then poll a round each
        interval until stopped, reopening the database connection after a failed write."""
        self._log("refreshing token")
        try:
            self._client.refresh()  # Mitchell: refresh at launch (keeps queries from ever refreshing)
        except CloudError as e:
            self._err(f"warning: cloud heater polling off this session (couldn't refresh the token): {e}")
            return
        conn: psycopg.Connection | None = None
        try:
            while not self._stop.is_set():
                if conn is None:
                    conn = self._open()  # may stay None while the database is down; we still poll the cloud
                try:
                    self._poll_round(conn)
                except Exception as e:  # a bad round must never stop the thread
                    self._err(f"warning: cloud heater poll round failed: {type(e).__name__}: {e}")
                if self._db_broken:  # a write failed: drop the connection and reopen next round
                    self._close(conn)
                    conn = None
                    self._db_broken = False
                wait = self.next_wait()
                if self._failures:
                    self._log(f"backing off: next query in {wait:g} s ({self._failures} failed in a row)")
                if self._stop.wait(wait):
                    break
        finally:
            self._close(conn)

    def _poll_round(self, conn: psycopg.Connection | None) -> None:
        """Query one pack (round-robin); if its heater changed state, query the other(s) at once."""
        i = self._rr % len(self._batteries)
        self._rr += 1
        if self._query(conn, i):
            for j in range(len(self._batteries)):
                if j != i:
                    self._query(conn, j)

    def _query(self, conn: psycopg.Connection | None, i: int) -> bool:
        """Poll pack i, log it, store the row (or note it unwritten), and return True if its heater changed state."""
        sn = self._batteries[i]
        self._log(f"Battery {sn} querying")
        polled_at = self._now()
        heating: bool | None = None
        error: str | None = None
        retry_after: float | None = None
        readings: dict[str, float] = {}
        try:
            self._client.ensure_fresh()  # refresh only when the token is within an hour of expiry
            data = self._client.device_dynamic_info(self._station, sn, DEVICE_BATTERY)
            heating = heating_from(data)
            if heating is None:
                error = "no Heating status in the response"
            else:
                readings = readings_from(data)
        except CloudError as e:
            error, retry_after = str(e), e.retry_after
        written = self._write(conn, polled_at, i + 1, heating, error, readings)
        status = "written successfully" if written else "database unwritten"
        if error is None:
            self._failures, self._retry_after = 0, None
            if heating:
                self._last_on = polled_at
        else:
            self._failures, self._retry_after = self._failures + 1, retry_after
        if error is not None:
            self._log(f"Battery {sn} query failed: {error}, {status}")
            if error != self._last_error:  # a new kind of cloud problem: an operator warning, once
                self._err(f"warning: cloud heater poll of battery {i + 1} failed: {error}")
                self._last_error = error
            return False
        self._log(f"Battery {sn} heater {'On' if heating else 'Off'}, {status}")
        self._last_error = None
        previous = self._state.get(i)
        self._state[i] = heating
        return previous is not None and heating != previous

    def _write(
        self,
        conn: psycopg.Connection | None,
        polled_at: datetime.datetime,
        battery: int,
        heater_on: bool | None,
        error: str | None,
        readings: dict[str, float] | None = None,
    ) -> bool:
        """Store one poll's row (with the pack's readings_from values, if any); on any database trouble warn once,
        flag the connection for reopening, and return False so the caller logs it unwritten. A database outage must
        never stop the heater thread."""
        if conn is None:
            return False
        try:
            db.insert_battery_heater(
                conn, polled_at=polled_at, battery=battery, heater_on=heater_on, error=error, **(readings or {})
            )
            self._last_db_error = None
            return True
        except Exception as e:
            self._db_broken = True
            self._warn_db("can't write to the database", e)
            return False
