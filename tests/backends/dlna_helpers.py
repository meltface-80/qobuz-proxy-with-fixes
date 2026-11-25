"""Shared helpers for DLNA backend tests: a scripted renderer and poll driver."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from qobuz_proxy.backends.dlna import backend as backend_mod
from qobuz_proxy.backends.dlna.backend import DLNABackend
from qobuz_proxy.backends.dlna.client import DLNAClient, SoapResult
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
