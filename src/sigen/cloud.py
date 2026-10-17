"""Talk to the Sigen cloud (api-us.sigencloud.com), the same API the mySigen app uses, for the few things the local
Modbus link can't give us -- above all the LoadHub/gateway's grid and contactor data, and each battery pack's
heating status (the LoadHub has no Modbus at all).

Standard library for HTTP (urllib), like sigen.ntfy; the only heavier need is AES for the password, and that is
imported lazily inside login(), so refresh / query / poll run on the standard library alone.

**Auth.** A password login returns a short-lived access token (~12 h) and a refresh token; the cloud may first ask
for an emailed security code (MFA). The refresh token is **single-use**: each refresh issues a new one, so we must
persist it atomically every time. The token file (gitignored; config's cloud.token_file) holds:
    {access_token, refresh_token, expires_in, obtained_at, expires_at, device_id}
`login()` is the one interactive step (password + MFA); after that `refresh()` keeps the access token alive with no
password. Only one process should refresh at a time (poll owns it, keeping >1 h of life so queries never refresh).

The password is AES-128-CBC with a static key built into the app (key = IV, the same for every install), PKCS7,
base64: obfuscation inside HTTPS, not real encryption. That key and the app's OAuth client credential are NOT
shipped in this code; they come from the config (cloud.obfuscation_key, cloud.client_basic), so the repo carries
neither. We never store the password.
"""
import datetime
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

from sigen.errors import CloudError

DEFAULT_BASE_URL = "https://api-us.sigencloud.com"
DEFAULT_APP_URL = "https://app-us.sigencloud.com"      # the web-app host, sent as origin/referer (region-specific)
TOKEN_PATH = "auth/oauth/token"
REFRESH_MARGIN = 3600.0                      # refresh once the access token has less than this many seconds left
TIMEOUT = 20.0

# device types seen in the topology / device APIs
DEVICE_AIO, DEVICE_INVERTER, DEVICE_BATTERY, DEVICE_GATEWAY = 2, 3, 4, 8


