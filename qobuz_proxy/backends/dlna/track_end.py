"""
Deciding when a track has ended on the renderer.

One track end is reported per playback attempt (GitHub #52): once an end was
reported for the current URL — a stop, a stall at the end, or a stream the
proxy could not deliver — later reports for the same URL are ignored until a
new track is loaded.
"""

import logging
import time

from qobuz_proxy.backends.types import PlaybackState

from .didl import format_ms, proxy_key
from .gapless import GaplessMixin
from .timing import (
    EARLY_STOP_MARGIN_MS,
    END_STALL_DELIVERED_SECONDS,
    END_STALL_KNOWN_SECONDS,
    END_STALL_SECONDS,
    END_WINDOW_MS,
    IGNORED_NEXT_ARMED_FOR_SECONDS,
    PAST_END_MS,
    PLAYBACK_START_GRACE_PERIOD_SECONDS,
    SEEK_SETTLE_SECONDS,
    STALL_TOLERANCE_MS,
)

logger = logging.getLogger(__name__)


class TrackEndMixin(GaplessMixin):
    """Track-end decisions for the poll loop."""

    @property
    def _end_already_reported(self) -> bool:
        return (
            self._end_handled_for is not None and self._end_handled_for == self._current_proxy_url
        )

    def _report_track_end(self) -> None:
        """Report the end of the current track, once per playback attempt."""
        if self._end_already_reported:
            return
        self._end_handled_for = self._current_proxy_url
        self._stall_position_ms = -1
        # Mark stopped so the renderer's own stop, if it comes, is not a second end.
        self._state = PlaybackState.STOPPED
        self._notify_track_ended()

    def _skip_undeliverable(self) -> bool:
        """Skip a track the proxy could not get any audio for; True if skipped.

        Some renderers then sit in PLAYING or TRANSITIONING forever, so don't
        wait for a stop that may never come.
        """
        key = proxy_key(self._current_proxy_url or "")
        if self._delivery_failures.get(key) is not True:
            return False
        del self._delivery_failures[key]
        logger.warning(
            "[%s] Could not stream the current track to the renderer; skipping to the next track",
            self.name,
        )
        self._report_track_end()
        return True

    def _handle_stop(self) -> None:
        """The renderer went from playing to STOPPED: a track end or an interruption."""
        # If gapless was armed but the device stopped, clear and fall through.
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
        # Armed well before the end and not taken: the renderer ignores
        # SetNextAVTransportURI.
        if not early_stop and armed_for >= IGNORED_NEXT_ARMED_FOR_SECONDS:
            self._learn("gapless", False, "stopped at the end of a track with the next one queued")

        if early_stop:
            logger.info(
                "[%s] Renderer stopped at %s of %s without a Qobuz command; "
                "not a track end, so not advancing",
                self.name,
                format_ms(self._furthest_position_ms),
                format_ms(self._duration_ms),
            )
            self._notify_playback_interrupted(self._furthest_position_ms)
        else:
            self._end_handled_for = self._current_proxy_url
            self._notify_track_ended()

    def _stopped_before_track_end(self) -> bool:
        """Whether a stop landed well before the end of a track that streamed fine.

        A stream the proxy cut short ends early on the renderer too; that is
        still the end of what could be played, so it counts as a track end.
        """
        if self._current_proxy_url and (
            self._delivery_failures.pop(proxy_key(self._current_proxy_url), None) is not None
        ):
            return False
        # Unknown duration or a renderer that never reported a position: keep
        # treating the stop as a track end rather than stall the queue.
        return (
            self._duration_ms > 0
            and self._furthest_position_ms > 0
            and self._duration_ms - self._furthest_position_ms > EARLY_STOP_MARGIN_MS
        )

    def _ended_without_stop(self, pos: int) -> bool:
        """Whether a renderer still reporting PLAYING has finished the track.

        Some renderers (FiiO K17) never report STOPPED at the end of a track:
        they stay PLAYING with the position parked at the track's length.
        Never judged while paused, during the start grace period or just
        after a seek, and only against Qobuz's duration for the track.
        """
        duration = self._duration_ms
        now = time.monotonic()
        if (
            duration <= 0
            or self._starting_playback
            or self._end_already_reported
            or now - self._playback_started_at < PLAYBACK_START_GRACE_PERIOD_SECONDS
            or now - self._last_seek_at < SEEK_SETTLE_SECONDS
        ):
            return False
        if self._stall_position_ms < 0 or abs(pos - self._stall_position_ms) > STALL_TOLERANCE_MS:
            self._stall_position_ms = pos
            self._stall_since = now
        if pos >= duration + PAST_END_MS:
            return True
        if pos < duration - END_WINDOW_MS:
            return False
        if self._next_track_proxy_url:
            # A renderer about to move on to the armed track may hold at the
            # end for a moment; give it the full time before stepping in.
            needed = END_STALL_SECONDS
        elif proxy_key(self._current_proxy_url or "") in self._delivered_keys:
            needed = END_STALL_DELIVERED_SECONDS
        elif self._quirks.stops_at_end is False:
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
            format_ms(pos),
            format_ms(self._duration_ms),
        )
        self._learn("stops_at_end", False, "stayed PLAYING at the end of a track")
        if (
            self._next_track_proxy_url
            and not self._is_sonos
            and time.monotonic() - self._next_armed_at >= IGNORED_NEXT_ARMED_FOR_SECONDS
        ):
            self._learn("gapless", False, "stayed on a finished track with the next one queued")
        self._next_track_proxy_url = None
        self._next_track_metadata = None
        self._report_track_end()
