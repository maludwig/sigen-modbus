"""The --config file: names for things the register map only numbers, and the database.

    {
      "smart_loads": [
        {"slot": 7, "name": "Basement Plugs"},
        {"slot": 8, "name": "Furnace"}
      ],
      "database": {"url": "postgresql://user:password@localhost:5432/sigen"},
      "devices": {"inverter": {"ip": "192.168.1.100", "mac": "02:ab:cd:ef:00:01", "units": [1]},
                  "loadhub": {"ip": "192.168.1.101", "mac": "02:ab:cd:ef:00:02"}},
      "site": {"latitude": 51.0443, "longitude": -114.0631, "timezone": "America/Edmonton"},
      "alerts": {"ntfy_topic": "someone-solar-1a2b3c4d", "ntfy_server": "https://ntfy.sh"}
    }

Smart-load names are display only: they appear in summaries and tables, never in dumps
or writes. The database URL is used by `sigen db …` and `sigen poll`, and is never printed
with its password. devices.inverter lets `sigen poll` run without --ip and, with a MAC,
find the inverter again if its IP changes (Windows, or Linux with `ip`). Its `units` are the inverters'
own Modbus unit IDs: read, capture and poll read those as well as the plant (247).
database.test_url (optional) is a separate database the database tests may use; nothing
else reads it. site is where the panels are: `sigen weather` and `sigen radar` (and `poll --weather`,
`--radar`) ask about that point. The timezone (an IANA name) is for anything that reports
in local time; the weather tables themselves are in UTC. alerts is where notifications go: an ntfy
topic (ntfy_server defaults to https://ntfy.sh). On a public server the topic name is the only
secret, so it lives here rather than in the code, and is masked in what sigen prints.

Which file: get_config() takes --config if given, else the file named by $SIGEN_CONFIG, else the
nearest config.json in the current directory or above it (the repo's, from anywhere inside it).
With none of them the config is empty, and a command that needs a section says where it looked.
"""
import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

from sigen.data_io import read_json_file
from sigen.errors import ConfigError, NotFound
from sigen.netlookup import normalize_mac

SMART_LOAD_SLOTS = range(1, 25)


@dataclass(frozen=True)
class Device:
    ip: Optional[str] = None
    mac: Optional[str] = None      # normalised, e.g. "02:ab:cd:ef:00:01"
    units: Tuple[int, ...] = ()    # inverter Modbus unit IDs behind this IP (the plant is always 247)


@dataclass(frozen=True)
class Site:
    latitude: float
    longitude: float
    timezone: str                  # IANA name, e.g. "America/Edmonton"
    altitude_m: Optional[float] = None


@dataclass(frozen=True)
class Panel:
    kw: float                      # rated (STC) power of one panel
    gamma_pct_per_c: float         # power temperature coefficient, % per °C (negative)
    mount: str                     # pvlib SAPM cell-temperature parameter set, e.g. "close_mount_glass_glass"


@dataclass(frozen=True)
class Face:
    tilt: float                    # degrees from horizontal
    azimuth: float                 # degrees clockwise from true north


@dataclass(frozen=True)
class PVString:
    panels: int
    face: str                      # a key of Array.faces


@dataclass(frozen=True)
class Array:
    panel: Panel
    albedo: float                  # ground reflectance
    faces: Dict[str, Face]
    strings: Dict[int, PVString]   # PV input number (1 = PV1) -> its string

    def kw(self, string: int) -> float:
        return self.strings[string].panels * self.panel.kw


DEFAULT_NTFY_SERVER = "https://ntfy.sh"


@dataclass(frozen=True)
class Alerts:
    ntfy_topic: str = field(repr=False)        # repr=False: anyone who knows it can read and send
    ntfy_server: str = DEFAULT_NTFY_SERVER


