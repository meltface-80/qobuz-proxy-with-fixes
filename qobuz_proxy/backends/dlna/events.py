"""
UPnP (GENA) event subscription for a renderer's AVTransport service.

The renderer POSTs ``NOTIFY`` requests with a ``LastChange`` document when its
transport state changes. qobuz-proxy only uses them to poll at once rather
than at the next interval; the poll loop stays the single place where state
is interpreted, and keeps running for renderers whose events are late, poor
or missing. Subscriptions are renewed before they expire, and re-made with
back-off when a renderer stops answering.
"""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from typing import Callable, Optional

import aiohttp

from .soap import local_name

logger = logging.getLogger(__name__)

SUBSCRIPTION_SECONDS = 1800
# Renew when this share of the granted time has passed.
RENEW_AT = 0.8
MIN_RENEW_SECONDS = 30.0
RETRY_DELAYS_SECONDS = (30.0, 60.0, 120.0, 300.0)
REQUEST_TIMEOUT_SECONDS = 5.0


def parse_last_change(body: str) -> dict[str, str]:
    """State variables in a NOTIFY body: LastChange values flattened, then the rest.

    ``{"TransportState": "STOPPED", "CurrentTrackURI": "..."}``; empty when
    the body cannot be parsed.
    """
    values: dict[str, str] = {}
    try:
        propertyset = ET.fromstring(body)
    except ET.ParseError:
        return values
    for prop in propertyset.iter():
        if local_name(prop.tag) != "property":
            continue
        for var in prop:
            name = local_name(var.tag)
            text = var.text or ""
            if name != "LastChange":
                values[name] = text
                continue
            try:
                event = ET.fromstring(text)
            except ET.ParseError:
                continue
            for elem in event.iter():
                val = elem.get("val")
                if val is not None and local_name(elem.tag) != "InstanceID":
                    values[local_name(elem.tag)] = val
    return values


def _granted_seconds(timeout_header: str) -> float:
    match = re.search(r"Second-(\d+)", timeout_header or "", re.IGNORECASE)
    return float(match.group(1)) if match else float(SUBSCRIPTION_SECONDS)


class EventSubscription:
    """One GENA subscription, kept alive until ``stop()``."""

    def __init__(
        self,
        event_url: str,
        callback_url: str,
        on_event: Callable[[dict[str, str]], None],
        name: str = "",
    ):
        self._event_url = event_url
        self._callback_url = callback_url
        self._on_event = on_event
        self._name = name
        self._sid: Optional[str] = None
        self._task: Optional[asyncio.Task[None]] = None

    @property
    def active(self) -> bool:
        """Whether the renderer accepted the subscription."""
        return self._sid is not None

    async def start(self) -> bool:
        """Subscribe and keep the subscription renewed; False if refused."""
        granted = await self._subscribe()
        self._task = asyncio.create_task(self._keep_alive(granted))
        return granted is not None

    async def stop(self) -> None:
        """Cancel renewals and unsubscribe (best effort)."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        sid, self._sid = self._sid, None
        if sid:
            await self._request("UNSUBSCRIBE", {"SID": sid})

    def handle_notify(self, sid: str, body: str) -> bool:
        """Deliver a NOTIFY; False if it is not for this subscription."""
        if not self._sid or sid != self._sid:
            return False
        values = parse_last_change(body)
        if values:
            self._on_event(values)
        return True

    async def _subscribe(self) -> Optional[float]:
        """New subscription, or a renewal when one exists; seconds granted or None."""
        if self._sid:
            headers = {"SID": self._sid, "TIMEOUT": f"Second-{SUBSCRIPTION_SECONDS}"}
        else:
            headers = {
                "CALLBACK": f"<{self._callback_url}>",
                "NT": "upnp:event",
                "TIMEOUT": f"Second-{SUBSCRIPTION_SECONDS}",
            }
        response = await self._request("SUBSCRIBE", headers)
        if response is None:
            self._sid = None
            return None
        status, response_headers = response
        sid = response_headers.get("SID")
        if status != 200 or not sid:
            logger.debug(f"[{self._name}] Event subscription refused (HTTP {status})")
            self._sid = None
            return None
        if self._sid is None:
            logger.info(f"[{self._name}] Subscribed to renderer transport events")
        self._sid = sid
        return _granted_seconds(response_headers.get("TIMEOUT", ""))

    async def _keep_alive(self, granted: Optional[float]) -> None:
        failures = 0
        while True:
            if granted is not None:
                failures = 0
                await asyncio.sleep(max(MIN_RENEW_SECONDS, granted * RENEW_AT))
            else:
                delay = RETRY_DELAYS_SECONDS[min(failures, len(RETRY_DELAYS_SECONDS) - 1)]
                failures += 1
                await asyncio.sleep(delay)
            granted = await self._subscribe()
            if granted is None and self._sid is None:
                # A renewal that failed may mean the renderer rebooted and
                # forgot us: the next attempt makes a new subscription.
                logger.debug(f"[{self._name}] Event subscription lost; retrying")

    async def _request(
        self, method: str, headers: dict[str, str]
    ) -> Optional[tuple[int, dict[str, str]]]:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, self._event_url, headers=headers) as resp:
                    return resp.status, {k.upper(): v for k, v in resp.headers.items()}
        except Exception as e:
            logger.debug(f"[{self._name}] {method} {self._event_url} failed: {e}")
            return None
