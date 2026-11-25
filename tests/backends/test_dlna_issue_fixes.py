"""Regressions for upstream issues #43, #44, #51 and #52, and the review's
gapless/end-detection follow-ups (SCPD gate, proxy evidence for early switches)."""

from unittest.mock import AsyncMock, MagicMock

from qobuz_proxy.backends.dlna.client import SoapResult
from qobuz_proxy.backends.dlna.description import DLNADeviceInfo
from qobuz_proxy.backends.types import PlaybackState
from tests.backends.dlna_helpers import CURRENT, NEXT, UDN, make_backend, poll

# ------------------------------------------------------------- #51 failed reads


async def test_failed_state_read_is_not_a_stop(store, clock, monkeypatch):
    b = make_backend(store, clock)
    b._furthest_position_ms = 199_000  # in the natural-end window
    b._client.get_transport_info.return_value = None  # SOAP timeout
    await poll(b, monkeypatch, times=3)
    b.ended.assert_not_called()
    assert b._state == PlaybackState.PLAYING
    assert await b.get_state() == PlaybackState.PLAYING


async def test_real_stop_after_failed_reads_still_ends_track(store, clock, monkeypatch):
    b = make_backend(store, clock)
    b._furthest_position_ms = 199_000
    b._client.get_transport_info.side_effect = [None, "STOPPED"]
    await poll(b, monkeypatch, times=2)
    b.ended.assert_called_once()


# ------------------------------------------------------------- #52 one end per attempt


async def test_failed_stream_ends_the_track_once(store, clock, monkeypatch):
    b = make_backend(store, clock)
    b._delivery_failures["123"] = True
    # The renderer then reports PLAYING and STOPPED for the same URL.
    b._client.get_transport_info.side_effect = ["PLAYING", "PLAYING", "STOPPED", "STOPPED"]
    b._furthest_position_ms = 199_000
    await poll(b, monkeypatch, times=4)
    b.ended.assert_called_once()


async def test_stalled_end_is_not_followed_by_a_second_end(store, clock, monkeypatch):
    b = make_backend(store, clock)
    b._client.get_position_info.return_value = 199_500
    b._delivered_keys.add("123")
    await poll(b, monkeypatch)
    clock.now += 3
    await poll(b, monkeypatch)
    b.ended.assert_called_once()
    # The renderer finally reports STOPPED for the same track.
    b._client.get_transport_info.return_value = "STOPPED"
    await poll(b, monkeypatch, times=2)
    b.ended.assert_called_once()


async def test_new_track_can_end_again(store, clock, monkeypatch):
    b = make_backend(store, clock)
    b._end_handled_for = CURRENT
    b._reset_track_tracking()
    b._furthest_position_ms = 199_000
    b._client.get_transport_info.return_value = "STOPPED"
    await poll(b, monkeypatch)
    b.ended.assert_called_once()


# ------------------------------------------------------------- #43 SoundTouch


async def test_empty_current_uri_falls_back_to_track_uri(store, clock):
    b = make_backend(store, clock, manufacturer="Bose", model="SoundTouch 10")
    b._client.get_media_info.return_value = ""
    b._client.get_track_uri.return_value = CURRENT
    b._client.pause_detailed.return_value = SoapResult(success=True)
    await b.pause()
    b._client.pause_detailed.assert_awaited_once()
    assert not b._external_playback


async def test_qplay_placeholder_after_our_stop_is_idle_not_takeover(store, clock):
    b = make_backend(store, clock, manufacturer="Bose", model="SoundTouch 10")
    b._client.stop.return_value = True
    await b.stop()
    b._client.get_media_info.return_value = "qplay://"
    b._client.get_track_uri.return_value = "http://invalidHost.com/invalidUri.mp3"
    b._client.get_transport_info.return_value = "PAUSED_PLAYBACK"
    assert await b.check_external_playback() is False
    assert await b.can_apply_remote_state() is True
    assert not b._external_playback


