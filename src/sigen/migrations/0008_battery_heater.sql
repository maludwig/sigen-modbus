-- One row per cloud query of a battery pack's self-heating status (sigen.cloud, via the mySigen cloud API). The
-- LoadHub has no Modbus and the packs' heater isn't in any Modbus register, so this is the only way to see it. Written
-- by `sigen poll`'s cloud heater job when the config has a "cloud" section: every poll is stored, with no dedup (a
-- heater turns on and off a few times on a cold morning and never otherwise, so the volume is tiny and keeping every
-- poll avoids edge cases). Nothing here touches the other tables.
--
-- `battery` is the pack's number, the app's Battery(1) / Battery(2): the Nth entry of the config's cloud.batteries
-- (so the order there is what 1 and 2 mean -- list them Battery(1) first). A number rather than the serial keeps the
-- table small and readable; the serial for a number is in the config.
--
-- Cadence (sigen poll): every 10 minutes at rest; every minute while PV is over ~0.2 kW and the coolest cluster is
-- under ~15 °C (the heater might run), and every minute while any pack's heater is on. A grumpy poll (the cloud was
-- unreachable, the token expired, an API error) stores error and leaves heater_on NULL, so a row never guesses.
CREATE TABLE battery_heater (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    polled_at   TIMESTAMPTZ NOT NULL,     -- when the cloud was queried (this computer's clock)
    battery     SMALLINT    NOT NULL CHECK (battery >= 1),   -- 1 or 2: the config's cloud.batteries[battery - 1]
    heater_on   BOOLEAN,                  -- the pack's "Heating status"; NULL when error (unknown)
    error       TEXT,                     -- NULL on success; else what went wrong (unreachable, HTTP/API error)
    CHECK ((heater_on IS NULL) <> (error IS NULL))   -- exactly one: a reading, or a reason there isn't one
);

-- The latest state per pack, and time-range scans.
CREATE INDEX battery_heater_battery_polled_at ON battery_heater (battery, polled_at);
