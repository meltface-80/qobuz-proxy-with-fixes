"""
Background preloading for the QobuzProxy queue.

Fetches metadata and streaming URLs for the next few tracks so playback can
start without waiting on the Qobuz API.
"""

import asyncio
import logging
from typing import Optional

from .queue_types import MetadataCallback, QueueTrack, UrlCallback

logger = logging.getLogger(__name__)


class QueuePreloadMixin:
    """
    Preload upcoming tracks for ``QobuzQueue``.

    The track list, position and lock it reads are owned by ``QobuzQueue``.
    """

    # Number of tracks to preload ahead
    PRELOAD_COUNT = 3

    _tracks: list[QueueTrack]
    _shuffled_indexes: list[int]
    _current_index: int
    _preload_task: Optional[asyncio.Task[None]]
    _preloaded_ids: set[int]
    _get_url_callback: Optional[UrlCallback]
    _get_metadata_callback: Optional[MetadataCallback]
    _lock: asyncio.Lock
    _is_running: bool

    # =========================================================================
    # Callback Registration
    # =========================================================================

    def set_url_callback(self, callback: UrlCallback) -> None:
        """Set callback for fetching streaming URLs."""
        self._get_url_callback = callback

    def set_metadata_callback(self, callback: MetadataCallback) -> None:
        """Set callback for fetching track metadata."""
        self._get_metadata_callback = callback

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """Start queue preloading background task."""
        if self._is_running:
            return

        self._is_running = True
        self._preload_task = asyncio.create_task(self._preload_loop())
        logger.info("Queue preloading started")

    async def stop(self) -> None:
        """Stop queue preloading."""
        self._is_running = False
        if self._preload_task:
            self._preload_task.cancel()
            try:
                await self._preload_task
            except asyncio.CancelledError:
                pass
            self._preload_task = None
        logger.info("Queue preloading stopped")

    # =========================================================================
    # Preloading
    # =========================================================================

    async def _preload_loop(self) -> None:
        """Background task to preload upcoming tracks."""
        while self._is_running:
            try:
                await self._preload_upcoming()
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Preload error: {e}", exc_info=True)
                await asyncio.sleep(5.0)

    async def _preload_upcoming(self) -> None:
        """Preload metadata and URLs for upcoming tracks."""
        tracks_to_preload: list[QueueTrack] = []

        async with self._lock:
            for i in range(self.PRELOAD_COUNT):
                idx = self._current_index + i
                if idx >= len(self._shuffled_indexes):
                    break

                track_index = self._shuffled_indexes[idx]
                track = self._tracks[track_index]

                # Skip if already preloaded — unless its cached URL went stale,
                # in which case it needs a fresh one before it can be played
                if track.queue_item_id in self._preloaded_ids and not track.url_is_stale():
                    continue

                tracks_to_preload.append(track)

        # Preload outside lock
        for track in tracks_to_preload:
            try:
                # Fetch metadata if missing
                if not track.metadata and self._get_metadata_callback:
                    metadata = await self._get_metadata_callback(track.track_id)
                    if metadata:
                        track.metadata = metadata
                        track.duration_ms = metadata.get("duration_ms", 0)
                        logger.debug(
                            f"Preloaded metadata for track {track.track_id}: "
                            f"{metadata.get('artist', '?')} - {metadata.get('title', '?')}"
                        )

                # Fetch URL if missing or stale
                if track.url_is_stale() and self._get_url_callback:
                    url = await self._get_url_callback(track.track_id)
                    if url:
                        track.set_streaming_url(url)
                        logger.debug(f"Preloaded URL for track {track.track_id}")

                # Mark as preloaded
                async with self._lock:
                    self._preloaded_ids.add(track.queue_item_id)

            except Exception as e:
                logger.warning(f"Failed to preload track {track.track_id}: {e}")

    def _invalidate_stale_preloads(self) -> None:
        """Remove preloaded tracks that are no longer upcoming (must hold lock)."""
        if not self._preloaded_ids:
            return

        # Get expected upcoming queue_item_ids
        expected_ids: set[int] = set()
        for i in range(self.PRELOAD_COUNT + 1):  # +1 to include current
            idx = self._current_index + i
            if idx >= len(self._shuffled_indexes):
                break
            track_index = self._shuffled_indexes[idx]
            expected_ids.add(self._tracks[track_index].queue_item_id)

        # Keep only expected
        stale = self._preloaded_ids - expected_ids
        if stale:
            self._preloaded_ids -= stale
            logger.debug(f"Invalidated {len(stale)} stale preloaded tracks")
