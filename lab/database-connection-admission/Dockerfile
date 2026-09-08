FROM python:3.13-slim

RUN pip install --no-cache-dir "psycopg[binary]>=3.2,<4"

WORKDIR /lab
COPY probe.py /lab/
