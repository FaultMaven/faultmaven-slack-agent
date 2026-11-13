# syntax=docker/dockerfile:1.6@sha256:ac85f380a63b13dfcefa89046420e1781752bab202122f8f50032edf31be0021
# FaultMaven Slack Agent — HTTP/OAuth transport container.
#
# Runs the FastAPI server (web.py) that hosts the Slack request handler
# (/slack/events, /slack/install, /slack/oauth_redirect) plus /health. The
# entrypoint dispatches on SLACK_TRANSPORT; the hosted deployment sets it to
# "http". See docs/HOSTING.md.

# The trust-store checks are core's scripts at a pinned commit, verified by digest. The
# commit moves when core's checks change; the Cloud image pins its own. A failed fetch
# (a 404 or 5xx body) reads as a digest mismatch. CI's gha cache restores these layers
# by their checksums, so a dead URL shows only on a cold cache. This stage only feeds
# the bind mounts below: nothing of it ships.
FROM scratch AS trust-check
ADD --checksum=sha256:5ee708a5a56f4ce45897ae9c9abb6132c9aa0e3617211411b4e96382ea1d292f \
    https://raw.githubusercontent.com/FaultMaven/faultmaven/b8e5ade7f3fbc9cba3d9deb1e0ecb39af450fb87/scripts/check_image_trust_store.py \
    /check_image_trust_store.py
ADD --checksum=sha256:b3af597fbe27b7e2e4ad71664f0336d6438cf13da888320c737c538c556cf7d6 \
    https://raw.githubusercontent.com/FaultMaven/faultmaven/b8e5ade7f3fbc9cba3d9deb1e0ecb39af450fb87/scripts/check_pip_root_set.py \
    /check_pip_root_set.py

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

# pip (24.2+) verifies through truststore, whose context loads the OS store, pip's own
# vendored certifi bundle (an old one, holding both roots the checks below ban) and
# PIP_CERT. So the vendored bundle is linked to the OS store and PIP_CERT is set to it,
# before the first pip. Never ENV a TLS root variable the agent owns (tls_root_set.py):
# any value but the set's own file refuses the start.
RUN cacert="$(python -c 'import pip._vendor.certifi as c; print(c.where())')" \
    && ln -sf /etc/ssl/certs/ca-certificates.crt "$cacert"
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

# Install deps first for layer caching, then the app source.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Link every pip's vendored bundle to the OS bundle, so a pip run --isolated or
# without PIP_CERT verifies against it, and remove the ensurepip pip wheel, which
# would install its old bundle into a new venv (core #2756). After the last install
# and the source COPY.
RUN find / -xdev \( -path '*/pip/_vendor/certifi/cacert.pem' \
        -exec ln -sfv /etc/ssl/certs/ca-certificates.crt {} \; \) \
    -o \( -path '*/ensurepip/_bundled/pip-*.whl' -print -delete \)

# Non-root: the runtime needs no privileged access. Owns /app/data so a SQLite
# fallback (dev only — the cluster uses Postgres) can be written if configured.
# The empty capath SSL_CERT_DIR names (tls_root_set.py) stays root-owned, so
# the runtime user cannot write into it.
RUN useradd --create-home --uid 10001 faultmaven \
    && mkdir -p /app/data \
    && chown -R faultmaven:faultmaven /app \
    && chown -R root:root /app/tls_empty_capath

# Fail the build if "emSign ECC Root CA - G3" (GHSA-6q48-6c7w-9gw2: compromised
# intermediate keys) or "AAA Certificate Services" is in OpenSSL's default cafile or
# capath, certifi or any pip wheel, or in any pip bundle, which must also be the
# cafile; then measure, from pip's own session, that a pip run --isolated or without
# PIP_CERT verifies against the cafile. The last root step, to see the stores the
# image ships; tests/test_dockerfile_trust_store.py keeps everything after it inert.
# CI runs scripts/check_tls_root_set.py in the built image.
RUN --mount=type=bind,from=trust-check,source=/check_image_trust_store.py,target=/tmp/check_image_trust_store.py \
    --mount=type=bind,from=trust-check,source=/check_pip_root_set.py,target=/tmp/check_pip_root_set.py \
    python /tmp/check_image_trust_store.py && python /tmp/check_pip_root_set.py
USER faultmaven

EXPOSE 3000

# Liveness/readiness both hit /health (dependency-free, so a slow FM backend
# can't fail the pod). k8s probes are defined in the Deployment; this HEALTHCHECK
# is for `docker run` parity.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('HTTP_PORT','3000')+'/health').read()" || exit 1

CMD ["python", "app.py"]
