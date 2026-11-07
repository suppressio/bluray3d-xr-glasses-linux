"""lookup.py (#84): the TheDiscDB client and the local cache, all against a fake
HTTP layer (urllib.request.urlopen) — no real network call is made by these tests."""
import http.client
import json
import logging
import urllib.error
from pathlib import Path
from typing import Any, Self, cast
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
        calls.append(req.get_header("User-agent", "") if isinstance(req, Request) else req)
        if error is not None:
            raise error
        return FakeResponse(thediscdb if thediscdb is not None else {})

    monkeypatch.setattr(lookup.urllib.request, "urlopen", urlopen)
    return calls


def answer(*discs: tuple[str, list[tuple[str, str | None]]]) -> dict[str, Any]:
    """TheDiscDB's answer, shaped as the live schema (#94): mediaItems ->
    releases -> discs -> titles, one release per (contentHash, [(sourceFile,
    item title)]) given; a title None is a file the catalogue has no item for."""
    return {"data": {"mediaItems": {"nodes": [{"releases": [
        {"discs": [{"contentHash": content_hash, "titles": [
            {"sourceFile": source, "item": {"title": name} if name else None}
            for source, name in titles]}]}
        for content_hash, titles in discs]}]}}}


DISC = answer(("HASH1", [("00070.mpls", "Pilot"), ("00071.mpls", "Lawnmower Dog"),
                         ("00072.mpls", None)]))
NO_DISC: dict[str, Any] = {"data": {"mediaItems": {"nodes": []}}}


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


@pytest.mark.parametrize("data", [
    {"data": {"mediaItems": "not an object"}},
    {"data": {"mediaItems": {"nodes": ["not an object"]}}},
    {"data": None},
    {"data": {"discs": {"nodes": []}}},       # the schema #84 assumed: no such field
])
def test_malformed_answer_falls_back_silently(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path, data: dict[str, Any]) -> None:
    fake_http(monkeypatch, thediscdb=data)
    assert episode_titles(str(tmp_path), "HASH4", ["00070"]) == [None]
    assert not list(tmp_path.glob("*.json"))  # retried next time, not cached


def test_graphql_errors_with_http_200_are_not_cached(monkeypatch: pytest.MonkeyPatch,
                                                     tmp_path: Path,
                                                     caplog: pytest.LogCaptureFixture) -> None:
    """GraphQL can answer HTTP 200 with "errors" (and data null, or partial):
    a failed lookup, logged and retried on the next insert, not "nothing found"."""
    errors = {"errors": [{"message": "The field `discs` does not exist on the type `Query`."}]}
    caplog.set_level(logging.INFO, logger="bd3d_fs")
    for data in ({**errors, "data": None}, {**errors, **DISC}):
        fake_http(monkeypatch, thediscdb=data)
        assert episode_titles(str(tmp_path), "HASH4", ["00070"]) == [None]
    assert "GraphQL errors" in caplog.text
    assert not list(tmp_path.glob("*.json"))


def test_http_error_is_not_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The live failure of #94: HTTP 400 for a query the schema rejects."""
    error = urllib.error.HTTPError(lookup.THEDISCDB_URL, 400, "Bad Request",
                                   http.client.HTTPMessage(), None)
    fake_http(monkeypatch, error=error)
    assert episode_titles(str(tmp_path), "HASH4", ["00070"]) == [None]
    assert not list(tmp_path.glob("*.json"))


def test_only_the_disc_with_our_hash_counts(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    """The matching show comes back with its releases: another disc of it (other
    season, same playlist numbers) must not name ours."""
    fake_http(monkeypatch, thediscdb=answer(("OTHER", [("00070.mpls", "Mortynight Run")]),
                                            ("HASH1", [("00070.mpls", "Pilot")])))
    assert episode_titles(str(tmp_path), "hash1", ["00070"]) == ["Pilot"]


def test_bare_clip_is_not_a_playlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A title played from a bare clip (00071.m2ts, an extra) does not name the
    playlist with the same number (00071.mpls): only .mpls sources map."""
    fake_http(monkeypatch, thediscdb=answer(("HASH1", [("00071.m2ts", "Behind The Scenes")])))
    assert episode_titles(str(tmp_path), "HASH1", ["00071"]) == [None]


def test_disc_in_two_releases(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The same disc catalogued twice (box set and single season): a title both
    agree on is kept, one they name differently is left out, not guessed."""
    fake_http(monkeypatch, thediscdb=answer(
        ("HASH1", [("00070.mpls", "Pilot"), ("00071.mpls", "Lawnmower Dog")]),
        ("HASH1", [("00070.mpls", "Pilot"), ("00071.mpls", "Lawnmower Dog (Uncut)")])))
    assert episode_titles(str(tmp_path), "HASH1", ["00070", "00071"]) == ["Pilot", None]


def test_query_is_the_live_schema_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path
                                        ) -> None:
    """#94: TheDiscDB has no root `discs` field (HTTP 400): the query starts at
    mediaItems and filters every level by the hash, which goes as a variable."""
    seen: list[dict[str, Any]] = []

    def urlopen(req: Request, timeout: float = 0) -> FakeResponse:
        assert timeout == lookup.TIMEOUT
        seen.append(json.loads(cast(bytes, req.data)))
        return FakeResponse(NO_DISC)

    monkeypatch.setattr(lookup.urllib.request, "urlopen", urlopen)
    episode_titles(str(tmp_path), "HASH9", ["00070"])
    query = seen[0]["query"]
    assert query.lstrip().startswith("query DiscByHash($hash: String!)")
    assert "mediaItems(where: { releases: { some: { discs: { some: { contentHash:" in query
    assert seen[0]["variables"] == {"hash": "HASH9"}


def test_cache_avoids_a_second_network_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path
                                            ) -> None:
    calls = fake_http(monkeypatch, thediscdb=DISC)
    first = episode_titles(str(tmp_path), "HASH1", ["00070"])
    second = episode_titles(str(tmp_path), "HASH1", ["00070"])
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
    episode_titles(str(tmp_path), "HASH1", ["00070"])
    assert not list(tmp_path.glob("*.json"))
    fake_http(monkeypatch, thediscdb=DISC)
    assert episode_titles(str(tmp_path), "HASH1", ["00070"]) == ["Pilot"]


def test_request_names_this_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The User-Agent says who is asking, so TheDiscDB can tell this traffic apart."""
    calls = fake_http(monkeypatch, thediscdb=NO_DISC)
    episode_titles(str(tmp_path), "HASH8", ["00070"])
    assert calls == [lookup.USER_AGENT]
