"""derive() and the summary, checked against real captures."""

import json

import pytest

from conftest import FULL_DATA, MIDSOC_DATA, REAL_DATA
from sigen.reading import derive, format_summary, readings_from_document


def load(scenario):
    return readings_from_document(json.loads((scenario / "sigenstor.json").read_text(encoding="utf-8")))


def test_capacity_from_midsoc_capture():
    d = derive(load(MIDSOC_DATA))
    assert d["battery_available_kwh"] == pytest.approx(8.80)
    assert d["battery_room_kwh"] == pytest.approx(7.08)
    assert d["battery_total_estimate_kwh"] == pytest.approx(15.88)
    assert d["battery_soc_x_nominal_kwh"] == pytest.approx(8.34)
    assert "battery_stored_kwh_estimate" not in d


@pytest.mark.parametrize("scenario", [MIDSOC_DATA, REAL_DATA])
def test_soc_is_available_over_total(scenario):
    readings = load(scenario)
    d = derive(readings)
    soc = readings["plant_ess_soc"]["value"]
    # SOC has 0.1 % resolution and the capacities 0.01 kWh, so allow a little rounding
    assert 100 * d["battery_available_kwh"] / d["battery_total_estimate_kwh"] == pytest.approx(soc, abs=0.15)


def test_summary_shows_available_over_total():
    out = format_summary("192.0.2.1", load(MIDSOC_DATA))
    assert "55.4%" in out and "8.80 / 15.88 kWh available / total" in out and "15.06 kWh nominal" in out


def test_alarms_none_on_the_real_system():
    readings = load(FULL_DATA)
    assert derive(readings)["active_alarms"] == []
    assert "Alarms:            none\n" in format_summary("192.0.2.1", readings)


def with_raw(readings, name, raw):
    from sigen.registers import BY_NAME

    value, label = BY_NAME[name].decode(raw)
    readings[name] = {**readings[name], "raw": raw, "value": value, "label": label}
    return readings


def test_active_alarms_are_listed_by_group():
    readings = with_raw(load(FULL_DATA), "plant_merged_alarm_1", [(1 << 2) | (1 << 9)])
    readings = with_raw(readings, "plant_merged_alarm_3", [1 << 6])
    d = derive(readings)
    assert d["active_alarms"] == ["PCS: Over-temperature", "PCS: Grid power outage", "ESS: Thermal runaway"]
    assert "Alarms:            PCS: Over-temperature; PCS: Grid power outage; ESS: Thermal runaway" in format_summary(
        "192.0.2.1", readings
    )


def test_unavailable_alarm_register_is_not_sixteen_alarms():
    readings = with_raw(load(FULL_DATA), "plant_merged_alarm_4", [0xFFFF])
    d = derive(readings)
    assert d["active_alarms"] == [] and d["alarm_registers_unavailable"] == ["plant_merged_alarm_4"]
    assert "(some alarm registers not available)" in format_summary("192.0.2.1", readings)


def test_inverter_section_from_a_two_unit_dump():
    from conftest import BOTH_DATA
    from sigen.reading import format_inverter, other_unit_readings

    doc = json.loads((BOTH_DATA / "sigenstor.json").read_text(encoding="utf-8"))
    inverters = other_unit_readings(doc)
    assert list(inverters) == [1]
    lines = "\n".join(format_inverter(1, inverters[1]))
    assert "SigenStor EC SP US" in lines and "State:           Running, internal 49.1 °C" in lines
    assert "2: 247 V 2.63 A (0.65 kW)" in lines and "Alarms:          none" in lines
    summary = format_summary("192.0.2.1", readings_from_document(doc), inverters=inverters)
    assert summary.index("Lifetime grid") < summary.index("Inverter (unit 1)")


def test_summary_without_capacity_registers_falls_back_to_nominal():
    readings = load(MIDSOC_DATA)
    del readings["plant_ess_available_max_charging_capacity"]
    out = format_summary("192.0.2.1", readings)
    assert "55.4% of 15.06 kWh nominal" in out and "available" not in out


def test_inverter_lines_survive_a_partial_read():
    from sigen.reading import format_inverter

    readings = {  # one register of each group read, the rest failed
        "inverter_model_type": {"value": "SigenStor EC"},
        "inverter_phase_a_voltage": {"value": 121.04},
        "inverter_ess_min_cluster_temperature": {"value": 8.5},
        "inverter_phase_b_voltage": {"error": "timed out"},
    }
    lines = "\n".join(format_inverter(1, readings))
    assert "firmware ?" in lines
    assert "Grid:            121.0 V / ? V (? V), ? Hz" in lines
    assert "Battery cells:   8.5–? °C, average ? V" in lines
