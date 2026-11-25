"""
Web UI speaker management for the QobuzProxy app: add, edit and remove
speakers at runtime, and reset what was learned about a renderer.

Every change is validated before anything is touched, and saved before it is
applied; a failed save is rolled back and reported to the web UI (GitHub #54).
"""

import asyncio
import contextlib
import logging
from typing import Any, Optional

from qobuz_proxy.config import (
    AUTO_QUALITY,
    VALID_QUALITIES,
    ConfigError,
    SpeakerConfig,
    _assign_ports,
    _generate_uuids,
    parse_device_type,
    parse_gapless_mode,
    slugify_name,
    validate_port,
)

from .app_speakers import ConfigSaveError, SpeakerLifecycleMixin, configured_speaker_status

logger = logging.getLogger(__name__)

BACKEND_TYPES = ("dlna", "local")


def _parse_quality(value: Any) -> int:
    if isinstance(value, str) and value.strip().lower() == "auto":
        return AUTO_QUALITY
    try:
        quality = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid max_quality: {value!r}") from None
    if quality not in VALID_QUALITIES:
        raise ValueError(f"Invalid max_quality: {value!r}")
    return quality


def _parse_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {field}: {value!r}") from None


def _parse_device_type(value: Any) -> str:
    try:
        return parse_device_type(value)
    except ConfigError as e:
        raise ValueError(str(e)) from e


def speaker_config_from_body(body: dict, old: Optional[SpeakerConfig] = None) -> SpeakerConfig:
    """Build and validate a speaker config from a web UI request body.

    ``old`` is the speaker being edited: fields the body leaves out keep
    their values. Raises ValueError (HTTP 400) for any invalid field, before
    the caller changes anything.
    """
    base = old or SpeakerConfig(name="", backend_type=str(body.get("backend", "dlna")))
    name = str(body.get("name", base.name) or "").strip()
    if not name:
        raise ValueError("name is required")
    backend_type = base.backend_type  # immutable on edit
    if backend_type not in BACKEND_TYPES:
        raise ValueError(f"Unknown backend '{backend_type}'")

    dlna_ip = str(body.get("dlna_ip", base.dlna_ip) or "").strip()
    dlna_port = _parse_int(body.get("dlna_port", base.dlna_port), "dlna_port")
    description_url = str(body.get("description_url", base.dlna_description_url) or "").strip()
    if backend_type == "dlna":
        if not dlna_ip:
            raise ValueError("dlna_ip is required for DLNA backend")
        if not validate_port(dlna_port):
            raise ValueError(f"Invalid dlna_port: {dlna_port}")
    buffer_size = _parse_int(body.get("buffer_size", base.audio_buffer_size), "buffer_size")
    if buffer_size <= 0:
        raise ValueError(f"Invalid buffer_size: {buffer_size}")

    # The renderer's UDN: from discovery on add; kept on edit unless the
    # address was changed (then the next connect learns the new one).
    address_same = old is not None and (dlna_ip, dlna_port, description_url) == (
        old.dlna_ip,
        old.dlna_port,
        old.dlna_description_url,
    )
    udn = str(body.get("dlna_udn", "") or "") or (base.dlna_udn if address_same else "")

    fixed_volume = body.get("fixed_volume")
    return SpeakerConfig(
        name=name,
        uuid=base.uuid,
        device_type=_parse_device_type(body.get("device_type", base.device_type)),
        backend_type=backend_type,
        max_quality=_parse_quality(
            body.get("max_quality", "auto" if old is None else base.max_quality)
        ),
        http_port=base.http_port,
        bind_address=base.bind_address,
        dlna_ip=dlna_ip,
        dlna_port=dlna_port,
        dlna_fixed_volume=(
            bool(fixed_volume)
            if fixed_volume is not None and backend_type == "dlna"
            else base.dlna_fixed_volume
        ),
        dlna_description_url=description_url,
        dlna_gapless=parse_gapless_mode(body.get("gapless", base.dlna_gapless)),
        dlna_udn=udn,
        proxy_port=base.proxy_port,
        audio_device=str(body.get("audio_device", base.audio_device) or "default"),
        audio_buffer_size=buffer_size,
        audio_fixed_volume=(
            bool(fixed_volume)
            if fixed_volume is not None and backend_type == "local"
            else base.audio_fixed_volume
        ),
    )


