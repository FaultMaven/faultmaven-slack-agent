#!/usr/bin/env python3
"""Regenerate faultmaven/api_generated.py from the API's OpenAPI contract.

The Python counterpart of faultmaven-copilot's `generate:api-types`, and
deliberately the same arrangement: the contract generates **models**, not a
client. The HTTP calls in `faultmaven/client.py` stay hand-written — the
refresh-token rotation has ordering requirements (persist before use) that a
generated client would not know about — but the shapes they send and receive
come from the contract rather than from a `.get()` and a hope.

The server cannot tell its clients apart, so they should not consume the
contract in different ways. Before this, the Slack agent was the only client
with no contract check at all: a renamed response field surfaced as
`body.get("agent_response")` returning None, and no test or CI job would say
anything, because the tests mock the API and the mocks encode the same
assumption the code does.

Defaults to the contract pinned in api-contract.pin.json — the same source CI
compares against. Moving `ref` there is how this repository adopts a contract
change; until it moves, a backend merge cannot change what this generates.

    python scripts/generate_api_models.py
    python scripts/generate_api_models.py --spec ../faultmaven/docs/reference/api/openapi.json
    FM_OPENAPI_SPEC=/tmp/openapi.json python scripts/generate_api_models.py

Requires the dev dependencies: datamodel-code-generator and the pinned ruff
(the generator formats its output with it, so an unpinned formatter would show
up as spurious drift).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PIN_PATH = REPO_ROOT / "api-contract.pin.json"
OUTPUT_PATH = REPO_ROOT / "faultmaven" / "api_generated.py"

RAW_URL = "https://raw.githubusercontent.com/{repository}/{ref}/docs/reference/api/openapi.json"


def pinned_spec_url() -> str:
    """The contract this client is written against, from the pin file."""
    try:
        pin = json.loads(PIN_PATH.read_text())
    except (OSError, ValueError) as exc:
        sys.exit(f"Could not read {PIN_PATH}: {exc}")

    missing = [key for key in ("repository", "ref") if not pin.get(key)]
    if missing:
        sys.exit(f"{PIN_PATH} must set {' and '.join(missing)}")

    return RAW_URL.format(repository=pin["repository"], ref=pin["ref"])


def local_copy(spec: str, directory: str, *, from_pin: bool) -> str:
    """A local path for ``spec``, downloading it first if it is a URL.

    `datamodel-codegen --input` reads a path, not a URL, and downloading here
    rather than passing `--url` puts the sanity check below on every path — a
    truncated download or an error page would otherwise generate an empty
    module, and a drift check comparing it would "pass" by matching nothing.
    """
    if not spec.startswith(("http://", "https://")):
        # Resolved against the CALLER's cwd before the generator runs with
        # cwd=REPO_ROOT, or `--spec ./openapi.json` from anywhere else would
        # silently read <repo>/openapi.json — a stale file if one exists, and a
        # confusing failure if not.
        return str(Path(spec).resolve())

    destination = os.path.join(directory, "openapi.json")
    try:
        with urllib.request.urlopen(spec, timeout=30) as response:
            payload = response.read()
    except OSError as exc:
        sys.exit(f"Could not fetch the contract from {spec}: {exc}")

    try:
        document = json.loads(payload)
    except ValueError as exc:
        sys.exit(f"The contract at {spec} is not valid JSON: {exc}")
    if not document.get("paths"):
        sys.exit(f"The contract at {spec} declares no paths; refusing to generate.")
    if from_pin:
        check_pinned_version(document)

    Path(destination).write_bytes(payload)
    return destination


def check_pinned_version(document: dict) -> None:
    """The pin names a contract version as well as a ref; they must agree.

    Without this the version is decorative: a pin bump that moves `ref` and
    forgets `contractVersion` (or the reverse) would pass silently, and the
    client would be generated from a contract nobody reviewed. copilot and
    dashboard check the same thing in CI; here it belongs in the generator,
    which has already parsed the document.

    Only called for the pinned contract. A `--spec` or FM_OPENAPI_SPEC
    override is the "prepare against a proposed contract" path, where
    disagreeing with the pin is the whole point.
    """
    try:
        pin = json.loads(PIN_PATH.read_text())
    except (OSError, ValueError):
        return

    pinned = pin.get("contractVersion")
    published = (document.get("info") or {}).get("version")
    if pinned and published and pinned != published:
        sys.exit(
            f"{PIN_PATH.name} says contractVersion {pinned}, but the pinned ref "
            f"serves {published}. The ref is not the commit whoever edited the "
            f"pin meant."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--spec",
        help="path or URL of the contract to generate from (default: the pin)",
    )
    args = parser.parse_args()

    override = args.spec or os.environ.get("FM_OPENAPI_SPEC")
    spec = override or pinned_spec_url()

    print(f"Generating {OUTPUT_PATH.relative_to(REPO_ROOT)} from {spec}")

    with tempfile.TemporaryDirectory() as directory:
        return generate(local_copy(spec, directory, from_pin=override is None))


def generate(input_path: str) -> int:
    result = subprocess.run(
        [
            "datamodel-codegen",
            "--input",
            input_path,
            "--input-file-type",
            "openapi",
            "--output-model-type",
            "pydantic_v2.BaseModel",
            # Pinned explicitly: the default set is changing upstream, and the
            # drift check compares generated text — a formatter that shifted
            # under us would read as a contract change that never happened.
            "--formatters",
            "ruff-format",
            # Without this the header carries a generation timestamp, so every
            # regeneration differs from the committed file and the drift job is
            # permanently red — reporting a contract change on every run, which
            # is the same as reporting none.
            "--disable-timestamp",
            "--output",
            str(OUTPUT_PATH),
        ],
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        print(
            "datamodel-codegen failed. Install the dev dependencies: "
            "pip install -r requirements-dev.txt",
            file=sys.stderr,
        )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
