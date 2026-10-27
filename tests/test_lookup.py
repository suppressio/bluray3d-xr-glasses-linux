"""lookup.py (#84): the TheDiscDB client and the local cache, all against a fake
HTTP layer (urllib.request.urlopen) — no real network call is made by these tests."""
import http.client
import json
import logging
from pathlib import Path
from typing import Any, Self
from urllib.request import Request

import pytest

import lookup
from lookup import episode_titles


class FakeResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = json.dumps(data).encode()

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def fake_http(monkeypatch: pytest.MonkeyPatch, thediscdb: dict[str, Any] | None = None,
              error: Exception | None = None) -> list[str]:
    """Stands in for urllib.request.urlopen: answers TheDiscDB's GraphQL POST with
    `thediscdb`, or raises `error` (a dropped connection, a response cut short).
    Returns the list of calls seen, for call-count assertions."""
    calls: list[str] = []

    def urlopen(req: Request | str, timeout: float = 0) -> FakeResponse:
        calls.append("thediscdb")
        if error is not None:
            raise error
        return FakeResponse(thediscdb if thediscdb is not None else {})

    monkeypatch.setattr(lookup.urllib.request, "urlopen", urlopen)
    return calls


DISC: dict[str, Any] = {"data": {"discs": {"nodes": [{"titles": [
    {"sourceFile": "00070.mpls", "item": {"title": "Pilot"}},
    {"sourceFile": "00071.mpls", "item": {"title": "Lawnmower Dog"}},
]}]}}}
NO_DISC: dict[str, Any] = {"data": {"discs": {"nodes": []}}}


def test_thediscdb_match(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_http(monkeypatch, thediscdb=DISC)
    assert episode_titles(str(tmp_path), "HASH1", ["00070", "00071"]) == \
        ["Pilot", "Lawnmower Dog"]


def test_thediscdb_ignores_other_playlists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path
                                           ) -> None:
    """Only the playlists this disc's episodes actually use are returned: the
    catalogued disc may list titles (extras, alternates) the caller did not ask for."""
    fake_http(monkeypatch, thediscdb=DISC)
    assert episode_titles(str(tmp_path), "HASH1", ["00071"]) == ["Lawnmower Dog"]


def test_no_match_falls_back_silently(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_http(monkeypatch, thediscdb=NO_DISC)
    assert episode_titles(str(tmp_path), "HASH2", ["00070"]) == [None]


@pytest.mark.parametrize("error", [OSError("connection refused"),
                                   http.client.IncompleteRead(b"{", 10)])
def test_network_error_falls_back_silently(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                           caplog: pytest.LogCaptureFixture,
                                           error: Exception) -> None:
    """A dropped connection, or a response cut short (http.client.IncompleteRead,
    not an OSError), ends in the offline naming, not in a failed disc."""
    fake_http(monkeypatch, error=error)
    caplog.set_level(logging.INFO, logger="bd3d_fs")
    assert episode_titles(str(tmp_path), "HASH3", ["00070"]) == [None]
    assert "lookup failed" in caplog.text


def test_malformed_answer_falls_back_silently(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path) -> None:
    fake_http(monkeypatch, thediscdb={"data": {"discs": "not an object"}})
    assert episode_titles(str(tmp_path), "HASH4", ["00070"]) == [None]


def test_cache_avoids_a_second_network_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path
                                            ) -> None:
    calls = fake_http(monkeypatch, thediscdb=DISC)
    first = episode_titles(str(tmp_path), "HASH5", ["00070"])
    second = episode_titles(str(tmp_path), "HASH5", ["00070"])
    assert first == second == ["Pilot"]
    assert len(calls) == 1               # no new HTTP call on the cache hit


def test_cache_also_remembers_no_match(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A disc TheDiscDB does not know must not be queried again either (#84:
    "same disc should not query the network again", success or not)."""
    calls = fake_http(monkeypatch, thediscdb=NO_DISC)
    episode_titles(str(tmp_path), "HASH6", ["00070"])
    episode_titles(str(tmp_path), "HASH6", ["00070"])
    assert len(calls) == 1


def test_network_error_is_not_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A lookup left empty by a network error is retried on the next insert,
    instead of being remembered as "nothing found" for good."""
    fake_http(monkeypatch, error=OSError("connection refused"))
    episode_titles(str(tmp_path), "HASH7", ["00070"])
    assert not list(tmp_path.glob("*.json"))
    fake_http(monkeypatch, thediscdb=DISC)
    assert episode_titles(str(tmp_path), "HASH7", ["00070"]) == ["Pilot"]
