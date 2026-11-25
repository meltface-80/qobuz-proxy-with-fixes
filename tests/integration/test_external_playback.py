"""A Qobuz reconnect must leave another controller's audio and queue untouched."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import websockets
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from qobuz_proxy.backends.types import PlaybackState
from qobuz_proxy.config import Config
from qobuz_proxy.connect.discovery import DiscoveryService
from qobuz_proxy.connect.protocol import DecodedMessage, MessageType
from qobuz_proxy.proto import qconnect_payload_pb2 as pb
from qobuz_proxy.speaker import Speaker
from tests.connect.test_ws_manager import _last_join, _send_report
from tests.connect.test_ws_manager import valid_tokens as valid_tokens


from tests.integration.conftest import (
    QOBUZ_URI,
    assert_no_transport_writes,
    deliver_snapshot,
)


@pytest.mark.parametrize("playing_state", [1, 2, 3])
async def test_reconnect_snapshot_releases_spotify_without_transport_writes(rig, playing_state):
    backend, client, player, manager, tasks = rig
    await deliver_snapshot(manager, tasks, playing_state)
    assert_no_transport_writes(client)
    assert player.state == PlaybackState.STOPPED
    assert backend._next_track_queue_nr is None
    assert not manager.is_renderer_active
    assert await _send_report(manager) is False

    # Even a server that replays active=True cannot reacquire an external source.
    await manager._send_join_session()
    assert _last_join(manager).isActive is False
    assert _last_join(manager).reason == 2
    manager._is_connected = True  # Simulate a cached activation on another connection.
    await deliver_snapshot(manager, tasks, 2)
    assert_no_transport_writes(client)
    assert not manager.is_renderer_active


@pytest.mark.parametrize("already_detected", [True, False])
async def test_explicit_reselection_can_start_same_qobuz_track(rig, valid_tokens, already_detected):
    backend, client, player, manager, tasks = rig
    if already_detected:
        await deliver_snapshot(manager, tasks)
    speaker = Speaker(config=MagicMock(), api_client=MagicMock(), app_id="test")
    speaker._backend = backend
    speaker._player = player
    speaker._queue = player.queue
    speaker._ws_manager = manager
    manager._connection_loop = AsyncMock()
    await speaker._setup_websocket(valid_tokens)
    await manager._send_join_session()
    manager._is_connected = True
    assert _last_join(manager).isActive is True
    assert _last_join(manager).reason == 1

    async def start_queue(index):
        client.get_track_uri.return_value = QOBUZ_URI
        return True

    client.play_from_queue.side_effect = start_queue
    await deliver_snapshot(manager, tasks, 2)
    client.play_from_queue.assert_awaited_once_with(0)
    client.seek.assert_awaited_once_with(4000)
    assert player.state == PlaybackState.PLAYING
    assert manager.is_renderer_active


async def test_takeover_leaves_cloud_and_http_reselection_recovers_playback_and_volume(
    speaker_rig, valid_tokens
):
    speaker, backend, client, player, manager, tasks = speaker_rig
    connections = asyncio.Queue()
    connected = asyncio.Event()
    original_connected = manager._on_connected

    def note_connected():
        if original_connected:
            original_connected()
        connected.set()

    manager.on_connected(note_connected)
    manager.set_token_refresher(AsyncMock())

    async def accept(ws):
        # Exercise the actual WebSocket lifetime, including context-manager
        # cleanup on cancellation, instead of only changing connection flags.
        for _ in range(3):  # AUTHENTICATE, SUBSCRIBE, JOIN_SESSION
            frame = await ws.recv()
        decoded = manager._codec.decode_frame(frame)
        batch = manager._codec.decode_qconnect_batch(decoded.payload)
        await connections.put((ws, batch.messages[0].rndrSrvrJoinSession))
        await ws.wait_closed()

    http_app = web.Application()
    discovery = DiscoveryService(
        Config(), "test", on_connect=speaker._on_app_connected, web_app=http_app
    )
    speaker._discovery = discovery
    await discovery._start_http_server()
    async with websockets.serve(accept, "127.0.0.1", 0) as server:
        valid_tokens.ws_token.endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        async with TestClient(TestServer(http_app)) as http:
            try:
                manager.set_tokens(valid_tokens, activate=True)
                discovery.set_session(valid_tokens)
                await manager.start()
                original_task = manager._receive_task
                await manager.start()
                assert manager._receive_task is original_task
                old_ws, original_join = await asyncio.wait_for(connections.get(), 1)
                await asyncio.wait_for(connected.wait(), 1)
                old_task = manager._receive_task

                assert await backend.check_external_playback()
                response = await http.get("/streamcore/get-connect-info")
                assert (await response.json())["current_session_id"] == ""
                assert discovery.get_received_tokens() is None
                assert not manager.is_connected
                assert not manager.is_renderer_active
                await asyncio.wait_for(old_ws.wait_closed(), 1)
                assert_no_transport_writes(client)

                # Refreshing credentials must never resume the released session.
                manager.set_tokens(valid_tokens)
                await manager.start()
                assert manager._receive_task is old_task
                manager._token_refresher.assert_not_awaited()

                # The app can retry with the same device identity and tokens.
                connected.clear()
                response = await http.post(
                    "/streamcore/connect-to-qconnect",
                    json={
                        "session_id": valid_tokens.session_id,
                        "jwt_qconnect": {
                            "jwt": valid_tokens.ws_token.jwt,
                            "exp": valid_tokens.ws_token.exp,
                            "endpoint": valid_tokens.ws_token.endpoint,
                        },
                    },
                )
                assert response.status == 200
                _, new_join = await asyncio.wait_for(connections.get(), 1)
                await asyncio.wait_for(connected.wait(), 1)
                assert new_join.deviceInfo.deviceUuid == original_join.deviceInfo.deviceUuid
                assert new_join.isActive
                assert new_join.deviceInfo.capabilities.volumeRemoteControl == 2
                assert old_task.done()
                response = await http.get("/streamcore/get-connect-info")
                assert (await response.json())["current_session_id"] == valid_tokens.session_id

                async def start_queue(index):
                    client.get_track_uri.return_value = QOBUZ_URI
                    return True

                client.play_from_queue.side_effect = start_queue
                await deliver_snapshot(manager, tasks, 2)
                assert player.state == PlaybackState.PLAYING
                client.play_from_queue.assert_awaited_once_with(0)
                volume = pb.QConnectBatch()
                volume.messages.add(messageType=42).srvrRndrSetVolume.volume = 28
                await manager._handle_payload(
                    DecodedMessage(msg_type=MessageType.PAYLOAD, payload=volume.SerializeToString())
                )
                await asyncio.gather(*tasks)
                client.set_volume.assert_awaited_once_with(28)
            finally:
                await manager.stop()


async def test_selection_that_detects_takeover_keeps_new_discovery_session(
    speaker_rig, valid_tokens
):
    speaker, _, _, _, manager, _ = speaker_rig
    discovery = DiscoveryService(Config(), "test")
    speaker._discovery = discovery
    discovery.set_session(valid_tokens)
    manager._connection_loop = AsyncMock()

    await speaker._setup_websocket(valid_tokens)

    response = await discovery._handle_connect_info(MagicMock())
    assert json.loads(response.text)["current_session_id"] == valid_tokens.session_id
    assert discovery.get_received_tokens() is valid_tokens
    assert not manager._external_playback
    await manager.stop()
