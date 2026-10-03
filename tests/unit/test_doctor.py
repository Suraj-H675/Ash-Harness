import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from ash.commands.doctor import DoctorCheck, render_doctor, run_doctor
from ash.commands.doctor import (
    _check_a2a,
    _check_automation,
    _check_browser,
    _check_credentials,
    _check_lsp,
    _check_observability,
    _check_mcp,
    _check_storage,
    _check_connectivity,
    _check_workspace,
    _check_web_search,
)
from ash.automation.schedules import build_schedule
from ash.automation.store import AutomationStore
from ash.config import AshConfig
from ash.lsp.config import LSPServerConfig
from ash.providers.readiness import (
    ProviderConnection,
    ProviderVerification,
    ProviderVerificationError,
)
from .provider_test_helpers import patch_catalog_client


def test_render_doctor_json_has_stable_schema() -> None:
    rendered = render_doctor([DoctorCheck("config", "pass", "valid")], json_output=True)
    payload = json.loads(rendered)
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["checks"][0]["name"] == "config"


def test_render_doctor_human_output_sanitizes_untrusted_fields() -> None:
    rendered = render_doctor(
        [
            DoctorCheck(
                "model\nname",
                "fail",
                "bad\x1b[2J\u202ehidden\u202c",
                "[bold]repair\tme",
            )
        ]
    )

    assert "model\\x0aname" in rendered
    assert "bad\\x1b[2J\\u202ehidden\\u202c" in rendered
    assert "repair\\x09me" in rendered
    assert "\x1b[2J" not in rendered
    assert "\u202e" not in rendered


@pytest.mark.asyncio
async def test_doctor_reports_unsupported_native_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ash.commands.doctor.platform_support_error",
        lambda: "Native Windows is not currently supported by Ash. Use WSL2.",
    )
    monkeypatch.setattr(
        "ash.commands.doctor.AshConfig.load",
        lambda: (_ for _ in ()).throw(ValueError("stop after platform check")),
    )

    checks = await run_doctor(connect=False)

    by_name = {check.name: check for check in checks}
    assert by_name["platform"].status == "fail"
    assert "WSL2" in by_name["platform"].remedy
    assert by_name["runtime"].status == "pass"


@pytest.mark.asyncio
async def test_run_doctor_connect_uses_shared_provider_verification(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"data": [{"id": "gateway-model"}]},
            request=request,
        ),
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ASH_MODEL", "openai/gateway-model")
    monkeypatch.setenv("OPENAI_API_KEY", "gateway-secret")
    monkeypatch.setenv("OPENAI_API_BASE", "https://gateway.invalid/v1")
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(tmp_path / "db"))

    checks = await run_doctor(connect=True)

    by_name = {check.name: check for check in checks}
    assert by_name["connectivity"].status == "pass"
    assert len(requests) == 1
    request, timeout = requests[0]
    assert str(request.url) == "https://gateway.invalid/v1/models"
    assert request.headers["authorization"] == "Bearer gateway-secret"
    assert timeout == 10.0


@pytest.mark.asyncio
async def test_connectivity_uses_runtime_openai_override_and_validates_model(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"data": [{"id": "gateway-model"}]},
            request=request,
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "gateway-secret")
    monkeypatch.setenv("OPENAI_API_BASE", "https://gateway.invalid/v1")

    check = await _check_connectivity(
        AshConfig(model="openai/gateway-model", workspace_root=tmp_path)
    )

    assert check.status == "pass"
    assert len(requests) == 1
    request, _ = requests[0]
    assert str(request.url) == "https://gateway.invalid/v1/models"
    assert request.headers["authorization"] == "Bearer gateway-secret"


def test_chatgpt_credentials_do_not_require_openai_api_key(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Store:
        def credential_state(self):
            return "usable"

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "ash.providers.openai_chatgpt_auth.ChatGPTCredentialStore",
        Store,
    )

    check = _check_credentials(
        AshConfig(
            model="openai/gpt-plan",
            openai_auth_mode="chatgpt",
            workspace_root=tmp_path,
        )
    )

    assert check.status == "pass"
    assert "usable" in check.message


