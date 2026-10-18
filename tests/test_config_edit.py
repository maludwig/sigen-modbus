"""`sigen config show/get/set/unset`: values typed from JSON, every change checked before it's written, a backup,
the file's mode kept, and secrets never printed unless asked."""

import io
import json
import os
import stat
import sys

import pytest

from sigen import config_edit
from sigen.errors import ConfigError, ExitCode

PASSWORD = "s3cret-pw"
TOPIC_TAIL = "1a2b3c4d"
KEY = "the-apps-static-key"
DOC = {
    "database": {"url": f"postgresql://sigen:{PASSWORD}@localhost:5432/sigen"},
    "alerts": {"ntfy_topic": f"someone-solar-{TOPIC_TAIL}"},
    "cloud": {"token_file": "token.json", "obfuscation_key": KEY},
    "smart_loads": [{"slot": 7, "name": "Basement Plugs"}],
    "poll": {"interval": 15, "weather": True},
}


@pytest.fixture
def cfg(tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps(DOC, indent=2), encoding="utf-8")
    return f


def read(f):
    return json.loads(f.read_text(encoding="utf-8"))


def no_secrets(text):
    return PASSWORD not in text and TOPIC_TAIL not in text and KEY not in text


def test_parse_value():
    assert config_edit.parse_value("15") == 15 and config_edit.parse_value("1.5") == 1.5
    assert config_edit.parse_value("true") is True and config_edit.parse_value("null") is None
    assert config_edit.parse_value('{"ip": "192.168.1.5"}') == {"ip": "192.168.1.5"}
    assert config_edit.parse_value("America/Edmonton") == "America/Edmonton"  # not JSON: a string
    assert config_edit.parse_value("15", as_string=True) == "15"


def test_show_masks_every_secret(run, cfg):
    code, out, err = run("config", "show", "--config", cfg)
    assert code == ExitCode.OK and f"{cfg}: OK" in err
    assert no_secrets(out) and no_secrets(err)
    shown = json.loads(out)
    assert shown["database"]["url"] == "postgresql://sigen:***@localhost:5432/sigen"
    assert shown["alerts"]["ntfy_topic"] == "someone-solar-********"
    assert shown["cloud"] == {"token_file": "token.json", "obfuscation_key": "********"}
    assert shown["poll"] == DOC["poll"]


def test_show_says_what_is_wrong_but_still_shows_it(run, cfg):
    cfg.write_text(json.dumps({"poll": {"interval": 0}}), encoding="utf-8")
    code, out, err = run("config", "show", "--config", cfg)
    assert code == ExitCode.USAGE and "poll.interval" in err and json.loads(out) == {"poll": {"interval": 0}}


def test_get(run, cfg):
    assert run("config", "get", "poll.interval", "--config", cfg)[:2] == (ExitCode.OK, "15\n")
    assert run("config", "get", "smart_loads.0.name", "--config", cfg)[:2] == (ExitCode.OK, "Basement Plugs\n")
    code, out, _ = run("config", "get", "poll", "--config", cfg)
    assert json.loads(out) == DOC["poll"]
    code, out, _ = run("config", "get", "database", "--config", cfg)
    assert no_secrets(out)  # a section's secrets are masked too
    code, out, _ = run("config", "get", "database.url", "--config", cfg, "--reveal")
    assert out.strip() == DOC["database"]["url"]
    code, _, err = run("config", "get", "poll.radar", "--config", cfg)
    assert code == ExitCode.NOT_FOUND and "isn't set" in err


def test_set_types_the_value_and_keeps_a_backup(run, cfg):
    before = cfg.read_bytes()
    code, out, _ = run("config", "set", "poll.interval", "30", "--config", cfg)
    assert code == ExitCode.OK and "poll.interval: 15 -> 30" in out
    assert read(cfg)["poll"]["interval"] == 30  # a number, not "30"
    assert (cfg.parent / "config.json.bak").read_bytes() == before
    run("config", "set", "poll.weather", "false", "--config", cfg)
    run("config", "set", "site", '{"latitude": 51.04, "longitude": -114.06, "timezone": "UTC"}', "--config", cfg)
    run("config", "set", "site.timezone", "America/Edmonton", "--config", cfg)  # a new section's key, no quotes
    run("config", "set", "smart_loads.0.name", "Garage", "--config", cfg)
    doc = read(cfg)
    assert doc["poll"]["weather"] is False and doc["site"]["timezone"] == "America/Edmonton"
    assert doc["smart_loads"] == [{"slot": 7, "name": "Garage"}]
    assert doc["database"] == DOC["database"] and doc["cloud"] == DOC["cloud"]  # the rest untouched


