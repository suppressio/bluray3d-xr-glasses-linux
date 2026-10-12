"""didl.py — DIDL-Lite (the XML UPnP uses to list media) and the SOAP
envelopes of the two mandatory MediaServer services: ContentDirectory
(Browse: the folders and files) and ConnectionManager (GetProtocolInfo and
the connection-id stubs most renderers probe for before they trust a
server). Pure string building plus a tiny regex extractor for the handful of
flat arguments these actions take: no XML parser on requests from the LAN
(see AGENTS.md on keeping dependencies to what the system already provides;
a full parser is also needless attack surface for a SOAP body that is never
more than a few flat tags).
"""
import re
from collections.abc import Callable
from urllib.parse import quote
from xml.sax.saxutils import escape

from tree import FileEntry, Folder, Tree

DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaServer:1"
CONTENT_DIRECTORY = "urn:schemas-upnp-org:service:ContentDirectory:1"
CONNECTION_MANAGER = "urn:schemas-upnp-org:service:ConnectionManager:1"


class SoapError(Exception):
    """A UPnP control error: code is the standard UPnPError errorCode."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _arg(body: bytes, name: str) -> str:
    """The text of a flat SOAP argument, e.g. <ObjectID>0</ObjectID>, with or
    without a namespace prefix. "" if absent: every action here treats a
    missing argument as its default."""
    text = body.decode("utf-8", "replace")
    m = re.search(rf"<(?:[\w.]+:)?{name}[^>]*>(.*?)</(?:[\w.]+:)?{name}>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


# what a renderer can play: the virtual movies. Sidecars (external subtitles
# .srt/.ass/.sup...) stay on the Samba share, where players pick them up next
# to the movie; over DLNA they would show up as unplayable "videos"
VIDEO_SUFFIXES = (".ts", ".m2ts", ".mkv", ".mp4")


def _object_id(tree: Tree, inode: int) -> str:
    """UPnP wants the root container's id to be literally "0"."""
    return "0" if inode == tree.root.inode else str(inode)


def _parent_id(tree: Tree, node: Folder | FileEntry) -> str:
    """The root container has no parent: "-1" (ContentDirectory spec)."""
    return "-1" if node.inode == tree.root.inode else _object_id(tree, node.parent)


def _browsable(tree: Tree, inode: int) -> list[Folder | FileEntry]:
    """The children a renderer is shown: folders and videos, by name."""
    return sorted((e for e in tree.children_of(inode)
                   if isinstance(e, Folder) or e.name.lower().endswith(VIDEO_SUFFIXES)),
                  key=lambda e: e.name)


def _ui4(body: bytes, name: str) -> int:
    """A UPnP ui4 argument (absent = 0); anything else is error 402."""
    text = _arg(body, name) or "0"
    # ASCII and the length first: int() refuses (ValueError) Unicode digits
    # like "²" and digit strings over ~4300 digits
    if not (text.isascii() and text.isdigit()) or len(text) > 10 or int(text) > 0xFFFFFFFF:
        raise SoapError(402, f"invalid {name}: {text[:20]}")
    return int(text)


def _soap_response(service: str, action: str, args: dict[str, str]) -> str:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action}Response xmlns:u="{service}">{body}</u:{action}Response>'
        "</s:Body></s:Envelope>"
    )


def soap_fault(code: int, message: str) -> str:
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body><s:Fault>"
        "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>"
        '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
        f"<errorCode>{code}</errorCode>"
        f"<errorDescription>{escape(message)}</errorDescription>"
        "</UPnPError></detail>"
        "</s:Fault></s:Body></s:Envelope>"
    )


def _container_xml(tree: Tree, folder: Folder) -> str:
    title = escape(folder.name or "bluray3d-xr")
    child_count = len(_browsable(tree, folder.inode))
    return (
        f'<container id="{_object_id(tree, folder.inode)}" '
        f'parentID="{_parent_id(tree, folder)}" restricted="1" searchable="0" '
        f'childCount="{child_count}">'
        f"<dc:title>{title}</dc:title>"
        "<upnp:class>object.container.storageFolder</upnp:class>"
        "</container>"
    )