@pytest.mark.asyncio
async def test_chatgpt_connectivity_uses_plan_model_verification(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    async def verify(config, *, timeout=10.0):
        calls.append((config.model, timeout))
        return ProviderVerification(
            connection=ProviderConnection(
                provider="openai",
                model_name="gpt-plan",
                base_url="https://api.openai.com/v1",
                catalog_endpoint="https://api.openai.com/v1/models",
                catalog_format="openai",
                auth_mode="chatgpt",
                uses_default_base_url=True,
            ),
            models=("gpt-plan",),
            selected_model_available=True,
        )

    monkeypatch.setattr(
        "ash.commands.doctor.verify_chatgpt_plan_connection",
        verify,
    )
    check = await _check_connectivity(
        AshConfig(
            model="openai/gpt-plan",
            openai_auth_mode="chatgpt",
            workspace_root=tmp_path,
        )
    )

    assert check.status == "pass"
    assert calls == [("openai/gpt-plan", 10.0)]


@pytest.mark.asyncio
async def test_connectivity_fails_when_selected_model_is_not_advertised(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"data": [{"id": "another-model"}]},
            request=request,
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)

    check = await _check_connectivity(
        AshConfig(model="openai/missing-model", workspace_root=tmp_path)
    )

    assert check.status == "fail"
    assert "missing-model" in check.message


@pytest.mark.asyncio
async def test_connectivity_verification_failure_has_direct_remedy(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "ash.commands.doctor.verify_provider_connection",
        lambda _config: (_ for _ in ()).throw(
            ProviderVerificationError("catalog endpoint rejected the request")
        ),
    )

    check = await _check_connectivity(
        AshConfig(model="openai/test-model", workspace_root=tmp_path)
    )

    assert check.status == "fail"
    assert "catalog endpoint rejected" in check.message
    assert "ash providers test" in check.remedy
    assert "ash setup model" in check.remedy


