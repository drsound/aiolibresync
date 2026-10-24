"""A stand-in for the hub: both ports, scripted answers, replayable captures.

Modelled on observed behaviour, not on what would be convenient:

- power and volume events are sent **twice**, as the real hub sends them;
- setting room correction produces **no frame at all** — the finding that makes
  a poll loop mandatory;
- a 7777 request on an unregistered session is answered `mid 0x0000 status
  0x03` and dropped.
"""

import asyncio
import json
import ssl
from dataclasses import dataclass

import trustme

from aiolibresync.const import (
    CLASS_EQ,
    CLASS_PRESET,
    CLASS_ROOM,
    CLASS_SYSTEM,
    MAX_PAYLOAD,
    MID_MODEL,
    MID_MUTE,
    MID_PLAY_STATE,
    MID_PLAYVIEW,
    MID_REGISTER,
    MID_SERIAL,
    MID_TRANSPORT,
    MID_VOLUME,
    MSG_COMMAND,
    MSG_QUERY,
    STATUS_NOT_REGISTERED,
    STATUS_REFUSED,
    STATUS_REPLY,
)
from aiolibresync.frames import (
    MEDIA_HEADER,
    ControlFrameReader,
    MediaFrameReader,
    build_control,
    media_command,
    media_crc,
)

SOURCES = [
    (8, "Line"),
    (7, "AUX"),
    (6, "HDMI"),
    (5, "OPT1"),
    (4, "OPT2"),
    (3, "OPT3"),
    (2, "COAX"),
    (1, "USB"),
    (9, "Bluetooth"),
    (0, "Streaming"),
]

#: Byte 5 of `02 21`, keyed by JSON `ix`. A second enumeration, built
#: independently of the JSON one: `ix + 1` for the eight physical inputs, but
#: Streaming (`ix` 0) reports 10 and Bluetooth (`ix` 9) reports 1. Computing one
#: from the other is exactly the mistake docs/protocol-system-control.md warns
#: against, so the table is written out.
AV_SOURCE_BYTE = {
    8: 9,
    7: 8,
    6: 7,
    5: 6,
    4: 5,
    3: 4,
    2: 3,
    1: 2,
    9: 1,
    0: 10,
}


class ClientMediaFrameReader(MediaFrameReader):
    """Port 7777, read from the device's side: the length is little-endian here.

    `MediaFrameReader` reads it big-endian, which is right for every frame the
    library receives and wrong for every frame it sends — one rule, two
    directions. Only the fake ever parses the client's direction, so the
    little-endian half of that rule lives here rather than in the library.
    """

    def _total(self, header: bytes) -> int | None:
        if header[2] not in (MSG_QUERY, MSG_COMMAND):
            return None
        length = int.from_bytes(header[8:10], "little")
        if length > MAX_PAYLOAD:
            return None
        return MEDIA_HEADER + length


def device_frame(mid: int, payload: bytes = b"", status: int = STATUS_REPLY) -> bytes:
    """A device->client 7777 frame: 16-bit fields big-endian, real CRC."""
    frame = (
        b"\x00\x00\x02"
        + mid.to_bytes(2, "big")
        + bytes([status])
        + b"\x00\x00"
        + len(payload).to_bytes(2, "big")
        + payload
    )
    return frame[:6] + media_crc(frame).to_bytes(2, "big") + frame[8:]


@dataclass(frozen=True)
class FakeTLS:
    """Both ends of a mutual-TLS 7777, signed by a CA that exists for one test.

    Generated in memory on every call to `make`: no certificate or key is
    stored in this repository, and none belongs here. The hub's end is shaped
    like the newer hubs' 7777: TLS 1.2 at most, a client certificate required,
    and a certificate that does not name the address it is reached at — which
    is why the client's end verifies the chain and not the host name.
    """

    hub: ssl.SSLContext
    client: ssl.SSLContext
    #: Trusts the hub, presents nothing: what a hub that wants a client
    #: certificate has to refuse.
    client_without_certificate: ssl.SSLContext

    @classmethod
    def make(cls) -> "FakeTLS":
        ca = trustme.CA()
        hub = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        hub.maximum_version = ssl.TLSVersion.TLSv1_2
        hub.verify_mode = ssl.CERT_REQUIRED
        ca.issue_cert("hub.invalid").configure_cert(hub)
        ca.configure_trust(hub)

        def client_context() -> ssl.SSLContext:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            ca.configure_trust(context)
            return context

        client = client_context()
        ca.issue_cert("client.invalid").configure_cert(client)
        return cls(hub=hub, client=client, client_without_certificate=client_context())


