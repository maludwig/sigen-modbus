"""`sigen poll --alerts`: run sigen.battery_watch's detectors after every poll and push what they find to ntfy.

- Watcher, in poll's thread: a Buffer of the last SPAN of polls (plant and inverter values side by side, named like
  the scaled views' columns, after the gain) and the outside air (Open-Meteo's 15-minute temperatures, which the
  weather thread adds as they arrive). Filled once from the database when poll starts (one query for the readings,
  one for the air), then from each poll's values in memory: the database isn't read again.
- After each stored poll it builds a Window, runs every detector and decides, per event:
  * only events still going on (or that ended within RECENT) count, so history never alerts after a restart;
  * an event alerts once: the same kind (or key: each alarm bit has its own) carrying on (starting within MAX_GAP of
    where it was last seen) is the same event, however long it lasts; there are no "cleared" messages;
  * each kind or key waits COOLDOWN (1 h) between notifications, across restarts too (alert_log's last sent time);
    a new event inside the cooldown is logged as suppressed, once;
  * kinds without a Notice (charge_limited: the BMS throttles every cold night by design) aren't sent.
  Every message ends with when the event began: ISO 8601, local time with its offset, to the second.
- Unreachable: after UNREACHABLE_POLLS polls in a row in which no unit gave a reading, one "unreachable" alert per
  outage (a restart in the middle of one doesn't alert again: alert_log says it was sent after the last reading).
  Getting readings again is logged in alert_log as "reachable", but not sent.
- Sender, its own thread: posts to ntfy and logs every alert, sent or not, in alert_log through its own database
  connection, so poll's thread never waits on the network.

Nothing here can stop the polling: a failure is a warning line (the same warning at most once an hour).
"""
import datetime
import queue
import sys
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from sigen import battery_watch as bw
from sigen import db, ntfy
from sigen.config import Alerts

SPAN = datetime.timedelta(hours=6)              # history the detectors see (a plateau needs 2 h)
COOLDOWN = datetime.timedelta(hours=1)          # per kind, between notifications
RECENT = datetime.timedelta(minutes=2)          # an event that ended longer ago than this is history
AIR_HOLD = datetime.timedelta(minutes=30)       # the newest air temperature holds this long after its period
WARN_EVERY = datetime.timedelta(hours=1)        # the same warning, at most this often
UNREACHABLE_POLLS = 20                          # polls in a row with no reading at all: 5 min at --interval 15
COLUMN_SET = frozenset(bw.COLUMNS)


@dataclass(frozen=True)
class Notice:
    title: str
    priority: str                   # ntfy: min, low, default, high, urgent (breaks through Do Not Disturb)
    tags: Tuple[str, ...]           # ntfy shows emoji short codes as icons


NOTICES: Dict[str, Notice] = {
    "fault": Notice("The SigenStor reports a fault", "urgent", ("rotating_light",)),
    "unreachable": Notice("Lost contact with the SigenStor", "high", ("satellite",)),
    "alarm": Notice("SigenStor alarm", "high", ("warning",)),       # each alarm bit brings its own title
    "below_reserve": Notice("Battery below its backup reserve", "urgent", ("battery",)),
    "cold_charging": Notice("Battery charging while near freezing", "urgent", ("rotating_light",)),
    "cold": Notice("Battery is cold: blanket time", "high", ("snowflake",)),
    "cooling": Notice("Battery cooling toward 5 °C", "default", ("snowflake",)),
    "plateau": Notice("Battery heater? Temperature held steady", "high", ("fire",)),
    "warming": Notice("Battery heater? Cells warmed up", "high", ("fire",)),
    "sawtooth": Notice("Battery heater? Sawtooth temperature", "high", ("fire",)),
    "unexplained_power": Notice("Battery heater? Unexplained power", "high", ("electric_plug",)),
    "idle_drain": Notice("Battery heater? Energy leaving while idle", "high", ("battery",)),
}
REACHABLE = Notice("In contact with the SigenStor again", "default", ("white_check_mark",))   # logged, never sent