async def test_qplay_at_natural_end_with_our_track_uri_is_ours(store, clock):
    b = make_backend(store, clock)
    b._client.get_media_info.return_value = "qplay://"
    b._client.get_track_uri.return_value = CURRENT
    assert await b._owns_transport() is True


async def test_qplay_without_our_stop_is_still_a_takeover(store, clock):
    """Anything but our own Stop or our TrackURI still fails closed."""
    b = make_backend(store, clock)
    b._client.get_media_info.return_value = "qplay://"
    b._client.get_track_uri.return_value = "http://elsewhere/x.mp3"
    assert await b.check_external_playback() is True


def test_soundtouch_profile():
    from qobuz_proxy.backends.dlna.quirks import resolve_quirks

    q = resolve_quirks("uuid:x", "Bose Corporation", "SoundTouch 20")
    assert q.gapless is False and q.max_quality == 6


# ------------------------------------------------------------- #44 Pause refused


async def test_refused_pause_stops_and_resume_reloads_at_position(store, clock, monkeypatch):
    monkeypatch.setattr("qobuz_proxy.backends.dlna.backend_base.asyncio.sleep", AsyncMock())
    b = make_backend(store, clock)
    states = []
    b.on_state_change(states.append)
    b._position_ms = 42_000
    b._client.pause_detailed.return_value = SoapResult(success=False, error_code=401)
    b._client.stop.return_value = True
    await b.pause()
    b._client.stop.assert_awaited_once()
    assert states[-1] == PlaybackState.PAUSED
    assert store.get(UDN)["can_pause"] is False

    # Stopped on purpose: the poll loop must not report a track end or STOPPED.
    b._client.get_transport_info.return_value = "STOPPED"
    await poll(b, monkeypatch)
    b.ended.assert_not_called()
    assert states[-1] == PlaybackState.PAUSED

    # upmpdcli 1.9 clears CurrentURI on Stop; resume must still reload.
    b._client.get_media_info.return_value = ""
    b._client.get_track_uri.return_value = ""
    b._client.get_transport_info.side_effect = ["STOPPED"] + ["PLAYING"] * 5
    assert await b.resume() is True
    b._client.set_av_transport_uri.assert_awaited()
    b._client.seek.assert_awaited_once_with(42_000)
    assert states[-1] == PlaybackState.PLAYING


async def test_known_non_pausing_renderer_goes_straight_to_stop(store, clock):
    b = make_backend(store, clock)
    store.learn(UDN, "can_pause", False)
    b._apply_quirks(b._client.device_info)
    b._client.stop.return_value = True
    await b.pause()
    b._client.pause_detailed.assert_not_called()
    b._client.stop.assert_awaited_once()


async def test_transient_pause_failure_does_not_stop(store, clock):
    b = make_backend(store, clock)
    b._client.pause_detailed.return_value = SoapResult(success=False, error_code=501)
    await b.pause()
    b._client.stop.assert_not_called()


async def test_seek_while_paused_by_stop_applies_on_resume(store, clock):
    b = make_backend(store, clock)
    b._paused_by_stop_ms = 10_000
    await b.seek(90_000)
    assert b._paused_by_stop_ms == 90_000
    b._client.seek.assert_not_called()


def test_bare_sink_entry_declares_byte_ranges():
    """Upstream PR #45: a bare '*' entry made the NAD treat the stream as live."""
    from qobuz_proxy.backends.dlna.capabilities import (
        build_protocol_info,
        parse_protocol_info_sink,
    )

    caps = parse_protocol_info_sink("http-get:*:audio/flac:*")
    assert build_protocol_info(caps, "audio/flac") == "http-get:*:audio/flac:DLNA.ORG_OP=01"
    caps = parse_protocol_info_sink("http-get:*:audio/flac:DLNA.ORG_PN=FLAC;DLNA.ORG_OP=11")
    assert build_protocol_info(caps, "audio/flac").endswith("DLNA.ORG_PN=FLAC;DLNA.ORG_OP=11")


