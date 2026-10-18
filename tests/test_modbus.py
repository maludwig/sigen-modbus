"""ModbusClient against a scripted socket: malformed replies are ConnectionFailed, never an IndexError."""

import struct

import pytest

from sigen.errors import ConnectionFailed, ModbusError
from sigen.modbus import ModbusClient


class ScriptedSocket:
    """Takes whatever is sent and answers with `reply`, the bytes after the request, in one go."""

    def __init__(self, reply: bytes):
        self.reply = reply

    def sendall(self, data: bytes) -> None:
        pass

    def recv(self, n: int) -> bytes:
        out, self.reply = self.reply[:n], self.reply[n:]
        return out


def client_answering(body: bytes, unit: int = 247) -> ModbusClient:
    """A client whose first request (transaction 1) gets `body` as its reply PDU."""
    c = ModbusClient("192.0.2.1", unit=unit)
    c._sock = ScriptedSocket(struct.pack(">HHHB", 1, 0, len(body) + 1, unit) + body)
    return c


def test_a_reply_with_no_body_is_connection_failed():
    with pytest.raises(ConnectionFailed, match="short reply"):
        client_answering(b"").read("input", 30014, 1)


def test_a_one_byte_exception_reply_is_connection_failed():
    with pytest.raises(ConnectionFailed, match="short reply"):
        client_answering(b"\x84").read("input", 30014, 1)  # an exception reply with no exception code


def test_a_whole_exception_reply_is_still_a_modbus_error():
    with pytest.raises(ModbusError) as e:
        client_answering(b"\x84\x02").read("input", 30014, 1)
    assert e.value.code == 2


def test_a_good_read():
    assert client_answering(b"\x04\x02\x03\x70").read("input", 30014, 1) == [880]


def test_an_unknown_table_is_refused_before_anything_is_sent():
    with pytest.raises(ValueError, match="unknown register table"):
        client_answering(b"\x04\x02\x03\x70").read("Input", 30014, 1)


def test_writing_nothing_is_refused():
    with pytest.raises(ValueError, match="nothing to write"):
        client_answering(b"").write(40046, [])
