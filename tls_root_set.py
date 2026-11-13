"""The one writer of the agent process's TLS root set (FaultMaven #2733, §13.4).

The set is the OS CA bundle, plus the certificates in ``TLS_EXTRA_CA_FILE`` when
set. The agent's clients read roots from places of their own: slack-sdk and
Socket Mode through OpenSSL's default paths, httpx through certifi unless
``SSL_CERT_FILE`` is set, libpq through ``PGSSLROOTCERT``. So ``apply_root_set``
names the set in every variable one reads (``OWNED_VARIABLES``) before any client
is built: it is called first in ``app.main()``, and in ``scripts/preflight.py``,
``scripts/push_manifest.py`` and ``scripts/generate_api_models.py``.

It also sets ``PGSSLMODE=verify-full`` unless the environment names a mode
(ruled 2026-11-13, FaultMaven #2733): libpq applies it only when neither the
URL nor a service file names ``sslmode`` (a service is refused here, and in the
URL by ``oauth_store``), so a database URL that names none verifies the chain
against the set and the host name, and an explicit choice is never lowered.

libpq also loads a CRL file or directory into its trust store, CA certificates
in it included: ``sslcrl``/``sslcrldir``, ``PGSSLCRL``/``PGSSLCRLDIR``, and by
default ``~/.postgresql/root.crl``. So ``PGSSLCRL`` names an empty file written
beside the set file (``empty.crl``, same directory and checks), and libpq never
reads the default; an inherited ``PGSSLCRL`` or ``PGSSLCRLDIR`` naming anything
else refuses the start, as ``sslcrl`` or ``sslcrldir`` in the URL does.

A ported copy of core's ``faultmaven/config/tls_root_set.py`` at ``b8e5ade7f``
(the agent does not depend on core). Deliberate differences:

- no Windows certificate store: a platform without ``os.getuid`` (Windows)
  refuses the start;
- no status surface (core's ``root_set_status``): ``apply_root_set`` logs the
  set's file and certificate count once at start instead;
- the setting is a parameter (``Settings.tls_extra_ca_file``), ``expanduser``
  is applied to it, and a relative path resolves against this repository, as
  ``config.py`` anchors its store paths;
- the resolved setting is exported by plain assignment to
  ``TLS_EXTRA_CA_FILE`` (core replaces every letter-case spelling);
- owned variables are read from the process environment only: one written in
  ``.env`` is neither seen nor refused (core loads ``.env`` into the
  environment first), and no client reads it either;
- ``PGSSLMODE`` defaults to ``verify-full`` (core keeps libpq's ``prefer``);
- the shipped empty capath is checked: a hashed entry in it refuses the start;
- the set directory is ``faultmaven-slack-tls-<uid>``.

A certificate is a PEM block labelled ``CERTIFICATE``. When a set file is
written (``TLS_EXTRA_CA_FILE`` set, or an OS bundle built from the capath) it is
the OS bundle, a newline, then the setting's certificates re-encoded through DER
(never its keys or other blocks), at ``<tmp>/faultmaven-slack-tls-<uid>/
<sha256>.pem``: one file per content, never deleted, and re-written by every
apply that finds it missing or altered. The set is read once per start.

An inherited owned file variable is accepted only when its real path is the
file this process names, or its bytes are exactly the bytes this process would
write; an inherited ``SSL_CERT_DIR`` only when no element holds a hashed entry.
Anything else refuses the start (``SystemExit``), as do an OS bundle that does
not load, a setting with no certificate or a non-plain one, no usable temporary
directory, a set file that cannot be written safely or does not load, a hashed
entry in the shipped empty capath, a platform without ``os.getuid``, an
inherited ``PGSERVICE`` of any value, empty included (a service's options,
``sslrootcert`` among them, apply before the environment's), and an inherited
``PGSSLCRL`` or ``PGSSLCRLDIR`` that is not the agent's empty file. A
``SLACK_DATABASE_URL`` whose conninfo names ``sslrootcert``, ``sslcrl``,
``sslcrldir`` or ``service`` is refused by ``oauth_store``.
Imports no client.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import ssl
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

logger = logging.getLogger("faultmaven.slack.tls")

#: The operator's one setting: a PEM file of CA certificates added to the OS bundle.
EXTRA_CA_FILE_SETTING = "TLS_EXTRA_CA_FILE"

#: Each names the set's file. ``SSL_CERT_FILE``: OpenSSL's default file (urllib,
#: so slack-sdk's WebClient and Socket Mode) and httpx. ``REQUESTS_CA_BUNDLE``,
#: then ``CURL_CA_BUNDLE``: requests. ``AWS_CA_BUNDLE``: botocore.
#: ``GRPC_DEFAULT_SSL_ROOTS_FILE_PATH``: gRPC. ``PGSSLROOTCERT``: libpq's
#: ``require`` and ``verify-*`` verify against it, and ``prefer`` and ``allow``
#: too, falling back to plaintext when that fails.
FILE_VARIABLES = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "AWS_CA_BUNDLE",
    "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH",
    "PGSSLROOTCERT",
)
#: Names a directory holding no certificate, so OpenSSL's default directory is
#: never a second root source.
DIRECTORY_VARIABLE = "SSL_CERT_DIR"
OWNED_VARIABLES = FILE_VARIABLES + (DIRECTORY_VARIABLE,)
#: libpq's default ``sslmode`` for a URL that names none; an inherited one is kept.
PG_SSLMODE_VARIABLE, PG_SSLMODE_DEFAULT = "PGSSLMODE", "verify-full"
#: Names a libpq service, whose options libpq applies before the environment's;
#: refused. ``PGSERVICEFILE`` and ``PGSYSCONFDIR`` alone name no service.
PG_SERVICE_VARIABLE = "PGSERVICE"
#: libpq loads these into its trust store, roots included; ``PGSSLCRL`` is
#: exported as the agent's empty file, so the ``~/.postgresql/root.crl``
#: default is never read.
PG_CRL_VARIABLES = ("PGSSLCRL", "PGSSLCRLDIR")
EMPTY_CRL_NAME = "empty.crl"

_REPO = Path(__file__).resolve().parent
#: Shipped with the agent; holds only a README.
EMPTY_CAPATH = str(_REPO / "tls_empty_capath")

#: Tried in order when OpenSSL's compiled default file does not load.
DISTRIBUTION_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/ssl/cert.pem",
)

_PEM_CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s.*?-----END CERTIFICATE-----", re.DOTALL
)
#: OpenSSL loads these as roots too, but their content cannot be compared.
_NON_PLAIN = re.compile(rb"-----BEGIN (?:X509|TRUSTED) CERTIFICATE-----")
#: OpenSSL's hashed-directory entry for a certificate (``.r0`` is a CRL).
_HASHED_ENTRY = re.compile(r"[0-9a-f]{8}\.\d+")


@dataclass(frozen=True)
class RootSet:
    """The set ``apply_root_set`` exported."""

    path: str
    sources: tuple[str, ...]
    extra_ca_file: str | None
    certificates: int


def apply_root_set(extra_ca_file: str | None) -> RootSet:
    """Name the one root set in every owned variable, or refuse the start.

    ``extra_ca_file`` is ``TLS_EXTRA_CA_FILE``; empty or whitespace is unset.
    """

    if not hasattr(os, "getuid"):
        raise SystemExit(
            "TLS root set: this platform is not supported (no os.getuid, so the "
            "set directory's owner cannot be checked; no Windows certificate store)."
        )
    if PG_SERVICE_VARIABLE in os.environ:
        raise SystemExit(
            f"TLS root set: {PG_SERVICE_VARIABLE}={os.environ[PG_SERVICE_VARIABLE]!r} "
            "names a libpq service, whose options (sslrootcert, sslmode) apply before "
            f"the root set's: unset {PG_SERVICE_VARIABLE}, name the connection in "
            f"SLACK_DATABASE_URL, and put its CA certificates in {EXTRA_CA_FILE_SETTING}."
        )
    if _hashed_entries(EMPTY_CAPATH):
        raise SystemExit(
            f"TLS root set: {EMPTY_CAPATH} must hold no certificate, and holds a "
            "hashed certificate entry: remove it."
        )
    os_source, pem, os_file = _resolve_os_bundle()
    extra = _resolve_setting(extra_ca_file)
    if extra is not None:
        # The newline: a bundle without a trailing one would merge its last
        # END line with the first BEGIN, and that set does not load.
        pem += b"\n" + _extra_certificates(extra)
        os.environ[EXTRA_CA_FILE_SETTING] = extra
    named = extra is None and os_file
    path = os_source if named else os.path.join(_set_directory(), _name(pem))
    _check_inherited(path, pem, extra)
    empty_crl = os.path.join(_set_directory(), EMPTY_CRL_NAME)
    _check_inherited_crl(empty_crl)
    if not named:  # written, or re-written if missing or altered (self-heal)
        _write_set(path, pem)
    _write_safely(empty_crl, b"")
    for name in FILE_VARIABLES:
        os.environ[name] = path
    os.environ[DIRECTORY_VARIABLE] = EMPTY_CAPATH
    os.environ.setdefault(PG_SSLMODE_VARIABLE, PG_SSLMODE_DEFAULT)
    os.environ[PG_CRL_VARIABLES[0]] = empty_crl
    sources = (os_source,) + ((extra,) if extra else ())
    root_set = RootSet(path, sources, extra, len(pem_digests(pem)))
    logger.info(
        "TLS clients verify against %s: %d certificates, %s %s",
        path,
        root_set.certificates,
        EXTRA_CA_FILE_SETTING,
        extra or "unset",
    )
    return root_set


def _resolve_setting(value: str | None) -> str | None:
    """The setting as an absolute real path; relative to this repository."""

    value = (value or "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = _REPO / path
    return os.path.realpath(path)


def _ders(pem: bytes) -> list[bytes]:
    ders = []
    for block in _PEM_CERTIFICATE.findall(pem):
        try:
            ders.append(ssl.PEM_cert_to_DER_cert(block.decode("ascii")))
        except (UnicodeDecodeError, ValueError):
            continue
    return ders


def pem_digests(pem: bytes) -> frozenset[str]:
    """The SHA-256 of each ``CERTIFICATE`` block's DER: order, comments and
    duplicates do not count."""

    return frozenset(hashlib.sha256(der).hexdigest() for der in _ders(pem))


def _loads(path: str) -> bool:
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cafile=path)
    except (OSError, ssl.SSLError):
        return False
    return True


def _read(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError:
        return b""


def _is_bytes(path: str, pem: bytes) -> bool:
    """Read only a regular file of ``pem``'s size: a FIFO or device is not read."""

    try:
        status = os.stat(path)
    except OSError:
        return False
    size_matches = stat.S_ISREG(status.st_mode) and status.st_size == len(pem)
    return size_matches and _read(path) == pem


