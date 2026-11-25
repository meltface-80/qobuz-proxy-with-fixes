"""Tests for WsManager session ownership: JOIN_SESSION activation and SET_ACTIVE handling."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from qobuz_proxy.auth.tokens import WSToken
from qobuz_proxy.connect.ws_manager import TokenRefreshRequired, WsManager
from tests.connect.ws_helpers import _deliver_active, _last_join, _send_report


class TestSessionOwnership:
    @pytest.mark.parametrize(
        ("backend_type", "fixed_volume", "expected"),
        [("dlna", True, 1), ("dlna", False, 2), ("local", True, 2)],
    )
    async def test_join_volume_capability(
        self, config, valid_tokens, backend_type, fixed_volume, expected
    ):
        config.backend.type = backend_type
        config.backend.dlna.fixed_volume = fixed_volume
        manager = WsManager(config)
        manager.set_tokens(valid_tokens, activate=True)
        manager._ws = AsyncMock()
        await manager._send_join_session()
        assert _last_join(manager).deviceInfo.capabilities.volumeRemoteControl == expected

        # Reconnects must preserve the same capability.
        await manager._send_join_session()
        assert _last_join(manager).deviceInfo.capabilities.volumeRemoteControl == expected

    @pytest.mark.parametrize(
        ("device_type", "expected"),
        [("speaker", 1), ("streamer", 2), ("soundbar", 4), ("headphones", 8)],
    )
    async def test_join_device_type(self, config, valid_tokens, device_type, expected):
        config.device.device_type = device_type
        manager = WsManager(config)
        manager.set_tokens(valid_tokens, activate=True)
        manager._ws = AsyncMock()
        await manager._send_join_session()
        assert _last_join(manager).deviceInfo.type == expected

    async def test_three_speaker_refresh_preserves_selected_owner(self, config, valid_tokens):
        managers = [WsManager(config) for _ in range(3)]
        for manager in managers:
            manager.set_tokens(valid_tokens, activate=True)
            manager._ws = AsyncMock()
            await manager._send_join_session()
            assert _last_join(manager).isActive is True
            assert _last_join(manager).reason == 1
            manager._is_connected = True
            await _deliver_active(manager, True)

        # The user selected speaker 2; both former owners have been deactivated.
        await _deliver_active(managers[0], False)
        await _deliver_active(managers[2], False)
        for index, manager in enumerate(managers):
            manager._invalidate_connection()
            manager._should_run = True
            manager._ws_token = WSToken(jwt="expired", exp_s=1, endpoint="wss://test/ws")
            manager.set_token_refresher(
                AsyncMock(
                    return_value=WSToken(
                        jwt="fresh",
                        exp_s=9999999999,
                        endpoint="wss://test/ws",
                    )
                )
            )
            assert await manager._wait_for_valid_token(buffer_s=60)
            await manager._send_join_session()
            assert _last_join(manager).isActive is (index == 1)
            assert _last_join(manager).reason == 2
            # A join request itself is not confirmation to send playback state.
            manager._is_connected = True
            assert await _send_report(manager) is False

    async def test_explicit_selection_of_connected_inactive_speaker(self, ws_manager, valid_tokens):
        ws_manager.set_tokens(valid_tokens)
        ws_manager._ws = AsyncMock()
        ws_manager._is_connected = True
        ws_manager._should_run = True
        await _deliver_active(ws_manager, False)

        # Identical tokens still represent a fresh selection via discovery.
        ws_manager.set_tokens(valid_tokens, activate=True)
        await asyncio.sleep(0)
        ws_manager._ws.close.assert_awaited_once()
        await ws_manager._send_join_session()
        assert _last_join(ws_manager).isActive is True

        # Even without any server confirmation, the selection is consumed.
        await ws_manager._send_join_session()
        assert _last_join(ws_manager).isActive is False

    async def test_app_refresh_without_selection_does_not_activate(self, ws_manager, valid_tokens):
        ws_manager.set_tokens(valid_tokens)
        ws_manager._ws = AsyncMock()
        ws_manager._is_connected = True
        await _deliver_active(ws_manager, False)
        valid_tokens.ws_token.jwt = "refreshed"
        ws_manager.set_tokens(valid_tokens)
        await ws_manager._send_join_session()
        assert _last_join(ws_manager).isActive is False

    async def test_noop_app_attach_cannot_arm_future_takeover(self, ws_manager, valid_tokens):
        ws_manager.set_tokens(valid_tokens, activate=True)
        ws_manager._ws = AsyncMock()
        await ws_manager._send_join_session()
        ws_manager._is_connected = True
        await _deliver_active(ws_manager, True)
        ws_manager.set_tokens(valid_tokens, activate=True)
        await _deliver_active(ws_manager, False)
        await ws_manager._send_join_session()
        assert _last_join(ws_manager).isActive is False

    async def test_new_session_does_not_inherit_ownership(self, ws_manager, valid_tokens):
        ws_manager.set_tokens(valid_tokens)
        ws_manager._ws = AsyncMock()
        ws_manager._is_connected = True
        await _deliver_active(ws_manager, True)
        valid_tokens.session_id = str(uuid.uuid4())
        ws_manager.set_tokens(valid_tokens)
        await ws_manager._send_join_session()
        assert _last_join(ws_manager).isActive is False

    async def test_failed_join_keeps_explicit_selection_for_retry(self, ws_manager, valid_tokens):
        ws_manager.set_tokens(valid_tokens, activate=True)
        ws_manager._ws = AsyncMock()
        ws_manager._ws.send.side_effect = OSError("connection lost")
        with pytest.raises(OSError):
            await ws_manager._send_join_session()
        ws_manager._ws.send.side_effect = None
        await ws_manager._send_join_session()
        assert _last_join(ws_manager).isActive is True
        await ws_manager._send_join_session()
        assert _last_join(ws_manager).isActive is False

    async def test_selection_during_join_is_not_consumed_by_old_join(
        self, ws_manager, valid_tokens
    ):
        ws_manager.set_tokens(valid_tokens, activate=True)
        ws_manager._ws = AsyncMock()
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_send(frame):
            entered.set()
            await release.wait()

        ws_manager._ws.send.side_effect = slow_send
        task = asyncio.create_task(ws_manager._send_join_session())
        await entered.wait()
        ws_manager.set_tokens(valid_tokens, activate=True)
        release.set()
        await task
        ws_manager._ws.send.side_effect = None
        await ws_manager._send_join_session()
        assert _last_join(ws_manager).isActive is True
        await ws_manager._send_join_session()
        assert _last_join(ws_manager).isActive is False

    @pytest.mark.parametrize("reactivate", [False, True])
    async def test_report_waiting_on_send_lock_cannot_outlive_ownership(
        self, ws_manager, reactivate
    ):
        ws_manager._ws = AsyncMock()
        ws_manager._is_connected = True
        await _deliver_active(ws_manager, True)
        await ws_manager._send_lock.acquire()
        task = asyncio.create_task(_send_report(ws_manager))
        await asyncio.sleep(0)
        await _deliver_active(ws_manager, False)
        if reactivate:
            await _deliver_active(ws_manager, True)
        counter = ws_manager._codec._msg_counter
        ws_manager._send_lock.release()
        assert await task is False
        ws_manager._ws.send.assert_not_awaited()
        assert ws_manager._codec._msg_counter == counter
        if reactivate:
            assert await _send_report(ws_manager) is True

    async def test_deactivated_stop_report_is_suppressed(self, ws_manager):
        ws_manager._ws = AsyncMock()
        ws_manager._is_connected = True
        await _deliver_active(ws_manager, True)
        reports = []
        ws_manager.register_handler(
            43, lambda *_: reports.append(asyncio.create_task(_send_report(ws_manager)))
        )
        await _deliver_active(ws_manager, False)
        assert await reports[0] is False
        ws_manager._ws.send.assert_not_awaited()
        # Volume is device-scoped and still available for inactive speakers.
        assert await ws_manager.send_volume_changed(42) is True

    async def test_old_socket_messages_ignored_after_invalidation(self, ws_manager):
        ws_manager._is_connected = True
        await _deliver_active(ws_manager, False)
        handler = MagicMock()
        ws_manager.register_handler(43, handler)
        ws_manager._invalidate_connection()
        await _deliver_active(ws_manager, True)
        assert ws_manager._renderer_active is False
        handler.assert_not_called()

    @pytest.mark.parametrize("paused", [False, True])
    async def test_owner_reconnect_preserves_playback_and_requires_confirmation(
        self, ws_manager, valid_tokens, paused
    ):
        from qobuz_proxy.backends import PlaybackState
        from qobuz_proxy.playback.command_handler import PlaybackCommandHandler
        from tests.playback.test_command_handler_set_state import _set_state_msg
        from tests.playback.test_player_serialization import _make_player

        player, backend = _make_player()
        handler = PlaybackCommandHandler(player)
        ws_manager.on_connected(handler.note_connected)
        ws_manager.on_disconnected(handler.note_disconnected)
        tasks = []

        def dispatch(msg_type, message):
            task = handler.dispatch_message(msg_type, message)
            tasks.append(task)
            return task

        ws_manager.register_handler(43, dispatch)
        ws_manager.set_tokens(valid_tokens, activate=True)
        ws_manager._ws = AsyncMock()
        await ws_manager._send_join_session()
        ws_manager._is_connected = True
        handler.note_connected()
        await _deliver_active(ws_manager, True)
        await asyncio.gather(*tasks)
        await handler._handle_set_state(_set_state_msg(track_id=2001, queue_item_id=1))
        if paused:
            await player.pause()
        expected = PlaybackState.PAUSED if paused else PlaybackState.PLAYING
        assert player.state == expected

        ws_manager._invalidate_connection()
        assert player.state == expected
        await ws_manager._send_join_session()
        assert _last_join(ws_manager).isActive is True
        ws_manager._is_connected = True
        handler.note_connected()
        assert await _send_report(ws_manager) is False
        await _deliver_active(ws_manager, True)
        await asyncio.gather(*tasks)
        assert await _send_report(ws_manager) is True
        await handler._handle_set_state(
            _set_state_msg(
                track_id=2001,
                queue_item_id=1,
                playing_state=3 if paused else 2,
            )
        )
        assert player.state == expected
        assert backend.played == ["2001"]

    async def test_token_refresh_disconnect_invalidates_before_rejoining(
        self, ws_manager, valid_tokens, monkeypatch
    ):
        ws_manager.set_tokens(valid_tokens, activate=True)
        ws_manager._should_run = True
        connection = AsyncMock()
        context = AsyncMock()
        context.__aenter__.return_value = connection
        monkeypatch.setattr(
            "qobuz_proxy.connect.ws_manager.websockets.connect", MagicMock(return_value=context)
        )
        disconnected = MagicMock()
        ws_manager.on_disconnected(disconnected)
        joins = []

        async def receive_until_refresh():
            joins.append(_last_join(ws_manager).isActive)
            await _deliver_active(ws_manager, False)
            raise TokenRefreshRequired()

        monkeypatch.setattr(ws_manager, "_receive_loop", receive_until_refresh)
        for _ in range(2):
            with pytest.raises(TokenRefreshRequired):
                await ws_manager._connect_and_run()
            assert ws_manager.is_connected is False
            assert ws_manager.is_renderer_active is False
        assert joins == [True, False]
        assert disconnected.call_count == 2

    @pytest.mark.parametrize("deactivate", [False, True])
    async def test_next_waiting_for_send_lock_is_revocable(self, ws_manager, deactivate):
        ws_manager._ws = AsyncMock()
        ws_manager._is_connected = True
        await _deliver_active(ws_manager, True)
        needed = True
        await ws_manager._send_lock.acquire()
        task = asyncio.create_task(ws_manager.request_next_track(lambda: needed))
        await asyncio.sleep(0)
        if deactivate:
            await _deliver_active(ws_manager, False)
        else:
            needed = False  # A new user command superseded the skip.
        counter = ws_manager._codec._msg_counter
        ws_manager._send_lock.release()
        assert await task is False
        ws_manager._ws.send.assert_not_awaited()
        assert ws_manager._codec._msg_counter == counter


async def test_reselection_waits_for_released_socket_cleanup(ws_manager, valid_tokens):
    closing = asyncio.Event()
    finish_close = asyncio.Event()
    running = asyncio.Event()

    async def old_connection():
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            closing.set()
            await finish_close.wait()
            ws_manager._invalidate_connection()
            ws_manager._ws = None

    ws_manager._should_run = True
    ws_manager._receive_task = asyncio.create_task(old_connection())
    await running.wait()
    ws_manager.release_external_playback()
    await closing.wait()
    ws_manager.set_tokens(valid_tokens, activate=True)
    ws_manager._connection_loop = AsyncMock()
    restarting = asyncio.create_task(ws_manager.start())
    try:
        await asyncio.sleep(0)
        assert not restarting.done()
        ws_manager._connection_loop.assert_not_awaited()
        finish_close.set()
        await asyncio.wait_for(restarting, 1)
        await asyncio.sleep(0)
        ws_manager._connection_loop.assert_awaited_once()
        assert ws_manager._activation_request is not None
    finally:
        finish_close.set()
        await ws_manager.stop()
