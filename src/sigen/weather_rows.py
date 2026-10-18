"""The rows sigen.weather fetches and sigen.db stores: one module both import, so neither imports the other's
callers (db would otherwise import weather, which stores through db)."""

import datetime
from dataclasses import dataclass, field


@dataclass
class WeatherRow:
    """One Open-Meteo fetch at one resolution (an open_meteo_weather row):
    OPEN_METEO_VARIABLES for the latest finished period, or an error."""

    fetched_at: datetime.datetime
    resolution_minutes: int
    valid_at: datetime.datetime | None = None  # end of the period; None when error is set
    values: dict[str, object] = field(default_factory=dict)
    error: str | None = None


@dataclass
class RadarRow:
    """One radar frame at the site (a radar_frames row): rain in mm/h and snow
    in cm/h, each with GeoMet's class, and whether radar covers the site; or an
    error (frame_at None when even listing the frames failed)."""

    fetched_at: datetime.datetime
    frame_at: datetime.datetime | None = None
    rain_mm_h: float | None = None
    rain_class: str | None = None
    snow_cm_h: float | None = None
    snow_class: str | None = None
    covered: bool | None = None
    error: str | None = None
