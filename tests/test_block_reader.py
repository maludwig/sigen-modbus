"""The block planner, the fake server's device-like read rules, and BlockReader.

The fake server follows the real SigenStor: a read that starts on an address it has no data
for, or splits a mapped register, is rejected (exception 2); addresses without data inside an
accepted read come back as 0xFFFF. The 10-06 fake data predates most of the register map,
so it exercises both: some blocks are rejected (-> one-by-one fallback), some read 0xFFFF.
"""
import functools

import pytest

from sigen.block_reader import MAX_WORDS, BlockReader, entry_for, plan_blocks
from sigen.errors import ConnectionFailed, ModbusError
from sigen.modbus import ModbusClient
from sigen.registers import BY_NAME, INVERTER_REGISTERS, PLANT_REGISTERS, Register


def R(address, rtype="u16", table="input"):
    return Register(f"r{address}", table, address, rtype)


def spans(plan):
    return [(b.table, b.start, b.count) for b in plan]


# ---------- planner ----------
def test_plan_for_the_real_register_map():
    assert spans(plan_blocks(PLANT_REGISTERS)) == [
        ("input", 30000, 124),     # 30124-30125 would make it 126
        ("input", 30124, 124),     # ends on 30244-30247
        ("input", 30248, 39),
        ("holding", 40029, 20),
    ]


def test_plan_covers_every_register_once_on_boundaries():
    plan = plan_blocks(PLANT_REGISTERS)
    seen = [r.name for b in plan for r in b.registers]
    assert sorted(seen) == sorted(r.name for r in PLANT_REGISTERS)
    for b in plan:
        first, last = b.registers[0], b.registers[-1]
        assert b.start == first.address and b.end == last.address + last.words - 1
        assert b.count <= MAX_WORDS and all(r.table == b.table for r in b.registers)


def test_no_gap_limit_by_default():
    assert spans(plan_blocks([R(1), R(100)])) == [("input", 1, 100)]          # a 98-word gap: one read
    assert spans(plan_blocks([R(1), R(200)])) == [("input", 1, 1), ("input", 200, 1)]   # too far for 125


def test_optional_gap_limit():
    regs = [R(1), R(3), R(10)]                                     # gaps of 1 and 6
    assert spans(plan_blocks(regs, max_gap=6)) == [("input", 1, 10)]
    assert spans(plan_blocks(regs, max_gap=5)) == [("input", 1, 3), ("input", 10, 1)]
    assert spans(plan_blocks(regs, max_gap=0)) == [("input", 1, 1), ("input", 3, 1), ("input", 10, 1)]


def test_size_limit_never_splits_a_register():
    regs = [R(0, "u64"), R(4, "u64"), R(8, "u64")]
    assert spans(plan_blocks(regs, max_words=10)) == [("input", 0, 8), ("input", 8, 4)]
    with pytest.raises(ValueError, match="more than max_words"):
        plan_blocks(regs, max_words=3)


def test_unsorted_mixed_tables():
    regs = [R(40005, table="holding"), R(30002), R(40004, table="holding"), R(30001)]
    assert spans(plan_blocks(regs)) == [("input", 30001, 2), ("holding", 40004, 2)]


def test_overlapping_registers_are_a_map_bug():
    with pytest.raises(ValueError, match="overlaps"):
        plan_blocks([R(10, "u32"), R(11)])


def test_empty():
    assert plan_blocks([]) == []


# ---------- the fake server reads like the device ----------
def connector(server, cls=ModbusClient, unit=247):
    return functools.partial(cls, "127.0.0.1", server.port, unit, 3.0)


def read(server, start, count):
    with connector(server)() as c:
        return c.read("input", start, count)


def test_fake_rejects_reads_starting_on_missing_data(server):
    with pytest.raises(ModbusError) as e:
        read(server, 30098, 2)                   # smart load 1: not in the 10-06 data
    assert e.value.code == 2


def test_fake_rejects_split_registers(server):
    for start, count in ((30001, 1), (30001, 3), (30000, 1), (30004, 2)):   # 30000-01 and 30005-06 are u32/s32
        with pytest.raises(ModbusError):
            read(server, start, count)


