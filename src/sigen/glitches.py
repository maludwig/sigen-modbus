"""Polls the device got wrong: its summary registers caught mid-update, for a single reading.

Two kinds so far (scratch/check_glitches.py and pv_zero_explore.py look for more):
- the plant's total load power (30284) exactly 0 while Running, often with other powers wrong too (2026-10-15
  07:40:51: the battery discharging 0.6 kW and the grid exporting 1.0 kW with no PV, where the readings 15 s either
  side had the battery idle and ~0.55 kW coming from the grid). No house draws nothing.
- the PV power (the inverter's 31035, or the plant's 30035) exactly 0 while the inverter's strings carry current, with
  the load, the battery or the phase currents often wrong in the same reading (2026-10-08 17:43:30: 1.61 → 0 →
  1.51 kW while the strings made a steady 1.9 kW and the DC side's output still matched them). Above 0.5 kW it's always
  one reading, back on the next; below, at dusk, the PV can really be 0 while the strings' V × I still says a few
  hundred watts, so between 0.1 and 0.5 kW it's only a suspect.

judge() decides for one poll, from its own readings only (never its neighbours: a step in the load, a cloud or a
sunset must never look like a glitch). `sigen poll` stores the verdict in readings.glitch (migration 0007), on every
reading of the poll, and takes another poll a second later; whatever reads readings leaves flagged polls out with
`glitch IS NULL`. A sure glitch is always flagged; a suspect only when the retry replaces it (the last attempt is kept:
at dusk, PV 0 is the truth).
"""

from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

from sigen.registers import BY_NAME

TOTAL_LOAD_ZERO, PV_ZERO, PV_ZERO_SUSPECT = "total_load_zero", "pv_zero", "pv_zero_suspect"
RULES = (TOTAL_LOAD_ZERO, PV_ZERO, PV_ZERO_SUSPECT)
PV_ZERO_KW = 0.5  # the strings over this, PV 0: sure
PV_SUSPECT_KW = 0.1  # over this: a suspect
RUNNING = 1  # registers.RUNNING_STATES
STRINGS = [n for n in range(1, 37) if f"inverter_pv{n}_voltage" in BY_NAME]
SHOWN = [("battery", "plant_ess_power"), ("grid", "plant_grid_sensor_active_power"), ("PV", "plant_photovoltaic_power")]
_BY_ADDRESS = {r.address: r for r in BY_NAME.values() if r.type != "string"}


@dataclass(frozen=True)
class Verdict:
    rule: str  # one of RULES: what goes in readings.glitch
    why: str  # in plain words, with the numbers
    sure: bool  # False for a suspect: flagged only if a retry replaces it


def _values(values: Iterable[Tuple[int, int]]) -> Dict[str, float]:
    """[(address, value before the gain)] -> {register name: value after the gain}."""
    out = {}
    for address, raw in values:
        reg = _BY_ADDRESS.get(address)
        if reg is not None:
            out[reg.name] = raw / reg.gain
    return out


def strings_kw(inverter: Dict[str, float]) -> Optional[float]:
    """The inverter's strings' voltage × current, added up (kW); None without them."""
    if not any(f"inverter_pv{n}_voltage" in inverter for n in STRINGS):
        return None
    return (
        sum(
            inverter.get(f"inverter_pv{n}_voltage", 0.0) * inverter.get(f"inverter_pv{n}_current", 0.0) for n in STRINGS
        )
        / 1000
    )


def judge(readings: Dict[str, Iterable[Tuple[int, int]]]) -> Optional[Verdict]:
    """The verdict on one poll: `readings` maps "plant" / "inverter" to that unit's stored values ([(address, value
    before the gain)], as sigen.poll stores them; a unit that failed is left out). None if it looks right."""
    plant = _values(readings.get("plant", ()))
    inverter = _values(readings.get("inverter", ()))
    if plant.get("plant_total_load_power") == 0 and plant.get("plant_running_state") == RUNNING:
        shown = ", ".join(f"{label} {plant[name]:.2f} kW" for label, name in SHOWN if name in plant)
        return Verdict(TOTAL_LOAD_ZERO, "total load 0 while Running" + (f" ({shown})" if shown else ""), True)
    made = strings_kw(inverter)
    if made is not None and inverter.get("inverter_running_state") == RUNNING and made > PV_SUSPECT_KW:
        zero = [
            who
            for who, name, values in (
                ("inverter", "inverter_pv_power", inverter),
                ("plant", "plant_photovoltaic_power", plant),
            )
            if values.get(name) == 0
        ]
        if zero:
            sure = made > PV_ZERO_KW
            why = f"PV power 0 ({' and '.join(zero)}) while the strings make {made:.2f} kW"
            return Verdict(PV_ZERO if sure else PV_ZERO_SUSPECT, why + ("" if sure else " (perhaps dusk)"), sure)
    return None


def describe(rule: str) -> str:
    return {
        TOTAL_LOAD_ZERO: "total load 0 while Running",
        PV_ZERO: f"PV power 0 while the strings make over {PV_ZERO_KW:g} kW",
        PV_ZERO_SUSPECT: f"PV power 0 while the strings make over {PV_SUSPECT_KW:g} kW (replaced by a retry)",
    }[rule]