def _item_xml(tree: Tree, entry: FileEntry, base_url: str) -> str:
    title = escape(entry.name)
    url = escape(f"{base_url}/{quote(entry.path)}")
    return (
        f'<item id="{_object_id(tree, entry.inode)}" '
        f'parentID="{_object_id(tree, entry.parent)}" restricted="1">'
        f"<dc:title>{title}</dc:title>"
        "<upnp:class>object.item.videoItem</upnp:class>"
        f'<res protocolInfo="http-get:*:video/mpeg:*" size="{entry.size}">{url}</res>'
        "</item>"
    )


def browse(tree: Tree, body: bytes, base_url: str) -> str:
    """The Browse action: BrowseMetadata (one object) or BrowseDirectChildren
    (its children, paginated) — the only way these renderers discover the
    virtual files, since they do not open the share."""
    object_id = _arg(body, "ObjectID") or "0"
    flag = _arg(body, "BrowseFlag") or "BrowseDirectChildren"
    starting = _ui4(body, "StartingIndex")
    requested = _ui4(body, "RequestedCount")
    try:
        inode = tree.root.inode if object_id in ("0", "") else int(object_id)
        node = tree.nodes[inode]
    except (KeyError, ValueError):
        raise SoapError(701, f"no such object: {object_id}") from None
    if flag not in {"BrowseMetadata", "BrowseDirectChildren"}:
        raise SoapError(402, f"invalid BrowseFlag: {flag}")
    if flag == "BrowseMetadata":
        items, total = [node], 1
    elif not isinstance(node, Folder):
        raise SoapError(710, f"not a container: {object_id}")
    else:
        children = _browsable(tree, inode)
        total = len(children)
        end = len(children) if requested == 0 else starting + requested
        items = children[starting:end]
    result_xml = "".join(
        _container_xml(tree, e) if isinstance(e, Folder) else _item_xml(tree, e, base_url)
        for e in items
    )
    didl_lite = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">' + result_xml + "</DIDL-Lite>"
    )
    return _soap_response(CONTENT_DIRECTORY, "Browse", {
        "Result": escape(didl_lite),
        "NumberReturned": str(len(items)),
        "TotalMatches": str(total),
        "UpdateID": str(tree.revision),
    })


def get_search_capabilities() -> str:
    return _soap_response(CONTENT_DIRECTORY, "GetSearchCapabilities", {"SearchCaps": ""})


def get_sort_capabilities() -> str:
    return _soap_response(CONTENT_DIRECTORY, "GetSortCapabilities", {"SortCaps": ""})


def get_system_update_id(tree: Tree) -> str:
    return _soap_response(CONTENT_DIRECTORY, "GetSystemUpdateID", {"Id": str(tree.revision)})


def get_protocol_info() -> str:
    return _soap_response(CONNECTION_MANAGER, "GetProtocolInfo", {
        "Source": "http-get:*:video/mpeg:*", "Sink": "",
    })


def get_current_connection_ids() -> str:
    return _soap_response(CONNECTION_MANAGER, "GetCurrentConnectionIDs", {
        "ConnectionIDs": "0",
    })


def get_current_connection_info() -> str:
    return _soap_response(CONNECTION_MANAGER, "GetCurrentConnectionInfo", {
        "RcsID": "-1", "AVTransportID": "-1", "ProtocolInfo": "",
        "PeerConnectionManager": "", "PeerConnectionID": "-1",
        "Direction": "Output", "Status": "Unknown",
    })


# every action but Browse takes no argument worth reading: a renderer calls
# them to decide whether to trust this server, not to configure anything
_SIMPLE_ACTIONS: dict[str, Callable[[], str]] = {
    "GetSearchCapabilities": get_search_capabilities,
    "GetSortCapabilities": get_sort_capabilities,
    "GetProtocolInfo": get_protocol_info,
    "GetCurrentConnectionIDs": get_current_connection_ids,
    "GetCurrentConnectionInfo": get_current_connection_info,
}


def handle_action(soap_action: str, body: bytes, tree: Tree, base_url: str) -> str:
    """soap_action: the SOAPAction header, e.g. '"urn:...:ContentDirectory:1#Browse"'."""
    action = soap_action.strip('"').rsplit("#", 1)[-1]
    if action == "Browse":
        return browse(tree, body, base_url)
    if action == "GetSystemUpdateID":
        return get_system_update_id(tree)
    simple = _SIMPLE_ACTIONS.get(action)
    if simple is None:
        raise SoapError(401, f"no such action: {action}")
    return simple()


