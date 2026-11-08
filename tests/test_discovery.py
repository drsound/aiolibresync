"""Discovery: the parsers against the golden file, the sockets against a fake.

`fixtures/upnp-device-description.xml` is the description a real hub served
in January 2026. Every field the config flow needs is asserted against it, so a
parser that stops recognising a real document fails here rather than on someone
else's network.
"""

import asyncio
import contextlib
import socket
from pathlib import Path

import pytest
from fakes import FakeHTTPServer, FakeHub, FakeSSDPDevice

from aiolibresync import discovery
from aiolibresync.frames import media_command

CAPTURES = Path(__file__).parent / "fixtures"
DESCRIPTION = (CAPTURES / "upnp-device-description.xml").read_bytes()

UDN = "uuid:1a2b3c4d-5e6f-11f0-9193-0123456789ab"
#: The `friendlyName` in that file. It is one unit's name and the owner may
#: change it — what is asserted is that the parser reads the field out of the
#: document, never that a device on someone's network is called this.
FRIENDLY_NAME = "Stereo"

SONOS_DESCRIPTION = b"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
<friendlyName>Kitchen</friendlyName><manufacturer>Sonos, Inc.</manufacturer>
<modelName>Sonos One</modelName><modelDescription>Sonos One</modelDescription>
<UDN>uuid:aaaaaaaa-0000-0000-0000-000000000000</UDN>
</device></root>"""


def test_parses_the_real_device_description():
    desc = discovery.parse_description(DESCRIPTION)

    assert desc is not None
    assert desc.udn == UDN
    assert desc.friendly_name == FRIENDLY_NAME
    assert desc.manufacturer == "LibreWireless"
    assert desc.model_name == "LibreWireless"
    assert desc.model_description == "LibreSyncDMR"
    assert desc.is_libresync


def test_the_serial_number_is_not_read():
    # It is the vendor placeholder 1234567890 on every unit. Reading it at all
    # would invite using it as an identifier.
    assert not hasattr(discovery.parse_description(DESCRIPTION), "serial_number")


#: The reply this hub sent on 2026-10-23, identifiers replaced.
LSSDP_REPLY = (
    b"HTTP/1.1 200 OK\r\nUSN:0123456789ab\r\nHOST: 239.255.255.250:1800\r\n"
    b"Version:LSSDP 1.0\r\nFN:1\r\nFWVERSION:p15262.515.0\r\n"
    b"CAST_FWVERSION:1.52.15262\r\nCAST_TIMEZONE:\r\nCAST_MODEL:Stereo Hub\r\n"
    b"PORT:7777\r\nDeviceName:Stereo\r\nState:F\r\nNETMODE:WLAN\r\n"
    b"SPEAKERTYPE:0\r\nTCPPORT:2020\r\nWIFIBAND:2G\r\n"
    b"SOURCE_LIST:LS9::f7ffffff\r\nMRAMode:APMS\r\n\r\n"
)


@pytest.fixture(autouse=True)
def quick_lssdp(monkeypatch):
    """Nothing answers LSSDP on localhost unless a test starts a responder."""
    monkeypatch.setattr(discovery, "LSSDP_TIMEOUT", 0.2)


def test_another_vendors_renderer_is_not_ours():
    desc = discovery.parse_description(SONOS_DESCRIPTION)

    assert desc is not None
    assert desc.manufacturer == "Sonos, Inc."
    assert not desc.is_libresync


def test_a_rebranded_device_is_recognised_by_its_model_description():
    # The manufacturer is the brand's, and the stock DMR string survives. Both
    # halves of the filter exist because neither is guaranteed on its own.
    xml = b"""<?xml version="1.0"?>
    <root xmlns="urn:schemas-upnp-org:device-1-0"><device>
    <friendlyName>A500</friendlyName><manufacturer>Buchardt Audio</manufacturer>
    <modelName>A500</modelName><modelDescription>LibreSyncDMR</modelDescription>
    <UDN>uuid:bbbbbbbb-0000-0000-0000-000000000000</UDN>
    </device></root>"""

    desc = discovery.parse_description(xml)

    assert desc is not None
    assert desc.is_libresync


def test_a_newer_platin_hub_is_recognised_by_its_model_description():
    # The Stereo Hub HT and the Surround Hub: Platin as the manufacturer, and
    # none of the stock strings.
    xml = b"""<?xml version="1.0"?>
    <root xmlns="urn:schemas-upnp-org:device-1-0"><device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>Living room</friendlyName><manufacturer>Platin</manufacturer>
    <modelName>Stereo Hub HT</modelName><modelNumber>2.0</modelNumber>
    <modelDescription>C4A Wireless Speaker</modelDescription>
    <UDN>uuid:cccccccc-0000-0000-0000-000000000000</UDN>
    </device></root>"""

    desc = discovery.parse_description(xml)

    assert desc is not None
    assert desc.is_libresync


def test_a_document_type_declaration_is_refused():
    # This parses a reply from a host nobody has identified yet.
    xml = b"""<?xml version="1.0"?><!DOCTYPE root [<!ENTITY a "x">]>
    <root xmlns="urn:schemas-upnp-org:device-1-0"><device>
    <friendlyName>&a;</friendlyName></device></root>"""

    assert discovery.parse_description(xml) is None


def test_junk_is_not_a_description():
    assert discovery.parse_description(b"<html>404</html>") is None
    assert discovery.parse_description(b"not xml at all") is None
    assert discovery.parse_description(b"") is None


SSDP_REPLY = (
    b"HTTP/1.1 200 OK\r\n"
    b"CACHE-CONTROL: max-age=1800\r\n"
    b"EXT:\r\n"
    b"LOCATION: http://192.168.10.50:38400/description.xml\r\n"
    b"SERVER: Platform 1.0 CyberLinkC/3.0 UPnP/1.0 DLNADOC/1.50\r\n"
    b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
    b"USN: uuid:1a2b3c4d-5e6f-11f0-9193-0123456789ab::"
    b"urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
    b"\r\n"
)


def test_the_search_asks_for_a_mediarenderer():
    message = discovery.build_msearch("239.255.255.250", 1900, discovery.SSDP_ST, 2)

    assert message.startswith(b"M-SEARCH * HTTP/1.1\r\n")
    assert b"HOST: 239.255.255.250:1900\r\n" in message
    assert b'MAN: "ssdp:discover"\r\n' in message
    assert b"MX: 2\r\n" in message
    assert b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n" in message
    assert message.endswith(b"\r\n\r\n")


def test_reply_headers_are_read_case_insensitively():
    headers = discovery.parse_ssdp_headers(SSDP_REPLY)

    assert headers["location"] == "http://192.168.10.50:38400/description.xml"
    assert headers["st"] == "urn:schemas-upnp-org:device:MediaRenderer:1"
    assert headers["usn"].startswith("uuid:1a2b3c4d")
    assert headers["ext"] == ""


def test_a_datagram_that_is_not_a_reply_yields_nothing_useful():
    assert "location" not in discovery.parse_ssdp_headers(b"\x00\x01\x02")
    assert discovery.parse_ssdp_headers(b"") == {}


async def test_fetches_and_parses_a_description():
    server = FakeHTTPServer(DESCRIPTION)
    await server.start()
    try:
        desc = await discovery.async_fetch_description(server.url())
    finally:
        await server.stop()

    assert desc is not None
    assert desc.udn == UDN
    assert server.requests == ["/description.xml"]


async def test_a_chunked_response_is_reassembled():
    # UPnP servers are HTTP/1.1 and chunked is legal. CyberLinkC sends a
    # Content-Length, so this is the path no hardware has exercised.
    server = FakeHTTPServer(DESCRIPTION, chunked=True)
    await server.start()
    try:
        desc = await discovery.async_fetch_description(server.url())
    finally:
        await server.stop()

    assert desc is not None
    assert desc.udn == UDN


async def test_a_404_is_not_a_description():
    server = FakeHTTPServer(DESCRIPTION)
    await server.start()
    try:
        assert await discovery.async_fetch_description(server.url("/nothing.xml")) is None
    finally:
        await server.stop()


async def test_an_oversized_body_is_dropped_rather_than_read():
    server = FakeHTTPServer(b"<root>" + b"x" * discovery.MAX_DESCRIPTION_BYTES + b"</root>")
    await server.start()
    try:
        assert await discovery.async_fetch_description(server.url()) is None
    finally:
        await server.stop()


async def test_an_unreachable_url_is_none_not_an_exception():
    # A device can answer SSDP and refuse the connection a moment later.
    assert await discovery.async_fetch_description("http://127.0.0.1:1/x.xml") is None
    assert await discovery.async_fetch_description("ftp://127.0.0.1/x.xml") is None
    assert await discovery.async_fetch_description("nonsense") is None


async def test_an_open_control_port_is_a_hub_and_nothing_is_sent():
    received = bytearray()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.extend(await reader.read(64))
        # On 3.12 a handler that returns without closing its writer leaves the
        # transport attached to the server, and `wait_closed()` below then waits
        # for a detach that never comes. Fixed upstream in 3.13.
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await discovery.async_probe_control("127.0.0.1", port) is True
        await asyncio.sleep(0.05)
    finally:
        server.close()
        await server.wait_closed()

    # The device on the other end has not been identified. 50006 is a shared
    # broadcast bus and this probe stays silent on it.
    assert received == b""


async def test_a_closed_control_port_is_not_a_hub():
    assert await discovery.async_probe_control("127.0.0.1", 1, timeout=1.0) is False


async def _discover_against(monkeypatch, device: FakeSSDPDevice, **kwargs):
    """Point the whole search at a fake on the loopback interface."""
    monkeypatch.setattr(discovery, "SSDP_ADDRESS", "127.0.0.1")
    monkeypatch.setattr(discovery, "SSDP_PORT", device.ssdp_port)
    monkeypatch.setattr(discovery, "SSDP_ATTEMPTS", 1)
    if device.control_wanted:
        monkeypatch.setattr(discovery, "CONTROL_PORT", device.control_port)
    return await discovery.async_discover(timeout=0.5, **kwargs)


async def test_discovers_a_hub_end_to_end(monkeypatch):
    device = FakeSSDPDevice(DESCRIPTION)
    await device.start()
    try:
        location, control_port = device.location(), device.control_port
        found = await _discover_against(monkeypatch, device)
    finally:
        await device.stop()

    assert len(found) == 1
    assert found[0].host == "127.0.0.1"
    assert found[0].udn == UDN
    assert found[0].name == FRIENDLY_NAME
    assert found[0].model == "LibreWireless"
    assert found[0].description_url == location
    assert found[0].control_port == control_port


async def test_a_device_that_answers_three_times_is_found_once(monkeypatch):
    device = FakeSSDPDevice(DESCRIPTION, replies=3)
    await device.start()
    try:
        found = await _discover_against(monkeypatch, device)
    finally:
        await device.stop()

    assert len(found) == 1
    # And it was asked for its description once, not three times.
    assert device.description_requests == ["/description.xml"]


async def test_another_vendors_renderer_is_not_returned(monkeypatch):
    device = FakeSSDPDevice(SONOS_DESCRIPTION)
    await device.start()
    try:
        assert await _discover_against(monkeypatch, device) == []
    finally:
        await device.stop()


async def test_a_librewireless_device_with_no_control_port_is_not_returned(monkeypatch):
    device = FakeSSDPDevice(DESCRIPTION, control=False)
    await device.start()
    try:
        monkeypatch.setattr(discovery, "CONTROL_PORT", 1)  # nothing listens there
        found = await _discover_against(monkeypatch, device)
    finally:
        await device.stop()

    assert found == []


async def test_an_empty_network_is_an_empty_list(monkeypatch):
    monkeypatch.setattr(discovery, "SSDP_ADDRESS", "127.0.0.1")
    monkeypatch.setattr(discovery, "SSDP_PORT", 1)
    monkeypatch.setattr(discovery, "SSDP_ATTEMPTS", 1)

    assert await discovery.async_discover(timeout=0.3) == []


async def test_a_typed_address_gets_the_same_identity_as_a_discovered_one(monkeypatch):
    device = FakeSSDPDevice(DESCRIPTION)
    await device.start()
    try:
        monkeypatch.setattr(discovery, "CONTROL_PORT", device.control_port)
        monkeypatch.setattr(discovery, "UPNP_PORT", device.http_port)
        found = await discovery.async_probe("127.0.0.1")
    finally:
        await device.stop()

    assert found is not None
    assert found.udn == UDN
    assert found.name == FRIENDLY_NAME


async def test_a_reachable_hub_with_no_upnp_still_probes(monkeypatch):
    # The renderer can be unreachable while the control port answers. The hub is
    # usable; only its identity is missing, and the caller decides what to do about
    # that rather than being told there is no device here.
    device = FakeSSDPDevice(DESCRIPTION)
    await device.start()
    try:
        monkeypatch.setattr(discovery, "CONTROL_PORT", device.control_port)
        monkeypatch.setattr(discovery, "UPNP_PORT", 1)
        monkeypatch.setattr(discovery, "SSDP_PORT", 1)
        monkeypatch.setattr(discovery, "UNICAST_SEARCH_TIMEOUT", 0.5)
        found = await discovery.async_probe("127.0.0.1", timeout=0.5)
    finally:
        await device.stop()

    assert found is not None
    assert found.host == "127.0.0.1"
    assert found.udn is None


async def _probe_on_its_own_port(monkeypatch, device: FakeSSDPDevice, host: str):
    """Nothing on the usual description port; the description is where the
    device's own search reply says, as on the newer Platin hubs."""
    monkeypatch.setattr(discovery, "CONTROL_PORT", device.control_port)
    monkeypatch.setattr(discovery, "UPNP_PORT", 1)
    monkeypatch.setattr(discovery, "SSDP_PORT", device.ssdp_port)
    monkeypatch.setattr(discovery, "UNICAST_SEARCH_TIMEOUT", 2.0)
    return await discovery.async_probe(host, timeout=2.0)


