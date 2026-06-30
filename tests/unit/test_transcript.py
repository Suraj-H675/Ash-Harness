from ui.transcript import Transcript


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
