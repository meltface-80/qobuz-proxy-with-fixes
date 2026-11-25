"""Tests for speaker starts racing logout, login, retries and edits in QobuzProxy."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qobuz_proxy.app import QobuzProxy
from tests.multi_speaker_helpers import _make_config, _make_mock_speaker, _make_speaker_config


class TestSpeakerStartRaces:
    """Starts racing logout, retries and edits."""

    async def test_logout_during_start_releases_speaker(self):
        """A speaker that finishes starting after a logout must be released,
        not kept running with no logged-in user."""
        config = _make_config(_make_speaker_config())
        app = QobuzProxy(config)
        app._api_client = MagicMock()

        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_start() -> bool:
            started.set()
            await release.wait()
            return True

        speaker = _make_mock_speaker("Test Speaker", starts=True)
        speaker.start = AsyncMock(side_effect=slow_start)

        with (
            patch("qobuz_proxy.app_speakers.Speaker", return_value=speaker),
            patch("qobuz_proxy.app.clear_user_token"),
        ):
            task = asyncio.create_task(app._start_speakers())
            await started.wait()
            await app._on_logout()
            release.set()
            await task

        assert app._speakers == []
        speaker.stop.assert_awaited_once()
        assert app._starting_speaker_ids == set()

    async def test_slow_retry_does_not_block_other_speakers(self):
        """A retry stuck waiting on an unreachable renderer must not delay a
        login from starting the other speakers."""
        config = _make_config(
            _make_speaker_config("Kitchen", http_port=8689),
            _make_speaker_config("Office", http_port=8690),
        )
        app = QobuzProxy(config)
        app._api_client = MagicMock()

        retry_started = asyncio.Event()

        async def hanging_start() -> bool:
            retry_started.set()
            await asyncio.Event().wait()
            return False

        hanging = _make_mock_speaker("Kitchen", starts=False)
        hanging.start = AsyncMock(side_effect=hanging_start)
        office = _make_mock_speaker("Office", starts=True)

        with (
            patch("qobuz_proxy.app_speakers.Speaker", side_effect=[hanging, office]),
            patch("qobuz_proxy.app_speakers.SPEAKER_RETRY_DELAYS_SECONDS", (0,)),
        ):
            app._schedule_speaker_retry(config.speakers[0])
            await retry_started.wait()

            await asyncio.wait_for(app._start_speakers(), timeout=1.0)

            assert app._speakers == [office]
            await app._stop_speakers()

        hanging.stop.assert_awaited()

    async def test_relogin_during_stale_start_restarts_speaker(self):
        """Start, logout, login again, then the first start finishes: the new
        login skipped the speaker because of the old reservation, so it must be
        retried rather than left stopped until the next login."""
        config = _make_config(_make_speaker_config())
        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._is_running = True

        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_start() -> bool:
            started.set()
            await release.wait()
            return True

        stale = _make_mock_speaker("Test Speaker", starts=True)
        stale.start = AsyncMock(side_effect=slow_start)
        fresh = _make_mock_speaker("Test Speaker", starts=True)

        with (
            patch("qobuz_proxy.app_speakers.Speaker", side_effect=[stale, fresh]),
            patch("qobuz_proxy.app.clear_user_token"),
            patch("qobuz_proxy.app.save_user_token", return_value=True),
            patch("qobuz_proxy.app.QobuzAPIClient"),
            patch("qobuz_proxy.app_speakers.SPEAKER_RETRY_DELAYS_SECONDS", (0,)),
        ):
            first = asyncio.create_task(app._start_speakers())
            await started.wait()
            await app._on_logout()
            await app._on_auth_token("42", "valid_token")
            release.set()
            await first

            retry = app._speaker_retry_tasks["test-speaker"]
            await asyncio.wait_for(retry, timeout=1.0)

        stale.stop.assert_awaited_once()
        assert app._speakers == [fresh]

    async def test_edit_cancels_in_flight_retry(self):
        """Editing a speaker whose renderer is unreachable is the usual fix, and
        must not be refused because a retry attempt happens to be running."""
        config = _make_config(_make_speaker_config(name="Wyse"))
        app = QobuzProxy(config)
        app._api_client = MagicMock()

        retry_started = asyncio.Event()

        async def hanging_start() -> bool:
            retry_started.set()
            await asyncio.Event().wait()
            return False

        hanging = _make_mock_speaker("Wyse", starts=False)
        hanging.start = AsyncMock(side_effect=hanging_start)
        edited = _make_mock_speaker("Wyse", starts=True)
        edited.get_status = MagicMock(return_value={"name": "Wyse"})

        with (
            patch("qobuz_proxy.app_speakers.Speaker", side_effect=[hanging, edited]),
            patch("qobuz_proxy.app_speakers.SPEAKER_RETRY_DELAYS_SECONDS", (0,)),
            patch.object(QobuzProxy, "_save_config"),
        ):
            app._schedule_speaker_retry(config.speakers[0])
            await retry_started.wait()

            await app._on_edit_speaker("wyse", {"dlna_ip": "192.168.1.200"})

        hanging.stop.assert_awaited()
        assert app._speakers == [edited]
        assert app._config.speakers[0].dlna_ip == "192.168.1.200"

    async def test_login_right_after_logout_starts_retrying_speaker(self):
        """Logout cancels a retry attempt in flight; a login right after must
        not skip the speaker because the attempt still held its reservation."""
        config = _make_config(_make_speaker_config())
        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._is_running = True

        retry_started = asyncio.Event()

        async def hanging_start() -> bool:
            retry_started.set()
            await asyncio.Event().wait()
            return False

        hanging = _make_mock_speaker("Test Speaker", starts=False)
        hanging.start = AsyncMock(side_effect=hanging_start)
        fresh = _make_mock_speaker("Test Speaker", starts=True)

        with (
            patch("qobuz_proxy.app_speakers.Speaker", side_effect=[hanging, fresh]),
            patch("qobuz_proxy.app_speakers.SPEAKER_RETRY_DELAYS_SECONDS", (0,)),
            patch("qobuz_proxy.app.clear_user_token"),
            patch("qobuz_proxy.app.save_user_token", return_value=True),
            patch("qobuz_proxy.app.QobuzAPIClient"),
        ):
            app._schedule_speaker_retry(config.speakers[0])
            await retry_started.wait()
            await app._on_logout()
            await app._on_auth_token("42", "valid_token")

        hanging.stop.assert_awaited()
        assert app._speakers == [fresh]

    async def test_speaker_removed_while_starting_is_released(self):
        """Deleting a speaker while it starts must not leave it running with
        no config entry."""
        config = _make_config(_make_speaker_config())
        app = QobuzProxy(config)
        app._api_client = MagicMock()

        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_start() -> bool:
            started.set()
            await release.wait()
            return True

        speaker = _make_mock_speaker("Test Speaker", starts=True)
        speaker.start = AsyncMock(side_effect=slow_start)

        with (
            patch("qobuz_proxy.app_speakers.Speaker", return_value=speaker),
            patch.object(QobuzProxy, "_save_config"),
        ):
            task = asyncio.create_task(app._start_speakers())
            await started.wait()
            await app._on_remove_speaker("test-speaker")
            release.set()
            await task

        assert app._speakers == []
        speaker.stop.assert_awaited_once()

    async def test_edit_after_waiting_for_retry_finds_its_entry_again(self):
        """While an edit waits for a retry to unwind, another speaker can be
        removed and shift the config list; the edit must not overwrite a
        different speaker."""
        config = _make_config(
            _make_speaker_config("Kitchen", http_port=8689),
            _make_speaker_config("Office", http_port=8690),
            _make_speaker_config("Den", http_port=8691),
        )
        app = QobuzProxy(config)
        app._api_client = MagicMock()

        retry_started = asyncio.Event()
        removed = False

        async def hanging_start() -> bool:
            retry_started.set()
            await asyncio.Event().wait()
            return False

        async def stop_and_remove_kitchen() -> None:
            nonlocal removed
            if not removed:
                removed = True
                await app._on_remove_speaker("kitchen")

        hanging = _make_mock_speaker("Office", starts=False)
        hanging.start = AsyncMock(side_effect=hanging_start)
        hanging.stop = AsyncMock(side_effect=stop_and_remove_kitchen)
        edited = _make_mock_speaker("Office", starts=True)
        edited.get_status = MagicMock(return_value={"name": "Office"})

        with (
            patch("qobuz_proxy.app_speakers.Speaker", side_effect=[hanging, edited]),
            patch("qobuz_proxy.app_speakers.SPEAKER_RETRY_DELAYS_SECONDS", (0,)),
            patch.object(QobuzProxy, "_save_config"),
        ):
            app._schedule_speaker_retry(config.speakers[1])
            await retry_started.wait()
            await app._on_edit_speaker("office", {"dlna_ip": "192.168.1.200"})

        assert [sc.name for sc in app._config.speakers] == ["Office", "Den"]
        assert app._config.speakers[0].dlna_ip == "192.168.1.200"
        assert app._config.speakers[1].dlna_ip == "192.168.1.100"

    async def test_edit_refuses_speaker_that_is_starting(self):
        """Editing a speaker while another path starts it would run two
        instances on the same ports."""
        config = _make_config(_make_speaker_config(name="Wyse"))
        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._starting_speaker_ids.add("wyse")

        with (
            patch("qobuz_proxy.app_speakers.Speaker") as MockSpeaker,
            pytest.raises(ValueError, match="starting"),
        ):
            await app._on_edit_speaker("wyse", {"name": "Wyse"})

        MockSpeaker.assert_not_called()