def device_description(friendly_name: str, udn: str) -> str:
    """The UPnP device description: one MediaServer with ContentDirectory
    (the folders and files) and ConnectionManager (mandatory, even though
    http-get needs no real connection setup). No GENA eventing: eventSubURL
    is empty and no state variable is evented, so renderers poll
    GetSystemUpdateID / UpdateID instead of subscribing."""
    return (
        '<?xml version="1.0"?>'
        '<root xmlns="urn:schemas-upnp-org:device-1-0">'
        "<specVersion><major>1</major><minor>0</minor></specVersion>"
        "<device>"
        f"<deviceType>{DEVICE_TYPE}</deviceType>"
        f"<friendlyName>{escape(friendly_name)}</friendlyName>"
        "<manufacturer>bluray3d-xr</manufacturer>"
        "<modelName>bluray3d-xr</modelName>"
        f"<UDN>uuid:{udn}</UDN>"
        "<serviceList>"
        "<service>"
        f"<serviceType>{CONTENT_DIRECTORY}</serviceType>"
        "<serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>"
        "<SCPDURL>/ContentDirectory.xml</SCPDURL>"
        "<controlURL>/ctl/ContentDirectory</controlURL>"
        "<eventSubURL></eventSubURL>"
        "</service>"
        "<service>"
        f"<serviceType>{CONNECTION_MANAGER}</serviceType>"
        "<serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>"
        "<SCPDURL>/ConnectionManager.xml</SCPDURL>"
        "<controlURL>/ctl/ConnectionManager</controlURL>"
        "<eventSubURL></eventSubURL>"
        "</service>"
        "</serviceList>"
        "</device></root>"
    )


CONTENT_DIRECTORY_SCPD = (
    '<?xml version="1.0"?>'
    '<scpd xmlns="urn:schemas-upnp-org:service-1-0">'
    "<specVersion><major>1</major><minor>0</minor></specVersion>"
    "<actionList>"
    "<action><name>Browse</name><argumentList>"
    "<argument><name>ObjectID</name><direction>in</direction>"
    "<relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>"
    "<argument><name>BrowseFlag</name><direction>in</direction>"
    "<relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>"
    "<argument><name>Filter</name><direction>in</direction>"
    "<relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>"
    "<argument><name>StartingIndex</name><direction>in</direction>"
    "<relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>"
    "<argument><name>RequestedCount</name><direction>in</direction>"
    "<relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>"
    "<argument><name>SortCriteria</name><direction>in</direction>"
    "<relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>"
    "<argument><name>Result</name><direction>out</direction>"
    "<relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>"
    "<argument><name>NumberReturned</name><direction>out</direction>"
    "<relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>"
    "<argument><name>TotalMatches</name><direction>out</direction>"
    "<relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>"
    "<argument><name>UpdateID</name><direction>out</direction>"
    "<relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>"
    "</argumentList></action>"
    "<action><name>GetSearchCapabilities</name><argumentList>"
    "<argument><name>SearchCaps</name><direction>out</direction>"
    "<relatedStateVariable>SearchCapabilities</relatedStateVariable></argument>"
    "</argumentList></action>"
    "<action><name>GetSortCapabilities</name><argumentList>"
    "<argument><name>SortCaps</name><direction>out</direction>"
    "<relatedStateVariable>SortCapabilities</relatedStateVariable></argument>"
    "</argumentList></action>"
    "<action><name>GetSystemUpdateID</name><argumentList>"
    "<argument><name>Id</name><direction>out</direction>"
    "<relatedStateVariable>SystemUpdateID</relatedStateVariable></argument>"
    "</argumentList></action>"
    "</actionList>"
    "<serviceStateTable>"
    "<stateVariable><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>"
    "<stateVariable><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>"
    "<stateVariable><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType>"
    "<allowedValueList><allowedValue>BrowseMetadata</allowedValue>"
    "<allowedValue>BrowseDirectChildren</allowedValue></allowedValueList>"
    "</stateVariable>"
    "<stateVariable><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>"
    "<stateVariable><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType>"
    "</stateVariable>"
    "<stateVariable><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>"
    "<stateVariable><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>"
    "<stateVariable><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>"
    '<stateVariable sendEvents="no"><name>SearchCapabilities</name>'
    "<dataType>string</dataType></stateVariable>"
    '<stateVariable sendEvents="no"><name>SortCapabilities</name>'
    "<dataType>string</dataType></stateVariable>"
    '<stateVariable sendEvents="no"><name>SystemUpdateID</name>'
    "<dataType>ui4</dataType></stateVariable>"
    "</serviceStateTable>"
    "</scpd>"
)

