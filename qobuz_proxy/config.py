"""
QobuzProxy Configuration System.

Priority order (highest to lowest):
1. Command-line arguments
2. Environment variables
3. Configuration file (YAML)
4. Default values

The data model lives in ``config_models``, speaker-list building in
``config_speakers`` and validation in ``config_validation``; every name stays
importable from this module.
"""

import logging
import os
import platform as platform  # Re-exported: tests patch qobuz_proxy.config.platform.node
from pathlib import Path
from typing import Any, Optional

import yaml

from qobuz_proxy.config_models import (
    AUTO_FALLBACK_QUALITY,
    AUTO_QUALITY,
    DEFAULT_DEVICE_TYPE,
    DEFAULT_HTTP_PORT,
    DEFAULT_PROXY_PORT,
    DEVICE_TYPES,
    GAPLESS_AUTO,
    GAPLESS_MODES,
    GAPLESS_OFF,
    GAPLESS_ON,
    VALID_LOG_LEVELS,
    VALID_QUALITIES,
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
    parse_device_type,
    parse_gapless_mode,
)
from qobuz_proxy.config_speakers import (
    build_speaker_configs,
    generate_speaker_uuid,
    slugify_name,
    speaker_component_config,
    speaker_config_to_dict,
)
from qobuz_proxy.config_validation import validate_config, validate_email, validate_port

# Private helpers kept importable from their historical location.
from qobuz_proxy.config_models import _parse_quality_value as _parse_quality_value
from qobuz_proxy.config_speakers import (
    _SPEAKER_UUID_NAMESPACE as _SPEAKER_UUID_NAMESPACE,
    _assign_ports as _assign_ports,
    _generate_uuids as _generate_uuids,
    _parse_env_speakers as _parse_env_speakers,
    _parse_yaml_speakers as _parse_yaml_speakers,
    _single_speaker_from_config as _single_speaker_from_config,
    _split_env as _split_env,
    _split_env_padded as _split_env_padded,
)
from qobuz_proxy.config_validation import _validate_speakers as _validate_speakers

__all__ = [
    "AUTO_FALLBACK_QUALITY",
    "AUTO_QUALITY",
    "DEFAULT_DEVICE_TYPE",
    "DEFAULT_HTTP_PORT",
    "DEFAULT_PROXY_PORT",
    "DEVICE_TYPES",
    "ENV_MAPPINGS",
    "GAPLESS_AUTO",
    "GAPLESS_MODES",
    "GAPLESS_OFF",
    "GAPLESS_ON",
    "VALID_LOG_LEVELS",
    "VALID_QUALITIES",
    "BackendConfig",
    "Config",
    "ConfigError",
    "DeviceConfig",
    "DLNAConfig",
    "LocalConfig",
    "LoggingConfig",
    "QobuzConfig",
    "ServerConfig",
    "SpeakerConfig",
    "build_speaker_configs",
    "dict_to_config",
    "generate_speaker_uuid",
    "load_config",
    "load_env_config",
    "load_yaml_config",
    "merge_configs",
    "parse_device_type",
    "parse_gapless_mode",
    "slugify_name",
    "speaker_component_config",
    "speaker_config_to_dict",
    "validate_config",
    "validate_email",
    "validate_port",
]

logger = logging.getLogger(__name__)

# Environment variable mappings
ENV_MAPPINGS = {
    # Qobuz
    "QOBUZ_EMAIL": ("qobuz", "email"),
    "QOBUZ_AUTH_TOKEN": ("qobuz", "auth_token"),
    "QOBUZ_USER_ID": ("qobuz", "user_id"),
    "QOBUZ_PASSWORD": ("qobuz", "auth_token"),  # Deprecated alias
    "QOBUZ_MAX_QUALITY": ("qobuz", "max_quality"),
    # Device
    "QOBUZPROXY_DEVICE_NAME": ("device", "name"),
    "QOBUZPROXY_DEVICE_TYPE": ("device", "device_type"),
    # DLNA
    "QOBUZPROXY_DLNA_IP": ("backend", "dlna", "ip"),
    "QOBUZPROXY_DLNA_PORT": ("backend", "dlna", "port"),
    "QOBUZPROXY_DLNA_FIXED_VOLUME": ("backend", "dlna", "fixed_volume"),
    "QOBUZPROXY_DLNA_GAPLESS": ("backend", "dlna", "gapless"),
    # Backend type
    "QOBUZPROXY_BACKEND": ("backend", "type"),
    # Local audio
    "QOBUZPROXY_AUDIO_DEVICE": ("backend", "local", "device"),
    "QOBUZPROXY_AUDIO_BUFFER_SIZE": ("backend", "local", "buffer_size"),
    "QOBUZPROXY_AUDIO_FIXED_VOLUME": ("backend", "local", "fixed_volume"),
    # Server
    "QOBUZPROXY_HTTP_PORT": ("server", "http_port"),
    "QOBUZPROXY_PROXY_PORT": ("backend", "dlna", "proxy_port"),
    # Logging
    "QOBUZPROXY_LOG_LEVEL": ("logging", "level"),
}


