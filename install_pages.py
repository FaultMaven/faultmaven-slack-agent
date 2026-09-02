"""The two pages an installing admin sees, and the HTML shell they share.

Kept apart from the flow logic so the security-relevant code in :mod:`binding`
is not interleaved with markup, and so every page here is built the same way:
**no request input is ever interpolated.** Only values the server established —
a workspace name from the Slack install, an organization id from a bind we just
performed — reach the templates, and they are escaped regardless.
"""

from __future__ import annotations

from html import escape

_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{title} · FaultMaven</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.55 system-ui, sans-serif; margin: 0; padding: 3rem 1.25rem;
         display: flex; justify-content: center; }}
  main {{ max-width: 34rem; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 .75rem; }}
  p {{ margin: 0 0 1rem; }}
  dl {{ margin: 1.25rem 0; padding: .9rem 1.1rem; border-radius: .5rem;
        background: rgba(127,127,127,.12); }}
  dt {{ font-size: .78rem; text-transform: uppercase; letter-spacing: .04em;
        opacity: .7; }}
  dd {{ margin: .1rem 0 .8rem; font-weight: 600; }}
  dd:last-child {{ margin-bottom: 0; }}
  .btn {{ display: inline-block; padding: .6rem 1.1rem; border-radius: .4rem;
          background: #2d6cdf; color: #fff; text-decoration: none; font-weight: 600; }}
  .muted {{ opacity: .72; font-size: .9rem; }}
  .bad {{ color: #b3261e; }}
</style></head><body><main>{body}</main></body></html>"""


def _page(title: str, body: str) -> str:
    return _SHELL.format(title=escape(title), body=body)


def confirm_page(*, workspace_name: str, team_id: str, authorize_url: str) -> str:
    """Ask the installer to confirm the workspace before the FaultMaven leg.

    This page exists because the dashboard's consent screen **cannot** carry
    this information: it renders the client name and a caller-supplied scope
    string, so it is evidence of nothing about which workspace is involved.
    Naming the workspace here is the only point in the flow where a human can
    catch an install they did not mean to connect.

    Deliberately before the authorization, not after: an admin who reads this
    and stops has handed over nothing, and the flow never has to hold their
    token across a request.
    """

    body = f"""
<h1>Connect this Slack workspace to FaultMaven</h1>
<p>You are about to give FaultMaven access to file investigations from this
   workspace, and to make them visible to a FaultMaven team.</p>
<dl>
  <dt>Slack workspace</dt><dd>{escape(workspace_name)}</dd>
  <dt>Workspace ID</dt><dd>{escape(team_id)}</dd>
</dl>
<p>Continuing signs you in to FaultMaven. The workspace is connected to
   <strong>the organization you sign in to</strong>, and this creates a service
   account and a team inside it.</p>
<p><a class="btn" href="{escape(authorize_url, quote=True)}">Continue to FaultMaven</a></p>
<p class="muted">If you did not just install FaultMaven in Slack, close this
   page — nothing has been connected.</p>"""
    return _page("Connect workspace", body)


def bound_page(*, workspace_name: str, organization_id: str) -> str:
    """Reported after a bind actually succeeded."""

    body = f"""
<h1>Workspace connected</h1>
<p><strong>{escape(workspace_name)}</strong> is now connected to FaultMaven.
   Investigations started from this workspace belong to it, and are visible to
   its team.</p>
<dl><dt>Organization</dt><dd>{escape(organization_id)}</dd></dl>
<p class="muted">You can close this page and go back to Slack.</p>"""
    return _page("Workspace connected", body)


def error_page(message: str) -> str:
    """A refusal or failure, in terms an administrator can act on."""

    body = f"""
<h1 class="bad">Couldn't connect the workspace</h1>
<p>{escape(message)}</p>
<p class="muted">Nothing was changed in FaultMaven. Slack still has the app
   installed — re-run the installation to try again.</p>"""
    return _page("Couldn't connect", body)


def unavailable_page() -> str:
    """Install succeeded, but this deployment cannot bind from the browser."""

    body = """
<h1>FaultMaven is installed</h1>
<p>The Slack app is installed and ready.</p>
<p>Connecting this workspace to a FaultMaven organization is a step your
   FaultMaven administrator completes — this deployment doesn't offer it from
   the browser.</p>
<p class="muted">You can close this page.</p>"""
    return _page("Installed", body)
