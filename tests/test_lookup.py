"""lookup.py (#84): TheDiscDB and TMDB clients, duration matching and the local
cache, all against a fake HTTP layer (urllib.request.urlopen) — no real network
call is made by these tests."""
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
              tmdb_search: dict[str, Any] | None = None, tmdb_season: dict[str, Any] | None = None,
              fail: bool = False) -> list[str]:
    """Stands in for urllib.request.urlopen: routes a GraphQL POST to `thediscdb`,
    a TMDB show search to `tmdb_search`, a TMDB season request to `tmdb_season`.
    Returns the list of URLs/queries seen, for call-count assertions. `fail`:
    every call raises, as a dropped connection would."""
    calls: list[str] = []

    def urlopen(req: Request | str, timeout: float = 0) -> FakeResponse:
        if fail:
            raise OSError("connection refused")
        if isinstance(req, Request):
            calls.append("thediscdb")
            return FakeResponse(thediscdb if thediscdb is not None else {})
        calls.append(req)
        if "search/tv" in req:
            return FakeResponse(tmdb_search if tmdb_search is not None else {})
        return FakeResponse(tmdb_season if tmdb_season is not None else {})

    monkeypatch.setattr(lookup.urllib.request, "urlopen", urlopen)
    return calls


DISC: dict[str, Any] = {"data": {"discs": {"nodes": [{"titles": [
    {"sourceFile": "00070.mpls", "item": {"title": "Pilot"}},
    {"sourceFile": "00071.mpls", "item": {"title": "Lawnmower Dog"}},
]}]}}}
NO_DISC: dict[str, Any] = {"data": {"discs": {"nodes": []}}}
SEARCH: dict[str, Any] = {"results": [{"id": 1399}]}
SEASON: dict[str, Any] = {"episodes": [{"episode_number": 1, "name": "Pilot", "runtime": 42},
                                       {"episode_number": 2, "name": "Lawnmower Dog",
                                        "runtime": 45}]}


def test_thediscdb_match(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_http(monkeypatch, thediscdb=DISC)
    titles = episode_titles(str(tmp_path), "HASH1", ["00070", "00071"], [2520.0, 2700.0],
                            None, None)
    assert titles == ["Pilot", "Lawnmower Dog"]


def test_thediscdb_ignores_other_playlists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path
                                           ) -> None:
    """Only the playlists this disc's episodes actually use are returned: the
    catalogued disc may list titles (extras, alternates) the caller did not ask for."""
    fake_http(monkeypatch, thediscdb=DISC)
    titles = episode_titles(str(tmp_path), "HASH1", ["00071"], [2700.0], None, None)
    assert titles == ["Lawnmower Dog"]


def test_no_match_falls_back_silently(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_http(monkeypatch, thediscdb=NO_DISC)
    titles = episode_titles(str(tmp_path), "HASH2", ["00070"], [2520.0], None, None)
    assert titles == [None]


def test_network_error_falls_back_silently(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                           caplog: pytest.LogCaptureFixture) -> None:
    fake_http(monkeypatch, fail=True)
    caplog.set_level(logging.INFO, logger="bd3d_fs")
    titles = episode_titles(str(tmp_path), "HASH3", ["00070"], [2520.0], None, None)
    assert titles == [None]
    assert "lookup failed" in caplog.text


def test_tmdb_fallback_on_no_thediscdb_match(monkeypatch: pytest.MonkeyPatch, tmp_path: Path
                                             ) -> None:
    fake_http(monkeypatch, thediscdb=NO_DISC, tmdb_search=SEARCH, tmdb_season=SEASON)
    titles = episode_titles(str(tmp_path), "HASH4", ["00070", "00071"], [2520.0, 2700.0],
                            (1, "Rick and Morty"), "a-key")
    assert titles == ["Pilot", "Lawnmower Dog"]


def test_tmdb_skipped_without_key_or_season(monkeypatch: pytest.MonkeyPatch, tmp_path: Path
                                            ) -> None:
    calls = fake_http(monkeypatch, thediscdb=NO_DISC, tmdb_search=SEARCH, tmdb_season=SEASON)
    assert episode_titles(str(tmp_path), "HASH5", ["00070"], [2520.0], None, "a-key") == [None]
    assert episode_titles(str(tmp_path), "HASH6", ["00070"], [2520.0],
                          (1, "Show"), None) == [None]
    assert not any("search" in c or "season" in c for c in calls)


def test_tmdb_ambiguous_duration_not_matched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path
                                             ) -> None:
    """Two TMDB episodes within DURATION_TOLERANCE of the disc episode: no title is
    used for it rather than guessing (the issue's "only if the match is unique")."""
    close_pair: dict[str, Any] = {"episodes": [{"episode_number": 1, "name": "A", "runtime": 42},
                                               {"episode_number": 2, "name": "B", "runtime": 43}]}
    fake_http(monkeypatch, thediscdb=NO_DISC, tmdb_search=SEARCH, tmdb_season=close_pair)
    titles = episode_titles(str(tmp_path), "HASH7", ["00070"], [2520.0], (1, "Show"), "a-key")
    assert titles == [None]


def test_tmdb_show_not_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_http(monkeypatch, thediscdb=NO_DISC, tmdb_search={"results": []})
    titles = episode_titles(str(tmp_path), "HASH8", ["00070"], [2520.0], (1, "Show"), "a-key")
    assert titles == [None]


def test_cache_avoids_a_second_network_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path
                                            ) -> None:
    calls = fake_http(monkeypatch, thediscdb=DISC)
    cache_dir = str(tmp_path)
    first = episode_titles(cache_dir, "HASH9", ["00070"], [2520.0], None, None)
    before = len(calls)
    second = episode_titles(cache_dir, "HASH9", ["00070"], [2520.0], None, None)
    assert first == second == ["Pilot"]
    assert len(calls) == before          # no new HTTP call on the cache hit


def test_cache_also_remembers_no_match(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A disc TheDiscDB does not know must not be queried again either (#84:
    "same disc should not query the network again", success or not)."""
    calls = fake_http(monkeypatch, thediscdb=NO_DISC)
    cache_dir = str(tmp_path)
    episode_titles(cache_dir, "HASH10", ["00070"], [2520.0], None, None)
    before = len(calls)
    episode_titles(cache_dir, "HASH10", ["00070"], [2520.0], None, None)
    assert len(calls) == before


def test_no_content_hash_skips_cache_and_thediscdb(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path) -> None:
    """content_hash None (e.g. the disc's clips could not be read): TheDiscDB is
    skipped, nothing is cached, but TMDB can still run."""
    calls = fake_http(monkeypatch, tmdb_search=SEARCH, tmdb_season=SEASON)
    titles = episode_titles(str(tmp_path), None, ["00070"], [2520.0], (1, "Show"), "a-key")
    assert titles == ["Pilot"]
    assert "thediscdb" not in calls
    assert not list(Path(tmp_path).glob("*.json"))
