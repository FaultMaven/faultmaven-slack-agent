"""What the image build cannot catch about its trust-store treatment (core #2633).

CI builds the image on every PR, and its check step fails the build if the
OpenSSL or certifi stores trust a banned root, or if the pip on PATH does not
read the file PIP_CERT names. These tests pin what a passing build does not
prove:

- Every pip trusts only the OS store. pip 24.2+ verifies through truststore,
  whose context also loads pip's vendored certifi bundle (an old one, holding
  the banned roots); PIP_CERT alone only adds to that set. The build asserts the
  final image's pip, not a pip that ran earlier, so in every stage that runs pip
  the bundle must be linked to the OS store, and PIP_CERT set, before its first
  pip.
- The check runs, as written, on the stores the image ships. BuildKit skips a
  stage nothing references, so a deleted check RUN leaves a green build, and a
  check that is moved earlier, or whose command gains an ``|| true``, passes
  without proving anything. So the check RUN carries an exact command, bind-mounts
  the script from the trust-check stage, and is the final stage's last
  instruction that can change a store: only USER, EXPOSE, HEALTHCHECK, CMD,
  ENTRYPOINT and LABEL may follow it.
- The script is core's, fetched at a 40-hex commit with a sha256 checksum, in a
  ``FROM scratch AS trust-check`` stage, and reaches the final stage only through
  that bind mount, so nothing of it ships.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"
OS_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
SCRIPT = "check_image_trust_store.py"
INERT = {"USER", "EXPOSE", "HEALTHCHECK", "CMD", "ENTRYPOINT", "LABEL"}
CHECK_COMMAND = (
    "python {target}"
    " && pip_python=\"$(sed -n '1s/^#!//p' \"$(command -v pip)\")\""
    " && \"$pip_python\" -c 'import os, sys, pip._vendor.certifi as c;"
    ' bundle, cert = (os.path.realpath(p) for p in (c.where(), os.environ["PIP_CERT"]));'
    ' bundle == cert or sys.exit(f"the pip on PATH reads {bundle}, not PIP_CERT ({cert})")\''
)

PIP = re.compile(r"(?<![\w.-])pip(?:\d+(?:\.\d+)*)?(?![\w.-])")  # pip, pip3.12, -m pip
SCRIPT_URL = re.compile(
    r"https://raw\.githubusercontent\.com/FaultMaven/faultmaven/[0-9a-f]{40}"
    rf"/scripts/{re.escape(SCRIPT)}"
)
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


def _fetch() -> tuple[str, str]:
    """The FROM line of the stage holding the script's fetch, and the ADD's arguments."""
    found = [
        (stage[0][1], args)
        for stage in _stages()
        for kw, args in stage
        if kw == "ADD" and any(SCRIPT_URL.fullmatch(t) for t in _split(args))
    ]
    assert len(found) == 1, f"core's {SCRIPT} must be fetched once, by commit"
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


def test_the_check_script_is_fetched_at_a_pinned_commit_by_digest() -> None:
    flags, _ = _flags_and_rest(_fetch()[1])
    assert any(re.fullmatch(r"--checksum=sha256:[0-9a-f]{64}", flag) for flag in flags), flags


def test_the_fetch_is_in_a_scratch_trust_check_stage() -> None:
    from_line, _ = _fetch()
    assert re.fullmatch(r"scratch\s+AS\s+trust-check", from_line, re.IGNORECASE), from_line


def test_the_check_runs_as_written_last_and_is_the_only_reference_to_the_script() -> None:
    final = _stages()[-1]
    last = max(i for i, (kw, _) in enumerate(final) if kw not in INERT)
    keyword, args = final[last]
    assert keyword == "RUN", f"only {sorted(INERT)} may follow the check RUN, not {keyword}"
    assert "USER" in [kw for kw, _ in final[last + 1 :]], "the final stage must drop root"
    dest = _flags_and_rest(_fetch()[1])[1][-1]
    mounts = [
        m for m in _bind_mounts(args) if m.get("from") == "trust-check" and m.get("source") == dest
    ]
    assert len(mounts) == 1, "the check RUN must bind-mount the script from trust-check"
    expected = CHECK_COMMAND.replace("{target}", mounts[0]["target"])
    assert " ".join(_flags_and_rest(args)[1]) == " ".join(expected.split())
    others = [a for kw, a in final if (kw, a) != (keyword, args)]
    assert not [a for a in others if SCRIPT in a or "trust-check" in a], (
        "the final stage must reach the script only through the check's bind mount"
    )
