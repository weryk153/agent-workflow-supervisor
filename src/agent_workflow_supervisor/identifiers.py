"""Canonical identifiers at tracker and runner boundaries."""

from __future__ import annotations

import re
from urllib.parse import urlparse


def canonical_github_issue_id(
    value: str,
    repository: str,
    *,
    strict: bool = False,
) -> str:
    """Normalize supported references for one configured GitHub repository.

    AO may expose either a bare issue number or
    ``github:owner/repository#number``. Internally both become the decimal issue
    number because the project configuration already supplies the repository
    namespace. References to another repository remain distinct unless strict
    input validation is requested.
    """

    raw = value.strip()
    expected_repository = repository.strip("/").casefold()
    if re.fullmatch(r"\d+", raw):
        return str(int(raw))
    shorthand = re.fullmatch(r"#(\d+)", raw)
    if shorthand:
        return str(int(shorthand.group(1)))

    qualified = re.fullmatch(r"(?:github:)?([^/#\s]+/[^/#\s]+)#(\d+)", raw, re.IGNORECASE)
    if qualified:
        candidate = qualified.group(1).casefold()
        number = str(int(qualified.group(2)))
        if candidate == expected_repository:
            return number
        if strict:
            raise ValueError(
                f"issue reference belongs to {candidate!r}, not {expected_repository!r}"
            )
        return f"github:{candidate}#{number}"

    if "://" in raw:
        parsed = urlparse(raw)
        parts = parsed.path.strip("/").split("/")
        if (
            parsed.hostname is not None
            and parsed.hostname.casefold() == "github.com"
            and len(parts) == 4
            and parts[2] == "issues"
            and re.fullmatch(r"\d+", parts[3])
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
        ):
            candidate = f"{parts[0]}/{parts[1]}".casefold()
            number = str(int(parts[3]))
            if candidate == expected_repository:
                return number
            if strict:
                raise ValueError(
                    f"issue reference belongs to {candidate!r}, not {expected_repository!r}"
                )
            return f"github:{candidate}#{number}"

    if strict:
        raise ValueError(f"unsupported GitHub issue reference: {value!r}")
    return raw
