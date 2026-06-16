"""JSON-RPC 2.0 server for Ash."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ash.core.loop import AshLoop


class JSONRPCServer:
    def __init__(self, loop: "AshLoop") -> None:
        self.loop = loop

    async def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        method = request.get("method", "")
        params = request.get("params", {})
        request_id = request.get("id")

        if method == "run_turn":
            result = await self.loop.run_turn(params["input"])
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }
