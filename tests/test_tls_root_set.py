"""One TLS root set, extended by ``TLS_EXTRA_CA_FILE``, named in every owned
variable before any client is built (FaultMaven #2733).

Ported from core's ``tests/unit/config/test_tls_root_set_2692.py``, minus the
Windows store and the status surface. Each test builds an OS bundle of CA
certificates (made with the ``openssl`` CLI; the agent does not depend on
``cryptography``) and points the resolution at it. ``os.environ`` is restored
after each test, since ``apply_root_set`` writes it directly.
"""

from __future__ import annotations

import hashlib
import os
import ssl
import subprocess
import sys
import tempfile
from functools import cache
from pathlib import Path

import pytest

import config
import tls_root_set
from tls_root_set import (
    EMPTY_CAPATH,
    EXTRA_CA_FILE_SETTING,
    FILE_VARIABLES,
    OWNED_VARIABLES,
    apply_root_set,
)

ROOT = Path(tls_root_set.__file__).resolve().parent

_CONFIG = """\
[req]
distinguished_name = dn
prompt = no
[dn]
CN = {name}
[ext]
basicConstraints = critical,CA:{ca}
"""


@cache
def _ca_pem(name: str, ca: bool = True) -> bytes:
    """A self-signed certificate, made once per name."""

    with tempfile.TemporaryDirectory() as work:
        cnf = Path(work, "cnf")
        cnf.write_text(_CONFIG.format(name=name, ca="TRUE" if ca else "FALSE"))
        made = subprocess.run(
            ["openssl", "req", "-x509", "-config", str(cnf), "-extensions", "ext",
             "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
             "-keyout", str(Path(work, "key")), "-days", "1"],
            check=True, capture_output=True,
        )  # fmt: skip
    return made.stdout


def _digest(pem: bytes) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem.decode())).hexdigest()


def _loaded(path: str) -> set[str]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=path)
    return {hashlib.sha256(der).hexdigest() for der in context.get_ca_certs(True)}


def _roots() -> list[bytes]:
    return [_ca_pem(f"os root {index}") for index in range(3)]


