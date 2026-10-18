"""The one and only write path: validate → read → write → read back → journal.

Nothing else in sigen-modbus writes to the device. Every write:
  1. is checked against the allowlist in registers.py (exit 6 if refused),
  2. reads the current value first (and checks --expect-current, exit 7),
  3. does nothing if the value is already right (exit 0) or --apply wasn't given (exit 8),
  4. writes, then reads the register back and compares the raw words (exit 5 on mismatch),
  5. is recorded in the journal (one JSON object per line) when --apply was given.
"""

import datetime
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from sigen.errors import ConnectionFailed, ExitCode, ModbusError, SigenError
from sigen.modbus import ModbusClient
from sigen.registers import BY_NAME, Register

DEFAULT_JOURNAL = Path.home() / ".local" / "state" / "sigen-modbus" / "journal.jsonl"

# Outcomes, and the exit code each one produces
OUTCOME_EXIT = {
    "written": ExitCode.OK,  # written and verified by read-back
    "unchanged": ExitCode.OK,  # already at the target; nothing written
    "dry_run": ExitCode.DRY_RUN,  # change needed but --apply not given
    "refused": ExitCode.REFUSED,  # failed the allowlist
    "precondition_failed": ExitCode.PRECONDITION,
    "verify_failed": ExitCode.VERIFY_FAILED,  # read-back differs from what was written
    "write_unconfirmed": ExitCode.VERIFY_FAILED,  # write sent, but couldn't read back: state unknown
    "write_rejected": ExitCode.MODBUS,  # device refused the write with a Modbus exception
    "read_failed": ExitCode.CONNECTION,  # couldn't do the first read; nothing was written
}


@dataclass
class WriteResult:
    register: Register
    outcome: str
    message: str
    target_words: Optional[List[int]] = None
    before_words: Optional[List[int]] = None
    after_words: Optional[List[int]] = None
    write_sent: bool = False
    undo_of: Optional[str] = None
    id: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
        + secrets.token_hex(3)
    )

    @property
    def exit_code(self) -> ExitCode:
        if self.outcome == "read_failed" and "Modbus exception" in self.message:
            return ExitCode.MODBUS
        return OUTCOME_EXIT[self.outcome]

    def _show(self, words):
        return self.register.describe(words) if words is not None else None

    def to_dict(self, **context) -> dict:
        r = self.register
        return {
            "id": self.id,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            **context,
            "register": r.name,
            "table": r.table,
            "address": r.address,
            "before": self._show(self.before_words),
            "before_raw": self.before_words,
            "target": self._show(self.target_words),
            "target_raw": self.target_words,
            "after": self._show(self.after_words),
            "after_raw": self.after_words,
            "write_sent": self.write_sent,
            "outcome": self.outcome,
            "exit_code": int(self.exit_code),
            "message": self.message,
            "undo_of": self.undo_of,
        }


class Journal:
    def __init__(self, path: Path = DEFAULT_JOURNAL):
        self.path = Path(path)

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entries(self) -> List[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # never let one damaged line block undo of the others
        return out

    def last_undoable(self, ip: str, port: int, unit: int) -> Optional[dict]:
        """Most recent successful write to this device that hasn't been undone yet."""
        entries = [e for e in self.entries() if e.get("ip") == ip and e.get("port") == port and e.get("unit") == unit]
        undone = {e["undo_of"] for e in entries if e.get("undo_of") and e.get("outcome") in ("written", "unchanged")}
        for e in reversed(entries):
            if e.get("outcome") == "written" and not e.get("undo_of") and e["id"] not in undone:
                return e
        return None


def set_register(
    connect: Callable[[], ModbusClient],
    reg: Register,
    value: float,
    *,
    apply: bool,
    expect_words: Optional[List[int]] = None,
    undo_of: Optional[str] = None,
) -> WriteResult:
    """Read → write → read back. Never raises for device problems; returns the outcome."""

    def result(outcome, message, **kw):
        return WriteResult(reg, outcome, message, undo_of=undo_of, **kw)

    # 1. Allowlist
    try:
        target = reg.validate(value)
    except ValueError as e:
        return result("refused", str(e))

    with_target = {"target_words": target}
    try:
        with connect() as client:
            # 2. Read before
            try:
                before = client.read(reg.table, reg.address, reg.words)
            except SigenError as e:
                return result("read_failed", f"couldn't read {reg.name} before writing: {e}", **with_target)
            seen = {**with_target, "before_words": before}

            if expect_words is not None and before != expect_words:
                return result(
                    "precondition_failed",
                    f"expected current value {reg.describe(expect_words)}, "
                    f"but it is {reg.describe(before)}; nothing written",
                    **seen,
                )
            if before == target:
                return result("unchanged", f"already {reg.describe(target)}; nothing written", **seen)
            if not apply:
                return result("dry_run", "dry run: nothing written (add --apply to write)", **seen)

            # 3. Write
            try:
                client.write(reg.address, target)
            except ModbusError as e:
                after = _try_read(client, connect, reg)
                return result(
                    "write_rejected", f"device rejected the write: {e}", write_sent=True, after_words=after, **seen
                )
            except ConnectionFailed as e:
                # The write may or may not have landed. Find out on a fresh connection.
                after = _try_read(None, connect, reg)
                if after is None:
                    return result(
                        "write_unconfirmed",
                        f"connection failed during the write ({e}) and the read-back also "
                        f"failed: the register's state is UNKNOWN; check it before retrying",
                        write_sent=True,
                        **seen,
                    )
                if after == target:
                    return result(
                        "written",
                        f"verified (the write's reply was lost: {e})",
                        write_sent=True,
                        after_words=after,
                        **seen,
                    )
                return result(
                    "verify_failed",
                    f"write failed ({e}); register reads {reg.describe(after)}",
                    write_sent=True,
                    after_words=after,
                    **seen,
                )

            # 4. Read back
            after = _try_read(client, connect, reg)
    except ConnectionFailed as e:
        return result("read_failed", f"couldn't connect to read {reg.name}: {e}", **with_target)

    if after is None:
        return result(
            "write_unconfirmed",
            "write was acknowledged but the read-back failed: "
            "the register's state is UNKNOWN; check it before retrying",
            write_sent=True,
            **seen,
        )
    if after != target:
        return result(
            "verify_failed",
            f"read back {reg.describe(after)} but wrote {reg.describe(target)}",
            write_sent=True,
            after_words=after,
            **seen,
        )
    return result("written", "written and verified", write_sent=True, after_words=after, **seen)


def _try_read(client: Optional[ModbusClient], connect, reg: Register) -> Optional[List[int]]:
    """Read back on the current connection, falling back to a fresh one. None if both fail."""
    if client is not None:
        try:
            return client.read(reg.table, reg.address, reg.words)
        except SigenError:
            pass
    try:
        with connect() as fresh:
            return fresh.read(reg.table, reg.address, reg.words)
    except SigenError:
        return None


def plan_undo(journal: Journal, ip: str, port: int, unit: int):
    """Find the write to undo. Returns (entry, register, value_to_restore, expect_words) or None."""
    entry = journal.last_undoable(ip, port, unit)
    if entry is None:
        return None
    reg = BY_NAME.get(entry["register"])
    if reg is None:
        raise SigenError(f"journal entry {entry['id']} refers to unknown register {entry['register']}")
    restore, _ = reg.decode(entry["before_raw"])
    return entry, reg, restore, entry["after_raw"]
