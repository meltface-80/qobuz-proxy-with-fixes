"""
Queue value types for QobuzProxy.

Tracks, versions, repeat modes and state snapshots shared by the queue,
the player and the protocol handlers.
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

# How long a cached streaming URL may be trusted. Qobuz signed URLs live for
# ~5 minutes; anything older than this must be re-fetched before use.
URL_CACHE_TTL_SECONDS = 240.0


class RepeatMode(Enum):
    """Queue repeat modes."""

    OFF = "off"  # Stop after last track
    ONE = "one"  # Repeat current track
    ALL = "all"  # Loop entire queue


@dataclass
class QueueTrack:
    """
    Represents a track in the queue.

    Attributes:
        queue_item_id: Unique ID for this queue entry (from server)
        track_id: Qobuz track ID (for API calls)
        context_uuid: Optional context (album, playlist) UUID
        streaming_url: Cached streaming URL (may expire)
        metadata: Cached track metadata dict
        start_ms: Start position for partial plays
        duration_ms: Track duration in milliseconds
    """

    queue_item_id: int
    track_id: str
    context_uuid: Optional[bytes] = None
    streaming_url: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    start_ms: int = 0
    duration_ms: int = 0
    url_fetched_at: float = 0.0  # time.time() when streaming_url was cached

    def set_streaming_url(self, url: Optional[str]) -> None:
        """Cache a streaming URL together with its fetch time."""
        self.streaming_url = url
        self.url_fetched_at = time.time() if url else 0.0

    def url_is_stale(self, ttl_s: float = URL_CACHE_TTL_SECONDS) -> bool:
        """Whether the cached URL must be treated as absent (missing or past TTL)."""
        if not self.streaming_url:
            return True
        return (time.time() - self.url_fetched_at) >= ttl_s


@dataclass
class QueueVersion:
    """
    Queue version for synchronization.

    The server tracks queue state with major/minor version numbers.
    Major increments on structural changes (add/remove/reorder).
    Minor increments on metadata updates.
    """

    major: int = 0
    minor: int = 0

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"

    def is_newer_than(self, other: "QueueVersion") -> bool:
        """Check if this version is newer than another."""
        if self.major != other.major:
            return self.major > other.major
        return self.minor > other.minor


@dataclass
class QueueState:
    """
    Snapshot of current queue state for reporting.

    Used when sending state updates to the Qobuz app.
    """

    version: QueueVersion
    track_count: int
    current_index: int
    current_queue_item_id: int
    shuffle_enabled: bool
    repeat_mode: RepeatMode


# Type alias for callbacks
UrlCallback = Callable[[str], Coroutine[Any, Any, Optional[str]]]
MetadataCallback = Callable[[str], Coroutine[Any, Any, Optional[dict[str, Any]]]]
