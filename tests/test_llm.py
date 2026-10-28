"""LLM layer: schemas, validation and repair, every backend against a fake transport."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from witness.llm import (
    LLM,
    BackendError,
    CompletionRequest,
    Message,
    RecordingBackend,
    ReplayBackend,
    create_backend,
)
from witness.llm.anthropic_backend import DEFAULT_MODEL, AnthropicBackend
from witness.llm.claude_cli import ClaudeCliBackend
from witness.llm.openrouter_backend import OpenRouterBackend
from witness.llm.schema import strict_schema
from witness.prove import ReproReply

REQUEST = CompletionRequest(
    tag="prove/C1/attempt-1",
    system="be precise",
    messages=(Message("user", "hello"),),
    schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    schema_name="ReproReply",
)
VALID_REPLY = {
    "reasoning": "r",
    "concede": False,
    "test_code": "def test_x(): assert False",
    "expected_failure": {"kind": "assertion", "exception_type": None},
}


def test_strict_schema_is_self_contained_and_closed() -> None:
    schema = strict_schema(ReproReply)
    text = json.dumps(schema)
    assert "$ref" not in text
    assert "$defs" not in text
    assert '"title"' not in text
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["reasoning", "concede", "test_code", "expected_failure"]
    nested = schema["properties"]["expected_failure"]
    assert nested["additionalProperties"] is False
    assert nested["required"] == ["kind", "exception_type"]


class TestStructured:
    def test_valid_reply(self) -> None:
        llm = LLM(ReplayBackend({"responses": {"t": VALID_REPLY}}))
        reply, raw = llm.structured("t", "sys", [Message("user", "u")], ReproReply)
        assert reply.test_code.startswith("def test_x")
        assert json.loads(raw) == VALID_REPLY
        assert llm.usage.calls == 1

    def test_invalid_reply_gets_one_repair_round(self) -> None:
        backend = ReplayBackend(
            {"responses": {"t": {"reasoning": "missing fields"}, "t/repair": VALID_REPLY}}
        )
        reply, _ = LLM(backend).structured("t", "sys", [Message("user", "u")], ReproReply)
        assert reply.expected_failure.kind == "assertion"
        assert backend.served == ["t", "t/repair"]

    def test_two_invalid_replies_raise(self) -> None:
        backend = ReplayBackend({"responses": {"t": "not json", "t/repair": "still not"}})
        with pytest.raises(BackendError, match="failed validation twice"):
            LLM(backend).structured("t", "sys", [Message("user", "u")], ReproReply)

    def test_json_wrapped_in_prose_is_accepted(self) -> None:
        wrapped = f"Here you go:\n```json\n{json.dumps(VALID_REPLY)}\n```"
        llm = LLM(ReplayBackend({"responses": {"t": wrapped}}))
        reply, _ = llm.structured("t", "sys", [Message("user", "u")], ReproReply)
        assert reply.concede is False


class TestReplay:
    def test_unknown_tag_names_what_is_available(self) -> None:
        backend = ReplayBackend({"responses": {"hypothesize": {}}})
        with pytest.raises(BackendError, match=r"no recorded response for 'prove/C1/attempt-1'"):
            backend.complete(REQUEST)

    def test_recording_round_trips_into_replay(self, tmp_path: Path) -> None:
        path = tmp_path / "recorded.json"
        live = ReplayBackend({"model": "m-1", "responses": {REQUEST.tag: VALID_REPLY}})
        RecordingBackend(live, path).complete(REQUEST)
        assert ReplayBackend(path).complete(REQUEST).text == json.dumps(VALID_REPLY)

    def test_recording_is_cache_through(self, tmp_path: Path) -> None:
        path = tmp_path / "recorded.json"
        first = RecordingBackend(ReplayBackend({"responses": {REQUEST.tag: VALID_REPLY}}), path)
        first.complete(REQUEST)
        resumed = RecordingBackend(ReplayBackend({"responses": {}}), path)  # live side is empty
        assert resumed.complete(REQUEST).text == json.dumps(VALID_REPLY)
        assert (first.live_calls, resumed.live_calls) == (1, 0)

    def test_create_backend_requires_fixtures(self) -> None:
        with pytest.raises(ValueError, match="--fixtures"):
            create_backend("replay")
        with pytest.raises(ValueError, match="unknown backend"):
            create_backend("gpt")


class FakeAnthropicMessages:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


def anthropic_response(stop_reason: str = "end_turn", text: str = '{"a": 1}') -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason,
        model="claude-opus-5-5",
        content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=90,
            cache_creation_input_tokens=0,
        ),
    )


class TestAnthropic:
    def make(self, response: Any) -> tuple[AnthropicBackend, FakeAnthropicMessages]:
        messages = FakeAnthropicMessages(response)
        client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
        return AnthropicBackend(client=client), messages

    def test_request_shape(self) -> None:
        backend, messages = self.make(anthropic_response())
        completion = backend.complete(REQUEST)

        assert DEFAULT_MODEL == "claude-opus-5-5"
        sent = messages.kwargs
        assert sent["model"] == "claude-opus-5-5"
        assert sent["system"] == "be precise"
        assert sent["messages"] == [{"role": "user", "content": "hello"}]
        assert sent["output_config"]["format"] == {"type": "json_schema", "schema": REQUEST.schema}
        assert sent["fallbacks"] == "default"
        assert completion.text == '{"a": 1}'
        assert (completion.input_tokens, completion.output_tokens) == (100, 5)

    @pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
    def test_unusable_stops_raise(self, stop_reason: str) -> None:
        backend, _ = self.make(anthropic_response(stop_reason=stop_reason))
        with pytest.raises(BackendError):
            backend.complete(REQUEST)


def test_openrouter_request_shape() -> None:
    calls: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            model="anthropic/claude-opus-5.5",
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="{}"))],
            usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3),
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    completion = OpenRouterBackend(client=client).complete(REQUEST)

    [sent] = calls
    assert sent["messages"][0] == {"role": "system", "content": "be precise"}
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert completion.input_tokens == 7


class TestClaudeCli:
    def run_with(self, stdout: str, returncode: int = 0) -> tuple[Any, list[dict[str, Any]]]:
        seen: list[dict[str, Any]] = []

        def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            option = "--system-prompt-file"
            system = (
                Path(command[command.index(option) + 1]).read_text() if option in command else None
            )
            seen.append({"command": command, "system": system, **kwargs})
            return subprocess.CompletedProcess(command, returncode, stdout, "boom")

        # Any real executable satisfies the PATH lookup; the runner never launches it.
        backend = ClaudeCliBackend(executable=sys.executable, runner=runner)
        return backend, seen

    def test_prompt_goes_over_stdin_and_reply_is_parsed(self) -> None:
        payload = {
            "result": '{"ok": true}',
            "usage": {"input_tokens": 3, "cache_read_input_tokens": 100, "output_tokens": 9},
            "modelUsage": {"claude-opus-5-5": {}},
            "total_cost_usd": 0.02,
        }
        backend, seen = self.run_with(json.dumps(payload))
        completion = backend.complete(REQUEST)

        [call] = seen
        assert call["command"][1:4] == ["-p", "--output-format", "json"]
        assert "hello" in call["input"]
        assert call["system"] == "be precise"  # replaces Claude Code's own system prompt
        assert "be precise" not in call["input"]
        assert call["command"][-3:] == ["--setting-sources", "", "--strict-mcp-config"]
        assert all("hello" not in part for part in call["command"])
        assert (completion.text, completion.model) == ('{"ok": true}', "claude-opus-5-5")
        assert (completion.input_tokens, completion.cost_usd) == (103, 0.02)

    def test_cli_failure(self) -> None:
        backend, _ = self.run_with("", returncode=1)
        with pytest.raises(BackendError, match="exited with 1"):
            backend.complete(REQUEST)

    def test_rejects_unsafe_model_names(self) -> None:
        with pytest.raises(ValueError, match="invalid model"):
            ClaudeCliBackend("opus & calc.exe")
