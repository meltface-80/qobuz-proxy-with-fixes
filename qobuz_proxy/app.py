"""
QobuzProxy Application.

Orchestrates authentication, the web UI, and per-speaker lifecycle.
The HTTP server starts first so users can submit credentials through the
web UI even before a valid Qobuz token is available.
"""

import asyncio
import contextlib
import logging
import os
import signal
from typing import Optional

from aiohttp import web

from qobuz_proxy import __commit__, __version__
from qobuz_proxy.auth import (
    QobuzAPIClient,
    TransientAuthError,
    clear_user_token,
    load_user_token,
    save_user_token,
)
from qobuz_proxy.auth.oauth import OAUTH_APP_ID, OAUTH_APP_SECRET
from qobuz_proxy.app_speaker_edits import SpeakerEditMixin
from qobuz_proxy.app_speakers import (  # noqa: F401 (re-exported)
    SPEAKER_RETRY_DELAYS_SECONDS,
    SPEAKER_RETRY_STEADY_DELAY_SECONDS,
    ConfigSaveError,
)
from qobuz_proxy.config import Config
from qobuz_proxy.speaker import Speaker
from qobuz_proxy.webui.config_writer import persist_speaker_uuids
from qobuz_proxy.webui.routes import register_routes

logger = logging.getLogger(__name__)


# Backoff for validating the saved token at startup. Qobuz's login endpoint
# can be slow or unreachable while the network is still coming up (Docker
# starting before DNS on a NAS/Pi). Retries run in the background so the
# proxy recovers on its own instead of waiting for a manual re-login.
AUTH_RETRY_DELAYS_SECONDS: tuple[float, ...] = (3.0, 8.0, 20.0, 40.0, 60.0)
AUTH_RETRY_STEADY_DELAY_SECONDS: float = 300.0


