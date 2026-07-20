"""Dynamic federation node discovery via GitHub topic search.

Uses GitHub's REST search API to find repositories tagged with the
``agent-federation-node`` topic.  Returns *candidate* repositories —
validation against ``.well-known/agent-federation.json`` happens in
the collection stage.

Key design decisions (verified against federation-map):
- Query: ``topic:agent-federation-node`` (same as discover_federation_peers.py:24)
- Endpoint: ``https://api.github.com/search/repositories`` (same, line 25)
- Pagination: all-or-nothing — any page failure fails the entire discovery
- No org scope by default (federation-map's --org is opt-in, not protocol)
- Uses ``default_branch`` from repo metadata (no ``main`` fallback)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any

# ── Protocol constants ──────────────────────────────────────────────────────

FEDERATION_TOPIC = "agent-federation-node"
SEARCH_API = "https://api.github.com/search/repositories"
_USER_AGENT = "agent-music/0.1 (observer node)"


@dataclass(frozen=True)
class DiscoveryConfig:
    """Behaviour-only configuration for topic discovery.

    ``topic`` is a protocol constant (not casually configurable).
    Runtime values for timeouts and page limits come from
    ``config/federation.json``.
    """
    topic: str = FEDERATION_TOPIC
    per_page: int = 100
    max_pages: int = 10
    http_timeout_seconds: float = 15.0
    max_response_bytes: int = 10 * 1024 * 1024  # 10 MiB

    def __post_init__(self) -> None:
        if self.per_page < 1 or self.per_page > 100:
            raise ValueError(f"per_page must be 1-100, got {self.per_page}")
        if self.max_pages < 1 or self.max_pages > 30:
            raise ValueError(f"max_pages must be 1-30, got {self.max_pages}")
        if self.http_timeout_seconds < 1 or self.http_timeout_seconds > 120:
            raise ValueError(f"http_timeout_seconds must be 1-120, got {self.http_timeout_seconds}")
        if self.max_response_bytes < 1024:
            raise ValueError(f"max_response_bytes must be >= 1024, got {self.max_response_bytes}")


# ── Structured failure types ────────────────────────────────────────────────


@dataclass
class FetchError:
    category: str           # "timeout", "dns", "http", "rate_limited", "invalid_json", "wrong_type"
    status_code: int | None = None
    message: str = ""


@dataclass
class FetchResult:
    data: dict | list | None = None
    error: FetchError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @staticmethod
    def ok_result(data: dict | list) -> FetchResult:
        return FetchResult(data=data, error=None)

    @staticmethod
    def err(category: str, message: str = "", status_code: int | None = None) -> FetchResult:
        return FetchResult(error=FetchError(category=category, status_code=status_code, message=message))


# ── Candidate types ─────────────────────────────────────────────────────────


@dataclass
class RepositoryCandidate:
    full_name: str          # "owner/repo"
    default_branch: str     # actual default branch from GitHub metadata
    html_url: str
    description: str
    is_archived: bool
    is_fork: bool
    topics: list[str] = field(default_factory=list)
    stargazers_count: int = 0


# ── HTTP fetch with structured errors ───────────────────────────────────────


def fetch_json(
    url: str,
    config: DiscoveryConfig | None = None,
) -> FetchResult:
    """Fetch and parse JSON from *url* with structured error reporting.

    Distinguishes: timeout, DNS/connection failure, HTTP error codes,
    rate-limit exhaustion, invalid JSON, and wrong payload type.
    """
    if config is None:
        cfg = DiscoveryConfig()
    else:
        cfg = config

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers: dict[str, str] = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=cfg.http_timeout_seconds) as resp:
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining is not None and int(remaining) == 0:
                retry = resp.headers.get("X-RateLimit-Reset", "unknown")
                return FetchResult.err("rate_limited", f"reset at {retry}")

            raw = resp.read(cfg.max_response_bytes + 1)
            if len(raw) > cfg.max_response_bytes:
                return FetchResult.err(
                    "too_large",
                    f"response exceeded {cfg.max_response_bytes} bytes",
                )

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                return FetchResult.err("invalid_json", str(e)[:200])

            if not isinstance(data, (dict, list)):
                return FetchResult.err("wrong_type", f"expected dict or list, got {type(data).__name__}")

            return FetchResult.ok_result(data)

    except urllib.error.HTTPError as e:
        if e.code == 403:
            return FetchResult.err("rate_limited", f"HTTP 403 rate limited", status_code=403)
        return FetchResult.err("http", f"HTTP {e.code}", status_code=e.code)

    except urllib.error.URLError as e:
        reason = str(e.reason).lower()
        if "time" in reason or "timed" in reason:
            return FetchResult.err("timeout", str(e.reason)[:200])
        return FetchResult.err("dns", str(e.reason)[:200])

    except Exception as e:
        return FetchResult.err("unknown", f"{type(e).__name__}: {e}"[:200])


# ── Pagination helper ───────────────────────────────────────────────────────


def _build_search_url(query: str, page: int, per_page: int) -> str:
    from urllib.parse import urlencode
    params = urlencode({"q": query, "per_page": per_page, "page": page})
    return f"{SEARCH_API}?{params}"


# ── Candidate validation ────────────────────────────────────────────────────


def _validate_search_response(data: dict) -> FetchError | None:
    """Validate first-level GitHub search API response structure."""
    items = data.get("items")
    if not isinstance(items, list):
        return FetchError(category="wrong_type", message="'items' is not a list in search response")
    return None


def _validate_candidate_entry(repo: dict) -> str:  # returns rejection reason or ""
    """Validate a single candidate repository entry. Returns empty string if valid."""
    full_name = repo.get("full_name")
    if not isinstance(full_name, str) or not full_name or "/" not in full_name:
        return "invalid_full_name"

    default_branch = repo.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        return "invalid_default_branch"

    archived = repo.get("archived")
    if not isinstance(archived, bool):
        return "invalid_archived_field"

    fork_val = repo.get("fork")
    if not isinstance(fork_val, bool):
        return "invalid_fork_field"

    return ""


# ── Topic discovery ─────────────────────────────────────────────────────────


def discover_candidate_repositories(
    config: DiscoveryConfig | None = None,
) -> tuple[list[RepositoryCandidate], list[FetchError]]:
    """Discover federation node candidates via GitHub topic search.

    Discovery is **atomic**: if any required pagination page fails,
    the entire discovery fails and no partial candidate set is returned.

    Candidates are sorted deterministically by full_name after all pages
    are collected.  Archived repos and repos with missing/invalid
    ``default_branch`` are rejected.
    """
    if config is None:
        config = DiscoveryConfig()

    all_candidates: list[dict] = []
    page_errors: list[FetchError] = []
    seen: set[str] = set()

    for page in range(1, config.max_pages + 1):
        url = _build_search_url(f"topic:{config.topic}", page, config.per_page)
        result = fetch_json(url, config)

        if not result.ok:
            assert result.error is not None
            page_errors.append(result.error)
            # Atomic: any page failure → entire discovery fails
            return [], page_errors

        data = result.data
        if not isinstance(data, dict):
            return [], [FetchError(category="wrong_type", message="search response is not a dict")]

        # Validate search response structure
        resp_err = _validate_search_response(data)
        if resp_err is not None:
            return [], [resp_err]

        items = data.get("items", [])
        if not isinstance(items, list):
            return [], [FetchError(category="wrong_type", message="items is not a list")]

        # Exit early on empty
        if len(items) == 0:
            break

        # Validate and filter candidates
        rejection_counts: dict[str, int] = {}
        for repo in items:
            if not isinstance(repo, dict):
                continue
            reject_reason = _validate_candidate_entry(repo)
            if reject_reason:
                rejection_counts[reject_reason] = rejection_counts.get(reject_reason, 0) + 1
                continue

            full_name = repo["full_name"]
            if full_name in seen:
                continue
            if repo["archived"]:
                continue
            seen.add(full_name)
            all_candidates.append(repo)

        # Log page-level stats
        if rejection_counts:
            cats = ", ".join(f"{k}:{v}" for k, v in sorted(rejection_counts.items()))
            print(f"discovery: page {page} — {len(items)} raw, "
                  f"{len(items) - sum(rejection_counts.values())} valid, "
                  f"rejected: {cats}", file=sys.stderr)

        # Partial page = last page
        if len(items) < config.per_page:
            break

    # Deterministic ordering
    all_candidates.sort(key=lambda r: r.get("full_name", ""))

    # Truncate to safety bound
    if len(all_candidates) > config.max_pages * config.per_page:
        all_candidates = all_candidates[:config.max_pages * config.per_page]

    candidates = [
        RepositoryCandidate(
            full_name=r["full_name"],
            default_branch=r["default_branch"],
            html_url=r.get("html_url", ""),
            description=r.get("description") or "",
            is_archived=r.get("archived", False),
            is_fork=r.get("fork", False),
            topics=list(r.get("topics", [])),
            stargazers_count=r.get("stargazers_count", 0),
        )
        for r in all_candidates
    ]

    return candidates, page_errors
