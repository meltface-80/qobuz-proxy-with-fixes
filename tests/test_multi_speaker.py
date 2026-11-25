"""Tests for multi-speaker orchestration and auth lifecycle in QobuzProxy."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from qobuz_proxy.app import QobuzProxy
from qobuz_proxy.config import Config, load_config
from tests.multi_speaker_helpers import _make_config, _make_mock_speaker, _make_speaker_config


class TestStartupPersistsSpeakerUuids:
    @pytest.mark.parametrize("missing_uuid", [None, ""])
    async def test_existing_config_survives_hostname_change(self, tmp_path, missing_uuid):
        path = tmp_path / "config.yaml"
        original = {
            "qobuz": {"auth_token": "saved-token", "user_id": "12345"},
            "server": {"http_port": 9000, "bind_address": "127.0.0.1"},
            "custom_setting": "keep-me",
            "speakers": [
                {
                    "name": "Kitchen",
                    "backend": "dlna",
                    "dlna_ip": "192.0.2.1",
                    "http_port": 9001,
                    "proxy_port": 7200,
                    "bind_address": "127.0.0.1",
                },
                {"name": "Office", "backend": "local", "uuid": "existing-uuid"},
            ],
        }
        if missing_uuid is not None:
            original["speakers"][0]["uuid"] = missing_uuid
        path.write_text(yaml.safe_dump(original))
        with patch("qobuz_proxy.config.platform.node", return_value="container-a"):
            config = load_config(path)
        generated_uuid = config.speakers[0].uuid
        app = QobuzProxy(config)
        with (
            patch.object(app, "_start_web_server", new_callable=AsyncMock),
            patch.object(app, "_stop_web_server", new_callable=AsyncMock),
            patch.object(app, "_authenticate", new_callable=AsyncMock, return_value=False),
        ):
            try:
                await app.start()
                # Persist even when authentication fails and speakers never start.
                assert app._speakers == []
            finally:
                await app.stop()

        original["speakers"][0]["uuid"] = generated_uuid
        assert yaml.safe_load(path.read_text()) == original
        with patch("qobuz_proxy.config.platform.node", return_value="container-b"):
            restarted = load_config(path)
        assert [sc.uuid for sc in restarted.speakers] == [generated_uuid, "existing-uuid"]

    async def test_existing_uuid_does_not_rewrite_file(self, tmp_path):
        path = tmp_path / "config.yaml"
        content = (
            "# Keep this comment and formatting on subsequent starts\n"
            "speakers:\n  - name: Office\n    backend: local\n    uuid: existing-uuid\n"
        )
        path.write_text(content)
        app = QobuzProxy(load_config(path))
        with (
            patch.object(app, "_start_web_server", new_callable=AsyncMock),
            patch.object(app, "_stop_web_server", new_callable=AsyncMock),
            patch.object(app, "_get_token_from_config_or_cache", return_value=None),
            patch("qobuz_proxy.webui.config_writer.os.replace") as replace,
        ):
            try:
                await app.start()
            finally:
                await app.stop()
        replace.assert_not_called()
        assert path.read_text() == content

    async def test_failed_write_leaves_config_intact_and_startup_available(self, tmp_path, caplog):
        path = tmp_path / "config.yaml"
        content = "speakers:\n  - name: Office\n    backend: local\n"
        path.write_text(content)
        app = QobuzProxy(load_config(path))
        with (
            patch.object(app, "_start_web_server", new_callable=AsyncMock),
            patch.object(app, "_stop_web_server", new_callable=AsyncMock),
            patch.object(app, "_get_token_from_config_or_cache", return_value=None),
            patch("qobuz_proxy.webui.config_writer.os.replace", side_effect=PermissionError),
        ):
            try:
                await app.start()
                assert app.is_running
            finally:
                await app.stop()
        assert path.read_text() == content
        assert list(tmp_path.glob("*.tmp")) == []
        assert "Failed to persist speaker UUIDs" in caplog.text


class TestMultiSpeakerOrchestration:
    async def test_starts_multiple_speakers(self):
        """Two speakers constructed, both start() called, app is running."""
        sc1 = _make_speaker_config("Living Room", http_port=8689)
        sc2 = _make_speaker_config("Bedroom", http_port=8690)
        config = _make_config(sc1, sc2)

        mock_speaker_instances = [MagicMock(), MagicMock()]
        for m in mock_speaker_instances:
            m.start = AsyncMock(return_value=True)
            m.stop = AsyncMock()
            m.name = "mock"

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch(
                "qobuz_proxy.app_speakers.Speaker", side_effect=mock_speaker_instances
            ) as MockSpeaker,
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()

            assert MockSpeaker.call_count == 2
            for instance in mock_speaker_instances:
                instance.start.assert_called_once()
            assert app.is_running

    async def test_continues_when_one_speaker_fails(self):
        """One speaker succeeds, one returns False -> app still running with one speaker."""
        sc1 = _make_speaker_config("Living Room", http_port=8689)
        sc2 = _make_speaker_config("Bedroom", http_port=8690)
        config = _make_config(sc1, sc2)

        good = MagicMock()
        good.start = AsyncMock(return_value=True)
        good.stop = AsyncMock()
        good.name = "Living Room"

        bad = MagicMock()
        bad.start = AsyncMock(return_value=False)
        bad.stop = AsyncMock()
        bad.name = "Bedroom"

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app_speakers.Speaker", side_effect=[good, bad]),
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()

            assert app.is_running
            assert len(app._speakers) == 1
            assert app._speakers[0] is good

    async def test_no_speakers_started_still_running(self):
        """All speakers fail -> app stays running (waiting for auth or retry)."""
        sc1 = _make_speaker_config("Living Room", http_port=8689)
        config = _make_config(sc1)

        bad = MagicMock()
        bad.start = AsyncMock(return_value=False)
        bad.stop = AsyncMock()
        bad.name = "Living Room"

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app_speakers.Speaker", side_effect=[bad]),
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()

            # App stays running even if speakers failed (no RuntimeError)
            assert app.is_running
            assert len(app._speakers) == 0

    async def test_failed_speaker_retries_in_background(self):
        """A speaker that fails at boot is retried and eventually joins the running list."""
        sc1 = _make_speaker_config("Living Room")
        config = _make_config(sc1)

        bad = _make_mock_speaker("Living Room", starts=False)
        still_bad = _make_mock_speaker("Living Room", starts=False)
        good = _make_mock_speaker("Living Room", starts=True)

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app_speakers.Speaker", side_effect=[bad, still_bad, good]),
            patch("qobuz_proxy.app_speakers.SPEAKER_RETRY_DELAYS_SECONDS", (0.01,)),
            patch("qobuz_proxy.app_speakers.SPEAKER_RETRY_STEADY_DELAY_SECONDS", 0.01),
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()

            assert app._speakers == []
            retry_task = app._speaker_retry_tasks["living-room"]

            await asyncio.wait_for(retry_task, timeout=2.0)

            assert app._speakers == [good]
            assert app._speaker_retry_tasks == {}
            still_bad.start.assert_awaited_once()
            good.start.assert_awaited_once()

    async def test_retry_stops_once_speaker_is_running(self):
        """A retry finding its speaker already running (started by another path,
        e.g. a second login) ends instead of starting a duplicate whose port
        bind can never succeed."""
        sc1 = _make_speaker_config("Living Room")
        config = _make_config(sc1)

        bad = _make_mock_speaker("Living Room", starts=False)
        running = _make_mock_speaker("Living Room", starts=True)
        duplicate = _make_mock_speaker("Living Room", starts=True)

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app_speakers.Speaker", side_effect=[bad, duplicate]),
            patch("qobuz_proxy.app_speakers.SPEAKER_RETRY_DELAYS_SECONDS", (0.01,)),
            patch("qobuz_proxy.app_speakers.SPEAKER_RETRY_STEADY_DELAY_SECONDS", 0.01),
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()
            retry_task = app._speaker_retry_tasks["living-room"]

            app._speakers.append(running)
            await asyncio.wait_for(retry_task, timeout=2.0)

            assert app._speakers == [running]
            assert app._speaker_retry_tasks == {}
            duplicate.start.assert_not_awaited()

    async def test_stop_cancels_pending_retries(self):
        """Shutting down cancels background speaker retries."""
        sc1 = _make_speaker_config("Living Room")
        config = _make_config(sc1)

        bad = _make_mock_speaker("Living Room", starts=False)

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app_speakers.Speaker", side_effect=[bad]),
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()

            retry_task = app._speaker_retry_tasks["living-room"]
            await app.stop()

            assert app._speaker_retry_tasks == {}
            await asyncio.sleep(0)
            assert retry_task.cancelled()

    async def test_stop_stops_all_speakers(self):
        """After a successful start, stop() calls stop() on all started speakers."""
        sc1 = _make_speaker_config("Living Room", http_port=8689)
        sc2 = _make_speaker_config("Bedroom", http_port=8690)
        config = _make_config(sc1, sc2)

        mock_instances = []
        for name in ("Living Room", "Bedroom"):
            m = MagicMock()
            m.start = AsyncMock(return_value=True)
            m.stop = AsyncMock()
            m.name = name
            mock_instances.append(m)

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app_speakers.Speaker", side_effect=mock_instances),
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()
            await app.stop()

            for instance in mock_instances:
                instance.stop.assert_called_once()
            assert not app.is_running


class TestGracefulStartup:
    """Tests for the new graceful startup behavior."""

    async def test_starts_without_credentials(self):
        """App starts in waiting-for-auth state when no credentials in config or cache."""
        config = Config()  # No qobuz credentials
        config.speakers = [_make_speaker_config()]

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.load_user_token", return_value=None),
        ):
            app = QobuzProxy(config)
            await app.start()

            assert app.is_running
            assert app._auth_state["authenticated"] is False
            assert len(app._speakers) == 0

    async def test_starts_with_cached_token(self):
        """App picks up cached token and authenticates automatically."""
        config = Config()  # No config credentials
        config.speakers = [_make_speaker_config()]

        mock_speaker = MagicMock()
        mock_speaker.start = AsyncMock(return_value=True)
        mock_speaker.stop = AsyncMock()
        mock_speaker.name = "mock"

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch(
                "qobuz_proxy.app.load_user_token",
                return_value={
                    "user_id": "99",
                    "user_auth_token": "cached_tok",
                    "email": "cached@example.com",
                },
            ),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app_speakers.Speaker", return_value=mock_speaker),
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()

            assert app._auth_state["authenticated"] is True
            assert app._auth_state["user_id"] == "99"
            assert len(app._speakers) == 1

    async def test_config_token_takes_priority_over_cache(self):
        """Config credentials are used even if cache exists."""
        config = _make_config(_make_speaker_config())

        mock_speaker = MagicMock()
        mock_speaker.start = AsyncMock(return_value=True)
        mock_speaker.stop = AsyncMock()
        mock_speaker.name = "mock"

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.load_user_token") as mock_load_cache,
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app_speakers.Speaker", return_value=mock_speaker),
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()

            # Cache should not have been consulted
            mock_load_cache.assert_not_called()
            assert app._auth_state["authenticated"] is True
            assert app._auth_state["user_id"] == "12345"

    async def test_invalid_cached_token_enters_waiting(self):
        """When cached token is invalid, app enters waiting-for-auth state."""
        config = Config()
        config.speakers = [_make_speaker_config()]

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch(
                "qobuz_proxy.app.load_user_token",
                return_value={
                    "user_id": "99",
                    "user_auth_token": "bad_tok",
                    "email": "",
                },
            ),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=False)

            app = QobuzProxy(config)
            await app.start()

            assert app.is_running
            assert app._auth_state["authenticated"] is False
            assert len(app._speakers) == 0