class FakeHub:
    """Both ports on ephemeral local sockets.

    With `media_ssl`, 7777 speaks TLS with that context, as on the newer hubs;
    50006 is plain TCP either way, as it is on every hub. With `room_grouped`,
    room correction answers as the Surround Hub's does: set with `07 01 01 <x>`,
    read with `07 02 01` as `07 03 01 <x> 00`, and a bare `07 02` answered with
    address `00`.
    """

    def __init__(
        self, media_ssl: ssl.SSLContext | None = None, *, room_grouped: bool = False
    ) -> None:
        self.media_ssl = media_ssl
        self.room_grouped = room_grouped
        self.power = True
        self.volume = 28
        self.muted = False
        self.current_source = 0
        self.room_correction = True
        self.manual_eq = True
        self.eq_preset = 3
        self.play_state = 2
        #: `02 21` byte 8. A separate field from `play_state` because on the
        #: real hub the two agree on Streaming and nowhere else — which is the
        #: case worth being able to write a test for.
        self.audio_state = 2
        #: Whether box 0x3f refuses a read. Two contradictory observations of
        #: the same firmware: on 2026-08-15 it answered MUTE/UNMUTE, and on
        #: 2026-08-16 it came back `status 0x02` twice, hub off and hub playing.
        #: The friendlier one is the default so the older tests keep testing
        #: what they were written for; a test that wants the other flips this.
        self.refuse_mute_query = False
        # Off to model a hub that never acknowledges a transport command.
        self.ack_transport = True
        #: What box 231 holds: `<serial>,VALID` on the hub measured, fabricated
        #: here. A test for a unit with no valid factory serial sets this.
        self.serial_box = b"FAKE0SERIAL000000000,VALID"
        self.registered = False
        self.received: dict[str, list[bytes]] = {"control": [], "media": []}
        #: Everything the hub put on the wire, per port. The mirror of
        #: `received`, and the only way a test can ask what the *device* did
        #: rather than what the client's state ended up being — which is the
        #: question when a command's effects land on two ports.
        self.sent: dict[str, list[bytes]] = {"control": [], "media": []}
        self._control_writers: list[asyncio.StreamWriter] = []
        self._media_writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        self._control = await asyncio.start_server(self._handle_control, "127.0.0.1", 0)
        self._media = await asyncio.start_server(
            self._handle_media, "127.0.0.1", 0, ssl=self.media_ssl
        )

    async def stop(self) -> None:
        """Close every client first, then the servers. Safe to call twice.

        Since Python 3.12.1 `wait_closed()` waits for the server's transports to
        detach, not merely for the handlers to return: a writer still open makes
        it block for ever.
        """
        for writers in (self._control_writers, self._media_writers):
            for writer in list(writers):
                writer.close()
            writers.clear()
        for server in (self._control, self._media):
            server.close()
            await server.wait_closed()

    @property
    def control_port(self) -> int:
        return int(self._control.sockets[0].getsockname()[1])

    @property
    def media_port(self) -> int:
        return int(self._media.sockets[0].getsockname()[1])

    # --- port 50006 ---------------------------------------------------------

    def source_list(self) -> bytes:
        body = {
            "src": [
                {"ix": ix, "name": name, "sts": int(ix == self.current_source)}
                for ix, name in SOURCES
            ]
        }
        return build_control(CLASS_SYSTEM, 0x02, json.dumps(body).encode())

    def av_report(self) -> bytes:
        # 0 is not a byte the device ever sends here: selecting an index the
        # device has no source for gets a visibly wrong byte rather than a
        # KeyError that would kill the handler.
        source_byte = AV_SOURCE_BYTE.get(self.current_source, 0)
        # Byte 3 is audio activity on the selected input, and it is its own
        # attribute rather than `play_state` because on the real hub the two
        # disagree on every physical input: 7777 answers PLAYING to an empty USB
        # socket for as long as anyone asks, while this byte settles at IDLE
        # within a second. A fake that kept them equal could not express the one
        # case the library exists to get right. Set both to model Streaming.
        return build_control(
            CLASS_SYSTEM,
            0x21,
            bytes([source_byte, self.volume, int(self.muted), self.audio_state]),
        )

    def broadcast_control(self, frame: bytes) -> None:
        """Every device->client frame reaches every connected client.

        One `write` per writer, no `drain` and no per-writer error handling:
        every test attaches exactly one client. A second client whose socket
        broke would take this call — and so the handler that made it — down with
        it, so a multi-client test needs this guarded first.
        """
        self.sent["control"].append(frame)
        for writer in self._control_writers:
            writer.write(frame)

    async def _handle_control(self, reader, writer) -> None:
        self._control_writers.append(writer)
        framer = ControlFrameReader()
        try:
            while chunk := await reader.read(65536):
                for frame in framer.feed(chunk):
                    self.received["control"].append(frame)
                    self._answer_control(frame)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if writer in self._control_writers:
                self._control_writers.remove(writer)
            # A writer left open keeps its transport attached to the server, and
            # then stop() waits for it for ever.
            writer.close()

    def _answer_control(self, frame: bytes) -> None:
        cls, opcode, data = frame[3], frame[4], frame[5:]
        if cls == CLASS_SYSTEM:
            if opcode == 0x01:
                self.broadcast_control(self.source_list())
            elif opcode == 0x0D:
                self.broadcast_control(build_control(CLASS_SYSTEM, 0x0E, bytes([int(self.power)])))
            elif opcode == 0x0F:
                self.power = not self.power  # the data byte is ignored
                frame_out = build_control(CLASS_SYSTEM, 0x0E, bytes([int(self.power)]))
                self.broadcast_control(frame_out)
                self.broadcast_control(frame_out)  # sent twice, as the hub does
            elif opcode == 0x20:
                # The getter, sent to the real hub 2026-08-16: one `02 21`,
                # broadcast like every other device->client frame.
                self.broadcast_control(self.av_report())
            elif opcode == 0x03 and data:
                self.current_source = data[0]
                self.broadcast_control(self.source_list())
                self.broadcast_control(self.av_report())
        elif cls == CLASS_ROOM and self.room_grouped:
            if opcode == 0x01 and len(data) == 2 and data[0] == 0x01:
                self.room_correction = bool(data[1])
            elif opcode == 0x02:
                address = data[0] if data else 0x00
                state = int(self.room_correction) if address == 0x01 else 0x00
                self.broadcast_control(build_control(CLASS_ROOM, 0x03, bytes([address, state, 0])))
        elif cls == CLASS_ROOM:
            if opcode == 0x01 and data:
                self.room_correction = bool(data[0])  # and announces nothing
            elif opcode == 0x02:
                self.broadcast_control(
                    build_control(CLASS_ROOM, 0x03, bytes([int(self.room_correction)]))
                )
        elif cls == CLASS_EQ:
            if opcode == 0x04 and data:
                self.manual_eq = bool(data[0])
                self.broadcast_control(build_control(CLASS_EQ, 0x05, bytes([int(self.manual_eq)])))
            elif opcode == 0x05 and not data:
                self.broadcast_control(build_control(CLASS_EQ, 0x05, bytes([int(self.manual_eq)])))
        elif cls == CLASS_PRESET:
            if opcode == 0x02 and len(data) == 4:
                self.eq_preset = data[1]
                self.broadcast_control(self._preset_report())
            elif opcode == 0x05:
                self.broadcast_control(self._preset_report())

    def _preset_report(self) -> bytes:
        return build_control(
            CLASS_PRESET, 0x05, bytes([0x02, 0x04, self.eq_preset, 0x01, 0x01, 0x01])
        )

    # --- port 7777 ----------------------------------------------------------

    def playview(self) -> dict:
        """The PlayView JSON, reporting the play state the hub is actually in."""
        return {
            "CMD ID": 3,
            "Title": "PlayView",
            "Window CONTENTS": {
                "Album": "La Sentencia",
                "Artist": "Melissa Aldana",
                "TrackName": "La Sentencia",
                "CoverArtUrl": "https://resources.tidal.com/images/x/640x640.jpg",
                "CastContentApp": "TIDAL",
                "PlayState": self.play_state,
                "Current_time": -1,
                "TotalTime": 277597,
                "Current Source": 24,
            },
        }

    def push_media(self, frame: bytes) -> None:
        """Same single-peer assumption as broadcast_control: no drain, no
        per-writer guard."""
        self.sent["media"].append(frame)
        for writer in self._media_writers:
            writer.write(frame)

    async def _handle_media(self, reader, writer) -> None:
        self._media_writers.append(writer)
        framer = ClientMediaFrameReader()
        try:
            while chunk := await reader.read(65536):
                for frame in framer.feed(chunk):
                    self.received["media"].append(frame)
                    self._answer_media(frame)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if writer in self._media_writers:
                self._media_writers.remove(writer)
            writer.close()

    def _answer_media(self, frame: bytes) -> None:
        mid = media_command(frame, from_device=False)
        payload = frame[10:]
        if mid == MID_REGISTER:
            self.registered = True
            self.push_media(device_frame(MID_REGISTER))
            return
        if not self.registered:
            self.push_media(device_frame(0x0000, status=STATUS_NOT_REGISTERED))
            return
        if mid == MID_VOLUME:
            # Measured on 2026-08-15: a set that moves the level is echoed
            # twice and crosses to the other port as `02 21`, while a set to the
            # value the hub already holds is echoed once and crosses nowhere.
            # That second half is why the volume confirmation cannot demand the
            # cross-port report — see the two tests in test_client.py.
            changed = bool(payload) and int(payload) != self.volume
            if payload:
                self.volume = int(payload)
            out = device_frame(MID_VOLUME, str(self.volume).encode())
            self.push_media(out)
            # A query is answered twice here as well, which is stricter than the
            # device — it answers once — and must not be read the other way
            # round as evidence that the hub doubles query replies.
            if changed or not payload:
                self.push_media(out)
            if changed:
                self.broadcast_control(self.av_report())
        elif mid == MID_PLAY_STATE:
            self.push_media(device_frame(MID_PLAY_STATE, str(self.play_state).encode()))
        elif mid == MID_MUTE:
            # Reads answer. A write would be accepted and ignored, which is what
            # the real hub does — so the fake does not model one, because the
            # library has no way to send it.
            if self.refuse_mute_query:
                self.push_media(device_frame(MID_MUTE, b"", status=STATUS_REFUSED))
            else:
                self.push_media(device_frame(MID_MUTE, b"MUTE" if self.muted else b"UNMUTE"))
        elif mid == MID_PLAYVIEW:
            self.push_media(device_frame(MID_PLAYVIEW, json.dumps(self.playview()).encode()))
        elif mid == MID_MODEL:
            self.push_media(device_frame(MID_MODEL, b"Model:Stereo Hub"))
        elif mid == MID_SERIAL:
            self.push_media(device_frame(MID_SERIAL, self.serial_box))
        elif mid == MID_TRANSPORT:
            if self.ack_transport:
                self.push_media(device_frame(MID_TRANSPORT))
            # Like the hub: a command that changes nothing announces nothing.
            previous = self.play_state
            self.play_state = {b"PLAY": 0, b"PAUSE": 2, b"STOP": 1}.get(payload, self.play_state)
            if self.play_state != previous:
                self.push_media(device_frame(MID_PLAY_STATE, str(self.play_state).encode()))


