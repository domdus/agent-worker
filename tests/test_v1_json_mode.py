"""/v1/chat/completions with response_format json_schema / json_object (JSON mode)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from agent_worker.app import create_app
from agent_worker.openai_compat import extract_json, json_format, json_instruction
from tests.conftest import FakeEngine

SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
    "required": ["name", "count"],
    "additionalProperties": False,
}
JSON_SCHEMA = {"type": "json_schema", "json_schema": {"name": "thing", "schema": SCHEMA, "strict": True}}


class ReplyEngine(FakeEngine):
    """FakeEngine whose final answer (and streamed text) is `reply`, when set."""

    def __init__(self) -> None:
        super().__init__()
        self.reply: str | None = None

    async def run(self, spec, emit):
        if self.reply is None:
            return await super().run(spec, emit)

        async def quiet(event):
            if event.get("type") != "text":
                await emit(event)

        outcome = await super().run(spec, quiet)
        await emit({"type": "text", "delta": self.reply})
        return replace(outcome, text=self.reply)


@pytest.fixture
def engine() -> ReplyEngine:
    return ReplyEngine()


@pytest.fixture
def client(settings, engine):
    with TestClient(create_app(settings, engine), base_url="http://127.0.0.1:8787") as c:
        yield c


def _post(client, headers=None, **body):
    payload = {"model": "composer-2.5", "messages": [{"role": "user", "content": "hello"}], **body}
    return client.post("/v1/chat/completions", json=payload, headers=headers or {})


def _stream(client, headers=None, **body):
    payload = {"model": "composer-2.5", "stream": True, "messages": [{"role": "user", "content": "hello"}], **body}
    with client.stream("POST", "/v1/chat/completions", json=payload, headers=headers or {}) as resp:
        lines = [line for line in resp.iter_lines() if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    return [json.loads(line[6:]) for line in lines[:-1]]


# ---- tools and prompt --------------------------------------------------------------------------


def test_json_schema_forces_tools_none_even_if_header_asks_for_more(client, engine):
    engine.reply = '{"name": "a", "count": 1}'
    resp = _post(client, headers={"X-Agent-Tools": "full"}, response_format=JSON_SCHEMA)
    assert resp.status_code == 200
    assert engine.specs[-1].profile == "none"


def test_json_object_forces_tools_none_with_a_wide_container_default(tmp_path, engine):
    from tests.conftest import make_settings

    settings = make_settings(tmp_path, tool_profile="full")
    engine.reply = '{"ok": true}'
    with TestClient(create_app(settings, engine), base_url="http://127.0.0.1:8787") as c:
        resp = _post(c, headers={"X-Agent-Tools": "local"}, response_format={"type": "json_object"})
    assert resp.status_code == 200 and engine.specs[-1].profile == "none"


def test_schema_is_injected_into_the_instructions(client, engine):
    engine.reply = '{"name": "a", "count": 1}'
    _post(client, messages=[{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hello"}],
          response_format=JSON_SCHEMA)
    spec = engine.specs[-1]
    assert spec.instructions.startswith("Be brief.")
    assert "ONLY a single JSON value" in spec.instructions and "no code fences" in spec.instructions
    assert json.dumps(SCHEMA, indent=2) in spec.instructions and "thing" in spec.instructions
    # No session: the task stays as the user wrote it.
    assert spec.task == "hello"


def test_json_object_instruction_and_warm_session_gets_it_in_the_task(client, engine):
    engine.reply = '{"ok": true}'
    _post(client, headers={"X-Session-Id": "chat-1"}, response_format={"type": "json_object"})
    spec = engine.specs[-1]
    assert "ONLY a single JSON object" in spec.instructions
    assert spec.task.startswith("hello") and "ONLY a single JSON object" in spec.task


# ---- answer post-processing --------------------------------------------------------------------


def test_code_fences_are_stripped(client, engine):
    engine.reply = '\n```json\n{"name": "a", "count": 2}\n```\n'
    body = _post(client, response_format=JSON_SCHEMA).json()
    assert body["choices"][0]["message"]["content"] == '{"name": "a", "count": 2}'
    engine.reply = '```\n[1, 2]\n```'
    body = _post(client, response_format=JSON_SCHEMA).json()
    assert json.loads(body["choices"][0]["message"]["content"]) == [1, 2]


def test_json_is_extracted_from_surrounding_text(client, engine):
    engine.reply = 'Sure! Here it is: {"name": "x {y}", "count": 3} Hope that helps [not json'
    body = _post(client, response_format=JSON_SCHEMA).json()
    assert json.loads(body["choices"][0]["message"]["content"]) == {"name": "x {y}", "count": 3}


def test_no_json_is_an_openai_error(client, engine):
    engine.reply = "I could not find anything, sorry."
    resp = _post(client, response_format=JSON_SCHEMA)
    assert resp.status_code == 502
    err = resp.json()["error"]
    assert err["code"] == "invalid_json" and err["type"] == "agent_error" and "choices" not in resp.json()


def test_json_object_rejects_an_array(client, engine):
    engine.reply = "[1, 2, 3]"
    resp = _post(client, response_format={"type": "json_object"})
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "invalid_json"


def test_failed_job_is_an_error_not_prose(client, engine):
    engine.fail = "Cursor said no"
    resp = _post(client, response_format=JSON_SCHEMA)
    assert resp.status_code == 502 and resp.json()["error"]["message"] == "Cursor said no"


def test_extract_json_helper():
    assert extract_json('  {"a": 1}  ') == '{"a": 1}'
    assert extract_json("```json\n{\"a\": 1}\n```") == '{"a": 1}'
    assert extract_json('text ```json\n{"a": [1, {"b": "}"}]}\n``` more') == '{"a": [1, {"b": "}"}]}'
    assert extract_json("{broken {\"a\": 1}") == '{"a": 1}'
    assert extract_json("42") == "42"
    assert extract_json("42", object_only=True) is None
    assert extract_json("nothing here") is None
    assert extract_json("") is None


def test_json_format_detection():
    assert json_format({"response_format": {"type": "json_object"}}) == {"type": "json_object"}
    assert json_format({"response_format": JSON_SCHEMA}) is JSON_SCHEMA
    assert json_format({"response_format": {"type": "text"}}) is None
    assert json_format({"response_format": "json"}) is None
    assert json_format({}) is None
    assert "JSON value" in json_instruction({"type": "json_schema", "json_schema": {}})


# ---- streaming ---------------------------------------------------------------------------------


def test_stream_buffers_and_sends_the_json_once(client, engine):
    engine.reply = 'Here:\n```json\n{"name": "s", "count": 9}\n```'
    chunks = _stream(client, stream_options={"include_usage": True}, response_format=JSON_SCHEMA)
    contents = [c["choices"][0]["delta"].get("content") for c in chunks if c.get("choices")]
    contents = [c for c in contents if c]
    assert contents == ['{"name": "s", "count": 9}']
    assert chunks[-1]["usage"]["total_tokens"] == 15
    assert engine.specs[-1].profile == "none"


def test_stream_without_json_ends_with_an_error_chunk(client, engine):
    engine.reply = "no json at all"
    chunks = _stream(client, response_format=JSON_SCHEMA)
    assert chunks[-1]["error"]["code"] == "invalid_json" and "choices" not in chunks[-1]
    assert not any(c["choices"][0]["delta"].get("content") for c in chunks if c.get("choices"))


# ---- without response_format: as before --------------------------------------------------------


@pytest.mark.parametrize("extra", [{}, {"response_format": {"type": "text"}}])
def test_without_json_response_format_nothing_changes(client, engine, extra):
    resp = _post(client, headers={"X-Agent-Tools": "local"},
                 messages=[{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hello"}], **extra)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "Done: hello"
    spec = engine.specs[-1]
    assert spec.profile == "local" and spec.instructions == "Be brief." and spec.task == "hello"
    # Prose answers stay prose.
    engine.reply = "Plain words, no JSON."
    assert _post(client, **extra).json()["choices"][0]["message"]["content"] == "Plain words, no JSON."
    assert engine.specs[-1].profile == "full"
    chunks = _stream(client, **extra)
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c.get("choices"))
    assert text == "Plain words, no JSON."
