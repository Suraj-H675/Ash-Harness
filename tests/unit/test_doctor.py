import json
import sqlite3

import pytest

from cli.doctor import DoctorCheck, render_doctor, run_doctor
from cli.doctor import _check_a2a, _check_browser, _check_storage, _check_web_search
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


def test_web_search_doctor_reports_auto_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "configured")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    check = _check_web_search(AshConfig(web_search_provider="auto"))

    assert check.status == "pass"
    assert "brave" in check.message


def test_browser_doctor_distinguishes_missing_extra_and_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cli.doctor.importlib.util.find_spec", lambda name: None)
    missing_extra = _check_browser()
    assert missing_extra.status == "warn"
    assert "ash-ai[browser]" in missing_extra.remedy

    monkeypatch.setattr("cli.doctor.importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr(
        "cli.doctor.subprocess.run",
        lambda *args, **kwargs: type(
            "Completed", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )(),
    )
    missing_binary = _check_browser()
    assert missing_binary.status == "warn"
    assert "setup browser" in missing_binary.remedy


def test_a2a_doctor_reports_unset_remote_credentials(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    config_path = home / ".ash" / "a2a.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"agents":{"review":{"url":"https://review.example.com",'
        '"token_env":"REVIEW_TOKEN"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("REVIEW_TOKEN", raising=False)

    check = _check_a2a(AshConfig(workspace_root=tmp_path))

    assert check.status == "warn"
    assert "REVIEW_TOKEN" in check.message


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
