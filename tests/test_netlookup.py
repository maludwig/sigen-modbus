"""MAC lookup (Windows, and Linux through `ip neigh`), the config's devices section, and their CLI behaviour."""
import json
import shutil
import subprocess
import sys

import pytest

from sigen import netlookup
from sigen.config import Device, load_config
from sigen.errors import ConfigError, ExitCode, PlatformNotSupported

INVERTER_MAC = "02:ab:cd:ef:00:01"


@pytest.mark.parametrize("text", ["02:ab:cd:ef:00:01", "02-AB-CD-EF-00-01", "02abcdef0001", "02ab.cdef.0001"])
def test_normalize_mac(text):
    assert netlookup.normalize_mac(text) == INVERTER_MAC


@pytest.mark.parametrize("text", ["", "02:ab:cd:ef:00", "02:ab:cd:ef:00:01:00", "zz:ab:cd:ef:00:01"])
def test_normalize_mac_rejects(text):
    with pytest.raises(ValueError, match="isn't a MAC"):
        netlookup.normalize_mac(text)


@pytest.fixture
def unsupported(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")


def test_lookups_refuse_elsewhere(unsupported):
    with pytest.raises(PlatformNotSupported, match="Windows and Linux, not darwin"):
        netlookup.mac_for_ip("192.168.1.100")
    with pytest.raises(PlatformNotSupported):
        netlookup.find_ip_for_mac(INVERTER_MAC, ["192.168.1.100"])


def test_linux_needs_the_ip_command(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(PlatformNotSupported, match="needs the `ip` command"):
        netlookup.require_supported()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_ip_command_is_installed():
    """On Linux, MAC lookup needs iproute2's `ip`, with JSON output (iproute2 4.13+)."""
    assert shutil.which("ip"), "the `ip` command (iproute2) isn't installed or isn't on PATH"
    netlookup.require_supported()
    table = netlookup.neighbours()                    # the real kernel table: whatever it holds, it must parse
    assert all(isinstance(state, str) for state, _ in table.values())


# ---------- Linux, with `ip neigh` and the clock faked ----------
NEIGH_JSON = json.dumps([
    {"dst": "192.168.1.1", "dev": "wlan0", "lladdr": "AA:BB:CC:00:00:01", "state": ["REACHABLE"]},
    {"dst": "192.168.1.100", "dev": "wlan0", "lladdr": INVERTER_MAC, "state": ["STALE"]},
    {"dst": "192.168.1.9", "dev": "wlan0", "state": ["FAILED"]},
    {"dst": "192.168.1.7", "dev": "wlan0", "lladdr": "02:00:00:00:00:07", "state": ["REACHABLE", "ROUTER"]},
])


def test_neighbours_parses_ip_json():
    def run(argv, **kw):
        assert argv == ["ip", "-4", "-j", "neigh", "show"]
        return subprocess.CompletedProcess(argv, 0, stdout=NEIGH_JSON)
    assert netlookup.neighbours(run) == {"192.168.1.1": ("REACHABLE", "aa:bb:cc:00:00:01"),
                                         "192.168.1.100": ("STALE", INVERTER_MAC),
                                         "192.168.1.9": ("FAILED", None),
                                         "192.168.1.7": ("REACHABLE/ROUTER", "02:00:00:00:00:07")}


class Clock:
    """A fake monotonic clock that sleep() advances."""
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def scripted(tables, clock):
    """neigh(): the table for the current fake time; tables is [(from time, {ip: (state, mac)})]."""
    def neigh():
        return [table for since, table in tables if since <= clock.t][-1]
    return neigh


def linux_lookup(fn, tables, *args, **kw):
    clock, poked = Clock(), []
    result = fn(*args, neigh=scripted(tables, clock), poke=lambda ips: poked.extend(ips), sleep=clock.sleep,
                monotonic=clock, **kw)
    return result, clock.t, poked


def test_linux_mac_for_ip_waits_for_a_stale_entry_to_be_confirmed():
    ip = "192.168.1.100"
    tables = [(0, {ip: ("DELAY", INVERTER_MAC)}), (5.1, {ip: ("REACHABLE", INVERTER_MAC)})]   # as seen on a Pi
    mac, waited, poked = linux_lookup(netlookup._linux_mac_for_ip, tables, ip)
    assert (mac, poked) == (INVERTER_MAC, [ip]) and 5.1 <= waited < 5.3


def test_linux_mac_for_ip_nothing_there():
    ip = "192.168.1.250"
    tables = [(0, {ip: ("FAILED", None)}), (0.1, {ip: ("INCOMPLETE", None)}), (1.9, {ip: ("FAILED", None)})]
    mac, waited, _ = linux_lookup(netlookup._linux_mac_for_ip, tables, ip)
    assert mac is None and 1.9 <= waited < 2.1         # a FAILED from before the poke didn't end it early
    mac, waited, _ = linux_lookup(netlookup._linux_mac_for_ip, [(0, {})], ip, timeout=3)
    assert mac is None and waited >= 3                  # never appeared: gives up at the timeout


def test_linux_find_ignores_a_stale_match_at_the_old_address():
    """The inverter moved from .100 to .120: the old entry still says .100 until the kernel re-checks it."""
    ips = [f"192.168.1.{n}" for n in range(100, 125)]
    start = {ip: ("INCOMPLETE", None) for ip in ips}
    tables = [(0, {**start, "192.168.1.100": ("DELAY", INVERTER_MAC)}),
              (2.0, {**{ip: ("FAILED", None) for ip in ips}, "192.168.1.100": ("PROBE", INVERTER_MAC)}),
              (2.5, {**{ip: ("FAILED", None) for ip in ips}, "192.168.1.100": ("PROBE", INVERTER_MAC),
                     "192.168.1.120": ("REACHABLE", INVERTER_MAC)})]
    found, waited, poked = linux_lookup(netlookup._linux_find, tables, INVERTER_MAC, ips)
    assert found == "192.168.1.120" and 2.5 <= waited < 2.7 and poked == ips


def test_linux_find_gives_up_once_everything_settled():
    ips = ["192.168.1.1", "192.168.1.2"]
    tables = [(0, {ip: ("INCOMPLETE", None) for ip in ips}),
              (1.0, {"192.168.1.1": ("REACHABLE", "02:00:00:00:00:99"), "192.168.1.2": ("FAILED", None)})]
    found, waited, _ = linux_lookup(netlookup._linux_find, tables, INVERTER_MAC, ips)
    assert found is None and 1.0 <= waited < 1.2         # not the full timeout: nothing left to wait for


def test_find_ip_for_mac_with_a_stand_in_lookup():
    table = {"10.0.0.1": "00:11:22:33:44:55", "10.0.0.7": INVERTER_MAC}
    ips = [f"10.0.0.{i}" for i in range(1, 20)]
    assert netlookup.find_ip_for_mac("02-AB-CD-EF-00-01", ips, lookup=table.get) == "10.0.0.7"
    assert netlookup.find_ip_for_mac("00:00:00:00:00:01", ips, lookup=table.get) is None


# ---------- config ----------
def write_config(tmp_path, doc):
    f = tmp_path / "config.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    return f


def test_config_devices(tmp_path):
    cfg = load_config(write_config(tmp_path, {"devices": {
        "inverter": {"ip": "192.168.1.100", "mac": "02-AB-CD-EF-00-01"}, "loadhub": {"mac": "02abcdef0002"}}}))
    assert cfg.devices == {"inverter": Device("192.168.1.100", INVERTER_MAC),
                           "loadhub": Device(None, "02:ab:cd:ef:00:02")}


def test_config_device_units(tmp_path):
    cfg = load_config(write_config(tmp_path, {"devices": {"inverter": {"ip": "192.168.1.100", "units": [1, 2]}}}))
    assert cfg.devices["inverter"].units == (1, 2)


@pytest.mark.parametrize("devices,match", [
    ([], "must be an object"),
    ({"inverter": {}}, "any of them"),
    ({"inverter": {"ip": "192.168.1.100", "port": 502}}, "any of them"),
    ({"inverter": {"units": 1}}, "list of distinct unit IDs"),
    ({"inverter": {"units": [0]}}, "list of distinct unit IDs"),
    ({"inverter": {"units": [247]}}, "list of distinct unit IDs"),
    ({"inverter": {"units": [1, 1]}}, "list of distinct unit IDs"),
    ({"inverter": {"ip": "192.168.1.300"}}, "isn't an IPv4"),
    ({"inverter": {"mac": "nope"}}, "isn't a MAC"),
])
def test_config_devices_rejects(tmp_path, devices, match):
    with pytest.raises(ConfigError, match=match):
        load_config(write_config(tmp_path, {"devices": devices}))


# ---------- CLI ----------
def test_mac_lookup_refuses_elsewhere(run, unsupported):
    code, _, err = run("mac-lookup", "--ip", "192.168.1.100")
    assert code == ExitCode.USAGE and "Windows and Linux, not darwin" in err


def test_mac_lookup_bad_mac(run):
    code, _, err = run("mac-lookup", "--mac", "nope")
    assert code == ExitCode.USAGE and "isn't a MAC" in err


def test_poll_with_a_mac_it_cant_follow_warns_and_carries_on(run, tmp_path, monkeypatch):
    # Only MAC lookup is faked as unsupported: faking sys.platform would also send psycopg's library loading
    # down another platform's path.
    pytest.importorskip("psycopg")

    def no(*a):
        raise PlatformNotSupported("MAC lookup on Linux needs the `ip` command (iproute2)")
    monkeypatch.setattr(netlookup, "require_supported", no)
    f = write_config(tmp_path, {"database": {"url": "postgresql://u:p@127.0.0.1:1/x"},
                                "devices": {"inverter": {"ip": "192.168.1.100", "mac": INVERTER_MAC}}})
    code, _, err = run("poll", "--config", f, "--count", 1)
    assert "warning: MAC lookup on Linux needs the `ip` command" in err and "polling without" in err
    assert code == ExitCode.DATABASE and "following MAC" not in err     # got as far as the database (none here)


def test_poll_takes_the_ip_from_the_config(run, tmp_path):
    pytest.importorskip("psycopg")
    f = write_config(tmp_path, {"database": {"url": "postgresql://u:p@127.0.0.1:1/x"},
                                "devices": {"inverter": {"ip": "192.168.1.100"}}})
    code, _, err = run("poll", "--config", f, "--count", 1)
    assert code == ExitCode.DATABASE and "Polling 192.168.1.100:502" in err   # got past the IP; no DB here
    f = write_config(tmp_path, {"database": {"url": "postgresql://u:p@127.0.0.1:1/x"}})
    code, _, err = run("poll", "--config", f, "--count", 1)
    assert code == ExitCode.USAGE and "devices.inverter.ip" in err
