"""What the image build cannot catch about its trust-store treatment (core #2633,
#2756; FaultMaven #2733).

CI builds the image on every PR, and its check step runs core's two scripts:
``check_image_trust_store.py`` fails the build if the OpenSSL or certifi stores,
any pip bundle or any pip wheel trust a banned root, or a pip bundle is not the
cafile; ``check_pip_root_set.py`` fails it unless a pip run ``--isolated`` or
without PIP_CERT verifies against the cafile. These tests pin what a passing
build does not prove:

- Every pip that runs during the build trusts only the OS store. pip 24.2+
  verifies through truststore, whose context also loads pip's vendored certifi
  bundle (an old one, holding the banned roots); PIP_CERT alone only adds to
  that set. So in every stage that runs pip the bundle is linked to the OS
  store, and PIP_CERT set, before its first pip.
- After the last pip and the source COPY, one step links every pip's vendored
  bundle to the OS store and deletes the ensurepip wheel, as written.
- The checks run, as written, on the stores the image ships. BuildKit skips a
  stage nothing references, so a deleted check RUN leaves a green build, and a
  check that is moved earlier, or whose command gains an ``|| true``, passes
  without proving anything. So the check RUN carries an exact command,
  bind-mounts both scripts from the trust-check stage, and is the final
  stage's last instruction that can change a store: only USER, EXPOSE,
  HEALTHCHECK, CMD, ENTRYPOINT and LABEL may follow it.
- Each script is core's, fetched at a 40-hex commit with a sha256 checksum, in
  a ``FROM scratch AS trust-check`` stage, and reaches the final stage only
  through its bind mount, so nothing of it ships.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"
OS_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
SCRIPTS = ("check_image_trust_store.py", "check_pip_root_set.py")
INERT = {"USER", "EXPOSE", "HEALTHCHECK", "CMD", "ENTRYPOINT", "LABEL"}
CHECK_COMMAND = "python {0} && python {1}"
LINK_COMMAND = (
    "find / -xdev \\( -path '*/pip/_vendor/certifi/cacert.pem'"
    f" -exec ln -sfv {OS_BUNDLE} {{}} \\; \\)"
    " -o \\( -path '*/ensurepip/_bundled/pip-*.whl' -print -delete \\)"
)

PIP = re.compile(r"(?<![\w.-])pip(?:\d+(?:\.\d+)*)?(?![\w.-])")  # pip, pip3.12, -m pip
HEREDOC_WORD = re.compile(r"<<(-?)([\"']?)(\w+)\2")


def _instructions(text: str) -> list[tuple[str, str]]:
    """Instructions in order: continuations joined (across blank lines, as Docker
    does), comments dropped, a RUN/COPY/ADD heredoc body kept with its instruction."""
    out: list[tuple[str, str]] = []
    lines = iter(text.splitlines())
    parts: list[str] = []
    for line in lines:
        if line.lstrip().startswith("#") or (parts and not line.strip()):
            continue
        body = line.rstrip()
        parts.append(body.removesuffix("\\").strip())
        if body.endswith("\\"):
            continue
        joined = " ".join(p for p in parts if p)
        parts = []
        if not joined:
            continue
        keyword, _, args = joined.partition(" ")
        if keyword.upper() in {"RUN", "COPY", "ADD"}:
            for word in args.split():
                heredoc = HEREDOC_WORD.fullmatch(word)
                if not heredoc:
                    continue
                for heredoc_line in lines:
                    end = heredoc_line.strip() if heredoc[1] else heredoc_line
                    if end == heredoc[3]:
                        break
                    args += "\n" + heredoc_line
        out.append((keyword.upper(), args.strip()))
    return out


def _stages() -> list[list[tuple[str, str]]]:
    stages: list[list[tuple[str, str]]] = []
    for keyword, args in _instructions(DOCKERFILE.read_text()):
        if keyword == "FROM":
            stages.append([])
        if stages:
            stages[-1].append((keyword, args))
    return stages


def _split(args: str) -> list[str]:
    try:
        return shlex.split(args)
    except ValueError as error:
        raise AssertionError(f"cannot tokenize {args!r}: {error}") from None


def _env_keys(args: str) -> set[str]:
    tokens = _split(args)
    if tokens and "=" not in tokens[0]:  # legacy `ENV KEY value`
        return {tokens[0]}
    return {token.split("=", 1)[0] for token in tokens}


def _flags_and_rest(args: str) -> tuple[list[str], list[str]]:
    words = args.split()
    n = next((i for i, w in enumerate(words) if not w.startswith("--")), len(words))
    return words[:n], words[n:]


def _bind_mounts(args: str) -> list[dict[str, str]]:
    mounts = [
        dict(option.split("=", 1) for option in flag[len("--mount=") :].split(",") if "=" in option)
        for flag in _flags_and_rest(args)[0]
        if flag.startswith("--mount=")
    ]
    return [m for m in mounts if m.get("type") == "bind"]


def _script_url(script: str) -> re.Pattern[str]:
    return re.compile(
        r"https://raw\.githubusercontent\.com/FaultMaven/faultmaven/[0-9a-f]{40}"
        rf"/scripts/{re.escape(script)}"
    )


def _fetch(script: str) -> tuple[str, str]:
    """The FROM line of the stage holding the script's fetch, and the ADD's arguments."""
    found = [
        (stage[0][1], args)
        for stage in _stages()
        for kw, args in stage
        if kw == "ADD" and any(_script_url(script).fullmatch(t) for t in _split(args))
    ]
    assert len(found) == 1, f"core's {script} must be fetched once, by commit"
    return found[0]


def test_every_pip_trusts_only_the_os_store() -> None:
    pip_stages = 0
    for stage in _stages():
        if stage[0][1].split()[0].lower() == "scratch":
            continue
        first = next((i for i, (kw, a) in enumerate(stage) if kw == "RUN" and PIP.search(a)), None)
        if first is None:
            continue
        pip_stages += 1
        before = stage[:first]
        assert any(
            kw == "RUN" and "pip._vendor.certifi" in a and OS_BUNDLE in a for kw, a in before
        ), f"FROM {stage[0][1]}: link pip's vendored bundle to {OS_BUNDLE} before the first pip"
        assert any(
            kw == "ENV" and "PIP_CERT" in _env_keys(a) for kw, a in before
        ), f"FROM {stage[0][1]}: set PIP_CERT before the first instruction that runs pip"
    assert pip_stages, "no stage runs pip"


@pytest.mark.parametrize("script", SCRIPTS)
def test_each_check_script_is_fetched_at_a_pinned_commit_by_digest(script) -> None:
    flags, _ = _flags_and_rest(_fetch(script)[1])
    assert any(re.fullmatch(r"--checksum=sha256:[0-9a-f]{64}", flag) for flag in flags), flags


@pytest.mark.parametrize("script", SCRIPTS)
def test_each_fetch_is_in_a_scratch_trust_check_stage(script) -> None:
    from_line, _ = _fetch(script)
    assert re.fullmatch(r"scratch\s+AS\s+trust-check", from_line, re.IGNORECASE), from_line


def test_every_pip_bundle_is_linked_and_the_ensurepip_wheel_deleted_after_the_last_pip() -> (
    None
):
    final = _stages()[-1]
    links = [i for i, (kw, a) in enumerate(final) if kw == "RUN" and a == " ".join(LINK_COMMAND.split())]
    assert len(links) == 1, "the final stage must run the link step once, as written"
    copies = [i for i, (kw, a) in enumerate(final) if kw == "COPY" and _split(a)[-2:] == [".", "."]]
    assert copies and links[0] > max(copies), "the link step must follow COPY . ."
    later = [a for kw, a in final[links[0] + 1 :] if kw == "RUN" and PIP.search(a)]
    assert not later, f"the link step must follow the last pip: {later}"


def test_the_checks_run_as_written_last_and_are_the_only_reference_to_the_scripts() -> None:
    final = _stages()[-1]
    last = max(i for i, (kw, _) in enumerate(final) if kw not in INERT)
    keyword, args = final[last]
    assert keyword == "RUN", f"only {sorted(INERT)} may follow the check RUN, not {keyword}"
    assert "USER" in [kw for kw, _ in final[last + 1 :]], "the final stage must drop root"
    targets = []
    for script in SCRIPTS:
        dest = _flags_and_rest(_fetch(script)[1])[1][-1]
        mounts = [
            m for m in _bind_mounts(args) if m.get("from") == "trust-check" and m.get("source") == dest
        ]
        assert len(mounts) == 1, f"the check RUN must bind-mount {script} from trust-check"
        targets.append(mounts[0]["target"])
    expected = CHECK_COMMAND.format(*targets)
    assert " ".join(_flags_and_rest(args)[1]) == " ".join(expected.split())
    others = [a for kw, a in final if (kw, a) != (keyword, args)]
    assert not [a for a in others if any(s in a for s in SCRIPTS) or "trust-check" in a], (
        "the final stage must reach the scripts only through the check's bind mounts"
    )
