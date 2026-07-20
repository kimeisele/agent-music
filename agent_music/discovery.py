"""Dynamic federation node discovery via GitHub topic search.

Uses GitHub's REST search API to find repositories tagged with the
``agent-federation-node`` topic.  Returns *candidate* repositories —
validation against ``.well-known/agent-federation.json`` happens in
the collection stage.

Key design decisions (verified against federation-map):
- Query: ``topic:agent-federation-node`` (same as discover_federation_peers.py:24)
- Endpoint: ``https://api.github.com/search/repositories`` (same, line 25)
- Pagination: implemented (gap in federation-map's discover() which only
  fetches one page)
- No org scope by default (federation-map's --org is opt-in, not protocol)
- Uses ``default_branch`` from repo metadata instead of assuming ``main``
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any

# ── Protocol constants (verified against federation-map) ────────────────────

FEDERATION_TOPIC = "agent-federation-node"
SEARCH_API = "https://api.github.com/search/repositories"
PER_PAGE = 100          # GitHub max
MAX_PAGES = 10          # safety bound: 10 × 100 = 1000 candidates
HTTP_TIMEOUT = 15       # seconds
MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MiB safety bound
_USER_AGENT = "agent-music/0.1 (observer node)"

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


def _get_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def fetch_json(url: str) -> FetchResult:
    """Fetch and parse JSON from *url* with structured error reporting.

    Distinguishes: timeout, DNS/connection failure, HTTP error codes,
    rate-limit exhaustion, invalid JSON, and wrong payload type.
    """
    token = _get_token()
    headers: dict[str, str] = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            # Check rate-limit headers
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining is not None and int(remaining) == 0:
                retry = resp.headers.get("X-RateLimit-Reset", "unknown")
                return FetchResult.err("rate_limited", f"reset at {retry}")

            raw = resp.read(MAX_RESPONSE_BYTES)
            if len(raw) >= MAX_RESPONSE_BYTES:
                return FetchResult.err("too_large", f"response exceeded {MAX_RESPONSE_BYTES} bytes")

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                return FetchResult.err("invalid_json", str(e)[:200])

            if not isinstance(data, (dict, list)):
                return FetchResult.err("wrong_type", f"expected dict or list, got {type(data).__name__}")

            return FetchResult.ok_result(data)

    except urllib.error.HTTPError as e:
        if e.code == 403:
            remaining = e.headers.get("X-RateLimit-Remaining", "?") if hasattr(e, "headers") else "?"
            if remaining == "0":
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


def _parse_link_header(link_str: str) -> dict[str, str]:
    """Parse GitHub's Link header into a dict of {rel: url}."""
    links: dict[str, str] = {}
    for part in link_str.split(","):
        part = part.strip()
        url_start = part.find("<")
        url_end = part.find(">")
        rel_start = part.find('rel="')
        if url_start >= 0 and url_end > url_start and rel_start >= 0:
            url = part[url_start + 1:url_end]
            rel_end = part.find('"', rel_start + 5)
            rel = part[rel_start + 5:rel_end] if rel_end > rel_start else ""
            links[rel] = url
    return links


def _build_search_url(query: str, page: int, per_page: int = PER_PAGE) -> str:
    """Build a GitHub search API URL with encoded query parameters."""
    from urllib.parse import urlencode
    params = urlencode({"q": query, "per_page": per_page, "page": page})
    return f"{SEARCH_API}?{params}"


# ── Topic discovery ─────────────────────────────────────────────────────────


def discover_candidate_repositories(
    topic: str = FEDERATION_TOPIC,
    token: str | None = None,
) -> tuple[list[RepositoryCandidate], list[FetchError]]:
    """Discover federation node candidates via GitHub topic search.

    Returns (candidates, errors).  Errors are per-page fetch failures that
    did not abort the entire discovery (e.g. one page timed out but others
    succeeded).  If no pages could be fetched, candidates will be empty and
    errors will contain the root cause.

    Paginates through all available results up to ``MAX_PAGES``.
    Results are deterministically ordered by full_name after collection.
    Archived repos are excluded (federation-map does not explicitly handle
    them, but they cannot publish live descriptors).
    """
    all_candidates: list[dict] = []
    page_errors: list[FetchError] = []
    seen: set[str] = set()
    total_count: int | None = None

    for page in range(1, MAX_PAGES + 1):
        url = _build_search_url(f"topic:{topic}", page)
        result = fetch_json(url)

        if not result.ok:
            page_errors.append(result.error)  # type: ignore[arg-type]
            if page == 1:
                # First page failed entirely — cannot continue
                return [], page_errors
            # Subsequent page failure — keep what we have
            print(f"discovery: page {page} failed ({result.error.category}), "
                  f"continuing with {len(all_candidates)} candidates", file=sys.stderr)
            break

        data = result.data
        assert isinstance(data, dict)

        if total_count is None:
            total_count = data.get("total_count", 0)
            if total_count == 0:
                return [], []

        items = data.get("items", [])
        if not isinstance(items, list):
            page_errors.append(FetchError(category="wrong_type", message="items is not a list"))
            break

        for repo in items:
            if not isinstance(repo, dict):
                continue
            full_name = repo.get("full_name", "")
            if not full_name or full_name in seen:
                continue
            if repo.get("archived", False):
                continue
            seen.add(full_name)
            all_candidates.append(repo)

        # Check for more pages via Link header
        # (we can't access response headers from fetch_json — use item count)
        if len(items) < PER_PAGE:
            break  # partial page = last page

    # Sort deterministically by full_name
    all_candidates.sort(key=lambda r: r.get("full_name", ""))

    # Truncate to total_count safety bound
    if len(all_candidates) > MAX_PAGES * PER_PAGE:
        all_candidates = all_candidates[:MAX_PAGES * PER_PAGE]

    candidates = [
        RepositoryCandidate(
            full_name=r["full_name"],
            default_branch=r.get("default_branch", "main"),
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
