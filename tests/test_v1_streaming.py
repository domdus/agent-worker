"""The /v1 chat stream as OpenAI clients (Open WebUI, LibreChat, Hermes) read it."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_worker.app import create_app
from agent_worker.claude_engine import _Turn
from agent_worker.openai_compat import session_of


@pytest.fixture
def client(settings, engine):
    with TestClient(create_app(settings, engine), base_url="http://127.0.0.1:8787") as c:
        yield c


def _stream(client, headers=None, **body):
    payload = {"model": "composer-2.5", "stream": True, "messages": [{"role": "user", "content": "hello"}], **body}
    with client.stream("POST", "/v1/chat/completions", json=payload, headers=headers or {}) as resp:
        lines = [line for line in resp.iter_lines() if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    return [json.loads(line[6:]) for line in lines[:-1]]


def test_one_id_for_every_chunk_and_usage_null_until_the_end(client):
    chunks = _stream(client, stream_options={"include_usage": True})
    assert len({c["id"] for c in chunks}) == 1 and len({c["created"] for c in chunks}) == 1
    assert all(c["usage"] is None for c in chunks[:-1]) and chunks[-1]["usage"]["total_tokens"] == 15


def test_a_failed_job_ends_with_an_error_chunk(client, engine):
    engine.fail = "Cursor said no"
    chunks = _stream(client)
    assert chunks[-1]["error"]["message"] == "Cursor said no" and "choices" not in chunks[-1]


def test_steps_can_be_dropped_per_request(client):
    chunks = _stream(client, headers={"X-Agent-Steps": "none"})
    assert not any("reasoning_content" in c["choices"][0]["delta"] for c in chunks if c.get("choices"))
    other = _stream(client, headers={"X-Agent-Steps": "reasoning"})
    assert any("reasoning" in c["choices"][0]["delta"] for c in other if c.get("choices"))


def test_auth_error_is_an_openai_error(settings, engine):
    secured = replace(settings, host="0.0.0.0", token="s3cret")
    with TestClient(create_app(secured, engine), base_url="http://worker:8787") as c:
        body = c.post("/v1/chat/completions", json={"messages": []}).json()
    assert body["error"]["code"] == "invalid_api_key" and body["detail"]


def test_session_ids_from_clients():
    def req(**headers):
        return SimpleNamespace(headers=headers)

    assert session_of(req(**{"x-conversation-id": "abc"}), {}) == "abc"
    assert session_of(req(**{"x-conversation-id": "new"}), {"conversation_id": "c-7"}) == "c-7"
    assert session_of(req(**{"x-openwebui-chat-id": "{{CHAT_ID}}"}), {}) == ""
    assert session_of(req(), {"metadata": {"chat_id": "m-1"}}) == "m-1"


def test_claude_partial_text_is_not_sent_twice():
    turn = _Turn()
    assert turn.partial({"type": "message_start", "message": {"id": "msg_1"}}) == []
    assert turn.partial({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}) == [
        {"type": "text", "delta": "Hi"}
    ]
    assert turn.already_streamed(SimpleNamespace(message_id="msg_1"))
    assert not turn.already_streamed(SimpleNamespace(message_id="msg_2"))
    # A second text block after a tool call starts a new paragraph.
    assert turn.partial({"type": "content_block_start", "content_block": {"type": "text"}}) == [
        {"type": "text", "delta": "\n\n"}
    ]
