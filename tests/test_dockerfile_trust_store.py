"""What the image build cannot catch about its trust-store treatment (core #2633).

CI builds the image on every PR, and the build runs core's trust-store check, so
the build is the regression for the OpenSSL and certifi stores. These tests pin
what a passing build does not prove:

- pip trusts only the OS store. pip 24.2+ verifies through truststore, whose
  context also loads pip's vendored certifi bundle (an old one, holding the
  banned roots), and the check does not read that bundle. So the bundle must be
  linked to the OS store, and PIP_CERT name it, before the final stage's first
  pip. Without the link, PIP_CERT only adds to the banned set.
- The check runs. BuildKit skips a stage nothing references, so a check RUN that
  is deleted, or moved before the stores are final, still leaves a green build.
  It must be the final stage's last root instruction, bind-mounting the script
  from the trust-check stage.
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

PIP = re.compile(r"(?<![\w.-])pip(?:\d+(?:\.\d+)*)?(?![\w.-])")  # pip, pip3.12, -m pip
LINKS_PIP_BUNDLE = re.compile(rf"\bln\s+-\w*s\w*\s+{re.escape(OS_BUNDLE)}(?=\s|$)")
SCRIPT_URL = re.compile(
    r"https://raw\.githubusercontent\.com/FaultMaven/faultmaven/[0-9a-f]{40}"
    rf"/scripts/{re.escape(SCRIPT)}"
)
HEREDOC = re.compile(r"<<(-?)[\"']?(\w+)[\"']?")


def _instructions(text: str) -> list[tuple[str, str]]:
    """Instructions in order: continuations joined (across blank lines, as Docker
    does), comments dropped, each heredoc body kept with its instruction."""
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
        for strip, delimiter in HEREDOC.findall(joined):
            for heredoc_line in lines:
                if (heredoc_line.strip() if strip else heredoc_line) == delimiter:
                    break
                joined += "\n" + heredoc_line
        if joined:
            keyword, _, args = joined.partition(" ")
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


def _env_keys(args: str) -> set[str]:
    tokens = shlex.split(args)
    if tokens and "=" not in tokens[0]:  # legacy `ENV KEY value`
        return {tokens[0]}
    return {token.split("=", 1)[0] for token in tokens}


def _flags_and_rest(args: str) -> tuple[list[str], list[str]]:
    tokens = shlex.split(args)
    n = next((i for i, t in enumerate(tokens) if not t.startswith("--")), len(tokens))
    return tokens[:n], tokens[n:]


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
        if kw == "ADD" and any(SCRIPT_URL.fullmatch(t) for t in shlex.split(args))
    ]
    assert len(found) == 1, f"core's {SCRIPT} must be fetched once, by commit"
    return found[0]


def test_pip_trusts_only_the_os_store_before_the_first_pip() -> None:
    final = _stages()[-1]
    first_pip = next((i for i, (kw, a) in enumerate(final) if kw == "RUN" and PIP.search(a)), None)
    assert first_pip is not None, "no instruction in the final stage runs pip"
    before = final[:first_pip]
    assert any(
        kw == "RUN" and "pip._vendor.certifi" in a and LINKS_PIP_BUNDLE.search(a)
        for kw, a in before
    ), f"pip's vendored bundle must be linked to {OS_BUNDLE} before the first pip"
    assert any(
        kw == "ENV" and "PIP_CERT" in _env_keys(a) for kw, a in before
    ), "PIP_CERT must be set before the first instruction that runs pip"


def test_the_check_script_is_fetched_at_a_pinned_commit_by_digest() -> None:
    flags, _ = _flags_and_rest(_fetch()[1])
    assert any(re.fullmatch(r"--checksum=sha256:[0-9a-f]{64}", flag) for flag in flags), flags


def test_the_fetch_is_in_a_scratch_trust_check_stage() -> None:
    from_line, _ = _fetch()
    assert re.fullmatch(r"scratch\s+AS\s+trust-check", from_line, re.IGNORECASE), from_line


def test_the_check_is_the_last_root_step_and_the_only_reference_to_the_script() -> None:
    final = _stages()[-1]
    users = [i for i, (kw, _) in enumerate(final) if kw == "USER"]
    assert users and users[-1] > 0, "the final stage must drop root with USER"
    keyword, args = final[users[-1] - 1]
    assert keyword == "RUN", "the instruction before USER must be the check RUN"
    dest = _flags_and_rest(_fetch()[1])[1][-1]
    mounts = [
        m for m in _bind_mounts(args) if m.get("from") == "trust-check" and m.get("source") == dest
    ]
    assert len(mounts) == 1, "the check RUN must bind-mount the script from trust-check"
    command = _flags_and_rest(args)[1]
    assert command[:2] == ["python", mounts[0]["target"]], command
    others = [a for kw, a in final if (kw, a) != (keyword, args)]
    assert not [a for a in others if SCRIPT in a or "trust-check" in a], (
        "the final stage must reach the script only through the check's bind mount"
    )