def encode_password(password: str, key: bytes) -> str:
    """The password as the app encodes it: AES-128-CBC, key = IV = `key` (the app's 16-byte static key), PKCS7,
    base64. `key` is NOT shipped in this code -- it comes from the config via CloudClient. Only login() calls this."""
    import base64
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise CloudError("`sigen cloud login` needs the cryptography package: pip install cryptography "
                         "(or pip install -e \".[dev]\").")
    if len(key) != 16:
        raise CloudError(f"cloud.obfuscation_key must be 16 bytes (it is the AES-128 key and the IV), got {len(key)}")
    padder = padding.PKCS7(128).padder()
    data = padder.update(password.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode("ascii")


class Tokens:
    """What's in the token file, with the derived timing. access_token/refresh_token may be None before a login."""

    def __init__(self, data: dict):
        self.access_token: Optional[str] = data.get("access_token")
        self.refresh_token: Optional[str] = data.get("refresh_token")
        self.expires_in = data.get("expires_in")
        self.obtained_at = data.get("obtained_at")
        self.expires_at = data.get("expires_at")
        self.device_id: Optional[str] = data.get("device_id")

    def seconds_left(self) -> float:
        """Seconds until the access token expires (negative if already expired); +inf if unknown."""
        if not self.expires_at:
            return float("inf")
        when = datetime.datetime.fromisoformat(self.expires_at)
        return (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds()

    def as_dict(self) -> dict:
        return {"access_token": self.access_token, "refresh_token": self.refresh_token,
                "expires_in": self.expires_in, "obtained_at": self.obtained_at,
                "expires_at": self.expires_at, "device_id": self.device_id}


class CloudClient:
    """Auth and read-only GETs against the Sigen cloud, backed by a token file. Never raises for the sake of a token
    that merely needs refreshing -- it refreshes; it raises CloudError only when it can't recover (unreachable, an
    API error, or a refresh/login that was rejected)."""

    def __init__(self, token_file: Path, base_url: str = DEFAULT_BASE_URL, timeout: float = TIMEOUT,
                 opener: Optional[Callable] = None, *, app_base_url: str = DEFAULT_APP_URL,
                 obfuscation_key: Optional[str] = None, client_basic: Optional[str] = None):
        self.token_file = Path(token_file)
        self.base_url = base_url.rstrip("/")
        self.app_base_url = app_base_url.rstrip("/")   # origin/referer host (region-specific)
        self.timeout = timeout
        self._opener = opener or self._urlopen         # tests inject a stand-in
        # The app's static constants, NOT shipped in this code: the password key (login) and the OAuth client
        # Basic credential (login + refresh). They come from the config, so they aren't committed here.
        self.obfuscation_key = obfuscation_key
        self.client_basic = client_basic

    # ---------- token file ----------
    def load(self) -> Tokens:
        if not self.token_file.exists():
            raise CloudError(f"no token file {self.token_file}: run `sigen cloud login` first.")
        try:
            return Tokens(json.loads(self.token_file.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            raise CloudError(f"can't read the token file {self.token_file}: {e}")

    def _save(self, tokens: Tokens) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_file.with_suffix(self.token_file.suffix + ".tmp")
        tmp.write_text(json.dumps(tokens.as_dict(), indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, self.token_file)               # atomic: never leaves a half-written token file

    def _device_id(self) -> str:
        """A stable per-install id, kept in the token file (made once)."""
        if self.token_file.exists():
            dev = Tokens(json.loads(self.token_file.read_text(encoding="utf-8"))).device_id
            if dev:
                return dev
        return uuid.uuid4().hex

    # ---------- HTTP ----------
    def _urlopen(self, request):
        return urllib.request.urlopen(request, timeout=self.timeout)

    def _request(self, method: str, url: str, headers: dict, data: Optional[bytes] = None):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener(req) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            raise CloudError(f"couldn't reach the Sigen cloud at {url}: {getattr(e, 'reason', e)}")

    def _headers(self, access_token: Optional[str] = None, form: bool = False) -> dict:
        h = {
            "accept": "*/*", "accept-language": "en_US", "lang": "en_US",
            "auth-client-id": "sigen", "client-server": "us",
            "origin": self.app_base_url, "referer": self.app_base_url + "/",
            "sg-pkg": "sigen_app", "sg-platform": "web", "sg-source": "flutterweb",
            "sg-v": "4.1.0", "sg-app-version": "4.1.0", "sg-env": "1", "sg-bui": "1",
            "sg-log-id": str(uuid.uuid4()), "sg-session": str(uuid.uuid4()),
            "user-agent": "sigen-modbus (github.com/maludwig/sigen-modbus)",
        }
        if form:
            h["content-type"] = "application/x-www-form-urlencoded"
            if not self.client_basic:
                raise CloudError("cloud.client_basic is not set in the config: `sigen cloud login` and `refresh` "
                                 "need the app's OAuth client credential (see the README). A query with a valid "
                                 "token does not need it.")
            h["authorization"] = "Basic " + self.client_basic
        else:
            h["content-type"] = "application/json; charset=utf-8"
        if access_token:
            h["authorization"] = "bearer " + access_token
        return h

    def _token_post(self, form: dict) -> dict:
        """POST the OAuth token endpoint; return the parsed JSON body (any `code`), or raise CloudError on a
        transport/HTTP failure."""
        data = urllib.parse.urlencode(form).encode()
        status, text = self._request("POST", f"{self.base_url}/{TOKEN_PATH}", self._headers(form=True), data)
        try:
            body = json.loads(text)
        except ValueError:
            raise CloudError(f"the token endpoint returned HTTP {status}, not JSON: {text[:200]}")
        if status >= 400:
            raise CloudError(f"the token endpoint returned HTTP {status}: {json.dumps(_redact(body))[:300]}")
        return body

    # ---------- auth ----------
    def _obfuscation_key_bytes(self) -> bytes:
        if not self.obfuscation_key:
            raise CloudError("cloud.obfuscation_key is not set in the config: `sigen cloud login` needs the app's "
                             "static password key (see the README). Refresh and query do not need it.")
        return self.obfuscation_key.encode("utf-8")

    def login(self, username: str, password: str, mfa_prompt: Optional[Callable[[dict], str]] = None) -> Tokens:
        """Password login, saving the tokens. If the cloud asks for a security code and `mfa_prompt` is given, it is
        called with the response to get the code and a second request is made. Raises CloudError if not logged in.
        One attempt per request: the account locks after a few failures, so nothing is retried."""
        dev = self._device_id()
        body = self._token_post({"scope": "server", "grant_type": "password", "user_device_id": dev,
                                 "username": username, "password": encode_password(password,
                                                                                   self._obfuscation_key_bytes())})
        tokens = _tokens_from(body, dev)
        if tokens:
            self._save(tokens)
            return tokens
        challenge = _challenge_id(body)
        if challenge and mfa_prompt:
            code = mfa_prompt(body)
            if code:
                body = self._token_post({"scope": "server", "grant_type": "login_mfa", "user_device_id": dev,
                                         "challenge_id": challenge, "verification_code": code})
                tokens = _tokens_from(body, dev)
                if tokens:
                    self._save(tokens)
                    return tokens
        raise CloudError(f"not logged in: code {body.get('code')}, {body.get('msg')!r}")

    def refresh(self) -> Tokens:
        """Exchange the saved (single-use) refresh token for a fresh access token and a new refresh token, and save
        them atomically. Raises CloudError if there is no refresh token or the cloud rejects it."""
        current = self.load()
        if not current.refresh_token:
            raise CloudError(f"{self.token_file} has no refresh token: run `sigen cloud login`.")
        body = self._token_post({"scope": "server", "grant_type": "refresh_token",
                                 "refresh_token": current.refresh_token})
        tokens = _tokens_from(body, current.device_id)
        if not tokens:
            raise CloudError(f"refresh rejected: code {body.get('code')}, {body.get('msg')!r}. The refresh token "
                             f"expires too, or was already used; run `sigen cloud login` again.")
        self._save(tokens)
        return tokens

    def ensure_fresh(self, margin: float = REFRESH_MARGIN) -> Tokens:
        """Return valid tokens, refreshing first if the access token expires within `margin` seconds. The one place
        that decides when to refresh, so queries never have to."""
        tokens = self.load()
        if tokens.seconds_left() < margin:
            return self.refresh()
        return tokens

    def access_token(self, refresh_if_needed: bool = False) -> str:
        tokens = self.ensure_fresh() if refresh_if_needed else self.load()
        if not tokens.access_token:
            raise CloudError(f"{self.token_file} has no access token: run `sigen cloud login`.")
        return tokens.access_token

    # ---------- read-only queries ----------
    def get(self, path: str, params: Optional[dict] = None, *, refresh_if_needed: bool = False):
        """GET an API path (e.g. "/device/gateway/<id>") and return its `data`. refresh_if_needed only for callers
        that own refreshing (poll); plain queries pass False and fail cleanly on an expired token."""
        token = self.access_token(refresh_if_needed=refresh_if_needed)
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, text = self._request("GET", url, self._headers(access_token=token))
        if status == 401:
            raise CloudError("the access token is expired or invalid (HTTP 401): run `sigen cloud refresh` "
                             "(or `sigen cloud login`).")
        try:
            body = json.loads(text)
        except ValueError:
            raise CloudError(f"{path} returned HTTP {status}, not JSON: {text[:200]}")
        if status >= 400 or body.get("code") not in (0, None):
            raise CloudError(f"{path}: HTTP {status}, API code {body.get('code')}, {body.get('msg')!r}")
        return body.get("data")

    def device_dynamic_info(self, station_id: str, sn_code: str, device_type: int):
        """The realTimeInfo list for one device (battery heating status, SOC, etc.)."""
        return self.get("/device/sigen/device/dynamic/info",
                        {"stationId": station_id, "deviceType": device_type, "snCode": sn_code, "refreshFlag": ""},
                        refresh_if_needed=False)

    def gateway(self, station_id: str):
        """The LoadHub/gateway's grid, inverter, generator measurements and contactor states."""
        return self.get(f"/device/gateway/{station_id}")


# ---------- helpers ----------
def _tokens_from(body: dict, device_id: Optional[str]) -> Optional[Tokens]:
    """Build Tokens from a token-endpoint body if it carries an access token, stamping the timing; else None."""
    data = body if body.get("access_token") or body.get("accessToken") else body.get("data")
    if not isinstance(data, dict) or not (data.get("access_token") or data.get("accessToken")):
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    expires_in = data.get("expires_in", data.get("expiresIn"))
    return Tokens({
        "access_token": data.get("access_token") or data.get("accessToken"),
        "refresh_token": data.get("refresh_token") or data.get("refreshToken"),
        "expires_in": expires_in,
        "obtained_at": now.isoformat(),
        "expires_at": (now + datetime.timedelta(seconds=int(expires_in))).isoformat() if expires_in else None,
        "device_id": device_id,
    })


def _challenge_id(body: dict):
    d = body.get("data") if isinstance(body.get("data"), dict) else {}
    return d.get("challenge_id") or d.get("challengeId") or body.get("challenge_id")


def _redact(obj):
    secret = {"access_token", "refresh_token", "accessToken", "refreshToken", "password", "verification_code"}
    if isinstance(obj, dict):
        return {k: ("***" if k in secret else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj
