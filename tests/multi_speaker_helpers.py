"""Shared helpers for the multi-speaker orchestration tests."""

from unittest.mock import AsyncMock, MagicMock

from qobuz_proxy.config import Config, QobuzConfig, SpeakerConfig


def _make_mock_speaker(name: str, starts: bool) -> MagicMock:
    speaker = MagicMock()
    speaker.start = AsyncMock(return_value=starts)
    speaker.stop = AsyncMock()
    speaker.name = name
    return speaker


def _make_speaker_config(
    name: str = "Test Speaker", http_port: int = 8689, **kwargs
) -> SpeakerConfig:
    """Return a minimal SpeakerConfig for tests."""
    defaults = dict(
        name=name,
        uuid=f"uuid-{name.lower().replace(' ', '-')}",
        backend_type="dlna",
        max_quality=27,
        http_port=http_port,
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


def _make_config(*speaker_configs: SpeakerConfig) -> Config:
    """Return a Config with the given speakers and auth credentials."""
    config = Config()
    config.qobuz = QobuzConfig(email="test@example.com", auth_token="secret", user_id="12345")
    config.speakers = list(speaker_configs)
    return config
