"""parse_ip_range: every form, and the size cap (a typo mustn't port-scan millions of addresses); local_hosts without
a route out, and poll riding that out."""

import argparse
import socket

import pytest

from sigen import poll
from sigen.discovery import local_hosts, parse_ip_range
from sigen.errors import ConnectionFailed


def test_the_forms():
    assert parse_ip_range("192.168.1.170-172") == ["192.168.1.170", "192.168.1.171", "192.168.1.172"]
    assert parse_ip_range("192.168.1.170-192.168.1.171") == ["192.168.1.170", "192.168.1.171"]
    assert parse_ip_range("192.168.1.5") == ["192.168.1.5"]
    assert len(parse_ip_range("192.168.1.0/24")) == 254
    assert len(parse_ip_range("10.0.0.0/16")) == 65534  # the largest a CIDR may be


@pytest.mark.parametrize("text", ["10.0.0.0/8", "10.0.0.0/15", "10.0.0.0-10.2.0.0"])
def test_too_large_is_refused(text):
    with pytest.raises(argparse.ArgumentTypeError, match="too large"):
        parse_ip_range(text)


def test_ipv6_is_refused_like_every_other_form():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_ip_range("2001:db8::/120")


class NoRoute:
    """A socket with no route out, as during a network outage."""

    def __init__(self, *args):
        pass

    def connect(self, address):
        raise OSError(101, "Network is unreachable")

    def close(self):
        pass


def test_no_route_out_is_a_connection_failure(monkeypatch):
    monkeypatch.setattr(socket, "socket", NoRoute)
    with pytest.raises(ConnectionFailed, match="is the network up"):
        local_hosts()


def test_poll_keeps_going_when_the_mac_lookup_fails(monkeypatch):
    # poll looks the inverter up by MAC during an outage, exactly when local_hosts() has no route out
    monkeypatch.setattr(socket, "socket", NoRoute)
    said = []
    assert poll._rediscover(lambda ip: netlookup_like_sweep(), "192.0.2.1", said.append) is None
    assert "the lookup failed" in said[-1] and "keep trying the same IP" in said[-1]


def netlookup_like_sweep():
    """What cli.cmd_poll's rediscover does once the MAC isn't at the old IP: sweep the local hosts."""
    return local_hosts()[0]
