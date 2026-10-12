"""The SigenStor register map, defined once as data.

This table drives decoding, the JSON dumps, the fake server, and the write
allowlist. A register can only be written if it is marked `writable=True` here,
and only with a value that passes `Register.validate()`.

Addresses and scaling follow the Sigenergy Modbus protocol as used by the
TypQxQ/Sigenergy-Local-Modbus Home Assistant integration. Unit 247 is the plant
(whole system).
"""
import re
import struct
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple

PLANT_UNIT_ID = 247

# ---------- value codecs ----------
TYPE_WORDS = {"u16": 1, "s16": 1, "u32": 2, "s32": 2, "u64": 4, "s64": 4}
_TYPE_FMT = {"u16": ">H", "s16": ">h", "u32": ">I", "s32": ">i", "u64": ">Q", "s64": ">q"}

# Sigenergy uses all-ones to mean "not set" (e.g. no power limit configured).
NOT_SET = {"u32": 0xFFFFFFFF, "u64": 0xFFFFFFFFFFFFFFFF}


def decode_int(words: List[int], rtype: str) -> int:
    """16-bit words (big-endian, high word first) -> integer of the given type."""
    return struct.unpack(_TYPE_FMT[rtype], struct.pack(f">{len(words)}H", *words))[0]


def encode_int(n: int, rtype: str) -> List[int]:
    """Integer -> 16-bit words. Raises ValueError if it doesn't fit."""
    try:
        packed = struct.pack(_TYPE_FMT[rtype], n)
    except struct.error:
        raise ValueError(f"{n} doesn't fit in {rtype}")
    return list(struct.unpack(f">{TYPE_WORDS[rtype]}H", packed))


def encode_string(text: str, count: int) -> List[int]:
    data = text.encode("ascii", "replace")[: count * 2].ljust(count * 2, b"\x00")
    return list(struct.unpack(f">{count}H", data))


def decode_string(words: List[int]) -> str:
    return struct.pack(f">{len(words)}H", *words).rstrip(b"\x00").decode("ascii", "replace")


def slug(text: str) -> str:
    """'Max Self-Consumption' -> 'max-self-consumption' (used for CLI choices)."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# ---------- value labels ----------
EMS_MODES = {0: "Max Self-Consumption", 1: "AI Mode", 2: "TOU", 5: "Full Feed-in to Grid",
             6: "VPP Scheduling", 7: "Remote EMS", 9: "Custom"}
REMOTE_EMS_MODES = {0: "PCS Remote Control", 1: "Standby", 2: "Max Self-Consumption",
                    3: "Charge (Grid First)", 4: "Charge (PV First)", 5: "Discharge (PV First)",
                    6: "Discharge (ESS First)", 8: "V2G"}
RUNNING_STATES = {0: "Standby", 1: "Running", 2: "Fault", 3: "Shutdown", 7: "Environmental Abnormality"}
GRID_STATUS = {0: "On-grid", 1: "Off-grid (auto)", 2: "Off-grid (manual)"}
ENABLED = {0: "Disabled", 1: "Enabled"}

# Alarm bit fields (bit -> meaning), from protocol V2.9 appendices 2-5, 11 and 12, checked
# against the PDF. Gaps are the protocol's own (e.g. PCS alarms 2 has no bit 6).
PCS_ALARMS_1 = {0: "Software version mismatch", 1: "Low insulation resistance", 2: "Over-temperature",
                3: "Equipment fault", 4: "System grounding fault", 5: "PV string over-voltage",
                6: "PV string reversely connected", 7: "PV string back-filling", 8: "AFCI fault",
                9: "Grid power outage", 10: "Grid over-voltage", 11: "Grid under-voltage",
                12: "Grid over-frequency", 13: "Grid under-frequency", 14: "Grid voltage imbalance",
                15: "DC component of output current out of limit"}
PCS_ALARMS_2 = {0: "Leak current out of limit", 1: "Communication abnormal", 2: "System internal protection",
                3: "AFCI self-checking circuit fault", 4: "Off-grid protection", 5: "Manual operation protection",
                7: "Abnormal phase sequence", 8: "Short circuit to PE", 9: "Soft start failure"}
ESS_ALARMS = {0: "Software version mismatch", 1: "Low insulation resistance to ground", 2: "Over-temperature",
              3: "Equipment fault", 4: "Under-temperature", 5: "Internal protection", 6: "Thermal runaway"}
GATEWAY_ALARMS = {0: "Software version mismatch", 1: "Over-temperature", 2: "Equipment fault",
                  3: "Excessive leakage current in off-grid output", 4: "N line grounding fault",
                  5: "Abnormal phase sequence of grid wiring", 6: "Abnormal phase sequence of inverter wiring",
                  7: "Grid phase loss"}
DC_CHARGER_ALARMS = {0: "Software version mismatch", 1: "Low insulation resistance to ground",
                     2: "Over-temperature", 3: "Equipment fault", 4: "Charging fault", 5: "Equipment protection"}
PLANT_ALARMS_1 = {0: "Gateway communication abnormal", 1: "Meter communication abnormal",
                  2: "AC power sensor communication abnormal",
                  6: "Hard protection against grid-feed power limit exceedance",
                  8: "Generator failure to start", 10: "CLS fault"}
ALL_ONES_16 = 0xFFFF   # what an unsupported register reads as inside a block read


def flag_names(n: int, flags: Dict[int, str]) -> List[str]:
    """The meanings of the bits set in n; bits the protocol doesn't define are named, not dropped."""
    return [flags.get(bit, f"undefined bit {bit}") for bit in range(16) if n >> bit & 1]


