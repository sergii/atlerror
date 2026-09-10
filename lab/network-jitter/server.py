#!/usr/bin/env python3

import os
import socket

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "9000"))
BUFFER_SIZE = 65535

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((HOST, PORT))

while True:
    payload, peer = sock.recvfrom(BUFFER_SIZE)
    sock.sendto(payload, peer)
