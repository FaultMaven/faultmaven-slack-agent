# FaultMaven Slack Agent — HTTP/OAuth transport container.
#
# Runs the FastAPI server (web.py) that hosts the Slack request handler
# (/slack/events, /slack/install, /slack/oauth_redirect) plus /health. The
# entrypoint dispatches on SLACK_TRANSPORT; the hosted deployment sets it to
# "http". See docs/HOSTING.md.

FROM python:3.12-slim

# - PYTHONUNBUFFERED: logs flush immediately (structured-log visibility in k8s).
# - PYTHONDONTWRITEBYTECODE: no .pyc litter on the read-only-ish layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SLACK_TRANSPORT=http \
    HTTP_PORT=3000

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
USER faultmaven

EXPOSE 3000

# Liveness/readiness both hit /health (dependency-free, so a slow FM backend
# can't fail the pod). k8s probes are defined in the Deployment; this HEALTHCHECK
# is for `docker run` parity.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('HTTP_PORT','3000')+'/health').read()" || exit 1

CMD ["python", "app.py"]
