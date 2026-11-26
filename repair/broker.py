"""Forward only bounded inference requests to Mistral's chat endpoint."""
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler
import json
import socketserver
from threading import Lock


class InferenceBroker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, api_key, model, requests, tokens):
        self.api_key = api_key
        self.model = model
        self.requests = requests
        self.tokens = tokens
        self.lock = Lock()
        self.used = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
        super().__init__(path, InferenceRequest)

    def reserve(self, body):
        with self.lock:
            if self.requests <= 0 or self.tokens <= 0:
                return None
            asked = body.get("max_tokens")
            limit = min(asked, self.tokens) if type(asked) is int and asked > 0 else self.tokens
            self.tokens -= limit
            self.requests -= 1
            self.used["requests"] += 1
            return limit

    def record_usage(self, data, streamed):
        events = (line[6:] for line in data.splitlines() if line.startswith(b"data: ")) if streamed else (data,)
        for event in events:
            try:
                usage = json.loads(event).get("usage")
            except (ValueError, AttributeError):
                continue
            if not isinstance(usage, dict):
                continue
            with self.lock:
                for key in ("prompt_tokens", "completion_tokens"):
                    value = usage.get(key)
                    if type(value) is int and value >= 0:
                        self.used[key] += value
            break


class InferenceRequest(BaseHTTPRequestHandler):
    def log_message(self, *args):
        # Never echo client-controlled URLs or inference messages in runner logs.
        pass

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 1_000_000:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(length))
        except (ValueError, TimeoutError):
            self.send_error(400)
            return
        if (not isinstance(body, dict) or body.get("model") != self.server.model
                or not isinstance(body.get("messages"), list) or body.get("tools")):
            self.send_error(400)
            return
        limit = self.server.reserve(body)
        if limit is None:
            self.send_error(429)
            return
        body["max_tokens"] = limit
        upstream = HTTPSConnection("api.mistral.ai", timeout=90)
        try:
            upstream.request("POST", "/v1/chat/completions", json.dumps(body),
                             {"Content-Type": "application/json",
                              "Authorization": "Bearer " + self.server.api_key})
            response = upstream.getresponse()
            if response.status != 200:
                self.send_error(response.status)
                return
            self.send_response(200)
            kind = response.getheader("Content-Type", "application/json")
            self.send_header("Content-Type", kind)
            self.end_headers()
            trace = bytearray()
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
                if len(trace) < 2_000_000:
                    trace.extend(chunk)
            self.server.record_usage(trace, kind.startswith("text/event-stream"))
        except OSError:
            self.close_connection = True
        finally:
            upstream.close()