@dataclass(frozen=True)
class Register:
    name: str
    table: str                  # "input" (read-only, function 04) or "holding" (settings, function 03)
    address: int
    type: str                   # u16 s16 u32 s32 u64 s64, or "string" (ASCII, `count` words)
    gain: float = 1             # raw = round(value * gain)
    unit: Optional[str] = None
    labels: Optional[Dict[int, str]] = None
    flags: Optional[Dict[int, str]] = None     # bit -> meaning, for bit-field registers (alarms)
    description: str = ""
    # Write allowlist. Only registers with writable=True can ever be written.
    writable: bool = False
    allowed: Optional[FrozenSet[int]] = None   # allowed raw values (for coded registers)
    min: Optional[float] = None                # allowed range, in engineering units
    max: Optional[float] = None
    cli_option: Optional[str] = None           # e.g. "--backup-soc"
    device: str = "plant"                      # "plant" (unit 247) or "inverter" (its own unit ID)
    count: Optional[int] = None                # words, for type "string" only

    @property
    def words(self) -> int:
        return self.count if self.type == "string" else TYPE_WORDS[self.type]

    @property
    def stored(self) -> bool:
        """Kept in the database? Strings (model, serial, firmware) are display only."""
        return self.type != "string"

    @property
    def function(self) -> int:
        return 4 if self.table == "input" else 3

    # --- decoding ---
    def decode(self, words: List[int]) -> Tuple[Optional[float], Optional[str]]:
        """Words -> (engineering value, label). Value is None for the "not set" sentinel.
        A string register's value is its text."""
        if self.type == "string":
            return decode_string(words), None
        n = decode_int(words, self.type)
        if NOT_SET.get(self.type) == n:
            return None, "Not set (no limit)"
        if self.flags is not None:
            if n == ALL_ONES_16:
                return n, "Not available (all bits set)"
            return n, "; ".join(flag_names(n, self.flags)) or "none"
        value = n / self.gain if self.gain != 1 else n
        label = self.labels.get(n, "Unknown") if self.labels is not None else None
        return value, label

    def describe(self, words: List[int]) -> str:
        value, label = self.decode(words)
        if self.type == "string":
            return value
        if label is not None:
            raw = decode_int(words, self.type)
            return f"{label} ({raw})" if value is not None else label
        unit = f" {self.unit}" if self.unit else ""
        return f"{value:g}{unit}"

    # --- encoding / validation for writes ---
    def parse_value(self, text: str) -> float:
        """CLI text -> engineering value. Coded registers accept a label slug or a number."""
        if self.labels:
            by_slug = {slug(lbl): code for code, lbl in self.labels.items()}
            if slug(text) in by_slug:
                return by_slug[slug(text)]
        try:
            return float(text)
        except ValueError:
            options = ", ".join(self.choices()) if self.labels else "a number"
            raise ValueError(f"'{text}' isn't valid for {self.name}; use {options}")

    def choices(self) -> List[str]:
        """Allowed label slugs for coded registers (for --help and error messages)."""
        if not self.labels:
            return []
        codes = sorted(self.allowed) if self.allowed is not None else sorted(self.labels)
        return [slug(self.labels[c]) for c in codes if c in self.labels]

    def encode_unchecked(self, value: float) -> List[int]:
        """Encode without the write allowlist (for comparisons only, never for writing)."""
        return encode_int(round(value * self.gain), self.type)

    def validate(self, value: float) -> List[int]:
        """Check a value against the allowlist and return its encoded words.

        Raises ValueError with a human-readable reason if it isn't allowed.
        """
        if not self.writable:
            raise ValueError(f"{self.name} ({self.table} {self.address}) is not writable")
        raw = round(value * self.gain)
        if abs(raw - value * self.gain) > 1e-6:
            raise ValueError(f"{value} has more precision than {self.name} supports "
                             f"(steps of {1 / self.gain:g})")
        if self.allowed is not None and raw not in self.allowed:
            names = ", ".join(f"{c} ({self.labels.get(c, '?')})" if self.labels else str(c)
                              for c in sorted(self.allowed))
            raise ValueError(f"{self.name}: {raw} is not an allowed value (allowed: {names})")
        if self.min is not None and value < self.min:
            raise ValueError(f"{self.name}: {value} is below the minimum {self.min}")
        if self.max is not None and value > self.max:
            raise ValueError(f"{self.name}: {value} is above the maximum {self.max}")
        return encode_int(raw, self.type)


