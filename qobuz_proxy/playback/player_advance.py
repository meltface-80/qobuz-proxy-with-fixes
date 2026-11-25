"""Player auto-advance on track end, gapless prefetch and backend events."""

import asyncio
import logging
import time
from typing import Optional, TYPE_CHECKING

from qobuz_proxy.backends import BackendTrackMetadata, PlaybackState
from .player_remote_state import RemoteStateMixin
from .queue import QueueTrack, RepeatMode


logger = logging.getLogger("qobuz_proxy.playback.player")


class AutoAdvanceMixin(RemoteStateMixin):
    """Backend event callbacks, track-end auto-advance and gapless arming."""

    if TYPE_CHECKING:
        # Implemented by QobuzPlayer (player.py) with unplayable-track skipping.
        async def _skip_past_unplayable(self, failed_info: dict, generation: int) -> None: ...

    # =========================================================================
    # Backend Callbacks and Track End
    # =========================================================================

    def _on_track_ended(self) -> None:
        """Callback when backend reports track ended naturally."""
        logger.debug("Track ended callback")
        # Snapshot the track that ended synchronously, before any queued user
        # command (stop/next/play) task can run. The automatic repeat restart
        # is only valid while this exact track is still the active one.
        asyncio.create_task(self._handle_track_ended(self._current_track))

    async def _handle_track_ended(self, ended_track: Optional[QueueTrack]) -> None:
        """Handle natural track end.

        ``ended_track`` is the track that was playing when the backend reported
        the end, used to detect a user command that superseded the restart.
        """
        generation = self._command_generation
        # Clear gapless state — prevents stale gapless callbacks from racing
        self._transition_generation += 1
        self._gapless_armed = False
        self._pending_next_track = None

        logger.info("Track ended naturally")

        # The track finished — report the completed play before advancing.
        await self._report_stopped()

        # Get queue state to check repeat mode
        queue_state = await self.queue.get_state()
        if generation != self._command_generation or ended_track is not self._current_track:
            return

        if queue_state.repeat_mode == RepeatMode.ONE and ended_track is not None:
            # Restart the current track from the beginning under repeat-one,
            # unless a user command superseded us while we were reporting.
            await self._restart_current_track(ended_track)
            return

        # Try to get next track from command handler (SET_STATE nextQueueItem)
        if self._get_next_track_callback:
            next_track_info = self._get_next_track_callback()
            if next_track_info:
                logger.info(f"Auto-advancing to next track: {next_track_info['trackId']}")
                # Clear the stored next track info since we're using it
                if self._clear_next_track_callback:
                    self._clear_next_track_callback()

                # Load and play the next track
                advance_generation = self._command_generation + 1
                started = await self.play_track(
                    queue_item_id=next_track_info["queueItemId"],
                    track_id=next_track_info["trackId"],
                    position_ms=0,
                    context_uuid=next_track_info.get("contextUuid"),
                )
                if not started:
                    # e.g. "Not available" in this region — skip past it
                    # instead of leaving the album stopped (GitHub #21).
                    await self._skip_past_unplayable(next_track_info, advance_generation)
                return

        # No next track available - stop playback
        logger.info("No next track available - playback stopped")
        self._state = PlaybackState.STOPPED
        self._current_track = None
        self._position_value_ms = 0
        await self._send_state_update()

    async def _restart_current_track(self, ended_track: QueueTrack) -> None:
        """Restart the current track from the beginning (repeat-one).

        ``ended_track`` is the track that ended. The restart only proceeds while
        that exact track is still active and the player is still PLAYING — a
        user stop (-> STOPPED), pause (-> PAUSED), or next/play (different
        ``_current_track``) that raced the natural end therefore wins, and we
        skip the replay rather than override their intent.
        """
        async with self._playback_lock:
            if self._current_track is not ended_track or self._state != PlaybackState.PLAYING:
                logger.debug("repeat-one restart superseded by user command; skipping")
                return
            await self._restart_current_track_locked()

    async def _restart_current_track_locked(self) -> None:
        """Restart the current track, assuming the playback lock is held.

        On natural end the backend has already transitioned to STOPPED, so a
        bare seek(0) leaves it silent — we must re-issue play. The cached URL
        is cleared so a fresh, non-expired streaming link is fetched for the
        repeat. ``_start_playback`` reports the new play; the completed one was
        already reported by the caller.
        """
        if not self._current_track:
            return
        self._current_track.streaming_url = None
        self._set_position(0)
        await self._start_playback()

    def _on_playback_interrupted(self, position_ms: int) -> None:
        """Callback when the device stopped before the track ended."""
        asyncio.create_task(self._handle_playback_interrupted(self._current_track, position_ms))

    async def _handle_playback_interrupted(
        self, track: Optional[QueueTrack], position_ms: int
    ) -> None:
        """Report a stop made outside Qobuz instead of advancing the queue.

        Another controller (or the device's own buttons) stopped the renderer.
        Auto-advancing here would take the renderer back from whoever stopped
        it. Report where it stopped so a later play from the app resumes there.
        """
        async with self._playback_lock:
            if self._current_track is not track or self._state != PlaybackState.PLAYING:
                return
            logger.info("Renderer stopped before the track ended; reporting playback stopped")
            self._clear_gapless_state()
            self._state = PlaybackState.STOPPED
            self._set_position(position_ms)
            await self._send_state_update()
            await self._report_stopped()

    def _on_playback_error(self, message: str) -> None:
        """Callback when backend reports playback error."""
        logger.error(f"Playback error: {message}")
        self._state = PlaybackState.ERROR
        asyncio.create_task(self._send_state_update())

    def _on_position_update(self, position_ms: int) -> None:
        """Callback when backend reports position update."""
        self._set_position(position_ms)

    # =========================================================================
    # Gapless Playback
    # =========================================================================

    async def _prepare_next_track_for_gapless(self) -> None:
        """Prepare the next track for gapless playback on the backend.

        Serialized via a lock: overlapping calls (monitor loop racing a
        re-arm) would each push the next track to the backend, and on Sonos
        that queues the track twice — making it play twice.
        """
        if not self.backend.supports_gapless or self._gapless_armed:
            return

        if not self._get_next_track_callback:
            return

        async with self._gapless_arm_lock:
            await self._prepare_next_track_locked()

    async def _prepare_next_track_locked(self) -> None:
        """Arm the next track. Caller must hold `_gapless_arm_lock`."""
        # Re-check after waiting on the lock — a concurrent arm may have won
        if self._gapless_armed or not self._get_next_track_callback:
            return
        # Only a playing track has a "next". Arming after a stop landed (e.g.
        # a deactivation racing a just-started play) would queue a track on an
        # idle renderer.
        if self._state != PlaybackState.PLAYING:
            return

        next_track_info = self._get_next_track_callback()
        if not next_track_info:
            return

        track_id = next_track_info["trackId"]
        queue_item_id = next_track_info["queueItemId"]
        my_generation = self._transition_generation

        try:
            # Fetch URL and metadata
            url = await self._get_track_url(track_id)
            if not url:
                logger.debug(f"Gapless: failed to get URL for next track {track_id}")
                return

            meta = await self._get_track_metadata(track_id)

            _, sample_rate, bit_depth = self.metadata.get_track_format(track_id)
            backend_meta = BackendTrackMetadata(
                track_id=track_id,
                title=meta.get("title", f"Track {track_id}") if meta else f"Track {track_id}",
                artist=meta.get("artist", "") if meta else "",
                album=meta.get("album", "") if meta else "",
                duration_ms=meta.get("duration_ms", 0) if meta else 0,
                artwork_url=meta.get("artwork_url", "") if meta else "",
                sample_rate=sample_rate,
                bit_depth=bit_depth,
            )

            success = await self.backend.set_next_track(url, backend_meta, queue_item_id)

            # State changed while arming (skip, stop, queue edit) — the arm
            # is stale; undo it on the backend instead of marking it armed
            if my_generation != self._transition_generation:
                logger.debug(f"Gapless: discarding stale arm for track {track_id}")
                if success:
                    await self.backend.clear_next_track()
                return

            if success:
                self._pending_next_track = {
                    "trackId": track_id,
                    "queueItemId": queue_item_id,
                    "contextUuid": next_track_info.get("contextUuid"),
                    "url": url,
                    "metadata": meta,
                    "backend_meta": backend_meta,
                }
                self._gapless_armed = True
                logger.info(f"Gapless: armed next track {track_id}")
            else:
                logger.debug(f"Gapless: backend rejected next track {track_id}")

        except Exception as e:
            logger.warning(f"Gapless: failed to prepare next track: {e}")

    def _on_next_track_started(self) -> None:
        """Callback when backend reports gapless transition to next track."""
        logger.debug("Gapless: next track started callback from backend")
        asyncio.create_task(self._handle_gapless_transition())

    async def _handle_gapless_transition(self) -> None:
        """Handle a gapless transition to the next track."""
        # Capture generation to detect concurrent state changes (e.g. explicit skip/stop)
        my_generation = self._transition_generation

        if not self._pending_next_track or not self._gapless_armed:
            logger.warning("Gapless: transition callback but no pending track")
            return

        # Check generation hasn't changed (guards against concurrent transitions)
        if my_generation != self._transition_generation:
            logger.debug("Gapless: stale transition callback, ignoring")
            return

        next_info = self._pending_next_track
        track_id = next_info["trackId"]
        queue_item_id = next_info["queueItemId"]
        meta = next_info.get("metadata")

        logger.info(f"Gapless: transitioning to track {track_id}")

        # Update current track (no stop/start cycle)
        self._current_track = QueueTrack(
            queue_item_id=queue_item_id,
            track_id=track_id,
            context_uuid=next_info.get("contextUuid"),
            streaming_url=next_info.get("url"),
            metadata=meta or {},
            duration_ms=meta.get("duration_ms", 0) if meta else 0,
        )
        self._current_duration_ms = self._current_track.duration_ms

        # Reset position
        self._position_value_ms = 0
        self._position_timestamp_ms = int(time.time() * 1000)

        # Clear gapless state
        self._pending_next_track = None
        self._gapless_armed = False

        # Clear next track info from command handler
        if self._clear_next_track_callback:
            self._clear_next_track_callback()

        # Report file quality
        actual_quality = self.metadata.get_track_actual_quality(track_id)
        backend_meta = next_info.get("backend_meta")
        if backend_meta:
            self.metadata.log_now_playing_info(backend_meta, actual_quality)
        if self._file_quality_report_callback and actual_quality:
            await self._file_quality_report_callback(actual_quality)

        # Report the play swap: ends the previous track, starts this one.
        await self._report_playing()

        # Send state update
        await self._send_state_update()

        # Try to arm the next next track
        await self._prepare_next_track_for_gapless()

    async def on_next_track_info_changed(self) -> None:
        """Called when command handler reports the next track info has changed."""
        new_info = self._get_next_track_callback() if self._get_next_track_callback else None

        # The server resends the same next track in bursts — if it's already
        # armed, re-arming would queue a duplicate on the backend
        pending = self._pending_next_track
        if (
            self._gapless_armed
            and new_info is not None
            and pending is not None
            and new_info["trackId"] == pending["trackId"]
            and new_info["queueItemId"] == pending["queueItemId"]
            and new_info.get("contextUuid") == pending.get("contextUuid")
        ):
            logger.debug("Gapless: next track unchanged, keeping current arming")
            return

        logger.debug("Gapless: next track info changed, re-arming")

        async with self._gapless_arm_lock:
            # Clear current gapless arming
            self._transition_generation += 1
            self._gapless_armed = False
            self._pending_next_track = None
            await self.backend.clear_next_track()

            # Re-arm with new track if playing
            if self._state == PlaybackState.PLAYING:
                await self._prepare_next_track_locked()
