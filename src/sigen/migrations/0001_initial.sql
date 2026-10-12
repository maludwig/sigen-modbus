-- One row per poll. A failed poll still gets a row, with the error and no values,
-- so gaps in the data are explained.
CREATE TABLE readings (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ip_addr    INET        NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,   -- this computer's clock, microseconds
    ended_at   TIMESTAMPTZ NOT NULL,
    version    TEXT        NOT NULL,   -- sigen-modbus version that took the reading
    error      TEXT                    -- NULL = every register was read
);
CREATE INDEX readings_started_at ON readings (started_at);

-- What each address means. `sigen db migrate` keeps it in sync with sigen/registers.py.
-- Changing the TYPE of an address that already has values needs a migration that
-- converts those values and updates this table in the same step.
CREATE TABLE register_meta (
    address INTEGER          PRIMARY KEY,
    name    TEXT             NOT NULL UNIQUE,
    type    TEXT             NOT NULL CHECK (type IN ('u16', 's16', 'u32', 's32', 'u64', 's64')),
    gain    DOUBLE PRECISION NOT NULL CHECK (gain > 0),
    unit    TEXT
);

-- value: the register's words decoded as its type, BEFORE the gain
-- (so -2000 for -2.0 kW; engineering value = value / gain).
-- u64 is stored as the signed 64-bit number with the same bits, so the
-- "not set" marker 0xFFFFFFFFFFFFFFFF is -1.
CREATE TABLE register_values (
    reading_id BIGINT  NOT NULL REFERENCES readings (id) ON DELETE CASCADE,
    address    INTEGER NOT NULL REFERENCES register_meta (address),
    value      BIGINT  NOT NULL,
    PRIMARY KEY (reading_id, address)
);
CREATE INDEX register_values_by_address ON register_values (address, reading_id);
