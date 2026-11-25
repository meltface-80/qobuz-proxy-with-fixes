"""
The DLNA state poll loop.

Polls the renderer's transport state (and position while playing) to notice
track ends, gapless transitions, takeovers and renderer quirks. When the
renderer sends UPnP AVTransport events, an event wakes the loop at once;
polling stays on as the backup for renderers whose events are late or absent.
"""

import asyncio
import logging
import time
from typing import Optional

from qobuz_proxy.backends.types import PlaybackState

from .timing import (
    NEAR_END_POLL_INTERVAL_SECONDS,
    NEAR_END_WINDOW_MS,
    PLAYBACK_START_GRACE_PERIOD_SECONDS,
    STATE_POLL_INTERVAL_SECONDS,
)
from .track_end import TrackEndMixin

logger = logging.getLogger(__name__)

_TRANSPORT_STATES = {
    "PLAYING": PlaybackState.PLAYING,
    "PAUSED_PLAYBACK": PlaybackState.PAUSED,
    "TRANSITIONING": PlaybackState.LOADING,
    "STOPPED": PlaybackState.STOPPED,
    "NO_MEDIA_PRESENT": PlaybackState.STOPPED,
}


class PollingMixin(TrackEndMixin):
    """Transport-state polling."""

    async def get_state(self) -> PlaybackState:
        """Current playback state from the device (the last known one if the read fails)."""
        state = await self._read_state()
        return self._state if state is None else state

    async def _read_state(self) -> Optional[PlaybackState]:
        """Transport state, or None when it could not be read.

        A failed read (SOAP timeout, Wi-Fi blip) is "unknown", not STOPPED:
        treating it as a stop advanced the queue on a renderer that was still
        playing (GitHub #51).
        """
        if not self._client:
            return PlaybackState.STOPPED
        state_str = await self._client.get_transport_info()
        if not state_str:
            return None
        # Unknown vendor states (e.g. "RECORDING") read as stopped, as before.
        return _TRANSPORT_STATES.get(state_str.strip().upper(), PlaybackState.STOPPED)

    def wake_poll(self) -> None:
        """Poll now instead of at the next interval (a UPnP event arrived)."""
        self._poll_wake.set()

    async def _wait_next_poll(self) -> None:
        delay = self._next_poll_delay()
        if not self._events_active:
            await asyncio.sleep(delay)
            return
        try:
            await asyncio.wait_for(self._poll_wake.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        self._poll_wake.clear()

    def _next_poll_delay(self) -> float:
        """Seconds to wait before the next state poll."""
        near_end = (
            self._state == PlaybackState.PLAYING
            and self._duration_ms > 0
            and self._duration_ms - self._furthest_position_ms <= NEAR_END_WINDOW_MS
        )
        return NEAR_END_POLL_INTERVAL_SECONDS if near_end else STATE_POLL_INTERVAL_SECONDS

    async def _poll_state_loop(self) -> None:
        """Poll device state periodically."""
        while self._is_connected:
            try:
                await self._wait_next_poll()
                if not self._is_connected:
                    break
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"State poll error: {e}")

    def _poll_suspended(self) -> bool:
        """Nothing of ours to watch, or a Qobuz command is changing the transport."""
        return (
            self._starting_playback
            or self._stopping_playback
            or not self._current_proxy_url
            # Paused by a Stop (the renderer refused Pause): it is meant to be stopped.
            or self._paused_by_stop_ms is not None
            # This playback already ended; wait for the player to load the next.
            or self._end_already_reported
        )

    async def _poll_once(self) -> None:
        # Spotify/AirPlay/other controllers own their transport and queue.
        # Their state changes must never trigger Qobuz auto-advance.
        if self._poll_suspended() or await self.check_external_playback():
            return

        new_state = await self._read_state()
        if new_state is None:
            logger.debug("[%s] Transport state unreadable; keeping %s", self.name, self._state)
            return

        if new_state == PlaybackState.PLAYING and await self._detect_transition():
            return

        # The proxy could not get any audio for the current track from Qobuz
        # (e.g. the CDN edge failed before the first byte).
        if self._skip_undeliverable():
            return

        if new_state != self._state and not self._handle_state_change(new_state):
            return

        if new_state == PlaybackState.PLAYING:
            await self._poll_position()

    def _handle_state_change(self, new_state: PlaybackState) -> bool:
        """Handle a transport state change; False to skip the rest of this poll."""
        logger.debug(f"State changed: {self._state} -> {new_state}")
        was_playing = self._state == PlaybackState.PLAYING or (
            self._state == PlaybackState.LOADING and self._loading_after_playing
        )
        if was_playing and new_state == PlaybackState.STOPPED:
            started_ago = time.monotonic() - self._playback_started_at
            if started_ago < PLAYBACK_START_GRACE_PERIOD_SECONDS:
                # Ignore STOPPED while the device is still loading. Checked
                # before touching the armed gapless track, which a short
                # track may still move on to.
                logger.debug(
                    f"Ignoring STOPPED state during grace period (started {started_ago:.1f}s ago)"
                )
                return False
            self._handle_stop()

        self._loading_after_playing = was_playing and new_state == PlaybackState.LOADING
        self._notify_state_change(new_state)
        return True

    async def _poll_position(self) -> None:
        furthest_before = self._furthest_position_ms
        pos = await self.get_position()
        self._note_playing(pos)

        if self._switched_early(furthest_before, pos):
            await self._recover_from_early_switch(furthest_before)
            return

        if self._ended_without_stop(pos):
            self._handle_end_without_stop(pos)
            return

        self._notify_position_update(pos)

    async def get_position(self) -> int:
        """Current position (the last known one if the renderer does not say)."""
        if self._client and await self._owns_transport():
            pos = await self._client.get_position_info()
            if pos is not None:
                self._position_ms = pos
                self._furthest_position_ms = max(self._furthest_position_ms, pos)
                logger.debug(f"DLNA position: {pos}ms")
            else:
                logger.debug("DLNA position: None returned")
        return self._position_ms
