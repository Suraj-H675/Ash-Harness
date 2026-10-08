import pytest

from ash.ui.transcript import Transcript, TranscriptEntry


def test_transcript_streaming_replaces_immutable_snapshots() -> None:
    transcript = Transcript()
    entry_id = transcript.begin("assistant", title="ash")
    original = transcript.snapshot()[0]

    transcript.append_delta(entry_id, "hello")
    updated = transcript.snapshot()[0]
    transcript.finalize(entry_id)

    assert original.content == ""
    assert original.finalized is False
    assert updated.content == "hello"
    assert transcript.snapshot()[0].finalized is True


def test_transcript_prunes_old_finalized_entries_but_keeps_active() -> None:
    transcript = Transcript(max_entries=2, max_characters=5)
    active = transcript.begin("assistant")
    transcript.append_delta(active, "abcdef")
    transcript.append("status", "old")
    transcript.append("status", "new")

    entries = transcript.snapshot()
    assert [entry.entry_id for entry in entries] == [active]
    assert entries[0].content == "abcdef"


@pytest.mark.parametrize("method", ["append_delta", "replace_content"])
def test_transcript_prunes_reclaimable_history_during_streaming(method: str) -> None:
    transcript = Transcript(max_entries=8, max_characters=10)
    old = transcript.append("status", "stored")
    active = transcript.begin("assistant")
    events = []
    transcript.subscribe(events.append)

    getattr(transcript, method)(active, "current response")

    assert [entry.entry_id for entry in transcript.snapshot()] == [active]
    assert transcript.snapshot()[0].content == "current response"
    assert transcript.omitted_entries == 1
    assert [event.action for event in events] == ["updated", "pruned"]
    assert old != active

    transcript.replace_content(active, "short")
    transcript.finalize(active)
    transcript.append("status", "extra")
    assert [entry.content for entry in transcript.snapshot()] == ["short", "extra"]


def test_transcript_character_budget_tracks_replace_remove_and_clear() -> None:
    transcript = Transcript(max_characters=8)
    transcript.replace(
        [
            TranscriptEntry("first", "user", "abc"),
            TranscriptEntry("second", "user", "def"),
        ]
    )
    pending = transcript.begin("assistant")
    transcript.append_delta(pending, "xy")
    assert len(transcript.snapshot()) == 3

    transcript.remove("first")
    transcript.append_delta(pending, "123")
    assert [entry.content for entry in transcript.snapshot()] == ["def", "xy123"]

    transcript.clear()
    transcript.append("user", "abcdefgh")
    assert len(transcript.snapshot()) == 1
    transcript.append("status", "z")
    assert [entry.content for entry in transcript.snapshot()] == ["z"]


def test_transcript_subscription_is_ordered_and_unsubscribes() -> None:
    transcript = Transcript()
    events = []
    unsubscribe = transcript.subscribe(events.append)

    entry_id = transcript.begin("reasoning")
    transcript.append_delta(entry_id, "inspect")
    transcript.finalize(entry_id)
    unsubscribe()
    transcript.append("status", "ignored")

    assert [event.action for event in events] == ["added", "updated", "finalized"]
    assert [event.revision for event in events] == [1, 2, 3]


def test_transcript_can_replace_and_remove_ephemeral_entry() -> None:
    transcript = Transcript()
    events = []
    transcript.subscribe(events.append)
    entry_id = transcript.begin("status", title="working")

    updated = transcript.replace_content(entry_id, "Thinking…")
    removed = transcript.remove(entry_id)

    assert updated.content == "Thinking…"
    assert updated.finalized is False
    assert removed.entry_id == entry_id
    assert transcript.snapshot() == ()
    assert [event.action for event in events] == ["added", "updated", "removed"]


def test_transcript_replacement_applies_bounds_and_reports_omitted_entries() -> None:
    transcript = Transcript(max_entries=2, max_characters=8)
    transcript.replace(
        TranscriptEntry(str(index), "user", content, title="you")
        for index, content in enumerate(("aa", "bb", "cc"))
    )

    assert [entry.content for entry in transcript.snapshot()] == ["bb", "cc"]
    assert transcript.omitted_entries == 1


def test_transcript_rejects_invalid_limits_and_finalized_updates() -> None:
    for kwargs in ({"max_entries": 0}, {"max_characters": 0}):
        try:
            Transcript(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid transcript limit accepted")

    transcript = Transcript()
    entry_id = transcript.append("assistant", "done")
    try:
        transcript.append_delta(entry_id, "more")
    except ValueError:
        pass
    else:
        raise AssertionError("finalized transcript entry accepted a delta")
