"""ssdp.py — announces the MediaServer on the LAN (SSDP: UPnP's discovery
half), so DLNA renderers find it without being told its address: answers
M-SEARCH and sends NOTIFY ssdp:alive/byebye. Pure message building/parsing
below, unit-tested on their own; SsdpResponder at the bottom is the only
part that touches a socket (a background thread, like the pipelines in
bd3d_fs.py), kept apart from the HTTP server (http_server.py, dlna.py) that
answers the description/control/media requests it advertises the location
of.
"""
import contextlib
import logging
import socket
import threading
import time

MULTICAST_ADDR = "239.255.255.250"
PORT = 1900
MAX_AGE = 1800            # seconds a response/NOTIFY says it is valid for

log = logging.getLogger("ssdp")


def parse_request(data: bytes) -> tuple[str, dict[str, str]]:
    """The request line and headers (lower-cased names) of an SSDP datagram,
    e.g. "M-SEARCH * HTTP/1.1" with an "ST" header."""
    lines = data.decode("utf-8", "replace").split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return (lines[0].strip() if lines else ""), headers


def matching_targets(st_header: str, search_targets: list[str]) -> list[str]:
    """Which of our targets (upnp:rootdevice, the device UUID, each service
    type) a M-SEARCH's ST asks for: "ssdp:all" asks for every one, a specific
    ST only the one that equals it."""
    if st_header == "ssdp:all":
        return search_targets
    return [st for st in search_targets if st == st_header]


def build_response(location: str, st: str, usn: str, server: str,
                    max_age: int = MAX_AGE) -> bytes:
    """A unicast reply to one M-SEARCH, for one matching search target."""
    return (
        "HTTP/1.1 200 OK\r\n"
        f"CACHE-CONTROL: max-age={max_age}\r\n"
        f"LOCATION: {location}\r\n"
        "EXT:\r\n"
        f"SERVER: {server}\r\n"
        f"ST: {st}\r\n"
        f"USN: {usn}\r\n"
        "\r\n"
    ).encode()


def build_notify(location: str, nt: str, usn: str, server: str, *, alive: bool,
                  max_age: int = MAX_AGE) -> bytes:
    """A multicast NOTIFY, unprompted: ssdp:alive (periodic, and at startup)
    or ssdp:byebye (on shutdown, so renderers drop it at once instead of
    waiting out max-age)."""
    nts = "ssdp:alive" if alive else "ssdp:byebye"
    extra = f"CACHE-CONTROL: max-age={max_age}\r\nSERVER: {server}\r\n" if alive else ""
    return (
        "NOTIFY * HTTP/1.1\r\n"
        f"HOST: {MULTICAST_ADDR}:{PORT}\r\n"
        f"{extra}"
        f"LOCATION: {location}\r\n"
        f"NT: {nt}\r\n"
        f"NTS: {nts}\r\n"
        f"USN: {usn}\r\n"
        "\r\n"
    ).encode()


def search_targets(device_uuid: str, device_type: str,
                    services: list[str]) -> list[tuple[str, str]]:
    """(ST/NT, USN) pairs this device must answer or announce, per the UPnP
    device architecture: the root device (as upnp:rootdevice, and by its own
    UUID), then one per service it offers."""
    uuid_usn = f"uuid:{device_uuid}"
    targets = [
        ("upnp:rootdevice", f"{uuid_usn}::upnp:rootdevice"),
        (uuid_usn, uuid_usn),
        (device_type, f"{uuid_usn}::{device_type}"),
    ]
    targets += [(s, f"{uuid_usn}::{s}") for s in services]
    return targets


class SsdpResponder:
    """Answers M-SEARCH and sends NOTIFY on a background thread. start()/
    stop() open and close the multicast socket; everything else above is
    plain message building, so the format is tested without a network."""

    def __init__(self, location: str, device_uuid: str, device_type: str, services: list[str],
                 server_header: str, notify_every: float = 600.0) -> None:
        self.location = location
        self.targets = search_targets(device_uuid, device_type, services)
        self.server_header = server_header
        self.notify_every = notify_every
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", PORT))
        # INADDR_ANY: join the multicast group on every interface, not a listening
        # bind (SSDP discovery must be heard on whichever interface the LAN is on)
        group = socket.inet_aton(MULTICAST_ADDR) + socket.inet_aton("0.0.0.0")  # noqa: S104
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group)
        sock.settimeout(1.0)
        self._sock = sock
        self._notify(alive=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _notify(self, *, alive: bool) -> None:
        sock = self._sock
        if sock is None:
            return
        for nt, usn in self.targets:
            message = build_notify(self.location, nt, usn, self.server_header, alive=alive)
            with contextlib.suppress(OSError):
                sock.sendto(message, (MULTICAST_ADDR, PORT))

    def _run(self) -> None:
        sock = self._sock
        if sock is None:
            return
        next_notify = time.monotonic() + self.notify_every
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except TimeoutError:
                data, addr = b"", None
            except OSError:
                break
            if data and addr is not None:
                self._handle(data, addr, sock)
            if time.monotonic() >= next_notify:
                self._notify(alive=True)
                next_notify = time.monotonic() + self.notify_every

    def _handle(self, data: bytes, addr: tuple[str, int], sock: socket.socket) -> None:
        line, headers = parse_request(data)
        if not line.startswith("M-SEARCH"):
            return
        all_targets = [st for st, _ in self.targets]
        for st in matching_targets(headers.get("st", ""), all_targets):
            usn = next(usn for target, usn in self.targets if target == st)
            response = build_response(self.location, st, usn, self.server_header)
            with contextlib.suppress(OSError):
                sock.sendto(response, addr)

    def stop(self) -> None:
        self._stop.set()
        self._notify(alive=False)
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
