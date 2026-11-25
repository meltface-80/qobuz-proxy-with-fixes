"""Helpers for driving a WsManager in tests: SET_ACTIVE delivery, JOIN and report frames."""

from qobuz_proxy.connect.ws_manager import WsManager


async def _deliver_active(manager: WsManager, active: bool) -> None:
    from qobuz_proxy.connect.protocol import DecodedMessage, MessageType
    from qobuz_proxy.proto import qconnect_payload_pb2 as pb

    batch = pb.QConnectBatch()
    message = batch.messages.add(messageType=43)
    message.srvrRndrSetActive.active = active
    await manager._handle_payload(
        DecodedMessage(msg_type=MessageType.PAYLOAD, payload=batch.SerializeToString())
    )


def _last_join(manager: WsManager):
    frame = manager._ws.send.call_args.args[0]
    decoded = manager._codec.decode_frame(frame)
    return manager._codec.decode_qconnect_batch(decoded.payload).messages[0].rndrSrvrJoinSession


async def _send_report(manager: WsManager) -> bool:
    return await manager.send_state_update(
        playing_state=1,
        buffer_state=2,
        position_ms=0,
        duration_ms=60000,
        queue_item_id=1,
        queue_version_major=1,
        queue_version_minor=0,
    )
