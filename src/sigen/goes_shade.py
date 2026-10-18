"""Cloud over the site and at the points that could shade it, for every GOES scan, from the cubes (sigen.goes).

Each scan is sampled SAMPLE_DELAY after it starts (full-disk scans run north to south over 10 minutes; mid-latitudes
are reached about a minute in):

* For each assumed cloud height (0 = the pixel over the site): the shading cloud sits toward the sun by
  height / tan(sun elevation), and the satellite sees a raised cloud displaced away from itself (parallax), so the
  pixel is looked up for that raised point. Over its 3×3 pixels:
      prob_<h>km, cloudy_<h>km   mean cloud probability; share probably cloudy or cloudy        (ACMF)
      cod_<h>km, trans_<h>km     mean optical depth; mean estimated share of sunlight through it   (CODF)
* "top": the same at the measured cloud-top height (ACHAF). From a first guess, place the shade point, read the
  cloud-top height there (3×3 median), and repeat until it settles. top_km is that height (NaN if no cloud top was
  found there: the point then stays at the last guess); cod_top and trans_top are read at that point.

The share of sunlight through a cloud is cloud_transmission(): an Eddington two-stream approximation for a
non-absorbing cloud at the scan's sun angle, not fitted to any array.

Cubes are read whole into memory and closed at once (Cube), so `sigen satellite` can swap in a new one at any
moment. Needs numpy, h5py and pvlib.
"""

import datetime
import io
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

import h5py
import numpy as np

from sigen.goes import DEFAULT_SATELLITE, FixedGrid, cube_path

SAMPLE_DELAY = datetime.timedelta(minutes=1)
SCAN = datetime.timedelta(minutes=10)  # full-disk scans: one every 10 minutes
ASYMMETRY = 0.85  # cloud droplets scatter mostly forward; ice cloud is a little lower (~0.75-0.8)
FIRST_GUESS_KM = 8.0  # starting cloud height for the cloud-top search
SETTLED_KM = 0.25  # stop when the height changes less than this
HEIGHTS_KM = (0, 2, 5, 10)
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def decode(raw, attrs):
    """Stored integers -> floats: unsigned fix-up, fill values to NaN, scale and offset (NetCDF conventions)."""
    if attrs.get("_Unsigned", b"") in (b"true", "true") and raw.dtype.kind == "i":
        raw = raw.view(raw.dtype.str.replace("i", "u"))
    values = raw.astype(float)
    fill = attrs.get("_FillValue")
    if fill is not None:
        values[raw == np.array(fill).astype(raw.dtype).ravel()[0]] = np.nan
    if "scale_factor" in attrs:
        values = values * float(np.ravel(attrs["scale_factor"])[0]) + float(np.ravel(attrs.get("add_offset", [0]))[0])
    return values


def cloud_transmission(tau, mu0, g=ASYMMETRY):
    """Share of sunlight (direct + diffuse) through a non-absorbing cloud of optical depth tau, sun at cos(zenith) mu0.

    Eddington two-stream reflectance R = (a + (1/2 - 3/4 mu0)(1 - exp(-tau/mu0))) / (1 + a), a = 3/4 (1-g) tau;
    transmission = 1 - R, clipped to [0, 1]."""
    a = 0.75 * (1 - g) * tau
    reflectance = (a + (0.5 - 0.75 * mu0) * (1 - np.exp(-tau / mu0))) / (1 + a)
    return np.clip(1 - reflectance, 0, 1)


def toward_sun(lat, lon, azimuth_deg, distance_km):
    """The point `distance_km` from (lat, lon) toward compass bearing `azimuth_deg` (flat-earth, fine to ~200 km)."""
    d_lat = distance_km * np.cos(np.radians(azimuth_deg)) / 111.2
    d_lon = distance_km * np.sin(np.radians(azimuth_deg)) / (111.2 * np.cos(np.radians(lat)))
    return lat + d_lat, lon + d_lon


def _mean(values):
    """Mean of the non-NaN values, NaN if there are none (without numpy's empty-slice warning)."""
    valid = values[~np.isnan(values)]
    return float(valid.mean()) if valid.size else math.nan


def _median(values):
    valid = values[~np.isnan(values)]
    return float(np.median(valid)) if valid.size else math.nan


