import json

import pytest

from conftest import REPO
from sigen.registers import BY_NAME, PLANT_REGISTERS, WRITABLE, decode_int, decode_string, encode_int, encode_string


def _fake_data_strings():
    for path in sorted((REPO / "fake_data").glob("*/*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for section in [doc] + doc.get("other_units", []):
            for table in ("input_registers", "holding_registers"):
                for address, reg in section.get(table, {}).items():
                    if reg.get("type") == "string" and "raw" in reg:
                        yield pytest.param(reg["raw"], reg["value"], id=f"{path.parent.name}:{address}")


@pytest.mark.parametrize("raw,value", list(_fake_data_strings()))
def test_fake_data_strings_match_their_words(raw, value):
    """The fake server replays the raw words, the docs show the value: they must agree
    (hand-edited values, such as the scrubbed serial number, included)."""
    assert decode_string(raw) == value


@pytest.mark.parametrize(
    "rtype,n",
    [
        ("u16", 0),
        ("u16", 65535),
        ("s16", -420),
        ("s16", 32767),
        ("u32", 0),
        ("u32", 1791301710),
        ("s32", -40),
        ("s32", -2147483648),
        ("u64", 1538),
        ("u64", 2**64 - 2),
        ("s64", -1),
    ],
)
def test_int_round_trip(rtype, n):
    assert decode_int(encode_int(n, rtype), rtype) == n


def test_encode_rejects_overflow():
    with pytest.raises(ValueError):
        encode_int(70000, "u16")
    with pytest.raises(ValueError):
        encode_int(-1, "u32")


def test_string_round_trip():
    assert decode_string(encode_string("SigenStor", 15)) == "SigenStor"


def test_real_raw_words_decode():
    # Words captured from the real system on 2026-10-06
    assert BY_NAME["plant_grid_sensor_active_power"].decode([65535, 65516])[0] == pytest.approx(-0.02)
    assert BY_NAME["plant_system_timezone"].decode([65116])[0] == -420
    assert BY_NAME["plant_ess_soc"].decode([999])[0] == pytest.approx(99.9)


def test_alarm_bit_fields():
    pcs1, pcs2 = BY_NAME["plant_merged_alarm_1"], BY_NAME["plant_merged_alarm_2"]
    assert pcs1.decode([0]) == (0, "none")
    assert pcs1.describe([0]) == "none (0)"
    assert pcs1.decode([(1 << 2) | (1 << 9)]) == (516, "Over-temperature; Grid power outage")
    assert pcs2.decode([1 << 6])[1] == "undefined bit 6"  # the protocol skips bit 6 here
    assert pcs1.decode([0xFFFF])[1].startswith("Not available")  # unsupported, read inside a block
    assert {r.name for r in PLANT_REGISTERS if r.flags} == {f"plant_merged_alarm_{n}" for n in range(1, 7)}


def test_not_set_sentinel():
    value, label = BY_NAME["plant_ess_max_charging_limit"].decode([65535, 65535])
    assert value is None and label.startswith("Not set")


def test_unique_addresses_and_names_across_devices():
    from sigen.registers import ALL_REGISTERS

    keys = [(r.table, r.address) for r in ALL_REGISTERS]
    assert len(keys) == len(set(keys))
    assert len({r.name for r in ALL_REGISTERS}) == len(ALL_REGISTERS)
    spans = sorted((r.table, r.address, r.address + r.words - 1) for r in ALL_REGISTERS)
    for (t1, _, end), (t2, start, _) in zip(spans, spans[1:]):
        assert t1 != t2 or start > end, f"overlap at {start}"


def test_inverter_map():
    from sigen.registers import INVERTER_REGISTERS

    assert len(INVERTER_REGISTERS) == 65
    assert all(
        r.device == "inverter" and r.table == "input" and r.name.startswith("inverter_") for r in INVERTER_REGISTERS
    )
    assert all(r.device == "plant" for r in PLANT_REGISTERS)
    model = BY_NAME["inverter_model_type"]
    assert model.words == 15 and not model.stored
    assert model.decode_text(encode_string("SigenStor EC SP US", 15)) == "SigenStor EC SP US"
    assert model.describe(encode_string("SigenStor EC SP US", 15)) == "SigenStor EC SP US"
    with pytest.raises(ValueError, match="is text"):  # decode is for numbers only
        model.decode(encode_string("SigenStor EC SP US", 15))
    with pytest.raises(ValueError, match="is a number"):
        BY_NAME["plant_ess_soc"].decode_text([999])
    assert (BY_NAME["inverter_pv4_voltage"].address, BY_NAME["inverter_pv4_current"].address) == (31033, 31034)


def test_allowlist_is_small_and_explicit():
    assert {r.name for r in WRITABLE} == {"plant_remote_ems_control_mode", "plant_backup_soc"}
    assert all(r.table == "holding" and r.cli_option for r in WRITABLE)


def test_validate_backup_soc():
    reg = BY_NAME["plant_backup_soc"]
    assert reg.validate(20) == [200]
    assert reg.validate(25.5) == [255]
    for bad in (-1, 100.1, 20.05):
        with pytest.raises(ValueError):
            reg.validate(bad)


def test_validate_remote_ems_mode():
    reg = BY_NAME["plant_remote_ems_control_mode"]
    assert reg.validate(reg.parse_value("max-self-consumption")) == [2]
    assert reg.validate(reg.parse_value("0")) == [0]
    with pytest.raises(ValueError):  # forced discharge: not allowed yet
        reg.validate(reg.parse_value("discharge-ess-first"))
    with pytest.raises(ValueError):
        reg.parse_value("turbo")


def test_read_only_register_refuses():
    with pytest.raises(ValueError, match="not writable"):
        BY_NAME["plant_grid_point_maximum_export_limitation"].validate(5)


def test_validate_words_is_the_same_allowlist_for_raw_words():
    soc, mode = BY_NAME["plant_backup_soc"], BY_NAME["plant_remote_ems_control_mode"]
    assert soc.validate_words([200]) == [200] and mode.validate_words([2]) == [2]
    refused = [
        (mode, [1], "not an allowed value"),
        (soc, [0xFFFF], "above the maximum"),
        (soc, [1, 2], "16-bit word"),
        (soc, [70000], "16-bit word"),
        (BY_NAME["plant_ess_soc"], [500], "not writable"),
    ]
    for reg, words, why in refused:
        with pytest.raises(ValueError, match=why):
            reg.validate_words(words)
