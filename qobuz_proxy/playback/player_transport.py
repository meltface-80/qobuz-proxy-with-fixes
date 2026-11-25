"""Player transport commands: play, pause, stop, next and previous."""

import logging
import time

from qobuz_proxy.backends import PlaybackState
from .player_loading import TrackLoadingMixin


logger = logging.getLogger("qobuz_proxy.playback.player")

# Threshold for restart vs previous track (milliseconds)
PREVIOUS_TRACK_THRESHOLD_MS = 3000


class TransportMixin(TrackLoadingMixin):
    """Serialized play/pause/stop/next/previous commands."""

    # =========================================================================
    # Playback Commands
    # =========================================================================

    async def play(self, position_ms: int = 0) -> bool:
        """
        Start or resume playback.

        Args:
            position_ms: Optional starting position (only used when starting new playback)

        Returns:
            True if playback started/resumed successfully
        """
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("play superseded by newer command; skipping")
                return False
            return await self._play_locked(position_ms)

    async def _play_locked(self, position_ms: int = 0) -> bool:
        logger.debug(f"Play command, current state: {self._state}")

        # Resume from pause
        if self._state == PlaybackState.PAUSED:
            if not await self.backend.resume():
                # The renderer rejected the resume (e.g. SOAP failure) — stay
                # PAUSED rather than reporting PLAYING over a silent device, and
                # push the real PAUSED state so the app (which requested PLAY)
                # corrects immediately instead of waiting for the next heartbeat.
                logger.warning("Backend failed to resume; remaining paused")
                await self._send_state_update()
                return False
            self._state = PlaybackState.PLAYING
            self._position_timestamp_ms = int(time.time() * 1000)
            await self._send_state_update()
            # Resume continues an existing listen — pass the current position so
            # we don't re-report a start (and re-scrobble) on every pause/resume.
            await self._report_playing(self._position_value_ms)
            logger.info("Playback resumed")
            return True

        # Already playing — seek if position changed
        if self._state == PlaybackState.PLAYING:
            if position_ms > 0:
                logger.info(f"Scrubbing to {position_ms}ms while playing")
                await self.seek(position_ms)
            return True

        # Get track to play (if not already loaded)
        if not self._current_track:
            track = await self.queue.get_current_track()
            if not track:
                track = await self.queue.advance_to_next()
            if not track:
                logger.warning("No track to play - queue empty")
                return False
            self._current_track = track

        # Set starting position
        if position_ms > 0:
            self._position_value_ms = position_ms
            self._position_timestamp_ms = int(time.time() * 1000)

        # Start playback
        success = await self._start_playback(position_ms)

        # Seek if position > 0 and playback started
        if success and position_ms > 0:
            await self.backend.seek(position_ms)

        return success

    async def pause(self) -> bool:
        """
        Pause playback.

        Returns:
            True if paused successfully
        """
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("pause superseded by newer command; skipping")
                return False
            return await self._pause_locked()

    async def _pause_locked(self) -> bool:
        if self._state != PlaybackState.PLAYING:
            logger.debug(f"Cannot pause in state {self._state}")
            return False

        # Capture position before pausing
        self._position_value_ms = self.current_position_ms
        self._position_timestamp_ms = int(time.time() * 1000)

        await self.backend.pause()
        self._state = PlaybackState.PAUSED
        await self._send_state_update()
        # A pause does not end the listen — keeping the play-reporting session
        # open across pause/resume avoids emitting a streaming-end (and a
        # duplicate scrobble) on every pause. The session is closed on a real
        # stop, track change, or track end. note_paused stops the played-time
        # clock so paused time is excluded from the reported duration.
        self._report_paused()

        logger.info("Playback paused")
        return True

    async def stop_playback(self) -> None:
        """
        Stop playback completely.

        Resets position to 0 but keeps queue position.
        """
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("stop_playback superseded by newer command; skipping")
                return
            await self._stop_playback_locked()

    async def _stop_playback_locked(self) -> None:
        # Clear gapless state — explicit stop
        self._clear_gapless_state()
        self._clear_skip_pending()
        self._unavailable_skip_count = 0

        await self.backend.stop()

        self._state = PlaybackState.STOPPED
        self._position_value_ms = 0
        self._position_timestamp_ms = int(time.time() * 1000)

        await self._send_state_update()
        await self._report_stopped()
        logger.info("Playback stopped")

    async def next_track(self) -> bool:
        """
        Skip to next track.

        Returns:
            True if advanced to next track, False if at end
        """
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("next_track superseded by newer command; skipping")
                return False
            return await self._next_track_locked()

    async def _next_track_locked(self) -> bool:
        # Clear gapless state — explicit skip
        self._clear_gapless_state()
        self._clear_skip_pending()

        logger.debug("Next track command")

        # Get next track from queue
        track = await self.queue.advance_to_next()

        # Stop after resolving the target so local audio can retain its prefetch.
        if self._state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            await self.backend.stop(next_track_id=track.track_id if track else None)

        if not track:
            # End of queue
            self._state = PlaybackState.STOPPED
            self._current_track = None
            self._position_value_ms = 0
            await self._send_state_update()
            # Report the finished play so the last track lands in listening
            # history / is scrobbled, and the lingering session is closed (an
            # open session would inflate the next play's reported duration).
            await self._report_stopped()
            logger.info("End of queue - playback stopped")
            return False

        # Start playing next track
        self._current_track = track
        await self._start_playback()
        return True

    async def previous_track(self) -> bool:
        """
        Go to previous track or restart current track.

        - If position > 3 seconds: Restart current track
        - If position <= 3 seconds: Go to previous track

        Returns:
            True if action taken successfully
        """
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("previous_track superseded by newer command; skipping")
                return False
            return await self._previous_track_locked()

    async def _previous_track_locked(self) -> bool:
        # Clear gapless state — explicit navigation
        self._clear_gapless_state()
        self._clear_skip_pending()

        logger.debug("Previous track command")

        current_pos = self.current_position_ms

        # Restart if past threshold
        if current_pos > PREVIOUS_TRACK_THRESHOLD_MS:
            logger.debug(
                f"Restarting track (position {current_pos}ms > {PREVIOUS_TRACK_THRESHOLD_MS}ms)"
            )
            await self.backend.seek(0)
            self._position_value_ms = 0
            self._position_timestamp_ms = int(time.time() * 1000)
            await self._send_state_update()
            if self._state == PlaybackState.PAUSED:
                # Restarting a paused track ends the prior listen so the next
                # resume reports the replay as a fresh play instead of merging
                # into the open (paused) session.
                await self._report_stopped()
            return True

        # Stop current playback
        if self._state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            await self.backend.stop()

        # Get previous track from queue
        track = await self.queue.go_to_previous()

        if not track:
            logger.warning("No previous track")
            return False

        # Start playing previous track
        self._current_track = track
        await self._start_playback()
        return True
