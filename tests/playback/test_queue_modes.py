"""Tests for queue shuffle and repeat modes."""

from typing import Any

import pytest

from qobuz_proxy.playback.queue import QobuzQueue, QueueVersion, RepeatMode


class TestQueueShuffle:
    """Tests for shuffle mode."""

    @pytest.mark.asyncio
    async def test_enable_shuffle_preserves_current(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test enabling shuffle keeps current track at current position."""
        await queue.load_queue(sample_tracks, QueueVersion(), current_item_id=2)

        # Current is B
        track_before = await queue.get_current_track()
        assert track_before is not None
        assert track_before.track_id == "B"

        # Enable shuffle with B as pivot
        await queue.set_shuffle(enabled=True, pivot_item_id=2)

        # B should still be current
        track_after = await queue.get_current_track()
        assert track_after is not None
        assert track_after.track_id == "B"

    @pytest.mark.asyncio
    async def test_disable_shuffle_restores_order(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test disabling shuffle restores original order."""
        await queue.load_queue(sample_tracks, QueueVersion(), current_item_id=3)  # Start at C

        # Enable shuffle with C as pivot
        await queue.set_shuffle(enabled=True, pivot_item_id=3)

        # C should still be current
        track = await queue.get_current_track()
        assert track is not None
        assert track.track_id == "C"

        # Disable shuffle
        await queue.set_shuffle(enabled=False)

        # C should still be current but now at its original position
        track = await queue.get_current_track()
        assert track is not None
        assert track.track_id == "C"

        # Navigate forward from C
        tracks = [track.track_id]
        track = await queue.advance_to_next()
        while track:
            tracks.append(track.track_id)
            track = await queue.advance_to_next()

        # Should be C, D, E (remaining in original order)
        assert tracks == ["C", "D", "E"]

    @pytest.mark.asyncio
    async def test_shuffle_state_reported(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test shuffle state is reported correctly."""
        await queue.load_queue(sample_tracks, QueueVersion())

        state = await queue.get_state()
        assert state.shuffle_enabled is False

        await queue.set_shuffle(enabled=True)

        state = await queue.get_state()
        assert state.shuffle_enabled is True


class TestQueueRepeat:
    """Tests for repeat modes."""

    @pytest.mark.asyncio
    async def test_repeat_one_stays_on_track(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test repeat one mode stays on current track."""
        await queue.load_queue(sample_tracks, QueueVersion(), current_item_id=2)
        await queue.set_repeat_mode(RepeatMode.ONE)

        # Advance should return same track
        track = await queue.advance_to_next()
        assert track is not None
        assert track.track_id == "B"

        track = await queue.advance_to_next()
        assert track is not None
        assert track.track_id == "B"

    @pytest.mark.asyncio
    async def test_repeat_all_wraps_to_beginning(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test repeat all wraps from end to beginning."""
        await queue.load_queue(sample_tracks, QueueVersion(), current_item_id=5)
        await queue.set_repeat_mode(RepeatMode.ALL)

        # At E, advance should wrap to A
        track = await queue.advance_to_next()
        assert track is not None
        assert track.track_id == "A"

    @pytest.mark.asyncio
    async def test_repeat_all_previous_wraps_to_end(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test repeat all wraps from beginning to end on previous."""
        await queue.load_queue(sample_tracks, QueueVersion())  # Start at A
        await queue.set_repeat_mode(RepeatMode.ALL)

        # At A, previous should wrap to E
        track = await queue.go_to_previous()
        assert track is not None
        assert track.track_id == "E"

    @pytest.mark.asyncio
    async def test_repeat_off_stops_at_end(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test repeat off stops at end of queue."""
        await queue.load_queue(sample_tracks, QueueVersion(), current_item_id=5)
        await queue.set_repeat_mode(RepeatMode.OFF)

        # At E, advance should return None
        track = await queue.advance_to_next()
        assert track is None

    @pytest.mark.asyncio
    async def test_repeat_mode_state_reported(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test repeat mode is reported in state."""
        await queue.load_queue(sample_tracks, QueueVersion())

        state = await queue.get_state()
        assert state.repeat_mode == RepeatMode.OFF

        await queue.set_repeat_mode(RepeatMode.ALL)

        state = await queue.get_state()
        assert state.repeat_mode == RepeatMode.ALL
