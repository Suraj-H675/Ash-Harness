import json
import os
import sqlite3

import httpx
import pytest

from ash.commands.doctor import DoctorCheck, render_doctor, run_doctor
from ash.commands.doctor import (
    _check_a2a,
    _check_automation,
    _check_browser,
    _check_lsp,
    _check_storage,
    _check_connectivity,
    _check_web_search,
)
from ash.automation.schedules import build_schedule
from ash.automation.store import AutomationStore
from ash.config import AshConfig
from ash.lsp.config import LSPServerConfig


def test_render_doctor_json_has_stable_schema() -> None:
    rendered = render_doctor([DoctorCheck("config", "pass", "valid")], json_output=True)
    payload = json.loads(rendered)
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["checks"][0]["name"] == "config"


@pytest.mark.asyncio
async def test_run_doctor_connect_uses_shared_provider_verification(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[tuple[str, dict[str, str], float]] = []
    response = httpx.Response(
        200,
        json={"data": [{"id": "gateway-model"}]},
        request=httpx.Request("GET", "http://gateway.invalid/v1/models"),
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ASH_MODEL", "openai/gateway-model")
    monkeypatch.setenv("OPENAI_API_KEY", "gateway-secret")
    monkeypatch.setenv("OPENAI_API_BASE", "http://gateway.invalid/v1")
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(tmp_path / "db"))

    def fake_get(endpoint: str, *, headers: dict[str, str], timeout: float) -> object:
        requests.append((endpoint, headers, timeout))
        return response

    monkeypatch.setattr("ash.providers.readiness.httpx.get", fake_get)

    checks = await run_doctor(connect=True)

    by_name = {check.name: check for check in checks}
    assert by_name["connectivity"].status == "pass"
    assert requests == [
        (
            "http://gateway.invalid/v1/models",
            {"Authorization": "Bearer gateway-secret"},
            10.0,
        )
    ]


@pytest.mark.asyncio
async def test_connectivity_uses_runtime_openai_override_and_validates_model(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[tuple[str, dict[str, str]]] = []
    response = httpx.Response(
        200,
        json={"data": [{"id": "gateway-model"}]},
        request=httpx.Request("GET", "http://gateway.invalid/v1/models"),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "gateway-secret")
    monkeypatch.setenv("OPENAI_API_BASE", "http://gateway.invalid/v1")
    def fake_get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        requests.append((url, headers))
        return response

    monkeypatch.setattr("ash.providers.readiness.httpx.get", fake_get)

    check = await _check_connectivity(
        AshConfig(model="openai/gateway-model", workspace_root=tmp_path)
    )

    assert check.status == "pass"
    assert requests == [
        ("http://gateway.invalid/v1/models", {"Authorization": "Bearer gateway-secret"})
    ]


@pytest.mark.asyncio
async def test_connectivity_fails_when_selected_model_is_not_advertised(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[tuple[str, dict[str, str]]] = []
    response = httpx.Response(
        200,
        json={"data": [{"id": "another-model"}]},
        request=httpx.Request("GET", "https://api.openai.com/v1/models"),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    def fake_get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        requests.append((url, headers))
        return response

    monkeypatch.setattr("ash.providers.readiness.httpx.get", fake_get)

    check = await _check_connectivity(
        AshConfig(model="openai/missing-model", workspace_root=tmp_path)
    )

    assert check.status == "fail"
    assert "missing-model" in check.message


@pytest.mark.asyncio
async def test_connectivity_checks_anthropic_catalog_with_runtime_headers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[tuple[str, dict[str, str]]] = []
    response = httpx.Response(
        200,
        json={"data": [{"id": "claude-test"}]},
        request=httpx.Request("GET", "http://gateway.invalid/v1/models"),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("ANTHROPIC_API_BASE", "http://gateway.invalid")
    def fake_get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        requests.append((url, headers))
        return response

    monkeypatch.setattr("ash.providers.readiness.httpx.get", fake_get)

    check = await _check_connectivity(
        AshConfig(model="anthropic/claude-test", workspace_root=tmp_path)
    )

    assert check.status == "pass"
    assert requests == [
        (
            "http://gateway.invalid/v1/models",
            {
                "x-api-key": "anthropic-secret",
                "anthropic-version": "2023-06-01",
            },
        )
    ]


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
    assert by_name["automation"].status == "pass"
    assert by_name["extensions"].status == "pass"
    assert "connectivity" not in by_name
    assert not (tmp_path / "db" / ".doctor.sqlite3").exists()
    assert not (tmp_path / "db" / "automation.db").exists()


def test_storage_check_reports_sqlite_open_failures(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_connect(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr("ash.commands.doctor.sqlite3.connect", fail_connect)
    check = _check_storage(AshConfig(db_directory=tmp_path / "db"))
    assert check.name == "storage"
    assert check.status == "fail"
    assert "unable to open database file" in check.message


def test_automation_doctor_does_not_open_database_when_disabled(tmp_path) -> None:
    database = tmp_path / "db" / "automation.db"
    database.parent.mkdir()
    database.write_bytes(b"not a database")

    check = _check_automation(
        AshConfig(
            automation_enabled=False,
            db_directory=database.parent,
            workspace_root=tmp_path,
        )
    )

    assert check.status == "pass"
    assert "disabled" in check.message


def test_automation_doctor_does_not_create_unused_database(tmp_path) -> None:
    config = AshConfig(
        db_directory=tmp_path / "db",
        workspace_root=tmp_path,
    )

    check = _check_automation(config)

    assert check.status == "pass"
    assert "No automation database" in check.message
    assert not (config.db_directory / "automation.db").exists()


def test_automation_doctor_warns_for_enabled_job_without_worker(tmp_path) -> None:
    database = tmp_path / "db" / "automation.db"
    config = AshConfig(db_directory=database.parent, workspace_root=tmp_path)
    with AutomationStore(database) as store:
        store.create_job(
            name="daily-review",
            prompt="Review the workspace",
            workspace=tmp_path,
            schedule=build_schedule(every="1h"),
        )

    check = _check_automation(config)

    assert check.status == "warn"
    assert "1 enabled job(s)" in check.message
    assert "no live worker" in check.message
    assert "ash cron worker" in check.remedy


def test_automation_doctor_reports_healthy_database_and_live_worker(tmp_path) -> None:
    database = tmp_path / "db" / "automation.db"
    config = AshConfig(db_directory=database.parent, workspace_root=tmp_path)
    with AutomationStore(database) as store:
        store.create_job(
            name="daily-review",
            prompt="Review the workspace",
            workspace=tmp_path,
            schedule=build_schedule(every="1h"),
        )
        store.heartbeat_worker(
            worker_id="doctor-test-worker",
            workspace=tmp_path,
            pid=os.getpid(),
            max_concurrent_runs=1,
        )

    check = _check_automation(config)

    assert check.status == "pass"
    assert "1 enabled job(s), 1 live worker(s)" in check.message


def test_automation_doctor_reports_corrupt_database(tmp_path) -> None:
    database = tmp_path / "db" / "automation.db"
    database.parent.mkdir()
    database.write_bytes(b"not a sqlite database")

    check = _check_automation(
        AshConfig(db_directory=database.parent, workspace_root=tmp_path)
    )

    assert check.status == "fail"
    assert "Cannot validate automation database" in check.message


def test_automation_doctor_refuses_future_schema(tmp_path) -> None:
    database = tmp_path / "db" / "automation.db"
    database.parent.mkdir()
    with AutomationStore(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 999")

    check = _check_automation(
        AshConfig(db_directory=database.parent, workspace_root=tmp_path)
    )

    assert check.status == "fail"
    assert "newer than supported" in check.message
    assert "Upgrade Ash" in check.remedy


def test_automation_doctor_reports_pending_schema_migration(tmp_path) -> None:
    database = tmp_path / "db" / "automation.db"
    database.parent.mkdir()
    with AutomationStore(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 1")

    check = _check_automation(
        AshConfig(db_directory=database.parent, workspace_root=tmp_path)
    )

    assert check.status == "warn"
    assert "requires migration" in check.message
    assert "ash cron status" in check.remedy


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
    monkeypatch.setattr(
        "ash.commands.doctor.importlib.util.find_spec", lambda name: None
    )
    missing_extra = _check_browser()
    assert missing_extra.status == "warn"
    assert "installer.py | python3 - --extra browser" in missing_extra.remedy
    assert "pipx install" not in missing_extra.remedy

    monkeypatch.setattr(
        "ash.commands.doctor.importlib.util.find_spec", lambda name: object()
    )
    monkeypatch.setattr(
        "ash.commands.doctor.subprocess.run",
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


def test_lsp_doctor_reports_untrusted_workspace(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ash.safety.trust.is_workspace_trusted", lambda workspace: False
    )

    check = _check_lsp(AshConfig(workspace_root=tmp_path))

    assert check.status == "warn"
    assert "untrusted" in check.message


def test_lsp_doctor_reports_missing_configured_executable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ash.safety.trust.is_workspace_trusted", lambda workspace: True)
    monkeypatch.setattr(
        "ash.lsp.config.load_lsp_server_configs",
        lambda workspace, include_project: {
            "missing": LSPServerConfig(
                "missing", (str(tmp_path / "not-installed"),), {".x": "x"}
            )
        },
    )

    check = _check_lsp(AshConfig(workspace_root=tmp_path))

    assert check.status == "fail"
    assert "missing" in check.message


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
