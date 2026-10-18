"""Push notifications through ntfy (https://ntfy.sh): an HTTP POST of the message to <server>/<topic>; the ntfy
phone app subscribed to that topic shows it within seconds. Standard library only.

On the public server the topic name is the only secret (anyone who knows it can read and send), so it comes from
the config's "alerts" section and is masked in what sigen prints. Every attempt can be stored in the database's
alert_log table (db.insert_alert), the topic left out.
"""

import datetime
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Sequence

from sigen.config import Alerts
from sigen.errors import ConnectionFailed

PRIORITIES = ("min", "low", "default", "high", "urgent")  # urgent can break through Do Not Disturb
TIMEOUT = 5.0


def masked(topic: str) -> str:
    """'someone-solar-1a2b3c4d' -> 'someone-solar-********': enough to recognise, not enough to subscribe. Without
    a random-looking last part (4+ characters after the last '-'), only the first quarter is shown."""
    head, sep, tail = topic.rpartition("-")
    if sep and len(tail) >= 4:
        return head + sep + "*" * len(tail)
    keep = len(topic) // 4
    return topic[:keep] + "*" * (len(topic) - keep)


def where(alerts: Alerts) -> str:
    """'<server>/<masked topic>': where alerts go, safe to print or put in an error message."""
    return f"{alerts.ntfy_server}/{masked(alerts.ntfy_topic)}"


def _latin1(text: str) -> str:
    """`text` as a header value can carry it: latin-1, anything else as "?"."""
    return text.encode("latin-1", "replace").decode("latin-1")


@dataclass(frozen=True)
class Attempt:
    """One try at sending a notification: what was sent and what came back (an alert_log row)."""

    alerted_at: datetime.datetime
    message: str
    title: str | None = None
    priority: str | None = None
    tags: tuple[str, ...] = ()
    click: str | None = None
    kind: str | None = None  # what raised it: a battery_watch event kind, or "test"
    was_suppressed: bool = False  # raised but not sent (cooldown): no HTTP fields
    http_code: int | None = None  # None: nothing answered (or suppressed)
    ntfy_id: str | None = None
    error: str | None = None  # None: accepted; else the 4xx/5xx body, or what went wrong
    where: str = field(default="", compare=False)  # server and masked topic, for messages

    @property
    def ok(self) -> bool:
        """True if ntfy accepted it, or it was suppressed (nothing tried, so nothing failed)."""
        return self.error is None

    def problem(self) -> str:
        """One line saying why a failed attempt failed (unreachable vs refused), topic masked; only for `not ok`."""
        if self.http_code is None:
            return f"couldn't reach ntfy at {self.where}: {self.error}"
        return f"ntfy at {self.where} refused the message: HTTP {self.http_code} {self.error[:300]}"


def suppressed(
    message: str,
    *,
    title: str | None = None,
    priority: str | None = None,
    tags: Sequence[str] = (),
    click: str | None = None,
    kind: str | None = None,
) -> Attempt:
    """An alert that was raised but deliberately not sent (its kind is in cooldown), for the alert_log."""
    return Attempt(
        alerted_at=datetime.datetime.now(datetime.UTC),
        message=message,
        title=title,
        priority=priority,
        tags=tuple(tags),
        click=click,
        kind=kind,
        was_suppressed=True,
    )


def post(
    alerts: Alerts,
    message: str,
    *,
    title: str | None = None,
    priority: str | None = None,
    tags: Sequence[str] = (),
    click: str | None = None,
    kind: str | None = None,
    timeout: float = TIMEOUT,
) -> Attempt:
    """Post `message` to the topic and say how it went; never raises for a network or HTTP failure (that's in the
    Attempt). Header values must be latin-1, so the title, tags and click URL lose anything else ("?"); the message
    body is UTF-8 and may hold anything (°C, emoji)."""
    if priority is not None and priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}")
    sent = dict(
        alerted_at=datetime.datetime.now(datetime.UTC),
        message=message,
        title=title,
        priority=priority,
        tags=tuple(tags),
        click=click,
        kind=kind,
        where=where(alerts),
    )
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if title:
        headers["Title"] = _latin1(title)
    if priority:
        headers["Priority"] = priority
    if tags:
        headers["Tags"] = _latin1(",".join(tags))
    if click:
        headers["Click"] = _latin1(click)
    request = urllib.request.Request(
        f"{alerts.ntfy_server}/{alerts.ntfy_topic}", data=message.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code, body = response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace").strip()
        return Attempt(**sent, http_code=e.code, error=body or f"(empty body) {e.reason}")
    except (urllib.error.URLError, OSError) as e:
        return Attempt(**sent, error=str(getattr(e, "reason", e)) or type(e).__name__)
    try:
        ntfy_id = json.loads(body).get("id")
    except (ValueError, AttributeError):
        ntfy_id = None
    return Attempt(**sent, http_code=code, ntfy_id=ntfy_id)


def send(alerts: Alerts, message: str, **kw: Any) -> Attempt:
    """post(), raising ConnectionFailed (exit 3) if it didn't get through."""
    attempt = post(alerts, message, **kw)
    if not attempt.ok:
        raise ConnectionFailed(attempt.problem())
    return attempt
