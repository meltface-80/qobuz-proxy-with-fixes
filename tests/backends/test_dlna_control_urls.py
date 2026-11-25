"""Control URLs in a device description resolve per UPnP Device Architecture 2.1."""

from qobuz_proxy.backends.dlna.client import DLNAClient

DESC = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  {url_base}
  <device>
    <friendlyName>Desk</friendlyName>
    <manufacturer>FiiO</manufacturer>
    <modelName>K17</modelName>
    <UDN>uuid:abc</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <controlURL>{avt}</controlURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
        <controlURL>/rc/control</controlURL>
      </service>
    </serviceList>
  </device>
</root>"""


def parse(avt: str, url_base: str = "", doc_url: str = "http://10.0.0.5:49152/dmr/desc.xml"):
    client = DLNAClient("10.0.0.5", 49152)
    xml = DESC.format(avt=avt, url_base=f"<URLBase>{url_base}</URLBase>" if url_base else "")
    return client._parse_device_description(xml, "http://10.0.0.5:49152", doc_url)


def test_absolute_path_unchanged():
    info = parse("/avt/control")
    assert info.av_transport_url == "http://10.0.0.5:49152/avt/control"
    assert info.rendering_control_url == "http://10.0.0.5:49152/rc/control"


def test_relative_path_resolves_against_description_url():
    assert parse("avt/control").av_transport_url == "http://10.0.0.5:49152/dmr/avt/control"


def test_relative_path_resolves_against_url_base():
    info = parse("avt/control", url_base="http://10.0.0.5:8080/base/")
    assert info.av_transport_url == "http://10.0.0.5:8080/base/avt/control"


def test_full_url_kept():
    assert parse("http://10.0.0.6/x").av_transport_url == "http://10.0.0.6/x"


def test_without_document_url_behaves_as_before():
    client = DLNAClient("10.0.0.5", 49152)
    xml = DESC.format(avt="/avt/control", url_base="")
    info = client._parse_device_description(xml, "http://10.0.0.5:49152")
    assert info.av_transport_url == "http://10.0.0.5:49152/avt/control"