def R(*args, **kwargs) -> Register:
    return Register(*args, **kwargs)


PLANT_REGISTERS: List[Register] = [
    # --- live status (input registers, read-only) ---
    R("plant_system_time", "input", 30000, "u32", unit="s",
      description="Device clock. Note: stores LOCAL wall-clock time, not UTC."),
    R("plant_system_timezone", "input", 30002, "s16", unit="min"),
    R("plant_ems_work_mode", "input", 30003, "u16", labels=EMS_MODES),
    R("plant_grid_sensor_status", "input", 30004, "u16", labels={0: "Not connected", 1: "Connected"}),
    R("plant_grid_sensor_active_power", "input", 30005, "s32", 1000, "kW",
      description=">0 importing from grid, <0 exporting"),
    R("plant_grid_sensor_reactive_power", "input", 30007, "s32", 1000, "kvar"),
    R("plant_on_off_grid_status", "input", 30009, "u16", labels=GRID_STATUS),
    R("plant_max_active_power", "input", 30010, "u32", 1000, "kW",
      description="Base value for active power adjustments"),
    R("plant_max_apparent_power", "input", 30012, "u32", 1000, "kVA",
      description="Base value for reactive power adjustments. The protocol says kVar, but it reads 11.5, "
                  "equal to 30010, while the real reactive limit (30043) reads 6.84 kvar: it's kVA"),
    R("plant_ess_soc", "input", 30014, "u16", 10, "%",
      description="Equals 30066 / (30064 + 30066), i.e. available / live total capacity. "
                  "Any reserve the BMS keeps lies outside this 0-100 % scale and isn't visible"),
    R("plant_phase_a_active_power", "input", 30015, "s32", 1000, "kW"),
    R("plant_phase_b_active_power", "input", 30017, "s32", 1000, "kW"),
    R("plant_phase_c_active_power", "input", 30019, "s32", 1000, "kW"),
    R("plant_phase_a_reactive_power", "input", 30021, "s32", 1000, "kvar"),
    R("plant_phase_b_reactive_power", "input", 30023, "s32", 1000, "kvar"),
    R("plant_phase_c_reactive_power", "input", 30025, "s32", 1000, "kvar"),
    R("plant_merged_alarm_1", "input", 30027, "u16", flags=PCS_ALARMS_1,
      description="PCS alarms 1 (protocol Appendix 2), merged across inverters"),
    R("plant_merged_alarm_2", "input", 30028, "u16", flags=PCS_ALARMS_2,
      description="PCS alarms 2 (protocol Appendix 3), merged across inverters"),
    R("plant_merged_alarm_3", "input", 30029, "u16", flags=ESS_ALARMS,
      description="ESS alarms (protocol Appendix 4), merged across inverters"),
    R("plant_merged_alarm_4", "input", 30030, "u16", flags=GATEWAY_ALARMS,
      description="Gateway alarms (protocol Appendix 5), merged across inverters"),
    R("plant_active_power", "input", 30031, "s32", 1000, "kW"),
    R("plant_reactive_power", "input", 30033, "s32", 1000, "kvar"),
    R("plant_photovoltaic_power", "input", 30035, "s32", 1000, "kW"),
    R("plant_ess_power", "input", 30037, "s32", 1000, "kW", description=">0 charging, <0 discharging"),
    R("plant_available_max_active_power", "input", 30039, "u32", 1000, "kW",
      description="Can feed to the AC side; running inverters only"),
    R("plant_available_min_active_power", "input", 30041, "u32", 1000, "kW",
      description="Can absorb from the AC side; running inverters only"),
    R("plant_available_max_reactive_power", "input", 30043, "u32", 1000, "kvar"),
    R("plant_available_min_reactive_power", "input", 30045, "u32", 1000, "kvar"),
    R("plant_ess_available_max_charging_power", "input", 30047, "u32", 1000, "kW"),
    R("plant_ess_available_max_discharging_power", "input", 30049, "u32", 1000, "kW"),
    R("plant_running_state", "input", 30051, "u16", labels=RUNNING_STATES),
    R("plant_grid_sensor_phase_a_active_power", "input", 30052, "s32", 1000, "kW",
      description=">0 importing from grid, <0 exporting"),
    R("plant_grid_sensor_phase_b_active_power", "input", 30054, "s32", 1000, "kW",
      description=">0 importing from grid, <0 exporting"),
    R("plant_grid_sensor_phase_c_active_power", "input", 30056, "s32", 1000, "kW",
      description=">0 importing from grid, <0 exporting"),
    R("plant_grid_sensor_phase_a_reactive_power", "input", 30058, "s32", 1000, "kvar"),
    R("plant_grid_sensor_phase_b_reactive_power", "input", 30060, "s32", 1000, "kvar"),
    R("plant_grid_sensor_phase_c_reactive_power", "input", 30062, "s32", 1000, "kvar"),
    R("plant_ess_available_max_charging_capacity", "input", 30064, "u32", 100, "kWh",
      description="Room left to fill. 30064 + 30066 is the BMS's live estimate of total capacity "
                  "(15.9-16.2 kWh on this 15.06 kWh nominal pack; dips a little when the cells are cool)"),
    R("plant_ess_available_max_discharging_capacity", "input", 30066, "u32", 100, "kWh",
      description="Energy the battery can still deliver. 30066 / (30064 + 30066) = SOC (30014)"),
    R("plant_ess_rated_charging_power", "input", 30068, "u32", 1000, "kW"),
    R("plant_ess_rated_discharging_power", "input", 30070, "u32", 1000, "kW"),
    R("plant_merged_alarm_5", "input", 30072, "u16", flags=DC_CHARGER_ALARMS,
      description="DC charger alarms (protocol Appendix 11), merged across inverters"),
    R("plant_ess_rated_energy_capacity", "input", 30083, "u32", 100, "kWh",
      description="Nominal (nameplate) pack size, e.g. BAT 10.0 + BAT 6.0 = 15.06 kWh; ~14.60 usable per "
                  "datasheet. Fixed; for the live estimate use 30064 + 30066"),
    R("plant_ess_charge_cut_off_soc", "input", 30085, "u16", 10, "%"),
    R("plant_ess_discharge_cut_off_soc", "input", 30086, "u16", 10, "%"),
    R("plant_ess_soh", "input", 30087, "u16", 10, "%"),
    R("plant_accumulated_pv_energy", "input", 30088, "u64", 100, "kWh"),
    R("plant_daily_consumed_energy", "input", 30092, "u32", 100, "kWh"),
    R("plant_accumulated_load_consumption", "input", 30094, "u64", 100, "kWh"),
    # Smart loads 1-24 (protocol v2.7): read-only. Which slot is which LoadHub circuit
    # (or Shelly device) is not documented; map them by switching known appliances.
    *[R(f"plant_smart_load_{n}_total_consumption", "input", 30098 + 2 * (n - 1), "u32", 100, "kWh")
      for n in range(1, 25)],
    *[R(f"plant_smart_load_{n}_power", "input", 30146 + 2 * (n - 1), "s32", 1000, "kW")
      for n in range(1, 25)],
    R("plant_third_party_inverter_power", "input", 30194, "s32", 1000, "kW"),
    R("plant_accumulated_third_party_inverter_energy", "input", 30196, "u64", 100, "kWh"),
    R("plant_accumulated_battery_charge_energy", "input", 30200, "u64", 100, "kWh"),
    R("plant_accumulated_battery_discharge_energy", "input", 30204, "u64", 100, "kWh"),
    R("plant_accumulated_evdc_charge_energy", "input", 30208, "u64", 100, "kWh"),
    R("plant_accumulated_evdc_discharge_energy", "input", 30212, "u64", 100, "kWh"),
    R("plant_accumulated_grid_import_energy", "input", 30216, "u64", 100, "kWh"),
    R("plant_accumulated_grid_export_energy", "input", 30220, "u64", 100, "kWh"),
    R("plant_accumulated_generator_energy", "input", 30224, "u64", 100, "kWh"),
    # The "new statistics interface" (30228-30268): counters reset to 0 when a firmware
    # update added them, so they don't include history from before that.
    R("plant_si_common_load_consumption", "input", 30228, "u64", 100, "kWh",
      description="Excludes EV AC and DC charging"),
    R("plant_si_evac_charge_energy", "input", 30232, "u64", 100, "kWh"),
    R("plant_si_pv_energy", "input", 30236, "u64", 100, "kWh",
      description="Own PV only, excluding third-party inverters"),
    R("plant_si_third_party_inverter_energy", "input", 30240, "u64", 100, "kWh"),
    R("plant_si_battery_charge_energy", "input", 30244, "u64", 100, "kWh"),
    R("plant_si_battery_discharge_energy", "input", 30248, "u64", 100, "kWh"),
    R("plant_si_evdc_charge_energy", "input", 30252, "u64", 100, "kWh"),
    R("plant_si_evdc_discharge_energy", "input", 30256, "u64", 100, "kWh"),
    R("plant_si_grid_import_energy", "input", 30260, "u64", 100, "kWh"),
    R("plant_si_grid_export_energy", "input", 30264, "u64", 100, "kWh"),
    R("plant_si_generator_energy", "input", 30268, "u64", 100, "kWh"),
    R("plant_pv_total_daily_generation", "input", 30272, "u32", 100, "kWh"),
    R("plant_pv_total_previous_day_generation", "input", 30274, "u32", 100, "kWh"),
    R("plant_grid_code_rated_frequency", "input", 30276, "u16", 100, "Hz"),
    R("plant_grid_code_rated_voltage", "input", 30277, "u32", 100, "V"),
    # 30279 (current control command value, Japan) and 30281 (merged alarm 7) are in V2.8+
    # but this inverter rejects them when read alone (inside a block they read 0xFFFF).
    R("plant_merged_alarm_6", "input", 30280, "u16", flags=PLANT_ALARMS_1,
      description="Plant alarms 1 (protocol Appendix 12)"),
    R("plant_general_load_power", "input", 30282, "s32", 1000, "kW"),
    R("plant_total_load_power", "input", 30284, "s32", 1000, "kW"),
    R("plant_ess_average_cell_temperature", "input", 30286, "s16", 10, "°C"),

    # --- settings (holding registers) ---
    R("plant_remote_ems_enable", "holding", 40029, "u16", labels=ENABLED),
    R("plant_remote_ems_control_mode", "holding", 40031, "u16", labels=REMOTE_EMS_MODES,
      writable=True, allowed=frozenset({0, 2}), cli_option="--remote-ems-mode",
      description="What the plant does under Remote EMS. Only 0 and 2 are allowed for now; "
                  "forced charge/discharge modes (3-6) come later."),
    R("plant_ess_max_charging_limit", "holding", 40032, "u32", 1000, "kW"),
    R("plant_ess_max_discharging_limit", "holding", 40034, "u32", 1000, "kW"),
    R("plant_pv_max_power_limit", "holding", 40036, "u32", 1000, "kW"),
    # Grid export/import limits are part of the utility interconnection setup: read-only here.
    R("plant_grid_point_maximum_export_limitation", "holding", 40038, "u32", 1000, "kW"),
    R("plant_grid_maximum_import_limitation", "holding", 40040, "u32", 1000, "kW"),
    R("plant_pcs_maximum_export_limitation", "holding", 40042, "u32", 1000, "kW"),
    R("plant_pcs_maximum_import_limitation", "holding", 40044, "u32", 1000, "kW"),
    R("plant_backup_soc", "holding", 40046, "u16", 10, "%",
      writable=True, min=0, max=100, cli_option="--backup-soc",
      description="Battery reserve kept for outages"),
    R("plant_charge_cut_off_soc", "holding", 40047, "u16", 10, "%"),
    R("plant_discharge_cut_off_soc", "holding", 40048, "u16", 10, "%"),
]

