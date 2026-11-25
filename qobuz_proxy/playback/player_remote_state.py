"""Player handling of the app's SET_STATE intents and external release."""

import logging
from typing import Awaitable, Callable, Optional, TYPE_CHECKING

from qobuz_proxy.backends import PlaybackState
from .player_transport import TransportMixin


logger = logging.getLogger("qobuz_proxy.playback.player")

# After a WebSocket reconnect, the Qobuz server replays its last-known session
# snapshot via SET_STATE — typically PAUSED at a position from before the drop.
# If the renderer is actually still playing further along the same track, treat
# that as a stale replay and ignore the pause/seek. This is the minimum gap
# (renderer ahead of server) at which we suppress.
_STALE_SNAPSHOT_THRESHOLD_MS = 5000


class RemoteStateMixin(TransportMixin):
    """Apply SET_STATE snapshots from the app atomically under the playback lock."""

    if TYPE_CHECKING:
        # Implemented by QobuzPlayer (player.py) with unplayable-track skipping.
        async def _begin_skip_wait_locked(self) -> None: ...

        async def _skip_past_unplayable_locked(self) -> bool: ...

    # =========================================================================
    # Remote Session Control
    # =========================================================================

    def invalidate_pending_commands(self) -> None:
        """Revoke queued/in-flight playback intents without interrupting current audio."""
        self._next_generation()
        self._clear_skip_pending()

    def set_playback_permission_check(self, check: Callable[[], Awaitable[bool]]) -> None:
        """Check renderer ownership before applying a remote session snapshot."""
        self._playback_permission_check = check

    async def release_external_playback(self) -> None:
        """Forget live playback without sending commands to another source."""
        self.invalidate_pending_commands()
        self._transition_generation += 1
        self._gapless_armed = False
        self._pending_next_track = None
        if self._clear_next_track_callback:
            self._clear_next_track_callback()
        self._state = PlaybackState.STOPPED
        self._set_position(0)
        self._report_paused()
        await self._report_stopped()

    def set_next_track_request_callback(
        self, callback: Callable[[Callable[[], bool]], Awaitable[bool]]
    ) -> None:
        """Wire the renderer NEXT action, with a guard checked immediately before sending."""
        self._request_next_track = callback

    async def apply_remote_state(
        self,
        *,
        track_id: Optional[str],
        queue_item_id: Optional[int],
        position_ms: Optional[int],
        playing_state: Optional[int],
        context_uuid: Optional[bytes] = None,
    ) -> None:
        """Apply a full SET_STATE intent from the app atomically.

        A SET_STATE is a multi-step intent (load this track, seek here, then
        play/pause/stop). Each SET_STATE message is dispatched as its own task,
        so if these steps were applied via separate locked methods they could
        interleave — an older SET_STATE could play a stale track after a newer
        one already queued a different load. Applying the whole sequence under a
        single lock acquisition and a single generation check makes the newest
        SET_STATE win as a unit, with no interleaving.

        Args:
            track_id: Target track id, or None if the message had no currentQueueItem.
            queue_item_id: Queue item id for the target track (if any).
            position_ms: Target position, or None if no currentPosition was sent.
            playing_state: Proto playing state (1=STOPPED, 2=PLAYING, 3=PAUSED),
                or None if the message had no playingState.
            context_uuid: Album/playlist context bytes for the target track, used
                for play reporting (listening history / scrobbles).
        """
        # A SET_STATE that only updates the queue version or next item carries
        # no playback intent. It must not supersede a track change still being
        # loaded: the server's reply to a state report that crossed a manual
        # skip is one of these, and taking a generation for it cancelled the
        # skip's pending play, leaving the renderer stopped (GitHub #33).
        if track_id is None and position_ms is None and playing_state is None:
            return
        # A replay of the failed current item is not a new playback intent.
        # In particular it must not invalidate a NEXT waiting for the send lock.
        pending = self._skip_pending_track
        if (
            self._request_next_track
            and pending is not None
            and playing_state not in (1, 3)
            and (
                track_id is None
                or (
                    track_id == pending.track_id
                    and (queue_item_id is None or queue_item_id == pending.queue_item_id)
                )
            )
        ):
            return
        # A bare "PLAYING" SET_STATE (no track), which the app sends right after
        # the one naming the track, must not supersede an older one that still has
        # a track change to apply: the track would never load and a quick second
        # skip would be silently lost. It queues behind it on the lock instead.
        # Stop/pause without a track are new intents and still supersede, and so
        # does a seek (a position): only the latest of several queued seeks runs.
        bare_play = track_id is None and playing_state == 2 and position_ms is None
        gen = self._command_generation if bare_play else self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("SET_STATE superseded by newer command; skipping")
                return

            if self._playback_permission_check and not await self._playback_permission_check():
                # Logged: a silent refusal looked like a dead speaker (#36).
                logger.info(
                    "SET_STATE ignored: the renderer is busy or playing another source; "
                    "select the speaker again in the Qobuz app to take it back"
                )
                return
            if gen != self._command_generation:
                return

            # Detect a stale session-restore snapshot (server replays an old
            # PAUSED position after a reconnect while we're still playing). Done
            # before any mutation so a replayed snapshot can't overwrite live
            # state (position or context) with its outdated values.
            stale = self._is_stale_pause_snapshot_locked(track_id, position_ms, playing_state)

            if (
                self._skip_pending_track is not None
                and playing_state in (1, 3)
                and (
                    playing_state == 1
                    or track_id is None
                    or (
                        track_id == self._skip_pending_track.track_id
                        and (
                            queue_item_id is None
                            or queue_item_id == self._skip_pending_track.queue_item_id
                        )
                    )
                )
            ):
                # A user stop/pause wins over a pending automatic skip, even
                # when the app sends it without a current queue item.
                self._clear_skip_pending()
                self._unavailable_skip_count = 0
                await self.backend.stop()
                self._state = PlaybackState.STOPPED if playing_state == 1 else PlaybackState.PAUSED
                await self._send_state_update()
                return

            if self._skip_pending_track is not None and track_id is None:
                logger.debug("Ignoring SET_STATE without a queue item while skipping a track")
                return

            # Load if a track is specified and differs from the loaded one.
            if track_id is not None:
                cur = self._current_track
                if (
                    cur is not None
                    and cur is self._skip_pending_track
                    and cur.track_id == track_id
                    and (queue_item_id is None or cur.queue_item_id == queue_item_id)
                ):
                    if self._request_next_track:
                        # NEXT is already in flight. An echo of the unavailable
                        # current item is not its acknowledgement: wait for a
                        # new current item instead of issuing a second skip.
                        return
                    # The server acknowledged the unplayable item we reported as
                    # current; its nextQueueItem (already stored by the command
                    # handler) is where playback continues.
                    await self._skip_past_unplayable_locked()
                    return
                if (
                    cur is None
                    or cur.track_id != track_id
                    or cur is self._skip_pending_track
                    or (playing_state == 2 and cur.streaming_url is None)
                ):
                    logger.info(f"Loading new track: {track_id}")
                    load_context = context_uuid
                    if (
                        load_context is None
                        and cur is not None
                        and cur.track_id == track_id
                        and (queue_item_id is None or cur.queue_item_id == queue_item_id)
                    ):
                        load_context = cur.context_uuid
                    if not await self._load_track_locked(
                        queue_item_id or 0,
                        track_id,
                        load_context,
                        for_playback=playing_state == 2,
                    ):
                        failed = self._current_track
                        if (
                            playing_state == 2
                            and failed is not None
                            and failed.streaming_url is None
                        ):
                            # Asked to play a track that cannot be fetched (e.g.
                            # "Not available" in this region): skip it as the
                            # official app does.
                            if gen != self._command_generation:
                                return
                            if self._request_next_track:
                                await self._begin_skip_wait_locked()
                            else:
                                await self._skip_past_unplayable_locked()
                        return
                    if gen != self._command_generation:
                        # A stop/next/other SET_STATE queued up while the URL
                        # and metadata were fetched (e.g. the server deactivated
                        # this renderer right after its join snapshot). Don't
                        # push this track to the backend only for the queued
                        # command to tear it down again.
                        logger.info(
                            f"SET_STATE superseded while loading track {track_id}; "
                            "not starting playback"
                        )
                        if self._state == PlaybackState.LOADING:
                            self._state = PlaybackState.STOPPED
                        return
                elif (
                    not stale
                    and queue_item_id
                    and cur.queue_item_id
                    and (cur.queue_item_id != queue_item_id)
                ):
                    # Same track but a different known queue occurrence id. Adopt
                    # it. Only split the play report when not currently playing:
                    # a paused/stopped track re-armed from a different slot is a
                    # distinct play, so end the prior report and let the
                    # subsequent play report fresh. While PLAYING the audio is
                    # continuous (e.g. a queue reorder reassigned the id), so it
                    # stays one listen — splitting it would double-scrobble.
                    cur.queue_item_id = queue_item_id
                    if context_uuid is not None:
                        cur.context_uuid = context_uuid
                    if self._state != PlaybackState.PLAYING:
                        await self._report_stopped()
                elif not stale:
                    # Same play. Fill in a late queue item id if we never had a
                    # real one, and adopt a changed/late context. Only overwrite
                    # context with a real value so a context-less SET_STATE can't
                    # wipe a known context.
                    if queue_item_id and not cur.queue_item_id:
                        cur.queue_item_id = queue_item_id
                    if context_uuid is not None and cur.context_uuid != context_uuid:
                        cur.context_uuid = context_uuid
                        # The play may already be active in the reporter (we
                        # return early from _play_locked while PLAYING), so
                        # re-sync its session or the end report keeps the old
                        # context.
                        if self._play_reporter:
                            self._play_reporter.update_context(
                                track_id=track_id,
                                context_uuid=self._format_context_uuid(context_uuid),
                            )

            # Position, then play/pause/stop — same order as the app expects.
            if position_ms is not None and not stale:
                await self.seek(position_ms)

            if playing_state is not None and not stale:
                if gen != self._command_generation:
                    logger.debug("SET_STATE superseded before applying playback state; skipping")
                    return
                # Proto: 1=STOPPED, 2=PLAYING, 3=PAUSED
                if playing_state == 2:
                    await self._play_locked(position_ms or 0)
                elif playing_state == 3:
                    await self._pause_locked()
                elif playing_state == 1:
                    await self._stop_playback_locked()

    def _is_stale_pause_snapshot_locked(
        self,
        track_id: Optional[str],
        position_ms: Optional[int],
        playing_state: Optional[int],
    ) -> bool:
        """Decide whether an inbound SET_STATE is a stale session-restore replay.

        Must be called while holding ``_playback_lock`` so the live player state
        it reads is consistent with the surrounding mutation. Returns True when
        ALL of:
          - server says PAUSED
          - renderer is still PLAYING
          - it's the same track the renderer is on
          - server position is more than _STALE_SNAPSHOT_THRESHOLD_MS behind the
            renderer's actual position
        """
        if playing_state != 3:
            return False
        if self._state != PlaybackState.PLAYING:
            return False
        if position_ms is None:
            return False
        # A different target track is a real command (track change), not a replay.
        cur = self._current_track
        if track_id is not None and (cur is None or cur.track_id != track_id):
            return False

        actual_pos = self.current_position_ms
        gap_ms = actual_pos - position_ms
        if gap_ms <= _STALE_SNAPSHOT_THRESHOLD_MS:
            return False

        logger.info(
            "Ignoring stale SET_STATE: server says PAUSED at %dms, renderer is "
            "PLAYING at %dms (%.1fs ahead) on same track — likely a session-"
            "restore replay after WebSocket reconnect; keeping playback.",
            position_ms,
            actual_pos,
            gap_ms / 1000.0,
        )
        return True
