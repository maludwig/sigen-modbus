"""sigen.goes and `sigen satellite`, with NOAA faked: synthetic full-disk files on disk, a fake bucket listing."""
import datetime
import json

import h5py
import numpy as np
import pytest

from sigen import goes
from sigen.errors import ConnectionFailed, ExitCode, SigenError
from sigen.weather import Response

LAT, LON, TZ = 51.0443, -114.0631, "America/Edmonton"
PROJECTION = {"semi_major_axis": np.array([6378137.0]), "semi_minor_axis": np.array([6356752.31414]),
              "perspective_point_height": np.array([35786023.0]), "longitude_of_projection_origin": np.array([-137.0])}
STEP = 56e-6                          # radians, ~2 km at the sub-satellite point
NY, NX = 80, 120
KM = 50
UTC = datetime.timezone.utc


def site_angles():
    grid = goes.FixedGrid(PROJECTION, [0, 1], [0, 1])
    return grid.scan_angles(LAT, LON)


def write_scan(path, value, shift=0.0):
    """A small 'full-disk' file around the site: scan angles, projection, three 2-D variables and a 1-D one."""
    x0, y0 = site_angles()
    x = x0 + shift + STEP * (np.arange(NX) - NX // 2)
    y = y0 - STEP * (np.arange(NY) - NY // 2)                   # rows run north to south, like the real files
    rng = np.random.default_rng(value)
    with h5py.File(path, "w") as f:
        for name, axis in (("x", x), ("y", y)):
            d = f.create_dataset(name, data=axis)
            d.attrs["scale_factor"], d.attrs["add_offset"] = np.array([1.0]), np.array([0.0])
        proj = f.create_dataset("goes_imager_projection", data=np.int32(0))
        proj.attrs.update(PROJECTION)
        acm = f.create_dataset("ACM", data=rng.integers(0, 4, (NY, NX), dtype=np.uint8), chunks=(16, NX),
                               compression="gzip")
        acm.attrs["flag_values"] = np.array([0, 1, 2, 3], dtype=np.uint8)
        acm.attrs["NAME"] = "dropped: netCDF bookkeeping"
        prob = f.create_dataset("Cloud_Probabilities", data=rng.integers(0, 65535, (NY, NX), dtype=np.uint16),
                                chunks=(8, NX), compression="gzip")
        prob.attrs["scale_factor"], prob.attrs["_FillValue"] = np.array([1.5e-5]), np.array([65535], np.uint16)
        f.create_dataset("DQF", data=np.full((NY, NX), value, dtype=np.uint8))
        f.create_dataset("band_id", data=np.array([14], dtype=np.uint8))   # not 2-D: not stored
    return path


def key_for(product, t):
    return (f"ABI-L2-{product}/{t:%Y}/{t.timetuple().tm_yday:03d}/{t:%H}/"
            f"OR_ABI-L2-{product}-M6_G18_s{t:%Y}{t.timetuple().tm_yday:03d}{t:%H%M%S}0_e0_c0.nc")


class FakeBucket:
    """Scans at the given UTC times for every product, served from synthetic files."""

    def __init__(self, tmp_path, times):
        self.files, self.listed = {}, []
        for n, t in enumerate(times):
            for product in ("ACMF", "CODF", "ACHAF"):
                key = key_for(product, t)
                self.files[key] = write_scan(tmp_path / f"{product}_{n}.nc", value=n + 1)

    def lister(self, satellite, prefix):
        self.listed.append(prefix)
        return [(k, 1000) for k in reversed(self.files) if k.startswith(prefix)]       # deliberately unsorted

    def opener(self, url):
        key = url.split(".amazonaws.com/", 1)[1]
        return open(self.files[key], "rb")


DAY_SCANS = [datetime.datetime(2026, 10, 10, h, m, 22, tzinfo=UTC) for h, m in ((7, 0), (19, 10), (19, 0))]


def build(tmp_path, bucket, **kw):
    params = dict(lat=LAT, lon=LON, timezone=TZ, start=datetime.datetime(2026, 10, 10, 6, tzinfo=UTC),
                  end=datetime.datetime(2026, 10, 11, 6, tzinfo=UTC), cube_dir=tmp_path / "cubes", km=KM,
                  products=["ACM"], workers=1, daylight_minutes=None, lister=bucket.lister, opener=bucket.opener)
    params.update(kw)
    return goes.build_cubes(**params)


# ---------- names, times, listing ----------
def test_scan_start_and_nanoseconds():
    t = goes.scan_start("ABI-L2-ACMF/2026/283/19/OR_ABI-L2-ACMF-M6_G18_s20262831900224_e2026_c2026.nc")
    assert t == datetime.datetime(2026, 10, 10, 19, 0, 22, tzinfo=UTC)          # day 283 of 2026; tenths ignored
    assert goes.nanoseconds(t) == int(t.timestamp()) * 10 ** 9
    with pytest.raises(ValueError):
        goes.scan_start("no-time-here.nc")


def listing(keys, token=None):
    contents = "".join(f"<Contents><Key>{k}</Key><Size>{s}</Size></Contents>" for k, s in keys)
    more = f"<IsTruncated>true</IsTruncated><NextContinuationToken>{token}</NextContinuationToken>" if token \
        else "<IsTruncated>false</IsTruncated>"
    return Response(200, f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{contents}{more}'
                         f"</ListBucketResult>")


def test_list_keys_follows_pages_and_fails_cleanly():
    pages = {None: listing([("a.nc", 1)], token="T"), "T": listing([("b.nc", 2)])}
    asked = []

    def get(url, params):
        asked.append(params.get("continuation-token"))
        return pages[params.get("continuation-token")]
    assert goes.list_keys("18", "ABI-L2-ACMF/2026/283/19/", get) == [("a.nc", 1), ("b.nc", 2)]
    assert asked == [None, "T"]
    with pytest.raises(ConnectionFailed, match="HTTP 503"):
        goes.list_keys("18", "x/", lambda url, params: Response(503, "Slow Down"))


# ---------- geometry and daylight ----------
def test_fixed_grid_round_trip():
    grid = goes.FixedGrid(PROJECTION, [0, 1], [0, 1])
    lat, lon = grid.lat_lon(*grid.scan_angles(LAT, LON))
    assert lat == pytest.approx(LAT, abs=1e-6) and lon == pytest.approx(LON, abs=1e-6)


def test_sun_rise_set_close_to_pvlib():
    sunrise, sunset = goes.sun_rise_set(datetime.date(2026, 10, 10), LAT, LON)
    # pvlib's SPA for the site that day: 13:51:41 and 00:55:56 UTC
    assert abs(sunrise - datetime.datetime(2026, 10, 10, 13, 51, 41, tzinfo=UTC)) < datetime.timedelta(minutes=2)
    assert abs(sunset - datetime.datetime(2026, 10, 11, 0, 55, 56, tzinfo=UTC)) < datetime.timedelta(minutes=2)
    assert goes.sun_rise_set(datetime.date(2026, 12, 21), 80.0, 0.0) is None          # polar night
    assert goes.in_daylight(datetime.datetime(2026, 12, 21, 3, tzinfo=UTC), 80.0, 0.0, "UTC", 60)


# ---------- cubes ----------
def test_build_cubes_cuts_the_box_in_time_order(tmp_path):
    bucket = FakeBucket(tmp_path, DAY_SCANS)
    (result,) = build(tmp_path, bucket)
    assert (result.product, result.scans, result.added) == ("ACMF", 3, 3)
    # 07:00Z is 01:00 MDT on 10-10, so all three are in 10-10's cube.
    path = tmp_path / "cubes" / "goes18_ACMF_2026-10-10.h5"
    assert result.cubes == [path]
    with h5py.File(write_scan(tmp_path / "ref.nc", 0), "r") as ref:
        rows, cols = goes.box_window(goes.FixedGrid.from_netcdf(ref), LAT, LON, KM)
    with h5py.File(path, "r") as cube:
        assert sorted(k for k in cube) == ["ACM", "Cloud_Probabilities", "DQF", "time", "x", "y"]   # no band_id
        assert list(cube["time"][:]) == [goes.nanoseconds(t) for t in sorted(DAY_SCANS)]
        assert (cube.attrs["row0"], cube.attrs["col0"]) == (rows.start, cols.start)
        assert cube.attrs["center_lat"] == LAT and cube.attrs["box_half_km"] == KM
        assert cube.attrs["product"] == "ACMF" and cube.attrs["satellite"] == "GOES-18"
        assert "NAME" not in cube["ACM"].attrs and list(cube["ACM"].attrs["flag_values"]) == [0, 1, 2, 3]
        assert cube["ACM"].chunks == (1, rows.stop - rows.start, cols.stop - cols.start)
        # Scans in time order: 07:00 is file 1 (DQF 1), 19:00 is file 3, 19:10 is file 2.
        assert [int(cube["DQF"][i, 0, 0]) for i in range(3)] == [1, 3, 2]
        for i, n in enumerate((1, 3, 2)):
            with h5py.File(bucket.files[key_for("ACMF", sorted(DAY_SCANS)[i])], "r") as src:
                assert np.array_equal(cube["Cloud_Probabilities"][i], src["Cloud_Probabilities"][rows, cols])
                assert int(src["DQF"][0, 0]) == n
    (again,) = build(tmp_path, bucket)
    assert again.added == 0                                    # already there: nothing fetched


def test_daylight_skips_night_scans(tmp_path):
    (result,) = build(tmp_path, FakeBucket(tmp_path, DAY_SCANS), daylight_minutes=60)
    assert (result.scans, result.added) == (2, 2)               # 01:00 MDT is out; 13:00 and 13:10 MDT are in


def test_a_shifted_grid_is_refused(tmp_path):
    bucket = FakeBucket(tmp_path, DAY_SCANS[:1])
    build(tmp_path, bucket)
    later = datetime.datetime(2026, 10, 10, 19, 0, 22, tzinfo=UTC)
    bucket.files[key_for("ACMF", later)] = write_scan(tmp_path / "shifted.nc", 9, shift=STEP / 3)
    with pytest.raises(SigenError, match="grid doesn't match"):
        build(tmp_path, bucket)


# ---------- CLI ----------
@pytest.fixture
def site_config(tmp_path):
    f = tmp_path / "site.json"
    f.write_text(json.dumps({"site": {"latitude": LAT, "longitude": LON, "timezone": TZ}}), encoding="utf-8")
    return f


def test_cli_builds_a_day(run, tmp_path, site_config, monkeypatch):
    bucket = FakeBucket(tmp_path, DAY_SCANS)
    monkeypatch.setattr(goes, "list_keys", bucket.lister)
    real_fetch = goes.fetch_box
    monkeypatch.setattr(goes, "fetch_box", lambda *a, opener=None: real_fetch(*a, opener=bucket.opener))
    code, out, err = run("satellite", "--config", site_config, "--day", "2026-10-10", "--all-day", "--km", KM,
                         "--workers", 1, "--products", "ACM", "--cube-dir", tmp_path / "c")
    assert code == ExitCode.OK and "ACMF: 3 scans, 3 added" in out and "America/Edmonton" in err
    # The local day 2026-10-10 MDT is 06:00Z to 06:00Z next day: listed hour by hour.
    assert bucket.listed[0] == "ABI-L2-ACMF/2026/283/06/" and len(bucket.listed) == 24
    assert (tmp_path / "c" / "goes18_ACMF_2026-10-10.h5").exists()


def test_cli_usage_and_network_errors(run, tmp_path, site_config, monkeypatch):
    code, _, err = run("satellite", "--config", site_config, "--start", "2026-10-10 06:00")
    assert code == ExitCode.USAGE and "--start and --end together" in err
    code, _, err = run("satellite", "--config", site_config, "--day", "10/10/2026")
    assert code == ExitCode.USAGE and "isn't a date" in err
    code, _, err = run("satellite", "--day", "2026-10-10")                     # the test config has no site
    assert code == ExitCode.USAGE and "no site" in err

    def unreachable(satellite, prefix):
        raise ConnectionFailed("listing failed: no response")
    monkeypatch.setattr(goes, "list_keys", unreachable)
    code, _, err = run("satellite", "--config", site_config, "--day", "2026-10-10", "--cube-dir", tmp_path / "c")
    assert code == ExitCode.CONNECTION and "listing failed" in err
