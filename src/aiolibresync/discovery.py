"""SSDP discovery, and the TCP probe that turns a MediaRenderer into a hub.

Discovery lives here rather than in the integration's config flow so it is
testable without Home Assistant and usable from any script.

Three steps, in increasing order of cost:

1. an SSDP `M-SEARCH` for `MediaRenderer:1` — a UDP datagram, sent more than
   once because UDP loses them;
2. the device description at the `LOCATION` header, which is the only place the
   manufacturer and the UDN appear;
3. a TCP connect to port 50006, which is what separates a LibreSync hub from
   every other MediaRenderer on the network.

Nothing here writes anything, and step 3 connects and closes **without sending a
byte**: at that point the host has been identified by a description it served
itself, which is not the same as being the device it claims to be.
"""

import asyncio
import contextlib
import itertools
import logging
import socket
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree

from . import frames, media
from .const import (
    CONTROL_PORT,
    DESCRIPTION_PATH,
    DISCOVERY_TIMEOUT,
    HTTP_TIMEOUT,
    LSSDP_PORT,
    LSSDP_QUERY,
    LSSDP_RETRY_INTERVAL,
    LSSDP_TIMEOUT,
    MAX_DESCRIPTION_BYTES,
    MAX_SSDP_CANDIDATES,
    MEDIA_PORT,
    MID_REGISTER,
    MID_SERIAL,
    PROBE_TIMEOUT,
    SSDP_ADDRESS,
    SSDP_ATTEMPTS,
    SSDP_MX,
    SSDP_PORT,
    SSDP_RETRY_INTERVAL,
    SSDP_ST,
    SSDP_TTL,
    UNICAST_SEARCH_TIMEOUT,
    UPNP_PORT,
)

_LOGGER = logging.getLogger(__name__)

#: What marks a description as ours, all lower-cased before comparison.
#: `manufacturer` is the stock string on this unit. `modelDescription` is
#: checked as well because a rebranded device is likely to carry its own brand
#: as the manufacturer while keeping the LibreSync DMR string — and neither
#: half is guaranteed, which is why the TCP probe is what actually confirms.
#: `C4A Wireless Speaker` is what the newer Platin hubs (Stereo Hub HT, Surround
#: Hub) report, with `Platin` as the manufacturer, in two owners' reports.
_MANUFACTURER_MARK = "librewireless"
_MODEL_DESCRIPTION_MARKS = ("libresync", "c4a wireless speaker")

_WANTED_TAGS = frozenset({"UDN", "friendlyName", "manufacturer", "modelName", "modelDescription"})


@dataclass(frozen=True, slots=True)
class DeviceDescription:
    """The five fields of a UPnP device description this library reads.

    `serialNumber` is deliberately absent: it is the vendor placeholder
    `1234567890` on this hardware, and a field that looks like an identifier but
    is not is worse than no field at all. See docs/devices.md.
    """

    udn: str | None = None
    friendly_name: str | None = None
    manufacturer: str | None = None
    model_name: str | None = None
    model_description: str | None = None

    @property
    def is_libresync(self) -> bool:
        """Whether this description is worth a TCP probe."""
        return _MANUFACTURER_MARK in (self.manufacturer or "").lower() or any(
            mark in (self.model_description or "").lower() for mark in _MODEL_DESCRIPTION_MARKS
        )


def parse_description(payload: bytes) -> DeviceDescription | None:
    """A UPnP device description, or None if `payload` is not one.

    Namespaces are stripped rather than matched: the document declares
    `urn:schemas-upnp-org:device-1-0` on this hub, and a rebranded device is
    free to declare something else without ceasing to be a MediaRenderer.
    """
    if b"<!DOCTYPE" in payload.upper():
        # An unidentified host on the network wrote this. A document type
        # declaration is the entry point to the entity-expansion attacks
        # ElementTree does not defend against, and no device description needs
        # one.
        _LOGGER.debug("Refusing a device description with a DOCTYPE")
        return None
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return None
    fields: dict[str, str] = {}
    for element in root.iter():
        tag = element.tag.rpartition("}")[2]
        if tag in _WANTED_TAGS and element.text and tag not in fields:
            fields[tag] = element.text.strip()
    if not fields:
        return None
    return DeviceDescription(
        udn=fields.get("UDN"),
        friendly_name=fields.get("friendlyName"),
        manufacturer=fields.get("manufacturer"),
        model_name=fields.get("modelName"),
        model_description=fields.get("modelDescription"),
    )


