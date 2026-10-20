"""sigen.satellite (the shade straight into satellite_shade, no files), `sigen satellite` without --cube-dir, and
poll's satellite job, with NOAA faked: synthetic files for all three products, a fake bucket listing."""

import collections
import datetime
import json
import math

import aiohttp
import h5py
import numpy as np
import pytest

from sigen import db, gaps, goes, goes_shade, satellite
from sigen.config import Config, Site
from sigen.errors import ConnectionFailed, ExitCode
from test_db import config, conn, db_url, url_of  # noqa: F401  (pytest fixtures; never request db_url in a test)
from test_goes import KM, LAT, LON, TZ, key_for, write_scan

UTC = datetime.UTC
SITE = Site(LAT, LON, TZ)
DELAY = goes_shade.SAMPLE_DELAY
# 13:00, 13:10 and 12:50 MDT on 2026-10-10 (UTC day 283), and 01:00 MDT, in the night
DAY = [datetime.datetime(2026, 10, 10, h, m, 22, tzinfo=UTC) for h, m in ((19, 0), (19, 10), (18, 50))]
NIGHT = datetime.datetime(2026, 10, 10, 7, 0, 22, tzinfo=UTC)
START, END = datetime.datetime(2026, 10, 10, 6, tzinfo=UTC), datetime.datetime(2026, 10, 11, 6, tzinfo=UTC)
LATER = datetime.datetime(2026, 10, 13, tzinfo=UTC)  # both UTC days long over: their listings are final


def add_product(path, product, value):
    """Give a write_scan file the variable its product's shade reads: COD (CODF) or HT (ACHAF)."""
    rng = np.random.default_rng(100 + value)
    with h5py.File(path, "a") as f:
        shape = f["ACM"].shape
        if product == "CODF":
            d = f.create_dataset("COD", data=rng.integers(0, 3000, shape, dtype=np.uint16))
            d.attrs["scale_factor"], d.attrs["_FillValue"] = np.array([0.01]), np.array([65535], np.uint16)
        elif product == "ACHAF":
            d = f.create_dataset("HT", data=rng.integers(2000, 9000, shape, dtype=np.uint16))
            d.attrs["scale_factor"], d.attrs["_FillValue"] = np.array([1.0]), np.array([65535], np.uint16)
    return path


class Bucket:
    """Every product's file for each scan time, listed by prefix, opened from disk; it notes what was asked."""

    def __init__(self, tmp_path, times):
        self.files, self.listed, self.opened = {}, [], []
        for n, t in enumerate(times):
            for product in ("ACMF", "CODF", "ACHAF"):
                path = write_scan(tmp_path / f"{product}_{n}.nc", value=n + 1)
                self.files[key_for(product, t)] = add_product(path, product, n + 1)

    def lister(self, satellite_number, prefix):
        self.listed.append(prefix)
        return [(k, 1000) for k in self.files if k.startswith(prefix)]

    def opener(self, url):
        key = url.split(".amazonaws.com/", 1)[1]
        self.opened.append(key)
        return open(self.files[key], "rb")


@pytest.fixture
def bucket(tmp_path):
    return Bucket(tmp_path, [*DAY, NIGHT])


def listings_for(bucket, now=LATER):
    return satellite.Listings(lister=bucket.lister, now=lambda: now)


# ---------- listing and planning ----------
def test_one_listing_per_product_and_utc_day_kept_once_final(bucket):
    young = listings_for(bucket, now=datetime.datetime(2026, 10, 10, 20, tzinfo=UTC))
    ready = young.ready(START, END)
    assert sorted(ready) == sorted([*DAY, NIGHT])
    assert ready[DAY[0]] == {s: key_for(f"{s}F", DAY[0]) for s in ("ACM", "COD", "ACHA")}
    assert bucket.listed == [f"ABI-L2-{p}F/2026/{d}/" for d in (283, 284) for p in ("ACM", "COD", "ACHA")]
    young.ready(START, END)
    assert len(bucket.listed) == 12 and young.requests == 12  # neither day is over an hour gone: asked again
    final = listings_for(bucket)
    final.ready(START, END)
    final.ready(START, END)
    assert final.requests == 6  # kept


def test_a_scan_is_ready_only_with_all_three_products(bucket):
    del bucket.files[key_for("CODF", DAY[1])]  # optical depth comes last: not published yet
    assert DAY[1] not in listings_for(bucket).ready(START, END)


