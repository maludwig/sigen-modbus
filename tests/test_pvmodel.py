"""The config's "array" section and sigen.pvmodel, the clear-sky model."""

import json

import pandas as pd
import pytest

from sigen.config import ConfigError, load_config

# A made-up hip roof facing the four compass points, one string per face.
ARRAY = {
    "panel": {"kw": 0.5, "gamma_pct_per_c": -0.28, "mount": "close_mount_glass_glass"},
    "albedo": 0.2,
    "faces": {
        "south": {"tilt": 20, "azimuth": 180},
        "east": {"tilt": 20, "azimuth": 90},
        "west": {"tilt": 20, "azimuth": 270},
        "north": {"tilt": 20, "azimuth": 0},
    },
    "strings": {
        "1": {"panels": 10, "face": "south"},
        "2": {"panels": 8, "face": "east"},
        "3": {"panels": 8, "face": "west"},
        "4": {"panels": 6, "face": "north"},
    },
}
SITE = {"latitude": 51.0443, "longitude": -114.0631, "timezone": "America/Edmonton", "altitude_m": 1045}


def config_with(tmp_path, **sections):
    f = tmp_path / "config.json"
    f.write_text(json.dumps(sections), encoding="utf-8")
    return load_config(f)


def test_array_section(tmp_path):
    c = config_with(tmp_path, site=SITE, array=ARRAY)
    assert c.site.altitude_m == 1045 and c.array.kw(1) == 5.0 and c.array.strings[2].face == "east"
    assert list(c.array.strings) == [1, 2, 3, 4] and c.array.faces["west"].azimuth == 270
    assert config_with(tmp_path, site={k: v for k, v in SITE.items() if k != "altitude_m"}).site.altitude_m is None
    bad = [
        {**ARRAY, "albedo": 2},
        {**ARRAY, "strings": {"1": {"panels": 9, "face": "shed"}}},
        {**ARRAY, "strings": {"one": {"panels": 9, "face": "south"}}},
        {**ARRAY, "faces": {}},
        {**ARRAY, "panel": {"kw": 0.5}},
        {k: v for k, v in ARRAY.items() if k != "albedo"},
    ]
    for array in bad:
        with pytest.raises(ConfigError, match="array"):
            config_with(tmp_path, array=array)


pvmodel = pytest.importorskip("sigen.pvmodel")


@pytest.fixture
def model(tmp_path):
    c = config_with(tmp_path, site=SITE, array=ARRAY)
    return pvmodel.Model(c.site, c.array)


def test_whole_array_clear_sky_day(model):
    """A cloudless October day. The total is pinned so that any change to the model shows up here: it should
    only move on purpose."""
    day = pd.Timestamp("2026-10-10", tz="America/Edmonton")
    times = pd.date_range(day, day + pd.Timedelta(days=1), freq="1min", inclusive="left")
    strings = model.strings_clear_sky(times)
    assert strings.sum().sum() / 60 == pytest.approx(52.06, abs=0.05)
    same = sum(
        model.clear_sky_kw(day, f.tilt, f.azimuth, 1.0) * model.array.kw(n)
        for n, s in model.array.strings.items()
        for f in [model.array.faces[s.face]]
    )
    assert same.sum() / 60 == pytest.approx(strings.sum().sum() / 60)  # face by face agrees
    noon = strings.sum(axis=1).idxmax()
    assert noon.hour in (12, 13) and strings.loc[noon, 4] < strings.loc[noon, 2] < strings.loc[noon, 1]
    morning, evening = strings.loc[day + pd.Timedelta(hours=9)], strings.loc[day + pd.Timedelta(hours=17)]
    assert morning[2] > morning[3] and evening[3] > evening[2]  # east in the morning, west later


def test_a_day_runs_to_the_next_local_midnight_across_a_clock_change(model):
    day = pd.Timestamp("2026-10-25", tz="Europe/London")  # the clocks go back: a 25-hour day
    kw = model.clear_sky_kw(day, 20, 180, 1.0, freq="1h")
    assert len(kw) == 25 and kw.index[-1] == pd.Timestamp("2026-10-25 23:00", tz="Europe/London")


def test_weather_changes_the_answer(model):
    day = pd.Timestamp("2026-10-10", tz="America/Edmonton")
    times = pd.date_range(day, day + pd.Timedelta(days=1), freq="5min", inclusive="left")
    mild = model.strings_clear_sky(times).sum().sum()
    cold = (
        model.strings_clear_sky(times, temp_air=pd.Series(-15.0, index=times), wind_speed=pd.Series(5.0, index=times))
        .sum()
        .sum()
    )
    assert cold > mild * 1.03  # cold panels make more