def build_msearch(address: str, port: int, st: str, mx: int) -> bytes:
    """One `M-SEARCH` datagram.

    The target is an argument rather than a constant read inside because the
    tests point the whole search at a responder on the loopback interface.
    """
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {address}:{port}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {mx}\r\n"
        f"ST: {st}\r\n"
        "\r\n"
    ).encode()


def parse_ssdp_headers(datagram: bytes) -> dict[str, str]:
    """Header name, lower-cased, to value. The status line is discarded.

    Anything that is not a header line is skipped rather than raising: this
    parses whatever arrived on a UDP port, which is not necessarily SSDP and not
    necessarily well formed.
    """
    text = datagram.decode("utf-8", errors="replace")
    headers: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    return headers


async def async_fetch_description(
    url: str,
    *,
    # Not a deadline the caller can impose from outside: this function's
    # contract is that it returns None instead of raising, and an
    # `asyncio.timeout` around it would raise.
    timeout: float = HTTP_TIMEOUT,  # noqa: ASYNC109
) -> DeviceDescription | None:
    """GET `url` and parse it, or None for anything that goes wrong.

    Every failure is a None: an unreachable host, a refused connection, a 404, a
    body too large, junk instead of XML. A control point that raises on any of
    those turns one misbehaving device on the network into a failed scan.
    """
    payload = await _async_http_get(url, timeout=timeout, limit=MAX_DESCRIPTION_BYTES)
    if payload is None:
        return None
    return parse_description(payload)


async def _async_http_get(
    url: str,
    *,
    timeout: float,  # noqa: ASYNC109 — see async_fetch_description
    limit: int,
) -> bytes | None:
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        return None
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {parts.netloc}\r\n"
        "Accept: text/xml\r\n"
        "User-Agent: aiolibresync\r\n"
        # The reply is read to EOF, so the server has to close it for us.
        "Connection: close\r\n"
        "\r\n"
    ).encode()
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(parts.hostname, parts.port or 80)
            try:
                writer.write(request)
                await writer.drain()
                raw = await _read_capped(reader, limit)
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
    except (OSError, TimeoutError, ValueError):
        _LOGGER.debug("No device description at %s", url)
        return None
    if raw is None:
        _LOGGER.debug("The document at %s exceeds %d bytes; ignored", url, limit)
        return None
    return _http_body(raw)