def stamp(t: datetime.datetime, tz) -> str:
    """ISO 8601 in local time, to the second, with the offset: 2026-10-12T19:49:30-06:00."""
    return t.astimezone(tz).isoformat(timespec="seconds")


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Buffer:
    """The last `span` of rows (time, {view column: value}) and air temperatures (time, °C), both ascending. Rows
    come from poll's thread; air from the weather thread, hence the lock."""

    def __init__(self, span: datetime.timedelta = SPAN):
        self.span = span
        self._rows: deque = deque()
        self._air: deque = deque()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._rows)

    def add_row(self, t: datetime.datetime, values: Dict[str, float]) -> None:
        if self._rows and t <= self._rows[-1][0]:
            return                                       # keep times ascending (a Window insists)
        self._rows.append((t, values))
        while self._rows and self._rows[0][0] < t - self.span:
            self._rows.popleft()
        with self._lock:                                 # keep one air point before the rows, to interpolate from
            while len(self._air) > 1 and self._air[1][0] <= t - self.span:
                self._air.popleft()

    def add_air(self, t: datetime.datetime, value: Optional[float]) -> None:
        if value is None:
            return
        with self._lock:
            if self._air and t == self._air[-1][0]:
                self._air[-1] = (t, float(value))       # a later fetch of the same period wins
            elif not self._air or t > self._air[-1][0]:
                self._air.append((t, float(value)))

    def window(self) -> bw.Window:
        rows = list(self._rows)
        with self._lock:
            air = list(self._air)
        times = [t for t, _ in rows]
        columns = {c: [values.get(c) for _, values in rows] for c in bw.COLUMNS}
        columns[bw.AIR] = bw.interpolate(times, air, hold=AIR_HOLD)
        return bw.Window(times, columns)


def poll_values(results, units: Dict[int, str]) -> Tuple[Optional[datetime.datetime], Dict[str, float]]:
    """One poll's results (poll.PollResult per unit) -> (the plant reading's start time, {view column: value after
    the gain}) for the columns the detectors read. (None, {}) if no unit gave values."""
    values: Dict[str, float] = {}
    plant = [r for u, r in results.items() if units.get(u) == "plant"]
    when = plant[0].started_at if plant else min((r.started_at for r in results.values()), default=None)
    for result in results.values():
        for address, raw in result.values:
            reg = db.BY_ADDRESS.get(address)
            if reg is not None:
                column = db.view_column(reg)
                if column in COLUMN_SET:
                    values[column] = raw / reg.gain
    return (when, values) if values else (None, {})


class Sender:
    """Posts alerts to ntfy and logs each one (sent or suppressed) in alert_log, from its own daemon thread, in the
    order they were raised. `store` is a weather.Store (its own database connection), or None to log nowhere."""

    def __init__(self, alerts: Alerts, store=None, out=print, err=lambda msg: print(msg, file=sys.stderr),
                 post: Callable[..., ntfy.Attempt] = ntfy.post):
        self.alerts, self._store, self._out, self._err, self._post = alerts, store, out, err, post
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="sigen-alerts", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def send(self, kind: str, notice: Notice, message: str) -> None:
        self._queue.put(("send", kind, notice, message))

    def suppress(self, kind: str, notice: Notice, message: str, why: str = "cooldown") -> None:
        """Log it in alert_log without sending it: inside its cooldown, or a kind that's only logged."""
        self._queue.put((why, kind, notice, message))

    def stop(self, wait: float = 6.0) -> None:
        """Let the queued alerts go out (up to `wait` seconds), then end the thread."""
        self._queue.put(None)
        if self._thread.is_alive():
            self._thread.join(wait)

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            what, kind, notice, message = item
            one_line = message.replace("\n", " ")
            try:
                fields = dict(title=notice.title, priority=notice.priority, tags=notice.tags, kind=kind)
                if what != "send":
                    attempt = ntfy.suppressed(message, **fields)
                    self._out(f"alert {'logged' if what == 'log only' else 'suppressed'} ({kind}, {what}): "
                              f"{notice.title}: {one_line}")
                else:
                    attempt = self._post(self.alerts, message, **fields)
                    if attempt.ok:
                        self._out(f"alert sent ({kind}): {notice.title}: {one_line}")
                    else:
                        self._err(f"warning: alert NOT sent ({kind}): {attempt.problem()}")
                if self._store is not None:
                    self._store.use("this alert isn't logged", lambda conn: db.insert_alert(conn, attempt))
            except Exception as e:                       # never let an alert take anything else down
                self._err(f"warning: alert ({kind}) failed: {type(e).__name__}: {e}")


