"""Async client for Libre Wireless LibreSync audio devices.

    import asyncio
    from aiolibresync import DeviceState, LibreSyncClient

    async def main() -> None:
        client = LibreSyncClient("192.168.1.50")

        def on_state(state: DeviceState) -> None:
            print(state)

        client.subscribe(on_state)
        await client.async_connect(timeout=10)
        await client.async_set_power(True)
        await asyncio.sleep(60)
        await client.async_disconnect()

    asyncio.run(main())

With a `timeout`, `async_connect()` returns once both ports are open and raises
`NotConnectedError` if they are not in time, leaving nothing running. Without
one it returns as soon as the two sockets have been *scheduled*, so a command
issued on the next line raises `NotConnectedError`. Either way,
`state.available` goes false again on any disconnection and true when the
client has reconnected on its own, so a long-running program should treat it
as a condition, not a one-off.

`DeviceState` reports playback twice, because the hub does. `play_state` is the
streaming renderer's transport and answers `PLAYING` to a physical input with
nothing connected to it; `audio_state` is whether sound is coming out, from
`02 21` byte 8, and is right on all ten sources. **Read `state.playback`**,
which is the second wherever it is known and the first otherwise.

Finding a hub, when its address is not already known:

    from aiolibresync import async_discover

    for device in await async_discover():
        print(device.host, device.udn, device.name)

`async_discover()` sends a multicast search and waits the full timeout — it does
not return early on the first answer — and `async_probe(host)` performs the same
identification for an address the user already has. `async_probe_control(host)`
is the last of those steps alone, for a caller that already holds the device
description, such as Home Assistant's own SSDP discovery. All three are
read-only: a search, an HTTP GET of the device description, and a TCP connect to
50006 that sends nothing. `async_read_serial(host)` is not: it registers a
session on 7777, so call it only for a host already believed to be a hub.
`async_query_lssdp(host)` asks the hub on Libre's own discovery port, UDP 1800,
and is read-only too. Its `module` is `LS9` on the original Stereo Hub and `LS10`
on the newer hubs, whose 7777 this version cannot open: it tells them apart
before anything touches 7777.

The codec is importable on its own — `aiolibresync.frames`,
`aiolibresync.systemcontrol`, `aiolibresync.media` — and does no I/O.
`frames.build_media` is deliberately not re-exported here: its `command`
argument is a LUCI message-box id, and the box space includes factory reset.
Use the named operations in `aiolibresync.media` instead.
"""

from importlib.metadata import PackageNotFoundError, version

from .client import LibreSyncClient
from .const import CONTROL_PORT, MEDIA_PORT, POLL_INTERVAL
from .discovery import (
    DiscoveredDevice,
    LssdpInfo,
    async_discover,
    async_probe,
    async_probe_control,
    async_query_lssdp,
    async_read_serial,
)
from .exceptions import (
    ConfirmationTimeout,
    LibreSyncError,
    NotConnectedError,
    UnsafeCommandError,
)
from .state import DeviceState, NowPlaying, PlayState, Source

try:
    __version__ = version(__name__)
except PackageNotFoundError:  # imported from a checkout that was never installed
    __version__ = "0+unknown"

__all__ = [
    "CONTROL_PORT",
    "MEDIA_PORT",
    "POLL_INTERVAL",
    "ConfirmationTimeout",
    "DeviceState",
    "DiscoveredDevice",
    "LibreSyncClient",
    "LibreSyncError",
    "LssdpInfo",
    "NotConnectedError",
    "NowPlaying",
    "PlayState",
    "Source",
    "UnsafeCommandError",
    "async_discover",
    "async_probe",
    "async_probe_control",
    "async_query_lssdp",
    "async_read_serial",
]
