"""The client against a fake hub. No device, no network beyond localhost."""

import asyncio
import json
import logging
import socket

import pytest
from conftest import tap_log_frames, until
from fakes import FakeHub, FakeTLS, device_frame

from aiolibresync import media
from aiolibresync.client import LibreSyncClient
from aiolibresync.const import (
    CLASS_SYSTEM,
    MID_PLAY_STATE,
    MID_REGISTER,
    MID_TRANSPORT,
    MID_VOLUME,
    STATUS_NOT_REGISTERED,
)
from aiolibresync.exceptions import ConfirmationTimeout, NotConnectedError
from aiolibresync.frames import MEDIA_HEADER, media_command, media_crc
from aiolibresync.state import DeviceState, PlayState, Source


@pytest.fixture
async def hub():
    hub = FakeHub()
    await hub.start()
    yield hub
    await hub.stop()


def open_sockets(hub: FakeHub) -> tuple[int, int]:
    """How many live client sockets the hub holds, as (control, media).

    Reaching into the fake's private lists: it exposes no count, and this is
    the only direct evidence that a socket was actually closed rather than
    merely superseded by a newer one.
    """
    return len(hub._control_writers), len(hub._media_writers)


def registrations(hub: FakeHub) -> int:
    """How many times a session registered. One per 7777 connection."""
    return sum(
        1 for f in hub.received["media"] if media_command(f, from_device=False) == MID_REGISTER
    )


def test_the_fake_builds_frames_the_codec_accepts():
    """If the fake's frames did not pass the real CRC check, nothing below means anything."""
    frame = device_frame(0x0040, b"30")
    assert int.from_bytes(frame[6:8], "big") == media_crc(frame)
    assert media.decode(frame) == {"volume": 30}


def make_client(hub: FakeHub, **kwargs) -> LibreSyncClient:
    return LibreSyncClient(
        "127.0.0.1",
        control_port=hub.control_port,
        media_port=hub.media_port,
        base_delay=0.01,
        max_delay=0.02,
        **kwargs,
    )


async def test_connect_reads_the_whole_state_without_firing_a_command(hub):
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.model is not None)

    assert client.state.available is True
    assert client.state.power is True
    assert client.state.volume == 28
    assert client.state.muted is False  # from the 01 003f query, not from a push
    assert client.state.source == Source(index=0, name="Streaming")
    assert len(client.state.sources) == 10
    assert client.state.room_correction is True
    assert client.state.manual_eq is True
    assert client.state.eq_preset == 3
    assert client.state.play_state is PlayState.PAUSED
    assert client.state.now_playing.artist == "Melissa Aldana"
    assert client.state.model == "Stereo Hub"
    # Nothing that changes the device was sent.
    assert not any(f[3:5] == bytes.fromhex("020f") for f in hub.received["control"])
    await client.async_disconnect()


async def test_connect_learns_the_audio_state_without_waiting_for_a_change(hub):
    """`02 21` is pushed on change only, so before `02 20` this was unknowable.

    The fake starts on Streaming, where the audio activity and the transport
    agree, so both read PAUSED here.
    """
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.model is not None)

    assert client.state.audio_state is PlayState.PAUSED
    assert client.state.playback is PlayState.PAUSED
    assert any(f[3:5] == bytes.fromhex("0220") for f in hub.received["control"])
    await client.async_disconnect()


async def test_playback_believes_the_audio_state_over_the_transport(hub):
    """A physical input with nothing plugged into it.

    7777 answers PLAYING to an empty socket for as long as anyone asks, and byte
    8 settles at IDLE within a second — measured 2026-08-16 over five USB
    selections. The library believes byte 8.
    """
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.model is not None)

    hub.play_state = 0  # PLAYING, as the renderer claims on any physical input
    hub.audio_state = 1  # IDLE — nothing is actually making a sound
    hub.push_media(device_frame(MID_PLAY_STATE, str(hub.play_state).encode()))
    hub.broadcast_control(hub.av_report())
    await until(lambda: client.state.audio_state is PlayState.IDLE)

    assert client.state.play_state is PlayState.PLAYING
    assert client.state.playback is PlayState.IDLE
    await client.async_disconnect()


async def test_mute_survives_a_hub_that_refuses_to_be_asked(hub):
    """Box 0x3f answered `status 0x02` twice on 2026-08-16, hub off and playing.

    There is no other 7777 route to mute, so before `02 20` a hub behaving that
    way left it unknown until something changed it. It rides in `02 21` byte 7,
    which the connect burst now fetches, so the field fills in anyway.
    """
    hub.refuse_mute_query = True
    hub.muted = True
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.model is not None)

    assert client.state.muted is True
    await client.async_disconnect()


async def test_the_session_registers_before_it_commands_anything(hub):
    """An unregistered connection is answered `mid 0x0000 status 0x03` and dropped."""
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: hub.registered)

    first = hub.received["media"][0]
    assert media_command(first, from_device=False) == MID_REGISTER
    assert first[10:].decode() == "127.0.0.1"  # the real local address of this socket
    await client.async_disconnect()


async def test_duplicate_events_produce_one_callback(hub):
    """The hub sends 02 0e and 0x40 twice. Equality collapses the second."""
    client = make_client(hub)
    seen = []
    client.subscribe(seen.append)
    await client.async_connect()
    await until(lambda: client.state.model is not None)
    before = len(seen)

    frame = bytes.fromhex("000400020e01")
    hub.broadcast_control(frame)
    hub.broadcast_control(frame)
    await asyncio.sleep(0.1)

    assert len(seen) == before  # power was already True: no change, no callback
    await client.async_disconnect()


