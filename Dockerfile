# FaultMaven Slack Agent — HTTP/OAuth transport container.
#
# Runs the FastAPI server (web.py) that hosts the Slack request handler
# (/slack/events, /slack/install, /slack/oauth_redirect) plus /health. The
# entrypoint dispatches on SLACK_TRANSPORT; the hosted deployment sets it to
# "http". See docs/HOSTING.md.

# The trust-store check is core's script at a pinned commit, verified by digest. The
# Cloud image pins the same commit, and the two move together; its scheduled pin-drift
# check is the signal for both. A failed fetch (a 404 or 5xx body) reads as a digest
# mismatch. CI's gha cache restores this layer by its checksum, so a dead URL shows
# only on a cold cache. This stage only feeds a bind mount below: nothing of it ships.
FROM scratch AS trust-check
ADD --checksum=sha256:af182a0403cd0c82fe1d6f159a5e59395fd47d66a46cacab65cb973e0c905ff7 \
    https://raw.githubusercontent.com/FaultMaven/faultmaven/9a702772183c045576c069f9e1d81f50bcf2b831/scripts/check_image_trust_store.py \
    /check_image_trust_store.py

FROM python:3.12-slim

# - PYTHONUNBUFFERED: logs flush immediately (structured-log visibility in k8s).
# - PYTHONDONTWRITEBYTECODE: no .pyc litter on the read-only-ish layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SLACK_TRANSPORT=http \
    HTTP_PORT=3000

# ca-certificates is a no-op on a current base, which holds the fixed package; it
# protects a stale local base tag.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The base's pip vendors an old CA bundle: every pip here verifies against the OS store.
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

# Install deps first for layer caching, then the app source.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root: the runtime needs no privileged access. Owns /app/data so a SQLite
# fallback (dev only — the cluster uses Postgres) can be written if configured.
RUN useradd --create-home --uid 10001 faultmaven \
    && mkdir -p /app/data \
    && chown -R faultmaven:faultmaven /app

# Fail the build if "emSign ECC Root CA - G3" (GHSA-6q48-6c7w-9gw2: compromised
# intermediate keys) or "AAA Certificate Services" is in OpenSSL's default cafile or
# capath, which slack-sdk reads through urllib, or in certifi, which httpx (the
# FaultMaven API client) reads. pip reads the cafile through PIP_CERT. The last
# root step, to see the stores the image ships.
RUN --mount=type=bind,from=trust-check,source=/check_image_trust_store.py,target=/tmp/check_image_trust_store.py \
    python /tmp/check_image_trust_store.py
USER faultmaven

EXPOSE 3000

# Liveness/readiness both hit /health (dependency-free, so a slow FM backend
# can't fail the pod). k8s probes are defined in the Deployment; this HEALTHCHECK
# is for `docker run` parity.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('HTTP_PORT','3000')+'/health').read()" || exit 1

CMD ["python", "app.py"]
