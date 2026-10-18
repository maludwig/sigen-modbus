"""parse_ip_range: every form, and the size cap (a typo mustn't port-scan millions of addresses)."""

import argparse

import pytest

from sigen.discovery import parse_ip_range


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
