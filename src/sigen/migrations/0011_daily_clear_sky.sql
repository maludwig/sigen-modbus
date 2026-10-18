-- daily_energy.clear_sky_kwh: the whole array's clear-sky model energy for the day (kWh) -- the same pvlib model the
-- dashboard draws per string (sigen.charts.clear_sky_strings), summed over the local day. NULL when there's no
-- site/array config (or pvlib) to model it. Written by `sigen db daily` / `sigen poll` alongside the rest of the day;
-- the all-time page divides the day's measured solar by it to show how close the array came to a cloudless day.
ALTER TABLE daily_energy ADD COLUMN clear_sky_kwh DOUBLE PRECISION;
