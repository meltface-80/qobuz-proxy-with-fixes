"""
Shared state and helpers for the DLNA backend.

``DLNABackend`` is built in layers, each a class in its own module that adds
one concern on top of the one below:

    backend_base  state, renderer quirks, DIDL, transport helpers
    ownership     is the renderer still playing our stream? (takeovers)
    gapless       arming the next track and detecting the switch to it
    track_end     deciding when a track has ended
    polling       the state poll loop (and UPnP event wake-ups)
    backend       connect, play / pause / seek / volume, info
"""

import asyncio
import logging
import secrets
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.backends.types import BackendTrackMetadata

from .capabilities import (
    CapabilityCache,
    DLNACapabilities,
    apply_device_overrides,
    build_protocol_info,
    parse_protocol_info_sink,
)
from .client import DLNAClient
from .description import DLNADeviceInfo
from .didl import build_didl, proxy_key
from .events import EventSubscription
from .timing import RELOAD_PLAYING_STEP_SECONDS, RELOAD_PLAYING_WAIT_STEPS
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

# Class-level capability cache (shared across instances)
_capability_cache = CapabilityCache()


class DLNABackendBase(AudioBackend):
    """State shared by every layer of the DLNA backend."""

    def __init__(
        self,
        ip: str,
        port: int = 1400,
        fixed_volume: bool = False,
        name: Optional[str] = None,
        description_url: Optional[str] = None,
        gapless: str = GAPLESS_AUTO,
        quirk_store: Optional[QuirkStore] = None,
        udn: str = "",
        bind_address: str = "0.0.0.0",
    ):
        super().__init__(name or f"DLNA ({ip})")
        self._ip = ip
        self._port = port
        self._fixed_volume = fixed_volume
        self._description_url = description_url
        # The renderer's UPnP UDN, when known: finds it again after an IP change.
        self._udn = udn
        self._bind_address = bind_address

        self._client: Optional[DLNAClient] = None
        self._poll_task: Optional[asyncio.Task[None]] = None
        # Set to poll at once (a UPnP event arrived) instead of after the interval.
        self._poll_wake = asyncio.Event()
        # UPnP event subscription (renderer -> audio proxy NOTIFY callbacks).
        self._events: Optional[EventSubscription] = None
        self._events_task: Optional[asyncio.Task[None]] = None
        self._events_active = False
        self._event_token = secrets.token_hex(8)
        # Fire-and-forget renderer commands, kept referenced until they finish.
        self._background_tasks: set[asyncio.Task[None]] = set()

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
        # Proxy keys whose whole file the proxy has sent to the renderer.
        self._delivered_keys: set[str] = set()
        # Proxy keys the renderer has asked the proxy for.
        self._requested_keys: set[str] = set()

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
        # When we last asked the renderer to seek (monotonic).
        self._last_seek_at: float = 0.0
        # The track URL whose end was already reported: one end per playback.
        self._end_handled_for: Optional[str] = None
        # Position to resume from after a Pause the renderer refused was
        # carried out as a Stop (GitHub #44); None when not paused that way.
        self._paused_by_stop_ms: Optional[int] = None

        # Sonos queue-based playback (for Sonos app metadata display)
        self._is_sonos: bool = False
        self._sonos_queue_head_url: Optional[str] = None
        self._external_playback = False
        self._starting_playback = False
        self._stopping_playback = False
        self._last_stop_at: Optional[float] = None
        self._on_external_playback: Optional[Callable[[], Awaitable[None]]] = None

    # =========================================================================
    # Identity
    # =========================================================================

    @property
    def device_info(self) -> Optional[DLNADeviceInfo]:
        """Description of the connected renderer."""
        info = getattr(self._client, "device_info", None)
        return info if isinstance(info, DLNADeviceInfo) else None

    @property
    def udn(self) -> str:
        """UDN of the connected renderer (or the configured one before connecting)."""
        info = getattr(self._client, "device_info", None)
        return str(getattr(info, "udn", "") or "") or self._udn

    @property
    def location(self) -> dict[str, str]:
        """Where the renderer was found: ip, port, description URL and UDN."""
        return {
            "ip": self._ip,
            "port": str(self._port),
            "description_url": self._description_url or "",
            "udn": self.udn,
        }

    # =========================================================================
    # Renderer quirks
    # =========================================================================

    @property
    def quirks(self) -> DeviceQuirks:
        """Effective renderer behaviour (profile, learned and user layers)."""
        return self._quirks

    def _apply_quirks(self, device_info: DLNADeviceInfo) -> None:
        """Resolve profile, learned and user behaviour for this renderer."""
        self._quirks = resolve_quirks(
            device_info.udn,
            device_info.manufacturer,
            device_info.model_name,
            self._gapless_mode,
            self._quirk_store,
            friendly_name=getattr(device_info, "friendly_name", ""),
        )
        # The service description says SetNextAVTransportURI does not exist:
        # no need to try it (unless the user forced gapless on).
        supports_next = getattr(device_info, "supports_action", None)
        if (
            callable(supports_next)
            and supports_next("SetNextAVTransportURI") is False
            and self._quirks.sources.get("gapless") != "user"
        ):
            self._quirks.gapless = False
            self._quirks.sources["gapless"] = "service description"
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
        saved = self._quirk_store.learn(self.udn, name, value, label=self.name)
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

    def forget_learned_quirks(self) -> bool:
        """Drop learned behaviour for this renderer and resolve it again."""
        forgotten = self._quirk_store.forget(self.udn)
        info = self.device_info
        if info is not None:
            self._gapless_supported = True
            self._apply_quirks(info)
        return forgotten

    # =========================================================================
    # Capabilities and recommended quality
    # =========================================================================

    async def _discover_capabilities(self, device_info: DLNADeviceInfo) -> None:
        """Query and parse device capabilities."""
        device_id = device_info.udn or self._ip
        cached = _capability_cache.get(device_id)
        if cached:
            logger.debug(f"Using cached capabilities for {device_id}")
            self._capabilities = cached
            return
        try:
            if not self._client:
                return
            sink = await self._client.get_protocol_info()
            if sink:
                self._capabilities = parse_protocol_info_sink(sink)
                apply_device_overrides(
                    self._capabilities, device_info.manufacturer, device_info.model_name
                )
                _capability_cache.set(device_id, self._capabilities)
            else:
                logger.debug("GetProtocolInfo not supported, using defaults")
                self._capabilities = None
        except Exception as e:
            logger.warning(f"Failed to discover capabilities: {e}")
            self._capabilities = None

    def get_capabilities(self) -> Optional[DLNACapabilities]:
        """Discovered device capabilities, if any."""
        return self._capabilities

    def get_recommended_quality(self) -> Optional[int]:
        """Recommended Qobuz quality (5, 6, 7 or 27), or None if unknown.

        What the renderer states about its formats wins; a model profile
        fills in for renderers that list FLAC without sample rates.
        """
        caps = self._capabilities
        if caps is not None and caps.format_info_confirmed:
            return caps.max_quality
        profile_quality = self._quirks.max_quality
        if profile_quality and (caps is None or caps.supports_flac):
            return profile_quality
        return caps.max_quality if caps is not None else None

    @property
    def quality_detection_confirmed(self) -> bool:
        """Whether the recommended quality came from device format info or a model profile.

        False means the device advertised FLAC without stating sample rates or
        bit depths, so the recommendation is just a conservative CD default.
        """
        caps = self._capabilities
        if caps is not None and caps.format_info_confirmed:
            return True
        return bool(self._quirks.max_quality) and (caps is None or caps.supports_flac)

    # =========================================================================
    # Per-track state
    # =========================================================================

    def _reset_track_tracking(self) -> None:
        """Forget per-track observations when a new track starts."""
        self._playing_confirmed = False
        self._first_playing_at = None
        self._stall_position_ms = -1
        self._stall_since = 0.0
        self._early_uri_logged_for = None
        self._end_handled_for = None
        self._paused_by_stop_ms = None

    def _clear_next_track(self) -> None:
        """Forget the armed next track (without touching the renderer)."""
        self._next_track_proxy_url = None
        self._next_track_metadata = None
        self._next_track_queue_nr = None

    def _forget_delivery(self, url: str) -> None:
        """Proxy evidence from an earlier attempt at this URL is not this play's."""
        key = proxy_key(url)
        self._delivery_failures.pop(key, None)
        self._delivered_keys.discard(key)
        self._requested_keys.discard(key)

    # =========================================================================
    # Proxy callbacks
    # =========================================================================

    def _on_delivery_complete(self, key: str) -> None:
        """Remember a track whose whole file the renderer has received."""
        self._delivered_keys.add(key)

    def _on_delivery_failure(self, key: str, before_audio: bool) -> None:
        """Remember a track the renderer could not get (fully) from the proxy."""
        self._delivery_failures[key] = before_audio or self._delivery_failures.get(key, False)

    def _on_stream_requested(self, key: str) -> None:
        """Remember that the renderer fetched this track from the proxy."""
        self._requested_keys.add(key)

    # =========================================================================
    # Transport helpers
    # =========================================================================

    def _build_didl(
        self,
        url: str,
        metadata: BackendTrackMetadata,
        content_type: str = "audio/flac",
        size: Optional[int] = None,
    ) -> str:
        """Build DIDL-Lite metadata XML for a track."""
        if self._capabilities:
            protocol_info = build_protocol_info(self._capabilities, content_type)
        else:
            protocol_info = f"http-get:*:{content_type}:DLNA.ORG_OP=01"
        return build_didl(url, metadata, protocol_info, size)

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

    async def _wait_until_playing(self) -> None:
        """Wait (briefly) for the renderer to report PLAYING after a reload."""
        assert self._client
        for _ in range(RELOAD_PLAYING_WAIT_STEPS):
            await asyncio.sleep(RELOAD_PLAYING_STEP_SECONDS)
            if await self._client.get_transport_info() == "PLAYING":
                return

    async def _reload_current_track(self, resume_ms: int) -> bool:
        """Load the current track again and seek to ``resume_ms``.

        Used where the renderer lost our track: after a Pause carried out as
        a Stop, and after a renderer jumped to the queued track too early.
        Returns False if the renderer would not load it.
        """
        url, metadata = self._current_proxy_url, self._current_metadata
        if not url or not metadata or not self._client:
            return False
        content_type = "audio/mpeg" if url.lower().endswith(".mp3") else "audio/flac"
        size = self._proxy_server.known_size(proxy_key(url)) if self._proxy_server else None
        didl = self._build_didl(url, metadata, content_type, size)
        self._forget_delivery(url)
        self._starting_playback = True
        try:
            if await self._try_transport_sequence(url, didl) is not None:
                logger.warning("[%s] Could not reload the current track", self.name)
                return False
            self._playback_started_at = time.monotonic()
            self._last_stop_at = None
            # Seek once the renderer is playing again (most refuse it before).
            await self._wait_until_playing()
            if resume_ms > 0 and await self._client.seek(resume_ms):
                self._last_seek_at = time.monotonic()
            elif resume_ms > 0:
                logger.info("[%s] Renderer refused the seek; track restarted", self.name)
                resume_ms = 0
        finally:
            self._starting_playback = False
        self._position_ms = resume_ms
        self._furthest_position_ms = resume_ms
        self._stall_position_ms = -1
        self._notify_position_update(resume_ms)
        return True
