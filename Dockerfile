FROM python:3.12-slim

# No third-party dependencies — everything used (urllib, json, hmac, gzip, http.server)
# is Python stdlib, so there's nothing to pip install and nothing to audit.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY deddrop.py .
COPY web ./web

RUN useradd --create-home --uid 1000 uploader \
    && mkdir -p /data/snapshots /data/state \
    && chown -R uploader:uploader /data /app

USER uploader
VOLUME ["/data"]
EXPOSE 8080

# /healthz is unauthenticated by design and exposes nothing beyond liveness.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import os,sys,urllib.request; \
sys.exit(0) if os.environ.get('WEB_ENABLED','true').lower() in ('0','false','no') \
else urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('WEB_PORT','8080'), timeout=4)"

ENTRYPOINT ["python3", "-u", "deddrop.py"]
