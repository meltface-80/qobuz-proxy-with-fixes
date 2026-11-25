"""Device descriptions: identity from the renderer device, SCPD actions."""

from unittest.mock import AsyncMock

from qobuz_proxy.backends.dlna import transport
from qobuz_proxy.backends.dlna.client import DLNAClient
from qobuz_proxy.backends.dlna.description import (
    DLNADeviceInfo,
    parse_device_description,
    parse_scpd_actions,
)
from qobuz_proxy.backends.dlna.soap import SoapResult, time_string_to_ms

EMBEDDED = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <friendlyName>Root Box</friendlyName>
    <manufacturer>Acme</manufacturer>
    <modelName>Hub</modelName>
    <UDN>uuid:root</UDN>
    <deviceList>
      <device>
        <friendlyName>Renderer</friendlyName>
        <modelName>Hub Renderer</modelName>
        <UDN>uuid:renderer</UDN>
        <serviceList>
          <service>
            <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
            <SCPDURL>/avt/scpd.xml</SCPDURL>
            <controlURL>/avt/control</controlURL>
            <eventSubURL>/avt/event</eventSubURL>
          </service>
          <service>
            <serviceType>urn:schemas-upnp-org:service:GroupRenderingControl:1</serviceType>
            <controlURL>/grc/control</controlURL>
          </service>
          <service>
            <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
            <controlURL>/rc/control</controlURL>
          </service>
        </serviceList>
      </device>
      <device>
        <friendlyName>Media Server</friendlyName>
        <manufacturer>Other</manufacturer>
        <modelName>Server</modelName>
        <UDN>uuid:server</UDN>
        <serviceList>
          <service>
            <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
            <controlURL>/cm/control</controlURL>
          </service>
        </serviceList>
      </device>
    </deviceList>
  </device>
</root>"""

SCPD = """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <actionList>
    <action><name>Play</name></action>
    <action><name>Stop</name></action>
    <action><name>SetAVTransportURI</name></action>
  </actionList>
</scpd>"""


def test_identity_comes_from_renderer_not_later_embedded_device():
    info = parse_device_description(EMBEDDED, "http://10.0.0.5:80", "http://10.0.0.5/desc.xml")
    assert info.friendly_name == "Renderer"
    assert info.udn == "uuid:renderer"
    assert info.model_name == "Hub Renderer"
    # Missing on the renderer: filled in from the root device.
    assert info.manufacturer == "Acme"


def test_service_urls_and_event_urls_resolve():
    info = parse_device_description(EMBEDDED, "http://10.0.0.5:80", "http://10.0.0.5/desc.xml")
    assert info.av_transport_url == "http://10.0.0.5/avt/control"
    assert info.av_transport_scpd_url == "http://10.0.0.5/avt/scpd.xml"
    assert info.av_transport_event_url == "http://10.0.0.5/avt/event"
    # Standard RenderingControl wins over Sonos GroupRenderingControl.
    assert info.rendering_control_url == "http://10.0.0.5/rc/control"
    assert info.connection_manager_url == "http://10.0.0.5/cm/control"


def test_unparseable_description_returns_empty_info():
    assert parse_device_description("<not xml", "") == DLNADeviceInfo()


def test_scpd_actions():
    actions = parse_scpd_actions(SCPD)
    assert actions == frozenset({"Play", "Stop", "SetAVTransportURI"})
    info = DLNADeviceInfo(av_transport_actions=actions)
    assert info.supports_action("SetNextAVTransportURI") is False
    assert DLNADeviceInfo().supports_action("Play") is None
    assert parse_scpd_actions("<scpd/>") is None
    assert parse_scpd_actions("garbage<") is None


def test_time_strings():
    assert time_string_to_ms("0:00:01.5") == 1500
    assert time_string_to_ms(None) is None
    assert time_string_to_ms("-0:00:01") is None


class _ErrorSession:
    """A session whose every POST answers a UPnP error."""

    def __init__(self, code: int):
        self.code = code
        self.posts = 0

    def post(self, *args, **kwargs):
        session = self

        class _Resp:
            status = 500

            async def __aenter__(self):
                session.posts += 1
                return self

            async def __aexit__(self, *exc):
                return False

            async def text(self):
                return (
                    f"<Envelope><errorCode>{session.code}</errorCode>"
                    "<errorDescription>nope</errorDescription></Envelope>"
                )

        return _Resp()


async def test_seek_mode_not_supported_is_not_retried(monkeypatch):
    monkeypatch.setattr(transport, "RETRY_DELAY_SECONDS", 0)
    client = DLNAClient("10.0.0.5")
    client.device_info = DLNADeviceInfo(av_transport_url="http://10.0.0.5/avt")
    session = _ErrorSession(710)
    client._session = session  # type: ignore[assignment]
    assert await client.seek(30_000) is False
    assert session.posts == 1


async def test_transient_error_is_retried(monkeypatch):
    monkeypatch.setattr(transport, "RETRY_DELAY_SECONDS", 0)
    client = DLNAClient("10.0.0.5")
    client.device_info = DLNADeviceInfo(av_transport_url="http://10.0.0.5/avt")
    session = _ErrorSession(701)
    client._session = session  # type: ignore[assignment]
    assert await client.seek(30_000) is False
    assert session.posts == transport.MAX_RETRIES


async def test_pause_detailed_keeps_error_code():
    client = DLNAClient("10.0.0.5")
    client.device_info = DLNADeviceInfo(av_transport_url="http://10.0.0.5/avt")
    client._soap_action_detailed = AsyncMock(  # type: ignore[method-assign]
        return_value=SoapResult(success=False, error_code=401)
    )
    result = await client.pause_detailed()
    assert result.error_code == 401 and result.is_permanent_failure


async def test_position_details_from_one_call():
    client = DLNAClient("10.0.0.5")
    client.device_info = DLNADeviceInfo(av_transport_url="http://10.0.0.5/avt")
    client._soap_action = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            "<r><TrackDuration>0:03:20</TrackDuration><RelTime>0:01:00</RelTime>"
            "<TrackURI>http://x/audio/1.flac</TrackURI></r>"
        )
    )
    details = await client.get_position_details()
    assert details is not None
    assert details.position_ms == 60_000
    assert details.duration_ms == 200_000
    assert details.track_uri == "http://x/audio/1.flac"
    assert client._soap_action.await_count == 1
