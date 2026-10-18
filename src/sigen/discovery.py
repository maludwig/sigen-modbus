"""Finding a SigenStor on the local network."""

import argparse
import concurrent.futures
import ipaddress
import socket
import sys
from typing import List, Optional

from sigen.errors import SigenError
from sigen.modbus import ModbusClient
from sigen.registers import BY_NAME, EMS_MODES


def local_subnet() -> ipaddress.IPv4Network:
    """The /24 around this computer's LAN address (the interface used to reach the internet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # UDP "connect" sends nothing; it just picks the interface
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ipaddress.ip_network(f"{ip}/24", strict=False)


def parse_ip_range(text: str) -> List[str]:
    """'192.168.1.170-192.168.1.180', '192.168.1.170-180', '192.168.1.0/24' or one IP -> list."""
    text = text.strip()
    try:
        if "/" in text:
            return [str(ip) for ip in ipaddress.ip_network(text, strict=False).hosts()]
        if "-" in text:
            start_s, end_s = (p.strip() for p in text.split("-", 1))
            start = ipaddress.IPv4Address(start_s)
            if "." not in end_s:  # short form: 192.168.1.170-180
                end_s = start_s.rsplit(".", 1)[0] + "." + end_s
            end = ipaddress.IPv4Address(end_s)
            if end < start:
                raise ValueError("range end is before range start")
            if int(end) - int(start) > 65535:
                raise ValueError("range is too large (max 65,536 addresses)")
            return [str(ipaddress.IPv4Address(i)) for i in range(int(start), int(end) + 1)]
        return [str(ipaddress.IPv4Address(text))]
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid range '{text}': {e}")


def _port_open(ip: str, port: int, timeout: float) -> Optional[str]:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return ip
    except OSError:
        return None


def looks_like_sigen(ip: str, port: int, unit: int, timeout: float) -> bool:
    reg = BY_NAME["plant_ems_work_mode"]
    try:
        with ModbusClient(ip, port, unit, timeout) as c:
            return c.read(reg.table, reg.address, 1)[0] in EMS_MODES
    except SigenError:
        return False


def scan(
    ips: List[str],
    *,
    port: int,
    unit: int,
    timeout: float,
    scan_timeout: float,
    log=lambda msg: print(msg, file=sys.stderr),
) -> List[str]:
    """Return every address in `ips` that answers like a SigenStor."""
    label = f"{ips[0]} – {ips[-1]}" if len(ips) > 1 else ips[0]
    log(f"Scanning {label} ({len(ips)} address{'es' if len(ips) != 1 else ''}) for Modbus TCP on port {port}…")
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        open_ips = [ip for ip in ex.map(lambda ip: _port_open(ip, port, scan_timeout), ips) if ip]
    if open_ips:
        log(f"  Port {port} open on: {', '.join(open_ips)}")
    found = [ip for ip in open_ips if looks_like_sigen(ip, port, unit, timeout)]
    if open_ips and not found:
        log("  …but none of them answered like a SigenStor.")
    return found
