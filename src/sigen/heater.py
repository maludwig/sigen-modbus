"""Log each battery pack's self-heating status from the Sigen cloud while `sigen poll` runs. The heater isn't in any
Modbus register (the LoadHub has no Modbus), so the cloud is the only window on it.

A daemon thread, like sigen.weather.Background, that never affects the Modbus polling and never stops itself on a
cloud or database problem: it logs each query (UTC-timestamped "Cloud: ..." lines on its out stream), stores an
error row for a cloud problem, and keeps polling the cloud right through a database outage (reopening the connection
when the database returns). The one thing that disables it for the session is being unable to refresh the token at
launch.

Cadence (Mitchell's design): every `base_interval` (10 min) at rest; every `active_interval` (1 min) while the
heater might run -- PV over `pv_threshold` and the coolest cluster under `cold_threshold` -- and every minute while
any pack's heater is on. Each tick queries ONE pack (round-robin); if its state changed, the other pack(s) are
queried at once, to catch the ~10-minute stagger between them. Every poll is stored (no dedup), one row per query.

The main poll feeds the gate (PV, coolest temperature) through observe() after each Modbus poll; the cloud token is
refreshed once at launch and kept fresh (>1 h of life) by the thread, so queries never have to refresh.
"""

import datetime
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from sigen import db
from sigen.cloud import DEVICE_BATTERY, CloudClient
from sigen.config import Cloud
from sigen.errors import CloudError

BASE_INTERVAL = 600.0  # seconds between polls at rest (10 min)
ACTIVE_INTERVAL = 60.0  # seconds between polls while the heater might run / is on (1 min)
PV_THRESHOLD_KW = 0.2  # PV above this (with a cold pack) -> the heater might turn on soon
COLD_THRESHOLD_C = 15.0  # coolest cluster below this -> the heater might run (its off-point is ~15 °C)
PV_REGISTER = "inverter_pv_power"
COOLEST_REGISTER = "inverter_ess_min_cluster_temperature"


def heating_from(data) -> Optional[bool]:
    """The pack's "Heating status" (True/False) from a device dynamic-info response, or None if it isn't there."""
    rows = data.get("realTimeInfo", []) if isinstance(data, dict) else []
    for row in rows:
        if isinstance(row, dict) and row.get("paramKey") == "Heating status":
            value = str(row.get("paramValue", "")).strip().lower()
            if value in ("on", "off"):
                return value == "on"
    return None


