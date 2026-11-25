"""
Configuration data model.

Constants, the ``Config`` dataclass tree, per-speaker ``SpeakerConfig`` and
the small value parsers shared by the YAML and environment loaders.
"""

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Valid quality values (0 = auto-detect from device capabilities)
AUTO_QUALITY = 0
AUTO_FALLBACK_QUALITY = 6  # CD quality fallback when auto-detection fails
VALID_QUALITIES = {0, 5, 6, 7, 27}

# Default ports
DEFAULT_HTTP_PORT = 8689
DEFAULT_PROXY_PORT = 7120

# Namespace UUID for deterministic speaker UUID generation
_SPEAKER_UUID_NAMESPACE = uuid.UUID("f47ac10b-58cc-4372-a567-0e02b2c3d479")

# Qobuz Connect device types (DeviceType enum in protos/qconnect_common.proto).
# The type selects the icon the Qobuz app shows for the device. The enum values
# come from StreamCore32 (https://github.com/tobiasguyer/StreamCore32); qonductor
# (https://github.com/nickblt/qonductor, Rust) uses the same values. The names are
# the ones the Qobuz web player uses, as documented by qobuz-connect
# (https://github.com/ciaens/qobuz-connect).
DEFAULT_DEVICE_TYPE = "speaker"
DEVICE_TYPES = {
    "speaker": 1,
    "streamer": 2,
    "tv": 3,
    "soundbar": 4,
    "computer": 5,
    "mobile": 6,
    "cast": 7,
    "headphones": 8,
    "tablet": 9,
}

# Valid log levels
VALID_LOG_LEVELS = {"debug", "info", "warning", "error"}


class ConfigError(Exception):
    """Configuration error."""

    pass


@dataclass
class QobuzConfig:
    """Qobuz account configuration."""

    email: str = ""
    auth_token: str = ""
    user_id: str = ""
    max_quality: int = 27  # 5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k


@dataclass
class DeviceConfig:
    """Device identification configuration."""

    name: str = "QobuzProxy"
    uuid: str = ""  # Auto-generated if empty
    device_type: str = DEFAULT_DEVICE_TYPE  # Key of DEVICE_TYPES

    def __post_init__(self) -> None:
        if not self.uuid:
            self.uuid = str(uuid.uuid4())


GAPLESS_AUTO = "auto"
GAPLESS_ON = "on"
GAPLESS_OFF = "off"
GAPLESS_MODES = (GAPLESS_AUTO, GAPLESS_ON, GAPLESS_OFF)


def parse_gapless_mode(value: object) -> str:
    """Normalise a gapless setting to "auto", "on" or "off".

    YAML booleans are accepted (``gapless: false`` means "off"); anything
    unrecognised means "auto".
    """
    if isinstance(value, bool):
        return GAPLESS_ON if value else GAPLESS_OFF
    text = str(value or "").strip().lower()
    if text in ("on", "true", "yes", "1", "enabled"):
        return GAPLESS_ON
    if text in ("off", "false", "no", "0", "disabled"):
        return GAPLESS_OFF
    return GAPLESS_AUTO


@dataclass
class DLNAConfig:
    """DLNA backend configuration."""

    ip: str = ""
    port: int = 1400
    fixed_volume: bool = False
    proxy_port: int = 7120
    description_url: str = ""  # Full URL to UPnP device description XML (auto-discovered via SSDP)
    # Gapless via SetNextAVTransportURI: "auto" (device profile and learned
    # behaviour decide), "on" or "off".
    gapless: str = "auto"
    # The renderer's UPnP UDN: finds it again by SSDP if its IP address changes.
    udn: str = ""


@dataclass
class LocalConfig:
    """Local audio backend configuration."""

    device: str = "default"  # Device name, index, or "default"
    buffer_size: int = 2048  # Audio buffer size in frames
    # Keep output at 100% and ignore volume commands (volume set downstream).
    fixed_volume: bool = False


@dataclass
class BackendConfig:
    """Audio backend configuration."""

    type: str = "dlna"
    dlna: DLNAConfig = field(default_factory=DLNAConfig)
    local: LocalConfig = field(default_factory=LocalConfig)


@dataclass
class ServerConfig:
    """Server configuration."""

    http_port: int = 8689
    bind_address: str = "0.0.0.0"


@dataclass
class LoggingConfig:
    """Logging configuration."""

    level: str = "info"


@dataclass
class SpeakerConfig:
    """Per-speaker configuration for multi-speaker mode."""

    name: str = "QobuzProxy"
    uuid: str = ""
    device_type: str = DEFAULT_DEVICE_TYPE
    backend_type: str = "dlna"
    max_quality: int = 27
    http_port: int = 0  # 0 = auto-assign
    bind_address: str = "0.0.0.0"
    dlna_ip: str = ""
    dlna_port: int = 1400
    dlna_fixed_volume: bool = False
    dlna_description_url: str = ""
    dlna_gapless: str = "auto"  # "auto", "on" or "off"
    dlna_udn: str = ""  # Renderer UDN, learned on first connect
    proxy_port: int = 0  # 0 = auto-assign
    audio_device: str = "default"
    audio_buffer_size: int = 2048
    audio_fixed_volume: bool = False

    @property
    def fixed_volume(self) -> bool:
        """Whether volume commands are ignored for this speaker's backend."""
        if self.backend_type == "dlna":
            return self.dlna_fixed_volume
        return self.audio_fixed_volume


@dataclass
class Config:
    """Complete QobuzProxy configuration."""

    qobuz: QobuzConfig = field(default_factory=QobuzConfig)
    device: DeviceConfig = field(default_factory=DeviceConfig)
    backend: BackendConfig = field(default_factory=BackendConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    speakers: list[SpeakerConfig] = field(default_factory=list)
    config_path: Optional[Path] = None  # Set by load_config() when loading from file


def parse_device_type(value: Any) -> str:
    """Normalize a device type name (e.g. "Headphones" -> "headphones").

    Raises:
        ConfigError: If the value is not a known device type.
    """
    device_type = str(value).strip().lower() if value is not None else ""
    if not device_type:
        return DEFAULT_DEVICE_TYPE
    if device_type not in DEVICE_TYPES:
        raise ConfigError(
            f"Invalid device_type: {value!r}. Valid values: {', '.join(DEVICE_TYPES)}"
        )
    return device_type


def _parse_bool(value: Any) -> bool:
    """A YAML or environment boolean ("true", "1", "yes", "on")."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _parse_quality_value(value: Any) -> int:
    """Parse a quality value, handling 'auto' string -> AUTO_QUALITY (0)."""
    if isinstance(value, str) and value.lower() == "auto":
        return AUTO_QUALITY
    return int(value)
