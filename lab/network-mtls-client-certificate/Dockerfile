FROM python:3.13-slim
RUN apt-get update \
  && apt-get install -y --no-install-recommends openssl \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /lab
COPY generate_certs.sh server.py probe.py probe-wrong-ca.py probe-invalid-eku.py /lab/
RUN chmod +x /lab/generate_certs.sh && /lab/generate_certs.sh
CMD ["python", "/lab/server.py"]
