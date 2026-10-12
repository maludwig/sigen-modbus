import shutil
from pathlib import Path

import pytest

from sigen.fake_server import start_in_thread

REPO = Path(__file__).resolve().parent.parent
REAL_DATA = REPO / "fake_data" / "real-2026-10-06"
MIDSOC_DATA = REPO / "fake_data" / "real-2026-10-07-midsoc"   # 55.4 %, not pinned at full
FULL_DATA = REPO / "fake_data" / "real-2026-10-07-full"        # every plant register (137)
BOTH_DATA = REPO / "fake_data" / "real-2026-10-08-both-units"  # plant (247) + inverter (unit 1)


@pytest.fixture(scope="session")
def empty_config_file(tmp_path_factory):
    f = tmp_path_factory.mktemp("config") / "empty-config.json"
    f.write_text("{}", encoding="utf-8")
    return f


@pytest.fixture(autouse=True)
def no_real_config(monkeypatch, empty_config_file):
    """Every test starts with an empty config (through $SIGEN_CONFIG), never the repo's
    config.json: a test that leaves out --config or --ip must not reach the real inverter.
    Tests of the search itself delete the variable."""
    monkeypatch.setenv("SIGEN_CONFIG", str(empty_config_file))


@pytest.fixture
def data_dir(tmp_path):
    """A private copy of the real-system capture, so tests can't touch the repo's data."""
    d = tmp_path / "fake_data"
    shutil.copytree(REAL_DATA, d)
    return d


def _server(data_dir, **kw):
    server = start_in_thread(data_dir, **kw)
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def server(data_dir):
    yield from _server(data_dir)


def _scenario_server(tmp_path, scenario):
    d = tmp_path / scenario.name
    shutil.copytree(scenario, d)
    yield from _server(d)


@pytest.fixture
def midsoc_server(tmp_path):
    yield from _scenario_server(tmp_path, MIDSOC_DATA)


@pytest.fixture
def full_server(tmp_path):
    yield from _scenario_server(tmp_path, FULL_DATA)


@pytest.fixture
def both_server(tmp_path):
    yield from _scenario_server(tmp_path, BOTH_DATA)


@pytest.fixture
def lying_server(data_dir):
    """Acknowledges writes but never stores them, so every read-back mismatches."""
    yield from _server(data_dir, ignore_writes=True)


@pytest.fixture
def rejecting_server(data_dir):
    """Answers every write with Modbus exception 04."""
    yield from _server(data_dir, reject_writes=True)


@pytest.fixture
def journal(tmp_path):
    return tmp_path / "journal.jsonl"


@pytest.fixture
def run(capsys):
    """Run the CLI in-process; returns (exit_code, stdout, stderr)."""
    from sigen.cli import main

    def _run(*argv):
        code = main([str(a) for a in argv])
        out = capsys.readouterr()
        return code, out.out, out.err
    return _run
