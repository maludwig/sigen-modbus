"""NOAA GOES-West cloud data around the site: per-day "cubes" cut out of the full-disk Level-2 files on AWS.

GOES-18 scans the whole Earth disk every 10 minutes, and NOAA publishes each product as one NetCDF (HDF5) file per
scan in a public bucket (https://noaa-goes18.s3.amazonaws.com, no account). Three products are used:
    ACMF   clear sky mask: ACM (4 levels), BCM, Cloud_Probabilities      ~2 km pixels, 5424 × 5424
    CODF   cloud optical depth (COD, daytime only)                         ~4 km, 2712 × 2712
    ACHAF  cloud-top height (HT)                                           ~10 km
each with a DQF (data quality) variable. Values are raw integers plus scale / fill attributes.

A cube, <cube dir>/goes<sat>_<product>_<local date>.h5, holds one product's box ±km around the site for one local
day: every 2-D variable as time × rows × columns (gzip, one scan per chunk), the scan times (int64 ns, UTC), the
box's scan angles x / y, its place on the full disk (row0, col0) and the projection constants, so FixedGrid can
rebuild the latitude / longitude of any pixel (with parallax for a raised cloud). The first scan of a cube sets
its variables' attributes; later scans only add data.

Only the box is read from AWS: the files are HDF5 stored as compressed strips of rows (ACMF: 48 rows × the whole
width), so with HTTP Range requests (fsspec, 128 KiB blocks) a scan needs the file's metadata and the few strips the
box falls in: ~1.4 MB of ACMF's 24 MB. Scans are fetched in parallel processes (h5py holds one global lock, so
threads would wait on each other's network reads) and appended in time order. Scans already in a cube are skipped,
so running it again only fetches what's new.

Needs numpy, h5py and fsspec[http] (dependencies of the package); imported only by `sigen satellite`.
"""
import concurrent.futures
import datetime
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import h5py
import numpy as np

from sigen.errors import ConnectionFailed, SigenError

DEFAULT_SATELLITE = "18"                 # GOES-West: western North America and the Pacific
PRODUCTS = ("ACM", "COD", "ACHA")
DEFAULT_KM = 200                         # box half-width: a 10 km cloud's shade at 10° sun is ~57 km away
DEFAULT_WORKERS = 8
DEFAULT_DAYLIGHT_MINUTES = 60            # an hour either side of sunrise..sunset: when clouds shade panels
BLOCK_SIZE = 128 * 1024                  # Range request size: ACMF ~1.4 MB a scan (256 KiB: 2.4, 1 MiB: 6.3)
PROJECTION_ATTRS = ("semi_major_axis", "semi_minor_axis", "perspective_point_height", "longitude_of_projection_origin")
SKIP_ATTRS = {"DIMENSION_LIST", "REFERENCE_LIST", "_Netcdf4Dimid", "_Netcdf4Coordinates", "CLASS", "NAME"}
S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
SCAN_START = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})")


# ---------- the bucket ----------
def bucket_url(satellite: str) -> str:
    return f"https://noaa-goes{satellite}.s3.amazonaws.com"


def list_keys(satellite: str, prefix: str, get=None) -> List[Tuple[str, int]]:
    """(key, size) of every object under `prefix` in the satellite's bucket."""
    from sigen.weather import http_get
    get = get or http_get
    keys, token = [], None
    while True:
        params = {"list-type": "2", "prefix": prefix, **({"continuation-token": token} if token else {})}
        response = get(bucket_url(satellite), params)
        if not response.ok:
            raise ConnectionFailed(f"listing {prefix} in {bucket_url(satellite)} failed: "
                                   f"{response.describe().splitlines()[0]}")
        root = ET.fromstring(response.text)
        keys += [(c.find("s3:Key", S3_NS).text, int(c.find("s3:Size", S3_NS).text))
                 for c in root.findall("s3:Contents", S3_NS)]
        if root.findtext("s3:IsTruncated", namespaces=S3_NS) != "true":
            return keys
        token = root.findtext("s3:NextContinuationToken", namespaces=S3_NS)


def scan_start(key: str) -> datetime.datetime:
    """The scan start (UTC, whole seconds) in a GOES file name: …_s20262831900224_… is 2026 day 283 19:00:22."""
    m = SCAN_START.search(key)
    if m is None:
        raise ValueError(f"no scan start in {key!r}")
    year, doy, hh, mm, ss = map(int, m.groups())
    return datetime.datetime(year, 1, 1, hh, mm, ss, tzinfo=datetime.timezone.utc) + datetime.timedelta(days=doy - 1)


def nanoseconds(t: datetime.datetime) -> int:
    """A UTC datetime as int64 nanoseconds since the epoch (the cubes' time axis)."""
    delta = t - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10 ** 9 + delta.microseconds * 1000


