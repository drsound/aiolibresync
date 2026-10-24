"""Port 50006 semantics: build the frames a client may send, decode what arrives.

Two properties of this port shape everything here.

It is a **shared broadcast bus**: every device->client frame reaches every
connected client, and other clients' commands are visible too. So `decode`
matches only the *report* forms and returns an empty patch for the request
forms — decoding `02 0f` as a power state would let another client's toggle
rewrite ours.

And byte 2 is **not** a direction marker. The firmware accepts a client frame
carrying `0x00`, so the checksum byte cannot be used to tell a command from an
event. Match on (class, opcode, data length) and nothing else.
"""

import json
from typing import Any

from .const import (
    CLASS_EQ,
    CLASS_PRESET,
    CLASS_ROOM,
    CLASS_SYSTEM,
    KNOWN_UNDECODED_CONTROL,
    OP_AV_GET,
    OP_AV_REPORT,
    OP_EQ_GET_REPORT,
    OP_EQ_SET,
    OP_POWER_GET,
    OP_POWER_REPORT,
    OP_POWER_TOGGLE,
    OP_PRESET_GET_REPORT,
    OP_PRESET_SET,
    OP_ROOM_GET,
    OP_ROOM_REPORT,
    OP_ROOM_SET,
    OP_SOURCE_GET,
    OP_SOURCE_REPORT,
    OP_SOURCE_SET,
    PRESET_MAX,
    PRESET_MIN,
    PRESET_SET_PREFIX,
    PRESET_SET_SUFFIX,
    ROOM_GROUP,
)
from .frames import build_control
from .state import PlayState, Source

QUERY_SOURCES = build_control(CLASS_SYSTEM, OP_SOURCE_GET)
QUERY_POWER = build_control(CLASS_SYSTEM, OP_POWER_GET)
QUERY_AV = build_control(CLASS_SYSTEM, OP_AV_GET)
QUERY_MANUAL_EQ = build_control(CLASS_EQ, OP_EQ_GET_REPORT)
QUERY_EQ_PRESET = build_control(CLASS_PRESET, OP_PRESET_GET_REPORT, b"\x00")
QUERY_ROOM_CORRECTION = build_control(CLASS_ROOM, OP_ROOM_GET)
#: The LS10 form, the one the vendor app sends: see `ROOM_GROUP`.
QUERY_ROOM_CORRECTION_GROUP = build_control(CLASS_ROOM, OP_ROOM_GET, bytes([ROOM_GROUP]))

#: What to ask on connect. The device answers queries — the earlier spec was
#: wrong about that — so state is read, never inferred by firing commands.
#: `02 4a`, `02 4d` and `02 8b` are deliberately not here: they are
#: configuration, invariant across power and source, and nothing reads them.
#:
#: `QUERY_AV` is here and **not** in the poll loop. `02 21` is emitted on change
#: only, so without it a client that had just attached knew neither volume nor
#: mute nor audio state until the user touched something. Polling it, though,
#: buys nothing: a 1 Hz probe on 2026-08-16 found the polled value and the
#: pushed one flipping 105 ms apart, so the device's own state is what lags, not
#: its notifications. A connect-time read, once.
CONNECT_QUERIES = (
    QUERY_SOURCES,
    QUERY_POWER,
    QUERY_AV,
    QUERY_MANUAL_EQ,
    QUERY_EQ_PRESET,
    QUERY_ROOM_CORRECTION,
)


def toggle_power() -> bytes:
    """The power command. It is a toggle: the firmware ignores the data byte.

    The vendor's app sends `0f 01` for "on" and turns a powered-on hub off with
    it. Callers must read the current state first — see
    LibreSyncClient.async_set_power.
    """
    return build_control(CLASS_SYSTEM, OP_POWER_TOGGLE, b"\x00")


def select_source(index: int) -> bytes:
    """Select the source with the given JSON `ix`."""
    if not 0 <= index <= 0xFF:
        raise ValueError(f"source index out of range: {index}")
    return build_control(CLASS_SYSTEM, OP_SOURCE_SET, bytes([index]))


def set_room_correction(enabled: bool, *, grouped: bool = False) -> bytes:
    """`07 01 <state>`, or with `grouped`, the LS10 form `07 01 01 <state>`.

    The grouped form is the vendor app's on a Surround Hub. Nobody has seen the
    app set it on a Stereo Hub HT, whose report has the same address byte.
    """
    data = bytes([ROOM_GROUP, int(enabled)]) if grouped else bytes([int(enabled)])
    return build_control(CLASS_ROOM, OP_ROOM_SET, data)


def set_manual_eq(enabled: bool) -> bytes:
    return build_control(CLASS_EQ, OP_EQ_SET, bytes([int(enabled)]))


