"""Transport commands only touch the renderer while it plays a Qobuz track."""

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from qobuz_proxy.backends.dlna.backend import _source_label
from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState
from tests.integration.conftest import (
    NEXT_URI,
    QOBUZ_URI,
    SPOTIFY_URI,
    assert_no_transport_writes,
    deliver_snapshot,
)


@pytest.mark.parametrize("uri", [QOBUZ_URI, NEXT_URI])
async def test_current_and_gapless_qobuz_tracks_remain_controllable(rig, uri):
    backend, client, _, manager, _ = rig
    client.get_track_uri.return_value = uri
    await backend.seek(4000)
    assert await backend.resume()
    await backend.pause()
    client.seek.assert_awaited_once_with(4000)
    client.play.assert_awaited_once()
    client.pause_detailed.assert_awaited_once()
    assert manager.is_renderer_active


@pytest.mark.parametrize("uri", [SPOTIFY_URI, None, ""])
async def test_transport_and_queue_commands_fail_closed_for_foreign_or_unknown_uri(rig, uri):
    backend, client, _, _, _ = rig
    client.get_track_uri.return_value = uri
    with pytest.raises(RuntimeError, match="Cannot seek"):
        await backend.seek(4000)
    await backend.pause()
    assert not await backend.resume()
    metadata = BackendTrackMetadata(track_id="456", title="Next", artist="Artist")
    assert not await backend.set_next_track(NEXT_URI, metadata)
    await backend.clear_next_track()
    await backend.stop()
    await backend.disconnect()
    assert_no_transport_writes(client)
    client.disconnect.assert_awaited_once()
    assert backend._external_playback is (uri == SPOTIFY_URI)


async def test_source_change_during_poll_does_not_advance_qobuz_queue(rig, monkeypatch):
    backend, client, player, manager, _ = rig
    player._state = PlaybackState.PLAYING
    backend._state = PlaybackState.PLAYING
    client.get_transport_info.return_value = "STOPPED"
    ended = MagicMock()
    backend.on_track_ended(ended)
    backend._is_connected = True
    ticks = 0

    async def tick(_):
        nonlocal ticks
        ticks += 1
        if ticks == 2:
            backend._is_connected = False

    monkeypatch.setattr("qobuz_proxy.backends.dlna.backend.asyncio.sleep", tick)
    await backend._poll_state_loop()
    ended.assert_not_called()
    assert_no_transport_writes(client)
    assert player.state == PlaybackState.STOPPED
    assert not manager.is_renderer_active


async def test_standard_dlna_uses_media_uri(rig):
    backend, client, _, _, _ = rig
    backend._is_sonos = False
    client.get_media_info.return_value = SPOTIFY_URI
    assert await backend.check_external_playback()
    client.get_media_info.assert_awaited_once()
    client.get_track_uri.assert_not_awaited()


@pytest.mark.parametrize("uri", [None, ""])
async def test_unknown_source_blocks_snapshot_without_relinquishing_session(rig, uri):
    backend, client, player, manager, tasks = rig
    client.get_track_uri.return_value = uri
    await deliver_snapshot(manager, tasks, 2)
    assert_no_transport_writes(client)
    assert not backend._external_playback
    assert manager.is_renderer_active

    # A transient read failure must not require a new device selection.
    client.get_track_uri.return_value = QOBUZ_URI
    await deliver_snapshot(manager, tasks, 3)
    client.seek.assert_awaited_once_with(4000)
    assert player.state == PlaybackState.PAUSED


async def test_pending_track_load_cannot_replace_new_external_source(rig):
    backend, client, _, _, _ = rig
    metadata = BackendTrackMetadata(track_id="456", title="Next", artist="Artist")
    with pytest.raises(RuntimeError, match="External source"):
        await backend.play(NEXT_URI, metadata)
    assert_no_transport_writes(client)


async def test_loading_old_uri_is_not_mistaken_for_external_takeover(rig):
    import time

    backend, client, _, manager, _ = rig
    backend._playback_started_at = time.monotonic()
    assert not await backend.check_external_playback()
    with pytest.raises(RuntimeError, match="Cannot seek"):
        await backend.seek(4000)
    assert_no_transport_writes(client)
    client.get_track_uri.return_value = QOBUZ_URI
    await backend.seek(4000)
    client.seek.assert_awaited_once_with(4000)
    assert manager.is_renderer_active


async def test_inflight_uri_read_cannot_revoke_a_new_transport_load(rig):
    backend, client, _, manager, _ = rig
    reading = asyncio.Event()
    finish = asyncio.Event()

    async def read_uri():
        reading.set()
        await finish.wait()
        return SPOTIFY_URI

    client.get_track_uri.side_effect = read_uri
    check = asyncio.create_task(backend.check_external_playback())
    await reading.wait()
    backend._starting_playback = True
    finish.set()
    assert not await check
    assert manager.is_renderer_active


