FROM python:3.12-slim

# No third-party dependencies — everything used (urllib, json, hmac, gzip, http.server)
# is Python stdlib, so there's nothing to pip install and nothing to audit.

WORKDIR /app
COPY deddrop.py .
COPY web ./web

RUN useradd --create-home --uid 1000 uploader \
    && mkdir -p /data/snapshots \
    && chown -R uploader:uploader /data /app

USER uploader
VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["python3", "-u", "deddrop.py"]
