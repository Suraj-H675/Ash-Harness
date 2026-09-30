from pathlib import Path

import pytest

from ash.config import AshConfig
from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.guard import SafetyGuard
from ash.ui.headless import HeadlessUI


class MemoryTestProvider(ProviderABC):
    model_name = "memory-test"

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(content="done", is_done=True)

    def count_tokens(self, text: str) -> int:
        return len(text.split())


@pytest.mark.asyncio
async def test_project_memory_auto_index_is_bounded_and_respects_excludes(
    tmp_path,
) -> None:
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "included.py").write_text("alpha\n" * 10, encoding="utf-8")
    (tmp_path / "excluded.py").write_text("beta\n", encoding="utf-8")
    (tmp_path / "large.py").write_text("gamma\n" * 100_000, encoding="utf-8")

    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="sqlite",
                repo_map_exclude_patterns=["excluded.py"],
    )
    loop = AshLoop(
        SessionStore(config.db_directory / "sessions.db"),
        MemoryTestProvider(),
        SafetyGuard(project_root=tmp_path),
        None,
        tmp_path,
        config=config,
        enable_project_memory=True,
        memory_db_path=tmp_path / "memory" / "memory.db",
            )
    try:
        indexed = await loop.index_project_memory(
            max_files=2,
            max_bytes_per_file=1_000,
        )
        assert indexed == 1
        hits = await loop.search_memory("beta")
        assert all(hit.file_path != "excluded.py" for hit in hits)
        hits = await loop.search_memory("alpha")
        assert any(hit.file_path.endswith("included.py") for hit in hits)
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_manual_memory_index_skips_oversized_file(tmp_path) -> None:
    path = tmp_path / "large.py"
    path.write_bytes(b"x" * 9)
    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="sqlite",
            )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=MemoryTestProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        config=config,
        enable_project_memory=True,
        memory_db_path=tmp_path / "memory" / "memory.db",
            )
    try:
        assert await loop.index_file_for_memory(path, max_bytes_per_file=8) == 0
        assert await loop.search_memory("x") == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_manual_then_workspace_reindex_uses_one_document_identity(
    tmp_path,
) -> None:
    path = tmp_path / "notes.py"
    path.write_text("legacy_manual_memory_marker\n", encoding="utf-8")
    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="sqlite",
            )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=MemoryTestProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        config=config,
        enable_project_memory=True,
        memory_db_path=tmp_path / "memory" / "memory.db",
            )
    try:
        assert await loop.index_file_for_memory(path) == 1
        assert loop._memory_pipeline is not None
        assert loop._memory_pipeline.document_paths() == {"notes.py"}

        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=4096) == 1
        assert loop._memory_pipeline.document_paths() == {"notes.py"}

        path.write_text("fresh_project_memory_marker\n", encoding="utf-8")
        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=4096) == 1
        exported = str(loop._memory_pipeline.export(limit=20))
        assert "fresh_project_memory_marker" in exported
        assert "legacy_manual_memory_marker" not in exported
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_large_repository_memory_indexing_is_bounded(tmp_path) -> None:
    file_count = 120
    for index in range(file_count + 50):
        directory = tmp_path / "packages" / f"pkg-{index % 25}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"module-{index}.py").write_text(
            f"def function_{index}():\n    return {index}\n" * 20,
            encoding="utf-8",
        )

    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="sqlite",
            )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=MemoryTestProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        config=config,
        enable_project_memory=True,
        memory_db_path=tmp_path / "memory" / "memory.db",
            )
    try:
        indexed = await loop.index_project_memory(
            max_files=100,
            max_bytes_per_file=4_096,
        )
        assert indexed == 100

        hits = await loop.search_memory("function_100")
        assert hits
        assert all(hit.file_path.endswith("module-100.py") for hit in hits)

        hits = await loop.search_memory("function_99")
        assert not any(hit.file_path.endswith("module-99.py") for hit in hits)
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_lower_workspace_index_limit_prunes_unselected_documents(
    tmp_path,
) -> None:
    (tmp_path / "a.py").write_text("alpha_limit_marker\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("beta_limit_marker\n", encoding="utf-8")
    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="sqlite",
            )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=MemoryTestProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        config=config,
        enable_project_memory=True,
        memory_db_path=tmp_path / "memory" / "memory.db",
            )
    try:
        assert await loop.index_project_memory(max_files=2, max_bytes_per_file=4096) == 2
        assert await loop.search_memory("beta_limit_marker")

        (tmp_path / "b.py").write_text("stale_beta_limit_marker\n", encoding="utf-8")
        assert await loop.index_project_memory(max_files=1, max_bytes_per_file=4096) == 1
        assert await loop.search_memory("beta_limit_marker") == []
        assert await loop.search_memory("stale_beta_limit_marker") == []
        assert loop._memory_pipeline is not None
        assert loop._memory_pipeline.document_paths() == {"a.py"}
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_workspace_reindex_preserves_document_on_transient_read_error(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "stable.py"
    path.write_text("transient_read_memory_marker\n", encoding="utf-8")
    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="sqlite",
            )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=MemoryTestProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        config=config,
        enable_project_memory=True,
        memory_db_path=tmp_path / "memory" / "memory.db",
            )
    try:
        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=4096) == 1
        assert await loop.search_memory("transient_read_memory_marker")

        def fail_read(_path, _max_bytes_per_file=128_000):
            raise OSError("transient read failure")

        monkeypatch.setattr(loop, "_chunk_file", fail_read)
        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=4096) == 0
        assert await loop.search_memory("transient_read_memory_marker")
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_truncated_workspace_scan_does_not_evict_unseen_documents(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "a.py").write_text("alpha_scan_marker\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("beta_scan_marker\n", encoding="utf-8")
    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="sqlite",
            )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=MemoryTestProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        config=config,
        enable_project_memory=True,
        memory_db_path=tmp_path / "memory" / "memory.db",
            )
    try:
        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=4096) == 2
        assert await loop.search_memory("beta_scan_marker")

        monkeypatch.setattr("ash.core.loop.MAX_MEMORY_SCAN_ENTRIES", 1)
        await loop.index_project_memory(max_files=10, max_bytes_per_file=4096)
        assert await loop.search_memory("beta_scan_marker")
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_incomplete_nested_scan_does_not_evict_unseen_documents(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    nested = package / "nested.py"
    nested.write_text("nested_scan_memory_marker\n", encoding="utf-8")
    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="sqlite",
            )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=MemoryTestProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        config=config,
        enable_project_memory=True,
        memory_db_path=tmp_path / "memory" / "memory.db",
            )
    try:
        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=4096) == 1
        assert await loop.search_memory("nested_scan_memory_marker")

        real_iterdir = Path.iterdir

        def fail_nested_iterdir(path: Path):
            if path == package:
                raise OSError("transient directory enumeration failure")
            return real_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", fail_nested_iterdir)
        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=4096) == 0
        assert await loop.search_memory("nested_scan_memory_marker")
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_empty_unselected_workspace_file_drops_stale_memory(
    tmp_path,
) -> None:
    (tmp_path / "a.py").write_text("selected_empty_guard\n", encoding="utf-8")
    stale = tmp_path / "z.py"
    stale.write_text("unselected_empty_memory_marker\n", encoding="utf-8")
    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="sqlite",
            )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=MemoryTestProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        config=config,
        enable_project_memory=True,
        memory_db_path=tmp_path / "memory" / "memory.db",
            )
    try:
        assert await loop.index_project_memory(max_files=2, max_bytes_per_file=4096) == 2
        assert await loop.search_memory("unselected_empty_memory_marker")

        stale.write_text("", encoding="utf-8")
        assert await loop.index_project_memory(max_files=1, max_bytes_per_file=4096) == 1
        assert loop._memory_pipeline is not None
        assert "z.py" not in loop._memory_pipeline.document_paths()
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_workspace_reindex_forgets_documents_deleted_from_disk(
    tmp_path
) -> None:
    stale = tmp_path / "stale.py"
    stale.write_text("deleted_workspace_memory_marker\n", encoding="utf-8")
    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="sqlite",
            )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=MemoryTestProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        config=config,
        enable_project_memory=True,
        memory_db_path=tmp_path / "memory" / "memory.db",
            )
    try:
        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=4096) == 1
        assert await loop.search_memory("deleted_workspace_memory_marker")
        stale.unlink()
        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=4096) == 0
        assert await loop.search_memory("deleted_workspace_memory_marker") == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["oversized", "excluded", "empty"])
