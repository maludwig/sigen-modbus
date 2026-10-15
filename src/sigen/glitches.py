"""Plant readings the device got wrong: its summary registers caught mid-refresh, for a single reading.

Since 2026-10-13 the plant now and then reports the house's total load power (30284) as exactly 0 while Running, with
other powers wrong too (2026-10-15 07:40:51: the battery discharging 0.6 kW and the grid exporting 1.0 kW with no PV,
where the readings 15 s either side had the battery idle and ~0.55 kW coming from the grid). No house draws nothing, so
such a reading is a glitch. It's stored like
any other (it's what the device said); `sigen poll` takes another reading a second later (sigen.poll), and whatever
reads plant values leaves glitches out, by this one rule: plant_glitch() for a reading in hand, the SQL below for stored
ones. Nothing in the database marks them, so a change to the rule here applies to the history too.

Only plant readings glitch: the inverter's reading in the same poll was right (2026-10-15).
"""
from typing import Iterable, Optional, Tuple

from sigen.registers import BY_NAME

TOTAL_LOAD = BY_NAME["plant_total_load_power"]          # 30284, s32
RUNNING_STATE = BY_NAME["plant_running_state"]          # 30051
RUNNING = 1                                             # registers.RUNNING_STATES
TOTAL_LOAD_ZERO = "total load 0 while Running"


def plant_glitch(values: Iterable[Tuple[int, int]]) -> Optional[str]:
    """Why a plant reading's values ([(address, value before the gain)], as sigen.poll stores them) are a glitch, or
    None if they aren't."""
    v = dict(values)
    if v.get(TOTAL_LOAD.address) == 0 and v.get(RUNNING_STATE.address) == RUNNING:
        return TOTAL_LOAD_ZERO
    return None


def glitch_sql(alias: str) -> str:
    """The same rule over a raw_plant_register_values row aliased `alias`: true for a glitch, NULL where a column it
    reads is NULL (so it's never true by accident)."""
    return f"({alias}.{TOTAL_LOAD.name} = 0 AND {alias}.{RUNNING_STATE.name} = {RUNNING})"


def not_glitch_sql(alias: str) -> str:
    """True for a raw_plant_register_values row aliased `alias` that isn't a glitch (NULLs included)."""
    return f"NOT coalesce({glitch_sql(alias)}, false)"


def good_reading_sql(reading_id: str) -> str:
    """For a query over readings: the reading whose id is `reading_id` (a column, e.g. "readings.id") isn't a glitched
    plant reading. True for other units' readings and for failed ones (they have no plant row)."""
    return (f"NOT EXISTS (SELECT 1 FROM raw_plant_register_values g WHERE g.reading_id = {reading_id} "
            f"AND {glitch_sql('g')})")
