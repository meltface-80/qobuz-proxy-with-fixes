"""
Speaker configuration building.

Builds the list of ``SpeakerConfig`` objects from YAML, comma-separated
environment variables or the flat single-speaker config, then assigns ports,
generates stable UUIDs and validates the result.
"""

import os
import platform
import re
import uuid
from typing import Optional

from qobuz_proxy.config_models import (
    AUTO_QUALITY,
    DEFAULT_DEVICE_TYPE,
    DEFAULT_HTTP_PORT,
    DEFAULT_PROXY_PORT,
    BackendConfig,
    Config,
    ConfigError,
    DeviceConfig,
    DLNAConfig,
    LocalConfig,
    LoggingConfig,
    QobuzConfig,
    ServerConfig,
    SpeakerConfig,
    _parse_bool,
    _parse_quality_value,
    parse_device_type,
    parse_gapless_mode,
)
from qobuz_proxy.config_validation import _validate_speakers

# Namespace UUID for deterministic speaker UUID generation
_SPEAKER_UUID_NAMESPACE = uuid.UUID("f47ac10b-58cc-4372-a567-0e02b2c3d479")


def generate_speaker_uuid(speaker_name: str) -> str:
    """Generate a deterministic UUID for a speaker based on hostname and name."""
    return str(uuid.uuid5(_SPEAKER_UUID_NAMESPACE, f"{platform.node()}:{speaker_name}"))


