from __future__ import annotations

import json

import pytest

from ash.providers.replay_state import (
    ProviderReplayStateCipher,
    ProviderReplayStateError,
)


def test_provider_replay_state_round_trips_across_instances_without_plaintext(
    tmp_path,
) -> None:
    directory = tmp_path / "provider-state"
    plaintext = "private reasoning OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz"
    first = ProviderReplayStateCipher(directory, trusted_root=tmp_path)

    sealed = first.seal(
        provider="deepseek",
        kind="reasoning_content",
        text=plaintext,
    )

    assert plaintext not in json.dumps(sealed)
    second = ProviderReplayStateCipher(directory, trusted_root=tmp_path)
    assert (
        second.open(
            sealed,
            provider="deepseek",
            kind="reasoning_content",
        )
        == plaintext
    )


def test_provider_replay_state_rejects_tampered_ciphertext(tmp_path) -> None:
    directory = tmp_path / "provider-state"
    cipher = ProviderReplayStateCipher(directory, trusted_root=tmp_path)
    sealed = cipher.seal(
        provider="deepseek",
        kind="reasoning_content",
        text="exact reasoning",
    )
    ciphertext = str(sealed["ciphertext"])
    replacement = "A" if ciphertext[0] != "A" else "B"
    tampered = {**sealed, "ciphertext": replacement + ciphertext[1:]}

    with pytest.raises(
        ProviderReplayStateError,
        match="integrity verification",
    ):
        cipher.open(
            tampered,
            provider="deepseek",
            kind="reasoning_content",
        )


def test_provider_replay_state_does_not_regenerate_missing_decryption_key(
    tmp_path,
) -> None:
    first = ProviderReplayStateCipher(
        tmp_path / "first",
        trusted_root=tmp_path,
    )
    sealed = first.seal(
        provider="deepseek",
        kind="reasoning_content",
        text="must remain recoverable",
    )
    unrelated = ProviderReplayStateCipher(
        tmp_path / "second",
        trusted_root=tmp_path,
    )

    with pytest.raises(ProviderReplayStateError, match="key is missing"):
        unrelated.open(
            sealed,
            provider="deepseek",
            kind="reasoning_content",
        )
