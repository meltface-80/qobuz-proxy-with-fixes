"""
Protocol buffer message encoding and decoding.

Handles the custom frame format used by Qobuz Connect WebSocket protocol.
"""

import logging
import time
from typing import Optional

from .framing import decode_varint, pack_frame
from .pb import QConnectBatch, QConnectMessage, RendererState, common_pb2, envelope_pb2, payload_pb2
from .protocol_types import (
    PROTOCOL_TO_QUALITY,
    QUALITY_AUDIO_PROPERTIES,
    QUALITY_TO_PROTOCOL,
    DecodedMessage,
    JoinSessionReason,
    MessageType,
    QConnectMessageType,
    QConnectProto,
)

__all__ = [
    "INT32_MAX",
    "INT32_MIN",
    "PROTOCOL_TO_QUALITY",
    "QUALITY_AUDIO_PROPERTIES",
    "QUALITY_TO_PROTOCOL",
    "DecodedMessage",
    "JoinSessionReason",
    "MessageType",
    "ProtocolCodec",
    "QConnectMessageType",
    "QConnectProto",
]

logger = logging.getLogger(__name__)


INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1


class ProtocolCodec:
    """
    Encodes and decodes Qobuz Connect protocol messages.

    Frame format: [msg_type: 1 byte][length: varint][payload: N bytes]
    """

    def __init__(self, device_uuid: bytes):
        """
        Initialize codec.

        Args:
            device_uuid: 16-byte device UUID
        """
        self.device_uuid = device_uuid
        # Envelope msgId and batch messagesId are independent sequences.
        # The server validates that envelope msgIds on a connection increase
        # with only small gaps, so the envelope counter must not be consumed
        # by anything else.
        self._msg_counter = 0
        self._batch_counter = 0

    def _next_msg_id(self) -> int:
        """Get next envelope message ID."""
        self._msg_counter += 1
        return self._msg_counter

    def _next_batch_id(self) -> int:
        """Get next QConnectBatch messagesId."""
        self._batch_counter += 1
        return self._batch_counter

    def _now_ms(self) -> int:
        """Current time in milliseconds."""
        return int(time.time() * 1000)

    # -------------------------------------------------------------------------
    # Encoding
    # -------------------------------------------------------------------------

    def encode_authenticate(self, jwt: str) -> bytes:
        """
        Encode AUTHENTICATE message.

        Args:
            jwt: WebSocket JWT token

        Returns:
            Encoded frame bytes
        """
        msg = envelope_pb2.Authenticate()
        msg.msgId = self._next_msg_id()
        msg.msgDate = self._now_ms()
        msg.jwt = jwt

        return pack_frame(MessageType.AUTHENTICATE, msg.SerializeToString())

    def encode_subscribe(self, session_uuid: bytes) -> bytes:
        """
        Encode SUBSCRIBE message.

        Args:
            session_uuid: 16-byte session UUID to subscribe to

        Returns:
            Encoded frame bytes
        """
        msg = envelope_pb2.Subscribe()
        msg.msgId = self._next_msg_id()
        msg.msgDate = self._now_ms()
        msg.proto = QConnectProto.QP_QCONNECT
        msg.channels.append(session_uuid)

        return pack_frame(MessageType.SUBSCRIBE, msg.SerializeToString())

    def encode_payload(
        self,
        inner_payload: bytes,
        dest_channels: Optional[list[bytes]] = None,
    ) -> bytes:
        """
        Encode PAYLOAD message.

        Args:
            inner_payload: Serialized QConnectBatch
            dest_channels: Optional destination channel UUIDs

        Returns:
            Encoded frame bytes
        """
        msg = envelope_pb2.Payload()
        msg.msgId = self._next_msg_id()
        msg.msgDate = self._now_ms()
        msg.proto = QConnectProto.QP_QCONNECT
        msg.src = self.device_uuid
        if dest_channels:
            msg.dests.extend(dest_channels)
        msg.payload = inner_payload

        return pack_frame(MessageType.PAYLOAD, msg.SerializeToString())

    def encode_state_update(
        self,
        playing_state: int,
        buffer_state: int,
        position_ms: int,
        duration_ms: int,
        queue_item_id: int,
        queue_version_major: int,
        queue_version_minor: int,
        position_timestamp_ms: Optional[int] = None,
    ) -> bytes:
        """
        Encode renderer state update message.

        The app interpolates current playback position as
        ``value + (now - timestamp)``. Pass ``position_timestamp_ms`` as the
        real-time at which ``position_ms`` was actually measured so the bar
        advances smoothly between heartbeats. If omitted, falls back to "now",
        which under-counts by the polling lag.

        Returns:
            Encoded frame bytes ready to send
        """
        # Build inner QueueRendererState
        state = common_pb2.QueueRendererState()
        state.playingState = playing_state
        state.bufferState = buffer_state

        position = common_pb2.Position()
        position.timestamp = (
            position_timestamp_ms if position_timestamp_ms is not None else self._now_ms()
        )
        position.value = position_ms
        state.currentPosition.CopyFrom(position)

        state.duration = duration_ms
        # currentQueueItemId is an int32. The app's "no item" sentinel
        # (2**64 - 1) made every state report fail with "Value out of range"
        # (upstream #44); leave the field out instead of dropping the report.
        if INT32_MIN <= queue_item_id <= INT32_MAX:
            state.currentQueueItemId = queue_item_id
        else:
            logger.debug(f"Not reporting out-of-range queue item id {queue_item_id}")

        queue_ver = common_pb2.QueueVersion()
        queue_ver.major = queue_version_major
        queue_ver.minor = queue_version_minor
        state.queueVersion.CopyFrom(queue_ver)

        # Wrap in RndrSrvrStateUpdated
        state_updated = payload_pb2.RndrSrvrStateUpdated()
        state_updated.state.CopyFrom(state)

        # Wrap in QConnectMessage
        qc_msg = payload_pb2.QConnectMessage()
        qc_msg.messageType = QConnectMessageType.RNDR_SRVR_STATE_UPDATED
        qc_msg.rndrSrvrStateUpdated.CopyFrom(state_updated)

        return self._encode_batch(qc_msg)

    def encode_join_session(
        self,
        device_uuid: bytes,
        friendly_name: str,
        session_uuid: bytes,
        initial_state: Optional[RendererState] = None,
        max_audio_quality: int = 27,
        *,
        supports_volume_control: bool = True,
        device_type: int = common_pb2.DEVICE_TYPE_SPEAKER,
        is_active: bool = False,
        reason: JoinSessionReason = JoinSessionReason.RECONNECTION,
    ) -> bytes:
        """
        Encode join session message (sent when connecting).

        Args:
            device_uuid: 16-byte device UUID
            friendly_name: Device display name
            session_uuid: 16-byte session UUID to join
            initial_state: Optional initial renderer state
            max_audio_quality: Max quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)
            supports_volume_control: Whether the app may control device volume.
            device_type: DeviceType enum value; selects the icon shown in the app.
            is_active: Request activation or restore this renderer's session ownership.
            reason: Distinguish explicit selection from transport reconnection.

        Returns:
            Encoded frame bytes
        """
        # Build DeviceInfo
        device_info = common_pb2.DeviceInfo()
        device_info.deviceUuid = device_uuid
        device_info.friendlyName = friendly_name
        device_info.brand = "QobuzProxy"
        device_info.model = "Python"
        device_info.type = device_type
        device_info.softwareVersion = "py-1.0.0"

        # Device capabilities - map quality ID to protocol value
        proto_quality = QUALITY_TO_PROTOCOL.get(max_audio_quality, 4)
        caps = common_pb2.DeviceCapabilities()
        caps.minAudioQuality = 1
        caps.maxAudioQuality = proto_quality
        caps.volumeRemoteControl = 2 if supports_volume_control else 1  # CONTROLLER / DEVICE
        device_info.capabilities.CopyFrom(caps)

        # Build JoinSession message
        join = payload_pb2.RndrSrvrJoinSession()
        join.sessionUuid = session_uuid  # Required!
        join.deviceInfo.CopyFrom(device_info)
        join.reason = reason
        join.isActive = is_active

        if initial_state:
            join.initialState.CopyFrom(initial_state)

        # Wrap in QConnectMessage
        qc_msg = payload_pb2.QConnectMessage()
        qc_msg.messageType = QConnectMessageType.RNDR_SRVR_JOIN_SESSION
        qc_msg.rndrSrvrJoinSession.CopyFrom(join)

        return self._encode_batch(qc_msg)

    def encode_volume_changed(self, volume: int) -> bytes:
        """
        Encode volume changed notification.

        Args:
            volume: Volume level 0-100

        Returns:
            Encoded frame bytes
        """
        vol_msg = payload_pb2.RndrSrvrVolumeChanged()
        vol_msg.volume = volume

        qc_msg = payload_pb2.QConnectMessage()
        qc_msg.messageType = QConnectMessageType.RNDR_SRVR_VOLUME_CHANGED
        qc_msg.rndrSrvrVolumeChanged.CopyFrom(vol_msg)

        return self._encode_batch(qc_msg)

    def encode_next_track(self) -> bytes:
        """Request the server's next queue item using renderer action NEXT=2."""
        message = payload_pb2.QConnectMessage()
        message.messageType = QConnectMessageType.RNDR_SRVR_RENDERER_ACTION
        message.rndrSrvrRendererAction.action = 2
        return self._encode_batch(message)

    def encode_file_audio_quality_changed(
        self,
        quality: int,
        sampling_rate: int = 0,
        bit_depth: int = 0,
        nb_channels: int = 0,
    ) -> bytes:
        """
        Encode file audio quality changed notification.

        This reports the quality of the currently playing file.

        Args:
            quality: Quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)
            sampling_rate: Sample rate in Hz (e.g. 44100, 96000). 0 = derive from quality.
            bit_depth: Bit depth (16 or 24). 0 = derive from quality.
            nb_channels: Number of channels. 0 = derive from quality.

        Returns:
            Encoded frame bytes
        """
        proto_quality = QUALITY_TO_PROTOCOL.get(quality, 4)
        defaults = QUALITY_AUDIO_PROPERTIES.get(quality, (44100, 16, 2))
        sampling_rate = sampling_rate or defaults[0]
        bit_depth = bit_depth or defaults[1]
        nb_channels = nb_channels or defaults[2]

        quality_msg = payload_pb2.RndrSrvrFileAudioQualityChanged()
        quality_msg.sampling_rate = sampling_rate
        quality_msg.bit_depth = bit_depth
        quality_msg.nb_channels = nb_channels
        quality_msg.audio_quality = proto_quality

        qc_msg = payload_pb2.QConnectMessage()
        qc_msg.messageType = QConnectMessageType.RNDR_SRVR_FILE_AUDIO_QUALITY_CHANGED
        qc_msg.rndrSrvrFileAudioQualityChanged.CopyFrom(quality_msg)

        return self._encode_batch(qc_msg)

    def encode_device_audio_quality_changed(
        self,
        quality: int,
        sampling_rate: int = 0,
        bit_depth: int = 0,
        nb_channels: int = 0,
    ) -> bytes:
        """
        Encode device audio quality changed notification.

        This reports the device's current max audio quality capability.

        Args:
            quality: Quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)
            sampling_rate: Max sample rate in Hz. 0 = derive from quality.
            bit_depth: Max bit depth. 0 = derive from quality.
            nb_channels: Number of channels. 0 = derive from quality.

        Returns:
            Encoded frame bytes
        """
        defaults = QUALITY_AUDIO_PROPERTIES.get(quality, (44100, 16, 2))
        sampling_rate = sampling_rate or defaults[0]
        bit_depth = bit_depth or defaults[1]
        nb_channels = nb_channels or defaults[2]

        quality_msg = payload_pb2.RndrSrvrDeviceAudioQualityChanged()
        quality_msg.sampling_rate = sampling_rate
        quality_msg.bit_depth = bit_depth
        quality_msg.nb_channels = nb_channels

        qc_msg = payload_pb2.QConnectMessage()
        qc_msg.messageType = QConnectMessageType.RNDR_SRVR_DEVICE_AUDIO_QUALITY_CHANGED
        qc_msg.rndrSrvrDeviceAudioQualityChanged.CopyFrom(quality_msg)

        return self._encode_batch(qc_msg)

    def encode_max_audio_quality_changed(self, quality: int, network_type: int = 1) -> bytes:
        """
        Encode max audio quality changed notification.

        This reports when the user changes the quality setting.

        Args:
            quality: Quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)
            network_type: Network type (1=WiFi)

        Returns:
            Encoded frame bytes
        """
        if quality not in QUALITY_TO_PROTOCOL:
            logger.warning(
                f"Unknown quality {quality} for MAX_AUDIO_QUALITY_CHANGED, defaulting to Hi-Res 192k"
            )
        proto_quality = QUALITY_TO_PROTOCOL.get(quality, 4)

        quality_msg = payload_pb2.RndrSrvrMaxAudioQualityChanged()
        quality_msg.audio_quality = proto_quality
        quality_msg.network_type = network_type

        qc_msg = payload_pb2.QConnectMessage()
        qc_msg.messageType = QConnectMessageType.RNDR_SRVR_MAX_AUDIO_QUALITY_CHANGED
        qc_msg.rndrSrvrMaxAudioQualityChanged.CopyFrom(quality_msg)

        return self._encode_batch(qc_msg)

    def _encode_batch(self, message: QConnectMessage) -> bytes:
        """Wrap one QConnectMessage in a QConnectBatch and encode it as a PAYLOAD frame."""
        batch = payload_pb2.QConnectBatch()
        batch.messagesTime = self._now_ms()
        batch.messagesId = self._next_batch_id()
        batch.messages.append(message)
        return self.encode_payload(batch.SerializeToString())

    # -------------------------------------------------------------------------
    # Decoding
    # -------------------------------------------------------------------------

    def decode_frame(self, data: bytes) -> Optional[DecodedMessage]:
        """
        Decode a WebSocket frame.

        Args:
            data: Raw frame bytes

        Returns:
            DecodedMessage or None if invalid
        """
        if len(data) < 2:
            return None

        try:
            msg_type = MessageType(data[0])
        except ValueError:
            logger.warning(f"Unknown message type: {data[0]}")
            return None

        # Decode varint length
        length, offset = decode_varint(data, 1)
        if offset < 0:
            return None

        payload = data[offset : offset + length]

        return self._decode_by_type(msg_type, payload)

    def _decode_by_type(self, msg_type: MessageType, payload: bytes) -> DecodedMessage:
        """Decode payload based on message type."""
        result = DecodedMessage(msg_type=msg_type)

        try:
            if msg_type == MessageType.PAYLOAD:
                msg = envelope_pb2.Payload()
                msg.ParseFromString(payload)
                result.msg_id = msg.msgId
                result.msg_date = msg.msgDate
                result.payload = msg.payload

            elif msg_type == MessageType.ERROR:
                msg = envelope_pb2.Error()
                msg.ParseFromString(payload)
                result.msg_id = msg.msgId
                result.error_code = msg.code
                result.error_message = msg.message

            elif msg_type == MessageType.DISCONNECT:
                msg = envelope_pb2.Disconnect()
                msg.ParseFromString(payload)
                result.msg_id = msg.msgId

        except Exception as e:
            logger.error(f"Failed to decode {msg_type.name}: {e}")

        return result

    def decode_qconnect_batch(self, payload: bytes) -> Optional[QConnectBatch]:
        """
        Decode inner QConnectBatch from PAYLOAD message.

        Args:
            payload: Inner payload bytes

        Returns:
            QConnectBatch or None
        """
        try:
            batch = payload_pb2.QConnectBatch()
            batch.ParseFromString(payload)
            return batch
        except Exception as e:
            logger.error(f"Failed to decode QConnectBatch: {e}")
            return None
