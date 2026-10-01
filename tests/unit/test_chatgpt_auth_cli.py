from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ash.cli import main


def test_chatgpt_auth_status_dispatches_without_loading_provider_config(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        "ash.commands.chatgpt_auth.render_chatgpt_status",
        lambda *, json_output: json.dumps({"state": "usable", "json": json_output}),
    )

    assert main(["auth", "chatgpt", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "usable"


def test_chatgpt_login_retains_plan_disabled_registration_but_exits_nonzero(
    monkeypatch,
    capsys,
) -> None:
    account = SimpleNamespace(
        plan_enabled=False,
        email="user@example.com",
        client_id="oaiapp_saved",
    )

    async def login(_client_id):
        return account

    monkeypatch.setattr("ash.commands.chatgpt_auth.login_chatgpt", login)

    assert main(["auth", "chatgpt", "login", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "client_id": "oaiapp_saved",
        "email": "user@example.com",
        "ok": False,
        "plan_enabled": False,
    }


def test_chatgpt_account_use_requires_signed_in_plan_permission(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        "ash.commands.chatgpt_auth.use_chatgpt_account",
        lambda client_id: SimpleNamespace(
            plan_enabled=True,
            signed_in=True,
            email="user@example.com",
            client_id=client_id,
        ),
    )

    assert main(["auth", "chatgpt", "use", "oaiapp_saved", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["client_id"] == "oaiapp_saved"


def test_chatgpt_account_use_does_not_activate_ineligible_registration(
    monkeypatch,
) -> None:
    from ash.commands import chatgpt_auth
    from ash.providers.openai_chatgpt_auth import ChatGPTPlanUnavailable

    class Store:
        def get(self, _client_id):
            return SimpleNamespace(signed_in=True, plan_enabled=False)

        def set_active(self, _client_id):
            raise AssertionError("ineligible registration must not become active")

    monkeypatch.setattr(chatgpt_auth, "ChatGPTCredentialStore", Store)

    with pytest.raises(ChatGPTPlanUnavailable, match="does not authorize"):
        chatgpt_auth.use_chatgpt_account("oaiapp_ineligible")


def test_chatgpt_logout_reports_unconfirmed_remote_revocation(
    monkeypatch,
    capsys,
) -> None:
    async def logout():
        return SimpleNamespace(client_id="oaiapp_saved"), False

    monkeypatch.setattr("ash.commands.chatgpt_auth.logout_chatgpt", logout)

    assert main(["auth", "chatgpt", "logout", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["had_active_account"] is True
    assert payload["remote_revocation_confirmed"] is False