async def test_a_description_on_another_port_is_found_by_asking_the_host(monkeypatch):
    device = FakeSSDPDevice(DESCRIPTION)
    await device.start()
    try:
        started = asyncio.get_running_loop().time()
        found = await _probe_on_its_own_port(monkeypatch, device, "127.0.0.1")
        elapsed = asyncio.get_running_loop().time() - started
        location = device.location()
    finally:
        await device.stop()

    assert found is not None
    assert found.udn == UDN
    assert found.description_url == location
    assert device.searches == 1
    # The search ends on the answer rather than waiting out its timeout.
    assert elapsed < 1.0


async def test_a_search_to_one_host_is_sent_until_it_answers(monkeypatch):
    # The hub's own responder drops searches, and on the newer hubs this is the
    # only way to the description, so three sends are not the limit here.
    device = FakeSSDPDevice(DESCRIPTION, replies=0)
    await device.start()
    try:
        monkeypatch.setattr(discovery, "SSDP_RETRY_INTERVAL", 0.1)
        asyncio.get_running_loop().call_later(0.45, setattr, device, "_replies", 1)
        found = await _probe_on_its_own_port(monkeypatch, device, "127.0.0.1")
    finally:
        await device.stop()

    assert found is not None
    assert found.udn == UDN
    assert device.searches > discovery.SSDP_ATTEMPTS


