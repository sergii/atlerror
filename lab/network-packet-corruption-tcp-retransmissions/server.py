#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import socket
import struct

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "9000"))
COMMAND_DATA = b"D"
HEADER_SIZE = 8 + 32


def recv_exact(conn: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = conn.recv(min(65536, remaining))
        if not chunk:
            raise ConnectionError("peer closed before expected bytes arrived")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def tcp_counters() -> dict[str, int]:
    with open("/proc/net/snmp", encoding="utf-8") as handle:
        lines = [line.strip() for line in handle if line.startswith("Tcp:")]
    if len(lines) < 2:
        raise RuntimeError("Tcp counters are missing from /proc/net/snmp")
    headers = lines[-2].split()[1:]
    values = lines[-1].split()[1:]
    counters = dict(zip(headers, map(int, values), strict=True))
    return {
        "in_errs": counters.get("InErrs", 0),
        "in_csum_errors": counters.get("InCsumErrors", 0),
    }


def handle_data(conn: socket.socket) -> None:
    header = recv_exact(conn, HEADER_SIZE)
    expected_bytes = struct.unpack("!Q", header[:8])[0]
    expected_digest = header[8:]
    counters_before = tcp_counters()
    conn.sendall(b"R")

    digest = hashlib.sha256()
    received = 0
    while received < expected_bytes:
        chunk = conn.recv(min(65536, expected_bytes - received))
        if not chunk:
            raise ConnectionError("peer closed during payload transfer")
        digest.update(chunk)
        received += len(chunk)

    counters_after = tcp_counters()
    actual_digest = digest.digest()
    result = {
        "received_bytes": received,
        "digest_match": actual_digest == expected_digest,
        "tcp_in_errs_before": counters_before["in_errs"],
        "tcp_in_errs_after": counters_after["in_errs"],
        "tcp_in_errs_delta": counters_after["in_errs"] - counters_before["in_errs"],
        "tcp_in_csum_errors_before": counters_before["in_csum_errors"],
        "tcp_in_csum_errors_after": counters_after["in_csum_errors"],
        "tcp_in_csum_errors_delta": counters_after["in_csum_errors"] - counters_before["in_csum_errors"],
    }
    payload = json.dumps(result, sort_keys=True).encode("utf-8")
    conn.sendall(struct.pack("!I", len(payload)) + payload)


listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind((HOST, PORT))
listener.listen(16)

while True:
    conn, _ = listener.accept()
    with conn:
        try:
            command = conn.recv(1)
            if not command:
                continue
            if command != COMMAND_DATA:
                continue
            handle_data(conn)
        except (ConnectionError, OSError):
            continue
