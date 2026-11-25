"""
Audio Proxy Server.

HTTP server that proxies audio streams from Qobuz CDN to DLNA devices,
handling URL expiration transparently. It also receives the renderer's UPnP
event notifications (NOTIFY), since the renderer can already reach it.
"""

import logging
import time
from typing import Callable, Dict, Optional

from aiohttp import ClientSession, ClientTimeout, web

from qobuz_proxy.net import get_local_ip

from .proxy_http import (  # noqa: F401 (re-exported)
    DEFAULT_URL_MAX_AGE_SECONDS,
    DLNA_CONTENT_FEATURES,
    RegisteredTrack,
    dlna_headers,
    full_size,
    parse_range_start,
    reaches_end_of_file,
)
from .proxy_stream import StreamRelay
from .url_provider import StreamingURLProvider

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30
# A size probe for the DIDL must not hold up arming the next track for long.
SIZE_PROBE_TIMEOUT_SECONDS = 3
# Registrations kept; older ones are dropped (a session registers every track).
MAX_REGISTERED_TRACKS = 64

# Kept under their old names for callers and tests.
_dlna_headers = dlna_headers
_reaches_end_of_file = reaches_end_of_file
_parse_range_start = parse_range_start

NotifyHandler = Callable[[str, str], bool]


class AudioProxyServer:
    """
    Local HTTP proxy server for DLNA audio streaming.

    Provides stable local URLs to DLNA devices while handling:
    - Qobuz URL expiration (5-minute TTL)
    - HTTP range requests for seeking
    - Streaming without full buffering

    Usage:
        proxy = AudioProxyServer(url_provider=provider, host="0.0.0.0", port=7120)
        await proxy.start()
        proxy_url = proxy.register_track("12345", qobuz_url, "audio/flac")
        # proxy_url = "http://192.168.1.100:7120/audio/12345.flac"
    """

    def __init__(
        self,
        url_provider: StreamingURLProvider,
        host: str = "0.0.0.0",
        port: int = 7120,
        url_max_age: float = DEFAULT_URL_MAX_AGE_SECONDS,
    ):
        self._url_provider = url_provider
        self._host = host
        self._port = port
        self._url_max_age = url_max_age

        self._tracks: Dict[str, RegisteredTrack] = {}
        self._on_delivery_failure: Optional[Callable[[str, bool], None]] = None
        self._on_delivery_complete: Optional[Callable[[str], None]] = None
        self._on_request: Optional[Callable[[str], None]] = None
        self._event_handlers: Dict[str, NotifyHandler] = {}
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        # Will be set after start() to actual bound address
        self._actual_host: Optional[str] = None

    @property
    def base_url(self) -> str:
        """Get the base URL for this proxy server."""
        host = self._actual_host or self._host
        if host == "0.0.0.0":
            host = self._get_local_ip()
        return f"http://{host}:{self._port}"

    @property
    def is_running(self) -> bool:
        """Check if the server is running."""
        return self._site is not None

    async def start(self) -> None:
        """Start the proxy server."""
        self._app = web.Application()
        self._app.router.add_get("/audio/{track_id}", self._handle_audio)
        self._app.router.add_get("/audio/{track_id}.flac", self._handle_audio)
        self._app.router.add_get("/audio/{track_id}.mp3", self._handle_audio)
        self._app.router.add_route("NOTIFY", "/upnp/event/{token}", self._handle_notify)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info(f"Audio proxy server started on {self._host}:{self._port}")

    async def stop(self) -> None:
        """Stop the proxy server."""
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._tracks.clear()
        self._event_handlers.clear()
        logger.info("Audio proxy server stopped")

    # =========================================================================
    # Registrations
    # =========================================================================

    def register_track(
        self,
        track_id: str,
        qobuz_url: str,
        content_type: str = "audio/flac",
        proxy_key: Optional[str] = None,
    ) -> str:
        """
        Register a track for proxying; returns its local proxy URL.

        ``proxy_key`` names the URL path (default: the track ID). Gapless
        uses keys like "{track_id}_{queue_item_id}" so duplicates in a queue
        get distinct URLs.
        """
        key = proxy_key or track_id
        previous = self._tracks.pop(key, None)
        self._tracks[key] = RegisteredTrack(
            track_id=track_id,
            qobuz_url=qobuz_url,
            content_type=content_type,
            url_fetched_at=time.time(),
            # The same track keeps its size across registrations.
            size=previous.size if previous and previous.track_id == track_id else None,
        )
        while len(self._tracks) > MAX_REGISTERED_TRACKS:
            self._tracks.pop(next(iter(self._tracks)))

        ext = "flac" if "flac" in content_type else "mp3"
        proxy_url = f"{self.base_url}/audio/{key}.{ext}"
        logger.debug(f"Registered track {track_id} (key={key}) -> {proxy_url}")
        return proxy_url

    def unregister_track(self, track_id: str) -> None:
        """Remove a track from the registry."""
        if self._tracks.pop(track_id, None):
            logger.debug(f"Unregistered track {track_id}")

    def update_track_url(self, track_id: str, qobuz_url: str) -> None:
        """Update the Qobuz URL for a registered track."""
        track = self._tracks.get(track_id)
        if track:
            track.qobuz_url = qobuz_url
            track.url_fetched_at = time.time()
            logger.debug(f"Updated URL for track {track_id}")

    def known_size(self, proxy_key: str) -> Optional[int]:
        """File size of a registered track, if an upstream answer has told us."""
        track = self._tracks.get(proxy_key)
        return track.size if track else None

    async def probe_size(self, proxy_key: str) -> Optional[int]:
        """Ask the CDN for a registered track's size (HEAD); None if it won't say."""
        track = self._tracks.get(proxy_key)
        if track is None:
            return None
        if track.size is None:
            track.size = await self._upstream_size(track, SIZE_PROBE_TIMEOUT_SECONDS)
        return track.size

    # =========================================================================
    # Callbacks
    # =========================================================================

    def set_delivery_failure_callback(self, callback: Callable[[str, bool], None]) -> None:
        """Register ``callback(proxy_key, before_audio)`` for streams we could not serve.

        ``before_audio`` is True when the renderer got no audio at all (an error
        response), False when the stream was cut short after audio was sent.
        """
        self._on_delivery_failure = callback

    def set_delivery_complete_callback(self, callback: Callable[[str], None]) -> None:
        """Register ``callback(proxy_key)`` for a track sent through to its last byte.

        Lets the backend tell a renderer that has the whole file (and is
        only playing out its buffer) from one still waiting for audio.
        """
        self._on_delivery_complete = callback

    def set_request_callback(self, callback: Callable[[str], None]) -> None:
        """Register ``callback(proxy_key)`` for every audio request a renderer makes.

        A renderer cannot play a track it never fetched, which tells a real
        switch to a queued track from a seek on the renderer.
        """
        self._on_request = callback

    def _call(self, callback: Optional[Callable[..., None]], *args: object) -> None:
        if callback:
            try:
                callback(*args)
            except Exception as e:
                logger.error(f"Proxy callback error: {e}")

    def _report_delivery_complete(self, proxy_key: str) -> None:
        self._call(self._on_delivery_complete, proxy_key)

    def _report_delivery_failure(self, proxy_key: str, before_audio: bool) -> None:
        self._call(self._on_delivery_failure, proxy_key, before_audio)

    # =========================================================================
    # UPnP events
    # =========================================================================

    def register_event_handler(self, token: str, handler: NotifyHandler) -> str:
        """Route NOTIFY requests for ``token`` to ``handler(sid, body)``; returns the URL."""
        self._event_handlers[token] = handler
        return f"{self.base_url}/upnp/event/{token}"

    def unregister_event_handler(self, token: str) -> None:
        """Stop routing NOTIFY requests for ``token``."""
        self._event_handlers.pop(token, None)

    async def _handle_notify(self, request: web.Request) -> web.Response:
        handler = self._event_handlers.get(request.match_info["token"])
        if handler is None:
            return web.Response(status=412)
        body = await request.text()
        accepted = handler(request.headers.get("SID", ""), body)
        return web.Response(status=200 if accepted else 412)

    # =========================================================================
    # Audio
    # =========================================================================

    async def _handle_audio(self, request: web.Request) -> web.StreamResponse:
        """Handle audio stream requests from DLNA devices."""
        # The registration key, not necessarily the Qobuz track ID: gapless
        # registers tracks with composite keys like "{track_id}_{queue_item_id}".
        proxy_key = request.match_info["track_id"].rsplit(".", 1)[0]
        response = await self._serve_audio(request, proxy_key)
        logger.debug(
            "Renderer %s %s range=%s -> %s length=%s",
            request.method,
            proxy_key,
            request.headers.get("Range", "-"),
            response.status,
            response.headers.get("Content-Length", "none"),
        )
        return response

    async def _serve_audio(self, request: web.Request, proxy_key: str) -> web.StreamResponse:
        track = self._tracks.get(proxy_key)
        if not track:
            logger.warning(f"Unknown track requested: {proxy_key}")
            return web.Response(status=404, text="Track not found")
        if request.method != "HEAD":
            self._call(self._on_request, proxy_key)

        # force=True bypasses the metadata cache: its TTL is longer than the
        # proxy's max age, so a plain fetch inside the overlap window would hand
        # back the same dying URL while resetting url_fetched_at.
        if track.is_url_expired(self._url_max_age):
            logger.info(f"Refreshing expired URL for track {track.track_id}")
            if not await self._refresh_track_url(track, force=True):
                if request.method != "HEAD":
                    self._report_delivery_failure(proxy_key, True)
                return web.Response(status=502, text="Failed to refresh streaming URL")

        # HEAD probes (Denon/HEOS send one before every GET) route here too via
        # add_get's implicit HEAD support. Answer them headers-only — streaming
        # the body just to have aiohttp discard it downloads the whole track.
        if request.method == "HEAD":
            response = await self._handle_head_probe(track)
            response.headers.update(dlna_headers(request))
            return response

        relay = StreamRelay(
            request,
            track,
            proxy_key,
            refresh_url=lambda t: self._refresh_track_url(t, force=True),
            report_failure=self._report_delivery_failure,
            report_complete=self._report_delivery_complete,
        )
        return await relay.run()

    async def _handle_head_probe(self, track: RegisteredTrack) -> web.Response:
        """Answer a HEAD probe from upstream headers, without a body transfer."""
        response = web.Response(
            status=200,
            headers={"Content-Type": track.content_type, "Accept-Ranges": "bytes"},
        )
        size = track.size or await self._upstream_size(track, REQUEST_TIMEOUT_SECONDS)
        if size:
            track.size = size
            # Via the header: the content_length property raises on a
            # body-less Response, so the size was silently dropped.
            response.headers["Content-Length"] = str(size)
        return response

    async def _upstream_size(self, track: RegisteredTrack, timeout_s: float) -> Optional[int]:
        """The file size from an upstream HEAD, or None."""
        try:
            async with ClientSession(timeout=ClientTimeout(total=timeout_s)) as session:
                async with session.head(track.qobuz_url, allow_redirects=True) as upstream:
                    return full_size(upstream.status, upstream.headers)
        except Exception as e:
            # An answer without a size is still useful; renderers mostly check
            # availability and type with HEAD.
            logger.debug(f"Upstream HEAD failed for track {track.track_id}: {e}")
            return None

    async def _refresh_track_url(self, track: RegisteredTrack, force: bool = False) -> bool:
        """Fetch a fresh streaming URL before retrying (signed URLs can go stale).

        force=True bypasses the URL provider's cache — required whenever the
        current URL is known or suspected dead, since a cached "valid" URL may
        be the very one that just failed.
        """
        try:
            fresh_url = await self._url_provider.get_streaming_url(track.track_id, force=force)
            track.qobuz_url = fresh_url
            track.url_fetched_at = time.time()
            return True
        except Exception as e:
            logger.warning(
                f"Could not refresh URL for track {track.track_id}: {e}; retrying with old URL"
            )
            return False

    def _get_local_ip(self) -> str:
        """Local LAN address for proxy URLs."""
        return get_local_ip() or "127.0.0.1"
