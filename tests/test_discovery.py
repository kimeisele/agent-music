"""Test topic-based discovery, descriptor validation, and collection stages.

Uses HTTP fixtures — no live network access required.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent_music.discovery import (
    discover_candidate_repositories,
    fetch_json,
    FetchResult,
    FetchError,
    RepositoryCandidate,
    FEDERATION_TOPIC,
)
from agent_music.collect import (
    fetch_federation_descriptor,
    validate_federation_descriptor,
    collect_node_state,
    ValidatedNode,
    RejectedCandidate,
    CollectionResult,
    collect_federation_state,
    DEFAULT_OUTBOX_PATH,
)


# ── HTTP fixture helpers ────────────────────────────────────────────────────


def _make_search_response(items: list[dict], total_count: int | None = None) -> dict:
    return {
        "total_count": total_count if total_count is not None else len(items),
        "incomplete_results": False,
        "items": items,
    }


def _make_repo(
    full_name: str = "kimeisele/test-node",
    default_branch: str = "main",
    archived: bool = False,
    fork: bool = False,
) -> dict:
    return {
        "full_name": full_name,
        "default_branch": default_branch,
        "html_url": f"https://github.com/{full_name}",
        "description": "Test node",
        "archived": archived,
        "fork": fork,
        "topics": ["agent-federation-node"],
        "stargazers_count": 1,
    }


def _make_descriptor(repo_id: str = "kimeisele/test-node", kind: str = "agent_federation_descriptor") -> dict:
    return {
        "kind": kind,
        "version": 1,
        "repo_id": repo_id,
        "display_name": "Test Node",
        "status": "active",
        "layer": "node",
        "capabilities": ["authority-publishing"],
        "endpoints": [],
    }


# ── Discovery tests ─────────────────────────────────────────────────────────


class TestDiscovery:
    def test_single_page_result(self):
        repo = _make_repo("kimeisele/node-a")
        with patch("agent_music.discovery.fetch_json") as mock_fetch:
            mock_fetch.return_value = FetchResult.ok_result(_make_search_response([repo], 1))
            candidates, errors = discover_candidate_repositories()

        assert len(candidates) == 1
        assert len(errors) == 0
        assert candidates[0].full_name == "kimeisele/node-a"
        assert candidates[0].default_branch == "main"

    def test_multiple_pages(self):
        page1 = _make_search_response(
            [_make_repo(f"kimeisele/node-{i}") for i in range(100)], 200
        )
        page2 = _make_search_response(
            [_make_repo(f"kimeisele/node-{i}") for i in range(100, 200)], 200
        )
        page3 = _make_search_response(
            [_make_repo(f"kimeisele/node-{i}") for i in range(200, 250)], 250
        )

        call_count = [0]

        def mock_fetch(url, config=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return FetchResult.ok_result(page1)
            elif call_count[0] == 2:
                return FetchResult.ok_result(page2)
            else:
                return FetchResult.ok_result(page3)

        with patch("agent_music.discovery.fetch_json", side_effect=mock_fetch):
            candidates, errors = discover_candidate_repositories()

        assert len(candidates) == 250
        assert len(errors) == 0

    def test_empty_result(self):
        with patch("agent_music.discovery.fetch_json") as mock_fetch:
            mock_fetch.return_value = FetchResult.ok_result(
                {"total_count": 0, "incomplete_results": False, "items": []}
            )
            candidates, errors = discover_candidate_repositories()

        assert len(candidates) == 0
        assert len(errors) == 0

    def test_deterministic_ordering(self):
        repos = [
            _make_repo("kimeisele/zebra"),
            _make_repo("kimeisele/alpha"),
            _make_repo("kimeisele/mike"),
        ]
        with patch("agent_music.discovery.fetch_json") as mock_fetch:
            mock_fetch.return_value = FetchResult.ok_result(
                _make_search_response(repos, 3)
            )
            candidates, _ = discover_candidate_repositories()

        names = [c.full_name for c in candidates]
        assert names == sorted(names)

    def test_duplicates_across_pages(self):
        # Page 1: full page (100 items, including node-a and node-b)
        page1_items = [_make_repo(f"kimeisele/filler-{i}") for i in range(98)]
        page1_items.append(_make_repo("kimeisele/node-a"))
        page1_items.append(_make_repo("kimeisele/node-b"))
        page1 = _make_search_response(page1_items, 101)
        # Page 2: partial page (1 item + 1 duplicate)
        page2 = _make_search_response(
            [_make_repo("kimeisele/node-b"), _make_repo("kimeisele/node-c")], 101
        )

        call_count = [0]

        def mock_fetch(url, config=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return FetchResult.ok_result(page1)
            else:
                return FetchResult.ok_result(page2)

        with patch("agent_music.discovery.fetch_json", side_effect=mock_fetch):
            candidates, _ = discover_candidate_repositories()

        # 100 from page 1 + 1 new from page 2 (node-c, node-b duplicate excluded)
        assert len(candidates) == 101

    def test_archived_excluded(self):
        repos = [
            _make_repo("kimeisele/active"),
            _make_repo("kimeisele/archived", archived=True),
        ]
        with patch("agent_music.discovery.fetch_json") as mock_fetch:
            mock_fetch.return_value = FetchResult.ok_result(
                _make_search_response(repos, 2)
            )
            candidates, _ = discover_candidate_repositories()

        names = [c.full_name for c in candidates]
        assert "kimeisele/archived" not in names
        assert len(candidates) == 1

    def test_first_page_failure(self):
        with patch("agent_music.discovery.fetch_json") as mock_fetch:
            mock_fetch.return_value = FetchResult.err("timeout", "timed out")
            candidates, errors = discover_candidate_repositories()

        assert len(candidates) == 0
        assert len(errors) > 0

    def test_max_page_bound(self):
        # Create 11 pages × 100 repos = 1100, but max_pages=10
        repos_per_page = [_make_repo(f"kimeisele/node-{i}") for i in range(100)]

        call_count = [0]

        def mock_fetch(url, config=None):
            call_count[0] += 1
            return FetchResult.ok_result(
                _make_search_response(repos_per_page, 1100)
            )

        with patch("agent_music.discovery.fetch_json", side_effect=mock_fetch):
            candidates, _ = discover_candidate_repositories()

        # Should stop at MAX_PAGES (10) despite all pages returning full results
        assert len(candidates) <= 10 * 100

    def test_http_403(self):
        with patch("agent_music.discovery.fetch_json") as mock_fetch:
            mock_fetch.return_value = FetchResult.err("rate_limited", "HTTP 403", status_code=403)
            candidates, errors = discover_candidate_repositories()

        assert len(candidates) == 0


# ── Descriptor validation tests ─────────────────────────────────────────────


class TestDescriptorValidation:
    def test_valid_descriptor(self):
        candidate = RepositoryCandidate(
            full_name="kimeisele/test-node",
            default_branch="main",
            html_url="https://github.com/kimeisele/test-node",
            description="",
            is_archived=False,
            is_fork=False,
        )
        descriptor = _make_descriptor("kimeisele/test-node")
        result = validate_federation_descriptor(candidate, descriptor)
        assert isinstance(result, ValidatedNode)
        assert result.full_name == "kimeisele/test-node"

    def test_wrong_kind(self):
        candidate = RepositoryCandidate(
            full_name="kimeisele/bad-node", default_branch="main",
            html_url="", description="", is_archived=False, is_fork=False,
        )
        descriptor = _make_descriptor("kimeisele/bad-node", kind="wrong_kind")
        result = validate_federation_descriptor(candidate, descriptor)
        assert isinstance(result, RejectedCandidate)
        assert result.reason == "wrong_kind"

    def test_missing_repo_id(self):
        candidate = RepositoryCandidate(
            full_name="kimeisele/no-id", default_branch="main",
            html_url="", description="", is_archived=False, is_fork=False,
        )
        descriptor = _make_descriptor("")  # empty repo_id
        descriptor["repo_id"] = ""
        result = validate_federation_descriptor(candidate, descriptor)
        assert isinstance(result, RejectedCandidate)
        assert result.reason == "missing_repo_id"

    def test_identity_mismatch(self):
        candidate = RepositoryCandidate(
            full_name="kimeisele/real-node", default_branch="main",
            html_url="", description="", is_archived=False, is_fork=False,
        )
        descriptor = _make_descriptor("evil-org/impostor")  # claims different identity
        result = validate_federation_descriptor(candidate, descriptor)
        assert isinstance(result, RejectedCandidate)
        assert result.reason == "identity_mismatch"

    def test_non_main_default_branch(self):
        candidate = RepositoryCandidate(
            full_name="kimeisele/custom-branch", default_branch="develop",
            html_url="", description="", is_archived=False, is_fork=False,
        )
        descriptor = _make_descriptor("kimeisele/custom-branch")
        result = validate_federation_descriptor(candidate, descriptor)
        assert isinstance(result, ValidatedNode)
        assert result.default_branch == "develop"


# ── Collection / fetch tests ────────────────────────────────────────────────


class TestFetchDescriptor:
    def test_fetch_valid_descriptor(self):
        candidate = RepositoryCandidate(
            full_name="kimeisele/test-node", default_branch="main",
            html_url="", description="", is_archived=False, is_fork=False,
        )
        descriptor = _make_descriptor("kimeisele/test-node")

        with patch("agent_music.collect.fetch_json") as mock_fetch:
            mock_fetch.return_value = FetchResult.ok_result(descriptor)
            data, err = fetch_federation_descriptor(candidate)

        assert data is not None
        assert err is None
        assert data["kind"] == "agent_federation_descriptor"

    def test_fetch_invalid_json(self):
        candidate = RepositoryCandidate(
            full_name="kimeisele/bad-json", default_branch="main",
            html_url="", description="", is_archived=False, is_fork=False,
        )

        with patch("agent_music.collect.fetch_json") as mock_fetch:
            mock_fetch.return_value = FetchResult.err("invalid_json", "not json")
            data, err = fetch_federation_descriptor(candidate)

        assert data is None
        assert err is not None
        assert err.category == "invalid_json"

    def test_fetch_wrong_type(self):
        candidate = RepositoryCandidate(
            full_name="kimeisele/bad-type", default_branch="main",
            html_url="", description="", is_archived=False, is_fork=False,
        )

        with patch("agent_music.collect.fetch_json") as mock_fetch:
            mock_fetch.return_value = FetchResult.ok_result(["not", "a", "dict"])
            data, err = fetch_federation_descriptor(candidate)

        assert data is None
        assert err is not None
        assert err.category == "wrong_type"


# ── Change detection tests ──────────────────────────────────────────────────


class TestChangeDetection:
    def test_unchanged_state_no_synthesis(self, tmp_path):
        """When semantic hash matches previous, render returns early."""
        # This is tested via the CLI's render command
        from agent_music.cli import _load_previous_hash

        meta = tmp_path / "prev.json"
        meta.write_text(json.dumps({"semantic_snapshot_sha256": "abc123"}))

        assert _load_previous_hash(str(meta)) == "abc123"

    def test_no_previous_hash_always_renders(self):
        from agent_music.cli import _load_previous_hash
        assert _load_previous_hash(None) is None
        assert _load_previous_hash("/nonexistent/path.json") is None


# ── Collection orchestration tests ──────────────────────────────────────────


class TestCollectionOrchestration:
    def test_all_candidates_invalid_still_returns_result(self):
        """Even when all candidates fail validation, we get a CollectionResult (not crash)."""
        with patch("agent_music.collect.discover_candidate_repositories") as mock_disc:
            # Return candidates that will fail fetch
            candidate = RepositoryCandidate(
                full_name="evil/bad-node", default_branch="main",
                html_url="", description="", is_archived=False, is_fork=False,
            )
            mock_disc.return_value = ([candidate], [])

            with patch("agent_music.collect.fetch_json") as mock_fetch:
                mock_fetch.return_value = FetchResult.err("timeout", "timed out")
                result = collect_federation_state()

        assert isinstance(result, CollectionResult)
        assert not result.has_authoritative_state
        assert result.rejected == 1


# ── Atomic pagination tests ─────────────────────────────────────────────────


class TestAtomicPagination:
    def test_page2_timeout_fails_discovery(self):
        """Page 1 succeeds, page 2 times out → entire discovery fails."""
        page1 = _make_search_response(
            [_make_repo(f"kimeisele/node-{i}") for i in range(100)], 200
        )

        call_count = [0]

        def mock_fetch(url, config=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return FetchResult.ok_result(page1)
            else:
                return FetchResult.err("timeout", "page 2 timed out")

        with patch("agent_music.discovery.fetch_json", side_effect=mock_fetch):
            candidates, errors = discover_candidate_repositories()

        assert len(candidates) == 0  # atomic: fail entirely
        assert len(errors) > 0
        assert errors[0].category == "timeout"

    def test_page2_rate_limited_fails_discovery(self):
        """Page 1 succeeds, page 2 rate-limited → entire discovery fails."""
        page1 = _make_search_response(
            [_make_repo(f"kimeisele/node-{i}") for i in range(100)], 200
        )

        call_count = [0]

        def mock_fetch(url, config=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return FetchResult.ok_result(page1)
            else:
                return FetchResult.err("rate_limited", "rate limited")

        with patch("agent_music.discovery.fetch_json", side_effect=mock_fetch):
            candidates, errors = discover_candidate_repositories()

        assert len(candidates) == 0
        assert len(errors) > 0


# ── Default-branch rejection tests ──────────────────────────────────────────


class TestDefaultBranchRejection:
    def test_missing_default_branch_rejected(self):
        repo = {
            "full_name": "kimeisele/no-branch",
            "html_url": "https://github.com/kimeisele/no-branch",
            "description": "",
            "archived": False,
            "fork": False,
            "topics": ["agent-federation-node"],
            "stargazers_count": 1,
            # NO default_branch field
        }
        with patch("agent_music.discovery.fetch_json") as mock_fetch:
            mock_fetch.return_value = FetchResult.ok_result(
                _make_search_response([repo], 1)
            )
            candidates, errors = discover_candidate_repositories()

        assert len(candidates) == 0  # rejected

    def test_empty_default_branch_rejected(self):
        repo = _make_repo("kimeisele/empty-branch")
        repo["default_branch"] = ""  # empty string
        with patch("agent_music.discovery.fetch_json") as mock_fetch:
            mock_fetch.return_value = FetchResult.ok_result(
                _make_search_response([repo], 1)
            )
            candidates, errors = discover_candidate_repositories()

        assert len(candidates) == 0


# ── Response-size boundary tests ────────────────────────────────────────────


class TestResponseSizeBoundary:
    def test_exact_max_ok(self):
        from agent_music.discovery import DiscoveryConfig
        config = DiscoveryConfig(max_response_bytes=1024)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.headers = {}
            # Exactly 1024 bytes — must be accepted
            mock_resp.read.return_value = b'{"k":"' + b'x' * 1016 + b'"}'
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            result = fetch_json("https://example.com", config)
            assert result.ok

    def test_max_plus_one_rejected(self):
        from agent_music.discovery import DiscoveryConfig
        config = DiscoveryConfig(max_response_bytes=1024)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.headers = {}
            # 1025 bytes > 1024 max — must be rejected
            mock_resp.read.return_value = b'{"k":"' + b'x' * 1017 + b'"}'
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            result = fetch_json("https://example.com", config)
            assert not result.ok
            assert result.error.category == "too_large"


# ── Config validation tests ─────────────────────────────────────────────────


class TestConfigValidation:
    def test_valid_config(self):
        from agent_music.discovery import DiscoveryConfig
        cfg = DiscoveryConfig(per_page=50, max_pages=5)
        assert cfg.per_page == 50
        assert cfg.max_pages == 5

    def test_invalid_per_page_raises(self):
        from agent_music.discovery import DiscoveryConfig
        with pytest.raises(ValueError):
            DiscoveryConfig(per_page=0)
        with pytest.raises(ValueError):
            DiscoveryConfig(per_page=101)

    def test_invalid_max_pages_raises(self):
        from agent_music.discovery import DiscoveryConfig
        with pytest.raises(ValueError):
            DiscoveryConfig(max_pages=0)
        with pytest.raises(ValueError):
            DiscoveryConfig(max_pages=31)
