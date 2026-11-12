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

No local cache: TheDiscDB answers in well under a second, once per disc
insert, so every insert asks again. A disc catalogued later, or a title
corrected there, is picked up on the next insert instead of a stale answer
staying for good.
"""
import http.client
import json
import logging
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("bd3d_fs")

TIMEOUT = 8.0  # seconds: a small JSON request on the LAN's uplink

# what a failed request or a malformed answer raises: a dropped connection
# (OSError), a response cut short (http.client.IncompleteRead), bad JSON
# (ValueError), an answer missing the expected fields or of another shape: a
# KeyError, TypeError or AttributeError
LOOKUP_ERRORS = (OSError, http.client.HTTPException, ValueError, KeyError, TypeError,
                 AttributeError)

THEDISCDB_URL = "https://thediscdb.com/graphql"
# who is asking, so TheDiscDB can tell this project's traffic apart and reach us
USER_AGENT = "bluray3d-xr (+https://github.com/suppressio/bluray3d-xr-glasses-linux)"
# TheDiscDB's schema (https://thediscdb.com/graphql, checked by introspection):
# no root field for discs, they hang off mediaItems -> releases -> discs. The
# same filter on every level keeps the answer to the matching disc only, instead
# of every release of the show. Each title carries the file it is played from
# (sourceFile: "00070.mpls" for a playlist, "00040.m2ts" for a bare clip) and,
# when catalogued, the item it is (episode, extra) with its title.
THEDISCDB_QUERY = """
query DiscByHash($hash: String!) {
  mediaItems(where: { releases: { some: { discs: { some: { contentHash: { eq: $hash } } } } } }) {
    nodes {
      releases(where: { discs: { some: { contentHash: { eq: $hash } } } }) {
        discs(where: { contentHash: { eq: $hash } }) {
          contentHash
          titles { sourceFile item { title } }
        }
      }
    }
  }
}
"""


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(  # noqa: S310 — fixed https URL
        url, data=body, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 — fixed https URL
        result: dict[str, Any] = json.load(resp)
    return result


def _thediscdb_titles(content_hash: str, playlists: list[str]) -> dict[str, str]:
    """playlist -> episode title for a disc TheDiscDB knows; empty when it is not
    catalogued, has no title for any of our playlists, or on a network/data
    problem (logged). The same disc can be catalogued in more than one release
    (a box set and a single season): a playlist they name differently is left
    without a title rather than given a guess."""
    try:
        data = _post_json(THEDISCDB_URL,
                          {"query": THEDISCDB_QUERY, "variables": {"hash": content_hash}})
        if data.get("errors"):  # GraphQL reports errors with HTTP 200 too
            raise ValueError(f"GraphQL errors: {data['errors']}")
        nodes: list[dict[str, Any]] = data["data"]["mediaItems"]["nodes"]
        discs = [disc for node in nodes for release in node["releases"]
                 for disc in release["discs"]
                 if str(disc["contentHash"]).upper() == content_hash.upper()]
        names: dict[str, set[str]] = {}
        for disc in discs:
            for title in disc["titles"]:
                source = Path(str(title.get("sourceFile") or ""))
                item: dict[str, Any] = title.get("item") or {}
                name: Any = item.get("title")
                # a bare .m2ts clip (an extra) may share its number with a playlist
                if source.suffix.lower() == ".mpls" and isinstance(name, str) and name:
                    names.setdefault(source.stem, set()).add(name)
    except LOOKUP_ERRORS as e:
        log.info("TheDiscDB: lookup failed: %s", e)
        return {}
    return {p: next(iter(names[p])) for p in playlists if len(names.get(p, ())) == 1}


def episode_titles(content_hash: str, playlists: list[str]) -> list[str | None]:
    """The title of each episode (parallel to `playlists`) from TheDiscDB, None
    where it has none: on no match and on a network/data problem alike."""
    titles = _thediscdb_titles(content_hash, playlists)
    return [titles.get(p) for p in playlists]
