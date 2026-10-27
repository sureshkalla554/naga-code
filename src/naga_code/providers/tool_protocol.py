"""Text-based tool-call protocol for models without reliable native tool calling.

Format (the same tags Qwen/Hermes-style models are trained on):

    <tool_call>
    {"name": "read_file", "arguments": {"path": "src/app.py"}}
    </tool_call>

Only well-formed blocks are turned into ToolCalls. Anything that merely *looks*
like a tool call but fails to parse becomes a ToolCall with ``parse_error`` set,
which the runtime reports back to the model; it is never executed.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .base import ToolCall

_BLOCK = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
# Qwen3-Coder's own XML call style, which it sometimes emits as plain text instead of a native call:
#   <function=write_file> <parameter=path> a.txt </parameter> <parameter=content> hi </parameter> </function>
_XML_FN = re.compile(r"(?:<tool_call>\s*)?<function=([\w.-]+)>(.*?)(?:</function>\s*(?:</tool_call>)?|</tool_call>|\Z)",
                     re.DOTALL)
_XML_PARAM = re.compile(r"<parameter=([\w.-]+)>(.*?)(?=</parameter>|<parameter=|\Z)(?:</parameter>)?", re.DOTALL)


def describe_tools(tools: list[dict[str, Any]]) -> str:
    lines = [
        "# Tool use",
        "You can call tools. To call a tool, output exactly one block per call:",
        "<tool_call>",
        '{"name": "<tool name>", "arguments": {<JSON arguments>}}',
        "</tool_call>",
        "Output nothing after your tool call blocks; wait for <tool_result> messages.",
        "Never invent tool results. When the task is finished, reply without any tool_call block.",
        "",
        "Available tools:",
    ]
    for t in tools:
        fn = t["function"]
        lines.append(f"- {fn['name']}: {fn['description']}")
        lines.append(f"  parameters: {json.dumps(fn['parameters'], separators=(',', ':'))}")
    return "\n".join(lines)


def _xml_value(raw: str, schema: dict[str, Any] | None) -> Any:
    value = raw.strip("\n")
    if "\n" not in value:
        value = value.strip()   # single-line values: surrounding spaces are layout, not content
    if schema and schema.get("type", "string") != "string":
        try:
            return json.loads(value.strip())
        except json.JSONDecodeError:
            return value.strip()
    return value


def parse_xml_tool_calls(text: str, tools: list[dict[str, Any]] | None = None) -> tuple[str, list[ToolCall]]:
    """Parse Qwen-style ``<function=name><parameter=k>v</parameter></function>`` calls written as text."""
    props = {t["function"]["name"]: t["function"].get("parameters", {}).get("properties", {}) for t in tools or []}
    calls = []
    for name, body in _XML_FN.findall(text):
        args = {k: _xml_value(v, props.get(name, {}).get(k)) for k, v in _XML_PARAM.findall(body)}
        calls.append(ToolCall(name=name, arguments=args, source="text"))
    return _XML_FN.sub("", text).strip(), calls


def parse_tool_calls(text: str, tools: list[dict[str, Any]] | None = None) -> tuple[str, list[ToolCall]]:
    """Return (text with tool blocks removed, parsed tool calls)."""
    if "<function=" in text:
        return parse_xml_tool_calls(text, tools)
    calls: list[ToolCall] = []
    for raw in _BLOCK.findall(text):
        body = _FENCE.sub("", raw.strip())
        try:
            obj = json.loads(body)
        except json.JSONDecodeError as e:
            calls.append(ToolCall(name="", arguments={}, parse_error=f"tool_call block is not valid JSON: {e}",
                                  source="text"))
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
            calls.append(ToolCall(name="", arguments={}, parse_error="tool_call block needs a string 'name'",
                                  source="text"))
            continue
        args = obj.get("arguments", obj.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                pass
        if not isinstance(args, dict):
            calls.append(ToolCall(name=obj["name"], arguments={}, source="text",
                                  parse_error="'arguments' must be a JSON object"))
            continue
        calls.append(ToolCall(name=obj["name"], arguments=args, source="text"))
    remaining = _BLOCK.sub("", text).strip()
    # An unterminated block with content is a malformed call, not prose. A bare trailing "<tool_call>" with
    # nothing after it (some models emit one before stopping) is just dropped.
    if "<tool_call>" in remaining:
        head, _, tail = remaining.partition("<tool_call>")
        if tail.strip():
            calls.append(ToolCall(name="", arguments={}, source="text", parse_error="unterminated <tool_call> block"))
        remaining = head.strip()
    return remaining, calls


def format_tool_result(name: str, content: str) -> str:
    return f'<tool_result name="{name}">\n{content}\n</tool_result>'