def _hashed_entries(directory: str) -> list[str]:
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(name for name in names if _HASHED_ENTRY.fullmatch(name))


def _directory_name() -> str:
    return f"faultmaven-slack-tls-{os.getuid()}"


def _set_directory() -> str:
    try:
        return os.path.join(tempfile.gettempdir(), _directory_name())
    except OSError as error:  # no usable temporary directory
        raise _tmpdir_refusal(error) from error


def _tmpdir_refusal(error: OSError) -> SystemExit:
    return SystemExit(
        f"TLS root set: cannot write the set's file ({error}); "
        "TMPDIR must name a writable directory."
    )


def _name(pem: bytes) -> str:
    return hashlib.sha256(pem).hexdigest() + ".pem"


def _resolve_os_bundle() -> tuple[str, bytes, bool]:
    """(where the OS bundle came from, its PEM, whether that is a file to name).

    OpenSSL's default file if it loads, else the first distribution bundle that
    does, else the certificates in OpenSSL's default directory, joined.
    """

    paths = ssl.get_default_verify_paths()
    tried: list[str] = []
    for candidate in (paths.openssl_cafile, *DISTRIBUTION_BUNDLES):
        if not candidate or candidate in tried:
            continue
        tried.append(candidate)
        if _loads(candidate) and pem_digests(_read(candidate)):
            return candidate, _read(candidate), True
    capath = paths.openssl_capath
    entries = _hashed_entries(capath) if capath else []
    pem = b"\n".join(_read(os.path.join(capath, name)) for name in entries)
    if pem_digests(pem):
        return capath, pem, False
    tried.append(f"{capath} (directory)")
    advice = {
        "darwin": "Install the OS CA bundle (on a python.org build, run its "
        "'Install Certificates' step).",
    }.get(sys.platform, "Install the OS CA bundle.")
    raise SystemExit(
        "TLS root set: no operating-system CA bundle loads; tried "
        + ", ".join(tried)
        + f". {advice} {EXTRA_CA_FILE_SETTING} only adds to it."
    )


