#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import socketserver
import time

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "9000"))
HANDLER_DELAY_SECONDS = float(os.getenv("HANDLER_DELAY_SECONDS", "0.005"))


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        request = self.rfile.readline()
        if request != b"ping\n":
            return

        started = time.perf_counter()
        time.sleep(HANDLER_DELAY_SECONDS)
        handler_ms = (time.perf_counter() - started) * 1000.0

        response = {
            "status": "pong",
            "handler_ms": round(handler_ms, 3),
        }
        self.wfile.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
        self.wfile.flush()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server((HOST, PORT), Handler) as server:
        server.serve_forever()
