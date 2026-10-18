"""Finding devices by MAC address on the local network. Windows and Linux, no admin rights.

Windows: iphlpapi's SendARP sends a real ARP request ("who has this IP?") and returns the MAC. A
lookup takes ~10 ms when something answers and up to ~3 s when nothing does, so a /24 sweep runs
in parallel (~14 s measured on 2026-10-07).

Linux: an unprivileged program can't send ARP itself, but the kernel does whenever it has a packet
for a local address it hasn't resolved: one empty UDP datagram to IP:9 (discard) is enough, and no
answer is needed (ARP sits below ICMP, so a device that ignores ping still answers). The result is
read back with `ip -j neigh` (iproute2, over netlink, unprivileged). Not /proc/net/arp: it shows a
STALE entry (an old IP <-> MAC pairing, e.g. from before the device moved) exactly like a fresh one,
while `ip neigh` gives the state, and only REACHABLE (just confirmed) is trusted. A poke makes the
kernel re-check a STALE entry, which takes it ~5 s; a /24 sweep takes ~3-8 s (measured on a
Raspberry Pi, 2026-10-12).

Elsewhere (macOS, …) every function here raises PlatformNotSupported.
"""

import ctypes
import json
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, Iterable, Optional, Tuple

from sigen.errors import PlatformNotSupported

LINUX_TIMEOUT = 8.0  # a STALE entry's re-check: 5 s of DELAY, then unicast probes
CONFIRMED = {"REACHABLE", "PERMANENT", "NOARP"}
PENDING = {"INCOMPLETE", "DELAY", "PROBE"}


def normalize_mac(text: str) -> str:
    """'02-AB-CD-EF-00-01', '02:ab:cd:ef:00:01', '02ab.cdef.0001' -> '02:ab:cd:ef:00:01'."""
    raw = re.sub(r"[:\-.\s]", "", text)
    if not re.fullmatch(r"[0-9a-fA-F]{12}", raw):
        raise ValueError(f"'{text}' isn't a MAC address (expected 6 hex bytes, e.g. 02:ab:cd:ef:00:01)")
    return ":".join(raw[i : i + 2] for i in range(0, 12, 2)).lower()


def require_supported() -> None:
    """Raise PlatformNotSupported unless MAC lookup can work here: Windows, or Linux with `ip`."""
    if sys.platform == "win32":
        return
    if sys.platform.startswith("linux"):
        if shutil.which("ip") is None:
            raise PlatformNotSupported(
                "MAC lookup on Linux needs the `ip` command (iproute2), which isn't " "installed or isn't on PATH"
            )
        return
    raise PlatformNotSupported(f"MAC lookup works on Windows and Linux, not {sys.platform}")


# ---------- Linux ----------
def neighbours(run: Callable = subprocess.run) -> Dict[str, Tuple[str, Optional[str]]]:
    """{ip: (state, mac or None)} for the kernel's IPv4 neighbours, from `ip -4 -j neigh show`. A state is
    e.g. "REACHABLE", "STALE", "FAILED" (several flags are joined with "/")."""
    try:
        out = run(["ip", "-4", "-j", "neigh", "show"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        raise PlatformNotSupported(f"`ip -4 -j neigh show` failed: {e}")
    table = {}
    for entry in json.loads(out or "[]"):
        if "dst" not in entry:
            continue
        mac = entry.get("lladdr")
        table[entry["dst"]] = ("/".join(entry.get("state") or []) or "?", mac.lower() if mac else None)
    return table


def _confirmed(state: str) -> bool:
    return any(s in CONFIRMED for s in state.split("/"))


def _poke(ips: Iterable[str]) -> None:
    """One empty UDP datagram to port 9 of each address, so the kernel ARPs for the ones it doesn't know (and
    re-checks the STALE ones). Errors don't matter: the resolution is what's wanted, not the delivery."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setblocking(False)
        for ip in ips:
            try:
                s.sendto(b"", (ip, 9))
            except OSError:
                pass


def _linux_mac_for_ip(
    ip: str,
    timeout: float = LINUX_TIMEOUT,
    neigh: Callable = neighbours,
    poke: Callable = _poke,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> Optional[str]:
    poke([ip])
    deadline, resolving = monotonic() + timeout, False
    while True:
        state, mac = neigh().get(ip, ("absent", None))
        if mac and _confirmed(state):
            return mac
        if any(s in PENDING for s in state.split("/")):
            resolving = True
        elif "FAILED" in state and resolving:  # a FAILED left over from before the poke doesn't count
            return None
        if monotonic() >= deadline:
            return None
        sleep(0.1)


def _linux_find(
    mac: str,
    ips: Iterable[str],
    timeout: float = LINUX_TIMEOUT,
    neigh: Callable = neighbours,
    poke: Callable = _poke,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> Optional[str]:
    """The first of `ips` confirmed (REACHABLE) to have `mac`; a STALE match only counts once the kernel has
    re-checked it."""
    ips = list(ips)
    poke(ips)
    deadline = monotonic() + timeout
    while True:
        table = neigh()
        for ip in ips:
            state, found = table.get(ip, ("absent", None))
            if found == mac and _confirmed(state):
                return ip
        waiting = PENDING | {"STALE"}  # still being resolved or re-checked
        unsettled = [ip for ip in ips if any(s in waiting for s in table.get(ip, ("absent",))[0].split("/"))]
        if not unsettled or monotonic() >= deadline:
            return None
        sleep(0.1)


# ---------- Windows ----------
def _windows_mac_for_ip(ip: str) -> Optional[str]:
    send_arp = ctypes.WinDLL("iphlpapi").SendARP
    send_arp.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    dest = struct.unpack("<I", socket.inet_aton(ip))[0]  # same bytes as network order
    buf = (ctypes.c_ubyte * 8)()
    length = ctypes.c_ulong(6)
    if send_arp(dest, 0, buf, ctypes.byref(length)) != 0 or length.value != 6:
        return None
    return ":".join(f"{b:02x}" for b in buf[:6])


def _parallel_find(
    target: str, ips: Iterable[str], lookup: Callable[[str], Optional[str]], workers: int
) -> Optional[str]:
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(lookup, ip): ip for ip in ips}
        for future in as_completed(futures):
            if future.result() == target:
                return futures[future]
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # lookups already running finish on their own


# ---------- the interface ----------
def mac_for_ip(ip: str) -> Optional[str]:
    """The MAC of `ip` (an IPv4 address on this computer's network), or None if nothing answered."""
    require_supported()
    return _windows_mac_for_ip(ip) if sys.platform == "win32" else _linux_mac_for_ip(ip)


def find_ip_for_mac(
    mac: str, ips: Iterable[str], workers: int = 64, lookup: Optional[Callable[[str], Optional[str]]] = None
) -> Optional[str]:
    """The first of `ips` whose MAC is `mac`; None if none of them. `lookup` (ip -> MAC) replaces the
    platform's own (for tests): it's then asked for every address in parallel."""
    target = normalize_mac(mac)
    if lookup is not None:
        return _parallel_find(target, ips, lookup, workers)
    require_supported()
    if sys.platform == "win32":
        return _parallel_find(target, ips, _windows_mac_for_ip, workers)
    return _linux_find(target, ips)
