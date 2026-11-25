"""
Automatic quality selection for a speaker configured with ``max_quality: auto``.
"""

from dataclasses import dataclass

from qobuz_proxy.backends import AudioBackend
from qobuz_proxy.backends.dlna import DLNABackend
from qobuz_proxy.config import AUTO_FALLBACK_QUALITY

# Display names for Qobuz quality IDs, used when explaining auto-detection
_QUALITY_NAMES = {
    5: "MP3",
    6: "CD (FLAC 16/44)",
    7: "Hi-Res (24/96)",
    27: "Hi-Res (24/192)",
}


@dataclass(frozen=True)
class AutoQuality:
    """Outcome of auto quality resolution.

    Attributes:
        quality: Effective Qobuz quality ID.
        source: "auto" (detected from the device) or "auto_fallback" (the
            device never said, so a conservative quality was chosen).
        explanation: Human-readable reason, logged by the speaker.
    """

    quality: int
    source: str
    explanation: str


def resolve_auto_quality(backend: AudioBackend) -> AutoQuality:
    """Pick the effective quality for ``max_quality: auto`` from the backend."""
    if not isinstance(backend, DLNABackend):
        # Local backend: default to Hi-Res 192k
        return AutoQuality(27, "auto", "Local backend, using max quality: Hi-Res (24/192)")

    recommended = backend.get_recommended_quality()
    if not recommended:
        return AutoQuality(
            AUTO_FALLBACK_QUALITY,
            "auto_fallback",
            "Capability discovery unavailable, using fallback quality: CD (FLAC 16/44)",
        )

    name = _QUALITY_NAMES.get(recommended, recommended)
    if backend.quality_detection_confirmed:
        return AutoQuality(recommended, "auto", f"Auto-detected max quality: {name}")
    return AutoQuality(
        recommended,
        "auto_fallback",
        f"Device did not report its supported formats; using conservative quality: "
        f"{name}. Set max_quality manually if the device supports hi-res.",
    )
