"""MAC lookup (Windows only), the config's devices section, and their CLI behaviour."""
import json
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
def not_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")


def test_lookups_refuse_off_windows(not_windows):
    with pytest.raises(PlatformNotSupported, match="only works on Windows"):
        netlookup.mac_for_ip("192.168.1.100")
    with pytest.raises(PlatformNotSupported):
        netlookup.find_ip_for_mac(INVERTER_MAC, ["192.168.1.100"])


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
def test_mac_lookup_refuses_off_windows(run, not_windows):
    code, _, err = run("mac-lookup", "--ip", "192.168.1.100")
    assert code == ExitCode.USAGE and "only works on Windows" in err


def test_mac_lookup_bad_mac(run):
    code, _, err = run("mac-lookup", "--mac", "nope")
    assert code == ExitCode.USAGE and "isn't a MAC" in err


def test_poll_with_a_mac_refuses_off_windows_at_startup(run, tmp_path, not_windows):
    f = write_config(tmp_path, {"database": {"url": "postgresql://u:p@127.0.0.1:1/x"},
                                "devices": {"inverter": {"ip": "192.168.1.100", "mac": INVERTER_MAC}}})
    code, _, err = run("poll", "--config", f, "--count", 1)
    assert code == ExitCode.USAGE and "only works on Windows" in err


def test_poll_takes_the_ip_from_the_config(run, tmp_path):
    pytest.importorskip("psycopg")
    f = write_config(tmp_path, {"database": {"url": "postgresql://u:p@127.0.0.1:1/x"},
                                "devices": {"inverter": {"ip": "192.168.1.100"}}})
    code, _, err = run("poll", "--config", f, "--count", 1)
    assert code == ExitCode.DATABASE and "Polling 192.168.1.100:502" in err   # got past the IP; no DB here
    f = write_config(tmp_path, {"database": {"url": "postgresql://u:p@127.0.0.1:1/x"}})
    code, _, err = run("poll", "--config", f, "--count", 1)
    assert code == ExitCode.USAGE and "devices.inverter.ip" in err
