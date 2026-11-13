"""A tiny stand-in for the OpenAI Responses API, so the real Codex runtime can be tested offline.

Each POST /v1/responses answers with the next scripted output item (a tool call) until the
script is used up, then with a final assistant message. Request bodies are kept for asserts.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

USAGE = {
    "input_tokens": 100,
    "input_tokens_details": {"cached_tokens": 10},
    "output_tokens": 5,
    "output_tokens_details": {"reasoning_tokens": 1},
    "total_tokens": 105,
}


class FakeResponses:
    def __init__(self, steps: list[dict[str, Any]], final: str = "done") -> None:
        self.steps = steps
        self.final = final
        self.requests: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def _reply(self, body: dict[str, Any]) -> dict[str, Any]:
        outputs = [i for i in body.get("input", []) if i.get("type") in ("function_call_output", "custom_tool_call_output")]
        if len(outputs) < len(self.steps):
            return self.steps[len(outputs)]
        return {"type": "message", "role": "assistant", "id": "msg_final", "content": [{"type": "output_text", "text": self.final}]}

    def __enter__(self) -> "FakeResponses":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return None

            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"object":"list","data":[]}')

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                fake.requests.append(body)
                item = fake._reply(body)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for event in (
                    {"type": "response.created", "response": {"id": "resp"}},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {"id": "resp", "usage": USAGE}},
                ):
                    self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def tool_names(self) -> set[str]:
        """Every tool name Codex offered the model, including namespaced ones."""

        def walk(tools: list[dict[str, Any]] | None) -> set[str]:
            found: set[str] = set()
            for tool in tools or []:
                if tool.get("type") == "namespace":
                    found |= walk(tool.get("tools"))
                found.add(str(tool.get("name") or tool.get("type")))
            return found

        names: set[str] = set()
        for body in self.requests:
            names |= walk(body.get("tools"))
            for item in body.get("input", []):
                if item.get("type") == "additional_tools":
                    names |= walk(item.get("tools"))
        return names
