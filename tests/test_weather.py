"""sigen.weather without the network or a database: Open-Meteo and GeoMet are faked."""

import datetime
import json
import threading
import time

import pytest

from sigen import weather
from sigen.config import Site, load_config
from sigen.errors import ConfigError, ExitCode
from sigen.weather import COVERAGE_LAYER, OPEN_METEO_URL, OPEN_METEO_VARIABLES, Response

UTC = datetime.UTC
SITE = Site(51.0443, -114.0631, "America/Edmonton")
NOW = datetime.datetime(2026, 10, 10, 14, 20, 30, tzinfo=UTC)


def at(hh, mm):
    return datetime.datetime(2026, 10, 10, hh, mm, tzinfo=UTC)


def open_meteo_body(resolution, missing=(), start=None):
    """Periods every `resolution` minutes for 5 hours from `start` (default 12:00); value i at period i."""
    start = at(12, 0) if start is None else start
    times = [int(start.timestamp()) + k * resolution * 60 for k in range(5 * 60 // resolution + 1)]
    block = {"time": times}
    for name in OPEN_METEO_VARIABLES:
        if name not in missing:
            block[name] = [(i % 2 if name == "is_day" else i) for i in range(len(times))]
    return json.dumps({"latitude": 51.05, "longitude": -114.06, weather.RESOLUTIONS[resolution]: block})


CAPABILITIES = (
    "<WMS_Capabilities><Layer><Name>RADAR_1KM_RRAI</Name>"
    '<Dimension name="time" units="ISO8601" default="2026-10-10T14:18:00Z" nearestValue="0">'
    "2026-10-10T14:06:00Z/2026-10-10T14:18:00Z/PT6M</Dimension></Layer></WMS_Capabilities>"
)
FRAMES = [at(14, 6), at(14, 12), at(14, 18)]


def feature(value, cls):
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"value": value, "class": cls, "time": "x"}}],
        }
    )


class FakeWeb:
    """Answers like Open-Meteo and GeoMet. `fail(url, params)` can return a Response to use instead."""

    def __init__(self, fail=None, covered=True, rain=0.0, start=None):
        self.requests = []
        self.fail = fail
        self.covered = covered
        self.rain = rain
        self.start = start  # of the Open-Meteo periods; None = 12:00 on the test day

    @classmethod
    def around_now(cls, **kw):
        """Open-Meteo periods starting 2 hours before the real clock, for tests that don't fake `now`."""
        hour = datetime.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        return cls(start=hour - datetime.timedelta(hours=2), **kw)

    def __call__(self, url, params):
        self.requests.append((url, dict(params)))
        if self.fail is not None:
            response = self.fail(url, params)
            if response is not None:
                return response
        if url == OPEN_METEO_URL:
            resolution = 60 if "hourly" in params else 15
            return Response(200, open_meteo_body(resolution, start=self.start))
        if params["request"] == "GetCapabilities":
            return Response(200, CAPABILITIES)
        if params["layers"] == COVERAGE_LAYER:
            return Response(
                200,
                json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature"}] if self.covered else []}),
            )
        if self.rain:
            return Response(200, feature(self.rain, "0.5-1"))
        return Response(200, feature(0, "Undetected"))


# ---------- Open-Meteo ----------
@pytest.mark.parametrize("resolution, valid_at, index", [(60, at(14, 0), 2), (15, at(14, 15), 9)])
def test_open_meteo_row_is_the_latest_period_ended(resolution, valid_at, index):
    web = FakeWeb()
    row, response = weather.fetch_open_meteo(SITE, resolution, get=web, now=lambda: NOW)
    assert row.error is None and row.valid_at == valid_at and row.fetched_at == NOW
    assert row.values["temperature_2m"] == float(index) and row.values["weather_code"] == index
    assert row.values["is_day"] is bool(index % 2)
    assert set(row.values) == set(OPEN_METEO_VARIABLES)
    url, params = web.requests[0]
    assert params[weather.RESOLUTIONS[resolution]].split(",") == list(OPEN_METEO_VARIABLES)
    assert params["timezone"] == "GMT" and params["timeformat"] == "unixtime"
    assert params["latitude"] == SITE.latitude and params["longitude"] == SITE.longitude


def test_a_period_ending_exactly_now_counts():
    row = weather.parse_open_meteo(Response(200, open_meteo_body(60)), 60, at(14, 0))
    assert row.valid_at == at(14, 0)


def test_a_missing_variable_is_none():
    row = weather.parse_open_meteo(Response(200, open_meteo_body(15, missing={"showers"})), 15, NOW)
    assert row.error is None and row.values["showers"] is None and row.values["rain"] == 9.0


def test_http_error_keeps_the_whole_response():
    body = '{"error":true,"reason":"Cannot initialize WeatherVariable from invalid String value nope"}'
    row = weather.parse_open_meteo(Response(400, body), 60, NOW)
    assert row.error == f"HTTP 400\n{body}" and row.valid_at is None and row.values == {}


