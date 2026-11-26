"""
Per-renderer DLNA behaviour ("quirks").

GetProtocolInfo says which containers a renderer takes, but nothing about how
its transport behaves. Renderers differ in ways that decide whether gapless
playback and end-of-track detection work at all, so what qobuz-proxy assumes
about a renderer comes in layers, each overriding the one before it:

    default   what the UPnP AVTransport spec promises
    profile   a built-in table matched on manufacturer and model
    learned   what this renderer was seen to do, saved per UDN
    user      the speaker's ``gapless`` setting ("on" / "off"; "auto" defers)

Learned behaviour is saved to ``dlna_quirks.json`` in the data directory, so a
restart does not have to rediscover it (and replay the glitch that taught it).
Delete an entry to make qobuz-proxy probe that renderer again.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from qobuz_proxy.config import (  # noqa: F401 (re-exported)
    GAPLESS_AUTO,
    GAPLESS_MODES,
    GAPLESS_OFF,
    GAPLESS_ON,
    parse_gapless_mode,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelProfile:
    """What a renderer model is known to do that it cannot say for itself.

    Each behaviour is True / False when known, None when the spec default
    applies.
    """

    id: str
    label: str
    # Matched anywhere in the device's identity: manufacturer, model name,
    # friendly name and UDN. Vendors that ship a stock renderer keep its
    # manufacturer string (the FiiO K17 reports gmediarender's authors), so
    # the brand often only shows in the model name or UDN.
    brand: re.Pattern[str]
    # Matched against the model name or the friendly name.
    model: re.Pattern[str]
    # SetNextAVTransportURI queues the next track and moves on to it at the
    # end of the current one, as the spec says.
    gapless: Optional[bool] = None
    # Reports STOPPED (or moves on) at the end of a track.
    stops_at_end: Optional[bool] = None
    # Reports the queued URI as the current one as soon as it is queued,
    # while still playing the current track.
    reports_next_early: Optional[bool] = None
    notes: str = ""


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


PROFILES: tuple[ModelProfile, ...] = (
    ModelProfile(
        id="fiio-k17",
        label="FiiO K17",
        brand=_rx(r"fiio"),
        model=_rx(r"\bk17\b"),
        gapless=False,
        stops_at_end=False,
        notes=(
            "Tested on a K17 with hand-sent SOAP (Nov 2026). It describes itself as "
            "stock gmediarender (manufacturer 'Ivo Clarysse, Henner Zeller', "
            "modelName 'FIIO K17', UDN uuid:FiiORender-...) and fetches audio with "
            "ffmpeg. SetNextAVTransportURI replaces the playing track at once, even "
            "when sent 20 s into a track, and the queued URI survives Stop until an "
            "empty SetNextAVTransportURI clears it. At a track's end it stays "
            "PLAYING with RelTime parked at TrackDuration, with Content-Length known "
            "and no next track queued, and sends no STOPPED event either."
        ),
    ),
    ModelProfile(
        id="fiio-sr11",
        label="FiiO SR11",
        brand=_rx(r"fiio"),
        model=_rx(r"\bsr11\b"),
        gapless=True,
        reports_next_early=True,
        notes=(
            "Plays gaplessly, but names the queued track as current as soon as "
            "it is queued (seen in Mandarin). Stays PLAYING at the end of a "
            "stream whose length it does not know."
        ),
    ),
)


def profile_for(
    manufacturer: str, model: str, friendly_name: str = "", udn: str = ""
) -> Optional[ModelProfile]:
    """The built-in profile matching this renderer, if any."""
    identity = " ".join((manufacturer or "", model or "", friendly_name or "", udn or ""))
    for profile in PROFILES:
        if profile.brand.search(identity) and (
            profile.model.search(model or "") or profile.model.search(friendly_name or "")
        ):
            return profile
    return None


@dataclass
class DeviceQuirks:
    """Effective behaviour for one renderer, with where each value came from."""

    gapless: Optional[bool] = None
    stops_at_end: Optional[bool] = None
    reports_next_early: Optional[bool] = None
    sources: dict[str, str] = field(default_factory=dict)
    profile_id: Optional[str] = None

    def describe(self) -> str:
        parts = []
        for name in ("gapless", "stops_at_end", "reports_next_early"):
            value = getattr(self, name)
            if value is not None:
                parts.append(f"{name}={value} ({self.sources.get(name, '?')})")
        return ", ".join(parts) or "spec defaults"


_FIELDS = ("gapless", "stops_at_end", "reports_next_early")


def _default_store_path() -> Path:
    data_dir = os.environ.get("QOBUZPROXY_DATA_DIR")
    base = Path(data_dir) if data_dir else Path.home() / ".qobuz-proxy"
    return base / "dlna_quirks.json"


class QuirkStore:
    """Learned renderer behaviour, saved as JSON keyed by UDN."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or _default_store_path()
        self._entries: Optional[dict[str, dict]] = None

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[str, dict]:
        if self._entries is None:
            try:
                with open(self._path, encoding="utf-8") as f:
                    raw = json.load(f)
                self._entries = raw if isinstance(raw, dict) else {}
            except FileNotFoundError:
                self._entries = {}
            except (OSError, ValueError) as e:
                logger.warning(f"Ignoring unreadable DLNA quirk store {self._path}: {e}")
                self._entries = {}
        return self._entries

    def get(self, udn: str) -> dict:
        return dict(self._load().get(udn, {})) if udn else {}

    def learn(self, udn: str, name: str, value: bool, label: str = "") -> bool:
        """Record a behaviour; returns True when it changed what was stored."""
        if not udn or name not in _FIELDS:
            return False
        entries = self._load()
        entry = entries.setdefault(udn, {})
        if entry.get(name) == value:
            return False
        entry[name] = value
        if label:
            entry["label"] = label
        entry["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._save()
        return True

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".dlna_quirks.")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._entries, f, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except OSError as e:
            # Learning still applies for this run; it just won't survive a restart.
            logger.warning(f"Could not save DLNA quirk store {self._path}: {e}")


def resolve_quirks(
    udn: str,
    manufacturer: str,
    model: str,
    gapless_mode: str = GAPLESS_AUTO,
    store: Optional[QuirkStore] = None,
    friendly_name: str = "",
) -> DeviceQuirks:
    """Combine profile, learned and user layers for one renderer."""
    quirks = DeviceQuirks()
    profile = profile_for(manufacturer, model, friendly_name, udn)
    if profile:
        quirks.profile_id = profile.id
        for name in _FIELDS:
            value = getattr(profile, name)
            if value is not None:
                setattr(quirks, name, value)
                quirks.sources[name] = f"profile:{profile.id}"
    if store is not None:
        for name, value in store.get(udn).items():
            if name in _FIELDS and isinstance(value, bool):
                setattr(quirks, name, value)
                quirks.sources[name] = "learned"
    mode = parse_gapless_mode(gapless_mode)
    if mode != GAPLESS_AUTO:
        quirks.gapless = mode == GAPLESS_ON
        quirks.sources["gapless"] = "user"
    return quirks


# Shared store (one file for all speakers), like the capability cache.
default_store = QuirkStore()