def keys_between(satellite: str, product: str, start: datetime.datetime, end: datetime.datetime,
                 lister: Optional[Callable] = None) -> List[Tuple[str, int]]:
    """(key, size) of the `product` files (e.g. "ABI-L2-ACMF") whose scan starts in [start, end), by time."""
    lister = lister or list_keys
    found = []
    hour = start.astimezone(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
    while hour < end:
        prefix = f"{product}/{hour:%Y}/{hour.timetuple().tm_yday:03d}/{hour:%H}/"
        found += [(k, s) for k, s in lister(satellite, prefix) if start <= scan_start(k) < end]
        hour += datetime.timedelta(hours=1)
    return sorted(found, key=lambda ks: scan_start(ks[0]))


# ---------- geometry (GOES-R Product User Guide, section 4.2.8) ----------
class FixedGrid:
    """An ABI fixed grid (or a box of one): scan angles <-> latitude/longitude, and pixel lookup."""

    def __init__(self, projection, x, y):
        self.r_eq = float(np.ravel(projection["semi_major_axis"])[0])
        self.r_pol = float(np.ravel(projection["semi_minor_axis"])[0])
        self.H = float(np.ravel(projection["perspective_point_height"])[0]) + self.r_eq
        self.lon0 = np.radians(float(np.ravel(projection["longitude_of_projection_origin"])[0]))
        self.x, self.y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)

    @classmethod
    def from_netcdf(cls, f):
        """The grid of a GOES NetCDF file opened with h5py."""
        def axis(d):
            return d[:] * float(d.attrs["scale_factor"][0]) + float(d.attrs["add_offset"][0])
        return cls(f["goes_imager_projection"].attrs, axis(f["x"]), axis(f["y"]))

    def scan_angles(self, lat, lon, height_m=0.0):
        """Scan angles (x, y) in radians at which the satellite sees a point `height_m` above (lat, lon)."""
        phi, lam = np.radians(lat), np.radians(lon)
        phi_c = np.arctan((self.r_pol ** 2 / self.r_eq ** 2) * np.tan(phi))
        e2 = (self.r_eq ** 2 - self.r_pol ** 2) / self.r_eq ** 2
        r_c = self.r_pol / np.sqrt(1 - e2 * np.cos(phi_c) ** 2) + height_m
        s_x = self.H - r_c * np.cos(phi_c) * np.cos(lam - self.lon0)
        s_y = -r_c * np.cos(phi_c) * np.sin(lam - self.lon0)
        s_z = r_c * np.sin(phi_c)
        return np.arcsin(-s_y / np.sqrt(s_x ** 2 + s_y ** 2 + s_z ** 2)), np.arctan(s_z / s_x)

    def lat_lon(self, x, y):
        """Ground (lat, lon) in degrees seen at scan angles x, y (radians)."""
        a = np.sin(x) ** 2 + np.cos(x) ** 2 * (np.cos(y) ** 2 + (self.r_eq ** 2 / self.r_pol ** 2) * np.sin(y) ** 2)
        b = -2 * self.H * np.cos(x) * np.cos(y)
        c = self.H ** 2 - self.r_eq ** 2
        r_s = (-b - np.sqrt(b ** 2 - 4 * a * c)) / (2 * a)
        s_x, s_y, s_z = r_s * np.cos(x) * np.cos(y), -r_s * np.sin(x), r_s * np.cos(x) * np.sin(y)
        lat = np.degrees(np.arctan((self.r_eq ** 2 / self.r_pol ** 2) * s_z / np.sqrt((self.H - s_x) ** 2 + s_y ** 2)))
        return lat, np.degrees(self.lon0 - np.arctan(s_y / (self.H - s_x)))

    def pixel(self, lat, lon, height_m=0.0):
        """(row, col) of the pixel showing a point `height_m` above (lat, lon), or None if outside the grid."""
        x, y = self.scan_angles(lat, lon, height_m)
        step_x, step_y = abs(self.x[1] - self.x[0]), abs(self.y[1] - self.y[0])
        if not (self.x.min() - step_x / 2 <= x <= self.x.max() + step_x / 2
                and self.y.min() - step_y / 2 <= y <= self.y.max() + step_y / 2):
            return None
        return int(np.abs(self.y - y).argmin()), int(np.abs(self.x - x).argmin())


