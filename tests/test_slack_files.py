"""Slack file ingestion — auth'd download, bounds, and failure tolerance.

Uses httpx.MockTransport to stand in for Slack's file CDN, so these exercise the
real request shaping (Bearer header, url_private routing) without the network.
"""

from __future__ import annotations

import httpx

from slack_files import download_message_files


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _file(**over) -> dict:
    meta = {
        "name": "app.log",
        "mimetype": "text/plain",
        "size": 4,
        "url_private_download": "https://files.slack.com/app.log?d=1",
        "url_private": "https://files.slack.com/app.log",
    }
    meta.update(over)
    return meta


# -- happy path ---------------------------------------------------------------
def test_downloads_file_with_bearer_and_prefers_download_url():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"boom", headers={"content-type": "text/plain"})

    out = download_message_files(
        "xoxb-tok", {"files": [_file()]}, http_client=_client(handler)
    )
    assert out == [("app.log", b"boom", "text/plain")]
    assert seen["auth"] == "Bearer xoxb-tok"
    assert seen["url"].endswith("d=1")  # url_private_download preferred


def test_no_files_or_no_token_returns_empty():
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        called["n"] += 1
        return httpx.Response(200, content=b"x")

    assert download_message_files("tok", {}, http_client=_client(handler)) == []
    assert download_message_files("", {"files": [_file()]}, http_client=_client(handler)) == []
    assert called["n"] == 0  # never hit the network


# -- bounds & failure tolerance ----------------------------------------------
def test_skips_file_missing_url():
    out = download_message_files(
        "tok",
        {"files": [{"name": "x", "mimetype": "text/plain"}]},
        http_client=_client(lambda r: httpx.Response(200, content=b"x")),
    )
    assert out == []


def test_skips_oversized_by_declared_size_without_downloading():
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        called["n"] += 1
        return httpx.Response(200, content=b"x" * 100)

    out = download_message_files(
        "tok",
        {"files": [_file(size=10_000)]},
        http_client=_client(handler),
        max_bytes=1000,
    )
    assert out == []
    assert called["n"] == 0  # pre-checked declared size, skipped the transfer


def test_skips_oversized_by_downloaded_length():
    # Declared size absent/small but the body is large → caught after download.
    out = download_message_files(
        "tok",
        {"files": [_file(size=None)]},
        http_client=_client(
            lambda r: httpx.Response(200, content=b"x" * 5000, headers={"content-type": "text/plain"})
        ),
        max_bytes=1000,
    )
    assert out == []


def test_skips_html_login_page_when_file_is_not_html():
    # A token lacking files:read gets a 200 HTML login page, not the bytes.
    out = download_message_files(
        "tok",
        {"files": [_file(mimetype="text/plain")]},
        http_client=_client(
            lambda r: httpx.Response(200, content=b"<html>login</html>", headers={"content-type": "text/html"})
        ),
    )
    assert out == []


def test_keeps_genuine_html_file():
    out = download_message_files(
        "tok",
        {"files": [_file(name="page.html", mimetype="text/html")]},
        http_client=_client(
            lambda r: httpx.Response(200, content=b"<html>real</html>", headers={"content-type": "text/html"})
        ),
    )
    assert out == [("page.html", b"<html>real</html>", "text/html")]


def test_download_error_skips_that_file_but_keeps_others():
    def handler(request: httpx.Request) -> httpx.Response:
        if "bad" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, content=b"good", headers={"content-type": "text/plain"})

    out = download_message_files(
        "tok",
        {"files": [
            _file(name="bad.log", url_private_download="https://files.slack.com/bad.log"),
            _file(name="good.log", url_private_download="https://files.slack.com/good.log"),
        ]},
        http_client=_client(handler),
    )
    assert out == [("good.log", b"good", "text/plain")]


def test_respects_max_files_cap():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x", headers={"content-type": "text/plain"})

    files = [_file(name=f"f{i}.log", url_private_download=f"https://files.slack.com/f{i}") for i in range(10)]
    out = download_message_files("tok", {"files": files}, http_client=_client(handler), max_files=3)
    assert len(out) == 3
