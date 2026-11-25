"""Tests for LocalAudioBackend controls, connection, decoding and feeding (QPROXY-021)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import numpy as np
import pytest

from qobuz_proxy.backends.local.backend import LocalAudioBackend
from tests.backends.local_helpers import (
    _SD_PATCH,
    FAKE_AUDIO_44100,
    _create_connected_backend,
    _make_metadata,
    _mock_aiohttp_session,
    _mock_sounddevice,
)


# ---------------------------------------------------------------------------
# Tests: Volume
# ---------------------------------------------------------------------------


class TestVolume:
    async def test_volume_delegated_to_stream(self) -> None:
        backend = await _create_connected_backend()
        backend._stream.set_volume = MagicMock()

        await backend.set_volume(75)
        assert await backend.get_volume() == 75
        backend._stream.set_volume.assert_called_with(75)

        await backend.disconnect()

    async def test_volume_clamped(self) -> None:
        backend = await _create_connected_backend()
        backend._stream.set_volume = MagicMock()

        await backend.set_volume(150)
        assert await backend.get_volume() == 100

        await backend.set_volume(-10)
        assert await backend.get_volume() == 0

        await backend.disconnect()

    async def test_volume_before_connect(self) -> None:
        backend = LocalAudioBackend()
        await backend.set_volume(80)
        assert await backend.get_volume() == 80


# ---------------------------------------------------------------------------
# Tests: Seek
# ---------------------------------------------------------------------------


class TestSeek:
    async def test_seek_sets_target(self) -> None:
        backend = await _create_connected_backend()
        backend._sample_rate = 44100
        backend._audio_data = np.zeros((441000, 2), dtype=np.float32)
        backend._total_frames = 441000

        await backend.seek(5000)  # 5 seconds
        assert backend._seek_target == int(5000 / 1000 * 44100)

        await backend.disconnect()

    async def test_seek_no_op_without_sample_rate(self) -> None:
        backend = await _create_connected_backend()
        assert backend._sample_rate == 0

        await backend.seek(5000)
        assert backend._seek_target is None

        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Position
# ---------------------------------------------------------------------------


class TestPosition:
    async def test_position_zero_initially(self) -> None:
        backend = await _create_connected_backend()
        assert await backend.get_position() == 0
        await backend.disconnect()

    async def test_position_reflects_frames_fed(self) -> None:
        backend = await _create_connected_backend()
        backend._sample_rate = 44100
        backend._frames_fed = 44100  # 1 second

        pos = await backend.get_position()
        assert pos == 1000  # 1000ms

        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Connect / Disconnect
# ---------------------------------------------------------------------------


class TestConnectDisconnect:
    async def test_connect_creates_stream(self) -> None:
        backend = LocalAudioBackend()
        with patch(_SD_PATCH, return_value=_mock_sounddevice()):
            result = await backend.connect()

        assert result is True
        assert backend.is_connected()
        assert backend._stream is not None
        assert "Test Output" in backend.name

        await backend.disconnect()

    async def test_connect_failure(self) -> None:
        sd = _mock_sounddevice()
        sd.query_devices.return_value = []  # No devices

        backend = LocalAudioBackend()
        with patch(_SD_PATCH, return_value=sd):
            result = await backend.connect()

        assert result is False
        assert not backend.is_connected()

    async def test_disconnect_stops_playback(self) -> None:
        backend = await _create_connected_backend()

        async def fake_download(url):
            return np.random.rand(441000, 2).astype(np.float32), 44100

        backend._download_and_decode = fake_download
        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = MagicMock()
        backend._stream.start = MagicMock()
        backend._stream.stop = MagicMock()
        backend._stream.close = MagicMock()

        await backend.play("http://example.com/track.flac", _make_metadata())
        await asyncio.sleep(0.01)

        await backend.disconnect()

        assert not backend.is_connected()
        assert backend._stream is None

    async def test_get_info(self) -> None:
        backend = LocalAudioBackend(device="usb-dac")
        info = backend.get_info()
        assert info.backend_type == "local"
        assert info.device_id == "local-usb-dac"


# ---------------------------------------------------------------------------
# Tests: Download and Decode
# ---------------------------------------------------------------------------


class TestDownloadAndDecode:
    async def test_download_and_decode_stereo(self) -> None:
        backend = LocalAudioBackend()

        mock_sf = MagicMock()
        mock_sf.read.return_value = (FAKE_AUDIO_44100.copy(), 44100)

        mock_session = _mock_aiohttp_session()

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch.dict("sys.modules", {"soundfile": mock_sf}),
        ):
            audio, sr = await backend._download_and_decode("http://example.com/track.flac")

        assert sr == 44100
        assert audio.shape == (44100, 2)
        assert audio.dtype == np.float32

    async def test_download_and_decode_mono(self) -> None:
        backend = LocalAudioBackend()
        mono_data = np.random.rand(44100).astype(np.float32)  # 1D

        mock_sf = MagicMock()
        mock_sf.read.return_value = (mono_data, 44100)

        mock_session = _mock_aiohttp_session()

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch.dict("sys.modules", {"soundfile": mock_sf}),
        ):
            audio, sr = await backend._download_and_decode("http://example.com/track.flac")

        assert audio.shape == (44100, 1)  # Reshaped to 2D

    async def test_download_http_error(self) -> None:
        backend = LocalAudioBackend()

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=404,
                message="Not Found",
            )
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch.dict("sys.modules", {"soundfile": MagicMock()}),
        ):
            with pytest.raises(aiohttp.ClientResponseError):
                await backend._download_and_decode("http://example.com/track.flac")


# ---------------------------------------------------------------------------
# Tests: Feeding Loop
# ---------------------------------------------------------------------------


class TestFeedingLoop:
    async def test_feeding_loop_feeds_all_frames(self) -> None:
        backend = await _create_connected_backend()

        audio = np.random.rand(1000, 2).astype(np.float32)

        async def fake_download(url):
            return audio, 44100

        backend._download_and_decode = fake_download
        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = MagicMock()
        backend._stream.start = MagicMock()

        positions: list[int] = []
        backend.on_position_update(lambda p: positions.append(p))

        await backend.play("http://example.com/track.flac", _make_metadata())
        await asyncio.sleep(0.1)

        # All frames should be fed (ring buffer is large enough)
        assert backend._frames_fed == 1000
        assert len(positions) > 0

        # Positions are buffer-corrected: since nothing reads from the buffer
        # in tests, reported positions may be 0 (all frames still buffered)
        for pos in positions:
            assert pos >= 0

        await backend.stop()
        await backend.disconnect()

    async def test_feeding_loop_seek(self) -> None:
        backend = await _create_connected_backend()

        # 10 seconds of audio at 44100
        audio = np.random.rand(441000, 2).astype(np.float32)

        async def fake_download(url):
            return audio, 44100

        backend._download_and_decode = fake_download
        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = MagicMock()
        backend._stream.start = MagicMock()

        await backend.play("http://example.com/track.flac", _make_metadata())
        await asyncio.sleep(0.05)

        # Seek to 5 seconds
        await backend.seek(5000)
        await asyncio.sleep(0.1)

        # frames_fed should be at or past 5s mark
        assert backend._frames_fed >= int(5000 / 1000 * 44100)

        await backend.stop()
        await backend.disconnect()
