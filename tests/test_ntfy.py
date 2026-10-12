"""The config's alerts section, sigen.ntfy and `sigen ntfy-test`, against a local stand-in for an ntfy server."""
import datetime
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sigen import ntfy
from sigen.config import Alerts, load_config
from sigen.errors import ConfigError, ConnectionFailed, ExitCode

TOPIC = "someone-solar-1a2b3c4d"


@pytest.fixture
def fake_ntfy():
    """A local server that answers like ntfy.sh and records each request; status 200 unless `.status` is set."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            server.requests.append({"path": self.path, "headers": dict(self.headers), "body": body.decode("utf-8")})
            reply = (json.dumps({"id": "abc123", "event": "message", "topic": self.path[1:]})
                     if server.status == 200 else '{"code":42901,"error":"limit reached"}').encode()
            self.send_response(server.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests, server.status = [], 200
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def config_file(tmp_path, alerts):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"alerts": alerts}), encoding="utf-8")
    return f


# ---------- config ----------
def test_alerts_section(tmp_path):
    c = load_config(config_file(tmp_path, {"ntfy_topic": TOPIC}))
    assert c.alerts == Alerts(TOPIC, "https://ntfy.sh") and TOPIC not in repr(c)
    c = load_config(config_file(tmp_path, {"ntfy_topic": TOPIC, "ntfy_server": "http://192.168.1.5:8080/"}))
    assert c.alerts.ntfy_server == "http://192.168.1.5:8080"
    for bad in ({}, {"ntfy_topic": "has space"}, {"ntfy_topic": "x" * 65}, {"ntfy_topic": TOPIC, "token": "t"},
                {"ntfy_topic": TOPIC, "ntfy_server": "ntfy.sh"}, "just-a-topic"):
        with pytest.raises(ConfigError, match="alerts"):
            load_config(config_file(tmp_path, bad))


def test_require_alerts(tmp_path):
    with pytest.raises(ConfigError, match='add "alerts"'):
        load_config(None).require_alerts()


def test_masked():
    assert ntfy.masked(TOPIC) == "someone-solar-********"
    assert ntfy.masked("abcdefgh") == "ab******"            # no random-looking tail: only the first quarter
    assert ntfy.masked("solar-ab") == "so******"


# ---------- sending ----------
def test_send(fake_ntfy):
    alerts = Alerts(TOPIC, fake_ntfy.url)
    a = ntfy.send(alerts, "Coolest cluster 4.9 °C", title="Battery cold", priority="high", tags=["snowflake"],
                  kind="cold")
    assert (a.ok, a.http_code, a.ntfy_id, a.error, a.kind, a.tags) == (True, 200, "abc123", None, "cold", ("snowflake",))
    (req,) = fake_ntfy.requests
    assert req["path"] == f"/{TOPIC}" and req["body"] == "Coolest cluster 4.9 °C"
    assert (req["headers"]["Title"], req["headers"]["Priority"], req["headers"]["Tags"]) == ("Battery cold", "high",
                                                                                             "snowflake")
    with pytest.raises(ValueError):
        ntfy.send(alerts, "x", priority="loud")


def test_post_says_how_it_went(fake_ntfy):
    fake_ntfy.status = 429
    a = ntfy.post(Alerts(TOPIC, fake_ntfy.url), "x")
    assert (a.ok, a.http_code, a.ntfy_id) == (False, 429, None)
    assert a.error == '{"code":42901,"error":"limit reached"}'                 # the whole response body
    a = ntfy.post(Alerts(TOPIC, "http://127.0.0.1:1"), "x", timeout=2)
    assert (a.ok, a.http_code) == (False, None) and a.error
    s = ntfy.suppressed("still cold", kind="cold", priority="high")
    assert (s.was_suppressed, s.http_code, s.error, s.kind) == (True, None, None, "cold")


def test_send_failures(fake_ntfy):
    fake_ntfy.status = 429
    with pytest.raises(ConnectionFailed, match=r"refused the message: HTTP 429 .*limit reached") as e:
        ntfy.send(Alerts(TOPIC, fake_ntfy.url), "x")
    assert TOPIC not in str(e.value) and "someone-solar-********" in str(e.value)
    with pytest.raises(ConnectionFailed, match="couldn't reach ntfy"):
        ntfy.send(Alerts(TOPIC, "http://127.0.0.1:1"), "x", timeout=2)


# ---------- the command ----------
def test_ntfy_test_command(run, tmp_path, fake_ntfy):
    cfg = config_file(tmp_path, {"ntfy_topic": TOPIC, "ntfy_server": fake_ntfy.url})
    code, out, _ = run("ntfy-test", "--config", cfg, "--msg", "Hello world", "--priority", "low")
    assert code == ExitCode.OK and "someone-solar-********" in out and "abc123" in out and TOPIC not in out
    assert fake_ntfy.requests[0]["body"] == "Hello world" and fake_ntfy.requests[0]["headers"]["Priority"] == "low"
    fake_ntfy.status = 500
    code, _, err = run("ntfy-test", "--config", cfg)
    assert code == ExitCode.CONNECTION and "HTTP 500" in err and TOPIC not in err


def test_ntfy_server_flag(run, tmp_path, fake_ntfy):
    cfg = config_file(tmp_path, {"ntfy_topic": TOPIC, "ntfy_server": "http://127.0.0.1:1"})     # nothing there
    code, out, _ = run("ntfy-test", "--config", cfg, "--ntfy-server", fake_ntfy.url + "/")
    assert code == ExitCode.OK and out.startswith(f"sent to {fake_ntfy.url}/someone-solar-********")
    code, _, err = run("ntfy-test", "--config", cfg)
    assert code == ExitCode.CONNECTION and "couldn't reach ntfy at http://127.0.0.1:1/" in err
    with pytest.raises(SystemExit) as e:
        run("ntfy-test", "--config", cfg, "--ntfy-server", "localhost:8051")
    assert e.value.code == ExitCode.USAGE


def test_ntfy_test_needs_alerts(run):
    code, _, err = run("ntfy-test")                         # the tests' config is empty
    assert code == ExitCode.USAGE and 'add "alerts"' in err


# ---------- alert_log, with the test database ----------
psycopg = pytest.importorskip("psycopg")
from sigen import db  # noqa: E402
from test_db import config, conn, db_url  # noqa: E402,F401  (pytest fixtures; never request db_url in a test)


def test_alert_log(run, config, conn):
    run("db", "migrate", "--config", config)
    t = datetime.datetime(2026, 10, 12, 3, 0, tzinfo=datetime.timezone.utc)
    sent = ntfy.Attempt(t, "Coolest cluster 4.9 °C", title="Battery cold", priority="high", tags=("snowflake",),
                        kind="cold", http_code=200, ntfy_id="abc123")
    failed = ntfy.Attempt(t + datetime.timedelta(minutes=1), "x", kind="cold", http_code=429, error="limit reached")
    later = ntfy.Attempt(t + datetime.timedelta(minutes=2), "y", kind="cold", was_suppressed=True)
    for a in (sent, failed, later):
        db.insert_alert(conn, a)
    row = conn.execute("SELECT kind, was_suppressed, title, message, priority, tags, click, http_code, ntfy_id, error "
                       "FROM alert_log ORDER BY id LIMIT 1").fetchone()
    assert row == ("cold", False, "Battery cold", "Coolest cluster 4.9 °C", "high", ["snowflake"], None, 200, "abc123",
                   None)
    assert db.last_alerts_sent(conn) == {"cold": t + datetime.timedelta(minutes=1)}      # suppressed doesn't count
    assert db.status(conn)["alerts"][:3] == (1, 1, 1)
    with pytest.raises(Exception, match="check"):               # suppressed means nothing was sent
        db.insert_alert(conn, ntfy.Attempt(t, "z", was_suppressed=True, http_code=200))
    with pytest.raises(Exception, match="check"):
        db.insert_alert(conn, ntfy.Attempt(t, "z", priority="loud"))


def test_ntfy_test_records_the_attempt(run, config, conn, fake_ntfy):
    run("db", "migrate", "--config", config)
    doc = json.loads(config.read_text(encoding="utf-8"))
    doc["alerts"] = {"ntfy_topic": TOPIC, "ntfy_server": fake_ntfy.url}
    config.write_text(json.dumps(doc), encoding="utf-8")
    code, out, _ = run("ntfy-test", "--config", config, "--msg", "Hello world", "--record-to-db")
    assert code == ExitCode.OK and "stored as alert_log" in out
    fake_ntfy.status = 503
    code, out, err = run("ntfy-test", "--config", config, "--record-to-db")
    assert code == ExitCode.CONNECTION and "HTTP 503" in err and "stored as alert_log" in out
    rows = conn.execute("SELECT kind, message, http_code, ntfy_id, error IS NULL FROM alert_log ORDER BY id").fetchall()
    assert rows == [("test", "Hello world", 200, "abc123", True), ("test", "Hello world", 503, None, False)]
    code, out, _ = run("db", "status", "--config", config)
    assert "Alerts:        1 sent, 1 failed, 0 suppressed" in out
