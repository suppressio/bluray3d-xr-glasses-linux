"""dlna.py: the UPnP description/SCPD/control endpoints added in front of
http_server.py's plain file serving, over a real loopback server (SSDP is
not started here: it needs a multicast socket, out of scope for a unit
test, see test_ssdp.py for its message building)."""
import threading
from collections.abc import Iterator
from http.client import HTTPConnection

import pytest

import didl
from dlna import DlnaServer, device_uuid, local_ip
from tree import Tree


def test_local_ip_returns_something_usable() -> None:
    ip = local_ip()
    assert isinstance(ip, str) and ip


def test_device_uuid_is_stable_and_specific() -> None:
    assert device_uuid("http://a") == device_uuid("http://a")
    assert device_uuid("http://a") != device_uuid("http://b")


@pytest.fixture
def server() -> Iterator[DlnaServer]:
    tree = Tree()
    bluray = tree.folder("Blu-ray")
    movie_id = tree.new_id()

    class Movie:
        inode, parent, name, path, size = movie_id, bluray.inode, "x.ts", "Blu-ray/x.ts", 42

        def read(self, offset: int, size: int) -> bytes:
            return b"x" * min(size, self.size - offset)

    tree.add(Movie())  # type: ignore[arg-type]
    srv = DlnaServer(("127.0.0.1", 0), tree, "bluray3d-xr", "uuid-1", "http://127.0.0.1:8200")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def get(server: DlnaServer, method: str, path: str, body: bytes = b"",
       headers: dict[str, str] | None = None) -> tuple[int, str]:
    conn = HTTPConnection("127.0.0.1", server.server_address[1])
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    text = resp.read().decode()
    conn.close()
    return resp.status, text


def test_description_xml(server: DlnaServer) -> None:
    status, body = get(server, "GET", "/description.xml")
    assert status == 200
    assert "<friendlyName>bluray3d-xr</friendlyName>" in body
    assert "uuid:uuid-1" in body


def test_scpd_endpoints(server: DlnaServer) -> None:
    for path, expected in [("/ContentDirectory.xml", didl.CONTENT_DIRECTORY_SCPD),
                           ("/ConnectionManager.xml", didl.CONNECTION_MANAGER_SCPD)]:
        status, body = get(server, "GET", path)
        assert status == 200 and body == expected


def test_plain_files_still_served_through_the_same_handler(server: DlnaServer) -> None:
    status, body = get(server, "GET", "/Blu-ray/x.ts")
    assert status == 200 and body == "x" * 42


def test_browse_soap_control(server: DlnaServer) -> None:
    soap_body = (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<s:Body><u:Browse xmlns:u="{didl.CONTENT_DIRECTORY}">'
        "<ObjectID>0</ObjectID><BrowseFlag>BrowseDirectChildren</BrowseFlag>"
        "</u:Browse></s:Body></s:Envelope>"
    ).encode()
    status, body = get(server, "POST", "/ctl/ContentDirectory", soap_body,
                       {"SOAPAction": f'"{didl.CONTENT_DIRECTORY}#Browse"',
                        "Content-Length": str(len(soap_body))})
    assert status == 200
    assert "Blu-ray" in body


def test_soap_unknown_action_is_500(server: DlnaServer) -> None:
    body = b"<s:Envelope><s:Body></s:Body></s:Envelope>"
    status, text = get(server, "POST", "/ctl/ContentDirectory", body,
                       {"SOAPAction": '"urn:x#NoSuchAction"', "Content-Length": str(len(body))})
    assert status == 500
    assert "UPnPError" in text


def test_post_to_unknown_path_is_404(server: DlnaServer) -> None:
    status, _ = get(server, "POST", "/nope")
    assert status == 404
