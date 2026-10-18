"""A small, dependency-free Modbus TCP client.

Supports function 03 (read holding), 04 (read input), 06 (write single register)
and 16 (write multiple registers). Errors are raised as sigen.errors exceptions
so callers get a meaningful exit code.
"""

import socket
import struct
from typing import List

from sigen.errors import ConnectionFailed, ModbusError
from sigen.registers import PLANT_UNIT_ID

DEFAULT_PORT = 502
DEFAULT_TIMEOUT = 3.0


class ModbusClient:
    """Use as a context manager so the socket is always closed:

    with ModbusClient("192.168.1.100") as c:
        words = c.read("input", 30014, 1)
    """

    def __init__(
        self, host: str, port: int = DEFAULT_PORT, unit: int = PLANT_UNIT_ID, timeout: float = DEFAULT_TIMEOUT
    ):
        self.host, self.port, self.unit, self.timeout = host, port, unit, timeout
        self._sock = None
        self._tid = 0

    def __enter__(self):
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock.settimeout(self.timeout)
        except OSError as e:
            raise ConnectionFailed(f"couldn't connect to {self.host}:{self.port}: {e}") from e
        return self

    def __exit__(self, *exc):
        if self._sock:
            self._sock.close()
            self._sock = None

    # ---------- transport ----------
    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionFailed(f"{self.host}:{self.port} closed the connection")
            buf += chunk
        return buf

    def _request(self, pdu: bytes, address: int) -> bytes:
        if self._sock is None:
            raise ConnectionFailed("not connected (use ModbusClient as a context manager)")
        self._tid = (self._tid + 1) & 0xFFFF
        try:
            self._sock.sendall(struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, self.unit) + pdu)
            tid, proto, length, _unit = struct.unpack(">HHHB", self._recv_exact(7))
            body = self._recv_exact(length - 1)
        except socket.timeout as e:
            raise ConnectionFailed(f"{self.host}:{self.port} timed out after {self.timeout}s") from e
        except OSError as e:
            raise ConnectionFailed(f"connection to {self.host}:{self.port} failed: {e}") from e
        if tid != self._tid or proto != 0:
            raise ConnectionFailed(f"unexpected reply framing (transaction {tid}, expected {self._tid})")
        if body[0] & 0x80:
            raise ModbusError(body[0] & 0x7F, body[1], address)
        if body[0] != pdu[0]:
            raise ConnectionFailed(f"reply was for function {body[0]}, expected {pdu[0]}")
        return body

    # ---------- operations ----------
    def read(self, table: str, address: int, count: int) -> List[int]:
        function = 4 if table == "input" else 3
        body = self._request(struct.pack(">BHH", function, address, count), address)
        nbytes = body[1]
        if nbytes != count * 2 or len(body) < 2 + nbytes:
            raise ConnectionFailed(f"short reply reading {address}: {nbytes} bytes for {count} registers")
        return list(struct.unpack(f">{count}H", body[2 : 2 + nbytes]))

    def write(self, address: int, words: List[int]) -> None:
        """Write holding registers: function 06 for one word, 16 for several."""
        if len(words) == 1:
            pdu = struct.pack(">BHH", 6, address, words[0])
            body = self._request(pdu, address)
            if body != pdu:
                raise ConnectionFailed(f"write to {address} wasn't echoed back correctly")
        else:
            pdu = struct.pack(f">BHHB{len(words)}H", 16, address, len(words), len(words) * 2, *words)
            body = self._request(pdu, address)
            if body[:5] != struct.pack(">BHH", 16, address, len(words)):
                raise ConnectionFailed(f"write to {address} wasn't acknowledged correctly")