@dataclass(frozen=True)
class Config:
    smart_load_names: Dict[int, str] = field(default_factory=dict)   # slot -> name
    database_url: Optional[str] = field(default=None, repr=False)    # repr=False: holds a password
    devices: Dict[str, Device] = field(default_factory=dict)          # "inverter", "loadhub", …
    site: Optional[Site] = None
    test_database_url: Optional[str] = field(default=None, repr=False)   # only tests/test_db.py uses it
    alerts: Optional[Alerts] = None
    array: Optional[Array] = None
    # Where it came from, for messages: the file, "--no-config", or where get_config looked.
    origin: Optional[str] = field(default=None, compare=False)

    def _from(self) -> str:
        return f" ({self.origin})" if self.origin else ""

    def require_database_url(self) -> str:
        if not self.database_url:
            raise ConfigError(f'the config has no database URL{self._from()}; '
                              f'add "database": {{"url": "postgresql://…"}}')
        return self.database_url

    def require_site(self) -> Site:
        if self.site is None:
            raise ConfigError(f'the config has no site{self._from()}; add "site": {{"latitude": …, '
                              f'"longitude": …, "timezone": "America/Edmonton"}}')
        return self.site

    def require_alerts(self) -> Alerts:
        if self.alerts is None:
            raise ConfigError(f'the config has no alerts{self._from()}; add "alerts": {{"ntfy_topic": "…"}} '
                              f'(a long random name: on a public ntfy server anyone who knows it can read it)')
        return self.alerts

    def require_array(self) -> Array:
        if self.array is None:
            raise ConfigError(f'the config has no array{self._from()}; add "array": {{"panel": …, "albedo": …, '
                              f'"faces": …, "strings": …}} (see the README\'s config example)')
        return self.array

    def register_alias(self, register_name: str) -> Optional[str]:
        """'plant_smart_load_7_power' -> 'Basement Plugs', if slot 7 is named."""
        prefix = "plant_smart_load_"
        if register_name.startswith(prefix):
            slot = register_name[len(prefix):].split("_", 1)[0]
            if slot.isdigit():
                return self.smart_load_names.get(int(slot))
        return None


CONFIG_NAME = "config.json"
CONFIG_ENV = "SIGEN_CONFIG"


def find_config(start: Optional[Path] = None) -> Optional[Path]:
    """The nearest config.json: in `start` (default: the current directory) or the closest
    directory above it; None if there's none."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        if (directory / CONFIG_NAME).is_file():
            return directory / CONFIG_NAME
    return None


def get_config(path: Optional[Path] = None) -> Config:
    """The config to use: `path` (--config) if given, else the file named by $SIGEN_CONFIG,
    else the nearest config.json in the current directory or above it. With none of those, an
    empty Config whose origin says where it looked. A named file that's missing is exit 9."""
    if path is None and os.environ.get(CONFIG_ENV, "").strip():
        path = Path(os.environ[CONFIG_ENV].strip())
    if path is None:
        path = find_config()
    if path is None:
        return Config(origin=f"no config file: no --config, ${CONFIG_ENV} isn't set, and there's no "
                             f"{CONFIG_NAME} in {Path.cwd()} or above it")
    return load_config(path)