# ------------------------------------------------------------- gapless gates


async def test_scpd_without_setnext_turns_gapless_off(store, clock):
    b = make_backend(store, clock)
    info = DLNADeviceInfo(
        udn=UDN, manufacturer="Acme", model_name="X", av_transport_actions=frozenset({"Play"})
    )
    b._apply_quirks(info)
    assert b.supports_gapless is False
    assert b.quirks.sources["gapless"] == "service description"


async def test_user_gapless_on_beats_scpd(store, clock):
    b = make_backend(store, clock, gapless="on")
    info = DLNADeviceInfo(udn=UDN, av_transport_actions=frozenset({"Play"}))
    b._apply_quirks(info)
    assert b.supports_gapless is True


async def test_position_jump_without_next_fetch_is_not_an_early_switch(store, clock, monkeypatch):
    """With the proxy in the path, a renderer cannot play a track it never fetched."""
    b = make_backend(store, clock)
    b._proxy_server = MagicMock()
    b._next_track_proxy_url = NEXT
    b._furthest_position_ms = 60_000
    b._client.get_position_info.return_value = 1_000  # user restarted the track
    await poll(b, monkeypatch)
    assert b.supports_gapless is True
    b._client.set_av_transport_uri.assert_not_called()

    b._requested_keys.add("456_2")  # now it did fetch the queued track
    b._furthest_position_ms = 60_000
    await poll(b, monkeypatch)
    assert b.supports_gapless is False


async def test_stall_check_waits_after_a_seek(store, clock, monkeypatch):
    b = make_backend(store, clock)
    b._client.get_position_info.return_value = 199_500
    b._delivered_keys.add("123")
    b._last_seek_at = clock.now
    await poll(b, monkeypatch)
    clock.now += 3
    await poll(b, monkeypatch)
    b.ended.assert_not_called()


# ------------------------------------------------------------- quality from profile


async def test_profile_quality_when_sink_lists_bare_flac(store, clock):
    from qobuz_proxy.backends.dlna.capabilities import parse_protocol_info_sink

    b = make_backend(store, clock, manufacturer="FiiO", model="K17")
    b._capabilities = parse_protocol_info_sink("http-get:*:audio/flac:*")
    assert b.get_recommended_quality() == 27
    assert b.quality_detection_confirmed is True


async def test_stated_formats_beat_the_profile(store, clock):
    from qobuz_proxy.backends.dlna.capabilities import parse_protocol_info_sink

    b = make_backend(store, clock, manufacturer="FiiO", model="K17")
    b._capabilities = parse_protocol_info_sink("http-get:*:audio/flac:DLNA.ORG_PN=FLAC")
    assert b.get_recommended_quality() == 6


def test_pcm_formats_vouch_for_bare_flac():
    from qobuz_proxy.backends.dlna.capabilities import parse_protocol_info_sink

    caps = parse_protocol_info_sink(
        "http-get:*:audio/flac:*,http-get:*:audio/L24;rate=192000;channels=2:*"
    )
    assert caps.max_quality == 27 and caps.format_info_confirmed


# ------------------------------------------------------------- events


async def test_transport_event_wakes_the_poll_loop(store, clock):
    b = make_backend(store, clock)
    b._on_event({"TransportState": "STOPPED"})
    assert b._poll_wake.is_set()


def test_parse_last_change():
    from qobuz_proxy.backends.dlna.events import parse_last_change

    inner = (
        '&lt;Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/"&gt;'
        '&lt;InstanceID val="0"&gt;&lt;TransportState val="PLAYING"/&gt;'
        "&lt;/InstanceID&gt;&lt;/Event&gt;"
    )
    body = (
        '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0"><e:property>'
        f"<LastChange>{inner}</LastChange></e:property></e:propertyset>"
    )
    assert parse_last_change(body) == {"TransportState": "PLAYING"}
    assert parse_last_change("not xml<") == {}