def load_yaml_config(path: Path) -> dict:
    """
    Load configuration from YAML file.

    Args:
        path: Path to YAML file

    Returns:
        Configuration dictionary

    Raises:
        ConfigError: If file cannot be read or parsed
    """
    if not path.exists():
        logger.debug(f"Config file not found: {path}")
        return {}

    try:
        with open(path) as f:
            data = yaml.safe_load(f)
            return data if data else {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Error parsing YAML config: {e}")
    except IOError as e:
        raise ConfigError(f"Error reading config file: {e}")


def _set_nested(d: dict, path: tuple, value: Any) -> None:
    """Set a nested dictionary value using a path tuple."""
    for key in path[:-1]:
        d = d.setdefault(key, {})
    d[path[-1]] = value


def load_env_config() -> dict:
    """
    Load configuration from environment variables.

    Returns:
        Configuration dictionary with values from environment
    """
    result: dict = {}

    for env_var, path in ENV_MAPPINGS.items():
        value: Any = os.environ.get(env_var)
        if value is not None:
            # Multi-speaker device types are split and validated by
            # _parse_env_speakers(), rather than treated as one flat value.
            if env_var == "QOBUZPROXY_DEVICE_TYPE" and "," in os.environ.get(
                "QOBUZPROXY_DEVICE_NAME", ""
            ):
                continue
            # Handle max_quality specially to support "auto"
            if env_var == "QOBUZ_MAX_QUALITY":
                if value.lower() == "auto":
                    value = AUTO_QUALITY
                else:
                    try:
                        value = int(value)
                    except ValueError:
                        logger.warning(f"Invalid value for {env_var}: {value}")
                        continue
            # Convert other numeric values
            elif env_var in (
                "QOBUZPROXY_DLNA_PORT",
                "QOBUZPROXY_HTTP_PORT",
                "QOBUZPROXY_PROXY_PORT",
                "QOBUZPROXY_AUDIO_BUFFER_SIZE",
            ):
                try:
                    value = int(value)
                except ValueError:
                    logger.warning(f"Invalid integer for {env_var}: {value}")
                    continue
            # Convert boolean values
            elif env_var in ("QOBUZPROXY_DLNA_FIXED_VOLUME", "QOBUZPROXY_AUDIO_FIXED_VOLUME"):
                value = _parse_bool(value)

            # Set nested value
            _set_nested(result, path, value)

    return result


def _deep_merge(base: dict, override: dict) -> None:
    """Recursively merge override into base."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def merge_configs(*configs: dict) -> dict:
    """
    Deep merge multiple configuration dictionaries.
    Later configs override earlier ones.
    """
    result: dict = {}
    for config in configs:
        _deep_merge(result, config)
    return result


def dict_to_config(d: dict) -> Config:
    """Convert a dictionary to Config dataclass."""
    config = Config()

    # Qobuz
    if "qobuz" in d:
        q = d["qobuz"]
        config.qobuz.email = q.get("email", config.qobuz.email)
        config.qobuz.auth_token = q.get("auth_token", config.qobuz.auth_token)
        config.qobuz.user_id = q.get("user_id", config.qobuz.user_id)

        # Accept "password" as deprecated alias for "auth_token"
        if "password" in q and "auth_token" not in q:
            logger.warning(
                "Config key 'password' is deprecated and will be removed "
                "in a future release. Use 'auth_token' instead."
            )
            config.qobuz.auth_token = q["password"]

        max_quality = q.get("max_quality", config.qobuz.max_quality)
        # Handle "auto" string from YAML
        if isinstance(max_quality, str) and max_quality.lower() == "auto":
            config.qobuz.max_quality = AUTO_QUALITY
        else:
            config.qobuz.max_quality = max_quality

    # Device
    if "device" in d:
        dev = d["device"]
        config.device.name = dev.get("name", config.device.name)
        if dev.get("uuid"):
            config.device.uuid = dev["uuid"]
        if "device_type" in dev:
            config.device.device_type = parse_device_type(dev["device_type"])

    # Backend
    if "backend" in d:
        b = d["backend"]
        config.backend.type = b.get("type", config.backend.type)
        if "dlna" in b:
            dlna = b["dlna"]
            config.backend.dlna.ip = dlna.get("ip", config.backend.dlna.ip)
            config.backend.dlna.port = dlna.get("port", config.backend.dlna.port)
            config.backend.dlna.fixed_volume = dlna.get(
                "fixed_volume", config.backend.dlna.fixed_volume
            )
            config.backend.dlna.proxy_port = dlna.get("proxy_port", config.backend.dlna.proxy_port)
            config.backend.dlna.description_url = dlna.get(
                "description_url", config.backend.dlna.description_url
            )
            config.backend.dlna.gapless = parse_gapless_mode(
                dlna.get("gapless", config.backend.dlna.gapless)
            )
            config.backend.dlna.udn = str(dlna.get("udn", config.backend.dlna.udn) or "")
        if "local" in b:
            local = b["local"]
            config.backend.local.device = local.get("device", config.backend.local.device)
            config.backend.local.buffer_size = local.get(
                "buffer_size", config.backend.local.buffer_size
            )
            config.backend.local.fixed_volume = _parse_bool(
                local.get("fixed_volume", config.backend.local.fixed_volume)
            )

    # Server
    if "server" in d:
        s = d["server"]
        config.server.http_port = s.get("http_port", config.server.http_port)
        config.server.bind_address = s.get("bind_address", config.server.bind_address)

    # Logging
    if "logging" in d:
        config.logging.level = d["logging"].get("level", config.logging.level)

    return config


def load_config(
    config_path: Optional[Path] = None,
    cli_args: Optional[dict] = None,
) -> Config:
    """
    Load configuration from all sources.

    Priority (highest to lowest):
    1. CLI arguments
    2. Environment variables
    3. Config file
    4. Defaults

    Args:
        config_path: Path to YAML config file
        cli_args: Dictionary of CLI arguments

    Returns:
        Merged Config object

    Raises:
        ConfigError: If configuration is invalid
    """
    # Start with empty dict (defaults come from dataclasses)
    configs = []
    raw_yaml_speakers: Optional[list[dict]] = None

    # Resolve config path: explicit > ./config.yaml > $QOBUZPROXY_DATA_DIR/config.yaml
    if config_path is None:
        local = Path("./config.yaml")
        if local.is_file():
            config_path = local
        else:
            data_dir = os.environ.get("QOBUZPROXY_DATA_DIR")
            if data_dir:
                candidate = Path(data_dir) / "config.yaml"
                if candidate.is_file():
                    config_path = candidate

    # 1. Load from file (lowest priority of explicit configs)
    if config_path:
        file_config = load_yaml_config(config_path)
        if file_config:
            # Extract speakers key before merging into flat config
            raw_yaml_speakers = file_config.pop("speakers", None)
            configs.append(file_config)
            logger.debug(f"Loaded config from {config_path}")

    # 2. Load from environment
    env_config = load_env_config()
    if env_config:
        configs.append(env_config)
        logger.debug("Loaded config from environment variables")

    # 3. Load from CLI (highest priority)
    if cli_args:
        configs.append(cli_args)
        logger.debug("Loaded config from CLI arguments")

    # Merge all configs
    merged = merge_configs(*configs) if configs else {}

    # Convert to Config object (fills in defaults)
    config = dict_to_config(merged)

    # Remember where to persist config changes (for web UI speaker management).
    # When no file exists, prefer $QOBUZPROXY_DATA_DIR/config.yaml so Docker
    # users — whose working directory is not writable — get saves into the
    # mounted /data volume alongside credentials.json.
    if config_path:
        config.config_path = config_path
    else:
        data_dir = os.environ.get("QOBUZPROXY_DATA_DIR")
        if data_dir:
            config.config_path = Path(data_dir) / "config.yaml"
        else:
            config.config_path = Path("./config.yaml")

    # Build speaker configs
    config.speakers = build_speaker_configs(config, raw_yaml_speakers)

    # Validate only for single-speaker (non-multi) mode; skip when no speakers
    # (empty config is valid — speakers can be added via the web UI)
    device_name_env = os.environ.get("QOBUZPROXY_DEVICE_NAME", "")
    is_multi = raw_yaml_speakers is not None or "," in device_name_env
    if not is_multi and config.speakers:
        validate_config(config)

    return config