async def test_the_metadata_pair_produces_one_callback(hub):
    client = make_client(hub)
    await client.async_connect()
    # `model`, not `now_playing`: the model reply is the last of the connect
    # burst, and gating on the first one leaves the rest of the burst free to
    # land after the subscription and be counted as a callback of the pair.
    # That made this test fail about once in fifteen runs.
    await until(lambda: client.state.model is not None)
    seen = []
    client.subscribe(seen.append)

    payload = device_frame(0x002A, b'{"Window CONTENTS": {"TrackName": "X"}}')
    twin = device_frame(0x002D, b'{"Window CONTENTS": {"TrackName": "X"}}')
    hub.push_media(payload)
    hub.push_media(twin)
    await asyncio.sleep(0.1)

    assert len(seen) == 1
    assert seen[0].now_playing.title == "X"
    await client.async_disconnect()


async def test_a_failing_subscriber_does_not_stop_the_others(hub):
    client = make_client(hub)
    seen = []

    def boom(_state):
        raise RuntimeError("subscriber bug")

    client.subscribe(boom)
    client.subscribe(seen.append)
    await client.async_connect()
    await until(lambda: seen)
    await client.async_disconnect()


async def test_unsubscribe_stops_the_callbacks(hub):
    client = make_client(hub)
    seen = []
    unsubscribe = client.subscribe(seen.append)
    await client.async_connect()
    await until(lambda: seen)
    unsubscribe()
    count = len(seen)
    hub.broadcast_control(bytes.fromhex("000400020e00"))
    await asyncio.sleep(0.1)
    assert len(seen) == count
    await client.async_disconnect()


async def test_unknown_frames_are_recorded_rather_than_raised(hub):
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.model is not None)
    hub.broadcast_control(bytes.fromhex("000400029900"))
    await asyncio.sleep(0.1)
    assert client.diagnostics()["unknown_frames"]["control"] == ["00 04 00 02 99 00"]
    await client.async_disconnect()


async def test_connecting_twice_does_not_fork_a_second_session(hub):
    """A second connect must not orphan the first reader loop.

    Home Assistant retries `async_setup_entry` and `async_connect` never
    raises, so this is a caller move rather than a theoretical one. Two loops
    on one instance share `_writer`: the orphan closes the live socket and goes
    on driving callbacks into a client that has already been thrown away.
    """
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.model is not None)

    await client.async_connect()
    await asyncio.sleep(0.3)  # long enough for a forked session to register

    assert open_sockets(hub) == (1, 1)
    assert registrations(hub) == 1

    await client.async_disconnect()
    await until(lambda: open_sockets(hub) == (0, 0))


async def test_disconnect_is_safe_before_a_connect_and_twice_over(hub):
    client = make_client(hub)
    await client.async_disconnect()  # never connected

    await client.async_connect()
    await until(lambda: client.state.model is not None)
    await client.async_disconnect()
    await client.async_disconnect()  # and again

    assert client.state.available is False
    await until(lambda: open_sockets(hub) == (0, 0))


async def test_a_not_registered_reply_re_registers_the_session(hub):
    """status 0x03 on a socket that never drops would otherwise wedge for ever.

    Nothing else can recover it in time: a confirmation waiter only exists while a
    command is in flight, and the poll loop would not draw a fresh refusal out
    of 7777 until the next interval, up to POLL_INTERVAL away.
    """
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.model is not None)
    before = registrations(hub)

    hub.push_media(device_frame(0x0000, status=STATUS_NOT_REGISTERED))

    await until(lambda: registrations(hub) == before + 1)
    await until(lambda: client.state.available)
    await client.async_disconnect()


async def test_recovery_fires_once_per_cooldown_not_once_per_refusal(hub):
    """The connect burst is five queries, so a forgotten session refuses five
    times in a row. `Connection._run` counts a socket that received anything as
    proved and retries at the floor, so only the client's own cooldown keeps
    this from becoming a reconnect loop."""
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.model is not None)
    before = registrations(hub)

    refusal = device_frame(0x0000, status=STATUS_NOT_REGISTERED)
    for _ in range(5):
        hub.push_media(refusal)
    await until(lambda: registrations(hub) == before + 1)
    await until(lambda: client.state.available)

    # A sixth refusal, inside the cooldown, must not drop the socket again.
    hub.push_media(refusal)
    await asyncio.sleep(0.3)

    assert registrations(hub) == before + 1
    assert client.diagnostics()["counters"]["media_not_registered"] == 6
    await client.async_disconnect()


def _one_dead_port_client(hub: FakeHub) -> LibreSyncClient:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead = probe.getsockname()[1]
    probe.close()  # nothing listens here
    return LibreSyncClient(
        "127.0.0.1",
        control_port=hub.control_port,
        media_port=dead,
        base_delay=0.01,
        max_delay=0.02,
    )


async def test_available_stays_false_while_only_one_port_is_up(hub):
    """`available` is the AND of both sockets, and it is not `power`."""
    client = _one_dead_port_client(hub)
    await client.async_connect()
    await until(lambda: client.state.power is not None)  # 50006 is fully up

    assert client.state.available is False
    await client.async_disconnect()


async def test_connect_with_a_timeout_returns_once_both_ports_are_open(hub):
    client = make_client(hub)
    await client.async_connect(timeout=2.0)

    assert client.state.available is True
    await client.async_disconnect()


async def test_available_waits_for_the_7777_registration(hub):
    """A command issued as soon as `available` is true must find the session
    registered, or the hub refuses it with status 0x03."""
    client = make_client(hub)
    sent: list[bytes] = []
    real_send = client._media.send

    async def spy(*frames: bytes) -> None:
        sent.extend(frames)
        await real_send(*frames)

    client._media.send = spy  # type: ignore[method-assign]
    registered_when_available: list[bool] = []

    def on_state(state: DeviceState) -> None:
        if state.available and not registered_when_available:
            registered_when_available.append(
                any(media_command(f, from_device=False) == MID_REGISTER for f in sent)
            )

    client.subscribe(on_state)
    await client.async_connect(timeout=2.0)

    assert registered_when_available == [True]
    await client.async_disconnect()


async def test_connect_with_a_timeout_disconnects_before_raising(hub):
    """A setup that gives up must not leave two reconnect loops and a poll behind."""
    client = _one_dead_port_client(hub)

    with pytest.raises(NotConnectedError):
        await client.async_connect(timeout=0.2)

    assert client._poll_task is None
    await until(lambda: open_sockets(hub) == (0, 0))


