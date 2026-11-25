"""Tests for queue management."""

from typing import Any

import pytest

from qobuz_proxy.playback.queue import (
    QobuzQueue,
    QueueTrack,
    QueueVersion,
    RepeatMode,
)


class TestQueueVersion:
    """Tests for QueueVersion class."""

    def test_str(self) -> None:
        """Test string representation."""
        version = QueueVersion(major=5, minor=3)
        assert str(version) == "5.3"

    def test_is_newer_than_major(self) -> None:
        """Test version comparison by major."""
        v1 = QueueVersion(major=2, minor=0)
        v2 = QueueVersion(major=1, minor=5)
        assert v1.is_newer_than(v2) is True
        assert v2.is_newer_than(v1) is False

    def test_is_newer_than_minor(self) -> None:
        """Test version comparison by minor."""
        v1 = QueueVersion(major=1, minor=5)
        v2 = QueueVersion(major=1, minor=3)
        assert v1.is_newer_than(v2) is True
        assert v2.is_newer_than(v1) is False

    def test_is_newer_than_equal(self) -> None:
        """Test version comparison when equal."""
        v1 = QueueVersion(major=1, minor=5)
        v2 = QueueVersion(major=1, minor=5)
        assert v1.is_newer_than(v2) is False
        assert v2.is_newer_than(v1) is False


class TestQueueTrack:
    """Tests for QueueTrack class."""

    def test_defaults(self) -> None:
        """Test default values."""
        track = QueueTrack(queue_item_id=1, track_id="12345")
        assert track.queue_item_id == 1
        assert track.track_id == "12345"
        assert track.context_uuid is None
        assert track.streaming_url is None
        assert track.metadata == {}
        assert track.start_ms == 0
        assert track.duration_ms == 0

    def test_url_staleness(self) -> None:
        """A cached URL is only trusted within its TTL."""
        track = QueueTrack(queue_item_id=1, track_id="12345")
        assert track.url_is_stale()  # no URL at all

        track.set_streaming_url("https://example.com/track.flac")
        assert not track.url_is_stale()

        track.url_fetched_at -= 241  # age the URL past the 240s TTL
        assert track.url_is_stale()

        track.set_streaming_url(None)
        assert track.url_is_stale()
        assert track.url_fetched_at == 0.0


