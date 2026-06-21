"""Slack request authentication helpers.

Every inbound request from Slack (Events API, interactivity, slash commands)
is signed with an HMAC-SHA256 signature derived from the app's *signing
secret*. Verifying this signature is the only thing standing between the
public webhook endpoint and a spoofed request, so it MUST run before any
payload is parsed or acted upon.

We lean on ``slack_sdk.signature.SignatureVerifier`` for the constant-time
comparison and add an explicit timestamp/replay check on top so the failure
modes are visible in our own logs.
"""

from __future__ import annotations

import logging
import time

from fastapi import Request
from slack_sdk.signature import SignatureVerifier

from config import get_settings

logger = logging.getLogger(__name__)


class SlackVerificationError(Exception):
    """Raised when an inbound request cannot be authenticated as Slack."""


_verifier: SignatureVerifier | None = None


def _get_verifier() -> SignatureVerifier:
    """Lazily construct the signature verifier from settings."""

    global _verifier
    if _verifier is None:
        _verifier = SignatureVerifier(get_settings().slack_signing_secret)
    return _verifier


async def verify_slack_request(request: Request) -> bytes:
    """Authenticate an inbound Slack request and return its raw body.

    Slack signs the *raw* request body, so callers must use the bytes
    returned here for any subsequent JSON/form parsing — re-reading the
    stream is not possible and re-serializing could alter the bytes and
    break verification.

    Raises:
        SlackVerificationError: if headers are missing, the timestamp is
            stale (replay), or the signature does not match.
    """

    body = await request.body()

    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not timestamp or not signature:
        raise SlackVerificationError("Missing Slack signature headers")

    # Replay protection: reject anything older than the configured window.
    try:
        request_age = abs(time.time() - int(timestamp))
    except ValueError as exc:
        raise SlackVerificationError("Malformed Slack timestamp header") from exc

    max_age = get_settings().slack_request_max_age
    if request_age > max_age:
        raise SlackVerificationError(
            f"Stale Slack request: {request_age:.0f}s old (max {max_age}s)"
        )

    # Constant-time HMAC comparison handled by the Slack SDK.
    if not _get_verifier().is_valid(
        body=body, timestamp=timestamp, signature=signature
    ):
        raise SlackVerificationError("Invalid Slack request signature")

    return body
