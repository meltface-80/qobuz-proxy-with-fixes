"""Renderer relocation by UDN, and the audio proxy's size and event handling."""

from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer

from qobuz_proxy.backends.dlna import backend as backend_mod
from qobuz_proxy.backends.dlna.backend import DLNABackend
from qobuz_proxy.backends.dlna.client import DLNAClientError
from qobuz_proxy.backends.dlna.description import DLNADeviceInfo
from qobuz_proxy.backends.dlna.discovery import DiscoveredDevice
from qobuz_proxy.backends.dlna.proxy_server import AudioProxyServer

UDN = "uuid:renderer-1"


def _client_factory(answers: dict[str, object]):
    """DLNAClient stand-in: connect() answers per IP (an info, or an exception)."""

    def make(ip, port, description_url=None):
        client = MagicMock()
        client.ip = ip
        answer = answers[ip]
        if isinstance(answer, Exception):
            client.connect = AsyncMock(side_effect=answer)
        else:
            client.connect = AsyncMock(return_value=answer)
            client.device_info = answer
        client.disconnect = AsyncMock()
        client.get_protocol_info = AsyncMock(return_value=None)
        return client

    return make


async def test_unreachable_renderer_is_found_again_by_udn(tmp_path, monkeypatch):
    info = DLNADeviceInfo(friendly_name="Desk", udn=UDN, av_transport_url="http://x/avt")
    monkeypatch.setattr(
        backend_mod,
        "DLNAClient",
        _client_factory({"10.0.0.5": DLNAClientError("timeout"), "10.0.0.77": info}),
    )
    found = DiscoveredDevice("Desk", "10.0.0.77", 49152, udn=UDN, location="http://10.0.0.77/d")
    find = AsyncMock(return_value=found)
    monkeypatch.setattr(backend_mod.DLNADiscovery, "find", find)

    b = DLNABackend("10.0.0.5", port=49152, udn=UDN)
    b._quirk_store = MagicMock(get=MagicMock(return_value={}))
    with patch.object(b, "_poll_state_loop", AsyncMock()):
        assert await b.connect() is True
    find.assert_awaited_once()
    assert b.location["ip"] == "10.0.0.77"
    assert b.location["description_url"] == "http://10.0.0.77/d"
    await b.disconnect()


async def test_without_udn_an_unreachable_renderer_stays_unreachable(monkeypatch):
    monkeypatch.setattr(
        backend_mod, "DLNAClient", _client_factory({"10.0.0.5": DLNAClientError("timeout")})
    )
    find = AsyncMock()
    monkeypatch.setattr(backend_mod.DLNADiscovery, "find", find)
    assert await DLNABackend("10.0.0.5").connect() is False
    find.assert_not_called()


async def test_other_device_at_the_address_triggers_a_search(monkeypatch):
    other = DLNADeviceInfo(friendly_name="Other", udn="uuid:other", av_transport_url="http://x")
    mine = DLNADeviceInfo(friendly_name="Desk", udn=UDN, av_transport_url="http://y")
    monkeypatch.setattr(
        backend_mod, "DLNAClient", _client_factory({"10.0.0.5": other, "10.0.0.9": mine})
    )
    found = DiscoveredDevice("Desk", "10.0.0.9", 80, udn=UDN, location="http://10.0.0.9/d")
    monkeypatch.setattr(backend_mod.DLNADiscovery, "find", AsyncMock(return_value=found))
    b = DLNABackend("10.0.0.5", port=80, udn=UDN)
    b._quirk_store = MagicMock(get=MagicMock(return_value={}))
    with patch.object(b, "_poll_state_loop", AsyncMock()):
        assert await b.connect() is True
    assert b.udn == UDN and b.name == "Desk"
    await b.disconnect()


class _Provider:
    async def get_streaming_url(self, track_id, force=False):
        return "http://unused"


async def test_proxy_learns_size_and_answers_notify():
    proxy = AudioProxyServer(_Provider(), host="127.0.0.1", port=0)
    proxy.register_track("1", "http://cdn/1.flac")
    proxy._tracks["1"].size = 1234
    assert proxy.known_size("1") == 1234
    # Re-registering the same track keeps what was learned about it.
    proxy.register_track("1", "http://cdn/1-fresh.flac")
    assert proxy.known_size("1") == 1234

    seen = []
    url = proxy.register_event_handler("tok", lambda sid, body: seen.append((sid, body)) or True)
    assert url.endswith("/upnp/event/tok")

    from aiohttp import web

    app = web.Application()
    app.router.add_route("NOTIFY", "/upnp/event/{token}", proxy._handle_notify)
    async with TestClient(TestServer(app)) as client:
        resp = await client.request("NOTIFY", "/upnp/event/tok", data="<x/>", headers={"SID": "s"})
        assert resp.status == 200
        resp = await client.request("NOTIFY", "/upnp/event/unknown", data="<x/>")
        assert resp.status == 412
    assert seen == [("s", "<x/>")]


def test_proxy_keeps_a_bounded_registry():
    from qobuz_proxy.backends.dlna import proxy_server

    proxy = AudioProxyServer(_Provider(), host="127.0.0.1", port=0)
    for i in range(proxy_server.MAX_REGISTERED_TRACKS + 10):
        proxy.register_track(str(i), f"http://cdn/{i}")
    assert len(proxy._tracks) == proxy_server.MAX_REGISTERED_TRACKS
    assert "0" not in proxy._tracks and str(proxy_server.MAX_REGISTERED_TRACKS + 9) in proxy._tracks


def test_full_size_from_upstream_answers():
    from qobuz_proxy.backends.dlna.proxy_http import full_size

    assert full_size(200, {"Content-Length": "500"}) == 500
    assert full_size(206, {"Content-Length": "100", "Content-Range": "bytes 0-99/500"}) == 500
    assert full_size(206, {"Content-Range": "bytes 0-99/*"}) is None
    assert full_size(200, {}) is None