def _extra_certificates(path: str) -> bytes:
    """The setting's certificates, re-encoded one by one: nothing else is copied."""

    pem = _read(path)
    ders = _ders(pem)
    if not ders or _NON_PLAIN.search(pem):
        raise SystemExit(
            f"TLS root set: {EXTRA_CA_FILE_SETTING}={path!r} must be a readable PEM "
            "file of certificates labelled CERTIFICATE (an X509 or TRUSTED "
            "CERTIFICATE block cannot be compared)."
        )
    return "".join(ssl.DER_cert_to_PEM_cert(der) for der in ders).encode()


def _check_inherited(path: str, pem: bytes, extra: str | None) -> None:
    """Refuse an owned file variable already set to anything but ``path`` (by
    real path) or ``pem`` (by bytes), and an ``SSL_CERT_DIR`` holding a hashed
    entry. No inherited file's certificates are compared."""

    the_set = "the OS CA bundle" + (f" plus {EXTRA_CA_FILE_SETTING}" if extra else "")
    reason = f"is neither the set's file nor byte-identical to it ({the_set})"
    for name in FILE_VARIABLES:
        value = os.environ.get(name)
        if value and os.path.realpath(value) != os.path.realpath(path):
            if not _is_bytes(value, pem):
                _refuse(name, value, reason)
    # OpenSSL reads SSL_CERT_DIR as a list; an element it cannot list holds none,
    # and a dangling hashed link is an entry.
    directories = os.environ.get(DIRECTORY_VARIABLE, "")
    if any(
        _hashed_entries(element) for element in directories.split(os.pathsep) if element
    ):
        reason = "holds a hashed certificate entry, and the set names none"
        _refuse(DIRECTORY_VARIABLE, directories, reason)


