"""
Local audio backend.

Downloads FLAC audio from Qobuz, decodes to float32 samples,
and plays through the local audio device via PortAudio.

See ``backend_base.py`` for how the backend is layered.
"""

import asyncio
import logging
from typing import Optional

import numpy as np

from qobuz_proxy.backends.types import (
    BackendInfo,
    BackendTrackMetadata,
    BufferStatus,
    PlaybackState,
)
from .backend_base import (
    BUFFER_HIGH_WATER,
    BUFFER_SECONDS,
    CHUNK_SIZE,
    buffer_status_for_level,
)
from .device import resolve_device
from .gapless import LocalGaplessMixin
from .ring_buffer import RingBuffer
from .stream import AudioOutputStream

logger = logging.getLogger(__name__)

# How long to keep waiting for the next track's download after the ring buffer
# has fully drained before abandoning the gapless transition.
NEXT_TRACK_GRACE_SECONDS = 10.0


class LocalAudioBackend(LocalGaplessMixin):
    """Local audio output backend using sounddevice/PortAudio."""

    async def play(self, url: str, metadata: BackendTrackMetadata) -> None:
        """Download FLAC, decode, and start playback."""
        await self._cancel_feeding()

        # A manual skip can use the download (or decode) already in progress.
        self._transition_pending = False
        next_meta = self._next_track_meta
        # Signed URLs may change, but a different requested audio format
        # must never reuse the old quality's bytes.
        reuse_next = (
            next_meta is not None
            and next_meta.track_id == metadata.track_id
            and (next_meta.sample_rate, next_meta.bit_depth)
            == (metadata.sample_rate, metadata.bit_depth)
        )
        if not reuse_next:
            await self.clear_next_track()

        # Silence the previous track immediately. Without this the callback
        # keeps draining up to BUFFER_SECONDS of old audio through the whole
        # download/decode, then cuts mid-note when the buffers are swapped.
        if self._ring_buffer:
            self._ring_buffer.clear()
            if self._stream:
                self._stream.pause()  # stream.start() below unpauses for the new track

        self._notify_state_change(PlaybackState.LOADING)

        try:
            next_audio = await self._take_next_track_audio() if reuse_next else None
            await self.clear_next_track()
            if next_audio is not None:
                audio_data, sample_rate = next_audio
                logger.info(f"Reusing prefetched audio for track {metadata.track_id}")
            else:
                audio_data, sample_rate = await self._download_and_decode(url)
            self._audio_data = audio_data
            self._sample_rate = sample_rate
            self._total_frames = len(audio_data)
            self._frames_fed = 0
            self._seek_target = None

            # Create ring buffer for this track's sample rate
            buffer_frames = int(sample_rate * BUFFER_SECONDS)
            channels = audio_data.shape[1] if audio_data.ndim > 1 else 1
            self._ring_buffer = RingBuffer(buffer_frames, channels)

            # Update stream's ring buffer and open/reconfigure
            stream = self._active_stream()
            stream.set_ring_buffer(self._ring_buffer)
            stream.open(sample_rate, channels)
            stream.start()

            # Start feeding loop
            self._feeding_task = asyncio.create_task(self._feeding_loop())
            self._notify_state_change(PlaybackState.PLAYING)

            logger.info(
                f"Playing: {metadata.artist} - {metadata.title} "
                f"({sample_rate}Hz, {self._total_frames} frames)"
            )

        except Exception as e:
            logger.error(f"Playback error: {e}")
            self._notify_state_change(PlaybackState.ERROR)
            self._notify_playback_error(str(e))

    async def _feeding_loop(self) -> None:
        """Feed decoded audio to the ring buffer, transitioning gaplessly into
        the prefetched next track when one is armed."""
        try:
            while True:
                await self._feed_current_track()

                if await self._transition_to_next_track():
                    continue

                # No next track. Drain the tail, watching for a transition that
                # completes mid-drain (next track shorter than the buffer) and
                # for a late arm from the player after that callback fires.
                while self._active_buffer().available() > 0:
                    if self._state == PlaybackState.STOPPED:
                        return
                    self._maybe_complete_transition()
                    if self._next_track_armed():
                        break
                    await asyncio.sleep(0.1)
                if self._next_track_armed():
                    continue
                self._maybe_complete_transition()

                # Track ended naturally
                self._notify_state_change(PlaybackState.STOPPED)
                self._notify_track_ended()
                return

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Feeding loop error: {e}")
            self._notify_state_change(PlaybackState.ERROR)
            self._notify_playback_error(str(e))

    async def _feed_current_track(self) -> None:
        """Feed the current track's remaining frames into the ring buffer."""
        while self._frames_fed < self._total_frames:
            # Handle seek
            if self._seek_target is not None:
                target = self._seek_target
                self._seek_target = None
                self._active_buffer().clear()
                self._frames_fed = min(target, self._total_frames)
                logger.debug(f"Seek applied: jumping to frame {self._frames_fed}")
                if self._frames_fed >= self._total_frames:
                    break
                # Notify position immediately after seek
                position_ms = int(self._frames_fed / self._sample_rate * 1000)
                self._notify_position_update(position_ms)
                continue

            self._maybe_complete_transition()

            # Pace: wait if buffer is full enough
            if self._active_buffer().fill_level() > BUFFER_HIGH_WATER:
                await asyncio.sleep(0.05)
                continue

            # Feed next chunk
            end = min(self._frames_fed + CHUNK_SIZE, self._total_frames)
            chunk = self._active_audio()[self._frames_fed : end]
            written = self._active_buffer().write(chunk)
            self._frames_fed += written

            # Check buffer health
            self._check_buffer_status()

            # Notify position update (with buffer latency correction)
            self._notify_position_update(self._playback_position_ms())

            await asyncio.sleep(0)  # Yield to event loop

    async def _cancel_feeding(self) -> None:
        """Cancel the current feeding task if running."""
        if self._feeding_task and not self._feeding_task.done():
            self._feeding_task.cancel()
            try:
                await self._feeding_task
            except asyncio.CancelledError:
                pass
        self._feeding_task = None

    async def _take_next_track_audio(self) -> Optional[tuple[np.ndarray, int]]:
        """Wait for the armed next track's download, then decode it.

        Returns None when no next track is armed, the prefetch was cancelled,
        the download stalled past the drained buffer's grace period, or the
        download/decode failed — the caller then falls back to the normal
        (gapped) track-end path.
        """
        loop = asyncio.get_running_loop()
        empty_since: Optional[float] = None

        # Poll instead of awaiting the task directly: clear_next_track()
        # cancelling the prefetch must not cancel the feeding loop with it,
        # and a re-arm mid-wait should be picked up seamlessly.
        while True:
            if self._next_decode_task is not None:
                # Cancelling the old feeder on a manual skip must not discard
                # a decode running in a worker thread. The new play awaits it.
                decode_task = self._next_decode_task
                try:
                    audio = await asyncio.shield(decode_task)
                except asyncio.CancelledError:
                    if decode_task.cancelled():
                        if self._next_decode_task is not decode_task:
                            continue  # clear_next_track() may have armed a replacement.
                        await self.clear_next_track()
                        return None
                    raise
                except Exception as e:
                    if self._next_decode_task is not decode_task:
                        continue
                    logger.warning(f"Gapless: next track decode failed: {e}")
                    await self.clear_next_track()
                    return None
                if self._next_decode_task is not decode_task:
                    continue  # A queue edit replaced this prefetch during decoding.
                self._next_prefetch_task = None
                return audio

            task = self._next_prefetch_task
            if task is None:
                return None

            if task.done():
                if task.cancelled():
                    await self.clear_next_track()
                    return None
                exc = task.exception()
                if exc is not None:
                    logger.warning(f"Gapless: next track download failed: {exc}")
                    await self.clear_next_track()
                    return None
                self._next_decode_task = asyncio.create_task(self._decode(task.result()))
                continue

            # Still downloading. The unplayed buffer tail is the time cushion;
            # give up shortly after it runs dry so playback doesn't hang silent.
            if self._ring_buffer is None or self._ring_buffer.available() == 0:
                now = loop.time()
                if empty_since is None:
                    empty_since = now
                elif now - empty_since > NEXT_TRACK_GRACE_SECONDS:
                    logger.warning(
                        "Gapless: next track still downloading after buffer "
                        "drained; falling back to normal track advance"
                    )
                    await self.clear_next_track()
                    return None
            else:
                empty_since = None

            await asyncio.sleep(0.05)

    async def _transition_to_next_track(self) -> bool:
        """Switch feeding to the prefetched next track, if one is armed.

        Returns True when the swap happened and feeding should continue.
        """
        if not self._next_track_armed():
            return False
        next_audio = await self._take_next_track_audio()
        if next_audio is None:
            return False

        old_buffer = self._ring_buffer
        if old_buffer is None:
            return False

        audio_data, sample_rate = next_audio
        channels = audio_data.shape[1] if audio_data.ndim > 1 else 1

        same_format = sample_rate == self._sample_rate and channels == old_buffer.channels

        if same_format:
            # Keep feeding the same ring buffer: the old track's tail drains
            # ahead of the new track's first frames — a true zero-gap.
            self._prev_total_frames = self._total_frames
            self._transition_pending = True
        else:
            # Format change: the stream must be reopened, so let the old track
            # finish draining first. The gap is just the reconfiguration time.
            logger.debug(
                f"Gapless: format change ({self._sample_rate}Hz/"
                f"{old_buffer.channels}ch -> {sample_rate}Hz/{channels}ch), "
                f"draining before reconfigure"
            )
            while old_buffer.available() > 0:
                if self._state == PlaybackState.STOPPED:
                    return False
                await asyncio.sleep(0.05)

            buffer_frames = int(sample_rate * BUFFER_SECONDS)
            self._ring_buffer = RingBuffer(buffer_frames, channels)
            self._sample_rate = sample_rate
            if self._stream:
                self._stream.set_ring_buffer(self._ring_buffer)
                self._stream.open(sample_rate, channels)
                self._stream.start()
            self._transition_pending = False
            self._forget_consumed_next_track()
            logger.info("Gapless: transitioned to next track (format change)")
            self._notify_next_track_started()

        self._audio_data = audio_data
        self._total_frames = len(audio_data)
        self._frames_fed = 0
        self._seek_target = None
        return True

    async def pause(self) -> None:
        if self._stream:
            self._stream.pause()
        self._notify_state_change(PlaybackState.PAUSED)

    async def resume(self) -> bool:
        # Resume only makes sense with a track loaded (paused mid-play). The
        # stream object survives stop(), so checking it alone would report a
        # phantom PLAYING state with nothing to play.
        if not self._stream or self._audio_data is None:
            return False
        self._stream.resume()
        self._notify_state_change(PlaybackState.PLAYING)
        return True

    async def stop(self, *, next_track_id: Optional[str] = None) -> None:
        await self._cancel_feeding()
        self._transition_pending = False
        if self._next_track_meta is None or self._next_track_meta.track_id != next_track_id:
            await self.clear_next_track()
        if self._ring_buffer:
            self._ring_buffer.clear()
        if self._stream:
            self._stream.stop()
        self._frames_fed = 0
        self._audio_data = None
        self._notify_state_change(PlaybackState.STOPPED)

    async def seek(self, position_ms: int) -> None:
        """Seek to position in current track."""
        if self._sample_rate == 0 or self._audio_data is None:
            return

        target_frame = int(position_ms / 1000 * self._sample_rate)

        # Edge case: seek beyond duration → trigger track end
        if target_frame >= self._total_frames:
            logger.debug(f"Seek beyond duration ({position_ms}ms), ending track")
            await self._cancel_feeding()
            if self._ring_buffer:
                self._ring_buffer.clear()
            self._frames_fed = self._total_frames
            self._notify_state_change(PlaybackState.STOPPED)
            self._notify_track_ended()
            return

        # Edge case: seek to negative → clamp to 0
        target_frame = max(0, target_frame)

        logger.debug(f"Seek to {position_ms}ms (frame {target_frame})")
        self._seek_target = target_frame

        # If no feeding loop is running (e.g., paused after track end),
        # update position directly
        if self._feeding_task is None or self._feeding_task.done():
            if self._ring_buffer:
                self._ring_buffer.clear()
            self._frames_fed = target_frame

    async def get_position(self) -> int:
        """Get current playback position accounting for buffer latency."""
        return self._playback_position_ms()

    async def set_volume(self, level: int) -> None:
        self._volume = max(0, min(100, level))
        if self._stream:
            self._stream.set_volume(level)

    async def get_volume(self) -> int:
        return self._volume

    async def get_state(self) -> PlaybackState:
        return self._state

    async def get_buffer_status(self) -> BufferStatus:
        """Get buffer health based on ring buffer fill level."""
        if not self._ring_buffer:
            return BufferStatus.OK

        return buffer_status_for_level(self._ring_buffer.fill_level())

    def _check_buffer_status(self) -> None:
        """Check and notify buffer status changes."""
        if not self._ring_buffer:
            return

        status = buffer_status_for_level(self._ring_buffer.fill_level())

        if status != self._last_buffer_status:
            self._last_buffer_status = status
            self._notify_buffer_status(status)

            if status == BufferStatus.EMPTY:
                logger.warning("Audio buffer underrun — audio may glitch")

    async def connect(self) -> bool:
        """Initialize connection — resolve device and create audio stream."""
        try:
            self._device_info = resolve_device(self._device_config)
            self.name = f"Local: {self._device_info.name}"

            # Create audio output stream (not opened until play)
            self._stream = AudioOutputStream(
                device_index=self._device_info.index,
                ring_buffer=RingBuffer(1, 2),  # Placeholder, replaced per-track
                blocksize=self._buffer_size,
            )

            self._is_connected = True
            logger.info(
                f"Audio output device: {self._device_info.name} "
                f"({int(self._device_info.default_samplerate)} Hz, "
                f"{self._device_info.channels}ch)"
            )
            return True
        except (ValueError, ImportError) as e:
            logger.error(f"Failed to initialize audio device: {e}")
            return False

    async def disconnect(self) -> None:
        await self.stop()
        if self._stream:
            self._stream.close()
            self._stream = None
        self._is_connected = False

    def get_info(self) -> BackendInfo:
        return BackendInfo(
            backend_type="local",
            name=self.name,
            device_id=f"local-{self._device_config}",
        )
