"""
Shared state and helpers for the local audio backend.

``LocalAudioBackend`` is built in layers, each a class in its own module that
adds one concern on top of the one below:

    backend_base  state, download and decode, position and buffer helpers
    gapless       arming and clearing the next-track prefetch
    backend       play, the feeding loop and next-track switch, controls, connect
"""

import asyncio
import io
import logging
from typing import Optional

import aiohttp
import numpy as np

from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.backends.types import BackendTrackMetadata, BufferStatus

from .device import AudioDeviceInfo
from .ring_buffer import RingBuffer
from .stream import AudioOutputStream

logger = logging.getLogger(__name__)

CHUNK_SIZE = 8192  # Frames per feed iteration
BUFFER_SECONDS = 10  # Ring buffer capacity in seconds
BUFFER_HIGH_WATER = 0.8  # Pause feeding when buffer above this level


def buffer_status_for_level(level: float) -> BufferStatus:
    """Classify a ring buffer fill level (0.0 to 1.0) as a buffer health status."""
    if level == 0.0:
        return BufferStatus.EMPTY
    elif level < 0.10:
        return BufferStatus.LOW
    elif level >= 1.0:
        return BufferStatus.FULL
    else:
        return BufferStatus.OK


class LocalBackendBase(AudioBackend):
    """State shared by every layer of the local audio backend."""

    def __init__(
        self,
        device: str = "default",
        buffer_size: int = 2048,
        name: str = "Local Audio",
    ):
        super().__init__(name)
        self._device_config = device
        self._buffer_size = buffer_size

        # Device and audio components (initialized in connect())
        self._device_info: Optional[AudioDeviceInfo] = None
        self._ring_buffer: Optional[RingBuffer] = None
        self._stream: Optional[AudioOutputStream] = None

        # Playback state
        self._audio_data: Optional[np.ndarray] = None
        self._sample_rate: int = 0
        self._frames_fed: int = 0
        self._total_frames: int = 0
        self._feeding_task: Optional[asyncio.Task] = None

        # Seek support
        self._seek_target: Optional[int] = None

        # Buffer status tracking
        self._last_buffer_status: BufferStatus = BufferStatus.OK

        # Gapless: prefetched next track (compressed bytes; decoded at transition)
        self._next_track_meta: Optional[BackendTrackMetadata] = None
        self._next_prefetch_task: Optional["asyncio.Task[bytes]"] = None
        self._next_decode_task: Optional["asyncio.Task[tuple[np.ndarray, int]]"] = None

        # Gapless: audio data has been swapped to the next track but the old
        # track's tail is still draining from the ring buffer
        self._transition_pending: bool = False
        self._prev_total_frames: int = 0

    def _active_stream(self) -> AudioOutputStream:
        """The audio output stream; it exists once connect() has succeeded."""
        if self._stream is None:
            raise RuntimeError("Audio output stream is not initialized (connect() first)")
        return self._stream

    def _active_buffer(self) -> RingBuffer:
        """The ring buffer of the track being fed; play() creates it."""
        if self._ring_buffer is None:
            raise RuntimeError("No ring buffer: no track has been loaded")
        return self._ring_buffer

    def _active_audio(self) -> np.ndarray:
        """Decoded samples of the track being fed; play() loads them."""
        if self._audio_data is None:
            raise RuntimeError("No audio data: no track has been loaded")
        return self._audio_data

    async def _download(self, url: str) -> bytes:
        """Download the audio file bytes."""
        logger.debug("Downloading audio from URL...")
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.read()

        logger.debug(f"Downloaded {len(data)} bytes")
        return data

    async def _decode(self, data: bytes) -> tuple[np.ndarray, int]:
        """Decode audio bytes to a float32 numpy array in a worker thread.

        Decoding a full track blocks long enough to starve the feeding loop
        (and thus the ring buffer) if run on the event loop.
        """
        import soundfile as sf  # type: ignore[import-untyped]

        audio_data, sample_rate = await asyncio.to_thread(
            sf.read, io.BytesIO(data), dtype="float32"
        )

        # Ensure 2D array (frames, channels)
        if audio_data.ndim == 1:
            audio_data = audio_data.reshape(-1, 1)

        logger.debug(f"Decoded: {len(audio_data)} frames, {audio_data.shape[1]}ch, {sample_rate}Hz")
        return audio_data, sample_rate

    async def _download_and_decode(self, url: str) -> tuple[np.ndarray, int]:
        """Download audio file and decode to float32 numpy array."""
        return await self._decode(await self._download(url))

    def _playback_position_ms(self) -> int:
        """Current position accounting for buffer latency and, mid-transition,
        for the old track's tail still draining from the buffer."""
        if self._sample_rate == 0:
            return 0

        available = self._ring_buffer.available() if self._ring_buffer else 0

        if self._transition_pending and available > self._frames_fed:
            # Buffer still holds old-track frames: report the old track's position
            old_remaining = available - self._frames_fed
            position_frames = max(0, self._prev_total_frames - old_remaining)
        else:
            position_frames = max(0, self._frames_fed - available)

        return int(position_frames / self._sample_rate * 1000)
