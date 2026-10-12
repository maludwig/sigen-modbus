-- A reading is one device's snapshot: the plant (unit 247) or an inverter (its own unit ID,
-- e.g. 1). One poll takes one reading per unit, and those readings share a poll_id.
-- Every reading before this migration was a plant reading, and its own poll.
ALTER TABLE readings ADD COLUMN unit_id SMALLINT NOT NULL DEFAULT 247;
ALTER TABLE readings ALTER COLUMN unit_id DROP DEFAULT;

CREATE SEQUENCE poll_ids;
ALTER TABLE readings ADD COLUMN poll_id BIGINT;
UPDATE readings SET poll_id = id;
ALTER TABLE readings ALTER COLUMN poll_id SET NOT NULL;
SELECT setval('poll_ids', greatest((SELECT max(id) FROM readings), 1));

CREATE INDEX readings_unit_started_at ON readings (unit_id, started_at);
CREATE INDEX readings_poll_id ON readings (poll_id);

-- Which kind of device a register belongs to. Address ranges never overlap between device
-- types, so address stays the key, and every inverter shares the inverter definitions.
ALTER TABLE register_meta ADD COLUMN device TEXT NOT NULL DEFAULT 'plant'
    CHECK (device IN ('plant', 'inverter'));
ALTER TABLE register_meta ALTER COLUMN device DROP DEFAULT;
