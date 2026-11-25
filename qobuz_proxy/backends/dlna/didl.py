"""
DIDL-Lite track metadata and small helpers shared by the DLNA backend modules.
"""

import hashlib
import re
from typing import Optional
from urllib.parse import urlsplit

from qobuz_proxy.backends.types import BackendTrackMetadata

from .soap import xml_escape


def proxy_key(url: str) -> str:
    """Audio proxy registration key of one of our proxy URLs."""
    return urlsplit(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def content_type_for(url: str) -> str:
    """MIME type of a Qobuz or proxy stream URL (FLAC unless it is MP3)."""
    lowered = url.lower()
    if ".mp3" in lowered or "format=5" in lowered:
        return "audio/mpeg"
    return "audio/flac"


def source_label(uri: Optional[str]) -> str:
    """Identify sources without logging credentials, signed queries, or opaque IDs."""
    if not uri:
        return "none"
    fingerprint = hashlib.sha256(uri.encode()).hexdigest()[:12]
    try:
        parsed = urlsplit(uri)
        # Our audio paths contain only track IDs and optional generation numbers.
        path = (
            parsed.path if re.fullmatch(r"/audio/\d+(?:_\d+)?\.(?:flac|mp3)", parsed.path) else ""
        )
        return f"{parsed.scheme}://{parsed.hostname or ''}{path} [sha256={fingerprint}]"
    except ValueError:
        return f"unparseable [sha256={fingerprint}]"


def format_ms(ms: int) -> str:
    """Milliseconds as M:SS for log messages."""
    seconds = max(0, ms) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def _duration(ms: int) -> str:
    total_s = ms // 1000
    return f"{total_s // 3600}:{(total_s % 3600) // 60:02d}:{total_s % 60:02d}"


def build_didl(
    url: str,
    metadata: BackendTrackMetadata,
    protocol_info: str,
    size: Optional[int] = None,
) -> str:
    """DIDL-Lite for one track.

    ``size`` (bytes) is included when known: renderers that cannot tell where
    a stream ends otherwise may stay PLAYING at its end, waiting for more.
    """
    res_attrs = f'protocolInfo="{xml_escape(protocol_info)}"'
    if metadata.duration_ms > 0:
        res_attrs += f' duration="{_duration(metadata.duration_ms)}"'
    if size:
        res_attrs += f' size="{size}"'
    if metadata.sample_rate:
        res_attrs += f' sampleFrequency="{metadata.sample_rate}"'
    if metadata.bit_depth:
        res_attrs += f' bitsPerSample="{metadata.bit_depth}"'

    art = (
        f"\n        <upnp:albumArtURI>{xml_escape(metadata.artwork_url)}</upnp:albumArtURI>"
        if metadata.artwork_url
        else ""
    )
    return f"""<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
                   xmlns:dc="http://purl.org/dc/elements/1.1/"
                   xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
    <item id="1" parentID="0" restricted="1">
        <dc:title>{xml_escape(metadata.title)}</dc:title>
        <dc:creator>{xml_escape(metadata.artist)}</dc:creator>
        <upnp:artist>{xml_escape(metadata.artist)}</upnp:artist>
        <upnp:album>{xml_escape(metadata.album)}</upnp:album>
        <upnp:class>object.item.audioItem.musicTrack</upnp:class>{art}
        <res {res_attrs}>{xml_escape(url)}</res>
    </item>
</DIDL-Lite>"""
