"""
DLNA/UPnP SOAP client.

Low-level client for sending UPnP commands to DLNA devices.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import urlparse

from aiohttp import ClientTimeout

from .description import (
    DESCRIPTION_PATHS,
    DLNADeviceInfo,
    parse_device_description,
    parse_scpd_actions,
)
from .soap import (
    UPNP_AV_TRANSPORT,
    UPNP_CONNECTION_MANAGER,
    UPNP_RENDERING_CONTROL,
    SoapResult,
    ms_to_time_string,
    parse_xml_value,
    parse_xml_value_exact,
    time_string_to_ms,
)
from .sonos_queue import SonosQueueMixin
from .transport import MAX_RETRIES, RETRY_DELAY_SECONDS, SoapTransport

logger = logging.getLogger(__name__)

# Minimum interval between volume commands (ms) to avoid overwhelming device
VOLUME_MIN_INTERVAL_MS = 200

# The SCPD is optional information: don't hold up a connect for it.
SCPD_TIMEOUT_SECONDS = 4.0

__all__ = [
    "MAX_RETRIES",
    "RETRY_DELAY_SECONDS",
    "DLNAClient",
    "DLNAClientError",
    "DLNADeviceInfo",
    "DLNAUnsupportedDeviceError",
    "PositionInfo",
    "SoapResult",
]


class DLNAClientError(Exception):
    """DLNA client error."""


class DLNAUnsupportedDeviceError(DLNAClientError):
    """The device answered but cannot act as a renderer (no AVTransport)."""


@dataclass
class PositionInfo:
    """One GetPositionInfo answer; None marks a field the renderer left out."""

    position_ms: Optional[int] = None
    duration_ms: Optional[int] = None
    track_uri: Optional[str] = None


class DLNAClient(SonosQueueMixin, SoapTransport):
    """
    Low-level DLNA/UPnP SOAP client.

    Handles device description fetching and parsing, SOAP action encoding,
    response parsing, and retries for transient failures.
    """

    def __init__(self, ip: str, port: int = 1400, description_url: Optional[str] = None):
        """
        Initialize DLNA client.

        Args:
            ip: Device IP address
            port: Device port (default 1400 for Sonos)
            description_url: Full URL to UPnP device description XML (from SSDP LOCATION)
        """
        super().__init__()
        self.ip = ip
        self.port = port
        self.description_url = description_url
        self.device_info: Optional[DLNADeviceInfo] = None
        self._last_volume_time_ms: float = 0
        self._pending_volume: Optional[int] = None
        self._volume_debounce_task: Optional[asyncio.Task[None]] = None
        self._volume_lock = asyncio.Lock()

    async def connect(self) -> DLNADeviceInfo:
        """
        Connect to device and fetch description.

        Raises:
            DLNAClientError: If connection fails
        """
        self._session = self._build_session()

        try:
            self.device_info = await self._fetch_device_description()
            if not self.device_info.av_transport_url:
                raise DLNAUnsupportedDeviceError(
                    f"Device at {self.ip}:{self.port} does not support AVTransport"
                )
        except BaseException:
            session, self._session = self._session, None
            try:
                await session.close()
            except Exception:
                pass
            raise

        await self._read_av_transport_actions()
        logger.info(f"Connected to DLNA device: {self.device_info.friendly_name}")
        return self.device_info

    async def disconnect(self) -> None:
        """Disconnect and clean up."""
        if self._volume_debounce_task and not self._volume_debounce_task.done():
            self._volume_debounce_task.cancel()
        if self._session:
            await self._session.close()
            self._session = None

    # =========================================================================
    # AVTransport Actions
    # =========================================================================

    async def av_action(
        self, action: str, args: Optional[Dict[str, str]] = None, max_retries: Optional[int] = None
    ) -> SoapResult:
        """Send an AVTransport action (InstanceID 0) and return the full result."""
        if not self.device_info:
            return SoapResult(success=False)
        return await self._soap_action_detailed(
            self.device_info.av_transport_url,
            UPNP_AV_TRANSPORT,
            action,
            {"InstanceID": "0", **(args or {})},
            max_retries,
        )

    async def _av_ok(self, action: str, args: Optional[Dict[str, str]] = None) -> bool:
        if not self.device_info:
            return False
        response = await self._soap_action(
            self.device_info.av_transport_url,
            UPNP_AV_TRANSPORT,
            action,
            {"InstanceID": "0", **(args or {})},
        )
        return response is not None

    async def _av_query(self, action: str) -> Optional[str]:
        if not self.device_info:
            return None
        return await self._soap_action(
            self.device_info.av_transport_url,
            UPNP_AV_TRANSPORT,
            action,
            {"InstanceID": "0"},
        )

    async def set_av_transport_uri(self, url: str, metadata: str = "") -> bool:
        """Set the URI for playback (with DIDL-Lite metadata)."""
        return await self._av_ok(
            "SetAVTransportURI", {"CurrentURI": url, "CurrentURIMetaData": metadata}
        )

    async def play(self, speed: str = "1") -> bool:
        """Start playback."""
        return await self._av_ok("Play", {"Speed": speed})

    async def pause(self) -> bool:
        """Pause playback."""
        return await self._av_ok("Pause")

    async def pause_detailed(self) -> SoapResult:
        """Pause playback, keeping the UPnP error when the renderer refuses."""
        return await self.av_action("Pause")

    async def stop(self) -> bool:
        """Stop playback."""
        return await self._av_ok("Stop")

    async def seek(self, position_ms: int) -> bool:
        """Seek to a position in the current track."""
        result = await self.av_action(
            "Seek", {"Unit": "REL_TIME", "Target": ms_to_time_string(position_ms)}
        )
        return result.success

    async def get_transport_info(self) -> Optional[str]:
        """Current transport state ("PLAYING", "STOPPED", ...); None if the read failed."""
        response = await self._av_query("GetTransportInfo")
        if response:
            return parse_xml_value(response, "CurrentTransportState")
        return None

    async def get_position_details(self) -> Optional[PositionInfo]:
        """RelTime, TrackDuration and TrackURI from one GetPositionInfo call."""
        response = await self._av_query("GetPositionInfo")
        if not response:
            logger.debug("GetPositionInfo: No response from SOAP action")
            return None
        rel_time = parse_xml_value(response, "RelTime")
        info = PositionInfo(
            position_ms=time_string_to_ms(rel_time),
            duration_ms=time_string_to_ms(parse_xml_value_exact(response, "TrackDuration")),
            track_uri=parse_xml_value_exact(response, "TrackURI"),
        )
        if rel_time and info.position_ms is None:
            logger.debug(f"GetPositionInfo: unparseable RelTime {rel_time!r}")
        return info

    async def get_position_info(self) -> Optional[int]:
        """Current position in milliseconds; None if unknown."""
        details = await self.get_position_details()
        return details.position_ms if details else None

    async def get_track_uri(self) -> Optional[str]:
        """
        URI of the current track via GetPositionInfo.TrackURI.

        Unlike GetMediaInfo.CurrentURI (which returns the queue URI for
        queue-based playback), this returns the actual audio track URL.
        """
        details = await self.get_position_details()
        return details.track_uri if details else None

    async def set_next_av_transport_uri(self, url: str, metadata: str = "") -> SoapResult:
        """Set the next URI for gapless playback."""
        return await self.av_action(
            "SetNextAVTransportURI",
            {"NextURI": url, "NextURIMetaData": metadata},
            max_retries=1,
        )

    async def get_media_info(self) -> Optional[str]:
        """
        CurrentURI from GetMediaInfo.

        Returns "" when the renderer has nothing loaded, None if the request
        failed.
        """
        response = await self._av_query("GetMediaInfo")
        if response:
            return parse_xml_value_exact(response, "CurrentURI") or ""
        return None

    # =========================================================================
    # RenderingControl Actions
    # =========================================================================

    async def get_volume(self) -> Optional[int]:
        """Current volume 0-100; None if unknown."""
        if not self.device_info or not self.device_info.rendering_control_url:
            return None

        response = await self._soap_action(
            self.device_info.rendering_control_url,
            UPNP_RENDERING_CONTROL,
            "GetVolume",
            {"InstanceID": "0", "Channel": "Master"},
        )

        if response:
            vol_str = parse_xml_value(response, "CurrentVolume")
            if vol_str:
                try:
                    return int(vol_str)
                except ValueError:
                    return None
        return None

    async def set_volume(self, volume: int) -> bool:
        """
        Set volume with debouncing.

        Rate limits volume commands but ensures the final value is always sent.
        """
        if not self.device_info or not self.device_info.rendering_control_url:
            logger.warning("Cannot set volume: no RenderingControl URL")
            return False

        async with self._volume_lock:
            now_ms = time.time() * 1000
            elapsed = now_ms - self._last_volume_time_ms

            if elapsed < VOLUME_MIN_INTERVAL_MS:
                # Too soon - store pending value and schedule delayed send
                self._pending_volume = volume
                if self._volume_debounce_task is None or self._volume_debounce_task.done():
                    delay_ms = VOLUME_MIN_INTERVAL_MS - elapsed
                    self._volume_debounce_task = asyncio.create_task(
                        self._send_pending_volume(delay_ms / 1000.0)
                    )
                    logger.debug(f"Debouncing SetVolume({volume}), will send in {delay_ms:.0f}ms")
                return True

            # Clear any pending volume since we're sending now
            self._pending_volume = None
            return await self._do_set_volume(volume)

    async def _send_pending_volume(self, delay: float) -> None:
        """Send pending volume after delay."""
        await asyncio.sleep(delay)
        async with self._volume_lock:
            if self._pending_volume is not None:
                volume = self._pending_volume
                self._pending_volume = None
                await self._do_set_volume(volume)

    async def _do_set_volume(self, volume: int) -> bool:
        """Actually send the volume command."""
        if not self.device_info or not self.device_info.rendering_control_url:
            return False
        self._last_volume_time_ms = time.time() * 1000
        logger.debug(f"SetVolume({volume}) to {self.device_info.rendering_control_url}")

        result = await self._soap_action(
            self.device_info.rendering_control_url,
            UPNP_RENDERING_CONTROL,
            "SetVolume",
            {"InstanceID": "0", "Channel": "Master", "DesiredVolume": str(volume)},
            max_retries=1,  # Don't retry volume commands (UI will send new ones)
        )
        return result is not None

    # =========================================================================
    # ConnectionManager Actions
    # =========================================================================

    async def get_protocol_info(self) -> Optional[str]:
        """Sink string from ConnectionManager GetProtocolInfo; None if unsupported."""
        if not self.device_info or not self.device_info.connection_manager_url:
            logger.debug("ConnectionManager not available on this device")
            return None

        response = await self._soap_action(
            self.device_info.connection_manager_url,
            UPNP_CONNECTION_MANAGER,
            "GetProtocolInfo",
            {},
        )

        if response:
            sink = parse_xml_value(response, "Sink")
            if sink:
                # Log the full Sink (not truncated): capability-detection bugs
                # are impossible to diagnose without the exact advertised types.
                logger.debug(f"GetProtocolInfo Sink ({len(sink)} chars): {sink}")
                return sink
        return None

    # =========================================================================
    # Description
    # =========================================================================

    async def _fetch_device_description(self) -> DLNADeviceInfo:
        """Fetch and parse device description XML.

        If a description_url is available (from SSDP LOCATION or config), it is
        tried first.  Falls back to probing well-known paths on ip:port.
        """
        if not self._session:
            raise DLNAClientError("Session not initialized")

        candidates: list[tuple[str, str]] = []
        if self.description_url:
            parsed = urlparse(self.description_url)
            base = f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"
            candidates.append((self.description_url, base))
        base = f"http://{self.ip}:{self.port}"
        candidates += [(base + path, base) for path in DESCRIPTION_PATHS]

        for url, base_url in candidates:
            try:
                async with self._session.get(url) as response:
                    if response.status == 200:
                        xml_text = await response.text()
                        logger.debug(f"Found device description at {url}")
                        return parse_device_description(xml_text, base_url, url)
            except Exception as e:
                logger.debug(f"Device description {url} failed: {e}")

        raise DLNAClientError(f"Could not find device description for {self.ip}:{self.port}")

    def _parse_device_description(
        self, xml_text: str, base_url: str, doc_url: str = ""
    ) -> DLNADeviceInfo:
        """Parse device description XML (see ``description.parse_device_description``)."""
        return parse_device_description(xml_text, base_url, doc_url)

    async def _read_av_transport_actions(self) -> None:
        """Read which actions the AVTransport SCPD lists (best effort)."""
        info = self.device_info
        if not info or not info.av_transport_scpd_url or not self._session:
            return
        try:
            async with self._session.get(
                info.av_transport_scpd_url, timeout=ClientTimeout(total=SCPD_TIMEOUT_SECONDS)
            ) as response:
                if response.status == 200:
                    info.av_transport_actions = parse_scpd_actions(await response.text())
        except Exception as e:
            logger.debug(f"AVTransport SCPD {info.av_transport_scpd_url} failed: {e}")
        if info.av_transport_actions is not None:
            logger.debug(f"AVTransport actions: {', '.join(sorted(info.av_transport_actions))}")
