"""
Gapless playback: prefetching the next track and completing the switch to it.

Only the next track's compressed bytes are downloaded ahead of time. The
feeding loop (in ``backend.py``) decodes them at the track boundary and keeps
feeding the same ring buffer, so the old track's tail plays straight into the
new track's first frames.
"""

import asyncio
import logging

from qobuz_proxy.backends.types import BackendTrackMetadata

from .backend_base import LocalBackendBase

logger = logging.getLogger(__name__)


class LocalGaplessMixin(LocalBackendBase):
    """Arming, clearing and completing the next-track prefetch."""

    @property
    def supports_gapless(self) -> bool:
        """Gapless is supported by prefetching the next track."""
        return True

    async def set_next_track(
        self, url: str, metadata: BackendTrackMetadata, queue_item_id: int = 0
    ) -> bool:
        """Prefetch the next track so the feeding loop can transition without a gap.

        Only the compressed bytes are downloaded up front; decoding happens at
        transition time (in a worker thread, with the buffered tail of the
        current track as cushion) to avoid holding two fully decoded tracks in
        memory for the whole duration of the current one.
        """
        await self.clear_next_track()
        self._next_track_meta = metadata
        self._next_prefetch_task = asyncio.create_task(self._download(url))
        logger.debug(f"Gapless: prefetching next track {metadata.track_id}")
        return True

    async def clear_next_track(self) -> None:
        """Cancel and discard the prefetched next track."""
        tasks = (self._next_prefetch_task, self._next_decode_task)
        self._next_prefetch_task = None
        self._next_decode_task = None
        self._next_track_meta = None
        for task in tasks:
            if task is None:
                continue
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"Discarding failed next-track prefetch: {e}")

    def _next_track_armed(self) -> bool:
        """Whether a next track is currently prefetching or prefetched."""
        return self._next_prefetch_task is not None

    def _maybe_complete_transition(self) -> None:
        """Fire the next-track-started callback once the old track's tail has
        fully left the ring buffer (everything still buffered is new audio)."""
        if not self._transition_pending:
            return
        available = self._ring_buffer.available() if self._ring_buffer else 0
        if self._frames_fed >= available:
            self._transition_pending = False
            self._forget_consumed_next_track()
            logger.info("Gapless: transitioned to next track")
            self._notify_next_track_started()

    def _forget_consumed_next_track(self) -> None:
        """Release retained decoding once the audible track boundary is crossed."""
        if self._next_prefetch_task is None:
            self._next_decode_task = None
            self._next_track_meta = None
