import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import psycopg
from psycopg_pool import ConnectionPool

DB_HOST = os.environ.get("DB_HOST", "db")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_USER = os.environ.get("DB_USER", "atlerror")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "atlerror")
DB_NAME = os.environ.get("DB_NAME", "atlerror")
APP_PORT = int(os.environ.get("APP_PORT", "8080"))
POOL_SIZE = int(os.environ.get("POOL_SIZE", "1"))

CONNINFO = (
    f"host={DB_HOST} port={DB_PORT} user={DB_USER} "
    f"password={DB_PASSWORD} dbname={DB_NAME}"
)

pool = ConnectionPool(
    conninfo=CONNINFO,
    min_size=POOL_SIZE,
    max_size=POOL_SIZE,
    timeout=5,
    open=True,
)
pool.wait(timeout=15)
holder_active = threading.Event()


def json_response(handler, payload, status=200):
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/health":
            json_response(self, {"ok": True, "pool_size": POOL_SIZE})
            return

        if parsed.path == "/status":
            busy = 1 if holder_active.is_set() else 0
            json_response(
                self,
                {
                    "holder_active": holder_active.is_set(),
                    "pool_busy": busy,
                    "pool_capacity": POOL_SIZE,
                    "pool_utilization": busy / POOL_SIZE,
                },
            )
            return

        if parsed.path == "/hold":
            hold_ms = int(params.get("ms", ["300"])[0])
            with pool.connection(timeout=5):
                holder_active.set()
                try:
                    time.sleep(hold_ms / 1000.0)
                finally:
                    holder_active.clear()
            json_response(self, {"held_ms": hold_ms})
            return

        if parsed.path == "/work":
            request_started = time.monotonic()
            checkout_started = time.monotonic()
            with pool.connection(timeout=5) as connection:
                checkout_wait_ms = (time.monotonic() - checkout_started) * 1000.0
                query_started = time.monotonic()
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                query_latency_ms = (time.monotonic() - query_started) * 1000.0
            request_latency_ms = (time.monotonic() - request_started) * 1000.0
            json_response(
                self,
                {
                    "checkout_wait_ms": checkout_wait_ms,
                    "database_query_latency_ms": query_latency_ms,
                    "request_latency_ms": request_latency_ms,
                },
            )
            return

        if parsed.path == "/direct-control":
            total_started = time.monotonic()
            with psycopg.connect(CONNINFO, connect_timeout=5) as connection:
                query_started = time.monotonic()
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                query_latency_ms = (time.monotonic() - query_started) * 1000.0
            total_latency_ms = (time.monotonic() - total_started) * 1000.0
            json_response(
                self,
                {
                    "database_query_latency_ms": query_latency_ms,
                    "direct_total_latency_ms": total_latency_ms,
                },
            )
            return

        json_response(self, {"error": "not found"}, status=404)


server = ThreadingHTTPServer(("0.0.0.0", APP_PORT), Handler)
server.serve_forever()
