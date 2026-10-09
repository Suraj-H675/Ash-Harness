from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Any

from hypothesis import settings, strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule

from ash.agents.shared_state import IPCMessage, SharedState


JsonValue = Any
json_value = st.recursive(
    st.none() | st.booleans() | st.integers(-1000, 1000) | st.text(max_size=12),
    lambda children: st.lists(children, max_size=3)
    | st.dictionaries(st.text(max_size=8), children, max_size=3),
    max_leaves=8,
)
json_content = st.dictionaries(st.text(max_size=8), json_value, max_size=4)


@dataclass(frozen=True)
class ExpectedMessage:
    sender: str
    recipient: str
    message_type: str
    content: dict[str, JsonValue]
    delivered: bool = False


class SharedStateLifecycleMachine(RuleBasedStateMachine):
    sent_ids = Bundle("sent_ids")
    senders = st.sampled_from(("agent-a", "agent-b", "agent-c"))
    recipients = st.sampled_from(("agent-a", "agent-b", "lead"))

    def __init__(self) -> None:
        super().__init__()
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self._temporary_directory.name) / "shared-state.db"
        self.state = SharedState(self.database)
        self.expected: dict[int, ExpectedMessage] = {}

    @rule(
        target=sent_ids,
        sender=senders,
        recipient=recipients,
        message_type=st.text(min_size=1, max_size=12),
        content=json_content,
    )
    def send(
        self,
        sender: str,
        recipient: str,
        message_type: str,
        content: dict[str, JsonValue],
    ) -> int:
        message_id = self.state.send_message(
            sender, recipient, message_type, content
        )
        self.expected[message_id] = ExpectedMessage(
            sender=sender,
            recipient=recipient,
            message_type=message_type,
            content=content,
        )
        return message_id

    @rule(recipient=recipients)
    def fetch_pending(self, recipient: str) -> None:
        actual_ids = [
            message.message_id
            for message in self.state.fetch_messages(recipient, limit=1000)
        ]
        expected_ids = [
            message_id
            for message_id, message in self.expected.items()
            if message.recipient == recipient and not message.delivered
        ]
        assert set(actual_ids) == set(expected_ids)

    @rule(message_id=sent_ids)
    def mark_delivered(self, message_id: int) -> None:
        assert self.state.mark_delivered([message_id]) == 1
        previous = self.expected[message_id]
        self.expected[message_id] = ExpectedMessage(
            sender=previous.sender,
            recipient=previous.recipient,
            message_type=previous.message_type,
            content=previous.content,
            delivered=True,
        )

    @rule()
    def close_and_reopen(self) -> None:
        self.state.close()
        self.state = SharedState.open_existing(self.database)

    @invariant()
    def persisted_inboxes_match_the_model(self) -> None:
        for recipient in ("agent-a", "agent-b", "lead"):
            full = self.state.fetch_messages(
                recipient, undelivered_only=False, limit=1000
            )
            pending = self.state.fetch_messages(recipient, limit=1000)
            assert self._messages_by_id(full) == {
                message_id: expected
                for message_id, expected in self.expected.items()
                if expected.recipient == recipient
            }
            assert [message.message_id for message in pending] == [
                message.message_id for message in full if not message.delivered
            ]

    @staticmethod
    def _messages_by_id(messages: list[IPCMessage]) -> dict[int, ExpectedMessage]:
        return {
            message.message_id: ExpectedMessage(
                sender=message.sender_id,
                recipient=message.recipient_id,
                message_type=message.message_type,
                content=message.content,
                delivered=message.delivered,
            )
            for message in messages
        }

    def teardown(self) -> None:
        self.state.close()
        self._temporary_directory.cleanup()


class TestSharedStateLifecycle(SharedStateLifecycleMachine.TestCase):
    settings = settings(
        max_examples=12,
        stateful_step_count=20,
        derandomize=True,
        deadline=None,
    )
