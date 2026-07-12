"""Download files attached to a Slack message, for forwarding as evidence.

Slack file URLs (``url_private``) are auth-gated: fetching the bytes requires the
bot token in an ``Authorization: Bearer`` header. We download here so the
FaultMaven backend never needs Slack credentials — the agent stays the sole
bridge (design §8): it reads Slack, forwards bytes, and the backend sees only a
normal multipart turn.

Downloads are **bounded** and **failure-tolerant**:

- *Streamed* with a hard byte cap — the transfer is aborted the moment it
  exceeds ``MAX_FILE_BYTES``, so a file with missing/understated ``size``
  metadata can't buffer an unbounded body into memory.
- Capped at ``MAX_FILES`` *successful* downloads (not the first N candidates, so
  a readable file isn't dropped behind unreadable ones).
- A file that won't download (permission, size, network, or Slack's HTML sign-in
  page — the tell-tale of missing access) is skipped with a warning, never fatal
  to the turn. Partial evidence beats none.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# (filename, content, content_type) — the shape faultmaven.submit_turn expects.
SlackFile = tuple[str, bytes, str]

# Bounds. The backend also size-guards; these keep the turn payload sane and cap
# the work a single shortcut can trigger.
MAX_FILES = 5
MAX_FILE_BYTES = 8 * 1024 * 1024  # 8 MiB
DOWNLOAD_TIMEOUT = 20.0
_STREAM_CHUNK = 64 * 1024


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
    empty list when the message has no files or the token is missing. Stops after
    ``max_files`` successful downloads; skips any single file that is oversized,
    unreadable, empty, or comes back as Slack's HTML sign-in page.
    """

    candidates = message.get("files") or []
    if not candidates or not token:
        return []

    owns_client = http_client is None
    # follow_redirects: Slack file URLs legitimately 3xx to a CDN host; without
    # this httpx (unlike requests) would treat the redirect as the response.
    client = http_client or httpx.Client(
        timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
    )
    headers = {"Authorization": f"Bearer {token}"}
    out: list[SlackFile] = []
    try:
        for meta in candidates:
            if len(out) >= max_files:
                break
            if not isinstance(meta, dict):
                continue
            got = _download_one(client, headers, meta, max_bytes)
            if got is not None:
                out.append(got)
    finally:
        if owns_client:
            client.close()

    if not out:
        logger.warning(
            "Message had %d file(s) but none were ingested "
            "(size/permission/scope/empty?)",
            len(candidates),
        )
    return out


def _safe_name(raw: str | None) -> str:
    """Reduce a Slack-supplied filename to a bare, bounded basename.

    Defense in depth (parent CLAUDE.md: "sanitized filenames"): strip any path
    components and control chars so the name can't traverse or inject when the
    backend writes/echoes it.
    """

    name = os.path.basename((raw or "").replace("\\", "/")).strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '/\x00')
    name = name[:255]
    # basename() already strips separators (no ``../x`` is constructible), but a
    # whole name of only dots (``.``/``..``) survives it — reject those outright
    # so the returned name can never be a relative-path token.
    if not name or set(name) <= {"."}:
        return "attachment"
    return name


def _is_slack_login_page(content: bytes) -> bool:
    """True if the body looks like Slack's sign-in page (an access denial).

    When the token can't read a file, Slack answers 200 with its sign-in HTML
    instead of the bytes. We sniff the body (not just the content-type header, so
    an oddly-typed login response or a genuine .html attachment is still caught)
    for the sign-in markers. A real log/config/screenshot won't match.
    """

    head = content[:2048].lower()
    if b"<html" not in head and b"<!doctype html" not in head:
        return False
    return b"slack" in head and (
        b"sign in" in head or b"signin" in head or b"sign-in" in head
    )


def _download_one(
    client: httpx.Client, headers: dict[str, str], meta: dict, max_bytes: int
) -> SlackFile | None:
    name = _safe_name(meta.get("name") or meta.get("title") or meta.get("id"))
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
        content = _stream_capped(client, url, headers, max_bytes, name)
    except _TooLarge:
        logger.warning(
            "Skipping %s: exceeded cap %d bytes mid-stream", name, max_bytes
        )
        return None
    except Exception as exc:  # noqa: BLE001 — a bad file must never fail the turn
        logger.warning("Failed to download Slack file %s: %s", name, exc)
        return None

    if not content:
        logger.warning("Skipping %s: empty response body", name)
        return None
    if _is_slack_login_page(content):
        logger.warning(
            "Slack file %s came back as the sign-in page (missing files:read "
            "or no access?) — skipping",
            name,
        )
        return None

    return (name, content, declared_type)


class _TooLarge(Exception):
    """Internal signal: the streamed body passed the byte cap."""


def _stream_capped(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    max_bytes: int,
    name: str,
) -> bytes:
    """Stream the response, aborting as soon as it exceeds ``max_bytes``.

    Bounds peak memory to ~max_bytes + one chunk regardless of the declared or
    actual body size, so a missing/spoofed ``size`` can't force an unbounded
    buffer.
    """

    chunks: list[bytes] = []
    total = 0
    with client.stream("GET", url, headers=headers) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_bytes(_STREAM_CHUNK):
            total += len(chunk)
            if total > max_bytes:
                raise _TooLarge
            chunks.append(chunk)
    return b"".join(chunks)