async def test_a_search_to_one_host_still_names_the_group(monkeypatch):
    # The Stereo Hub HT ignores a search whose HOST header is its own address,
    # even when the datagram is sent to it alone.
    device = FakeSSDPDevice(DESCRIPTION, group_host_only=True)
    await device.start()
    try:
        found = await _probe_on_its_own_port(monkeypatch, device, "127.0.0.1")
    finally:
        await device.stop()

    assert found is not None
    assert found.udn == UDN


async def test_a_typed_name_is_resolved_before_asking(monkeypatch):
    # The reply comes from an address, never from the name the user typed.
    device = FakeSSDPDevice(DESCRIPTION)
    await device.start()
    try:
        found = await _probe_on_its_own_port(monkeypatch, device, "localhost")
    finally:
        await device.stop()

    assert found is not None
    assert found.udn == UDN


async def test_a_search_reply_naming_another_host_is_not_followed(monkeypatch):
    device = FakeSSDPDevice(DESCRIPTION)
    await device.start()
    try:
        elsewhere = device.location().replace("127.0.0.1", "127.0.0.2")
        device.location = lambda: elsewhere  # type: ignore[method-assign]
        found = await _probe_on_its_own_port(monkeypatch, device, "127.0.0.1")
    finally:
        await device.stop()

    assert found is not None
    assert found.udn is None
    assert device.description_requests == []


