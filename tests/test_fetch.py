"""Test structured HTTP fetch error handling."""

from __future__ import annotations

from unittest.mock import patch, MagicMock
import urllib.error
import urllib.request
import json

from agent_music.discovery import fetch_json, FetchResult


class TestFetchJson:
    def test_ok_dict(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.headers = {}
            mock_resp.read.return_value = b'{"key": "value"}'
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            result = fetch_json("https://example.com/test")
            assert result.ok
            assert result.data == {"key": "value"}

    def test_timeout(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("timed out")

            result = fetch_json("https://example.com/timeout")
            assert not result.ok
            assert result.error is not None
            assert result.error.category == "timeout"

    def test_dns_failure(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("getaddrinfo failed")

            result = fetch_json("https://nonexistent.example.com")
            assert not result.ok
            assert result.error is not None
            assert result.error.category == "dns"

    def test_http_404(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                "https://example.com", 404, "Not Found", {}, None
            )

            result = fetch_json("https://example.com/not-found")
            assert not result.ok
            assert result.error is not None
            assert result.error.category == "http"
            assert result.error.status_code == 404

    def test_http_403(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.headers = {"X-RateLimit-Remaining": "0"}
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.side_effect = urllib.error.HTTPError(
                "https://example.com", 403, "Forbidden",
                {"X-RateLimit-Remaining": "0"}, None
            )

            result = fetch_json("https://example.com/forbidden")
            assert not result.ok
            assert result.error is not None
            assert result.error.category == "rate_limited"

    def test_invalid_json(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.headers = {}
            mock_resp.read.return_value = b"not json at all"
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            result = fetch_json("https://example.com/bad-json")
            assert not result.ok
            assert result.error is not None
            assert result.error.category == "invalid_json"

    def test_wrong_type_array(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.headers = {}
            mock_resp.read.return_value = b'["array"]'
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            result = fetch_json("https://example.com/array")
            # Arrays ARE valid FetchResult data (list | dict)
            assert result.ok

    def test_wrong_type_string(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.headers = {}
            mock_resp.read.return_value = b'"a string"'
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            result = fetch_json("https://example.com/string")
            assert not result.ok
            assert result.error is not None
            assert result.error.category == "wrong_type"

    def test_malformed_search_response(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.headers = {}
            # Missing "items" key
            mock_resp.read.return_value = b'{"total_count": 5}'
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            result = fetch_json("https://api.github.com/search/repositories?q=test")
            assert result.ok  # fetch_json itself succeeds (valid JSON dict)
