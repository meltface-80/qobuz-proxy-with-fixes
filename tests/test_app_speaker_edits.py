"""Speaker edits from the web UI: validation first, rollback on failed saves
(GitHub #54), and the renderer UDN / address saved after a start."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qobuz_proxy.app import QobuzProxy
from qobuz_proxy.app_speaker_edits import speaker_config_from_body
from qobuz_proxy.config import Config, QobuzConfig, SpeakerConfig
from qobuz_proxy.webui.config_writer import ConfigSaveError


def _speaker_config(name: str = "Desk", **kwargs) -> SpeakerConfig:
    defaults = dict(
        name=name,
        uuid=f"uuid-{name.lower()}",
        backend_type="dlna",
        http_port=8690,
        dlna_ip="192.168.1.100",
        proxy_port=7120,
    )
    defaults.update(kwargs)
    return SpeakerConfig(**defaults)


def _app(*speakers: SpeakerConfig) -> QobuzProxy:
    config = Config()
    config.qobuz = QobuzConfig(user_id="1", auth_token="t")
    config.speakers = list(speakers)
    app = QobuzProxy(config)
    app._api_client = MagicMock()
    return app


class TestBodyValidation:
    def test_edit_cannot_blank_the_dlna_ip(self):
        with pytest.raises(ValueError, match="dlna_ip"):
            speaker_config_from_body({"dlna_ip": ""}, _speaker_config())

    @pytest.mark.parametrize("quality", ["8", 999, "nope"])
    def test_invalid_quality(self, quality):
        with pytest.raises(ValueError, match="max_quality"):
            speaker_config_from_body({"max_quality": quality}, _speaker_config())

    def test_invalid_port(self):
        with pytest.raises(ValueError, match="dlna_port"):
            speaker_config_from_body({"dlna_port": 70000}, _speaker_config())

    def test_udn_kept_unless_the_address_changes(self):
        old = _speaker_config(dlna_udn="uuid:r1")
        assert speaker_config_from_body({"max_quality": 6}, old).dlna_udn == "uuid:r1"
        assert speaker_config_from_body({"dlna_ip": "10.0.0.9"}, old).dlna_udn == ""
        body = {"name": "New", "dlna_ip": "10.0.0.9", "dlna_udn": "uuid:found"}
        assert speaker_config_from_body(body).dlna_udn == "uuid:found"

    def test_fixed_volume_goes_to_the_speakers_backend(self):
        local = _speaker_config(backend_type="local", dlna_ip="")
        edited = speaker_config_from_body({"fixed_volume": True}, local)
        assert edited.audio_fixed_volume and not edited.dlna_fixed_volume
        assert edited.fixed_volume


class TestSaveFailures:
    async def test_invalid_edit_changes_nothing(self):
        sc = _speaker_config()
        app = _app(sc)
        with patch.object(app, "_save_config") as save:
            with pytest.raises(ValueError):
                await app._on_edit_speaker("desk", {"dlna_ip": ""})
        save.assert_not_called()
        assert app._config.speakers == [sc]

    async def test_failed_save_rolls_the_edit_back(self):
        sc = _speaker_config()
        app = _app(sc)
        running = MagicMock()
        running.name = "Desk"
        running.stop = AsyncMock()
        app._speakers = [running]
        with patch.object(app, "_save_config", side_effect=ConfigSaveError("ro")):
            with pytest.raises(ConfigSaveError):
                await app._on_edit_speaker("desk", {"max_quality": 6})
        assert app._config.speakers == [sc]
        assert app._speakers == [running]
        running.stop.assert_not_called()

    async def test_failed_save_rolls_an_add_back(self):
        app = _app()
        with patch.object(app, "_save_config", side_effect=ConfigSaveError("ro")):
            with pytest.raises(ConfigSaveError):
                await app._on_add_speaker({"name": "New", "dlna_ip": "10.0.0.1"})
        assert app._config.speakers == []

    async def test_failed_save_keeps_a_removed_speaker(self):
        sc = _speaker_config()
        app = _app(sc)
        with patch.object(app, "_save_config", side_effect=ConfigSaveError("ro")):
            with pytest.raises(ConfigSaveError):
                await app._on_remove_speaker("desk")
        assert app._config.speakers == [sc]

    def test_save_config_raises(self, tmp_path):
        app = _app(_speaker_config())
        app._config.config_path = tmp_path / "missing-dir" / "x" / "config.yaml"
        with patch("qobuz_proxy.app_speakers.save_config", side_effect=OSError("denied")):
            with pytest.raises(ConfigSaveError, match="denied"):
                app._save_config()
            app._save_config_quietly()  # background callers only log


class TestRendererLocation:
    def _started(self, location):
        speaker = MagicMock()
        speaker.name = "Desk"
        speaker.renderer_location.return_value = location
        return speaker

    async def test_udn_is_saved_after_first_start(self):
        sc = _speaker_config()
        app = _app(sc)
        speaker = self._started(
            {"ip": "192.168.1.100", "port": "1400", "description_url": "", "udn": "uuid:r1"}
        )
        with patch.object(app, "_save_config") as save:
            assert await app._adopt_started_speaker(speaker, sc, app._speakers_epoch)
        assert sc.dlna_udn == "uuid:r1"
        save.assert_called_once()

    async def test_moved_renderer_address_is_saved(self):
        sc = _speaker_config(dlna_udn="uuid:r1", dlna_description_url="http://192.168.1.100/d")
        app = _app(sc)
        speaker = self._started(
            {
                "ip": "192.168.1.150",
                "port": "49152",
                "description_url": "http://x/d",
                "udn": "uuid:r1",
            }
        )
        with patch.object(app, "_save_config"):
            await app._adopt_started_speaker(speaker, sc, app._speakers_epoch)
        assert (sc.dlna_ip, sc.dlna_port, sc.dlna_description_url) == (
            "192.168.1.150",
            49152,
            "http://x/d",
        )

    async def test_nothing_saved_when_nothing_changed(self):
        sc = _speaker_config(dlna_udn="uuid:r1")
        app = _app(sc)
        speaker = self._started(
            {"ip": "192.168.1.100", "port": "1400", "description_url": "", "udn": "uuid:r1"}
        )
        with patch.object(app, "_save_config") as save:
            await app._adopt_started_speaker(speaker, sc, app._speakers_epoch)
        save.assert_not_called()
