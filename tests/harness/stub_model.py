#!/usr/bin/env python3
"""
stub_model.py — a fake LLM server that records what an agent harness sends.

Speaks just enough of two protocols for a harness to complete one turn:
  Anthropic Messages API   POST /v1/messages              (Claude Code)
  OpenAI Chat Completions  POST /v1/chat/completions      (pi, goose)

Every request is appended as one JSON line ({"path", "body"}) to the record
file, and answered with a fixed text reply, streamed (SSE) when the request
asks for it. Nothing here is a model: the recorded request bodies are the
point, because they show exactly what context the harness built, including
anything a hook added.

Standalone use:
  python3 tests/harness/stub_model.py --port 8765 --record /tmp/requests.jsonl
"""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLY = 'stub reply'
MODEL = 'stub-model'


def anthropic_message(model: str) -> dict:
    return {
        'id': 'msg_stub', 'type': 'message', 'role': 'assistant', 'model': model,
        'content': [{'type': 'text', 'text': REPLY}],
        'stop_reason': 'end_turn', 'stop_sequence': None,
        'usage': {'input_tokens': 1, 'output_tokens': 2},
    }


def anthropic_events(model: str) -> list[tuple[str, dict]]:
    start = {**anthropic_message(model), 'content': [], 'stop_reason': None}
    return [
        ('message_start', {'type': 'message_start', 'message': start}),
        ('content_block_start', {'type': 'content_block_start', 'index': 0,
                                 'content_block': {'type': 'text', 'text': ''}}),
        ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                 'delta': {'type': 'text_delta', 'text': REPLY}}),
        ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
        ('message_delta', {'type': 'message_delta',
                           'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                           'usage': {'output_tokens': 2}}),
        ('message_stop', {'type': 'message_stop'}),
    ]


def openai_completion(model: str) -> dict:
    return {
        'id': 'chatcmpl-stub', 'object': 'chat.completion', 'created': 0, 'model': model,
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': REPLY},
                     'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3},
    }


def openai_chunks(model: str) -> list[dict]:
    base = {'id': 'chatcmpl-stub', 'object': 'chat.completion.chunk', 'created': 0, 'model': model}
    return [
        {**base, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': REPLY},
                              'finish_reason': None}]},
        {**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
         'usage': {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3}},
    ]


def make_handler(record_path: str, lock: threading.Lock):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep test output quiet
            pass

        def _record(self, body) -> None:
            with lock, open(record_path, 'a') as f:
                f.write(json.dumps({'path': self.path, 'body': body}) + '\n')

        def _send_json(self, payload: dict, status: int = 200) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_sse(self, frames: list[str]) -> None:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.end_headers()
            for frame in frames:
                self.wfile.write(frame.encode())
                self.wfile.flush()
            self.close_connection = True

        def do_GET(self):
            self._record(None)
            if self.path.rstrip('/').endswith('/models'):
                self._send_json({'object': 'list', 'data': [{'id': MODEL, 'object': 'model'}]})
            else:
                self._send_json({'error': 'not found'}, 404)

        def do_POST(self):
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(length) if length else b''
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {'_raw': raw.decode(errors='replace')}
            self._record(body)

            path = self.path.split('?', 1)[0].rstrip('/')
            model = body.get('model', MODEL) if isinstance(body, dict) else MODEL
            stream = isinstance(body, dict) and body.get('stream')

            if path.endswith('/messages/count_tokens'):
                self._send_json({'input_tokens': 1})
            elif path.endswith('/messages'):
                if stream:
                    self._send_sse([f'event: {name}\ndata: {json.dumps(data)}\n\n'
                                    for name, data in anthropic_events(model)])
                else:
                    self._send_json(anthropic_message(model))
            elif path.endswith('/chat/completions'):
                if stream:
                    frames = [f'data: {json.dumps(c)}\n\n' for c in openai_chunks(model)]
                    self._send_sse(frames + ['data: [DONE]\n\n'])
                else:
                    self._send_json(openai_completion(model))
            else:
                self._send_json({'error': f'stub does not implement {self.path}'}, 404)

    return Handler


class StubModel:
    """Run the stub in a background thread: `with StubModel(path) as stub: stub.url`."""

    def __init__(self, record_path: str, port: int = 0):
        self.record_path = record_path
        self.server = ThreadingHTTPServer(('127.0.0.1', port), make_handler(record_path, threading.Lock()))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def __enter__(self) -> 'StubModel':
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    def requests(self) -> list[dict]:
        try:
            with open(self.record_path) as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--record', required=True, help='JSON-lines file to append requests to')
    args = parser.parse_args()
    stub = StubModel(args.record, args.port)
    print(f'stub model listening on {stub.url}, recording to {args.record}')
    try:
        stub.server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
