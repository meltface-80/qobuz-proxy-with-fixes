"""Player volume, seek and loop/shuffle/autoplay controls."""

import logging

from qobuz_proxy.backends import PlaybackState
from .player_base import PlayerBase
from .queue import RepeatMode


logger = logging.getLogger("qobuz_proxy.playback.player")


class PlayerControlsMixin(PlayerBase):
    """Volume, seek and play-mode commands."""

    # =========================================================================
    # Volume Controls
    # =========================================================================

    async def set_volume(self, level: int) -> int:
        """
        Set absolute volume level.

        Args:
            level: Volume level (0-100), will be clamped to valid range

        Returns:
            Actual volume level after clamping
        """
        # Clamp to valid range
        clamped = max(0, min(100, level))

        if self._fixed_volume:
            logger.debug(f"Fixed volume mode: ignoring set_volume({level})")
            return self._volume  # Return current (ignored)

        # Apply to backend
        await self.backend.set_volume(clamped)
        self._volume = clamped

        # Report change to app
        await self._report_volume_change()

        logger.info(f"Volume set to {clamped}")
        return clamped

    async def set_volume_delta(self, delta: int) -> int:
        """
        Adjust volume by relative amount.

        Args:
            delta: Amount to adjust (+/- value)

        Returns:
            New volume level after adjustment
        """
        current = await self.get_volume()
        new_level = current + delta
        return await self.set_volume(new_level)

    async def get_volume(self) -> int:
        """
        Get current volume level.

        Returns:
            Volume level (0-100)
        """
        if self._fixed_volume:
            return 100  # Fixed volume always reports 100

        # Get from backend (authoritative source)
        self._volume = await self.backend.get_volume()
        return self._volume

    async def _report_volume_change(self) -> None:
        """Send volume change notification to app."""
        if not self._volume_report_callback:
            return

        try:
            await self._volume_report_callback(self._volume)
        except Exception as e:
            logger.error(f"Failed to report volume change: {e}")

    async def broadcast_current_volume(self) -> None:
        """Refresh volume from the backend and re-emit it to the controller.

        Used when a controller (re)attaches — e.g. on `SrvrRndrSetActive(active=true)`
        — because the Qobuz cloud does not seem to replay our last
        `RndrSrvrVolumeChanged` to a freshly-subscribed controller, leaving the
        device picker without a volume bar until we send it again.
        """
        try:
            volume = await self.get_volume()
            await self._report_volume_change()
            logger.info(f"Re-broadcast current volume to app: {volume}%")
        except Exception as e:
            logger.warning(f"Failed to re-broadcast volume: {e}")

    # =========================================================================
    # Seek Control
    # =========================================================================

    async def seek(self, position_ms: int) -> bool:
        """
        Seek to position in current track.

        Args:
            position_ms: Target position in milliseconds

        Returns:
            True if seek successful, False if rejected (no track loaded)
        """
        # Reject if no track loaded
        if not self._current_track:
            logger.warning("Cannot seek: no track loaded")
            return False
        if self._state in (PlaybackState.STOPPED, PlaybackState.LOADING):
            # Nothing is playing on the backend yet; the caller applies the
            # start position when it issues play.
            logger.debug(f"Seek to {position_ms}ms ignored: track not started ({self._state.name})")
            return False

        # Get track duration
        duration = self._current_duration_ms
        if duration <= 0:
            logger.warning("Cannot seek: unknown track duration")
            return False

        # Clamp position to valid range
        # Leave 1 second buffer at end to avoid triggering track end
        max_position = max(0, duration - 1000)
        clamped_position = max(0, min(position_ms, max_position))

        if clamped_position != position_ms:
            logger.debug(f"Seek position clamped: {position_ms}ms -> {clamped_position}ms")

        logger.info(f"Seeking to {clamped_position}ms (duration: {duration}ms)")

        try:
            # Send seek to backend
            await self.backend.seek(clamped_position)

            # Update position tracking
            self._set_position(clamped_position)

            # Send state update (immediate, not waiting for heartbeat)
            await self._send_state_update()

            logger.info(f"Seek complete to {clamped_position}ms")
            return True

        except Exception as e:
            logger.error(f"Seek failed: {e}", exc_info=True)
            return False

    async def seek_seconds(self, position_seconds: float) -> bool:
        """
        Seek to position in seconds (convenience method).

        Args:
            position_seconds: Target position in seconds

        Returns:
            True if seek successful
        """
        position_ms = int(position_seconds * 1000)
        return await self.seek(position_ms)

    # =========================================================================
    # Play Modes
    # =========================================================================

    async def set_loop_mode(self, mode: int) -> None:
        """
        Set loop/repeat mode.

        Args:
            mode: Protocol LoopMode - 0=UNKNOWN, 1=OFF, 2=REPEAT_ONE, 3=REPEAT_ALL
        """
        logger.debug(f"Set loop mode: {mode}")
        # Map protocol LoopMode to internal RepeatMode
        # Protocol: 0=UNKNOWN, 1=OFF, 2=REPEAT_ONE, 3=REPEAT_ALL
        # Internal: OFF, ONE, ALL
        mode_map = {
            0: RepeatMode.OFF,  # UNKNOWN -> OFF
            1: RepeatMode.OFF,  # OFF
            2: RepeatMode.ONE,  # REPEAT_ONE
            3: RepeatMode.ALL,  # REPEAT_ALL
        }
        repeat_mode = mode_map.get(mode, RepeatMode.OFF)
        await self.queue.set_repeat_mode(repeat_mode)

    async def set_shuffle_mode(self, enabled: bool) -> None:
        """
        Set shuffle mode.

        Args:
            enabled: True to enable shuffle
        """
        logger.debug(f"Set shuffle mode: {enabled}")
        await self.queue.set_shuffle(enabled)

    async def set_autoplay_mode(self, enabled: bool) -> None:
        """
        Set autoplay mode.

        Args:
            enabled: True to enable autoplay (similar content when queue ends)
        """
        logger.debug(f"Set autoplay mode: {enabled}")
        # Autoplay is handled at queue level - just log for now
        # Full implementation would require fetching similar tracks
