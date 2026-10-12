"""The --config file: loading, validation, and how names show up."""
import json

import pytest

from conftest import FULL_DATA, REPO
from sigen.config import Config, find_config, get_config, load_config
from sigen.errors import ConfigError, ExitCode, NotFound

SOURCE = FULL_DATA / "sigenstor.json"


def write(tmp_path, doc):
    f = tmp_path / "config.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    return f


def test_no_config_is_empty():
    assert load_config(None) == Config()


def test_example_config_is_valid():
    """config.example.json is what people copy to start: every section, and it must load."""
    c = load_config(REPO / "config.example.json")
    assert c.smart_load_names and c.database_url and c.test_database_url and c.alerts
    assert c.devices["inverter"].units == (1,) and c.site.altitude_m == 1045 and c.array.kw(1) == 5.0


def test_find_config_walks_up(tmp_path):
    f = write(tmp_path, {})
    deep = tmp_path / "notebooks" / "sub"
    deep.mkdir(parents=True)
    assert find_config(tmp_path) == f.resolve()
    assert find_config(deep) == f.resolve()
    (deep / "config.json").write_text("{}", encoding="utf-8")
    assert find_config(deep) == (deep / "config.json").resolve()      # the nearest wins


def no_config_above(path):
    if any((d / "config.json").is_file() for d in (path, *path.parents)):
        pytest.skip("a config.json sits above the temp directory")


def test_find_config_none(tmp_path):
    no_config_above(tmp_path)
    assert find_config(tmp_path) is None


def test_get_config_precedence(tmp_path, monkeypatch):
    found = write(tmp_path, {"smart_loads": [{"slot": 1, "name": "found"}]})
    named = tmp_path / "named.json"
    named.write_text(json.dumps({"smart_loads": [{"slot": 1, "name": "named"}]}), encoding="utf-8")
    env = tmp_path / "env.json"
    env.write_text(json.dumps({"smart_loads": [{"slot": 1, "name": "env"}]}), encoding="utf-8")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    monkeypatch.setenv("SIGEN_CONFIG", str(env))
    assert get_config(named).smart_load_names == {1: "named"}                 # --config wins
    assert get_config().smart_load_names == {1: "env"}                        # then $SIGEN_CONFIG
    assert get_config().origin == str(env)
    monkeypatch.setenv("SIGEN_CONFIG", "  ")                                  # blank = not set
    assert get_config().smart_load_names == {1: "found"}                      # then the search upward
    assert get_config().origin == str(found.resolve())


def test_get_config_with_nothing_found(tmp_path, monkeypatch):
    no_config_above(tmp_path)
    monkeypatch.delenv("SIGEN_CONFIG")
    monkeypatch.chdir(tmp_path)
    cfg = get_config()
    assert cfg == Config()
    with pytest.raises(ConfigError, match=r"no database URL \(no config file: .*no config\.json in"):
        cfg.require_database_url()


def test_get_config_named_file_must_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGEN_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(NotFound):
        get_config()
    with pytest.raises(NotFound):
        get_config(tmp_path / "also-missing.json")


def test_messages_say_which_config(tmp_path):
    f = write(tmp_path, {})
    with pytest.raises(ConfigError, match=r"no site \(.*config\.json\)"):
        load_config(f).require_site()


def test_loads_names(tmp_path):
    f = write(tmp_path, {"smart_loads": [{"slot": 7, "name": " Basement Plugs "}, {"slot": 8, "name": "Furnace"}]})
    cfg = load_config(f)
    assert cfg.smart_load_names == {7: "Basement Plugs", 8: "Furnace"}
    assert cfg.register_alias("plant_smart_load_7_power") == "Basement Plugs"
    assert cfg.register_alias("plant_smart_load_8_total_consumption") == "Furnace"
    assert cfg.register_alias("plant_smart_load_1_power") is None
    assert cfg.register_alias("plant_ess_soc") is None


@pytest.mark.parametrize("doc,match", [
    ([], "JSON object"),
    ({"smart_load": []}, "unknown key"),
    ({"smart_loads": {}}, "must be a list"),
    ({"smart_loads": [{"slot": 1}]}, "expected"),
    ({"smart_loads": [{"slot": 0, "name": "x"}]}, "1 to 24"),
    ({"smart_loads": [{"slot": 25, "name": "x"}]}, "1 to 24"),
    ({"smart_loads": [{"slot": "7", "name": "x"}]}, "whole number"),
    ({"smart_loads": [{"slot": True, "name": "x"}]}, "whole number"),
    ({"smart_loads": [{"slot": 7, "name": "  "}]}, "non-empty"),
    ({"smart_loads": [{"slot": 7, "name": "a"}, {"slot": 7, "name": "b"}]}, "named twice"),
])
def test_rejects_bad_config(tmp_path, doc, match):
    with pytest.raises(ConfigError, match=match):
        load_config(write(tmp_path, doc))


def test_missing_file_is_not_found(tmp_path):
    with pytest.raises(NotFound):
        load_config(tmp_path / "nope.json")


# ---------- through the CLI ----------
def test_read_json_uses_names(run, tmp_path):
    f = write(tmp_path, {"smart_loads": [{"slot": 7, "name": "Basement Plugs"}, {"slot": 2, "name": "Spare"}]})
    code, out, _ = run("read-json", "--data-file", SOURCE, "--config", f)
    assert code == ExitCode.OK
    assert "Smart loads:" in out
    assert "Basement Plugs (7)" in out and "0.77 kW" in out
    assert "Spare (2)" in out                                   # named slots show even when idle
    assert "slot 8" in out                                      # unnamed but in use
    assert "slot 9" not in out                                  # unnamed and never used
    assert "plant_smart_load_7_power (Basement Plugs)" in out   # the full table too


def test_summary_without_config_lists_used_slots(run):
    code, out, _ = run("read-json", "--data-file", SOURCE, "--summary-only")
    assert code == ExitCode.OK and "slot 7" in out and "slot 2" not in out


def test_read_with_config(run, full_server, tmp_path):
    f = write(tmp_path, {"smart_loads": [{"slot": 8, "name": "Furnace"}]})
    code, out, _ = run("read", "--ip", "127.0.0.1", "--port", full_server.port, "--config", f)
    assert code == ExitCode.OK and "Furnace (8)" in out


def test_bad_config_fails_before_touching_the_device(run, tmp_path):
    f = write(tmp_path, {"smart_loads": [{"slot": 99, "name": "x"}]})
    # Port 9 on localhost: if the config weren't checked first, this would be a connection error (3).
    code, _, err = run("capture", "--ip", "127.0.0.1", "--port", 9, "--timeout", 1,
                       "--out-dir", tmp_path / "caps", "--config", f)
    assert code == ExitCode.USAGE and "1 to 24" in err
    assert not (tmp_path / "caps").exists()
    code, _, _ = run("read-json", "--data-file", SOURCE, "--config", tmp_path / "missing.json")
    assert code == ExitCode.NOT_FOUND