def _extra() -> bytes:
    return _ca_pem("private CA")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No inherited owned variable or setting; the set's files land under
    tmp_path; ``os.environ`` and the settings cache restored after."""

    saved = dict(os.environ)
    for name in (
        *OWNED_VARIABLES, EXTRA_CA_FILE_SETTING, "PGSSLMODE", "PGSERVICE", "PGSSLCRL",
        "PGSSLCRLDIR",
    ):  # fmt: skip
        os.environ.pop(name, None)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    config.get_settings.cache_clear()
    yield
    os.environ.clear()
    os.environ.update(saved)
    config.get_settings.cache_clear()


def _os_paths(monkeypatch, cafile: str, capath: str = "/nonexistent-capath") -> None:
    paths = ssl.DefaultVerifyPaths(
        cafile, capath, "SSL_CERT_FILE", cafile, "SSL_CERT_DIR", capath
    )
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: paths)
    monkeypatch.setattr(tls_root_set, "DISTRIBUTION_BUNDLES", ())


@pytest.fixture
def os_bundle(tmp_path, monkeypatch) -> Path:
    bundle = tmp_path / "os-bundle.pem"
    bundle.write_bytes(b"".join(_roots()))
    _os_paths(monkeypatch, str(bundle))
    return bundle


@pytest.fixture
def extra_file(tmp_path) -> Path:
    path = tmp_path / "extra-ca.pem"
    path.write_bytes(_extra())
    return path


def _set_directory(tmp_path: Path) -> Path:
    return tmp_path / f"faultmaven-slack-tls-{os.getuid()}"


# --- refusals ---------------------------------------------------------------


@pytest.mark.parametrize("content", [None, b"not a certificate\n"])
def test_an_unreadable_extra_file_refuses_naming_the_setting(os_bundle, tmp_path, content):
    path = tmp_path / "extra.pem"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(SystemExit, match="TLS_EXTRA_CA_FILE=.*must be a readable PEM"):
        apply_root_set(str(path))
    assert "SSL_CERT_FILE" not in os.environ


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_no_loadable_os_bundle_refuses_naming_each_path_tried(
    tmp_path, monkeypatch, platform
):
    empty_capath = tmp_path / "capath"
    empty_capath.mkdir()
    _os_paths(monkeypatch, str(tmp_path / "absent.pem"), str(empty_capath))
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(SystemExit) as refusal:
        apply_root_set(None)
    message = str(refusal.value)
    assert str(tmp_path / "absent.pem") in message and str(empty_capath) in message
    assert ("Install Certificates" in message) == (platform == "darwin")
    assert "TLS_EXTRA_CA_FILE" in message


def _non_ascii(pem: bytes) -> bytes:
    """One non-ASCII byte on a base64 line: the digests skip the block, and
    OpenSSL still loads it."""

    lines = pem.split(b"\n")
    lines[1] += b"\xc3\xa9"
    return b"\n".join(lines)


def test_an_inherited_file_hiding_a_root_openssl_loads_refuses(os_bundle, tmp_path):
    tainted = tmp_path / "tainted.pem"
    tainted.write_bytes(b"".join(_roots()) + _non_ascii(_ca_pem("rogue")))
    os.environ["SSL_CERT_FILE"] = str(tainted)
    with pytest.raises(SystemExit, match="SSL_CERT_FILE="):
        apply_root_set(None)


def test_a_capath_entry_hiding_a_root_openssl_loads_refuses(os_bundle, tmp_path):
    capath = tmp_path / "capath"
    capath.mkdir()
    (capath / "0badc0de.0").write_bytes(_non_ascii(_ca_pem("rogue")))
    os.environ["SSL_CERT_DIR"] = str(capath)
    with pytest.raises(SystemExit, match="SSL_CERT_DIR="):
        apply_root_set(None)


@pytest.mark.parametrize("name", FILE_VARIABLES)
def test_an_inherited_variable_holding_another_root_refuses(os_bundle, tmp_path, name):
    other = tmp_path / "other.pem"
    other.write_bytes(_ca_pem("someone else's root"))
    os.environ[name] = str(other)
    with pytest.raises(SystemExit) as refusal:
        apply_root_set(None)
    message = str(refusal.value)
    assert f"{name}=" in message and "TLS_EXTRA_CA_FILE" in message
    assert f"unset {name}" in message and "byte-identical" in message


def test_an_inherited_file_one_certificate_short_refuses(os_bundle, tmp_path):
    short = tmp_path / "short.pem"
    short.write_bytes(b"".join(_roots()[:-1]))
    os.environ["SSL_CERT_FILE"] = str(short)
    with pytest.raises(SystemExit, match="SSL_CERT_FILE="):
        apply_root_set(None)


def test_an_inherited_os_bundle_refuses_once_the_extra_ca_is_set(os_bundle, extra_file):
    os.environ["PGSSLROOTCERT"] = str(os_bundle)
    with pytest.raises(SystemExit, match="plus TLS_EXTRA_CA_FILE"):
        apply_root_set(str(extra_file))


def _relabel(pem: bytes, label: bytes) -> bytes:
    return pem.replace(b" CERTIFICATE-----", b" " + label + b"-----")


@pytest.mark.parametrize("label", [b"X509 CERTIFICATE", b"TRUSTED CERTIFICATE"])
def test_a_root_in_a_non_plain_block_refuses_inherited_or_extra(
    os_bundle, tmp_path, label
):
    """OpenSSL loads such a block as a root, but it cannot be compared."""

    hidden = tmp_path / "hidden.pem"
    hidden.write_bytes(b"".join(_roots()) + _relabel(_ca_pem("rogue"), label))
    os.environ["SSL_CERT_FILE"] = str(hidden)
    with pytest.raises(SystemExit, match="SSL_CERT_FILE="):
        apply_root_set(None)
    del os.environ["SSL_CERT_FILE"]
    with pytest.raises(SystemExit, match="TLS_EXTRA_CA_FILE=.*CERTIFICATE"):
        apply_root_set(str(hidden))


def test_a_stale_set_an_earlier_start_wrote_says_restart(os_bundle, extra_file):
    apply_root_set(str(extra_file))
    extra_file.write_bytes(_ca_pem("rotated private CA"))
    with pytest.raises(SystemExit) as refusal:
        apply_root_set(str(extra_file))
    assert "restart that process" in str(refusal.value)


@pytest.mark.parametrize("unsafe", ["group-writable", "symlink"])
def test_a_set_directory_others_can_write_refuses_naming_tmpdir(
    os_bundle, extra_file, tmp_path, unsafe
):
    directory = _set_directory(tmp_path)
    if unsafe == "symlink":
        (tmp_path / "elsewhere").mkdir()
        directory.symlink_to(tmp_path / "elsewhere")
    else:
        directory.mkdir()
        directory.chmod(0o770)
    with pytest.raises(SystemExit, match="TMPDIR"):
        apply_root_set(str(extra_file))


def test_a_set_directory_another_user_owns_refuses(os_bundle, extra_file, monkeypatch):
    """The directory is the real user's; this process claims another uid."""

    other = os.getuid() + 1
    directory = Path(tempfile.gettempdir()) / f"faultmaven-slack-tls-{other}"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(os, "getuid", lambda: other)
    with pytest.raises(SystemExit, match="not this user's alone.*TMPDIR"):
        apply_root_set(str(extra_file))


