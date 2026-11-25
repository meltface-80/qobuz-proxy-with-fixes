"""Tests for LocalAudioBackend playback (QPROXY-021)."""

import asyncio
from unittest.mock import MagicMock, patch

import aiohttp
import numpy as np

from qobuz_proxy.backends.types import PlaybackState
from tests.backends.local_helpers import (
    FAKE_AUDIO_44100,
    FAKE_AUDIO_96000,
    _create_connected_backend,
    _make_metadata,
    _mock_aiohttp_session,
)


# ---------------------------------------------------------------------------
# Tests: State Transitions
# ---------------------------------------------------------------------------


class TestPlayStateTransitions:
    """Test state transitions during play()."""

    async def test_play_transitions_loading_then_playing(self) -> None:
        backend = await _create_connected_backend()
        states: list[PlaybackState] = []
        backend.on_state_change(lambda s: states.append(s))

        mock_sf = MagicMock()
        mock_sf.read.return_value = (FAKE_AUDIO_44100.copy(), 44100)

        with (
            patch("aiohttp.ClientSession", return_value=_mock_aiohttp_session()),
            patch("qobuz_proxy.backends.local.backend.sf", mock_sf, create=True),
            patch.dict("sys.modules", {"soundfile": mock_sf}),
        ):

            async def patched_download(url):
                return FAKE_AUDIO_44100.copy(), 44100

            backend._download_and_decode = patched_download

            # Mock stream methods
            backend._stream.set_ring_buffer = MagicMock()
            backend._stream.open = MagicMock()
            backend._stream.start = MagicMock()

            await backend.play("http://example.com/track.flac", _make_metadata())

            # Let the feeding task start
            await asyncio.sleep(0.01)

        assert PlaybackState.LOADING in states
        assert PlaybackState.PLAYING in states
        assert states.index(PlaybackState.LOADING) < states.index(PlaybackState.PLAYING)

        await backend.stop()
        await backend.disconnect()

    async def test_play_download_failure(self) -> None:
        backend = await _create_connected_backend()
        states: list[PlaybackState] = []
        errors: list[str] = []
        backend.on_state_change(lambda s: states.append(s))
        backend.on_playback_error(lambda e: errors.append(e))

        async def failing_download(url):
            raise aiohttp.ClientError("Download failed")

        backend._download_and_decode = failing_download

        await backend.play("http://example.com/track.flac", _make_metadata())

        assert PlaybackState.LOADING in states
        assert PlaybackState.ERROR in states
        assert len(errors) == 1
        assert "Download failed" in errors[0]

        await backend.disconnect()

    async def test_play_decode_failure(self) -> None:
        backend = await _create_connected_backend()
        errors: list[str] = []
        backend.on_playback_error(lambda e: errors.append(e))

        async def failing_decode(url):
            raise RuntimeError("Decode error: unsupported format")

        backend._download_and_decode = failing_decode

        await backend.play("http://example.com/track.flac", _make_metadata())

        assert backend._state == PlaybackState.ERROR
        assert len(errors) == 1
        assert "Decode error" in errors[0]

        await backend.disconnect()


class TestTrackChangeSilencesOldAudio:
    async def test_play_silences_previous_track_during_download(self) -> None:
        """Regression for BUG-13: the old track must stop at the top of play(),
        not keep draining the ring buffer through the new track's download."""
        backend = await _create_connected_backend()

        async def fake_download(url):
            return FAKE_AUDIO_44100.copy(), 44100

        backend._download_and_decode = fake_download
        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = MagicMock()
        backend._stream.start = MagicMock()
        backend._stream.pause = MagicMock()

        await backend.play("http://example.com/a.flac", _make_metadata())
        await asyncio.sleep(0.05)  # let the feeder fill the buffer
        old_buffer = backend._ring_buffer
        assert old_buffer.available() > 0

        download_entered = asyncio.Event()

        async def slow_download(url):
            download_entered.set()
            await asyncio.sleep(0.05)
            return FAKE_AUDIO_44100.copy(), 44100

        backend._download_and_decode = slow_download
        play_task = asyncio.create_task(backend.play("http://example.com/b.flac", _make_metadata()))
        await download_entered.wait()

        # While the new track downloads, the old audio is already silenced
        assert old_buffer.available() == 0
        backend._stream.pause.assert_called()

        await play_task
        await backend.stop()
        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Pause / Resume
# ---------------------------------------------------------------------------


