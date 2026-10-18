"""Reading many registers in as few Modbus requests as possible.

What the real SigenStor accepts (read-only probes, 2026-10-07):
- A read that STARTS on an unsupported address is rejected with exception 2, and so is
  one that splits a multi-word register at either end (input 30000+124 works, 30000+125
  fails: it ends on the first word of 30124-30125).
- Anything else inside a read is fine: addresses outside our map come back with data,
  and unsupported ones (e.g. 30279, 30281, 30073-30082) come back as 0xFFFF.
- 125 words is the Modbus maximum for one read.

So a block starts and ends on register boundaries and is at most 125 words. Gaps between
mapped registers are bridged regardless of size, unless max_gap says otherwise.

Consequence: a block read can't tell you a register is unsupported; it just reads 0xFFFF.
Only a read that starts on it is rejected. Check new registers one at a time.
"""

from dataclasses import dataclass
from collections.abc import Callable, Sequence

from sigen.errors import ModbusError
from sigen.modbus import ModbusClient
from sigen.registers import PLANT_REGISTERS, Register

MAX_WORDS = 125
ILLEGAL_DATA_ADDRESS = 2
TABLE_ORDER = ("input", "holding")


@dataclass(frozen=True)
class Block:
    """One planned read: `count` words from `start` in `table`, covering `registers` (in address order)."""

    table: str
    start: int
    count: int
    registers: tuple[Register, ...]

    @property
    def end(self) -> int:
        """Last address in the block (inclusive)."""
        return self.start + self.count - 1

    def words_for(self, words: list[int], reg: Register) -> list[int]:
        """`reg`'s slice of this block's read result: `words` must be the whole block, starting at `start`."""
        offset = reg.address - self.start
        return words[offset : offset + reg.words]

    def __str__(self) -> str:
        return f"{self.table} {self.start}-{self.end} ({self.count} words, {len(self.registers)} registers)"


def plan_blocks(registers: Sequence[Register], max_words: int = MAX_WORDS, max_gap: int | None = None) -> list[Block]:
    """The fewest reads that cover `registers`, input table first, then holding.

    Greedy: walk the registers in address order and extend the current block while the
    size (and max_gap, if given) allow. For covering points on a line with intervals of
    bounded length, extending each interval as far as possible is optimal.
    """
    if max_words < 1 or (max_gap is not None and max_gap < 0):
        raise ValueError("max_words must be at least 1 and max_gap at least 0")
    gap_limit = max_words if max_gap is None else max_gap
    tables = sorted({r.table for r in registers}, key=lambda t: TABLE_ORDER.index(t) if t in TABLE_ORDER else 99)
    plan: list[Block] = []
    for table in tables:
        regs = sorted((r for r in registers if r.table == table), key=lambda r: r.address)
        current: list[Register] = []
        start = end = 0
        for reg in regs:
            if reg.words > max_words:
                raise ValueError(f"{reg.name} is {reg.words} words, more than max_words={max_words}")
            reg_end = reg.address + reg.words - 1
            if current:
                if reg.address <= end:
                    raise ValueError(
                        f"{reg.name} ({reg.address}) overlaps {current[-1].name} ({current[-1].address}-{end})"
                    )
                if reg.address - end - 1 <= gap_limit and reg_end - start + 1 <= max_words:
                    current.append(reg)
                    end = reg_end
                    continue
                plan.append(Block(table, start, end - start + 1, tuple(current)))
            current, start, end = [reg], reg.address, reg_end
        if current:
            plan.append(Block(table, start, end - start + 1, tuple(current)))
    return plan


def entry_for(reg: Register, words: list[int] | None = None, error: str | None = None) -> dict:
    """One register's reading: what dumps, summaries and the database are built from. Needs `words` or `error`."""
    if words is None and error is None:
        raise ValueError(f"entry_for({reg.name}) needs the words read or the error")
    entry = {"table": reg.table, "address": reg.address, "type": reg.type, "gain": reg.gain}
    if reg.unit:
        entry["unit"] = reg.unit
    if error is not None:
        entry["error"] = error
        return entry
    value, label = reg.decode(words)
    entry["value"] = value
    if label is not None:
        entry["label"] = label
    entry["raw"] = words
    return entry


class BlockReader:
    """Reads a fixed set of registers in planned blocks, on a fresh connection each time.

        reader = BlockReader(functools.partial(ModbusClient, "192.168.1.100"), strict=True)
        readings = reader.read_all()

    strict=True: any failure raises (ModbusError / ConnectionFailed), so callers store a
    complete snapshot or nothing. strict=False: a block rejected with exception 2 is re-read
    one register at a time, and unreadable registers get an 'error' entry.
    Connection problems, and other Modbus exceptions on a block (e.g. 11, wrong unit ID),
    always raise.
    """

    def __init__(
        self,
        connector: Callable,
        registers: Sequence[Register] = PLANT_REGISTERS,
        *,
        strict: bool = False,
        max_words: int = MAX_WORDS,
        max_gap: int | None = None,
    ) -> None:
        self.connector = connector
        self.registers = list(registers)
        self.strict = strict
        self.plan = plan_blocks(self.registers, max_words, max_gap)

    def read_all(self) -> dict[str, dict]:
        """One connection, one request per block: register name -> entry_for() dict, in the order given at
        construction. Unreadable registers (strict=False only) get an 'error' entry instead of a value."""
        words: dict[str, list[int]] = {}
        errors: dict[str, str] = {}
        with self.connector() as client:
            for block in self.plan:
                try:
                    block_words = client.read(block.table, block.start, block.count)
                except ModbusError as e:
                    if self.strict or e.code != ILLEGAL_DATA_ADDRESS:
                        raise
                    self._one_by_one(client, block, words, errors)
                    continue
                for reg in block.registers:
                    words[reg.name] = block.words_for(block_words, reg)
        return {reg.name: entry_for(reg, words.get(reg.name), errors.get(reg.name)) for reg in self.registers}

    @staticmethod
    def _one_by_one(client: ModbusClient, block: Block, words: dict[str, list[int]], errors: dict[str, str]) -> None:
        """Fallback for a rejected block: read each register alone, filling `words` or `errors` in place.
        Any Modbus exception code is recorded, not raised; connection failures still raise."""
        for reg in block.registers:
            try:
                words[reg.name] = client.read(reg.table, reg.address, reg.words)
            except ModbusError as e:
                errors[reg.name] = str(e)
