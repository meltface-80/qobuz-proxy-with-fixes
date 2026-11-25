"""Tests for MetadataService: API lookups, caching, unavailable tracks."""

import time
from unittest.mock import AsyncMock

import pytest

from qobuz_proxy.playback.metadata import (
    MetadataService,
    TrackMetadata,
)


class MockAPIClient:
    """Mock API client for testing MetadataService."""

    def __init__(self) -> None:
        self.get_track_metadata = AsyncMock()
        self.get_track_url = AsyncMock()


@pytest.fixture
def mock_api() -> MockAPIClient:
    """Create a mock API client."""
    return MockAPIClient()


@pytest.fixture
def metadata_service(mock_api: MockAPIClient) -> MetadataService:
    """Create a MetadataService with mock API client."""
    return MetadataService(mock_api, max_quality=27)  # type: ignore[arg-type]


class TestMetadataService:
    """Tests for MetadataService class."""

    @pytest.mark.asyncio
    async def test_get_metadata_success(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test successful metadata retrieval."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "https://example.com/art.jpg",
        }

        result = await metadata_service.get_metadata("12345")

        assert result is not None
        assert result.track_id == "12345"
        assert result.title == "Test Track"
        assert result.artist == "Test Artist"
        assert result.album == "Test Album"
        assert result.duration_ms == 180000
        assert result.artwork_url == "https://example.com/art.jpg"

    @pytest.mark.asyncio
    async def test_get_metadata_not_found(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test metadata retrieval for nonexistent track."""
        mock_api.get_track_metadata.return_value = None

        result = await metadata_service.get_metadata("99999999")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_metadata_cache_hit(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test metadata is cached and reused."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "",
        }

        # First call
        result1 = await metadata_service.get_metadata("12345")
        # Second call
        result2 = await metadata_service.get_metadata("12345")

        assert result1 is not None
        assert result2 is not None
        assert result1 is result2  # Same object
        # API called only once
        assert mock_api.get_track_metadata.call_count == 1

    @pytest.mark.asyncio
    async def test_get_metadata_with_url(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test metadata retrieval with streaming URL."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "",
        }
        mock_api.get_track_url.return_value = {
            "url": "https://streaming.example.com/track.flac",
            "format_id": 27,
            "bit_depth": 24,
            "sampling_rate": 192000,
            "mime_type": "audio/flac",
        }

        result = await metadata_service.get_metadata("12345", fetch_url=True)

        assert result is not None
        assert result.streaming_url == "https://streaming.example.com/track.flac"
        assert result.actual_quality == 27

    @pytest.mark.asyncio
    async def test_get_streaming_url(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test streaming URL retrieval."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "",
        }
        mock_api.get_track_url.return_value = {
            "url": "https://streaming.example.com/track.flac",
            "format_id": 27,
            "bit_depth": 24,
            "sampling_rate": 192000,
            "mime_type": "audio/flac",
        }

        result = await metadata_service.get_streaming_url("12345")

        assert result == "https://streaming.example.com/track.flac"

    @pytest.mark.asyncio
    async def test_quality_fallback(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test quality fallback when preferred quality unavailable."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "",
        }
        # Return None for quality 27 and 7, dict for quality 6
        mock_api.get_track_url.side_effect = [
            None,  # 27 fails
            None,  # 7 fails
            {  # 6 succeeds
                "url": "https://streaming.example.com/track.flac",
                "format_id": 6,
                "bit_depth": 16,
                "sampling_rate": 44100,
                "mime_type": "audio/flac",
            },
        ]

        result = await metadata_service.get_metadata("12345", fetch_url=True)

        assert result is not None
        assert result.streaming_url == "https://streaming.example.com/track.flac"
        assert result.actual_quality == 6  # Fell back to CD quality

    @pytest.mark.asyncio
    async def test_refresh_streaming_url(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test forced URL refresh."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "",
        }
        mock_api.get_track_url.side_effect = [
            {
                "url": "https://streaming.example.com/first.flac",
                "format_id": 27,
                "bit_depth": 24,
                "sampling_rate": 192000,
                "mime_type": "audio/flac",
            },
            {
                "url": "https://streaming.example.com/second.flac",
                "format_id": 27,
                "bit_depth": 24,
                "sampling_rate": 192000,
                "mime_type": "audio/flac",
            },
        ]

        # Get initial URL
        url1 = await metadata_service.get_streaming_url("12345")
        # Force refresh
        url2 = await metadata_service.refresh_streaming_url("12345")

        assert url1 == "https://streaming.example.com/first.flac"
        assert url2 == "https://streaming.example.com/second.flac"

    @pytest.mark.asyncio
    async def test_failed_refresh_clears_expired_url(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """An expired URL must not be served as valid when its refresh fails."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "",
        }
        mock_api.get_track_url.return_value = {
            "url": "https://streaming.example.com/old.flac",
            "format_id": 27,
            "bit_depth": 24,
            "sampling_rate": 192.0,
            "mime_type": "audio/flac",
        }

        url1 = await metadata_service.get_streaming_url("12345")
        assert url1 == "https://streaming.example.com/old.flac"

        # Expire the cached URL and make every refresh attempt fail
        cached = metadata_service._cache.get("12345")
        assert cached is not None
        cached.streaming_url_expires_at = int(time.time()) - 1
        mock_api.get_track_url.return_value = None

        url2 = await metadata_service.get_streaming_url("12345")
        assert not url2  # the dead URL must not come back as "valid"

    @pytest.mark.asyncio
    async def test_preload_tracks(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test preloading multiple tracks."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "",
        }

        await metadata_service.preload_tracks(["1", "2", "3"])

        # All tracks should be fetched
        assert mock_api.get_track_metadata.call_count == 3

        # Subsequent gets should be cached
        mock_api.get_track_metadata.reset_mock()
        await metadata_service.get_metadata("1")
        await metadata_service.get_metadata("2")
        await metadata_service.get_metadata("3")

        # No new API calls
        assert mock_api.get_track_metadata.call_count == 0

    def test_get_quality_fallback_order(self, metadata_service: MetadataService) -> None:
        """Test quality fallback order generation."""
        # Default max_quality is 27
        assert metadata_service._get_quality_fallback_order() == [27, 7, 6, 5]

        # Test with lower max_quality
        service = MetadataService(MockAPIClient(), max_quality=7)  # type: ignore[arg-type]
        assert service._get_quality_fallback_order() == [7, 6, 5]

        service = MetadataService(MockAPIClient(), max_quality=6)  # type: ignore[arg-type]
        assert service._get_quality_fallback_order() == [6, 5]

        service = MetadataService(MockAPIClient(), max_quality=5)  # type: ignore[arg-type]
        assert service._get_quality_fallback_order() == [5]

    def test_get_quality_fallback_order_invalid(self, metadata_service: MetadataService) -> None:
        """Test quality fallback with invalid max_quality returns full list."""
        service = MetadataService(MockAPIClient(), max_quality=99)  # type: ignore[arg-type]
        assert service._get_quality_fallback_order() == [27, 7, 6, 5]

    @pytest.mark.asyncio
    async def test_streaming_url_stores_sample_rate_and_bit_depth(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test that sample_rate and bit_depth from the API are stored on TrackMetadata."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "",
        }
        mock_api.get_track_url.return_value = {
            "url": "https://streaming.example.com/track.flac",
            "format_id": 27,
            "bit_depth": 24,
            "sampling_rate": 96.0,  # kHz as returned by Qobuz API
            "mime_type": "audio/flac",
        }

        result = await metadata_service.get_metadata("12345", fetch_url=True)

        assert result is not None
        assert result.sample_rate == 96000
        assert result.bit_depth == 24

    @pytest.mark.asyncio
    async def test_streaming_url_stores_cd_quality_format(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test sample_rate and bit_depth for CD quality (16-bit/44.1kHz)."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "",
        }
        mock_api.get_track_url.return_value = {
            "url": "https://streaming.example.com/track.flac",
            "format_id": 6,
            "bit_depth": 16,
            "sampling_rate": 44.1,
            "mime_type": "audio/flac",
        }

        result = await metadata_service.get_metadata("12345", fetch_url=True)

        assert result is not None
        assert result.sample_rate == 44100
        assert result.bit_depth == 16

    def test_get_track_format_returns_values_after_url_fetch(
        self, metadata_service: MetadataService
    ) -> None:
        """Test get_track_format returns cached quality, sample_rate, and bit_depth."""
        from qobuz_proxy.playback.metadata import TrackMetadata

        cached = TrackMetadata(track_id="12345", actual_quality=7, sample_rate=96000, bit_depth=24)
        metadata_service._cache.set("12345", cached)

        quality, sample_rate, bit_depth = metadata_service.get_track_format("12345")

        assert quality == 7
        assert sample_rate == 96000
        assert bit_depth == 24

    def test_get_track_format_returns_zeros_for_unknown_track(
        self, metadata_service: MetadataService
    ) -> None:
        """Test get_track_format returns (0, 0, 0) for a track not in cache."""
        quality, sample_rate, bit_depth = metadata_service.get_track_format("unknown")

        assert quality == 0
        assert sample_rate == 0
        assert bit_depth == 0

    def test_log_now_playing(
        self, metadata_service: MetadataService, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test now playing log output."""
        import logging

        metadata = TrackMetadata(
            track_id="12345",
            title="Test Track",
            artist="Test Artist",
            album="Test Album",
            actual_quality=27,
        )

        with caplog.at_level(logging.INFO):
            metadata_service.log_now_playing(metadata)

        assert "Now playing: Test Artist - Test Track" in caplog.text
        assert "[Test Album]" in caplog.text
        assert "FLAC Hi-Res (24-bit/192kHz)" in caplog.text


class TestUnavailableTracks:
    """Tracks Qobuz cannot serve ("Not available" in the region) are remembered.

    The player retries the upcoming track on every gapless-arm poll, so without
    a negative cache one unavailable track hammers the API twice a second for
    the whole preceding track (GitHub #21 logs).
    """

    async def test_missing_metadata_is_not_refetched_within_retry_window(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        mock_api.get_track_metadata.return_value = None

        assert await metadata_service.get_metadata("1") is None
        assert await metadata_service.get_streaming_url("1") is None
        assert await metadata_service.get_metadata("1") is None

        assert mock_api.get_track_metadata.await_count == 1
        assert metadata_service.is_unavailable("1")

    async def test_unavailable_track_is_retried_after_window(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        mock_api.get_track_metadata.return_value = None
        await metadata_service.get_metadata("1")

        # Expire the mark
        metadata_service._unavailable_until["1"] = time.monotonic() - 1

        assert not metadata_service.is_unavailable("1")
        await metadata_service.get_metadata("1")
        assert mock_api.get_track_metadata.await_count == 2

    async def test_no_streaming_url_marks_unavailable(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        mock_api.get_track_metadata.return_value = {"title": "T", "duration_ms": 1000}
        mock_api.get_track_url.return_value = None

        assert await metadata_service.get_streaming_url("2") is None
        calls_after_first = mock_api.get_track_url.await_count
        assert calls_after_first > 0

        assert await metadata_service.get_streaming_url("2") is None
        assert mock_api.get_track_url.await_count == calls_after_first
        assert metadata_service.is_unavailable("2")
        # Metadata itself stays served from cache
        cached = await metadata_service.get_metadata("2")
        assert cached is not None and cached.title == "T"

    async def test_successful_url_clears_unavailable_mark(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        mock_api.get_track_metadata.return_value = {"title": "T", "duration_ms": 1000}
        mock_api.get_track_url.return_value = None
        await metadata_service.get_streaming_url("3")
        assert metadata_service.is_unavailable("3")

        metadata_service._unavailable_until["3"] = time.monotonic() - 1
        mock_api.get_track_url.return_value = {
            "url": "https://cdn/3.flac",
            "format_id": 6,
            "bit_depth": 16,
            "sampling_rate": 44.1,
        }

        assert await metadata_service.get_streaming_url("3") == "https://cdn/3.flac"
        assert not metadata_service.is_unavailable("3")
