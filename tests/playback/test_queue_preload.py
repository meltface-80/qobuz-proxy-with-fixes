"""Tests for queue preloading of upcoming tracks."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from qobuz_proxy.playback.queue import QobuzQueue, QueueVersion


class TestQueuePreload:
    """Tests for background preloading."""

    @pytest.mark.asyncio
    async def test_preload_fetches_metadata(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test preloading fetches metadata for upcoming tracks."""
        # Setup mock callbacks
        metadata_callback = AsyncMock(
            return_value={
                "title": "Test Track",
                "artist": "Test Artist",
                "duration_ms": 180000,
            }
        )
        url_callback = AsyncMock(return_value="https://streaming.example.com/track.flac")

        queue.set_metadata_callback(metadata_callback)
        queue.set_url_callback(url_callback)

        await queue.load_queue(sample_tracks, QueueVersion())
        await queue.start()

        # Wait for preload
        await asyncio.sleep(1.5)

        await queue.stop()

        # Check that callbacks were called for first few tracks
        assert metadata_callback.call_count >= 1
        assert url_callback.call_count >= 1

    @pytest.mark.asyncio
    async def test_preload_refreshes_stale_urls(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """A preloaded track whose URL aged past the TTL gets a fresh one."""
        url_callback = AsyncMock(return_value="https://example.com/track.flac")
        queue.set_url_callback(url_callback)

        await queue.load_queue(sample_tracks, QueueVersion())
        await queue.start()
        await asyncio.sleep(1.5)

        initial_url_calls = url_callback.call_count
        assert initial_url_calls >= 1

        # Age all cached URLs past the TTL
        for track in queue._tracks:
            if track.streaming_url:
                track.url_fetched_at -= 300

        await asyncio.sleep(1.5)
        await queue.stop()

        assert url_callback.call_count > initial_url_calls

    @pytest.mark.asyncio
    async def test_preload_skips_already_preloaded(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test preloading doesn't re-fetch already preloaded tracks."""
        metadata_callback = AsyncMock(return_value={"title": "Test"})
        url_callback = AsyncMock(return_value="https://example.com/track.flac")

        queue.set_metadata_callback(metadata_callback)
        queue.set_url_callback(url_callback)

        await queue.load_queue(sample_tracks, QueueVersion())
        await queue.start()

        # Wait for initial preload
        await asyncio.sleep(1.5)

        initial_metadata_calls = metadata_callback.call_count
        initial_url_calls = url_callback.call_count

        # Wait for another preload cycle
        await asyncio.sleep(1.5)

        await queue.stop()

        # Should not have called again for same tracks
        assert metadata_callback.call_count == initial_metadata_calls
        assert url_callback.call_count == initial_url_calls
