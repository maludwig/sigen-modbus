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
import functools
import json
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

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
                "MAC lookup on Linux needs the `ip` command (iproute2), which isn't installed or isn't on PATH"
            )
        return
    raise PlatformNotSupported(f"MAC lookup works on Windows and Linux, not {sys.platform}")


# ---------- Linux ----------
def neighbours(
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, tuple[str, str | None]]:
    """{ip: (state, mac or None)} for the kernel's IPv4 neighbours, from `ip -4 -j neigh show`. A state is
    e.g. "REACHABLE", "STALE", "FAILED" (several flags are joined with "/")."""
    try:
        out = run(["ip", "-4", "-j", "neigh", "show"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        raise PlatformNotSupported(f"`ip -4 -j neigh show` failed: {e}") from e
    table = {}
    for entry in json.loads(out or "[]"):
        if "dst" not in entry:
            continue
        mac = entry.get("lladdr")
        table[entry["dst"]] = ("/".join(entry.get("state") or []) or "?", mac.lower() if mac else None)
    return table


def _has_flag(state: str, flags: Iterable[str]) -> bool:
    """True if any of the "/"-joined flags in a neighbour state (from neighbours()) is one of `flags`."""
    return any(s in flags for s in state.split("/"))


def _confirmed(state: str) -> bool:
    """True if the neighbour state says the MAC is trusted: REACHABLE, PERMANENT or NOARP."""
    return _has_flag(state, CONFIRMED)


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
    neigh: Callable[[], dict[str, tuple[str, str | None]]] = neighbours,
    poke: Callable[[Iterable[str]], None] = _poke,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str | None:
    """mac_for_ip() on Linux: pokes `ip`, then polls the neighbour table
    every 0.1 s until its MAC is confirmed (REACHABLE). None once the
    resolution the poke started FAILED, or after `timeout` seconds. The
    keyword arguments after `timeout` are stand-ins for tests."""
    poke([ip])
    deadline, resolving = monotonic() + timeout, False
    while True:
        state, mac = neigh().get(ip, ("absent", None))
        if mac and _confirmed(state):
            return mac
        if _has_flag(state, PENDING):
            resolving = True
        elif _has_flag(state, {"FAILED"}) and resolving:  # a FAILED left over from before the poke doesn't count
            return None
        if monotonic() >= deadline:
            return None
        sleep(0.1)


def _linux_find(
    mac: str,
    ips: Iterable[str],
    timeout: float = LINUX_TIMEOUT,
    neigh: Callable[[], dict[str, tuple[str, str | None]]] = neighbours,
    poke: Callable[[Iterable[str]], None] = _poke,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str | None:
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
        unsettled = [ip for ip in ips if _has_flag(table.get(ip, ("absent",))[0], waiting)]
        if not unsettled or monotonic() >= deadline:
            return None
        sleep(0.1)


# ---------- Windows ----------
@functools.cache
def _send_arp() -> Callable:
    """iphlpapi's SendARP with its argument types set, made once (a sweep
    calls it from up to 64 threads)."""
    send_arp = ctypes.WinDLL("iphlpapi").SendARP
    send_arp.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    return send_arp


def _windows_mac_for_ip(ip: str) -> str | None:
    """mac_for_ip() on Windows: one SendARP call, blocking ~10 ms on an
    answer and up to ~3 s without one. None if nothing answered."""
    send_arp = _send_arp()
    dest = struct.unpack("<I", socket.inet_aton(ip))[0]  # same bytes as network order
    buf = (ctypes.c_ubyte * 8)()
    length = ctypes.c_ulong(6)
    if send_arp(dest, 0, buf, ctypes.byref(length)) != 0 or length.value != 6:
        return None
    return ":".join(f"{b:02x}" for b in buf[:6])


def _parallel_find(target: str, ips: Iterable[str], lookup: Callable[[str], str | None], workers: int) -> str | None:
    """Runs `lookup` (ip -> MAC) on every one of `ips` across `workers`
    threads; the first to answer `target` wins, without waiting for the
    rest. `target` must already be normalized."""
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(lookup, ip): ip for ip in ips}
        for future in as_completed(futures):
            try:
                mac = future.result()
            except Exception:  # noqa: BLE001 -- one address that fails is "no answer", not the end of the sweep
                continue
            if mac == target:
                return futures[future]
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # lookups already running finish on their own


# ---------- the interface ----------
def mac_for_ip(ip: str) -> str | None:
    """The MAC of `ip` (an IPv4 address on this computer's network), or None if nothing answered."""
    require_supported()
    return _windows_mac_for_ip(ip) if sys.platform == "win32" else _linux_mac_for_ip(ip)


def find_ip_for_mac(
    mac: str, ips: Iterable[str], workers: int = 64, lookup: Callable[[str], str | None] | None = None
) -> str | None:
    """The first of `ips` whose MAC is `mac`; None if none of them. `lookup` (ip -> MAC) replaces the
    platform's own (for tests): it's then asked for every address in parallel."""
    target = normalize_mac(mac)
    if lookup is not None:
        return _parallel_find(target, ips, lookup, workers)
    require_supported()
    if sys.platform == "win32":
        return _parallel_find(target, ips, _windows_mac_for_ip, workers)
    return _linux_find(target, ips)
