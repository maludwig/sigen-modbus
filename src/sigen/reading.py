"""Turning readings into useful numbers and summaries. (Reading itself: sigen.block_reader.)"""

import datetime
from typing import Any

from sigen.config import SMART_LOAD_SLOTS, Config
from sigen.registers import ALL_ONES_16, BY_ADDRESS, BY_NAME, flag_names

TABLE_KEYS = {"input": "input_registers", "holding": "holding_registers"}
ALARM_GROUPS = {
    "plant_merged_alarm_1": "PCS",
    "plant_merged_alarm_2": "PCS",
    "plant_merged_alarm_3": "ESS",
    "plant_merged_alarm_4": "Gateway",
    "plant_merged_alarm_5": "DC charger",
    "plant_merged_alarm_6": "Plant",
}


def readings_from_document(doc: dict) -> dict[str, dict]:
    """A saved dump -> readings, as if just read from the device.

    Registers with raw words are re-decoded with the current register map, so dumps
    made by older versions display correctly. Unknown addresses keep their stored values.
    """
    readings: dict[str, dict] = {}
    for table, key in TABLE_KEYS.items():
        for addr_s, spec in sorted(doc.get(key, {}).items(), key=lambda kv: int(kv[0])):
            addr = int(addr_s)
            reg = BY_ADDRESS.get((table, addr))
            name = reg.name if reg else spec.get("name", f"{table}_{addr}")
            entry = {
                "table": table,
                "address": addr,
                "type": spec.get("type", reg.type if reg else "u16"),
                "gain": spec.get("gain", reg.gain if reg else 1),
            }
            if reg and reg.unit or spec.get("unit"):
                entry["unit"] = reg.unit if reg and reg.unit else spec.get("unit")
            raw = spec.get("raw")
            if reg and raw is not None and len(raw) == reg.words:
                entry["value"], label = reg.decode(raw)
                if label is not None:
                    entry["label"] = label
            else:
                entry["value"] = spec.get("value")
                if "label" in spec:
                    entry["label"] = spec["label"]
            if raw is not None:
                entry["raw"] = raw
            readings[name] = entry
    for name, e in doc.get("errors", {}).items():
        readings[name] = {"table": e.get("table"), "address": e.get("address"), "error": e.get("error")}
    return readings


def other_unit_readings(doc: dict) -> dict[int, dict[str, dict]]:
    """A saved dump's other units (inverters) -> {unit ID: readings}."""
    return {int(s["unit_id"]): readings_from_document(s) for s in doc.get("other_units", [])}


def format_table(readings: dict[str, dict], config: Config = Config(), title: str = "") -> str:
    """Every register, grouped into live status and settings, aligned for a terminal."""
    rows = {"input": [], "holding": []}
    for name, r in readings.items():
        alias = config.register_alias(name)
        if alias:
            name = f"{name} ({alias})"
        if "error" in r:
            shown = f"(error: {r['error']})"
        else:
            v, label, unit = r.get("value"), r.get("label"), r.get("unit")
            if label is not None and v is not None:
                shown = f"{label} ({v:g})" if isinstance(v, (int, float)) else f"{label}"
            elif label is not None:
                shown = label
            elif isinstance(v, str):
                shown = v
            elif isinstance(v, float):
                shown = f"{v:,.3f}".rstrip("0").rstrip(".") + (f" {unit}" if unit else "")
            elif v is not None:
                shown = f"{v:,}" + (f" {unit}" if unit else "")
            else:
                shown = "—"
        rows.setdefault(r.get("table") or "input", []).append((r.get("address") or 0, name, shown))
    width = max((len(n) for t in rows.values() for _, n, _ in t), default=20)
    out = []
    for table, heading in (("input", "Live status (input registers)"), ("holding", "Settings (holding registers)")):
        if not rows.get(table):
            continue
        heading = f"{title}: {heading}" if title else heading
        out += ["", heading, "─" * len(heading)]
        for addr, name, shown in sorted(rows[table]):
            out.append(f"  {addr:>5}  {name:<{width}}  {shown}")
    return "\n".join(out)


def value(readings: dict[str, dict], name: str, default: Any = None) -> Any:
    """A register's value from readings (a number, or text for string registers); default when it wasn't read, failed,
    or holds the "not set" sentinel."""
    v = readings.get(name, {}).get("value")
    return default if v is None else v


