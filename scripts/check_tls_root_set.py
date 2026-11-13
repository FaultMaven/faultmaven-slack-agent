#!/usr/bin/env python3
"""Every TLS client the agent builds verifies against the one root set (FaultMaven #2733).

Modelled on core's ``scripts/check_tls_root_set.py``. Two steps, as CI runs them:

    python scripts/check_tls_root_set.py --make-certs DIR   # on the host: openssl CLI
    docker run --rm -v DIR:/certs:ro IMAGE python scripts/check_tls_root_set.py --certs /certs

``--make-certs`` writes two throwaway CAs, each with a leaf for ``127.0.0.1``:
``set``, given to the agent as ``TLS_EXTRA_CA_FILE``, and ``outside``, in no
set. ``--certs`` serves them from loopback responders that count completed TLS
handshakes, and runs each client, built through its product constructor, in a
fresh process (retries off), in three arms:

- **set**: ``apply_root_set`` with the setting, as ``app.main()`` calls it;
- **unset**: ``apply_root_set`` without it;
- **bare**: no ``apply_root_set`` (the agent before #2733).

An HTTPS row passes when **set** succeeds after a handshake and **unset** and
**bare** fail with a certificate verification error. The Postgres rows go
through ``build_oauth_stores``: ``verify-full`` against the set's leaf; a URL
naming no ``sslmode`` (so ``require``, ruled 2026-11-13) against the outside
leaf, which only the set refuses; and ``sslmode=disable``, which attempts no
TLS. One row reads ``TLS_EXTRA_CA_FILE`` from a ``.env`` through ``Settings``.

Socket Mode's builtin client wraps TLS only on port 443, so its responder
listens on ``127.0.0.1:443`` (unprivileged in a container). Every row connects
to ``127.0.0.1``, never a name. Each client process runs without ``PG*`` or
``*_proxy`` variables, and refuses a Python-level connect or lookup off
loopback; libpq's C socket is not patched. Exits non-zero on any failed row.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HOST = "127.0.0.1"
SOCKET_MODE_PORT = 443
MARKER = "tls-root-set-probe reached"
VERIFY_FAILED = ("CERTIFICATE_VERIFY_FAILED", "certificate verify failed")
CHILD_TIMEOUT = 60
ARMS = ("set", "unset", "bare")
_SSL_REQUEST, _GSSENC_REQUEST = 80877103, 80877104
_WEBSOCKET_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --- the clients, each in its own process -------------------------------------


def _url(ports: dict, path: str = "/probe") -> str:
    return f"https://{HOST}:{ports['https']}{path}"


def _pg_url(port: int, query: str) -> str:
    return f"postgresql://probe@{HOST}:{port}/probe?connect_timeout=10{query}"


def _reached(connect) -> None:
    """The Postgres responder's error came back: the connection got through."""

    try:
        connect()
    except Exception as error:
        if MARKER in str(error):
            return
        raise
    raise RuntimeError("the responder never answered")


def _no_warnings(logs: list[str]) -> None:
    """A client that logs its failure instead of raising it."""

    if logs:
        raise RuntimeError(" | ".join(logs))


def _web_client(ports: dict, logs: list[str]) -> None:
    from app import make_web_client

    client = make_web_client("xoxb-probe")
    client.base_url, client.retry_handlers = _url(ports, "/api/"), []
    client.api_call("auth.test")


def _fault_client(ports: dict, logs: list[str]) -> None:
    from app import make_fault_client
    from config import get_settings

    fm = make_fault_client(get_settings())
    try:
        fm.health()
    finally:
        fm.close()


def _download(ports: dict, logs: list[str]) -> None:
    from slack_files import download_message_content

    meta = {"id": "F1", "name": "probe.log", "mimetype": "text/plain"}
    message = {"files": [dict(meta, url_private_download=_url(ports))]}
    files, pasted, _ = download_message_content("xoxb-probe", message)
    if not files and not pasted:
        raise RuntimeError(" | ".join(logs) or "nothing was downloaded")


def _respond_ephemeral(ports: dict, logs: list[str]) -> None:
    from listeners.shortcuts import _respond_ephemeral

    _respond_ephemeral(_url(ports), "probe")
    _no_warnings(logs)


