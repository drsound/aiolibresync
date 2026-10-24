"""Port 50006: build only tested frames, decode only reports."""

import pytest
from conftest import tap_log_frames

from aiolibresync import systemcontrol as sc
from aiolibresync.const import CLASS_SYSTEM, OP_AV_REPORT, OP_SOURCE_REPORT
from aiolibresync.frames import build_control
from aiolibresync.state import PlayState, Source


def test_the_builders_match_the_command_reference_byte_for_byte():
    """Every frame here is `tested` in docs/commands.md."""
    assert sc.QUERY_SOURCES == bytes.fromhex("0003030201")
    assert sc.QUERY_POWER == bytes.fromhex("00030f020d")
    assert sc.QUERY_ROOM_CORRECTION == bytes.fromhex("0003090702")
    assert sc.QUERY_MANUAL_EQ == bytes.fromhex("0003080305")
    assert sc.QUERY_EQ_PRESET == bytes.fromhex("00040d080500")
    # Sent to the hub for the first time on 2026-08-16; it answered a full
    # `02 21` in 84 ms. Pinned rather than merely rebuilt, because this opcode
    # had never been sent to any device and the frame that was is the evidence.
    assert sc.QUERY_AV == bytes.fromhex("0003220220")
    assert sc.toggle_power() == bytes.fromhex("000411020f00")
    assert sc.select_source(6) == bytes.fromhex("00040b020306")
    assert sc.set_room_correction(True) == bytes.fromhex("000409070101")
    assert sc.set_room_correction(False) == bytes.fromhex("000408070100")
    # The LS10 form, as the vendor app sends it to a Surround Hub
    assert sc.set_room_correction(True, grouped=True) == bytes.fromhex("00050a07010101")
    assert sc.set_room_correction(False, grouped=True) == bytes.fromhex("00050907010100")
    assert sc.QUERY_ROOM_CORRECTION_GROUP == bytes.fromhex("00040a070201")
    assert sc.set_manual_eq(True) == bytes.fromhex("000408030401")
    assert sc.set_manual_eq(False) == bytes.fromhex("000407030400")
    assert sc.select_eq_preset(1) == bytes.fromhex("00070f080201010102")
    assert sc.select_eq_preset(3) == bytes.fromhex("000711080201030102")


def test_connect_queries_ask_for_the_av_report():
    """Without it, source byte, volume, mute and audio state are all unknown
    after a connect: `02 21` is emitted on change only, never spontaneously."""
    assert sc.QUERY_AV in sc.CONNECT_QUERIES


@pytest.mark.parametrize("preset", [0, 4, -1])
def test_select_eq_preset_refuses_a_preset_the_device_does_not_have(preset):
    with pytest.raises(ValueError):
        sc.select_eq_preset(preset)


def test_select_source_refuses_a_byte_that_is_not_a_byte():
    with pytest.raises(ValueError):
        sc.select_source(256)


def test_decode_power_report():
    assert sc.decode(bytes.fromhex("000400020e01")) == {"power": True}
    assert sc.decode(bytes.fromhex("000400020e00")) == {"power": False}


def test_decode_av_report_ignores_the_second_source_numbering():
    """Byte 5 disagrees with the JSON `ix` on the wireless inputs. Do not map it."""
    # 00 07 00 02 21 0a 1c 01 01 — source 10, volume 28, muted, audio idle
    patch = sc.decode(bytes.fromhex("00070002210a1c0101"))
    assert patch == {"volume": 28, "muted": True, "audio_state": PlayState.IDLE}
    assert "source" not in patch


@pytest.mark.parametrize(
    ("data_byte_3", "expected"),
    [
        (0x00, PlayState.PLAYING),
        (0x01, PlayState.IDLE),
        (0x02, PlayState.PAUSED),
        (0x05, PlayState.LOADING),
    ],
)
def test_decode_av_report_reads_the_audio_activity_byte(data_byte_3, expected):
    """Data byte 3 is audio activity on the selected input, in PlayState's values.

    It was read wrong twice before it was read right. As `signal: bool(data[3])`
    it was True in every state where nothing played, which the 2026-08-15
    session killed by seeing `05` and `02`. As the play state it held on
    Streaming and failed on every physical input, where it falls to `01` while
    7777 goes on answering PLAYING to an empty socket.

    What survives both, and eleven timed transitions on 2026-08-16, is whether
    sound is coming out. It gets a field of its own — `play_state` still comes
    from 7777, because a second source for one field is how two ports get to
    disagree.
    """
    frame = build_control(CLASS_SYSTEM, OP_AV_REPORT, bytes([10, 28, 1, data_byte_3]))
    assert sc.decode(frame) == {"volume": 28, "muted": True, "audio_state": expected}


