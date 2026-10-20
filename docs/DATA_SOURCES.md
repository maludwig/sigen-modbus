# Data sources

Besides the SigenStor itself, `sigen` fetches weather and satellite data for the config's `site`.
All of it is free, and none of it needs an account or a key.

| Source | Used by | What | Licence |
|---|---|---|---|
| [Open-Meteo](https://open-meteo.com) forecast API | `poll --weather`, `sigen weather`, the dashboard | A weather model at your site: sunlight, cloud, temperature, wind, precipitation (23 variables) | [CC BY 4.0](https://open-meteo.com/en/license) |
| [MSC GeoMet](https://eccc-msc.github.io/open-data/msc-geomet/readme_en/) radar (Environment and Climate Change Canada) | `poll --radar`, `sigen radar` | Rain and snow rate at your site, from the North American radar composite | [ECCC Data Server End-use Licence](https://eccc-msc.github.io/open-data/licence/readme_en/) |
| [NOAA GOES-18](https://registry.opendata.aws/noaa-goes/) (GOES-West) on AWS | `poll --satellite`, `sigen satellite` | Cloud mask, cloud optical depth and cloud-top height, every 10 minutes | NOAA open data: use as desired |

If you publish charts or data made from these, credit the sources:
- **Open-Meteo**: CC BY 4.0 requires attribution, e.g. "Weather data by Open-Meteo.com" with a
  link to it.
- **ECCC**: "Contains information licenced under the Data Server End-use Licence of Environment
  and Climate Change Canada."
- **NOAA** asks for attribution of unaltered data, and that nothing implies NOAA endorses your
  work.

## Open-Meteo

A weather **model**, not an observation, even for past hours. Each fetch asks for the past day
and a 3-day forecast at one resolution (60 or 15 minutes), and stores the row for **the latest
period that ended at or before the fetch**: at 14:20 that's the hourly row for 13:00–14:00 and
the 15-minute row for 14:00–14:15. `poll --weather` fetches the hourly data every hour and the
15-minute data every 15 minutes.

- **Times are period ends.** Radiation is the mean over the period before, precipitation the
  sum, temperature and cloud the value at the end. The tables are in UTC.
- Open-Meteo has no "rain or snow" field: `rain`, `showers`, `snowfall` and `weather_code`
  (66–67 freezing rain, 7x snow) split it.
- **In North America**, Open-Meteo's default (`best_match`) serves NOAA's **HRRR** model (3 km, a
  new run every hour) for about the next 40 hours, then **GFS**. Elsewhere it picks other models.
  For HRRR:
  - the **15-minute data is real model output** for the past day and about the next 16 hours;
    beyond that it's the hourly values interpolated;
  - the **hourly radiation is a snapshot at the hour, not an hourly mean**, so it tracks a solar
    array badly. **Use the 15-minute rows for sunlight**; the mean of four tracks the array far
    better;
  - the past day is stitched from several runs, so the hourly and 15-minute values can come from
    different runs, even when fetched in the same second.

## Radar

Environment Canada's 1 km North American composite (it covers Canada and the northern US), a
frame every 6 minutes, sampled at your site:

- `rain_mm_h` and `snow_cm_h` are **the same echo converted two ways**: they say how much is
  falling, not whether it's rain or snow.
- `covered` says whether the radars cover your site at all. "Undetected" means nothing is falling.
- GeoMet keeps only the **last 3 hours**, so the radar has to be recorded live (`poll --radar`);
  anything missed is gone.
- It can overstate light snow, for example when flakes evaporate below the beam.

## Satellite

`sigen satellite` and `poll --satellite` read three of NOAA GOES-West's cloud products for a box
around your site, from every scan (every 10 minutes), into memory, and store only what the
dashboard's strips use: the share of sunlight through the cloud over the house, per scan, in
`satellite_shade`. A scan is fetched once all three products are out: NOAA publishes ACMF about 10
minutes after the scan starts, ACHAF about 11, and CODF about 12.5 (now and then up to ~20). It's
all public: the bucket is part of AWS's open data programme, so the requests cost you nothing.

### Satellite cubes

With `--cube-dir`, `sigen satellite` keeps the boxes instead, as one file per product per local
day, a **cube**: `captures/satellite/cubes/goes18_<product>_<date>.h5` by default.

| Product | What | Pixel size |
|---|---|---|
| ACMF | clear-sky mask: 4 levels from clear to cloudy, plus cloud probabilities | ~2 km |
| CODF | cloud optical depth (daytime only; 0 = clear) | ~4 km |
| ACHAF | cloud-top height | ~10 km |

Each cube holds every scan of the day (time × rows × columns) for a box ±200 km around the site
(`--km`), with enough projection data to work out the latitude and longitude of any pixel. The
box is that big because the cloud shading your panels is toward the sun: a 10 km high cloud at a
10° sun angle shades the ground about 57 km away. A day is ~3–6 MB on disk.

By default only scans from an hour before sunrise to an hour after sunset are fetched
(`--daylight`, `--all-day`). Only the box is downloaded, not the whole Earth disk, and scans
already in a cube are skipped, so re-running a day only fetches what's new. GOES-West covers
western North America and the Pacific; NOAA keeps every GOES-18 scan since 2022-05-11, so you can
fetch any past day.

## When a fetch fails

A failed weather or radar fetch is stored too, as a row whose `error` column holds the whole HTTP
response (or what went wrong if nothing answered), so gaps in the data are explained. Nothing a
weather fetch does can stop `poll` from reading the device.
