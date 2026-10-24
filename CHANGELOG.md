# Changelog

## 0.6.0b3 — 2026-10-24

- `LibreSyncClient(media_ssl=...)` opens port 7777 over TLS with an `ssl.SSLContext` from the caller, for the newer hubs whose 7777 is wrapped in mutual TLS. The frames inside the tunnel are the same, so registration, volume, transport and metadata work as on the original hub. Port 50006 stays plain TCP. The library bundles no certificate and builds no context. Measured on a Platin Surround Hub.
- A TLS handshake that fails takes the same reconnect backoff as a host that is down, and is logged at warning level once per run of failures, then at debug level.
- `media_ssl` together with `media=False` raises `ValueError`.
- Room correction is read on the LS10 hubs too. Their `07 03` report puts an address byte before the state, `07 03 01 <state>` on the Stereo Hub HT and `07 03 01 <state> 00` on the Surround Hub, and both are decoded. A report for another address, which is how the Surround Hub answers the bare `07 02`, sets nothing.
- `LibreSyncClient(module=...)` takes the module from LSSDP. With `LS10`, room correction is set with `07 01 01 <state>` and read with `07 02 01` as well as `07 02`, the vendor app's frames on a Surround Hub. On a Stereo Hub HT the set is untested. `diagnostics()` reports the module.

## 0.6.0b2 — 2026-10-23

- The unicast M-SEARCH that `async_probe` sends to a single host names the multicast group in its `HOST` header, as a multicast search does. The Stereo Hub HT ignores a search that names its own address, so 0.6.0b1 found no description on it and the hub had no identity. The original Stereo Hub answers either form.

## 0.6.0b1 — 2026-10-23

Beta, for the newer Platin hubs (Stereo Hub HT, Surround Hub). Everything is backward compatible: code written for 0.5 runs unchanged.

- `async_probe` finds a device description served on a port of the hub's own choosing. When nothing answers on 38400, it asks the host alone with a unicast M-SEARCH, resent every second for up to 10 s, and follows the `LOCATION` of the reply. A host name is resolved first.
- The `C4A Wireless Speaker` model description of the newer hubs is recognised as LibreSync.
- `async_query_lssdp(host)` asks a hub on Libre's discovery port, UDP 1800, and returns an `LssdpInfo` with the module, model, firmware and media port. The module is `LS9` on the original Stereo Hub and `LS10` on the newer ones. `async_probe` reports it as `DiscoveredDevice.module`.
- `LibreSyncClient(media=False)` uses port 50006 alone and is available when it is, for hubs whose 7777 is wrapped in mutual TLS. The 7777 commands then raise `NotConnectedError`.
- A socket error on the UDP queries costs `async_probe` the module or the identity, never the device.

## 0.5.1 — 2026-09-26

- `async_media_play()`, `async_media_pause()` and `async_media_stop()` are confirmed by the device's acknowledgement of the command, an empty `0x0028` reply, instead of by a play state. A command that changes nothing, such as a stop on a hub that is already stopped, produces no play state, so it used to raise `ConfirmationTimeout` after the full timeout although it had been accepted. Measured on a Platin Stereo Hub: the acknowledgement arrives within about 10 ms, also when nothing changes.
- `media.is_transport_ack()` recognises that acknowledgement.

## 0.5.0 — 2026-09-26

- `async_connect(timeout=...)` waits until both ports are open and raises `NotConnectedError` if they are not in time. On that error, and on cancellation, it disconnects again first, so a failed attempt leaves no reconnect loop or poll running. Without `timeout` it behaves as before.
- `state.available` and `Connection.connected` turn true only once the connect-time sequence has run, which on 7777 is the session registration. Before, they turned true at the TCP connect, and a command sent at once could be refused as unregistered.
- A command's confirmation waiter is armed before the command is sent, so a reply that arrives while the write is still draining is no longer missed.
- `async_disconnect()` no longer swallows a cancellation aimed at its caller while it waits for the poll loop or a socket task to end.
- `async_discover()` follows an SSDP reply only when its `LOCATION` names the address the reply came from, and follows up at most 64 hosts per search.
- `async_probe_control` is exported: the control-port check on its own, for a caller that already has the device description.
- `__version__` is read from the installed package metadata, and is `0+unknown` without it. It had been left at `0.2.0`.
- A 7777 session the device has forgotten is dropped directly, without a background task. The cooldown still limits recovery to one per `REREGISTER_COOLDOWN`.
- `frames.crc16_ccitt` uses `binascii.crc_hqx`. The result is unchanged.

## 0.4.0 — 2026-09-26

- The factory serial, read from message box 231: `DeviceState.serial` on a connected client, and `async_read_serial(host)` for a hub that is not connected. It is the preferred stable identifier, because it does not depend on the UPnP daemon. `None` when the unit has no serial marked valid.
- `diagnostics()` redacts the serial.
- The connect-time queries include box 231.

## 0.3.0 — 2026-09-26

First public release.

- `LibreSyncClient`: both ports, push-driven state, reconnection, confirmation of every command, a slow poll for power and room correction, and `diagnostics()`.
- `DeviceState.playback`: the play state to believe, from `audio_state` (whether sound is coming out) where it is known.
- `async_discover()` and `async_probe()`: SSDP discovery, device description, and a control-port check that sends nothing.
- `DeviceState.eq_preset` can be `0` while the vendor app's EQ editor is open.
