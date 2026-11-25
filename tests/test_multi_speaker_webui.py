"""Tests for QobuzProxy web UI callbacks: auth, speaker add/edit/remove and listing."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qobuz_proxy.app import QobuzProxy
from qobuz_proxy.config import Config
from tests.multi_speaker_helpers import _make_config, _make_mock_speaker, _make_speaker_config


class TestWebUICallbacks:
    """Tests for auth token submission and logout callbacks."""

    async def test_on_auth_token_success(self):
        """Successful token submission sets up client and starts speakers."""
        config = Config()
        config.speakers = [_make_speaker_config()]

        mock_speaker = MagicMock()
        mock_speaker.start = AsyncMock(return_value=True)
        mock_speaker.stop = AsyncMock()
        mock_speaker.name = "mock"

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.load_user_token", return_value=None),
            patch("qobuz_proxy.app.QobuzAPIClient"),
            patch("qobuz_proxy.app_speakers.Speaker", return_value=mock_speaker),
            patch("qobuz_proxy.app.save_user_token", return_value=True),
        ):
            app = QobuzProxy(config)
            await app.start()
            assert not app._auth_state["authenticated"]

            result = await app._on_auth_token("42", "valid_token")

            assert result is True
            assert app._auth_state["authenticated"] is True
            assert app._auth_state["user_id"] == "42"
            assert len(app._speakers) == 1

    async def test_concurrent_logins_start_speaker_once(self):
        """A second OAuth callback landing while the first login is still
        starting the speaker must not start it again: the duplicate would fail
        to bind the discovery port and leave a retry loop behind."""
        config = Config()
        config.speakers = [_make_speaker_config()]

        async def slow_start() -> bool:
            await asyncio.sleep(0.05)  # mDNS registration takes ~1-2 s for real
            return True

        first = _make_mock_speaker("Test Speaker", starts=True)
        first.start = AsyncMock(side_effect=slow_start)
        duplicate = _make_mock_speaker("Test Speaker", starts=False)

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.load_user_token", return_value=None),
            patch("qobuz_proxy.app.QobuzAPIClient"),
            patch("qobuz_proxy.app_speakers.Speaker", side_effect=[first, duplicate]),
            patch("qobuz_proxy.app.save_user_token", return_value=True),
        ):
            app = QobuzProxy(config)
            await app.start()

            await asyncio.gather(
                app._on_auth_token("42", "valid_token"),
                app._on_auth_token("42", "valid_token"),
            )

            assert app._speakers == [first]
            assert app._speaker_retry_tasks == {}
            duplicate.start.assert_not_awaited()

    async def test_on_logout_stops_speakers_and_clears_state(self):
        """Logout stops speakers and resets auth state."""
        config = _make_config(_make_speaker_config())

        mock_speaker = MagicMock()
        mock_speaker.start = AsyncMock(return_value=True)
        mock_speaker.stop = AsyncMock()
        mock_speaker.name = "mock"

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app_speakers.Speaker", return_value=mock_speaker),
            patch("qobuz_proxy.app.clear_user_token") as mock_clear,
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()
            assert app._auth_state["authenticated"] is True
            assert len(app._speakers) == 1

            await app._on_logout()

            assert app._auth_state["authenticated"] is False
            assert app._auth_state["user_id"] == ""
            assert len(app._speakers) == 0
            mock_speaker.stop.assert_called_once()
            mock_clear.assert_called_once()


class TestSpeakerEditCallbacks:
    """Tests for runtime add/edit speaker callbacks from the web UI."""

    async def test_start_speakers_cancelled_mid_start_releases_speakers(self):
        """Cancellation (shutdown, or a login racing the background token validation)
        must not leave half-started speakers holding their ports."""
        config = _make_config(
            _make_speaker_config("Fast", http_port=8689),
            _make_speaker_config("Slow", http_port=8690),
        )
        app = QobuzProxy(config)
        app._api_client = MagicMock()

        slow_started = asyncio.Event()

        async def slow_start():
            slow_started.set()
            await asyncio.Event().wait()  # never finishes on its own

        fast = _make_mock_speaker("Fast", starts=True)
        slow = _make_mock_speaker("Slow", starts=True)
        slow.start = AsyncMock(side_effect=slow_start)

        with patch("qobuz_proxy.app_speakers.Speaker", side_effect=[fast, slow]):
            task = asyncio.create_task(app._start_speakers())
            await slow_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        fast.stop.assert_awaited_once()
        slow.stop.assert_awaited_once()
        assert app._speakers == []

    async def test_edit_persists_config_when_restart_fails(self):
        """Edits are saved even if the speaker can't start (e.g. device offline).

        GitHub #53: the failed instance must not count as running, and the
        speaker keeps retrying so it comes up once the renderer is back.
        """
        config = _make_config(_make_speaker_config(name="Wyse", max_quality=6))
        old_speaker = MagicMock()
        old_speaker.name = "Wyse"
        old_speaker.stop = AsyncMock()
        old_speaker.start = AsyncMock(return_value=True)

        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = [old_speaker]

        new_speaker = MagicMock()
        new_speaker.name = "Wyse"
        new_speaker.start = AsyncMock(return_value=False)  # device unreachable

        with (
            patch("qobuz_proxy.app_speakers.Speaker", return_value=new_speaker),
            patch.object(app, "_save_config") as mock_save,
        ):
            result = await app._on_edit_speaker(
                "wyse", {"max_quality": 27, "dlna_ip": "192.168.1.60"}
            )

        assert config.speakers[0].max_quality == 27
        assert config.speakers[0].dlna_ip == "192.168.1.60"
        mock_save.assert_called_once()
        assert app._speakers == []
        assert result["status"] == "starting"
        assert "warning" in result
        retry = app._speaker_retry_tasks.get("wyse")
        assert retry is not None and not retry.done()
        assert {s["status"] for s in app._speakers_for_api()} == {"starting"}
        await app._stop_speakers()

    async def test_edit_toggles_fixed_volume(self):
        """The edit form exposes Fixed volume; the flag must round-trip and survive
        edits that don't mention it."""
        config = _make_config(_make_speaker_config(name="Moode", dlna_fixed_volume=False))
        old_speaker = MagicMock()
        old_speaker.name = "Moode"
        old_speaker.stop = AsyncMock()

        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = [old_speaker]

        new_speaker = MagicMock()
        new_speaker.name = "Moode"
        new_speaker.start = AsyncMock(return_value=True)
        new_speaker.stop = AsyncMock()
        new_speaker.get_status.return_value = {"id": "moode", "status": "idle"}

        with (
            patch("qobuz_proxy.app_speakers.Speaker", return_value=new_speaker) as mock_speaker_cls,
            patch.object(app, "_save_config"),
        ):
            await app._on_edit_speaker("moode", {"fixed_volume": True})
            assert config.speakers[0].dlna_fixed_volume is True
            assert mock_speaker_cls.call_args.kwargs["config"].dlna_fixed_volume is True

            # An edit that doesn't mention the flag keeps the current value
            await app._on_edit_speaker("moode", {"max_quality": 6})
            assert config.speakers[0].dlna_fixed_volume is True

            await app._on_edit_speaker("moode", {"fixed_volume": False})
            assert config.speakers[0].dlna_fixed_volume is False

    async def test_edit_sets_device_type(self):
        """The edit form's device type must round-trip and survive edits that omit it."""
        import pytest

        config = _make_config(_make_speaker_config(name="Moode"))
        old_speaker = MagicMock()
        old_speaker.name = "Moode"
        old_speaker.stop = AsyncMock()

        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = [old_speaker]

        new_speaker = MagicMock()
        new_speaker.name = "Moode"
        new_speaker.start = AsyncMock(return_value=True)
        new_speaker.stop = AsyncMock()
        new_speaker.get_status.return_value = {"id": "moode", "status": "idle"}

        with (
            patch("qobuz_proxy.app_speakers.Speaker", return_value=new_speaker) as mock_speaker_cls,
            patch.object(app, "_save_config"),
        ):
            await app._on_edit_speaker("moode", {"device_type": "headphones"})
            assert config.speakers[0].device_type == "headphones"
            assert mock_speaker_cls.call_args.kwargs["config"].device_type == "headphones"

            await app._on_edit_speaker("moode", {"max_quality": 6})
            assert config.speakers[0].device_type == "headphones"

            with pytest.raises(ValueError, match="Invalid device_type"):
                await app._on_edit_speaker("moode", {"device_type": "toaster"})
            assert config.speakers[0].device_type == "headphones"

    async def test_add_sets_device_type(self):
        config = _make_config()
        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = []

        new_speaker = MagicMock()
        new_speaker.start = AsyncMock(return_value=True)

        with (
            patch("qobuz_proxy.app_speakers.Speaker", return_value=new_speaker),
            patch.object(app, "_save_config"),
        ):
            await app._on_add_speaker(
                {"name": "Phones", "backend": "local", "device_type": "headphones"}
            )
        assert config.speakers[0].device_type == "headphones"

    async def test_add_rejects_duplicate_name_from_config(self):
        """A config entry whose speaker isn't running must still block the name."""
        import pytest

        config = _make_config(_make_speaker_config(name="Living Room"))
        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = []  # speaker failed to start, so it's not running

        with pytest.raises(ValueError, match="already exists"):
            await app._on_add_speaker(
                {"name": "Living Room", "backend": "dlna", "dlna_ip": "1.2.3.4"}
            )

    async def test_edit_rejects_rename_to_existing_name(self):
        import pytest

        config = _make_config(
            _make_speaker_config(name="Speaker A", http_port=8689),
            _make_speaker_config(name="Speaker B", http_port=8690),
        )
        speaker_a = MagicMock()
        speaker_a.name = "Speaker A"
        speaker_b = MagicMock()
        speaker_b.name = "Speaker B"

        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = [speaker_a, speaker_b]

        with pytest.raises(ValueError, match="already exists"):
            await app._on_edit_speaker("speaker-b", {"name": "Speaker A"})

    async def test_edit_uses_config_entry_matched_by_name(self):
        """Config entry must be matched by name, not by running-list index."""
        config = _make_config(
            _make_speaker_config(name="Speaker A", http_port=8689, max_quality=6),
            _make_speaker_config(name="Speaker B", http_port=8690, max_quality=6),
        )
        # Speaker A failed to start, so only B is running (index misalignment)
        speaker_b = MagicMock()
        speaker_b.name = "Speaker B"
        speaker_b.stop = AsyncMock()

        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = [speaker_b]

        new_speaker = MagicMock()
        new_speaker.name = "Speaker B"
        new_speaker.start = AsyncMock(return_value=True)
        new_speaker.get_status.return_value = {"id": "speaker-b", "status": "idle"}

        with (
            patch("qobuz_proxy.app_speakers.Speaker", return_value=new_speaker),
            patch.object(app, "_save_config"),
        ):
            await app._on_edit_speaker("speaker-b", {"max_quality": 27})

        assert config.speakers[1].max_quality == 27  # B updated
        assert config.speakers[0].max_quality == 6  # A untouched

    async def test_remove_deletes_config_entry_matched_by_name(self):
        """Config entry must be removed by name, not by running-list index.

        When an earlier speaker failed to start, the running list and the
        config list are misaligned; popping the config by the running index
        would delete the wrong speaker.
        """
        config = _make_config(
            _make_speaker_config(name="Speaker A", http_port=8689),
            _make_speaker_config(name="Speaker B", http_port=8690),
        )
        # Speaker A failed to start, so only B is running (index misalignment):
        # B is at running index 0 but config index 1.
        speaker_b = MagicMock()
        speaker_b.name = "Speaker B"
        speaker_b.stop = AsyncMock()

        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = [speaker_b]

        with patch.object(app, "_save_config"):
            await app._on_remove_speaker("speaker-b")

        # Only B removed; A's config must survive.
        assert [sc.name for sc in config.speakers] == ["Speaker A"]
        assert app._speakers == []
        speaker_b.stop.assert_called_once()

    async def test_remove_non_running_speaker(self):
        """A configured speaker that failed to start can still be removed."""
        config = _make_config(
            _make_speaker_config(name="Speaker A", http_port=8689),
            _make_speaker_config(name="Speaker B", http_port=8690),
        )
        # A failed to start; only B is running.
        speaker_b = MagicMock()
        speaker_b.name = "Speaker B"
        speaker_b.stop = AsyncMock()

        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = [speaker_b]

        with patch.object(app, "_save_config"):
            await app._on_remove_speaker("speaker-a")

        # A removed from config; B left untouched and still running.
        assert [sc.name for sc in config.speakers] == ["Speaker B"]
        assert app._speakers == [speaker_b]
        speaker_b.stop.assert_not_called()

    async def test_remove_unknown_speaker_raises(self):
        """Removing a speaker that isn't configured raises KeyError."""
        import pytest

        config = _make_config(_make_speaker_config(name="Speaker A"))
        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = []

        with pytest.raises(KeyError):
            await app._on_remove_speaker("nope")