def slugify_name(name: str) -> str:
    """Convert a speaker name to a URL-safe slug."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def speaker_config_to_dict(sc: SpeakerConfig) -> dict:
    """Convert a SpeakerConfig to a YAML-serializable dict.

    Ports are omitted so they can be re-assigned on next startup. The Qobuz
    Connect device UUID is persisted so the same speaker identity survives
    restarts (including Docker hostname changes).
    """
    d: dict = {
        "name": sc.name,
        "backend": sc.backend_type,
        "max_quality": "auto" if sc.max_quality == AUTO_QUALITY else sc.max_quality,
    }
    if sc.uuid:
        d["uuid"] = sc.uuid
    if sc.device_type != DEFAULT_DEVICE_TYPE:
        d["device_type"] = sc.device_type
    if sc.backend_type == "dlna":
        d["dlna_ip"] = sc.dlna_ip
        d["dlna_port"] = sc.dlna_port
        d["dlna_fixed_volume"] = sc.dlna_fixed_volume
        if sc.dlna_description_url:
            d["dlna_description_url"] = sc.dlna_description_url
        if sc.dlna_gapless != "auto":
            d["dlna_gapless"] = sc.dlna_gapless
        if sc.dlna_udn:
            d["dlna_udn"] = sc.dlna_udn
    elif sc.backend_type == "local":
        d["audio_device"] = sc.audio_device
        d["audio_buffer_size"] = sc.audio_buffer_size
        if sc.audio_fixed_volume:
            d["audio_fixed_volume"] = True
    return d


def _single_speaker_from_config(config: Config) -> SpeakerConfig:
    """Map flat Config fields to a single SpeakerConfig."""
    return SpeakerConfig(
        name=config.device.name,
        uuid=config.device.uuid,
        device_type=config.device.device_type,
        backend_type=config.backend.type,
        max_quality=config.qobuz.max_quality,
        http_port=0,  # auto-assigned by _assign_ports (avoids conflict with web UI port)
        bind_address=config.server.bind_address,
        dlna_ip=config.backend.dlna.ip,
        dlna_port=config.backend.dlna.port,
        dlna_fixed_volume=config.backend.dlna.fixed_volume,
        dlna_description_url=config.backend.dlna.description_url,
        dlna_gapless=parse_gapless_mode(config.backend.dlna.gapless),
        dlna_udn=config.backend.dlna.udn,
        proxy_port=config.backend.dlna.proxy_port,
        audio_device=config.backend.local.device,
        audio_buffer_size=config.backend.local.buffer_size,
        audio_fixed_volume=config.backend.local.fixed_volume,
    )


def speaker_component_config(sc: SpeakerConfig) -> Config:
    """
    Synthesize a Config object from a single SpeakerConfig.

    Existing components (DiscoveryService, WsManager, BackendFactory) all
    accept a Config, so SpeakerConfig fields are mapped into one to keep those
    components unchanged.
    """
    return Config(
        # Qobuz config carries max_quality only; credentials are on api_client
        qobuz=QobuzConfig(
            max_quality=sc.max_quality,
        ),
        device=DeviceConfig(
            name=sc.name,
            uuid=sc.uuid,
            device_type=sc.device_type,
        ),
        backend=BackendConfig(
            type=sc.backend_type,
            dlna=DLNAConfig(
                ip=sc.dlna_ip,
                port=sc.dlna_port,
                fixed_volume=sc.dlna_fixed_volume,
                proxy_port=sc.proxy_port,
                description_url=sc.dlna_description_url,
                gapless=sc.dlna_gapless,
                udn=sc.dlna_udn,
            ),
            local=LocalConfig(
                device=sc.audio_device,
                buffer_size=sc.audio_buffer_size,
                fixed_volume=sc.audio_fixed_volume,
            ),
        ),
        server=ServerConfig(
            http_port=sc.http_port,
            bind_address=sc.bind_address,
        ),
        logging=LoggingConfig(),
    )


def _assign_ports(speakers: list[SpeakerConfig], webui_port: int = DEFAULT_HTTP_PORT) -> None:
    """Auto-assign http_port and proxy_port to speakers that have 0 (auto)."""
    used_http: set[int] = {s.http_port for s in speakers if s.http_port != 0}
    # Reserve the web UI port so speakers don't collide with it
    used_http.add(webui_port)
    used_proxy: set[int] = {s.proxy_port for s in speakers if s.proxy_port != 0}

    next_http = DEFAULT_HTTP_PORT
    next_proxy = DEFAULT_PROXY_PORT

    for speaker in speakers:
        if speaker.http_port == 0:
            while next_http in used_http:
                next_http += 1
            speaker.http_port = next_http
            used_http.add(next_http)
            next_http += 1

        if speaker.backend_type == "dlna" and speaker.proxy_port == 0:
            while next_proxy in used_proxy:
                next_proxy += 1
            speaker.proxy_port = next_proxy
            used_proxy.add(next_proxy)
            next_proxy += 1


def _generate_uuids(speakers: list[SpeakerConfig]) -> None:
    """Generate deterministic UUIDs for speakers that have an empty uuid."""
    for speaker in speakers:
        if not speaker.uuid:
            speaker.uuid = generate_speaker_uuid(speaker.name)


def _parse_yaml_speakers(raw_speakers: list[dict], config: Config) -> list[SpeakerConfig]:
    """Parse a list of raw YAML dicts into SpeakerConfig objects."""
    speakers = []
    for raw in raw_speakers:
        speaker = SpeakerConfig(
            name=raw.get("name", "QobuzProxy"),
            uuid=raw.get("uuid", ""),
            device_type=parse_device_type(raw.get("device_type", DEFAULT_DEVICE_TYPE)),
            backend_type=raw.get("backend", "dlna"),
            max_quality=_parse_quality_value(raw.get("max_quality", 27)),
            http_port=int(raw.get("http_port", 0)),
            bind_address=raw.get("bind_address", config.server.bind_address),
            dlna_ip=raw.get("dlna_ip", ""),
            dlna_port=int(raw.get("dlna_port", 1400)),
            dlna_fixed_volume=bool(raw.get("dlna_fixed_volume", False)),
            dlna_description_url=raw.get("dlna_description_url", ""),
            dlna_gapless=parse_gapless_mode(raw.get("dlna_gapless", "auto")),
            dlna_udn=str(raw.get("dlna_udn", "") or ""),
            proxy_port=int(raw.get("proxy_port", 0)),
            audio_device=raw.get("audio_device", "default"),
            audio_buffer_size=int(raw.get("audio_buffer_size", 2048)),
            audio_fixed_volume=_parse_bool(raw.get("audio_fixed_volume", False)),
        )
        speakers.append(speaker)
    return speakers


def _split_env(var: str) -> list[str]:
    """Split a comma-separated environment variable value into a list of strings."""
    value = os.environ.get(var, "")
    if not value:
        return []
    return [v.strip() for v in value.split(",")]


def _split_env_padded(var: str, count: int, default: str) -> list[str]:
    """
    Split env var, broadcasting a single value to count items.

    Raises:
        ConfigError: If length is not 1 and not count.
    """
    values = _split_env(var)
    if not values:
        return [default] * count
    if len(values) == 1:
        return values * count
    if len(values) != count:
        raise ConfigError(f"Env var {var} has {len(values)} values but expected 1 or {count}")
    return values


def _parse_env_speakers(config: Config) -> list[SpeakerConfig]:
    """Parse multi-speaker configuration from environment variables."""
    names = _split_env("QOBUZPROXY_DEVICE_NAME")
    if not names:
        return []

    count = len(names)

    backend_types = _split_env_padded("QOBUZPROXY_BACKEND", count, "dlna")
    device_types = _split_env_padded("QOBUZPROXY_DEVICE_TYPE", count, DEFAULT_DEVICE_TYPE)
    dlna_ips = _split_env_padded("QOBUZPROXY_DLNA_IP", count, "")
    dlna_ports_raw = _split_env_padded("QOBUZPROXY_DLNA_PORT", count, "1400")
    dlna_fixed_volumes_raw = _split_env_padded("QOBUZPROXY_DLNA_FIXED_VOLUME", count, "false")
    dlna_gapless_raw = _split_env_padded("QOBUZPROXY_DLNA_GAPLESS", count, "auto")
    http_ports_raw = _split_env_padded("QOBUZPROXY_HTTP_PORT", count, "0")
    proxy_ports_raw = _split_env_padded("QOBUZPROXY_PROXY_PORT", count, "0")
    audio_devices = _split_env_padded("QOBUZPROXY_AUDIO_DEVICE", count, "default")
    audio_buffer_sizes_raw = _split_env_padded("QOBUZPROXY_AUDIO_BUFFER_SIZE", count, "2048")
    audio_fixed_raw = _split_env_padded("QOBUZPROXY_AUDIO_FIXED_VOLUME", count, "false")
    qualities_raw = _split_env_padded("QOBUZ_MAX_QUALITY", count, "27")

    speakers = []
    for i, name in enumerate(names):
        speaker = SpeakerConfig(
            name=name,
            device_type=parse_device_type(device_types[i]),
            backend_type=backend_types[i],
            max_quality=_parse_quality_value(qualities_raw[i]),
            http_port=int(http_ports_raw[i]),
            bind_address=config.server.bind_address,
            dlna_ip=dlna_ips[i],
            dlna_port=int(dlna_ports_raw[i]),
            dlna_fixed_volume=_parse_bool(dlna_fixed_volumes_raw[i]),
            dlna_gapless=parse_gapless_mode(dlna_gapless_raw[i]),
            proxy_port=int(proxy_ports_raw[i]),
            audio_device=audio_devices[i],
            audio_buffer_size=int(audio_buffer_sizes_raw[i]),
            audio_fixed_volume=_parse_bool(audio_fixed_raw[i]),
        )
        speakers.append(speaker)

    return speakers


def build_speaker_configs(
    config: Config, raw_yaml_speakers: Optional[list[dict]] = None
) -> list[SpeakerConfig]:
    """
    Build speaker configs from YAML, env vars, or flat config.

    Priority: YAML speakers > comma-separated env vars > single flat config.
    After building, assigns ports, generates UUIDs, and validates.
    """
    speakers: list[SpeakerConfig] = []

    if raw_yaml_speakers is not None:
        speakers = _parse_yaml_speakers(raw_yaml_speakers, config)
    else:
        env_speakers = _parse_env_speakers(config)
        if env_speakers:
            speakers = env_speakers
        else:
            # Only create a speaker from flat config if a backend is actually configured
            has_backend = config.backend.dlna.ip or config.backend.type == "local"
            if has_backend:
                speakers = [_single_speaker_from_config(config)]

    _assign_ports(speakers, webui_port=config.server.http_port)
    _generate_uuids(speakers)
    _validate_speakers(speakers)

    return speakers