async def _read_capped(reader: asyncio.StreamReader, limit: int) -> bytes | None:
    """Everything until EOF, or None once `limit` is exceeded."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)


def _http_body(raw: bytes) -> bytes | None:
    """The body of a 200 response, de-chunked if it needs to be."""
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        return None
    status = head.split(b"\r\n", 1)[0].split()
    if len(status) < 2 or status[1] != b"200":
        return None
    headers = parse_ssdp_headers(head)
    if "chunked" in headers.get("transfer-encoding", "").lower():
        return _dechunk(body)
    return body


def _dechunk(body: bytes) -> bytes | None:
    out = bytearray()
    while True:
        line, separator, rest = body.partition(b"\r\n")
        if not separator:
            return None
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError:
            return None
        if size == 0:
            return bytes(out)
        if len(rest) < size + 2:
            return None
        out += rest[:size]
        body = rest[size + 2 :]


async def async_probe_control(
    host: str,
    port: int = CONTROL_PORT,
    *,
    # Returns False rather than raising, so the caller cannot express this
    # with `asyncio.timeout`.
    timeout: float = PROBE_TIMEOUT,  # noqa: ASYNC109
) -> bool:
    """Whether something accepts a TCP connection on the System Control port.

    Connects and closes without sending a byte. This is the confirmation step of
    discovery, and at this point the host is a stranger that served a plausible
    description — writing to it would be a probe of an unidentified device,
    which is exactly what this library refuses to do.
    """
    try:
        async with asyncio.timeout(timeout):
            _reader, writer = await asyncio.open_connection(host, port)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError, TimeoutError):
        await writer.wait_closed()
    return True


async def async_read_serial(
    host: str,
    port: int = MEDIA_PORT,
    *,
    # Returns None rather than raising, like async_probe_control.
    timeout: float = PROBE_TIMEOUT,  # noqa: ASYNC109
) -> str | None:
    """The hub's factory serial, read from message box 231, or None.

    Unlike `async_probe_control` this does write: a session registration and one
    query, both read-only in effect. Call it for a host that is already believed
    to be a hub — one the user typed, or one whose description and control port
    both checked out — and not for a stranger.

    None covers every way of not knowing: nothing listening, no answer in time,
    or a unit whose factory serial is missing or not marked valid, which the
    firmware itself allows for. The caller falls back to the UDN.
    """
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(host, port)
    except (OSError, TimeoutError):
        return None
    try:
        async with asyncio.timeout(timeout):
            local = writer.get_extra_info("sockname")
            if not local:
                return None
            writer.write(media.register(local[0]))
            await writer.drain()
            framer = frames.MediaFrameReader()
            asked = False
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return None
                for frame in framer.feed(chunk):
                    mid = media.mid_of(frame)
                    if mid == MID_REGISTER and not asked:
                        # The query is only answered on a registered session.
                        writer.write(media.QUERY_SERIAL)
                        await writer.drain()
                        asked = True
                    elif mid == MID_SERIAL:
                        return media.parse_serial(frame[frames.MEDIA_HEADER :])
    except (OSError, TimeoutError):
        return None
    finally:
        writer.close()
        with contextlib.suppress(OSError, TimeoutError):
            await writer.wait_closed()


@dataclass(frozen=True, slots=True)
class LssdpInfo:
    """What a hub says about itself on Libre's own discovery port.

    `module` is the Libre module before `::` in `SOURCE_LIST`, `LS9` on the
    original Stereo Hub and `LS10` on the newer ones, whose 7777 needs TLS. It
    tells the generations apart before anything touches 7777.
    """

    module: str | None = None
    model: str | None = None
    firmware: str | None = None
    media_port: int | None = None


def parse_lssdp(datagram: bytes) -> LssdpInfo | None:
    """An LSSDP `200 OK`, or None if `datagram` is not one."""
    if not datagram.startswith(b"HTTP/1.1 200"):
        return None
    headers = parse_ssdp_headers(datagram)
    if "source_list" not in headers and "fwversion" not in headers:
        return None
    module = headers.get("source_list", "").partition("::")[0] or None
    port = headers.get("port", "")
    return LssdpInfo(
        module=module,
        model=headers.get("cast_model") or None,
        firmware=headers.get("fwversion") or None,
        # `isascii` too: `isdigit` accepts digits such as "²" that `int` refuses.
        media_port=int(port) if port.isascii() and port.isdigit() else None,
    )


async def async_query_lssdp(
    host: str,
    *,
    timeout: float = LSSDP_TIMEOUT,  # noqa: ASYNC109 — see async_probe_control
) -> LssdpInfo | None:
    """Ask `host` alone on Libre's discovery port.

    None means unknown, not "the original hub": the hub did not answer within
    `timeout`, the host has no IPv4 address, or no socket could be opened. A
    caller that refuses a module should treat None as no decision.

    The query is sent again every `LSSDP_RETRY_INTERVAL` until the answer or
    `timeout`: it is one UDP datagram. Nothing in it changes the hub.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(
            host, LSSDP_PORT, family=socket.AF_INET, type=socket.SOCK_DGRAM
        )
    except OSError:
        return None
    address = str(infos[0][4][0])
    # A host name may resolve to several addresses, and the reply may come from
    # any of them.
    addresses = {str(info[4][0]) for info in infos}
    answer: asyncio.Future[LssdpInfo] = loop.create_future()

    class _Listener(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
            if str(addr[0]) not in addresses or answer.done():
                return
            if (info := parse_lssdp(data)) is not None:
                answer.set_result(info)

    try:
        transport, _protocol = await loop.create_datagram_endpoint(
            _Listener, local_addr=("0.0.0.0", 0), family=socket.AF_INET
        )
    except OSError as err:
        _LOGGER.debug("no socket for the LSSDP query: %s", err)
        return None
    try:
        deadline = loop.time() + timeout
        while (remaining := deadline - loop.time()) > 0:
            transport.sendto(LSSDP_QUERY, (address, LSSDP_PORT))
            with contextlib.suppress(TimeoutError):
                return await asyncio.wait_for(
                    asyncio.shield(answer), min(LSSDP_RETRY_INTERVAL, remaining)
                )
    finally:
        transport.close()
    _LOGGER.debug("%s did not answer on LSSDP", host)
    return None


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    """A hub on the network, as its UPnP description presents it.

    `udn` survived seven months, a reboot and a subnet change, and it is what
    SSDP carries in its `USN`. It is None only on the manual path, when the
    address is known but the UPnP renderer did not answer — see `async_probe`.
    The factory serial, which does not depend on that renderer, is the
    identifier to prefer where it can be read: see `async_read_serial`.
    """

    host: str
    udn: str | None = None
    name: str | None = None
    model: str | None = None
    manufacturer: str | None = None
    description_url: str | None = None
    control_port: int = CONTROL_PORT
    #: From LSSDP, on `async_probe` only: see `LssdpInfo.module`.
    module: str | None = None


async def async_discover(
    *,
    # A collection window, not a deadline. The search waits this out on
    # purpose and returns what it gathered; cancelling it from outside with
    # `asyncio.timeout` would throw those results away.
    timeout: float = DISCOVERY_TIMEOUT,  # noqa: ASYNC109
    source_ip: str | None = None,
) -> list[DiscoveredDevice]:
    """Every LibreSync hub that answers an SSDP search on this network.

    Waits the full `timeout` rather than returning on the first answer: there is
    no count to wait for, and a second hub is always allowed to be slower.

    `source_ip` selects the interface to search from, which matters on a machine
    with more than one — without it the search leaves by whichever interface the
    routing table picks for multicast, and a hub on the other one is never
    found.
    """
    candidates = await _async_msearch(timeout=timeout, source_ip=source_ip)
    identified = await asyncio.gather(
        *(_async_identify(host, location) for host, location in candidates.items())
    )
    devices: dict[str, DiscoveredDevice] = {}
    for device in identified:
        if device is not None:
            # Two addresses for one UDN is one device on two interfaces, and a
            # config entry keyed on the UDN can only have one of them.
            devices.setdefault(device.udn or device.host, device)
    return list(devices.values())


async def _async_identify(host: str, location: str) -> DiscoveredDevice | None:
    description = await async_fetch_description(location)
    if description is None:
        return None
    if not description.is_libresync:
        _LOGGER.debug("%s is a %s renderer, not ours", host, description.manufacturer)
        return None
    if not await async_probe_control(host, CONTROL_PORT):
        _LOGGER.debug("%s looks like ours but port %d is closed", host, CONTROL_PORT)
        return None
    return _discovered(host, location, description, CONTROL_PORT)


def _discovered(
    host: str, location: str, description: DeviceDescription, control_port: int
) -> DiscoveredDevice:
    return DiscoveredDevice(
        host=host,
        udn=description.udn,
        name=description.friendly_name,
        model=description.model_name,
        manufacturer=description.manufacturer,
        description_url=location,
        control_port=control_port,
    )


async def _async_msearch(
    *,
    timeout: float,  # noqa: ASYNC109 — see async_discover
    source_ip: str | None,
    address: str | None = None,
) -> dict[str, str]:
    """Host to `LOCATION`, for everything that answered within `timeout`.

    A reply whose `LOCATION` names a host other than its sender is dropped: the
    description would then speak for a different machine from the one probed
    afterwards.

    With `address`, the search goes to that host alone instead of the multicast
    group, is sent again every `SSDP_RETRY_INTERVAL` until the `timeout` rather
    than `SSDP_ATTEMPTS` times, and ends as soon as that host answers. A host
    with nothing on the SSDP port costs the whole `timeout`: an unconnected UDP
    socket is not told of the refusal, and a connected one would drop the
    replies that come from a port other than 1900.
    """
    loop = asyncio.get_running_loop()
    found: dict[str, str] = {}
    target = address or SSDP_ADDRESS
    done = asyncio.Event()

    class _Collector(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
            headers = parse_ssdp_headers(data)
            location = headers.get("location")
            if not location:
                return
            host = str(addr[0])
            if address is not None and host != address:
                return
            _LOGGER.debug("SSDP reply from %s: %s", host, headers)
            try:
                named = urlsplit(location).hostname
            except ValueError:
                return
            if named != host:
                _LOGGER.debug("%s answered with a LOCATION on %s; ignored", host, named)
                return
            if host in found or len(found) < MAX_SSDP_CANDIDATES:
                found.setdefault(host, location)
            if address is not None:
                done.set()

    transport, _protocol = await loop.create_datagram_endpoint(
        _Collector, local_addr=(source_ip or "0.0.0.0", 0), family=socket.AF_INET
    )
    try:
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, SSDP_TTL)
            if source_ip:
                sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(source_ip)
                )
        # The HOST header names the group even when the search goes to one
        # host: the Stereo Hub HT ignores a search that names its own address.
        message = build_msearch(SSDP_ADDRESS, SSDP_PORT, SSDP_ST, SSDP_MX)
        deadline = loop.time() + timeout
        sends = range(SSDP_ATTEMPTS) if address is None else itertools.count()
        for _attempt in sends:
            if done.is_set():
                break
            transport.sendto(message, (target, SSDP_PORT))
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            # The replies arrive on the protocol's callback; this wait is what
            # gives the event loop the chance to run it.
            await _wait(done, min(SSDP_RETRY_INTERVAL, remaining))
        remaining = deadline - loop.time()
        if remaining > 0 and not done.is_set():
            await _wait(done, remaining)
    finally:
        transport.close()
    return found