class FakeHTTPServer:
    """Serves one document at one path, on 127.0.0.1 and an ephemeral port.

    `requests` records what was asked for, which is how the discovery tests
    prove a device that answered SSDP three times is fetched once.
    """

    def __init__(
        self,
        body: bytes,
        *,
        path: str = "/description.xml",
        status: str = "200 OK",
        chunked: bool = False,
    ) -> None:
        self._body = body
        self._path = path
        self._status = status
        self._chunked = chunked
        self._server: asyncio.Server | None = None
        self.requests: list[str] = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    def url(self, path: str | None = None) -> str:
        return f"http://127.0.0.1:{self.port}{path or self._path}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        self.requests.append(request.split(b" ")[1].decode())
        if self.requests[-1] != self._path:
            writer.write(
                b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
        elif self._chunked:
            parts = (self._body[:10], self._body[10:])
            writer.write(
                f"HTTP/1.1 {self._status}\r\nTransfer-Encoding: chunked\r\n"
                "Connection: close\r\n\r\n".encode()
                + b"".join(b"%x\r\n%s\r\n" % (len(part), part) for part in parts)
                + b"0\r\n\r\n"
            )
        else:
            writer.write(
                f"HTTP/1.1 {self._status}\r\nContent-Type: text/xml\r\n"
                f"Content-Length: {len(self._body)}\r\nConnection: close\r\n\r\n".encode()
                + self._body
            )
        await writer.drain()
        writer.close()


SSDP_TEMPLATE = (
    "HTTP/1.1 200 OK\r\n"
    "CACHE-CONTROL: max-age=1800\r\n"
    "EXT:\r\n"
    "LOCATION: {location}\r\n"
    "SERVER: Platform 1.0 CyberLinkC/3.0 UPnP/1.0 DLNADOC/1.50\r\n"
    "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
    "USN: {udn}::urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
    "\r\n"
)


class FakeSSDPDevice:
    """A UPnP device on 127.0.0.1: SSDP responder, description, control port.

    Everything binds an ephemeral port and the responder answers unicast — the
    tests point `discovery.SSDP_ADDRESS` and `discovery.SSDP_PORT` at it, so no
    test ever sends to the real multicast group.

    `replies` is how many datagrams it answers each search with. A real device
    answers a MediaRenderer search once and an `ssdp:all` search once per
    embedded device and service, and discovery has to collapse that.

    `group_host_only` ignores a search whose `HOST` header is not the multicast
    group, even one sent to the device alone, as the Stereo Hub HT does.
    """

    def __init__(
        self,
        description: bytes,
        *,
        udn: str = "uuid:1a2b3c4d-5e6f-11f0-9193-0123456789ab",
        control: bool = True,
        replies: int = 1,
        group_host_only: bool = False,
    ) -> None:
        self.udn = udn
        self.group_host_only = group_host_only
        self.searches = 0
        self.control_wanted = control
        self._replies = replies
        self._http = FakeHTTPServer(description)
        self._control: asyncio.Server | None = None
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        await self._http.start()
        if self.control_wanted:
            self._control = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: self._Responder(self), local_addr=("127.0.0.1", 0)
        )
        self._transport = transport

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        if self._control is not None:
            self._control.close()
            await self._control.wait_closed()
            self._control = None
        await self._http.stop()

    @property
    def ssdp_port(self) -> int:
        assert self._transport is not None
        return int(self._transport.get_extra_info("socket").getsockname()[1])

    @property
    def control_port(self) -> int:
        assert self._control is not None
        return int(self._control.sockets[0].getsockname()[1])

    @property
    def http_port(self) -> int:
        return self._http.port

    @property
    def description_requests(self) -> list[str]:
        return self._http.requests

    def location(self) -> str:
        return self._http.url()

    async def _accept(self, _reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    class _Responder(asyncio.DatagramProtocol):
        def __init__(self, device: "FakeSSDPDevice") -> None:
            self._device = device
            self._transport: asyncio.DatagramTransport | None = None

        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            assert isinstance(transport, asyncio.DatagramTransport)
            self._transport = transport

        def datagram_received(self, data: bytes, addr) -> None:
            if not data.startswith(b"M-SEARCH") or self._transport is None:
                return
            self._device.searches += 1
            if self._device.group_host_only and b"HOST: 239.255.255.250:" not in data:
                return
            reply = SSDP_TEMPLATE.format(
                location=self._device.location(), udn=self._device.udn
            ).encode()
            for _ in range(self._device._replies):
                self._transport.sendto(reply, addr)
