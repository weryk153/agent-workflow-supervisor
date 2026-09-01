"""Canonical identifiers at tracker and runner boundaries."""

from __future__ import annotations

import re
from urllib.parse import urlparse


def canonical_github_repository(remote: str) -> str:
    """Return ``owner/repository`` for an exact github.com Git remote."""

    value = remote.strip()
    host: str | None = None
    path = ""
    if "://" in value:
        parsed = urlparse(value)
        host = parsed.hostname
        path = parsed.path
        if parsed.params or parsed.query or parsed.fragment:
            raise ValueError(f"only plain GitHub repository remotes are supported: {remote}")
    else:
        scp = re.fullmatch(r"(?:[^@/:\s]+@)?([^/:\s]+):(.+)", value)
        if scp:
            host, path = scp.groups()
        else:
            bare = re.fullmatch(r"([^/\s]+)/(.+)", value)
            if bare:
                host, path = bare.groups()
    if host is None or host.casefold() != "github.com":
        raise ValueError(f"only GitHub remotes are supported by the built-in tracker: {remote}")
    parts = path.strip("/").split("/")
    if len(parts) != 2:
        raise ValueError(f"invalid GitHub repository remote: {remote}")
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if (
        not owner
        or not repository
        or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", component) for component in (owner, repository))
    ):
        raise ValueError(f"invalid GitHub repository remote: {remote}")
    return f"{owner}/{repository}"


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
