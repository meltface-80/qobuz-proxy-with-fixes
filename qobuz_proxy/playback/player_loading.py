"""Player track loading: fetch URL/metadata and start a track on the backend."""

import logging
import time
from typing import Optional

from qobuz_proxy.backends import BackendTrackMetadata, PlaybackState
from .player_controls import PlayerControlsMixin
from .queue import QueueTrack


logger = logging.getLogger("qobuz_proxy.playback.player")


class TrackLoadingMixin(PlayerControlsMixin):
    """Load, play and reload specific tracks."""

    # =========================================================================
    # Track Loading
    # =========================================================================

    async def load_track(
        self,
        queue_item_id: int,
        track_id: str,
    ) -> bool:
        """
        Load a track without starting playback.

        This prepares the track (fetches URL and metadata) so it's ready
        to play immediately when play() is called.

        Args:
            queue_item_id: Queue item identifier
            track_id: Qobuz track ID

        Returns:
            True if track loaded successfully
        """
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("load_track superseded by newer command; skipping")
                return False
            return await self._load_track_locked(queue_item_id, track_id)

    async def _load_track_locked(
        self,
        queue_item_id: int,
        track_id: str,
        context_uuid: Optional[bytes] = None,
        *,
        for_playback: bool = False,
    ) -> bool:
        """Replace the current track and pre-fetch its URL and metadata.

        ``for_playback`` says the caller starts playback right after. The player
        then sits in LOADING rather than STOPPED for the load. LOADING is
        reported as PLAYING + BUFFERING, so a track change never reaches the
        app as "the renderer stopped" — which the app answered with a PAUSED
        SET_STATE, leaving a manual skip on a paused track (GitHub #22).
        """
        logger.info(f"Loading track: track_id={track_id}, queue_item_id={queue_item_id}")
        self._clear_skip_pending()
        self._clear_gapless_state()

        # Stop current playback if playing
        if self._state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            await self.backend.stop(next_track_id=track_id)
            # End the outgoing track's play report now that it's being replaced.
            # Pause no longer ends the session, so a load-only track change (no
            # immediate play) would otherwise leave the previous play unreported.
            await self._report_stopped()
        self._state = PlaybackState.LOADING if for_playback else PlaybackState.STOPPED

        # Create track object. The context UUID identifies the album/playlist the
        # track is played from and is required for Qobuz listening history /
        # Last.fm scrobbles, so it must be carried onto the QueueTrack.
        self._current_track = QueueTrack(
            queue_item_id=queue_item_id,
            track_id=track_id,
            context_uuid=context_uuid,
        )
        # A fresh track starts at 0 (callers starting elsewhere set the position
        # after loading) and its duration is unknown until metadata arrives.
        # Without this a report sent during the load shows the new item at the
        # old track's position and length.
        self._set_position(0)
        self._current_duration_ms = 0

        # Pre-fetch URL and metadata
        try:
            url = await self._get_track_url(track_id)
            if url:
                self._current_track.set_streaming_url(url)
            else:
                logger.error(f"Failed to get URL for track {track_id}")
                self._state = PlaybackState.STOPPED
                return False

            meta = await self._get_track_metadata(track_id)
            if meta:
                self._current_track.metadata = meta
                self._current_track.duration_ms = meta.get("duration_ms", 0)
                self._current_duration_ms = self._current_track.duration_ms

            logger.info(f"Track loaded: {track_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to load track {track_id}: {e}")
            self._state = PlaybackState.STOPPED
            return False

    async def play_track(
        self,
        queue_item_id: int,
        track_id: str,
        position_ms: int = 0,
        context_uuid: Optional[bytes] = None,
    ) -> bool:
        """
        Play a specific track from the queue.

        Args:
            queue_item_id: Queue item identifier
            track_id: Qobuz track ID
            position_ms: Starting position in milliseconds
            context_uuid: Album/playlist context bytes, used for play reporting.

        Returns:
            True if playback started successfully
        """
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("play_track superseded by newer command; skipping")
                return False
            return await self._play_track_locked(queue_item_id, track_id, position_ms, context_uuid)

    async def _play_track_locked(
        self,
        queue_item_id: int,
        track_id: str,
        position_ms: int = 0,
        context_uuid: Optional[bytes] = None,
    ) -> bool:
        # Clear gapless state — explicit track change
        self._clear_gapless_state()
        generation = self._command_generation

        logger.info(
            f"Play track requested: track_id={track_id}, queue_item_id={queue_item_id}, pos={position_ms}ms"
        )

        # Load the track first
        if not await self._load_track_locked(
            queue_item_id, track_id, context_uuid, for_playback=True
        ):
            return False

        if generation != self._command_generation:
            return False

        # Set starting position
        self._position_value_ms = position_ms
        self._position_timestamp_ms = int(time.time() * 1000)

        # Start playback
        success = await self._start_playback(position_ms)

        # Seek if position > 0 and playback started
        if success and position_ms > 0:
            await self.backend.seek(position_ms)

        return success

    async def reload_current_track(self) -> bool:
        """
        Reload the current track (e.g. after quality change).

        Saves position, stops, clears cached URL, and restarts at saved position.

        Returns:
            True if track was reloaded successfully
        """
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("reload_current_track superseded by newer command; skipping")
                return False
            return await self._reload_current_track_locked()

    async def _reload_current_track_locked(self) -> bool:
        if not self._current_track:
            return False

        if self._state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            # Not actively playing — just clear cached URL so next play uses new quality
            self._current_track.streaming_url = None
            return True

        was_playing = self._state == PlaybackState.PLAYING

        # Save current position
        saved_position = self.current_position_ms
        logger.info(
            f"Reloading track {self._current_track.track_id} at position {saved_position}ms"
        )

        # Stop current playback
        await self.backend.stop()

        # Clear cached streaming URL so it's re-fetched at new quality
        self._current_track.streaming_url = None

        if was_playing:
            # Restart playback from saved position
            success = await self._start_playback(saved_position)
            if success and saved_position > 0:
                await self.backend.seek(saved_position)
            return success
        else:
            # Was paused — just reset state, will re-fetch URL on next play
            self._state = PlaybackState.STOPPED
            self._position_value_ms = saved_position
            self._position_timestamp_ms = int(time.time() * 1000)
            # End the paused play's report: the next play re-fetches at the new
            # quality (new blob/format), so it must report as a fresh play rather
            # than resume this now-stale session.
            await self._report_stopped()
            return True

    # =========================================================================
    # Internal Playback Management
    # =========================================================================

    async def _start_playback(self, start_position_ms: int = 0) -> bool:
        """
        Start playback of current track.

        Args:
            start_position_ms: Position the track begins at. A large value means
                we're adopting an in-progress track from the app (handoff), which
                suppresses our play-start report.

        Returns:
            True if playback started successfully
        """
        if not self._current_track:
            return False

        track = self._current_track
        logger.info(f"Starting playback: track {track.track_id}")

        # Set loading state
        self._state = PlaybackState.LOADING
        await self._send_state_update()

        try:
            # Get streaming URL if not cached. A cached URL past its TTL is
            # treated as absent: a track loaded PAUSED and played later than
            # the URL lifetime must not start from an expired URL.
            url = None if track.url_is_stale() else track.streaming_url
            if not url:
                url = await self._get_track_url(track.track_id)
                if not url:
                    logger.error(f"Failed to get URL for track {track.track_id}")
                    self._state = PlaybackState.ERROR
                    await self._send_state_update()
                    return False
                track.set_streaming_url(url)

            # Get metadata if not cached
            meta: Optional[dict] = track.metadata if track.metadata else None
            if not meta:
                meta = await self._get_track_metadata(track.track_id)
                if meta:
                    track.metadata = meta
                    track.duration_ms = meta.get("duration_ms", 0)

            # Get actual quality and format info from cache (set during URL fetch)
            actual_quality, sample_rate, bit_depth = self.metadata.get_track_format(track.track_id)

            # Build backend metadata
            backend_meta = BackendTrackMetadata(
                track_id=track.track_id,
                title=(
                    meta.get("title", f"Track {track.track_id}")
                    if meta
                    else f"Track {track.track_id}"
                ),
                artist=meta.get("artist", "") if meta else "",
                album=meta.get("album", "") if meta else "",
                duration_ms=track.duration_ms,
                artwork_url=meta.get("artwork_url", "") if meta else "",
                sample_rate=sample_rate,
                bit_depth=bit_depth,
            )

            # Log now playing with actual quality (0 = cache miss, fall back to max quality)
            self.metadata.log_now_playing_info(backend_meta, actual_quality or None)

            # Report file quality if callback is set
            if self._file_quality_report_callback:
                logger.debug(f"Track {track.track_id} actual_quality={actual_quality}")
                if actual_quality:
                    await self._file_quality_report_callback(actual_quality)
                else:
                    logger.debug(
                        f"No actual_quality for track {track.track_id}, skipping file quality report"
                    )

            # Start playback on backend
            await self.backend.play(url, backend_meta)

            # Update state. Report the start position, not 0 — the caller
            # seeks the backend right after, and reporting 0 first makes the
            # app's progress bar snap to 0:00 until the next heartbeat.
            self._state = PlaybackState.PLAYING
            self._current_duration_ms = track.duration_ms
            self._unavailable_skip_count = 0
            self._position_value_ms = start_position_ms
            self._position_timestamp_ms = int(time.time() * 1000)

            await self._send_state_update()
            await self._report_playing(start_position_ms)
            return True

        except Exception as e:
            logger.error(f"Failed to start playback: {e}", exc_info=True)
            self._state = PlaybackState.ERROR
            await self._send_state_update()
            return False
