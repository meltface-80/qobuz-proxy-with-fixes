"""Player lifecycle and background loops (playback monitor, heartbeat)."""

import asyncio
import logging
import time

from qobuz_proxy.backends import PlaybackState
from .player_advance import AutoAdvanceMixin


logger = logging.getLogger("qobuz_proxy.playback.player")

# While paused, the monitor watches for an external renderer stop. A DLNA
# get_state() collapses transient read failures (and unrecognized device state
# strings) to STOPPED, so a real stop must be confirmed by this many consecutive
# STOPPED polls (~0.5s each) before ending the listen — one bad poll must not
# prematurely stop a normal paused track or lose its resume position.
_PAUSED_STOP_CONFIRMATIONS = 3


class PlayerMonitorMixin(AutoAdvanceMixin):
    """Start/stop the player and run its background tasks."""

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """Start the player and its components."""
        if self._is_running:
            return

        self._is_running = True

        # Start queue preloading
        await self.queue.start()

        # Connect backend
        if not self.backend.is_connected():
            await self.backend.connect()

        # Start background tasks
        self._playback_monitor_task = asyncio.create_task(self._playback_monitor_loop())
        self._state_update_task = asyncio.create_task(self._state_update_loop())

        logger.info("Player started")

    async def stop(self) -> None:
        """Stop the player and clean up."""
        self._is_running = False
        self._clear_skip_pending()

        # Cancel background tasks
        for task in [self._playback_monitor_task, self._state_update_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Close any open play report (incl. a paused listen, which no longer
        # closes on pause) so a shutdown mid-listen still lands in history.
        await self._report_stopped()

        # Stop queue
        await self.queue.stop()

        # Disconnect backend
        await self.backend.disconnect()

        logger.info("Player stopped")

    # =========================================================================
    # Background Tasks
    # =========================================================================

    async def _playback_monitor_loop(self) -> None:
        """Monitor playback and handle backend state changes."""
        while self._is_running:
            try:
                await asyncio.sleep(0.5)

                if self._state == PlaybackState.PLAYING:
                    self._paused_stop_polls = 0
                    # Poll backend state
                    backend_state = await self.backend.get_state()
                    if self._state != PlaybackState.PLAYING:
                        continue

                    if backend_state == PlaybackState.STOPPED:
                        # Track finished naturally (handled by callback)
                        pass
                    elif backend_state == PlaybackState.PAUSED:
                        # External pause (e.g., DLNA device)
                        self._state = PlaybackState.PAUSED
                        await self._send_state_update()
                        # Stop the played-time clock so this pause is excluded
                        # from the reported duration, like an app-driven pause.
                        self._report_paused()

                    # Update position from backend
                    position = await self.backend.get_position()
                    if self._state != PlaybackState.PLAYING:
                        continue
                    self._set_position(position)

                    # Try to arm gapless if not already armed
                    if not self._gapless_armed:
                        await self._prepare_next_track_for_gapless()

                elif self._state == PlaybackState.PAUSED:
                    # Keep watching while paused: a pause leaves the play-report
                    # session open, so an external stop/timeout on the renderer
                    # must close it (otherwise a later play merges into it).
                    # Require consecutive STOPPED polls before trusting it —
                    # get_state() reports STOPPED on a transient read failure, and
                    # one bad poll must not end a normal paused listen.
                    if await self.backend.get_state() == PlaybackState.STOPPED:
                        self._paused_stop_polls += 1
                        if self._paused_stop_polls >= _PAUSED_STOP_CONFIRMATIONS:
                            self._paused_stop_polls = 0
                            self._state = PlaybackState.STOPPED
                            # Zero the position like _stop_playback_locked: a
                            # stale pause-point position makes "previous" try a
                            # restart-seek on a stopped renderer (a no-op)
                            # instead of navigating.
                            self._position_value_ms = 0
                            self._position_timestamp_ms = int(time.time() * 1000)
                            await self._send_state_update()
                            await self._report_stopped()
                    else:
                        self._paused_stop_polls = 0

                else:
                    self._paused_stop_polls = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Playback monitor error: {e}")
                await asyncio.sleep(1.0)

    async def _state_update_loop(self) -> None:
        """Periodic state updates (heartbeat)."""
        while self._is_running:
            try:
                await asyncio.sleep(5.0)  # 5 second heartbeat like C++

                # Skip if StateReporter is handling heartbeats
                if self._state_reporter:
                    continue

                if self._state == PlaybackState.PLAYING:
                    await self._send_state_update()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"State update loop error: {e}")