def load_config(path: Optional[Path]) -> Config:
    if path is None:
        return Config()
    try:
        doc = read_json_file(path)
    except FileNotFoundError:
        raise NotFound(f"config file {path} doesn't exist")
    except OSError as e:
        raise NotFound(f"can't read config file {path}: {e}")
    except ValueError as e:
        raise ConfigError(f"{path} isn't valid JSON: {e}")
    if not isinstance(doc, dict):
        raise ConfigError(f"{path}: expected a JSON object at the top level")
    unknown = set(doc) - {"smart_loads", "database", "devices", "site", "alerts", "array"}
    if unknown:
        raise ConfigError(f"{path}: unknown key(s) {', '.join(sorted(unknown))}; "
                          f"known: alerts, array, database, devices, site, smart_loads")

    devices = {}
    raw_devices = doc.get("devices", {})
    if not isinstance(raw_devices, dict):
        raise ConfigError(f'{path}: "devices" must be an object, e.g. {{"inverter": {{"ip": …, "mac": …}}}}')
    for name, spec in raw_devices.items():
        where = f"{path}: devices.{name}"
        if not isinstance(spec, dict) or not spec or set(spec) - {"ip", "mac", "units"}:
            raise ConfigError(f'{where} must be {{"ip": "…", "mac": "…", "units": [1]}} (any of them)')
        units = spec.get("units", [])
        if (not isinstance(units, list)
                or any(isinstance(u, bool) or not isinstance(u, int) or not 1 <= u <= 246 for u in units)
                or len(set(units)) != len(units)):
            raise ConfigError(f"{where}.units must be a list of distinct unit IDs from 1 to 246, got {units!r}")
        ip = spec.get("ip")
        if ip is not None:
            try:
                ip = str(ipaddress.IPv4Address(ip))
            except (ipaddress.AddressValueError, ValueError):
                raise ConfigError(f"{where}.ip: {ip!r} isn't an IPv4 address")
        mac = spec.get("mac")
        if mac is not None:
            try:
                mac = normalize_mac(str(mac))
            except ValueError as e:
                raise ConfigError(f"{where}.mac: {e}")
        devices[name] = Device(ip=ip, mac=mac, units=tuple(units))

    database_url = test_database_url = None
    if "database" in doc:
        db = doc["database"]
        if not isinstance(db, dict) or "url" not in db or set(db) - {"url", "test_url"}:
            raise ConfigError(f'{path}: "database" must be {{"url": "postgresql://…"}}, optionally with "test_url"')
        for key in db:
            if not isinstance(db[key], str) or not db[key].strip():
                raise ConfigError(f"{path}: database.{key} must be a non-empty string")
        database_url = db["url"].strip()
        test_database_url = db["test_url"].strip() if "test_url" in db else None

    names: Dict[int, str] = {}
    loads = doc.get("smart_loads", [])
    if not isinstance(loads, list):
        raise ConfigError(f"{path}: 'smart_loads' must be a list")
    for i, item in enumerate(loads):
        where = f"{path}: smart_loads[{i}]"
        if not isinstance(item, dict) or set(item) != {"slot", "name"}:
            raise ConfigError(f'{where}: expected {{"slot": N, "name": "..."}}')
        slot, name = item["slot"], item["name"]
        if isinstance(slot, bool) or not isinstance(slot, int) or slot not in SMART_LOAD_SLOTS:
            raise ConfigError(f"{where}: slot must be a whole number from 1 to 24, got {slot!r}")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{where}: name must be a non-empty string")
        if slot in names:
            raise ConfigError(f"{where}: slot {slot} is named twice")
        names[slot] = name.strip()
    return Config(smart_load_names=names, database_url=database_url, devices=devices,
                  site=_site(path, doc["site"]) if "site" in doc else None, test_database_url=test_database_url,
                  alerts=_alerts(path, doc["alerts"]) if "alerts" in doc else None,
                  array=_array(path, doc["array"]) if "array" in doc else None, origin=str(path))


NTFY_TOPIC = re.compile(r"[-_A-Za-z0-9]{1,64}")      # what ntfy accepts as a topic name
NTFY_SERVER = re.compile(r"https?://[^/\s]+/?")       # a server's base URL, e.g. https://ntfy.sh


def _alerts(path: Path, spec) -> Alerts:
    shape = '"alerts" must be {"ntfy_topic": "…"}, optionally with "ntfy_server": "https://…"'
    if not isinstance(spec, dict) or "ntfy_topic" not in spec or set(spec) - {"ntfy_topic", "ntfy_server"}:
        raise ConfigError(f"{path}: {shape}")
    topic, server = spec["ntfy_topic"], spec.get("ntfy_server", DEFAULT_NTFY_SERVER)
    if not isinstance(topic, str) or not NTFY_TOPIC.fullmatch(topic):
        raise ConfigError(f"{path}: alerts.ntfy_topic must be 1-64 letters, digits, - or _")
    if not isinstance(server, str) or not NTFY_SERVER.fullmatch(server):
        raise ConfigError(f'{path}: alerts.ntfy_server must be a server URL like "https://ntfy.sh", got {server!r}')
    return Alerts(ntfy_topic=topic, ntfy_server=server.rstrip("/"))