async def test_a_cancelled_connect_leaves_nothing_running(hub):
    client = _one_dead_port_client(hub)
    task = asyncio.create_task(client.async_connect(timeout=5.0))
    await until(lambda: client.state.power is not None)  # 50006 is up, the wait goes on

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client._poll_task is None
    assert client._control._task is None
    assert client._media._task is None  # the port still reconnecting
    await until(lambda: open_sockets(hub) == (0, 0))


def test_a_patch_naming_something_that_is_not_a_field_raises():
    """Deliberately unwrapped: a decoder bug must look like a bug, not like a
    protocol error something upstream can swallow."""
    client = LibreSyncClient("127.0.0.1")
    with pytest.raises(TypeError):
        client._apply({"nonesuch": True})


async def test_diagnostics_describes_the_session_without_naming_the_device(hub):
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.model is not None)
    hub.broadcast_control(bytes.fromhex("000400029900"))
    await until(lambda: client.diagnostics()["unknown_frames"]["control"])

    diagnostics = client.diagnostics()
    assert set(diagnostics) == {
        "host",
        "module",
        "connected",
        "state",
        "counters",
        "unknown_frames",
        "unknown_signatures",
    }
    assert diagnostics["host"] == "<redacted>"
    assert diagnostics["connected"] == {"control": True, "media": True}
    assert set(diagnostics["counters"]) == {
        "control_frames",
        "media_frames",
        "control_unknown",
        "media_unknown",
        "media_refused",
        "media_not_registered",
        "control_resyncs",
        "media_resyncs",
    }
    assert diagnostics["counters"]["control_unknown"] == 1
    assert diagnostics["counters"]["media_not_registered"] == 0
    # Aggregated as well as sampled: twenty entries look the same whether they
    # stand for twenty frames or twenty thousand.
    assert diagnostics["unknown_signatures"] == {"control": {"0299": 1}, "media": {}}
    # The assertion that catches the next leak, wherever it appears.
    assert client.host not in json.dumps(diagnostics)
    await client.async_disconnect()


async def test_diagnostics_does_not_ship_what_the_user_is_listening_to(hub):
    """A half-redaction is worse than none: it invites the reader to trust it."""
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.now_playing is not None)

    dump = client.diagnostics()
    assert dump["state"]["now_playing"] == {
        "title": True,
        "artist": True,
        "album": True,
        "app": True,
        "artwork_url_scheme": "https",
        "duration_ms": 277597,
    }
    # Device facts stay: they are what a protocol bug report is about.
    assert dump["state"]["volume"] == 28
    assert "Melissa Aldana" not in json.dumps(dump)
    await client.async_disconnect()


async def test_power_on_when_already_on_sends_no_toggle(hub):
    """The bug not to reproduce: the vendor's app sends `0f 01` and turns it off."""
    hub.power = True
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.power is True)

    await client.async_set_power(True)

    assert not any(f[3:5] == bytes.fromhex("020f") for f in hub.received["control"])
    assert hub.power is True
    await client.async_disconnect()


async def test_power_off_toggles_once_and_waits_for_the_confirmation(hub):
    hub.power = True
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.power is True)

    await client.async_set_power(False)

    toggles = [f for f in hub.received["control"] if f[3:5] == bytes.fromhex("020f")]
    assert len(toggles) == 1
    assert hub.power is False
    assert client.state.power is False  # confirmed by the event, not assumed
    await client.async_disconnect()


async def test_power_is_read_from_the_device_not_from_the_cache(hub):
    """Something else changed the hub since we last heard: the read must win."""
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.power is True)
    # Changed behind our back, with no event reaching us. On firmware 1.52 the
    # RF remote does broadcast `02 0e`, so this stands in
    # for whatever else drifts the state — a missed frame, a firmware that does
    # not announce it. What is under test is that the read wins over the cache.
    hub.power = False

    await client.async_set_power(False)

    assert not any(f[3:5] == bytes.fromhex("020f") for f in hub.received["control"])
    await client.async_disconnect()


async def test_two_commands_in_flight_do_not_confirm_each_other(hub):
    """One bus, two waiters, different DeviceState keys — and one of the two is
    a power command, which reads before it acts while the other is in flight."""
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.volume == 28 and client.state.power is True)

    await asyncio.gather(client.async_set_volume(40), client.async_set_power(False))

    assert (client.state.volume, client.state.power) == (40, False)
    assert (hub.volume, hub.power) == (40, False)
    toggles = [f for f in hub.received["control"] if f[3:5] == bytes.fromhex("020f")]
    assert len(toggles) == 1
    assert client._waiters == []
    await client.async_disconnect()


async def test_selecting_a_source_waits_for_the_device_to_report_it(hub):
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.source is not None)

    await client.async_select_source(6)

    assert client.state.source == Source(index=6, name="HDMI")
    await client.async_disconnect()


async def test_setting_the_volume_goes_out_on_7777(hub):
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.volume == 28)

    await client.async_set_volume(40)

    assert client.state.volume == 40
    await client.async_disconnect()


def av_reports(hub: FakeHub) -> list[bytes]:
    """The `02 21` frames the hub put on port 50006."""
    return [f for f in hub.sent["control"] if (f[3], f[4]) == (CLASS_SYSTEM, 0x21)]


def volume_frames(hub: FakeHub) -> list[bytes]:
    """The `0x40` frames the hub put on port 7777."""
    return [f for f in hub.sent["media"] if media_command(f, from_device=True) == MID_VOLUME]


