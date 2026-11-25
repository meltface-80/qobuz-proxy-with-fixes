"""
Session setup for the Qobuz Connect WebSocket.

Obtains a usable WS token, then authenticates, subscribes to the session
channel and joins the session as a renderer on a freshly opened connection.
"""

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from websockets import ClientConnection

from qobuz_proxy.auth.tokens import WSToken
from qobuz_proxy.config import DEVICE_TYPES, Config

from .protocol import ProtocolCodec
from .protocol_types import JoinSessionReason

logger = logging.getLogger(__name__)

TOKEN_MINT_RETRY_DELAY = 30.0  # seconds between self-mint attempts

# Async callback that mints a fresh WS token (returns None on failure)
TokenRefresher = Callable[[], Awaitable[Optional[WSToken]]]


class SessionSetupMixin:
    """
    Token acquisition and the AUTHENTICATE/SUBSCRIBE/JOIN_SESSION handshake.

    The token, session and ownership state it uses is created and maintained
    by ``WsManager``.
    """

    config: Config
    _ws: Optional[ClientConnection]
    _codec: ProtocolCodec
    _device_uuid: bytes
    _ws_token: Optional[WSToken]
    _session_uuid: Optional[bytes]
    _should_run: bool
    _renderer_active: bool
    _active_confirmed: bool
    _activation_request: Optional[object]
    _token_update_event: asyncio.Event
    _token_version: int
    _token_refresher: Optional[TokenRefresher]
    _max_audio_quality: int

    @property
    def _fixed_volume(self) -> bool:
        """Whether this speaker ignores volume commands (DLNA or local backend)."""
        backend = self.config.backend
        if backend.type == "dlna":
            return backend.dlna.fixed_volume
        return backend.local.fixed_volume

    async def _wait_for_valid_token(self, buffer_s: int = 0) -> bool:
        """Obtain a non-expired token, or wait until one arrives or shutdown is requested.

        Tries the token refresher (self-mint via the Qobuz API) first; falls
        back to waiting for the Qobuz app to push fresh tokens, retrying the
        refresher periodically in case the failure was transient.
        """
        logged_wait = False

        while self._should_run:
            if (
                self._ws_token
                and self._ws_token.is_valid()
                and not self._ws_token.is_expired(buffer_s)
            ):
                return True

            if self._token_refresher:
                try:
                    token = await self._token_refresher()
                except Exception as e:
                    logger.warning(f"WS token refresh failed: {e}")
                    token = None
                if token and token.is_valid() and not token.is_expired(buffer_s):
                    self._ws_token = token
                    return True

            if not logged_wait:
                logger.warning("Token expired, waiting for refreshed token from Qobuz app")
                logged_wait = True

            token_version = self._token_version
            self._token_update_event.clear()
            if self._token_version != token_version:
                continue
            if self._token_refresher:
                # Wake up periodically to retry the refresher even if the app
                # never reconnects.
                try:
                    await asyncio.wait_for(
                        self._token_update_event.wait(), timeout=TOKEN_MINT_RETRY_DELAY
                    )
                except asyncio.TimeoutError:
                    pass
            else:
                await self._token_update_event.wait()

        return False

    async def _authenticate(self) -> bool:
        """Send AUTHENTICATE message."""
        if not self._ws_token:
            return False
        auth_frame = self._codec.encode_authenticate(self._ws_token.jwt)
        assert self._ws is not None  # only called on an open connection
        await self._ws.send(auth_frame)
        logger.debug("Sent AUTHENTICATE")
        return True

    async def _subscribe(self) -> bool:
        """Send SUBSCRIBE message."""
        if not self._session_uuid:
            logger.error("No session UUID for subscribe")
            return False

        sub_frame = self._codec.encode_subscribe(self._session_uuid)
        assert self._ws is not None  # only called on an open connection
        await self._ws.send(sub_frame)
        logger.debug("Sent SUBSCRIBE")
        return True

    async def _send_join_session(self) -> None:
        """Send join session message to register as renderer."""
        if not self._session_uuid:
            logger.error("No session UUID for join session")
            return

        activation_request = self._activation_request
        is_active = activation_request is not None or self._renderer_active
        self._active_confirmed = False
        join_frame = self._codec.encode_join_session(
            device_uuid=self._device_uuid,
            friendly_name=self.config.device.name,
            session_uuid=self._session_uuid,
            max_audio_quality=self._max_audio_quality,
            supports_volume_control=not self._fixed_volume,
            device_type=DEVICE_TYPES[self.config.device.device_type],
            is_active=is_active,
            reason=(
                JoinSessionReason.CONTROLLER_REQUEST
                if activation_request is not None
                else JoinSessionReason.RECONNECTION
            ),
        )
        assert self._ws is not None  # only called on an open connection
        await self._ws.send(join_frame)
        if self._activation_request is activation_request:
            self._activation_request = None
        logger.info(
            "[%s] Sent JOIN_SESSION: isActive=%s, cause=%s, max_quality=%s",
            self.config.device.name,
            is_active,
            "app selection" if activation_request is not None else "reconnect",
            self._max_audio_quality,
        )
