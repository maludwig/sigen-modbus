-- Weather at the site (config "site"), for comparing with what the panels make.
-- Both tables only gain rows; nothing here touches the readings tables.

-- Open-Meteo (https://open-meteo.com), a weather MODEL, not an observation. Each fetch stores
-- one row: the latest period that ended at or before fetched_at, at that resolution
-- (hourly, or 15-minute). The same valid_at can appear in several rows if fetches overlap.
--
-- Times are period ENDS. "sum" and "mean" columns cover the resolution_minutes before
-- valid_at; "at" columns are the value at the instant valid_at.
-- A failed fetch is a row with valid_at NULL, every value NULL, and the error: the whole
-- HTTP response text, or what went wrong if there was no response.
CREATE TABLE open_meteo_weather (
    id                        BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fetched_at                TIMESTAMPTZ NOT NULL,     -- this computer's clock
    resolution_minutes        SMALLINT    NOT NULL CHECK (resolution_minutes IN (15, 60)),
    valid_at                  TIMESTAMPTZ,              -- end of the period
    shortwave_radiation       REAL,       -- W/m², mean: global horizontal irradiance (GHI)
    direct_radiation          REAL,       -- W/m², mean: direct beam on a horizontal surface
    diffuse_radiation         REAL,       -- W/m², mean: diffuse on a horizontal surface
    direct_normal_irradiance  REAL,       -- W/m², mean: direct beam facing the sun (DNI)
    sunshine_duration         REAL,       -- s, sum: time with DNI above 120 W/m²
    cloud_cover               REAL,       -- %, at: total cloud
    cloud_cover_low           REAL,       -- %, at: cloud below 3 km
    cloud_cover_mid           REAL,       -- %, at: cloud 3–8 km
    cloud_cover_high          REAL,       -- %, at: cloud above 8 km
    temperature_2m            REAL,       -- °C, at
    relative_humidity_2m      REAL,       -- %, at
    wind_speed_10m            REAL,       -- km/h, at
    wind_gusts_10m            REAL,       -- km/h, max over the period
    visibility                REAL,       -- m, at
    precipitation             REAL,       -- mm, sum: rain + showers + snowfall (as water)
    rain                      REAL,       -- mm, sum: liquid, from widespread precipitation
    showers                   REAL,       -- mm, sum: liquid, from convective showers
    snowfall                  REAL,       -- cm, sum: fresh snow
    snow_depth                REAL,       -- m, at: snow on the ground
    weather_code              SMALLINT,   -- WMO code for the period: 0 clear … 3 overcast, 6x rain, 66-67 freezing rain, 7x snow, 8x showers, 95+ thunder
    freezing_level_height     REAL,       -- m above sea level, at: where the air crosses 0 °C
    precipitation_probability REAL,       -- %, chance of more than 0.1 mm in the period
    is_day                    BOOLEAN,    -- at
    error                     TEXT,
    CHECK ((error IS NULL) = (valid_at IS NOT NULL))
);
CREATE INDEX open_meteo_weather_valid_at ON open_meteo_weather (resolution_minutes, valid_at);

-- Environment Canada's weather radar (MSC GeoMet, 1 km North American composite, a frame
-- every 6 minutes), sampled at the site. GeoMet keeps 3 hours of frames, so each fetch stores
-- every frame in that window not stored yet. An observation: never revised, so one good row
-- per frame.
--
-- rain_mm_h and snow_cm_h are the SAME radar echo converted with two formulas; they say how
-- much is falling, not whether it's rain or snow. The *_class columns are GeoMet's label
-- ("Undetected" when nothing is falling). covered: whether the radar network covered the site.
-- A failed fetch is a row with the error (the whole HTTP response text, or what went wrong);
-- frame_at is NULL if even the list of frames couldn't be read.
CREATE TABLE radar_frames (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fetched_at TIMESTAMPTZ NOT NULL,     -- this computer's clock
    frame_at   TIMESTAMPTZ,              -- the radar frame's time
    rain_mm_h  REAL,
    rain_class TEXT,
    snow_cm_h  REAL,
    snow_class TEXT,
    covered    BOOLEAN,
    error      TEXT,
    CHECK (error IS NOT NULL OR (frame_at IS NOT NULL AND covered IS NOT NULL))
);
CREATE UNIQUE INDEX radar_frames_one_good_row ON radar_frames (frame_at) WHERE error IS NULL;
CREATE INDEX radar_frames_fetched_at ON radar_frames (fetched_at);