async def test_a_volume_change_is_reported_on_the_control_port_too(hub):
    """A set that moves the level has an effect on the *other* port.

    Measured on the hub on 2026-08-15 over seven sets, with the hub off, on an
    idle physical input and playing on Streaming: the `02 21` always followed,
    51 to 155 ms after the `0x40` and never more than 192 ms after the write.
    """
    client = make_client(hub)
    await client.async_connect()
    # The whole connect burst, so that its own volume replies are inside the
    # `before` counts rather than racing them.
    await until(lambda: client.state.model is not None)
    assert client.state.volume == 28
    before_av, before_volume = len(av_reports(hub)), len(volume_frames(hub))

    await client.async_set_volume(40)

    assert [f[6] for f in av_reports(hub)[before_av:]] == [40]
    assert len(volume_frames(hub)) - before_volume == 2  # the `0x40` arrives twice
    await client.async_disconnect()


async def test_a_redundant_volume_set_is_confirmed_by_the_echo_alone(hub):
    """And this is why `async_set_volume` still reads back the box it wrote.

    Setting the level the hub already holds draws **one** `0x40` and no `02 21`
    at all — measured twice on 2026-08-15. So the cross-port confirmation the
    project's rule would prefer is not available in this case, and demanding it
    would turn a legitimate no-op into a five-second `ConfirmationTimeout`.
    Home Assistant issues redundant sets: a slider that lands back where it
    was, a scene re-applied.
    """
    client = make_client(hub)
    await client.async_connect()
    # The whole connect burst, so that its own volume replies are inside the
    # `before` counts rather than racing them.
    await until(lambda: client.state.model is not None)
    assert client.state.volume == 28
    before_av, before_volume = len(av_reports(hub)), len(volume_frames(hub))

    await client.async_set_volume(28)

    assert av_reports(hub)[before_av:] == []
    assert len(volume_frames(hub)) - before_volume == 1
    assert client.state.volume == 28
    await client.async_disconnect()


async def test_transport_commands_move_the_play_state(hub):
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.play_state is not None)

    await client.async_media_play()
    await until(lambda: client.state.play_state is PlayState.PLAYING)
    await client.async_media_pause()
    await until(lambda: client.state.play_state is PlayState.PAUSED)
    await client.async_disconnect()


async def test_stop_moves_the_play_state_too(hub):
    """Pinned separately from play/pause: the poll loop puts traffic on the
    bus that could incidentally satisfy a waiter and hide a regression here."""
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.play_state is PlayState.PAUSED)

    await client.async_media_stop()

    await until(lambda: client.state.play_state is PlayState.IDLE)
    assert hub.play_state == 1
    await client.async_disconnect()


async def test_a_stop_that_changes_nothing_is_still_confirmed(hub):
    """A stopped hub answers a second stop with its acknowledgement and nothing
    else, so waiting for a play state would time out on a command that worked."""
    client = make_client(hub, confirm_timeout=0.5)
    await client.async_connect(timeout=2.0)
    await client.async_media_stop()
    await until(lambda: client.state.play_state is PlayState.IDLE)
    before = len(hub.received["media"])

    await client.async_media_stop()

    assert len(hub.received["media"]) == before + 1
    assert client.state.play_state is PlayState.IDLE
    await client.async_disconnect()


async def test_a_transport_command_the_hub_never_acknowledges_times_out(hub):
    hub.ack_transport = False
    client = make_client(hub, confirm_timeout=0.3)
    await client.async_connect(timeout=2.0)

    with pytest.raises(ConfirmationTimeout):
        await client.async_media_pause()
    await client.async_disconnect()


async def test_next_and_previous_send_their_payload_and_do_not_wait(hub):
    """NEXT and PREV are byte-identical on the wire and the device reports the
    resulting track rather than the button, so there is nothing to wait for."""
    client = make_client(hub, confirm_timeout=0.2)
    await client.async_connect()
    await until(lambda: client.state.now_playing is not None)
    hub._answer_media = lambda frame: None  # the hub answers nothing at all

    await client.async_media_next_track()
    await client.async_media_previous_track()  # neither raises ConfirmationTimeout

    def payloads() -> list[bytes]:
        return [
            f[10:]
            for f in hub.received["media"]
            if media_command(f, from_device=False) == MID_TRANSPORT
        ]

    # Both calls returned before the hub had even read them, which is the point:
    # a command that waited for a confirmation the hub never sends would have
    # raised above instead.
    await until(lambda: len(payloads()) == 2)
    assert payloads() == [b"NEXT", b"PREV"]
    assert client._waiters == []  # nothing was ever armed
    await client.async_disconnect()


async def test_setting_room_correction_re_reads_because_nothing_is_pushed(hub):
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.room_correction is True)

    await client.async_set_room_correction(False)

    assert hub.room_correction is False
    assert client.state.room_correction is False  # from the re-read, not from hope
    queries = [f for f in hub.received["control"] if f[3:5] == bytes.fromhex("0702")]
    assert len(queries) >= 2  # one on connect, one after the set
    await client.async_disconnect()


async def test_manual_eq_and_preset_are_driven_from_the_push(hub):
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.manual_eq is True)

    await client.async_set_manual_eq(False)
    assert client.state.manual_eq is False

    await client.async_select_eq_preset(1)
    assert client.state.eq_preset == 1
    await client.async_disconnect()


async def test_a_command_before_the_socket_is_up_raises(hub):
    client = make_client(hub)
    with pytest.raises(NotConnectedError):
        await client.async_set_volume(10)


async def test_power_refuses_to_toggle_blind(hub):
    """If the query goes unanswered and nothing is cached, do not guess."""
    client = make_client(hub, confirm_timeout=0.2)
    await client.async_connect()
    # Gated on power rather than on `available`: the socket is available before
    # the connect burst has even been sent, so blanking the cache there races a
    # connect-time `02 0e` still in flight. No other frame in the burst carries
    # a `power` key, so this gate is exact.
    await until(lambda: client.state.power is True)
    client._state = client._state.evolve(power=None)  # nothing known
    hub._answer_control = lambda frame: None  # the hub stops answering

    with pytest.raises(ConfirmationTimeout):
        await client.async_set_power(True)

    assert not any(f[3:5] == bytes.fromhex("020f") for f in hub.received["control"])
    await client.async_disconnect()


