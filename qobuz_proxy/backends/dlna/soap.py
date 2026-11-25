"""
SOAP helpers for UPnP control: envelopes, results, XML values and times.

Pure functions only; the HTTP side lives in ``client.py``.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Mapping, Optional

SOAP_ENVELOPE_NS = "http://schemas.xmlsoap.org/soap/envelope/"
UPNP_AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
UPNP_RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
UPNP_CONNECTION_MANAGER = "urn:schemas-upnp-org:service:ConnectionManager:1"

# UPnP errors that mean "this device does not do that": 401 Invalid Action,
# 602 Optional Action Not Implemented (often sent as Invalid Args).
UNSUPPORTED_ERROR_CODES = frozenset({401, 602})

# Errors a retry cannot fix, so the request is not repeated: the above plus
# AVTransport 710 Seek Mode Not Supported, 711 Illegal Seek Target and
# 712 Play Mode Not Supported. Retrying a 710 three times delayed every
# track start by about five seconds on a NAD C 338 (GitHub #44).
NO_RETRY_ERROR_CODES = UNSUPPORTED_ERROR_CODES | frozenset({710, 711, 712})


@dataclass
class SoapResult:
    """Result from a SOAP action, including error classification."""

    success: bool
    body: Optional[str] = None
    # UPnP error code from device (e.g. 401, 602)
    error_code: Optional[int] = None
    error_description: Optional[str] = None

    @property
    def is_permanent_failure(self) -> bool:
        """Whether the device does not support this action at all."""
        return self.error_code in UNSUPPORTED_ERROR_CODES

    @property
    def is_retryable(self) -> bool:
        """Whether repeating the same request could succeed."""
        return self.error_code not in NO_RETRY_ERROR_CODES


def xml_escape(text: str) -> str:
    """Escape text for an XML element or attribute value."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def build_soap_envelope(service: str, action: str, args: Mapping[str, str]) -> str:
    """Build a SOAP envelope.

    Single-line format matching the SoCo library, which Sonos requires.
    """
    args_xml = "".join(f"<{k}>{xml_escape(v)}</{k}>" for k, v in args.items())
    return (
        '<?xml version="1.0"?>'
        f'<s:Envelope xmlns:s="{SOAP_ENVELOPE_NS}" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{service}">'
        f"{args_xml}"
        f"</u:{action}>"
        "</s:Body>"
        "</s:Envelope>"
    )


def local_name(tag: str) -> str:
    """An element tag without its ``{namespace}`` prefix."""
    return tag.split("}")[-1]


def parse_xml_value(xml_text: str, tag_name: str) -> Optional[str]:
    """Text of the first element whose tag contains ``tag_name``."""
    try:
        root = ET.fromstring(xml_text)
        for elem in root.iter():
            if tag_name in elem.tag:
                return elem.text
    except ET.ParseError:
        pass
    return None


def parse_xml_value_exact(xml_text: str, tag_name: str) -> Optional[str]:
    """Text of the first element whose local tag name is exactly ``tag_name``."""
    try:
        root = ET.fromstring(xml_text)
        for elem in root.iter():
            if local_name(elem.tag) == tag_name:
                return elem.text
    except ET.ParseError:
        pass
    return None


def ms_to_time_string(ms: int) -> str:
    """Milliseconds as UPnP ``HH:MM:SS``."""
    seconds = max(0, ms) // 1000
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def time_string_to_ms(time_str: Optional[str]) -> Optional[int]:
    """UPnP ``H:MM:SS[.fff]`` as milliseconds; None if absent or unparseable.

    Many renderers report RelTime as "NOT_IMPLEMENTED" — some always, some
    transiently during transitions. That must surface as "unknown", not as
    position 0, or it stomps the last known position every poll.
    """
    if not time_str or time_str.strip().startswith("-"):
        return None
    try:
        parts = time_str.strip().split(":")
        if len(parts) == 3:
            hours, minutes, seconds = parts
            total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            return int(total * 1000)
    except (ValueError, IndexError):
        pass
    return None