class SpeakerEditMixin(SpeakerLifecycleMixin):
    """Runtime speaker changes from the web UI; mixed into ``QobuzProxy``."""

    def _config_index(self, speaker_id: str) -> Optional[int]:
        """Index of a speaker's config entry, matched by name.

        The running list and the config list can be misaligned when some
        speakers failed to start, so running indexes never address the config.
        """
        return next(
            (
                i
                for i, sc in enumerate(self._config.speakers)
                if slugify_name(sc.name) == speaker_id
            ),
            None,
        )

    def _running_index(self, speaker_id: str) -> Optional[int]:
        return next(
            (i for i, s in enumerate(self._speakers) if slugify_name(s.name) == speaker_id), None
        )

    def _name_taken(self, name: str, except_index: Optional[int] = None) -> bool:
        target = slugify_name(name)
        return any(
            i != except_index and slugify_name(sc.name) == target
            for i, sc in enumerate(self._config.speakers)
        )

    async def _on_add_speaker(self, body: dict) -> dict:
        """Add a new speaker at runtime."""
        sc = speaker_config_from_body(body)
        # Check against the config: a speaker can exist in config without
        # running (its device is offline), and a duplicate name in the saved
        # config prevents the app from booting.
        if self._name_taken(sc.name):
            raise ValueError(f"Speaker '{sc.name}' already exists")
        new_id = slugify_name(sc.name)

        # Assign ports against every configured speaker, including ones that
        # failed to start — otherwise a hot-add can collide with their ports.
        _assign_ports(list(self._config.speakers) + [sc], webui_port=self._config.server.http_port)
        _generate_uuids([sc])

        # Persist first so a restart still has this speaker even if start fails.
        self._config.speakers.append(sc)
        try:
            self._save_config()
        except ConfigSaveError:
            self._config.speakers.remove(sc)
            raise

        speaker = self._new_speaker(sc)
        epoch = self._speakers_epoch
        self._starting_speaker_ids.add(new_id)
        try:
            started = await speaker.start()
        finally:
            self._starting_speaker_ids.discard(new_id)
        if started and await self._adopt_started_speaker(speaker, sc, epoch):
            return speaker.get_status()

        logger.warning(f"Speaker '{sc.name}' saved but failed to start — retrying in background")
        self._schedule_speaker_retry(sc)
        status = configured_speaker_status(sc, "starting")
        status["warning"] = "Configuration saved, but the speaker failed to start"
        return status

    async def _on_edit_speaker(self, speaker_id: str, body: dict) -> dict:
        """Edit a speaker at runtime (stop, reconfigure, restart).

        The new config is saved even when the speaker fails to restart —
        edits are often the fix for connectivity (e.g. a changed device IP).
        A failed restart keeps retrying in the background, so the speaker
        comes up on its own once the renderer is back (GitHub #53).
        """
        config_idx = self._config_index(speaker_id)
        if config_idx is None:
            raise KeyError(speaker_id)
        old_config = self._config.speakers[config_idx]
        new_config = speaker_config_from_body(body, old_config)
        new_id = slugify_name(new_config.name)
        if new_id != speaker_id and self._name_taken(new_config.name, config_idx):
            raise ValueError(f"Speaker '{new_config.name}' already exists")

        # The edited speaker replaces any pending retry of the old config.
        # Wait for an attempt in flight to unwind so its reservation is gone.
        retry_task = self._speaker_retry_tasks.pop(speaker_id, None)
        if retry_task is not None and not retry_task.done():
            retry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retry_task
            # Speakers may have been removed or renamed while waiting.
            config_idx = self._config_index(speaker_id)
            if config_idx is None:
                raise KeyError(speaker_id)
            if new_id != speaker_id and self._name_taken(new_config.name, config_idx):
                raise ValueError(f"Speaker '{new_config.name}' already exists")
        if {speaker_id, new_id} & self._starting_speaker_ids:
            # A login is starting it right now; a second instance would fight
            # it for the same ports.
            raise ValueError(f"Speaker '{old_config.name}' is starting; try again in a moment")

        # Persist first: the edit is saved even if the restart below fails.
        self._config.speakers[config_idx] = new_config
        try:
            self._save_config()
        except ConfigSaveError:
            self._config.speakers[config_idx] = old_config
            if retry_task is not None and self._running_index(speaker_id) is None:
                self._schedule_speaker_retry(old_config)
            raise

        epoch = self._speakers_epoch
        reserved = {speaker_id, new_id}
        self._starting_speaker_ids |= reserved
        try:
            speaker_idx = self._running_index(speaker_id)
            if speaker_idx is not None:
                await self._speakers.pop(speaker_idx).stop()
            new_speaker = self._new_speaker(new_config)
            started = await new_speaker.start()
        finally:
            self._starting_speaker_ids -= reserved

        if started:
            if await self._adopt_started_speaker(new_speaker, new_config, epoch):
                return new_speaker.get_status()
            return configured_speaker_status(new_config, "disconnected")

        # The failed instance is not kept as "running": nothing would ever
        # retry it. Retry in the background instead, like a failed boot.
        logger.warning(
            f"Speaker '{new_config.name}' failed to start with new config "
            "(configuration saved; retrying in background)"
        )
        if epoch == self._speakers_epoch:
            self._schedule_speaker_retry(new_config)
        status = configured_speaker_status(new_config, "starting")
        status["warning"] = "Configuration saved, but the speaker failed to start"
        return status

    async def _on_remove_speaker(self, speaker_id: str) -> None:
        """Remove a speaker at runtime."""
        config_idx = self._config_index(speaker_id)
        if config_idx is None:
            raise KeyError(speaker_id)

        removed = self._config.speakers.pop(config_idx)
        try:
            self._save_config()
        except ConfigSaveError:
            self._config.speakers.insert(config_idx, removed)
            raise

        self._cancel_speaker_retry(speaker_id)
        speaker_idx = self._running_index(speaker_id)
        if speaker_idx is not None:
            await self._speakers.pop(speaker_idx).stop()

    async def _on_forget_speaker_quirks(self, speaker_id: str) -> dict:
        """Forget learned renderer behaviour for a running speaker (re-detect it)."""
        speaker_idx = self._running_index(speaker_id)
        if speaker_idx is None:
            if self._config_index(speaker_id) is None:
                raise KeyError(speaker_id)
            raise ValueError("The speaker is not running")
        speaker = self._speakers[speaker_idx]
        forgotten = speaker.forget_learned_behaviour()
        status = speaker.get_status()
        status["forgotten"] = forgotten
        return status