def poll_tasks() -> int:
    """How many poll loops are alive on this event loop.

    Counted by task name rather than through `client._poll_task`, because an
    unguarded `create_task` overwrites that attribute: the orphan it leaves
    behind is invisible from the client and visible only here.
    """
    return sum(1 for t in asyncio.all_tasks() if t.get_name() == "aiolibresync-poll")


async def test_the_poll_loop_asks_50006_for_power_and_room_correction_only(hub):
    """Power is invisible when changed from the RF remote; room correction always.

    Nothing else is asked of 50006: the config queries 02 4a, 02 4d and 02 8b
    are invariant across power and source, and everything else pushes. What the
    poll writes on 7777 is not read for its content — see the next test.
    """
    client = make_client(hub, poll_interval=0.05)
    await client.async_connect()
    await until(lambda: client.state.model is not None)
    hub.received["control"].clear()

    await asyncio.sleep(0.2)

    polled = {bytes(f[3:5]) for f in hub.received["control"]}
    assert polled == {bytes.fromhex("020d"), bytes.fromhex("0702")}
    await client.async_disconnect()


async def test_the_poll_loop_writes_on_7777_as_well(hub):
    """Every cycle must put a frame on the media socket, or it never notices.

    Writing is the only thing that tells a socket its peer has left without a
    FIN: `reader.read()` blocks on a dead connection for ever. 50006 found that
    out on hardware because the poll writes there; 7777 was written to by
    nobody, so it stayed nominally up — and `available`, the AND of the two,
    went back to True over a session on which every command was refused.

    Exactly one query, and specifically the play-state read: it is already in
    `media.CONNECT_QUERIES` and so in `ALLOWED_MIDS`, it is idempotent, and its
    reply folds into the state as a `play_state` the device is in anyway.
    """
    client = make_client(hub, poll_interval=0.05)
    await client.async_connect()
    await until(lambda: client.state.model is not None)
    hub.received["media"].clear()

    await asyncio.sleep(0.2)

    polled = {media_command(f, from_device=False) for f in hub.received["media"]}
    assert polled == {MID_PLAY_STATE}
    await client.async_disconnect()


async def test_a_remote_power_change_is_caught_by_the_poll(hub):
    client = make_client(hub, poll_interval=0.05)
    await client.async_connect()
    await until(lambda: client.state.power is True)

    hub.power = False  # the RF remote: no event on either port

    await until(lambda: client.state.power is False, timeout=3.0)
    await client.async_disconnect()


async def test_the_poll_loop_survives_the_socket_going_away_and_resumes(hub):
    """A poll loop that dies takes the staleness bound with it, silently."""
    client = make_client(hub, poll_interval=0.05)
    await client.async_connect()
    await until(lambda: client.state.model is not None)
    hub.received["control"].clear()

    client._control.drop()  # the socket goes, the loop must not

    # 02 01 comes only from the connect burst, so it is the evidence that the
    # socket really did go away and come back rather than never dropping.
    await until(lambda: any(f[3:5] == bytes.fromhex("0201") for f in hub.received["control"]))

    # Counted rather than gated on `connected` going False, which reconnects
    # too fast to observe. The reconnect burst contributes exactly one 02 0d,
    # so three of them mean the poll ran twice on the new socket.
    def power_queries() -> int:
        return sum(1 for f in hub.received["control"] if f[3:5] == bytes.fromhex("020d"))

    await until(lambda: power_queries() >= 3)

    assert poll_tasks() == 1
    await client.async_disconnect()


class VanishingMediaLink:
    """A relay to the hub's 7777 port that can stop dead without sending a FIN.

    `FakeHub` cannot model a hub whose mains was pulled and must not be taught
    to: such a hub stops *reading* as well as answering, so nothing the client
    sends is ever noticed and no FIN ever comes back. A hub that closes its
    socket is the other, easier failure — the client's read loop sees EOF and
    reconnects unaided — and that is exactly the case
    `test_a_connection_dropped_mid_frame_recovers` covers, and exactly why it
    did not catch this one. Relaying through a peer that can be told to stop
    reading leaves the client's socket open with nothing behind it, and leaves
    `FakeHub` byte-for-byte the hub every other test uses.
    """

    def __init__(self, media_port: int) -> None:
        self._media_port = media_port
        self._stopped = asyncio.Event()
        self._pumps: list[asyncio.Task[None]] = []
        self._client_writers: list[asyncio.StreamWriter] = []
        self._hub_writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    @property
    def port(self) -> int:
        return int(self._server.sockets[0].getsockname()[1])

    async def _handle(self, reader, writer) -> None:
        self._client_writers.append(writer)
        hub_reader, hub_writer = await asyncio.open_connection("127.0.0.1", self._media_port)
        self._hub_writers.append(hub_writer)
        self._pumps += [
            asyncio.create_task(self._pump(reader, hub_writer)),
            asyncio.create_task(self._pump(hub_reader, writer)),
        ]
        # This handler never returns of its own accord. asyncio closes a
        # connection whose handler raises or is cancelled, and a close is
        # precisely the event this relay exists to withhold.
        await self._stopped.wait()

    @staticmethod
    async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()

    def vanish(self) -> None:
        """Mains pulled: stop reading, stop answering, close nothing this side.

        The hub-side connection goes, so the hub sees the session end and a
        later reconnection is a genuinely new one. The client's socket is left
        exactly as a departed peer leaves it: open, with nobody behind it.
        """
        for pump in self._pumps:
            pump.cancel()
        for writer in self._hub_writers:
            writer.close()
        self._hub_writers.clear()

    async def stop(self) -> None:
        self.vanish()
        await asyncio.gather(*self._pumps, return_exceptions=True)
        self._stopped.set()
        for writer in self._client_writers:
            writer.close()
        self._client_writers.clear()
        self._server.close()
        await self._server.wait_closed()


@pytest.fixture
async def link(hub):
    link = VanishingMediaLink(hub.media_port)
    await link.start()
    yield link
    await link.stop()


