# aiolibresync

An asyncio client for audio hubs built on the Libre Wireless **LibreSync** platform, such as the Platin Stereo Hub. It controls them over the local network, with no vendor app and no cloud.

The same hardware and protocol are sold under at least a dozen brands, including Platin, Buchardt, System Audio, Econik and Triangle. See [docs/devices.md](docs/devices.md) for the list and how confident each entry is.

- Pure Python, no runtime dependencies, fully typed.
- Push-driven: the hub announces volume, source, transport and metadata as they change, and a slow poll covers the two properties nothing announces.
- Discovery over SSDP, plus a probe for an address you already know.
- Written against the Home Assistant integration quality scale, but it does not depend on Home Assistant.

## Status

Alpha. Everything the library sends has been sent to a real hub and its effect observed, on a Platin Stereo Hub running firmware 1.52. Other brands and firmware versions have not been tested yet, and reports from owners are very welcome. `diagnostics()` produces a redacted dump designed for exactly that.

## Install

```bash
pip install aiolibresync
```

Python 3.12 or later.

## Finding a hub

```python
from aiolibresync import async_discover, async_probe

for device in await async_discover():
    print(device.host, device.udn, device.name)

device = await async_probe("192.168.1.50")  # an address you already have
```

`async_discover()` sends an SSDP search, fetches the device description from whatever answers, and confirms that the control port is open. The probe sends nothing to the control port: it only opens a TCP connection.

Two stable identifiers are available. The **factory serial** (`await async_read_serial(host)`, or `state.serial` on a connected client) does not depend on the UPnP daemon and is the one to prefer. The **UDN** (`device.udn`) is what discovery sees. Either can be `None`: a unit may lack a valid factory serial, and the UDN is served by a UPnP daemon that occasionally stops on its own until the hub is power-cycled. See [docs/devices.md](docs/devices.md#identity).

## Controlling it

```python
import asyncio
from aiolibresync import DeviceState, LibreSyncClient

async def main() -> None:
    client = LibreSyncClient("192.168.1.50")

    def on_state(state: DeviceState) -> None:
        print(state)

    client.subscribe(on_state)
    await client.async_connect(timeout=10)
    await client.async_set_volume(30)
    await asyncio.sleep(60)
    await client.async_disconnect()

asyncio.run(main())
```

With a `timeout`, `async_connect()` returns once both ports are open, and raises `NotConnectedError` if they are not in time, after disconnecting again so that nothing is left running. Without one it returns as soon as the two sockets have been *scheduled*, so a command issued on the next line raises `NotConnectedError`. Either way `state.available` becomes false on any disconnection and true again when the client has reconnected on its own, so treat it as a condition that can change at any time.

| Method | What it does |
| --- | --- |
| `async_set_power(on)` | reads the power state first and toggles only if needed, because the device has no discrete on or off |
| `async_select_source(index)` | selects a source by the device's own index, from `state.sources` |
| `async_set_volume(level)` | 0–100 |
| `async_media_play()`, `_pause()`, `_stop()`, `_next_track()`, `_previous_track()` | transport, for the streaming renderer |
| `async_set_room_correction(enabled)`, `async_set_manual_eq(enabled)` | the two DSP switches |
| `async_select_eq_preset(preset)` | 1–3, the presets built in the vendor's app |
| `async_refresh()` | re-reads everything |
| `subscribe(callback)` | called with a new `DeviceState` on every change; returns an unsubscribe function |
| `diagnostics()` | a redacted snapshot with frame counters and any unrecognised frames |

Every command waits for the device to confirm the new state and raises `ConfirmationTimeout` if it does not.

### The newer hubs

On the Surround Hub, port 7777 is wrapped in mutual TLS. The Stereo Hub HT is built on the same module (`LS10`, see `async_query_lssdp`) and resets a plaintext registration the same way, so it very likely is too, but nobody has run a handshake against one yet. Either pass `media=False` to use port 50006 alone, or pass `media_ssl`, an `ssl.SSLContext` carrying a client certificate the hub accepts, and the client works as it does on the original hub. The library bundles no certificate, reads no file and builds no context. Loading a certificate with `load_cert_chain` is blocking I/O, so in Home Assistant build the context in the executor. See [docs/protocol-media.md](docs/protocol-media.md#tls-on-the-newer-hubs).

## Things that will surprise you

**Power is not power.** Switching the hub off is a *stop*: it ends the playback session. Switching it back on restores nothing and only allows playback again. When playback starts on a hub that is off, power-on arrives *last*, as a consequence of the session starting. The hub answers on both ports while "off". Keep `available` and `power` apart.

**There is no mute you can set.** The device accepts a mute write, reports the new value back, and leaves the audio alone. `state.muted` reflects mute set from the remote or the vendor's app, but the library offers no way to set it. See [docs/protocol-media.md](docs/protocol-media.md).

**Read `state.playback`, not `state.play_state`.** The hub reports playback twice. `play_state` is the streaming renderer's transport, and it says `PLAYING` on any physical input whether or not anything is connected. `audio_state` says whether sound is actually coming out. `playback` gives you the one to believe.

**Room correction is never announced.** It is polled, so a change made in the vendor's app shows up within one poll interval (`POLL_INTERVAL`, 30 s).

**Preset 0 is the vendor app's EQ editor.** While someone has the editor open, `state.eq_preset` is `0`. It is a legitimate state, not an error.

## Documentation

| | |
| --- | --- |
| [docs/devices.md](docs/devices.md) | what the device is, which brands share it, how a hub is found and identified |
| [docs/protocol-system-control.md](docs/protocol-system-control.md) | port 50006: framing, events, power, what must be polled |
| [docs/protocol-media.md](docs/protocol-media.md) | port 7777: the LUCI media session, message boxes, metadata |
| [docs/commands.md](docs/commands.md) | every known frame on both ports, with how firmly each is known |

Everything here was established by observing a real hub. Each frame in the reference is labelled with how it is known, from sent-and-observed down to inferred.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
```

The tests run the codec against real traffic captured from a hub, with personal data replaced (see [tests/fixtures/README.md](tests/fixtures/README.md)), and the client against a fake hub that speaks both ports.

## Legal

Independent reverse engineering, for interoperability. Not affiliated with Platin, Hansong, or Libre Wireless Technologies. LibreSync is a trademark of Libre Wireless Technologies, Inc. MIT licensed.