async def _wait(event: asyncio.Event, timeout: float) -> None:  # noqa: ASYNC109
    """Wait for `event`, or `timeout` seconds, whichever comes first."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), timeout)


async def _async_search_location(host: str) -> str | None:
    """The `LOCATION` that `host` names in reply to a search sent to it alone.

    The reply comes from an address, so a name is resolved first.
    """
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, SSDP_PORT, family=socket.AF_INET, type=socket.SOCK_DGRAM
        )
    except OSError:
        return None
    address = str(infos[0][4][0])
    try:
        found = await _async_msearch(
            timeout=UNICAST_SEARCH_TIMEOUT, source_ip=None, address=address
        )
    except OSError as err:
        # The control port has answered by now: a failed search costs the
        # identity, not the device.
        _LOGGER.debug("no socket for the search: %s", err)
        return None
    return found.get(address)


async def async_probe(
    host: str,
    *,
    port: int = 0,
    timeout: float = PROBE_TIMEOUT,  # noqa: ASYNC109 — see async_probe_control
) -> DiscoveredDevice | None:
    """Identify a hub at a known address — the config flow's manual path.

    None means nothing answered on the System Control port, which is the only
    thing that makes an address unusable. A device that answers but whose UPnP
    renderer does not comes back with `udn` None: it is reachable and
    controllable, and only its identity is missing.

    `port` overrides the control port for tests; leave it at 0 for the real one.
    """
    control_port = port or CONTROL_PORT
    if not await async_probe_control(host, control_port, timeout=timeout):
        return None
    (location, description), lssdp = await asyncio.gather(
        _async_describe(host, timeout=timeout), async_query_lssdp(host, timeout=LSSDP_TIMEOUT)
    )
    module = lssdp.module if lssdp is not None else None
    if description is None:
        _LOGGER.debug("%s answers on %d but served no description", host, control_port)
        return DiscoveredDevice(host=host, control_port=control_port, module=module)
    assert location is not None
    return replace(_discovered(host, location, description, control_port), module=module)


async def _async_describe(
    host: str,
    *,
    timeout: float,  # noqa: ASYNC109 — see async_probe_control
) -> tuple[str | None, DeviceDescription | None]:
    """The description of the hub at `host`, and where it was found."""
    # There is no LOCATION header on this path, so the description URL is first
    # the one this hub has always served. `URLBase` inside the document is no
    # help: it is baked at boot and still carries the hub's old address.
    location = f"http://{host}:{UPNP_PORT}{DESCRIPTION_PATH}"
    description = await async_fetch_description(location, timeout=timeout)
    if description is None:
        # The newer hubs serve it on a port of their own choosing, which only
        # the LOCATION of a search reply names. Ask the host alone.
        searched = await _async_search_location(host)
        if searched is not None and searched != location:
            location = searched
            description = await async_fetch_description(location, timeout=timeout)
    return (location if description is not None else None), description
