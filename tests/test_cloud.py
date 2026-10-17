"""The config's cloud section, sigen.cloud.CloudClient and `sigen cloud …`, against a local stand-in for the
Sigen cloud. No network, no real credentials."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from sigen import cloud
from sigen.cloud import CloudClient, Tokens
from sigen.config import Cloud, load_config
from sigen.errors import CloudError, ConfigError, ExitCode

STATION = "1234567890123456"
BAT1, BAT2 = "110AXX1234000A", "110AXX1234000B"
TEST_KEY = "test-key-16bytes"       # 16 bytes: a stand-in for the app's static AES key (not the real one)
TEST_BASIC = "dGVzdDp0ZXN0"         # base64("test:test"): a stand-in for the app's OAuth client credential


@pytest.fixture
def fake_cloud():
    """A local server that answers like api-us.sigencloud.com and records each request. Behaviour is set on the
    server object: .mfa_first (challenge before tokens), .reject (a non-zero token code), .heating ({sn: "On"})."""
    token_seq = {"n": 0}

    def make_tokens(server):
        token_seq["n"] += 1
        i = token_seq["n"]
        return {"code": 0, "msg": "success", "data": {
            "access_token": f"access-{i}", "refresh_token": f"refresh-{i}", "expires_in": server.expires_in}}

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, obj, status=200):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            grant = form.get("grant_type", [""])[0]
            server.requests.append({"path": self.path, "grant": grant, "form": form,
                                    "auth": self.headers.get("authorization")})
            if server.reject:
                return self._reply({"code": 10401, "msg": "invalid"})
            if grant == "password" and server.mfa_first:
                return self._reply({"code": 2001, "msg": "need code", "data": {"challenge_id": "chal-1"}})
            self._reply(make_tokens(server))

        def do_GET(self):
            u = urlparse(self.path)
            server.requests.append({"path": u.path, "query": parse_qs(u.query),
                                    "auth": self.headers.get("authorization")})
            if server.get_status == 401:
                return self._reply({"code": 10401, "msg": "unauthorized"}, status=401)
            if u.path.startswith("/device/gateway/"):
                return self._reply({"code": 0, "msg": "success", "data": {"deviceModel": "Sigen LoadHub"}})
            sn = parse_qs(u.query).get("snCode", [""])[0]
            heating = server.heating.get(sn, "Off")
            self._reply({"code": 0, "msg": "success", "data": {"realTimeInfo": [
                {"paramKey": "Battery SOC", "paramValue": "52.0 %"},
                {"paramKey": "Heating status", "paramValue": heating}]}})

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests, server.expires_in = [], 43199
    server.mfa_first = server.reject = False
    server.get_status = 200
    server.heating = {}
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def client(tmp_path, fake_cloud):
    return CloudClient(tmp_path / "token.json", base_url=fake_cloud.url,
                       obfuscation_key=TEST_KEY, client_basic=TEST_BASIC)


# ---------- config ----------
def config_file(tmp_path, cloud_section):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"cloud": cloud_section}), encoding="utf-8")
    return f


def test_cloud_section(tmp_path):
    c = load_config(config_file(tmp_path, {"token_file": "tok.json", "station_id": STATION,
                                           "batteries": [BAT1, BAT2]}))
    assert c.cloud.token_file == (tmp_path / "tok.json").resolve()
    assert c.cloud.base_url == "https://api-us.sigencloud.com"      # the default
    assert c.cloud.app_base_url == "https://app-us.sigencloud.com"  # the default
    assert c.cloud.station_id == STATION and c.cloud.batteries == (BAT1, BAT2)
    assert c.cloud.obfuscation_key is None and c.cloud.client_basic is None      # both optional
    c = load_config(config_file(tmp_path, {"token_file": "t", "base_url": "http://192.168.1.5:8080/",
                                           "app_base_url": "http://192.168.1.5:9090/"}))
    assert c.cloud.base_url == "http://192.168.1.5:8080" and c.cloud.app_base_url == "http://192.168.1.5:9090"
    assert c.cloud.batteries == ()
    c = load_config(config_file(tmp_path, {"token_file": "t", "obfuscation_key": TEST_KEY, "client_basic": TEST_BASIC}))
    assert c.cloud.obfuscation_key == TEST_KEY and c.cloud.client_basic == TEST_BASIC


def test_cloud_section_invalid(tmp_path):
    for bad in ({}, {"base_url": "x"}, {"token_file": ""}, {"token_file": "t", "base_url": "ftp://x"},
                {"token_file": "t", "station_id": ""}, {"token_file": "t", "batteries": "x"},
                {"token_file": "t", "batteries": [1]}, {"token_file": "t", "nope": 1},
                {"token_file": "t", "obfuscation_key": ""}, {"token_file": "t", "client_basic": 5},
                {"token_file": "t", "app_base_url": "ftp://x"}):
        with pytest.raises(ConfigError, match="cloud"):
            load_config(config_file(tmp_path, bad))


def test_require_cloud(tmp_path):
    with pytest.raises(ConfigError, match='add\\s+"cloud"'):
        load_config(None).require_cloud()


# ---------- password encoding ----------
def test_encode_password_roundtrips():
    # AES-128-CBC, key = IV = the given key, PKCS7, base64: deterministic (fixed key+IV) and reversible. Verified by
    # decrypting, so no real app key is used or needed here.
    import base64

    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = TEST_KEY.encode()
    enc = cloud.encode_password("hunter2", key)
    assert cloud.encode_password("hunter2", key) == enc              # deterministic
    dec = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    raw = dec.update(base64.b64decode(enc)) + dec.finalize()
    unpad = padding.PKCS7(128).unpadder()
    assert unpad.update(raw) + unpad.finalize() == b"hunter2"


def test_encode_password_wrong_key_length():
    with pytest.raises(CloudError, match="16 bytes"):
        cloud.encode_password("x", b"too-short")


# ---------- login ----------
def test_login_saves_tokens(tmp_path, fake_cloud):
    c = client(tmp_path, fake_cloud)
    tokens = c.login("me@example.com", "pw")
    assert tokens.access_token == "access-1" and tokens.refresh_token == "refresh-1"
    on_disk = json.loads((tmp_path / "token.json").read_text())
    assert on_disk["access_token"] == "access-1" and on_disk["device_id"]
    pw = fake_cloud.requests[0]["form"]["password"][0]
    assert pw != "pw" and "password" not in on_disk          # encoded on the wire, never stored


def test_login_mfa(tmp_path, fake_cloud):
    fake_cloud.mfa_first = True
    c = client(tmp_path, fake_cloud)
    asked = {}

    def mfa(body):
        asked["code"] = body.get("code")
        return "123456"

    tokens = c.login("me@example.com", "pw", mfa)
    assert asked["code"] == 2001 and tokens.access_token
    assert [r["grant"] for r in fake_cloud.requests] == ["password", "login_mfa"]


def test_login_rejected(tmp_path, fake_cloud):
    fake_cloud.reject = True
    with pytest.raises(CloudError, match="not logged in"):
        client(tmp_path, fake_cloud).login("me@example.com", "pw")


def test_login_needs_obfuscation_key(tmp_path, fake_cloud):
    c = CloudClient(tmp_path / "token.json", base_url=fake_cloud.url, client_basic=TEST_BASIC)
    with pytest.raises(CloudError, match="obfuscation_key"):
        c.login("me@example.com", "pw")


def test_refresh_needs_client_basic(tmp_path, fake_cloud):
    (tmp_path / "token.json").write_text(json.dumps({"refresh_token": "r", "device_id": "d"}))
    c = CloudClient(tmp_path / "token.json", base_url=fake_cloud.url, obfuscation_key=TEST_KEY)
    with pytest.raises(CloudError, match="client_basic"):
        c.refresh()


# ---------- refresh ----------
def test_refresh_rotates_and_persists(tmp_path, fake_cloud):
    c = client(tmp_path, fake_cloud)
    c.login("me@example.com", "pw")
    before = json.loads((tmp_path / "token.json").read_text())
    tokens = c.refresh()
    after = json.loads((tmp_path / "token.json").read_text())
    assert tokens.refresh_token != before["refresh_token"]        # single-use: rotated
    assert after["access_token"] == tokens.access_token
    assert fake_cloud.requests[-1]["grant"] == "refresh_token"
    assert fake_cloud.requests[-1]["form"]["refresh_token"][0] == before["refresh_token"]


def test_refresh_without_token(tmp_path, fake_cloud):
    (tmp_path / "token.json").write_text(json.dumps({"access_token": "a"}))      # no refresh_token
    with pytest.raises(CloudError, match="no refresh token"):
        client(tmp_path, fake_cloud).refresh()


def test_refresh_rejected(tmp_path, fake_cloud):
    c = client(tmp_path, fake_cloud)
    c.login("me@example.com", "pw")
    fake_cloud.reject = True
    with pytest.raises(CloudError, match="refresh rejected"):
        c.refresh()


# ---------- ensure_fresh ----------
def test_ensure_fresh_refreshes_only_when_near_expiry(tmp_path, fake_cloud):
    c = client(tmp_path, fake_cloud)
    c.login("me@example.com", "pw")           # 43199 s left: well above the margin
    n = len(fake_cloud.requests)
    c.ensure_fresh()
    assert len(fake_cloud.requests) == n      # no refresh
    fake_cloud.expires_in = 10                # next token expires almost at once
    c.refresh()
    c.ensure_fresh()                          # now under the margin -> refreshes
    assert fake_cloud.requests[-1]["grant"] == "refresh_token"


# ---------- get ----------
def test_get_and_heating_status(tmp_path, fake_cloud):
    fake_cloud.heating = {BAT1: "On"}
    c = client(tmp_path, fake_cloud)
    c.login("me@example.com", "pw")
    data = c.device_dynamic_info(STATION, BAT1, cloud.DEVICE_BATTERY)
    rows = {r["paramKey"]: r["paramValue"] for r in data["realTimeInfo"]}
    assert rows["Heating status"] == "On"
    assert fake_cloud.requests[-1]["auth"] == "bearer access-1"
    assert c.gateway(STATION)["deviceModel"] == "Sigen LoadHub"


def test_get_401(tmp_path, fake_cloud):
    c = client(tmp_path, fake_cloud)
    c.login("me@example.com", "pw")
    fake_cloud.get_status = 401
    with pytest.raises(CloudError, match="401"):
        c.gateway(STATION)


def test_get_does_not_refresh(tmp_path, fake_cloud):
    """A plain query uses the current token and never refreshes (poll owns refreshing)."""
    c = client(tmp_path, fake_cloud)
    c.login("me@example.com", "pw")
    fake_cloud.expires_in = 10
    c.refresh()                                           # token now nearly expired, but still present
    n_before = sum(r.get("grant") == "refresh_token" for r in fake_cloud.requests)
    c.gateway(STATION)
    n_after = sum(r.get("grant") == "refresh_token" for r in fake_cloud.requests)
    assert n_after == n_before                            # get() did not refresh


def test_seconds_left_unknown_is_infinite():
    assert Tokens({}).seconds_left() == float("inf")


# ---------- CLI ----------
def cli_config(tmp_path, fake_cloud, token=True):
    if token:
        (tmp_path / "tok.json").write_text(json.dumps(
            {"access_token": "seed-access", "refresh_token": "seed-refresh", "device_id": "d"}))
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"cloud": {"token_file": "tok.json", "base_url": fake_cloud.url,
                                       "obfuscation_key": TEST_KEY, "client_basic": TEST_BASIC,
                                       "station_id": STATION, "batteries": [BAT1, BAT2]}}), encoding="utf-8")
    return f


def test_cli_query(run, tmp_path, fake_cloud):
    fake_cloud.heating = {BAT1: "On", BAT2: "Off"}
    code, out, err = run("cloud", "query", "--config", cli_config(tmp_path, fake_cloud))
    assert code == ExitCode.OK
    assert BAT1 in out and "Heating status" in out and "On" in out


def test_cli_query_path(run, tmp_path, fake_cloud):
    code, out, err = run("cloud", "query", "--path", f"/device/gateway/{STATION}",
                         "--config", cli_config(tmp_path, fake_cloud))
    assert code == ExitCode.OK and "Sigen LoadHub" in out


def test_cli_refresh(run, tmp_path, fake_cloud):
    code, out, err = run("cloud", "refresh", "--config", cli_config(tmp_path, fake_cloud))
    assert code == ExitCode.OK and "Refreshed" in out
    after = json.loads((tmp_path / "tok.json").read_text())
    assert after["access_token"] != "seed-access" and after["refresh_token"] != "seed-refresh"


def test_cli_query_no_station(run, tmp_path, fake_cloud):
    f = tmp_path / "config.json"
    (tmp_path / "tok.json").write_text(json.dumps({"access_token": "a", "refresh_token": "r"}))
    f.write_text(json.dumps({"cloud": {"token_file": "tok.json", "base_url": fake_cloud.url}}), encoding="utf-8")
    code, out, err = run("cloud", "query", "--config", f)
    assert code == ExitCode.USAGE and "station_id" in err
