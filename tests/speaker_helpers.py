"""Shared helpers for the Speaker tests."""

from unittest.mock import MagicMock

from qobuz_proxy.config import SpeakerConfig


def _make_speaker_config(**kwargs) -> SpeakerConfig:
    """Create a minimal SpeakerConfig suitable for testing."""
    defaults = dict(
        name="Test Speaker",
        uuid="test-uuid-1234",
        backend_type="dlna",
        max_quality=27,
        http_port=8689,
        bind_address="0.0.0.0",
        dlna_ip="192.168.1.100",
        dlna_port=1400,
        dlna_fixed_volume=False,
        proxy_port=7120,
        audio_device="default",
        audio_buffer_size=2048,
    )
    defaults.update(kwargs)
    return SpeakerConfig(**defaults)


def _make_api_client() -> MagicMock:
    return MagicMock()
