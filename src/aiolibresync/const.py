"""Wire constants for both LibreSync ports.

Every value here is `tested` or `capture` in docs/commands.md. The
inferred-but-never-sent opcodes — `02 20`, `02 22`, `02 4c`, `02 4f` — are
deliberately absent and must stay absent: they would write properties nobody has
identified, on a port whose command language lives in a microcontroller whose
firmware is not on the device.
"""

from typing import Final

CONTROL_PORT: Final = 50006
MEDIA_PORT: Final = 7777

# --- port 50006: System Control ---------------------------------------------

CLASS_SYSTEM: Final = 0x02
CLASS_EQ: Final = 0x03
CLASS_ROOM: Final = 0x07
CLASS_PRESET: Final = 0x08

OP_SOURCE_GET: Final = 0x01
OP_SOURCE_REPORT: Final = 0x02
OP_SOURCE_SET: Final = 0x03
OP_POWER_GET: Final = 0x0D
OP_POWER_REPORT: Final = 0x0E
OP_POWER_TOGGLE: Final = 0x0F
OP_AV_GET: Final = 0x20  # the getter for the report below — sent 2026-08-16, answered
OP_AV_REPORT: Final = 0x21  # source, volume, mute and audio activity in one frame

OP_EQ_SET: Final = 0x04
OP_EQ_GET_REPORT: Final = 0x05  # one opcode both ways: the data length tells them apart

OP_ROOM_SET: Final = 0x01
OP_ROOM_GET: Final = 0x02
OP_ROOM_REPORT: Final = 0x03
#: On the LS10 hubs room correction is addressed. The vendor app sets it with
#: `07 01 01 <state>` and reads it with `07 02 01` (Surround Hub, 2026-10-24), and
#: the report puts the address before the state: `07 03 01 <state>` pushed on the
#: Stereo Hub HT, `07 03 <address> <state> 00` in reply on the Surround Hub, where
#: the address echoes the query's. `01` is every address the app was seen to use.
#: That it is the `groupId` of the `EQ1` group in the `08 08` reply is an
#: inference, not a measurement.
ROOM_GROUP: Final = 0x01
#: The modules, as LSSDP names them, whose room correction takes that address.
GROUP_MODULES: Final = frozenset({"LS10"})

OP_PRESET_SET: Final = 0x02
OP_PRESET_GET_REPORT: Final = 0x05

PRESET_SET_PREFIX: Final = b"\x01"  # data is 01 <n> 01 02
PRESET_SET_SUFFIX: Final = b"\x01\x02"
PRESET_MIN: Final = 1
PRESET_MAX: Final = 3

VOLUME_MIN: Final = 0
VOLUME_MAX: Final = 100

# Frames we recognise but decode to no state: our own and other clients'
# requests, plus the two blocks deliberately left undecoded. Anything
# outside both this set and the decode table is recorded for diagnostics.
KNOWN_UNDECODED_CONTROL: Final = frozenset(
    {
        (CLASS_SYSTEM, OP_SOURCE_GET),
        (CLASS_SYSTEM, OP_SOURCE_SET),
        (CLASS_SYSTEM, OP_POWER_GET),
        (CLASS_SYSTEM, OP_POWER_TOGGLE),
        (CLASS_SYSTEM, 0x4A),  # connect-time configuration queries and their
        (CLASS_SYSTEM, 0x4B),  # replies: read once at most, never polled
        (CLASS_SYSTEM, 0x4D),
        (CLASS_SYSTEM, 0x4E),
        (CLASS_SYSTEM, 0x8B),
        (CLASS_EQ, OP_EQ_SET),
        (CLASS_EQ, OP_EQ_GET_REPORT),
        (CLASS_ROOM, OP_ROOM_SET),
        (CLASS_ROOM, OP_ROOM_GET),
        (CLASS_ROOM, 0x09),  # calibration curve query
        (CLASS_ROOM, 0x0A),  # calibration curve, 143 bytes, not decoded
        (CLASS_PRESET, OP_PRESET_SET),
        (CLASS_PRESET, 0x04),  # per-preset filter parameters, 17 bytes, not decoded
        (CLASS_PRESET, OP_PRESET_GET_REPORT),
    }
)

# --- port 7777: LUCI media session ------------------------------------------

MSG_QUERY: Final = 0x01
MSG_COMMAND: Final = 0x02

STATUS_UNSOLICITED: Final = 0x00
STATUS_REPLY: Final = 0x01
STATUS_REFUSED: Final = 0x02
STATUS_NOT_REGISTERED: Final = 0x03

MID_REGISTER: Final = 0x0003
MID_SOURCE_ID_A: Final = 0x000A
MID_TRANSPORT: Final = 0x0028
MID_PLAYVIEW: Final = 0x002A
MID_PLAYVIEW_DUPLICATE: Final = 0x002D
MID_POSITION: Final = 0x0031
MID_SOURCE_ID_B: Final = 0x0032
MID_PLAY_STATE: Final = 0x0033
MID_MUTE: Final = 0x003F
MID_VOLUME: Final = 0x0040
MID_SPEAKER: Final = 0x0046
MID_TUNNEL: Final = 0x0070
MID_MODEL: Final = 0x00D0
MID_SAMPLE_RATE: Final = 0x00E6
#: The factory serial, `<serial>,VALID`. luci_service writes it here at boot
#: from the factory partition, so it does not depend on the UPnP daemon.
MID_SERIAL: Final = 0x00E7
MID_TRACK_CHANGE: Final = 0x01EE

