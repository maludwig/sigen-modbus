-- readings.glitch: why a poll's readings aren't to be trusted (sigen.glitches), NULL for a good one. Every reading of a
-- poll gets the same value, so whatever reads readings leaves the whole poll out with `glitch IS NULL`, as it leaves
-- out failed readings with `error IS NULL`. `sigen poll` writes it as it stores a poll:
--   total_load_zero   the plant's total load exactly 0 while Running
--   pv_zero           the PV power (the plant's or the inverter's) exactly 0 while the inverter is Running and its
--                     strings' voltage × current add up to over 0.5 kW
--   pv_zero_suspect   the same with the strings over 0.1 kW (at dusk the PV can really be 0 while they read a few
--                     hundred watts): poll took another reading a second later, which replaces this one
-- A flag is never cleared, so an interval keeps at most one unflagged poll even if a rule later changes.
--
-- The history before this column: the two sure rules, as they stood on 2026-10-15 (no poll had been retried yet, so
-- no pv_zero_suspect). The strings' power from the raw values: volts are stored × 10 and amps × 100, so a raw V × I
-- of 500,000 is 0.5 kW.
ALTER TABLE readings ADD COLUMN glitch TEXT;

UPDATE readings SET glitch = 'total_load_zero'
WHERE poll_id IN (SELECT r.poll_id FROM readings r JOIN raw_plant_register_values p ON p.reading_id = r.id
                  WHERE p.plant_total_load_power = 0 AND p.plant_running_state = 1);

UPDATE readings SET glitch = 'pv_zero'
WHERE glitch IS NULL AND poll_id IN (
    SELECT ri.poll_id
    FROM readings ri
    JOIN raw_inverter_register_values i ON i.reading_id = ri.id
    LEFT JOIN readings rp ON rp.poll_id = ri.poll_id AND rp.id <> ri.id
    LEFT JOIN raw_plant_register_values p ON p.reading_id = rp.id
    WHERE i.inverter_running_state = 1
      AND (i.inverter_pv_power = 0 OR p.plant_photovoltaic_power = 0)
      AND coalesce(i.inverter_pv1_voltage::BIGINT * i.inverter_pv1_current, 0)
          + coalesce(i.inverter_pv2_voltage::BIGINT * i.inverter_pv2_current, 0)
          + coalesce(i.inverter_pv3_voltage::BIGINT * i.inverter_pv3_current, 0)
          + coalesce(i.inverter_pv4_voltage::BIGINT * i.inverter_pv4_current, 0) > 500000);