class QobuzProxy(SpeakerEditMixin):
    """
    Main QobuzProxy application.

    Starts the shared HTTP server (web UI + discovery routes) first, then
    attempts automatic authentication from config or cached tokens. If no
    valid credentials are available the app stays running in a
    "waiting-for-auth" state so the user can provide a token through the
    web UI.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._is_running = False
        self._shutdown_event = asyncio.Event()

        # Auth / API
        self._api_client: Optional[QobuzAPIClient] = None
        self._app_id: str = ""
        self._app_secret: str = ""
        # Task validating the saved token at startup (first attempt and any
        # retries). Also keeps a strong reference to the task.
        self._auth_retry_task: Optional[asyncio.Task[None]] = None

        # Auth state — shared with the web UI status endpoint via _web_app["auth_state"].
        # Always the *same* dict object so route handlers see live updates.
        self._auth_state: dict[str, object] = {
            "authenticated": False,
            "user_id": "",
            "email": "",
            "name": "",
            "avatar": "",
        }

        # Shared aiohttp application (web UI + per-speaker discovery routes)
        self._web_app: Optional[web.Application] = None
        self._web_runner: Optional[web.AppRunner] = None
        self._web_site: Optional[web.TCPSite] = None

        # Speakers
        self._speakers: list[Speaker] = []
        # Background retry tasks for speakers that failed to start, keyed by
        # slugified speaker name. Also keeps strong references to the tasks.
        self._speaker_retry_tasks: dict[str, asyncio.Task[None]] = {}
        # Speakers whose start is in progress, keyed by slugified name. Two web
        # UI logins, or a login and a background retry, would otherwise start
        # the same speaker twice and the second start would fail to bind its
        # discovery port. Checking and reserving happen without an await in
        # between, so no lock is needed and one slow renderer never holds up
        # the others.
        self._starting_speaker_ids: set[str] = set()
        # Bumped by _stop_speakers (logout, shutdown). A start that was in
        # flight when it changed releases its speaker instead of keeping it.
        self._speakers_epoch = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start web server, fetch app credentials, attempt auto-auth."""
        logger.info("Starting qobuz-proxy...")

        # 1. Start the HTTP server so the web UI is reachable immediately
        await self._start_web_server()

        # Persist identities before authentication or speaker startup, so an
        # offline renderer still keeps its UUID after a container recreation.
        if self._config.config_path:
            try:
                persist_speaker_uuids(self._config, self._config.config_path)
            except Exception as e:
                logger.warning(f"Failed to persist speaker UUIDs: {e}")

        # 2. Set Qobuz app credentials (desktop app OAuth + signing secret)
        self._app_id = OAUTH_APP_ID
        self._app_secret = OAUTH_APP_SECRET

        # 3. Attempt auto-auth from config or cache
        token_info = self._get_token_from_config_or_cache()
        if token_info:
            user_id = token_info["user_id"]
            auth_token = token_info["user_auth_token"]
            email = token_info.get("email", "")

            # Validation runs in a tracked task from the very first attempt so a
            # web UI login can supersede it at any point (see _cancel_auth_retry).
            # start() only waits for that first attempt to be handled.
            first_attempt_done = asyncio.Event()
            self._auth_retry_task = asyncio.create_task(
                self._validate_saved_token(user_id, auth_token, email, first_attempt_done)
            )
            await first_attempt_done.wait()

        if not self._auth_state["authenticated"]:
            port = self._config.server.http_port
            if self._auth_retry_task is not None:
                logger.info(
                    f"Saved token not validated yet — will keep trying; "
                    f"or re-authenticate at http://localhost:{port}"
                )
            else:
                logger.info(f"No valid credentials — visit http://localhost:{port} to authenticate")

        self._is_running = True

    async def stop(self) -> None:
        """Stop speakers, then the web server."""
        if not self._is_running:
            return

        self._is_running = False
        await self._cancel_auth_retry()
        await self._stop_speakers()
        await self._stop_web_server()
        logger.info("qobuz-proxy stopped")

    async def run(self) -> None:
        """Run until SIGINT / SIGTERM."""
        loop = asyncio.get_running_loop()

        def handle_signal() -> None:
            logger.info("Shutdown signal received")
            self._shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_signal)

        try:
            await self.start()
            await self._shutdown_event.wait()
        finally:
            await self.stop()

    @property
    def is_running(self) -> bool:
        """Return True if the application event loop is active."""
        return self._is_running

    # ------------------------------------------------------------------
    # Token helpers
    # ------------------------------------------------------------------

    def _get_token_from_config_or_cache(self) -> Optional[dict[str, str]]:
        """Return user credentials from config (highest priority) or cache."""
        # Config values take precedence
        if self._config.qobuz.auth_token and self._config.qobuz.user_id:
            return {
                "user_id": self._config.qobuz.user_id,
                "user_auth_token": self._config.qobuz.auth_token,
                "email": self._config.qobuz.email,
            }

        # Fall back to cached token
        cached = load_user_token()
        if cached:
            logger.info("Found cached user token")
            return cached

        return None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _authenticate(self, user_id: str, auth_token: str) -> bool:
        """Validate credentials against the Qobuz API.

        Returns True when Qobuz accepted the token (the API client is installed),
        False when Qobuz rejected it. Raises TransientAuthError when Qobuz could
        not be reached, so callers can retry instead of treating the token as bad.
        """
        if not self._app_id:
            logger.error("Cannot authenticate — app credentials not available")
            return False

        # Install the client only on success so an in-flight validation can't
        # clobber a client set up by a concurrent web UI login.
        client = QobuzAPIClient(self._app_id, self._app_secret)
        logger.info(f"Authenticating user {user_id}...")
        if await client.login_with_token(user_id=user_id, auth_token=auth_token):
            logger.info("Authentication successful")
            self._api_client = client
            return True

        logger.warning("Authentication failed — Qobuz rejected the saved token")
        return False

    async def _activate_auth(self, user_id: str, email: str) -> None:
        """Publish the validated identity to the web UI and bring the speakers up."""
        self._auth_state["user_id"] = user_id
        self._auth_state["email"] = email
        self._auth_state["authenticated"] = True
        await self._start_speakers()

    async def _validate_saved_token(
        self,
        user_id: str,
        auth_token: str,
        email: str,
        first_attempt_done: asyncio.Event,
    ) -> None:
        """Validate the saved token, retrying with backoff while Qobuz is unreachable.

        The first attempt runs immediately and `first_attempt_done` is set once
        its outcome has been handled, so `start()` can return without waiting on
        retries. Those continue in the background so the proxy comes up on its
        own once the network is reachable. Stops on the first definitive answer
        from Qobuz, or when the user authenticates via the web UI (or logs out)
        in the meantime.
        """

        def retry_delay(attempt: int) -> float:
            if attempt < len(AUTH_RETRY_DELAYS_SECONDS):
                return AUTH_RETRY_DELAYS_SECONDS[attempt]
            return AUTH_RETRY_STEADY_DELAY_SECONDS

        attempt = 0
        try:
            while True:
                if self._auth_state["authenticated"]:
                    return  # Web UI login happened in the meantime

                try:
                    valid = await self._authenticate(user_id, auth_token)
                except TransientAuthError as e:
                    delay = retry_delay(attempt)
                    if attempt == 0:
                        logger.warning(
                            f"Could not reach Qobuz to validate the saved token ({e}) — "
                            f"retrying in background, next attempt in {delay:.0f}s"
                        )
                    else:
                        logger.warning(
                            f"Still unable to reach Qobuz to validate the saved token "
                            f"(attempt {attempt + 1}: {e}) — next attempt in {delay:.0f}s"
                        )
                    first_attempt_done.set()
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue

                if valid:
                    if attempt:
                        logger.info(f"Saved token validated after {attempt} retry attempt(s)")
                    await self._activate_auth(user_id, email)
                else:
                    logger.warning("Cached/config token is invalid — waiting for auth via web UI")
                return
        finally:
            first_attempt_done.set()
            if self._auth_retry_task is asyncio.current_task():
                self._auth_retry_task = None

    async def _cancel_auth_retry(self) -> None:
        """Stop the background token validation (web UI login, logout, shutdown)."""
        task = self._auth_retry_task
        self._auth_retry_task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # ------------------------------------------------------------------
    # Web UI callbacks
    # ------------------------------------------------------------------

    async def _on_auth_token(
        self,
        user_id: str,
        auth_token: str,
        profile: dict[str, str] | None = None,
        **_kwargs: object,
    ) -> bool:
        """Called by the web UI when the user submits a token.

        Validates credentials, persists them to cache, and starts speakers
        if they are not already running.
        """
        if profile is None:
            profile = {}

        # A fresh login supersedes any pending validation of the old saved token
        await self._cancel_auth_retry()

        # Set up API client directly — token is pre-validated by OAuth
        # and scoped to OAUTH_APP_ID which we use for all requests.
        self._api_client = QobuzAPIClient(self._app_id, self._app_secret)
        self._api_client.user_auth_token = auth_token
        self._api_client.user_id = user_id

        email = profile.get("email", "")
        name = profile.get("name", "")
        avatar = profile.get("avatar", "")

        # Persist to cache
        save_user_token(user_id=user_id, auth_token=auth_token, email=email)

        # Update shared auth state
        self._auth_state["authenticated"] = True
        self._auth_state["user_id"] = user_id
        self._auth_state["email"] = email
        self._auth_state["name"] = name
        self._auth_state["avatar"] = avatar

        # Start speakers if not already running
        if not self._speakers:
            await self._start_speakers()

        return True

    async def _on_logout(self) -> None:
        """Called by the web UI when the user requests logout."""
        logger.info("Logout requested — stopping speakers and clearing token")
        await self._cancel_auth_retry()
        await self._stop_speakers()

        self._auth_state["authenticated"] = False
        self._auth_state["user_id"] = ""
        self._auth_state["email"] = ""
        self._api_client = None

        clear_user_token()

    # ------------------------------------------------------------------
    # Web server
    # ------------------------------------------------------------------

    async def _start_web_server(self) -> None:
        """Create the shared aiohttp app and start listening."""
        self._web_app = web.Application()

        # Expose state for route handlers
        self._web_app["auth_state"] = self._auth_state
        self._web_app["get_speakers"] = self._speakers_for_api
        self._web_app["version"] = __version__
        self._web_app["commit"] = __commit__
        self._web_app["http_port"] = self._config.server.http_port
        self._web_app["on_auth_token"] = self._on_auth_token
        self._web_app["on_logout"] = self._on_logout
        self._web_app["on_add_speaker"] = self._on_add_speaker
        self._web_app["on_edit_speaker"] = self._on_edit_speaker
        self._web_app["on_remove_speaker"] = self._on_remove_speaker
        self._web_app["on_forget_speaker_quirks"] = self._on_forget_speaker_quirks
        self._web_app["bind_address"] = self._config.server.bind_address
        self._web_app["local_audio_enabled"] = os.environ.get(
            "QOBUZPROXY_LOCAL_AUDIO_UI", ""
        ).lower() in ("true", "1", "yes")

        register_routes(self._web_app)

        self._web_runner = web.AppRunner(self._web_app, access_log=None)
        await self._web_runner.setup()
        self._web_site = web.TCPSite(
            self._web_runner,
            self._config.server.bind_address,
            self._config.server.http_port,
        )
        await self._web_site.start()
        logger.info(
            f"Web server listening on "
            f"{self._config.server.bind_address}:{self._config.server.http_port}"
        )

    async def _stop_web_server(self) -> None:
        """Shut down the shared aiohttp app."""
        if self._web_site:
            await self._web_site.stop()
        if self._web_runner:
            await self._web_runner.cleanup()
