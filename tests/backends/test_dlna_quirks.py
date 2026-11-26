"""Renderer quirks: profiles, learned behaviour, and the poll-loop handling of
renderers that switch to a queued track at once, name it early, or never
report STOPPED at the end of a track (FiiO K17 / SR11 reports)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from qobuz_proxy.backends.dlna import backend as backend_mod
from qobuz_proxy.backends.dlna.backend import (
    ARM_AFTER_PLAYING_SECONDS,
    ARM_AFTER_POSITION_MS,
    END_STALL_AT_END_SECONDS,
    END_STALL_DELIVERED_SECONDS,
    END_STALL_KNOWN_SECONDS,
    END_STALL_SECONDS,
    DLNABackend,
)
from qobuz_proxy.backends.dlna.client import DLNAClient, SoapResult
from qobuz_proxy.backends.dlna.proxy_server import _dlna_headers, _reaches_end_of_file
from qobuz_proxy.backends.dlna.quirks import (
    QuirkStore,
    parse_gapless_mode,
    profile_for,
    resolve_quirks,
)
from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState

CURRENT = "http://proxy:7120/audio/123.flac"
NEXT = "http://proxy:7120/audio/456_2.flac"
UDN = "uuid:k17-test"


class Clock:
    """Stands in for time.monotonic() in the backend module."""

    def __init__(self, now: float = 1_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(backend_mod.time, "monotonic", c)
    return c


@pytest.fixture
def store(tmp_path):
    return QuirkStore(tmp_path / "dlna_quirks.json")


def make_backend(store, clock, gapless="auto", manufacturer="Acme", model="Streamer"):
    b = DLNABackend("192.0.2.9", name="Desk", gapless=gapless, quirk_store=store)
    client = AsyncMock(spec=DLNAClient)
    client.device_info = SimpleNamespace(udn=UDN, manufacturer=manufacturer, model_name=model)
    client.get_media_info.return_value = CURRENT
    client.get_transport_info.return_value = "PLAYING"
    client.get_position_info.return_value = 0
    client.set_av_transport_uri.return_value = True
    client.play.return_value = True
    client.seek.return_value = True
    client.set_next_av_transport_uri.return_value = SoapResult(success=True)
    b._client = client
    b._apply_quirks(client.device_info)
    b._current_proxy_url = CURRENT
    b._current_metadata = BackendTrackMetadata(
        track_id="123", title="T", artist="A", album="B", duration_ms=200_000
    )
    b._duration_ms = 200_000
    b._state = PlaybackState.PLAYING
    b._playback_started_at = clock.now - 60  # past the start grace period
    b.ended = MagicMock()
    b.next_started = MagicMock()
    b.on_track_ended(b.ended)
    b.on_next_track_started(b.next_started)
    return b


async def poll(b, monkeypatch, times=1):
    b._is_connected = True
    ticks = 0

    async def tick(_):
        nonlocal ticks
        ticks += 1
        if ticks > times:
            b._is_connected = False

    monkeypatch.setattr(backend_mod.asyncio, "sleep", tick)
    await b._poll_state_loop()


# --------------------------------------------------------------- layers


def test_parse_gapless_mode():
    assert parse_gapless_mode(False) == "off"
    assert parse_gapless_mode(True) == "on"
    assert parse_gapless_mode("OFF") == "off"
    assert parse_gapless_mode("disabled") == "off"
    assert parse_gapless_mode("") == "auto"
    assert parse_gapless_mode("nonsense") == "auto"


def test_k17_profile():
    p = profile_for("FiiO", "K17")
    assert p is not None and p.id == "fiio-k17"
    q = resolve_quirks("uuid:x", "FiiO", "FiiO K17")
    assert q.gapless is False and q.stops_at_end is False
    assert q.sources["gapless"] == "profile:fiio-k17"


def test_unknown_renderer_gets_spec_defaults():
    q = resolve_quirks("uuid:x", "Acme", "Streamer")
    assert q.gapless is None and q.stops_at_end is None
    assert q.describe() == "spec defaults"


def test_learned_overrides_profile_and_user_overrides_learned(store):
    store.learn("uuid:x", "gapless", True)
    assert resolve_quirks("uuid:x", "FiiO", "K17", store=store).gapless is True
    q = resolve_quirks("uuid:x", "FiiO", "K17", gapless_mode="off", store=store)
    assert q.gapless is False and q.sources["gapless"] == "user"


def test_store_survives_reload(store):
    assert store.learn(UDN, "stops_at_end", False, label="Desk")
    assert not store.learn(UDN, "stops_at_end", False)  # unchanged
    again = QuirkStore(store.path)
    assert again.get(UDN)["stops_at_end"] is False


def test_profile_turns_gapless_off(store, clock):
    b = make_backend(store, clock, manufacturer="FiiO", model="K17")
    assert b.supports_gapless is False


# --------------------------------------------------------------- arming


async def test_no_arming_until_renderer_heard_playing(store, clock):
    b = make_backend(store, clock)
    meta = BackendTrackMetadata(track_id="456", title="N", artist="A", album="B", duration_ms=1)
    assert b.ready_for_next_track is False
    assert await b.set_next_track(NEXT, meta, 2) is False
    b._client.set_next_av_transport_uri.assert_not_called()

    b._note_playing(ARM_AFTER_POSITION_MS - 1_000)
    assert b.ready_for_next_track is False
    b._note_playing(ARM_AFTER_POSITION_MS)
    assert b.ready_for_next_track is True
    assert await b.set_next_track(NEXT, meta, 2) is True
    b._client.set_next_av_transport_uri.assert_called_once()


def test_renderer_without_positions_confirmed_by_time(store, clock):
    b = make_backend(store, clock)
    b._note_playing(None)
    assert not b._playing_confirmed
    clock.now += ARM_AFTER_PLAYING_SECONDS
    b._note_playing(None)
    assert b._playing_confirmed


async def test_poll_confirms_playing(store, clock, monkeypatch):
    b = make_backend(store, clock)
    b._client.get_position_info.return_value = 5_000
    await poll(b, monkeypatch)
    assert b.ready_for_next_track


# --------------------------------------------------------------- end without STOPPED


def parked(b, clock, seconds, pos=200_000):
    """Report a stalled position (default: at the track's end) for ``seconds``."""
    assert not b._ended_without_stop(pos)
    clock.now += seconds
    return b._ended_without_stop(pos)


@pytest.mark.parametrize(
    "pos, delivered, known, needed",
    [
        # Parked at the length (where the K17 parks): short wait with evidence.
        (200_000, True, False, END_STALL_AT_END_SECONDS),
        (200_000, False, True, END_STALL_AT_END_SECONDS),
        (200_000, False, False, END_STALL_SECONDS),
        # Parked just short of it (Qobuz's length can differ by a second).
        (199_000, True, False, END_STALL_DELIVERED_SECONDS),
        (199_000, False, True, END_STALL_KNOWN_SECONDS),
        (199_000, False, False, END_STALL_SECONDS),
    ],
)
def test_end_stall_thresholds(store, clock, pos, delivered, known, needed):
    b = make_backend(store, clock)
    if delivered:
        b._on_delivery_complete("123")
    if known:
        b._quirks.stops_at_end = False
    assert not parked(b, clock, needed - 0.5, pos)
    clock.now += 0.5
    assert b._ended_without_stop(pos)


def test_moving_position_near_end_is_not_a_stall(store, clock):
    b = make_backend(store, clock)
    b._on_delivery_complete("123")
    for pos in (198_000, 199_000, 200_000):
        assert not b._ended_without_stop(pos)
        clock.now += 1


def test_position_far_past_end_ends_track(store, clock):
    b = make_backend(store, clock)
    assert b._ended_without_stop(204_000)


async def test_poll_reports_stalled_end_once_and_learns(store, clock, monkeypatch):
    b = make_backend(store, clock)
    b._on_delivery_complete("123")
    b._client.get_position_info.return_value = 200_000
    await poll(b, monkeypatch)  # stall starts
    clock.now += END_STALL_DELIVERED_SECONDS
    await poll(b, monkeypatch)  # stall long enough: track end
    b.ended.assert_called_once()
    assert store.get(UDN)["stops_at_end"] is False
    clock.now += 30
    await poll(b, monkeypatch, times=3)  # still parked: no second end
    b.ended.assert_called_once()


# --------------------------------------------------------------- early URI


async def test_next_uri_named_mid_track_is_not_a_transition(store, clock, monkeypatch):
    b = make_backend(store, clock)
    b._next_track_proxy_url = NEXT
    b._next_track_metadata = MagicMock(duration_ms=180_000)
    b._furthest_position_ms = 30_000
    b._client.get_media_info.return_value = NEXT
    b._client.get_position_info.return_value = 32_000
    await poll(b, monkeypatch)
    b.next_started.assert_not_called()
    assert b._current_proxy_url == CURRENT
    assert store.get(UDN)["reports_next_early"] is True

    # Later the position goes back to the start: the renderer moved on.
    b._client.get_position_info.return_value = 1_000
    await poll(b, monkeypatch)
    b.next_started.assert_called_once()
    assert b._current_proxy_url == NEXT


# --------------------------------------------------------------- early switch


async def test_early_switch_turns_gapless_off_and_restores_track(store, clock, monkeypatch):
    b = make_backend(store, clock)
    b._next_track_proxy_url = NEXT
    b._next_armed_at = clock.now - 5
    b._furthest_position_ms = 60_000
    b._client.get_position_info.return_value = 1_000  # URI unchanged, back at 0:01
    await poll(b, monkeypatch, times=3)

    assert store.get(UDN)["gapless"] is False
    assert b.supports_gapless is False
    assert b._next_track_proxy_url is None
    b._client.set_av_transport_uri.assert_called_once()
    assert b._client.set_av_transport_uri.call_args.args[0] == CURRENT
    b._client.seek.assert_called_once_with(60_000)
    b.ended.assert_not_called()


async def test_early_switch_right_after_arming(store, clock, monkeypatch):
    """Armed at 0:03, the renderer jumps straight to the queued track."""
    b = make_backend(store, clock)
    b._next_track_proxy_url = NEXT
    b._furthest_position_ms = 4_000
    b._client.get_position_info.return_value = 0
    await poll(b, monkeypatch, times=3)
    assert store.get(UDN)["gapless"] is False
    # Little to lose this early: the track restarts rather than seek on a
    # renderer that just misbehaved (a K17 crashed after reload + Seek).
    b._client.set_av_transport_uri.assert_called_once()
    b._client.seek.assert_not_called()


async def test_armed_next_waits_full_stall_time(store, clock):
    b = make_backend(store, clock)
    b._on_delivery_complete("123")
    b._next_track_proxy_url = NEXT
    assert not parked(b, clock, END_STALL_DELIVERED_SECONDS)
    clock.now += END_STALL_SECONDS
    assert b._ended_without_stop(200_000)


async def test_early_switch_ignored_right_after_our_seek(store, clock, monkeypatch):
    b = make_backend(store, clock)
    b._next_track_proxy_url = NEXT
    b._furthest_position_ms = 60_000
    b._last_seek_at = clock.now - 1
    b._client.get_position_info.return_value = 1_000
    await poll(b, monkeypatch)
    b._client.set_av_transport_uri.assert_not_called()


async def test_user_forced_gapless_is_not_overridden(store, clock, monkeypatch):
    b = make_backend(store, clock, gapless="on")
    b._next_track_proxy_url = NEXT
    b._furthest_position_ms = 60_000
    b._client.get_position_info.return_value = 1_000
    await poll(b, monkeypatch)
    assert b.supports_gapless is True
    assert "gapless" not in store.get(UDN)
    b._client.set_av_transport_uri.assert_not_called()


# --------------------------------------------------------------- ignored SetNext


async def test_stop_with_next_long_armed_learns_gapless_off(store, clock, monkeypatch):
    b = make_backend(store, clock)
    b._next_track_proxy_url = NEXT
    b._next_armed_at = clock.now - 60
    b._furthest_position_ms = 199_000
    b._client.get_transport_info.return_value = "STOPPED"
    await poll(b, monkeypatch)
    b.ended.assert_called_once()
    assert store.get(UDN)["gapless"] is False


# --------------------------------------------------------------- proxy helpers


def test_reaches_end_of_file():
    assert _reaches_end_of_file(200, None, 1000, 1000)
    assert not _reaches_end_of_file(200, None, 1000, 400)
    assert _reaches_end_of_file(206, "bytes 500-999/1000", 500, 500)
    assert not _reaches_end_of_file(206, "bytes 0-1023/50000", 1024, 1024)
    assert not _reaches_end_of_file(206, "bytes 0-99/*", 100, 100)


def test_dlna_headers_only_when_asked():
    asked = SimpleNamespace(
        headers={"getcontentFeatures.dlna.org": "1", "transferMode.dlna.org": "Streaming"}
    )
    headers = _dlna_headers(asked)  # type: ignore[arg-type]
    assert headers["contentFeatures.dlna.org"].startswith("DLNA.ORG_OP=01")
    assert headers["transferMode.dlna.org"] == "Streaming"
    assert _dlna_headers(SimpleNamespace(headers={})) == {}  # type: ignore[arg-type]


# --------------------------------------------------------------- real FiiO K17
# From hand-sent SOAP and a live run against a real K17: it describes itself
# as stock gmediarender, replaces the playing track as soon as a next one is
# queued, and keeps a queued URI through Stop until an empty
# SetNextAVTransportURI clears it.

K17 = {
    "manufacturer": "Ivo Clarysse, Henner Zeller",
    "model": "FIIO K17",
    "friendly_name": "FIIO K17",
    "udn": "uuid:FiiORender-1234",
}


def test_k17_profile_matches_the_real_description():
    p = profile_for(K17["manufacturer"], K17["model"], K17["friendly_name"], K17["udn"])
    assert p is not None and p.id == "fiio-k17"
    # A plain gmediarender, or another brand's "K17", is not a FiiO K17.
    assert profile_for(K17["manufacturer"], "gmediarender", "Living Room", "uuid:a") is None
    assert profile_for("Acme", "K17", "K17", "uuid:a") is None


async def test_real_k17_never_receives_set_next(store, clock):
    b = make_backend(store, clock, manufacturer=K17["manufacturer"], model=K17["model"])
    assert b.supports_gapless is False
    b._playing_confirmed = True
    meta = BackendTrackMetadata(track_id="456", title="N", artist="A", album="B", duration_ms=1)
    assert await b.set_next_track(NEXT, meta, 2) is False
    b._client.set_next_av_transport_uri.assert_not_called()


async def test_early_switch_clears_the_queued_uri_first(store, clock, monkeypatch):
    b = make_backend(store, clock)
    b._next_track_proxy_url = NEXT
    b._furthest_position_ms = 60_000
    b._client.get_position_info.return_value = 1_000
    await poll(b, monkeypatch)
    b._client.set_next_av_transport_uri.assert_awaited_with("", "")


async def test_clearing_the_next_track_clears_it_on_the_renderer(store, clock):
    b = make_backend(store, clock)
    b._next_track_proxy_url = NEXT
    await b.clear_next_track()
    b._client.set_next_av_transport_uri.assert_awaited_once_with("", "")


async def test_reload_reports_where_the_renderer_really_is(store, clock, monkeypatch):
    """The K17 accepted the Seek after a reload but played from 0:00."""
    b = make_backend(store, clock)
    positions = []
    b.on_position_update(positions.append)
    b._next_track_proxy_url = NEXT
    b._furthest_position_ms = 90_000
    b._client.get_position_info.side_effect = [1_000] + [3_000] * 20
    await poll(b, monkeypatch)
    b._client.seek.assert_awaited_once_with(90_000)
    assert positions[-1] == 3_000