def test_no_usable_temporary_directory_refuses_naming_tmpdir(
    os_bundle, extra_file, monkeypatch
):
    def none_usable():
        raise FileNotFoundError("No usable temporary directory found")

    monkeypatch.setattr(tempfile, "gettempdir", none_usable)
    with pytest.raises(SystemExit, match="TMPDIR must name a writable directory"):
        apply_root_set(str(extra_file))


def test_an_extra_block_that_decodes_but_is_not_a_certificate_refuses_at_the_load(
    os_bundle, tmp_path
):
    """Its base64 decodes, so it is written; OpenSSL cannot load the set."""

    junk = tmp_path / "junk.pem"
    junk.write_bytes(b"-----BEGIN CERTIFICATE-----\nAAAAAAAA\n-----END CERTIFICATE-----\n")
    with pytest.raises(SystemExit, match="the set written to .* does not load"):
        apply_root_set(str(junk))


def test_an_inherited_file_with_a_malformed_certificate_block_refuses(os_bundle, tmp_path):
    """Its good blocks match the set, but OpenSSL refuses the file whole."""

    broken = tmp_path / "broken.pem"
    bad = b"-----BEGIN CERTIFICATE-----\nMIIBabc\n-----END CERTIFICATE-----\n"
    broken.write_bytes(b"".join(_roots()) + bad)
    os.environ["REQUESTS_CA_BUNDLE"] = str(broken)
    with pytest.raises(SystemExit, match="REQUESTS_CA_BUNDLE="):
        apply_root_set(None)


@pytest.mark.parametrize("other_bytes", ["reordered", "commented"])
def test_an_inherited_file_with_the_same_roots_in_other_bytes_refuses(
    os_bundle, tmp_path, other_bytes
):
    """No inherited file's certificates are compared, so a Nix or conda bundle
    with the same roots refuses."""

    nix = tmp_path / "nix-bundle.pem"
    if other_bytes == "reordered":
        nix.write_bytes(b"".join(reversed(_roots())))
    else:
        nix.write_bytes(b"# a comment\n" + b"".join(_roots()))
    os.environ["SSL_CERT_FILE"] = str(nix)
    with pytest.raises(SystemExit, match="byte-identical"):
        apply_root_set(None)


@pytest.mark.parametrize("held", ["empty", "the-set", "dangling"])
def test_an_inherited_capath_with_any_hashed_entry_refuses_and_an_empty_one_is_accepted(
    os_bundle, tmp_path, held
):
    """Even exactly the set's roots, and even a dangling hashed link."""

    capath = tmp_path / "capath"
    capath.mkdir()
    if held == "the-set":
        for index, pem in enumerate(_roots()):
            (capath / f"abcdef0{index}.0").write_bytes(pem)
    elif held == "dangling":
        (capath / "abcdef00.0").symlink_to(tmp_path / "gone.pem")
    os.environ["SSL_CERT_DIR"] = str(capath)
    if held == "empty":
        apply_root_set(None)
        assert os.environ["SSL_CERT_DIR"] == EMPTY_CAPATH
    else:
        with pytest.raises(SystemExit, match="SSL_CERT_DIR=.*hashed certificate entry"):
            apply_root_set(None)