def test_fake_fills_missing_data_inside_a_read_with_ffff(server):
    words = read(server, 30004, 5)               # 30004, 30005-06, then 30007-08 (no data in 10-06)
    assert words[3:] == [0xFFFF, 0xFFFF]


# ---------- BlockReader ----------
class CountingClient(ModbusClient):
    requests = 0

    def read(self, *args):
        CountingClient.requests += 1
        return super().read(*args)


def single_reads(server):
    """Every register read on its own: the reference BlockReader must match."""
    with connector(server)() as c:
        return {r.name: entry_for(r, c.read(r.table, r.address, r.words)) for r in PLANT_REGISTERS}


@pytest.mark.parametrize("strict", [False, True])
def test_blocks_match_single_reads(full_server, strict):
    CountingClient.requests = 0
    reader = BlockReader(connector(full_server, CountingClient), strict=strict)
    readings = reader.read_all()
    assert readings == single_reads(full_server)
    assert list(readings) == [r.name for r in PLANT_REGISTERS]       # same order as the map
    assert CountingClient.requests == len(reader.plan) == 4


def test_non_strict_falls_back_one_by_one_for_rejected_blocks(server):
    readings = BlockReader(connector(server)).read_all()
    assert set(readings) == {r.name for r in PLANT_REGISTERS}
    # Block 30124-30247 starts on a register the 10-06 data lacks, so it's re-read one by one:
    assert "error" in readings["plant_smart_load_14_total_consumption"]
    assert readings["plant_accumulated_battery_charge_energy"]["raw"] == \
        single_reads_one(server, "plant_accumulated_battery_charge_energy")
    # Block 30000-30123 starts on data it has, so registers it lacks read 0xFFFF, like the device:
    assert readings["plant_smart_load_1_total_consumption"]["raw"] == [0xFFFF, 0xFFFF]
    assert readings["plant_ess_soc"]["value"] == pytest.approx(99.9)


def single_reads_one(server, name):
    reg = BY_NAME[name]
    with connector(server)() as c:
        return c.read(reg.table, reg.address, reg.words)


def test_strict_raises_on_a_rejected_block(server):
    with pytest.raises(ModbusError) as e:
        BlockReader(connector(server), strict=True).read_all()
    assert e.value.code == 2


def test_wrong_unit_id_times_out_like_the_real_device(full_server):
    # The real SigenStor doesn't answer for a unit ID it doesn't have; nor does the fake.
    silent = functools.partial(ModbusClient, "127.0.0.1", full_server.port, 9, 0.2)
    with pytest.raises(ConnectionFailed, match="timed out"):
        BlockReader(silent).read_all()


def test_plan_for_the_inverter_map():
    assert spans(plan_blocks(INVERTER_REGISTERS)) == [
        ("input", 30500, 122),     # model/serial/firmware strings through the cluster temperatures
        ("input", 31000, 42),      # grid, PV strings 1-4, PV power, startup/shutdown time
        ("input", 31509, 4),       # PV daily and total generation
    ]


@pytest.mark.parametrize("strict", [False, True])
def test_inverter_blocks_match_single_reads(both_server, strict):
    inverter = functools.partial(ModbusClient, "127.0.0.1", both_server.port, 1, 3.0)
    readings = BlockReader(inverter, INVERTER_REGISTERS, strict=strict).read_all()
    with inverter() as c:
        single = {r.name: entry_for(r, c.read(r.table, r.address, r.words)) for r in INVERTER_REGISTERS}
    assert readings == single
    assert readings["inverter_model_type"]["value"] == "SigenStor EC SP US"
    assert readings["inverter_pv_string_count"]["value"] == 4


def test_connection_failure_raises():
    with pytest.raises(ConnectionFailed):
        BlockReader(functools.partial(ModbusClient, "127.0.0.1", 9, 247, 0.2)).read_all()


def test_subset_of_registers():
    reader = BlockReader(lambda: None, [BY_NAME["plant_ess_soc"], BY_NAME["plant_backup_soc"]])
    assert spans(reader.plan) == [("input", 30014, 1), ("holding", 40046, 1)]
