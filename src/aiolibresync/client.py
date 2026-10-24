"""The client: two sockets, one immutable state, callbacks on change.

Data flows one way — socket, frame, decoder, patch, new state, subscribers — and
commands travel the reverse path **without** updating state optimistically. The
device is talkative enough to afford waiting for its own confirmation, and on a
shared broadcast bus an optimistic write would be a guess about a device three
other clients are also driving.
"""

import asyncio
import logging
import ssl
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from . import media, systemcontrol
from .connection import Connection, cancel_and_wait
from .const import (
    CONFIRM_TIMEOUT,
    CONTROL_PORT,
    GROUP_MODULES,
    MEDIA_PORT,
    POLL_INTERVAL,
    RECONNECT_BASE,
    RECONNECT_MAX,
    REGISTER_SETTLE,
    REREGISTER_COOLDOWN,
    ROOM_REREAD_DELAY,
    ROOM_REREAD_INTERVAL,
)
from .exceptions import ConfirmationTimeout, NotConnectedError
from .frames import ControlFrameReader, MediaFrameReader
from .state import DeviceState

_LOGGER = logging.getLogger(__name__)

_UNKNOWN_FRAME_LIMIT = 20
#: The key a transport acknowledgement puts in the patch confirmation waiters
#: see. It never reaches the state: an acknowledgement is an event, not a value.
_TRANSPORT_ACK = "transport_ack"
_NO_URL = "<none>"


def _redact_now_playing(track: dict[str, Any]) -> dict[str, Any]:
    """Presence, not content — what the user is listening to is not a device fact.

    A local renderer's `CoverArtUrl` is very plausibly `http://192.168.x.y/...`,
    which would walk a diagnostics dump's redacted host straight back in, so the
    URL is reduced to its scheme.
    """
    url = track.get("artwork_url")
    scheme = url.split("://", 1)[0] if isinstance(url, str) and "://" in url else None
    return {
        "title": track.get("title") is not None,
        "artist": track.get("artist") is not None,
        "album": track.get("album") is not None,
        "app": track.get("app") is not None,
        "artwork_url_scheme": scheme if scheme is not None else _NO_URL,
        "duration_ms": track.get("duration_ms"),
    }


