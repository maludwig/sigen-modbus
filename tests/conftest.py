import functools
import os
import shutil
import socketserver
import threading
import uuid
from pathlib import Path

import pytest

from sigen.fake_server import start_in_thread

REPO = Path(__file__).resolve().parent.parent
REAL_DATA = REPO / "fake_data" / "real-2026-10-06"
MIDSOC_DATA = REPO / "fake_data" / "real-2026-10-07-midsoc"   # 55.4 %, not pinned at full
FULL_DATA = REPO / "fake_data" / "real-2026-10-07-full"        # every plant register (137)
BOTH_DATA = REPO / "fake_data" / "real-2026-10-08-both-units"  # plant (247) + inverter (unit 1)


def pytest_configure(config):
    config.addinivalue_line("markers", "fresh_schema: the database test gets a new, empty schema of its own "
                                       "(not yet migrated) instead of the shared migrated one")


# ---------- the test database (the fixtures that use these are in test_db.py) ----------
# A commit doesn't wait for the disk (seconds, at times, on a Raspberry Pi's SD card). A crash could lose the last few
# commits, which doesn't matter in a throwaway schema.
FAST_COMMITS = "-c synchronous_commit=off"
KEPT_TABLES = ("schema_migrations", "register_meta")     # what a migration leaves in them is part of the schema


def base_database_url():
    """SIGEN_TEST_DB_URL, or database.test_url from the repo's config.json, or None."""
    from sigen.config import load_config
    from sigen.errors import SigenError
    if os.environ.get("SIGEN_TEST_DB_URL"):
        return os.environ["SIGEN_TEST_DB_URL"]
    if (REPO / "config.json").exists():
        try:
            return load_config(REPO / "config.json").test_database_url
        except SigenError:
            return None
    return None


def new_schema(base: str) -> str:
    import psycopg
    schema = f"sigen_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True, options=FAST_COMMITS) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
    return schema


def drop_schema(base: str, schema: str) -> None:
    import psycopg
    with psycopg.connect(base, autocommit=True, options=FAST_COMMITS) as c:
        c.execute(f'DROP SCHEMA "{schema}" CASCADE')


def schema_url(base: str, schema: str) -> str:
    import psycopg
    return psycopg.conninfo.make_conninfo(base, options=f"-c search_path={schema} {FAST_COMMITS}")


def fingerprint(conn) -> tuple:
    """The schema's shape: its tables, views, indexes, sequences and functions, every column, the migrations applied
    and register_meta. Not the other tables' rows."""
    def rows(query):
        return tuple(conn.execute(query).fetchall())
    return (rows("SELECT relname, relkind FROM pg_class WHERE relnamespace = current_schema()::regnamespace "
                 "ORDER BY relname"),
            rows("SELECT table_name, ordinal_position, column_name, data_type FROM information_schema.columns "
                 "WHERE table_schema = current_schema() ORDER BY table_name, ordinal_position"),
            rows("SELECT proname FROM pg_proc WHERE pronamespace = current_schema()::regnamespace ORDER BY proname"),
            rows("SELECT * FROM schema_migrations ORDER BY version"),
            rows("SELECT * FROM register_meta ORDER BY address"))


def empty_tables(conn) -> None:
    """Delete every row (but KEPT_TABLES') and restart the sequences. DELETE and setval rather than TRUNCATE and
    ALTER SEQUENCE … RESTART: those give a table new files, and the old ones linger until the next checkpoint, which
    slows pg_database_size (db status) a little more with every test."""
    import psycopg
    from psycopg import sql
    tables = [t for t, in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
                                       "ORDER BY tablename") if t not in KEPT_TABLES]
    for _ in range(len(tables)):              # a table another refers to empties once that one has
        left = []
        for table in tables:
            try:
                conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
            except psycopg.errors.ForeignKeyViolation:
                left.append(table)
        tables = left
        if not tables:
            break
    if tables:
        raise RuntimeError(f"couldn't empty {', '.join(tables)}")
    conn.execute("SELECT setval(format('%I', sequencename)::regclass, start_value, false) FROM pg_sequences "
                 "WHERE schemaname = current_schema()")


class SharedSchema:
    """One migrated schema shared by the database tests, so each doesn't pay for migrating its own (see db_url). After
    each test it's emptied; if the test changed the schema itself, it's built again from scratch."""

    def __init__(self, base: str):
        self.base = base
        self.rebuilds = 0
        self._build()

    def _build(self) -> None:
        from sigen import db
        self.schema = new_schema(self.base)
        self.url = schema_url(self.base, self.schema)
        with db.connect(self.url) as conn:
            db.migrate(conn)
            self.shape = fingerprint(conn)

    def reset(self) -> None:
        from sigen import db
        with db.connect(self.url) as conn:
            conn.execute("SET lock_timeout = '10s'")      # a connection a test left open fails here, not hangs
            empty_tables(conn)
            changed = fingerprint(conn) != self.shape
        if changed:
            self.drop()
            self.rebuilds += 1
            self._build()

    def drop(self) -> None:
        drop_schema(self.base, self.schema)


@pytest.fixture(scope="session")
def shared_schema():
    """The SharedSchema, built the first time a database test asks for it; None without a test database."""
    base = base_database_url()
    if not base:
        yield None
        return
    shared = SharedSchema(base)
    yield shared
    shared.drop()


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
def hangs_up():
    """A localhost port whose server closes every connection at once: a database or ntfy server that's down,
    failing fast everywhere. A closed port won't do on Windows, which retries a refused connection for ~2 s
    (and libpq there waits out its whole connect_timeout, 10 s)."""
    class HangUp(socketserver.BaseRequestHandler):
        def handle(self):
            pass

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), HangUp)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def quick_polls(monkeypatch):
    """`sigen poll` without the waits between polls: they run back to back. The pacing itself is tested on its
    own (test_seconds_to_boundary)."""
    from sigen import poll
    monkeypatch.setattr(poll, "run", functools.partial(poll.run, sleep=lambda seconds: None))


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