def gate_values(results, units: Dict[int, str]) -> Tuple[Optional[float], Optional[float]]:
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
        batteries: List[str],
        db_url: str,
        *,
        out: Callable[[str], None] = print,
        err: Callable[[str], None] = print,
        base_interval: float = BASE_INTERVAL,
        active_interval: float = ACTIVE_INTERVAL,
        pv_threshold: float = PV_THRESHOLD_KW,
        cold_threshold: float = COLD_THRESHOLD_C,
        now=lambda: datetime.datetime.now(datetime.timezone.utc),
    ):
        self._client = client
        self._station = station_id
        self._batteries = list(batteries)
        self._db_url = db_url
        self._out, self._err = out, err
        self._base, self._active_iv = base_interval, active_interval
        self._pv_threshold, self._cold_threshold = pv_threshold, cold_threshold
        self._now = now
        self._pv_kw, self._coolest_c = 0.0, float("inf")  # gate; not active until the first poll says cold
        self._state: Dict[int, bool] = {}  # battery index -> last heating (True/False)
        self._rr = 0  # round-robin cursor
        self._last_error: Optional[str] = None  # to avoid repeating the same cloud warning each minute
        self._last_db_error: Optional[str] = None  # the same, for database trouble
        self._db_broken = False  # a write failed -> reopen the connection next round
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="sigen-heater", daemon=True)

    @classmethod
    def from_config(cls, cloud: Cloud, db_url: str, *, out=print, err=print) -> Optional["HeaterPoller"]:
        """Build one from the config's cloud section, or warn and return None if it can't poll (no station / packs)."""
        if not cloud.station_id or not cloud.batteries:
            err("warning: cloud heater polling off: the config's cloud section needs station_id and batteries")
            return None
        client = CloudClient(
            cloud.token_file,
            cloud.base_url,
            app_base_url=cloud.app_base_url,
            obfuscation_key=cloud.obfuscation_key,
            client_basic=cloud.client_basic,
        )
        return cls(client, cloud.station_id, list(cloud.batteries), db_url, out=out, err=err)

    # ---------- the main poll feeds the gate ----------
    def observe(self, results, units: Dict[int, str]) -> None:
        pv_kw, coolest_c = gate_values(results, units)
        with self._lock:
            if pv_kw is not None:
                self._pv_kw = pv_kw
            if coolest_c is not None:
                self._coolest_c = coolest_c

    def _active(self) -> bool:
        with self._lock:
            might_run = self._pv_kw > self._pv_threshold and self._coolest_c < self._cold_threshold
        return might_run or any(self._state.values())

    # ---------- the thread ----------
    def start(self) -> None:
        self._thread.start()

    def stop(self, wait: float = 2.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(wait)

    def _log(self, message: str) -> None:
        """One UTC-timestamped activity line, e.g. `2026-10-17 15:21:38.225991Z  Cloud: Battery … heater Off, …`."""
        self._out(f"{self._now().strftime('%Y-%m-%d %H:%M:%S.%f')}Z  Cloud: {message}")

    def _open(self):
        """A database connection, or None (warned once) if the database is down -- the cloud is still polled."""
        try:
            return db.connect(self._db_url)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if msg != self._last_db_error:
                self._err(f"warning: cloud heater can't reach the database ({msg}); will keep polling the cloud")
                self._last_db_error = msg
            return None

    @staticmethod
    def _close(conn) -> None:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _loop(self) -> None:
        self._log("refreshing token")
        try:
            self._client.refresh()  # Mitchell: refresh at launch (keeps queries from ever refreshing)
        except CloudError as e:
            self._err(f"warning: cloud heater polling off this session (couldn't refresh the token): {e}")
            return
        conn = None
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
                if self._stop.wait(self._active_iv if self._active() else self._base):
                    break
        finally:
            self._close(conn)

    def _poll_round(self, conn) -> None:
        """Query one pack (round-robin); if its heater changed state, query the other(s) at once."""
        i = self._rr % len(self._batteries)
        self._rr += 1
        if self._query(conn, i):
            for j in range(len(self._batteries)):
                if j != i:
                    self._query(conn, j)

    def _query(self, conn, i: int) -> bool:
        """Poll pack i, log it, store the row (or note it unwritten), and return True if its heater changed state."""
        sn = self._batteries[i]
        self._log(f"Battery {sn} querying")
        polled_at = self._now()
        heating: Optional[bool] = None
        error: Optional[str] = None
        try:
            self._client.ensure_fresh()  # refresh only when the token is within an hour of expiry
            data = self._client.device_dynamic_info(self._station, sn, DEVICE_BATTERY)
            heating = heating_from(data)
            if heating is None:
                error = "no Heating status in the response"
        except CloudError as e:
            error = str(e)
        written = self._write(conn, polled_at, i + 1, heating, error)
        status = "written successfully" if written else "database unwritten"
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

    def _write(self, conn, polled_at, battery: int, heater_on: Optional[bool], error: Optional[str]) -> bool:
        """Store one poll's row; on any database trouble warn once, flag the connection for reopening, and return
        False so the caller logs it unwritten. A database outage must never stop the heater thread."""
        if conn is None:
            return False
        try:
            db.insert_battery_heater(conn, polled_at=polled_at, battery=battery, heater_on=heater_on, error=error)
            self._last_db_error = None
            return True
        except Exception as e:
            self._db_broken = True
            msg = f"{type(e).__name__}: {e}"
            if msg != self._last_db_error:
                self._err(f"warning: cloud heater can't write to the database ({msg}); will keep polling the cloud")
                self._last_db_error = msg
            return False
