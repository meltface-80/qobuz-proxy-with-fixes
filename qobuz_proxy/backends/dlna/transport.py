"""
HTTP side of UPnP control: the aiohttp session and SOAP requests with retries.
"""

import asyncio
import logging
from typing import Dict, Optional

import aiohttp
from aiohttp import ClientTimeout

from .soap import (
    NO_RETRY_ERROR_CODES,
    SoapResult,
    build_soap_envelope,
    ms_to_time_string,
    parse_xml_value,
    parse_xml_value_exact,
    time_string_to_ms,
)

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 10.0


class SoapTransport:
    """Owns the HTTP session and sends SOAP actions to one device."""

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None

    @staticmethod
    def _build_session() -> aiohttp.ClientSession:
        """Create the HTTP session used for SOAP requests.

        force_close=True opens a fresh TCP connection for every request and
        closes it after the response. DLNA renderers (gmediarender in
        particular) tend to drop idle keep-alive sockets on their own, which
        surfaces as `[Errno None] Can not write request body` when aiohttp
        picks the half-closed socket out of its pool.
        """
        connector = aiohttp.TCPConnector(force_close=True)
        return aiohttp.ClientSession(
            timeout=ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            connector=connector,
        )

    async def reset_session(self) -> None:
        """Recreate the HTTP session after a connection-level failure.

        When a renderer wedges or drops its control socket, the existing
        session can keep handing out dead connections. Closing and rebuilding
        it lets subsequent SOAP calls recover without restarting the addon.
        """
        logger.debug("Resetting DLNA HTTP session")
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = self._build_session()

    async def _soap_action(
        self,
        url: str,
        service: str,
        action: str,
        args: Dict[str, str],
        max_retries: Optional[int] = None,
    ) -> Optional[str]:
        """Send a SOAP action with retries; the response body, or None on failure."""
        result = await self._soap_action_detailed(url, service, action, args, max_retries)
        return result.body if result.success else None

    async def _soap_action_detailed(
        self,
        url: str,
        service: str,
        action: str,
        args: Dict[str, str],
        max_retries: Optional[int] = None,
    ) -> SoapResult:
        """Send a SOAP action with retries, keeping UPnP error codes."""
        if not url or not self._session:
            logger.warning(f"No URL for service {service}")
            return SoapResult(success=False)

        retries = max_retries if max_retries is not None else MAX_RETRIES
        envelope = build_soap_envelope(service, action, args)
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service}#{action}"',
        }

        last_error = None
        last_error_code: Optional[int] = None
        last_error_desc: Optional[str] = None
        for attempt in range(retries):
            session = self._session
            if session is None:
                break
            try:
                async with session.post(url, data=envelope, headers=headers) as response:
                    if response.status == 200:
                        return SoapResult(success=True, body=await response.text())
                    text = await response.text()
                    logger.warning(f"SOAP {action} failed ({response.status}): {text[:500]}")
                    error_code_str = parse_xml_value(text, "errorCode")
                    last_error_desc = parse_xml_value(text, "errorDescription")
                    if error_code_str:
                        try:
                            last_error_code = int(error_code_str)
                        except ValueError:
                            pass
                    if error_code_str or last_error_desc:
                        logger.warning(
                            f"UPnP error: code={error_code_str}, description={last_error_desc}"
                        )
                    if last_error_code in NO_RETRY_ERROR_CODES:
                        return SoapResult(
                            success=False,
                            error_code=last_error_code,
                            error_description=last_error_desc,
                        )
                    last_error = f"HTTP {response.status}"

            except Exception as e:
                # aiohttp connection/timeout errors often stringify to an empty
                # string, so include the exception type to keep the log useful.
                detail = f"{type(e).__name__}: {e}".rstrip(": ")
                logger.warning(f"SOAP {action} error (attempt {attempt + 1}): {detail}")
                last_error = detail
                # A connection/timeout failure can leave the force_close session
                # holding a half-dead socket. Recreate it so the remaining
                # retries run against a fresh connection.
                if isinstance(e, (aiohttp.ClientConnectionError, asyncio.TimeoutError, OSError)):
                    await self.reset_session()

            if attempt < retries - 1:
                await asyncio.sleep(RETRY_DELAY_SECONDS)

        if retries > 1:
            logger.error(f"SOAP {action} failed after {retries} attempts: {last_error}")
        else:
            logger.warning(f"SOAP {action} failed: {last_error}")
        return SoapResult(
            success=False,
            error_code=last_error_code,
            error_description=last_error_desc,
        )

    # Kept as methods for callers and tests that use the client's helpers.
    @staticmethod
    def _parse_xml_value(xml_text: str, tag_name: str) -> Optional[str]:
        return parse_xml_value(xml_text, tag_name)

    @staticmethod
    def _parse_xml_value_exact(xml_text: str, tag_name: str) -> Optional[str]:
        return parse_xml_value_exact(xml_text, tag_name)

    @staticmethod
    def _ms_to_time_string(ms: int) -> str:
        return ms_to_time_string(ms)

    @staticmethod
    def _time_string_to_ms(time_str: str) -> Optional[int]:
        return time_string_to_ms(time_str)
