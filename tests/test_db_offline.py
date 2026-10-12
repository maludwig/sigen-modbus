"""Database code that doesn't need a database: value encoding, migration files, meta sync
planning, password redaction, the poll schedule, and `db query`'s table."""
import decimal
import json

import pytest

from conftest import FULL_DATA
from sigen import db
from sigen.cli import format_query_table
from sigen.errors import ExitCode
from sigen.poll import seconds_to_boundary
from sigen.reading import readings_from_document
from sigen.registers import BY_NAME, PLANT_REGISTERS


# ---------- stored values ----------
@pytest.mark.parametrize("name,words,expected", [
    ("plant_ess_power", [65535, 63536], -2000),                          # s32: decoded, sign kept, no gain
    ("plant_ess_soc", [945], 945),                                        # u16
    ("plant_system_timezone", [65116], -420),                             # s16
    ("plant_ess_max_charging_limit", [65535, 65535], 0xFFFFFFFF),         # u32 "not set" fits as-is
    ("plant_accumulated_pv_energy", [0, 0, 0, 3066], 3066),               # u64
    ("plant_accumulated_pv_energy", [65535, 65535, 65535, 65535], -1),    # u64 "not set" -> -1
])
def test_stored_value(name, words, expected):
    assert db.stored_value(BY_NAME[name], words) == expected


def test_values_for_full_capture():
    doc = json.loads((FULL_DATA / "sigenstor.json").read_text(encoding="utf-8"))
    values = dict(db.values_for(readings_from_document(doc)))
    assert len(values) == len(PLANT_REGISTERS)
    assert values[30014] == 880            # SOC 88.0 %
    assert values[30037] == -1859          # battery -1.859 kW (discharging)
    assert values[40032] == 0xFFFFFFFF     # charging limit "not set"


def test_values_for_refuses_partial_readings():
    with pytest.raises(ValueError, match="no raw words"):
        db.values_for({"plant_ess_soc": {"error": "Modbus exception 2"}})


# ---------- migrations ----------
def test_migration_files_load_in_order():
    migrations = db.load_migrations()
    assert [m.label for m in migrations][:1] == ["0001_initial"]
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert all("\r" not in m.sql for m in migrations)     # CRLF checkouts checksum the same


def test_checksum_changes_with_content():
    a = db.Migration(1, "initial", "CREATE TABLE t (x int);\n")
    assert a.checksum == db.Migration(1, "initial", "CREATE TABLE t (x int);\n").checksum
    assert a.checksum != db.Migration(1, "initial", "CREATE TABLE t (x bigint);\n").checksum


# ---------- register_meta sync planning ----------
REG = BY_NAME["plant_ess_power"]
ROW = (REG.name, REG.type, float(REG.gain), REG.unit, REG.device)


def test_meta_plan_fresh_inserts_everything_stored():
    plan = db.plan_meta_sync({}, lambda a: False)
    assert len(plan.inserts) == len(db.STORED_REGISTERS) and not plan.updates and not plan.problems
    assert {r.device for r in plan.inserts} == {"plant", "inverter"}
    assert not any(r.type == "string" for r in plan.inserts)        # display only, never stored


def test_meta_plan_device_change_is_an_update():
    plan = db.plan_meta_sync({REG.address: ROW[:4] + ("inverter",)}, lambda a: True, [REG])
    assert plan.updates == [REG] and not plan.problems


def test_meta_plan_in_sync():
    assert db.plan_meta_sync({REG.address: ROW}, lambda a: True, [REG]).in_sync


def test_meta_plan_cosmetic_changes_are_updates_even_with_values():
    plan = db.plan_meta_sync({REG.address: ("old_name", REG.type, 10.0, "W", "plant")}, lambda a: True, [REG])
    assert plan.updates == [REG] and not plan.problems


def test_meta_plan_type_change():
    other = ("plant_ess_power", "u32", float(REG.gain), REG.unit, "plant")
    assert db.plan_meta_sync({REG.address: other}, lambda a: False, [REG]).updates == [REG]   # no data yet
    plan = db.plan_meta_sync({REG.address: other}, lambda a: True, [REG])                     # data stored
    assert not plan.updates and "write a migration" in plan.problems[0]