@pytest.mark.asyncio
async def test_connectivity_checks_anthropic_catalog_with_runtime_headers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"data": [{"id": "claude-test"}]},
            request=request,
        ),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("ANTHROPIC_API_BASE", "https://gateway.invalid")

    check = await _check_connectivity(
        AshConfig(model="anthropic/claude-test", workspace_root=tmp_path)
    )

    assert check.status == "pass"
    assert len(requests) == 1
    request, _ = requests[0]
    assert str(request.url) == "https://gateway.invalid/v1/models"
    assert request.headers["x-api-key"] == "anthropic-secret"
    assert request.headers["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_connectivity_refuses_plaintext_remote_credentials_before_network(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = patch_catalog_client(
        monkeypatch,
        lambda request: (_ for _ in ()).throw(
            AssertionError("network request must not be attempted")
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "gateway-secret")
    monkeypatch.setenv("OPENAI_API_BASE", "http://gateway.invalid/v1")

    check = await _check_connectivity(
        AshConfig(model="openai/gateway-model", workspace_root=tmp_path)
    )

    assert check.status == "fail"
    assert "must use HTTPS" in check.message
    assert requests == []


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
    assert "ripgrep" not in by_name
    assert "connectivity" not in by_name
    assert not (tmp_path / "db" / ".doctor.sqlite3").exists()
    assert not any((tmp_path / "db").glob(".doctor-*"))
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
    assert "ash storage check" in check.remedy


def test_workspace_check_failure_has_direct_remedy(tmp_path) -> None:
    missing = tmp_path / "missing"

    check = _check_workspace(AshConfig(workspace_root=missing))

    assert check.status == "fail"
    assert "Not a directory" in check.message
    assert "ASH_WORKSPACE_ROOT" in check.remedy


def test_mcp_check_failure_has_direct_remedy(tmp_path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".mcp.json").write_text("{not-json", encoding="utf-8")

    check = _check_mcp(AshConfig(workspace_root=workspace))

    assert check.status == "fail"
    assert "Invalid .mcp.json" in check.message
    assert "ash mcp status" in check.remedy


def test_storage_check_preserves_pre_existing_doctor_database(tmp_path) -> None:
    database_directory = tmp_path / "db"
    database_directory.mkdir()
    sentinel = database_directory / ".doctor.sqlite3"
    with sqlite3.connect(sentinel) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('do not delete')")
    sentinel_bytes = sentinel.read_bytes()
    sentinel_stat = sentinel.stat()

    check = _check_storage(AshConfig(db_directory=database_directory))

    assert check.status == "pass"
    assert sentinel.read_bytes() == sentinel_bytes
    assert sentinel.stat().st_mode == sentinel_stat.st_mode
    assert sentinel.stat().st_mtime_ns == sentinel_stat.st_mtime_ns


def test_storage_checks_can_run_concurrently_without_colliding(tmp_path) -> None:
    database_directory = tmp_path / "db"
    database_directory.mkdir()
    sentinel = database_directory / ".doctor.sqlite3"
    with sqlite3.connect(sentinel) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('do not delete')")
    sentinel_bytes = sentinel.read_bytes()
    config = AshConfig(db_directory=database_directory)

    with ThreadPoolExecutor(max_workers=2) as executor:
        checks = list(executor.map(_check_storage, (config, config)))

    assert [check.status for check in checks] == ["pass", "pass"]
    assert sentinel.read_bytes() == sentinel_bytes
    assert not any(database_directory.glob(".doctor-*"))


def test_storage_check_cleans_probe_artifacts_after_sqlite_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_directory = tmp_path / "db"
    database_directory.mkdir()
    sentinel = database_directory / ".doctor.sqlite3"
    sentinel.write_bytes(b"user database")

    def fail_connect(database, *args: object, **kwargs: object) -> None:
        probe = os.fspath(database.path)
        for suffix in ("", "-journal", "-wal", "-shm"):
            with open(f"{probe}{suffix}", "wb") as handle:
                handle.write(b"probe artifact")
        raise sqlite3.OperationalError("probe failed")

    monkeypatch.setattr(
        "ash.commands.doctor.PinnedSQLiteDatabase.connect",
        fail_connect,
    )

    check = _check_storage(AshConfig(db_directory=database_directory))

    assert check.status == "fail"
    assert "probe failed" in check.message
    assert sentinel.read_bytes() == b"user database"
    assert not any(database_directory.glob(".doctor-*"))


@pytest.mark.skipif(os.name == "nt", reason="descriptor-anchored POSIX regression")
def test_storage_check_parent_swap_fails_without_writing_replacement(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.safety.anchored_fs as anchored_fs

    state_root = tmp_path / "state"
    state_root.mkdir()
    saved_root = tmp_path / "state-saved"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    database_directory = state_root / "db"
    real_open_or_create = anchored_fs._open_or_create_directory
    swapped = False

    def open_or_create_then_swap(
        parent_descriptor,
        name,
        *,
        create,
        expected=None,
    ):
        nonlocal swapped
        if name == "db" and not swapped:
            state_root.rename(saved_root)
            state_root.symlink_to(replacement, target_is_directory=True)
            swapped = True
        return real_open_or_create(
            parent_descriptor,
            name,
            create=create,
            expected=expected,
        )

    monkeypatch.setattr(
        anchored_fs,
        "_open_or_create_directory",
        open_or_create_then_swap,
    )

    check = _check_storage(AshConfig(db_directory=database_directory))

    assert swapped is True
    assert check.status == "fail"
    assert not (replacement / "db").exists()


def test_storage_check_reports_malformed_database_directory(tmp_path) -> None:
    database_directory = tmp_path / "db"
    database_directory.write_text("not a directory", encoding="utf-8")

    check = _check_storage(AshConfig(db_directory=database_directory))

    assert check.status == "fail"
    assert "Database directory is not writable" in check.message
    assert database_directory.read_text(encoding="utf-8") == "not a directory"


def test_storage_check_rejects_symlinked_database_directory(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    database_directory = tmp_path / "db"
    try:
        database_directory.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    check = _check_storage(AshConfig(db_directory=database_directory))

    assert check.status == "fail"
    assert "database probe" in check.message
    assert "symlink or junction" in check.message
    assert list(outside.iterdir()) == []


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


def test_automation_doctor_rejects_symlinked_database(tmp_path) -> None:
    database_directory = tmp_path / "db"
    database_directory.mkdir()
    outside = tmp_path / "outside.db"
    with AutomationStore(outside):
        pass
    database = database_directory / "automation.db"
    try:
        database.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    check = _check_automation(
        AshConfig(db_directory=database_directory, workspace_root=tmp_path)
    )

    assert check.status == "fail"
    assert "automation database" in check.message
    assert "symlink or junction" in check.message


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


def test_observability_doctor_reports_off_by_default() -> None:
    check = _check_observability(AshConfig())

    assert check.status == "pass"
    assert "disabled" in check.message


def test_observability_doctor_reports_ready_without_echoing_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ash.observability._module_available",
        lambda _name: True,
    )
    endpoint = "https://collector.example/private-team"
    check = _check_observability(
        AshConfig(
            observability_enabled=True,
            observability_otlp_endpoint=endpoint,
            observability_sample_rate=0.25,
        )
    )

    assert check.status == "pass"
    assert "sample_rate=0.25" in check.message
    assert "content_capture=false" in check.message
    assert endpoint not in check.message


def test_observability_doctor_reports_missing_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ash.observability._module_available",
        lambda _name: True,
    )
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)

    check = _check_observability(AshConfig(observability_enabled=True))

    assert check.status == "fail"
    assert "endpoints are incomplete" in check.message


