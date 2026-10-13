"""A fake SigenStor Modbus TCP server, for local testing and development.

Serves register values from the shared JSON format (see data_io.py). Supports
functions 03, 04, 06 and 16. Writes only change holding registers in memory and
are logged. JSON files are re-read automatically when they change.

Reads follow the real SigenStor's rules (probed 2026-10-07): a read is rejected with
exception 02 if it starts on an address with no data, or splits one of the register
map's multi-word registers at either end. Inside an accepted read, addresses with no
data read 0xFFFF. Like the real device, it doesn't answer at all for a unit ID it has no
data for (the client times out), rather than replying with an exception.

Testing modes:
  ignore_writes=True   acknowledge writes but don't store them, so the read-back
                       won't match (exercises the verify-failed path, exit code 5)
  reject_writes=True   answer every write with exception 04 (device failure)
                       (exercises the write-rejected path, exit code 4)
"""
import logging
import socketserver
import struct
import threading
from pathlib import Path
from typing import Optional

from sigen.data_io import load_dir
from sigen.registers import ALL_REGISTERS

log = logging.getLogger("sigen.fake_server")

EXC_ILLEGAL_FUNCTION, EXC_ILLEGAL_ADDRESS, EXC_ILLEGAL_VALUE, EXC_DEVICE_FAILURE = 1, 2, 3, 4
UNSUPPORTED_WORD = 0xFFFF

# table -> {address: (first address, word count)} of the mapped register covering it.
# Plant and inverter address ranges never overlap, so one map serves every unit.
REGISTER_SPANS = {"input": {}, "holding": {}}
for _r in ALL_REGISTERS:
    for _a in range(_r.address, _r.address + _r.words):
        REGISTER_SPANS[_r.table][_a] = (_r.address, _r.words)


class RegisterBank:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.lock = threading.Lock()
        self.image = {}       # (unit, table) -> {address: word}
        self.writes = {}      # (unit, address) -> word; survive reloads
        self._mtimes = None
        self.reload_if_changed(force=True)

    def reload_if_changed(self, force: bool = False) -> None:
        try:
            mtimes = {p: p.stat().st_mtime for p in sorted(self.data_dir.glob("*.json"))}
        except OSError as e:
            log.error("Can't read %s: %s", self.data_dir, e)
            return
        if not force and mtimes == self._mtimes:
            return
        try:
            image, files = load_dir(self.data_dir)
        except (OSError, ValueError) as e:
            log.error("Bad fake data, keeping the last good copy: %s", e)
            return
        with self.lock:
            self.image, self._mtimes = image, mtimes
        counts = ", ".join(f"unit {u} {t}: {len(r)}" for (u, t), r in sorted(image.items()) if r)
        log.info("Loaded %d file(s) from %s (%s)", len(files), self.data_dir, counts or "no registers")

    def units(self):
        with self.lock:
            return {u for (u, _t), regs in self.image.items() if regs}

    def read(self, unit, table, address, count) -> Optional[list]:
        """Words for address..address+count-1, or None (-> exception 02) if the device would refuse."""
        self.reload_if_changed()
        with self.lock:
            regs = self.image.get((unit, table), {})

            def word(a):
                if table == "holding" and (unit, a) in self.writes:
                    return self.writes[(unit, a)]
                return regs.get(a)

            spans = REGISTER_SPANS.get(table, {})
            end = address + count - 1
            if word(address) is None:
                return None                                   # starts on an unsupported address
            if address in spans and spans[address][0] != address:
                return None                                   # starts mid-register
            if end in spans and sum(spans[end]) - 1 != end:
                return None                                   # ends mid-register
            return [UNSUPPORTED_WORD if word(a) is None else word(a) for a in range(address, end + 1)]

    def write(self, unit, address, words, store=True) -> bool:
        with self.lock:
            regs = self.image.get((unit, "holding"), {})
            if any(address + i not in regs for i in range(len(words))):
                return False
            if store:
                for i, w in enumerate(words):
                    self.writes[(unit, address + i)] = w
            return True