def make_relayed_client(hub: FakeHub, link: VanishingMediaLink, **kwargs) -> LibreSyncClient:
    """A client whose 7777 runs through the relay and whose 50006 does not."""
    return LibreSyncClient(
        "127.0.0.1",
        control_port=hub.control_port,
        media_port=link.port,
        base_delay=0.01,
        max_delay=0.02,
        **kwargs,
    )


def strand_the_socket(conn) -> None:
    """Leave a connection's socket open but unwritable, as a departed peer does.

    On a LAN, a host that has gone makes the *write* fail — the kernel gets no
    ARP reply — while the read side blocks for ever, because no FIN was ever
    sent. Loopback has no such failure to offer, so the same pair of symptoms
    is produced from this end instead: shutting the write half makes every
    later write raise EPIPE, and `VanishingMediaLink` neither reads nor closes,
    so nothing comes back to wake the read loop either.
    """
    conn._writer.get_extra_info("socket").shutdown(socket.SHUT_WR)


async def test_the_media_socket_notices_a_peer_that_vanished_without_a_fin(hub, link):
    """A defect found on real hardware, in the form it took there.

    Mains pulled with the client connected: 50006 noticed inside one poll
    interval and reconnected on its own, 7777 stayed blocked in `read()` for
    the whole session, and `available` — `control.connected and
    media.connected` — went back to True with the media half only believing it
    was up. In that state no push can arrive and every command fails.
    """
    client = make_relayed_client(hub, link, poll_interval=0.05)
    # `available` goes False and back inside one reconnect delay, which is
    # shorter than `until` samples; a subscriber sees every state.
    seen: list[DeviceState] = []
    client.subscribe(seen.append)
    await client.async_connect()
    await until(lambda: client.state.available and client.state.model is not None)

    link.vanish()
    strand_the_socket(client._media)

    await until(lambda: any(s.available is False for s in seen), timeout=3.0)
    assert client._control.connected is True  # 50006 was never touched
    await client.async_disconnect()


async def test_the_7777_session_registers_again_after_a_silent_disappearance(hub, link):
    """The assertion whose absence let the defect ship.

    `test_a_connection_dropped_mid_frame_recovers` closes the hub's writers,
    which is a graceful close — the case that already worked. When the peer
    merely stops existing, nothing runs `_on_media_connect` again unless
    something writes first, and an unregistered session answers every request
    `mid 0x0000 status 0x03` and drops it while the entity reads healthy.
    """
    client = make_relayed_client(hub, link, poll_interval=0.05)
    await client.async_connect()
    await until(lambda: client.state.model is not None)
    assert registrations(hub) == 1

    link.vanish()
    strand_the_socket(client._media)

    await until(lambda: registrations(hub) == 2, timeout=3.0)
    await until(lambda: client.state.available is True, timeout=3.0)
    await client.async_disconnect()


async def test_connecting_twice_does_not_fork_a_second_poll_loop(hub):
    """The same defect `Connection.start()` had, in the client's own lifecycle.

    An unguarded `create_task` orphans the first loop and orphans it invisibly:
    the second assignment drops the only reference `async_disconnect` cancels,
    so the first goes on polling a device the integration has thrown away.
    """
    client = make_client(hub, poll_interval=0.05)
    await client.async_connect()
    await until(lambda: client.state.model is not None)

    await client.async_connect()
    await asyncio.sleep(0.2)

    assert poll_tasks() == 1

    await client.async_disconnect()
    assert poll_tasks() == 0


async def test_the_poll_does_not_confirm_a_command_it_did_not_cause(hub):
    """Poll traffic and a command in flight, on one bus, over the same keys.

    `async_set_power` is the sharp case: it reads power before it acts, and the
    poll induces `02 0e` frames carrying exactly the key its predicates look
    at. The separation holds only while those predicates stay value-exact — a
    future command reverting to a loose `"key" in patch` would be confirmed by
    a poll reply it did not cause, and nothing else in the suite would notice.
    """
    client = make_client(hub, poll_interval=0.01)
    await client.async_connect()
    await until(lambda: client.state.power is True)
    await asyncio.sleep(0.05)  # several poll cycles land before the command starts

    loop = asyncio.get_running_loop()
    answer = hub._answer_control

    def switch_slowly(frame: bytes) -> None:
        """The hub takes a moment to switch, and reports the old value until it
        does — so every poll reply inside that window carries exactly the value
        the command is trying to change, which is what a loose predicate would
        mistake for its own confirmation."""
        if frame[3:5] == bytes.fromhex("020f"):
            loop.call_later(0.15, answer, frame)
            return
        answer(frame)

    hub._answer_control = switch_slowly

    await client.async_set_power(False)

    toggles = [f for f in hub.received["control"] if f[3:5] == bytes.fromhex("020f")]
    assert len(toggles) == 1  # not confirmed early, and not sent twice
    assert hub.power is False
    assert client.state.power is False
    assert client._waiters == []
    await client.async_disconnect()


async def test_availability_is_the_socket_not_the_power_state(hub):
    """Port 7777 answers even when the hub is powered down."""
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.available)
    await client.async_set_power(False)
    assert client.state.power is False
    assert client.state.available is True
    await client.async_disconnect()


async def test_availability_goes_false_when_the_hub_disappears(hub):
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.available)
    await hub.stop()
    await until(lambda: client.state.available is False, timeout=3.0)
    await client.async_disconnect()


async def test_refresh_re_reads_every_property_on_demand(hub):
    """`homeassistant.update_entity`: everything, not the poll loop's two."""
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.model is not None)
    hub.received["control"].clear()
    hub.received["media"].clear()

    await client.async_refresh()

    # `send` returns once the frame is drained, not once the hub has read it.
    await until(lambda: len(hub.received["control"]) == 6 and len(hub.received["media"]) == 6)
    assert {bytes(f[3:5]) for f in hub.received["control"]} == {
        bytes.fromhex("0201"),
        bytes.fromhex("020d"),
        bytes.fromhex("0220"),  # volume, mute and audio activity in one reply
        bytes.fromhex("0305"),
        bytes.fromhex("0805"),
        bytes.fromhex("0702"),
    }
    assert {media_command(f, from_device=False) for f in hub.received["media"]} == {
        0x002A,
        0x0040,
        0x0033,
        0x003F,
        0x00D0,
        0x00E7,  # the factory serial
    }
    await client.async_disconnect()