def test_browser_doctor_distinguishes_missing_extra_and_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ash.commands.doctor.importlib.util.find_spec", lambda name: None
    )
    missing_extra = _check_browser()
    assert missing_extra.status == "warn"
    assert "api.github.com/repos/Suraj-H675/Ash-Harness/releases/latest" in missing_extra.remedy
    assert "raw.githubusercontent.com" not in missing_extra.remedy
    assert "--extra browser" in missing_extra.remedy
    assert "pipx install" not in missing_extra.remedy

    monkeypatch.setattr(
        "ash.commands.doctor.importlib.util.find_spec", lambda name: object()
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("ash.commands.doctor.run_browser_subprocess", fake_run)
    missing_binary = _check_browser()
    assert missing_binary.status == "warn"
    assert "setup browser" in missing_binary.remedy
    from ash.commands import doctor

    assert calls == [
        (
            [doctor.sys.executable, "-I", "-m", "playwright", "install", "--list"],
            {
                "check": False,
                "capture_output": True,
                "text": True,
                "timeout": 15,
            },
        )
    ]


def test_browser_doctor_probes_configured_cdp_without_requiring_local_chromium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ash.commands.doctor.importlib.util.find_spec", lambda name: object()
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("ash.commands.doctor.run_browser_subprocess", fake_run)
    check = _check_browser(AshConfig(browser_cdp_url="http://127.0.0.1:9222"))

    assert check.status == "pass"
    assert "CDP endpoint is reachable" in check.message
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:3] == [__import__("ash.commands.doctor", fromlist=["sys"]).sys.executable, "-I", "-c"]
    assert command[-1] == "http://127.0.0.1:9222"
    assert kwargs["timeout"] == 5


def test_browser_doctor_reports_unreachable_configured_cdp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ash.commands.doctor.importlib.util.find_spec", lambda name: object()
    )
    monkeypatch.setattr(
        "ash.commands.doctor.run_browser_subprocess",
        lambda *args, **kwargs: type(
            "Completed", (), {"returncode": 1, "stdout": "", "stderr": "failed"}
        )(),
    )

    check = _check_browser(AshConfig(browser_cdp_url="http://127.0.0.1:9222"))

    assert check.status == "warn"
    assert "not reachable" in check.message
    assert "remote debugging" in check.remedy


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


@pytest.mark.asyncio
async def test_doctor_warns_when_auto_approve_docker_limits_are_disabled(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(tmp_path / "db"))
    monkeypatch.setenv("ASH_SAFETY_TIER", "auto_approve")
    monkeypatch.setenv("ASH_SANDBOX_BACKEND", "docker")
    monkeypatch.setenv("ASH_SANDBOX_DOCKER_MEMORY_MB", "0")
    monkeypatch.setenv("ASH_SANDBOX_DOCKER_CPUS", "0")
    monkeypatch.setattr(
        "ash.sandbox.manager.has_docker",
        lambda _image, *, workspace_root=None: True,
    )

    checks = await run_doctor(connect=False)
    sandbox = {check.name: check for check in checks}["sandbox"]

    assert sandbox.status == "warn"
    assert "aggregate_resource_limits=false" in sandbox.message
    assert "sandbox_docker_memory_mb" in sandbox.remedy
