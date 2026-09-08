import os
import socket
import struct

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "9000"))


def handle(connection):
    try:
        payload = connection.recv(64)
        if payload.startswith(b"reset"):
            connection.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_LINGER,
                struct.pack("ii", 1, 0),
            )
            connection.close()
            return

        connection.sendall(b"pong")
        connection.close()
    except OSError:
        try:
            connection.close()
        except OSError:
            pass


with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(32)
    while True:
        connection, _ = server.accept()
        handle(connection)
