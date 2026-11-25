"""Shared rig for the external-playback integration tests: a Sonos-like DLNA
backend wired to a real player, WsManager and discovery service."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from qobuz_proxy.backends.dlna.backend import DLNABackend
from qobuz_proxy.backends.dlna.client import DLNAClient
from qobuz_proxy.backends.types import PlaybackState
from qobuz_proxy.config import Config, SpeakerConfig
from qobuz_proxy.connect.protocol import DecodedMessage, MessageType
from qobuz_proxy.connect.ws_manager import WsManager
from qobuz_proxy.playback.command_handler import PlaybackCommandHandler
from qobuz_proxy.playback.player import QobuzPlayer
from qobuz_proxy.playback.queue import QueueTrack
from qobuz_proxy.playback.volume_handler import VolumeCommandHandler
from qobuz_proxy.proto import qconnect_payload_pb2 as pb
from qobuz_proxy.speaker import Speaker
from tests.connect.test_ws_manager import valid_tokens as valid_tokens  # noqa: F401 (fixture)


QOBUZ_URI = "http://proxy:7120/audio/123.flac"
NEXT_URI = "http://proxy:7120/audio/456_2.flac"
SPOTIFY_URI = "x-sonos-spotify:spotify%3atrack%3aexample"


@pytest.fixture
def speaker_rig(valid_tokens):
    backend = DLNABackend("192.0.2.1", name="Kitchen")
    backend._is_sonos = True
    backend._current_proxy_url = QOBUZ_URI
    backend._next_track_proxy_url = NEXT_URI
    backend._next_track_queue_nr = 2
    backend._state = PlaybackState.PAUSED
    client = AsyncMock(spec=DLNAClient)
    client.get_track_uri.return_value = SPOTIFY_URI
    client.get_transport_info.return_value = "PLAYING"
    client.get_position_info.return_value = 60000
    backend._client = client

    metadata = MagicMock()
    metadata.get_streaming_url = AsyncMock(return_value=QOBUZ_URI)
    metadata.get_metadata = AsyncMock(return_value=None)
    metadata.get_track_format.return_value = (6, 44100, 16)
    queue = MagicMock()
    queue.set_current_by_item_id = AsyncMock()
    player = QobuzPlayer(queue, metadata, backend)
    player._current_track = QueueTrack(
        track_id="123", queue_item_id=1, streaming_url=QOBUZ_URI, duration_ms=116000
    )
    player._current_duration_ms = 116000
    player._state = PlaybackState.PAUSED
    player._set_position(4000)
    manager = WsManager(Config())
    manager.set_tokens(valid_tokens, activate=True)
    manager._session_uuid = b"s" * 16
    manager._ws = AsyncMock()
    manager._is_connected = True
    manager._renderer_active = True
    manager._active_confirmed = True
    handler = PlaybackCommandHandler(player)
    handler._note_active(True)
    manager.on_disconnected(handler.note_disconnected)
    tasks = []

    def dispatch(kind, message):
        tasks.append(handler.dispatch_message(kind, message))

    for kind in handler.get_message_types():
        manager.register_handler(kind, dispatch)

    volume_handler = VolumeCommandHandler(player)

    def dispatch_volume(kind, message):
        tasks.append(asyncio.create_task(volume_handler.handle_message(kind, message)))

    for kind in volume_handler.get_message_types():
        manager.register_handler(kind, dispatch_volume)

    speaker = Speaker(SpeakerConfig(name="Kitchen"), MagicMock(), "test")
    speaker._ws_manager = manager
    speaker._player = player
    speaker._backend = backend
    speaker._queue = queue
    backend.on_external_playback(speaker._on_external_playback)
    player.set_playback_permission_check(backend.can_apply_remote_state)
    player.set_next_track_callbacks(handler.get_next_track_info, handler.clear_next_track_info)
    return speaker, backend, client, player, manager, tasks


@pytest.fixture
def rig(speaker_rig):
    return speaker_rig[1:]


async def deliver_snapshot(manager, tasks, playing_state=3):
    batch = pb.QConnectBatch()
    batch.messages.add(messageType=43).srvrRndrSetActive.active = True
    state = batch.messages.add(messageType=41).srvrRndrSetState
    state.currentQueueItem.trackId = 123
    state.currentQueueItem.queueItemId = 1
    state.currentPosition = 4000
    state.playingState = playing_state
    await manager._handle_payload(
        DecodedMessage(msg_type=MessageType.PAYLOAD, payload=batch.SerializeToString())
    )
    await asyncio.gather(*tasks)
    tasks.clear()


def assert_no_transport_writes(client):
    for method in (
        "seek",
        "play",
        "pause",
        "pause_detailed",
        "stop",
        "set_av_transport_uri",
        "clear_queue",
        "add_uri_to_queue",
        "remove_track_from_queue",
        "set_next_av_transport_uri",
        "set_volume",
    ):
        getattr(client, method).assert_not_awaited()