def test_no_response_is_an_error_row():
    row = weather.parse_open_meteo(Response(None, "no response from x: URLError: timed out"), 60, NOW)
    assert row.error == "no response from x: URLError: timed out"


def test_unexpected_body_is_an_error_row():
    row = weather.parse_open_meteo(Response(200, "<html>maintenance</html>"), 60, NOW)
    assert row.error.startswith("unexpected response") and "<html>maintenance</html>" in row.error


def test_a_radar_answer_without_a_feature_says_so_plainly():
    with pytest.raises(ValueError) as e:
        weather.parse_feature(Response(200, json.dumps({"features": []})), "RADAR_1KM_RRAI")
    assert str(e.value) == "no feature at the site"  # not re-wrapped as "unexpected response" with the body


def test_no_period_before_now_is_an_error_row():
    row = weather.parse_open_meteo(Response(200, open_meteo_body(60)), 60, at(11, 0))
    assert row.valid_at is None and "no 60-minute period ends at or before" in row.error


def test_format_open_meteo():
    row, _ = weather.fetch_open_meteo(SITE, 60, get=FakeWeb(), now=lambda: NOW)
    text = weather.format_open_meteo(row)
    assert "period ending 2026-10-10 14:00Z" in text and "temperature_2m" in text and "°C" in text
    failed = weather.parse_open_meteo(Response(503, "Service Unavailable"), 60, NOW)
    assert "FAILED: HTTP 503 Service Unavailable" in weather.format_open_meteo(failed)
    assert "FAILED" in weather.summarize_open_meteo(failed) and "GHI" in weather.summarize_open_meteo(row)


# ---------- radar ----------
def test_parse_frames():
    assert weather.parse_frames(Response(200, CAPABILITIES)) == FRAMES
    with pytest.raises(ValueError, match="no time dimension"):
        weather.parse_frames(Response(200, "<WMS_Capabilities/>"))


def test_radar_newest_first_and_skips_stored_frames():
    web = FakeWeb(rain=0.7)
    rows = weather.fetch_radar(SITE, skip={at(14, 12)}, get=web, now=lambda: NOW)
    assert [r.frame_at for r in rows] == [at(14, 18), at(14, 6)]
    assert all(r.error is None and r.covered and r.rain_mm_h == 0.7 and r.rain_class == "0.5-1" for r in rows)
    sampled = [p for _, p in web.requests if p["request"] == "GetFeatureInfo"]
    assert {p["layers"] for p in sampled} == {"RADAR_1KM_RRAI", "RADAR_1KM_RSNO", COVERAGE_LAYER}
    assert sampled[0]["time"] == "2026-10-10T14:18:00Z" and sampled[0]["crs"] == "EPSG:4326"


def test_radar_latest_only():
    rows = weather.fetch_radar(SITE, latest_only=True, get=FakeWeb(covered=False), now=lambda: NOW)
    assert len(rows) == 1 and rows[0].frame_at == at(14, 18)
    assert rows[0].rain_mm_h == 0.0 and rows[0].rain_class == "Undetected" and rows[0].covered is False
    assert "NOT covered" in weather.format_radar(rows[0])


def test_radar_failure_ends_the_round_with_one_error_row():
    def fail(url, params):
        if params.get("time") == "2026-10-10T14:12:00Z":
            return Response(502, "Bad Gateway")

    rows = weather.fetch_radar(SITE, get=FakeWeb(fail=fail), now=lambda: NOW)
    assert [(r.frame_at, r.error) for r in rows] == [(at(14, 18), None), (at(14, 12), "HTTP 502\nBad Gateway")]


def test_radar_listing_failure():
    rows = weather.fetch_radar(SITE, get=FakeWeb(fail=lambda u, p: Response(None, "no response: DNS")), now=lambda: NOW)
    assert len(rows) == 1 and rows[0].frame_at is None and "listing the radar frames failed" in rows[0].error
    assert "no response: DNS" in rows[0].error


def test_save_capture_never_overwrites(tmp_path):
    first = weather.save_capture(tmp_path / "c", "open-meteo_60min", NOW, "one")
    second = weather.save_capture(tmp_path / "c", "open-meteo_60min", NOW, "two")
    assert first.name == "open-meteo_60min_2026-10-10T142030Z.json" and second.name.endswith("Z_2.json")
    assert first.read_text(encoding="utf-8") == "one" and second.read_text(encoding="utf-8") == "two"