class TestPauseResume:
    async def test_pause_resume(self) -> None:
        backend = await _create_connected_backend()

        # Start playback with patched download
        async def fake_download(url):
            return FAKE_AUDIO_44100.copy(), 44100

        backend._download_and_decode = fake_download
        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = MagicMock()
        backend._stream.start = MagicMock()
        backend._stream.pause = MagicMock()
        backend._stream.resume = MagicMock()

        await backend.play("http://example.com/track.flac", _make_metadata())
        await asyncio.sleep(0.01)

        await backend.pause()
        assert backend._state == PlaybackState.PAUSED
        backend._stream.pause.assert_called_once()

        await backend.resume()
        assert backend._state == PlaybackState.PLAYING
        backend._stream.resume.assert_called_once()

        await backend.stop()
        await backend.disconnect()

    async def test_resume_after_stop_reports_nothing_to_play(self) -> None:
        """Regression for BUG-25: stop() unloads the track, so a later resume()
        must fail instead of reporting a phantom PLAYING at 0:00 in silence."""
        backend = await _create_connected_backend()

        async def fake_download(url):
            return FAKE_AUDIO_44100.copy(), 44100

        backend._download_and_decode = fake_download
        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = MagicMock()
        backend._stream.start = MagicMock()
        backend._stream.stop = MagicMock()
        backend._stream.resume = MagicMock()

        await backend.play("http://example.com/track.flac", _make_metadata())
        await asyncio.sleep(0.01)
        await backend.stop()

        assert await backend.resume() is False
        assert backend._state == PlaybackState.STOPPED
        backend._stream.resume.assert_not_called()

        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Stop
# ---------------------------------------------------------------------------


class TestStop:
    async def test_stop_during_playback(self) -> None:
        backend = await _create_connected_backend()

        async def fake_download(url):
            # Return a lot of audio so feeding loop doesn't finish instantly
            return np.random.rand(441000, 2).astype(np.float32), 44100

        backend._download_and_decode = fake_download
        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = MagicMock()
        backend._stream.start = MagicMock()
        backend._stream.stop = MagicMock()

        await backend.play("http://example.com/track.flac", _make_metadata())
        await asyncio.sleep(0.01)

        assert backend._feeding_task is not None
        assert not backend._feeding_task.done()

        await backend.stop()

        assert backend._state == PlaybackState.STOPPED
        assert backend._feeding_task is None
        assert backend._audio_data is None
        assert backend._frames_fed == 0
        backend._stream.stop.assert_called_once()

        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Track End
# ---------------------------------------------------------------------------


class TestTrackEnd:
    async def test_track_ends_naturally(self) -> None:
        backend = await _create_connected_backend()
        ended = []
        backend.on_track_ended(lambda: ended.append(True))

        # Small audio so it finishes quickly
        small_audio = np.random.rand(100, 2).astype(np.float32)

        async def fake_download(url):
            return small_audio, 44100

        backend._download_and_decode = fake_download
        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = MagicMock()
        backend._stream.start = MagicMock()

        await backend.play("http://example.com/track.flac", _make_metadata())

        # The ring buffer has 10s * 44100 = 441000 frame capacity
        # With 100 frames, feeding finishes instantly. But draining waits
        # for ring_buffer.available() == 0. Since nothing reads from
        # the buffer in tests, we need to manually drain it.
        await asyncio.sleep(0.05)

        # The feeding loop fed all 100 frames. Now manually drain the buffer
        # so the drain check passes.
        if backend._ring_buffer:
            backend._ring_buffer.read(backend._ring_buffer.available())

        # Wait for drain check
        await asyncio.sleep(0.2)

        assert len(ended) == 1
        assert backend._state == PlaybackState.STOPPED

        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Sample Rate Change
# ---------------------------------------------------------------------------


class TestSampleRateChange:
    async def test_stream_reopened_on_sample_rate_change(self) -> None:
        backend = await _create_connected_backend()
        open_calls = []

        def track_open(sample_rate, channels=2):
            open_calls.append((sample_rate, channels))

        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = track_open
        backend._stream.start = MagicMock()
        backend._stream.stop = MagicMock()

        # First track at 44100
        async def fake_download_44100(url):
            return FAKE_AUDIO_44100.copy(), 44100

        backend._download_and_decode = fake_download_44100
        await backend.play("http://example.com/track1.flac", _make_metadata())
        await asyncio.sleep(0.01)
        await backend.stop()

        # Second track at 96000
        async def fake_download_96000(url):
            return FAKE_AUDIO_96000.copy(), 96000

        backend._download_and_decode = fake_download_96000
        await backend.play("http://example.com/track2.flac", _make_metadata())
        await asyncio.sleep(0.01)

        assert len(open_calls) == 2
        assert open_calls[0][0] == 44100
        assert open_calls[1][0] == 96000

        await backend.stop()
        await backend.disconnect()
