"""didl.py: DIDL-Lite/SOAP building and the tiny argument extractor, against
a small tree — no networking, dlna.py wires this onto the HTTP server."""
import re
from xml.sax.saxutils import unescape

import pytest

import didl
from tree import Tree


def didl_lite_of(response: str) -> str:
    """The <Result> argument of a Browse response, unescaped back to plain
    DIDL-Lite XML (it travels inside the SOAP body as escaped text)."""
    m = re.search(r"<Result>(.*?)</Result>", response, re.DOTALL)
    assert m
    return unescape(m.group(1))


def soap_body(**args: str) -> bytes:
    inner = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    return (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<s:Body><u:Browse xmlns:u="{didl.CONTENT_DIRECTORY}">{inner}</u:Browse>'
        "</s:Body></s:Envelope>"
    ).encode()


@pytest.fixture
def tree() -> Tree:
    tree = Tree()
    bluray = tree.folder("Blu-ray")
    movie_id = tree.new_id()

    class Movie:
        inode, parent, name, path = movie_id, bluray.inode, "ITA - x.ts", "Blu-ray/ITA - x.ts"
        size = 123

    tree.add(Movie())  # type: ignore[arg-type]
    return tree


def test_browse_root_lists_its_one_folder(tree: Tree) -> None:
    body = soap_body(ObjectID="0", BrowseFlag="BrowseDirectChildren")
    response = didl.browse(tree, body, "http://host:8200")
    didl_lite = didl_lite_of(response)
    assert "<container" in didl_lite and 'id="2"' in didl_lite
    assert "<dc:title>Blu-ray</dc:title>" in didl_lite
    assert "<NumberReturned>1</NumberReturned>" in response
    assert "<TotalMatches>1</TotalMatches>" in response


def test_browse_direct_children_lists_the_movie(tree: Tree) -> None:
    bluray_id = tree.folder("Blu-ray").inode
    body = soap_body(ObjectID=str(bluray_id), BrowseFlag="BrowseDirectChildren")
    response = didl.browse(tree, body, "http://host:8200")
    didl_lite = didl_lite_of(response)
    assert "<item" in didl_lite
    assert "ITA - x.ts" in didl_lite
    assert "http://host:8200/Blu-ray/ITA%20-%20x.ts" in didl_lite
    assert 'size="123"' in didl_lite


def test_browse_metadata_returns_the_object_itself(tree: Tree) -> None:
    bluray_id = tree.folder("Blu-ray").inode
    body = soap_body(ObjectID=str(bluray_id), BrowseFlag="BrowseMetadata")
    response = didl.browse(tree, body, "http://host:8200")
    didl_lite = didl_lite_of(response)
    assert "<NumberReturned>1</NumberReturned>" in response
    assert "<container" in didl_lite and "<item" not in didl_lite


def test_browse_pagination() -> None:
    tree = Tree()
    for i in range(5):
        tree.folder(f"f{i}")
    body = soap_body(ObjectID="0", BrowseFlag="BrowseDirectChildren", StartingIndex="1",
                      RequestedCount="2")
    response = didl.browse(tree, body, "http://host:8200")
    assert "<NumberReturned>2</NumberReturned>" in response
    assert "<TotalMatches>5</TotalMatches>" in response


def test_browse_unknown_object_raises_soap_error(tree: Tree) -> None:
    body = soap_body(ObjectID="9999", BrowseFlag="BrowseDirectChildren")
    with pytest.raises(didl.SoapError) as exc_info:
        didl.browse(tree, body, "http://host:8200")
    assert exc_info.value.code == 701


@pytest.mark.parametrize(("object_id", "flag", "code"), [
    ("0", "BrowseEverything", 402),       # not one of the two allowed values
    ("3", "BrowseDirectChildren", 710),   # the movie: a file has no children
])
def test_browse_rejects_invalid_requests(tree: Tree, object_id: str, flag: str,
                                         code: int) -> None:
    body = soap_body(ObjectID=object_id, BrowseFlag=flag)
    with pytest.raises(didl.SoapError) as exc_info:
        didl.browse(tree, body, "http://host:8200")
    assert exc_info.value.code == code


def test_handle_action_dispatches_on_soap_action_header(tree: Tree) -> None:
    body = soap_body(ObjectID="0", BrowseFlag="BrowseDirectChildren")
    response = didl.handle_action(f'"{didl.CONTENT_DIRECTORY}#Browse"', body, tree,
                                  "http://host:8200")
    assert "<Browse" in response or "BrowseResponse" in response


