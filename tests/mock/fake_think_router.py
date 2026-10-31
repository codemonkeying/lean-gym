"""Fake llama-server that STREAMS a reasoning_content block then a tool call, for
EVAL_KEEP_REASONING tests. /v1/models answers; chat streams SSE: a thinking delta,
then an apply_diff tool call, then usage + [DONE]. Model id: wintest-model."""
import http.server, json, os, sys

# FAKE_LOG=path: append every chat request body (one JSON line) for assertions.
# FAKE_PROPS=1: /props returns a router-preset card like .28 (temp 1.0, top_k 20, min_p 0).
LOG = os.environ.get("FAKE_LOG")
PROPS = os.environ.get("FAKE_PROPS") == "1"

MODEL = "wintest-model"


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/props"):
            if not PROPS:
                self.send_response(404); self.end_headers(); return
            body = json.dumps({"default_generation_settings": {"params": {
                "temperature": 1.0, "top_k": 20, "top_p": 0.95, "min_p": 0.0},
                "n_ctx": 81920}, "model_path": "/m/qwen3.8-27b.gguf"}).encode()
        else:
            body = json.dumps({"data": [{"id": MODEL}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, obj):
        self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
        self.wfile.flush()

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        if LOG:
            with open(LOG, "a") as f:
                f.write(raw.decode(errors="replace") + "\n")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def d(delta):
            return {"choices": [{"index": 0, "delta": delta}]}
        self._sse(d({"reasoning_content": "Let me think. "}))
        self._sse(d({"reasoning_content": "The file needs a greeting fix."}))
        self._sse(d({"tool_calls": [{"index": 0, "id": "call_0",
                    "function": {"name": "read_file", "arguments": {"path": "greet.py"}}}]}))
        self._sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                   "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