async def test_refresh_before_the_socket_is_up_raises(hub):
    """Unlike the poll loop, `async_refresh` is free to raise: someone asked."""
    client = make_client(hub)
    with pytest.raises(NotConnectedError):
        await client.async_refresh()


async def test_the_whole_labelled_capture_replays_into_a_sane_state(hub):
    """The remote-labelled session, byte for byte, through the real dispatch path."""
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.model is not None)

    for frame in tap_log_frames("2026-08-15-remote-labelled-50006.log"):
        hub.broadcast_control(frame)
    for frame in tap_log_frames("2026-08-15-remote-labelled-7777.log"):
        hub.push_media(frame)
    await asyncio.sleep(0.3)

    assert client.state.power is not None
    assert client.state.volume is not None
    assert client.state.source is not None
    assert client.state.now_playing is not None
    assert client.diagnostics()["unknown_frames"]["control"] == []
    assert len(client.diagnostics()["unknown_frames"]["media"]) <= 2  # 0x00e0 only
    await client.async_disconnect()


async def test_a_stream_of_garbage_does_not_kill_the_connection(hub):
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.model is not None)

    hub.broadcast_control(b"\xde\xad\xbe\xef" * 8)
    hub.broadcast_control(bytes.fromhex("000400020e00"))
    await until(lambda: client.state.power is False)

    assert client.state.available is True
    assert client.diagnostics()["counters"]["control_resyncs"] > 0
    await client.async_disconnect()


async def test_a_connection_dropped_mid_frame_recovers(hub):
    """Half a frame arrives, then the socket dies. The half must not poison the next."""
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.model is not None)
    # Both transitions this test waits on are shorter than the 10ms poll:
    # `available` goes False and back inside one reconnect delay, and the power
    # frame injected below is overwritten by the reply to the reconnect's own
    # power query. Subscribers see every state, so both are observed rather than
    # sampled.
    seen: list[DeviceState] = []
    client.subscribe(seen.append)

    hub.broadcast_control(bytes.fromhex("014a0002027b2273"))  # a truncated source list
    await asyncio.sleep(0.05)
    # Both sockets, as a real network drop takes them: closing only 50006 would
    # leave the 7777 session up and registered, and the registration count below
    # would then prove nothing.
    for writer in list(hub._control_writers) + list(hub._media_writers):
        writer.close()

    await until(lambda: any(s.available is False for s in seen), timeout=3.0)
    await until(lambda: client.state.available is True, timeout=5.0)
    hub.broadcast_control(bytes.fromhex("000400020e00"))
    await until(lambda: any(s.power is False for s in seen))
    # Availability alone would not notice a 7777 session that came back
    # unregistered: every reconnection must register again, once per socket.
    assert registrations(hub) == 2
    # The docstring's actual claim, which needs evidence rather than survival:
    # the 8 bytes above announced a 332-byte frame, so a reader kept across the
    # reconnect would swallow the new connection's frames to finish it and hand
    # the dispatcher one long frame of spliced garbage. That leaves fingerprints
    # — an undecodable frame recorded as unknown, and a resync when the reader
    # steps over what it could not use. Both must be untouched: the half-frame
    # died with its socket.
    diagnostics = client.diagnostics()
    assert diagnostics["unknown_frames"]["control"] == []
    assert diagnostics["counters"]["control_resyncs"] == 0
    await client.async_disconnect()


def test_the_public_api_does_not_expose_a_raw_media_builder():
    """`build_media` takes an arbitrary message-box id. It is not public API."""
    import aiolibresync

    assert "build_media" not in aiolibresync.__all__
    assert not hasattr(aiolibresync, "build_media")
    assert aiolibresync.LibreSyncClient is not None


async def test_the_package_docstring_example_actually_runs(hub):
    """The example new users copy is executed here, not merely read.

    It shipped once with `async_set_power` on the line straight after
    `async_connect`, which raises `NotConnectedError` every time: the sockets are
    only scheduled by then. Nothing caught it because no test ran the example.
    This one does, on the real text pulled out of `aiolibresync.__doc__`, so the
    two cannot drift apart again.
    """
    import textwrap

    import aiolibresync

    lines = aiolibresync.__doc__.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "import asyncio")
    end = next(i for i, line in enumerate(lines) if line.strip() == "asyncio.run(main())")
    source = textwrap.dedent("\n".join(lines[start:end]))

    # Only the address and the idle sleep are substituted; the asserts make a
    # silent no-op impossible if the example is ever reworded.
    for old, new in (
        (
            'LibreSyncClient("192.168.1.50")',
            f'LibreSyncClient("127.0.0.1", control_port={hub.control_port},'
            f" media_port={hub.media_port})",
        ),
        ("asyncio.sleep(60)", "asyncio.sleep(0.05)"),
    ):
        assert old in source, f"the example no longer contains {old!r}"
        source = source.replace(old, new)

    namespace: dict = {}
    exec(compile(source, "<aiolibresync docstring>", "exec"), namespace)
    await namespace["main"]()


async def test_the_serial_is_read_on_connect_and_redacted_in_diagnostics(hub):
    """The identifier a config entry prefers, and one that must not leak in a bug report."""
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.serial is not None)
    assert client.state.serial == "FAKE0SERIAL000000000"
    assert client.diagnostics()["state"]["serial"] == "<redacted>"
    await client.async_disconnect()


async def test_a_unit_without_a_valid_serial_has_none_and_nothing_unknown(hub):
    hub.serial_box = b"FAKE0SERIAL000000000,INVALID"
    client = make_client(hub)
    await client.async_connect()
    await until(lambda: client.state.model is not None)
    await until(
        lambda: any(media_command(f, from_device=False) == 0x00E7 for f in hub.received["media"])
    )
    await asyncio.sleep(0.05)
    assert client.state.serial is None
    assert client.diagnostics()["state"]["serial"] is None
    assert client.diagnostics()["unknown_frames"]["media"] == []
    await client.async_disconnect()