class Cube:
    """One product's cube for one local day, read whole into memory: its scan starts (UTC), its grid, and decoded
    3×3 blocks of any scan."""

    def __init__(self, path: Path):
        self.f = h5py.File(io.BytesIO(Path(path).read_bytes()), "r")  # the file itself is closed again at once
        self.grid = FixedGrid(dict(self.f.attrs), self.f["x"][:], self.f["y"][:])
        self.times = [EPOCH + datetime.timedelta(seconds=int(ns) // 10**9) for ns in self.f["time"][:]]
        self.index = {t: i for i, t in enumerate(self.times)}
        self._frames = {}

    def block(self, var: str, i: int, row: int, col: int, half: int = 1):
        if (var, i) not in self._frames:
            self._frames[var, i] = decode(self.f[var][i], self.f[var].attrs)
        return self._frames[var, i][max(row - half, 0) : row + half + 1, max(col - half, 0) : col + half + 1]


@dataclass
class ShadeSeries:
    """One row per scan, in time order: when it was sampled (UTC), the sun, and the cloud columns (see the module's
    docstring; NaN where there was nothing to measure)."""

    times: List[datetime.datetime] = field(default_factory=list)  # scan start + SAMPLE_DELAY, UTC
    sun_elevation: List[float] = field(default_factory=list)
    sun_azimuth: List[float] = field(default_factory=list)
    columns: Dict[str, List[float]] = field(default_factory=dict)

    def column(self, name: str) -> List[float]:
        return self.columns.get(name, [math.nan] * len(self.times))


def shade_day(
    cube_dir: Path,
    lat: float,
    lon: float,
    day: datetime.date,
    satellite: str = DEFAULT_SATELLITE,
    sector: str = "F",
    heights: Sequence[float] = HEIGHTS_KM,
) -> ShadeSeries:
    """The series for one local day's cubes (empty if there are none)."""
    import pandas as pd
    import pvlib

    def open_cube(short):
        path = cube_path(cube_dir, satellite, f"{short}{sector}", day)
        return Cube(path) if path.exists() else None

    acm, cod, acha = open_cube("ACM"), open_cube("COD"), open_cube("ACHA")
    starts = sorted(set().union(*(set(c.times) for c in (acm, cod, acha) if c is not None)))
    if not starts:
        return ShadeSeries()
    samples = [t + SAMPLE_DELAY for t in starts]
    sun = pvlib.solarposition.get_solarposition(pd.DatetimeIndex(samples), lat, lon)

    def sample_cod(row, i, at_lat, at_lon, height_m, tag, mu0):
        pixel = cod.grid.pixel(at_lat, at_lon, height_m)
        if pixel is not None:
            tau = cod.block("COD", i, *pixel)
            row[f"cod_{tag}"] = _mean(tau)
            row[f"trans_{tag}"] = _mean(cloud_transmission(tau, mu0))

    rows = []
    for t0, elevation, azimuth in zip(starts, sun["elevation"], sun["azimuth"]):
        elevation, azimuth = float(elevation), float(azimuth)
        mu0 = np.sin(np.radians(elevation))
        row = {}
        i_acm = acm.index.get(t0) if acm else None
        i_cod = cod.index.get(t0) if cod else None
        i_acha = acha.index.get(t0) if acha else None
        for h in heights:
            tag = f"{h:g}km"
            if h > 0 and elevation <= 1:
                continue
            at_lat, at_lon = (lat, lon) if h == 0 else toward_sun(lat, lon, azimuth, h / np.tan(np.radians(elevation)))
            if i_acm is not None and (pixel := acm.grid.pixel(at_lat, at_lon, h * 1000)) is not None:
                row[f"prob_{tag}"] = _mean(acm.block("Cloud_Probabilities", i_acm, *pixel))
                levels = acm.block("ACM", i_acm, *pixel)
                row[f"cloudy_{tag}"] = _mean(np.where(np.isnan(levels), np.nan, levels >= 2))
            if i_cod is not None and elevation > 1:
                sample_cod(row, i_cod, at_lat, at_lon, h * 1000, tag, mu0)
        if i_acha is not None and elevation > 1:
            h, found = FIRST_GUESS_KM, math.nan
            for _ in range(6):
                at_lat, at_lon = toward_sun(lat, lon, azimuth, h / np.tan(np.radians(elevation)))
                pixel = acha.grid.pixel(at_lat, at_lon, h * 1000)
                found = _median(acha.block("HT", i_acha, *pixel)) / 1000 if pixel is not None else math.nan
                if math.isnan(found):
                    break
                settled = abs(found - h) < SETTLED_KM
                h = found
                if settled:
                    break
            if not math.isnan(found):  # place the final point at the settled height
                at_lat, at_lon = toward_sun(lat, lon, azimuth, h / np.tan(np.radians(elevation)))
            row["top_km"] = found
            if i_cod is not None:
                sample_cod(row, i_cod, at_lat, at_lon, h * 1000, "top", mu0)
        rows.append(row)
    names = sorted(set().union(*rows))
    return ShadeSeries(
        samples,
        [float(e) for e in sun["elevation"]],
        [float(a) for a in sun["azimuth"]],
        {n: [float(r.get(n, math.nan)) for r in rows] for n in names},
    )


def shade_between(
    cube_dir: Path,
    lat: float,
    lon: float,
    timezone: str,
    start: datetime.datetime,
    end: datetime.datetime,
    satellite: str = DEFAULT_SATELLITE,
) -> ShadeSeries:
    """Every scan sampled in [start, end), from the cubes of the local days the window touches."""
    tz = ZoneInfo(timezone)
    day, last = start.astimezone(tz).date(), (end - datetime.timedelta(microseconds=1)).astimezone(tz).date()
    out = ShadeSeries()
    while day <= last:
        series = shade_day(cube_dir, lat, lon, day, satellite)
        keep = [k for k, t in enumerate(series.times) if start <= t < end]
        names = set(out.columns) | set(series.columns)
        out.columns = {n: out.column(n) + [series.column(n)[k] for k in keep] for n in sorted(names)}
        out.times += [series.times[k] for k in keep]
        out.sun_elevation += [series.sun_elevation[k] for k in keep]
        out.sun_azimuth += [series.sun_azimuth[k] for k in keep]
        day += datetime.timedelta(days=1)
    return out