CONNECTION_MANAGER_SCPD = (
    '<?xml version="1.0"?>'
    '<scpd xmlns="urn:schemas-upnp-org:service-1-0">'
    "<specVersion><major>1</major><minor>0</minor></specVersion>"
    "<actionList>"
    "<action><name>GetProtocolInfo</name><argumentList>"
    "<argument><name>Source</name><direction>out</direction>"
    "<relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>"
    "<argument><name>Sink</name><direction>out</direction>"
    "<relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>"
    "</argumentList></action>"
    "<action><name>GetCurrentConnectionIDs</name><argumentList>"
    "<argument><name>ConnectionIDs</name><direction>out</direction>"
    "<relatedStateVariable>CurrentConnectionIDs</relatedStateVariable></argument>"
    "</argumentList></action>"
    "<action><name>GetCurrentConnectionInfo</name><argumentList>"
    "<argument><name>ConnectionID</name><direction>in</direction>"
    "<relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>"
    "<argument><name>RcsID</name><direction>out</direction>"
    "<relatedStateVariable>A_ARG_TYPE_RcsID</relatedStateVariable></argument>"
    "<argument><name>AVTransportID</name><direction>out</direction>"
    "<relatedStateVariable>A_ARG_TYPE_AVTransportID</relatedStateVariable></argument>"
    "<argument><name>ProtocolInfo</name><direction>out</direction>"
    "<relatedStateVariable>A_ARG_TYPE_ProtocolInfo</relatedStateVariable></argument>"
    "<argument><name>PeerConnectionManager</name><direction>out</direction>"
    "<relatedStateVariable>A_ARG_TYPE_ConnectionManager</relatedStateVariable></argument>"
    "<argument><name>PeerConnectionID</name><direction>out</direction>"
    "<relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>"
    "<argument><name>Direction</name><direction>out</direction>"
    "<relatedStateVariable>A_ARG_TYPE_Direction</relatedStateVariable></argument>"
    "<argument><name>Status</name><direction>out</direction>"
    "<relatedStateVariable>A_ARG_TYPE_ConnectionStatus</relatedStateVariable></argument>"
    "</argumentList></action>"
    "</actionList>"
    "<serviceStateTable>"
    '<stateVariable sendEvents="no"><name>SourceProtocolInfo</name>'
    "<dataType>string</dataType></stateVariable>"
    '<stateVariable sendEvents="no"><name>SinkProtocolInfo</name>'
    "<dataType>string</dataType></stateVariable>"
    '<stateVariable sendEvents="no"><name>CurrentConnectionIDs</name>'
    "<dataType>string</dataType></stateVariable>"
    "<stateVariable><name>A_ARG_TYPE_ConnectionID</name><dataType>i4</dataType></stateVariable>"
    "<stateVariable><name>A_ARG_TYPE_RcsID</name><dataType>i4</dataType></stateVariable>"
    "<stateVariable><name>A_ARG_TYPE_AVTransportID</name><dataType>i4</dataType></stateVariable>"
    "<stateVariable><name>A_ARG_TYPE_ProtocolInfo</name><dataType>string</dataType>"
    "</stateVariable>"
    "<stateVariable><name>A_ARG_TYPE_ConnectionManager</name><dataType>string</dataType>"
    "</stateVariable>"
    "<stateVariable><name>A_ARG_TYPE_Direction</name><dataType>string</dataType></stateVariable>"
    "<stateVariable><name>A_ARG_TYPE_ConnectionStatus</name><dataType>string</dataType>"
    "</stateVariable>"
    "</serviceStateTable>"
    "</scpd>"
)
