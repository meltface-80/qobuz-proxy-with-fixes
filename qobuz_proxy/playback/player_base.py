"""
Player base: shared state and low-level helpers.

``QobuzPlayer`` is assembled from a linear chain of mixins, each in its own
module and each inheriting the previous one, so every piece of player state
lives as ``self._...`` on one object::

    PlayerBase            (player_base.py)        state, reporting, position
    PlayerControlsMixin   (player_controls.py)    volume, seek, loop/shuffle
    TrackLoadingMixin     (player_loading.py)     load / start a track
    TransportMixin        (player_transport.py)   play/pause/stop/next/previous
    RemoteStateMixin      (player_remote_state.py) SET_STATE from the app
    AutoAdvanceMixin      (player_advance.py)     track end, gapless prefetch
    PlayerMonitorMixin    (player_monitor.py)     lifecycle, background loops
    QobuzPlayer           (player.py)             wiring, unplayable-track skip

All modules log through the ``qobuz_proxy.playback.player`` logger so log
output keeps the name it had before the split.
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional, TYPE_CHECKING

from qobuz_proxy.backends import (
    AudioBackend,
    PlaybackState,
    BufferStatus,
)
from .queue import QobuzQueue, QueueTrack
from .metadata import MetadataService

if TYPE_CHECKING:
    from .state_reporter import StateReporter
    from .play_reporter import PlayReporter

logger = logging.getLogger("qobuz_proxy.playback.player")

# When we begin a track at a position beyond this, treat it as adopted mid-play
# from the controlling app (a Connect handoff) rather than a play we initiated.
# The app already reported/scrobbled that play, so we suppress our own
# reportStreamingStart to avoid a duplicate Last.fm scrobble.
_HANDOFF_POSITION_THRESHOLD_MS = 5000


class PlayerBase:
    """Player state, state/play reporting, position tracking and accessors."""

    def __init__(
        self,
        queue: QobuzQueue,
        metadata_service: MetadataService,
        backend: AudioBackend,
        play_reporter: Optional["PlayReporter"] = None,
    ):
        """Initialize player state (callbacks are wired by ``QobuzPlayer``)."""
        self.queue = queue
        self.metadata = metadata_service
        self.backend = backend
        # Optional: reports plays to Qobuz (listening history / Last.fm scrobbling).
        self._play_reporter = play_reporter

        # Current track
        self._current_track: Optional[QueueTrack] = None
        self._current_duration_ms: int = 0

        # Position tracking (timestamp-based like C++ implementation)
        self._position_timestamp_ms: int = 0
        self._position_value_ms: int = 0

        # State
        self._state: PlaybackState = PlaybackState.STOPPED

        # Consecutive STOPPED polls seen while paused (external-stop detection).
        self._paused_stop_polls = 0

        # Playback command serialization. A track switch in the Qobuz app sends
        # a burst of SET_STATE messages; without this lock the resulting
        # load/play/stop calls overlap and fire concurrent SOAP control
        # requests, which wedges DLNA AVTransport renderers. The generation
        # counter lets a newer command supersede an older one still waiting on
        # the lock (latest-command-wins).
        self._playback_lock = asyncio.Lock()
        self._command_generation = 0
        self._playback_permission_check: Optional[Callable[[], Awaitable[bool]]] = None

        # State reporting - supports both callback and StateReporter
        self._state_update_callback: Optional[Callable[[], asyncio.Future]] = None
        self._state_reporter: Optional["StateReporter"] = None

        # Volume
        self._volume: int = 50  # Cached volume level (0-100)
        self._fixed_volume: bool = False  # From config
        self._volume_report_callback: Optional[Callable[[int], asyncio.Future]] = None

        # File quality report callback - called when track starts playing
        self._file_quality_report_callback: Optional[Callable[[int], asyncio.Future]] = None

        # Next track callback - used when track ends to get the next track from SET_STATE
        self._get_next_track_callback: Optional[Callable[[], Optional[dict]]] = None
        self._clear_next_track_callback: Optional[Callable[[], None]] = None

        # Gapless playback state
        self._pending_next_track: Optional[dict] = None
        self._gapless_armed: bool = False
        self._transition_generation: int = 0
        self._gapless_arm_lock: asyncio.Lock = asyncio.Lock()

        # Unplayable-track skip: the track we are waiting to skip past, and the
        # timeout that gives up if the server never names its successor.
        self._skip_pending_track: Optional[QueueTrack] = None
        self._request_next_track: Optional[Callable[[Callable[[], bool]], Awaitable[bool]]] = None
        self._unavailable_skip_count = 0
        self._skip_timeout_task: Optional[asyncio.Task] = None

        # Callback for next track info changes (from command handler)
        self._on_next_track_changed_callback: Optional[Callable[[], None]] = None

        # Background tasks
        self._playback_monitor_task: Optional[asyncio.Task] = None
        self._state_update_task: Optional[asyncio.Task] = None
        self._is_running: bool = False

    # =========================================================================
    # Callback Registration
    # =========================================================================

    def set_state_update_callback(self, callback: Callable[[], asyncio.Future]) -> None:
        """Set callback to send state updates to app (legacy method)."""
        self._state_update_callback = callback

    def set_state_reporter(self, reporter: "StateReporter") -> None:
        """
        Set the StateReporter for this player.

        When set, the StateReporter handles all state reporting including
        the periodic heartbeat and immediate updates.
        """
        self._state_reporter = reporter

    def set_volume_report_callback(self, callback: Callable[[int], asyncio.Future]) -> None:
        """Set callback to report volume changes to app."""
        self._volume_report_callback = callback

    def set_file_quality_report_callback(self, callback: Callable[[int], asyncio.Future]) -> None:
        """Set callback to report file quality when track starts playing."""
        self._file_quality_report_callback = callback

    def set_fixed_volume_mode(self, enabled: bool) -> None:
        """Enable or disable fixed volume mode."""
        self._fixed_volume = enabled
        logger.info(f"Fixed volume mode: {enabled}")

    def set_next_track_callbacks(
        self,
        get_callback: Callable[[], Optional[dict]],
        clear_callback: Callable[[], None],
    ) -> None:
        """
        Set callbacks for getting next track info from command handler.

        This is used for auto-advance when the current track ends.
        The get_callback should return track info dict with queueItemId and trackId,
        or None if no next track is available.
        """
        self._get_next_track_callback = get_callback
        self._clear_next_track_callback = clear_callback

    # =========================================================================
    # Command Generation and Pending-State Reset
    # =========================================================================

    def _next_generation(self) -> int:
        """Bump and return the playback command generation.

        Public command entrypoints call this before awaiting the playback
        lock. If a newer command bumps the generation while an older one is
        still queued on the lock, the older one detects the mismatch after
        acquiring and skips — so only the latest command in a burst runs.
        """
        self._command_generation += 1
        return self._command_generation

    def _clear_gapless_state(self) -> None:
        """Clear all gapless state and increment generation."""
        self._transition_generation += 1
        self._gapless_armed = False
        self._pending_next_track = None

    def _clear_skip_pending(self) -> None:
        """Forget a pending unplayable-track skip and cancel its timeout."""
        self._skip_pending_track = None
        task = self._skip_timeout_task
        self._skip_timeout_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    # =========================================================================
    # Play Reporting (Qobuz listening history / Last.fm scrobbling)
    # =========================================================================

    async def _report_playing(self, start_position_ms: int = 0) -> None:
        """Tell the play reporter the current track is now playing.

        A start beyond the handoff threshold means the app already owns (and
        scrobbled) this play, so we track it locally but suppress our start
        report to avoid a duplicate scrobble.
        """
        if not self._play_reporter or not self._current_track:
            return
        track = self._current_track
        format_id = self.metadata.get_track_actual_quality(track.track_id) or 0
        blob = self.metadata.get_track_blob(track.track_id) or ""
        report_start = start_position_ms < _HANDOFF_POSITION_THRESHOLD_MS
        await self._play_reporter.note_playing(
            track_id=track.track_id,
            format_id=format_id,
            blob=blob,
            context_uuid=self._format_context_uuid(track.context_uuid),
            report_start=report_start,
        )

    async def _report_stopped(self) -> None:
        """Tell the play reporter playback stopped (pause/stop/track end)."""
        if not self._play_reporter:
            return
        await self._play_reporter.note_stopped()

    def _report_paused(self) -> None:
        """Tell the play reporter playback paused (session stays open)."""
        if self._play_reporter:
            self._play_reporter.note_paused()

    @staticmethod
    def _format_context_uuid(context_uuid: Optional[bytes]) -> Optional[str]:
        """Format the 16-byte queue context UUID as a canonical UUID string."""
        if not context_uuid:
            return None
        try:
            import uuid

            return str(uuid.UUID(bytes=bytes(context_uuid)))
        except (ValueError, TypeError):
            return None

    # =========================================================================
    # Position Tracking
    # =========================================================================

    @property
    def current_position_ms(self) -> int:
        """Get current playback position."""
        if self._state != PlaybackState.PLAYING:
            return self._position_value_ms

        # Calculate elapsed time since last position update
        now_ms = int(time.time() * 1000)
        elapsed = now_ms - self._position_timestamp_ms
        return self._position_value_ms + elapsed

    def _set_position(self, position_ms: int) -> None:
        """Update position tracking."""
        self._position_value_ms = position_ms
        self._position_timestamp_ms = int(time.time() * 1000)
        logger.debug(f"Position set: {position_ms}ms at ts={self._position_timestamp_ms}")

    # =========================================================================
    # Queue Callbacks (track URL / metadata lookups)
    # =========================================================================

    async def _get_track_url(self, track_id: str) -> Optional[str]:
        """Callback for queue to get streaming URL."""
        return await self.metadata.get_streaming_url(track_id)

    async def _get_track_metadata(self, track_id: str) -> Optional[dict]:
        """Callback for queue to get track metadata."""
        meta = await self.metadata.get_metadata(track_id)
        if meta:
            return meta.to_dict()
        return None

    # =========================================================================
    # State Reporting
    # =========================================================================

    async def _send_state_update(self) -> None:
        """Send state update to app via StateReporter or callback."""
        # Prefer StateReporter if set
        if self._state_reporter:
            try:
                await self._state_reporter.report_now()
            except Exception as e:
                logger.error(f"Failed to send state update via reporter: {e}")
            return

        # Fall back to legacy callback
        if not self._state_update_callback:
            return

        try:
            await self._state_update_callback()
        except Exception as e:
            logger.error(f"Failed to send state update: {e}")

    # =========================================================================
    # State Access
    # =========================================================================

    @property
    def state(self) -> PlaybackState:
        """Get current playback state."""
        return self._state

    @property
    def current_track(self) -> Optional[QueueTrack]:
        """Get current track."""
        return self._current_track

    @property
    def duration_ms(self) -> int:
        """Get current track duration."""
        return self._current_duration_ms

    def get_state_dict(self) -> dict:
        """Get current state as dictionary for reporting."""
        track = self._current_track
        queue_item_id = track.queue_item_id if track else 0

        return {
            "playingState": int(self._state),
            "bufferState": int(BufferStatus.OK),
            "currentPosition": {
                "timestamp": self._position_timestamp_ms,
                "value": self._position_value_ms,
            },
            "duration": self._current_duration_ms,
            "currentQueueItemId": queue_item_id,
        }
