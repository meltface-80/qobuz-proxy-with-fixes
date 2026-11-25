"""Shared fixtures for the playback tests."""

from typing import Any

import pytest

from qobuz_proxy.playback.queue import QobuzQueue


@pytest.fixture
def queue() -> QobuzQueue:
    """Create a fresh queue."""
    return QobuzQueue()


@pytest.fixture
def sample_tracks() -> list[dict[str, Any]]:
    """Sample track data for testing."""
    return [
        {"queueItemId": 1, "trackId": "A"},
        {"queueItemId": 2, "trackId": "B"},
        {"queueItemId": 3, "trackId": "C"},
        {"queueItemId": 4, "trackId": "D"},
        {"queueItemId": 5, "trackId": "E"},
    ]
