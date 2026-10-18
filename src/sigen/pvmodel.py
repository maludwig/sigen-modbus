"""What each roof face (and so each PV string) would make on a cloudless day: the dashboard's clear-sky model.
pvlib does the physics:

- the sun's position for the site; clear-sky irradiance from the Ineichen model with pvlib's monthly haze (Linke
  turbidity) climatology;
- light on each face (Perez: direct sun, diffuse sky, ground reflection at the array's albedo), minus what the glass
  reflects at low angles (physical IAM, Marion's integrals for the diffuse parts);
- cell temperature (SAPM, the panel's mount parameter set) from the air temperature and wind, and PVWatts DC: rated
  power × effective light, with the panel's temperature coefficient. No soiling, wiring, mismatch or low-light
  losses, and no rear-side gain (bifacial panels flush on a roof get almost none).

The site, faces, panel counts and panel come from the config ("site", "array"). Times are a pandas DatetimeIndex,
timezone-aware. Without measured weather it assumes air at 12 °C and wind at 2 m/s.
"""

from typing import Dict, Optional, Union

import pandas as pd
import pvlib

from sigen.config import Array, Site

TEMP_AIR, WIND = 12.0, 2.0  # °C, m/s: the fixed weather for clear-sky days without measurements
Number = Union[float, pd.Series]


class Model:
    def __init__(self, site: Site, array: Array):
        self.site, self.array = site, array
        self.location = pvlib.location.Location(
            site.latitude,
            site.longitude,
            tz=site.timezone,
            altitude=site.altitude_m if site.altitude_m is not None else 0,
        )
        self.temp_params = pvlib.temperature.TEMPERATURE_MODEL_PARAMETERS["sapm"][array.panel.mount]
        self.gamma = array.panel.gamma_pct_per_c / 100

    def face_kw(
        self,
        times: pd.DatetimeIndex,
        ghi,
        dni,
        dhi,
        temp_air: Number,
        wind_speed: Number,
        tilt: float,
        azimuth: float,
        kw: float,
        sun: Optional[pd.DataFrame] = None,
    ) -> pd.Series:
        """Modelled DC power (kW) of `kw` of panels at `tilt` / `azimuth`, given the sunlight and weather at `times`.
        `sun` (the solar position at `times`) saves recomputing it for several faces."""
        sun = self.location.get_solarposition(times) if sun is None else sun
        poa = pvlib.irradiance.get_total_irradiance(
            tilt,
            azimuth,
            sun["apparent_zenith"],
            sun["azimuth"],
            dni,
            ghi,
            dhi,
            dni_extra=pvlib.irradiance.get_extra_radiation(times),
            airmass=self.location.get_airmass(times, solar_position=sun)["airmass_relative"],
            albedo=self.array.albedo,
            model="perez",
        )
        aoi = pvlib.irradiance.aoi(tilt, azimuth, sun["apparent_zenith"], sun["azimuth"])
        iam_diffuse = pvlib.iam.marion_diffuse("physical", tilt)
        effective = (
            poa["poa_direct"] * pvlib.iam.physical(aoi)
            + poa["poa_sky_diffuse"] * iam_diffuse["sky"]
            + poa["poa_ground_diffuse"] * iam_diffuse["ground"]
        ).fillna(0)
        effective = effective.where(sun["apparent_zenith"] < 90, 0)
        temp_cell = pvlib.temperature.sapm_cell(poa["poa_global"].fillna(0), temp_air, wind_speed, **self.temp_params)
        return pvlib.pvsystem.pvwatts_dc(effective, temp_cell, kw * 1000, self.gamma).clip(lower=0) / 1000

    def clear_sky_at(
        self, times: pd.DatetimeIndex, faces: Dict[str, float], temp_air: Number = TEMP_AIR, wind_speed: Number = WIND
    ) -> pd.DataFrame:
        """Clear-sky DC power (kW) at `times` for {face name: kW of panels on it}, one column per face. One sky and
        sun computation for all of them."""
        sky = self.location.get_clearsky(times, model="ineichen")
        sun = self.location.get_solarposition(times)
        return pd.DataFrame(
            {
                name: self.face_kw(
                    times,
                    sky["ghi"],
                    sky["dni"],
                    sky["dhi"],
                    temp_air,
                    wind_speed,
                    self.array.faces[name].tilt,
                    self.array.faces[name].azimuth,
                    kw,
                    sun=sun,
                )
                for name, kw in faces.items()
            },
            index=times,
        )

    def strings_clear_sky(
        self, times: pd.DatetimeIndex, temp_air: Number = TEMP_AIR, wind_speed: Number = WIND
    ) -> pd.DataFrame:
        """Clear-sky DC power (kW) of each string (columns: PV input numbers) at `times`."""
        faces = self.clear_sky_at(times, {face: 1.0 for face in self.array.faces}, temp_air, wind_speed)
        return pd.DataFrame({n: faces[s.face] * self.array.kw(n) for n, s in self.array.strings.items()}, index=times)

    def clear_sky_kw(
        self,
        day: pd.Timestamp,
        tilt: float,
        azimuth: float,
        kw: float,
        days: int = 1,
        freq: str = "1min",
        temp_air: Number = TEMP_AIR,
        wind_speed: Number = WIND,
    ) -> pd.Series:
        """Clear-sky DC power (kW) of `kw` of panels at `tilt` / `azimuth`, every `freq` over
        `days` cloudless days from `day` (a local midnight)."""
        times = pd.date_range(day, day + pd.Timedelta(days=days), freq=freq, inclusive="left")
        sky = self.location.get_clearsky(times, model="ineichen")
        return self.face_kw(times, sky["ghi"], sky["dni"], sky["dhi"], temp_air, wind_speed, tilt, azimuth, kw)
