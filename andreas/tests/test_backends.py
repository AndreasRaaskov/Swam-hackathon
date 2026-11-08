"""Format conversions for both providers (no network)."""

from jourapist.llm.anthropic_backend import to_anthropic_messages
from jourapist.llm.openai_compat import to_chat_messages, to_responses_input
from jourapist.snapshot import Message, ToolCallRec

HIST = [
    Message(role="user", content="week 9 report"),
    Message(role="assistant", content="Reporting.", reasoning="secret",
            tool_calls=[ToolCallRec(id="call.1", name="report_to_owner", arguments={"message": "hi"})]),
    Message(role="tool", tool_call_id="call.1", name="report_to_owner", content="Delivered."),
    Message(role="user", content="[New message from X]\n\nHow did you set prices?"),
]


def test_anthropic_conversion_merges_and_sanitises():
    msgs, notes = to_anthropic_messages(HIST)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"][1] == {"type": "tool_use", "id": "call_1", "name": "report_to_owner", "input": {"message": "hi"}}
    assert msgs[2]["content"][0]["type"] == "tool_result" and msgs[2]["content"][0]["tool_use_id"] == "call_1"
    assert msgs[2]["content"][1]["type"] == "text"          # tool_result first, then the new question
    assert "secret" not in str(msgs) and notes == []


def test_anthropic_leading_assistant_is_noted():
    msgs, notes = to_anthropic_messages([Message(role="assistant", content="hello")])
    assert msgs[0]["role"] == "user" and notes == ["inserted_leading_user_turn"]


def test_responses_input_items():
    items = to_responses_input(HIST)
    assert items[1] == {"role": "assistant", "content": "Reporting."}
    assert items[2]["type"] == "function_call" and items[2]["call_id"] == "call.1"
    assert items[3] == {"type": "function_call_output", "call_id": "call.1", "output": "Delivered."}
    assert "secret" not in str(items)


def test_chat_messages():
    msgs = to_chat_messages("SYS", HIST)
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "report_to_owner"
    assert msgs[3] == {"role": "tool", "tool_call_id": "call.1", "content": "Delivered."}
