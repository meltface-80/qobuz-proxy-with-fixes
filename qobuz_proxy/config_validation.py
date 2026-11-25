"""
Configuration validation for the flat (single-speaker) config and speaker lists.
"""

import re

from qobuz_proxy.config_models import (
    VALID_LOG_LEVELS,
    VALID_QUALITIES,
    Config,
    ConfigError,
    SpeakerConfig,
)


def validate_email(email: str) -> bool:
    """Validate email format."""
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


def validate_port(port: int) -> bool:
    """Validate port number."""
    return 1 <= port <= 65535


def validate_config(config: Config) -> None:
    """
    Validate configuration.

    Raises:
        ConfigError: If configuration is invalid
    """
    errors = []

    # Qobuz credentials (optional — token-based auth may provide them later)
    if config.qobuz.email and not validate_email(config.qobuz.email):
        errors.append(f"Invalid email format: {config.qobuz.email}")

    if config.qobuz.max_quality not in VALID_QUALITIES:
        errors.append(
            f"Invalid max_quality: {config.qobuz.max_quality}. "
            f"Valid values: {sorted(VALID_QUALITIES)}"
        )

    # Backend
    if config.backend.type == "dlna":
        if not config.backend.dlna.ip:
            errors.append("DLNA IP address is required when backend type is 'dlna'")
        if not validate_port(config.backend.dlna.port):
            errors.append(f"Invalid DLNA port: {config.backend.dlna.port}")
        if not validate_port(config.backend.dlna.proxy_port):
            errors.append(f"Invalid proxy port: {config.backend.dlna.proxy_port}")
    elif config.backend.type == "local":
        if not (64 <= config.backend.local.buffer_size <= 16384):
            errors.append(
                f"Invalid buffer_size: {config.backend.local.buffer_size}. "
                f"Must be between 64 and 16384"
            )
    elif config.backend.type != "stub":
        errors.append(f"Unknown backend type: {config.backend.type}")

    # Server ports
    if not validate_port(config.server.http_port):
        errors.append(f"Invalid HTTP port: {config.server.http_port}")

    # Logging
    if config.logging.level.lower() not in VALID_LOG_LEVELS:
        errors.append(
            f"Invalid log level: {config.logging.level}. Valid values: {sorted(VALID_LOG_LEVELS)}"
        )

    if errors:
        raise ConfigError("Configuration validation failed:\n  - " + "\n  - ".join(errors))


def _validate_speakers(speakers: list[SpeakerConfig]) -> None:
    """
    Validate speaker list.

    Raises:
        ConfigError: If speakers list is empty, has duplicate names, or port conflicts.
    """
    if not speakers:
        return  # Empty is valid — speakers can be added at runtime via the web UI

    names = [s.name for s in speakers]
    if len(names) != len(set(names)):
        seen: set[str] = set()
        dupes: list[str] = []
        for n in names:
            if n in seen:
                dupes.append(n)
            else:
                seen.add(n)
        raise ConfigError(f"Duplicate speaker names: {dupes}")

    http_ports = [s.http_port for s in speakers]
    if len(http_ports) != len(set(http_ports)):
        raise ConfigError(f"HTTP port conflicts among speakers: {http_ports}")

    proxy_ports = [s.proxy_port for s in speakers if s.backend_type == "dlna" and s.proxy_port]
    if len(proxy_ports) != len(set(proxy_ports)):
        raise ConfigError(f"Proxy port conflicts among speakers: {proxy_ports}")

    # Per-speaker field validation
    errors = []
    for s in speakers:
        if s.backend_type == "dlna" and not s.dlna_ip:
            errors.append(f"Speaker '{s.name}': DLNA IP address is required")
        if s.backend_type not in ("dlna", "local", "stub"):
            errors.append(f"Speaker '{s.name}': unknown backend type '{s.backend_type}'")
        if s.http_port and not validate_port(s.http_port):
            errors.append(f"Speaker '{s.name}': invalid HTTP port {s.http_port}")
        if s.proxy_port and not validate_port(s.proxy_port):
            errors.append(f"Speaker '{s.name}': invalid proxy port {s.proxy_port}")
    if errors:
        raise ConfigError("Speaker validation failed:\n  - " + "\n  - ".join(errors))