async def test_an_address_with_nothing_on_it_is_none():
    assert await discovery.async_probe("127.0.0.1", port=1, timeout=1.0) is None


def test_discovery_is_importable_from_the_package():
    import aiolibresync

    assert aiolibresync.DiscoveredDevice is discovery.DiscoveredDevice
    assert aiolibresync.async_discover is discovery.async_discover
    assert aiolibresync.async_probe is discovery.async_probe
    for name in ("DiscoveredDevice", "async_discover", "async_probe"):
        assert name in aiolibresync.__all__


async def test_the_serial_is_read_from_a_registered_session():
    hub = FakeHub()
    await hub.start()
    try:
        serial = await discovery.async_read_serial("127.0.0.1", hub.media_port)
    finally:
        await hub.stop()
    assert serial == "FAKE0SERIAL000000000"
    # Registered first, then one query, and nothing else was written.
    assert [media_command(f, from_device=False) for f in hub.received["media"]] == [0x0003, 0x00E7]


async def test_a_serial_not_marked_valid_is_none():
    hub = FakeHub()
    hub.serial_box = b",INVALID"
    await hub.start()
    try:
        assert await discovery.async_read_serial("127.0.0.1", hub.media_port) is None
    finally:
        await hub.stop()


async def test_nothing_listening_is_none_not_an_exception():
    assert await discovery.async_read_serial("127.0.0.1", 1, timeout=0.5) is None


