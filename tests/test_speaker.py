"""Tests for the Speaker class: construction, lifecycle and start failures."""

from unittest.mock import AsyncMock, MagicMock, patch

from qobuz_proxy.config import AUTO_QUALITY, Config
from qobuz_proxy.speaker import Speaker
from tests.speaker_helpers import _make_api_client, _make_speaker_config


class TestSpeakerConstruction:
    def test_creates_with_config(self):
        config = _make_speaker_config(name="Living Room")
        api_client = _make_api_client()
        speaker = Speaker(config=config, api_client=api_client, app_id="my-app-id")

        assert speaker._config is config
        assert speaker._api_client is api_client
        assert speaker._app_id == "my-app-id"
        assert speaker._is_running is False

    def test_name_property(self):
        config = _make_speaker_config(name="Bedroom")
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        assert speaker.name == "Bedroom"

    def test_initial_component_slots_are_none(self):
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")

        assert speaker._discovery is None
        assert speaker._ws_manager is None
        assert speaker._metadata_service is None
        assert speaker._queue is None
        assert speaker._player is None
        assert speaker._backend is None
        assert speaker._proxy_server is None
        assert speaker._state_reporter is None
        assert speaker._queue_handler is None
        assert speaker._playback_handler is None
        assert speaker._volume_handler is None

    def test_build_component_config_maps_name_and_uuid(self):
        config = _make_speaker_config(name="Salon", uuid="abc-123")
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")

        comp = speaker._build_component_config()

        assert comp.device.name == "Salon"
        assert comp.device.uuid == "abc-123"

    def test_build_component_config_maps_backend_dlna(self):
        config = _make_speaker_config(
            backend_type="dlna",
            dlna_ip="10.0.0.1",
            dlna_port=1400,
            dlna_fixed_volume=True,
            proxy_port=7120,
        )
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")

        comp = speaker._build_component_config()

        assert comp.backend.type == "dlna"
        assert comp.backend.dlna.ip == "10.0.0.1"
        assert comp.backend.dlna.port == 1400
        assert comp.backend.dlna.fixed_volume is True
        assert comp.backend.dlna.proxy_port == 7120

    def test_build_component_config_maps_backend_local(self):
        config = _make_speaker_config(
            backend_type="local",
            audio_device="hw:1",
            audio_buffer_size=4096,
        )
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")

        comp = speaker._build_component_config()

        assert comp.backend.type == "local"
        assert comp.backend.local.device == "hw:1"
        assert comp.backend.local.buffer_size == 4096

    def test_build_component_config_maps_server(self):
        config = _make_speaker_config(http_port=9000, bind_address="192.168.1.5")
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")

        comp = speaker._build_component_config()

        assert comp.server.http_port == 9000
        assert comp.server.bind_address == "192.168.1.5"

    def test_build_component_config_maps_quality(self):
        config = _make_speaker_config(max_quality=6)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")

        comp = speaker._build_component_config()

        assert comp.qobuz.max_quality == 6

    def test_build_component_config_returns_config_instance(self):
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")

        comp = speaker._build_component_config()

        assert isinstance(comp, Config)


class TestSpeakerLifecycle:
    async def test_start_creates_backend_and_discovery(self):
        """start() should set up backend, player, queue, and discovery."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        mock_backend = MagicMock()
        mock_backend.name = "Mock DLNA Backend"
        # Not a DLNABackend instance, so proxy + fixed_volume branch is skipped
        mock_backend.__class__ = object  # not DLNABackend

        with (
            patch(
                "qobuz_proxy.speaker.BackendFactory.create_from_config",
                new_callable=AsyncMock,
                return_value=mock_backend,
            ),
            patch("qobuz_proxy.speaker.DiscoveryService") as mock_disc_cls,
        ):
            mock_disc = mock_disc_cls.return_value
            mock_disc.start = AsyncMock()

            result = await speaker.start()

        assert result is True
        assert speaker._is_running is True
        assert speaker._backend is mock_backend
        assert speaker._player is not None
        assert speaker._queue is not None
        assert speaker._discovery is not None

    async def test_start_with_dlna_backend_creates_proxy(self):
        """For a real DLNABackend, start() should create AudioProxyServer."""
        from qobuz_proxy.backends.dlna import DLNABackend

        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        mock_backend = MagicMock(spec=DLNABackend)
        mock_backend.name = "Mock DLNA"
        mock_backend.get_recommended_quality.return_value = 7

        mock_proxy = MagicMock()
        mock_proxy.start = AsyncMock()

        with (
            patch(
                "qobuz_proxy.speaker.BackendFactory.create_from_config",
                new_callable=AsyncMock,
                return_value=mock_backend,
            ),
            patch(
                "qobuz_proxy.speaker.AudioProxyServer",
                return_value=mock_proxy,
            ),
            patch("qobuz_proxy.speaker.DiscoveryService") as mock_disc_cls,
        ):
            mock_disc_cls.return_value.start = AsyncMock()

            result = await speaker.start()

        assert result is True
        assert speaker._proxy_server is mock_proxy
        mock_proxy.start.assert_called_once()
        mock_backend.set_proxy_server.assert_called_once_with(mock_proxy)

    async def test_start_auto_quality_dlna_uses_recommended(self):
        """AUTO_QUALITY with DLNA should resolve to get_recommended_quality()."""
        from qobuz_proxy.backends.dlna import DLNABackend

        config = _make_speaker_config(max_quality=AUTO_QUALITY)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        mock_backend = MagicMock(spec=DLNABackend)
        mock_backend.name = "Mock DLNA"
        mock_backend.get_recommended_quality.return_value = 6  # CD quality

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

        assert speaker._effective_quality == 6

    async def test_start_auto_quality_dlna_fallback(self):
        """AUTO_QUALITY with DLNA and no recommendation should use AUTO_FALLBACK_QUALITY."""
        from qobuz_proxy.backends.dlna import DLNABackend
        from qobuz_proxy.config import AUTO_FALLBACK_QUALITY

        config = _make_speaker_config(max_quality=AUTO_QUALITY)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        mock_backend = MagicMock(spec=DLNABackend)
        mock_backend.name = "Mock DLNA"
        mock_backend.get_recommended_quality.return_value = None  # No recommendation

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

        assert speaker._effective_quality == AUTO_FALLBACK_QUALITY

    async def test_start_failure_returns_false(self):
        """If BackendFactory raises, start() should return False and not raise."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        with patch(
            "qobuz_proxy.speaker.BackendFactory.create_from_config",
            new_callable=AsyncMock,
            side_effect=Exception("connection refused"),
        ):
            result = await speaker.start()

        assert result is False
        assert speaker._is_running is False

    async def test_start_failure_does_not_raise(self):
        """start() should swallow exceptions and return False, not propagate."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        with patch(
            "qobuz_proxy.speaker.BackendFactory.create_from_config",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            # Must NOT raise
            result = await speaker.start()

        assert result is False

    async def test_stop_tears_down_components(self):
        """stop() should call stop/disconnect on all wired components."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        # Simulate already-started state
        speaker._is_running = True
        speaker._state_reporter = MagicMock()
        speaker._state_reporter.stop = AsyncMock()
        speaker._player = MagicMock()
        speaker._player.stop = AsyncMock()
        speaker._ws_manager = MagicMock()
        speaker._ws_manager.stop = AsyncMock()
        speaker._discovery = MagicMock()
        speaker._discovery.stop = AsyncMock()
        speaker._proxy_server = MagicMock()
        speaker._proxy_server.stop = AsyncMock()
        speaker._backend = MagicMock()
        speaker._backend.disconnect = AsyncMock()

        await speaker.stop()

        speaker._state_reporter.stop.assert_called_once()
        speaker._player.stop.assert_called_once()
        speaker._ws_manager.stop.assert_called_once()
        speaker._discovery.stop.assert_called_once()
        speaker._proxy_server.stop.assert_called_once()
        speaker._backend.disconnect.assert_called_once()
        assert speaker._is_running is False

    async def test_stop_is_safe_with_no_components(self):
        """stop() on a speaker that never fully started should not raise."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")
        # All slots are None, _is_running is False
        await speaker.stop()  # Should not raise

    async def test_stop_continues_if_one_component_errors(self):
        """stop() should teardown all components even if some raise exceptions."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        speaker._is_running = True
        speaker._state_reporter = MagicMock()
        speaker._state_reporter.stop = AsyncMock(side_effect=Exception("state reporter error"))
        speaker._backend = MagicMock()
        speaker._backend.disconnect = AsyncMock()

        await speaker.stop()

        # backend.disconnect should still be called despite state_reporter failing
        speaker._backend.disconnect.assert_called_once()


class TestStartFailureLogging:
    """An offline renderer is an expected state and must not log as an error."""

    async def test_offline_backend_logs_info_without_error(self, caplog):
        import logging

        from qobuz_proxy.backends import BackendNotFoundError

        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        with (
            caplog.at_level(logging.INFO, logger="qobuz_proxy.speaker"),
            patch(
                "qobuz_proxy.speaker.BackendFactory.create_from_config",
                new_callable=AsyncMock,
                side_effect=BackendNotFoundError(
                    "Failed to connect to DLNA device at 192.168.1.100:1400"
                ),
            ),
        ):
            result = await speaker.start()

        assert result is False
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any(r.levelno == logging.INFO for r in caplog.records)

    async def test_unexpected_start_failure_still_logs_error(self, caplog):
        import logging

        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        with (
            caplog.at_level(logging.INFO, logger="qobuz_proxy.speaker"),
            patch(
                "qobuz_proxy.speaker.BackendFactory.create_from_config",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
        ):
            result = await speaker.start()

        assert result is False
        assert any(r.levelno >= logging.ERROR for r in caplog.records)
