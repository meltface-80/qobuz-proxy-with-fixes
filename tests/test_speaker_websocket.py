"""Tests for the Speaker WebSocket wiring, quality changes and state reports."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from qobuz_proxy.connect.types import ConnectTokens, JWTConnectToken
from qobuz_proxy.speaker import Speaker
from tests.speaker_helpers import _make_api_client, _make_speaker_config


class TestSpeakerWebSocket:
    async def test_setup_websocket_wires_gapless_rearm_callback(self):
        """Queue edits mid-track must re-arm gapless via the command handler callback."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")
        speaker._queue = MagicMock()
        player = MagicMock()
        player.start = AsyncMock()
        player.get_volume = AsyncMock(return_value=50)
        player.on_next_track_info_changed = AsyncMock()
        speaker._player = player

        tokens = ConnectTokens(
            session_id=str(uuid.uuid4()),
            ws_token=JWTConnectToken(
                jwt="jwt",
                exp=9999999999,
                endpoint="wss://test.qobuz.com/ws",
            ),
        )

        with (
            patch("qobuz_proxy.speaker.WsManager") as mock_ws_cls,
            patch("qobuz_proxy.speaker.StateReporter") as mock_sr_cls,
        ):
            mock_ws = mock_ws_cls.return_value
            mock_ws.start = AsyncMock()
            mock_ws.send_volume_changed = AsyncMock()
            mock_sr_cls.return_value.start = AsyncMock()

            await speaker._setup_websocket(tokens)

        assert speaker._playback_handler is not None
        assert speaker._playback_handler._on_next_track_changed is player.on_next_track_info_changed
        player.set_next_track_request_callback.assert_called_once_with(mock_ws.request_next_track)

    async def test_setup_websocket_wires_connect_notice_to_playback_handler(self):
        """The handler must invalidate commands on each connection change."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")
        speaker._queue = MagicMock()
        player = MagicMock()
        player.start = AsyncMock()
        player.get_volume = AsyncMock(return_value=50)
        speaker._player = player

        tokens = ConnectTokens(
            session_id=str(uuid.uuid4()),
            ws_token=JWTConnectToken(
                jwt="jwt",
                exp=9999999999,
                endpoint="wss://test.qobuz.com/ws",
            ),
        )

        with (
            patch("qobuz_proxy.speaker.WsManager") as mock_ws_cls,
            patch("qobuz_proxy.speaker.StateReporter") as mock_sr_cls,
        ):
            mock_ws = mock_ws_cls.return_value
            mock_ws.start = AsyncMock()
            mock_ws.send_volume_changed = AsyncMock()
            mock_sr_cls.return_value.start = AsyncMock()

            await speaker._setup_websocket(tokens)

        assert speaker._playback_handler is not None
        mock_ws.on_connected.assert_called_once_with(speaker._playback_handler.note_connected)
        mock_ws.on_disconnected.assert_called_once_with(speaker._playback_handler.note_disconnected)
        for msg_type in speaker._playback_handler.get_message_types():
            mock_ws.register_handler.assert_any_call(
                msg_type, speaker._playback_handler.dispatch_message
            )

    async def test_setup_websocket_refreshes_existing_manager(self):
        """If ws_manager already exists, _setup_websocket should only refresh tokens."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")
        speaker._queue = MagicMock()
        speaker._player = MagicMock()
        speaker._ws_manager = MagicMock()

        tokens = ConnectTokens(
            session_id=str(uuid.uuid4()),
            ws_token=JWTConnectToken(
                jwt="new_jwt",
                exp=9999999999,
                endpoint="wss://test.qobuz.com/ws",
            ),
        )
        speaker._ws_manager.start = AsyncMock()

        with patch("qobuz_proxy.speaker.WsManager") as mock_ws_cls:
            await speaker._setup_websocket(tokens)

        # Should refresh tokens on existing manager, not create a new one
        speaker._ws_manager.set_tokens.assert_called_once_with(tokens, activate=True)
        speaker._ws_manager.start.assert_awaited_once()
        mock_ws_cls.assert_not_called()
        assert speaker._ws_connected_event.is_set() is True

    async def test_on_app_connected_creates_task(self):
        """_on_app_connected should schedule _setup_websocket as an asyncio task."""
        import asyncio

        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        tokens = ConnectTokens(
            session_id=str(uuid.uuid4()),
            ws_token=JWTConnectToken(
                jwt="jwt",
                exp=9999999999,
                endpoint="wss://test.qobuz.com/ws",
            ),
        )

        setup_calls = []

        async def fake_setup(t):
            setup_calls.append(t)

        speaker._setup_websocket = fake_setup  # type: ignore[method-assign]
        speaker._on_app_connected(tokens)

        # Let the event loop process the created task
        await asyncio.sleep(0)

        assert len(setup_calls) == 1
        assert setup_calls[0] is tokens


class TestSpeakerQualityChange:
    async def test_quality_change_updates_effective_quality(self):
        config = _make_speaker_config(max_quality=27)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        speaker._metadata_service = MagicMock()
        speaker._player = MagicMock()
        speaker._player.reload_current_track = AsyncMock()

        await speaker._on_quality_change(6)

        assert speaker._effective_quality == 6
        speaker._metadata_service.set_max_quality.assert_called_once_with(6)
        speaker._player.reload_current_track.assert_called_once()

    async def test_quality_change_noop_if_same(self):
        config = _make_speaker_config(max_quality=27)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        speaker._effective_quality = 27
        speaker._metadata_service = MagicMock()
        speaker._player = MagicMock()
        speaker._player.reload_current_track = AsyncMock()

        await speaker._on_quality_change(27)

        speaker._metadata_service.set_max_quality.assert_not_called()
        speaker._player.reload_current_track.assert_not_called()


class TestSpeakerStateReport:
    async def test_send_state_report_maps_loading_to_playing(self):
        """A renderer between tracks is loading, not stopped. Reporting STOPPED
        made the app answer a track change with a PAUSED SET_STATE (GitHub #22)."""
        from qobuz_proxy.backends import PlaybackState

        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        speaker._ws_manager = MagicMock()
        speaker._ws_manager.send_state_update = AsyncMock()

        report = MagicMock()
        report.playing_state = PlaybackState.LOADING
        report.buffer_state = 1
        report.position_value_ms = 0
        report.duration_ms = 60000
        report.current_queue_item_id = "item-1"
        report.queue_version_major = 1
        report.queue_version_minor = 0

        await speaker._send_state_report(report)

        call_kwargs = speaker._ws_manager.send_state_update.call_args.kwargs
        assert call_kwargs["playing_state"] == int(PlaybackState.PLAYING)

    async def test_send_state_report_maps_error_to_stopped(self):
        from qobuz_proxy.backends import PlaybackState

        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        speaker._ws_manager = MagicMock()
        speaker._ws_manager.send_state_update = AsyncMock()

        report = MagicMock()
        report.playing_state = PlaybackState.ERROR
        report.buffer_state = 1
        report.position_value_ms = 0
        report.duration_ms = 0
        report.current_queue_item_id = ""
        report.queue_version_major = 0
        report.queue_version_minor = 0

        await speaker._send_state_report(report)

        call_kwargs = speaker._ws_manager.send_state_update.call_args.kwargs
        assert call_kwargs["playing_state"] == int(PlaybackState.STOPPED)

    async def test_send_state_report_noop_without_ws_manager(self):
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        speaker._ws_manager = None

        report = MagicMock()
        # Should not raise
        await speaker._send_state_report(report)
