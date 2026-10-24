# Port 7777: the LUCI media session protocol

Port 7777 is the hub's media session: a stateful TCP protocol over which a client registers, controls playback and volume, and receives pushed now-playing state. It is one of two client ports. The other, port 50006, carries source selection, power, mute and DSP settings and is described in [protocol-system-control.md](protocol-system-control.md). A complete client needs both. The full byte table for both ports is in [commands.md](commands.md), and what the device is and how to find it on the network is in [devices.md](devices.md).

Everything here was established on firmware 1.52 of one Platin Stereo Hub. Other LibreSync products and other firmware versions may differ.

## Evidence levels

Rows and statements carry one of these labels where the distinction matters:

- `tested`: sent to the device and confirmed by an effect of a different nature (an event, a change on the other port, or something audible).
- `capture`: observed on the wire, not provoked and confirmed independently.
- `binary`: read from static analysis of the device firmware (a string, a symbol or an instruction sequence). A symbol name that suggests a meaning is structure, not confirmation.
- `derived`: reconstructed from the frame rules, never sent to a device.
- `claimed`: asserted by earlier third-party material and not verified.

## What the port is

The daemon behind port 7777 is `luci_service`, the front end of the device's internal LUCI message bus. Port 7777 has no command vocabulary of its own: every frame names a **LUCI message box**, and the daemon services whichever box the frame names.

The port stays up when the hub looks "off" (network standby), and the registration exchange is identical in both states. **A successful connection or registration says nothing about power state.** Power is reported on port 50006.

> **Do not sweep this port.** Because the command field is a message-box id, an arbitrary value reaches whatever the bus attaches to that box, and somewhere in that space are factory reset and firmware upgrade. Port 50006 cannot reach those; port 7777 can. Send only boxes whose effect is known.

### TLS on the newer hubs

On the Surround Hub, built around Libre's `LS10` module, port 7777 is wrapped in TLS 1.2 with mutual authentication: the hub presents a certificate and requires one from the client (`tested`). The Stereo Hub HT is `LS10` too and resets a plaintext REGISTER at once, which fits, but no handshake has been tried on one. `async_query_lssdp` tells the module before anything touches 7777. Port 50006 stays plain TCP on these hubs.

Inside the tunnel nothing changes (`tested` on a Surround Hub). The frames, the byte order and the registration with the client's own address are the ones described below, and registration, volume read and set, transport and now-playing metadata all worked through it unchanged. The hub's certificate is not issued for its IP address, so a client that verifies it has to leave host-name checking off.

`LibreSyncClient(media_ssl=...)` takes a ready `ssl.SSLContext` carrying a client certificate the hub accepts, and opens 7777 with it. The library bundles no certificate, reads no file and builds no context. Without one, `media=False` runs the client on port 50006 alone.

## Frame format

Both directions share one 10-byte header followed by the payload.

```
+---------------+----------+---------------+--------+---------+-------------+
|   RemoteID    | msg_type |    command    | status | CRC-16  |   length    |  payload...
+---------------+----------+---------------+--------+---------+-------------+
      0..1           2            3..4         5       6..7       8..9          10..
```

