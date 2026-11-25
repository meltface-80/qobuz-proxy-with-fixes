"""
Relaying one audio request from the Qobuz CDN to a renderer.

If the upstream connection dies mid-stream (the CDN occasionally aborts
long-running transfers with a short read), the relay reconnects with a Range
request from the last byte delivered instead of dropping the renderer's
stream mid-track.
"""

import asyncio
import logging
import time
import traceback
from typing import Awaitable, Callable, Dict, Optional

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout, web

from .proxy_http import (
    RegisteredTrack,
    dlna_headers,
    full_size,
    log_stream_end,
    parse_range_start,
    reaches_end_of_file,
)

logger = logging.getLogger(__name__)

STREAM_CHUNK_SIZE = 64 * 1024  # 64KB chunks
# Per-read timeout: fail fast when the CDN stalls mid-stream instead of hanging
# forever (the overall stream has no total timeout).
READ_TIMEOUT_SECONDS = 30
# Mid-stream reconnect settings: how many times to reconnect (with a Range resume)
# before giving up, and how long to back off between attempts.
MAX_UPSTREAM_RETRIES = 3
UPSTREAM_RETRY_DELAY_SECONDS = 0.5


class _UpstreamFailed(Exception):
    """The upstream answered with a status the relay cannot use."""


class StreamRelay:
    """Streams one registered track to one renderer request."""

    def __init__(
        self,
        request: web.Request,
        track: RegisteredTrack,
        proxy_key: str,
        refresh_url: Callable[[RegisteredTrack], Awaitable[bool]],
        report_failure: Callable[[str, bool], None],
        report_complete: Callable[[str], None],
    ):
        self._request = request
        self._track = track
        self._key = proxy_key
        self._refresh_url = refresh_url
        self._report_failure = report_failure
        self._report_complete = report_complete

        self._range_header = request.headers.get("Range")
        self._request_start = parse_range_start(self._range_header or "")
        self._response: Optional[web.StreamResponse] = None
        self._expected_bytes: Optional[int] = None
        self._status = 200
        self._content_range: Optional[str] = None
        self._bytes_sent = 0
        self._started = time.monotonic()
        self._expired_url_retry_done = False

    async def run(self) -> web.StreamResponse:
        """Relay the stream; returns the response sent (or an error response)."""
        if self._range_header:
            logger.debug(f"Proxying with Range: {self._range_header}")
        # No total timeout for streaming, but a per-read timeout so a stalled
        # CDN connection fails fast instead of hanging forever.
        timeout = ClientTimeout(total=None, connect=30, sock_read=READ_TIMEOUT_SECONDS)
        retries = 0
        while True:
            try:
                async with ClientSession(timeout=timeout) as session:
                    async with session.get(
                        self._track.qobuz_url, headers=self._upstream_headers()
                    ) as upstream:
                        outcome = await self._relay(upstream)
                if outcome == "retry":
                    continue
                return self._finish(completed=outcome == "done")
            except _UpstreamFailed as e:
                return self._fail(str(e), status=502)
            except asyncio.CancelledError:
                logger.debug(f"Stream cancelled for track {self._track.track_id}")
                raise
            except (ClientError, asyncio.TimeoutError) as e:
                retries += 1
                if retries > MAX_UPSTREAM_RETRIES:
                    logger.error(
                        f"Upstream failed for track {self._track.track_id} after "
                        f"{MAX_UPSTREAM_RETRIES} retries: {type(e).__name__}: {e}"
                    )
                    return self._fail(f"Upstream error: {e}", status=502, log_end=True)
                logger.warning(
                    f"Upstream connection lost for track {self._track.track_id} after "
                    f"{self._bytes_sent} bytes ({type(e).__name__}: {e}); "
                    f"reconnecting ({retries}/{MAX_UPSTREAM_RETRIES})"
                )
                await asyncio.sleep(UPSTREAM_RETRY_DELAY_SECONDS * retries)
                await self._refresh_url(self._track)
            except (ConnectionResetError, ConnectionError) as e:
                # Client disconnected - this is normal when Sonos probes or seeks
                logger.debug(
                    f"Client connection closed for track {self._track.track_id}: {type(e).__name__}"
                )
                if self._response is not None:
                    return self._response
                return web.Response(status=499, text="Client closed connection")
            except Exception as e:
                logger.error(
                    f"Proxy error for track {self._track.track_id}: {type(e).__name__}: {e}"
                )
                logger.debug(f"Full traceback: {traceback.format_exc()}")
                return self._fail(f"Proxy error: {e}", status=502)

    def _upstream_headers(self) -> Dict[str, str]:
        if self._bytes_sent:
            return {"Range": f"bytes={self._request_start + self._bytes_sent}-"}
        return {"Range": self._range_header} if self._range_header else {}

    async def _relay(self, upstream: ClientResponse) -> str:
        """Relay one upstream answer: "done", "dropped" (client left) or "retry"."""
        if upstream.status not in (200, 206):
            return await self._on_upstream_error(upstream.status)
        if self._bytes_sent and upstream.status != 206:
            # Upstream ignored our resume Range; restarting from byte 0 would
            # corrupt the audio stream.
            logger.error(
                f"Upstream ignored resume Range for track {self._track.track_id}; aborting stream"
            )
            self._report_failure(self._key, False)
            return "dropped"

        size = full_size(upstream.status, upstream.headers)
        if size:
            self._track.size = size
        if self._response is None:
            await self._send_headers(upstream)
        assert self._response is not None
        async for chunk in upstream.content.iter_chunked(STREAM_CHUNK_SIZE):
            try:
                await self._response.write(chunk)
            except (ConnectionResetError, ConnectionError):
                return "dropped"
            self._bytes_sent += len(chunk)
        await self._response.write_eof()
        return "done"

    async def _on_upstream_error(self, status: int) -> str:
        # An expired signed URL surfaces as an error *status* (401/403/410),
        # not an exception — refresh and retry once before failing.
        if (
            self._response is None
            and not self._expired_url_retry_done
            and status in (401, 403, 410)
        ):
            self._expired_url_retry_done = True
            logger.info(
                f"Upstream {status} for track {self._track.track_id} — URL likely expired; "
                "fetching a fresh URL and retrying"
            )
            if await self._refresh_url(self._track):
                return "retry"
        logger.warning(f"Upstream error for track {self._track.track_id}: {status}")
        raise _UpstreamFailed(f"Upstream error: {status}")

    async def _send_headers(self, upstream: ClientResponse) -> None:
        """First successful connection: send the response headers to the renderer."""
        status = 206 if upstream.status == 206 else 200
        headers: Dict[str, str] = {
            "Content-Type": self._track.content_type,
            "Accept-Ranges": "bytes",
            **dlna_headers(self._request),
        }
        cl = upstream.headers.get("Content-Length")
        if cl is None and status == 200 and self._track.size and not self._range_header:
            # The CDN chose chunked transfer: keep the length we learned
            # earlier, since some renderers never stop on a stream of unknown
            # length (they wait at its end for more data).
            cl = str(self._track.size)
        if cl is not None:
            headers["Content-Length"] = cl
            self._expected_bytes = int(cl) if cl.isdigit() else None
        if "Content-Range" in upstream.headers:
            headers["Content-Range"] = upstream.headers["Content-Range"]
            self._content_range = headers["Content-Range"]
        self._status = status
        logger.debug(f"Streaming track {self._track.track_id}, headers: {headers}")
        self._response = web.StreamResponse(status=status, headers=headers)
        await self._response.prepare(self._request)

    def _log_end(self, completed: bool) -> None:
        log_stream_end(
            track=self._track,
            bytes_sent=self._bytes_sent,
            expected_bytes=self._expected_bytes,
            elapsed=time.monotonic() - self._started,
            is_range=self._range_header is not None,
            completed=completed,
        )

    def _finish(self, completed: bool) -> web.StreamResponse:
        assert self._response is not None
        self._log_end(completed)
        if completed and reaches_end_of_file(
            self._status, self._content_range, self._expected_bytes, self._bytes_sent
        ):
            self._report_complete(self._key)
        return self._response

    def _fail(self, text: str, status: int, log_end: bool = False) -> web.StreamResponse:
        self._report_failure(self._key, self._response is None)
        if self._response is None:
            return web.Response(status=status, text=text)
        if log_end:
            self._log_end(completed=False)
        # Headers already sent — nothing more we can do
        return self._response
