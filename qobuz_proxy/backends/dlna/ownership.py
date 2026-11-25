"""
Transport ownership: is the renderer still playing our stream?

Other controllers (Spotify, AirPlay, the renderer's own buttons) may take a
renderer over at any time; Qobuz commands must then leave it alone. Renderers
report what they play in different ways, so "what is playing" is read with
fallbacks:

- Sonos plays from its queue: GetPositionInfo.TrackURI names the track.
- Most renderers: GetMediaInfo.CurrentURI.
- Bose SoundTouch leaves CurrentURI empty while playing and puts a
  ``qplay://`` placeholder there after a Stop, while TrackURI keeps naming
  our track (GitHub #43).
"""

import logging
import time
from typing import Awaitable, Callable, Optional

from .backend_base import DLNABackendBase
from .didl import source_label
from .timing import PLAYBACK_START_GRACE_PERIOD_SECONDS

logger = logging.getLogger(__name__)

# Placeholder some renderers (Bose SoundTouch) report when nothing is loaded.
IDLE_PLACEHOLDER_PREFIX = "qplay:"
# What ``_read_source_uri`` returns for an idle placeholder after our own Stop.
IDLE = "<idle>"
# Transport states of a renderer that is not playing anything.
IDLE_TRANSPORT_STATES = ("STOPPED", "NO_MEDIA_PRESENT")
# States in which an idle placeholder after our own Stop is not a takeover.
NOT_PLAYING_STATES = ("STOPPED", "PAUSED_PLAYBACK", "NO_MEDIA_PRESENT")


class OwnershipMixin(DLNABackendBase):
    """Decides whether Qobuz still controls the renderer."""

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

    def _transport_busy(self) -> bool:
        """A Qobuz command is changing the transport right now."""
        return self._external_playback or self._starting_playback or self._stopping_playback

    async def _read_source_uri(self) -> Optional[str]:
        """What the renderer is playing.

        Returns the URI, "" when nothing is loaded, ``IDLE`` for a placeholder
        after our own Stop, or None when the read failed.
        """
        assert self._client
        if self._is_sonos:
            return await self._client.get_track_uri()
        uri = await self._client.get_media_info()
        if uri is None:
            return None
        placeholder = uri.lower().startswith(IDLE_PLACEHOLDER_PREFIX)
        if uri and not placeholder:
            return uri
        # CurrentURI is empty or a placeholder: TrackURI may still name ours.
        track_uri = await self._client.get_track_uri()
        if track_uri and track_uri in (self._current_proxy_url, self._next_track_proxy_url):
            return track_uri
        if placeholder and self._last_stop_at is not None:
            # Idle only while nothing plays (SoundTouch reports PAUSED_PLAYBACK
            # after our Stop). A placeholder while PLAYING is another source;
            # an unreadable state is unknown and fails closed.
            transport = await self._client.get_transport_info()
            if transport is None:
                return None
            if transport.strip().upper() in NOT_PLAYING_STATES:
                return IDLE
        return uri

    async def can_apply_remote_state(self) -> bool:
        """Require source confirmation for snapshots of an already loaded transport."""
        if not self._current_proxy_url:
            return not self._external_playback
        if await self._owns_transport():
            return True
        # upmpdcli 1.9+ clears CurrentURI when stopped, and KEF LSX does at the
        # end of a queue (GitHub #35, #36). An idle renderer with nothing
        # loaded belongs to no other source, so the app may load onto it
        # again; refusing left the speaker dead. An empty URI while playing
        # may be a device-internal source (AirPlay, line-in): still refused.
        if self._is_sonos or self._transport_busy() or not self._client:
            return False
        return await self._renderer_idle()

    async def _renderer_idle(self) -> bool:
        """Nothing loaded (or only an idle placeholder) and not playing."""
        assert self._client
        uri = await self._read_source_uri()
        if uri == IDLE:
            return True
        if uri != "":
            return False
        return await self._client.get_transport_info() in IDLE_TRANSPORT_STATES

    async def _owns_transport(self) -> bool:
        """Only control the current or armed Qobuz track; unknown sources fail closed."""
        if self._transport_busy() or not self._client or not self._current_proxy_url:
            return False
        expected = self._current_proxy_url
        armed_at_read = self._next_track_proxy_url
        current_uri = await self._read_source_uri()
        # A concurrent explicit play may have replaced the track during the read.
        if (
            self._transport_busy()
            or expected != self._current_proxy_url
            or not current_uri
            or current_uri == IDLE
        ):
            return False
        if current_uri in (self._current_proxy_url, self._next_track_proxy_url):
            return True
        if await self._sonos_queue_head_after_stop(current_uri, expected):
            return True
        # Renderers can briefly report the old source while loading our URI.
        if time.monotonic() - self._playback_started_at < PLAYBACK_START_GRACE_PERIOD_SECONDS:
            return False
        logger.warning(
            "[%s] Source mismatch: observed=%s expected=%s armed_at_read=%s armed_now=%s "
            "cached_state=%s last_successful_stop_age_s=%s",
            self.name,
            source_label(current_uri),
            source_label(expected),
            source_label(armed_at_read),
            source_label(self._next_track_proxy_url),
            self._state.name,
            round(time.monotonic() - self._last_stop_at, 3)
            if self._last_stop_at is not None
            else None,
        )
        self._external_playback = True
        self._clear_next_track()
        logger.info("[%s] External source took over; releasing Qobuz playback control", self.name)
        if self._on_external_playback:
            await self._on_external_playback()
        return False

    async def _sonos_queue_head_after_stop(self, current_uri: str, expected: str) -> bool:
        """Sonos Stop resets TrackURI to the first entry of our retained queue.

        Even after gapless playback advanced to a later entry. Only accept that
        exact URI following our own successful stop, while still stopped.
        """
        if not (
            self._is_sonos
            and self._client
            and self._last_stop_at is not None
            and current_uri == self._sonos_queue_head_url
        ):
            return False
        transport = await self._client.get_transport_info()
        if (
            self._transport_busy()
            or expected != self._current_proxy_url
            or self._last_stop_at is None
            or current_uri != self._sonos_queue_head_url
        ):
            return False
        if transport == "STOPPED":
            logger.debug("[%s] Sonos reported Qobuz queue head after Stop", self.name)
            return True
        return False