class TestQobuzQueue:
    """Tests for QobuzQueue class."""

    # =========================================================================
    # Load Queue Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_load_queue(self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]) -> None:
        """Test loading a queue with tracks."""
        version = QueueVersion(major=1, minor=0)
        await queue.load_queue(sample_tracks, version)

        state = await queue.get_state()
        assert state.track_count == 5
        assert state.version.major == 1
        assert state.current_index == 0

    @pytest.mark.asyncio
    async def test_load_queue_with_current_item(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test loading queue with specific current item."""
        version = QueueVersion(major=1, minor=0)
        await queue.load_queue(sample_tracks, version, current_item_id=3)

        track = await queue.get_current_track()
        assert track is not None
        assert track.track_id == "C"
        assert track.queue_item_id == 3

    @pytest.mark.asyncio
    async def test_load_queue_clears_previous(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test that loading queue clears previous data."""
        version = QueueVersion(major=1, minor=0)
        await queue.load_queue(sample_tracks, version)

        # Load different queue
        new_tracks = [{"queueItemId": 10, "trackId": "X"}]
        await queue.load_queue(new_tracks, QueueVersion(major=2, minor=0))

        state = await queue.get_state()
        assert state.track_count == 1
        assert state.version.major == 2

    # =========================================================================
    # Navigation Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_advance_to_next(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test advancing to next track."""
        await queue.load_queue(sample_tracks, QueueVersion())

        # Start at A
        track = await queue.get_current_track()
        assert track is not None
        assert track.track_id == "A"

        # Advance to B
        track = await queue.advance_to_next()
        assert track is not None
        assert track.track_id == "B"

        # Advance to C
        track = await queue.advance_to_next()
        assert track is not None
        assert track.track_id == "C"

    @pytest.mark.asyncio
    async def test_go_to_previous(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test going to previous track."""
        await queue.load_queue(sample_tracks, QueueVersion(), current_item_id=3)

        # Start at C
        track = await queue.get_current_track()
        assert track is not None
        assert track.track_id == "C"

        # Go back to B
        track = await queue.go_to_previous()
        assert track is not None
        assert track.track_id == "B"

        # Go back to A
        track = await queue.go_to_previous()
        assert track is not None
        assert track.track_id == "A"

    @pytest.mark.asyncio
    async def test_advance_at_end_no_repeat(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test advancing at end with repeat off returns None."""
        await queue.load_queue(sample_tracks, QueueVersion(), current_item_id=5)

        track = await queue.advance_to_next()
        assert track is None

    @pytest.mark.asyncio
    async def test_previous_at_beginning(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test going previous at beginning stays at first track."""
        await queue.load_queue(sample_tracks, QueueVersion())

        track = await queue.go_to_previous()
        assert track is not None
        assert track.track_id == "A"

    # =========================================================================
    # Clear Queue Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_clear_queue(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test clearing the queue."""
        await queue.load_queue(sample_tracks, QueueVersion())

        await queue.clear()

        assert queue.is_empty is True
        track = await queue.get_current_track()
        assert track is None

    # =========================================================================
    # Set Current By Item ID Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_set_current_by_item_id(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test setting current track by queue item ID."""
        await queue.load_queue(sample_tracks, QueueVersion())

        result = await queue.set_current_by_item_id(4)
        assert result is True

        track = await queue.get_current_track()
        assert track is not None
        assert track.track_id == "D"

    @pytest.mark.asyncio
    async def test_set_current_by_item_id_not_found(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test setting current to non-existent item ID."""
        await queue.load_queue(sample_tracks, QueueVersion())

        result = await queue.set_current_by_item_id(999)
        assert result is False

    # =========================================================================
    # Version Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_version_tracking(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test queue version is stored and retrieved."""
        version = QueueVersion(major=5, minor=3)
        await queue.load_queue(sample_tracks, version)

        stored_version = await queue.get_version()
        assert stored_version.major == 5
        assert stored_version.minor == 3

    @pytest.mark.asyncio
    async def test_set_version(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test updating queue version."""
        await queue.load_queue(sample_tracks, QueueVersion(major=1, minor=0))

        await queue.set_version(QueueVersion(major=2, minor=5))

        version = await queue.get_version()
        assert version.major == 2
        assert version.minor == 5

    # =========================================================================
    # State Access Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_get_state(self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]) -> None:
        """Test getting queue state snapshot."""
        await queue.load_queue(sample_tracks, QueueVersion(major=3, minor=2), current_item_id=2)
        await queue.set_shuffle(enabled=True, pivot_item_id=2)
        await queue.set_repeat_mode(RepeatMode.ALL)

        state = await queue.get_state()

        assert state.version.major == 3
        assert state.version.minor == 2
        assert state.track_count == 5
        assert state.current_queue_item_id == 2
        assert state.shuffle_enabled is True
        assert state.repeat_mode == RepeatMode.ALL

    @pytest.mark.asyncio
    async def test_is_empty(self, queue: QobuzQueue) -> None:
        """Test is_empty property."""
        assert queue.is_empty is True

        await queue.load_queue([{"queueItemId": 1, "trackId": "A"}], QueueVersion())
        assert queue.is_empty is False

        await queue.clear()
        assert queue.is_empty is True

    # =========================================================================
    # Lifecycle Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_start_stop(self, queue: QobuzQueue) -> None:
        """Test starting and stopping preload task."""
        await queue.start()
        assert queue._is_running is True
        assert queue._preload_task is not None

        await queue.stop()
        assert queue._is_running is False
        assert queue._preload_task is None

    @pytest.mark.asyncio
    async def test_start_idempotent(self, queue: QobuzQueue) -> None:
        """Test starting twice doesn't create multiple tasks."""
        await queue.start()
        task1 = queue._preload_task

        await queue.start()
        task2 = queue._preload_task

        assert task1 is task2

        await queue.stop()

    # =========================================================================
    # Empty Queue Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_operations_on_empty_queue(self, queue: QobuzQueue) -> None:
        """Test operations on empty queue don't crash."""
        track = await queue.get_current_track()
        assert track is None

        track = await queue.advance_to_next()
        assert track is None

        track = await queue.go_to_previous()
        assert track is None

        await queue.set_shuffle(enabled=True)  # Should not crash
        await queue.set_repeat_mode(RepeatMode.ALL)  # Should not crash


class TestMissingQueueItemLogging:
    """Renderers never receive the queue, so a lookup miss on an empty queue is
    normal and must not be logged as a warning (it misled GitHub #22)."""

    async def test_empty_queue_miss_logs_at_debug(self, caplog: Any) -> None:
        import logging

        queue = QobuzQueue()
        with caplog.at_level(logging.DEBUG, logger="qobuz_proxy.playback.queue"):
            assert await queue.set_current_by_item_id(68) is False

        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert "Queue item 68 not found" in caplog.text

    async def test_loaded_queue_miss_still_warns(self, caplog: Any) -> None:
        import logging

        queue = QobuzQueue()
        await queue.load_queue(
            tracks=[{"queueItemId": 1, "trackId": 100}], version=QueueVersion(1, 0)
        )
        with caplog.at_level(logging.WARNING, logger="qobuz_proxy.playback.queue"):
            assert await queue.set_current_by_item_id(68) is False

        assert "Queue item 68 not found" in caplog.text