@pytest.mark.parametrize("rogue", [True, False])
def test_an_inherited_capath_list_is_read_element_by_element(os_bundle, tmp_path, rogue):
    empty, second = tmp_path / "empty", tmp_path / "second"
    empty.mkdir()
    second.mkdir()
    if rogue:
        (second / "0badc0de.0").write_bytes(_ca_pem("rogue capath CA"))
    os.environ["SSL_CERT_DIR"] = f"{empty}{os.pathsep}{second}"
    if rogue:
        with pytest.raises(SystemExit, match="SSL_CERT_DIR="):
            apply_root_set(None)
    else:
        apply_root_set(None)
        assert os.environ["SSL_CERT_DIR"] == EMPTY_CAPATH


@pytest.mark.parametrize("kind", ["device", "fifo"])
def test_an_inherited_device_or_fifo_refuses_without_reading_it(tmp_path, kind):
    """In a capped child with a timeout: reading /dev/zero never ends, and a
    FIFO with no writer blocks, so a regression fails here, not the runner."""

    import resource

    target = "/dev/zero"
    if kind == "fifo":
        target = str(tmp_path / "fifo")
        os.mkfifo(target)

    def cap() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))

    child = subprocess.run(
        [sys.executable, "-c", "from tls_root_set import apply_root_set; apply_root_set(None)"],
        capture_output=True, text=True, timeout=60, preexec_fn=cap,
        env={**os.environ, "PYTHONPATH": str(ROOT), "TMPDIR": str(tmp_path),
             "SSL_CERT_FILE": target},
    )  # fmt: skip
    assert child.returncode == 1, child.stderr[-500:]
    assert f"SSL_CERT_FILE={target!r} is neither the set's file" in child.stderr


def test_a_hashed_entry_in_the_shipped_empty_capath_refuses(os_bundle, tmp_path, monkeypatch):
    capath = tmp_path / "shipped-capath"
    capath.mkdir()
    (capath / "README").write_text("holds no certificate")
    monkeypatch.setattr(tls_root_set, "EMPTY_CAPATH", str(capath))
    apply_root_set(None)
    (capath / "0badc0de.0").write_bytes(_ca_pem("planted root"))
    with pytest.raises(SystemExit, match="shipped-capath must hold no certificate"):
        apply_root_set(None)


def test_an_inherited_pgservice_refuses_and_a_service_file_alone_does_not(
    os_bundle, tmp_path
):
    """A service's options, sslrootcert among them, apply before the
    environment's; PGSERVICEFILE alone names no service."""

    os.environ["PGSERVICEFILE"] = str(tmp_path / "pg_service.conf")
    apply_root_set(None)
    for value in ("probe", ""):  # an empty name selects a "[]" section
        os.environ["PGSERVICE"] = value
        with pytest.raises(SystemExit, match=f"PGSERVICE={value!r}.*TLS_EXTRA_CA_FILE"):
            apply_root_set(None)


def test_a_platform_without_getuid_refuses_the_start(os_bundle, monkeypatch):
    """Windows: no certificate store support, and no owner check."""

    monkeypatch.delattr(os, "getuid")
    with pytest.raises(SystemExit, match="this platform is not supported"):
        apply_root_set(None)


# --- the OS bundle's resolution ----------------------------------------------


def test_a_distribution_bundle_is_used_when_openssls_default_file_is_absent(
    tmp_path, monkeypatch
):
    bundle = tmp_path / "distribution.pem"
    bundle.write_bytes(b"".join(_roots()))
    _os_paths(monkeypatch, str(tmp_path / "absent.pem"))
    monkeypatch.setattr(tls_root_set, "DISTRIBUTION_BUNDLES", (str(bundle),))
    assert apply_root_set(None).path == str(bundle)


