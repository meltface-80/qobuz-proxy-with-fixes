"""
DLNA audio backend.

Implements the AudioBackend interface for DLNA/UPnP renderers. The class is
assembled from layers (see ``backend_base.py``); this module adds connecting,
transport commands and volume.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

from qobuz_proxy.backends.types import (
    BackendInfo,
    BackendTrackMetadata,
    BufferStatus,
    PlaybackState,
)

from .client import DLNAClient, DLNAClientError, DLNAUnsupportedDeviceError
from .description import DLNADeviceInfo
from .didl import content_type_for, format_ms, proxy_key, source_label
from .discovery import DLNADiscovery
from .polling import PollingMixin
from .timing import (  # noqa: F401 (re-exported for callers and tests)
    ARM_AFTER_PLAYING_SECONDS,
    ARM_AFTER_POSITION_MS,
    EARLY_STOP_MARGIN_MS,
    END_STALL_AT_END_SECONDS,
    END_STALL_DELIVERED_SECONDS,
    END_STALL_KNOWN_SECONDS,
    END_STALL_SECONDS,
    NEAR_END_POLL_INTERVAL_SECONDS,
    NEAR_END_WINDOW_MS,
    PLAYBACK_START_GRACE_PERIOD_SECONDS,
    STATE_POLL_INTERVAL_SECONDS,
)

if TYPE_CHECKING:
    from .proxy_server import AudioProxyServer

logger = logging.getLogger(__name__)

# Kept under their old names for callers and tests.
_proxy_key = proxy_key
_source_label = source_label
_format_ms = format_ms

# How long a search for a renderer that moved (new IP) may take.
RELOCATE_TIMEOUT_SECONDS = 3.0


class DLNABackend(PollingMixin):
    """
    DLNA/UPnP audio backend.

    Connects to DLNA renderers and controls playback via SOAP commands.
    Polls device state, woken early by UPnP events where the renderer sends
    them. Expects URLs to be served by the audio proxy server.
    """

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def set_proxy_server(self, proxy: "AudioProxyServer") -> None:
        """Route playback URLs through the audio proxy (and receive its reports)."""
        self._proxy_server = proxy
        proxy.set_delivery_failure_callback(self._on_delivery_failure)
        proxy.set_delivery_complete_callback(self._on_delivery_complete)
        proxy.set_request_callback(self._on_stream_requested)
        logger.info("Audio proxy server configured for DLNA backend")
        self._schedule_events()

    async def connect(self) -> bool:
        """Connect to the renderer (finding it again by UDN if its address changed)."""
        try:
            device_info = await self._connect_client()
            if device_info is None:
                return False

            if device_info.friendly_name:
                self.name = device_info.friendly_name
            self._is_sonos = "sonos" in (device_info.manufacturer or "").lower()
            if self._is_sonos:
                logger.info("Sonos device detected — using queue-based playback")

            await self._discover_capabilities(device_info)
            self._apply_quirks(device_info)
            self._is_connected = True
            self._poll_task = asyncio.create_task(self._poll_state_loop())
            self._schedule_events()
            logger.info(f"Connected to DLNA device: {self.name}")
            return True

        except DLNAUnsupportedDeviceError as e:
            # The device is up but is not a usable renderer: a setup problem
            # the user needs to see, not an offline speaker.
            logger.error(f"DLNA device cannot be used as a renderer: {e}")
            return False
        except DLNAClientError as e:
            # An unreachable renderer is an expected state (powered off,
            # still booting); the caller retries with backoff. Log at info
            # so an offline speaker does not look like a crash in the logs.
            logger.info(f"DLNA device not reachable: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error connecting to DLNA: {e}", exc_info=True)
            return False

    async def _connect_client(self) -> Optional[DLNADeviceInfo]:
        """Connect at the configured address; on failure or a different device, search by UDN."""
        self._client = DLNAClient(self._ip, self._port, description_url=self._description_url)
        try:
            info = await self._client.connect()
        except DLNAUnsupportedDeviceError:
            raise
        except DLNAClientError:
            if not self._udn or not await self._relocate():
                raise
            return await self._client.connect()
        if self._udn and info.udn and info.udn != self._udn:
            logger.warning(
                "[%s] %s:%s now answers as %s, not the configured renderer %s",
                self.name,
                self._ip,
                self._port,
                info.udn,
                self._udn,
            )
            if await self._relocate():
                await self._client.disconnect()
                return await self._client.connect()
        return info

    async def _relocate(self) -> bool:
        """Find the configured UDN on the network (e.g. after a DHCP change)."""
        logger.info(f"[{self.name}] Searching the network for renderer {self._udn}")
        device = await DLNADiscovery(bind_address=self._bind_address).find(
            self._udn, timeout=RELOCATE_TIMEOUT_SECONDS
        )
        if device is None or (device.ip == self._ip and device.port == self._port):
            return False
        logger.info(f"[{self.name}] Renderer moved: {self._ip}:{self._port} -> {device.location}")
        old_client = self._client
        self._ip, self._port = device.ip, device.port
        self._description_url = device.location or None
        self._client = DLNAClient(self._ip, self._port, description_url=self._description_url)
        if old_client is not None:
            await old_client.disconnect()
        return True

    async def disconnect(self) -> None:
        """Disconnect from DLNA device."""
        self._is_connected = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._events_task:
            self._events_task.cancel()
            try:
                await self._events_task
            except (asyncio.CancelledError, Exception):
                pass
            self._events_task = None
        if self._events:
            await self._events.stop()
            self._events = None
            self._events_active = False
        if self._proxy_server:
            self._proxy_server.unregister_event_handler(self._event_token)
        if self._client:
            try:
                await self.stop()
            except Exception:
                pass
            await self._client.disconnect()
        logger.info(f"Disconnected from DLNA device: {self.name}")

    # =========================================================================
    # Playback Control
    # =========================================================================

    async def play(self, url: str, metadata: BackendTrackMetadata) -> None:
        """Start playback of track."""
        if not self._client:
            raise RuntimeError("Not connected")
        if await self.check_external_playback():
            raise RuntimeError("External source owns renderer; select it again in Qobuz")
        self._starting_playback = True
        try:
            await self._play(url, metadata)
        finally:
            self._starting_playback = False

    async def _play(self, url: str, metadata: BackendTrackMetadata) -> None:
        """Load audio while source polling is suspended during the transport change."""
        # An explicit play invalidates the armed next track (no queue removal
        # needed: Sonos play clears the whole queue).
        self._clear_next_track()
        self._current_metadata = metadata
        self._duration_ms = metadata.duration_ms

        content_type = content_type_for(url)
        actual_url = url
        size = None
        if self._proxy_server:
            actual_url = self._proxy_server.register_track(
                track_id=metadata.track_id, qobuz_url=url, content_type=content_type
            )
            size = self._proxy_server.known_size(proxy_key(actual_url))
            logger.debug(f"Using proxy URL: {actual_url}")
        self._forget_delivery(actual_url)

        didl = self._build_didl(actual_url, metadata, content_type, size)
        if self._is_sonos:
            success = await self._play_via_queue(actual_url, didl)
        else:
            success = await self._play_via_transport(actual_url, didl)

        if success:
            self._external_playback = False
            self._last_stop_at = None
            self._position_ms = 0
            self._furthest_position_ms = 0
            self._current_proxy_url = actual_url
            self._playback_started_at = time.monotonic()
            self._loading_after_playing = False
            self._reset_track_tracking()
            self._notify_state_change(PlaybackState.PLAYING)
            logger.info(f"Playing: {metadata.artist} - {metadata.title}")

    async def _play_via_transport(self, url: str, didl: str) -> bool:
        """Start playback using SetAVTransportURI (standard DLNA).

        If the first attempt fails (e.g. the renderer is wedged after a rapid
        track switch), recover the connection and reset the device transport,
        then retry once before reporting an error.
        """
        assert self._client
        stage = await self._try_transport_sequence(url, didl)
        if stage is None:
            return True

        logger.warning(f"Playback {stage} failed; attempting transport recovery")
        await self._client.reset_session()
        try:
            await self._client.stop()
        except Exception:
            pass

        stage = await self._try_transport_sequence(url, didl)
        if stage is None:
            logger.info("Transport recovery succeeded")
            return True
        self._notify_playback_error(
            "Failed to start playback" if stage == "play" else "Failed to set transport URI"
        )
        return False

    async def _play_via_queue(self, url: str, didl: str) -> bool:
        """Start playback using Sonos queue (shows metadata in Sonos app)."""
        assert self._client
        self._sonos_queue_head_url = None
        if not await self._client.clear_queue():
            logger.warning("Failed to clear queue, falling back to transport URI")
            return await self._play_via_transport(url, didl)
        if not await self._client.add_uri_to_queue(url, didl):
            logger.warning("Failed to add to queue, falling back to transport URI")
            return await self._play_via_transport(url, didl)
        if await self._client.play_from_queue(0):
            self._sonos_queue_head_url = url
            return True
        self._notify_playback_error("Failed to play from queue")
        return False

    async def pause(self) -> None:
        """Pause playback.

        Some renderers refuse Pause on a stream they take for a live one
        (NAD C 338: UPnP 401, GitHub #44) and keep playing, while the Qobuz
        app shows paused. Those are stopped instead, and ``resume`` reloads
        the track at the position it was at.
        """
        if not self._client or not await self._owns_transport():
            return
        if self._quirks.can_pause is not False:
            result = await self._client.pause_detailed()
            if result.success:
                self._notify_state_change(PlaybackState.PAUSED)
                return
            if not result.is_permanent_failure:
                return
            self._learn("can_pause", False, f"refused Pause with error {result.error_code}")
        await self._pause_by_stop()

    async def _pause_by_stop(self) -> None:
        assert self._client
        position = self._position_ms
        self._stopping_playback = True
        try:
            stopped = await self._client.stop()
        finally:
            self._stopping_playback = False
        if not stopped:
            return
        self._last_stop_at = time.monotonic()
        self._paused_by_stop_ms = position
        logger.info(
            "[%s] Renderer cannot pause; stopped at %s, resume reloads the track",
            self.name,
            format_ms(position),
        )
        self._notify_state_change(PlaybackState.PAUSED)

    async def resume(self) -> bool:
        """Resume playback.

        Returns True only when the renderer accepted the play command — a
        failed SOAP call must not be reported as a successful resume.
        """
        if not self._client:
            return False
        if self._paused_by_stop_ms is not None:
            resume_ms = self._paused_by_stop_ms
            if self._transport_busy() or not await self._renderer_idle_or_ours():
                return False
            self._paused_by_stop_ms = None
            if not await self._reload_current_track(resume_ms):
                self._paused_by_stop_ms = resume_ms
                return False
        elif not await self._owns_transport() or not await self._client.play():
            return False
        self._end_handled_for = None
        self._notify_state_change(PlaybackState.PLAYING)
        return True

    async def _renderer_idle_or_ours(self) -> bool:
        """The renderer is idle (after our stop) or still has our track loaded."""
        return await self._owns_transport() or await self._renderer_idle()

    async def stop(self, *, next_track_id: Optional[str] = None) -> None:
        """Stop playback."""
        owns_transport = await self._owns_transport()
        outgoing_next = self._next_track_proxy_url
        self._clear_next_track()
        self._paused_by_stop_ms = None

        if not self._client or not owns_transport:
            return
        self._stopping_playback = True
        try:
            stopped = await self._client.stop()
        finally:
            self._stopping_playback = False
        if stopped:
            self._last_stop_at = time.monotonic()
            logger.info(
                "[%s] Qobuz stopped transport: current=%s cleared_next=%s replacement_track_id=%s",
                self.name,
                source_label(self._current_proxy_url),
                source_label(outgoing_next),
                next_track_id,
            )
            self._position_ms = 0
            self._furthest_position_ms = 0
            self._playback_started_at = 0.0  # Clear grace period
            self._notify_state_change(PlaybackState.STOPPED)

    # =========================================================================
    # Position Control
    # =========================================================================

    async def seek(self, position_ms: int) -> None:
        """Seek to position."""
        if self._paused_by_stop_ms is not None:
            # Stopped in place of a pause: the seek applies on resume.
            self._paused_by_stop_ms = max(0, position_ms)
            self._position_ms = self._paused_by_stop_ms
            self._notify_position_update(self._position_ms)
            return
        if not await self._owns_transport():
            raise RuntimeError("Cannot seek: renderer is not playing the Qobuz track")
        if self._client and await self._client.seek(position_ms):
            self._last_seek_at = time.monotonic()
            self._stall_position_ms = -1
            self._end_handled_for = None
            self._position_ms = position_ms
            self._furthest_position_ms = position_ms
            self._notify_position_update(position_ms)

    # =========================================================================
    # Volume Control
    # =========================================================================

    async def set_volume(self, level: int) -> None:
        """Set volume (0-100)."""
        if self._fixed_volume:
            logger.debug("Fixed volume mode: ignoring set_volume")
            return
        if await self.check_external_playback():
            return
        clamped = max(0, min(100, level))
        if self._client:
            await self._client.set_volume(clamped)
            self._volume = clamped

    async def get_volume(self) -> int:
        """Get current volume (0-100)."""
        if self._fixed_volume:
            return 100
        if self._client:
            vol = await self._client.get_volume()
            if vol is not None:
                self._volume = vol
        return self._volume

    # =========================================================================
    # Info and capabilities
    # =========================================================================

    async def get_buffer_status(self) -> BufferStatus:
        """Get buffer status (always OK for DLNA)."""
        return BufferStatus.OK

    def get_info(self) -> BackendInfo:
        """Get backend information."""
        info = self.device_info
        backend_info = BackendInfo(
            backend_type="dlna",
            name=self.name,
            device_id=info.udn if info else "",
            ip=self._ip,
            port=self._port,
        )
        if info:
            backend_info.model = info.model_name
            backend_info.manufacturer = info.manufacturer
        return backend_info
