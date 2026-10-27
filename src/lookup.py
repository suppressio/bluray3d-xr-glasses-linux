"""
lookup.py — online episode lookup (--lookup, #84): names series episodes with
their real title instead of only "S01E05 - Show", from TheDiscDB: it
identifies the disc by a content hash (its own fingerprint: the size of every
stream file, see sources._content_hash) and maps each playlist straight to its
episode title. No API key.

Opt-in (off unless sources.py is told to use it) and resilient by design: a
network problem or an unknown disc both end the same way, an episode left
without an online title, so the caller's existing
offline naming (#27) is never broken. The caller logs the outcome; this module
only returns results or None.

Results are cached under `cache_dir`, keyed by the disc's content hash, so the
same disc is not queried again (not even to repeat a lookup that found
nothing), except after a network/data error, retried on the next insert: see
episode_titles().
"""
import http.client
import json
import logging
import urllib.request
from pathlib import Path
from typing import Any, cast

log = logging.getLogger("bd3d_fs")

TIMEOUT = 8.0  # seconds: a small JSON request on the LAN's uplink

# what a failed request or a malformed answer raises: a dropped connection
# (OSError), a response cut short (http.client.IncompleteRead), bad JSON
# (ValueError), an answer missing the expected fields (KeyError, TypeError)
LOOKUP_ERRORS = (OSError, http.client.HTTPException, ValueError, KeyError, TypeError)

THEDISCDB_URL = "https://thediscdb.com/graphql"
# TheDiscDB's schema (thediscdb.com): discs are looked up by their content hash,
# each title carries the clip it is played from (sourceFile, e.g. "00070.mpls")
# and, when catalogued, its season/episode/title.
THEDISCDB_QUERY = """
query DiscByHash($hash: String!) {
  discs(where: { contentHash: { eq: $hash } }) {
    nodes {
      titles {
        sourceFile
        item { title type }
      }
    }
  }
}
"""


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(  # noqa: S310 — fixed https URL
        url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 — fixed https URL
        result: dict[str, Any] = json.load(resp)
    return result


def _thediscdb_titles(content_hash: str, playlists: list[str]) -> dict[str, str] | None:
    """playlist -> episode title for a disc TheDiscDB knows; empty when it is not
    catalogued or has no title for any of our playlists; None on a network/data
    problem, so the caller does not cache it."""
    try:
        data = _post_json(THEDISCDB_URL,
                          {"query": THEDISCDB_QUERY, "variables": {"hash": content_hash}})
        nodes: list[dict[str, Any]] = data["data"]["discs"]["nodes"]
    except LOOKUP_ERRORS as e:
        log.info("TheDiscDB: lookup failed: %s", e)
        return None
    if not nodes:
        return {}
    wanted = set(playlists)
    titles: dict[str, str] = {}
    disc_titles: list[dict[str, Any]] = nodes[0].get("titles") or []
    for title in disc_titles:
        playlist = Path(str(title.get("sourceFile", ""))).stem
        item: dict[str, Any] = title.get("item") or {}
        name: str | None = item.get("title")
        if playlist in wanted and name:
            titles[playlist] = name
    return titles


def _cache_path(cache_dir: str, content_hash: str) -> Path:
    return Path(cache_dir) / f"{content_hash}.json"


def _load_cache(cache_dir: str, content_hash: str) -> dict[str, str] | None:
    try:
        data: dict[str, Any] = json.loads(_cache_path(cache_dir, content_hash).read_text())
    except (OSError, ValueError):
        return None
    titles: Any = data.get("titles")
    return cast(dict[str, str], titles) if isinstance(titles, dict) else None


def _save_cache(cache_dir: str, content_hash: str, titles: dict[str, str]) -> None:
    try:
        path = _cache_path(cache_dir, content_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"titles": titles}))
    except OSError as e:
        log.info("lookup cache: cannot write %s: %s", _cache_path(cache_dir, content_hash), e)


def episode_titles(cache_dir: str, content_hash: str, playlists: list[str]
                   ) -> list[str | None]:
    """The title of each episode (parallel to `playlists`) from TheDiscDB, None
    where it has none. The result (including "nothing found") is cached in
    `cache_dir` so the same disc is not queried again; not one left empty by a
    network/data error, which would otherwise stick for good."""
    cached = _load_cache(cache_dir, content_hash)
    if cached is not None:
        return [cached.get(p) for p in playlists]
    titles = _thediscdb_titles(content_hash, playlists)
    if titles is None:
        return [None] * len(playlists)
    _save_cache(cache_dir, content_hash, titles)
    return [titles.get(p) for p in playlists]
