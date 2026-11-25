"""
Queue management for QobuzProxy.

Handles track ordering, shuffle, repeat, and preloading.
"""

import asyncio
import logging
import random
from typing import Any, Optional

from .queue_preload import QueuePreloadMixin
from .queue_types import (
    URL_CACHE_TTL_SECONDS,
    MetadataCallback,
    QueueState,
    QueueTrack,
    QueueVersion,
    RepeatMode,
    UrlCallback,
)

__all__ = [
    "URL_CACHE_TTL_SECONDS",
    "MetadataCallback",
    "QobuzQueue",
    "QueueState",
    "QueueTrack",
    "QueueVersion",
    "RepeatMode",
    "UrlCallback",
]

logger = logging.getLogger(__name__)


class QobuzQueue(QueuePreloadMixin):
    """
    Queue manager for QobuzProxy.

    Handles:
    - Track list management
    - Shuffle mode with pivot preservation
    - Repeat modes (off, one, all)
    - Preloading upcoming tracks
    - Version synchronization with server
    """

    def __init__(self) -> None:
        """Initialize empty queue."""
        # Track storage
        self._tracks: list[QueueTrack] = []
        self._original_order: list[int] = []  # Track indexes in original order
        self._shuffled_indexes: list[int] = []  # Mapping: position -> track index

        # Position tracking
        self._current_index: int = 0  # Position in shuffled/ordered list

        # Mode settings
        self._shuffle_enabled: bool = False
        self._repeat_mode: RepeatMode = RepeatMode.OFF

        # Version tracking
        self._version: QueueVersion = QueueVersion()

        # Preloading
        self._preload_task: Optional[asyncio.Task[None]] = None
        self._preloaded_ids: set[int] = set()  # queue_item_ids that are preloaded

        # Callbacks for fetching URLs and metadata
        self._get_url_callback: Optional[UrlCallback] = None
        self._get_metadata_callback: Optional[MetadataCallback] = None

        # Thread safety
        self._lock = asyncio.Lock()

        # Running state
        self._is_running = False

        logger.debug("QobuzQueue initialized")

    # =========================================================================
    # Queue Management
    # =========================================================================

    async def load_queue(
        self,
        tracks: list[dict[str, Any]],
        version: QueueVersion,
        current_item_id: Optional[int] = None,
    ) -> None:
        """
        Load a complete queue from the Qobuz app.

        Args:
            tracks: List of track dicts with queueItemId, trackId, etc.
            version: Queue version from server
            current_item_id: Queue item ID to set as current (optional)
        """
        async with self._lock:
            # Clear existing queue
            self._tracks.clear()
            self._original_order.clear()
            self._shuffled_indexes.clear()
            self._preloaded_ids.clear()
            self._current_index = 0

            # Load new tracks
            for i, track_data in enumerate(tracks):
                track = QueueTrack(
                    queue_item_id=track_data.get("queueItemId", i),
                    track_id=str(track_data.get("trackId", "")),
                    context_uuid=track_data.get("contextUuid"),
                    start_ms=track_data.get("startMs", 0),
                    duration_ms=track_data.get("durationMs", 0),
                )
                self._tracks.append(track)
                self._original_order.append(i)

            # Initialize index mapping (identity until shuffle enabled)
            self._shuffled_indexes = list(range(len(self._tracks)))

            # Update version
            self._version = version

            # Set current position
            if current_item_id is not None:
                self._set_index_by_item_id(current_item_id)

            logger.info(
                f"Loaded queue: {len(self._tracks)} tracks, "
                f"version {self._version}, "
                f"current index {self._current_index}"
            )

    async def clear(self) -> None:
        """Clear the entire queue."""
        async with self._lock:
            self._tracks.clear()
            self._original_order.clear()
            self._shuffled_indexes.clear()
            self._preloaded_ids.clear()
            self._current_index = 0
            self._version = QueueVersion()

        logger.info("Queue cleared")

    async def set_current_by_item_id(self, queue_item_id: int) -> bool:
        """Set current track by queue item ID."""
        async with self._lock:
            return self._set_index_by_item_id(queue_item_id)

    def _set_index_by_item_id(self, queue_item_id: int) -> bool:
        """Internal: Set index by item ID (must hold lock)."""
        # Find track index by queue_item_id
        track_index = None
        for i, track in enumerate(self._tracks):
            if track.queue_item_id == queue_item_id:
                track_index = i
                break

        if track_index is None:
            if not self._tracks:
                # Renderers never receive the queue itself — only the current
                # and next items via SET_STATE — so an empty queue is the
                # normal case, not a desync worth warning about.
                logger.debug(f"Queue item {queue_item_id} not found (queue not loaded)")
            else:
                logger.warning(f"Queue item {queue_item_id} not found")
            return False

        # Find position in shuffled order
        try:
            new_index = self._shuffled_indexes.index(track_index)
            old_index = self._current_index
            self._current_index = new_index

            # Invalidate preloaded tracks that are no longer upcoming
            self._invalidate_stale_preloads()

            logger.debug(f"Queue index: {old_index} -> {new_index}")
            return True
        except ValueError:
            logger.error(f"Track index {track_index} not in shuffled indexes")
            return False

    # =========================================================================
    # Shuffle Mode
    # =========================================================================

    async def set_shuffle(self, enabled: bool, pivot_item_id: Optional[int] = None) -> None:
        """
        Enable or disable shuffle mode.

        Args:
            enabled: Whether to enable shuffle
            pivot_item_id: Track to keep at current position (usually current track)
        """
        async with self._lock:
            self._shuffle_enabled = enabled

            if enabled:
                self._apply_shuffle(pivot_item_id)
            else:
                self._restore_original_order()

            # Invalidate preloads since order changed
            self._preloaded_ids.clear()

        logger.info(f"Shuffle mode: {enabled}")

    def _apply_shuffle(self, pivot_item_id: Optional[int]) -> None:
        """Apply shuffle to queue (must hold lock)."""
        if not self._tracks:
            return

        # Create shuffled index list
        indexes = list(range(len(self._tracks)))
        random.shuffle(indexes)

        # If pivot specified, move it to current position
        if pivot_item_id is not None:
            # Find track index for pivot
            pivot_track_index = None
            for i, track in enumerate(self._tracks):
                if track.queue_item_id == pivot_item_id:
                    pivot_track_index = i
                    break

            if pivot_track_index is not None:
                # Find where pivot ended up in shuffle
                pivot_position = indexes.index(pivot_track_index)
                # Swap with current position
                current_pos = min(self._current_index, len(indexes) - 1)
                indexes[current_pos], indexes[pivot_position] = (
                    indexes[pivot_position],
                    indexes[current_pos],
                )

        self._shuffled_indexes = indexes
        logger.debug(f"Shuffle applied, indexes: {self._shuffled_indexes[:10]}...")

    def _restore_original_order(self) -> None:
        """Restore original order when shuffle disabled (must hold lock)."""
        if not self._tracks:
            return

        # Get current track's original index
        current_track_index = self._get_current_track_index()

        # Restore sequential order
        self._shuffled_indexes = list(range(len(self._tracks)))

        # Update current_index to point to same track in new order
        if current_track_index is not None:
            try:
                self._current_index = self._shuffled_indexes.index(current_track_index)
            except ValueError:
                self._current_index = 0

    # =========================================================================
    # Repeat Mode
    # =========================================================================

    async def set_repeat_mode(self, mode: RepeatMode) -> None:
        """Set repeat mode."""
        async with self._lock:
            self._repeat_mode = mode
        logger.info(f"Repeat mode: {mode.value}")

    # =========================================================================
    # Navigation
    # =========================================================================

    async def get_current_track(self) -> Optional[QueueTrack]:
        """Get the current track."""
        async with self._lock:
            return self._get_current_track()

    def _get_current_track(self) -> Optional[QueueTrack]:
        """Internal: Get current track (must hold lock)."""
        track_index = self._get_current_track_index()
        if track_index is not None:
            return self._tracks[track_index]
        return None

    def _get_current_track_index(self) -> Optional[int]:
        """Get index into _tracks for current position (must hold lock)."""
        if not self._tracks or not self._shuffled_indexes:
            return None
        if self._current_index >= len(self._shuffled_indexes):
            return None
        return self._shuffled_indexes[self._current_index]

    async def advance_to_next(self) -> Optional[QueueTrack]:
        """
        Advance to next track respecting repeat mode.

        Returns:
            Next track or None if at end (and repeat off)
        """
        async with self._lock:
            if not self._tracks:
                return None

            # Repeat one: stay on current track
            if self._repeat_mode == RepeatMode.ONE:
                return self._get_current_track()

            # Try to advance
            next_index = self._current_index + 1

            if next_index >= len(self._shuffled_indexes):
                # At end of queue
                if self._repeat_mode == RepeatMode.ALL:
                    # Wrap to beginning
                    self._current_index = 0
                    logger.info("Queue wrapped to beginning (repeat all)")
                else:
                    # End of queue, no repeat
                    logger.info("End of queue reached")
                    return None
            else:
                self._current_index = next_index

            track = self._get_current_track()
            if track:
                logger.debug(f"Advanced to track {track.track_id} at index {self._current_index}")
            return track

    async def go_to_previous(self) -> Optional[QueueTrack]:
        """
        Go to previous track respecting repeat mode.

        Note: The >3 second restart logic is handled by the player, not the queue.
        """
        async with self._lock:
            if not self._tracks:
                return None

            # Repeat one: stay on current track
            if self._repeat_mode == RepeatMode.ONE:
                return self._get_current_track()

            # Try to go back
            if self._current_index > 0:
                self._current_index -= 1
            elif self._repeat_mode == RepeatMode.ALL:
                # Wrap to end
                self._current_index = len(self._shuffled_indexes) - 1
                logger.info("Queue wrapped to end (repeat all)")
            else:
                # At beginning, no repeat
                logger.info("At beginning of queue")
                return self._get_current_track()  # Return first track

            track = self._get_current_track()
            if track:
                logger.debug(f"Went back to track {track.track_id} at index {self._current_index}")
            return track

    # =========================================================================
    # State Access
    # =========================================================================

    async def get_state(self) -> QueueState:
        """Get current queue state snapshot."""
        async with self._lock:
            current_track = self._get_current_track()
            return QueueState(
                version=self._version,
                track_count=len(self._tracks),
                current_index=self._current_index,
                current_queue_item_id=current_track.queue_item_id if current_track else 0,
                shuffle_enabled=self._shuffle_enabled,
                repeat_mode=self._repeat_mode,
            )

    @property
    def is_empty(self) -> bool:
        """Check if queue is empty."""
        return len(self._tracks) == 0

    async def get_version(self) -> QueueVersion:
        """Get current queue version."""
        async with self._lock:
            return self._version

    async def set_version(self, version: QueueVersion) -> None:
        """Update queue version."""
        async with self._lock:
            self._version = version