def select_eq_preset(preset: int) -> bytes:
    """Select one of the presets the user built in the vendor's app."""
    if not PRESET_MIN <= preset <= PRESET_MAX:
        raise ValueError(f"preset must be {PRESET_MIN}..{PRESET_MAX}, got {preset}")
    data = PRESET_SET_PREFIX + bytes([preset]) + PRESET_SET_SUFFIX
    return build_control(CLASS_PRESET, OP_PRESET_SET, data)


def parse_source_list(payload: bytes) -> tuple[tuple[Source, ...], Source | None] | None:
    """Decode the `02 02` JSON body into the device's own source list.

    The list is authoritative and self-describing, which is what makes the
    library work on the rebranded devices that expose a different input set.
    Order is the device's, not sorted.
    """
    try:
        entries = json.loads(payload)["src"]
    except (ValueError, KeyError, TypeError):
        return None
    # The bus is shared and the frame is untrusted: `src` parsing fine as
    # something other than a list (e.g. `null`) must not raise out of decode().
    if not isinstance(entries, list):
        return None
    sources: list[Source] = []
    current: Source | None = None
    for entry in entries:
        try:
            source = Source(index=int(entry["ix"]), name=str(entry["name"]))
        except (KeyError, TypeError, ValueError):
            return None
        sources.append(source)
        if entry.get("sts"):
            current = source
    return tuple(sources), current


def decode(frame: bytes) -> dict[str, Any] | None:
    """Decode one port-50006 frame into a partial DeviceState update.

    Returns the fields it learned, an empty dict for a frame that is understood
    but carries no state (a request, or one of the two blocks deliberately left
    undecoded), or None for a frame nobody has classified — which the client
    records for diagnostics.
    """
    if len(frame) < 5:
        return None
    cls, opcode, data = frame[3], frame[4], frame[5:]

    if cls == CLASS_SYSTEM:
        if opcode == OP_POWER_REPORT and len(data) == 1:
            return {"power": bool(data[0])}
        if opcode == OP_SOURCE_REPORT and data:
            parsed = parse_source_list(data)
            if parsed is None:
                return None
            sources, current = parsed
            # An absent `sts` on every entry means "this frame does not say",
            # not "no source is selected" — omit the key so the client keeps
            # whatever current source it already knows.
            patch: dict[str, Any] = {"sources": sources}
            if current is not None:
                patch["source"] = current
            return patch
        if opcode == OP_AV_REPORT and len(data) >= 4:
            # data[0] is a source index in a second numbering that disagrees
            # with the JSON `ix` on the wireless inputs. Informational only:
            # the current source comes from the `02 02` list, which is pushed
            # on every change anyway.
            #
            # data[3] is **audio activity on the selected input**, in the same
            # values PlayState uses: `00`, `01`, `02`, `05`. It was read wrong
            # twice — as `signal: bool(data[3])`, True wherever nothing played,
            # and then as the play state, which holds on Streaming alone. What
            # it is, is whether sound is coming out, and on a physical input it
            # is the only field on either port that knows: 7777 answers PLAYING
            # to an empty USB socket for as long as anyone asks.
            #
            # It gets a field of its own rather than feeding `play_state`, which
            # stays the 7777 transport. Read `DeviceState.playback` to be told
            # which of the two to believe.
            #
            # The length guard requires the byte. Four is the width the hub
            # sends; a shorter `02 21` is a shape change, and leaving it to fall
            # through to `None` puts it in the client's diagnostics instead of
            # decoding half of an unknown frame.
            av: dict[str, Any] = {"volume": data[1], "muted": bool(data[2])}
            try:
                av["audio_state"] = PlayState(data[3])
            except ValueError:
                # A value no firmware here has produced. Leave the field at
                # whatever it held: blanking it would send `playback` back to
                # the 7777 play state, which is the one that lies.
                pass
            return av
    elif cls == CLASS_EQ:
        if opcode == OP_EQ_GET_REPORT and len(data) == 1:
            return {"manual_eq": bool(data[0])}
    elif cls == CLASS_ROOM:
        if opcode == OP_ROOM_REPORT and len(data) == 1:
            return {"room_correction": bool(data[0])}
        if opcode == OP_ROOM_REPORT and len(data) in (2, 3):
            # The LS10 form, `<address> <state>` with a trailing `00` on the
            # Surround Hub: see ROOM_GROUP. A report for any other address is
            # the Surround Hub answering the bare `07 02`, with `00`, and what
            # its state byte means there nobody knows, so it sets nothing.
            if data[0] == ROOM_GROUP:
                return {"room_correction": bool(data[1])}
            return {}
    elif cls == CLASS_PRESET:
        if opcode == OP_PRESET_GET_REPORT and len(data) == 6:
            return {"eq_preset": data[2]}

    # This check is by (class, opcode) only, with no data-length test. A
    # report-form opcode arriving at a length none of the branches above
    # matches is therefore reported as understood-but-stateless rather than
    # unclassified, which can mask a shape change from the client's
    # diagnostics for unclassified frames.
    if (cls, opcode) in KNOWN_UNDECODED_CONTROL:
        return {}
    return None