OUTPUT_TYPES = {0: "L/N", 1: "L1/L2/L3", 2: "L1/L2/L3/N", 3: "L1/L2/N"}


def I(*args, **kwargs) -> Register:
    return Register(*args, device="inverter", **kwargs)


def _pv_strings(n: int) -> List[Register]:
    return [reg for i in range(1, n + 1) for reg in (
        I(f"inverter_pv{i}_voltage", "input", 31025 + 2 * i, "s16", 10, "V"),
        I(f"inverter_pv{i}_current", "input", 31026 + 2 * i, "s16", 100, "A"))]


# The hybrid inverter (Sigenergy calls it the Energy Controller) on its own unit ID (1 here),
# protocol V2.9 table 5-3. Every entry read without error, one register at a time, on
# 2026-10-08. Left out on purpose:
#  - PV strings 5-36 (31042-31105) and the DC charger (31500-31525): rejected, not fitted here
#    (31025 says 4 strings);
#  - 30622/30623 cluster cell voltages: rejected by this firmware;
#  - B-C/C-A line voltage, phase C voltage/current (31007, 31009, 31015, 31021): read all-ones,
#    invalid for output type L1/L2/N (split phase);
#  - 30613-30619 power-adjustment feedback: all-ones until remote setpoints are in use.
INVERTER_REGISTERS: List[Register] = [
    I("inverter_model_type", "input", 30500, "string", count=15),
    I("inverter_serial_number", "input", 30515, "string", count=10),
    I("inverter_firmware_version", "input", 30525, "string", count=15),
    I("inverter_rated_active_power", "input", 30540, "u32", 1000, "kW"),
    I("inverter_max_apparent_power", "input", 30542, "u32", 1000, "kVA"),
    I("inverter_max_active_power", "input", 30544, "u32", 1000, "kW"),
    I("inverter_max_absorption_power", "input", 30546, "u32", 1000, "kW"),
    I("inverter_rated_battery_capacity", "input", 30548, "u32", 100, "kWh"),
    I("inverter_ess_rated_charge_power", "input", 30550, "u32", 1000, "kW"),
    I("inverter_ess_rated_discharge_power", "input", 30552, "u32", 1000, "kW"),
    I("inverter_ess_daily_charge_energy", "input", 30566, "u32", 100, "kWh"),
    I("inverter_ess_accumulated_charge_energy", "input", 30568, "u64", 100, "kWh"),
    I("inverter_ess_daily_discharge_energy", "input", 30572, "u32", 100, "kWh"),
    I("inverter_ess_accumulated_discharge_energy", "input", 30574, "u64", 100, "kWh"),
    I("inverter_running_state", "input", 30578, "u16", labels=RUNNING_STATES),
    I("inverter_max_active_power_adjustment", "input", 30579, "s32", 1000, "kW"),
    I("inverter_min_active_power_adjustment", "input", 30581, "s32", 1000, "kW"),
    I("inverter_max_reactive_power_fed", "input", 30583, "u32", 1000, "kvar"),
    I("inverter_max_reactive_power_absorbed", "input", 30585, "u32", 1000, "kvar"),
    I("inverter_active_power", "input", 30587, "s32", 1000, "kW"),
    I("inverter_reactive_power", "input", 30589, "s32", 1000, "kvar"),
    I("inverter_ess_max_charge_power", "input", 30591, "u32", 1000, "kW",
      description="What the battery would accept right now"),
    I("inverter_ess_max_discharge_power", "input", 30593, "u32", 1000, "kW",
      description="What the battery modules could deliver right now (above the 11.5 kW AC limit)"),
    I("inverter_ess_available_charge_energy", "input", 30595, "u32", 100, "kWh"),
    I("inverter_ess_available_discharge_energy", "input", 30597, "u32", 100, "kWh"),
    I("inverter_ess_power", "input", 30599, "s32", 1000, "kW", description=">0 charging, <0 discharging"),
    I("inverter_ess_soc", "input", 30601, "u16", 10, "%"),
    I("inverter_ess_soh", "input", 30602, "u16", 10, "%"),
    I("inverter_ess_average_cell_temperature", "input", 30603, "s16", 10, "°C"),
    I("inverter_ess_average_cell_voltage", "input", 30604, "u16", 1000, "V"),
    I("inverter_alarm_1", "input", 30605, "u16", flags=PCS_ALARMS_1, description="PCS alarms 1 (Appendix 2)"),
    I("inverter_alarm_2", "input", 30606, "u16", flags=PCS_ALARMS_2, description="PCS alarms 2 (Appendix 3)"),
    I("inverter_alarm_3", "input", 30607, "u16", flags=ESS_ALARMS, description="ESS alarms (Appendix 4)"),
    I("inverter_alarm_4", "input", 30608, "u16", flags=GATEWAY_ALARMS, description="Gateway alarms (Appendix 5)"),
    I("inverter_alarm_5", "input", 30609, "u16", flags=DC_CHARGER_ALARMS,
      description="DC charger alarms (Appendix 11)"),
    I("inverter_ess_max_cluster_temperature", "input", 30620, "s16", 10, "°C"),
    I("inverter_ess_min_cluster_temperature", "input", 30621, "s16", 10, "°C"),
    I("inverter_rated_grid_voltage", "input", 31000, "u16", 10, "V"),
    I("inverter_rated_grid_frequency", "input", 31001, "u16", 100, "Hz"),
    I("inverter_grid_frequency", "input", 31002, "u16", 100, "Hz"),
    I("inverter_pcs_internal_temperature", "input", 31003, "s16", 10, "°C"),
    I("inverter_output_type", "input", 31004, "u16", labels=OUTPUT_TYPES),
    I("inverter_ab_line_voltage", "input", 31005, "u32", 100, "V", description="Leg to leg (240 V nominal)"),
    I("inverter_phase_a_voltage", "input", 31011, "u32", 100, "V"),
    I("inverter_phase_b_voltage", "input", 31013, "u32", 100, "V"),
    I("inverter_phase_a_current", "input", 31017, "s32", 100, "A"),
    I("inverter_phase_b_current", "input", 31019, "s32", 100, "A"),
    I("inverter_power_factor", "input", 31023, "s16", 1000),
    I("inverter_pack_count", "input", 31024, "u16", description="Battery packs (e.g. BAT 10.0 + BAT 6.0 = 2)"),
    I("inverter_pv_string_count", "input", 31025, "u16"),
    I("inverter_mppt_count", "input", 31026, "u16"),
    *_pv_strings(4),
    I("inverter_pv_power", "input", 31035, "s32", 1000, "kW"),
    I("inverter_insulation_resistance", "input", 31037, "u16", 1000, "MΩ"),
    I("inverter_startup_time", "input", 31038, "u32", unit="s",
      description="Last start, as local wall-clock time stored like an epoch (same as 30000)"),
    I("inverter_shutdown_time", "input", 31040, "u32", unit="s"),
    I("inverter_pv_daily_generation", "input", 31509, "u32", 100, "kWh"),
    I("inverter_pv_total_generation", "input", 31511, "u32", 100, "kWh"),
]

REGISTERS_BY_DEVICE: Dict[str, List[Register]] = {"plant": PLANT_REGISTERS, "inverter": INVERTER_REGISTERS}
ALL_REGISTERS: List[Register] = PLANT_REGISTERS + INVERTER_REGISTERS
# Addresses are unique across device types (each type has its own ranges, checked against
# every table of protocol V2.9), so one address -> register lookup serves both.
BY_NAME: Dict[str, Register] = {r.name: r for r in ALL_REGISTERS}
BY_ADDRESS: Dict[Tuple[str, int], Register] = {(r.table, r.address): r for r in ALL_REGISTERS}
WRITABLE: List[Register] = [r for r in PLANT_REGISTERS if r.writable]
