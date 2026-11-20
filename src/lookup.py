"""
lookup.py — online episode lookup (--lookup, #84): names series episodes with
their real title instead of only "S01E05 - Show", from TheDiscDB: it
identifies the disc by a content hash (its own fingerprint: the size of every
stream file, see sources._content_hash) and says what each playlist is (main
movie, episode with its numbers, extra) and its title (#69). No API key.

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
import hashlib
import http.client
import json
import logging
import urllib.request
from dataclasses import dataclass
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
USER_AGENT = "spinema (+https://github.com/suppressio/spinema)"
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
          titles { sourceFile itemType season episode item { title } }
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


@dataclass(frozen=True)
class Role:
    """What TheDiscDB says a playlist is (#69): `kind` is its itemType ("MainMovie",
    "Episode", "Extra", "DeletedScene"...), None when it gives none; `title` the
    item's name; `season` and `episode` its numbers, None when not given."""
    kind: str | None
    title: str | None
    season: int | None
    episode: int | None


def content_hash(sizes: dict[str, int]) -> str | None:
    """TheDiscDB's fingerprint of a disc, the same for Blu-ray and DVD: MD5 of each
    file's size, one 8-byte little-endian integer per file in name order, hex
    uppercase (TheDiscDb/web: DiscScanner.cs picks the files, BDMV/STREAM/*.m2ts or
    every file in VIDEO_TS/; ClientApiController.Hash sorts them by name;
    HashingExtensions.cs hashes). None for no file."""
    if not sizes:
        return None
    digest = hashlib.md5(usedforsecurity=False)
    for name in sorted(sizes):
        digest.update(sizes[name].to_bytes(8, "little"))
    return digest.hexdigest().upper()


def _title_key(source_file: str) -> str | None:
    """How TheDiscDB names a title, as the caller names it: a Blu-ray playlist
    ("00070.mpls" -> "00070") or a DVD title number ("1" or "01" -> "01"); None for
    a bare .m2ts clip (an extra that may share its number with a playlist)."""
    path = Path(source_file)
    if path.suffix.lower() == ".mpls":
        return path.stem
    if source_file.isascii() and source_file.isdigit():
        return f"{int(source_file):02d}"
    return None


def _number(value: object) -> int | None:
    """A season/episode number: a string of ASCII digits, or an integer; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    return int(value) if isinstance(value, str) and value.isascii() and value.isdigit() else None


def _merge(roles: list[Role]) -> Role | None:
    """One playlist described by more than one release of the same disc (a box set
    and a single season): each field they all give alike is kept, one given by only
    some of them is kept too, one they give differently is left out (None) rather
    than guessed. Nothing left: None."""
    def agreed[T](values: list[T | None]) -> T | None:
        given = {v for v in values if v is not None}
        return next(iter(given)) if len(given) == 1 else None

    merged = Role(agreed([r.kind for r in roles]), agreed([r.title for r in roles]),
                  agreed([r.season for r in roles]), agreed([r.episode for r in roles]))
    return merged if merged.kind or merged.title else None


def disc_roles(content_hash: str, ids: list[str]) -> dict[str, Role]:
    """title -> Role for a disc TheDiscDB knows (Blu-ray or DVD), only for `ids`
    (Blu-ray playlists "00070", DVD title numbers "01") and only
    where it gives a name or a role; empty when it is not catalogued, or on a
    network/data problem (logged). The same disc can be catalogued in more than
    one release (a box set and a single season): a playlist they describe
    differently keeps only what they agree on (see _merge)."""
    try:
        data = _post_json(THEDISCDB_URL,
                          {"query": THEDISCDB_QUERY, "variables": {"hash": content_hash}})
        if data.get("errors"):  # GraphQL reports errors with HTTP 200 too
            raise ValueError(f"GraphQL errors: {data['errors']}")
        nodes: list[dict[str, Any]] = data["data"]["mediaItems"]["nodes"]
        discs = [disc for node in nodes for release in node["releases"]
                 for disc in release["discs"]
                 if str(disc["contentHash"]).upper() == content_hash.upper()]
        roles: dict[str, list[Role]] = {}
        for disc in discs:
            for title in disc["titles"]:
                key = _title_key(str(title.get("sourceFile") or ""))
                if key is None:
                    continue
                item: dict[str, Any] = title.get("item") or {}
                name: Any = item.get("title")
                kind: Any = title.get("itemType")
                role = Role(kind if isinstance(kind, str) and kind else None,
                            name if isinstance(name, str) and name else None,
                            _number(title.get("season")), _number(title.get("episode")))
                if role.kind or role.title:
                    roles.setdefault(key, []).append(role)
    except LOOKUP_ERRORS as e:
        log.info("TheDiscDB: lookup failed: %s", e)
        return {}
    merged = {p: _merge(roles[p]) for p in ids if p in roles}
    return {p: r for p, r in merged.items() if r is not None}
