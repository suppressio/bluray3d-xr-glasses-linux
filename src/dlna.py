"""dlna.py — announces the tree as a UPnP MediaServer: SSDP (ssdp.py) so
renderers find it without being told its address, and the
ContentDirectory/ConnectionManager control endpoints (didl.py) added in
front of the same HTTP server that already serves the files
(http_server.py). A thin adapter on the shared tree, not a parallel
implementation: a renderer that browses instead of opening the SMB share
(a TV, Moon VR, Kodi...) gets to the same virtual files, over the same
Range-aware GET.
"""
import logging
import socket
import threading
import uuid as uuid_module
from typing import cast

import didl
from http_server import TreeHttpHandler, TreeHttpServer
from ssdp import SsdpResponder
from tree import Tree

log = logging.getLogger("dlna")

FRIENDLY_NAME = "bluray3d-xr"
SERVER_HEADER = "Linux/1.0 UPnP/1.0 bluray3d-xr/1.0"


class DlnaHandler(TreeHttpHandler):
    """The plain file/listing handler from http_server.py, with the UPnP
    description, SCPD and SOAP control endpoints added in front of it."""

    @property
    def dlna_server(self) -> "DlnaServer":
        return cast("DlnaServer", self.server)

    def do_GET(self) -> None:  # noqa: N802 (http.server's own naming: dispatched by command)
        server = self.dlna_server
        if self.path == "/description.xml":
            self._xml(didl.device_description(server.friendly_name, server.device_uuid))
        elif self.path == "/ContentDirectory.xml":
            self._xml(didl.CONTENT_DIRECTORY_SCPD)
        elif self.path == "/ConnectionManager.xml":
            self._xml(didl.CONNECTION_MANAGER_SCPD)
        else:
            super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path in ("/ctl/ContentDirectory", "/ctl/ConnectionManager"):
            self._soap()
        else:
            self.send_error(404)

    def _xml(self, body_text: str) -> None:
        self._respond(200, "text/xml; charset=utf-8", body_text.encode())

    def _soap(self) -> None:
        server = self.dlna_server
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        soap_action = self.headers.get("SOAPAction", "")
        try:
            response = didl.handle_action(soap_action, body, server.tree, server.base_url)
        except didl.SoapError as e:
            self._respond(500, "text/xml; charset=utf-8", didl.soap_fault(e.code, str(e)).encode())
            return
        self._respond(200, "text/xml; charset=utf-8", response.encode())

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DlnaServer(TreeHttpServer):
    def __init__(self, address: tuple[str, int], tree: Tree, friendly_name: str,
                 device_uuid: str, base_url: str) -> None:
        self.friendly_name = friendly_name
        self.device_uuid = device_uuid
        self.base_url = base_url
        super().__init__(address, tree, DlnaHandler)


def local_ip() -> str:
    """The address other devices on the LAN would reach this host at, found
    without sending anything (a UDP "connect" only picks the outbound route,
    it transmits nothing)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def device_uuid(seed: str) -> str:
    """Stable across restarts (renderers remember a device by its UUID), not
    random: derived from what identifies this install (its address)."""
    return str(uuid_module.uuid5(uuid_module.NAMESPACE_DNS, f"bluray3d-xr/{seed}"))


class Dlna:
    """Starts/stops the HTTP server (files + UPnP control) and the SSDP
    responder together: one lifecycle, since neither announces anything
    useful to a DLNA renderer without the other."""

    def __init__(self, tree: Tree, port: int, host: str | None = None,
                 friendly_name: str = FRIENDLY_NAME) -> None:
        self.host = host or local_ip()
        self.port = port
        base_url = f"http://{self.host}:{self.port}"
        uuid_value = device_uuid(base_url)
        self.server = DlnaServer((self.host, self.port), tree, friendly_name, uuid_value,
                                 base_url)
        self.ssdp = SsdpResponder(f"{base_url}/description.xml", uuid_value, didl.DEVICE_TYPE,
                                  [didl.CONTENT_DIRECTORY, didl.CONNECTION_MANAGER],
                                  SERVER_HEADER)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        self.ssdp.start()
        log.info("DLNA: %s announced at %s/description.xml", self.server.friendly_name,
                 self.server.base_url)

    def stop(self) -> None:
        self.ssdp.stop()
        self.server.shutdown()
        self.server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
