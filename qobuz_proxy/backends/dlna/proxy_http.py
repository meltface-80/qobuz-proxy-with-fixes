"""
HTTP helpers for the audio proxy: DLNA headers, ranges, registrations, logging.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

# URL refresh settings
DEFAULT_URL_MAX_AGE_SECONDS = 240  # Refresh before 5-minute TTL

# DLNA streaming headers, sent only when the renderer asks for them
# (getcontentFeatures.dlna.org / transferMode.dlna.org request headers).
# OP=01: byte-range seeking; FLAGS: streaming transfer mode, DLNA 1.5.
DLNA_CONTENT_FEATURES = (
    "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
)


@dataclass
class RegisteredTrack:
    """A track registered with the proxy server."""

    track_id: str
    qobuz_url: str
    content_type: str
    url_fetched_at: float = field(default_factory=time.time)
    # Full file size in bytes, once an upstream answer has told us.
    size: Optional[int] = None

    def is_url_expired(self, max_age: float = DEFAULT_URL_MAX_AGE_SECONDS) -> bool:
        """Check if the URL has expired or is about to expire."""
        return time.time() - self.url_fetched_at >= max_age


def dlna_headers(request: web.Request) -> Dict[str, str]:
    """DLNA headers a renderer asked for in its request."""
    headers: Dict[str, str] = {}
    if request.headers.get("getcontentFeatures.dlna.org", "").strip() == "1":
        headers["contentFeatures.dlna.org"] = DLNA_CONTENT_FEATURES
    if request.headers.get("transferMode.dlna.org"):
        headers["transferMode.dlna.org"] = "Streaming"
    return headers


def content_range_total(content_range: Optional[str]) -> Optional[int]:
    """Total size from "bytes 0-99/5000"; None if absent or "*"."""
    try:
        total = (content_range or "").split("/", 1)[1].strip()
        return int(total) if total.isdigit() else None
    except IndexError:
        return None


def full_size(status: int, headers: Mapping[str, str]) -> Optional[int]:
    """The whole file's size from an upstream answer, if it tells."""
    if status == 206:
        return content_range_total(headers.get("Content-Range"))
    cl = headers.get("Content-Length", "")
    # Only a 200 carries the whole file's size; a 206's length is just the range.
    return int(cl) if status == 200 and cl.isdigit() else None


def reaches_end_of_file(
    status: int,
    content_range: Optional[str],
    expected_bytes: Optional[int],
    bytes_sent: int,
) -> bool:
    """Whether a completed response carried the file through to its last byte."""
    if status == 206:
        # "bytes 1000-49999/50000": the range ends at the file's last byte.
        try:
            span, total = (content_range or "").split(" ", 1)[1].split("/", 1)
            end = int(span.split("-", 1)[1])
            return total.strip() != "*" and end + 1 == int(total)
        except (IndexError, ValueError):
            return False
    return expected_bytes is None or bytes_sent >= expected_bytes


def parse_range_start(range_header: str) -> int:
    """Extract the start byte from a Range header ("bytes=X-...")."""
    try:
        return int(range_header.split("=", 1)[1].split("-", 1)[0] or 0)
    except (IndexError, ValueError):
        return 0


def log_stream_end(
    track: RegisteredTrack,
    bytes_sent: int,
    expected_bytes: Optional[int],
    elapsed: float,
    is_range: bool,
    completed: bool,
) -> None:
    """Log the outcome of a proxied stream with throughput diagnostics.

    Distinguishes a genuine mid-stream drop (renderer gave up or its buffer
    underran) from benign disconnects (a Sonos probe, a seek, or a finished
    transfer). Average throughput is included so we can tell *which*: a low
    Mbit/s well under the stream bitrate points at the proxy/network not
    keeping the device's buffer full; a high rate that simply stops points
    at the renderer being unable to sustain decode/output at this quality.
    """
    rate_mbps = (bytes_sent * 8 / elapsed / 1_000_000) if elapsed > 0 else 0.0
    pct = f"{bytes_sent / expected_bytes * 100:.1f}%" if expected_bytes else "?"

    # A mid-transfer drop: a full-body request (not a range/seek) that ended
    # well short of the advertised length. This is the stutter/stop symptom.
    short = expected_bytes is not None and bytes_sent < expected_bytes * 0.95
    if not completed and short and not is_range:
        logger.warning(
            "Renderer dropped track %s mid-stream: sent %d/%d bytes (%s) in "
            "%.1fs (%.2f Mbit/s avg). Suspect buffer underrun or renderer "
            "cannot sustain this quality.",
            track.track_id,
            bytes_sent,
            expected_bytes,
            pct,
            elapsed,
            rate_mbps,
        )
    elif completed:
        logger.debug(
            "Finished streaming track %s: %d bytes in %.1fs (%.2f Mbit/s avg)",
            track.track_id,
            bytes_sent,
            elapsed,
            rate_mbps,
        )
    else:
        logger.debug(
            "Client disconnected after %d bytes (%s) for track %s "
            "(range=%s, %.1fs, %.2f Mbit/s avg)",
            bytes_sent,
            pct,
            track.track_id,
            is_range,
            elapsed,
            rate_mbps,
        )