async def test_workspace_reindex_forgets_documents_that_become_ineligible(
    tmp_path,
    transition: str,
) -> None:
    stale = tmp_path / "stale.py"
    stale.write_text("ineligible_workspace_memory_marker\n", encoding="utf-8")
    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="sqlite",
            )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=MemoryTestProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        config=config,
        enable_project_memory=True,
        memory_db_path=tmp_path / "memory" / "memory.db",
            )
    try:
        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=64) == 1
        assert await loop.search_memory("ineligible_workspace_memory_marker")

        if transition == "oversized":
            stale.write_text("x" * 256, encoding="utf-8")
        elif transition == "excluded":
            assert loop._config is not None
            loop._config.repo_map_exclude_patterns.append("stale.py")
        else:
            stale.write_text("", encoding="utf-8")

        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=64) == 0
        assert await loop.search_memory("ineligible_workspace_memory_marker") == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_workspace_reindex_forgets_document_hidden_by_symlinked_parent(
    tmp_path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    stale = package / "stale.py"
    stale.write_text("symlinked_workspace_memory_marker\n", encoding="utf-8")
    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="sqlite",
            )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=MemoryTestProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        config=config,
        enable_project_memory=True,
        memory_db_path=tmp_path / "memory" / "memory.db",
            )
    try:
        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=4096) == 1
        assert await loop.search_memory("symlinked_workspace_memory_marker")

        hidden = tmp_path / ".package-saved"
        package.rename(hidden)
        try:
            package.symlink_to(hidden, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")

        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=4096) == 0
        assert await loop.search_memory("symlinked_workspace_memory_marker") == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_workspace_reindex_contains_directory_symlink_race(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    inside = package / "inside.py"
    inside.write_text("inside_memory_marker\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-memory"
    outside.mkdir()
    (outside / "outside.py").write_text("outside_memory_marker\n", encoding="utf-8")
    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="sqlite",
    )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=MemoryTestProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        config=config,
        enable_project_memory=True,
        memory_db_path=tmp_path / "memory" / "memory.db",
    )
    saved = tmp_path / ".package-saved"
    real_is_dir = Path.is_dir
    swapped = False

    def race_directory_check(path: Path) -> bool:
        nonlocal swapped
        result = real_is_dir(path)
        if path == package and result and not swapped:
            package.rename(saved)
            package.symlink_to(outside, target_is_directory=True)
            swapped = True
        return result

    try:
        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=4096) == 1
        assert await loop.search_memory("inside_memory_marker")

        monkeypatch.setattr(Path, "is_dir", race_directory_check)
        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=4096) == 0
        assert swapped is True
        assert await loop.search_memory("outside_memory_marker") == []
        assert await loop.search_memory("inside_memory_marker")
    finally:
        await loop.aclose()
        if package.is_symlink():
            package.unlink()
        if outside.exists():
            for child in outside.iterdir():
                child.unlink()
            outside.rmdir()


@pytest.mark.asyncio
async def test_workspace_reindex_rejects_replaced_workspace_root(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = workspace / "original.py"
    original.write_text("original_workspace_memory_marker\n", encoding="utf-8")
    config = AshConfig(
        model="openai/memory-test",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="sqlite",
    )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=MemoryTestProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=workspace),
        project_root=workspace,
        config=config,
        enable_project_memory=True,
        memory_db_path=tmp_path / "memory" / "memory.db",
    )
    saved = tmp_path / "workspace-original"
    replacement = tmp_path / "workspace-replacement"
    replacement.mkdir()
    (replacement / "replacement.py").write_text(
        "replacement_workspace_memory_marker\n",
        encoding="utf-8",
    )
    try:
        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=4096) == 1
        assert await loop.search_memory("original_workspace_memory_marker")

        workspace.rename(saved)
        replacement.rename(workspace)

        assert await loop.index_project_memory(max_files=10, max_bytes_per_file=4096) == 0
        assert await loop.search_memory("replacement_workspace_memory_marker") == []
        assert await loop.search_memory("original_workspace_memory_marker") == []
    finally:
        await loop.aclose()
