"""Every exit code, exercised end to end against the in-process fake server."""

import json
import socket

import pytest

from sigen.errors import ExitCode


def net(server):
    return ["--ip", "127.0.0.1", "--port", server.port]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------- read ----------
def test_read_summary(run, server):
    code, out, _ = run("read", *net(server))
    assert code == ExitCode.OK
    assert "Remote EMS" in out and "99.9%" in out


def test_read_summary_midsoc(run, midsoc_server):
    code, out, _ = run("read", *net(midsoc_server))
    assert code == ExitCode.OK
    assert "55.4%" in out and "8.80 / 15.88 kWh" in out


def test_read_json_is_replayable(run, full_server, tmp_path):
    code, out, _ = run("read", *net(full_server), "--json")
    assert code == ExitCode.OK
    doc = json.loads(out)
    assert doc["errors"] == {}  # every register in the map is in this capture
    assert doc["holding_registers"]["40046"]["raw"] == [200]
    assert doc["holding_registers"]["40032"]["value"] is None  # "not set" sentinel
    assert doc["input_registers"]["30158"]["value"] == 0.767  # smart load 7 power


def test_read_reports_unreadable_registers(run, server):
    # The 10-06 capture predates most of the map: read carries on and says so rather than failing.
    code, out, _ = run("read", *net(server))
    assert code == ExitCode.OK and "register(s) not readable" in out and "99.9%" in out


def test_read_connection_failure(run):
    code, _, err = run("read", "--ip", "127.0.0.1", "--port", free_port(), "--timeout", 0.2)
    assert code == ExitCode.CONNECTION and "couldn't connect" in err


def test_read_wrong_unit_times_out(run, server):
    # Like the real device, a unit ID with nothing behind it simply doesn't answer.
    code, _, err = run("read", *net(server), "--unit-id", 9, "--timeout", 0.2)
    assert code == ExitCode.CONNECTION and "timed out" in err


def test_read_uses_the_found_config_unless_no_config(run, server, tmp_path, monkeypatch):
    # A config found by itself (here via $SIGEN_CONFIG) lists inverter unit 1, which this plant-only
    # data doesn't have: the read times out, as it would against the real device. --no-config skips it.
    f = tmp_path / "found.json"
    f.write_text(json.dumps({"devices": {"inverter": {"units": [1]}}}), encoding="utf-8")
    monkeypatch.setenv("SIGEN_CONFIG", str(f))
    code, _, err = run("read", *net(server), "--timeout", 0.2)
    assert code == ExitCode.CONNECTION and "timed out" in err
    code, out, _ = run("read", *net(server), "--timeout", 0.5, "--no-config")
    assert code == ExitCode.OK and "99.9%" in out


def test_config_and_no_config_conflict(run):
    with pytest.raises(SystemExit) as e:
        run("read", "--ip", "127.0.0.1", "--config", "x.json", "--no-config")
    assert e.value.code == 2


def write_both_config(tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"devices": {"inverter": {"units": [1]}}}), encoding="utf-8")
    return f


def test_read_with_inverter_units(run, both_server, tmp_path):
    code, out, _ = run("read", *net(both_server), "--config", write_both_config(tmp_path))
    assert code == ExitCode.OK
    assert "Inverter (unit 1): SigenStor EC SP US, firmware V100R001C22SPC117" in out
    assert "PV strings:      1: 294 V 9.82 A (2.89 kW)" in out and "4: 267 V" in out
    assert "not readable" not in out


def test_capture_and_read_json_with_inverter_units(run, both_server, tmp_path):
    code, out, _ = run(
        "capture", *net(both_server), "--config", write_both_config(tmp_path), "--out-dir", tmp_path / "caps"
    )
    assert code == ExitCode.OK
    (f,) = (tmp_path / "caps").iterdir()
    doc = json.loads(f.read_text(encoding="utf-8"))
    (inverter,) = doc["other_units"]
    assert inverter["unit_id"] == 1 and inverter["errors"] == {} and doc["errors"] == {}
    assert inverter["input_registers"]["30500"]["value"] == "SigenStor EC SP US"
    code, out, _ = run("read-json", "--data-file", f)
    assert code == ExitCode.OK and "Inverter (unit 1)" in out
    assert "Inverter (unit 1): Live status (input registers)" in out and "inverter_pv1_voltage" in out


