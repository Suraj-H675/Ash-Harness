"""Reusable embedded MCP server programs for unit tests."""

FAKE_MCP_SERVER = r"""
import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "server/discover":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "legacy"}}), flush=True)
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}, "resources": {}, "prompts": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]}
    elif method == "tools/call":
        text = message["params"]["arguments"]["text"]
        if text == "__environment__":
            text = f"{os.getenv('SERVER_TOKEN', 'missing')}|{os.getenv('UNRELATED_SECRET', 'missing')}"
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    elif method == "resources/list":
        result = {"resources": [{"uri": "file:///example", "name": "example"}]}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": [{"uriTemplate": "file:///{path}", "name": "file"}]}
    elif method == "prompts/list":
        result = {"prompts": [{"name": "review", "description": "Review"}]}
    elif method == "resources/read":
        result = {"contents": [{"uri": "file:///example", "text": "resource text"}]}
    elif method == "prompts/get":
        result = {"messages": [{"role": "user", "content": {"type": "text", "text": "review it"}}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""

INTERACTIVE_MCP_SERVER = r"""
import json, sys
pending_tools_id = None
root_uri = ""
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "server/discover":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "legacy"}}), flush=True)
    elif message.get("method") == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {"listChanged": True}, "logging": {}},
            "serverInfo": {"name": "interactive", "version": "1"},
            "instructions": "server guidance",
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif message.get("method") == "notifications/initialized":
        print(json.dumps({"jsonrpc": "2.0", "id": "roots-1", "method": "roots/list", "params": {}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "ready"}}), flush=True)
    elif message.get("method") == "tools/list":
        if root_uri:
            result = {"tools": [{"name": "root", "description": root_uri, "inputSchema": {"type": "object"}}]}
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
        else:
            pending_tools_id = message["id"]
    elif message.get("id") == "roots-1" and "result" in message:
        root_uri = message["result"]["roots"][0]["uri"]
        if pending_tools_id is not None:
            result = {"tools": [{"name": "root", "description": root_uri, "inputSchema": {"type": "object"}}]}
            print(json.dumps({"jsonrpc": "2.0", "id": pending_tools_id, "result": result}), flush=True)
            pending_tools_id = None
"""

DYNAMIC_MCP_SERVER = r"""
import json, sys
state = "old"
list_count = 0
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "legacy"}}), flush=True)
        continue
    if method == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "dynamic", "version": "1"},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "tools/list":
        list_count += 1
        schema = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
        result = {"tools": [{"name": state, "description": state, "inputSchema": schema}]}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
        if list_count == 1:
            state = "new"
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}), flush=True)
    elif method == "tools/call":
        name = message["params"]["name"]
        result = {"content": [{"type": "text", "text": name}]}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""