def _check_inherited_crl(empty_crl: str) -> None:
    """Refuse an inherited CRL variable that is not the agent's empty file."""

    for name in PG_CRL_VARIABLES:
        value = os.environ.get(name)
        if value and os.path.realpath(value) != os.path.realpath(empty_crl):
            reason = "names a CRL source, which libpq loads into its trust store, roots included"
            _refuse(name, value, reason)


def _refuse(name: str, value: str, reason: str) -> NoReturn:
    parent = os.path.dirname(os.path.realpath(value))
    if os.path.basename(parent) == _directory_name():
        raise SystemExit(
            f"TLS root set: {name}={value!r} names a set file from an earlier "
            "agent start that is missing, altered or not this start's set: "
            f"restart that process, or unset {name}."
        )
    raise SystemExit(
        f"TLS root set: {name}={value!r} {reason}. The agent names the set in "
        f"{name} itself: unset {name}, or put its CA certificates in "
        f"{EXTRA_CA_FILE_SETTING}."
    )


def _write_set(path: str, pem: bytes) -> None:
    """Write the set once per content, then check that it loads."""

    _write_safely(path, pem)
    if not _loads(path):
        raise SystemExit(
            f"TLS root set: the set written to {path} does not load; check "
            f"{EXTRA_CA_FILE_SETTING} and the OS CA bundle."
        )


def _write_safely(path: str, data: bytes) -> None:
    """Write ``data`` unless ``path`` already holds it, in a directory only
    this user can write."""

    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        status = os.lstat(directory)
        if not stat.S_ISDIR(status.st_mode):
            raise OSError(f"{directory} is not a directory")
        if status.st_uid != os.getuid() or status.st_mode & 0o022:
            raise OSError(f"{directory} is not this user's alone")
        if not _is_bytes(path, data):
            handle, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
            with os.fdopen(handle, "wb") as file:
                file.write(data)
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
    except OSError as error:
        raise _tmpdir_refusal(error) from error
