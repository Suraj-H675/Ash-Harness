"""Remote skill registry for community skill discovery."""

from __future__ import annotations

import httpx
from dataclasses import dataclass


@dataclass
class RemoteSkill:
    name: str
    description: str
    source: str  # URL or "community"
    trigger: str = ""


async def search_registry(query: str) -> list[RemoteSkill]:
    """Search the community skill registry."""
    # Placeholder: hit a skill registry API
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://registry.ash.dev/skills/search", params={"q": query}
        )
        resp.raise_for_status()
        data = resp.json()
        return [RemoteSkill(**s) for s in data.get("skills", [])]