def test_scan_finds_fake(run, server):
    code, out, _ = run("scan", "--ip-range", "127.0.0.1-3", "--port", server.port, "--scan-timeout", 0.2)
    assert code == ExitCode.OK and out.strip() == "127.0.0.1"


def test_scan_not_found(run):
    code, _, _ = run("scan", "--ip-range", "127.0.0.1", "--port", free_port(), "--scan-timeout", 0.2)
    assert code == ExitCode.NOT_FOUND


# ---------- set: every outcome ----------
def test_dry_run_writes_nothing(run, server, journal):
    code, out, _ = run("set", *net(server), "--backup-soc", 20, "--journal", journal)
    assert code == ExitCode.DRY_RUN and "dry run" in out
    assert server.bank.read(247, "holding", 40046, 1) == [0]
    assert not journal.exists()  # dry runs aren't journaled


def test_apply_writes_verifies_and_journals(run, server, journal):
    code, out, _ = run("set", *net(server), "--backup-soc", 20, "--apply", "--journal", journal)
    assert code == ExitCode.OK and "verified" in out
    assert server.bank.read(247, "holding", 40046, 1) == [200]
    entry = json.loads(journal.read_text().splitlines()[-1])
    assert (entry["outcome"], entry["before_raw"], entry["after_raw"]) == ("written", [0], [200])


def test_unchanged_is_ok_and_writes_nothing(run, server, journal):
    code, out, _ = run("set", *net(server), "--backup-soc", 0, "--apply", "--journal", journal)
    assert code == ExitCode.OK and "already" in out
    assert json.loads(journal.read_text())["write_sent"] is False


def test_refused_value(run, server, journal):
    code, _, _ = run("set", *net(server), "--backup-soc", 150, "--apply", "--journal", journal)
    assert code == ExitCode.REFUSED
    assert server.bank.read(247, "holding", 40046, 1) == [0]


def test_refused_mode(run, server, journal):
    code, _, _ = run("set", *net(server), "--remote-ems-mode", "discharge-ess-first", "--apply", "--journal", journal)
    assert code == ExitCode.REFUSED


def test_expect_current_guard(run, server, journal):
    code, out, _ = run("set", *net(server), "--backup-soc", 20, "--expect-current", 10, "--apply", "--journal", journal)
    assert code == ExitCode.PRECONDITION and "expected current value" in out
    assert server.bank.read(247, "holding", 40046, 1) == [0]
    code, _, _ = run("set", *net(server), "--backup-soc", 20, "--expect-current", 0, "--apply", "--journal", journal)
    assert code == ExitCode.OK


def test_verify_failed_when_device_ignores_write(run, lying_server, journal):
    code, out, _ = run("set", *net(lying_server), "--backup-soc", 20, "--apply", "--journal", journal)
    assert code == ExitCode.VERIFY_FAILED and "read back" in out
    entry = json.loads(journal.read_text())
    assert entry["outcome"] == "verify_failed" and entry["write_sent"] is True


def test_write_rejected_by_device(run, rejecting_server, journal):
    code, out, _ = run("set", *net(rejecting_server), "--backup-soc", 20, "--apply", "--journal", journal)
    assert code == ExitCode.MODBUS and "rejected" in out and "exception 4" in out
    entry = json.loads(journal.read_text())
    assert entry["outcome"] == "write_rejected" and entry["write_sent"] is True
    assert entry["after_raw"] == [0]  # read back after the refusal
    assert rejecting_server.bank.read(247, "holding", 40046, 1) == [0]


def test_set_connection_failure(run, journal):
    code, _, _ = run(
        "set",
        "--ip",
        "127.0.0.1",
        "--port",
        free_port(),
        "--timeout",
        0.2,
        "--backup-soc",
        20,
        "--apply",
        "--journal",
        journal,
    )
    assert code == ExitCode.CONNECTION


def test_set_json_output(run, server, journal):
    code, out, _ = run(
        "set", *net(server), "--remote-ems-mode", "max-self-consumption", "--apply", "--json", "--journal", journal
    )
    doc = json.loads(out)
    assert code == ExitCode.OK and doc["exit_code"] == 0
    assert doc["after"] == "Max Self-Consumption (2)"