async def test_a_peer_that_never_registers_us_is_none_within_the_timeout():
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(64)
        await asyncio.sleep(1)
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await discovery.async_read_serial("127.0.0.1", port, timeout=0.2) is None
    finally:
        server.close()
        await server.wait_closed()


async def test_a_reply_whose_location_names_another_host_is_ignored(monkeypatch):
    """The description would speak for a machine other than the one probed."""
    device = FakeSSDPDevice(DESCRIPTION)
    await device.start()
    try:
        elsewhere = device.location().replace("127.0.0.1", "127.0.0.2")
        device.location = lambda: elsewhere  # type: ignore[method-assign]
        found = await _discover_against(monkeypatch, device)
    finally:
        await device.stop()

    assert found == []
    assert device.description_requests == []


def test_the_lssdp_reply_names_the_module():
    info = discovery.parse_lssdp(LSSDP_REPLY)

    assert info == discovery.LssdpInfo(
        module="LS9", model="Stereo Hub", firmware="p15262.515.0", media_port=7777
    )
    newer = LSSDP_REPLY.replace(b"LS9::f7ffffff", b"LS10::01000010")
    assert discovery.parse_lssdp(newer).module == "LS10"  # type: ignore[union-attr]


def test_an_ssdp_reply_is_not_an_lssdp_one():
    assert discovery.parse_lssdp(b"M-SEARCH * HTTP/1.1\r\nHOST:x\r\n\r\n") is None
    assert discovery.parse_lssdp(b"HTTP/1.1 200 OK\r\nLOCATION: http://x/\r\n\r\n") is None


