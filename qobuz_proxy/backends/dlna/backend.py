"""
DLNA audio backend.

Implements AudioBackend interface for DLNA/UPnP renderers.
"""

import asyncio
import hashlib
import logging
import re
import time
from typing import Awaitable, Callable, Optional, TYPE_CHECKING
from urllib.parse import urlsplit

from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.backends.types import (
    BackendInfo,
    BackendTrackMetadata,
    BufferStatus,
    PlaybackState,
)
from .client import DLNAClient, DLNAClientError, DLNAUnsupportedDeviceError, SoapResult
from .capabilities import (
    DLNACapabilities,
    CapabilityCache,
    parse_protocol_info_sink,
    apply_device_overrides,
    build_protocol_info,
)
from .quirks import (
    GAPLESS_AUTO,
    DeviceQuirks,
    QuirkStore,
    default_store as _default_quirk_store,
    parse_gapless_mode,
    resolve_quirks,
)

if TYPE_CHECKING:
    from .proxy_server import AudioProxyServer

logger = logging.getLogger(__name__)

# State polling interval
STATE_POLL_INTERVAL_SECONDS = 2.0

# In the last seconds of a track, poll faster so the natural end is noticed
# (and the next track started) sooner. The window covers the normal interval
# plus a renderer position that lags a little.
NEAR_END_POLL_INTERVAL_SECONDS = 0.5
NEAR_END_WINDOW_MS = 6_000

# Grace period after starting playback to ignore STOPPED state (seconds)
# This prevents false track-ended events while the device is loading
PLAYBACK_START_GRACE_PERIOD_SECONDS = 5.0

# A stop with more than this much of the track left is not a natural track end
# (another controller or the device's own buttons stopped it). Covers the poll
# interval plus renderers whose reported position lags a little.
EARLY_STOP_MARGIN_MS = 15_000

# Gapless arming waits until the renderer itself has been heard playing the
# current track: some renderers replace a track that is still loading with a
# NextURI that arrives during TRANSITIONING. Confirmed once the reported
# position reaches ARM_AFTER_POSITION_MS, or (for renderers that never report
# a position) after PLAYING has been seen for ARM_AFTER_PLAYING_SECONDS.
# Arming no earlier than 5 s in also keeps an early switch visible: a jump
# back to the start of the queued track is then at least 2 s.
ARM_AFTER_POSITION_MS = 5_000
ARM_AFTER_PLAYING_SECONDS = 5.0

# A renderer that stays PLAYING at the end of a track (position parked at the
# track length, never STOPPED; the FiiO K17 does this even with the stream's
# length known). With gapless off, this wait is most of the gap between
# tracks, so it is kept short where the evidence allows. The track counts as
# ended when the position has not moved for:
#   END_STALL_AT_END_SECONDS     at or past the length, with the whole file
#                                sent or a renderer known not to stop
#   END_STALL_DELIVERED_SECONDS  within END_WINDOW_MS of it, whole file sent
#   END_STALL_KNOWN_SECONDS      within the window, renderer known not to stop
#   END_STALL_SECONDS            otherwise, or with a next track armed
# or once the position runs PAST_END_MS beyond the length.
END_WINDOW_MS = 2_000
END_STALL_AT_END_SECONDS = 1.0
END_STALL_DELIVERED_SECONDS = 2.0
END_STALL_KNOWN_SECONDS = 3.0
END_STALL_SECONDS = 6.0
PAST_END_MS = 4_000
# A position counts as "not moving" while it stays within this of where the
# stall started (RelTime is reported in whole seconds).
STALL_TOLERANCE_MS = 500

# A renderer that starts the queued track as soon as it gets it: with a next
# track armed and the current URI unchanged, the position falls back by at
# least EARLY_SWITCH_BACK_MS to the start of a track (at most
# EARLY_SWITCH_TO_MS) while plenty of the current one was left. Arming waits
# for ARM_AFTER_POSITION_MS, so the jump back is visible even when the switch
# follows the arming at once.
EARLY_SWITCH_BACK_MS = 2_000
EARLY_SWITCH_TO_MS = 3_000
EARLY_SWITCH_REMAINING_MS = 15_000
# Our own seeks also move the position back; ignore jumps this soon after one.
SEEK_SETTLE_SECONDS = 5.0

# A stop at the end of a track with a next track armed this long before it
# means the renderer ignores SetNextAVTransportURI.
IGNORED_NEXT_ARMED_FOR_SECONDS = 10.0

# After a seek, the position is read back this many times (0.25 s apart) to
# confirm the renderer got there, within this tolerance.
SEEK_CONFIRM_STEPS = 8
SEEK_CONFIRM_TOLERANCE_MS = 2_000
# After an early switch, the current track is sought back only when at least
# this much would otherwise be replayed; earlier it restarts from the start,
# sparing a renderer that just misbehaved a Seek. (A FiiO K17 crashed shortly
# after a reload + Seek; cause not proven.)
EARLY_SWITCH_MIN_SEEK_MS = 30_000

# Class-level capability cache (shared across instances)
_capability_cache = CapabilityCache()


