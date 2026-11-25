"""
DLNA/UPnP Discovery

SSDP-based discovery for DLNA renderers (Sonos, HEOS, etc.).

- M-SEARCH goes out on the LAN interface (the configured bind address, or the
  LAN IP), not whichever interface the OS picks: on hosts with Docker bridges
  or a VPN the default multicast route often misses the speakers.
- Each search is sent more than once and for both the MediaRenderer device
  and the AVTransport service, since SSDP is UDP and some renderers answer
  only one of the two.
- Answers are deduplicated by UDN, so a device that answers several search
  targets (or from several interfaces) is listed once.
- Hosts listed in ``QOBUZPROXY_DLNA_HOSTS`` get a unicast M-SEARCH and, when a
  port or URL is given, a direct description fetch: for Docker or VLAN setups
  where multicast does not arrive.
- A device seen in an earlier search but silent in this one is re-checked
  quietly through its description URL before it is dropped from the list.
"""

import asyncio
import logging
import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlparse

import aiohttp

from qobuz_proxy.net import ANY_ADDRESS, interface_address

from .description import DESCRIPTION_PATHS, parse_device_description
from .sonos_topology import enrich_discovered_devices, fetch_sonos_topology

logger = logging.getLogger(__name__)

# SSDP constants
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_MX = 3  # Maximum wait time in seconds
SSDP_TTL = 2  # UPnP Device Architecture default

# UPnP device type we're looking for
MEDIA_RENDERER_TYPE = "urn:schemas-upnp-org:device:MediaRenderer:1"
AV_TRANSPORT_TYPE = "urn:schemas-upnp-org:service:AVTransport:1"
SEARCH_TARGETS = (MEDIA_RENDERER_TYPE, AV_TRANSPORT_TYPE)
# Each search round is sent this many times, this far apart.
SEARCH_REPEATS = 2
SEARCH_REPEAT_INTERVAL_SECONDS = 1.0

# Comma-separated hosts to search directly: "ip", "ip:port" or a description URL.
HOSTS_ENV = "QOBUZPROXY_DLNA_HOSTS"
# How long a device that missed a search round is still re-checked.
RECHECK_WINDOW_SECONDS = 3600.0
DESCRIPTION_TIMEOUT_SECONDS = 10.0


@dataclass
class DiscoveredDevice:
    """Discovered DLNA device information."""

    friendly_name: str
    ip: str
    port: int
    model_name: str = ""
    manufacturer: str = ""
    udn: str = ""
    location: str = ""


@dataclass
class _RawDevice:
    """A device location from an SSDP answer or a listed host (before XML fetch)."""

    location: str
    usn: str
    ip: str
    port: int


# Devices seen by any discovery in this process: UDN -> (device, last seen).
_seen: dict[str, tuple[DiscoveredDevice, float]] = {}


def listed_hosts() -> list[str]:
    """Hosts from ``QOBUZPROXY_DLNA_HOSTS``."""
    return [h.strip() for h in os.environ.get(HOSTS_ENV, "").split(",") if h.strip()]


def build_search_message(search_target: str) -> bytes:
    """An SSDP M-SEARCH request."""
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {SSDP_MX}\r\n"
        f"ST: {search_target}\r\n"
        "\r\n"
    ).encode("utf-8")


def parse_ssdp_response(response: str, source_ip: str) -> Optional[_RawDevice]:
    """The device an SSDP answer (or NOTIFY) points at, if it names one."""
    headers: dict[str, str] = {}
    for line in response.split("\r\n")[1:]:  # Skip the status line
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.upper().strip()] = value.strip()

    location = headers.get("LOCATION", "")
    usn = headers.get("USN", "")
    if not location or not usn:
        return None
    try:
        parsed = urlparse(location)
        port = parsed.port or 80
        ip = parsed.hostname or source_ip
    except ValueError:
        port, ip = 80, source_ip
    return _RawDevice(location=location, usn=usn, ip=ip, port=port)


def _device_key(usn: str) -> str:
    """The UDN part of a USN ("uuid:x::urn:...") — one key per device."""
    return usn.split("::", 1)[0]


class _SsdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_answer: Callable[[str, str], None]):
        self._on_answer = on_answer

    def datagram_received(self, data: bytes, addr: tuple[Any, ...]) -> None:
        self._on_answer(data.decode("utf-8", errors="ignore"), str(addr[0]))

    def error_received(self, exc: Exception) -> None:
        logger.debug(f"SSDP socket error: {exc}")


