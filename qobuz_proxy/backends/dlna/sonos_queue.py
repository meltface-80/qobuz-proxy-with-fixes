"""
Sonos queue actions for the DLNA client.

Sonos shows track metadata in its own app only for queue playback, so the
backend plays Qobuz tracks through the Sonos queue instead of a bare
SetAVTransportURI.
"""

from typing import Dict, Optional

from .description import DLNADeviceInfo
from .soap import UPNP_AV_TRANSPORT, parse_xml_value
from .sonos_topology import zone_player_id


class SonosQueueMixin:
    """Queue actions; mixed into ``DLNAClient``, which provides the transport."""

    device_info: Optional[DLNADeviceInfo]

    async def _soap_action(
        self,
        url: str,
        service: str,
        action: str,
        args: Dict[str, str],
        max_retries: Optional[int] = None,
    ) -> Optional[str]:
        raise NotImplementedError

    async def set_av_transport_uri(self, url: str, metadata: str = "") -> bool:
        raise NotImplementedError

    async def play(self, speed: str = "1") -> bool:
        raise NotImplementedError

    async def _queue_action(self, action: str, args: Dict[str, str]) -> Optional[str]:
        if not self.device_info:
            return None
        return await self._soap_action(
            self.device_info.av_transport_url,
            UPNP_AV_TRANSPORT,
            action,
            {"InstanceID": "0", **args},
        )

    async def clear_queue(self) -> bool:
        """Clear the device playback queue."""
        return await self._queue_action("RemoveAllTracksFromQueue", {}) is not None

    async def add_uri_to_queue(self, url: str, metadata: str = "") -> Optional[int]:
        """
        Add a URI to the device playback queue.

        Returns:
            1-based queue position of the enqueued track, or None on failure
        """
        response = await self._queue_action(
            "AddURIToQueue",
            {
                "EnqueuedURI": url,
                "EnqueuedURIMetaData": metadata,
                "DesiredFirstTrackNumberEnqueued": "0",
                "EnqueueAsNext": "1",
            },
        )
        if response is None:
            return None

        track_nr = parse_xml_value(response, "FirstTrackNumberEnqueued")
        try:
            return int(track_nr) if track_nr else None
        except ValueError:
            return None

    async def remove_track_from_queue(self, track_nr: int) -> bool:
        """Remove the track at 1-based queue position ``track_nr``."""
        response = await self._queue_action(
            "RemoveTrackFromQueue", {"ObjectID": f"Q:0/{track_nr}", "UpdateID": "0"}
        )
        return response is not None

    async def play_from_queue(self, index: int = 0) -> bool:
        """Start playback from 0-based queue position ``index``."""
        if not self.device_info:
            return False

        # Set the queue as the active transport source (required by Sonos).
        # The queue URI names the root zone player, not the _MR sub-device.
        uid = zone_player_id(self.device_info.udn)
        if not await self.set_av_transport_uri(f"x-rincon-queue:{uid}#0", ""):
            return False

        seek = await self._queue_action("Seek", {"Unit": "TRACK_NR", "Target": str(index + 1)})
        if seek is None:
            return False
        return await self.play()