#: The closed set of message boxes this library may address. `command` on port
#: 7777 IS a message-box id, so an arbitrary value reaches an arbitrary box —
#: including factory reset and firmware upgrade. Never widen this from caller
#: input. See docs/protocol-media.md.
#:
#: MID_MUTE is here for the *query* only. Writing it is accepted by the device
#: and does nothing — tested 2026-08-15 — so media.py exposes no setter for it.
#: MID_SERIAL is query-only too: it was read on 2026-09-26 and never written.
ALLOWED_MIDS: Final = frozenset(
    {
        MID_REGISTER,
        MID_TRANSPORT,
        MID_PLAYVIEW,
        MID_PLAY_STATE,
        MID_MUTE,
        MID_VOLUME,
        MID_MODEL,
        MID_SERIAL,
    }
)

#: Boxes we receive and understand but that carry no state we model.
KNOWN_UNDECODED_MIDS: Final = frozenset(
    {
        MID_REGISTER,
        MID_SOURCE_ID_A,
        MID_SOURCE_ID_B,
        MID_TRANSPORT,
        MID_TUNNEL,
        MID_SAMPLE_RATE,
        MID_TRACK_CHANGE,
    }
)

TRANSPORT_PAYLOADS: Final = frozenset({b"PLAY", b"PAUSE", b"STOP", b"NEXT", b"PREV"})
READ_MODEL_PAYLOAD: Final = b"READ_Model"

MAX_MID: Final = 0x01F7  # luci_service's dispatch tops out here
MAX_PAYLOAD: Final = 0x1400  # MID_LENGTH_MAX
MIN_CONTROL_BODY: Final = 3  # checksum + class + opcode
MAX_CONTROL_BODY: Final = 0x1400

# --- timings ----------------------------------------------------------------

POLL_INTERVAL: Final = 30.0  # bounds how stale power and room correction get
RECONNECT_BASE: Final = 1.0
RECONNECT_MAX: Final = 60.0
REGISTER_SETTLE: Final = 0.1  # the app waits ~100 ms after registering
#: Floor between two forced re-registrations of the 7777 session. A socket that
#: received frames counts as proved, so Connection retries it at RECONNECT_BASE
#: and its own backoff cannot throttle this; the client's cooldown is what does.
REREGISTER_COOLDOWN: Final = 30.0
CONFIRM_TIMEOUT: Final = 5.0
#: Room correction pushes nothing, so a set is confirmed by re-reading `07 02`.
#: This is when to ask the first time, and it is **shorter than the device**:
#: measured on hardware on 2026-08-15, the hub took 0.21 s, 0.29 s, 0.57 s and
#: 0.76 s over four writes to apply one. The value is left where it is because
#: the retry below is what makes the confirmation correct — a first read that
#: lands early costs one extra query, and raising the delay would slow down the
#: writes that do land at 0.21 s.
ROOM_REREAD_DELAY: Final = 0.3
#: How often to ask again when that first re-read comes back stale. Against the
#: measurements above a single re-read would have read a stale value in two of
#: the four trials, and then failed the 5 s confirmation on a command that had
#: worked — so this retry is load-bearing, not a safety net.
ROOM_REREAD_INTERVAL: Final = 0.15

# --- discovery ---------------------------------------------------------------

UPNP_PORT: Final = 38400  # LibreDmr, bound to the device's own address only
DESCRIPTION_PATH: Final = "/description.xml"
SSDP_ADDRESS: Final = "239.255.255.250"
SSDP_PORT: Final = 1900
SSDP_ST: Final = "urn:schemas-upnp-org:device:MediaRenderer:1"
SSDP_MX: Final = 2  # seconds a device may wait before answering; must be < DISCOVERY_TIMEOUT
SSDP_TTL: Final = 2  # one router hop, for the site that routes multicast
#: M-SEARCH is a UDP datagram and one of them is allowed to vanish. Three sends
#: a second apart is what a UPnP control point does, and it costs nothing.
SSDP_ATTEMPTS: Final = 3
SSDP_RETRY_INTERVAL: Final = 1.0
DISCOVERY_TIMEOUT: Final = 5.0
#: A search sent to one host is sent again every `SSDP_RETRY_INTERVAL` until it
#: answers or this runs out. On the newer hubs it is the only way to the
#: description, and this hub's own responder dropped 1 search in 8 on 2026-10-23.
UNICAST_SEARCH_TIMEOUT: Final = 10.0
#: Hosts one search follows up with an HTTP fetch, all at once. Far more
#: renderers than a home has; the cap is for a sender that answers from many
#: addresses, which would otherwise open that many connections.
MAX_SSDP_CANDIDATES: Final = 64
HTTP_TIMEOUT: Final = 5.0
#: Libre's own discovery, on UDP 1800. Both hub generations answer this query
#: with a `200 OK` that names the module (`SOURCE_LIST:LS9::…` or `LS10::…`);
#: this hub answered 3 of 3 unicast queries in 10 ms on 2026-10-23.
LSSDP_PORT: Final = 1800
LSSDP_QUERY: Final = (
    b"M-SEARCH * HTTP/1.1\r\nHOST:239.255.255.250:1800\r\nPROTOCOL:Version 1.0\r\n\r\n"
)
LSSDP_TIMEOUT: Final = 3.0
PROBE_TIMEOUT: Final = 3.0
#: A device description is ~1.5 kB. This bound is what stops an unidentified
#: host from streaming until the machine runs out of memory.
MAX_DESCRIPTION_BYTES: Final = 65536
