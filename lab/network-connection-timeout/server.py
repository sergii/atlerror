import os
import socket

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "9000"))

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(32)
    while True:
        connection, _ = server.accept()
        try:
            payload = connection.recv(64)
            if payload.startswith(b"ping"):
                connection.sendall(b"pong")
        finally:
            connection.close()
