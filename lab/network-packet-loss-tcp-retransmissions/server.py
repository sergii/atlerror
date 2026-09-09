#!/usr/bin/env python3

import os
import socket
import struct

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "9000"))
HEADER_SIZE = 8


def recv_exact(conn: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = conn.recv(min(65536, remaining))
        if not chunk:
            raise ConnectionError("peer closed before expected payload completed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind((HOST, PORT))
listener.listen(16)

while True:
    conn, _ = listener.accept()
    with conn:
        header = recv_exact(conn, HEADER_SIZE)
        expected = struct.unpack("!Q", header)[0]
        received = 0
        while received < expected:
            chunk = conn.recv(min(65536, expected - received))
            if not chunk:
                raise ConnectionError("peer closed during transfer")
            received += len(chunk)
        conn.sendall(struct.pack("!Q", received))