def box_window(grid: FixedGrid, lat: float, lon: float, km: float) -> Tuple[slice, slice]:
    """(row slice, col slice) of `grid` covering the ground box ±km around (lat, lon), plus a 2-pixel margin."""
    d_lat, d_lon = km / 111.2, km / (111.2 * np.cos(np.radians(lat)))
    edge = np.linspace(-1, 1, 41)
    lats = np.concatenate([lat + d_lat * edge, lat + d_lat * edge, np.full(41, lat - d_lat), np.full(41, lat + d_lat)])
    lons = np.concatenate([np.full(41, lon - d_lon), np.full(41, lon + d_lon), lon + d_lon * edge, lon + d_lon * edge])
    pixels = [grid.pixel(a, o) for a, o in zip(lats, lons)]
    if any(p is None for p in pixels):
        raise SigenError(f"the ±{km:g} km box around the site runs off this product's grid; use a smaller --km")
    rows, cols = zip(*pixels)
    return slice(max(min(rows) - 2, 0), max(rows) + 3), slice(max(min(cols) - 2, 0), max(cols) + 3)


# ---------- daylight ----------
def sun_rise_set(day: datetime.date, lat: float, lon: float) -> Optional[Tuple[datetime.datetime, datetime.datetime]]:
    """Sunrise and sunset (UTC) on `day` at (lat, lon), NOAA's approximation (within ~2 minutes); None if the sun
    doesn't rise or doesn't set that day."""
    g = 2 * math.pi / 365 * (day.timetuple().tm_yday - 1 + 0.5)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))              # minutes
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    phi = math.radians(lat)
    cos_ha = math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(decl)) - math.tan(phi) * math.tan(decl)
    if not -1 <= cos_ha <= 1:
        return None
    ha = math.degrees(math.acos(cos_ha))
    midnight = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.timezone.utc)
    return (midnight + datetime.timedelta(minutes=720 - 4 * (lon + ha) - eqtime),
            midnight + datetime.timedelta(minutes=720 - 4 * (lon - ha) - eqtime))


def in_daylight(t: datetime.datetime, lat: float, lon: float, timezone: str, margin_minutes: float) -> bool:
    """Whether `t` is within margin_minutes of sunrise..sunset on its local day (always, if the sun never sets)."""
    times = sun_rise_set(t.astimezone(ZoneInfo(timezone)).date(), lat, lon)
    if times is None:
        return True
    margin = datetime.timedelta(minutes=margin_minutes)
    return times[0] - margin <= t <= times[1] + margin


# ---------- cutting and storing ----------
@dataclass
class Box:
    """One scan's box, in memory: what gets appended to the cube."""
    rows: slice
    cols: slice
    x: np.ndarray                  # the box's scan angles
    y: np.ndarray
    projection: dict
    variables: dict                # name -> (raw array, attributes, dtype), in the file's order


def cut_box(src, lat: float, lon: float, km: float, window=None) -> Box:
    """The box out of an open GOES file: `window` (rows, cols) if given (an existing cube's), else computed."""
    grid = FixedGrid.from_netcdf(src)
    rows, cols = window if window is not None else box_window(grid, lat, lon, km)
    shape = (len(grid.y), len(grid.x))
    variables = {name: (d[rows, cols], {k: v for k, v in d.attrs.items() if k not in SKIP_ATTRS}, d.dtype)
                 for name, d in src.items() if isinstance(d, h5py.Dataset) and d.shape == shape}
    projection = {k: src["goes_imager_projection"].attrs[k] for k in PROJECTION_ATTRS}
    return Box(rows, cols, grid.x[cols], grid.y[rows], projection, variables)


def fetch_box(url: str, lat: float, lon: float, km: float, window=None, opener=None) -> Tuple[Box, int]:
    """(Box, bytes fetched) for the file at `url`, reading only what the box needs. `opener(url)` returns a
    seekable binary file with a `.cache.total_requested_bytes` (default: fsspec over HTTP)."""
    if opener is None:
        import fsspec
        f = fsspec.filesystem("http").open(url, "rb", block_size=BLOCK_SIZE, cache_type="blockcache")
    else:
        f = opener(url)
    with f:
        with h5py.File(f, "r") as src:
            box = cut_box(src, lat, lon, km, window)
        return box, int(getattr(getattr(f, "cache", None), "total_requested_bytes", 0))


def cube_path(cube_dir: Path, satellite: str, product: str, day: datetime.date) -> Path:
    """<cube dir>/goes<sat>_<product>_<local date>.h5, e.g. goes18_ACMF_2026-10-10.h5."""
    return Path(cube_dir) / f"goes{satellite}_{product}_{day:%Y-%m-%d}.h5"


