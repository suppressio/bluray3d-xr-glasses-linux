"""ssdp.py: the message building/parsing SsdpResponder sends over its
multicast socket, tested on their own (no socket: see docs/DEVELOPMENT.md on
what needs real hardware/network, out of scope for unit tests)."""
import ssdp


def test_parse_request_m_search() -> None:
    data = (
        b"M-SEARCH * HTTP/1.1\r\n"
        b"HOST: 239.255.255.250:1900\r\n"
        b'MAN: "ssdp:discover"\r\n'
        b"ST: ssdp:all\r\n"
        b"MX: 3\r\n\r\n"
    )
    line, headers = ssdp.parse_request(data)
    assert line == "M-SEARCH * HTTP/1.1"
    assert headers == {"host": "239.255.255.250:1900", "man": '"ssdp:discover"',
                       "st": "ssdp:all", "mx": "3"}


def test_matching_targets_ssdp_all_matches_everything() -> None:
    targets = ["upnp:rootdevice", "uuid:x", "urn:schemas-upnp-org:device:MediaServer:1"]
    assert ssdp.matching_targets("ssdp:all", targets) == targets


def test_matching_targets_specific_st() -> None:
    targets = ["upnp:rootdevice", "uuid:x"]
    assert ssdp.matching_targets("uuid:x", targets) == ["uuid:x"]
    assert ssdp.matching_targets("urn:nope", targets) == []


def test_build_response_has_the_right_fields() -> None:
    response = ssdp.build_response("http://host:8200/description.xml", "upnp:rootdevice",
                                   "uuid:x::upnp:rootdevice", "server/1.0")
    text = response.decode()
    assert text.startswith("HTTP/1.1 200 OK\r\n")
    assert "LOCATION: http://host:8200/description.xml\r\n" in text
    assert "ST: upnp:rootdevice\r\n" in text
    assert "USN: uuid:x::upnp:rootdevice\r\n" in text
    assert "CACHE-CONTROL: max-age=1800\r\n" in text
    assert text.endswith("\r\n\r\n")


def test_build_notify_alive_vs_byebye() -> None:
    alive = ssdp.build_notify("http://host/d.xml", "uuid:x", "uuid:x", "server/1.0",
                              alive=True).decode()
    byebye = ssdp.build_notify("http://host/d.xml", "uuid:x", "uuid:x", "server/1.0",
                               alive=False).decode()
    assert alive.startswith("NOTIFY * HTTP/1.1\r\n")
    assert "NTS: ssdp:alive\r\n" in alive and "CACHE-CONTROL" in alive
    assert "NTS: ssdp:byebye\r\n" in byebye and "CACHE-CONTROL" not in byebye


def test_search_targets_covers_the_device_and_its_services() -> None:
    targets = ssdp.search_targets("abc", "urn:schemas-upnp-org:device:MediaServer:1",
                                   ["urn:schemas-upnp-org:service:ContentDirectory:1"])
    sts = [st for st, _ in targets]
    assert "upnp:rootdevice" in sts
    assert "uuid:abc" in sts
    assert "urn:schemas-upnp-org:device:MediaServer:1" in sts
    assert "urn:schemas-upnp-org:service:ContentDirectory:1" in sts
    root_usn = next(usn for st, usn in targets if st == "upnp:rootdevice")
    assert root_usn == "uuid:abc::upnp:rootdevice"
    uuid_usn = next(usn for st, usn in targets if st == "uuid:abc")
    assert uuid_usn == "uuid:abc"
