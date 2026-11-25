"""
Backend factory and registry.

Provides factory methods to instantiate backends by type name.
"""

import logging
from typing import Optional

from qobuz_proxy.config import Config

from .base import AudioBackend
from .dlna import DLNABackend

logger = logging.getLogger(__name__)


class BackendNotFoundError(Exception):
    """Raised when requested backend type is not available."""

    pass


class BackendRegistry:
    """
    Registry of available backend types.

    Backends register themselves here with their type name.
    Factory uses this to instantiate backends.
    """

    _backends: dict[str, type[AudioBackend]] = {}

    @classmethod
    def register(cls, type_name: str, backend_class: type[AudioBackend]) -> None:
        """Register a backend class."""
        cls._backends[type_name] = backend_class
        logger.debug(f"Registered backend type: {type_name}")

    @classmethod
    def get(cls, type_name: str) -> Optional[type[AudioBackend]]:
        """Get backend class by type name."""
        return cls._backends.get(type_name)

    @classmethod
    def available_types(cls) -> list[str]:
        """Get list of registered backend type names."""
        return list(cls._backends.keys())


class BackendFactory:
    """
    Factory for creating audio backend instances.

    Usage:
        backend = await BackendFactory.create_from_config(config)
    """

    @classmethod
    async def create_from_config(cls, config: Config) -> AudioBackend:
        """Create a backend based on configuration."""
        backend_type = config.backend.type

        # Check if type is available
        backend_class = BackendRegistry.get(backend_type)
        if not backend_class:
            available = BackendRegistry.available_types()
            raise BackendNotFoundError(
                f"Backend type '{backend_type}' not available. Available types: {available}"
            )

        # Dispatch to type-specific factory method
        if backend_type == "dlna":
            dlna = config.backend.dlna
            bind_address = config.server.bind_address
            description_url = dlna.description_url or None
            if not description_url:
                description_url = await cls._discover_description_url(
                    dlna.ip, dlna.port or 1400, dlna.udn, bind_address
                )
            return await cls.create_dlna(
                ip=dlna.ip,
                port=dlna.port or 1400,
                fixed_volume=dlna.fixed_volume,
                description_url=description_url,
                gapless=dlna.gapless,
                udn=dlna.udn,
                bind_address=bind_address,
            )
        elif backend_type == "local":
            return await cls.create_local(
                device=config.backend.local.device,
                buffer_size=config.backend.local.buffer_size,
            )
        else:
            # Generic instantiation for registered backends
            return backend_class(name=f"{backend_type} Backend")

    @classmethod
    async def create_dlna(
        cls,
        ip: str,
        port: int = 1400,
        fixed_volume: bool = False,
        name: Optional[str] = None,
        description_url: Optional[str] = None,
        gapless: str = "auto",
        udn: str = "",
        bind_address: str = "0.0.0.0",
    ) -> AudioBackend:
        """
        Create a DLNA backend.

        Args:
            ip: DLNA device IP address
            port: DLNA device port (default 1400 for Sonos)
            fixed_volume: If True, ignore volume commands
            name: Display name (auto-detected if not provided)
            description_url: Full URL to UPnP device description XML
            gapless: "auto", "on" or "off" (see backends/dlna/quirks.py)
            udn: The renderer's UDN, to find it again if its IP changed
            bind_address: Interface address for SSDP searches

        Returns:
            Connected DLNABackend instance

        Raises:
            BackendNotFoundError: If connection fails
        """
        backend = DLNABackend(
            ip=ip,
            port=port,
            fixed_volume=fixed_volume,
            name=name,
            description_url=description_url,
            gapless=gapless,
            udn=udn,
            bind_address=bind_address,
        )
        if await backend.connect():
            return backend
        raise BackendNotFoundError(f"Failed to connect to DLNA device at {ip}:{port}")

    @classmethod
    async def _discover_description_url(
        cls, target_ip: str, target_port: int, udn: str = "", bind_address: str = "0.0.0.0"
    ) -> Optional[str]:
        """Find a renderer's description URL by SSDP.

        Prefers the configured UDN (several renderers can share one host),
        then the same IP and port, then the same IP.
        """
        from qobuz_proxy.backends.dlna.discovery import DLNADiscovery

        try:
            devices = await DLNADiscovery(bind_address=bind_address).discover(timeout=3.0)
        except Exception as e:
            logger.debug(f"SSDP discovery failed: {e}")
            return None
        candidates = [d for d in devices if d.location]
        match = (
            next((d for d in candidates if udn and d.udn == udn), None)
            or next((d for d in candidates if d.ip == target_ip and d.port == target_port), None)
            or next((d for d in candidates if d.ip == target_ip), None)
        )
        if match is None:
            logger.debug(f"SSDP discovery did not find device at {target_ip}")
            return None
        logger.info(f"Auto-discovered description URL for {target_ip}: {match.location}")
        return match.location

    @classmethod
    async def create_local(
        cls,
        device: str = "default",
        buffer_size: int = 2048,
        name: Optional[str] = None,
    ) -> AudioBackend:
        """Create a local audio backend."""
        # Lazy import to avoid requiring sounddevice for DLNA users
        from qobuz_proxy.backends.local import LocalAudioBackend

        backend = LocalAudioBackend(
            device=device,
            buffer_size=buffer_size,
            name=name or "Local Audio",
        )
        if await backend.connect():
            return backend
        raise BackendNotFoundError("Failed to initialize local audio backend")

    @classmethod
    def list_available_backends(cls) -> list[str]:
        """List available backend types."""
        return BackendRegistry.available_types()


# Register backends
BackendRegistry.register("dlna", DLNABackend)

# Register local backend (lazy - import only when used)
try:
    from qobuz_proxy.backends.local import LocalAudioBackend

    BackendRegistry.register("local", LocalAudioBackend)
except ImportError:
    pass  # sounddevice not installed
