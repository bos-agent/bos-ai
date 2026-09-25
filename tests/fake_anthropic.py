"""A fake Anthropic Messages API for driving the real `claude` CLI in tests.

The CLI talks to whatever `ANTHROPIC_BASE_URL` names. This answers each model call
with the next scripted reply — text and/or tool_use blocks, streamed as SSE when the
request asks for a stream — and records every request body, so a test can assert on
what the CLI sent (the tools it offered, the tool_result it got back). The model is
fake; the CLI, the SDK and every permission decision are real.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Block = dict[str, Any]


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class FakeAnthropic:
    """A model that follows a script, not an API emulator.

    Each reply is a list of content blocks: ``{"type": "text", "text": ...}`` or
    ``{"type": "tool_use", "id": ..., "name": ..., "input": {...}}``. Once the script
    runs out, every further model call is answered with ``done``, so a test that
    cares how many calls the CLI made asserts on ``len(requests)``.
    """

    def __init__(self) -> None:
        self._replies: list[list[Block]] = []
        self.requests: list[dict[str, Any]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if not self.path.startswith("/v1/messages") or self.path.startswith("/v1/messages/count_tokens"):
                    self._json({"input_tokens": 10})
                    return
                fake.requests.append(body)
                blocks = fake._replies.pop(0) if fake._replies else [{"type": "text", "text": "done"}]
                stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
                message = {
                    "id": f"msg_{len(fake.requests)}",
                    "type": "message",
                    "role": "assistant",
                    "model": body.get("model", "fake"),
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 5, "output_tokens": 1},
                }
                if not body.get("stream"):
                    self._json({**message, "content": blocks, "stop_reason": stop})
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                write = self.wfile.write
                write(_sse("message_start", {"type": "message_start", "message": message}))
                for i, block in enumerate(blocks):
                    if block["type"] == "text":
                        start = {"type": "text", "text": ""}
                        delta = {"type": "text_delta", "text": block["text"]}
                    else:
                        start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
                        delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                    started = {"type": "content_block_start", "index": i, "content_block": start}
                    write(_sse("content_block_start", started))
                    write(_sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta}))
                    write(_sse("content_block_stop", {"type": "content_block_stop", "index": i}))
                delta = {"stop_reason": stop, "stop_sequence": None}
                write(_sse("message_delta", {"type": "message_delta", "delta": delta, "usage": {"output_tokens": 3}}))
                write(_sse("message_stop", {"type": "message_stop"}))

            def _json(self, payload: dict[str, Any]) -> None:
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # shutdown() waits out one poll interval; the 0.5s default was most of a test's teardown.
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def script(self, replies: list[list[Block]]) -> None:
        self._replies = list(replies)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
