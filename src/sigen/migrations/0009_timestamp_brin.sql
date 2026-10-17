-- A BRIN index on every TIMESTAMPTZ column that didn't already have a usable leading index of its own, so a scan can
-- be bounded by time instead of by id. This follows the move away from assuming a reading's id order matches its
-- started_at order (sigen sync backfills a gap now, giving old-timestamped rows the newest ids): the readers now order
-- and bound by the time column, and these indexes keep those scans cheap.
--
-- Why BRIN, not btree: every table here is append-mostly and written in roughly time order, so a row's time correlates
-- with its physical position -- exactly what BRIN exploits. A BRIN index is a few kilobytes (one summary per 128-page
-- block) against a btree's one entry per row, and almost free to maintain. The trade-off is selectivity, not
-- correctness: a backfill appends old-timestamped rows at the heap's end, which widens those blocks' summarised ranges,
-- so a query overlapping a backfilled stretch scans a little more of the heap. That is fine for these columns and
-- these volumes; a btree is reserved for the hot time columns that already have one (readings.started_at, the raw_*
-- tables' started_at, radar_frames).
--
-- Two groups:
--   1. The time column is a NON-LEADING key of a composite index, so a scan filtering it alone can't use that index
--      (open_meteo_weather's sync export filters valid_at without resolution_minutes; battery_heater's filters
--      polled_at without battery). The composite indexes stay -- they serve their leading-column queries.
--   2. The time column had no index at all. Some (readings.ended_at, open_meteo_weather.fetched_at) aren't range-
--      queried today; the daily_energy and schema_migrations columns are on tiny tables where any index is cosmetic.
--      They're indexed here for completeness, so "every timestamp column is covered" holds without exception; a BRIN
--      makes that cheap enough to not think about.

-- 1. Timestamp columns that are only a non-leading key of a composite index.
CREATE INDEX open_meteo_weather_valid_at_brin ON open_meteo_weather USING brin (valid_at);
CREATE INDEX battery_heater_polled_at_brin    ON battery_heater      USING brin (polled_at);
CREATE INDEX alert_log_alerted_at_brin        ON alert_log           USING brin (alerted_at);

-- 2. Timestamp columns that had no index at all.
CREATE INDEX readings_ended_at_brin               ON readings           USING brin (ended_at);
CREATE INDEX open_meteo_weather_fetched_at_brin   ON open_meteo_weather USING brin (fetched_at);
CREATE INDEX daily_energy_first_reading_brin      ON daily_energy       USING brin (first_reading);
CREATE INDEX daily_energy_last_reading_brin       ON daily_energy       USING brin (last_reading);
CREATE INDEX daily_energy_computed_at_brin        ON daily_energy       USING brin (computed_at);
CREATE INDEX schema_migrations_applied_at_brin    ON schema_migrations  USING brin (applied_at);
