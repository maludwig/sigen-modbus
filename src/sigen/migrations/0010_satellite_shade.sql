-- NOAA GOES-West cloud shade over the site (config "site"), per 10-minute full-disk scan (sigen.goes_shade, computed
-- from the cubes `sigen satellite` builds). One row per scan. The dashboard's string-power strips read this table (as
-- precipitation reads radar_frames), and the all-time page aggregates it per day. Written by `sigen satellite
-- --record-to-db`. Nothing here touches the readings tables.
--
-- Only the values the strips use are kept. Per scan, where a cloud at the measured cloud top, or at 0 / 2 / 5 / 10 km,
-- would shade the house:
--   trans_<h>   the estimated share of sunlight through the cloud there, 0 (no sun) to 1 (clear), from its optical depth
--   prob_<h>    the cloud probability there, 0 (clear) to 1 (overcast), as the strip uses it
-- Both are dimensionless fractions (no unit suffix). sun_elevation_deg (degrees above the horizon) drives the strips'
-- low-sun mask. NULL where there was nothing to measure (no product, or the sun too low for that shade point).
-- The fuller shade computation -- the sun's azimuth, and the raw cloud optical depth and top height -- isn't stored:
-- the strips don't use it, and it can be rebuilt by re-downloading the GOES cubes. An observation, never revised, so
-- one good row per scan. A failed run is a row with the error (what went wrong) and scan_at NULL.
CREATE TABLE satellite_shade (
    id                BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fetched_at        TIMESTAMPTZ NOT NULL,     -- this computer's clock
    scan_at           TIMESTAMPTZ,              -- the scan's sample time (scan start + SAMPLE_DELAY), UTC
    sun_elevation_deg REAL,                     -- degrees above the horizon at scan_at
    trans_top         REAL,                     -- sun through the cloud at the measured cloud top
    trans_0km         REAL,                     -- ... and where a cloud at 0 / 2 / 5 / 10 km would shade the house
    trans_2km         REAL,
    trans_5km         REAL,
    trans_10km        REAL,
    prob_0km          REAL,                     -- cloud probability over the house and at 2 / 5 / 10 km
    prob_2km          REAL,
    prob_5km          REAL,
    prob_10km         REAL,
    error             TEXT,
    CHECK (error IS NOT NULL OR (scan_at IS NOT NULL AND sun_elevation_deg IS NOT NULL))
);
-- scan_at is a btree (not BRIN): it enforces one good row per scan, which BRIN can't, and it serves the strips'
-- range scans. fetched_at is append-mostly and barely queried, so BRIN (tiny), as migration 0009 does elsewhere.
CREATE UNIQUE INDEX satellite_shade_one_good_row ON satellite_shade (scan_at) WHERE error IS NULL;
CREATE INDEX satellite_shade_fetched_at_brin ON satellite_shade USING brin (fetched_at);
