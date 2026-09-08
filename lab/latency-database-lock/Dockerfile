FROM python:3.13-alpine

RUN apk add --no-cache postgresql-client
WORKDIR /lab
COPY app.py probe.py /lab/

CMD ["python", "/lab/app.py"]