class _Handler(socketserver.BaseRequestHandler):
    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.request.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def handle(self):
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        log.debug("Client connected: %s", peer)
        while True:
            header = self._recv_exact(7)
            if header is None:
                break
            tid, proto, length, unit = struct.unpack(">HHHB", header)
            if proto != 0 or not 2 <= length <= 254:
                log.warning("%s sent a malformed frame; closing", peer)
                break
            pdu = self._recv_exact(length - 1)
            if pdu is None:
                break
            reply = self.server.process(unit, pdu, peer)
            if reply is None:
                continue                      # no such unit: stay silent, like the real device
            self.request.sendall(struct.pack(">HHHB", tid, 0, len(reply) + 1, unit) + reply)
        log.debug("Client disconnected: %s", peer)


class FakeSigenServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host: str, port: int, data_dir: Path, ignore_writes: bool = False,
                 reject_writes: bool = False):
        self.bank = RegisterBank(data_dir)
        self.ignore_writes = ignore_writes
        self.reject_writes = reject_writes
        super().__init__((host, port), _Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def process(self, unit: int, pdu: bytes, peer: str) -> Optional[bytes]:
        """The reply PDU, or None to send nothing (a unit ID with no data)."""
        fn = pdu[0]

        def exc(code, why):
            log.info("%s unit %d fn %02d -> exception %02d (%s)", peer, unit, fn, code, why)
            return struct.pack(">BB", fn | 0x80, code)

        if unit not in self.bank.units():
            log.info("%s unit %d fn %02d -> no reply (no fake data for that unit)", peer, unit, fn)
            return None

        if fn in (3, 4):
            if len(pdu) != 5:
                return exc(EXC_ILLEGAL_VALUE, "bad request length")
            address, count = struct.unpack(">HH", pdu[1:5])
            if not 1 <= count <= 125:
                return exc(EXC_ILLEGAL_VALUE, f"count {count}")
            table = "holding" if fn == 3 else "input"
            words = self.bank.read(unit, table, address, count)
            if words is None:
                return exc(EXC_ILLEGAL_ADDRESS, f"{table} {address}+{count} not defined")
            log.debug("%s read %s %d+%d -> %s", peer, table, address, count, words)
            return struct.pack(f">BB{count}H", fn, count * 2, *words)

        if fn in (6, 16):
            if fn == 6:
                if len(pdu) != 5:
                    return exc(EXC_ILLEGAL_VALUE, "bad request length")
                address, value = struct.unpack(">HH", pdu[1:5])
                words, reply = [value], pdu
            else:
                if len(pdu) < 6:
                    return exc(EXC_ILLEGAL_VALUE, "bad request length")
                address, count, nbytes = struct.unpack(">HHB", pdu[1:6])
                if not 1 <= count <= 123 or nbytes != count * 2 or len(pdu) != 6 + nbytes:
                    return exc(EXC_ILLEGAL_VALUE, "bad count/byte count")
                words = list(struct.unpack(f">{count}H", pdu[6:6 + nbytes]))
                reply = struct.pack(">BHH", fn, address, count)
            if self.reject_writes:
                return exc(EXC_DEVICE_FAILURE, f"rejecting write to holding {address} (--reject-writes)")
            if not self.bank.write(unit, address, words, store=not self.ignore_writes):
                return exc(EXC_ILLEGAL_ADDRESS, f"holding {address}+{len(words)} not defined")
            note = " (IGNORED: --ignore-writes)" if self.ignore_writes else ""
            log.info("%s WRITE holding %d = %s%s", peer, address, words, note)
            return reply

        return exc(EXC_ILLEGAL_FUNCTION, "unsupported function")


def start_in_thread(data_dir: Path, host: str = "127.0.0.1", port: int = 0,
                    ignore_writes: bool = False, reject_writes: bool = False) -> FakeSigenServer:
    """Start a fake server on a background thread (port 0 = pick a free port). For tests."""
    server = FakeSigenServer(host, port, data_dir, ignore_writes, reject_writes)
    # A short poll interval: shutdown() waits up to one of them (the default 0.5 s, per test).
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return server
