"""
lookup.py — online episode lookup (--lookup, #84): names series episodes with
their real title instead of only "S01E05 - Show", using two sources in order:

  TheDiscDB  identifies the disc by a content hash (its own fingerprint: the
             size of every stream file, see sources._content_hash) and maps
             each playlist straight to its episode title. No API key.
  TMDB       fallback when TheDiscDB has nothing: the show's episode list for
             the season read offline (#27), matched to the disc's playlists by
             duration. Needs a free API key (--tmdb-key / TMDB_API_KEY).

Opt-in (off unless sources.py is told to use it) and resilient by design: a
network problem, an unknown disc, or an ambiguous duration match all end the
same way, an episode left without an online title, so the caller's existing
offline naming (#27) is never broken. The caller logs the outcome; this module
only returns results or None.

Results are cached under `cache_dir`, keyed by the disc's content hash, so the
same disc is not queried again (not even to repeat a lookup that found
nothing): see episode_titles().
"""
import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, cast

log = logging.getLogger("bd3d_fs")

TIMEOUT = 8.0  # seconds: both APIs are small JSON requests on the LAN's uplink

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

TMDB_BASE = "https://api.themoviedb.org/3"
# episodes vary a little in opening/closing credits and recaps; a disc episode
# is matched to a TMDB one only within this many seconds, and only when exactly
# one candidate falls inside it (an ambiguous match is left alone, see below)
DURATION_TOLERANCE = 90.0


def _get_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(full_url, timeout=TIMEOUT) as resp:  # noqa: S310 — fixed https URL
        result: dict[str, Any] = json.load(resp)
    return result


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(  # noqa: S310 — fixed https URL
        url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 — fixed https URL
        result: dict[str, Any] = json.load(resp)
    return result


def _thediscdb_titles(content_hash: str, playlists: list[str]) -> dict[str, str] | None:
    """playlist -> episode title for a disc TheDiscDB knows, or None (not
    catalogued, no title for any of our playlists, or a network/data problem)."""
    try:
        data = _post_json(THEDISCDB_URL,
                          {"query": THEDISCDB_QUERY, "variables": {"hash": content_hash}})
        nodes: list[dict[str, Any]] = data["data"]["discs"]["nodes"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.info("TheDiscDB: lookup failed: %s", e)
        return None
    if not nodes:
        return None
    wanted = set(playlists)
    titles: dict[str, str] = {}
    disc_titles: list[dict[str, Any]] = nodes[0].get("titles") or []
    for title in disc_titles:
        playlist = Path(str(title.get("sourceFile", ""))).stem
        item: dict[str, Any] = title.get("item") or {}
        name: str | None = item.get("title")
        if playlist in wanted and name:
            titles[playlist] = name
    return titles or None


def _tmdb_titles(show: str, season: int, playlists: list[str], durations: list[float],
                 api_key: str) -> dict[str, str] | None:
    """playlist -> episode title matched by duration against `show`'s season
    `season` on TMDB, or None (show/season not found, no episode has a runtime,
    or a network/data problem). A playlist is matched only when exactly one of
    the season's episodes falls within DURATION_TOLERANCE of its duration."""
    try:
        search = _get_json(f"{TMDB_BASE}/search/tv", {"api_key": api_key, "query": show})
        results: list[dict[str, Any]] = search.get("results") or []
        if not results:
            return None
        season_json = _get_json(f"{TMDB_BASE}/tv/{results[0]['id']}/season/{season}",
                                {"api_key": api_key})
        episodes: list[dict[str, Any]] = season_json.get("episodes") or []
        candidates: list[tuple[float, str]] = [
            (ep["runtime"] * 60, ep.get("name", "")) for ep in episodes if ep.get("runtime")]
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.info("TMDB: lookup failed: %s", e)
        return None
    titles: dict[str, str] = {}
    for playlist, duration in zip(playlists, durations, strict=True):
        near = [name for runtime, name in candidates
               if abs(duration - runtime) <= DURATION_TOLERANCE]
        if len(near) == 1:
            titles[playlist] = near[0]
    return titles or None


def _cache_path(cache_dir: str, content_hash: str) -> Path:
    return Path(cache_dir) / f"{content_hash}.json"


def _load_cache(cache_dir: str, content_hash: str) -> dict[str, str] | None:
    try:
        data: dict[str, Any] = json.loads(_cache_path(cache_dir, content_hash).read_text())
    except (OSError, ValueError):
        return None
    titles: Any = data.get("titles")
    return cast(dict[str, str], titles) if isinstance(titles, dict) else None


def _save_cache(cache_dir: str, content_hash: str, source: str, titles: dict[str, str]) -> None:
    try:
        path = _cache_path(cache_dir, content_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"source": source, "titles": titles}))
    except OSError as e:
        log.info("lookup cache: cannot write %s: %s", _cache_path(cache_dir, content_hash), e)


def episode_titles(cache_dir: str, content_hash: str | None, playlists: list[str],
                   durations: list[float], season_show: tuple[int, str] | None,
                   tmdb_key: str | None) -> list[str | None]:
    """The title of each episode (parallel to `playlists`/`durations`), from
    TheDiscDB first (needs `content_hash`), then TMDB if that found nothing and
    `season_show` (season, show) is known and `tmdb_key` is set; None where
    neither finds one. A `content_hash` result (including "nothing found") is
    cached in `cache_dir` so the same disc is not queried again."""
    if content_hash:
        cached = _load_cache(cache_dir, content_hash)
        if cached is not None:
            return [cached.get(p) for p in playlists]

    titles = _thediscdb_titles(content_hash, playlists) if content_hash else None
    source = "thediscdb"
    if titles is None and season_show is not None and tmdb_key:
        season, show = season_show
        titles = _tmdb_titles(show, season, playlists, durations, tmdb_key)
        source = "tmdb"
    if titles is None:
        titles, source = {}, "none"

    if content_hash:
        _save_cache(cache_dir, content_hash, source, titles)
    return [titles.get(p) for p in playlists]
