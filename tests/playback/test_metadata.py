"""Tests for track metadata retrieval and caching."""

import time


from qobuz_proxy.playback.metadata import (
    AudioQuality,
    MetadataCache,
    TrackMetadata,
)


class TestAudioQuality:
    """Tests for AudioQuality class."""

    def test_quality_constants(self) -> None:
        """Test quality format constants."""
        assert AudioQuality.MP3_320 == 5
        assert AudioQuality.FLAC_CD == 6
        assert AudioQuality.FLAC_HIRES_96 == 7
        assert AudioQuality.FLAC_HIRES_192 == 27

    def test_get_name_known_quality(self) -> None:
        """Test getting name for known quality IDs."""
        assert AudioQuality.get_name(5) == "MP3 320kbps"
        assert AudioQuality.get_name(6) == "FLAC CD (16-bit/44.1kHz)"
        assert AudioQuality.get_name(7) == "FLAC Hi-Res (24-bit/96kHz)"
        assert AudioQuality.get_name(27) == "FLAC Hi-Res (24-bit/192kHz)"

    def test_get_name_unknown_quality(self) -> None:
        """Test getting name for unknown quality ID."""
        assert AudioQuality.get_name(99) == "Unknown (99)"


class TestTrackMetadata:
    """Tests for TrackMetadata class."""

    def test_default_values(self) -> None:
        """Test default field values."""
        metadata = TrackMetadata()
        assert metadata.track_id == ""
        assert metadata.title == ""
        assert metadata.artist == ""
        assert metadata.album == ""
        assert metadata.duration_ms == 0
        assert metadata.artwork_url == ""
        assert metadata.streaming_url == ""
        assert metadata.streaming_url_expires_at == 0
        assert metadata.actual_quality == 0
        assert metadata.sample_rate == 0
        assert metadata.bit_depth == 0

    def test_to_dict(self) -> None:
        """Test conversion to dictionary."""
        metadata = TrackMetadata(
            track_id="12345",
            title="Test Track",
            artist="Test Artist",
            album="Test Album",
            duration_ms=180000,
            artwork_url="https://example.com/art.jpg",
            actual_quality=27,
        )

        result = metadata.to_dict()

        assert result["track_id"] == "12345"
        assert result["title"] == "Test Track"
        assert result["artist"] == "Test Artist"
        assert result["album"] == "Test Album"
        assert result["duration_ms"] == 180000
        assert result["artwork_url"] == "https://example.com/art.jpg"
        assert result["quality"] == 27
        assert result["quality_name"] == "FLAC Hi-Res (24-bit/192kHz)"

    def test_duration_s_property(self) -> None:
        """Test duration_s property conversion."""
        metadata = TrackMetadata(duration_ms=180000)
        assert metadata.duration_s == 180.0

        metadata = TrackMetadata(duration_ms=0)
        assert metadata.duration_s == 0.0

    def test_is_url_expired_no_url(self) -> None:
        """Test URL expiry check with no URL."""
        metadata = TrackMetadata()
        assert metadata.is_url_expired() is True

    def test_is_url_expired_no_expiry(self) -> None:
        """Test URL expiry check with no expiry time."""
        metadata = TrackMetadata(streaming_url="https://example.com/stream")
        assert metadata.is_url_expired() is True

    def test_is_url_expired_valid(self) -> None:
        """Test URL expiry check with valid URL."""
        future_time = int(time.time()) + 300  # 5 minutes in future
        metadata = TrackMetadata(
            streaming_url="https://example.com/stream",
            streaming_url_expires_at=future_time,
        )
        assert metadata.is_url_expired() is False

    def test_is_url_expired_expired(self) -> None:
        """Test URL expiry check with expired URL."""
        past_time = int(time.time()) - 10  # 10 seconds ago
        metadata = TrackMetadata(
            streaming_url="https://example.com/stream",
            streaming_url_expires_at=past_time,
        )
        assert metadata.is_url_expired() is True

    def test_is_url_expired_within_buffer(self) -> None:
        """Test URL expiry check within buffer period."""
        # Expires in 20 seconds, default buffer is 30
        near_future = int(time.time()) + 20
        metadata = TrackMetadata(
            streaming_url="https://example.com/stream",
            streaming_url_expires_at=near_future,
        )
        assert metadata.is_url_expired() is True

        # But with smaller buffer, it's not expired
        assert metadata.is_url_expired(buffer_s=10) is False


class TestMetadataCache:
    """Tests for MetadataCache class."""

    def test_get_empty_cache(self) -> None:
        """Test getting from empty cache returns None."""
        cache = MetadataCache()
        assert cache.get("12345") is None

    def test_set_and_get(self) -> None:
        """Test setting and getting cache entry."""
        cache = MetadataCache()
        metadata = TrackMetadata(track_id="12345", title="Test")

        cache.set("12345", metadata)

        result = cache.get("12345")
        assert result is not None
        assert result.track_id == "12345"
        assert result.title == "Test"

    def test_clear(self) -> None:
        """Test clearing cache."""
        cache = MetadataCache()
        cache.set("12345", TrackMetadata(track_id="12345"))
        cache.set("67890", TrackMetadata(track_id="67890"))

        cache.clear()

        assert cache.get("12345") is None
        assert cache.get("67890") is None

    def test_invalidate_url(self) -> None:
        """Test invalidating URL keeps metadata."""
        cache = MetadataCache()
        metadata = TrackMetadata(
            track_id="12345",
            title="Test",
            streaming_url="https://example.com/stream",
            streaming_url_expires_at=int(time.time()) + 300,
        )
        cache.set("12345", metadata)

        cache.invalidate_url("12345")

        result = cache.get("12345")
        assert result is not None
        assert result.title == "Test"  # Metadata preserved
        assert result.streaming_url == ""  # URL cleared
        assert result.streaming_url_expires_at == 0

    def test_invalidate_url_nonexistent(self) -> None:
        """Test invalidating URL for nonexistent track does nothing."""
        cache = MetadataCache()
        cache.invalidate_url("nonexistent")  # Should not raise

    def test_lru_eviction(self) -> None:
        """Test LRU eviction when cache is full."""
        cache = MetadataCache()
        cache._max_size = 3  # Small cache for testing

        # Add 3 entries
        cache.set("1", TrackMetadata(track_id="1"))
        cache.set("2", TrackMetadata(track_id="2"))
        cache.set("3", TrackMetadata(track_id="3"))

        # All should be present
        assert cache.get("1") is not None
        assert cache.get("2") is not None
        assert cache.get("3") is not None

        # Add 4th entry, should evict oldest ("1")
        cache.set("4", TrackMetadata(track_id="4"))

        assert cache.get("1") is None  # Evicted
        assert cache.get("2") is not None
        assert cache.get("3") is not None
        assert cache.get("4") is not None

    def test_update_existing_no_eviction(self) -> None:
        """Test updating existing entry doesn't trigger eviction."""
        cache = MetadataCache()
        cache._max_size = 2

        cache.set("1", TrackMetadata(track_id="1", title="Original"))
        cache.set("2", TrackMetadata(track_id="2"))

        # Update "1" shouldn't evict "2"
        cache.set("1", TrackMetadata(track_id="1", title="Updated"))

        assert cache.get("1") is not None
        assert cache.get("1").title == "Updated"
        assert cache.get("2") is not None