def derive(readings: dict[str, dict], now: datetime.datetime | None = None) -> dict:
    """Figures worked out from the readings: house load and flow directions (kW), battery energy (kWh), active alarms by
    group, and the device clock. A key is left out when its registers weren't read. `now` (default: the current time)
    is only used for the clock skew; read-json passes the dump's read time. Saved as "derived" in JSON dumps."""
    now = now or datetime.datetime.now(datetime.UTC)
    out = {}
    pv, grid, ess = (
        value(readings, n) for n in ("plant_photovoltaic_power", "plant_grid_sensor_active_power", "plant_ess_power")
    )
    if None not in (pv, grid, ess):
        out["house_load_estimate_kw"] = round(pv + grid - ess, 3)
        out["battery_direction"] = "charging" if ess > 0 else "discharging" if ess < 0 else "idle"
        out["grid_direction"] = "importing" if grid > 0 else "exporting" if grid < 0 else "balanced"
    load = value(readings, "plant_total_load_power")
    if load is not None:
        out["house_load_reported_kw"] = load
    available = value(readings, "plant_ess_available_max_discharging_capacity")
    room = value(readings, "plant_ess_available_max_charging_capacity")
    if available is not None:
        out["battery_available_kwh"] = available
    if room is not None:
        out["battery_room_kwh"] = room
    if None not in (available, room):
        # The BMS's live total; SOC (30014) is exactly available / this.
        out["battery_total_estimate_kwh"] = round(available + room, 2)
    soc, cap = value(readings, "plant_ess_soc"), value(readings, "plant_ess_rated_energy_capacity")
    if None not in (soc, cap):
        # Cruder than battery_available_kwh: assumes the pack holds exactly its nameplate size.
        out["battery_soc_x_nominal_kwh"] = round(soc / 100 * cap, 2)
    alarms, unavailable = [], []
    for name, group in ALARM_GROUPS.items():
        n = value(readings, name)
        if n is None:
            continue
        if n == ALL_ONES_16:
            unavailable.append(name)
        else:
            alarms += [f"{group}: {meaning}" for meaning in flag_names(int(n), BY_NAME[name].flags)]
    if any(name in readings and "value" in readings[name] for name in ALARM_GROUPS):
        out["active_alarms"] = alarms
        if unavailable:
            out["alarm_registers_unavailable"] = unavailable
    t = value(readings, "plant_system_time")
    if t:
        # The SigenStor stores local wall-clock time in this "epoch" register, not UTC.
        out["device_clock_local"] = datetime.datetime.fromtimestamp(t, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S")
        out["device_clock_skew_hours_vs_utc"] = round((t - now.timestamp()) / 3600, 2)
    return out


def format_smart_loads(readings: dict[str, dict], config: Config) -> list[str]:
    """Named slots, plus any unnamed slot that has ever drawn power. Empty if none apply."""
    rows = []
    for slot in SMART_LOAD_SLOTS:
        power = readings.get(f"plant_smart_load_{slot}_power", {})
        total = readings.get(f"plant_smart_load_{slot}_total_consumption", {})
        name = config.smart_load_names.get(slot)
        if name is None and not (power.get("value") or total.get("value")):
            continue
        label = f"{name} ({slot})" if name else f"slot {slot}"
        if power.get("value") is None or total.get("value") is None:
            rows.append(f"    {label:<24} (not readable)")
        else:
            rows.append(f"    {label:<24} {power['value']:6.2f} kW   {total['value']:8.2f} kWh lifetime")
    return ["  Smart loads:"] + rows if rows else []


INVERTER_ALARMS = {
    f"inverter_alarm_{n}": group for n, group in zip(range(1, 6), ("PCS", "PCS", "ESS", "Gateway", "DC charger"))
}


def format_inverter(unit: int, readings: dict[str, dict]) -> list[str]:
    """Summary lines for one inverter unit; lines whose registers weren't read are left out."""
    v = lambda n: value(readings, n)
    label = lambda n: readings.get(n, {}).get("label")
    num = lambda n, spec: "?" if v(n) is None else format(v(n), spec)  # one unread register in a line: "?"
    head = f"  Inverter (unit {unit}):"
    if v("inverter_model_type"):
        head += f" {v('inverter_model_type')}, firmware {v('inverter_firmware_version') or '?'}"
    lines = [head]
    if label("inverter_running_state"):
        temp = v("inverter_pcs_internal_temperature")
        lines.append(
            f"    State:           {label('inverter_running_state')}"
            + (f", internal {temp:.1f} °C" if temp is not None else "")
        )
    strings = []
    for i in range(1, (v("inverter_pv_string_count") or 0) + 1):
        volts, amps = v(f"inverter_pv{i}_voltage"), v(f"inverter_pv{i}_current")
        if volts is not None and amps is not None:
            strings.append(f"{i}: {volts:.0f} V {amps:.2f} A ({volts * amps / 1000:.2f} kW)")
    if strings:
        lines.append(f"    PV strings:      {'   '.join(strings)}")
    if v("inverter_pv_power") is not None:
        lines.append(
            f"    PV:              {v('inverter_pv_power'):.2f} kW now, "
            f"{v('inverter_pv_daily_generation') or 0:.2f} kWh today"
        )
    if v("inverter_phase_a_voltage") is not None:
        lines.append(
            f"    Grid:            {num('inverter_phase_a_voltage', '.1f')} V / "
            f"{num('inverter_phase_b_voltage', '.1f')} V "
            f"({num('inverter_ab_line_voltage', '.1f')} V), {num('inverter_grid_frequency', '.2f')} Hz"
        )
    if v("inverter_ess_min_cluster_temperature") is not None:
        lines.append(
            f"    Battery cells:   {num('inverter_ess_min_cluster_temperature', '.1f')}–"
            f"{num('inverter_ess_max_cluster_temperature', '.1f')} °C, "
            f"average {num('inverter_ess_average_cell_voltage', '.3f')} V"
        )
    alarms, readable = [], False
    for name, group in INVERTER_ALARMS.items():
        n = v(name)
        if n is not None and n != ALL_ONES_16:
            readable = True
            alarms += [f"{group}: {meaning}" for meaning in flag_names(int(n), BY_NAME[name].flags)]
    if readable:
        lines.append(f"    Alarms:          {'; '.join(alarms) or 'none'}")
    failed = [n for n, r in readings.items() if "error" in r]
    if failed:
        lines.append(f"    ({len(failed)} register(s) not readable)")
    return lines


def format_summary(
    ip: str,
    readings: dict[str, dict],
    now: datetime.datetime | None = None,
    title: str | None = None,
    config: Config = Config(),
    inverters: dict[int, dict[str, dict]] | None = None,
) -> str:
    """The plant summary read, capture and read-json print: modes, alarms, battery, power flows, smart loads and lifetime
    grid totals, then a section per inverter (unit ID -> readings). Anything unread shows "(not readable)", never a
    0 that looks like a reading."""
    d = derive(readings, now)
    v = lambda n: value(readings, n)
    label = lambda n: readings.get(n, {}).get("label", "(not readable)")

    def show(name: str, spec: str, unit: str = "") -> str:
        """A register's value formatted with `spec` and `unit`, or "(not readable)"."""
        x = v(name)
        return "(not readable)" if x is None else f"{format(x, spec)}{unit}"

    lines = [
        title or f"👋 Hello from your SigenStor at {ip}",
        "",
        f"  EMS mode:          {label('plant_ems_work_mode')}",
        f"  Remote EMS mode:   {label('plant_remote_ems_control_mode')}",
    ]
    if "active_alarms" in d:
        lines.append(
            f"  Alarms:            {'; '.join(d['active_alarms']) or 'none'}"
            + (" (some alarm registers not available)" if "alarm_registers_unavailable" in d else "")
        )
    if "battery_total_estimate_kwh" in d:
        lines.append(
            f"  Battery:           {show('plant_ess_soc', '.1f', '%')}  {d['battery_available_kwh']:.2f} / "
            f"{d['battery_total_estimate_kwh']:.2f} kWh available / total  "
            f"({show('plant_ess_rated_energy_capacity', '.2f', ' kWh')} nominal)"
        )
    else:
        lines.append(
            f"  Battery:           {show('plant_ess_soc', '.1f', '%')} of "
            f"{show('plant_ess_rated_energy_capacity', '.2f', ' kWh')} nominal"
        )
    if v("plant_ess_average_cell_temperature") is not None:
        lines.append(f"  Battery temp:      {show('plant_ess_average_cell_temperature', '.1f', ' °C')}")
    lines += [
        f"  Solar now:         {show('plant_photovoltaic_power', '6.2f', ' kW')}",
        f"  Battery now:       {show('plant_ess_power', '+6.2f', ' kW')}  ({d.get('battery_direction', '?')})",
        f"  Grid now:          {show('plant_grid_sensor_active_power', '+6.2f', ' kW')}"
        f"  ({d.get('grid_direction', '?')})",
    ]
    if "house_load_reported_kw" in d:
        lines.append(f"  House load:        {d['house_load_reported_kw']:6.2f} kW")
    elif "house_load_estimate_kw" in d:
        lines.append(f"  House load (est):  {d['house_load_estimate_kw']:6.2f} kW")
    lines += format_smart_loads(readings, config)
    lines += [
        f"  Backup reserve:    {show('plant_backup_soc', '.0f', '%')}",
        f"  Lifetime grid:     {show('plant_accumulated_grid_import_energy', ',.1f', ' kWh')} in / "
        f"{show('plant_accumulated_grid_export_energy', ',.1f', ' kWh')} out",
    ]
    failed = [n for n, r in readings.items() if "error" in r]
    if failed:
        lines += ["", f"  ({len(failed)} register(s) not readable; see --json for details)"]
    for unit, unit_readings in sorted((inverters or {}).items()):
        lines += [""] + format_inverter(unit, unit_readings)
    return "\n".join(lines)