def test_a_capath_only_os_bundle_is_built_into_a_file(tmp_path, monkeypatch):
    capath = tmp_path / "capath"
    capath.mkdir()
    for index, pem in enumerate(_roots()):
        (capath / f"0000000{index}.0").write_bytes(pem)
    (capath / "README").write_text("not a certificate")
    _os_paths(monkeypatch, str(tmp_path / "absent.pem"), str(capath))
    applied = apply_root_set(None)
    assert Path(applied.path).parent == _set_directory(tmp_path)
    assert _loaded(applied.path) == {_digest(pem) for pem in _roots()}


# --- the set -----------------------------------------------------------------


@pytest.mark.parametrize("unset", [None, "", "  "])
def test_unset_names_the_os_bundle_in_every_variable_and_writes_only_the_empty_crl(
    os_bundle, tmp_path, unset
):
    applied = apply_root_set(unset)
    assert applied.path == str(os_bundle) and applied.certificates == len(_roots())
    assert {name: os.environ[name] for name in FILE_VARIABLES} == dict.fromkeys(
        FILE_VARIABLES, str(os_bundle)
    )
    assert os.environ["SSL_CERT_DIR"] == EMPTY_CAPATH
    assert os.listdir(EMPTY_CAPATH) == ["README"]
    assert os.listdir(_set_directory(tmp_path)) == ["empty.crl"]
    assert EXTRA_CA_FILE_SETTING not in os.environ


def test_set_names_a_file_of_the_os_bundle_a_newline_and_the_extra_ca(
    os_bundle, extra_file, tmp_path
):
    applied = apply_root_set(str(extra_file))
    der = ssl.PEM_cert_to_DER_cert(_extra().decode())
    assert Path(applied.path).read_bytes() == (
        os_bundle.read_bytes() + b"\n" + ssl.DER_cert_to_PEM_cert(der).encode()
    )
    assert Path(applied.path).parent == _set_directory(tmp_path)
    assert _set_directory(tmp_path).stat().st_mode & 0o777 == 0o700
    expected = {_digest(pem) for pem in (*_roots(), _extra())}
    assert _loaded(applied.path) == expected and applied.certificates == len(expected)
    assert all(os.environ[name] == applied.path for name in FILE_VARIABLES)
    assert applied.extra_ca_file == os.environ[EXTRA_CA_FILE_SETTING] == str(extra_file)


def test_an_os_bundle_without_a_trailing_newline_still_loads_whole(os_bundle, extra_file):
    os_bundle.write_bytes(b"".join(_roots()).rstrip(b"\n"))
    assert len(_loaded(apply_root_set(str(extra_file)).path)) == len(_roots()) + 1


def test_a_real_path_alias_or_a_byte_identical_copy_is_accepted_and_replaced(
    os_bundle, tmp_path
):
    """Accepted, and every variable then names this process's own file."""

    link = tmp_path / "link.pem"
    link.symlink_to(os_bundle)
    copy = tmp_path / "copy.pem"
    copy.write_bytes(os_bundle.read_bytes())
    os.environ["SSL_CERT_FILE"] = str(link)
    os.environ["PGSSLROOTCERT"] = str(copy)
    assert apply_root_set(None).path == str(os_bundle)
    assert {os.environ[name] for name in FILE_VARIABLES} == {str(os_bundle)}


def test_an_inherited_relative_path_to_the_set_is_replaced_by_its_own_path(
    os_bundle, monkeypatch
):
    monkeypatch.chdir(os_bundle.parent)
    os.environ["SSL_CERT_FILE"] = os_bundle.name
    apply_root_set(None)
    assert os.environ["SSL_CERT_FILE"] == str(os_bundle)


def test_an_extra_file_keeps_its_certificates_and_drops_everything_else(
    os_bundle, tmp_path
):
    """A fullchain with its key: the non-CA leaf loads, the key is never copied."""

    leaf = _ca_pem("a server leaf", ca=False)
    key = b"-----BEGIN PRIVATE KEY-----\nTUlJ\n-----END PRIVATE KEY-----\n"
    chain = tmp_path / "fullchain.pem"
    chain.write_bytes(leaf + _extra() + key)
    written = Path(apply_root_set(str(chain)).path).read_bytes()
    assert b"PRIVATE KEY" not in written
    assert tls_root_set.pem_digests(written) == {
        _digest(pem) for pem in (*_roots(), leaf, _extra())
    }