def append_box(path: Path, box: Box, t0: datetime.datetime, product: str, satellite: str, km: float,
               lat: float, lon: float) -> None:
    """Append one scan's box to the cube at `path` (created on first use)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "a") as cube:
        if "time" not in cube:
            cube.attrs.update({"satellite": f"GOES-{satellite}", "product": product, "box_half_km": km,
                               "center_lat": lat, "center_lon": lon, "row0": box.rows.start, "col0": box.cols.start,
                               **box.projection})
            cube["x"], cube["y"] = box.x, box.y
            cube.create_dataset("time", shape=(0,), maxshape=(None,), dtype="int64")
            for name, (data, attrs, dtype) in box.variables.items():
                out = cube.create_dataset(name, shape=(0, *data.shape), maxshape=(None, *data.shape), dtype=dtype,
                                          chunks=(1, *data.shape), compression="gzip", compression_opts=6, shuffle=True)
                out.attrs.update(attrs)
        elif not (np.allclose(cube["x"][:], box.x) and np.allclose(cube["y"][:], box.y)):
            raise SigenError(f"{product} {t0:%Y-%m-%d %H:%M}Z: its grid doesn't match {path.name}")
        n = cube["time"].shape[0]
        cube["time"].resize((n + 1,))
        cube["time"][n] = nanoseconds(t0)
        for name, (data, _, _) in box.variables.items():
            if name in cube:
                cube[name].resize((n + 1, *cube[name].shape[1:]))
                cube[name][n] = data


def _network_errors() -> tuple:
    """What a failed read raises: OSError (incl. timeouts and fsspec's FileNotFoundError for a 404), and
    aiohttp's own errors, which aren't all OSErrors."""
    try:
        import aiohttp
        return OSError, aiohttp.ClientError
    except ImportError:
        return (OSError,)


def _existing(path: Path):
    """(scan times already in the cube at `path`, its window) or (empty set, None)."""
    if not path.exists():
        return set(), None
    with h5py.File(path, "r") as cube:
        if "time" not in cube:
            return set(), None
        row0, col0 = int(cube.attrs["row0"]), int(cube.attrs["col0"])
        return (set(cube["time"][:].tolist()),
                (slice(row0, row0 + len(cube["y"])), slice(col0, col0 + len(cube["x"]))))


@dataclass
class ProductResult:
    product: str
    scans: int            # in the time window (after the daylight filter)
    added: int
    fetched_bytes: int
    seconds: float
    cubes: List[Path]


def build_cubes(*, lat: float, lon: float, timezone: str, start: datetime.datetime, end: datetime.datetime,
                cube_dir: Path, products: Sequence[str] = PRODUCTS, km: float = DEFAULT_KM,
                satellite: str = DEFAULT_SATELLITE, sector: str = "F", workers: int = DEFAULT_WORKERS,
                daylight_minutes: Optional[float] = DEFAULT_DAYLIGHT_MINUTES,
                lister: Optional[Callable] = None, opener=None, log=lambda msg: None) -> List[ProductResult]:
    """Add every scan in [start, end) to its day's cube, per product; scans already there are skipped.
    With `daylight_minutes`, only scans within that many minutes of the site's sunrise..sunset.
    `lister` and `opener` are for tests (an opener runs everything in this process)."""
    results = []
    pool = (concurrent.futures.ProcessPoolExecutor(workers) if workers > 1 and opener is None else None)
    try:
        for short in products:
            product = f"{short}{sector}"
            began = time.perf_counter()
            keys = keys_between(satellite, f"ABI-L2-{product}", start, end, lister)
            if daylight_minutes is not None:
                keys = [(k, s) for k, s in keys if in_daylight(scan_start(k), lat, lon, timezone, daylight_minutes)]
            todo, present, windows = [], {}, {}
            for key, _ in keys:
                t0 = scan_start(key)
                path = cube_path(cube_dir, satellite, product, t0.astimezone(ZoneInfo(timezone)).date())
                if path not in present:
                    present[path], windows[path] = _existing(path)
                if nanoseconds(t0) not in present[path]:
                    todo.append((key, t0, path))
                    present[path].add(nanoseconds(t0))
            urls = [f"{bucket_url(satellite)}/{key}" for key, _, _ in todo]
            calls = [(url, lat, lon, km, windows[path]) for url, (_, _, path) in zip(urls, todo)]
            if pool and calls:
                boxes = pool.map(fetch_box, *zip(*calls))
            else:
                boxes = (fetch_box(*call, opener=opener) for call in calls)
            fetched = 0
            try:
                for (key, t0, path), (box, n_bytes) in zip(todo, boxes):      # in time order
                    append_box(path, box, t0, product, satellite, km, lat, lon)
                    fetched += n_bytes
            except _network_errors() as e:
                raise ConnectionFailed(f"reading {product} from {bucket_url(satellite)} failed: {e}")
            result = ProductResult(product, len(keys), len(todo), fetched, time.perf_counter() - began,
                                   sorted(p for p in present if p.exists()))
            log(f"{product}: {result.scans} scans, {result.added} added, {result.scans - result.added} already in "
                f"cubes; {fetched / 1e6:.0f} MB fetched in {result.seconds:.0f} s; "
                + ", ".join(f"{p.name} {p.stat().st_size / 1e6:.1f} MB" for p in result.cubes))
            results.append(result)
    finally:
        if pool:
            pool.shutdown()
    return results
