"""http_server.py: parse_range() on its own, then a real loopback server (the
same TreeHttpServer dlna.py and bd3d_fs.py start) exercised with real HTTP
requests: a byte Range maps straight onto FileEntry.read(), as it does
through FUSE."""
import threading
from collections.abc import Iterator
from http.client import HTTPConnection

import pytest

from http_server import TreeHttpServer, parse_range
from tree import Tree


@pytest.mark.parametrize(("header", "size", "expected"), [
    ("bytes=0-99", 1000, (0, 99)),
    ("bytes=900-", 1000, (900, 999)),
    ("bytes=900-10000", 1000, (900, 999)),             # end clamped to the file
    ("bytes=-100", 1000, (900, 999)),                  # the last 100 bytes
    ("bytes=-10000", 1000, (0, 999)),                  # more than the file: the whole thing
    ("bytes=0-0", 1000, (0, 0)),
])
def test_parse_range_satisfiable(header: str, size: int, expected: tuple[int, int]) -> None:
    assert parse_range(header, size) == expected


@pytest.mark.parametrize(("header", "size"), [
    ("bytes=1000-", 1000),            # starts past the end
    ("bytes=-0", 1000),               # the last zero bytes: nothing to serve
    ("not-bytes=0-10", 1000),         # a unit this function does not understand
    ("bytes=0-10", 0),                # an empty file has no satisfiable range
])
def test_parse_range_unsatisfiable(header: str, size: int) -> None:
    assert parse_range(header, size) is None


class FakeFile:
    """A FileEntry whose bytes are just the offset repeated: read(o, n) must
    equal bytes([o % 256]) * n, so a response's content can be checked byte
    for byte against where it claims to start."""

    def __init__(self, inode: int, name: str, path: str, size: int, parent: int = 1) -> None:
        self.inode, self.parent, self.name, self.path = inode, parent, name, path
        self.size, self.mtime_ns = size, 0

    def read(self, offset: int, size: int) -> bytes:
        return bytes((offset + i) % 256 for i in range(min(size, self.size - offset)))

    def stop(self) -> None:
        pass

    def stop_if_idle(self) -> None:
        pass


@pytest.fixture
def server() -> Iterator[tuple[TreeHttpServer, Tree]]:
    tree = Tree()
    folder = tree.folder("Blu-ray")
    tree.add(FakeFile(tree.new_id(), "movie.ts", "Blu-ray/movie.ts", 10_000, folder.inode))
    srv = TreeHttpServer(("127.0.0.1", 0), tree)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, tree
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_whole_file(server: tuple[TreeHttpServer, Tree]) -> None:
    srv, _ = server
    conn = HTTPConnection("127.0.0.1", srv.server_address[1])
    conn.request("GET", "/Blu-ray/movie.ts")
    resp = conn.getresponse()
    body = resp.read()
    assert resp.status == 200
    assert resp.getheader("Content-Length") == "10000"
    assert resp.getheader("Accept-Ranges") == "bytes"
    assert len(body) == 10_000 and body[:3] == bytes([0, 1, 2])
    conn.close()


def test_range_maps_to_a_read_at_that_offset(server: tuple[TreeHttpServer, Tree]) -> None:
    srv, _ = server
    conn = HTTPConnection("127.0.0.1", srv.server_address[1])
    conn.request("GET", "/Blu-ray/movie.ts", headers={"Range": "bytes=5000-5099"})
    resp = conn.getresponse()
    body = resp.read()
    assert resp.status == 206
    assert resp.getheader("Content-Range") == "bytes 5000-5099/10000"
    assert body == bytes((5000 + i) % 256 for i in range(100))
    conn.close()


def test_unsatisfiable_range_is_416(server: tuple[TreeHttpServer, Tree]) -> None:
    srv, _ = server
    conn = HTTPConnection("127.0.0.1", srv.server_address[1])
    conn.request("GET", "/Blu-ray/movie.ts", headers={"Range": "bytes=50000-60000"})
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 416
    assert resp.getheader("Content-Range") == "bytes */10000"
    conn.close()


def test_head_has_no_body(server: tuple[TreeHttpServer, Tree]) -> None:
    srv, _ = server
    conn = HTTPConnection("127.0.0.1", srv.server_address[1])
    conn.request("HEAD", "/Blu-ray/movie.ts")
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Length") == "10000"
    assert resp.read() == b""
    conn.close()


def test_missing_path_is_404(server: tuple[TreeHttpServer, Tree]) -> None:
    srv, _ = server
    conn = HTTPConnection("127.0.0.1", srv.server_address[1])
    conn.request("GET", "/nope")
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 404
    conn.close()


def test_folder_listing(server: tuple[TreeHttpServer, Tree]) -> None:
    srv, _ = server
    conn = HTTPConnection("127.0.0.1", srv.server_address[1])
    conn.request("GET", "/Blu-ray/")
    resp = conn.getresponse()
    body = resp.read().decode()
    assert resp.status == 200 and "movie.ts" in body
    conn.close()
