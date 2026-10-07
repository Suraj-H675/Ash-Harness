"""Bounded external API-key helper support for provider credential pools."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, AsyncGenerator

from ash.providers.base import (
    CompletionStopCategory,
    ProviderABC,
    ProviderIncompleteStreamError,
    ProviderTerminalError,
    StreamChunk,
    completion_stop_category,
    stream_chunk_commits_provider,
)
from ash.providers.capabilities import ProviderCapabilities
from ash.providers.messages import MessageInput
from ash.providers.retry import (
    ProviderFailureCategory,
    classify_provider_failure,
)
from ash.safety.environment import build_scrubbed_environment, resolve_host_executable
from ash.safety.path_scope import is_relative_to
from ash.sandbox.process_utils import (
    ProcessOutputLimitExceeded,
    ProcessTreeError,
    ProcessTreeUnavailable,
    communicate_process,
    prepare_process_tree,
    settle_process_tree_after_cancellation,
    terminate_process_tree,
)


MAX_HELPER_OUTPUT_BYTES = 32 * 1024
MAX_HELPER_CREDENTIAL_BYTES = 16 * 1024


class CredentialHelperError(RuntimeError):
    """A helper could not safely provide one usable credential."""

    retriable = False
    provider_failure_category = ProviderFailureCategory.CREDENTIAL_SOURCE


class CredentialHelperSource:
    """Resolve and cache one helper-provided credential in memory only."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        env_names: Sequence[str] = (),
        timeout_seconds: float = 10.0,
        ttl_seconds: float = 300.0,
        workspace_root: str | Path,
        state_directory: str | Path,
        clock=time.monotonic,
    ) -> None:
        if not command:
            raise ValueError("credential helper command cannot be empty")
        self.command = tuple(command)
        self.env_names = tuple(env_names)
        self.timeout_seconds = float(timeout_seconds)
        self.ttl_seconds = float(ttl_seconds)
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.state_directory = Path(state_directory).expanduser().resolve(
            strict=False
        )
        self._clock = clock
        self._lock = asyncio.Lock()
        self._credential: str | None = None
        self._expires_at = 0.0
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    def clear(self) -> None:
        """Drop the cached credential without persisting it anywhere."""

        self._credential = None
        self._expires_at = 0.0

    async def resolve(self, *, force_refresh: bool = False) -> str:
        now = self._clock()
        if (
            not force_refresh
            and self._credential is not None
            and now < self._expires_at
        ):
            return self._credential
        async with self._lock:
            now = self._clock()
            if (
                not force_refresh
                and self._credential is not None
                and now < self._expires_at
            ):
                return self._credential
            credential = await self._run()
            self._credential = credential
            self._expires_at = self._clock() + self.ttl_seconds
            self._generation += 1
            return credential

    async def _run(self) -> str:
        executable = self._resolve_executable()
        state_directory = self.state_directory
        if is_relative_to(state_directory, self.workspace_root):
            raise CredentialHelperError(
                "credential helper state directory must be outside the workspace"
            )
        try:
            state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            raise CredentialHelperError(
                "credential helper state directory is unavailable"
            ) from None
        if not state_directory.is_dir():
            raise CredentialHelperError(
                "credential helper state directory is unavailable"
            )
        try:
            process_tree_plan = prepare_process_tree(
                workspace_root=self.workspace_root
            )
        except ProcessTreeUnavailable:
            raise CredentialHelperError(
                "credential helper process-tree isolation is unavailable"
            ) from None
        environment = build_scrubbed_environment(self.env_names)
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *self.command[1:],
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(state_directory),
                env=environment,
                **process_tree_plan.spawn_options,
            )
        except OSError:
            raise CredentialHelperError("credential helper could not be started") from None

        try:
            stdout, _stderr = await asyncio.wait_for(
                communicate_process(
                    process,
                    max_output_bytes=MAX_HELPER_OUTPUT_BYTES,
                    process_tree_plan=process_tree_plan,
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            try:
                await terminate_process_tree(process, plan=process_tree_plan)
            except ProcessTreeError:
                raise CredentialHelperError(
                    "credential helper timed out and cleanup could not be confirmed"
                ) from None
            raise CredentialHelperError("credential helper timed out") from None
        except asyncio.CancelledError as cancellation:
            cleanup_error, cleanup_cancelled = (
                await settle_process_tree_after_cancellation(
                    process,
                    plan=process_tree_plan,
                )
            )
            if cleanup_error is not None:
                cancellation.add_note(
                    "credential helper process-tree cleanup failed"
                )
            if cleanup_cancelled:
                cancellation.add_note(
                    "credential helper process-tree cleanup was cancelled"
                )
            raise
        except ProcessOutputLimitExceeded:
            raise CredentialHelperError(
                "credential helper output exceeded the safety limit"
            ) from None
        except Exception:
            cleanup_error, cleanup_cancelled = (
                await settle_process_tree_after_cancellation(
                    process,
                    plan=process_tree_plan,
                )
            )
            if cleanup_cancelled:
                raise asyncio.CancelledError(
                    "credential helper cleanup was cancelled"
                ) from None
            if cleanup_error is not None:
                raise CredentialHelperError(
                    "credential helper failed and cleanup could not be confirmed"
                ) from None
            raise CredentialHelperError("credential helper failed") from None

        try:
            await terminate_process_tree(process, plan=process_tree_plan)
        except ProcessTreeError:
            raise CredentialHelperError(
                "credential helper cleanup could not be confirmed"
            ) from None
        if process.returncode != 0:
            raise CredentialHelperError(
                f"credential helper exited with status {process.returncode}"
            )
        return _validate_credential_output(stdout)

    def _resolve_executable(self) -> str:
        command = self.command[0]
        path = Path(command).expanduser()
        if path.is_absolute():
            try:
                candidate = path.resolve()
            except OSError:
                candidate = path
            if (
                not candidate.is_file()
                or not os.access(candidate, os.X_OK)
                or is_relative_to(candidate, self.workspace_root)
            ):
                raise CredentialHelperError(
                    "credential helper executable is unavailable or untrusted"
                )
            return str(candidate)
        if len(path.parts) != 1 or command != path.name:
            raise CredentialHelperError(
                "credential helper executable must be a bare host command "
                "or an absolute path"
            )
        resolved = resolve_host_executable(
            command,
            workspace_root=self.workspace_root,
            cwd=self.state_directory,
        )
        if resolved is None:
            raise CredentialHelperError(
                "credential helper executable is unavailable or untrusted"
            )
        return resolved


class CredentialHelperProvider(ProviderABC):
    """Provider profile backed by one refreshable external credential helper."""

    def __init__(
        self,
        prototype: ProviderABC,
        provider_factory: Callable[[str], ProviderABC],
        source: CredentialHelperSource,
    ) -> None:
        self.provider_family = prototype.provider_family
        self._prototype = prototype
        self._provider_factory = provider_factory
        self._source = source
        self._active = prototype
        self._active_generation = 0
        self._provider_lock = asyncio.Lock()
        self._owned: list[ProviderABC] = [prototype]
        self._closed_provider_ids: set[int] = set()
        self._configured_max_tokens: int | None = None

    @property
    def model_name(self) -> str:
        return self._active.model_name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._active.capabilities

    def count_tokens(self, text: str) -> int:
        return self._active.count_tokens(text)

    def configure_max_tokens(self, max_tokens: int) -> None:
        super().configure_max_tokens(max_tokens)
        self._configured_max_tokens = max_tokens
        for provider in self._owned:
            provider.configure_max_tokens(max_tokens)

    def configure_reasoning_effort(self, effort: str | None) -> None:
        super().configure_reasoning_effort(effort)
        for provider in self._owned:
            provider.configure_reasoning_effort(effort)

    async def detect_capabilities(
        self,
        *,
        refresh: bool = False,
    ) -> ProviderCapabilities:
        provider = await self._resolve_provider()
        detect = getattr(provider, "detect_capabilities", None)
        if not callable(detect):
            return provider.capabilities
        try:
            try:
                result = await detect(refresh=refresh)
            except TypeError as exc:
                if "refresh" not in str(exc):
                    raise
                result = await detect()
        except Exception as exc:  # noqa: BLE001
            failure = classify_provider_failure(exc)
            if failure.category is not ProviderFailureCategory.AUTH:
                raise
            provider = await self._resolve_provider(force_refresh=True)
            detect = getattr(provider, "detect_capabilities", None)
            if not callable(detect):
                return provider.capabilities
            try:
                result = await detect(refresh=True)
            except TypeError as retry_exc:
                if "refresh" not in str(retry_exc):
                    raise
                result = await detect()
        return (
            result
            if isinstance(result, ProviderCapabilities)
            else provider.capabilities
        )

    async def stream_chat(
        self,
        messages: Sequence[MessageInput],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        provider = await self._resolve_provider()
        refreshed = False
        while True:
            emitted_output = False
            exposed_terminal = False
            saw_terminal = False
            try:
                async for chunk in provider.stream_chat(
                    messages,
                    temperature=temperature,
                    tools=tools,
                ):
                    has_output = stream_chunk_commits_provider(chunk)
                    if (
                        chunk.is_done
                        and completion_stop_category(chunk.stop_reason)
                        == CompletionStopCategory.ERROR
                        and not emitted_output
                        and not has_output
                    ):
                        raise ProviderTerminalError(chunk.stop_reason)
                    emitted_output = emitted_output or has_output
                    saw_terminal = saw_terminal or chunk.is_done
                    exposed_terminal = exposed_terminal or chunk.is_done
                    yield chunk
                if not saw_terminal:
                    raise ProviderIncompleteStreamError(
                        f"provider {provider.model_name!r} ended before a terminal chunk"
                    )
                return
            except Exception as exc:  # noqa: BLE001
                if emitted_output or exposed_terminal or refreshed:
                    raise
                failure = classify_provider_failure(exc)
                if failure.category is not ProviderFailureCategory.AUTH:
                    raise
                provider = await self._resolve_provider(force_refresh=True)
                refreshed = True

    async def aclose(self) -> None:
        self._source.clear()
        pending = [
            provider
            for provider in self._owned
            if id(provider) not in self._closed_provider_ids
        ]
        if not pending:
            return
        outcomes = await asyncio.gather(
            *(provider.aclose() for provider in pending),
            return_exceptions=True,
        )
        failures: list[BaseException] = []
        cancellation: asyncio.CancelledError | None = None
        for provider, outcome in zip(pending, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failures.append(outcome)
                if isinstance(outcome, asyncio.CancelledError) and cancellation is None:
                    cancellation = outcome
                continue
            self._closed_provider_ids.add(id(provider))
        if cancellation is not None:
            raise cancellation
        if failures:
            raise RuntimeError(
                f"failed to close {len(failures)} helper credential provider(s)"
            ) from None
        self._active = self._prototype
        self._owned = [self._prototype]

    async def _resolve_provider(
        self,
        *,
        force_refresh: bool = False,
    ) -> ProviderABC:
        async with self._provider_lock:
            try:
                credential = await self._source.resolve(
                    force_refresh=force_refresh
                )
            except asyncio.CancelledError:
                raise
            except CredentialHelperError:
                raise
            except Exception:
                raise CredentialHelperError("credential helper failed") from None
            generation = self._source.generation
            if (
                not force_refresh
                and self._active_generation == generation
                and self._active is not self._prototype
            ):
                return self._active
            try:
                provider = self._provider_factory(credential)
            except Exception:
                raise CredentialHelperError(
                    "credential helper output could not initialize the provider"
                ) from None
            if self._configured_max_tokens is not None:
                provider.configure_max_tokens(self._configured_max_tokens)
            if self.configured_reasoning_effort is not None:
                provider.configure_reasoning_effort(self.configured_reasoning_effort)
            self._owned.append(provider)
            previous = self._active
            self._active = provider
            self._active_generation = generation
            if previous is not self._prototype:
                try:
                    await previous.aclose()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise CredentialHelperError(
                        "previous credential provider could not be retired"
                    ) from None
                self._closed_provider_ids.add(id(previous))
                self._owned.remove(previous)
            return provider


def _validate_credential_output(stdout: bytes) -> str:
    if len(stdout) > MAX_HELPER_CREDENTIAL_BYTES:
        raise CredentialHelperError(
            "credential helper output is too large to be a credential"
        )
    try:
        value = stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise CredentialHelperError(
            "credential helper output must be valid UTF-8"
        ) from None
    lines = value.splitlines()
    if len(lines) != 1:
        raise CredentialHelperError(
            "credential helper must return exactly one credential line"
        )
    credential = lines[0]
    if (
        not credential
        or credential != credential.strip()
        or any(character.isspace() or ord(character) < 32 for character in credential)
    ):
        raise CredentialHelperError(
            "credential helper returned an invalid credential line"
        )
    return credential