# ---------- background ----------
def test_background_runs_each_job_at_once_then_on_its_interval():
    runs = []
    quick = weather.Job("quick", 0.05, lambda: runs.append("quick"))
    slow = weather.Job("slow", 3600, lambda: runs.append("slow"))
    bg = weather.Background([quick, slow], err=lambda msg: None)
    bg.start()
    time.sleep(0.3)
    started = time.monotonic()
    bg.stop()
    assert time.monotonic() - started < 0.5  # doesn't wait for the hourly job
    assert runs.count("slow") == 1 and runs.count("quick") >= 3


def test_background_stop_wakes_a_long_wait_at_once():
    ran = threading.Event()
    bg = weather.Background([weather.Job("hourly", 3600, ran.set)], err=lambda msg: None)
    bg.start()
    assert ran.wait(1)
    started = time.monotonic()
    bg.stop()
    assert time.monotonic() - started < 0.2 and not bg._thread.is_alive()


def test_background_survives_a_failing_job():
    errors, runs = [], []

    def boom():
        runs.append(1)
        raise RuntimeError("kaboom")

    bg = weather.Background([weather.Job("boom", 0.05, boom)], err=errors.append)
    bg.start()
    time.sleep(0.2)
    bg.stop()
    assert len(runs) >= 2 and "warning: boom failed: RuntimeError: kaboom" in errors[0]


# ---------- config ----------
def write_config(tmp_path, doc):
    f = tmp_path / "config.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    return f


def test_config_site(tmp_path):
    config = load_config(
        write_config(tmp_path, {"site": {"latitude": 51.04, "longitude": -114, "timezone": "America/Edmonton"}})
    )
    assert config.require_site() == Site(51.04, -114.0, "America/Edmonton")
    with pytest.raises(ConfigError, match='add "site"'):
        load_config(write_config(tmp_path, {})).require_site()


@pytest.mark.parametrize(
    "site, message",
    [
        ({"latitude": 50, "longitude": -114}, '"site" must be'),
        ({"latitude": 91, "longitude": -114, "timezone": "UTC"}, "site.latitude must be a number from -90 to 90"),
        ({"latitude": 50, "longitude": "west", "timezone": "UTC"}, "site.longitude"),
        ({"latitude": True, "longitude": -114, "timezone": "UTC"}, "site.latitude"),
        ({"latitude": 50, "longitude": -114, "timezone": "MST"}, "IANA name"),
        ({"latitude": 50, "longitude": -114, "timezone": "America/Edmontn"}, "isn't a time zone this computer knows"),
    ],
)
def test_config_site_rejects(tmp_path, site, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, {"site": site}))


# ---------- CLI (no database) ----------
@pytest.fixture
def site_config(tmp_path):
    return write_config(
        tmp_path, {"site": {"latitude": 51.0443, "longitude": -114.0631, "timezone": "America/Edmonton"}}
    )


def test_cli_weather(run, site_config, monkeypatch, tmp_path):
    monkeypatch.setattr(weather, "http_get", FakeWeb.around_now())
    code, out, _ = run("weather", "--config", site_config, "--capture", tmp_path / "cap")
    assert code == ExitCode.OK and "Open-Meteo 60 min" in out and "Open-Meteo 15 min" in out
    assert "stored" not in out
    assert sorted(p.name.split("_2026")[0] for p in (tmp_path / "cap").iterdir()) == [
        "open-meteo_15min",
        "open-meteo_60min",
    ]


def test_cli_weather_one_resolution_and_failure(run, site_config, monkeypatch):
    monkeypatch.setattr(weather, "http_get", FakeWeb(fail=lambda u, p: Response(500, "oops")))
    code, out, _ = run("weather", "--config", site_config, "--resolution", 15)
    assert code == ExitCode.CONNECTION and "Open-Meteo 15 min" in out and "60 min" not in out and "FAILED" in out


def test_cli_radar(run, site_config, monkeypatch, tmp_path):
    monkeypatch.setattr(weather, "http_get", FakeWeb())
    code, out, _ = run("radar", "--config", site_config, "--capture", tmp_path / "cap")
    assert code == ExitCode.OK and out.startswith("radar frame 2026-10-10 14:18Z: rain 0 mm/h (Undetected)")
    assert len(list((tmp_path / "cap").iterdir())) == 3  # rain, snow, coverage


def test_cli_weather_needs_a_site(run, tmp_path):
    code, _, err = run("weather", "--config", write_config(tmp_path, {}))
    assert code == ExitCode.USAGE and 'add "site"' in err


def test_poll_capture_needs_weather(run, tmp_path):
    config = write_config(tmp_path, {"database": {"url": "postgresql://nobody@127.0.0.1:1/x"}})
    code, _, err = run("poll", "--ip", "127.0.0.1", "--config", config, "--capture-open-meteo-weather", tmp_path)
    assert code == ExitCode.USAGE and "needs --weather" in err
    code, _, err = run("poll", "--ip", "127.0.0.1", "--config", config, "--capture-radar", tmp_path)
    assert code == ExitCode.USAGE and "needs --radar" in err