async def test_source_mismatch_logs_track_replacement_context(rig, caplog):
    backend, client, _, _, _ = rig
    client.get_track_uri.return_value = QOBUZ_URI
    caplog.set_level(logging.INFO, logger="qobuz_proxy.backends.dlna.backend")
    await backend.stop(next_track_id="789")

    # Model a renderer reporting the formerly armed track after Stop. These
    # diagnostics must distinguish this case from a foreign Spotify/AirPlay URI.
    client.get_track_uri.return_value = NEXT_URI
    await backend.check_external_playback()
    assert "replacement_track_id=789" in caplog.text
    assert "cleared_next=http://proxy/audio/456_2.flac" in caplog.text
    assert "observed=http://proxy/audio/456_2.flac" in caplog.text
    assert "expected=http://proxy/audio/123.flac" in caplog.text
    assert "cached_state=STOPPED" in caplog.text
    assert "last_successful_stop_age_s=None" not in caplog.text


@pytest.mark.parametrize(
    ("after_stop", "transport", "stop_succeeds", "allowed"),
    [
        (QOBUZ_URI, "STOPPED", True, True),
        (SPOTIFY_URI, "STOPPED", True, False),
        (QOBUZ_URI, "PLAYING", True, False),
        (QOBUZ_URI, "STOPPED", False, False),
        ("http://proxy:7120/audio/999.flac", "STOPPED", True, False),
    ],
)
async def test_replace_album_after_sonos_stop_resets_to_queue_head(
    rig, after_stop, transport, stop_succeeds, allowed
):
    backend, client, _, manager, _ = rig
    client.get_track_uri.return_value = QOBUZ_URI
    first = BackendTrackMetadata(track_id="123", title="First", artist="Artist")
    await backend.play(QOBUZ_URI, first)

    # Several gapless transitions later, the first entry remains in Sonos's queue.
    backend._current_proxy_url = NEXT_URI
    backend._playback_started_at = 0
    client.get_track_uri.return_value = NEXT_URI

    async def stop():
        client.get_track_uri.return_value = after_stop
        client.get_transport_info.return_value = transport
        return stop_succeeds

    client.stop.side_effect = stop
    await backend.stop(next_track_id="789")
    client.play_from_queue.reset_mock()
    replacement = BackendTrackMetadata(track_id="789", title="Replacement", artist="Artist")
    if not allowed:
        with pytest.raises(RuntimeError, match="External source"):
            await backend.play("http://proxy:7120/audio/789.flac", replacement)
        client.play_from_queue.assert_not_awaited()
        assert not manager.is_renderer_active
    else:
        await backend.play("http://proxy:7120/audio/789.flac", replacement)
        client.play_from_queue.assert_awaited_once_with(0)
        assert manager.is_renderer_active
        assert not backend._external_playback

        # Loading the replacement retires the old queue head.
        backend._playback_started_at = 0
        client.get_track_uri.return_value = QOBUZ_URI
        assert await backend.check_external_playback()


async def test_source_poll_during_stop_does_not_relinquish_queue(rig):
    backend, client, _, manager, _ = rig
    client.get_track_uri.return_value = QOBUZ_URI
    await backend.play(
        QOBUZ_URI, BackendTrackMetadata(track_id="123", title="First", artist="Artist")
    )
    backend._current_proxy_url = NEXT_URI
    backend._playback_started_at = 0
    client.get_track_uri.return_value = NEXT_URI

    async def stop():
        # Sonos changes its reported URI before the Stop response reaches us.
        client.get_track_uri.return_value = QOBUZ_URI
        assert not await backend.check_external_playback()
        assert manager.is_renderer_active
        return True

    client.stop.side_effect = stop
    await backend.stop(next_track_id="789")
    client.get_transport_info.return_value = "STOPPED"
    assert await backend.can_apply_remote_state()
    assert manager.is_renderer_active


@pytest.mark.parametrize(
    "uri",
    [
        "https://user:secret@cdn.example/private-id?token=secret#secret",
        "http://user:secret@proxy/audio/123.flac?token=secret",
        "x-sonos-spotify:secret",
        "http://[invalid-secret",
    ],
)
def test_source_diagnostics_do_not_expose_credentials_or_opaque_ids(uri):
    label = _source_label(uri)
    assert "secret" not in label
    assert "private-id" not in label
    assert "sha256=" in label


@pytest.mark.parametrize(
    ("uri", "transport", "allowed"),
    [
        ("", "STOPPED", True),
        ("", "NO_MEDIA_PRESENT", True),
        ("", "PLAYING", False),  # device-internal source (AirPlay, line-in)
        (None, "STOPPED", False),  # read failed: fail closed
        (SPOTIFY_URI, "STOPPED", False),
    ],
)
async def test_idle_renderer_without_a_uri_accepts_qobuz_again(rig, uri, transport, allowed):
    """GitHub #35: upmpdcli 1.9 clears CurrentURI when Qobuz stops it on
    deactivation, and the app's commands after switching back were refused."""
    backend, client, _, _, _ = rig
    backend._is_sonos = False
    backend._next_track_proxy_url = None
    client.get_media_info.return_value = uri
    client.get_transport_info.return_value = transport
    assert await backend.can_apply_remote_state() is allowed