class Watcher:
    """Poll's side of --alerts: keeps the Buffer, runs the detectors after each poll, hands alerts to the Sender."""

    def __init__(self, sender: Sender, *, cooldown: datetime.timedelta = COOLDOWN, recent: datetime.timedelta = RECENT,
                 span: datetime.timedelta = SPAN, detect: Optional[Callable[[bw.Window], List[bw.Event]]] = None,
                 tz=None, unreachable_polls: int = UNREACHABLE_POLLS,
                 out=print, err=lambda msg: print(msg, file=sys.stderr)):
        self.buffer = Buffer(span)
        self._sender, self._cooldown, self._recent = sender, cooldown, recent
        self._detect = detect or bw.detect_all
        self.tz = tz or datetime.datetime.now().astimezone().tzinfo      # for the timestamps in messages
        self._unreachable_polls = unreachable_polls
        self._out, self._err = out, err
        self.last_sent: Dict[Optional[str], datetime.datetime] = {}     # kind or key -> last notification
        self._seen_until: Dict[str, datetime.datetime] = {}             # kind or key -> where its last event got to
        self._warned: Dict[str, datetime.datetime] = {}
        self.failed_polls = 0                                           # in a row, no unit giving a reading
        self.outage_start: Optional[datetime.datetime] = None
        self.outage_alerted = False                                     # this outage's "unreachable" was raised

    # ---------- start / stop ----------
    def start(self, conn, units: Dict[int, str], now: datetime.datetime) -> None:
        """Fill the buffer from the database and learn when each kind was last sent, and whether an outage is
        going on; then start the sender."""
        try:
            self.preload(conn, units, now)
            self.last_sent.update(db.last_alerts_sent(conn))
            self.preload_outage(conn)
            self._out(f"alerts: {len(self.buffer)} poll(s) of history loaded; notifying {self._sender.alerts.ntfy_server}/"
                      f"{ntfy.masked(self._sender.alerts.ntfy_topic)}")
        except Exception as e:
            self._warn(f"alerts start without history: {type(e).__name__}: {e}", now)
        self._sender.start()

    def preload_outage(self, conn) -> None:
        """The polls in a row at the end of the readings table that got no reading at all, so a restart in the middle
        of an outage carries on counting, and doesn't alert twice: the outage was alerted if an "unreachable" was
        sent after its first failed poll."""
        with db.errors_as("loading the polls' recent outcome failed"):
            failed, start = conn.execute(
                "SELECT count(DISTINCT poll_id), min(started_at) FROM readings WHERE poll_id > "
                "coalesce((SELECT max(poll_id) FROM readings WHERE error IS NULL), 0)").fetchone()
        self.failed_polls, self.outage_start = failed, start      # every poll after the last one with a reading
        sent = self.last_sent.get("unreachable")
        self.outage_alerted = bool(failed) and sent is not None and sent >= start

    def stop(self) -> None:
        self._sender.stop()

    def preload(self, conn, units: Dict[int, str], now: datetime.datetime) -> None:
        from psycopg import sql
        plant = next(u for u, d in units.items() if d == "plant")
        inverter = next((u for u, d in units.items() if d == "inverter"), -1)
        plant_cols = [c for c in bw.COLUMNS if c.startswith("plant_")]
        inverter_cols = [c for c in bw.COLUMNS if c.startswith("inverter_")]
        query = sql.SQL(
            "SELECT rp.started_at, {} FROM readings rp JOIN plant_register_values p ON p.reading_id = rp.id "
            "LEFT JOIN readings ri ON ri.poll_id = rp.poll_id AND ri.unit_id = %s "
            "LEFT JOIN inverter_register_values i ON i.reading_id = ri.id "
            "WHERE rp.unit_id = %s AND rp.started_at >= %s ORDER BY rp.started_at").format(
            sql.SQL(", ").join([sql.Identifier("p", c) for c in plant_cols]
                               + [sql.Identifier("i", c) for c in inverter_cols]))
        since = now - self.buffer.span
        with db.errors_as("loading the alerts' history failed"):
            rows = conn.execute(query, (inverter, plant, since)).fetchall()
            air = conn.execute(
                "SELECT DISTINCT ON (valid_at) valid_at, temperature_2m FROM open_meteo_weather "
                "WHERE resolution_minutes = 15 AND error IS NULL AND temperature_2m IS NOT NULL AND valid_at >= %s "
                "ORDER BY valid_at, fetched_at DESC", (since - datetime.timedelta(hours=1),)).fetchall()
        for t, value in air:
            self.buffer.add_air(t, value)
        names = plant_cols + inverter_cols
        for t, *values in rows:
            self.buffer.add_row(t, {n: float(v) for n, v in zip(names, values) if v is not None})

    # ---------- every poll ----------
    def add_air(self, t: datetime.datetime, value: Optional[float]) -> None:
        self.buffer.add_air(t, value)

    def observe(self, results, units: Dict[int, str], now: datetime.datetime) -> None:
        """After a poll is stored: add it to the buffer, count it towards an outage, and check. Never raises."""
        try:
            when, values = poll_values(results, units)
            if when is not None:
                self.buffer.add_row(when, values)
            self.reachability(results, now)
            self.check(now)
        except Exception as e:
            self._warn(f"alert check failed: {type(e).__name__}: {e}", now)

    def reachability(self, results, now: datetime.datetime) -> Optional[str]:
        """Count polls in which no unit gave a reading. At unreachable_polls in a row, raise "unreachable" (once per
        outage); on the first reading after an alerted outage, log "reachable" without sending it. Returns
        "unreachable", "reachable" or None (for tests)."""
        if not results:
            return None
        if any(r.error is None for r in results.values()):
            back = self.outage_alerted
            if back:
                minutes = (now - self.outage_start).total_seconds() / 60
                self._sender.suppress("reachable", REACHABLE,
                                      f"Readings again, after {minutes:.0f} min without.\n{stamp(now, self.tz)}",
                                      why="log only")
            self.failed_polls, self.outage_start, self.outage_alerted = 0, None, False
            return "reachable" if back else None
        self.failed_polls += 1
        if self.outage_start is None:
            self.outage_start = min(r.started_at for r in results.values())
        if self.failed_polls < self._unreachable_polls or self.outage_alerted:
            return None
        self.outage_alerted = True
        last_error = next(iter(results.values())).error
        minutes = (now - self.outage_start).total_seconds() / 60
        self._raise("unreachable", NOTICES["unreachable"],
                    f"No reading from the SigenStor for {minutes:.0f} min ({self.failed_polls} polls in a row). "
                    f"Last error: {last_error}", self.outage_start, now)
        return "unreachable"

    def check(self, now: datetime.datetime) -> List[Tuple[str, bw.Event]]:
        """Run the detectors; returns [("sent" | "suppressed", event)] for the alerts raised (for tests)."""
        raised = []
        for e in sorted(self._detect(self.buffer.window()), key=lambda e: (e.start, e.kind)):
            if e.end < now - self._recent:
                continue                                 # history
            last = self._seen_until.get(e.ident)
            self._seen_until[e.ident] = max(min(e.end, now), last or e.start)
            if last is not None and e.start <= last + bw.MAX_GAP:
                continue                                 # the same event carrying on: already handled
            notice = NOTICES.get(e.kind)
            if notice is None:
                continue
            notice = Notice(e.title or notice.title, e.priority or notice.priority, notice.tags)
            raised.append((self._raise(e.ident, notice, e.summary, e.start, now), e))
        return raised

    def _raise(self, ident: str, notice: Notice, summary: str, start: datetime.datetime,
               now: datetime.datetime) -> str:
        """Send it, or log it as suppressed inside its cooldown; the message ends with the event's start."""
        message = f"{summary}\n{stamp(start, self.tz)}"
        sent = self.last_sent.get(ident)
        if sent is not None and now - sent < self._cooldown:
            self._sender.suppress(ident, notice, message)
            return "suppressed"
        self.last_sent[ident] = now
        self._sender.send(ident, notice, message)
        return "sent"

    def _warn(self, message: str, now: datetime.datetime) -> None:
        last = self._warned.get(message)
        if last is None or now - last >= WARN_EVERY:
            self._warned[message] = now
            self._err(f"warning: {message}")
