import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DB_HOST = os.environ.get("DB_HOST", "db")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_USER = os.environ.get("DB_USER", "atlerror")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "atlerror")
DB_NAME = os.environ.get("DB_NAME", "atlerror")


def run_query(delay_ms: float) -> float:
    delay_seconds = max(delay_ms, 0.0) / 1000.0
    env = dict(os.environ)
    env["PGPASSWORD"] = DB_PASSWORD
    command = [
        "psql", "-X", "-q", "-A", "-t",
        "-h", DB_HOST, "-p", DB_PORT,
        "-U", DB_USER, "-d", DB_NAME,
        "-c", f"SELECT pg_sleep({delay_seconds:.6f});",
    ]
    started = time.monotonic()
    subprocess.run(command, env=env, capture_output=True, text=True, check=True, timeout=10)
    return (time.monotonic() - started) * 1000.0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def reply(self, status: int, payload):
        if isinstance(payload, str):
            body = payload.encode()
            content_type = "text/plain"
        else:
            body = json.dumps(payload).encode()
            content_type = "application/json"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.reply(200, "ok")
            return
        if parsed.path != "/work":
            self.reply(404, {"error": "not_found"})
            return
        try:
            params = parse_qs(parsed.query)
            delay_ms = float(params.get("delay_ms", ["0"])[0])
            db_ms = run_query(delay_ms)
            self.reply(200, {"database_query_latency_ms": db_ms})
        except Exception as error:
            self.reply(500, {"error": type(error).__name__})


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
