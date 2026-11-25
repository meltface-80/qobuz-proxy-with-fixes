"""
Qobuz Connect protocol constants and value types.

Message type codes, join reasons, quality mappings and the decoded-frame
container shared by the codec and the WebSocket manager.
"""

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional


class MessageType(IntEnum):
    """Outer envelope message types."""

    AUTHENTICATE = 1
    SUBSCRIBE = 2
    UNSUBSCRIBE = 3
    PAYLOAD = 6
    ERROR = 9
    DISCONNECT = 10


class QConnectProto(IntEnum):
    """Protocol identifiers."""

    QP_QCONNECT = 1


class QConnectMessageType(IntEnum):
    """Inner QConnect message types (subset of commonly used)."""

    # Renderer -> Server
    RNDR_SRVR_JOIN_SESSION = 21
    RNDR_SRVR_DEVICE_INFO_UPDATED = 22
    RNDR_SRVR_STATE_UPDATED = 23
    RNDR_SRVR_RENDERER_ACTION = 24
    RNDR_SRVR_VOLUME_CHANGED = 25
    RNDR_SRVR_FILE_AUDIO_QUALITY_CHANGED = 26
    RNDR_SRVR_DEVICE_AUDIO_QUALITY_CHANGED = 27
    RNDR_SRVR_MAX_AUDIO_QUALITY_CHANGED = 28

    # Server -> Renderer commands
    SRVR_RNDR_SET_STATE = 41
    SRVR_RNDR_SET_VOLUME = 42
    SRVR_RNDR_SET_ACTIVE = 43
    SRVR_RNDR_SET_MAX_AUDIO_QUALITY = 44
    SRVR_RNDR_SET_LOOP_MODE = 45
    SRVR_RNDR_SET_SHUFFLE_MODE = 46
    SRVR_RNDR_SET_AUTOPLAY_MODE = 47

    # Server -> Controller
    SRVR_CTRL_QUEUE_STATE = 90
    SRVR_CTRL_QUEUE_TRACKS_LOADED = 91


class JoinSessionReason(IntEnum):
    """Join reasons used by the Qobuz client (web bundle 8.2.0-b034)."""

    CONTROLLER_REQUEST = 1
    RECONNECTION = 2


@dataclass
class DecodedMessage:
    """Decoded WebSocket message."""

    msg_type: MessageType
    msg_id: int = 0
    msg_date: int = 0
    payload: Optional[bytes] = None
    error_code: int = 0
    error_message: str = ""


# Quality ID to protocol value mapping
# Qobuz quality IDs: 5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k
# Protocol values: 1=MP3, 2=LOSSLESS, 3=HIRES_L1, 4=HIRES_L3
QUALITY_TO_PROTOCOL = {5: 1, 6: 2, 7: 3, 27: 4}
PROTOCOL_TO_QUALITY = {1: 5, 2: 6, 3: 7, 4: 27}

# Default audio properties for each quality level: (sample_rate_hz, bit_depth, nb_channels)
QUALITY_AUDIO_PROPERTIES = {
    5: (44100, 16, 2),  # MP3 320kbps
    6: (44100, 16, 2),  # CD quality FLAC
    7: (96000, 24, 2),  # Hi-Res 96kHz
    27: (192000, 24, 2),  # Hi-Res 192kHz
}
