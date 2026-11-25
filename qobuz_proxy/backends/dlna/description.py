"""
UPnP device and service descriptions.

Parses the device description a renderer serves at its SSDP LOCATION, and the
AVTransport service description (SCPD) that lists the actions it implements.
"""

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

from .soap import local_name

logger = logging.getLogger(__name__)

# Paths tried, in order, when no description URL is known.
DESCRIPTION_PATHS = (
    "/xml/device_description.xml",  # Sonos
    "/description.xml",
    "/DeviceDescription.xml",
    "/upnp/desc/aios_device/aios_device.xml",  # Denon/Marantz
    "/dmr/SamsungMRDesc.xml",  # Samsung
    "/rootDesc.xml",
)


@dataclass
class DLNADeviceInfo:
    """DLNA device information from a UPnP description."""

    friendly_name: str = ""
    manufacturer: str = ""
    model_name: str = ""
    udn: str = ""  # Unique Device Name
    av_transport_url: str = ""
    rendering_control_url: str = ""
    connection_manager_url: str = ""
    # AVTransport service description and event subscription URLs.
    av_transport_scpd_url: str = ""
    av_transport_event_url: str = ""
    # Actions the AVTransport SCPD lists; None when it could not be read.
    av_transport_actions: Optional[frozenset[str]] = None

    def supports_action(self, action: str) -> Optional[bool]:
        """Whether the AVTransport SCPD lists ``action`` (None: unknown)."""
        if self.av_transport_actions is None:
            return None
        return action in self.av_transport_actions


@dataclass
class _Service:
    service_type: str
    control_url: str
    scpd_url: str
    event_url: str


def _child(elem: ET.Element, name: str) -> Optional[ET.Element]:
    """First direct child with local tag ``name`` (namespace-agnostic)."""
    for child in elem:
        if local_name(child.tag) == name:
            return child
    return None


def _child_text(elem: Optional[ET.Element], name: str) -> str:
    if elem is None:
        return ""
    child = _child(elem, name)
    return (child.text or "").strip() if child is not None else ""


def _devices(device: ET.Element) -> list[ET.Element]:
    """This device and its embedded devices, depth-first, root first."""
    found = [device]
    device_list = _child(device, "deviceList")
    if device_list is not None:
        for sub in device_list:
            if local_name(sub.tag) == "device":
                found.extend(_devices(sub))
    return found


def _services(device: ET.Element) -> list[_Service]:
    """Services declared directly on ``device`` (not its embedded devices)."""
    service_list = _child(device, "serviceList")
    if service_list is None:
        return []
    return [
        _Service(
            service_type=_child_text(svc, "serviceType"),
            control_url=_child_text(svc, "controlURL"),
            scpd_url=_child_text(svc, "SCPDURL"),
            event_url=_child_text(svc, "eventSubURL"),
        )
        for svc in service_list
        if local_name(svc.tag) == "service"
    ]


def parse_device_description(
    xml_text: str, base_url: str = "", doc_url: str = ""
) -> DLNADeviceInfo:
    """Parse a UPnP device description.

    Identity (name, maker, model, UDN) comes from the device that holds the
    AVTransport service — the root device on most renderers, the embedded
    MediaRenderer on Sonos — with the root device filling in missing fields.
    Walking every element instead let an embedded device (a media server,
    say) overwrite the renderer's name or UDN.

    Relative URLs resolve against URLBase when the device gives one, else
    against the description document's own URL (UPnP Device Architecture
    1.0, section 2.1).
    """
    info = DLNADeviceInfo()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.error(f"Failed to parse device description: {e}")
        return info

    url_base = _child_text(root, "URLBase")
    anchor = url_base or doc_url or (base_url + "/" if base_url else "")

    def resolve(url: str) -> str:
        return urljoin(anchor, url) if anchor and url else url

    root_device = _child(root, "device")
    if root_device is None:
        logger.debug("Device description has no <device> element")
        return info
    devices = _devices(root_device)
    renderer = next(
        (d for d in devices if any("AVTransport" in s.service_type for s in _services(d))),
        root_device,
    )
    for name, attr in (
        ("friendlyName", "friendly_name"),
        ("manufacturer", "manufacturer"),
        ("modelName", "model_name"),
        ("UDN", "udn"),
    ):
        setattr(info, attr, _child_text(renderer, name) or _child_text(root_device, name))

    # The renderer's own services first, then the rest, so a ConnectionManager
    # or RenderingControl on the renderer wins over one elsewhere.
    ordered = [renderer] + [d for d in devices if d is not renderer]
    group_rendering_url = ""
    for device in ordered:
        for svc in _services(device):
            if not svc.control_url:
                continue
            if "AVTransport" in svc.service_type:
                if not info.av_transport_url:
                    info.av_transport_url = resolve(svc.control_url)
                    info.av_transport_scpd_url = resolve(svc.scpd_url)
                    info.av_transport_event_url = resolve(svc.event_url)
            elif "RenderingControl" in svc.service_type:
                # GroupRenderingControl is Sonos-specific with another API:
                # only a fallback when there is no standard RenderingControl.
                if "GroupRenderingControl" in svc.service_type:
                    group_rendering_url = group_rendering_url or resolve(svc.control_url)
                elif not info.rendering_control_url:
                    info.rendering_control_url = resolve(svc.control_url)
            elif "ConnectionManager" in svc.service_type and not info.connection_manager_url:
                info.connection_manager_url = resolve(svc.control_url)
    info.rendering_control_url = info.rendering_control_url or group_rendering_url

    logger.debug(
        f"Parsed device info: friendly_name={info.friendly_name}, "
        f"manufacturer={info.manufacturer}, model={info.model_name}, udn={info.udn}"
    )
    logger.debug(
        f"Service URLs: AVTransport={info.av_transport_url}, "
        f"RenderingControl={info.rendering_control_url}"
    )
    return info


def parse_scpd_actions(xml_text: str) -> Optional[frozenset[str]]:
    """Action names a service description (SCPD) lists; None if unreadable."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    action_list = _child(root, "actionList")
    if action_list is None:
        return None
    return frozenset(
        _child_text(action, "name")
        for action in action_list
        if local_name(action.tag) == "action" and _child_text(action, "name")
    )