class TestSpeakersApiListsConfig:
    def test_lists_configured_speakers_that_are_not_running(self):
        """After a restart, config-backed speakers must still appear in the API."""
        config = _make_config(
            _make_speaker_config(name="Living Room", http_port=8689),
            _make_speaker_config(name="Kitchen", http_port=8690),
        )
        running = MagicMock()
        running.name = "Kitchen"
        running.get_status.return_value = {
            "id": "kitchen",
            "name": "Kitchen",
            "backend": "dlna",
            "status": "idle",
            "config": {},
            "now_playing": None,
        }

        app = QobuzProxy(config)
        app._speakers = [running]

        speakers = app._speakers_for_api()
        by_id = {s["id"]: s for s in speakers}
        assert set(by_id) == {"living-room", "kitchen"}
        assert by_id["living-room"]["status"] == "disconnected"
        assert by_id["living-room"]["name"] == "Living Room"
        assert by_id["kitchen"]["status"] == "idle"

    def test_retrying_speaker_is_listed_as_starting(self):
        config = _make_config(_make_speaker_config(name="Office"))
        app = QobuzProxy(config)
        app._speakers = []
        app._speaker_retry_tasks["office"] = MagicMock()
        app._speaker_retry_tasks["office"].done.return_value = False

        speakers = app._speakers_for_api()
        assert speakers[0]["id"] == "office"
        assert speakers[0]["status"] == "starting"

    async def test_add_persists_when_start_fails(self):
        """A failed start must still write the speaker so a reboot can retry it."""
        config = _make_config()
        app = QobuzProxy(config)
        app._api_client = MagicMock()

        speaker = MagicMock()
        speaker.start = AsyncMock(return_value=False)
        speaker.get_status.return_value = {"id": "patio", "status": "disconnected"}

        with (
            patch("qobuz_proxy.app_speakers.Speaker", return_value=speaker),
            patch.object(app, "_save_config") as mock_save,
            patch.object(app, "_schedule_speaker_retry") as mock_retry,
        ):
            result = await app._on_add_speaker(
                {"name": "Patio", "backend": "dlna", "dlna_ip": "192.168.1.80"}
            )

        assert [sc.name for sc in config.speakers] == ["Patio"]
        assert config.speakers[0].uuid
        mock_save.assert_called_once()
        mock_retry.assert_called_once()
        assert app._speakers == []
        assert result["status"] == "starting"
        assert "warning" in result
