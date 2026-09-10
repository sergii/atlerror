import socket

HOST = "0.0.0.0"
PORT = 9000

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((HOST, PORT))

while True:
    payload, address = sock.recvfrom(4096)
    sock.sendto(payload, address)