def test_meta_plan_ignores_addresses_only_in_the_database():
    assert db.plan_meta_sync({REG.address: ROW, 39999: ("gone", "u16", 1.0, None, "plant")},
                             lambda a: True, [REG]).in_sync


def test_values_for_skips_strings():
    from sigen.block_reader import entry_for
    from sigen.registers import encode_string
    model, soc = BY_NAME["inverter_model_type"], BY_NAME["inverter_ess_soc"]
    readings = {model.name: entry_for(model, encode_string("SigenStor EC SP US", 15)),
                soc.name: entry_for(soc, [313])}
    assert db.values_for(readings) == [(30601, 313)]


# ---------- redaction ----------
@pytest.mark.parametrize("url,expected", [
    ("postgresql://postgres:s3cret@localhost:5432/sigen", "postgresql://postgres:***@localhost:5432/sigen"),
    ("postgresql://postgres@localhost/sigen", "postgresql://postgres@localhost/sigen"),
    ("postgresql://localhost/sigen?user=u&password=s3cret", "postgresql://localhost/sigen?user=u&password=***"),
    ("host=localhost password=s3cret dbname=sigen", "host=localhost password=*** dbname=sigen"),
    ("host=localhost password='s 3cret' dbname=sigen", "host=localhost password=*** dbname=sigen"),
])
def test_redact(url, expected):
    assert db.redact(url) == expected


# ---------- schedule ----------
def test_seconds_to_boundary():
    assert 1_000_020 % 60 == 0
    assert seconds_to_boundary(60, 1_000_040.5) == pytest.approx(39.5)     # 20.5 s past the minute
    assert seconds_to_boundary(60, 1_000_020.0) == pytest.approx(60.0)     # exactly on it: next one


# ---------- CLI without a database ----------
def test_db_commands_need_a_database_url(run, tmp_path):
    f = tmp_path / "config.json"
    f.write_text('{"smart_loads": []}', encoding="utf-8")
    for argv in (["db", "migrate", "--config", f], ["db", "status", "--config", f],
                 ["db", "query", "SELECT 1", "--config", f],
                 ["poll", "--ip", "127.0.0.1", "--config", f, "--count", 1]):
        code, _, err = run(*argv)
        assert code == ExitCode.USAGE and "no database URL" in err


def test_unreachable_database_is_exit_10(run, tmp_path):
    pytest.importorskip("psycopg")
    f = tmp_path / "config.json"
    # Port 1 on localhost: nothing listens there, so this fails fast without any real server.
    f.write_text(json.dumps({"database": {"url": "postgresql://u:s3cret@127.0.0.1:1/x"}}), encoding="utf-8")
    code, _, err = run("db", "status", "--config", f)
    assert code == ExitCode.DATABASE and "s3cret" not in err and "u:***@" in err


def test_query_table():
    table = format_query_table(["id", "name", "gain"],
                               [(7, "plant_ess_soc", decimal.Decimal("10")), (12345, None, 1000.0)])
    assert table.splitlines() == [
        "id    | name          | gain",
        "------+---------------+-------",
        "    7 | plant_ess_soc |     10",
        "12345 | NULL          | 1000.0",
    ]


def test_query_table_shows_line_breaks_and_cuts_long_cells():
    table = format_query_table(["error"], [("HTTP 429\n" + "x" * 100,)], max_width=20)
    assert table.splitlines()[2] == "HTTP 429\\nxxxxxxxxx…"
    assert len(format_query_table(["error"], [("y" * 100,)], max_width=0).splitlines()[2]) == 100


def test_query_table_with_no_rows():
    assert format_query_table(["a", "b"], []) == "a | b\n--+--"


def test_query_options_are_validated(run):
    with pytest.raises(SystemExit) as e:
        run("db", "query", "SELECT 1", "--config", "x.json", "--max-rows", "-1")
    assert e.value.code == 2


def test_poll_interval_validation(run):
    with pytest.raises(SystemExit) as e:
        run("poll", "--ip", "127.0.0.1", "--config", "x.json", "--interval", "0.5")
    assert e.value.code == 2
