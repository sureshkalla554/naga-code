"""Tiny stdio MCP server used by tests. Modes (argv[1]): normal | hang | badschema | crash."""

import json
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"
TOOLS = [
    {"name": "echo", "description": "Echo text back", "inputSchema": {
        "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "Add two numbers", "inputSchema": {
        "type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}, "required": ["a", "b"]}},
]
if MODE == "badschema":
    TOOLS.append({"name": "broken", "description": "bad", "inputSchema": {"type": "not-a-type"}})


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue  # notification
    if MODE == "hang":
        continue
    if MODE == "crash":
        sys.exit(3)
    method, mid = msg["method"], msg["id"]
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": "echo-server", "version": "1.0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"]["arguments"]
        if name == "echo":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": args["text"]}]}})
        elif name == "add":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "no such tool"}],
                                                           "isError": True}})
    elif method == "resources/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"resources": [{"uri": "mem://greeting", "name": "greeting"}]}})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})