def _socket_mode(ports: dict, logs: list[str]) -> None:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    from app import make_web_client

    app = App(client=make_web_client("xoxb-probe"), token_verification_enabled=False)
    handler = SocketModeHandler(app, "xapp-probe")
    # Preset, so connect() never asks slack.com for a URL.
    handler.client.wss_uri = f"wss://{HOST}:{SOCKET_MODE_PORT}/link"
    handler.client.default_auto_reconnect_enabled = False  # connect() restores it
    try:
        handler.connect()  # logs a failed connection rather than raising it
        connected = handler.client.is_connected()
    finally:
        handler.close()
    if not connected:
        raise RuntimeError(" | ".join(logs) or "the websocket did not connect")


def _oauth_stores(port: str, query: str):
    def connect(ports: dict, logs: list[str]) -> None:
        from oauth_store import build_oauth_stores

        url = _pg_url(ports[port], query)
        _reached(lambda: build_oauth_stores(database_url=url, client_id="probe"))

    return connect


# name: (responder, client, kind). kind picks the verdict rule in _verdict.
ROWS = {
    "WebClient (slack-sdk, urllib)": ("https", _web_client, "https"),
    "WebClient, setting from .env": ("https", _web_client, "dotenv"),
    "FaultMavenClient (httpx)": ("https", _fault_client, "https"),
    "slack_files download (httpx)": ("https", _download, "https"),
    "shortcut response_url (httpx)": ("https", _respond_ephemeral, "https"),
    "Socket Mode websocket": ("wss", _socket_mode, "https"),
    "postgres verify-full": ("pg", _oauth_stores("pg", "&sslmode=verify-full"), "pg"),
    "postgres no sslmode, outside CA": ("outside", _oauth_stores("outside", ""), "require"),
    "postgres sslmode=disable": ("pg", _oauth_stores("pg", "&sslmode=disable"), "plain"),
}


def _loopback_only() -> None:
    allowed = {None, HOST, HOST.encode()}
    real_connect, real_lookup = socket.socket.connect, socket.getaddrinfo

    def connect(self, address):
        if (
            self.family in (socket.AF_INET, socket.AF_INET6)
            and address[0] not in allowed
        ):
            raise OSError(f"check_tls_root_set: refused connecting to {address[0]}")
        return real_connect(self, address)

    def lookup(host, *args, **kwargs):
        if host not in allowed:
            raise socket.gaierror(f"check_tls_root_set: refused resolving {host!r}")
        return real_lookup(host, *args, **kwargs)

    socket.socket.connect, socket.getaddrinfo = connect, lookup


