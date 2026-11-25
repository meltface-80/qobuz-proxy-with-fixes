"""Shared helpers for the LocalAudioBackend tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

from qobuz_proxy.backends.local.backend import LocalAudioBackend
from qobuz_proxy.backends.types import BackendTrackMetadata

FAKE_AUDIO_44100 = np.random.rand(44100, 2).astype(np.float32)  # 1 second
FAKE_AUDIO_96000 = np.random.rand(96000, 2).astype(np.float32)  # 1 second


def _mock_sounddevice():
    """Create a mock sounddevice module."""
    sd = MagicMock()
    sd.query_devices.return_value = [
        {
            "name": "Test Output",
            "max_output_channels": 2,
            "max_input_channels": 0,
            "default_samplerate": 44100.0,
        },
    ]
    sd.default.device = (0, 0)
    return sd


_SD_PATCH = "qobuz_proxy.backends.local.device._import_sounddevice"


def _make_metadata() -> BackendTrackMetadata:
    return BackendTrackMetadata(
        track_id="123",
        title="Test Track",
        artist="Test Artist",
        album="Test Album",
        duration_ms=1000,
    )


def _mock_aiohttp_session(data: bytes = b"fake-flac-data"):
    """Create a mock aiohttp.ClientSession context manager."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.read = AsyncMock(return_value=data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


async def _create_connected_backend() -> LocalAudioBackend:
    """Create a backend that's connected with mocked device."""
    backend = LocalAudioBackend(device="default", buffer_size=2048)
    with patch(_SD_PATCH, return_value=_mock_sounddevice()):
        await backend.connect()
    return backend