def test_a_relative_extra_file_resolves_against_the_repo_not_the_cwd(
    os_bundle, tmp_path, monkeypatch
):
    """As config.py anchors CASE_STORE_PATH and CREDENTIAL_STORE_PATH."""

    monkeypatch.setattr(tls_root_set, "_REPO", tmp_path)
    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / "extra-ca.pem").write_bytes(_extra())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    applied = apply_root_set("certs/extra-ca.pem")
    assert applied.extra_ca_file == str(tmp_path / "certs" / "extra-ca.pem")
    assert _digest(_extra()) in _loaded(applied.path)


def test_two_applies_name_one_file_and_write_nothing_else(os_bundle, extra_file):
    first = apply_root_set(str(extra_file))
    for name in (*OWNED_VARIABLES, "PGSSLCRL"):  # a second start, not a child
        os.environ.pop(name)
    second = apply_root_set(str(extra_file))
    assert second.path == first.path
    assert sorted(os.listdir(Path(first.path).parent)) == sorted(
        [Path(first.path).name, "empty.crl"]
    )


def test_pgsslcrl_names_an_empty_file_beside_the_set(os_bundle, extra_file):
    """libpq loads a CRL source into its trust store, roots included; naming an
    empty one keeps it from reading ~/.postgresql/root.crl."""

    applied = apply_root_set(str(extra_file))
    crl = Path(os.environ["PGSSLCRL"])
    assert crl.parent == Path(applied.path).parent and crl.name == "empty.crl"
    assert crl.is_file() and crl.read_bytes() == b""
    crl.write_bytes(b"planted")  # re-written, as the set file is
    for name in (*OWNED_VARIABLES, "PGSSLCRL"):
        os.environ.pop(name)
    apply_root_set(str(extra_file))
    assert crl.read_bytes() == b""


@pytest.mark.parametrize("name", ["PGSSLCRL", "PGSSLCRLDIR"])
def test_an_inherited_crl_source_refuses_unless_it_is_the_agents_empty_file(
    os_bundle, tmp_path, name
):
    apply_root_set(None)
    own = os.environ.pop("PGSSLCRL")
    os.environ[name] = own  # the agent's own empty file: accepted
    apply_root_set(None)
    os.environ.pop("PGSSLCRL", None)
    other = tmp_path / "ca-plus-crl.pem"
    other.write_bytes(_ca_pem("outside CA"))
    os.environ[name] = str(other)
    with pytest.raises(SystemExit, match=f"{name}=.*CRL source.*TLS_EXTRA_CA_FILE"):
        apply_root_set(None)


@pytest.mark.parametrize("damage", ["deleted", "altered"])
def test_a_missing_or_altered_set_file_is_rewritten_and_the_start_goes_ahead(
    os_bundle, extra_file, damage
):
    """A host that ages /tmp can remove it under an idle process (self-heal)."""

    first = apply_root_set(str(extra_file))
    if damage == "deleted":
        os.remove(first.path)
    else:
        Path(first.path).write_bytes(b"garbage")
    assert apply_root_set(str(extra_file)).path == first.path
    assert _loaded(first.path) == {_digest(pem) for pem in (*_roots(), _extra())}


@pytest.mark.parametrize("inherited", [None, "disable", "prefer"])
def test_pgsslmode_defaults_to_verify_full_and_an_inherited_one_is_kept(
    os_bundle, inherited
):
    """Ruled 2026-11-13: a database URL naming no sslmode verifies the chain
    and the host name. libpq applies PGSSLMODE only when neither the URL nor a
    service file names one, so an explicit choice is never lowered."""

    if inherited:
        os.environ["PGSSLMODE"] = inherited
    apply_root_set(None)
    assert os.environ["PGSSLMODE"] == (inherited or "verify-full")


# --- the setting and the launchers -------------------------------------------


