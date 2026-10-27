from naga_code.providers.tool_protocol import describe_tools, parse_tool_calls


def test_parses_well_formed_blocks():
    text = 'I will read it.\n<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>'
    rest, calls = parse_tool_calls(text)
    assert rest == "I will read it."
    assert calls[0].name == "read_file" and calls[0].arguments == {"path": "a.py"} and calls[0].parse_error is None
    assert calls[0].source == "text"


def test_plain_text_is_never_a_tool_call():
    rest, calls = parse_tool_calls('I ran read_file("a.py") and it worked. {"name": "x"}')
    assert calls == [] and "read_file" in rest


def test_malformed_blocks_are_flagged():
    _, calls = parse_tool_calls("<tool_call>{oops}</tool_call>")
    assert calls[0].parse_error and "not valid JSON" in calls[0].parse_error
    _, calls = parse_tool_calls('<tool_call>{"arguments": {}}</tool_call>')
    assert "name" in calls[0].parse_error
    _, calls = parse_tool_calls('<tool_call>{"name": "x", "arguments": [1]}</tool_call>')
    assert "object" in calls[0].parse_error
    rest, calls = parse_tool_calls('text <tool_call>{"name": "x"')
    assert calls[0].parse_error == "unterminated <tool_call> block" and rest == "text"


def test_fenced_json_and_string_arguments():
    _, calls = parse_tool_calls('<tool_call>```json\n{"name": "x", "arguments": "{\\"a\\": 1}"}\n```</tool_call>')
    assert calls[0].arguments == {"a": 1}


def test_describe_tools_lists_schemas():
    out = describe_tools([{"type": "function", "function": {"name": "t", "description": "d", "parameters": {"type": "object"}}}])
    assert "<tool_call>" in out and "- t: d" in out


def test_parses_qwen_xml_calls_written_as_text():
    tools = [{"function": {"name": "write_file", "parameters": {"properties": {
        "path": {"type": "string"}, "content": {"type": "string"}}}}},
        {"function": {"name": "read_file", "parameters": {"properties": {
            "path": {"type": "string"}, "start_line": {"type": "integer"}}}}}]
    text = ("I'll create it.\n<function=write_file> <parameter=path> hello.txt </parameter>"
            "<parameter=content>\ndef f():\n    return 1\n</parameter> </function> </tool_call>")
    rest, calls = parse_tool_calls(text, tools)
    assert rest == "I'll create it."
    assert calls[0].name == "write_file" and calls[0].parse_error is None
    assert calls[0].arguments == {"path": "hello.txt", "content": "def f():\n    return 1"}
    # unterminated parameters/functions and typed (non-string) values
    _, calls = parse_tool_calls("<tool_call>\n<function=read_file>\n<parameter=path>\na.py\n<parameter=start_line>\n12\n", tools)
    assert calls[0].arguments == {"path": "a.py", "start_line": 12}


def test_bare_trailing_tool_call_tag_is_dropped():
    rest, calls = parse_tool_calls('The first line is "# demo".\n<tool_call>')
    assert rest == 'The first line is "# demo".' and calls == []
    _, calls = parse_tool_calls('text <tool_call>{"name": "x"')
    assert calls and calls[0].parse_error