async def test_without_media_the_client_uses_50006_alone(hub):
    """The newer hubs, whose 7777 is wrapped in TLS: never touched, and not
    needed for the hub to be available."""
    client = make_client(hub, media=False)
    await client.async_connect(timeout=2.0)
    await until(lambda: client.state.source is not None)

    assert client.state.available is True
    assert client.state.power is True
    assert client.state.source == Source(index=0, name="Streaming")
    assert open_sockets(hub) == (1, 0)
    assert hub.received["media"] == []

    await client.async_select_source(6)
    assert client.state.source is not None
    assert client.state.source.index == 6
    with pytest.raises(NotConnectedError):
        await client.async_set_volume(30)
    await client.async_refresh()
    assert hub.received["media"] == []
    await client.async_disconnect()
    assert client.state.available is False


# --- 7777 over TLS, on the newer hubs -----------------------------------------


@pytest.fixture
def tls():
    return FakeTLS.make()


@pytest.fixture
async def tls_hub(tls):
    hub = FakeHub(media_ssl=tls.hub)
    await hub.start()
    yield hub
    await hub.stop()


def is_tls(conn) -> bool:
    """Whether a live connection runs over TLS, from its own transport.

    Reaching into the client's private connections: the hub's view cannot tell
    a TLS 7777 from a plain one, since the frames inside are the same.
    """
    assert conn._writer is not None
    return conn._writer.get_extra_info("ssl_object") is not None


async def test_with_media_ssl_7777_runs_over_tls_and_nothing_else_changes(tls_hub, tls):
    """Registration, the connect queries, pushes and commands, exactly as on
    the original hub, with the host given as an IP address."""
    client = make_client(tls_hub, media_ssl=tls.client)
    await client.async_connect(timeout=2.0)
    await until(lambda: client.state.model is not None)

    assert client.state.available is True
    assert is_tls(client._media)
    assert not is_tls(client._control)
    assert client.state.volume == 28
    assert client.state.play_state is not None
    register = next(
        f for f in tls_hub.received["media"] if media_command(f, from_device=False) == MID_REGISTER
    )
    assert register[MEDIA_HEADER:] == b"127.0.0.1"

    await client.async_set_volume(30)
    assert tls_hub.volume == 30
    await client.async_media_pause()
    tls_hub.push_media(device_frame(MID_VOLUME, b"35"))
    await until(lambda: client.state.volume == 35)
    await client.async_disconnect()
    assert client.state.available is False


async def test_without_media_ssl_7777_stays_plain_tcp(hub):
    client = make_client(hub)
    await client.async_connect(timeout=2.0)

    assert client.state.available is True
    assert not is_tls(client._media)
    assert not is_tls(client._control)
    assert registrations(hub) == 1
    await client.async_disconnect()


async def test_a_refused_handshake_backs_off_and_warns_once(tls_hub, tls, caplog):
    """The hub wants a client certificate and gets none. The client keeps
    trying, with backoff, says so once at warning level, and stays unavailable
    without taking 50006 or its own teardown down with it."""
    caplog.set_level(logging.DEBUG, logger="aiolibresync.connection")
    client = make_client(tls_hub, media_ssl=tls.client_without_certificate)
    with pytest.raises(NotConnectedError):
        await client.async_connect(timeout=0.5)

    failures = [r for r in caplog.records if "TLS handshake failed" in r.getMessage()]
    assert len(failures) >= 2
    assert [r.levelno for r in failures].count(logging.WARNING) == 1
    assert tls_hub.received["media"] == []
    assert client.state.available is False


def test_media_ssl_with_media_false_is_refused(tls):
    with pytest.raises(ValueError, match="media_ssl"):
        LibreSyncClient("127.0.0.1", media=False, media_ssl=tls.client)


# --- room correction on the LS10 hubs ----------------------------------------


@pytest.fixture
async def grouped_hub():
    hub = FakeHub(room_grouped=True)
    await hub.start()
    yield hub
    await hub.stop()


def room_frames(hub: FakeHub, opcode: int) -> list[bytes]:
    return [bytes(f[5:]) for f in hub.received["control"] if f[3:5] == bytes([0x07, opcode])]


async def test_on_ls10_room_correction_is_read_and_set_in_the_addressed_form(grouped_hub):
    """The Surround Hub's form. Its answer to the bare query, address `00`,
    must not overwrite the state the addressed one reported."""
    client = make_client(grouped_hub, media=False, module="LS10")
    await client.async_connect(timeout=2.0)
    await until(lambda: client.state.room_correction is True)
    assert room_frames(grouped_hub, 0x02) == [b"", b"\x01"]

    await client.async_set_room_correction(False)

    assert grouped_hub.room_correction is False
    assert client.state.room_correction is False
    assert room_frames(grouped_hub, 0x01) == [b"\x01\x00"]
    await client.async_disconnect()


async def test_on_ls10_the_poll_loop_asks_in_both_forms(grouped_hub):
    client = make_client(grouped_hub, media=False, module="LS10", poll_interval=0.05)
    await client.async_connect(timeout=2.0)
    await until(lambda: client.state.room_correction is True)
    grouped_hub.received["control"].clear()

    await asyncio.sleep(0.2)

    assert set(room_frames(grouped_hub, 0x02)) == {b"", b"\x01"}
    assert client.state.room_correction is True
    await client.async_disconnect()


async def test_without_a_module_room_correction_keeps_the_original_form(hub):
    client = make_client(hub)
    await client.async_connect(timeout=2.0)
    await until(lambda: client.state.room_correction is True)
    await client.async_set_room_correction(False)

    assert set(room_frames(hub, 0x02)) == {b""}
    assert room_frames(hub, 0x01) == [b"\x00"]
    assert client.diagnostics()["module"] is None
    await client.async_disconnect()
