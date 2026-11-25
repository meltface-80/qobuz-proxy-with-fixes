"""
QobuzProxy Player.

Core playback controller that orchestrates queue, metadata, and audio backend.

``QobuzPlayer`` is the last class of a mixin chain split across the
``player_*.py`` modules; see ``player_base`` for the layout.
"""

import asyncio
import logging
from typing import Optional, TYPE_CHECKING

from qobuz_proxy.backends import AudioBackend, PlaybackState
from .queue import QobuzQueue, QueueTrack
from .metadata import MetadataService
from .player_base import _HANDOFF_POSITION_THRESHOLD_MS
from .player_monitor import _PAUSED_STOP_CONFIRMATIONS, PlayerMonitorMixin
from .player_remote_state import _STALE_SNAPSHOT_THRESHOLD_MS
from .player_transport import PREVIOUS_TRACK_THRESHOLD_MS

if TYPE_CHECKING:
    from .play_reporter import PlayReporter

__all__ = [
    "PREVIOUS_TRACK_THRESHOLD_MS",
    "QobuzPlayer",
    "_HANDOFF_POSITION_THRESHOLD_MS",
    "_MAX_UNAVAILABLE_SKIPS",
    "_PAUSED_STOP_CONFIRMATIONS",
    "_STALE_SNAPSHOT_THRESHOLD_MS",
    "_UNAVAILABLE_SKIP_TIMEOUT_S",
]

logger = logging.getLogger(__name__)

# When the track we advance to cannot be played (Qobuz reports it "Not
# available" in the user's region), ask the server to advance the queue.
# If it never acknowledges the NEXT action, stop rather than wait forever.
# These stay in this module, next to the code that reads them, so they can be
# monkeypatched on ``qobuz_proxy.playback.player``.
_UNAVAILABLE_SKIP_TIMEOUT_S = 10.0
_MAX_UNAVAILABLE_SKIPS = 20


class QobuzPlayer(PlayerMonitorMixin):
    """
    Main playback controller.

    Coordinates:
    - Queue: Track ordering, shuffle, repeat
    - MetadataService: Track info and streaming URLs
    - AudioBackend: Actual audio playback
    - WsManager: State reporting to app

    State machine:
        STOPPED -> LOADING (on play)
        LOADING -> PLAYING (when ready)
        LOADING -> ERROR (on failure)
        PLAYING -> PAUSED (on pause)
        PAUSED -> PLAYING (on play/resume)
        PLAYING -> STOPPED (on stop or track end)
        PAUSED -> STOPPED (on stop)
    """

    def __init__(
        self,
        queue: QobuzQueue,
        metadata_service: MetadataService,
        backend: AudioBackend,
        play_reporter: Optional["PlayReporter"] = None,
    ):
        """Initialize player."""
        super().__init__(queue, metadata_service, backend, play_reporter)

        # Wire up queue callbacks to metadata service
        self.queue.set_url_callback(self._get_track_url)
        self.queue.set_metadata_callback(self._get_track_metadata)

        # Wire up backend callbacks
        self.backend.on_track_ended(self._on_track_ended)
        self.backend.on_playback_error(self._on_playback_error)
        self.backend.on_position_update(self._on_position_update)
        self.backend.on_next_track_started(self._on_next_track_started)
        self.backend.on_playback_interrupted(self._on_playback_interrupted)

        logger.info("QobuzPlayer initialized")

    # =========================================================================
    # Unplayable Track Skipping
    # =========================================================================
    #
    # Qobuz albums can contain tracks that are "Not available" in the user's
    # region: track/get answers 404 and no streaming URL exists. The official
    # app skips them; so must we, or an album stops dead at the first one.
    #
    # A renderer only learns the current and the next queue item (via
    # SET_STATE), so once the next item turns out to be unplayable we don't know
    # what follows it. Reporting a new current item does not guarantee a
    # SET_STATE response (#21). Echo the current queue version with the failed
    # item, then explicitly request NEXT and wait for the server's selected
    # current item. This preserves shuffle/repeat/queue-context ordering.

    async def _begin_skip_wait_locked(self) -> None:
        """Report the unplayable current track and wait for the server to name its successor."""
        track = self._current_track
        if track is None:
            return
        generation = self._command_generation
        if self._request_next_track:
            self._unavailable_skip_count += 1
            if self._unavailable_skip_count > _MAX_UNAVAILABLE_SKIPS:
                logger.warning("Too many consecutive unavailable tracks; stopping playback")
                await self._stop_playback_locked()
                return
        self._clear_skip_pending()
        self._skip_pending_track = track
        self._state = PlaybackState.LOADING
        self._current_duration_ms = 0
        self._set_position(0)
        logger.info(
            f"Track {track.track_id} cannot be played (not available); "
            "waiting for the next queue item to skip to"
        )
        self._skip_timeout_task = asyncio.create_task(self._skip_wait_timeout(track))
        await self._send_state_update()
        if self._request_next_track:
            sent = await self._request_next_track(
                lambda: self._skip_pending_track is track and generation == self._command_generation
            )
            logger.info("Requested NEXT for unavailable track %s: sent=%s", track.track_id, sent)

    async def _skip_wait_timeout(self, pending: QueueTrack) -> None:
        await asyncio.sleep(_UNAVAILABLE_SKIP_TIMEOUT_S)
        async with self._playback_lock:
            if self._skip_pending_track is not pending:
                return
            logger.warning(
                f"No next track received after unplayable track {pending.track_id}; "
                "stopping playback"
            )
            self._clear_skip_pending()
            self._state = PlaybackState.STOPPED
            self._current_track = None
            self._set_position(0)
            await self._send_state_update()

    async def _skip_past_unplayable_locked(self) -> bool:
        """Skip from the unplayable current track to the server-provided next item.

        Returns True if a playable track was started. If the next item is
        unplayable too, the wait starts over for the item after it; if there is
        no next item, playback stops at the end of the queue.
        """
        self._clear_skip_pending()
        next_info = self._get_next_track_callback() if self._get_next_track_callback else None
        if not next_info:
            logger.info("No playable track follows the unavailable one; playback stopped")
            self._state = PlaybackState.STOPPED
            self._current_track = None
            self._set_position(0)
            await self._send_state_update()
            return False

        # Consume the next item like auto-advance does, or the gapless arm would
        # queue the track we are about to play as its own successor.
        if self._clear_next_track_callback:
            self._clear_next_track_callback()

        logger.info(f"Skipping unavailable track; playing next item {next_info['trackId']}")
        if await self._play_track_locked(
            next_info["queueItemId"],
            next_info["trackId"],
            0,
            next_info.get("contextUuid"),
        ):
            return True
        failed = self._current_track
        if failed is not None and failed.streaming_url is None:
            await self._begin_skip_wait_locked()
        return False

    async def _skip_past_unplayable(self, failed_info: dict, generation: int) -> None:
        """Start the skip flow after an auto-advance into an unplayable track."""
        async with self._playback_lock:
            cur = self._current_track
            if (
                generation != self._command_generation
                or cur is None
                or cur.track_id != failed_info["trackId"]
                or cur.queue_item_id != failed_info["queueItemId"]
                or cur.streaming_url is not None
            ):
                # A user command took over meanwhile, or the failure was in the
                # backend rather than the track being unavailable.
                return
            await self._begin_skip_wait_locked()