def test_decode_av_report_omits_an_audio_value_it_does_not_know():
    """An unrecognised byte leaves the field alone rather than blanking it.

    Blanking would send `DeviceState.playback` back to the 7777 play state,
    which is the field that lies on eight of the ten inputs — so a firmware we
    do not understand would be served worse than one we cannot read at all.
    """
    frame = build_control(CLASS_SYSTEM, OP_AV_REPORT, bytes([10, 28, 1, 0x03]))
    assert sc.decode(frame) == {"volume": 28, "muted": True}


def test_decode_av_report_still_requires_all_four_data_bytes():
    """A three-byte `02 21` is a shape the hub never sends.

    Keeping the guard at four means such a frame is reported as unclassified —
    it reaches the client's diagnostics — instead of being half-decoded silently.
    """
    assert sc.decode(build_control(CLASS_SYSTEM, OP_AV_REPORT, bytes([10, 28, 1]))) is None


def test_decode_source_list_reads_names_and_the_current_source():
    frame = next(
        f
        for f in tap_log_frames("2026-08-15-remote-labelled-50006.log")
        if f[3:5] == bytes.fromhex("0202")
    )
    patch = sc.decode(frame)
    assert Source(index=0, name="Streaming") in patch["sources"]
    assert len(patch["sources"]) == 10
    assert patch["source"] == Source(index=0, name="Streaming")


def test_decode_source_list_does_not_raise_on_a_hostile_src_field():
    """The bus is shared and the frame is untrusted: `src` parsing fine as
    something other than a list must not raise out of decode()."""
    null_src = build_control(CLASS_SYSTEM, OP_SOURCE_REPORT, b'{"src": null}')
    not_json = build_control(CLASS_SYSTEM, OP_SOURCE_REPORT, b"not json")
    assert sc.parse_source_list(null_src[5:]) is None
    assert sc.parse_source_list(not_json[5:]) is None
    assert sc.decode(null_src) is None
    assert sc.decode(not_json) is None


def test_decode_source_list_omits_source_when_no_entry_claims_it():
    """An absent `sts` means the frame does not say, not that no source is
    selected — the client must keep whatever current source it already knows."""
    payload = b'{"src": [{"ix": 0, "name": "Streaming"}, {"ix": 1, "name": "Bluetooth"}]}'
    frame = build_control(CLASS_SYSTEM, OP_SOURCE_REPORT, payload)
    patch = sc.decode(frame)
    assert "sources" in patch
    assert "source" not in patch


def test_decode_dsp_reports():
    assert sc.decode(bytes.fromhex("000400070301")) == {"room_correction": True}
    assert sc.decode(bytes.fromhex("000400030501")) == {"manual_eq": True}
    # 00 09 00 08 05 02 04 03 01 01 01 — the preset id is at offset 7
    assert sc.decode(bytes.fromhex("0009000805020403010101")) == {"eq_preset": 3}


def test_decode_room_correction_on_the_ls10_hubs():
    """The state is the byte after the address, whatever the width."""
    # Stereo Hub HT, pushed on change: `07 03 01 <state>`
    assert sc.decode(bytes.fromhex("00050007030100")) == {"room_correction": False}
    assert sc.decode(bytes.fromhex("00050007030101")) == {"room_correction": True}
    # Surround Hub, in reply to `07 02 01`: `07 03 01 <state> 00`
    assert sc.decode(bytes.fromhex("0006000703010100")) == {"room_correction": True}
    assert sc.decode(bytes.fromhex("0006000703010000")) == {"room_correction": False}
    # Surround Hub, in reply to the bare `07 02`: address 00, meaning unknown
    assert sc.decode(bytes.fromhex("0006000703000000")) == {}
    # A width nobody has seen stays unclassified, for the diagnostics
    assert sc.decode(bytes.fromhex("000700070301010000")) is None


def test_a_query_is_recognised_but_carries_no_state():
    """The bus is shared: another client's query arrives here too.

    03 05 with no data is the *get*; with one data byte it is the report. The
    same pair of readings applies to 08 05.
    """
    assert sc.decode(sc.QUERY_MANUAL_EQ) == {}
    assert sc.decode(sc.QUERY_EQ_PRESET) == {}
    assert sc.decode(sc.QUERY_POWER) == {}
    assert sc.decode(sc.toggle_power()) == {}


def test_an_unknown_frame_decodes_to_none_rather_than_a_guess():
    assert sc.decode(bytes.fromhex("000400029900")) is None


def test_every_control_frame_in_every_capture_is_understood():
    """A failure here names a frame nobody has classified — a finding.

    Decode it, or document it in docs/commands.md. Do not widen the assertion
    to make the test pass.
    """
    unknown = set()
    for log in (
        "2026-08-15-remote-labelled-50006.log",
        "2026-08-15-app-dsp-50006.log",
        "2026-08-15-app-dsp2-50006.log",
    ):
        for frame in tap_log_frames(log):
            if sc.decode(frame) is None:
                unknown.add((frame[3], frame[4]))
    assert unknown == set()