def _number(path: Path, where: str, value, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ConfigError(f"{path}: {where} must be a number from {low:g} to {high:g}, got {value!r}")
    return float(value)


def _array(path: Path, spec) -> Array:
    """The PV array: panel, ground albedo, roof faces, and which face each PV input's string is on."""
    shape = '"array" must be {"panel": {...}, "albedo": 0.2, "faces": {...}, "strings": {...}}'
    if not isinstance(spec, dict) or set(spec) != {"panel", "albedo", "faces", "strings"}:
        raise ConfigError(f"{path}: {shape}")
    p = spec["panel"]
    if not isinstance(p, dict) or set(p) != {"kw", "gamma_pct_per_c", "mount"}:
        raise ConfigError(f'{path}: array.panel must be {{"kw": 0.5, "gamma_pct_per_c": -0.28, '
                          f'"mount": "close_mount_glass_glass"}}')
    if not isinstance(p["mount"], str) or not p["mount"]:
        raise ConfigError(f"{path}: array.panel.mount must be a pvlib SAPM parameter set name")
    panel = Panel(_number(path, "array.panel.kw", p["kw"], 0.01, 5),
                  _number(path, "array.panel.gamma_pct_per_c", p["gamma_pct_per_c"], -2, 0), p["mount"])
    faces = {}
    if not isinstance(spec["faces"], dict) or not spec["faces"]:
        raise ConfigError(f'{path}: array.faces must be {{"name": {{"tilt": 20, "azimuth": 180}}, …}}')
    for name, f in spec["faces"].items():
        if not isinstance(f, dict) or set(f) != {"tilt", "azimuth"}:
            raise ConfigError(f'{path}: array.faces.{name} must be {{"tilt": …, "azimuth": …}}')
        faces[name] = Face(_number(path, f"array.faces.{name}.tilt", f["tilt"], 0, 90),
                           _number(path, f"array.faces.{name}.azimuth", f["azimuth"], 0, 360))
    strings = {}
    if not isinstance(spec["strings"], dict) or not spec["strings"]:
        raise ConfigError(f'{path}: array.strings must be {{"1": {{"panels": 9, "face": "…"}}, …}}')
    for key, s in spec["strings"].items():
        where = f"array.strings.{key}"
        if not key.isdigit() or not 1 <= int(key) <= 36:
            raise ConfigError(f"{path}: {where}: the key is the PV input number, 1 to 36")
        if not isinstance(s, dict) or set(s) != {"panels", "face"}:
            raise ConfigError(f'{path}: {where} must be {{"panels": 9, "face": "…"}}')
        if isinstance(s["panels"], bool) or not isinstance(s["panels"], int) or not 1 <= s["panels"] <= 100:
            raise ConfigError(f"{path}: {where}.panels must be a whole number from 1 to 100")
        if s["face"] not in faces:
            raise ConfigError(f"{path}: {where}.face {s['face']!r} isn't one of array.faces ({', '.join(faces)})")
        strings[int(key)] = PVString(s["panels"], s["face"])
    return Array(panel, _number(path, "array.albedo", spec["albedo"], 0, 1), faces, dict(sorted(strings.items())))


def _site(path: Path, spec) -> Site:
    shape = ('"site" must be {"latitude": 51.04, "longitude": -114.06, "timezone": "America/Edmonton"}, optionally '
             'with "altitude_m"')
    if not isinstance(spec, dict) or not {"latitude", "longitude", "timezone"} <= set(spec) \
            or set(spec) - {"latitude", "longitude", "timezone", "altitude_m"}:
        raise ConfigError(f"{path}: {shape}")
    for key, limit in (("latitude", 90), ("longitude", 180)):
        value = spec[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not -limit <= value <= limit:
            raise ConfigError(f"{path}: site.{key} must be a number from {-limit} to {limit}, got {value!r}")
    timezone = spec["timezone"]
    if not isinstance(timezone, str) or "/" not in timezone and timezone != "UTC":
        raise ConfigError(f'{path}: site.timezone must be an IANA name like "America/Edmonton", got {timezone!r}')
    altitude = _number(path, "site.altitude_m", spec["altitude_m"], -500, 9000) if "altitude_m" in spec else None
    return Site(float(spec["latitude"]), float(spec["longitude"]), timezone, altitude)
