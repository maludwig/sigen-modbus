-- One row per finished local day (in the config's site timezone): the day's energy, kWh, as the dashboard's daily page
-- shows it, so a month's or a year's is a single SUM. Written by `sigen db daily` and by `sigen poll` (sigen.daily);
-- recomputing a day replaces its row. Nothing here touches the other tables.
--
-- The totals: each of the plant's lifetime counters (solar, the house, the battery in and out, the grid in and out)
-- at the first reading at or after the next local midnight, minus its value at the day's first reading, all by this
-- server's clock (not the device's daily counters: it resets those by its own clock). Consecutive days share their
-- boundary reading, so days add up exactly; a gap in polling moves energy to the day before it.
-- The flows split them up, from the power readings: solar serves the house first, then the battery, then the grid;
-- the battery serves the house, then the grid; the grid serves the house, then the battery. Every flow is stored,
-- however small (the daily page leaves tiny ones out of its drawing). They add up to a little less than the totals
-- (the inverter's losses).
CREATE TABLE daily_energy (
    day                    DATE             PRIMARY KEY,    -- the local date
    timezone               TEXT             NOT NULL,       -- the IANA name the day was cut in
    solar_kwh              DOUBLE PRECISION,                -- NULL: that counter wasn't read that day
    load_kwh               DOUBLE PRECISION,
    battery_charged_kwh    DOUBLE PRECISION,
    battery_discharged_kwh DOUBLE PRECISION,
    grid_imported_kwh      DOUBLE PRECISION,
    grid_exported_kwh      DOUBLE PRECISION,
    solar_to_load_kwh      DOUBLE PRECISION NOT NULL,
    solar_to_battery_kwh   DOUBLE PRECISION NOT NULL,
    solar_to_grid_kwh      DOUBLE PRECISION NOT NULL,
    battery_to_load_kwh    DOUBLE PRECISION NOT NULL,
    battery_to_grid_kwh    DOUBLE PRECISION NOT NULL,
    grid_to_load_kwh       DOUBLE PRECISION NOT NULL,
    grid_to_battery_kwh    DOUBLE PRECISION NOT NULL,
    first_reading          TIMESTAMPTZ      NOT NULL,       -- the day's first and last plant readings
    last_reading           TIMESTAMPTZ      NOT NULL,
    covered_hours          DOUBLE PRECISION NOT NULL,       -- the time the power readings cover, gaps over 5 minutes
                                                            -- left out: about 24 for a day polled throughout
    method                 SMALLINT         NOT NULL,       -- how the day was computed (sigen.daily.METHOD)
    computed_at            TIMESTAMPTZ      NOT NULL DEFAULT now()
);
