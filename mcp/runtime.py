"""MCP tool discovery and Ash tool adapters."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, Field, create_model

from mcp.client import MCPClient
from mcp.server import MCPServerConfig
from safety.guard import SafetyGuard
from tools.base import BaseTool, ToolResult, count_output_tokens


def _schema_model(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields: dict[str, tuple[Any, Any]] = {}
    type_map: dict[str, Any] = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list[Any],
        "object": dict[str, Any],
    }
    for field_name, definition in properties.items():
        python_type = type_map.get(definition.get("type"), Any)
        default = ... if field_name in required else None
        fields[field_name] = (
            python_type,
            Field(default, description=definition.get("description", "")),
        )
    return create_model(name, **fields)  # type: ignore[call-overload]


class MCPTool(BaseTool):
    def __init__(
        self,
        safety_guard: SafetyGuard,
        *,
        client: MCPClient,
        server_name: str,
        definition: dict[str, Any],
    ) -> None:
        super().__init__(safety_guard)
        self.client = client
        self.remote_name = definition["name"]
        self.name = f"mcp__{server_name}__{self.remote_name}"
        self.description = definition.get("description", "MCP tool")
        self.args_schema = _schema_model(
            f"MCP_{server_name}_{self.remote_name}_Args",
            definition.get("inputSchema", {}),
        )

    async def run(self, **kwargs: Any) -> ToolResult:
        arguments = self.args_schema(**kwargs).model_dump(exclude_none=True)
        result = await self.client.call_tool(self.remote_name, arguments)
        content = result.get("content", [])
        rendered: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                rendered.append(str(item.get("text", "")))
            else:
                rendered.append(json.dumps(item, ensure_ascii=False))
        output = "\n".join(rendered)
        is_error = bool(result.get("isError", False))
        return ToolResult(
            success=not is_error,
            output=output if not is_error else "",
            error=output if is_error else None,
            token_count=count_output_tokens(output),
        )


class MCPReadResourceArgs(BaseModel):
    server: str
    uri: str


class MCPReadResourceTool(BaseTool):
    name = "mcp_read_resource"
    description = "Read one resource from a connected MCP server."
    args_schema = MCPReadResourceArgs

    def __init__(self, safety_guard: SafetyGuard, runtime: "MCPRuntime") -> None:
        super().__init__(safety_guard)
        self.runtime = runtime

    async def run(self, **kwargs: Any) -> ToolResult:
        args = MCPReadResourceArgs(**kwargs)
        client = self.runtime.clients.get(args.server)
        if client is None:
            return ToolResult(success=False, output="", error="Unknown MCP server")
        result = await client.read_resource(args.uri)
        output = json.dumps(result, ensure_ascii=False)
        return ToolResult(
            success=True, output=output, token_count=count_output_tokens(output)
        )


class MCPGetPromptArgs(BaseModel):
    server: str
    name: str
    arguments: dict[str, str] = Field(default_factory=dict)


class MCPGetPromptTool(BaseTool):
    name = "mcp_get_prompt"
    description = "Render one prompt from a connected MCP server."
    args_schema = MCPGetPromptArgs

    def __init__(self, safety_guard: SafetyGuard, runtime: "MCPRuntime") -> None:
        super().__init__(safety_guard)
        self.runtime = runtime

    async def run(self, **kwargs: Any) -> ToolResult:
        args = MCPGetPromptArgs(**kwargs)
        client = self.runtime.clients.get(args.server)
        if client is None:
            return ToolResult(success=False, output="", error="Unknown MCP server")
        result = await client.get_prompt(args.name, args.arguments)
        output = json.dumps(result, ensure_ascii=False)
        return ToolResult(
            success=True, output=output, token_count=count_output_tokens(output)
        )


class MCPRuntime:
    def __init__(
        self,
        configs: dict[str, MCPServerConfig],
        safety_guard: SafetyGuard,
    ) -> None:
        self.configs = configs
        self.safety_guard = safety_guard
        self.clients: dict[str, MCPClient] = {}
        self.errors: dict[str, str] = {}

    async def start(self) -> dict[str, BaseTool]:
        tools: dict[str, BaseTool] = {}
        for name, config in self.configs.items():
            client = MCPClient(config)
            try:
                await client.connect()
                definitions = await client.list_tools()
            except Exception as exc:  # noqa: BLE001
                self.errors[name] = str(exc)
                await client.disconnect()
                continue
            self.clients[name] = client
            for definition in definitions:
                tool = MCPTool(
                    self.safety_guard,
                    client=client,
                    server_name=name,
                    definition=definition,
                )
                tools[tool.name] = tool
        if self.clients:
            resource_tool = MCPReadResourceTool(self.safety_guard, self)
            prompt_tool = MCPGetPromptTool(self.safety_guard, self)
            tools[resource_tool.name] = resource_tool
            tools[prompt_tool.name] = prompt_tool
        return tools

    async def close(self) -> None:
        await asyncio.gather(
            *(client.disconnect() for client in self.clients.values()),
            return_exceptions=True,
        )
        self.clients.clear()

    async def list_resources(self) -> list[dict[str, Any]]:
        return await self._list_capability("list_resources")

    async def list_prompts(self) -> list[dict[str, Any]]:
        return await self._list_capability("list_prompts")

    async def _list_capability(self, method: str) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for server, client in self.clients.items():
            try:
                items = await getattr(client, method)()
            except Exception as exc:  # noqa: BLE001
                self.errors[f"{server}:{method}"] = str(exc)
                continue
            output.extend({"server": server, **item} for item in items)
        return output
