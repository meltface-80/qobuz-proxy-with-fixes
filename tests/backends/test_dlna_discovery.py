"""SSDP discovery: answer parsing, UDN dedupe, listed hosts, re-checks, find by UDN."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qobuz_proxy.backends.dlna import discovery as discovery_mod
from qobuz_proxy.backends.dlna.discovery import (
    DiscoveredDevice,
    DLNADiscovery,
    build_search_message,
    listed_hosts,
    parse_ssdp_response,
)
from qobuz_proxy.net import interface_address

RENDERER_XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <friendlyName>{name}</friendlyName>
    <manufacturer>FiiO</manufacturer>
    <modelName>K17</modelName>
    <UDN>{udn}</UDN>
    <serviceList>{services}</serviceList>
  </device>
</root>"""
AVT = """<service>
  <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
  <controlURL>/avt</controlURL>
</service>"""


def answer(location: str, usn: str) -> str:
    return f"HTTP/1.1 200 OK\r\nLOCATION: {location}\r\nUSN: {usn}\r\nST: x\r\n\r\n"


@pytest.fixture(autouse=True)
def clean_seen(monkeypatch):
    monkeypatch.setattr(discovery_mod, "_seen", {})
    monkeypatch.delenv(discovery_mod.HOSTS_ENV, raising=False)


def test_search_message_names_target():
    msg = build_search_message("urn:x").decode()
    assert msg.startswith("M-SEARCH * HTTP/1.1\r\n")
    assert "ST: urn:x\r\n" in msg and 'MAN: "ssdp:discover"' in msg


def test_parse_answer():
    raw = parse_ssdp_response(answer("http://10.0.0.7:49152/d.xml", "uuid:a::urn:x"), "10.0.0.9")
    assert raw is not None and raw.ip == "10.0.0.7" and raw.port == 49152
    assert parse_ssdp_response("HTTP/1.1 200 OK\r\n\r\n", "10.0.0.9") is None


def test_answers_for_several_targets_count_once():
    d = DLNADiscovery(hosts=[])
    d._parse_ssdp_response(answer("http://10.0.0.7/d.xml", "uuid:a::urn:MediaRenderer:1"), "x")
    d._parse_ssdp_response(answer("http://10.0.0.7/d.xml", "uuid:a::urn:AVTransport:1"), "x")
    d._parse_ssdp_response(answer("http://10.0.0.8/d.xml", "uuid:b::urn:MediaRenderer:1"), "x")
    assert sorted(d._devices) == ["uuid:a", "uuid:b"]


def test_listed_hosts_from_env(monkeypatch):
    monkeypatch.setenv(discovery_mod.HOSTS_ENV, " 10.0.0.5, 10.0.0.6:49152 ,,http://h/d.xml")
    assert listed_hosts() == ["10.0.0.5", "10.0.0.6:49152", "http://h/d.xml"]


def test_listed_hosts_become_description_candidates():
    d = DLNADiscovery(hosts=["10.0.0.5", "10.0.0.6:49152", "http://10.0.0.7:8080/desc.xml"])
    d._add_listed_host_locations()
    locations = sorted(r.location for r in d._devices.values())
    # A bare IP only gets the unicast M-SEARCH: no port to probe.
    assert locations == ["http://10.0.0.6:49152", "http://10.0.0.7:8080/desc.xml"]
    assert d._unicast_target("10.0.0.6:49152") == "10.0.0.6"
    assert d._unicast_target("http://10.0.0.7:8080/desc.xml") == "10.0.0.7"


def test_listed_host_that_answered_is_not_probed_again():
    d = DLNADiscovery(hosts=["10.0.0.6:49152"])
    d._parse_ssdp_response(answer("http://10.0.0.6:49152/d.xml", "uuid:a::x"), "10.0.0.6")
    d._add_listed_host_locations()
    assert list(d._devices) == ["uuid:a"]


def test_device_missing_a_round_is_rechecked(monkeypatch):
    seen = DiscoveredDevice("Desk", "10.0.0.7", 80, udn="uuid:a", location="http://10.0.0.7/d")
    discovery_mod._seen["uuid:a"] = (seen, discovery_mod.time.monotonic())
    d = DLNADiscovery(hosts=[])
    d._add_recheck_candidates(found_keys=set())
    assert d._devices["uuid:a"].location == "http://10.0.0.7/d"


def test_stale_device_is_forgotten(monkeypatch):
    seen = DiscoveredDevice("Desk", "10.0.0.7", 80, udn="uuid:a", location="http://10.0.0.7/d")
    discovery_mod._seen["uuid:a"] = (seen, -10_000.0)
    d = DLNADiscovery(hosts=[])
    d._add_recheck_candidates(found_keys=set())
    assert not d._devices and "uuid:a" not in discovery_mod._seen


def test_description_without_av_transport_is_not_a_renderer():
    d = DLNADiscovery(hosts=[])
    raw = discovery_mod._RawDevice("http://10.0.0.7/d.xml", "uuid:a", "10.0.0.7", 80)
    xml = RENDERER_XML.format(name="NAS", udn="uuid:a", services="")
    assert d._parse_device_description(raw, xml) is None
    xml = RENDERER_XML.format(name="Desk", udn="uuid:a", services=AVT)
    device = d._parse_device_description(raw, xml)
    assert device is not None and device.udn == "uuid:a" and device.model_name == "K17"


async def test_find_by_udn():
    d = DLNADiscovery(hosts=[])
    found = [
        DiscoveredDevice("Other", "10.0.0.8", 80, udn="uuid:b"),
        DiscoveredDevice("Desk", "10.0.0.99", 80, udn="uuid:a"),
    ]
    with patch.object(d, "discover", AsyncMock(return_value=found)):
        device = await d.find("uuid:a")
        assert device is not None and device.ip == "10.0.0.99"
        assert await d.find("uuid:zzz") is None
        assert await d.find("") is None


async def test_search_binds_to_interface_and_sends_unicast(monkeypatch):
    sock = MagicMock()
    monkeypatch.setattr(discovery_mod.socket, "socket", MagicMock(return_value=sock))
    monkeypatch.setattr(discovery_mod, "interface_address", lambda _: "10.0.0.2")
    transport = MagicMock()
    loop = MagicMock()
    loop.create_datagram_endpoint = AsyncMock(return_value=(transport, None))
    monkeypatch.setattr(discovery_mod.asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(discovery_mod.asyncio, "sleep", AsyncMock())

    d = DLNADiscovery(hosts=["10.0.0.6:49152"])
    await d._search(discovery_mod.SEARCH_TARGETS, timeout=2.0)

    sock.bind.assert_called_once_with(("10.0.0.2", 0))
    destinations = [c.args[1] for c in transport.sendto.call_args_list]
    repeats, targets = discovery_mod.SEARCH_REPEATS, len(discovery_mod.SEARCH_TARGETS)
    assert destinations.count(("239.255.255.250", 1900)) == repeats * targets
    assert destinations.count(("10.0.0.6", 1900)) == repeats * targets
    transport.close.assert_called_once()


def test_interface_address_prefers_explicit_bind():
    assert interface_address("192.168.1.4") == "192.168.1.4"
    with patch("qobuz_proxy.net.get_local_ip", return_value="10.1.1.1"):
        assert interface_address("0.0.0.0") == "10.1.1.1"
