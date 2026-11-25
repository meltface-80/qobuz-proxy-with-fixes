"""Fixtures for backend tests."""

import pytest

from qobuz_proxy.backends.dlna import backend as backend_mod
from qobuz_proxy.backends.dlna.quirks import QuirkStore
from tests.backends.dlna_helpers import Clock


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(backend_mod.time, "monotonic", c)
    return c


@pytest.fixture
def store(tmp_path):
    return QuirkStore(tmp_path / "dlna_quirks.json")
