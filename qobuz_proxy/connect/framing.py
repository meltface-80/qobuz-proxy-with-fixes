"""
Wire framing for the Qobuz Connect WebSocket protocol.

Every WebSocket message is one frame: [msg_type: 1 byte][length: varint][data: N bytes].
"""

from typing import Tuple

from .protocol_types import MessageType


def pack_frame(msg_type: MessageType, data: bytes) -> bytes:
    """
    Pack data into wire frame format.

    Format: [type: 1 byte][length: varint][data: N bytes]
    """
    frame = bytearray()
    frame.append(msg_type)

    # Encode length as varint
    length = len(data)
    while length > 0x7F:
        frame.append((length & 0x7F) | 0x80)
        length >>= 7
    frame.append(length & 0x7F)

    frame.extend(data)
    return bytes(frame)


def decode_varint(data: bytes, start: int) -> Tuple[int, int]:
    """
    Decode varint starting at position.

    Returns:
        (value, next_offset) or (-1, -1) on error
    """
    value = 0
    shift = 0
    offset = start

    while offset < len(data):
        byte = data[offset]
        value |= (byte & 0x7F) << shift
        offset += 1
        if not (byte & 0x80):
            return value, offset
        shift += 7

    return -1, -1