def test_settings_read_tls_extra_ca_file_from_the_environment_or_dotenv(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-x")
    monkeypatch.chdir(tmp_path)
    assert config.Settings(_env_file=None).tls_extra_ca_file is None
    (tmp_path / ".env").write_text("tls_extra_ca_file=/etc/private-ca.pem\n")
    assert config.Settings().tls_extra_ca_file == "/etc/private-ca.pem"


@pytest.mark.parametrize("transport", ["socket", "http"])
def test_app_main_applies_the_root_set_before_building_any_client(
    monkeypatch, transport
):
    """Both transports dispatch from main(); the set comes first in each."""

    import app
    import web

    calls: list[str] = []

    class Built(Exception):
        pass

    def built(name: str):
        def record(*args, **kwargs):
            calls.append(name)
            raise Built

        return record

    settings = type(
        "S", (), {"log_level": "INFO", "slack_transport": transport,
                  "tls_extra_ca_file": "/etc/private-ca.pem"},
    )()  # fmt: skip
    monkeypatch.setattr(app, "get_settings", lambda: settings)
    monkeypatch.setattr(app, "apply_root_set", lambda extra: calls.append(f"set {extra}"))
    monkeypatch.setattr(app, "build_app", built("build_app"))
    monkeypatch.setattr(web, "run_http", built("run_http"))
    with pytest.raises(Built):
        app.main()
    client = "run_http" if transport == "http" else "build_app"
    assert calls == ["set /etc/private-ca.pem", client]


def _preflight():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_preflight", "scripts/preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("refused", [False, True])
def test_preflight_applies_the_root_set_after_config_and_before_any_client(
    monkeypatch, capsys, refused
):
    """A refusal is a failed check and a non-zero exit, not a bare SystemExit."""

    preflight = _preflight()
    calls: list[str] = []
    settings = type(
        "S", (), {"slack_transport": "socket", "tls_extra_ca_file": "/etc/private-ca.pem"}
    )()

    def apply(extra):
        calls.append(f"set {extra}")
        if refused:
            raise SystemExit("TLS root set: SSL_CERT_FILE='/x' is neither the set's file")
        return tls_root_set.RootSet("/the/set.pem", (), extra, 3)

    def client_check(name, result=True):
        return lambda *args, **kwargs: calls.append(name) or result

    monkeypatch.setattr("sys.argv", ["preflight.py"])
    monkeypatch.setattr(preflight, "check_config", lambda: (calls.append("config"), (True, settings))[1])
    monkeypatch.setattr(preflight, "apply_root_set", apply)
    for name in ("check_slack_bot", "check_slack_app", "check_backend", "check_backend_auth"):
        monkeypatch.setattr(preflight, name, client_check(name))
    fm = type("FM", (), {"close": lambda self: None})()
    monkeypatch.setattr(preflight, "make_fault_client", lambda *a: calls.append("client") or fm)

    code = preflight.main()
    out = capsys.readouterr().out
    if refused:
        assert code == 1 and calls == ["config", "set /etc/private-ca.pem"]
        assert "✗" in out and "neither the set's file" in out
    else:
        assert code == 0
        assert calls[:3] == ["config", "set /etc/private-ca.pem", "check_slack_bot"]
        assert "/the/set.pem" in out


def test_generate_api_models_applies_the_set_from_dotenv_before_the_fetch(
    tmp_path, monkeypatch
):
    """The contract fetch is stubbed: no network."""

    import scripts.generate_api_models as generate

    (tmp_path / ".env").write_text("TLS_EXTRA_CA_FILE=/etc/private-ca.pem\n")
    calls: list[str] = []
    monkeypatch.setattr(generate, "REPO_ROOT", tmp_path)  # its .env is read
    monkeypatch.setattr(generate, "OUTPUT_PATH", tmp_path / "api_generated.py")
    monkeypatch.setattr(tls_root_set, "apply_root_set", lambda extra: calls.append(f"set {extra}"))
    monkeypatch.setattr(generate, "local_copy", lambda spec, *a, **k: calls.append("fetch") or "x")
    monkeypatch.setattr(generate, "generate", lambda path: calls.append("generate") or 0)
    monkeypatch.delenv("FM_OPENAPI_SPEC", raising=False)
    monkeypatch.setattr("sys.argv", ["generate_api_models.py"])
    assert generate.main() == 0
    assert calls == ["set /etc/private-ca.pem", "fetch", "generate"]
