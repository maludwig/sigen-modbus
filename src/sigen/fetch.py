"""HTTP GET that never raises for HTTP or network trouble, for the public data services (Open-Meteo and GeoMet in
sigen.weather, NOAA's GOES bucket in sigen.goes). Standard library only (urllib)."""

import http.client
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass

from sigen import __version__

TIMEOUT = 30  # seconds per HTTP request
USER_AGENT = f"sigen-modbus/{__version__}"


@dataclass(frozen=True)
class Response:
    """What http_get got: a status and body, or (status None) why nothing
    answered."""

    status: int | None  # None: nothing answered (DNS, refused, timeout)
    text: str  # the body, or what went wrong

    @property
    def ok(self) -> bool:
        """Only HTTP 200 counts; any other status is parsed as a failure."""
        return self.status == 200

    def describe(self) -> str:
        """For the error column: the whole response text."""
        return self.text if self.status is None else f"HTTP {self.status}\n{self.text}"


def http_get(url: str, params: Mapping[str, object], timeout: float = TIMEOUT) -> Response:
    """GET url?params (timeout in seconds). Never raises for HTTP or network
    failures: an error status keeps its body; no answer gives status None."""
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return Response(r.status, r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return Response(e.code, e.read().decode("utf-8", errors="replace"))
    except (OSError, http.client.HTTPException) as e:  # URLError and timeouts are OSErrors
        return Response(None, f"no response from {url}: {type(e).__name__}: {e}")