def _proxy_key(url: str) -> str:
    """Audio proxy registration key of one of our proxy URLs."""
    return urlsplit(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _source_label(uri: Optional[str]) -> str:
    """Identify sources without logging credentials, signed queries, or opaque IDs."""
    if not uri:
        return "none"
    fingerprint = hashlib.sha256(uri.encode()).hexdigest()[:12]
    try:
        parsed = urlsplit(uri)
        # Our audio paths contain only track IDs and optional generation numbers.
        path = (
            parsed.path if re.fullmatch(r"/audio/\d+(?:_\d+)?\.(?:flac|mp3)", parsed.path) else ""
        )
        return f"{parsed.scheme}://{parsed.hostname or ''}{path} [sha256={fingerprint}]"
    except ValueError:
        return f"unparseable [sha256={fingerprint}]"


def _format_ms(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


class DLNABackend(AudioBackend):
    """
    DLNA/UPnP audio backend.

    Connects to DLNA renderers and controls playback via SOAP commands.
    Uses polling to monitor device state.

    Note: This backend expects URLs to be provided by the Audio Proxy Server.
    It does not handle URL proxying itself.
    """

    def __init__(
        self,
        ip: str,
        port: int = 1400,
        fixed_volume: bool = False,
        name: Optional[str] = None,
        description_url: Optional[str] = None,
        gapless: str = GAPLESS_AUTO,
        quirk_store: Optional[QuirkStore] = None,
    ):
        """
        Initialize DLNA backend.

        Args:
            ip: DLNA device IP address
            port: DLNA device port (default 1400 for Sonos)
            fixed_volume: If True, ignore volume commands
            name: Display name (auto-detected if not provided)
            description_url: Full URL to UPnP device description XML
            gapless: "auto" (profile and learned behaviour decide), "on" or "off"
            quirk_store: Where learned renderer behaviour is kept (tests)
        """
        super().__init__(name or f"DLNA ({ip})")
        self._ip = ip
        self._port = port
        self._fixed_volume = fixed_volume
        self._description_url = description_url

        self._client: Optional[DLNAClient] = None
        self._poll_task: Optional[asyncio.Task] = None

        self._current_metadata: Optional[BackendTrackMetadata] = None
        self._position_ms: int = 0
        # Furthest position reached on the current track. Positions read after
        # a stop drop back to 0, so a stop is judged against this instead.
        self._furthest_position_ms: int = 0
        self._duration_ms: int = 0
        # Whether the renderer went PLAYING -> TRANSITIONING (reported as
        # LOADING). Many renderers pass through TRANSITIONING at the end of a
        # track, so a STOPPED that follows must still count as a track end.
        self._loading_after_playing = False

        # Audio proxy server for URL handling
        self._proxy_server: Optional["AudioProxyServer"] = None
        # Proxy keys the audio proxy failed to serve -> True if the renderer got
        # no audio at all, False if the stream was cut short.
        self._delivery_failures: dict[str, bool] = {}

        # Device capabilities
        self._capabilities: Optional[DLNACapabilities] = None

        # Track when playback was started to avoid false track-ended events
        # during device loading/transition period
        self._playback_started_at: float = 0.0

        # Gapless playback state
        self._next_track_proxy_url: Optional[str] = None
        self._next_track_metadata: Optional[BackendTrackMetadata] = None
        self._next_track_queue_nr: Optional[int] = None  # Sonos queue position of armed track
        self._gapless_supported: bool = True
        self._current_proxy_url: Optional[str] = None
        # When the armed next track was sent to the renderer (monotonic).
        self._next_armed_at: float = 0.0

        # Renderer behaviour: built-in profile, learned, and the user's setting.
        self._gapless_mode = parse_gapless_mode(gapless)
        self._quirk_store = quirk_store if quirk_store is not None else _default_quirk_store
        self._quirks = DeviceQuirks()
        # Logged once per track when the renderer names the armed URI early.
        self._early_uri_logged_for: Optional[str] = None

        # Whether the renderer has been heard playing the current track (gates
        # gapless arming), and when PLAYING was first seen for it.
        self._playing_confirmed = False
        self._first_playing_at: Optional[float] = None

        # End-of-track stall tracking for renderers that never report STOPPED.
        self._stall_position_ms: int = -1
        self._stall_since: float = 0.0
        # Proxy keys whose whole file the proxy has sent to the renderer.
        self._delivered_keys: set[str] = set()
        # When we last asked the renderer to seek (monotonic).
        self._last_seek_at: float = 0.0
        # The track URL a stalled end was already reported for.
        self._end_handled_for: Optional[str] = None
        # Fire-and-forget renderer commands, kept referenced until they finish.
        self._background_tasks: set[asyncio.Task[None]] = set()

        # Sonos queue-based playback (for Sonos app metadata display)
        self._is_sonos: bool = False
        self._sonos_queue_head_url: Optional[str] = None
        self._external_playback = False
        self._starting_playback = False
        self._stopping_playback = False
        self._last_stop_at: Optional[float] = None
        self._on_external_playback: Optional[Callable[[], Awaitable[None]]] = None

    def on_external_playback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a callback for another source taking over the renderer."""
        self._on_external_playback = callback

    def prepare_for_selection(self) -> None:
        """Allow a fresh Qobuz selection to load audio after an external takeover."""
        if self._external_playback:
            self._current_proxy_url = None
            self._external_playback = False

    async def check_external_playback(self) -> bool:
        """Detect source changes even when the cached Qobuz state is paused."""
        await self._owns_transport()
        return self._external_playback

    async def can_apply_remote_state(self) -> bool:
        """Require source confirmation for snapshots of an already loaded transport."""
        if not self._current_proxy_url:
            return not self._external_playback
        if await self._owns_transport():
            return True
        # upmpdcli 1.9+ clears CurrentURI when stopped. An idle renderer with
        # nothing loaded belongs to no other source, so the app may load onto it
        # again; refusing left the speaker dead after switching away and back
        # (GitHub #35). An empty URI while playing may be a device-internal
        # source (AirPlay, line-in), so that still fails closed.
        if (
            self._is_sonos
            or self._external_playback
            or self._starting_playback
            or self._stopping_playback
            or not self._client
        ):
            return False
        if await self._client.get_media_info() != "":
            return False
        return await self._client.get_transport_info() in ("STOPPED", "NO_MEDIA_PRESENT")

    async def _owns_transport(self) -> bool:
        """Only control the current or armed Qobuz track; unknown sources fail closed."""
        if (
            self._external_playback
            or self._starting_playback
            or self._stopping_playback
            or not self._client
            or not self._current_proxy_url
        ):
            return False
        expected = self._current_proxy_url
        armed_at_read = self._next_track_proxy_url
        current_uri = (
            await self._client.get_track_uri()
            if self._is_sonos
            else await self._client.get_media_info()
        )
        # A concurrent explicit play may have replaced the track during the read.
        if (
            self._external_playback
            or self._starting_playback
            or self._stopping_playback
            or expected != self._current_proxy_url
            or not current_uri
        ):
            return False
        if current_uri in (self._current_proxy_url, self._next_track_proxy_url):
            return True
        # Sonos Stop resets TrackURI to the first entry of our retained queue,
        # even after gapless playback advanced to a later entry. Only accept
        # that exact URI following our own successful stop, while still stopped.
        if (
            self._is_sonos
            and self._last_stop_at is not None
            and current_uri == self._sonos_queue_head_url
        ):
            transport = await self._client.get_transport_info()
            if (
                self._external_playback
                or self._starting_playback
                or self._stopping_playback
                or expected != self._current_proxy_url
                or self._last_stop_at is None
                or current_uri != self._sonos_queue_head_url
            ):
                return False
            if not transport:
                return False
            if transport == "STOPPED":
                logger.debug("[%s] Sonos reported Qobuz queue head after Stop", self.name)
                return True
        # Renderers can briefly report the old source while loading our URI.
        if time.monotonic() - self._playback_started_at < PLAYBACK_START_GRACE_PERIOD_SECONDS:
            return False
        logger.warning(
            "[%s] Source mismatch: observed=%s expected=%s armed_at_read=%s armed_now=%s "
            "cached_state=%s last_successful_stop_age_s=%s",
            self.name,
            _source_label(current_uri),
            _source_label(expected),
            _source_label(armed_at_read),
            _source_label(self._next_track_proxy_url),
            self._state.name,
            round(time.monotonic() - self._last_stop_at, 3)
            if self._last_stop_at is not None
            else None,
        )
        self._external_playback = True
        self._next_track_proxy_url = None
        self._next_track_metadata = None
        self._next_track_queue_nr = None
        logger.info("[%s] External source took over; releasing Qobuz playback control", self.name)
        if self._on_external_playback:
            await self._on_external_playback()
        return False

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def set_proxy_server(self, proxy: "AudioProxyServer") -> None:
        """
        Set the audio proxy server for URL proxying.

        When set, all playback URLs will be routed through the proxy,
        which handles Qobuz URL expiration transparently.

        Args:
            proxy: AudioProxyServer instance
        """
        self._proxy_server = proxy
        proxy.set_delivery_failure_callback(self._on_delivery_failure)
        proxy.set_delivery_complete_callback(self._on_delivery_complete)
        logger.info("Audio proxy server configured for DLNA backend")

    def _on_delivery_complete(self, proxy_key: str) -> None:
        """Remember a track whose whole file the renderer has received."""
        self._delivered_keys.add(proxy_key)

    def _on_delivery_failure(self, proxy_key: str, before_audio: bool) -> None:
        """Remember a track the renderer could not get (fully) from the proxy."""
        self._delivery_failures[proxy_key] = before_audio or self._delivery_failures.get(
            proxy_key, False
        )

    async def connect(self) -> bool:
        """Connect to DLNA device."""
        try:
            self._client = DLNAClient(self._ip, self._port, description_url=self._description_url)
            device_info = await self._client.connect()

            # Update name from device
            if device_info.friendly_name:
                self.name = device_info.friendly_name

            # Detect Sonos devices for queue-based playback
            self._is_sonos = "sonos" in (device_info.manufacturer or "").lower()
            if self._is_sonos:
                logger.info("Sonos device detected — using queue-based playback")

            # Query device capabilities
            await self._discover_capabilities(device_info)
            self._apply_quirks(device_info)

            self._is_connected = True

            # Start state polling
            self._poll_task = asyncio.create_task(self._poll_state_loop())

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

    def _apply_quirks(self, device_info) -> None:
        """Resolve profile, learned and user behaviour for this renderer."""
        self._quirks = resolve_quirks(
            device_info.udn,
            device_info.manufacturer,
            device_info.model_name,
            self._gapless_mode,
            self._quirk_store,
            friendly_name=getattr(device_info, "friendly_name", ""),
        )
        # Sonos gapless goes through its own queue, not SetNextAVTransportURI.
        if not self._is_sonos:
            self._gapless_supported = self._quirks.gapless is not False
        logger.info(
            "[%s] Renderer behaviour: %s; gapless %s",
            self.name,
            self._quirks.describe(),
            "on" if self._gapless_supported else "off",
        )

    def _learn(self, name: str, value: bool, reason: str) -> None:
        """Apply and save a behaviour this renderer has just shown."""
        if self._quirks.sources.get(name) == "user":
            return  # the user's setting stands
        changed = getattr(self._quirks, name) != value
        setattr(self._quirks, name, value)
        self._quirks.sources[name] = "learned"
        device_info = getattr(self._client, "device_info", None)
        udn = getattr(device_info, "udn", "") or ""
        saved = self._quirk_store.learn(udn, name, value, label=self.name)
        if changed or saved:
            logger.warning(
                "[%s] Learned %s=%s: %s%s",
                self.name,
                name,
                value,
                reason,
                f" (saved to {self._quirk_store.path})" if saved else "",
            )
        if name == "gapless" and value is False and not self._is_sonos:
            self._gapless_supported = False

    def _reset_track_tracking(self) -> None:
        """Forget per-track observations when a new track starts."""
        self._playing_confirmed = False
        self._first_playing_at = None
        self._stall_position_ms = -1
        self._stall_since = 0.0
        self._early_uri_logged_for = None
        self._end_handled_for = None

    @property
    def quirks(self) -> DeviceQuirks:
        """Effective renderer behaviour (profile, learned and user layers)."""
        return self._quirks

    @property
    def ready_for_next_track(self) -> bool:
        """Whether a next track may be armed now.

        Only once the renderer has been heard playing the current track:
        arming while it is still loading makes some renderers play the queued
        track instead.
        """
        return self._gapless_supported and (self._is_sonos or self._playing_confirmed)

    def _note_playing(self, position_ms: Optional[int]) -> None:
        """Confirm the current track is playing on the renderer."""
        if self._playing_confirmed:
            return
        now = time.monotonic()
        if self._first_playing_at is None:
            self._first_playing_at = now
        if (position_ms or 0) >= ARM_AFTER_POSITION_MS or (
            now - self._first_playing_at >= ARM_AFTER_PLAYING_SECONDS
        ):
            self._playing_confirmed = True

    async def _discover_capabilities(self, device_info) -> None:
        """Query and parse device capabilities."""
        # Check cache first
        device_id = device_info.udn or self._ip
        cached = _capability_cache.get(device_id)
        if cached:
            logger.debug(f"Using cached capabilities for {device_id}")
            self._capabilities = cached
            return

        # Query GetProtocolInfo
        try:
            if not self._client:
                return
            sink = await self._client.get_protocol_info()
            if sink:
                self._capabilities = parse_protocol_info_sink(sink)
                # Apply device-specific overrides
                apply_device_overrides(
                    self._capabilities,
                    device_info.manufacturer,
                    device_info.model_name,
                )
                # Cache the result
                _capability_cache.set(device_id, self._capabilities)
            else:
                logger.debug("GetProtocolInfo not supported, using defaults")
                self._capabilities = None
        except Exception as e:
            logger.warning(f"Failed to discover capabilities: {e}")
            self._capabilities = None

    async def disconnect(self) -> None:
        """Disconnect from DLNA device."""
        self._is_connected = False

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._client:
            # Stop playback before disconnecting
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

        # Clear gapless state — explicit play invalidates armed next track
        # (no queue removal needed: Sonos play clears the whole queue)
        self._next_track_proxy_url = None
        self._next_track_metadata = None
        self._next_track_queue_nr = None

        self._current_metadata = metadata
        self._duration_ms = metadata.duration_ms

        # Determine content type from URL or default to FLAC
        content_type = "audio/flac"
        if ".mp3" in url.lower() or "format=5" in url.lower():
            content_type = "audio/mpeg"

        # Register with proxy server if available
        actual_url = url
        if self._proxy_server:
            actual_url = self._proxy_server.register_track(
                track_id=metadata.track_id,
                qobuz_url=url,
                content_type=content_type,
            )
            logger.debug(f"Using proxy URL: {actual_url}")
        # Failures from an earlier attempt at this URL are not this play's.
        self._delivery_failures.pop(_proxy_key(actual_url), None)
        self._delivered_keys.discard(_proxy_key(actual_url))

        # Build DIDL-Lite metadata
        didl = self._build_didl(actual_url, metadata, content_type)

        # Start playback — Sonos uses queue for proper app metadata display
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
        then retry once before reporting an error. This lets a stuck renderer
        recover without restarting the addon or power-cycling the device.
        """
        assert self._client
        stage = await self._try_transport_sequence(url, didl)
        if stage is None:
            return True

        logger.warning(f"Playback {stage} failed; attempting transport recovery")
        # Recover the HTTP session and clear the device's stuck transport.
        await self._client.reset_session()
        try:
            await self._client.stop()
        except Exception:
            pass

        stage = await self._try_transport_sequence(url, didl)
        if stage is None:
            logger.info("Transport recovery succeeded")
            return True

        if stage == "play":
            self._notify_playback_error("Failed to start playback")
        else:
            self._notify_playback_error("Failed to set transport URI")
        return False

    async def _try_transport_sequence(self, url: str, didl: str) -> Optional[str]:
        """Run SetAVTransportURI + Play once.

        Returns None on success, or the name of the failed stage
        ("set_uri" or "play").
        """
        assert self._client
        if not await self._client.set_av_transport_uri(url, didl):
            return "set_uri"
        if not await self._client.play():
            return "play"
        return None

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
        """Pause playback."""
        if self._client and await self._owns_transport() and await self._client.pause():
            self._notify_state_change(PlaybackState.PAUSED)

    async def resume(self) -> bool:
        """Resume playback.

        Returns True only when the renderer accepted the play command — a
        failed SOAP call must not be reported as a successful resume.
        """
        if self._client and await self._owns_transport() and await self._client.play():
            self._notify_state_change(PlaybackState.PLAYING)
            return True
        return False

    async def stop(self, *, next_track_id: Optional[str] = None) -> None:
        """Stop playback."""
        owns_transport = await self._owns_transport()
        outgoing_next = self._next_track_proxy_url
        # Clear gapless state
        self._next_track_proxy_url = None
        self._next_track_metadata = None
        self._next_track_queue_nr = None

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
                _source_label(self._current_proxy_url),
                _source_label(outgoing_next),
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
        if not await self._owns_transport():
            raise RuntimeError("Cannot seek: renderer is not playing the Qobuz track")
        if self._client and await self._client.seek(position_ms):
            self._last_seek_at = time.monotonic()
            self._stall_position_ms = -1
            self._position_ms = position_ms
            self._furthest_position_ms = position_ms
            self._notify_position_update(position_ms)

    async def get_position(self) -> int:
        """Get current position."""
        if self._client and await self._owns_transport():
            pos = await self._client.get_position_info()
            if pos is not None:
                self._position_ms = pos
                self._furthest_position_ms = max(self._furthest_position_ms, pos)
                logger.debug(f"DLNA position: {pos}ms")
            else:
                logger.debug("DLNA position: None returned")
        return self._position_ms

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
    # State
    # =========================================================================

    async def get_state(self) -> PlaybackState:
        """Get current playback state from device."""
        if not self._client:
            return PlaybackState.STOPPED

        state_str = await self._client.get_transport_info()
        if state_str:
            if state_str == "PLAYING":
                return PlaybackState.PLAYING
            elif state_str == "PAUSED_PLAYBACK":
                return PlaybackState.PAUSED
            elif state_str == "TRANSITIONING":
                return PlaybackState.LOADING

        return PlaybackState.STOPPED

    async def get_buffer_status(self) -> BufferStatus:
        """Get buffer status (always OK for DLNA)."""
        return BufferStatus.OK

    # =========================================================================
    # Info
    # =========================================================================

    def get_info(self) -> BackendInfo:
        """Get backend information."""
        info = BackendInfo(
            backend_type="dlna",
            name=self.name,
            device_id=(
                self._client.device_info.udn if self._client and self._client.device_info else ""
            ),
            ip=self._ip,
            port=self._port,
        )
        if self._client and self._client.device_info:
            info.model = self._client.device_info.model_name
            info.manufacturer = self._client.device_info.manufacturer
        return info

    # =========================================================================
    # Capabilities
    # =========================================================================

    def get_capabilities(self) -> Optional[DLNACapabilities]:
        """
        Get discovered device capabilities.

        Returns:
            DLNACapabilities if discovered, None otherwise
        """
        return self._capabilities

    def get_recommended_quality(self) -> Optional[int]:
        """
        Get recommended Qobuz quality level based on device capabilities.

        Returns:
            Qobuz quality level (5, 6, 7, or 27), or None if not available
        """
        if self._capabilities:
            return self._capabilities.max_quality
        return None

    @property
    def quality_detection_confirmed(self) -> bool:
        """Whether the recommended quality came from explicit device format info.

        False means the device advertised FLAC without stating sample rates or
        bit depths, so the recommendation is just a conservative CD default.
        """
        return self._capabilities is not None and self._capabilities.format_info_confirmed

    # =========================================================================
    # Gapless Playback
    # =========================================================================

    @property
    def supports_gapless(self) -> bool:
        """Whether this backend supports gapless playback."""
        return self._gapless_supported

    async def set_next_track(
        self, url: str, metadata: BackendTrackMetadata, queue_item_id: int = 0
    ) -> bool:
        """Prepare the next track for gapless transition."""
        if not self._client or not self.ready_for_next_track or not await self._owns_transport():
            return False

        # Determine content type
        content_type = "audio/flac"
        if ".mp3" in url.lower() or "format=5" in url.lower():
            content_type = "audio/mpeg"

        # Register with proxy server using unique key
        actual_url = url
        if self._proxy_server:
            proxy_key = f"{metadata.track_id}_{queue_item_id}"
            actual_url = self._proxy_server.register_track(
                track_id=metadata.track_id,
                qobuz_url=url,
                content_type=content_type,
                proxy_key=proxy_key,
            )
            logger.debug(f"Gapless: registered next track proxy URL: {actual_url}")
        self._delivery_failures.pop(_proxy_key(actual_url), None)
        self._delivered_keys.discard(_proxy_key(actual_url))

        # Build DIDL-Lite metadata
        didl = self._build_didl(actual_url, metadata, content_type)

        # Send to device — Sonos uses queue, others use SetNextAVTransportURI
        if self._is_sonos:
            # Already armed with this URL — appending again would queue a
            # duplicate entry and make the song play twice
            if self._next_track_proxy_url == actual_url and self._next_track_queue_nr is not None:
                logger.debug("Gapless: next track already armed, skipping duplicate")
                return True
            queue_nr = await self._client.add_uri_to_queue(actual_url, didl)
            if queue_nr is not None:
                self._next_track_proxy_url = actual_url
                self._next_track_metadata = metadata
                self._next_track_queue_nr = queue_nr
                logger.info(f"Gapless: armed next track: {metadata.artist} - {metadata.title}")
                return True
            logger.warning("Gapless: failed to add next track to queue")
            return False

        result: SoapResult = await self._client.set_next_av_transport_uri(actual_url, didl)

        if result.success:
            self._next_track_proxy_url = actual_url
            self._next_track_metadata = metadata
            self._next_armed_at = time.monotonic()
            logger.info(f"Gapless: armed next track: {metadata.artist} - {metadata.title}")
            return True

        if result.is_permanent_failure:
            self._gapless_supported = False
            logger.warning(
                f"Gapless: disabled — device does not support SetNextAVTransportURI "
                f"(error {result.error_code}: {result.error_description})"
            )
            self._learn(
                "gapless",
                False,
                f"SetNextAVTransportURI refused with error {result.error_code}",
            )
        else:
            logger.warning(
                "Gapless: failed to arm next track (transient error), will retry on next poll cycle"
            )
        return False

    async def clear_next_track(self) -> None:
        """Clear prepared next track.

        On Sonos the armed track was appended to the device queue, so it must
        be removed there too — otherwise it still plays after the current track.
        """
        if (
            self._is_sonos
            and self._client
            and self._next_track_queue_nr is not None
            and await self._owns_transport()
        ):
            await self._client.remove_track_from_queue(self._next_track_queue_nr)
        elif not self._is_sonos and self._next_track_proxy_url and await self._owns_transport():
            await self._disarm_on_renderer()
        self._next_track_proxy_url = None
        self._next_track_metadata = None
        self._next_track_queue_nr = None

    async def _disarm_on_renderer(self) -> None:
        """Clear the renderer's NextURI with an empty SetNextAVTransportURI.

        Forgetting the armed track locally is not enough: the FiiO K17 keeps a
        queued URI through Stop and later tracks until it is cleared. Errors
        are ignored.
        """
        if not self._client:
            return
        result = await self._client.set_next_av_transport_uri("", "")
        if not result.success:
            logger.debug(f"[{self.name}] Clearing NextURI failed: {result.error_code}")

    def _disarm_soon(self) -> None:
        """Clear the renderer's NextURI from a synchronous code path."""
        task = asyncio.ensure_future(self._disarm_on_renderer())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # =========================================================================
    # Internal
    # =========================================================================

    async def _poll_state_loop(self) -> None:
        """Poll device state periodically."""
        while self._is_connected:
            try:
                await asyncio.sleep(self._next_poll_delay())

                if not self._is_connected:
                    break

                # Spotify/AirPlay/other controllers own their transport and queue.
                # Their state changes must never trigger Qobuz auto-advance.
                if (
                    self._starting_playback
                    or self._stopping_playback
                    or not self._current_proxy_url
                    or await self.check_external_playback()
                ):
                    continue

                # Get state from device
                new_state = await self.get_state()

                # Check if we're in the grace period after starting playback
                in_grace_period = (
                    time.monotonic() - self._playback_started_at
                    < PLAYBACK_START_GRACE_PERIOD_SECONDS
                )

                # Gapless transition detection: check if device has moved to next track
                if (
                    new_state == PlaybackState.PLAYING
                    and self._next_track_proxy_url
                    and self._client
                ):
                    # For Sonos queue playback, GetMediaInfo.CurrentURI returns the
                    # queue URI, not the track URL. Use GetPositionInfo.TrackURI instead.
                    if self._is_sonos:
                        current_uri = await self._client.get_track_uri()
                    else:
                        current_uri = await self._client.get_media_info()
                    if (
                        current_uri
                        and current_uri == self._next_track_proxy_url
                        and (self._is_sonos or await self._moved_on_to_next())
                    ):
                        logger.info("Gapless: transition detected — device moved to next track")
                        # Update state to reflect the new track
                        self._current_metadata = self._next_track_metadata
                        self._current_proxy_url = self._next_track_proxy_url
                        if self._next_track_metadata:
                            self._duration_ms = self._next_track_metadata.duration_ms
                        self._position_ms = 0
                        self._furthest_position_ms = 0
                        self._playback_started_at = time.monotonic()
                        self._loading_after_playing = False
                        self._reset_track_tracking()
                        # Already heard playing: the following track may be armed.
                        self._playing_confirmed = True
                        # Clear gapless state — the armed entry is now the
                        # playing one, so don't remove it from the queue
                        self._next_track_proxy_url = None
                        self._next_track_metadata = None
                        self._next_track_queue_nr = None
                        # The renderer is playing the new track, even if the last
                        # poll caught it in TRANSITIONING: record PLAYING so its
                        # own end is detected.
                        self._notify_state_change(PlaybackState.PLAYING)
                        # Notify player
                        self._notify_next_track_started()
                        self._notify_position_update(0)
                        continue

                # The proxy could not get any audio for the current track from
                # Qobuz (e.g. the CDN edge failed before the first byte). Some
                # renderers then sit in PLAYING or TRANSITIONING forever, so
                # don't wait for a stop that may never come: skip it.
                current_key = _proxy_key(self._current_proxy_url or "")
                if self._delivery_failures.get(current_key) is True:
                    del self._delivery_failures[current_key]
                    logger.warning(
                        "[%s] Could not stream the current track to the renderer; "
                        "skipping to the next track",
                        self.name,
                    )
                    # Mark stopped so the renderer's own stop is not a second end.
                    self._state = PlaybackState.STOPPED
                    self._notify_track_ended()
                    continue

                # Detect state changes
                if new_state != self._state:
                    logger.debug(f"State changed: {self._state} -> {new_state}")
                    was_playing = self._state == PlaybackState.PLAYING or (
                        self._state == PlaybackState.LOADING and self._loading_after_playing
                    )

                    # Check for track end before updating state
                    if was_playing and new_state == PlaybackState.STOPPED:
                        if in_grace_period:
                            # During grace period, ignore STOPPED state entirely
                            # This prevents false track-ended events while device is
                            # loading. Checked before touching the armed gapless
                            # track, which a short track may still move on to.
                            logger.debug(
                                f"Ignoring STOPPED state during grace period "
                                f"(started {time.monotonic() - self._playback_started_at:.1f}s ago)"
                            )
                            continue  # Skip state update entirely

                        # If gapless was armed but device stopped, clear and fall through
                        armed_for = (
                            time.monotonic() - self._next_armed_at
                            if self._next_track_proxy_url and not self._is_sonos
                            else 0.0
                        )
                        if self._next_track_proxy_url:
                            logger.debug(
                                "Gapless: device stopped despite armed next track, "
                                "falling through to normal track-ended"
                            )
                            self._next_track_proxy_url = None
                            self._next_track_metadata = None

                        early_stop = self._stopped_before_track_end()
                        # Armed well before the end and not taken: the
                        # renderer ignores SetNextAVTransportURI.
                        if not early_stop and armed_for >= IGNORED_NEXT_ARMED_FOR_SECONDS:
                            self._learn(
                                "gapless",
                                False,
                                "stopped at the end of a track with the next one queued",
                            )
                            self._disarm_soon()

                        if early_stop:
                            logger.info(
                                "[%s] Renderer stopped at %s of %s without a Qobuz command; "
                                "not a track end, so not advancing",
                                self.name,
                                _format_ms(self._furthest_position_ms),
                                _format_ms(self._duration_ms),
                            )
                            self._notify_playback_interrupted(self._furthest_position_ms)
                        else:
                            self._notify_track_ended()

                    self._loading_after_playing = was_playing and new_state == PlaybackState.LOADING
                    self._notify_state_change(new_state)

                # Update position while playing
                if new_state == PlaybackState.PLAYING:
                    furthest_before = self._furthest_position_ms
                    pos = await self.get_position()
                    self._note_playing(pos)

                    if self._switched_early(furthest_before, pos):
                        await self._recover_from_early_switch(furthest_before)
                        continue

                    if self._ended_without_stop(pos):
                        self._handle_end_without_stop(pos)
                        continue

                    self._notify_position_update(pos)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"State poll error: {e}")

    # =========================================================================
    # Renderer quirks: early URI, early switch, no STOPPED at the end
    # =========================================================================

    async def _moved_on_to_next(self) -> bool:
        """Whether a renderer naming the armed URI has really moved on to it.

        Some renderers (FiiO SR11) report the queued URI as current as soon as
        it is queued, while still playing the current track. Believe the
        change only when the position went back to the start of a track, or
        the current track was in its last seconds.
        """
        if not self._client:
            return True
        pos = await self._client.get_position_info()
        duration = self._duration_ms
        before = self._furthest_position_ms
        if not isinstance(pos, int) or duration <= 0 or before <= 0:
            # Nothing to judge by (no duration, or a renderer that never
            # reported a position for this track): trust the URI, as before.
            return True
        back_margin = min(5_000, duration // 3)
        went_back = pos + back_margin < before
        if self._quirks.reports_next_early:
            # Known to name the next track early: only a return to the
            # start of a track counts, even in the last seconds.
            if went_back and pos < min(15_000, duration // 3):
                return True
        elif went_back or before >= duration - min(15_000, duration // 3):
            return True

        # Still on the current track.
        self._position_ms = pos
        self._furthest_position_ms = max(self._furthest_position_ms, pos)
        if self._early_uri_logged_for != self._next_track_proxy_url:
            self._early_uri_logged_for = self._next_track_proxy_url
            logger.info(
                "[%s] Renderer names the queued track at %s of %s; "
                "keeping the current track until it ends",
                self.name,
                _format_ms(pos),
                _format_ms(duration),
            )
            self._learn(
                "reports_next_early",
                True,
                "named the queued track as current before reaching it",
            )
        return False

    def _switched_early(self, furthest_before: int, pos: int) -> bool:
        """Whether the renderer started the armed track as soon as it got it.

        The current URI is unchanged (no transition was seen), yet the
        position fell back to the start of a track with plenty of the current
        one left: the queued track is playing under the current one's name.
        """
        if self._is_sonos or not self._next_track_proxy_url:
            return False
        # No start-of-track grace here: arming waits for the renderer to play
        # (ARM_AFTER_POSITION_MS), and a renderer that switches at once does so
        # inside the first seconds, where a grace period could hide the jump.
        if time.monotonic() - self._last_seek_at < SEEK_SETTLE_SECONDS:
            return False
        remaining = self._duration_ms - furthest_before if self._duration_ms > 0 else None
        return (
            pos <= EARLY_SWITCH_TO_MS
            and pos + EARLY_SWITCH_BACK_MS <= furthest_before
            and (remaining is None or remaining >= EARLY_SWITCH_REMAINING_MS)
        )

    async def _recover_from_early_switch(self, resume_ms: int) -> None:
        """Turn gapless off for this renderer and put the current track back."""
        logger.warning(
            "[%s] Renderer started the queued track at %s into the current one",
            self.name,
            _format_ms(resume_ms),
        )
        self._learn("gapless", False, "starts a queued track as soon as it is queued")
        self._next_track_proxy_url = None
        self._next_track_metadata = None
        self._next_track_queue_nr = None
        # The renderer keeps the queued URI (FiiO K17) until it is cleared.
        await self._disarm_on_renderer()
        if self._quirks.sources.get("gapless") == "user":
            logger.warning(
                "[%s] Gapless is forced on for this speaker; set it to auto or off "
                "to stop this renderer skipping ahead",
                self.name,
            )
            return

        url, metadata = self._current_proxy_url, self._current_metadata
        if not url or not metadata or not self._client:
            return
        content_type = "audio/mpeg" if url.lower().endswith(".mp3") else "audio/flac"
        didl = self._build_didl(url, metadata, content_type)
        self._delivered_keys.discard(_proxy_key(url))
        self._starting_playback = True
        reached = 0
        try:
            if await self._try_transport_sequence(url, didl) is not None:
                logger.warning("[%s] Could not reload the current track", self.name)
                return
            self._playback_started_at = time.monotonic()
            # Seek once the renderer is playing again (most refuse it before).
            for _ in range(20):
                await asyncio.sleep(0.25)
                if await self._client.get_transport_info() == "PLAYING":
                    break
            # With little to lose, restart rather than send one more command
            # to a renderer that just misbehaved.
            if resume_ms >= EARLY_SWITCH_MIN_SEEK_MS:
                reached = await self._seek_and_confirm(resume_ms)
        finally:
            self._starting_playback = False
        self._position_ms = reached
        self._furthest_position_ms = reached
        self._stall_position_ms = -1
        self._notify_position_update(reached)
        logger.info(
            "[%s] Current track reloaded, now at %s; gapless is off for this renderer",
            self.name,
            _format_ms(reached),
        )

    async def _seek_and_confirm(self, target_ms: int) -> int:
        """Seek, then read back where the renderer really is.

        A renderer can accept a Seek and ignore it (a FiiO K17 right after a
        reload restarted from 0:00), so the position reported to the app is
        read from the renderer rather than assumed.
        """
        assert self._client
        if not await self._client.seek(target_ms):
            logger.info("[%s] Renderer refused the seek; track restarted", self.name)
            return 0
        self._last_seek_at = time.monotonic()
        position: Optional[int] = None
        for _ in range(SEEK_CONFIRM_STEPS):
            await asyncio.sleep(0.25)
            position = await self._client.get_position_info()
            if position is not None and position >= target_ms - SEEK_CONFIRM_TOLERANCE_MS:
                return position
        if position is None:
            return target_ms  # renderer reports no position: assume the seek held
        logger.warning(
            "[%s] Seek to %s did not take effect; renderer is at %s",
            self.name,
            _format_ms(target_ms),
            _format_ms(position),
        )
        return position

    def _ended_without_stop(self, pos: int) -> bool:
        """Whether a renderer still reporting PLAYING has finished the track.

        Some renderers (FiiO K17) never report STOPPED at the end of a track:
        they stay PLAYING with the position parked at the track's length.
        """
        duration = self._duration_ms
        if (
            duration <= 0
            or self._starting_playback
            or self._end_handled_for == self._current_proxy_url
            or time.monotonic() - self._playback_started_at < PLAYBACK_START_GRACE_PERIOD_SECONDS
            or time.monotonic() - self._last_seek_at < SEEK_SETTLE_SECONDS
        ):
            return False
        now = time.monotonic()
        if self._stall_position_ms < 0 or abs(pos - self._stall_position_ms) > STALL_TOLERANCE_MS:
            self._stall_position_ms = pos
            self._stall_since = now
        if pos >= duration + PAST_END_MS:
            return True
        if pos < duration - END_WINDOW_MS:
            return False
        delivered = _proxy_key(self._current_proxy_url or "") in self._delivered_keys
        known = self._quirks.stops_at_end is False
        if self._next_track_proxy_url:
            # A renderer about to move on to the armed track may hold at the
            # end for a moment; give it the full time before stepping in.
            needed = END_STALL_SECONDS
        elif pos >= duration and (delivered or known):
            needed = END_STALL_AT_END_SECONDS
        elif delivered:
            needed = END_STALL_DELIVERED_SECONDS
        elif known:
            needed = END_STALL_KNOWN_SECONDS
        else:
            needed = END_STALL_SECONDS
        return now - self._stall_since >= needed

    def _handle_end_without_stop(self, pos: int) -> None:
        """Report a track end the renderer itself never signalled."""
        logger.info(
            "[%s] Still PLAYING at %s of %s with the position not moving; "
            "treating it as the end of the track",
            self.name,
            _format_ms(pos),
            _format_ms(self._duration_ms),
        )
        self._learn("stops_at_end", False, "stayed PLAYING at the end of a track")
        if (
            self._next_track_proxy_url
            and not self._is_sonos
            and time.monotonic() - self._next_armed_at >= IGNORED_NEXT_ARMED_FOR_SECONDS
        ):
            self._learn(
                "gapless",
                False,
                "stayed on a finished track with the next one queued",
            )
        if self._next_track_proxy_url and not self._is_sonos:
            self._disarm_soon()
        self._next_track_proxy_url = None
        self._next_track_metadata = None
        self._end_handled_for = self._current_proxy_url
        self._stall_position_ms = -1
        # Mark stopped so the renderer's own stop, if it comes, is not a second end.
        self._state = PlaybackState.STOPPED
        self._notify_track_ended()

    def _next_poll_delay(self) -> float:
        """Seconds to wait before the next state poll."""
        near_end = (
            self._state == PlaybackState.PLAYING
            and self._duration_ms > 0
            and self._duration_ms - self._furthest_position_ms <= NEAR_END_WINDOW_MS
        )
        return NEAR_END_POLL_INTERVAL_SECONDS if near_end else STATE_POLL_INTERVAL_SECONDS

    def _stopped_before_track_end(self) -> bool:
        """Whether a stop landed well before the end of a track that streamed fine.

        A stream the proxy cut short ends early on the renderer too; that is
        still the end of what could be played, so it counts as a track end.
        """
        if self._current_proxy_url and (
            self._delivery_failures.pop(_proxy_key(self._current_proxy_url), None) is not None
        ):
            return False
        # Unknown duration or a renderer that never reported a position: keep
        # treating the stop as a track end rather than stall the queue.
        return (
            self._duration_ms > 0
            and self._furthest_position_ms > 0
            and self._duration_ms - self._furthest_position_ms > EARLY_STOP_MARGIN_MS
        )

    def _build_didl(
        self,
        url: str,
        metadata: BackendTrackMetadata,
        content_type: str = "audio/flac",
    ) -> str:
        """Build DIDL-Lite metadata XML."""

        def escape(s: str) -> str:
            return (
                s.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
            )

        # Build protocol info string based on capabilities
        if self._capabilities:
            protocol_info = build_protocol_info(self._capabilities, content_type)
        else:
            protocol_info = f"http-get:*:{content_type}:*"

        # Format duration as H:MM:SS for the res element
        duration_attr = ""
        if metadata.duration_ms > 0:
            total_s = metadata.duration_ms // 1000
            h = total_s // 3600
            m = (total_s % 3600) // 60
            s = total_s % 60
            duration_attr = f' duration="{h}:{m:02d}:{s:02d}"'

        didl = f"""<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
                   xmlns:dc="http://purl.org/dc/elements/1.1/"
                   xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
    <item id="1" parentID="0" restricted="1">
        <dc:title>{escape(metadata.title)}</dc:title>
        <dc:creator>{escape(metadata.artist)}</dc:creator>
        <upnp:artist>{escape(metadata.artist)}</upnp:artist>
        <upnp:album>{escape(metadata.album)}</upnp:album>
        <upnp:class>object.item.audioItem.musicTrack</upnp:class>"""

        if metadata.artwork_url:
            didl += f"\n        <upnp:albumArtURI>{escape(metadata.artwork_url)}</upnp:albumArtURI>"

        audio_attrs = ""
        if metadata.sample_rate:
            audio_attrs += f' sampleFrequency="{metadata.sample_rate}"'
        if metadata.bit_depth:
            audio_attrs += f' bitsPerSample="{metadata.bit_depth}"'

        didl += f"""
        <res protocolInfo="{escape(protocol_info)}"{duration_attr}{audio_attrs}>{escape(url)}</res>
    </item>
</DIDL-Lite>"""

        return didl