class DLNADiscovery:
    """
    SSDP-based discovery for DLNA Media Renderers.

    Usage:
        discovery = DLNADiscovery()
        devices = await discovery.discover(timeout=5.0)
    """

    def __init__(
        self, bind_address: str = ANY_ADDRESS, hosts: Optional[Sequence[str]] = None
    ) -> None:
        self._bind_address = bind_address
        self._hosts = list(hosts) if hosts is not None else listed_hosts()
        self._devices: dict[str, _RawDevice] = {}
        self._on_device_found: Callable[[DiscoveredDevice], None] | None = None

    async def discover(
        self, timeout: float = 5.0, search_target: Optional[str] = None
    ) -> list[DiscoveredDevice]:
        """Discover DLNA renderers on the network."""
        self._devices.clear()
        targets = (search_target,) if search_target else SEARCH_TARGETS
        await self._search(targets, timeout)

        found_keys = set(self._devices)
        self._add_listed_host_locations()
        self._add_recheck_candidates(found_keys)

        result = await self._fetch_device_descriptions(list(self._devices.values()))

        # Sonos: hide bonded pair members / HT satellites, use room names.
        # No-op (and no network traffic) when no Sonos device was found.
        topology = await fetch_sonos_topology(result)
        if topology:
            result = enrich_discovered_devices(result, topology)

        now = time.monotonic()
        for device in result:
            if device.udn:
                _seen[device.udn] = (device, now)
        logger.debug(f"Discovered {len(result)} DLNA device(s)")
        return result

    async def find(self, udn: str, timeout: float = 3.0) -> Optional[DiscoveredDevice]:
        """Search for one renderer by UDN (e.g. after its IP address changed)."""
        if not udn:
            return None
        for device in await self.discover(timeout=timeout):
            if device.udn == udn:
                return device
        return None

    # ------------------------------------------------------------------
    # SSDP
    # ------------------------------------------------------------------

    def _open_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, SSDP_TTL)
        iface = interface_address(self._bind_address)
        bound = False
        if iface:
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
                sock.bind((iface, 0))
                bound = True
                logger.debug(f"SSDP search on interface {iface}")
            except OSError as e:
                logger.debug(f"Could not bind SSDP search to {iface}: {e}")
        if not bound:
            sock.bind(("", 0))
        sock.setblocking(False)
        return sock

    async def _search(self, targets: Sequence[str], timeout: float) -> None:
        loop = asyncio.get_running_loop()
        try:
            sock = self._open_socket()
        except OSError as e:
            logger.warning(f"SSDP search unavailable: {e}")
            return
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _SsdpProtocol(self._on_answer), sock=sock
        )
        unicast = [self._unicast_target(h) for h in self._hosts]
        try:
            for repeat in range(SEARCH_REPEATS):
                for target in targets:
                    message = build_search_message(target)
                    transport.sendto(message, (SSDP_ADDR, SSDP_PORT))
                    for ip in unicast:
                        if ip:
                            transport.sendto(message, (ip, SSDP_PORT))
                logger.debug(f"Sent SSDP M-SEARCH for {', '.join(targets)}")
                if repeat < SEARCH_REPEATS - 1:
                    await asyncio.sleep(min(SEARCH_REPEAT_INTERVAL_SECONDS, timeout / 2))
            await asyncio.sleep(max(0.0, timeout - SEARCH_REPEAT_INTERVAL_SECONDS))
        finally:
            transport.close()

    def _on_answer(self, response: str, source_ip: str) -> None:
        self._parse_ssdp_response(response, source_ip)

    def _parse_ssdp_response(self, response: str, source_ip: str) -> None:
        """Record the device an SSDP answer points at (one entry per UDN)."""
        device = parse_ssdp_response(response, source_ip)
        if device is None:
            return
        key = _device_key(device.usn)
        if key not in self._devices:
            self._devices[key] = device
            logger.debug(f"Found device: {device.location}")

    # ------------------------------------------------------------------
    # Listed hosts and re-checks
    # ------------------------------------------------------------------

    @staticmethod
    def _unicast_target(host: str) -> Optional[str]:
        if "://" in host:
            return urlparse(host).hostname
        return host.rsplit(":", 1)[0] if host.count(":") == 1 else host

    def _add_listed_host_locations(self) -> None:
        """Description URLs to fetch for listed hosts that did not answer."""
        answered = {d.ip for d in self._devices.values()}
        for host in self._hosts:
            if "://" in host:
                parsed = urlparse(host)
                ip = parsed.hostname or ""
                if ip not in answered:
                    self._devices.setdefault(
                        f"listed:{host}", _RawDevice(host, f"listed:{host}", ip, parsed.port or 80)
                    )
            elif host.count(":") == 1:
                ip, port_text = host.split(":")
                if ip not in answered and port_text.isdigit():
                    base = f"http://{ip}:{port_text}"
                    self._devices.setdefault(
                        f"listed:{host}",
                        _RawDevice(base, f"listed:{host}", ip, int(port_text)),
                    )

    def _add_recheck_candidates(self, found_keys: set[str]) -> None:
        """Devices seen recently that missed this round: check them directly."""
        now = time.monotonic()
        for udn, (device, seen_at) in list(_seen.items()):
            if now - seen_at > RECHECK_WINDOW_SECONDS:
                del _seen[udn]
            elif udn not in found_keys and device.location:
                self._devices.setdefault(
                    udn, _RawDevice(device.location, udn, device.ip, device.port)
                )

    # ------------------------------------------------------------------
    # Descriptions
    # ------------------------------------------------------------------

    async def _fetch_device_descriptions(
        self, raw_devices: list[_RawDevice]
    ) -> list[DiscoveredDevice]:
        """Fetch device description XML for all devices (one entry per UDN)."""
        result: list[DiscoveredDevice] = []
        timeout = aiohttp.ClientTimeout(total=DESCRIPTION_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                tasks = [self._fetch_device_description(session, d) for d in raw_devices]
                fetched = await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"Error fetching device descriptions: {e}")
            return result
        udns: set[str] = set()
        for item in fetched:
            if isinstance(item, DiscoveredDevice) and (not item.udn or item.udn not in udns):
                udns.add(item.udn)
                result.append(item)
        return result

    async def _fetch_device_description(
        self, session: aiohttp.ClientSession, raw_device: _RawDevice
    ) -> DiscoveredDevice | None:
        """Fetch and parse one device description (probing paths for a bare host)."""
        parsed = urlparse(raw_device.location)
        urls = (
            [raw_device.location]
            if parsed.path not in ("", "/")
            else [raw_device.location.rstrip("/") + p for p in DESCRIPTION_PATHS]
        )
        for url in urls:
            try:
                async with session.get(url) as response:
                    if response.status != 200:
                        continue
                    xml_text = await response.text()
            except Exception as e:
                logger.debug(f"Error fetching description for {url}: {e}")
                continue
            return self._parse_device_description(
                _RawDevice(url, raw_device.usn, raw_device.ip, raw_device.port), xml_text
            )
        return None

    def _parse_device_description(
        self, raw_device: _RawDevice, xml_text: str
    ) -> DiscoveredDevice | None:
        """Parse device description XML into a renderer (None if it is not one)."""
        info = parse_device_description(xml_text, doc_url=raw_device.location)
        if not info.udn and not info.friendly_name:
            logger.debug(f"No device found in description at {raw_device.location}")
            return None
        if not info.av_transport_url:
            logger.debug(f"{raw_device.location} is not a renderer (no AVTransport)")
            return None
        device = DiscoveredDevice(
            friendly_name=info.friendly_name or f"Unknown ({raw_device.ip})",
            ip=raw_device.ip,
            port=raw_device.port,
            model_name=info.model_name,
            manufacturer=info.manufacturer,
            udn=info.udn,
            location=raw_device.location,
        )
        logger.debug(f"Parsed device: {device.friendly_name}")
        if self._on_device_found:
            self._on_device_found(device)
        return device

    def on_device_found(self, callback: Callable[[DiscoveredDevice], None]) -> None:
        """Register callback for when a device is found."""
        self._on_device_found = callback


async def discover_dlna_devices(
    timeout: float = 5.0, bind_address: str = ANY_ADDRESS
) -> list[DiscoveredDevice]:
    """Discover DLNA renderers (convenience wrapper)."""
    return await DLNADiscovery(bind_address=bind_address).discover(timeout=timeout)


__all__ = ["DLNADiscovery", "DiscoveredDevice", "discover_dlna_devices", "listed_hosts"]