| Field | Size | Notes |
| --- | --- | --- |
| `RemoteID` | 2 | `00 00` for a local client |
| `msg_type` | 1 | `0x01` query, `0x02` command or pushed event |
| `command` | 2 | a 16-bit LUCI message-box id (MID) |
| `status` | 1 | `0x00` unsolicited, `0x01` reply (and the `0x01ee` push), `0x02` refused, `0x03` not registered; see [Status byte](#status-byte) |
| `CRC-16` | 2 | CRC-16/XMODEM on device frames, `0x0000` from clients; see [Bytes 6..7](#bytes-67-crc-16xmodem) |
| `length` | 2 | payload byte count |

The layout matches the parser in `luci_service` (`handleCmdPacket`), which reads exactly these offsets and logs `RemoteID = %d Command = %d Type = %d Status = %d CRC = %d Len = %d` (`binary`). A payload longer than `MID_LENGTH_MAX` (`0x1400`, 5120 bytes) is ignored by the daemon.

`msg_type` is `0x01` on queries and `0x02` on commands and pushed events in every frame observed (`capture`).

### Byte order

**Every 16-bit field is big-endian when the device writes it and little-endian when the client writes it.** That one rule covers both `command` and `length`.

For every MID below 256 the client form is `00 00 <msg_type> <mid> 00 00 00 00 <len> 00 <payload>`, the same bytes the vendor's app sends. On device frames the MID's high byte lands at offset 3 and its low byte at offset 4.

> **Caution: `command` is two bytes, not one.** Reading offset 4 alone as "the command" and offset 5 as a flag works until a MID above 255 arrives, and then it invents a field. The device frame `00 00 02 01 ee 01 30 91 00 00` is MID `0x01ee` (494) with `status 0x01`, not command `0xee` with a flag.

> **Endianness caveat on requests.** Every request payload observed or sent is under 256 bytes, so on the request side a little-endian 16-bit length and "one length byte plus one zero byte" are indistinguishable. Device responses reach 493 bytes and are unambiguously big-endian. A request with a payload of 256 bytes or more needs re-testing.

### Status byte

Byte 5 sits outside the CRC. Values observed:

| Value | Seen on | Evidence |
| --- | --- | --- |
| `0x00` | unsolicited pushes: volume, play state, position, metadata | capture |
| `0x01` | replies to a request, **and** the unsolicited `0x01ee` track-change push | tested |
| `0x02` | refused request; boxes `0x6b` (107) and `0xdb` (219) answer this way on this model, with an empty payload | capture |
| `0x03` | session not registered; see [Session registration](#session-registration) | tested |
| `0x04` | seen once, on one query response; meaning unknown | capture |

**`status` is not a push/reply discriminator.** `0x01` appears on an unsolicited push as well as on replies, and what it means there is unknown. Dispatch on the message box, never on `status`. `0x02` and `0x03` are unambiguous and are the two worth acting on.

**An empty payload is not a value.** A refused request is empty too, so a client must not decode an empty reply as "zero" or "cleared".

### Bytes 6..7: CRC-16/XMODEM

> **Caution: bytes 6..7 look like a per-message session or sequence token. They are not.** They are a checksum, deterministic over header and payload: the same frame produces the same two bytes minutes apart and across a power cycle.

The algorithm is **CRC-16/XMODEM**: polynomial `0x1021`, init `0x0000`, no input or output reflection, no final xor. It is computed over the frame **with bytes 6..7 removed**, not zeroed in place: `header[0:6] || length || payload`.

```python
def media_crc(frame: bytes) -> int:
    crc = 0
    for byte in frame[0:6] + frame[8:]:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc
```

The library implements it as `crc16_ccitt` and `media_crc` in [frames.py](../src/aiolibresync/frames.py). It holds for every device-to-client frame observed, across two independent sessions seven months apart, with no exceptions. Device frames that can serve as test vectors (`msg_type 0x02`, `status 0x00` unless noted):

| MID | Payload | Bytes 6..7 |
| --- | --- | --- |
| `0x0040` | `28` | `5f 7a` |
| `0x0040` | `31` | `fd 62` |
| `0x0033` | `1` | `97 9c` |
| `0x0031` | `32563` | `58 78` |
| `0x01ee` | empty, `status 0x01` | `30 91` |

**Clients send `0x0000` and the device accepts it**, which is what the vendor's app does. A request carrying a deliberately wrong non-zero value (`0xaabb`) was also accepted and answered normally (`tested`), so on the TCP request path the field is not checked at all. The firmware contains the strings `CRC EQUAL OR ZERO` and `CRC missmatch discard packet`; they belong to the UART and Bluetooth LE transports of the same packet format (see [The 0x70 tunnel](#the-0x70-tunnel)), where corruption is a real possibility.

Recommended practice: verify the CRC on receive if you want to detect corruption, and send zero.

## Session registration

The first frame a client sends is a command to box `0x03` whose payload is the client's own IP address in ASCII:

```
00 00 02 03 00 00 00 00 0c 00 "192.168.1.20"
```

The hub replies with an empty `0x03` frame, `status 0x01`. Wait about 100 ms after registering before sending the first command; the vendor's app does the same.

**Registration is mandatory** (`tested`). A query on an unregistered connection is answered

```
00 00 00 00 00 03 xx xx 00 00      MID 0x0000, status 0x03, no payload
```

and the request is dropped. The reply names box `0` rather than the box asked for, which is easy to misread as a framing bug. After registration the same query answers normally with `status 0x01`.

Send the real local address of the interface that reaches the hub, not `127.0.0.1`. The registered IP appears to decide where pushes are delivered (see [Where pushes are delivered](#where-pushes-are-delivered)), and clients that register as `127.0.0.1` have been reported to receive fewer events.

## Commands

Commands use `msg_type 0x02`. The client writes MIDs little-endian; the column shows the MID value.

| MID | Payload | Meaning | Evidence |
| --- | --- | --- | --- |
| `0x0003` | client IP, ASCII | register session | tested |
| `0x0028` | `PLAY` `PAUSE` `STOP` `NEXT` `PREV`, ASCII | transport control | tested |
| `0x0040` | `0`–`100`, ASCII | set volume | tested |
| `0x00d0` | `READ_Model`, ASCII | read model; answered `Model:Stereo Hub` | capture |
| `0x003f` | `MUTE` / `UNMUTE` | **accepted and ineffective**; do not use, see [Writing a box is not commanding the device](#writing-a-box-is-not-commanding-the-device) | tested |

Transport commands are acknowledged with an empty `0x0028` frame, `status 0x01`, followed by the resulting events. `PAUSE` produces `0x33 "2"`, `PLAY` produces `0x33 "0"`, and a volume set produces the `0x40` event twice, just as the same actions from the RF remote do. The transport payloads are the vendor's own strings, and the device behaves the same whichever side triggers an action. A command that changes nothing is acknowledged all the same and followed by nothing: a `STOP` sent to a hub that is already stopped gets the `0x0028` reply within about 10 ms and no other frame (`tested`, 2026-09-26). The acknowledgement, not a play state, is therefore what confirms a transport command.

Mute cannot be set from either port. Its state is read here with a `0x003f` query, or from `02 21` on port 50006; see [protocol-system-control.md](protocol-system-control.md).

## Pushed events

Pushes use `msg_type 0x02` and arrive unsolicited on a registered session. No subscription is needed beyond registration.

| MID | Payload | Meaning | Evidence |
| --- | --- | --- | --- |
| `0x0031` | milliseconds, ASCII | playback position. Once a second while playing; once a minute, unchanged, while paused. **Signed**: goes to `-1000` on `STOP` | tested |
| `0x0033` | `0` `1` `2` `5`, ASCII | play state: `0` playing, `1` stopped/idle, `2` paused, `5` loading | tested |
| `0x003f` | `MUTE` / `UNMUTE` | mute state changed | capture |
| `0x0040` | `0`–`100`, ASCII | volume changed; same box as the set-volume command | capture |
| `0x002a` | PlayView JSON | now-playing metadata, on every track change | capture |
| `0x002d` | PlayView JSON | byte-identical to the `0x2a` sent with it, in the same millisecond | capture |
| `0x0046` | `SPEAKER_ACTIVE,<n>` / `SPEAKER_INACTIVE,<n>` | WiSA speakers active or idle; `<n>` is the internal source id. `INACTIVE` arrives on `STOP` | tested |
| `0x00e6` | `48000` | sample rate of the new track | capture |
| `0x01ee` | empty, `status 0x01` | emitted on every track change, between `0x33 "5"` and the metadata | capture |
| `0x000a` | `24` `25`, ASCII | internal source id, at source transitions | capture |
| `0x0032` | `24` `25`, ASCII | same value as `0x0a`, sent with it | capture |
| `0x0070` | binary | tunnel frames; see [The 0x70 tunnel](#the-0x70-tunnel) | tested |

Unidentified boxes that also appear are listed under [Observed but unidentified](#observed-but-unidentified).

**`0x40` volume events arrive duplicated**: two byte-identical frames within a few milliseconds. `0x3f` mute events do not. A reader must be idempotent.

**`0x2a` and `0x2d` are a pair.** A reader that acts on both handles every track change twice. Decoding both into state is harmless only if state updates are compared before being announced.

### Track change

Every track change, whether from next/previous or from a track ending on its own, produces the same sequence:

```
0x33 "1"  ->  0x33 "5"  ->  0x01ee  ->  0xe6 "48000"
          ->  0x2a JSON  +  0x2d JSON  ->  0x33 "0"  ->  0x46 SPEAKER_ACTIVE,<n>
```

`0x33` passes through `1` because the device goes idle between tracks. The position is reset along the way.

**Next and previous are indistinguishable on the wire** (`tested`). `NEXT` and `PREV` produce byte-identical sequences: the device reports the resulting track, not the button. A client can tell which way the queue moved only by comparing track names.

### Stop

`STOP` does not add a play-state value. `0x33` goes to `1` and stays there; a `0x33` query afterwards returns `1`. That is what identifies `1` as stopped/idle. The rest of the stop is announced by other boxes:

```
0x33 "1"                    idle
0x46 "SPEAKER_INACTIVE,24"  speakers released
0x31 "-1000"                position sentinel, negative
0xe0 "99"                   unidentified
0x28 status 0x01            the acknowledgement
```

On port 50006, the audio/video report `02 21` repeats source, volume and mute unchanged, with byte 8 going to `01` (see [protocol-system-control.md](protocol-system-control.md)).

Two things a client must get right:

- **`0x31` is signed.** `-1000` means "no position". Parsed unsigned it becomes a huge number and any seek UI misbehaves.
- **`SPEAKER_INACTIVE` is the reliable stop signal.** The play state alone cannot separate stopped from mid-transition, because both read `1`.

### Play state lies on physical inputs

Selecting a physical input pushes `0x33 "1"`, then `0x33 "0"` (playing) about a second later, together with `SPEAKER_ACTIVE`. It stays at `0` regardless of whether anything is connected: queried six times over three minutes with nothing plugged into the USB port, the box answered `0` every time, and HDMI with the television switched off behaves the same (`tested`).

**It does not reset either.** Selecting Streaming again pushes nothing on port 7777, so the box keeps the `0` it was given by the physical input. Eleven minutes and one source change later, with no cast session anywhere and port 50006 reporting no audio, a query still answered `0`. Only a real streaming session moves it.

The play state is therefore a property of the streaming renderer; on any other input it degrades to "an input was selected at some point". **Byte 8 of port 50006's `02 21` report is the only field on either port that says whether sound is actually playing.** A client that maps `0x33` straight onto a "playing" indicator will show playing over silence on every physical input, and on Streaming too until something casts to it.

## Queries

Queries use `msg_type 0x01` and an empty payload. Replies carry `status 0x01` and the same MID.

| MID | Response payload | Meaning | Evidence |
| --- | --- | --- | --- |
| `0x002a` | JSON, about 493 bytes | PlayView: full now-playing state | capture |
| `0x0033` | e.g. `"0"` | play state; returns `1` after a `STOP` | tested |
| `0x003f` | `MUTE` / `UNMUTE` | mute state | tested |
| `0x0040` | e.g. `"43"` | volume | tested |
| `0x0005` | e.g. `"15262"` | firmware incremental build number | tested |
| `0x00e7` | `<serial>,VALID`, 26 bytes on the hub measured | the factory serial; see [devices.md](devices.md#identity) | tested |
| `0x0032` | e.g. `"25"` | unknown; the value matches the internal source id pushed on `0x32` | capture |
| `0x006b` | empty, `status 0x02` | refused on this model | capture |
| `0x00db` | empty, `status 0x02` | refused on this model | capture |

`0x32` and `0x40` return different numbers in the same session (`25` against `43`), so `0x32` is not a second volume.

## Writing a box is not commanding the device

Volume can be set by writing box `0x40`, the same box that reports it. Mute cannot be set by writing box `0x3f`, though the write looks successful:

| | Reply | `02 21` on 50006 | `0x3f` event | Re-read of `0x3f` | Audio |
| --- | --- | --- | --- | --- | --- |
| mute from the RF remote | — | byte 7 set | `MUTE` | — | silent |
| `02 003f "MUTE"` from a client | none | **nothing** | **nothing** | `MUTE` | **unchanged** |

The write is accepted (no `status 0x02`), it changes the value that reads back, and the device does not act on it (`tested`). Box `0x3f` is a mirror that the audio microcontroller updates on its way up; a client write changes the mirror and nothing reaches the hardware.

The firmware exports separate `writeLocalMessage`, `writeRemoteMessage` and `readRemoteMessage` entry points, which suggests each box has a local and a remote copy, and that which one a write reaches decides whether anything happens (`binary`, hypothesis). Volume works because its box is backed by the environment item `current_volume`, which something propagates. **"Read one way, written the other" is a property of particular boxes, not of the bus**, and nothing on the wire tells which boxes have it.

### The rule for every command on this port

**Reading back the value you wrote is not evidence that the command worked.** Confirm with an effect of a different nature: an event on the other port, a change in `02 21`, or something audible. Every command marked `tested` above meets that standard.

## Where pushes are delivered

Two observations suggest pushes are routed by the registered IP rather than by socket (`capture`, not yet a rule):

- The reply to a query arrived on a different port-7777 session held by the same process from the same address, rather than on the socket that asked.
- `0x31` position pushes reached neither of two sessions registered from the same address seconds apart, while a single session registered from that address immediately afterwards received them once a second.

Registration announces an IP address, not a socket, which makes this plausible, and it would also explain why registering as `127.0.0.1` costs events. The evidence is not enough to state how delivery works. It already implies one rule for clients: **the absence of pushes on your session is not evidence about the device.** Prefer one registered session per client address, and do not conclude that the device did nothing because your socket heard nothing.

## The 0x70 tunnel

Box `0x70` (112) carries **port-50006 System Control frames verbatim**. On a registered 7777 session, `0x70` frames arrive whose payload is a run of complete 50006 frames, for example:

```
00 03 03 02 01   00 03 0f 02 0d   00 03 4c 02 4a
00 03 4f 02 4d   00 03 8d 02 8b
```

These are exactly the queries another client was sending on port 50006 at the time.

### It mirrors requests, not events

The tunnel carries **client-to-device frames only** (`tested`). With a 7777 session listening, queries sent on a separate 50006 connection appear as `0x70` payloads, verbatim; the device's replies to them never appear. Every `0x70` payload observed is a well-formed client-to-device frame with a valid checksum, and none carries the `0x00` direction marker of a device event.

So port 7777 alone is **not** a sufficient single connection: it shows what other clients are asking for, not what the device answers. A client needs port 50006 for device state. On that port, the device broadcasts its frames to every connected client, so a listener does not have to be the asker (see [protocol-system-control.md](protocol-system-control.md)).

### What the tunnel is

Static analysis of the device firmware shows the mechanism (`binary`):

- The daemon serving port 50006, `MessageBoxHandler`, puts every byte a TCP client sends into message box 112, unparsed; the MID is a constant in the code. Every change of box 112 is sent, unparsed, to every connected 50006 client. **Port 50006 is message box 112**, and the `0x70` frames on this port and the whole of [protocol-system-control.md](protocol-system-control.md) describe the same object from two sides.
- The firmware's LUCI helper library exports `WriteTunnelData`, `readTunnelData`, `WriteTunnelDataToUart` and `WriteTunnelDataToBle`. The tunnel is a generic opaque-payload channel with TCP, UART (to the audio microcontroller) and Bluetooth LE backends. System Control frames are what travels through it on this device: a 50006 frame reaches the microcontroller as the payload of a LUCI packet whose command is 112.

Why a 7777 session sees only the request direction of box 112 is not settled. The likely explanation is the local/remote copy split described above: the two directions of box 112 use different copies, and port 7777 is alerted about one of them. That is a hypothesis; the observation itself is `tested`.

## LUCI message boxes

State on the device lives in numbered **message boxes**. MIDs are 16-bit, and a component subscribes by handing the bus a list of the MIDs it cares about. The firmware's helper library exposes this shape directly (`binary`):

```
LUCIHelper::LUCI_TransportListener::onMessageBoxChanged(unsigned short mid, unsigned short len, unsigned char* data, unsigned short)
LUCIHelper::registerListener(sp<ILuciAlerts>, Vector<int>& mids)
LUCIHelper::readRemoteMessage(int mid, char*&)
LUCIHelper::writeLocalMessage(int mid, char const*)
LUCIHelper::writeRemoteMessage(int mid, char const*, int)
LUCIHelper::SendMBResponse(int, int, int, char const*)
```

What arrives on either TCP port is a projection of that bus, chosen by the MID list the daemon in front of it registered for. Port 50006 can only ever reach box 112. Port 7777 lets the client name any box.

### Boxes MessageBoxHandler subscribes to

`MessageBoxHandler` registers for 49 MIDs, in this order (`binary`):

```
0x8e 0x1f2 0x1f0 0x90 0x7c 0x65 0x66 0x40 0xdb 0xdc 0x64 [0x6f 0x70]
0x6b 0x8d 0x5b 0x7d 0x06 0x41 0x14 0x15 0x16 0x17 0x5f 0x60 0x61 0x96
0x0e 0x0f 0x50 0x48 0x71 0x25 0x73 0xd7 0xd6 0x51 0xdd 0xe8 0x1f6 0x1f7
0xee 0x1ee 0xfd 0x10 0xe5 0xfe 0xe6 0x7f
```

Two independent dispatch tables in the same daemon cover MIDs 6..254 and agree with this list; the five MIDs above 254 are handled on a separate branch. The list includes `0x1ee`, the track-change push.

`0x6f` and `0x70` (tunnel init and tunnel data) are added only when the customer-identification environment item is not the string `Lyric`. On a Platin unit the tunnel exists; on a Lyric-branded unit of the same platform, port 50006 would have nothing behind it.

### Named boxes

| MID | Meaning | Evidence |
| --- | --- | --- |
| `0x03` (3) | session registration | tested |
| `0x05` (5) | firmware version, from `ro.build.version.incremental` | binary |
| `0x0a` (10) | internal source id | capture |
| `0x28` (40) | transport control | tested |
| `0x2a` (42) | PlayView JSON | capture |
| `0x2d` (45) | PlayView JSON, duplicate of `0x2a` | capture |
| `0x31` (49) | playback position, ms, signed | tested |
| `0x32` (50) | internal source id | capture |
| `0x33` (51) | play state | tested |
| `0x3f` (63) | mute state (read-only in effect) | tested |
| `0x40` (64) | volume, backed by environment item `0x26`, `current_volume` | binary + tested |
| `0x46` (70) | `SPEAKER_ACTIVE,<n>` / `SPEAKER_INACTIVE,<n>` | tested |
| `0x5a` (90) | model/brand string, `LibreX` on this build | binary |
| `0x6f` (111) | tunnel init; carries the port-50006 TCP port as ASCII | binary |
| `0x70` (112) | tunnel data: **this is port 50006** | binary + tested |
| `0xd0` (208) | model read (`READ_Model` → `Model:Stereo Hub`) | capture |
| `0xe6` (230) | sample rate | capture |
| `0xe7` (231) | factory serial, written at boot from the factory partition with a validity flag, `<serial>,VALID` | binary + tested |
| `0x1ee` (494) | emitted on track change, empty payload | capture |

Because box 111 carries it, the port 50006 listens on is configuration rather than a compile-time constant.

`luci_service` log strings name further boxes without giving their numbers: `current_volume`, `PlayerState`, `FriendlyName`, `SceneName`, `StereoPairMode`, `zoneid`, `ddms_sp_type`, `hostpresent` (`binary`).

### Observed but unidentified

These boxes appear on a registered session and change no known state (`capture`):

| MID | Payload | Note |
| --- | --- | --- |
| `0xdf` (223) | `NO_UPDATE` | reads like the result of a firmware-update check |
| `0xe0` (224) | short numbers such as `13` and `99` | `99` was seen on a `STOP`, as the table in [Stop](#stop) shows. `MessageBoxHandler` does not subscribe to this box |

A client should accept and ignore them.

## PlayView JSON

The reply to query `0x2a`, and the `0x2a`/`0x2d` push on every track change, is the richest single source of media state:

```json
{"CMD ID": 3,
 "Title": "PlayView",
 "Window CONTENTS": {
   "Album": "Album Name",
   "Artist": "Artist Name",
   "TrackName": "Track Name",
   "CoverArtUrl": "https://example.com/cover/640x640.jpg",
   "CastContentApp": "TIDAL",
   "PlayState": 2,
   "Current_time": -1,
   "TotalTime": 277597,
   "Current Source": 24,
   "BitDepth": 24, "BitRate": "0", "SampleRate": "0",
   "Repeat": 0, "Shuffle": 0, "SinglePlay": false, "Fav": false,
   "DSDType": "", "FileSize": 0, "Genre": "", "Index": 0,
   "Mime": "", "PlayUrl": ""
 }}
```

- `TotalTime` is milliseconds.
- `Current_time` is `-1` in every frame observed, including during playback. Treat it as unimplemented; the live position arrives on the `0x31` push.
- `PlayState` tracks the `0x33` push on every transition observed and uses the same values: `0` playing, `1` stopped/idle, `2` paused, `5` loading.
- `Current Source` does **not** use the port-50006 source indices (0–9). `24` and `25` are both attested, and the same number appears as the payload of the `0x0a` and `0x32` pushes and inside `SPEAKER_ACTIVE,<n>`: one internal id, quoted in four places. Its meaning is unknown.
- Together these fields are a complete now-playing payload: title, artist, album, artwork URL, duration and the name of the casting app.

## Multiroom and stereo pairing vocabulary

The firmware contains a housekeeping handler in `luci_service` (`LucicontrolServer::IncomingHouseKeeping`) with this command vocabulary, read from its strings (`binary`):

| Command | Reading |
| --- | --- |
| `SETMASTER` | make this unit the group master |
| `SETSLAVE` | make it a follower |
| `SETFREE` | leave the group and stand alone |
| `JOINALL` | join every unit |
| `JOINTO` | join a named unit |
| `JOINNEXT` | join the next unit |
| `JOINNEXTLEFT` / `JOINNEXTRIGHT` | stereo pairing, with channel assignment |
| `DROPME` | remove this unit from its group |
| `DROPALL` | dissolve the group |

Related strings: `invalid stereo pair command`, `device is in stereo pair mode. type change not allowed`, `zone volume control`, `client zone volume`, `Zone Info`, and the boxes `StereoPairMode` and `zoneid`. `MessageBoxHandler` refuses AUX input while the unit is stereo-paired (`AuxAudioControl device is stereo pair mode. aux input will not be allowed`).

**These are names only.** None of these commands has been sent to a device, no wire format is known, and it is not established which transport carries them; "housekeeping" suggests the link between units rather than either client port. They indicate that a Stereo Hub can be grouped and stereo-paired with other LibreSync units.

## How the library applies this

[media.py](../src/aiolibresync/media.py) builds and decodes port-7777 frames:

- Every outbound frame goes through one builder that refuses any MID outside a closed allow-list (register, transport, PlayView, play state, mute, volume, model) with `UnsafeCommandError`. There is no raw-send entry point, and mute is in the list for the query only.
- After connecting it registers with the local address, waits about 100 ms, then queries PlayView, volume, play state and mute and reads the model.
- It dispatches on the message box, never on `status`; treats refused and not-registered replies as carrying no value; decodes `0x31` as signed with `-1000` as "no position"; and accepts unknown boxes without failing, recording them for diagnostics.

Frame splitting, the CRC and the byte-order rule live in [frames.py](../src/aiolibresync/frames.py). Library usage is described in [../README.md](../README.md).