class _Logs(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{record.levelname}: {record.getMessage()}")


def _child(name: str, arm: str, ports: dict) -> int:
    _loopback_only()
    logs = _Logs()
    logging.getLogger().addHandler(logs)
    logging.getLogger().setLevel(logging.WARNING)
    try:
        if arm != "bare":  # exactly as app.main() calls it
            from config import get_settings
            from tls_root_set import apply_root_set

            apply_root_set(get_settings().tls_extra_ca_file)
        ROWS[name][1](ports, logs.lines)
    except BaseException as error:
        print(f"refused: {type(error).__name__}: {error}")
        return 1
    return 0


# --- the responders -------------------------------------------------------------


def _serve(context: ssl.SSLContext, postgres: bool, port: int = 0):
    """A listener answering HTTPS, or PostgreSQL with or without TLS. Returns it
    and its counts: completed TLS handshakes, plaintext Postgres startups."""

    listener = socket.create_server((HOST, port))
    counts: dict[str, list] = {"tls": [], "plain": []}

    def postgres_error(connection) -> None:  # an ErrorResponse to the StartupMessage
        fields = b"SFATAL\0VFATAL\0C28000\0M" + MARKER.encode() + b"\0\0"
        connection.sendall(b"E" + struct.pack("!i", len(fields) + 4) + fields)

    def answer(connection: socket.socket) -> None:
        connection.settimeout(30)
        while postgres:  # SSLRequest gets "S"; GSSENCRequest "N"
            length, code = struct.unpack("!ii", connection.recv(8, socket.MSG_WAITALL))
            if code == _SSL_REQUEST:
                connection.sendall(b"S")
                break
            if code == _GSSENC_REQUEST:
                connection.sendall(b"N")
                continue
            connection.recv(length - 8, socket.MSG_WAITALL)  # a plaintext startup
            counts["plain"].append(1)
            postgres_error(connection)
            return
        tls = context.wrap_socket(connection, server_side=True)
        counts["tls"].append(1)
        data = tls.recv(65536)
        if postgres:
            postgres_error(tls)
            return
        while data and b"\r\n\r\n" not in data:
            data += tls.recv(65536)
        # Read the body too: closing on unread bytes resets the connection, and a
        # reset can drop the response before the client reads it.
        end = data.find(b"\r\n\r\n") + 4
        length = re.search(rb"(?im)^Content-Length:\s*(\d+)", data[:end])
        while length and len(data) - end < int(length[1]) and (more := tls.recv(65536)):
            data += more
        if key := re.search(rb"(?im)^Sec-WebSocket-Key:\s*(\S+)", data):
            digest = hashlib.sha1(key[1] + _WEBSOCKET_GUID).digest()
            accept = base64.b64encode(digest)
            tls.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            while tls.recv(65536):  # held open until the client closes
                pass
            return
        body = b'{"ok": true}'
        head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        head += b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
        tls.sendall(head + body)

    def serve(connection: socket.socket) -> None:
        with connection:
            try:
                answer(connection)
            except (OSError, ssl.SSLError, struct.error):
                pass

    def accept() -> None:
        while True:
            try:
                connection = listener.accept()[0]
            except OSError:
                return
            threading.Thread(target=serve, args=(connection,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return listener, counts


def _server_context(certs: Path, name: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs / f"{name}-chain.pem", certs / f"{name}-key.pem")
    return context


# --- the verdicts ------------------------------------------------------------------


def _verdict(kind: str, arm: str, code: int, said: str, tls: int, plain: int) -> bool:
    refused_unverified = code != 0 and any(text in said for text in VERIFY_FAILED)
    if kind == "plain":  # sslmode=disable: through in plaintext, in every arm
        return code == 0 and tls == 0 and plain >= 1
    if kind == "require":  # only the set verifies require; bare completes TLS
        return tls >= 1 if arm == "bare" else refused_unverified
    if arm == "set":
        return code == 0 and tls >= 1
    if kind == "pg" and arm == "bare":  # libpq has no root file: no handshake
        return code != 0 and tls == 0
    return refused_unverified and tls == 0


def _run(name: str, arm: str, ports: dict, counts: dict, env: dict, scratch: Path):
    server, _, kind = ROWS[name]
    before = {key: len(value) for key, value in counts[server].items()}
    env = dict(env)
    cwd = scratch / "cwd"
    if arm == "set" and kind == "dotenv":
        cwd = scratch / "dotenv"
    elif arm != "set":
        env.pop("TLS_EXTRA_CA_FILE")
    command = [sys.executable, __file__, "--child", name, "--arm", arm]
    try:
        result = subprocess.run(
            command + ["--ports", json.dumps(ports)],
            env=env, cwd=cwd, capture_output=True, text=True, timeout=CHILD_TIMEOUT,
        )  # fmt: skip
        code, said = result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        code, said = 1, f"refused: timed out after {CHILD_TIMEOUT}s"
    tls = len(counts[server]["tls"]) - before["tls"]
    plain = len(counts[server]["plain"]) - before["plain"]
    line = next((x for x in said.splitlines() if x.startswith("refused: ")), "ok")
    ok = _verdict(kind, arm, code, said, tls, plain)
    detail = f"{tls} TLS, {plain} plaintext; {line[:240]}"
    return ("PASS" if ok else "FAIL"), detail


def _child_env(certs: Path, scratch: Path, ports: dict) -> dict:
    from tls_root_set import OWNED_VARIABLES

    agent = ("SLACK_", "FAULTMAVEN_", "TLS_EXTRA_CA_FILE", "CASE_STORE", "CREDENTIAL_STORE")
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in OWNED_VARIABLES
        and not name.lower().endswith("_proxy")
        and not name.startswith(("PG", *agent))
    }
    env.update(
        SLACK_TRANSPORT="socket",
        SLACK_BOT_TOKEN="xoxb-probe",
        SLACK_APP_TOKEN="xapp-probe",
        FAULTMAVEN_API_URL=_url(ports, ""),
        FAULTMAVEN_API_TOKEN="probe",
        CASE_STORE_PATH=str(scratch / "cases.db"),
        CREDENTIAL_STORE_PATH=str(scratch / "credentials.db"),
        TLS_EXTRA_CA_FILE=str(certs / "set-ca.pem"),
        TMPDIR=str(scratch),
    )
    return env


def check(certs: Path) -> int:
    failed, ports, counts, listeners = False, {}, {}, []
    with tempfile.TemporaryDirectory(prefix="check-tls-root-set-") as name:
        scratch = Path(name)
        (scratch / "cwd").mkdir()
        (scratch / "dotenv").mkdir()
        (scratch / "dotenv" / ".env").write_text(
            f"TLS_EXTRA_CA_FILE={certs / 'set-ca.pem'}\n"
        )
        inside, outside = _server_context(certs, "set"), _server_context(certs, "outside")
        servers = {
            "https": (inside, False, 0),
            "wss": (inside, False, SOCKET_MODE_PORT),
            "pg": (inside, True, 0),
            "outside": (outside, True, 0),
        }
        unavailable = {}
        for server, (context, postgres, port) in servers.items():
            try:
                listener, counts[server] = _serve(context, postgres, port)
            except OSError as error:
                unavailable[server] = f"cannot listen on {HOST}:{port}: {error}"
                continue
            ports[server] = listener.getsockname()[1]
            listeners.append(listener)
        env = _child_env(certs, scratch, ports)
        for row, (server, _, _) in ROWS.items():
            for arm in ARMS:
                if server in unavailable:
                    verdict, detail = "FAIL", unavailable[server]
                else:
                    verdict, detail = _run(row, arm, ports, counts, env, scratch)
                failed |= verdict == "FAIL"
                print(f"{row:<34} {arm:<6} {verdict:<5} {detail}", flush=True)
        for listener in listeners:
            listener.close()
    print("FAIL" if failed else "OK: every client the agent builds verifies against the set")
    return 1 if failed else 0


# --- the certificates (on the host, with the openssl CLI) ---------------------------

_CONFIG = """\
[req]
distinguished_name = dn
prompt = no
[dn]
CN = {cn}
[ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[leaf]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = IP:127.0.0.1,DNS:localhost
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""


def make_certs(directory: Path) -> None:
    """``<name>-ca.pem``, ``<name>-chain.pem`` and ``<name>-key.pem`` for ``set``
    and ``outside``, world-readable (throwaway) so the image's user reads them
    through a bind mount. The CA keys are discarded."""

    def openssl(*args) -> None:
        subprocess.run(["openssl", *map(str, args)], check=True, capture_output=True)

    directory.mkdir(parents=True, exist_ok=True)
    ec = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes")
    with tempfile.TemporaryDirectory() as name:
        work = Path(name)
        for set_name in ("set", "outside"):
            ca, ca_key = directory / f"{set_name}-ca.pem", work / "ca-key.pem"
            ca_config, leaf_config = work / "ca.cnf", work / "leaf.cnf"
            ca_config.write_text(_CONFIG.format(cn=f"{set_name} probe CA"))
            leaf_config.write_text(_CONFIG.format(cn="localhost"))
            openssl("req", "-x509", "-config", ca_config, "-extensions", "ca", *ec,
                    "-keyout", ca_key, "-out", ca, "-days", "2")  # fmt: skip
            key, csr, leaf = directory / f"{set_name}-key.pem", work / "csr", work / "leaf"
            openssl("req", "-new", "-config", leaf_config, *ec, "-keyout", key, "-out", csr)
            openssl("x509", "-req", "-in", csr, "-CA", ca, "-CAkey", ca_key,
                    "-set_serial", secrets.randbits(63), "-days", "2", "-sha256",
                    "-extfile", leaf_config, "-extensions", "leaf", "-out", leaf)  # fmt: skip
            chain = leaf.read_bytes() + ca.read_bytes()
            (directory / f"{set_name}-chain.pem").write_bytes(chain)
    directory.chmod(0o755)
    for file in directory.iterdir():
        file.chmod(0o644)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--make-certs", type=Path, help="write the throwaway CAs here")
    group.add_argument("--certs", type=Path, help="run the check with these CAs")
    group.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("--arm", choices=ARMS, help=argparse.SUPPRESS)
    parser.add_argument("--ports", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return _child(args.child, args.arm, json.loads(args.ports))
    if args.make_certs:
        make_certs(args.make_certs)
        return 0
    return check(args.certs.resolve())


if __name__ == "__main__":
    sys.exit(main())
