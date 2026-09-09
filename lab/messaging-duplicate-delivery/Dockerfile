FROM python:3.13-slim

WORKDIR /lab
RUN python -m pip install --disable-pip-version-check "redis>=5,<7"
COPY probe.py /lab/probe.py

CMD ["python", "/lab/probe.py"]