# ---------- undo ----------
def test_undo_restores_in_order(run, server, journal):
    j = ["--journal", journal]
    run("set", *net(server), "--backup-soc", 20, "--apply", *j)
    run("set", *net(server), "--remote-ems-mode", "max-self-consumption", "--apply", *j)

    assert run("undo", *net(server), *j)[0] == ExitCode.DRY_RUN  # dry run by default
    assert run("undo", *net(server), "--apply", *j)[0] == ExitCode.OK  # undoes the mode first
    assert server.bank.read(247, "holding", 40031, 1) == [0]
    assert server.bank.read(247, "holding", 40046, 1) == [200]
    assert run("undo", *net(server), "--apply", *j)[0] == ExitCode.OK  # then the backup SOC
    assert server.bank.read(247, "holding", 40046, 1) == [0]
    assert run("undo", *net(server), "--apply", *j)[0] == ExitCode.NOT_FOUND


def test_undo_refuses_if_changed_since(run, server, journal):
    j = ["--journal", journal]
    run("set", *net(server), "--backup-soc", 20, "--apply", *j)
    server.bank.write(247, 40046, [300])  # someone changed it in the app meanwhile
    code, out, _ = run("undo", *net(server), "--apply", *j)
    assert code == ExitCode.PRECONDITION
    assert server.bank.read(247, "holding", 40046, 1) == [300]


def test_registers_listing(run):
    code, out, _ = run("registers", "--writable")
    assert code == ExitCode.OK and "--backup-soc" in out and "40031" in out


def test_usage_errors_exit_2(run):
    import pytest

    for argv in (
        ["set", "--ip", "1.2.3.4"],  # no setting chosen
        ["set", "--ip", "1.2.3.4", "--backup-soc", "1", "--remote-ems-mode", "0"],
        ["set", "--backup-soc", "20"],  # writes need --ip
        ["read", "--ip", "1.2.3.4", "--ip-range", "1.2.3.4-5"],
    ):
        with pytest.raises(SystemExit) as e:
            run(*argv)
        assert e.value.code == 2


# ---------- read-json ----------
def test_read_json_pretty_prints_and_redecodes(run, tmp_path):
    # An older-format dump: "not set" limits stored as a bogus decoded value
    doc = {
        "source": {"ip": "192.168.1.100", "read_at": "2026-10-06T21:48:31+00:00"},
        "unit_id": 247,
        "input_registers": {
            "30003": {"name": "plant_ems_work_mode", "type": "u16", "value": 7, "raw": [7]},
            "30014": {"name": "plant_ess_soc", "type": "u16", "gain": 10, "value": 99.9, "raw": [999]},
        },
        "holding_registers": {
            "40032": {
                "name": "plant_ess_max_charging_limit",
                "type": "u32",
                "gain": 1000,
                "value": 4294967.295,
                "raw": [65535, 65535],
            }
        },
    }
    f = tmp_path / "dump.json"
    f.write_text(json.dumps(doc))
    for cmd in ("read-json", "read_json"):
        code, out, _ = run(cmd, "--data-file", f)
        assert code == ExitCode.OK
        assert "Remote EMS (7)" in out and "99.9%" in out
        assert "Not set (no limit)" in out and "4294967" not in out


def test_read_json_round_trips_a_live_dump(run, server, tmp_path):
    _, out, _ = run("read", *net(server), "--json")
    f = tmp_path / "live.json"
    f.write_text(out, encoding="utf-8")
    code, out, _ = run("read-json", "--data-file", f)
    assert code == ExitCode.OK and "Settings (holding registers)" in out and "40046" in out


def test_read_json_accepts_powershell5_utf16(run, server, tmp_path):
    # Windows PowerShell 5's `>` re-encodes output as UTF-16LE with a BOM
    _, out, _ = run("read", *net(server), "--json")
    f = tmp_path / "ps5.json"
    f.write_text(out, encoding="utf-16")
    code, out, _ = run("read-json", "--data-file", f)
    assert code == ExitCode.OK and "99.9%" in out and "°C" in out


