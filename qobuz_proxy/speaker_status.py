"""
Speaker status reporting.

Builds the status payload the web UI and API show for one speaker: playback
state, the track now playing and the effective configuration.
"""

from typing import Optional

from qobuz_proxy.backends import AudioBackend, PlaybackState
from qobuz_proxy.backends.dlna import DLNABackend
from qobuz_proxy.config import AUTO_QUALITY, SpeakerConfig, slugify_name
from qobuz_proxy.playback import QobuzPlayer


def build_speaker_status(
    config: SpeakerConfig,
    *,
    is_running: bool,
    player: Optional[QobuzPlayer],
    backend: Optional[AudioBackend],
    effective_quality: int,
    quality_source: str,
) -> dict:
    """Return the rich status dict for one speaker."""
    # Determine playback status
    if not is_running:
        playback_status = "disconnected"
    elif player and player.state == PlaybackState.PLAYING:
        playback_status = "playing"
    elif player and player.state == PlaybackState.PAUSED:
        playback_status = "paused"
    else:
        playback_status = "idle"

    # Build now_playing if there's a current track with metadata
    now_playing = None
    if player and player.current_track and playback_status in ("playing", "paused"):
        track = player.current_track
        meta = track.metadata
        now_playing = {
            "title": meta.get("title", ""),
            "artist": meta.get("artist", ""),
            "album": meta.get("album", ""),
            "album_art_url": meta.get("artwork_url", ""),
            "quality": meta.get("quality_name", ""),
        }
        # With fixed volume the proxy never touches or tracks the output
        # level, so the cached value is just the initial default — omit it.
        if not config.fixed_volume:
            now_playing["volume"] = player._volume

    # Build config section
    config_dict: dict = {
        "max_quality": ("auto" if config.max_quality == AUTO_QUALITY else config.max_quality),
        "effective_quality": effective_quality,
        "quality_source": quality_source,
        "device_type": config.device_type,
    }
    if config.backend_type == "dlna":
        config_dict["dlna_ip"] = config.dlna_ip
        config_dict["dlna_port"] = config.dlna_port
        config_dict["description_url"] = config.dlna_description_url
        config_dict["fixed_volume"] = config.dlna_fixed_volume
        config_dict["gapless"] = config.dlna_gapless
        if isinstance(backend, DLNABackend):
            # What the renderer is assumed or known to do, and why
            # (profile / learned / user): shown so users can see why
            # gapless is on or off for a speaker.
            config_dict["renderer_behaviour"] = backend.quirks.describe()
    elif config.backend_type == "local":
        config_dict["audio_device"] = config.audio_device
        config_dict["buffer_size"] = config.audio_buffer_size
        config_dict["fixed_volume"] = config.audio_fixed_volume

    return {
        "id": slugify_name(config.name),
        "name": config.name,
        "backend": config.backend_type,
        "status": playback_status,
        "config": config_dict,
        "now_playing": now_playing,
    }
