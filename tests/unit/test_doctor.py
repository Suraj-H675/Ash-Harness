import json
import sqlite3

import pytest

from cli.doctor import DoctorCheck, render_doctor, run_doctor
from cli.doctor import _check_storage
from config import AshConfig


def test_render_doctor_json_has_stable_schema() -> None:
    rendered = render_doctor([DoctorCheck("config", "pass", "valid")], json_output=True)
    payload = json.loads(rendered)
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["checks"][0]["name"] == "config"


@pytest.mark.asyncio
async def test_doctor_reports_local_runtime_without_network(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(tmp_path / "db"))
    checks = await run_doctor(connect=False)
    by_name = {check.name: check for check in checks}
    assert by_name["runtime"].status == "pass"
    assert by_name["credentials"].status == "pass"
    assert by_name["storage"].status == "pass"
    assert by_name["extensions"].status == "pass"
    assert "connectivity" not in by_name
    assert not (tmp_path / "db" / ".doctor.sqlite3").exists()


def test_storage_check_reports_sqlite_open_failures(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_connect(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr("cli.doctor.sqlite3.connect", fail_connect)
    check = _check_storage(AshConfig(db_directory=tmp_path / "db"))
    assert check.name == "storage"
    assert check.status == "fail"
    assert "unable to open database file" in check.message


@pytest.mark.asyncio
async def test_doctor_reports_invalid_extension_configs(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(tmp_path / "db"))
    hooks = tmp_path / "home" / ".ash" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(json.dumps({"pre_tool": "bad"}), encoding="utf-8")

    checks = await run_doctor(connect=False)
    by_name = {check.name: check for check in checks}

    assert by_name["extensions"].status == "fail"
    assert "pre_tool hooks must be a list" in by_name["extensions"].message
