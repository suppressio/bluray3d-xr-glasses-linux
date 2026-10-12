"""dlna.py: the UPnP description/SCPD/control endpoints added in front of
http_server.py's plain file serving, over a real loopback server (SSDP is
not started here: it needs a multicast socket, out of scope for a unit
test, see test_ssdp.py for its message building)."""
import socket
import threading
from collections.abc import Iterator
from http.client import HTTPConnection
from pathlib import Path

import pytest

import didl
from dlna import MAX_SOAP_BODY, Dlna, DlnaServer, device_uuid, install_id, local_ip
from tree import Tree


def test_local_ip_returns_something_usable() -> None:
    ip = local_ip()
    assert isinstance(ip, str) and ip


def test_device_uuid_is_stable_and_specific() -> None:
    assert device_uuid("http://a") == device_uuid("http://a")
    assert device_uuid("http://a") != device_uuid("http://b")


def test_install_id_comes_from_the_machine_not_the_address(tmp_path: Path) -> None:
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("0123456789abcdef\n")
    assert install_id(str(machine_id)) == "0123456789abcdef"
    assert install_id(str(tmp_path / "missing")) == socket.gethostname()


def test_dlna_uuid_does_not_change_with_port() -> None:
    a, b = Dlna(Tree(), 0, host="127.0.0.1"), Dlna(Tree(), 0, host="127.0.0.1")
    try:
        assert a.server.device_uuid == b.server.device_uuid
    finally:
        a.server.server_close()
        b.server.server_close()


def test_start_rolls_back_http_when_ssdp_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    dlna = Dlna(Tree(), 0, host="127.0.0.1")

    def no_multicast() -> None:
        raise OSError("no route to multicast")

    monkeypatch.setattr(dlna.ssdp, "start", no_multicast)
    with pytest.raises(OSError, match="multicast"):
        dlna.start()
    assert dlna.server.socket.fileno() == -1          # HTTP server closed


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


def test_head_on_description_documents(server: DlnaServer) -> None:
    for path in ("/description.xml", "/ContentDirectory.xml", "/ConnectionManager.xml"):
        status, body = get(server, "HEAD", path)
        assert status == 200 and body == ""


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


def raw_post(server: DlnaServer, content_length: str) -> int:
    """A POST with a hand-written Content-Length (http.client would fix it)."""
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as s:
        s.sendall(b"POST /ctl/ContentDirectory HTTP/1.1\r\nHost: x\r\n"
                  b'SOAPAction: "x#Browse"\r\n'
                  + f"Content-Length: {content_length}\r\n\r\n".encode())
        return int(s.recv(1024).split(b" ", 2)[1])


@pytest.mark.parametrize(("content_length", "status"), [
    ("-1", 400), ("abc", 400), (str(MAX_SOAP_BODY + 1), 413), ("999999999999", 413),
    # beyond int()'s digit limit: 400, not a crashed handler
    pytest.param("9" * 5000, 400, id="5000-digits"),
])
def test_soap_body_length_is_checked_before_reading(server: DlnaServer, content_length: str,
                                                    status: int) -> None:
    assert raw_post(server, content_length) == status