def test_the_plan_is_daylight_missing_scans_newest_first(bucket):
    ready = listings_for(bucket).ready(START, END)
    failures = collections.Counter({DAY[1] + DELAY: satellite.MAX_FAILURES})
    p = satellite.plan(ready, {DAY[0] + DELAY}, failures, SITE, 60)
    assert (p.ready, p.stored, p.given_up) == (3, 1, 1)  # the night scan isn't daylight
    assert [t0 for t0, _ in p.todo] == [DAY[2]]
    every = satellite.plan(ready, set(), collections.Counter({DAY[1] + DELAY: 2}), SITE, None)
    assert [t0 for t0, _ in every.todo] == [DAY[1], DAY[0], DAY[2], NIGHT]  # newest first; 2 failures: tried again


# ---------- the shade, from memory ----------
def test_the_shade_from_memory_matches_the_shade_from_cubes(tmp_path, bucket):
    cubes = tmp_path / "cubes"
    goes.build_cubes(
        lat=LAT,
        lon=LON,
        timezone=TZ,
        start=START,
        end=END,
        cube_dir=cubes,
        km=KM,
        workers=1,
        daylight_minutes=None,
        lister=bucket.lister,
        opener=bucket.opener,
    )
    from_cubes = goes_shade.shade_day(cubes, LAT, LON, datetime.date(2026, 10, 10))
    ready = listings_for(bucket).ready(START, END)
    for i, t0 in enumerate(sorted(ready)):
        one, fetched = satellite.shade_scan(t0, ready[t0], SITE, km=KM, opener=bucket.opener)
        assert one.times == [from_cubes.times[i]] and fetched == 0  # (a file opener counts no bytes)
        assert set(one.columns) <= set(from_cubes.columns)  # (a night scan has only the pixel overhead)
        for name in from_cubes.columns:
            np.testing.assert_allclose(one.column(name), [from_cubes.column(name)[i]], equal_nan=True)
    assert not math.isnan(from_cubes.column("trans_top")[sorted(ready).index(DAY[0])])  # a full row in daylight


# ---------- fill ----------
def rows(conn):
    return conn.execute(
        "SELECT scan_at, error, prob_0km IS NOT NULL, trans_0km IS NOT NULL, trans_top IS NOT NULL "
        "FROM satellite_shade ORDER BY id"
    ).fetchall()


def fill(conn, bucket, **kw):
    listings = kw.pop("listings", None) or listings_for(bucket)
    params = dict(km=KM, opener=bucket.opener, pause=0)
    params.update(kw)
    return satellite.fill(conn, listings, SITE, START, END, **params)


def test_fill_stores_the_missing_scans_newest_first(conn, bucket):
    lines = []
    result = fill(conn, bucket, log=lines.append)
    assert (result.stored, result.tried, result.left, result.failed, result.error) == (3, 3, 0, 0, None)
    assert [r[0] for r in rows(conn)] == [t + DELAY for t in sorted(DAY, reverse=True)]
    assert all(r[1] is None and r[2] and r[3] and r[4] for r in rows(conn))  # every column, all three products
    assert lines[0].startswith("2026-10-10 19:11Z: stored") and result.newest == DAY[1] + DELAY
    assert "3 scan(s) stored" in satellite.summarize(result)
    opened = len(bucket.opened)
    again = fill(conn, bucket)
    assert (again.stored, again.plan.stored, again.left) == (0, 3, 0) and len(bucket.opened) == opened
    assert "3 of 3 daylight scans stored, 0 still to fetch" in satellite.summarize(again)


def test_a_broken_scan_is_skipped_and_given_up_on(conn, bucket):
    broken = key_for("CODF", DAY[0])

    def opener(url):
        if url.endswith(broken):
            raise OSError("Unable to synchronously open file (file signature not found)")  # not an HDF5 file
        return bucket.opener(url)

    for attempt in range(satellite.MAX_FAILURES):
        result = fill(conn, bucket, opener=opener)
        assert result.failed == 1 and result.error is None
    assert result.stored == 0 and len(rows(conn)) == 2 + satellite.MAX_FAILURES  # the other two, the first time
    failed = [r for r in rows(conn) if r[1] is not None]
    assert {r[0] for r in failed} == {DAY[0] + DELAY} and "file signature not found" in failed[0][1]
    last = fill(conn, bucket, opener=opener)
    assert (last.tried, last.plan.given_up) == (0, 1) and "given up on" in satellite.summarize(last)


