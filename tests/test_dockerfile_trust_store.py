"""What the image build cannot catch about its trust-store treatment (core #2633).

CI builds the image on every PR, and the build runs core's trust-store check, so
the build is the regression for the stores themselves. Two things a passing
build does not prove are pinned here: that every pip verifies against the OS
store (``PIP_CERT`` set before the first instruction that runs pip), and that
the check is core's script fetched at a fixed commit and verified by digest,
not a floating ref or an unverified download.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"

PIP = re.compile(r"(?<![\w.-])pip3?(?![\w.-])")
SCRIPT_URL = re.compile(
    r"https://raw\.githubusercontent\.com/FaultMaven/faultmaven/[0-9a-f]{40}"
    r"/scripts/check_image_trust_store\.py"
)


def _instructions(text: str) -> list[tuple[str, str]]:
    """Every stage's instructions in order: continuations joined, comments dropped."""
    out: list[tuple[str, str]] = []
    parts: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        body = line.rstrip()
        parts.append(body.removesuffix("\\").strip())
        if body.endswith("\\"):
            continue
        joined = " ".join(p for p in parts if p)
        parts = []
        if joined:
            keyword, _, args = joined.partition(" ")
            out.append((keyword.upper(), args.strip()))
    return out


def _env_keys(args: str) -> set[str]:
    tokens = shlex.split(args)
    if tokens and "=" not in tokens[0]:  # legacy `ENV KEY value`
        return {tokens[0]}
    return {token.split("=", 1)[0] for token in tokens}


def test_pip_cert_is_set_before_the_first_pip() -> None:
    instructions = _instructions(DOCKERFILE.read_text())
    first_pip = next(
        i for i, (kw, args) in enumerate(instructions) if kw == "RUN" and PIP.search(args)
    )
    assert any(
        kw == "ENV" and "PIP_CERT" in _env_keys(args) for kw, args in instructions[:first_pip]
    ), "PIP_CERT must be set before the first instruction that runs pip"


def test_the_check_script_is_fetched_at_a_pinned_commit_by_digest() -> None:
    adds = [shlex.split(args) for kw, args in _instructions(DOCKERFILE.read_text()) if kw == "ADD"]
    fetches = [tokens for tokens in adds if any(SCRIPT_URL.fullmatch(t) for t in tokens)]
    assert len(fetches) == 1, "core's check_image_trust_store.py must be fetched once, by commit"
    flags = [token for token in fetches[0] if token.startswith("--")]
    assert any(re.fullmatch(r"--checksum=sha256:[0-9a-f]{64}", flag) for flag in flags)