class _LssdpResponder(asyncio.DatagramProtocol):
    def __init__(self, reply: bytes, ignore: int = 0) -> None:
        self.reply = reply
        self.ignore = ignore
        self.queries = 0
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if not data.startswith(b"M-SEARCH") or self.transport is None:
            return
        self.queries += 1
        if self.queries > self.ignore:
            self.transport.sendto(self.reply, addr)


@contextlib.asynccontextmanager
async def _lssdp_responder(monkeypatch, reply: bytes = LSSDP_REPLY, ignore: int = 0):
    transport, responder = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: _LssdpResponder(reply, ignore), local_addr=("127.0.0.1", 0)
    )
    monkeypatch.setattr(discovery, "LSSDP_PORT", transport.get_extra_info("sockname")[1])
    try:
        yield responder
    finally:
        transport.close()


async def test_lssdp_is_asked_again_until_it_answers(monkeypatch):
    monkeypatch.setattr(discovery, "LSSDP_RETRY_INTERVAL", 0.05)
    async with _lssdp_responder(monkeypatch, ignore=2) as responder:
        info = await discovery.async_query_lssdp("127.0.0.1", timeout=3.0)

    assert info is not None
    assert info.module == "LS9"
    assert responder.queries == 3


async def test_no_lssdp_answer_is_none(monkeypatch):
    # A port nothing listens on, rather than 1800 on the machine running this.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    monkeypatch.setattr(discovery, "LSSDP_PORT", free_port)
    assert await discovery.async_query_lssdp("127.0.0.1", timeout=0.2) is None


def test_a_port_in_other_digits_is_not_a_port():
    info = discovery.parse_lssdp(LSSDP_REPLY.replace(b"PORT:7777", "PORT:²".encode()))
    assert info is not None
    assert info.media_port is None
    assert info.module == "LS9"


async def test_the_probe_reports_the_module(monkeypatch):
    device = FakeSSDPDevice(DESCRIPTION)
    await device.start()
    try:
        monkeypatch.setattr(discovery, "CONTROL_PORT", device.control_port)
        monkeypatch.setattr(discovery, "UPNP_PORT", device.http_port)
        monkeypatch.setattr(discovery, "LSSDP_TIMEOUT", 2.0)
        newer = LSSDP_REPLY.replace(b"LS9::f7ffffff", b"LS10::01000010")
        async with _lssdp_responder(monkeypatch, newer):
            found = await discovery.async_probe("127.0.0.1")
    finally:
        await device.stop()

    assert found is not None
    assert found.udn == UDN
    assert found.module == "LS10"


async def test_a_socket_error_on_the_udp_queries_costs_only_what_they_find(monkeypatch):
    """The control port has answered: no socket for LSSDP or the search must
    not turn into no device."""
    device = FakeSSDPDevice(DESCRIPTION)
    await device.start()
    loop = asyncio.get_running_loop()

    async def refuse(*_args, **_kwargs):
        raise OSError("no socket")

    try:
        monkeypatch.setattr(discovery, "CONTROL_PORT", device.control_port)
        monkeypatch.setattr(discovery, "UPNP_PORT", 1)
        monkeypatch.setattr(loop, "create_datagram_endpoint", refuse)
        found = await discovery.async_probe("127.0.0.1")
    finally:
        await device.stop()

    assert found is not None
    assert found.udn is None
    assert found.module is None
