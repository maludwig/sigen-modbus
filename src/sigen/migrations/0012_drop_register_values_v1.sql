-- Drops what migration 0004 kept for the switch-over, now that every reader uses the wide tables:
--   register_values     the view giving the wide tables the old (reading_id, address, value) shape
--   register_values_v1  the old one-row-per-value table; 0004 copied and counted every value into the wide tables
-- Without the view, adding a register later is one ALTER TABLE plus the <device>_register_values view.
DROP VIEW register_values;
DROP TABLE register_values_v1;
