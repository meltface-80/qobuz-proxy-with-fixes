"""
Speaker lifecycle for the QobuzProxy app: starting, background retries,
adopting started speakers, stopping, and the web UI's speaker list.
"""

import asyncio
import logging
from typing import Optional

from qobuz_proxy.auth import QobuzAPIClient
from qobuz_proxy.config import AUTO_QUALITY, Config, SpeakerConfig, slugify_name
from qobuz_proxy.speaker import Speaker
from qobuz_proxy.webui.config_writer import ConfigSaveError, save_config

logger = logging.getLogger(__name__)

# Backoff schedule for speakers that fail to start (renderer offline, boot
# races between containers). After the ramp, keep trying at a steady pace.
SPEAKER_RETRY_DELAYS_SECONDS: tuple[float, ...] = (5.0, 10.0, 20.0, 40.0, 60.0)
SPEAKER_RETRY_STEADY_DELAY_SECONDS: float = 300.0


def configured_speaker_status(sc: SpeakerConfig, status: str) -> dict:
    """API status for a config entry that is not currently running."""
    config_dict: dict = {
        "max_quality": "auto" if sc.max_quality == AUTO_QUALITY else sc.max_quality,
        "device_type": sc.device_type,
    }
    if sc.backend_type == "dlna":
        config_dict["dlna_ip"] = sc.dlna_ip
        config_dict["dlna_port"] = sc.dlna_port
        config_dict["description_url"] = sc.dlna_description_url
        config_dict["fixed_volume"] = sc.dlna_fixed_volume
        config_dict["gapless"] = sc.dlna_gapless
    elif sc.backend_type == "local":
        config_dict["audio_device"] = sc.audio_device
        config_dict["buffer_size"] = sc.audio_buffer_size
        config_dict["fixed_volume"] = sc.audio_fixed_volume
    return {
        "id": slugify_name(sc.name),
        "name": sc.name,
        "backend": sc.backend_type,
        "status": status,
        "config": config_dict,
        "now_playing": None,
    }


