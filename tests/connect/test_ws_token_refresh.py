"""Tests for WsManager token refresh: self-minted tokens and the idle-gated reconnect."""

import asyncio
import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from qobuz_proxy.auth.tokens import WSToken
from qobuz_proxy.connect.types import ConnectTokens, JWTConnectToken
from qobuz_proxy.connect.ws_manager import (
    TOKEN_REFRESH_IDLE_PERIOD,
    TokenRefreshRequired,
    WsManager,
)


class TestTokenRefresher:
    """Tests for self-minted WS tokens (qws/createToken flow)."""

    @pytest.mark.asyncio
    async def test_refresher_supplies_token_when_expired(
        self, ws_manager: WsManager, valid_tokens: ConnectTokens
    ) -> None:
        """An expiring token is replaced via the refresher without waiting for the app."""
        expired = ConnectTokens(
            session_id=str(uuid.uuid4()),
            ws_token=JWTConnectToken(jwt="expired", exp=1, endpoint="wss://test/ws"),
        )
        ws_manager.set_tokens(expired)
        ws_manager._should_run = True

        fresh = WSToken(jwt="fresh_jwt", exp_s=9999999999, endpoint="wss://test/ws")
        refresher = AsyncMock(return_value=fresh)
        ws_manager.set_token_refresher(refresher)

        result = await asyncio.wait_for(ws_manager._wait_for_valid_token(buffer_s=60), timeout=1.0)

        assert result is True
        assert ws_manager._ws_token is fresh
        refresher.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refresher_not_called_when_token_valid(
        self, ws_manager: WsManager, valid_tokens: ConnectTokens
    ) -> None:
        """A valid token is used as-is; the refresher is not consulted."""
        ws_manager.set_tokens(valid_tokens)
        ws_manager._should_run = True

        refresher = AsyncMock()
        ws_manager.set_token_refresher(refresher)

        result = await asyncio.wait_for(ws_manager._wait_for_valid_token(buffer_s=60), timeout=1.0)

        assert result is True
        refresher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_app_tokens_when_refresher_fails(
        self, ws_manager: WsManager, valid_tokens: ConnectTokens
    ) -> None:
        """When minting fails, app-provided tokens still unblock the wait."""
        expired = ConnectTokens(
            session_id=str(uuid.uuid4()),
            ws_token=JWTConnectToken(jwt="expired", exp=1, endpoint="wss://test/ws"),
        )
        ws_manager.set_tokens(expired)
        ws_manager._should_run = True
        ws_manager.set_token_refresher(AsyncMock(return_value=None))

        wait_task = asyncio.create_task(ws_manager._wait_for_valid_token(buffer_s=60))
        await asyncio.sleep(0)
        assert wait_task.done() is False

        ws_manager.set_tokens(valid_tokens)

        assert await asyncio.wait_for(wait_task, timeout=1.0) is True

    @pytest.mark.asyncio
    async def test_refresher_exception_does_not_crash_wait(
        self, ws_manager: WsManager, valid_tokens: ConnectTokens
    ) -> None:
        """A refresher that raises is treated as a failed mint."""
        expired = ConnectTokens(
            session_id=str(uuid.uuid4()),
            ws_token=JWTConnectToken(jwt="expired", exp=1, endpoint="wss://test/ws"),
        )
        ws_manager.set_tokens(expired)
        ws_manager._should_run = True
        ws_manager.set_token_refresher(AsyncMock(side_effect=RuntimeError("api down")))

        wait_task = asyncio.create_task(ws_manager._wait_for_valid_token(buffer_s=60))
        await asyncio.sleep(0)
        assert wait_task.done() is False

        ws_manager.set_tokens(valid_tokens)

        assert await asyncio.wait_for(wait_task, timeout=1.0) is True


class TestTokenRefreshIdleGate:
    """An expiring token only forces a reconnect once the session goes quiet.

    Swapping the token means dropping and re-establishing the connection, and
    the Qobuz app pauses a renderer that disappears and comes back mid-track.
    So the reconnect waits for a lull, matching the StreamCore32 reference.
    """

    @staticmethod
    def _set_expiring_token(ws_manager: WsManager) -> None:
        ws_manager.set_tokens(
            ConnectTokens(
                session_id=str(uuid.uuid4()),
                ws_token=JWTConnectToken(jwt="expiring", exp=1, endpoint="wss://test/ws"),
            )
        )

    def test_refresh_deferred_while_session_is_active(self, ws_manager: WsManager) -> None:
        """A track still streaming keeps sending state, so the reconnect waits."""
        self._set_expiring_token(ws_manager)
        ws_manager._last_activity_time = time.monotonic()

        ws_manager._check_token_refresh()

        assert ws_manager._refresh_deferred is True

    def test_refresh_triggered_once_session_is_idle(self, ws_manager: WsManager) -> None:
        """With nothing sent for the idle period, the reconnect goes ahead."""
        self._set_expiring_token(ws_manager)
        ws_manager._last_activity_time = time.monotonic() - TOKEN_REFRESH_IDLE_PERIOD - 1

        with pytest.raises(TokenRefreshRequired):
            ws_manager._check_token_refresh()

    def test_healthy_token_never_triggers_refresh(
        self, ws_manager: WsManager, valid_tokens: ConnectTokens
    ) -> None:
        """An idle connection with a token far from expiry is left alone."""
        ws_manager.set_tokens(valid_tokens)
        ws_manager._last_activity_time = time.monotonic() - TOKEN_REFRESH_IDLE_PERIOD - 1

        ws_manager._check_token_refresh()

        assert ws_manager._refresh_deferred is False

    @pytest.mark.asyncio
    async def test_sending_a_message_restarts_the_idle_clock(self, ws_manager: WsManager) -> None:
        """Any outgoing frame postpones a refresh that was otherwise due."""
        self._set_expiring_token(ws_manager)
        ws_manager._last_activity_time = time.monotonic() - TOKEN_REFRESH_IDLE_PERIOD - 1
        ws_manager._ws = AsyncMock()
        ws_manager._is_connected = True

        assert await ws_manager.send_message(b"state_update") is True

        ws_manager._check_token_refresh()

    @pytest.mark.asyncio
    async def test_inbound_command_restarts_the_idle_clock(self, ws_manager: WsManager) -> None:
        """A play command after a quiet spell means the session is about to get busy;
        the reconnect must not land while the track is still loading."""
        self._set_expiring_token(ws_manager)
        ws_manager._last_activity_time = time.monotonic() - TOKEN_REFRESH_IDLE_PERIOD - 1
        ws_manager.register_handler(41, MagicMock())  # SET_STATE
        ws_manager._is_connected = True
        batch = MagicMock()
        batch.messages = [MagicMock(messageType=41)]
        ws_manager._codec.decode_qconnect_batch = MagicMock(return_value=batch)

        await ws_manager._handle_payload(MagicMock(payload=b"frame"))

        ws_manager._check_token_refresh()  # raises TokenRefreshRequired if still idle

    def test_deferral_is_logged_only_once(
        self, ws_manager: WsManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The check runs every second while waiting; it must not spam the log."""
        self._set_expiring_token(ws_manager)
        ws_manager._last_activity_time = time.monotonic()

        with caplog.at_level("INFO", logger="qobuz_proxy.connect.ws_manager"):
            for _ in range(5):
                ws_manager._check_token_refresh()

        assert sum("deferring refresh" in r.message for r in caplog.records) == 1