def test_read_json_rejects_garbage(run, tmp_path):
    f = tmp_path / "bad.json"
    f.write_bytes(b"\x80\x81 not json")
    code, _, err = run("read-json", "--data-file", f)
    assert code == ExitCode.ERROR and "isn't valid JSON" in err


# ---------- capture ----------
def test_capture_writes_utf8_file(run, midsoc_server, tmp_path):
    import re
    from sigen.data_io import load_document

    out_dir = tmp_path / "new" / "captures"  # created if missing
    code, out, _ = run("capture", *net(midsoc_server), "--out-dir", out_dir)
    assert code == ExitCode.OK and "55.4%" in out
    (f,) = out_dir.iterdir()
    assert re.fullmatch(r"my_system_\d{4}-\d\d-\d\d_\d\d-\d\d-\d\dZ\.json", f.name)
    assert f"Saved {f}" in out
    data = f.read_bytes()
    assert "°C".encode() in data and b"\r\n" not in data
    doc = json.loads(data.decode("utf-8"))
    assert doc["input_registers"]["30066"]["raw"] == [0, 880]
    assert doc["derived"]["battery_total_estimate_kwh"] == 15.88
    assert load_document(f)  # replayable as fake data
    assert run("read-json", "--data-file", f)[0] == ExitCode.OK


def test_a_wrongly_shaped_data_file_is_a_value_error(tmp_path):
    from sigen.data_io import load_document

    for doc in ([1, 2], {"input_registers": "30014"}, {"other_units": [5]}):
        f = tmp_path / "bad.json"
        f.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(ValueError, match="not a register dump"):  # what the fake server's reload catches
            load_document(f)


def test_the_fake_server_answers_a_new_units_file_at_once(tmp_path):
    from sigen.fake_server import RegisterBank

    (tmp_path / "plant.json").write_text(json.dumps({"input_registers": {"30014": {"raw": [880]}}}), encoding="utf-8")
    bank = RegisterBank(tmp_path)
    assert bank.units() == {247}
    (tmp_path / "unit5.json").write_text(
        json.dumps({"unit_id": 5, "input_registers": {"30014": {"raw": [1]}}}), encoding="utf-8"
    )
    assert bank.units() == {5, 247}  # no read of a known unit needed first


def test_local_time_keeps_a_written_offset():
    import datetime

    from sigen.cli import _local_time

    utc = datetime.UTC
    with_offset = _local_time("2026-10-10T06:00+00:00", "America/Edmonton", "--start")
    assert with_offset == datetime.datetime(2026, 10, 10, 6, 0, tzinfo=utc)  # that instant, not 06:00 local
    assert str(with_offset.tzinfo) == "America/Edmonton"
    wall_clock = _local_time("2026-10-10T06:00", "America/Edmonton", "--start")
    assert wall_clock == datetime.datetime(2026, 10, 10, 12, 0, tzinfo=utc)  # no offset: Edmonton's 06:00 (UTC-6)


def test_capture_name_prefix_and_never_overwrites(run, server, tmp_path, monkeypatch):
    import sigen.cli

    monkeypatch.setattr(sigen.cli, "capture_filename", lambda doc, prefix: f"{prefix}_fixed.json")
    existing = tmp_path / "garage_fixed.json"
    existing.write_text("keep me")
    code, _, err = run("capture", *net(server), "--out-dir", tmp_path, "--name-prefix", "garage")
    assert code == ExitCode.ERROR and "already exists" in err
    assert existing.read_text() == "keep me"


def test_capture_connection_failure_writes_nothing(run, tmp_path):
    code, _, _ = run(
        "capture", "--ip", "127.0.0.1", "--port", free_port(), "--timeout", 0.2, "--out-dir", tmp_path / "caps"
    )
    assert code == ExitCode.CONNECTION and not (tmp_path / "caps").exists()


def test_capture_filename_is_utc():
    from sigen.data_io import capture_filename

    doc = {"source": {"read_at": "2026-10-07T11:13:34-06:00"}}
    assert capture_filename(doc, "my_system") == "my_system_2026-10-07_17-13-34Z.json"


def test_read_json_missing_file(run, tmp_path):
    code, _, _ = run("read-json", "--data-file", tmp_path / "nope.json")
    assert code == ExitCode.NOT_FOUND