def test_handle_action_simple_actions() -> None:
    response = didl.handle_action(f'"{didl.CONNECTION_MANAGER}#GetProtocolInfo"', b"",
                                  Tree(), "http://host:8200")
    assert "http-get" in response


def test_handle_action_unknown_raises() -> None:
    with pytest.raises(didl.SoapError) as exc_info:
        didl.handle_action('"urn:x#NoSuchAction"', b"", Tree(), "http://host:8200")
    assert exc_info.value.code == 401


def test_soap_fault_is_well_formed_xml() -> None:
    fault = didl.soap_fault(701, "no such object")
    assert "<errorCode>701</errorCode>" in fault
    assert "no such object" in fault


def test_device_description_has_both_services() -> None:
    description = didl.device_description("bluray3d-xr", "abc-123")
    assert "<friendlyName>bluray3d-xr</friendlyName>" in description
    assert "uuid:abc-123" in description
    assert description.count("<service>") == 2
    assert didl.CONTENT_DIRECTORY in description
    assert didl.CONNECTION_MANAGER in description


@pytest.mark.parametrize("scpd", [didl.CONTENT_DIRECTORY_SCPD, didl.CONNECTION_MANAGER_SCPD])
def test_scpd_is_well_formed_enough(scpd: str) -> None:
    assert re.match(r'^<\?xml version="1\.0"\?><scpd ', scpd)
    assert scpd.count("<action>") == scpd.count("</action>")
    assert scpd.count("<argument>") == scpd.count("</argument>")


def browse_xml(tree: Tree, **args: str) -> str:
    return didl_lite_of(didl.browse(tree, soap_body(**args), "http://host:8200"))


def test_root_metadata_has_parent_minus_one(tree: Tree) -> None:
    xml = browse_xml(tree, ObjectID="0", BrowseFlag="BrowseMetadata")
    assert 'id="0" parentID="-1"' in xml


def test_sidecars_are_not_offered_to_renderers(tree: Tree) -> None:
    bluray = tree.folder("Blu-ray")
    sub_id = tree.new_id()

    class Subtitle:
        inode, parent, name, path = sub_id, bluray.inode, "ITA - x.srt", "Blu-ray/ITA - x.srt"
        size = 10

    tree.add(Subtitle())  # type: ignore[arg-type]
    xml = browse_xml(tree, ObjectID=str(bluray.inode), BrowseFlag="BrowseDirectChildren")
    assert "x.ts" in xml and ".srt" not in xml
    assert 'childCount="1"' in browse_xml(tree, ObjectID="0", BrowseFlag="BrowseDirectChildren")


@pytest.mark.parametrize("args", [
    {"StartingIndex": "abc"}, {"StartingIndex": "-1"}, {"RequestedCount": "1.5"},
    {"RequestedCount": "4294967296"},
])
def test_invalid_ui4_arguments_are_error_402(tree: Tree, args: dict[str, str]) -> None:
    with pytest.raises(didl.SoapError) as excinfo:
        didl.browse(tree, soap_body(ObjectID="0", **args), "http://host:8200")
    assert excinfo.value.code == 402


def test_update_id_follows_the_tree(tree: Tree) -> None:
    def update_ids() -> tuple[str, str]:
        browse = didl.browse(tree, soap_body(ObjectID="0"), "http://host:8200")
        system = didl.handle_action(f'"{didl.CONTENT_DIRECTORY}#GetSystemUpdateID"', b"",
                                    tree, "http://host:8200")
        b = re.search(r"<UpdateID>(\d+)</UpdateID>", browse)
        s = re.search(r"<Id>(\d+)</Id>", system)
        assert b and s
        return b.group(1), s.group(1)

    before = update_ids()
    assert before[0] == before[1]
    tree.folder("DVD")                       # a disc went in
    after = update_ids()
    assert after[0] == after[1] and int(after[0]) > int(before[0])


def test_no_eventing_is_advertised() -> None:
    description = didl.device_description("bluray3d-xr", "u")
    assert "<eventSubURL></eventSubURL>" in description and "/evt/" not in description
    for scpd in (didl.CONTENT_DIRECTORY_SCPD, didl.CONNECTION_MANAGER_SCPD):
        assert 'sendEvents="yes"' not in scpd
