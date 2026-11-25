"""Network helpers shared by mDNS, SSDP and the audio proxy."""

import ipaddress
import logging
import socket
from typing import Optional

logger = logging.getLogger(__name__)

ANY_ADDRESS = "0.0.0.0"

# Interfaces that never reach the LAN the speakers and phones are on: VPN
# tunnels (a VPN's default route made mDNS advertise on utun, see upstream
# PR #28), container bridges, loopback and Apple's peer-to-peer links.
_VIRTUAL_PREFIXES = (
    "lo",
    "utun",
    "tun",
    "tap",
    "wg",
    "ppp",
    "ipsec",
    "awdl",
    "llw",
    "docker",
    "br-",
    "veth",
    "virbr",
    "vmnet",
    "zt",
    "tailscale",
)


def _route_ip() -> Optional[str]:
    """Source address the kernel would use to reach the internet.

    A UDP "connect" sends nothing; it only selects a route.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        try:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
        finally:
            s.close()
    except Exception as e:
        logger.debug(f"No default route: {e}")
        return None


def _lan_addresses() -> list[tuple[str, str]]:
    """(interface name, IPv4) for every interface; empty if ifaddr is missing."""
    try:
        import ifaddr  # zeroconf dependency
    except ImportError:
        return []
    found: list[tuple[str, str]] = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if isinstance(ip.ip, str):
                found.append((str(adapter.nice_name or adapter.name), ip.ip))
    return found


def _is_virtual(name: str) -> bool:
    return name.lower().startswith(_VIRTUAL_PREFIXES)


def get_local_ip() -> Optional[str]:
    """The local IPv4 address on the LAN.

    The address of the default route, unless that route goes through a VPN
    tunnel or another virtual interface: then the first private address on a
    physical interface.
    """
    route_ip = _route_ip()
    addresses = _lan_addresses()
    route_iface = next((name for name, ip in addresses if ip == route_ip), None)
    if route_ip and (route_iface is None or not _is_virtual(route_iface)):
        return route_ip
    for name, ip in addresses:
        if _is_virtual(name):
            continue
        try:
            if ipaddress.ip_address(ip).is_private and not ipaddress.ip_address(ip).is_loopback:
                logger.debug(f"Default route uses {route_iface}; using {ip} on {name}")
                return ip
        except ValueError:
            continue
    if route_ip:
        return route_ip
    logger.error("Failed to determine local IP")
    return None


def interface_address(bind_address: str = ANY_ADDRESS) -> Optional[str]:
    """The address to use for multicast: an explicit bind address, else the LAN IP."""
    if bind_address and bind_address != ANY_ADDRESS:
        return bind_address
    return get_local_ip()
