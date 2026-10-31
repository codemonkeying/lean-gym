"""Fake llama-server for run-window tests: /v1/models answers; chat sleeps SLOW secs
then returns 500, so every probe run is slow and voids (probe never certifies)."""
import http.server, json, os, sys, time

SLOW = float(os.environ.get("SLOW", "5"))


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "wintest-model"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        time.sleep(SLOW)
        self.send_response(500)
        self.end_headers()


http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
