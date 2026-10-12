"""Finding devices by MAC address on the local network. Windows only.

Uses Windows' iphlpapi SendARP: it sends a real ARP request ("who has this IP?") and
returns the MAC, with no admin rights and no extra packages. A lookup takes ~10 ms when
something answers and up to ~3 s when nothing does, so a /24 sweep runs in parallel
(~14 s measured on 2026-10-07). Other systems can't send ARP without root, so every
function here raises PlatformNotSupported there.
"""
import ctypes
import re
import socket
import struct
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, Optional

from sigen.errors import PlatformNotSupported


def normalize_mac(text: str) -> str:
    """'02-AB-CD-EF-00-01', '02:ab:cd:ef:00:01', '02ab.cdef.0001' -> '02:ab:cd:ef:00:01'."""
    raw = re.sub(r"[:\-.\s]", "", text)
    if not re.fullmatch(r"[0-9a-fA-F]{12}", raw):
        raise ValueError(f"'{text}' isn't a MAC address (expected 6 hex bytes, e.g. 02:ab:cd:ef:00:01)")
    return ":".join(raw[i:i + 2] for i in range(0, 12, 2)).lower()


def require_windows() -> None:
    if sys.platform != "win32":
        raise PlatformNotSupported(f"MAC lookup only works on Windows (this is {sys.platform}); "
                                   f"it relies on Windows' SendARP")


def mac_for_ip(ip: str) -> Optional[str]:
    """The MAC of `ip` (an IPv4 address on this computer's network), or None if nothing answered."""
    require_windows()
    send_arp = ctypes.WinDLL("iphlpapi").SendARP
    send_arp.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    dest = struct.unpack("<I", socket.inet_aton(ip))[0]     # same bytes as network order
    buf = (ctypes.c_ubyte * 8)()
    length = ctypes.c_ulong(6)
    if send_arp(dest, 0, buf, ctypes.byref(length)) != 0 or length.value != 6:
        return None
    return ":".join(f"{b:02x}" for b in buf[:6])


def find_ip_for_mac(mac: str, ips: Iterable[str], workers: int = 64,
                    lookup: Optional[Callable[[str], Optional[str]]] = None) -> Optional[str]:
    """The first of `ips` whose MAC is `mac`, asking in parallel; None if none of them.
    `lookup` replaces mac_for_ip (for tests)."""
    if lookup is None:
        require_windows()
        lookup = mac_for_ip
    target = normalize_mac(mac)
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(lookup, ip): ip for ip in ips}
        for future in as_completed(futures):
            if future.result() == target:
                return futures[future]
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)   # lookups already running finish on their own
