"""The scopes the agent asks Slack for, versus the ones the manifest advertises.

Slack grants exactly what the authorize URL's `scope` parameter names. The
manifest is what a workspace admin *reads* on the consent screen; it grants
nothing. So a scope added to one and not the other produces an install that
looks right and is missing a permission — and the listener that needs it fails
at runtime, per workspace, with `missing_scope`.

That is not hypothetical: `users:read` was added to both manifests for the
install-time admin check and not to `DEFAULT_BOT_SCOPES`, which would have made
`users.info` fail on every install and refused every workspace bind, forever.
The comment on `DEFAULT_BOT_SCOPES` already said "kept in lockstep" — a comment
is not a check, which is why this file exists.
"""

from __future__ import annotations

import json
from pathlib import Path

from config import DEFAULT_BOT_SCOPES, MANIFEST_ONLY_BOT_SCOPES

REPO_ROOT = Path(__file__).resolve().parent.parent


def _manifest_bot_scopes(name: str) -> set[str]:
    manifest = json.loads((REPO_ROOT / name).read_text())
    return set(manifest["oauth_config"]["scopes"]["bot"])


def test_every_requested_scope_is_advertised_in_the_manifest():
    """Asking for a scope the manifest omits is refused by Slack at install."""
    assert set(DEFAULT_BOT_SCOPES) <= _manifest_bot_scopes("manifest.json")


def test_every_manifest_scope_is_requested_unless_named_as_an_exception():
    """The direction that actually bites: advertised, never asked for, so never
    granted — and the failure lands in a listener, not at install."""
    unrequested = _manifest_bot_scopes("manifest.json") - set(DEFAULT_BOT_SCOPES)
    assert unrequested == MANIFEST_ONLY_BOT_SCOPES, (
        "manifest.json advertises bot scopes the install never requests: "
        f"{sorted(unrequested - MANIFEST_ONLY_BOT_SCOPES)}. Add them to "
        "DEFAULT_BOT_SCOPES, or to MANIFEST_ONLY_BOT_SCOPES with a reason."
    )


def test_users_read_is_requested_so_the_admin_check_can_run():
    """Named on its own: without it `users.info` raises `missing_scope`, which
    `installer_authority` reports as UNKNOWN and the install refuses the bind."""
    assert "users:read" in DEFAULT_BOT_SCOPES


def test_the_dev_manifest_advertises_no_scope_the_agent_cannot_use():
    """Socket mode installs by hand rather than through the authorize URL, so the
    dev manifest is not held to lockstep — but it must not advertise a scope that
    exists only for the OAuth install flow."""
    dev_only = _manifest_bot_scopes("manifest.dev.json") - set(DEFAULT_BOT_SCOPES)
    assert dev_only == set(), f"dev manifest carries unusable scopes: {sorted(dev_only)}"