def test_noaa_unreachable_ends_the_run_with_one_failed_row(conn, bucket):
    def opener(url):
        raise aiohttp.ClientConnectionError("Cannot connect to host noaa-goes18.s3.amazonaws.com:443")

    result = fill(conn, bucket, opener=opener)
    assert (result.stored, result.tried) == (0, 0) and "Cannot connect" in result.error
    assert [(r[0], "Cannot connect" in r[1]) for r in rows(conn)] == [(None, True)]  # the run's: scan_at NULL

    def lister(satellite_number, prefix):
        raise ConnectionFailed("listing ABI-L2-ACMF/2026/283/ failed: HTTP 503")

    result = fill(conn, bucket, listings=satellite.Listings(lister=lister))
    assert result.plan is None and "HTTP 503" in result.error and len(rows(conn)) == 2


def test_one_run_at_a_time(conn, config, bucket):
    other = db.connect(url_of(config))
    try:
        assert db.try_lock(other, *satellite.LOCK)
        result = fill(conn, bucket)
        assert result.locked_out and bucket.listed == [] and "skipped" in satellite.summarize(result)
        db.unlock(other, *satellite.LOCK)
        assert fill(conn, bucket).stored == 3
        assert db.try_lock(other, *satellite.LOCK)  # the run let go of it
        db.unlock(other, *satellite.LOCK)
    finally:
        other.close()


def test_a_run_stops_when_told(conn, bucket):
    assert fill(conn, bucket, stop=lambda: True).stopped and rows(conn) == []
    result = fill(conn, bucket, wait=lambda seconds: True)  # stopped during the pause after the first scan
    assert (result.stored, result.left, result.stopped) == (1, 2, True)


# ---------- poll's job ----------
def test_the_filler_runs_on_the_ten_minute_boundary_plus_its_offset():
    def filler(wall):
        return satellite.Filler(SITE, "postgresql://unused", wall=lambda: wall, monotonic=lambda: 50.0)

    assert filler(600_000 + 100.0).first_deadline() == 50.0 + satellite.RUN_OFFSET - 100
    assert filler(600_000 + satellite.RUN_OFFSET).first_deadline() == 50.0 + satellite.RUN_EVERY  # on it: the next


def test_the_filler_fills_the_last_week(conn, config, bucket):
    out = []
    f = satellite.Filler(
        SITE,
        url_of(config),
        km=KM,
        listings=listings_for(bucket),
        opener=bucket.opener,
        out=out.append,
        now=lambda: END,
    )
    result = f.run_once(deadline=float("inf"))
    assert result.stored == 3 and len(rows(conn)) == 3 and result.left == 0
    assert f.run_once(deadline=float("inf")).stored == 0


def test_poll_satellite_is_a_config_setting_and_says_what_gaps_mean(conn, tmp_path):
    from sigen.config import load_config

    f = tmp_path / "c.json"
    f.write_text(json.dumps({"site": {"latitude": LAT, "longitude": LON, "timezone": TZ}, "poll": {"satellite": True}}))
    polled = load_config(f)
    assert polled.commands["poll"]["satellite"] is True
    (shade,) = [s for s in gaps.default_series(conn, polled, 15) if s.table == "satellite_shade"]
    assert shade.daylight_only  # the poll keeps it filled: a gap is a real gap
    unpolled = gaps.default_series(conn, Config(site=SITE), 15)
    assert "satellite_shade" not in {s.table for s in unpolled}  # nothing collects it here: no endless gap


# ---------- `sigen satellite` ----------
@pytest.fixture
def site_db_config(tmp_path, config):
    doc = json.loads(config.read_text(encoding="utf-8"))
    doc["site"] = {"latitude": LAT, "longitude": LON, "timezone": TZ}
    f = tmp_path / "site-db.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    return f


def test_cli_stores_what_the_database_is_missing(run, conn, site_db_config, bucket, monkeypatch):
    monkeypatch.setattr(goes, "list_keys", bucket.lister)
    real_fetch = goes.fetch_box
    monkeypatch.setattr(goes, "fetch_box", lambda *a, opener=None: real_fetch(*a, opener=bucket.opener))
    monkeypatch.setattr(satellite, "PAUSE", 0.0)
    args = ("satellite", "--config", site_db_config, "--day", "2026-10-10", "--km", KM)
    code, out, err = run(*args)
    assert code == ExitCode.OK and "3 scan(s) stored" in out and "the scans the database is missing" in err
    assert len(rows(conn)) == 3
    code, out, _ = run(*args)
    assert code == ExitCode.OK and "0 scan(s) stored; 3 of 3 daylight scans stored" in out
    other = db.connect(json.loads(site_db_config.read_text())["database"]["url"])
    try:
        db.try_lock(other, *satellite.LOCK)
        code, _, err = run(*args)
        assert code == ExitCode.PRECONDITION and "another" in err
    finally:
        other.close()
    code, _, err = run(*args, "--products", "ACM")
    assert code == ExitCode.USAGE and "go with --cube-dir" in err