class SpeakerLifecycleMixin:
    """Starts, retries and stops speakers; mixed into QobuzProxy."""

    _config: Config
    _is_running: bool
    _api_client: Optional[QobuzAPIClient]
    _app_id: str
    _speakers: list[Speaker]
    _speaker_retry_tasks: dict[str, "asyncio.Task[None]"]
    _starting_speaker_ids: set[str]
    _speakers_epoch: int

    def _new_speaker(self, config: SpeakerConfig) -> Speaker:
        """A Speaker for config, sharing the app's API client."""
        assert self._api_client is not None
        return Speaker(config=config, api_client=self._api_client, app_id=self._app_id)

    def _save_config(self) -> None:
        """Persist the current config to its YAML file.

        Raises:
            ConfigSaveError: The file could not be written. Web UI edits let
                this reach the endpoint, so a lost change is reported rather
                than shown as saved (GitHub #54).
        """
        if self._config.config_path:
            try:
                save_config(self._config, self._config.config_path)
            except Exception as e:
                logger.error(f"Failed to save config: {e}")
                raise ConfigSaveError(f"Could not save the configuration: {e}") from e

    def _save_config_quietly(self) -> None:
        """Persist the config from a background task (failures are only logged)."""
        try:
            self._save_config()
        except ConfigSaveError:
            pass

    def _speakers_for_api(self) -> list[dict]:
        """Return every configured speaker, with live status when running.

        The web UI used to list only ``_speakers`` (successfully started
        instances). After a restart, config-backed speakers that had not yet
        come up looked deleted — while re-adding the same name failed because
        they were still in ``_config.speakers``.
        """
        running = {slugify_name(s.name): s.get_status() for s in self._speakers}
        retrying = {
            sid for sid, task in self._speaker_retry_tasks.items() if task and not task.done()
        }
        result: list[dict] = []
        for sc in self._config.speakers:
            sid = slugify_name(sc.name)
            if sid in running:
                result.append(running[sid])
            elif sid in retrying:
                result.append(configured_speaker_status(sc, "starting"))
            else:
                result.append(configured_speaker_status(sc, "disconnected"))
        return result

    async def _start_speakers(self) -> None:
        """Create and start Speaker instances from config.

        Speakers that fail to start (e.g. the renderer is still booting or
        temporarily unreachable) are retried in the background instead of
        being dropped until the next restart.
        """
        assert self._api_client is not None

        if not self._config.speakers:
            port = self._config.server.http_port
            logger.info(f"No speakers configured — add speakers at http://localhost:{port}")
            return

        configs = [sc for sc in self._config.speakers if not self._speaker_active(sc.name)]
        if not configs:
            return  # All running, or being started by another login or retry

        speakers = [self._new_speaker(sc) for sc in configs]

        epoch = self._speakers_epoch
        reserved = {slugify_name(sc.name) for sc in configs}
        self._starting_speaker_ids |= reserved
        tasks = [asyncio.ensure_future(s.start()) for s in speakers]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            # Cancelled mid-start (shutdown, or a web UI login/logout racing the
            # background token validation): let the starts unwind, then release
            # whatever came up so a later start doesn't find the ports taken.
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*[s.stop() for s in speakers], return_exceptions=True)
            raise
        finally:
            self._starting_speaker_ids -= reserved

        for sc, speaker, result in zip(configs, speakers, results):
            if isinstance(result, BaseException) or result is False:
                reason = f": {result}" if isinstance(result, BaseException) else ""
                if epoch != self._speakers_epoch:
                    self._retry_after_stale_start(sc)  # Only if logged in again
                    continue
                logger.warning(
                    f"Speaker '{speaker.name}' failed to start{reason} — retrying in background"
                )
                self._schedule_speaker_retry(sc)
            else:
                await self._adopt_started_speaker(speaker, sc, epoch)

        if epoch != self._speakers_epoch:
            return  # Stopped while starting (logout or shutdown)

        if not self._speakers:
            logger.error(
                "No speakers started successfully — retrying in background; "
                "check configuration and logs"
            )
            return

        names = ", ".join(s.name for s in self._speakers)
        port = self._config.server.http_port
        logger.info(f"qobuz-proxy ready — {len(self._speakers)} speaker(s): {names}")
        logger.info(f"Web UI: http://localhost:{port}")

    def _schedule_speaker_retry(self, config: SpeakerConfig) -> None:
        """Start (or keep) a background task retrying a failed speaker."""
        speaker_id = slugify_name(config.name)
        existing = self._speaker_retry_tasks.get(speaker_id)
        if existing and not existing.done():
            return
        self._speaker_retry_tasks[speaker_id] = asyncio.create_task(self._retry_speaker(config))

    def _cancel_speaker_retry(self, speaker_id: str) -> None:
        """Stop retrying a speaker (it was edited, removed, or is shutting down)."""
        task = self._speaker_retry_tasks.pop(speaker_id, None)
        if task:
            task.cancel()

    async def _retry_speaker(self, config: SpeakerConfig) -> None:
        """Retry starting a speaker with backoff until it comes up.

        Renderers routinely boot slower than qobuz-proxy (sibling Docker
        containers, powered-off devices), so a failed start must not drop
        the speaker until the next restart.
        """

        def retry_delay(attempt: int) -> float:
            if attempt < len(SPEAKER_RETRY_DELAYS_SECONDS):
                return SPEAKER_RETRY_DELAYS_SECONDS[attempt]
            return SPEAKER_RETRY_STEADY_DELAY_SECONDS

        speaker_id = slugify_name(config.name)
        attempt = 0
        try:
            while True:
                await asyncio.sleep(retry_delay(attempt))
                attempt += 1

                if self._api_client is None:
                    return  # Logged out — speakers restart on the next login

                if any(slugify_name(s.name) == speaker_id for s in self._speakers):
                    return  # Started meanwhile by another path (e.g. a second login)
                if speaker_id in self._starting_speaker_ids:
                    continue  # Another path is starting it; check again next round

                speaker = self._new_speaker(config)
                epoch = self._speakers_epoch
                self._starting_speaker_ids.add(speaker_id)
                try:
                    started = await speaker.start()
                except asyncio.CancelledError:
                    await speaker.stop()
                    raise
                except Exception as e:
                    logger.debug(f"Speaker '{config.name}' retry raised: {e}")
                    started = False
                finally:
                    self._starting_speaker_ids.discard(speaker_id)

                if started:
                    if await self._adopt_started_speaker(speaker, config, epoch):
                        logger.info(
                            f"Speaker '{config.name}' started after {attempt} retry attempt(s)"
                        )
                    return

                logger.info(
                    f"Speaker '{config.name}' still failing to start "
                    f"(attempt {attempt}) — next retry in {retry_delay(attempt):.0f}s"
                )
        finally:
            self._speaker_retry_tasks.pop(speaker_id, None)

    def _speaker_active(self, name: str) -> bool:
        """Whether a speaker is running or has a start in progress."""
        speaker_id = slugify_name(name)
        return speaker_id in self._starting_speaker_ids or any(
            slugify_name(s.name) == speaker_id for s in self._speakers
        )

    async def _adopt_started_speaker(
        self, speaker: Speaker, config: SpeakerConfig, epoch: int
    ) -> bool:
        """Add a just-started speaker to the running list.

        If the speakers were stopped while it was starting (logout, shutdown),
        or its config entry was removed or replaced meanwhile, it is stopped
        instead, so no speaker outlives a logout or its own config.
        """
        if not any(sc is config for sc in self._config.speakers):
            logger.info(f"Speaker '{speaker.name}' was removed or edited while starting; releasing")
            await speaker.stop()
            return False
        if epoch != self._speakers_epoch:
            logger.info(f"Speaker '{speaker.name}' came up after speakers were stopped; releasing")
            await speaker.stop()
            self._retry_after_stale_start(config)
            return False
        self._speakers.append(speaker)
        self._sync_renderer_location(speaker, config)
        return True

    def _sync_renderer_location(self, speaker: Speaker, config: SpeakerConfig) -> None:
        """Save the renderer's UDN, and its new address if it moved.

        The UDN lets a later start find the renderer again by SSDP after a
        DHCP change; a found address is saved so the next start goes there
        directly.
        """
        location = speaker.renderer_location()
        if not isinstance(location, dict) or config.backend_type != "dlna":
            return
        changed = False
        udn = location.get("udn", "")
        if udn and udn != config.dlna_udn:
            config.dlna_udn = udn
            changed = True
        ip, port = location.get("ip", ""), location.get("port", "")
        if ip and port.isdigit() and (ip, int(port)) != (config.dlna_ip, config.dlna_port):
            logger.info(f"Speaker '{config.name}': renderer now at {ip}:{port}; saving")
            config.dlna_ip, config.dlna_port = ip, int(port)
            if config.dlna_description_url:
                config.dlna_description_url = location.get("description_url", "")
            changed = True
        if changed:
            self._save_config_quietly()

    def _retry_after_stale_start(self, config: SpeakerConfig) -> None:
        """Restart a speaker whose start straddled a logout and a new login.

        The new login skipped it because this start still held its reservation,
        so without a retry it would stay stopped until the next login.
        """
        if self._api_client is None or not self._is_running:
            return  # Logged out or shutting down; the next login starts it
        if any(sc is config for sc in self._config.speakers):
            self._schedule_speaker_retry(config)

    async def _stop_speakers(self) -> None:
        """Stop all running speakers and any pending start retries.

        Starts still in flight (a login or a speaker edit) release their speaker
        when they finish, because the epoch changes here.
        """
        self._speakers_epoch += 1
        retries = list(self._speaker_retry_tasks.values())
        for task in retries:
            task.cancel()
        self._speaker_retry_tasks.clear()
        # Let cancelled attempts unwind (stop their half-started speaker and drop
        # their reservation) before returning, so a login right after a logout
        # doesn't skip a speaker whose old attempt still looks like it's starting.
        await asyncio.gather(*retries, return_exceptions=True)

        if self._speakers:
            await asyncio.gather(*[s.stop() for s in self._speakers], return_exceptions=True)
            self._speakers = []
