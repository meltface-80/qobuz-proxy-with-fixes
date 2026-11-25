"""
Outbound renderer messages for the Qobuz Connect WebSocket.

Encodes renderer -> server notifications (state, volume, quality, next track)
and sends them over the connection owned by ``WsManager``.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Callable, Optional

from websockets import ClientConnection

from .protocol import ProtocolCodec
from .protocol_types import QUALITY_TO_PROTOCOL

logger = logging.getLogger(__name__)


class OutboundMessagesMixin:
    """
    Send renderer -> server messages for ``WsManager``.

    The connection state it reads (socket, codec, send lock, ownership
    generation) is created and maintained by ``WsManager``.
    """

    _ws: Optional[ClientConnection]
    _codec: ProtocolCodec
    _send_lock: asyncio.Lock
    _is_connected: bool
    _last_activity_time: float
    _ownership_generation: int

    if TYPE_CHECKING:

        @property
        def is_renderer_active(self) -> bool: ...

    async def send_message(self, data: bytes) -> bool:
        """
        Send a pre-encoded message.

        Args:
            data: Encoded frame bytes

        Returns:
            True if sent, False if dropped (not connected)
        """
        return await self._encode_and_send(lambda: data)

    async def _encode_and_send(
        self, encode: Callable[[], bytes], *, allowed: Optional[Callable[[], bool]] = None
    ) -> bool:
        """
        Encode and transmit a message atomically.

        The codec stamps a monotonically increasing msgId at encode time and
        the server kills connections whose msgIds arrive out of order or with
        large gaps (error 1003 "Message gap too large"), so encoding and
        transmission must happen under the same lock. Messages produced while
        disconnected are dropped rather than queued: everything we send is
        ephemeral current state that is re-announced after reconnecting, and
        replaying stale frames with old msgIds gets the new connection killed.

        Returns:
            True if sent, False if dropped (not connected) or send failed
        """
        async with self._send_lock:
            if not (self._ws and self._is_connected):
                return False
            if allowed is not None and not allowed():
                return False
            try:
                await self._ws.send(encode())
                self._last_activity_time = time.monotonic()
                return True
            except Exception as e:
                logger.error(f"Failed to send message: {e}")
                return False

    async def send_state_update(
        self,
        playing_state: int,
        buffer_state: int,
        position_ms: int,
        duration_ms: int,
        queue_item_id: int,
        queue_version_major: int,
        queue_version_minor: int,
        position_timestamp_ms: Optional[int] = None,
    ) -> bool:
        """
        Send renderer state update.

        Returns:
            True if sent successfully
        """
        generation = self._ownership_generation
        return await self._encode_and_send(
            lambda: self._codec.encode_state_update(
                playing_state=playing_state,
                buffer_state=buffer_state,
                position_ms=position_ms,
                duration_ms=duration_ms,
                queue_item_id=queue_item_id,
                queue_version_major=queue_version_major,
                queue_version_minor=queue_version_minor,
                position_timestamp_ms=position_timestamp_ms,
            ),
            allowed=lambda: self.is_renderer_active and generation == self._ownership_generation,
        )

    async def send_volume_changed(self, volume: int) -> bool:
        """
        Send volume changed notification.

        Args:
            volume: Volume level 0-100

        Returns:
            True if sent successfully
        """
        return await self._encode_and_send(lambda: self._codec.encode_volume_changed(volume))

    async def request_next_track(self, still_needed: Callable[[], bool]) -> bool:
        """Advance the server queue only while the originating playback intent is valid."""
        generation = self._ownership_generation
        return await self._encode_and_send(
            self._codec.encode_next_track,
            allowed=lambda: (
                self.is_renderer_active
                and generation == self._ownership_generation
                and still_needed()
            ),
        )

    async def send_file_audio_quality_changed(
        self,
        quality: int,
        sampling_rate: int = 0,
        bit_depth: int = 0,
        nb_channels: int = 0,
    ) -> bool:
        """
        Send file audio quality changed notification.

        Args:
            quality: Quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)
            sampling_rate: Sample rate in Hz (e.g. 44100, 96000). 0 = derive from quality.
            bit_depth: Bit depth (16 or 24). 0 = derive from quality.
            nb_channels: Number of channels. 0 = derive from quality.

        Returns:
            True if sent successfully
        """
        proto_quality = QUALITY_TO_PROTOCOL.get(quality, 4)
        logger.debug(
            f"Sending FILE_AUDIO_QUALITY_CHANGED: qobuz={quality} -> proto={proto_quality}, "
            f"sr={sampling_rate}, bd={bit_depth}, ch={nb_channels}"
        )
        return await self._encode_and_send(
            lambda: self._codec.encode_file_audio_quality_changed(
                quality,
                sampling_rate=sampling_rate,
                bit_depth=bit_depth,
                nb_channels=nb_channels,
            )
        )

    async def send_device_audio_quality_changed(
        self,
        quality: int,
        sampling_rate: int = 0,
        bit_depth: int = 0,
        nb_channels: int = 0,
    ) -> bool:
        """
        Send device audio quality changed notification.

        Args:
            quality: Quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)
            sampling_rate: Max sample rate in Hz. 0 = derive from quality.
            bit_depth: Max bit depth. 0 = derive from quality.
            nb_channels: Number of channels. 0 = derive from quality.

        Returns:
            True if sent successfully
        """
        proto_quality = QUALITY_TO_PROTOCOL.get(quality, 4)
        logger.debug(
            f"Sending DEVICE_AUDIO_QUALITY_CHANGED: qobuz={quality} -> proto={proto_quality}, "
            f"sr={sampling_rate}, bd={bit_depth}, ch={nb_channels}"
        )
        return await self._encode_and_send(
            lambda: self._codec.encode_device_audio_quality_changed(
                quality,
                sampling_rate=sampling_rate,
                bit_depth=bit_depth,
                nb_channels=nb_channels,
            )
        )

    async def send_max_audio_quality_changed(self, quality: int, network_type: int = 1) -> bool:
        """
        Send max audio quality changed notification.

        Args:
            quality: Quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)
            network_type: Network type (1=WiFi)

        Returns:
            True if sent successfully
        """
        proto_quality = QUALITY_TO_PROTOCOL.get(quality, 4)
        logger.debug(f"Sending MAX_AUDIO_QUALITY_CHANGED: qobuz={quality} -> proto={proto_quality}")
        return await self._encode_and_send(
            lambda: self._codec.encode_max_audio_quality_changed(quality, network_type=network_type)
        )
