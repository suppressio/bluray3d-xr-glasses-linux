"""http_server.py — the same tree served over plain HTTP: for any player that
opens videos by URL instead of an SMB share, and as the transport dlna.py
rides its description, control and media URLs on (a thin adapter, not a
second copy of the serving logic).

The design that makes seeking work: every virtual file has a constant
bitrate, so a byte offset is also a point in time. An HTTP Range request
therefore maps 1:1 onto FileEntry.read(offset, size), exactly like a FUSE
read (bd3d_fs.py) — the player's jump restarts the pipeline from there, the
same way in both.
"""
import html
import logging
import re
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast, override
from urllib.parse import quote, unquote

from tree import Entry, FileEntry, Folder, Tree

log = logging.getLogger("http_server")

CHUNK = 1 << 20
_RANGE = re.compile(r"bytes=(?P<start>\d*)-(?P<end>\d*)")


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """"bytes=200-499" -> (200, 499); "bytes=200-" -> (200, size-1); "bytes=-500"
    (the last 500 bytes) -> (max(0, size-500), size-1). None for a header this
    function does not understand, or that does not fit the file: the caller
    then answers 416 (if a Range was sent) or the whole file (if it was not)."""
    m = _RANGE.fullmatch(header.split(",", maxsplit=1)[0].strip())  # one range: enough to seek
    if m is None or size <= 0 or not (m["start"] or m["end"]):
        return None  # also e.g. "bytes=abc-def": answered with 416 like any other bad range
    if not m["start"]:
        length = min(int(m["end"]), size)
        return (size - length, size - 1) if length > 0 else None
    start = int(m["start"])
    end = min(int(m["end"]), size - 1) if m["end"] else size - 1
    return (start, end) if start < size and end >= start else None


class TreeHttpHandler(BaseHTTPRequestHandler):
    """Serves one request: a folder as a plain HTML listing, a file with
    Range support. dlna.py subclasses this to add the UPnP endpoints in
    front of it."""

    @property
    def tree(self) -> Tree:
        return cast("TreeHttpServer", self.server).tree

    @override
    def log_message(self, format: str, *args: object) -> None:
        log.debug(format, *args)

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def _serve(self, *, send_body: bool) -> None:
        path = unquote(self.path.split("?", 1)[0])
        entry = self.tree.lookup(path)
        if entry is None:
            self.send_error(404)
            return
        if isinstance(entry, Folder):
            self._serve_listing(path, entry, send_body=send_body)
        else:
            self._serve_file(entry, send_body=send_body)

    def _serve_listing(self, path: str, folder: Folder, *, send_body: bool) -> None:
        children: Iterable[Entry] = sorted(self.tree.children_of(folder.inode),
                                           key=lambda e: e.name)
        rows = "".join(
            f'<li><a href="{quote(e.name)}{"/" if isinstance(e, Folder) else ""}">'
            f"{html.escape(e.name)}</a></li>"
            for e in children
        )
        title = html.escape(f"/{path}")
        body = f"<html><body><h1>{title}</h1><ul>{rows}</ul></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _serve_file(self, entry: FileEntry, *, send_body: bool) -> None:
        size = entry.size
        range_header = self.headers.get("Range")
        byte_range = parse_range(range_header, size) if range_header else None
        if range_header and byte_range is None:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        start, end = byte_range or (0, size - 1)
        length = max(0, end - start + 1)
        self.send_response(206 if byte_range else 200)
        self.send_header("Content-Type", "video/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if byte_range:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not send_body or length == 0:
            return
        offset = start
        try:
            while offset <= end:
                data = entry.read(offset, min(CHUNK, end - offset + 1))
                if not data:
                    break
                self.wfile.write(data)
                offset += len(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the player closed the connection (stopped, or jumped elsewhere)


class TreeHttpServer(ThreadingHTTPServer):
    """One thread per connection: players keep several open at once (playback
    + probing the end of the file), like the FUSE side already allows."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], tree: Tree,
                 handler: type[TreeHttpHandler] = TreeHttpHandler) -> None:
        self.tree = tree
        super().__init__(address, handler)
