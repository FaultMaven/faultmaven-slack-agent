"""Download files attached to a Slack message, for forwarding as evidence.

Slack file URLs (``url_private``) are auth-gated: fetching the bytes requires the
bot token in an ``Authorization: Bearer`` header. We download here so the
FaultMaven backend never needs Slack credentials — the agent stays the sole
bridge (design §8): it reads Slack, forwards bytes, and the backend sees only a
normal multipart turn.

Downloads are **bounded** (count + per-file size) and **failure-tolerant**: a
file that won't download (permission, size, network) is skipped with a warning,
never fatal to the turn — partial evidence still beats none.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

# (filename, content, content_type) — the shape faultmaven.submit_turn expects.
SlackFile = tuple[str, bytes, str]

# Bounds. The backend also size-guards; these keep the turn payload sane and cap
# the work a single shortcut can trigger.
MAX_FILES = 5
MAX_FILE_BYTES = 8 * 1024 * 1024  # 8 MiB
DOWNLOAD_TIMEOUT = 30.0


def download_message_files(
    token: str,
    message: dict,
    *,
    max_files: int = MAX_FILES,
    max_bytes: int = MAX_FILE_BYTES,
    http_client: httpx.Client | None = None,
) -> list[SlackFile]:
    """Download a Slack message's attached files as ``(name, bytes, ctype)``.

    ``token`` is the bot token (``client.token`` in a Bolt handler). Returns an
    empty list when the message has no files or the token is missing. Skips any
    single file that is oversized, unreadable, or comes back as Slack's HTML
    login page (the tell-tale of a token lacking ``files:read`` / access).
    """

    files = message.get("files") or []
    if not files or not token:
        return []

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=DOWNLOAD_TIMEOUT)
    headers = {"Authorization": f"Bearer {token}"}
    out: list[SlackFile] = []
    try:
        for meta in files[:max_files]:
            if not isinstance(meta, dict):
                continue
            got = _download_one(client, headers, meta, max_bytes)
            if got is not None:
                out.append(got)
    finally:
        if owns_client:
            client.close()

    if files and not out:
        logger.warning(
            "Message had %d file(s) but none were ingested "
            "(size/permission/scope?)",
            len(files),
        )
    return out


def _download_one(
    client: httpx.Client, headers: dict[str, str], meta: dict, max_bytes: int
) -> SlackFile | None:
    name = meta.get("name") or meta.get("title") or meta.get("id") or "attachment"
    declared_type = (meta.get("mimetype") or "application/octet-stream").lower()
    # url_private_download forces a download response; url_private may inline.
    url = meta.get("url_private_download") or meta.get("url_private")
    if not url:
        logger.warning("Slack file %s has no url_private; skipping", name)
        return None

    # Cheap pre-check on the declared size before spending the transfer.
    size = meta.get("size")
    if isinstance(size, int) and size > max_bytes:
        logger.warning(
            "Skipping %s: declared %d bytes exceeds cap %d", name, size, max_bytes
        )
        return None

    try:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Failed to download Slack file %s: %s", name, exc)
        return None

    content = resp.content
    if len(content) > max_bytes:
        logger.warning(
            "Skipping %s: downloaded %d bytes exceeds cap %d",
            name,
            len(content),
            max_bytes,
        )
        return None

    # A token without access to the file gets a 200 HTML login page instead of
    # the bytes. Detect that so we don't forward a login page as "evidence".
    resp_type = resp.headers.get("content-type", "").lower()
    if "text/html" in resp_type and "html" not in declared_type:
        logger.warning(
            "Slack file %s returned HTML, not its content (missing files:read "
            "or no access?) — skipping",
            name,
        )
        return None

    return (name, content, declared_type)
