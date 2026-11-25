"""Tests for the Speaker status payload (now playing, quality source)."""

from unittest.mock import AsyncMock, MagicMock, patch

from qobuz_proxy.config import AUTO_QUALITY
from qobuz_proxy.speaker import Speaker
from tests.speaker_helpers import _make_api_client, _make_speaker_config


class TestNowPlayingStatus:
    """get_status() must not report a volume the proxy doesn't control."""

    @staticmethod
    def _playing_speaker(**config_kwargs) -> Speaker:
        from qobuz_proxy.backends import PlaybackState

        config = _make_speaker_config(**config_kwargs)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        speaker._is_running = True

        player = MagicMock()
        player.state = PlaybackState.PLAYING
        player.current_track.metadata = {"title": "Song", "quality_name": "FLAC Hi-Res"}
        player._volume = 50  # Initial default — never updated in fixed-volume mode
        speaker._player = player
        return speaker

    def test_reports_volume_when_proxy_controls_it(self):
        speaker = self._playing_speaker(dlna_fixed_volume=False)
        np = speaker.get_status()["now_playing"]

        assert np["title"] == "Song"
        assert np["volume"] == 50

    def test_omits_volume_for_fixed_volume_speaker(self):
        speaker = self._playing_speaker(dlna_fixed_volume=True)
        status = speaker.get_status()

        assert "volume" not in status["now_playing"]
        assert status["config"]["fixed_volume"] is True

    def test_local_speaker_ignores_stale_fixed_volume_flag(self):
        """Fixed volume is a DLNA-only setting; a leftover flag on a local speaker
        must not hide the volume the proxy does control."""
        speaker = self._playing_speaker(backend_type="local", dlna_fixed_volume=True)
        np = speaker.get_status()["now_playing"]

        assert np["volume"] == 50


class TestQualitySourceStatus:
    """get_status() must say where the effective quality came from."""

    @staticmethod
    async def _start_auto_speaker(recommended, confirmed):
        from qobuz_proxy.backends.dlna import DLNABackend

        config = _make_speaker_config(max_quality=AUTO_QUALITY)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        mock_backend = MagicMock(spec=DLNABackend)
        mock_backend.name = "Mock DLNA"
        mock_backend.get_recommended_quality.return_value = recommended
        mock_backend.quality_detection_confirmed = confirmed

        mock_proxy = MagicMock()
        mock_proxy.start = AsyncMock()

        with (
            patch(
                "qobuz_proxy.speaker.BackendFactory.create_from_config",
                new_callable=AsyncMock,
                return_value=mock_backend,
            ),
            patch("qobuz_proxy.speaker.AudioProxyServer", return_value=mock_proxy),
            patch("qobuz_proxy.speaker.DiscoveryService") as mock_disc_cls,
        ):
            mock_disc_cls.return_value.start = AsyncMock()
            await speaker.start()
        return speaker

    async def test_confirmed_auto_detection(self):
        speaker = await self._start_auto_speaker(recommended=7, confirmed=True)
        cfg = speaker.get_status()["config"]

        assert cfg["max_quality"] == "auto"
        assert cfg["effective_quality"] == 7
        assert cfg["quality_source"] == "auto"

    async def test_unconfirmed_auto_detection_is_fallback(self):
        """gmediarender-style device: quality is a guess, the UI must say so."""
        speaker = await self._start_auto_speaker(recommended=6, confirmed=False)
        cfg = speaker.get_status()["config"]

        assert cfg["max_quality"] == "auto"
        assert cfg["effective_quality"] == 6
        assert cfg["quality_source"] == "auto_fallback"

    async def test_no_recommendation_is_fallback(self):
        speaker = await self._start_auto_speaker(recommended=None, confirmed=False)
        cfg = speaker.get_status()["config"]

        assert cfg["quality_source"] == "auto_fallback"

    def test_manual_quality(self):
        config = _make_speaker_config(max_quality=27)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        cfg = speaker.get_status()["config"]

        assert cfg["max_quality"] == 27
        assert cfg["effective_quality"] == 27
        assert cfg["quality_source"] == "manual"
