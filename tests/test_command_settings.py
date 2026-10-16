"""The config's "poll" and "dashboard" sections: checked like the rest of the config, and merged under the command
line (an option given wins, then the config, then the built-in default), with poll printing each setting and where
it came from."""
import json

import pytest

from sigen import cli
from sigen.config import Config, load_config
from sigen.errors import ConfigError, ExitCode


def write(tmp_path, doc):
    f = tmp_path / "config.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    return f


def test_loads_the_sections(tmp_path):
    cfg = load_config(write(tmp_path, {"poll": {"interval": 15, "offset": 7, "weather": True, "alerts": False,
                                                "capture_radar": "captures/radar"},
                                       "dashboard": {"host": " 127.0.0.1 ", "port": 9000, "reload": True}}))
    assert cfg.commands["poll"] == {"interval": 15.0, "offset": 7.0, "weather": True, "alerts": False,
                                    "capture_radar": tmp_path.resolve() / "captures/radar"}   # relative to the config
    assert cfg.commands["dashboard"] == {"host": "127.0.0.1", "port": 9000, "reload": True}
    assert load_config(write(tmp_path, {})).commands == {}


@pytest.mark.parametrize("doc, match", [
    ({"poll": []}, "must be an object"),
    ({"poll": {"wether": True}}, r"unknown setting\(s\) wether; known: interval, offset, weather"),
    ({"poll": {"count": 5}}, "unknown setting"),                       # --count is for trying things out
    ({"poll": {"weather": "yes"}}, "poll.weather must be true or false"),
    ({"poll": {"alerts": 1}}, "poll.alerts must be true or false"),
    ({"poll": {"interval": 0.5}}, "poll.interval must be a number from 1"),
    ({"poll": {"interval": True}}, "poll.interval must be a number"),
    ({"poll": {"offset": -1}}, "poll.offset must be a number from 0"),
    ({"poll": {"capture_radar": ""}}, "non-empty string"),
    ({"dashboard": {"port": 70000}}, "dashboard.port must be a whole number from 1 to 65535"),
    ({"dashboard": {"port": 80.5}}, "whole number"),
    ({"dashboard": {"host": 5}}, "non-empty string"),
    ({"set": {"apply": True}}, "unknown key"),                         # writes never take settings from a file
])
def test_rejects_bad_sections(tmp_path, doc, match):
    with pytest.raises(ConfigError, match=match):
        load_config(write(tmp_path, doc))


def merged(argv, config):
    args = cli.build_parser().parse_args(argv)
    settings = cli.POLL_SETTINGS if argv[0] == "poll" else cli.DASHBOARD_SETTINGS
    return args, {option: (text, source) for option, text, source in cli._settings(args, config, argv[0], settings)}


def test_the_command_line_wins_then_the_config_then_the_default(tmp_path):
    cfg = load_config(write(tmp_path, {"poll": {"interval": 15, "weather": True, "alerts": True},
                                       "dashboard": {"port": 9000}}))
    args, shown = merged(["poll"], cfg)
    assert (args.interval, args.offset, args.weather, args.radar, args.alerts) == (15.0, 0.0, True, False, True)
    assert shown["--interval"] == ("15", "config") and shown["--offset"] == ("0", "default")
    assert shown["--weather"] == ("on", "config") and shown["--capture-radar"] == ("(none)", "default")
    args, shown = merged(["poll", "--no-weather", "--interval", "30", "--radar"], cfg)
    assert (args.interval, args.weather, args.radar, args.alerts) == (30.0, False, True, True)
    assert shown["--weather"] == ("off", "command line") and shown["--radar"] == ("on", "command line")
    args, shown = merged(["dashboard", "--reload"], cfg)
    assert (args.host, args.port, args.reload) == ("0.0.0.0", 9000, True)
    assert shown == {"--host": ("0.0.0.0", "default"), "--port": ("9000", "config"), "--reload": ("on", "command line")}


def test_without_a_config_everything_is_the_built_in_default():
    args, shown = merged(["poll"], Config(origin="--no-config"))
    assert (args.interval, args.offset, args.weather, args.radar, args.alerts) == (60.0, 0.0, False, False, False)
    assert {source for _, source in shown.values()} == {"default"}


def test_poll_checks_settings_from_the_config_too(run, tmp_path):
    f = write(tmp_path, {"database": {"url": "postgresql://nobody@127.0.0.1:1/none"}, "poll": {"interval": 15,
                                                                                               "offset": 15}})
    code, _, err = run("poll", "--ip", "127.0.0.1", "--config", f, "--count", 1)
    assert code == ExitCode.USAGE and "--offset must be at least 0 and less than --interval (15)" in err
    assert f"Settings (config: {f}):" in err and ["--offset", "15", "config"] in [line.split() for line in
                                                                               err.splitlines()]
    code, _, err = run("poll", "--ip", "127.0.0.1", "--config", write(tmp_path, {
        "database": {"url": "postgresql://nobody@127.0.0.1:1/none"}, "poll": {"capture_radar": "x"}}), "--count", 1)
    assert code == ExitCode.USAGE and "--capture-radar needs --radar" in err


# ---------- poll against the test database ----------
psycopg = pytest.importorskip("psycopg")
from test_db import config, conn, db_url, net, url_of  # noqa: E402,F401  (fixtures; never request db_url in a test)


def test_poll_prints_each_setting_and_where_it_came_from(run, tmp_path, config, full_server, quick_polls):
    f = write(tmp_path, {"database": {"url": url_of(config)}, "poll": {"interval": 1, "weather": False}})
    run("db", "migrate", "--config", f)
    code, out, err = run("poll", *net(full_server), "--config", f, "--count", 1, "--no-alerts")
    assert code == ExitCode.OK and "1 stored, 0 failed" in out
    lines = err.splitlines()
    start = lines.index(f"Settings (config: {f}):")
    assert [line.split() for line in lines[start + 1:start + 8]] == [
        ["--interval", "1", "config"], ["--offset", "0", "default"], ["--weather", "off", "config"],
        ["--radar", "off", "default"], ["--alerts", "off", "command", "line"],
        ["--capture-open-meteo-weather", "(none)", "default"], ["--capture-radar", "(none)", "default"]]
