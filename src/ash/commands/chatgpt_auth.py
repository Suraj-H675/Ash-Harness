"""CLI-facing management for OpenAI Sign in with ChatGPT registrations."""

from __future__ import annotations

import json
from io import StringIO

from rich.console import Console
from rich.table import Table

from ash.commands.config import save_env_values
from ash.providers.openai_chatgpt_auth import (
    ChatGPTAccount,
    ChatGPTAuthManager,
    ChatGPTCredentialStore,
)
from ash.ui.safe_text import terminal_safe_text


def _account_payload(
    account: ChatGPTAccount,
    *,
    active_client_id: str = "",
) -> dict[str, object]:
    return {
        "client_id": account.client_id,
        "email": account.email,
        "active": account.client_id == active_client_id,
        "signed_in": account.signed_in,
        "plan_enabled": account.plan_enabled,
        "access_usable": account.access_usable(),
        "scopes": list(account.scopes),
    }


def render_chatgpt_status(*, json_output: bool = False) -> str:
    store = ChatGPTCredentialStore()
    account = store.active()
    state = store.credential_state()
    payload = {
        "auth_mode": "chatgpt",
        "state": state,
        "account": (
            None
            if account is None
            else _account_payload(account, active_client_id=account.client_id)
        ),
    }
    if json_output:
        return json.dumps(payload, indent=2, sort_keys=True)
    if account is None:
        return f"ChatGPT plan auth: {state}"
    email = terminal_safe_text(account.email or "(email unavailable)", single_line=True)
    return "\n".join(
        (
            f"ChatGPT plan auth: {state}",
            f"Account: {email}",
            f"Registration: {terminal_safe_text(account.client_id, single_line=True)}",
            f"Plan permission: {'enabled' if account.plan_enabled else 'not enabled'}",
        )
    )


def render_chatgpt_accounts(*, json_output: bool = False) -> str:
    store = ChatGPTCredentialStore()
    active = store.active()
    active_client_id = active.client_id if active is not None else ""
    accounts = [
        _account_payload(account, active_client_id=active_client_id)
        for account in store.list_accounts()
    ]
    if json_output:
        return json.dumps({"accounts": accounts}, indent=2, sort_keys=True)
    if not accounts:
        return "No saved ChatGPT registrations."
    buffer = StringIO()
    console = Console(file=buffer, soft_wrap=True)
    table = Table(title="Saved ChatGPT registrations")
    table.add_column("Active")
    table.add_column("Email")
    table.add_column("Registration")
    table.add_column("Session")
    table.add_column("Plan")
    for item in accounts:
        table.add_row(
            "*" if item["active"] else "",
            terminal_safe_text(str(item["email"] or "(unknown)"), single_line=True),
            terminal_safe_text(str(item["client_id"]), single_line=True),
            "signed in" if item["signed_in"] else "signed out",
            "enabled" if item["plan_enabled"] else "not enabled",
        )
    console.print(table)
    return buffer.getvalue().rstrip()


async def login_chatgpt(client_id: str | None = None) -> ChatGPTAccount:
    account = await ChatGPTAuthManager().login(client_id=client_id)
    if account.plan_enabled:
        save_env_values({"ASH_OPENAI_AUTH_MODE": "chatgpt"})
    return account


async def logout_chatgpt() -> tuple[ChatGPTAccount | None, bool]:
    return await ChatGPTAuthManager().logout()


def use_chatgpt_account(client_id: str) -> ChatGPTAccount:
    from ash.providers.openai_chatgpt_auth import (
        ChatGPTLoginRequired,
        ChatGPTPlanUnavailable,
    )

    store = ChatGPTCredentialStore()
    account = store.get(client_id)
    if account is None:
        raise KeyError(f"unknown ChatGPT registration: {client_id}")
    if not account.signed_in:
        raise ChatGPTLoginRequired(
            "saved ChatGPT registration is signed out; reauthorize it first"
        )
    if not account.plan_enabled:
        raise ChatGPTPlanUnavailable(
            "saved ChatGPT registration does not authorize plan usage"
        )
    selected = store.set_active(client_id)
    save_env_values({"ASH_OPENAI_AUTH_MODE": "chatgpt"})
    return selected


async def list_chatgpt_models() -> tuple[tuple[str, str], ...]:
    return await ChatGPTAuthManager().list_models()


def render_chatgpt_models(
    models: tuple[tuple[str, str], ...],
    *,
    json_output: bool = False,
) -> str:
    if json_output:
        return json.dumps(
            {
                "models": [
                    {"slug": slug, "display_name": display_name}
                    for slug, display_name in models
                ]
            },
            indent=2,
            sort_keys=True,
        )
    return "\n".join(
        f"{terminal_safe_text(display_name, single_line=True)}  "
        f"({terminal_safe_text(slug, single_line=True)})"
        for slug, display_name in models
    )
