"""core.library.service_search's 'deezer' dispatch branch.

A blanket `except Exception: data = []` around the raw Deezer request used to
turn every failure — a timeout, a connection error, an HTTP error status, or
Deezer's own `{"error": {...}}` payload (e.g. a shared-throttle rate limit) —
into a silent empty result. The manual-match modal then showed "No results
found", indistinguishable from the artist genuinely not being on Deezer, with
nothing in the logs to tell the two apart. Real failures must now propagate to
the route's own handler (web_server.py's `library_search_service`), which
already logs them and returns a proper error message — same as every other
service branch in this module.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import core.library.service_search as ss


def _response(json_body, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body

    def raise_for_status():
        if status_code >= 400:
            raise RuntimeError(f"HTTP {status_code}")

    resp.raise_for_status.side_effect = raise_for_status
    return resp


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr("core.deezer_throttle.wait_for_slot", lambda: None)


def test_artist_search_returns_every_candidate():
    payload = {
        "data": [
            {"id": 123, "name": "Boards of Canada", "picture_medium": "https://x/a.jpg", "nb_fan": 50000},
            {"id": 456, "name": "Boards of Canada Tribute", "picture_medium": None, "nb_fan": 10},
        ]
    }
    with patch("requests.get", return_value=_response(payload)) as mock_get:
        results = ss._search_service("deezer", "artist", "Boards of Canada")

    assert len(results) == 2
    assert results[0] == {"id": "123", "name": "Boards of Canada", "image": "https://x/a.jpg", "extra": "50000 fans"}
    mock_get.assert_called_once_with(
        "https://api.deezer.com/search/artist", params={"q": "Boards of Canada", "limit": 8}, timeout=10,
    )


def test_no_results_returns_empty_list():
    with patch("requests.get", return_value=_response({"data": []})):
        assert ss._search_service("deezer", "artist", "Nonexistent Artist XYZ") == []


def test_deezer_error_payload_raises_instead_of_silently_returning_empty():
    payload = {"error": {"type": "OAuthException", "message": "Quota limit exceeded", "code": 4}}
    with patch("requests.get", return_value=_response(payload)):
        with pytest.raises(ValueError, match="Quota limit exceeded"):
            ss._search_service("deezer", "artist", "Boards of Canada")


def test_http_error_status_propagates():
    with patch("requests.get", return_value=_response({}, status_code=503)):
        with pytest.raises(RuntimeError):
            ss._search_service("deezer", "artist", "Boards of Canada")


def test_network_exception_propagates_instead_of_being_swallowed():
    with patch("requests.get", side_effect=ConnectionError("no route to host")):
        with pytest.raises(ConnectionError):
            ss._search_service("deezer", "artist", "Boards of Canada")
