"""
Gapless playback: arming the next track and following the renderer onto it.

Renderers handle SetNextAVTransportURI in different ways:

- Most queue it and move on at the end of the current track (UPnP spec).
- Some start the queued track at once (reported for the FiiO K17): the
  position falls back to the start while the current URI is unchanged.
- Some name the queued URI as current as soon as it is queued, while still
  playing the current track (FiiO SR11, seen in Mandarin).
- Some ignore it and stop at the end of the track.

Arming waits until the renderer has been heard playing the current track,
and what the renderer is seen to do is learned per UDN (see ``quirks.py``).
"""

import asyncio
import logging
import time

from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState

from .client import SoapResult
from .didl import content_type_for, format_ms, proxy_key
from .ownership import OwnershipMixin
from .timing import (
    ARM_AFTER_PLAYING_SECONDS,
    ARM_AFTER_POSITION_MS,
    EARLY_SWITCH_BACK_MS,
    EARLY_SWITCH_REMAINING_MS,
    EARLY_SWITCH_TO_MS,
    SEEK_SETTLE_SECONDS,
)

logger = logging.getLogger(__name__)


class GaplessMixin(OwnershipMixin):
    """Next-track arming and transition detection."""

    @property
    def supports_gapless(self) -> bool:
        """Whether this backend supports gapless playback."""
        return self._gapless_supported

    @property
    def ready_for_next_track(self) -> bool:
        """Whether a next track may be armed now.

        Only once the renderer has been heard playing the current track:
        arming while it is still loading makes some renderers play the queued
        track instead.
        """
        return self._gapless_supported and (self._is_sonos or self._playing_confirmed)

    def _note_playing(self, position_ms: int) -> None:
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

    async def set_next_track(
        self, url: str, metadata: BackendTrackMetadata, queue_item_id: int = 0
    ) -> bool:
        """Prepare the next track for gapless transition."""
        if not self._client or not self.ready_for_next_track or not await self._owns_transport():
            return False

        content_type = content_type_for(url)
        actual_url = url
        size = None
        if self._proxy_server:
            key = f"{metadata.track_id}_{queue_item_id}"
            actual_url = self._proxy_server.register_track(
                track_id=metadata.track_id,
                qobuz_url=url,
                content_type=content_type,
                proxy_key=key,
            )
            logger.debug(f"Gapless: registered next track proxy URL: {actual_url}")
            # Not time-critical here (the current track is playing), so learn
            # the file size for the DIDL: renderers that cannot tell where a
            # stream ends may otherwise stay PLAYING at its end.
            size = await self._proxy_server.probe_size(key)
        self._forget_delivery(actual_url)

        didl = self._build_didl(actual_url, metadata, content_type, size)

        if self._is_sonos:
            return await self._arm_sonos(actual_url, metadata, didl)

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
                "gapless", False, f"SetNextAVTransportURI refused with error {result.error_code}"
            )
        else:
            logger.warning(
                "Gapless: failed to arm next track (transient error), will retry on next poll cycle"
            )
        return False

    async def _arm_sonos(self, url: str, metadata: BackendTrackMetadata, didl: str) -> bool:
        """Sonos gapless: append the next track to the device queue."""
        assert self._client
        # Already armed with this URL — appending again would queue a
        # duplicate entry and make the song play twice
        if self._next_track_proxy_url == url and self._next_track_queue_nr is not None:
            logger.debug("Gapless: next track already armed, skipping duplicate")
            return True
        queue_nr = await self._client.add_uri_to_queue(url, didl)
        if queue_nr is None:
            logger.warning("Gapless: failed to add next track to queue")
            return False
        self._next_track_proxy_url = url
        self._next_track_metadata = metadata
        self._next_track_queue_nr = queue_nr
        logger.info(f"Gapless: armed next track: {metadata.artist} - {metadata.title}")
        return True

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
        self._clear_next_track()

    async def _disarm_on_renderer(self) -> None:
        """Clear the renderer's NextURI with an empty SetNextAVTransportURI.

        Forgetting the armed track locally is not enough: the FiiO K17 keeps a
        queued URI through Stop and later tracks until it is cleared. Errors
        are ignored (renderers that refuse an empty NextURI have nothing to
        clear that matters).
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
    # Transition detection
    # =========================================================================

    async def _detect_transition(self) -> bool:
        """Follow the renderer onto the armed track; True if it moved on."""
        if not self._client or not self._next_track_proxy_url:
            return False
        current_uri = await self._read_source_uri()
        if (
            not current_uri
            or current_uri != self._next_track_proxy_url
            or not (self._is_sonos or await self._moved_on_to_next())
        ):
            return False
        logger.info("Gapless: transition detected — device moved to next track")
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
        # The armed entry is now the playing one: don't remove it from the queue.
        self._clear_next_track()
        # The renderer is playing the new track, even if the last poll caught it
        # in TRANSITIONING: record PLAYING so its own end is detected.
        self._notify_state_change(PlaybackState.PLAYING)
        self._notify_next_track_started()
        self._notify_position_update(0)
        return True

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
                format_ms(pos),
                format_ms(duration),
            )
            self._learn(
                "reports_next_early", True, "named the queued track as current before reaching it"
            )
        return False

    def _switched_early(self, furthest_before: int, pos: int) -> bool:
        """Whether the renderer started the armed track as soon as it got it.

        The current URI is unchanged (no transition was seen), yet the
        position fell back to the start of a track with plenty of the current
        one left: the queued track is playing under the current one's name.
        With the audio proxy in the path, the renderer must also have fetched
        the queued track — it cannot play what it never asked for, so a jump
        back without that (a seek from the renderer's own buttons) is not it.
        """
        if self._is_sonos or not self._next_track_proxy_url:
            return False
        # No start-of-track grace here: arming waits for the renderer to play
        # (ARM_AFTER_POSITION_MS), and a renderer that switches at once does so
        # inside the first seconds, where a grace period would hide the jump.
        if time.monotonic() - self._last_seek_at < SEEK_SETTLE_SECONDS:
            return False
        if (
            self._proxy_server is not None
            and proxy_key(self._next_track_proxy_url) not in self._requested_keys
        ):
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
            format_ms(resume_ms),
        )
        self._learn("gapless", False, "starts a queued track as soon as it is queued")
        self._clear_next_track()
        await self._disarm_on_renderer()
        if self._quirks.sources.get("gapless") == "user":
            logger.warning(
                "[%s] Gapless is forced on for this speaker; set it to auto or off "
                "to stop this renderer skipping ahead",
                self.name,
            )
            return
        if await self._reload_current_track(resume_ms):
            logger.info(
                "[%s] Current track reloaded at %s; gapless is off for this renderer",
                self.name,
                format_ms(self._position_ms),
            )