class LibreSyncClient:
    """Control one LibreSync device.

    Both ports are required and neither is a superset of the other: 50006
    reports source changes and carries volume, mute and source in one frame,
    7777 reports transport, metadata and position.

    Except with `media=False`, for the newer hubs whose 7777 is wrapped in
    mutual TLS: the client then uses 50006 alone, is available when 50006 is,
    and the 7777 commands raise `NotConnectedError` as they do when that port
    is down. Power, source and what 50006 reports still work.

    Or, on those hubs, with `media_ssl`: a context carrying a client
    certificate the hub accepts. 7777 is then opened over TLS and everything
    above works as on the original hub, because the frames inside the tunnel
    are the same. 50006 stays plain TCP either way, as it is on every hub.
    The library builds no context and reads no file: which certificate to
    present, and whether to verify the hub's, is the caller's. Building one
    is blocking I/O (`load_cert_chain` reads the files), so in Home Assistant
    it belongs in the executor, not on the event loop.

    `module` is the Libre module LSSDP reports (`async_query_lssdp`). On
    `LS10` room correction is set and read in that module's addressed form;
    reading the report needs nothing, since its width tells the two forms
    apart.
    """

    def __init__(
        self,
        host: str,
        *,
        control_port: int = CONTROL_PORT,
        media_port: int = MEDIA_PORT,
        base_delay: float = RECONNECT_BASE,
        max_delay: float = RECONNECT_MAX,
        confirm_timeout: float = CONFIRM_TIMEOUT,
        poll_interval: float = POLL_INTERVAL,
        media: bool = True,
        media_ssl: ssl.SSLContext | None = None,
        module: str | None = None,
    ) -> None:
        if media_ssl is not None and not media:
            # Two answers to the same hub, and silently preferring either one
            # would hide a caller bug that decides whether 7777 is used at all.
            raise ValueError("media_ssl is for port 7777, which media=False leaves unused")
        self.host = host
        self.media = media
        self.module = module
        self._room_grouped = module in GROUP_MODULES
        # Both queries on LS10. The Surround Hub answers the bare one with an
        # address of `00`, which sets nothing, and the grouped one with the
        # state; the Stereo Hub HT answered the bare one with address `01`, and
        # nobody has sent it the grouped one. Each hub gets the one it answers.
        self._room_queries = (
            (systemcontrol.QUERY_ROOM_CORRECTION, systemcontrol.QUERY_ROOM_CORRECTION_GROUP)
            if self._room_grouped
            else (systemcontrol.QUERY_ROOM_CORRECTION,)
        )
        self._control_connect_queries = (
            *systemcontrol.CONNECT_QUERIES,
            *self._room_queries[1:],
        )
        self._confirm_timeout = confirm_timeout
        self._poll_interval = poll_interval
        self._state = DeviceState()
        self._subscribers: list[Callable[[DeviceState], None]] = []
        self._waiters: list[
            tuple[Callable[[dict[str, Any], DeviceState], bool], asyncio.Future[DeviceState]]
        ] = []
        self._unknown: dict[str, deque[str]] = {
            "control": deque(maxlen=_UNKNOWN_FRAME_LIMIT),
            "media": deque(maxlen=_UNKNOWN_FRAME_LIMIT),
        }
        self._unknown_signatures: dict[str, Counter[str]] = {
            "control": Counter(),
            "media": Counter(),
        }
        self._counters = {
            "control_frames": 0,
            "media_frames": 0,
            # A hub refusing everything otherwise produces a dump byte-for-byte
            # identical to a healthy idle one: both statuses decode to an empty
            # patch and die on _apply's early return.
            "media_refused": 0,
            "media_not_registered": 0,
        }
        self._last_media_recovery: float | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._control = Connection(
            host,
            control_port,
            reader_factory=ControlFrameReader,
            on_frame=self._on_control_frame,
            on_availability=lambda _: self._refresh_availability(),
            on_connect=self._on_control_connect,
            base_delay=base_delay,
            max_delay=max_delay,
        )
        self._media = Connection(
            host,
            media_port,
            reader_factory=MediaFrameReader,
            on_frame=self._on_media_frame,
            on_availability=lambda _: self._refresh_availability(),
            on_connect=self._on_media_connect,
            base_delay=base_delay,
            max_delay=max_delay,
            ssl_context=media_ssl,
        )

    # --- lifecycle ----------------------------------------------------------

    async def async_connect(
        self,
        *,
        # A readiness wait with its own cleanup, not a deadline the caller could
        # impose with `asyncio.timeout`: on expiry the client disconnects too.
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> None:
        """Open both sockets, or 50006 alone with `media=False`, and start the poll loop.

        They keep themselves open from here on. Without `timeout` this returns
        once the sockets are *scheduled*, and never raises. With it, it also
        waits until the ports are open, and raises `NotConnectedError` if they
        are not within `timeout` seconds. On that error, and on cancellation,
        the client is disconnected again before the exception propagates, so a
        failed attempt leaves nothing running.

        Calling this twice is a caller move rather than a theoretical one —
        Home Assistant retries `async_setup_entry` — so the poll loop is
        guarded exactly as `Connection.start` is: a second `create_task` would
        overwrite the only reference `async_disconnect` cancels, and the orphan
        would go on polling a device the integration has already thrown away.
        """
        await self._control.start()
        if self.media:
            await self._media.start()
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop(), name="aiolibresync-poll")
        if timeout is None or self._state.available:
            return
        try:
            await self._async_expect(lambda _patch, state: state.available, timeout=timeout)
        except ConfirmationTimeout as err:
            await self.async_disconnect()
            raise NotConnectedError(
                f"{self.host} did not answer on its ports within {timeout} s"
            ) from err
        except asyncio.CancelledError:
            await self.async_disconnect()
            raise

    async def async_disconnect(self) -> None:
        # Cancel the poll loop first: left running, it would outlive unload and
        # go on driving a client the integration has already discarded.
        task, self._poll_task = self._poll_task, None
        try:
            if task is not None:
                await cancel_and_wait(task)
        finally:
            try:
                await self._control.stop()
            finally:
                await self._media.stop()
                self._apply({"available": False})

    async def _poll_loop(self) -> None:
        """Poll the properties that announce nothing — on both sockets.

        Room correction is never broadcast at all, whoever changes it, which is
        the whole case for polling 50006 on its own. Power is polled for a
        weaker reason: an early observation had a press on the RF remote reach no
        client, and firmware 1.52 was then measured pushing `02 0e` on that path
        too — one unit, two contradictory observations, so the query stays as
        the staleness bound in case the first behaviour is real elsewhere.
        Nothing else belongs on 50006: `02 4a`, `02 4d` and `02 8b` are
        invariant configuration, and every other property is pushed. Extra
        queries would be traffic on a bus other clients share.

        Port 7777 is polled for a different reason, and it is not staleness:
        **a socket only learns that its peer has gone by writing to it.** A hub
        that loses mains sends no FIN, so `reader.read()` blocks on the dead
        connection for ever and the reconnect loop — and with it
        `_on_media_connect`, which is the only thing that registers the LUCI
        session — never runs. Measured with the hub's mains pulled: 50006
        noticed inside one poll interval because the poll writes there, 7777
        never wrote and never noticed, and `available` returned to True over a
        session on which every command was refused. `QUERY_PLAY_STATE` is the
        frame for it because it costs nothing to be wrong about: it is already
        in the connect burst, it is idempotent, and its reply folds in as a
        play state the device is in anyway.

        The loop must not die, because a dead one leaves power and room
        correction quietly frozen while everything else still looks healthy —
        and worse, `async_disconnect` awaits this task, so a poll loop holding
        an exception makes unloading raise too. `except Exception` is what
        keeps it alive, since nothing above would notice this task ending.
        Sleeping first is what keeps a socket that is down from becoming a busy
        loop.
        """
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._poll(self._control, systemcontrol.QUERY_POWER, *self._room_queries)
                await self._poll(self._media, media.QUERY_PLAY_STATE)
            except Exception:
                _LOGGER.exception("the poll loop hit an unexpected error")

    async def _poll(self, conn: Connection, *queries: bytes) -> None:
        """Send one port's poll queries, and swallow the failure that is normal.

        A try of its own per port rather than one around both, because the two
        ports fail independently and the second one is polled precisely for the
        cycle in which it is about to fail: a shared try would let a 50006 that
        went down stop 7777 from ever discovering that it had gone down too —
        which is the same silence this poll exists to break, one port along.
        """
        if not conn.connected:
            return
        try:
            await conn.send(*queries)
        except NotConnectedError as err:
            _LOGGER.debug("poll on port %s failed: %s", conn.port, err)

    async def async_refresh(self) -> None:
        """Re-read everything. For a Home Assistant `homeassistant.update_entity`.

        Not the poll loop, and free to raise: a caller asked for this one.
        """
        await self._control.send(*self._control_connect_queries)
        if self.media:
            await self._media.send(*media.CONNECT_QUERIES)

    async def _on_control_connect(self, conn: Connection) -> None:
        await conn.send(*self._control_connect_queries)

    async def _on_media_connect(self, conn: Connection) -> None:
        ip = conn.local_ip
        if ip is None:
            # Defensive by invariant: Connection assigns its writer with no
            # intervening await, so this cannot fire while on_connect runs. If
            # it ever did, the session would stay unregistered and the device
            # would silently drop every request — so say so rather than return
            # in silence.
            _LOGGER.warning("no local address for the 7777 socket; leaving it unregistered")
            return
        await conn.send(media.register(ip))
        await asyncio.sleep(REGISTER_SETTLE)  # the app waits ~100 ms too
        await conn.send(*media.CONNECT_QUERIES)

    # --- state --------------------------------------------------------------

    @property
    def state(self) -> DeviceState:
        return self._state

    def subscribe(self, callback: Callable[[DeviceState], None]) -> Callable[[], None]:
        """Register a callback, and return the function that removes it.

        Callbacks run on the event loop, inside the socket read loop: they must
        not block and cannot await. Anything slow belongs in a task of its own.
        """
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def _on_control_frame(self, frame: bytes) -> None:
        self._counters["control_frames"] += 1
        patch = systemcontrol.decode(frame)
        if patch is None:
            self._record_unknown("control", frame, frame[3:5].hex())
            return
        self._apply(patch)

    def _on_media_frame(self, frame: bytes) -> None:
        self._counters["media_frames"] += 1
        if media.is_refused(frame):
            self._counters["media_refused"] += 1
        if media.is_not_registered(frame):
            self._counters["media_not_registered"] += 1
            self._recover_media_session()
            return
        if media.is_transport_ack(frame):
            self._notify_waiters({_TRANSPORT_ACK: True}, self._state)
            return
        patch = media.decode(frame)
        if patch is None:
            self._record_unknown("media", frame, f"{media.mid_of(frame):04x}")
            return
        self._apply(patch)

    def _record_unknown(self, port: str, frame: bytes, signature: str) -> None:
        """Keep the last few frames verbatim, and a tally of what kinds arrived.

        The samples alone lose the shape of the problem: one repeating unknown
        push fills all twenty slots and evicts every other kind, and twenty
        entries read the same whether they stand for twenty frames or twenty
        thousand.
        """
        shown = frame.hex(" ")
        self._unknown[port].append(shown)
        self._unknown_signatures[port][signature] += 1
        _LOGGER.debug("unknown %s frame: %s", port, shown)

    # --- the 7777 session ----------------------------------------------------

    def _recover_media_session(self) -> None:
        """Re-register after `status 0x03`, at most once per cooldown.

        Nothing else can recover this in time. A confirmation waiter only
        exists while a command is in flight, so a refusal during the connect
        burst or on a push would be seen by nobody. The poll's own 7777 query
        does draw a fresh refusal, and would arrive here through this same
        path — but not for up to POLL_INTERVAL, and every push and command
        inside that window is lost.

        Rate-limited on purpose. The connect burst is several queries, so a
        session the device has forgotten refuses several times in a row, and
        the cooldown turns that into one recovery. It also bounds the rest:
        `Connection._run` counts a socket that received anything as proved and
        retries at RECONNECT_BASE, so the connection's own backoff cannot
        throttle a forced drop — this is what does.

        The socket is dropped rather than the register frame re-sent in place.
        `_on_media_connect` is the only sequence known to produce a working
        session, and by the time a refusal arrives everything read over that
        socket is stale anyway — so the connect queries want re-reading too.
        """
        now = asyncio.get_running_loop().time()
        if (
            self._last_media_recovery is not None
            and now - self._last_media_recovery < REREGISTER_COOLDOWN
        ):
            _LOGGER.debug("7777 refused again inside the re-register cooldown; leaving it")
            return
        self._last_media_recovery = now
        _LOGGER.warning("the 7777 session is not registered; dropping it to register again")
        self._media.drop()

    def _apply(self, patch: dict[str, Any]) -> None:
        """Fold a patch into the state, and notify only on a real change.

        This is where protocol-level duplicates die: the second `02 0e`, the
        second `0x40` and the `0x2d` twin of `0x2a` all produce a state equal to
        the current one, so no subscriber ever sees them.
        """
        if not patch:
            return
        # A key that is not a DeviceState field raises a bare TypeError out of
        # evolve(), deliberately. Connection._read_loop already wraps every
        # on_frame call in a broad except that logs with exception(), so a
        # malformed patch is loud and cannot kill the socket; wrapping it in a
        # LibreSyncError would dress a decoder bug up as a protocol error and
        # make it easier to swallow.
        new = self._state.evolve(**patch)
        # Before the equality check, and that is load-bearing rather than
        # accidental: a confirmation waits on a key being present in the patch,
        # and the common case is a query confirming the value already held,
        # which changes no state. Notifying after the check would make setting a
        # property to the value it already has time out every time.
        self._notify_waiters(patch, new)
        if new == self._state:
            return
        self._state = new
        for callback in list(self._subscribers):
            try:
                # `self._state`, not `new`: a subscriber that synchronously
                # causes another _apply publishes a newer state, and the rest of
                # this loop must not then hand out the older one.
                callback(self._state)
            # One bad subscriber must not break the rest.
            except Exception:
                _LOGGER.exception("subscriber failed")

    def _refresh_availability(self) -> None:
        """`unavailable` is not `off`.

        Availability is the TCP connection; power is the hub's own state. Port
        7777 answers even when the hub is powered down, so conflating them is
        the single most likely bug in this project.
        """
        self._apply(
            {"available": self._control.connected and (self._media.connected or not self.media)}
        )

    def _notify_waiters(self, patch: dict[str, Any], new: DeviceState) -> None:
        """Resolve the futures a command's confirmation waits on.

        Predicates must be pure. They run once per patch, on the read loop, and
        against every frame either port produces — so a side effect here would
        fire on traffic that has nothing to do with the command that armed it.
        """
        for predicate, future in list(self._waiters):
            if future.done():
                self._waiters.remove((predicate, future))
                continue
            try:
                matched = predicate(patch, new)
            # A caller's predicate must not break the read loop.
            except Exception:
                matched = False
            if matched:
                future.set_result(new)
                self._waiters.remove((predicate, future))

    # --- diagnostics ---------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """Everything needed to debug a stranger's hub, with the address removed.

        The host, the serial and the metadata the user is listening to are
        redacted; the rest of the state stays, because volume, power, source
        names and DSP flags are device facts and are the whole point of the dump.

        `unknown_frames` carries raw device bytes. They are unclassified by
        definition, so they may contain anything the firmware chose to send.

        The resync counters accumulate for the lifetime of the connection,
        across reconnections — unlike the frame reader's `dropped`, which is
        per chunk and starts again with each new socket. A large number here is
        therefore a history, not necessarily a fault still in progress.
        """
        return {
            "host": "<redacted>",
            "module": self.module,
            "connected": {"control": self._control.connected, "media": self._media.connected},
            "state": self._redacted_state(),
            "counters": {
                **self._counters,
                "control_unknown": self._unknown_signatures["control"].total(),
                "media_unknown": self._unknown_signatures["media"].total(),
                "control_resyncs": self._control.resyncs,
                "media_resyncs": self._media.resyncs,
            },
            "unknown_frames": {port: list(frames) for port, frames in self._unknown.items()},
            "unknown_signatures": {
                port: dict(counts) for port, counts in self._unknown_signatures.items()
            },
        }

    def _redacted_state(self) -> dict[str, Any]:
        state = asdict(self._state)
        track = state.get("now_playing")
        if isinstance(track, dict):
            state["now_playing"] = _redact_now_playing(track)
        if state.get("serial") is not None:
            # An identifier of one physical unit, like the host.
            state["serial"] = "<redacted>"
        return state

    # --- commands ------------------------------------------------------------

    async def async_set_power(self, on: bool) -> None:
        """Turn the hub on or off — reading its state first.

        `02 0f` is a toggle and ignores its data byte. The vendor's own app
        sends `0f 01` for "on" and therefore turns a powered-on hub off when its
        cache is stale; this is the bug not to reproduce. If the read fails and
        nothing is cached we raise rather than toggle blind.

        Reading first also makes this safe to retry: a call that lost its
        confirmation and is issued again re-reads and does nothing, where a
        bare toggle would undo itself.

        Raises `NotConnectedError` if port 50006 is down, `ConfirmationTimeout`
        if the read went unanswered with nothing cached or the toggle was not
        confirmed in time.
        """
        current = await self._async_read_power()
        if current is on:
            return
        await self._async_expect(
            lambda _patch, state: state.power is on,
            send=self._control.send(systemcontrol.toggle_power()),
        )

    async def _async_read_power(self) -> bool | None:
        try:
            state = await self._async_expect(
                lambda patch, _state: "power" in patch,
                send=self._control.send(systemcontrol.QUERY_POWER),
            )
        except ConfirmationTimeout:
            if self._state.power is None:
                raise
            _LOGGER.warning("power query went unanswered; using the last known value")
            return self._state.power
        return state.power

    async def async_select_source(self, index: int) -> None:
        """Select a source by its JSON `ix`.

        Switching away from Streaming terminates the Chromecast session, and
        switching back does not resume it.

        Raises `ValueError` for an index outside 0..255, `NotConnectedError` if
        port 50006 is down, `ConfirmationTimeout` if the device does not
        confirm in time.
        """
        frame = systemcontrol.select_source(index)
        await self._async_expect(
            lambda _patch, state: state.source is not None and state.source.index == index,
            send=self._control.send(frame),
        )

    async def async_set_volume(self, level: int) -> None:
        """Set the volume, 0..100.

        Raises `ValueError` for a level outside that range,
        `NotConnectedError` if port 7777 is down, `ConfirmationTimeout` if the
        device does not confirm in time.
        """
        frame = media.set_volume(level)
        await self._async_expect(
            lambda _patch, state: state.volume == level, send=self._media.send(frame)
        )

    async def async_media_play(self) -> None:
        """Play.

        Confirmed by the device's acknowledgement of the command, not by a play
        state: a command that changes nothing produces no play state at all.

        Raises `NotConnectedError` if port 7777 is down, `ConfirmationTimeout`
        if the device does not acknowledge the command in time.
        """
        await self._async_transport(media.play())

    async def async_media_pause(self) -> None:
        """Pause.

        Confirmed by the device's acknowledgement of the command, not by a play
        state: a command that changes nothing produces no play state at all.

        Raises `NotConnectedError` if port 7777 is down, `ConfirmationTimeout`
        if the device does not acknowledge the command in time.
        """
        await self._async_transport(media.pause())

    async def async_media_stop(self) -> None:
        """Stop.

        Confirmed by the device's acknowledgement of the command, not by a play
        state: a command that changes nothing produces no play state at all.

        Raises `NotConnectedError` if port 7777 is down, `ConfirmationTimeout`
        if the device does not acknowledge the command in time.
        """
        await self._async_transport(media.stop())

    async def _async_transport(self, frame: bytes) -> None:
        await self._async_expect(
            lambda patch, _state: _TRANSPORT_ACK in patch, send=self._media.send(frame)
        )

    async def async_media_next_track(self) -> None:
        """Next track. Indistinguishable from previous on the wire: the device
        reports the resulting track, not the button.

        The only command here that is not safe to retry: every resend really
        does skip another track, and fire-and-forget leaves the caller no way
        to tell a lost command from a delivered one.

        Raises `NotConnectedError` if port 7777 is down, and nothing else —
        it waits for no confirmation.
        """
        await self._media.send(media.next_track())

    async def async_media_previous_track(self) -> None:
        """Previous track. Not safe to retry, for the same reason as next: each
        resend moves another track and nothing reports whether the first
        arrived.

        Raises `NotConnectedError` if port 7777 is down, and nothing else —
        it waits for no confirmation.
        """
        await self._media.send(media.previous_track())

    async def async_set_room_correction(self, enabled: bool) -> None:
        """Set room correction, then re-read it until it takes — nothing is
        pushed, ever.

        A bounded retry rather than one query after a fixed wait. The
        confirmation demands the value asked for, not merely that a value was
        reported — a device that refused the set and reported the old one must
        not count as confirmation — and ROOM_REREAD_DELAY is shorter than the
        device, which took 0.21 s to 0.76 s over four writes on 2026-08-15, so
        a single stale read would otherwise burn the whole budget on a question
        already asked. Two of those four trials would have failed that way. The
        entire wait, initial delay included, is bounded by the confirmation
        timeout.

        With `module="LS10"` the set and the re-reads are the addressed form.
        The set is the vendor app's on a Surround Hub; on a Stereo Hub HT it is
        untested, and a hub that ignores it ends in `ConfirmationTimeout`.

        Raises `NotConnectedError` if port 50006 is down, `ConfirmationTimeout`
        if the re-reads never come back with the value asked for.
        """
        await self._control.send(
            systemcontrol.set_room_correction(enabled, grouped=self._room_grouped)
        )

        def confirmed(_patch: dict[str, Any], state: DeviceState) -> bool:
            return state.room_correction is enabled

        try:
            async with asyncio.timeout(self._confirm_timeout):
                await asyncio.sleep(ROOM_REREAD_DELAY)
                while True:
                    try:
                        await self._async_expect(
                            confirmed,
                            send=self._control.send(*self._room_queries),
                            timeout=ROOM_REREAD_INTERVAL,
                        )
                    except ConfirmationTimeout:
                        continue  # still stale: ask again, until the budget runs out
                    return
        except TimeoutError as err:
            raise ConfirmationTimeout("room correction did not take in time") from err

    async def async_set_manual_eq(self, enabled: bool) -> None:
        """Turn manual EQ on or off. Independent of room correction, not exclusive.

        Raises `NotConnectedError` if port 50006 is down, `ConfirmationTimeout`
        if the device does not confirm in time.
        """
        await self._async_expect(
            lambda _patch, state: state.manual_eq is enabled,
            send=self._control.send(systemcontrol.set_manual_eq(enabled)),
        )

    async def async_select_eq_preset(self, preset: int) -> None:
        """Select EQ preset 1..3.

        Raises `ValueError` for a preset outside that range,
        `NotConnectedError` if port 50006 is down, `ConfirmationTimeout` if the
        device does not confirm in time.
        """
        frame = systemcontrol.select_eq_preset(preset)
        await self._async_expect(
            lambda _patch, state: state.eq_preset == preset, send=self._control.send(frame)
        )

    async def _async_expect(
        self,
        predicate: Callable[[dict[str, Any], DeviceState], bool],
        *,
        send: Awaitable[None] | None = None,
        # The timeout is the point of this helper, not a parameter that should
        # have been an asyncio.timeout at the call site.
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> DeviceState:
        """Send a frame, then wait for a decoded frame that satisfies `predicate`.

        The waiter is armed before `send` runs, so a reply that arrives while
        the write is still draining is not missed. The send is not part of the
        timeout, which bounds the device's answer only.

        The predicate sees the patch *and* the resulting state, because the two
        questions differ: "did the device report power at all" is about the
        patch, "is power now on" is about the state. Replies cannot be
        correlated to requests on a shared broadcast bus, so this is a
        condition on the bus, not a response.
        """
        future: asyncio.Future[DeviceState] = asyncio.get_running_loop().create_future()
        self._waiters.append((predicate, future))
        try:
            if send is not None:
                await send
            async with asyncio.timeout(timeout if timeout is not None else self._confirm_timeout):
                return await future
        except TimeoutError as err:
            raise ConfirmationTimeout("the device did not confirm in time") from err
        finally:
            self._waiters[:] = [w for w in self._waiters if w[1] is not future]