@pytest.mark.parametrize(
    "key, value, match",
    [
        ("poll.interval", "0", "poll.interval"),  # out of range
        ("poll.weather", "yes", "poll.weather"),  # not a bool
        ("pole.interval", "15", "unknown key"),  # a typo'd section
        ("site.timezone", "America/Edmontn", "site"),  # a typo'd zone (and a site needs more than that)
        ("poll.interval.x", "1", "isn't a section"),
    ],
)
def test_set_refuses_what_wouldnt_load_and_writes_nothing(run, cfg, key, value, match):
    before = cfg.read_bytes()
    code, _, err = run("config", "set", key, value, "--config", cfg)
    assert code == ExitCode.USAGE and match in err
    assert cfg.read_bytes() == before and not (cfg.parent / "config.json.bak").exists()


def test_a_bad_value_already_in_the_file_can_be_fixed(run, cfg):
    cfg.write_text(json.dumps({"poll": {"interval": 0}}), encoding="utf-8")
    assert run("config", "set", "poll.interval", "15", "--config", cfg)[0] == ExitCode.OK
    assert read(cfg) == {"poll": {"interval": 15}}


def test_dry_run_and_no_change(run, cfg):
    before = cfg.read_bytes()
    code, out, _ = run("config", "set", "poll.interval", "60", "--config", cfg, "--dry-run")
    assert code == ExitCode.DRY_RUN and "15 -> 60" in out and cfg.read_bytes() == before
    code, out, _ = run("config", "set", "poll.interval", "15", "--config", cfg)
    assert code == ExitCode.OK and "already 15" in out and not (cfg.parent / "config.json.bak").exists()


def test_unset(run, cfg):
    code, out, _ = run("config", "unset", "poll.weather", "--config", cfg)
    assert code == ExitCode.OK and "poll.weather: true -> (not set)" in out
    assert read(cfg)["poll"] == {"interval": 15}
    code, _, err = run("config", "unset", "poll.radar", "--config", cfg)
    assert code == ExitCode.USAGE and "isn't set" in err


def test_a_secret_from_stdin_is_never_printed(run, cfg, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("someone-solar-9z8y7x6w\n"))
    code, out, err = run("config", "set", "alerts.ntfy_topic", "--stdin", "--config", cfg)
    assert code == ExitCode.OK and "9z8y7x6w" not in out + err and no_secrets(out + err)
    assert read(cfg)["alerts"]["ntfy_topic"] == "someone-solar-9z8y7x6w"
    code, _, err = run("config", "set", "alerts.ntfy_topic", f"x-{TOPIC_TAIL}", "--config", cfg)
    assert "shell history" in err  # a secret on the command line gets a warning


def test_value_or_stdin_not_both(run, cfg):
    assert run("config", "set", "poll.interval", "--config", cfg)[0] == ExitCode.USAGE
    assert run("config", "set", "poll.interval", "15", "--stdin", "--config", cfg)[0] == ExitCode.USAGE


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_file_keeps_its_mode(run, cfg):
    os.chmod(cfg, 0o640)
    run("config", "set", "poll.interval", "30", "--config", cfg)
    for f in (cfg, cfg.parent / "config.json.bak"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o640, f


def test_keys():
    with pytest.raises(ConfigError):
        config_edit.parse_key("poll..interval")
    with pytest.raises(ConfigError, match="existing list item"):
        config_edit.set_value(DOC, "smart_loads.3", {"slot": 8, "name": "Furnace"})
    assert config_edit.get(DOC, "smart_loads.9.name") is config_edit.MISSING
