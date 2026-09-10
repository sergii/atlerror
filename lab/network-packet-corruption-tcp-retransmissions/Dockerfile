FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 ethtool \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /lab
COPY server.py probe.py /lab/
